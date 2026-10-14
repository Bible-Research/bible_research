"""Thin wrapper around the ESV HTTP API (api.esv.org/v3)."""
import logging
import re
import threading
from html.parser import HTMLParser
from typing import Any, Dict, List

import requests
from django.conf import settings

from bible.utils.bible_books import get_book_name_from_id

logger = logging.getLogger(__name__)

ESV_HTML_BASE_URL = "https://api.esv.org/v3/passage/html/"
ESV_AUDIO_BASE_URL = "https://api.esv.org/v3/passage/audio/"
ESV_SEARCH_BASE_URL = "https://api.esv.org/v3/passage/search/"


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
        self, book_id: str, chapter: int
    ) -> Dict[str, Any]:
        """Return the raw ESV API JSON for one chapter."""
        book_name = get_book_name_from_id(book_id)
        params = {
            "q": f"{book_name} {chapter}",
            "include-headings": "true",
            "include-footnotes": "false",
            "include-passage-references": "false",
            "include-short-copyright": "false",
            "include-audio-link": "false",
        }
        r = self.session.get(
            ESV_HTML_BASE_URL, params=params, timeout=10
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
        self, book_id: str, chapter: int
    ) -> Dict[str, Any]:
        """Return parsed verses and section headings.

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
        raw = self.fetch_chapter_raw(book_id, chapter)
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


class _PassageHTMLParser(HTMLParser):
    """Extract verses and section headings from ESV HTML.

    ESV markup (with our request params):

    - Section headings are ``<h3>`` elements. A heading attaches
      to the next verse number that follows it.
    - Verse boundaries are ``<b class="verse-num">`` elements
      (or ``verse-num inline``) whose text is the verse number;
      the chapter's first verse uses ``<b class="chapter-num">``
      with ``CHAPTER:VERSE`` text.
    - All other markup (poetry ``<span class="line">``,
      words-of-Christ ``<span class="woc">``, ``<br />``) is
      verse text and is flattened into the current verse.
    """

    _HEADING_TAGS = {"h2", "h3", "h4"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.verses: List[Dict[str, Any]] = []
        self.headings: List[Dict[str, Any]] = []
        self._verse_num: "int | None" = None
        self._verse_parts: List[str] = []
        self._pending_headings: List[str] = []
        self._in_heading = False
        self._heading_parts: List[str] = []
        self._in_verse_marker = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in self._HEADING_TAGS:
            self._in_heading = True
            self._heading_parts = []
        elif tag == "b":
            classes = dict(attrs).get("class", "")
            if "verse-num" in classes or "chapter-num" in classes:
                self._in_verse_marker = True

    def handle_endtag(self, tag: str) -> None:
        if tag in self._HEADING_TAGS:
            self._in_heading = False
            text = _normalise(" ".join(self._heading_parts))
            if text:
                self._pending_headings.append(text)
        elif tag == "b":
            self._in_verse_marker = False

    def handle_data(self, data: str) -> None:
        if self._in_heading:
            self._heading_parts.append(data)
        elif self._in_verse_marker:
            self._start_verse(data)
            self._in_verse_marker = False
        elif self._verse_num is not None:
            self._verse_parts.append(data)

    def _start_verse(self, marker_text: str) -> None:
        """Begin a new verse; attach pending headings to it."""
        # chapter-num carries "7:1"; verse-num carries "13".
        num_text = _normalise(marker_text).split(":")[-1]
        try:
            num = int(num_text)
        except ValueError:
            return
        self._flush_verse()
        for heading in self._pending_headings:
            self.headings.append(
                {"before_verse": num, "text": heading}
            )
        self._pending_headings = []
        self._verse_num = num
        self._verse_parts = []

    def _flush_verse(self) -> None:
        if self._verse_num is None:
            return
        text = _normalise(" ".join(self._verse_parts))
        if text:
            self.verses.append({
                "verse_start": self._verse_num,
                "verse_text": text,
            })

    def result(self) -> Dict[str, Any]:
        self._flush_verse()
        self._verse_num = None
        return {"verses": self.verses, "headings": self.headings}


def _parse_passage(passage: str) -> Dict[str, Any]:
    """Parse an ESV HTML passage into verses and headings."""
    parser = _PassageHTMLParser()
    parser.feed(passage)
    parser.close()
    return parser.result()


_default_client: "ESVClient | None" = None
_lock = threading.Lock()


def get_default_esv_client() -> ESVClient:
    global _default_client
    if _default_client is None:
        with _lock:
            if _default_client is None:
                _default_client = ESVClient()
    return _default_client
