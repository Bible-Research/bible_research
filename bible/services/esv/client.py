"""Thin wrapper around the ESV HTTP API (api.esv.org/v3)."""
import logging
import re
import threading
from typing import Any, Dict, List

import requests
from django.conf import settings

from bible.utils.bible_books import get_book_name_from_id

logger = logging.getLogger(__name__)

ESV_BASE_URL = "https://api.esv.org/v3/passage/text/"
ESV_AUDIO_BASE_URL = "https://api.esv.org/v3/passage/audio/"
ESV_SEARCH_BASE_URL = "https://api.esv.org/v3/passage/search/"

_VERSE_MARKER = re.compile(r"\[(\d+)\]")


class ESVClient:
    def __init__(self, api_key=None):
        api_key = api_key or getattr(settings, "ESV_KEY", None)
        if not api_key:
            raise ValueError("ESV_KEY not configured.")
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update(
            {"Authorization": f"Token {api_key}"}
        )

    def fetch_chapter_raw(
        self,
        book_id: str,
        chapter: int,
        verse_start: int = None,
        verse_end: int = None,
    ) -> Dict[str, Any]:
        """Return the raw ESV API JSON for one chapter or, when
        ``verse_start``/``verse_end`` are given, a verse range
        within it."""
        book_name = get_book_name_from_id(book_id)
        ref = f"{book_name} {chapter}"
        if verse_start is not None:
            ref += f":{verse_start}"
            if verse_end is not None and verse_end != verse_start:
                ref += f"-{verse_end}"
        params = {
            "q": ref,
            "include-headings": "true",
            "include-verse-numbers": "true",
            "include-footnotes": "false",
            "include-passage-references": "false",
            "include-short-copyright": "false",
            "include-first-verse-numbers": "true",
            "include-passage-horizontal-lines": "false",
            "include-heading-horizontal-lines": "false",
        }
        r = self.session.get(
            ESV_BASE_URL, params=params, timeout=10
        )
        r.raise_for_status()
        return r.json()

    def get_chapter_verses(
        self, book_id: str, chapter: int
    ) -> List[Dict[str, Any]]:
        """Return [{verse_start, verse_text}, ...] matching the
        shape used by BiblePassageSerializer."""
        raw = self.fetch_chapter_raw(book_id, chapter)
        passage = (raw.get("passages") or [""])[0]
        return _parse_passage(passage)["verses"]

    def get_chapter_audio_url(
        self, book_id: str, chapter: int
    ) -> str:
        """Return the CDN redirect URL for a chapter MP3.

        The ESV audio endpoint responds with a 3xx redirect to
        the actual MP3 on their CDN.  We capture the Location
        header and return it so the frontend can play directly.

        Args:
            book_id: Standard book ID (e.g. 'GEN', 'JHN').
            chapter: Chapter number.

        Returns:
            Absolute URL string pointing to the chapter MP3.

        Raises:
            ValueError: If the API does not redirect as expected.
            requests.HTTPError: On 4xx/5xx from the ESV API.
        """
        book_name = get_book_name_from_id(book_id)
        params = {"q": f"{book_name} {chapter}"}
        r = self.session.get(
            ESV_AUDIO_BASE_URL,
            params=params,
            allow_redirects=False,
            timeout=10,
        )
        if r.status_code not in (301, 302, 303, 307, 308):
            r.raise_for_status()
            raise ValueError(
                "Expected redirect from ESV audio API, "
                f"got HTTP {r.status_code}"
            )
        location = r.headers.get("Location")
        if not location:
            raise ValueError(
                "ESV audio API redirect contained no "
                f"Location header for {book_name} {chapter}"
            )
        return location

    def get_chapter_with_headings(
        self,
        book_id: str,
        chapter: int,
        verse_start: int = None,
        verse_end: int = None,
    ) -> Dict[str, Any]:
        """Return parsed verses and section headings.

        When ``verse_start``/``verse_end`` are given, only that
        verse range within the chapter is fetched upstream.

        Returns::

            {
                "verses": [
                    {"verse_start": 1, "verse_text": "..."},
                    ...
                ],
                "headings": [
                    {
                        "before_verse": 3,
                        "text": "The Beatitudes",
                    },
                    ...
                ],
            }
        """
        raw = self.fetch_chapter_raw(
            book_id, chapter,
            verse_start=verse_start, verse_end=verse_end,
        )
        passage = (raw.get("passages") or [""])[0]
        return _parse_passage(passage)

    def search(
        self,
        query: str,
        page: int = 1,
        page_size: int = 50
    ) -> Dict[str, Any]:
        """Search the ESV Bible for a word or phrase.

        Args:
            query: Search query string
            page: Page number (default: 1)
            page_size: Results per page (default: 50, max: 100)

        Returns:
            Dict with search results matching ESV API response format

        Raises:
            requests.HTTPError: On 4xx/5xx from the ESV API
        """
        params = {
            "q": query,
            "page": str(page),
            "page-size": str(min(page_size, 100)),  # ESV API max is 100
        }

        logger.info(
            f"ESV API search: query='{query}', page={page}, "
            f"page_size={page_size}"
        )

        response = self.session.get(
            ESV_SEARCH_BASE_URL,
            params=params,
            timeout=10
        )
        response.raise_for_status()

        result = response.json()
        logger.info(
            f"ESV API search returned {result.get('total_results', 0)} "
            f"total results"
        )

        return result


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _parse_passage(passage: str) -> Dict[str, Any]:
    """Parse ESV plain-text passage into verses and headings.

    ESV format (with our request params):

    - Headings appear as their own paragraph (blank-line
      delimited) with no ``[N]`` marker, and are NOT indented.
    - Verse text has inline ``[N]`` markers; multiple verses
      may share one paragraph.
    - Text before the first ``[N]`` in a verse paragraph is
      treated as a heading for the first verse in that block.
    - Poetry and quoted scripture may span multiple blocks
      within a single verse (between verse markers) and are
      typically indented. These should be appended to the
      current verse, not treated as headings.
    """
    verses: List[Dict[str, Any]] = []
    headings: List[Dict[str, Any]] = []
    pending_heading: List[str] = []
    pending_verse_text: List[str] = []
    current_verse_num: int | None = None

    blocks = re.split(r"\n{2,}", passage.strip())

    for block_raw in blocks:
        block = block_raw.strip()
        if not block:
            continue

        # Check if block is indented (poetry/quote continuation)
        is_indented = block_raw.startswith((' ', '\t'))

        first_marker = _VERSE_MARKER.search(block)
        if first_marker is None:
            # Block has no verse marker
            if current_verse_num is not None and is_indented:
                # Indented block after verse start = continuation
                pending_verse_text.append(_normalise(block))
            elif current_verse_num is not None:
                # Non-indented block after verse = new heading
                # Flush current verse first
                if pending_verse_text:
                    full_text = _normalise(
                        " ".join(pending_verse_text)
                    )
                    if full_text:
                        verses.append({
                            "verse_start": current_verse_num,
                            "verse_text": full_text,
                        })
                    pending_verse_text = []
                current_verse_num = None
                pending_heading.append(_normalise(block))
            else:
                # No verse started yet = heading
                pending_heading.append(_normalise(block))
            continue

        # Block has at least one verse marker
        pre = block[:first_marker.start()].strip()
        if pre:
            if is_indented and current_verse_num is not None:
                # Indented pre-text is continuation of current
                # verse
                pending_verse_text.append(_normalise(pre))
            else:
                # Non-indented pre-text is a heading
                # Flush current verse first if any
                if current_verse_num is not None and pending_verse_text:
                    full_text = _normalise(
                        " ".join(pending_verse_text)
                    )
                    if full_text:
                        verses.append({
                            "verse_start": current_verse_num,
                            "verse_text": full_text,
                        })
                    pending_verse_text = []
                    current_verse_num = None
                pending_heading.append(_normalise(pre))
        elif current_verse_num is not None and pending_verse_text:
            # No pre-text, but we have a pending verse - flush
            # it
            full_text = _normalise(" ".join(pending_verse_text))
            if full_text:
                verses.append({
                    "verse_start": current_verse_num,
                    "verse_text": full_text,
                })
            pending_verse_text = []
            current_verse_num = None

        rest = block[first_marker.start():]
        parts = _VERSE_MARKER.split(rest)
        i = 1
        while i + 1 < len(parts):
            v_num = int(parts[i])
            v_text = _normalise(parts[i + 1])

            # Flush any pending verse before starting new one
            if current_verse_num is not None and pending_verse_text:
                full_text = _normalise(
                    " ".join(pending_verse_text)
                )
                if full_text:
                    verses.append({
                        "verse_start": current_verse_num,
                        "verse_text": full_text,
                    })
                pending_verse_text = []

            # Process any pending heading for this new verse
            if pending_heading:
                h_text = _normalise(
                    " ".join(pending_heading)
                )
                if h_text:
                    headings.append({
                        "before_verse": v_num,
                        "text": h_text,
                    })
                pending_heading = []

            # Start collecting text for this verse
            current_verse_num = v_num
            if v_text:
                pending_verse_text.append(v_text)

            i += 2

    # Flush final verse if any
    if current_verse_num is not None and pending_verse_text:
        full_text = _normalise(" ".join(pending_verse_text))
        if full_text:
            verses.append({
                "verse_start": current_verse_num,
                "verse_text": full_text,
            })

    return {"verses": verses, "headings": headings}


_default_client: "ESVClient | None" = None
_lock = threading.Lock()


def get_default_esv_client() -> ESVClient:
    global _default_client
    if _default_client is None:
        with _lock:
            if _default_client is None:
                _default_client = ESVClient()
    return _default_client
