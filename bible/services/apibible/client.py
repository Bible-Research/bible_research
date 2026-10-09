"""Thin wrapper around the API.Bible HTTP API
(api.scripture.api.bible/v1)."""
import logging
import re
import threading
from typing import Any, Dict, List, Optional

import requests
from django.conf import settings

from bible.utils.provider_errors import PassageNotFoundError

logger = logging.getLogger(__name__)

APIBIBLE_BASE_URL = "https://api.scripture.api.bible/v1"

# Paragraph styles that mark a non-verse heading in API.Bible
# JSON content: s*/ms* = section headings, mr/r = parallel
# references, d = descriptive title, sp = speaker,
# qa = acrostic heading, cl = closure, lit = liturgical note.
# Keeping these out of verse text matters more than where they
# surface, so unknown-but-heading-styled paragraphs become
# ``headings`` entries instead of leaking into verse text.
_HEADING_STYLES = frozenset({
    "s", "s1", "s2", "s3", "s4",
    "ms", "ms1", "ms2", "mr", "d", "sp",
    "r", "qa", "cl", "lit",
})

# Trailing numeric component of a verse id like "JHN.3.16";
# ranged ids ("JHN.3.16-17") resolve to their first verse.
_VERSE_ID_RE = re.compile(r"\.(\d+)(?:-\d+)?$")


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


class ApiBibleClient:
    """HTTP client for API.Bible.

    Auth is an ``api-key`` header. The client is deliberately
    agnostic about *which* bible it reads — callers pass the
    API.Bible ``bible_id``/``audio_bible_id`` resolved from the
    registry so adding another API.Bible translation requires no
    client change.
    """

    def __init__(self, api_key=None):
        api_key = api_key or getattr(settings, "API_BIBLE_KEY", None)
        if not api_key:
            raise ValueError("API_BIBLE_KEY not configured.")
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({"api-key": api_key})

    def fetch_chapter_raw(
        self, bible_id: str, book_id: str, chapter: int
    ) -> Dict[str, Any]:
        """Return the raw API.Bible JSON for one chapter."""
        chapter_id = f"{book_id}.{chapter}"
        params = {
            "content-type": "json",
            "include-verse-numbers": "true",
            "include-verse-spans": "true",
            "include-titles": "true",
        }
        r = self.session.get(
            f"{APIBIBLE_BASE_URL}/bibles/{bible_id}"
            f"/chapters/{chapter_id}",
            params=params,
            timeout=10,
        )
        r.raise_for_status()
        return r.json()

    def get_chapter(
        self, bible_id: str, book_id: str, chapter: int
    ) -> Dict[str, Any]:
        """Return verses, headings and the FUMS ``meta`` block.

        Returns::

            {
                "verses": [
                    {"verse_start": 1, "verse_text": "..."},
                    ...
                ],
                "headings": [
                    {"before_verse": 3, "text": "..."},
                    ...
                ],
                "fums": {...},   # raw response ``meta`` block
            }

        Raises:
            PassageNotFoundError: when the chapter has no usable
                verse content.
            requests.HTTPError: on 4xx/5xx from API.Bible.
        """
        raw = self.fetch_chapter_raw(bible_id, book_id, chapter)
        data = raw.get("data") or {}
        parsed = _parse_chapter_content(data.get("content"))
        if not parsed["verses"]:
            raise PassageNotFoundError(
                f"No verses found for {book_id} {chapter} "
                f"in bible_id={bible_id}"
            )
        return {
            "verses": parsed["verses"],
            "headings": parsed["headings"],
            "fums": raw.get("meta") or {},
        }

    def get_chapter_audio(
        self, audio_bible_id: str, book_id: str, chapter: int
    ) -> Dict[str, Any]:
        """Return ``{audio_url, timecodes, fums}`` for a chapter.

        ``audio_url`` is API.Bible's presigned ``resourceUrl``
        (it expires — callers should not persist it long-term);
        ``timecodes`` is the optional verse→timestamp list used
        by the timestamps endpoint; ``fums`` is the raw response
        ``meta`` block for FUMS reporting.

        Raises:
            PassageNotFoundError: when no audio resource exists
                for the chapter.
            requests.HTTPError: on 4xx/5xx from API.Bible.
        """
        chapter_id = f"{book_id}.{chapter}"
        r = self.session.get(
            f"{APIBIBLE_BASE_URL}/audio-bibles/{audio_bible_id}"
            f"/chapters/{chapter_id}",
            timeout=10,
        )
        r.raise_for_status()
        payload = r.json() or {}
        data = payload.get("data") or {}
        url = data.get("resourceUrl")
        if not url:
            raise PassageNotFoundError(
                f"No audio found for {book_id} {chapter} "
                f"in audio_bible_id={audio_bible_id}"
            )
        return {
            "audio_url": url,
            "timecodes": data.get("timecodes") or [],
            "fums": payload.get("meta") or {},
        }

    def get_chapter_audio_url(
        self, audio_bible_id: str, book_id: str, chapter: int
    ) -> str:
        """Return the presigned MP3 ``resourceUrl`` for a chapter."""
        return self.get_chapter_audio(
            audio_bible_id, book_id, chapter
        )["audio_url"]

    def search(
        self,
        bible_id: str,
        query: str,
        offset: int = 0,
        limit: int = 50,
    ) -> Dict[str, Any]:
        """Search an API.Bible text for a word or phrase.

        API.Bible paginates with ``offset``/``limit`` rather than
        ``page`` — callers map page numbers onto ``offset``.

        Returns the raw API.Bible JSON: ``data.verses`` items
        carry ``id``/``orgId`` (e.g. ``JHN.3.16``), ``bookId``,
        ``chapterId``, ``reference`` and ``text``; ``data.total``
        is the provider-reported match count.
        """
        params = {
            "query": query,
            "limit": str(limit),
            "offset": str(offset),
        }
        r = self.session.get(
            f"{APIBIBLE_BASE_URL}/bibles/{bible_id}/search",
            params=params,
            timeout=10,
        )
        r.raise_for_status()
        return r.json()

    def get_bible(self, bible_id: str) -> Dict[str, Any]:
        """Return the API.Bible bible object (copyright etc.)."""
        r = self.session.get(
            f"{APIBIBLE_BASE_URL}/bibles/{bible_id}",
            timeout=10,
        )
        r.raise_for_status()
        return (r.json() or {}).get("data") or {}


