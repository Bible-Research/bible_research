"""Book/chapter structure resolution for ``fileset_id``.

The ``/api/v1/bible/structure/`` endpoint answers two questions
for a fileset: which books and chapters it really covers, and
how those books are named in the fileset's language. Sourcing
follows the same ``provider_for`` dispatch as passage requests:

- DBT-backed filesets read their own bible's book list
  (``v4_bible_books`` with ``verify_content``) — the bible's
  authentic coverage and orthography.
- SWORD filesets read the local module's coverage; names are
  overlaid from the canonical bible for the fileset's language
  because module names (e.g. LvGluck8) are English, not
  localized.
- ``ENGKJV`` answers from the bundled English structure: DBT
  reports KJV names all-caps ("GENESIS"), which degrades the
  display.
- Anything whose provider cannot answer falls back to the
  canonical bible for its ``language_iso``
  (``CANONICAL_BIBLE_BY_ISO``), then to English.

Every response is filtered to the recognized 66-book USFM set —
providers can report non-book entries (``C01``-style ids or
deuterocanon) that v1 cannot route anyway. Results are cached
process-wide for ``_CACHE_TTL_SECONDS``; the view additionally
marks responses ``Vercel-CDN-Cache-Control`` like the other
anonymous catalog endpoints. A degraded body (the SWORD
canonical-name overlay fetch failed, so English module names
shipped instead) carries ``'degraded': True``, is never cached
or edge-marked — mirroring the ``bible/translations/``
``no-store`` posture — and recovers as soon as the fetch does.
An empty book list is an upstream anomaly, not real coverage,
and is likewise never cached — the next request retries.
"""
import json
import logging
import os

from django.core.cache import cache

from bible.services.apibible.registry import (
    canonical_apibible_fileset_id,
    get_apibible_meta,
)
from bible.services.dbt.client import get_default_dbt_client
from bible.services.esv.registry import (
    ESV_TRANSLATIONS,
    canonical_esv_fileset_id,
)
from bible.services.sword.registry import (
    canonical_sword_fileset_id,
    get_sword_meta,
)
from bible.utils.bible_books import (
    BOOK_CODE_TO_TESTAMENT,
    _BIBLE_BOOKS,
    _book_title,
    get_book_name_from_id,
)
from bible.utils.provider_errors import upstream_status_code

logger = logging.getLogger(__name__)

# Curated canonical bible per language: names (and fallback
# coverage) come from these DBT bibles when the fileset's own
# provider cannot supply them — see the ``bible-structure``
# spec in openspec/specs/.
CANONICAL_BIBLE_BY_ISO = {
    'eng': 'ENGESV',
    'lvs': 'LAVNLI',
    'rus': 'RUSSYN',
}
DEFAULT_CANONICAL_BIBLE = 'ENGESV'

# DBT fileset-id prefixes are language codes in a mixed
# 639-2/639-3 space (``LAT``/``LAV`` both denote Latvian filesets
# on DBT). Normalise them onto the registry keys.
_ISO_ALIASES = {
    'lat': 'lvs',
    'lav': 'lvs',
}

_CACHE_TTL_SECONDS = 24 * 60 * 60
_CACHE_PREFIX = 'v1:structure:'
_FILESET_BIBLE_MAP_KEY = 'v1:structure:fileset_bible_map'

# Canonical book order for sorting the response — DBT rows are
# already ordered but SWORD/fallback rows are not guaranteed.
_BOOK_ORDER = {
    code: index for index, (_name, code, _t) in enumerate(_BIBLE_BOOKS)
}

_ESV_VERSES_PATH = os.path.join(
    os.path.dirname(__file__), '..', 'esv_bible_verses.json'
)


def get_structure(fileset_id):
    """Return ``(body, degraded)`` for the structure endpoint.

    ``body`` is ``{'books': [...]}``, plus ``'degraded': True``
    when the SWORD path could not overlay canonical-language
    names and shipped the module's own English names instead.

    Cached per ``fileset_id`` — identical for every anonymous
    caller, mirroring ``bible/translations/`` posture. Degraded
    bodies are never cached: they must recover as soon as the
    upstream fetch does. Neither is an empty book list — it is
    an upstream anomaly, not real coverage, and caching it
    would pin the empty body for the whole TTL.
    """
    key = (fileset_id or '').strip().upper()
    cache_key = f'{_CACHE_PREFIX}{key}'
    cached = cache.get(cache_key)
    if cached is not None:
        return cached, False
    books, degraded = _resolve_books(key)
    body = {'books': books}
    if degraded:
        body['degraded'] = True
    elif books:
        cache.set(cache_key, body, _CACHE_TTL_SECONDS)
    return body, degraded


