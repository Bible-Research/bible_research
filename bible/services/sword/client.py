"""Thin pysword wrapper exposing the slice of the DBT client API used
by `BiblePassageSerializer`."""
import logging
import threading
from typing import Any, Dict, List, Tuple

from pysword.modules import SwordModules

from bible.utils.bible_books import (
    _BIBLE_BOOKS,
    normalize_sword_book_name,
)
from bible.utils.provider_errors import PassageNotFoundError

from .registry import (
    SWORD_MODULES_DIR,
    SWORD_TRANSLATIONS,
    canonical_sword_fileset_id,
    get_sword_meta,
)

logger = logging.getLogger(__name__)


class SwordClient:
    """Process-wide cache of pysword Bible objects loaded from vendored
    raw-zip modules. The `SwordModules` instance must be retained for
    the lifetime of the bible (it owns the open zip stream)."""

    def __init__(self):
        self._lock = threading.Lock()
        # fileset_id -> (SwordModules, SwordBible)
        self._cache: Dict[str, tuple] = {}
        # fileset_id -> {book_id: pysword book.name}
        self._book_name_cache: Dict[str, Dict[str, str]] = {}

    def _load(self, fileset_id: str):
        canon = canonical_sword_fileset_id(fileset_id)
        if canon is None:
            raise ValueError(f"Unknown SWORD fileset_id: {fileset_id!r}")
        fileset_id = canon

        if fileset_id in self._cache:
            return self._cache[fileset_id]

        with self._lock:
            if fileset_id in self._cache:
                return self._cache[fileset_id]

            meta = get_sword_meta(fileset_id)
            zip_path = SWORD_MODULES_DIR / meta["zip_filename"]
            if not zip_path.exists():
                raise FileNotFoundError(
                    f"SWORD module zip not found: {zip_path}. "
                    f"Run scripts/download_sword_modules.sh."
                )

            modules = SwordModules(str(zip_path))
            modules.parse_modules()
            bible = modules.get_bible_from_module(meta["module_name"])
            self._cache[fileset_id] = (modules, bible)
            logger.info(
                "Loaded SWORD module %s (%s) for fileset_id=%s",
                meta["module_name"], meta["name"], fileset_id,
            )
            return self._cache[fileset_id]

    def _get_book_name_map(self, canon: str) -> Dict[str, str]:
        """Return a ``{book_id: pysword book.name}`` map for the given
        (already-canonical) fileset. pysword's ``bible.get(books=[...])``
        only accepts the module's own canonical book name (e.g.
        ``"I Kings"``), its OSIS name (``"1Kgs"``), or its preferred
        abbreviation — it will NOT match the lowercase English form
        stored in ``_BIBLE_BOOKS`` (``"1 kings"``). Resolve the DBT
        book_id via the module's own structure so every book (including
        Roman-numeral-prefixed ones like 1/2 Kings and the Johannine
        Revelation) round-trips correctly."""
        cached = self._book_name_cache.get(canon)
        if cached is not None:
            return cached
        _modules, bible = self._load(canon)
        name_to_id = {
            name.lower(): code for name, code, _ in _BIBLE_BOOKS
        }
        mapping: Dict[str, str] = {}
        for _testament, books in (
            bible.get_structure().get_books().items()
        ):
            for book in books:
                norm = normalize_sword_book_name(book.name)
                book_id = name_to_id.get(norm)
                if book_id is None:
                    continue
                mapping[book_id] = book.name
        self._book_name_cache[canon] = mapping
        return mapping

    def get_chapter_verses(
        self, fileset_id: str, book_id: str, chapter: int
    ) -> List[Dict[str, Any]]:
        """Return [{verse_start, verse_text}, ...] for a chapter,
        matching the shape used by `BiblePassageSerializer`."""
        canon = canonical_sword_fileset_id(fileset_id)
        if canon is None:
            raise ValueError(f"Unknown SWORD fileset: {fileset_id!r}")
        _modules, bible = self._load(canon)

        pysword_book = self._get_book_name_map(canon).get(
            book_id.upper()
        )
        if not pysword_book:
            raise PassageNotFoundError(
                f"Book {book_id} not present in fileset {canon}"
            )

        verses: List[Dict[str, Any]] = []
        verse_num = 1
        while True:
            try:
                text = bible.get(
                    books=[pysword_book],
                    chapters=[int(chapter)],
                    verses=[verse_num],
                    clean=True,
                )
            except Exception:
                break
            text = (text or "").strip()
            if not text:
                break
            verses.append({"verse_start": verse_num, "verse_text": text})
            verse_num += 1
            if verse_num > 200:
                break

        if not verses:
            raise PassageNotFoundError(
                f"No verses found for {book_id} {chapter} "
                f"in fileset_id={fileset_id}"
            )
        return verses

    def list_chapters(
        self, fileset_id: str
    ) -> List[Tuple[str, int]]:
        """Return an ordered ``[(book_id, chapter), ...]`` worklist
        sourced directly from this fileset's SWORD module structure.

        Traverses pysword's own OT → NT canonical order so that the
        audio generator never assumes any other Bible's versification
        (e.g. LVSGLU8 / Glück 1877 may not match ESV chapter/book
        counts). Books whose SWORD name has no mapping in
        ``_BIBLE_BOOKS`` are logged and skipped."""
        canon = canonical_sword_fileset_id(fileset_id)
        if canon is None:
            raise ValueError(
                f"Unknown SWORD fileset: {fileset_id!r}"
            )
        _modules, bible = self._load(canon)

        name_to_id = {
            name.lower(): code for name, code, _ in _BIBLE_BOOKS
        }
        out: List[Tuple[str, int]] = []
        for _testament, books in bible.get_structure().get_books().items():
            for book in books:
                norm = normalize_sword_book_name(book.name)
                book_id = name_to_id.get(norm)
                if book_id is None:
                    logger.warning(
                        "SWORD module %s book %r has no book_id "
                        "mapping in _BIBLE_BOOKS; skipping.",
                        canon, book.name,
                    )
                    continue
                for chap in range(1, book.num_chapters + 1):
                    out.append((book_id, chap))
        return out

    def get_translation_listing(self) -> List[Dict[str, Any]]:
        """Return registry entries shaped like DBT translations."""
        out = []
        for fid, meta in SWORD_TRANSLATIONS.items():
            out.append({
                "abbr": meta["abbr"],
                "name": meta["name"],
                "language": meta["language"],
                "iso": meta["language_iso"],
                "filesets": [
                    {
                        "id": fid,
                        "type": "text_plain",
                        "size": "C",
                    },
                    {
                        "id": meta.get("audio_fileset_id", fid),
                        "type": "audio",
                        "size": "C",
                    },
                ],
            })
        return out


_default_client: "SwordClient | None" = None
_default_client_lock = threading.Lock()


def get_default_sword_client() -> "SwordClient":
    global _default_client
    if _default_client is None:
        with _default_client_lock:
            if _default_client is None:
                _default_client = SwordClient()
    return _default_client