def _verse_marker(item: Dict[str, Any]) -> "Optional[tuple]":
    """Classify an API.Bible verse marker node.

    Returns ``("start", verse_number)`` for verse-start markers,
    ``("end",)`` for verse-end markers, and ``None`` for
    non-verse nodes and unparseable ids.

    Recognised shapes include::

        {"type": "verse", "verseId": "JHN.3.16"}
        {"name": "verse",
         "attrs": {"number": "16", "sid": "JHN.3.16"}}
        {"name": "verse", "attrs": {"eid": "JHN.3.16"}}
    """
    is_verse = (
        item.get("type") == "verse" or item.get("name") == "verse"
    )
    if not is_verse:
        return None
    attrs = item.get("attrs") or {}
    if attrs.get("eid") or item.get("eid"):
        return ("end",)
    ref = (
        attrs.get("verseId")
        or item.get("verseId")
        or attrs.get("sid")
        or item.get("sid")
    )
    if ref is not None:
        # Range tails take two shapes — "JHN.3.16-17" and
        # "JHN.3.16-JHN.3.17" — so drop everything after the
        # first "-" before matching the trailing verse number.
        head = str(ref).split("-", 1)[0]
        match = _VERSE_ID_RE.search(head)
        if match:
            return ("start", int(match.group(1)))
        return None
    num = attrs.get("number") or item.get("number")
    try:
        # "number" can carry a span too ("16-17").
        return ("start", int(str(num).split("-")[0]))
    except (TypeError, ValueError):
        return None


def _is_heading(item: Dict[str, Any]) -> bool:
    """True for section-heading nodes in API.Bible JSON content."""
    if item.get("type") == "heading":
        return True
    name = item.get("name")
    attrs = item.get("attrs") or {}
    style = str(attrs.get("style") or "").lower()
    return name in ("para", "heading") and style in _HEADING_STYLES


def _collect_text(node: Any, out: List[str]) -> None:
    """Append every ``text`` value under ``node`` to ``out``."""
    if isinstance(node, dict):
        text = node.get("text")
        if isinstance(text, str):
            out.append(text)
        for key in ("items", "content"):
            _collect_text(node.get(key), out)
    elif isinstance(node, list):
        for child in node:
            _collect_text(child, out)