def _resolve_books(fileset_id):
    """Resolve the (filtered, ordered) book rows for a fileset.

    Returns ``(books, degraded)`` — ``degraded`` is True only on
    the SWORD path when the canonical-name overlay could not be
    applied (fetch failed or empty) and the module's own names
    shipped instead.
    """
    # ``ENGKJV`` is DBT-backed but DBT reports all-caps names —
    # the bundled table keeps the Genesis-style display names.
    if fileset_id == 'ENGKJV':
        return _bundled_books(), False

    # Local import: providers.py must not depend on this module
    # (it would be a circular import — providers is imported by
    # the registries' call-sites before Django app loading
    # finishes in some test paths).
    from bible.services import providers

    provider = providers.provider_for(fileset_id)
    concrete = getattr(provider, 'inner', provider)
    try:
        rows = provider.get_structure()
    except NotImplementedError:
        # The provider surface cannot answer (ESV, API.Bible):
        # coverage and names both come from the canonical bible
        # for the fileset's language.
        return _canonical_books(
            _language_iso_for(fileset_id)
        ), False
    if isinstance(concrete, providers.DbtProvider):
        # The bible's own orthography wins for DBT filesets.
        return _normalize_books(rows), False
    # Non-DBT coverage (SWORD): real module chapters, names from
    # the canonical bible for the language. When the overlay
    # cannot be applied (fetch failure or an empty canonical
    # map) the provider's own names ship — a degraded label is
    # better than turning a local resource into a 502 — but the
    # body is flagged so it is never cached or edge-marked.
    names = _canonical_name_map(_language_iso_for(fileset_id))
    return _normalize_books(rows, names), not names


def _normalize_books(rows, name_map=None):
    """Filter provider rows to the USFM canon and shape them.

    ``name_map`` optionally overrides ``name``/``name_short``
    (canonical per-language naming for non-DBT sources).
    """
    books = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        book_id = (row.get('book_id') or '').upper()
        if book_id not in BOOK_CODE_TO_TESTAMENT:
            continue
        name = row.get('name')
        name_short = row.get('name_short')
        if name_map and book_id in name_map:
            name, name_short = name_map[book_id]
        if not name:
            name = get_book_name_from_id(book_id)
        if not name_short:
            name_short = name
        chapters = []
        for chapter in row.get('chapters') or []:
            try:
                chapters.append(int(chapter))
            except (TypeError, ValueError):
                continue
        books.append({
            'book_id': book_id,
            'name': name,
            'name_short': name_short,
            'testament': (
                row.get('testament')
                or BOOK_CODE_TO_TESTAMENT[book_id]
            ),
            'chapters': chapters,
        })
    books.sort(key=lambda b: _BOOK_ORDER.get(b['book_id'], 999))
    return books


def _bundled_books():
    """Bundled English 66-book structure (used for ``ENGKJV``).

    Chapter counts come from ``esv_bible_verses.json`` — only
    the chapter count matters here, and it is identical between
    the ESV and KJV versifications.
    """
    with open(_ESV_VERSES_PATH) as handle:
        verse_map = json.load(handle)
    rows = []
    for name, code, testament in _BIBLE_BOOKS:
        title = _book_title(name)
        chapters = list(range(1, len(
            (verse_map.get(title) or {}).get('chapters') or {}
        ) + 1))
        rows.append({
            'book_id': code,
            'name': title,
            'name_short': title,
            'testament': testament,
            'chapters': chapters,
        })
    return rows


def _language_iso_for(fileset_id):
    """Best-effort ``language_iso`` for a fileset id.

    Registry filesets carry the iso in their meta; DBT fileset
    ids embed the language code as the first three characters
    (with ``_ISO_ALIASES`` normalising the 639-2 spellings).
    """
    canon = canonical_sword_fileset_id(fileset_id)
    if canon is not None:
        return get_sword_meta(canon)['language_iso']
    canon = canonical_esv_fileset_id(fileset_id)
    if canon is not None:
        return ESV_TRANSLATIONS[canon]['language_iso']
    canon = canonical_apibible_fileset_id(fileset_id)
    if canon is not None:
        return get_apibible_meta(canon)['language_iso']
    prefix = fileset_id[:3].lower()
    return _ISO_ALIASES.get(prefix, prefix)