def _node_text(node: Any) -> str:
    """Concatenate every ``text`` value under ``node``."""
    texts: List[str] = []
    _collect_text(node, texts)
    return _normalise(" ".join(t for t in texts if t))


def _parse_chapter_content(content: Any) -> Dict[str, Any]:
    """Parse API.Bible structured ``data.content`` into verses and
    headings matching ``ESVClient.get_chapter_with_headings``.

    The content tree is a nested list of nodes: ``type: 'text'``
    leaves, verse start/end markers, heading paragraphs (style
    ``s``/``ms``/``d``/...), and generic container tags
    (``para``, ``chapter``, ...) whose children are walked in
    order.
    """
    verses: List[Dict[str, Any]] = []
    headings: List[Dict[str, Any]] = []
    pending_headings: List[str] = []
    # Text seen before the first verse marker. Some chapters emit
    # no marker for verse 1, so this preamble is real verse text.
    pre_buf: List[str] = []
    buf: List[str] = []
    current_verse: "Optional[int]" = None
    seen_first_marker = False

    def flush_verse() -> None:
        nonlocal current_verse, buf
        if current_verse is not None and buf:
            text = _normalise(" ".join(buf))
            if text:
                verses.append({
                    "verse_start": current_verse,
                    "verse_text": text,
                })
        buf = []

    def flush_headings(before_verse: int) -> None:
        nonlocal pending_headings
        if pending_headings:
            text = _normalise(" ".join(pending_headings))
            if text:
                headings.append({
                    "before_verse": before_verse,
                    "text": text,
                })
            pending_headings = []

    def claim_preamble(first: int) -> int:
        """Attribute pre-first-marker text to implicit verse 1.

        Some chapters emit no verse-1 marker; when the first
        discovered marker is >1 any collected preamble belongs to
        verse 1. Returns the verse number pending headings
        attach to (1 when a preamble was claimed).
        """
        if first <= 1:
            return first
        text = _normalise(" ".join(pre_buf))
        if not text:
            return first
        logger.warning(
            "API.Bible chapter content has no verse-1 marker; "
            "attributing pre-marker text to verse 1 "
            "(first marker: verse %s)",
            first,
        )
        verses.append({
            "verse_start": 1,
            "verse_text": text,
        })
        return 1

    def walk(node: Any) -> None:
        nonlocal current_verse, seen_first_marker
        items = node if isinstance(node, list) else [node]
        for item in items:
            if not isinstance(item, dict):
                continue
            marker = _verse_marker(item)
            if marker is not None:
                if marker[0] == "start":
                    flush_verse()
                    before = marker[1]
                    if not seen_first_marker:
                        seen_first_marker = True
                        before = claim_preamble(marker[1])
                    flush_headings(before)
                    current_verse = marker[1]
                    # Some payloads inline the verse text on the
                    # marker node itself.
                    text = item.get("text")
                    if isinstance(text, str) and text:
                        buf.append(text)
                else:
                    flush_verse()
                    current_verse = None
                continue
            if (
                item.get("type") == "verse"
                or item.get("name") == "verse"
            ):
                # A verse-shaped node whose id/number failed to
                # parse is skipped wholesale — descending into it
                # would leak its label and children into the
                # previous verse's text.
                continue
            if _is_heading(item):
                text = _node_text(item)
                if text:
                    pending_headings.append(text)
                continue
            if item.get("type") == "text":
                text = item.get("text")
                if current_verse is not None and text:
                    buf.append(text)
                elif not seen_first_marker and text:
                    pre_buf.append(text)
                continue
            # Generic container (para, chapter, ...) — descend.
            for key in ("items", "content"):
                child = item.get(key)
                if child is not None:
                    walk(child)

    walk(content)
    flush_verse()
    return {"verses": verses, "headings": headings}


_default_client: "ApiBibleClient | None" = None
_lock = threading.Lock()


def get_default_apibible_client() -> ApiBibleClient:
    global _default_client
    if _default_client is None:
        with _lock:
            if _default_client is None:
                _default_client = ApiBibleClient()
    return _default_client