def _canonical_bible_id(language_iso):
    return CANONICAL_BIBLE_BY_ISO.get(
        (language_iso or '').lower(), DEFAULT_CANONICAL_BIBLE
    )


def _canonical_books(language_iso):
    """Normalized book list of the canonical bible for a language.

    Fetched without ``verify_content`` so the canonical name
    table always covers the full canon even when the canonical
    bible's text fileset is partial (``LAVNLI``).
    """
    bible_id = _canonical_bible_id(language_iso)
    cache_key = f'{_CACHE_PREFIX}canonical:{bible_id}'
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    result = get_default_dbt_client().get_books(bible_id)
    books = _normalize_books((result or {}).get('data') or [])
    if books:
        # An empty canon is an upstream anomaly — caching it
        # would pin ``{books: []}``/``degraded`` responses for
        # the whole TTL (same stance as "never cache a failed
        # scan" in ``fileset_to_bible_id``).
        cache.set(cache_key, books, _CACHE_TTL_SECONDS)
    return books


def _canonical_name_map(language_iso):
    """``{book_id: (name, name_short)}`` of the canonical bible,
    or ``None`` when the fetch fails — the caller keeps its own
    names and flags the body degraded so it is never cached."""
    try:
        return {
            b['book_id']: (b['name'], b['name_short'])
            for b in _canonical_books(language_iso)
        }
    except Exception:
        logger.warning(
            'Canonical book-name fetch failed for iso=%s; '
            'keeping provider names.',
            language_iso, exc_info=True,
        )
        return None


def fileset_to_bible_id(fileset_id):
    """Resolve a DBT fileset id to its owning bible abbr.

    Most ids resolve by truncating to the 6-char bible id
    (``ENGESVN2DA`` → ``ENGESV``), but some DBT filesets use a
    different prefix than their bible (``LATLVRN1DA`` belongs to
    bible ``LAVLVR``). Those are found by scanning the
    translations listing filtered to the fileset's own
    language — DBT paginates ``/bibles`` at 50 rows regardless
    of ``limit`` (~3300 bibles total), so an unfiltered scan
    only ever sees page one, while a language's listing is
    small enough to fit it (largest observed: ~25). Results
    merge into the cached process-wide map; misses are
    recorded as ``None`` so an unresolvable id does not
    rescan on every request.
    """
    base = (fileset_id or '').split('-')[0]
    mapping = cache.get(_FILESET_BIBLE_MAP_KEY)
    if mapping is None:
        mapping = {}
    if fileset_id in mapping or base in mapping:
        return mapping.get(fileset_id) or mapping.get(base)
    iso = _language_iso_for(fileset_id)
    try:
        result = get_default_dbt_client().get_bibles(
            language=iso.upper() if iso else None,
            limit=500,
        )
        for bible in result.get('data') or []:
            abbr = bible.get('abbr')
            if not abbr:
                continue
            for _source, filesets in (
                bible.get('filesets') or {}
            ).items():
                for fs in filesets or []:
                    fs_id = fs.get('id')
                    if fs_id:
                        mapping[fs_id] = abbr
                        mapping.setdefault(
                            fs_id.split('-')[0], abbr
                        )
    except Exception as e:
        # Never cache a failed scan — a transient outage
        # would stick for the whole TTL. Logged by
        # type+status only: ``requests`` exceptions
        # stringify with the ``?key=`` credential.
        logger.warning(
            'Fileset->bible listing scan failed for '
            'fileset_id=%s: %s (status=%s)',
            fileset_id, type(e).__name__,
            upstream_status_code(e),
        )
        return None
    # Record the miss too — the listing is authoritative, so
    # an unresolvable id stays unresolved for the TTL instead
    # of rescanning on every request.
    mapping[fileset_id] = mapping.get(fileset_id)
    mapping[base] = mapping.get(base)
    cache.set(_FILESET_BIBLE_MAP_KEY, mapping, _CACHE_TTL_SECONDS)
    return mapping.get(fileset_id) or mapping.get(base)
