"""Fetch-and-group helpers for ``group_by=book`` search results.

Used by ``bible.views.BibleSearchView`` when a request carries
``group_by=book``: fetch every matching verse from the provider,
then bucket them into per-book groups in canonical order.
"""

import base64
import binascii
import hashlib
import logging
import time
from concurrent.futures import ThreadPoolExecutor

from bible.utils.bible_books import (
    BOOK_ORDER_MAP,
    CANONICAL_BOOK_IDS,
    CANONICAL_BOOKS_PARAM,
    get_book_name_from_id,
    get_dbt_book_id,
)
from bible.utils.provider_errors import (
    InvalidSearchCursorError,
    capture_internal_exception,
    sentry_span,
)

logger = logging.getLogger(__name__)

# Safety caps so a pathological query can't fetch unbounded data.
MAX_RESULTS = 5000
MAX_ESV_PAGES = 30
ESV_PAGE_SIZE = 100
ESV_PAGE_WORKERS = 4

# Wall-clock budget for one SWORD scan window: a request scans
# chapters until this many seconds elapse, then returns what it
# has plus a resume cursor. Sized well under gunicorn's 30s
# worker timeout so a slow scan can never get its worker killed.
SEARCH_SCAN_BUDGET_SECONDS = 6

# ``next_cursor`` wire format:
# ``base64url("v2:<ctx>:<index>")`` where index is a position in
# the fileset's chapter worklist and ``<ctx>`` is a short hash
# binding the token to the scan context — (fileset_id, books,
# query). A bare positional cursor minted under ``books=GEN``
# replayed with ``books=MAT`` would validate and silently scan a
# misaligned window, so decode rejects a context mismatch. Opaque
# to clients, stateless, and deterministic because the worklist
# is stable per (fileset_id, books).
_SCAN_CURSOR_PREFIX = 'v2'


def _scan_context_key(fileset_id, books, query):
    """Short hash identifying the scan a cursor belongs to.

    Normalized the way the scan itself sees the inputs so
    cosmetically different but equivalent requests still match:
    the fileset id uppercases (callers pass the canonical id),
    ``books`` tokens strip/uppercase/dedupe/sort (the worklist
    filter is membership-based), and the query lowercases like
    the scan needle.
    """
    books_key = ','.join(sorted({
        token.strip().upper()
        for token in (books or '').split(',')
        if token.strip()
    }))
    raw = '|'.join((
        (fileset_id or '').strip().upper(),
        books_key,
        (query or '').strip().lower(),
    ))
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:10]


def encode_scan_cursor(index, fileset_id, books, query):
    """Serialize a chapter-worklist position as an opaque token
    bound to the (fileset_id, books, query) scan context."""
    raw = (
        f'{_SCAN_CURSOR_PREFIX}:'
        f'{_scan_context_key(fileset_id, books, query)}:'
        f'{index}'
    ).encode('ascii')
    return base64.urlsafe_b64encode(raw).decode('ascii')


def decode_scan_cursor(cursor, fileset_id, books, query):
    """Inverse of ``encode_scan_cursor``.

    Raises ``InvalidSearchCursorError`` when the value is not a
    well-formed cursor for this (fileset_id, books, query) scan
    context — the view maps that to a 400 ``invalid_parameter``
    response.
    """
    try:
        raw = base64.b64decode(
            cursor.encode('ascii'), altchars=b'-_',
            validate=True,
        )
        text = raw.decode('ascii')
    except (binascii.Error, UnicodeError, ValueError) as e:
        raise InvalidSearchCursorError(
            'cursor could not be decoded'
        ) from e
    parts = text.split(':')
    if len(parts) != 3 or parts[0] != _SCAN_CURSOR_PREFIX:
        raise InvalidSearchCursorError(
            'cursor has an unknown format'
        )
    if parts[1] != _scan_context_key(fileset_id, books, query):
        raise InvalidSearchCursorError(
            'cursor belongs to a different scan'
        )
    try:
        index = int(parts[2])
    except ValueError as e:
        raise InvalidSearchCursorError(
            'cursor carries no position'
        ) from e
    if index < 0:
        raise InvalidSearchCursorError(
            'cursor position is negative'
        )
    return index


def parse_esv_results(results):
    """Parse ESV ``/passage/search/`` result items into verse dicts.

    Each item becomes ``{book_id, chapter, verse_start,
    verse_text}``; references that fail to parse (or map to no
    known book) are skipped.
    """
    verses = []
    for item in results:
        # Parse reference like "John 3:16" or "Genesis 1:1-2"
        reference = item.get('reference', '')
        try:
            parts = reference.split()
            if len(parts) >= 2:
                # Handle book names with spaces (e.g., "1 John")
                verse_part = parts[-1]  # "3:16" or "3:16-17"
                book_name = ' '.join(parts[:-1])

                if ':' in verse_part:
                    chapter_str, verse_str = verse_part.split(':', 1)
                    chapter = int(chapter_str)
                    # Ranges like "1-2" keep the first verse
                    verse_start = int(verse_str.split('-')[0])
                    book_id = get_dbt_book_id(book_name)
                    if book_id:
                        verses.append({
                            'book_id': book_id,
                            'chapter': chapter,
                            'verse_start': verse_start,
                            'verse_text': item.get('content', ''),
                        })
        except (ValueError, IndexError) as e:
            logger.warning(
                "Failed to parse ESV reference '%s': %s",
                reference, e,
            )
            continue
    return verses


def filter_canonical_verses(verses):
    """Drop normalized search rows outside the 66-book canon.

    Belt-and-braces behind the upstream ``books=`` filter —
    it covers providers that ignore ``books`` and providers
    with no book filter at all (API.Bible). Rows are removed
    outright, never nulled: the paginated path emits the list
    verbatim, and a ``None`` row would surface as a hole.
    """
    return [
        v for v in verses
        if v.get('book_id') in CANONICAL_BOOK_IDS
    ]


def _int_or_none(value):
    """Coerce a provider value to ``int`` (``None`` when not
    numeric) — DBT serializes ``chapter``/``verse_start`` as
    strings in some payloads."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_dbt_verse(item):
    """Normalize one DBT ``/search`` verse item."""
    return {
        'book_id': item.get('book_id'),
        'chapter': _int_or_none(item.get('chapter')),
        'verse_start': _int_or_none(item.get('verse_start')),
        'verse_text': item.get('verse_text'),
    }


def normalize_apibible_verse(item):
    """Normalize one API.Bible ``/search`` verse item.

    API.Bible verses carry ``id`` (first verse of the match,
    e.g. ``JHN.3.16``), ``orgId`` (the match range, e.g.
    ``JHN.3.16-17``), ``bookId``, ``chapterId``, ``reference``
    and ``text``. ``id`` is preferred because it points at the
    first verse; items that fail to parse keep ``None`` fields
    and are dropped by ``group_verses_by_book``.
    """
    book_id, chapter, verse_start = _parse_apibible_verse_ref(
        item.get('id') or item.get('orgId') or ''
    )
    return {
        'book_id': book_id or item.get('bookId'),
        'chapter': chapter,
        'verse_start': verse_start,
        'verse_text': item.get('text') or '',
    }


def _parse_apibible_verse_ref(verse_id):
    """Parse ``JHN.3.16`` into ``(book_id, chapter, verse_start)``.

    Ranges keep the first verse and come in two shapes:
    ``JHN.3.16-17`` and the ``orgId`` form
    ``JHN.3.16-JHN.3.17`` — strip the range tail before
    splitting on dots so the second parses too.
    """
    try:
        head = verse_id.split('-', 1)[0]
        book_id, chapter_str, verse_str = head.split('.')
        return book_id, int(chapter_str), int(verse_str)
    except (ValueError, AttributeError):
        return None, None, None


def _book_order(book_id):
    """Canonical sort key for a DBT book id (unknowns last)."""
    if not book_id:
        return len(BOOK_ORDER_MAP) + 1
    try:
        name = get_book_name_from_id(book_id)
    except (ValueError, TypeError, AttributeError):
        return len(BOOK_ORDER_MAP) + 1
    return BOOK_ORDER_MAP.get(name, len(BOOK_ORDER_MAP) + 1)


def group_verses_by_book(verses):
    """Bucket normalized verse dicts into per-book groups.

    Returns ``[{book_id, count, verses: [...]}, ...]`` with the
    groups sorted in canonical book order and the verses inside
    each group sorted by ``(chapter, verse_start)``.

    Verses without a string ``book_id`` are dropped, and
    duplicates on ``(book_id, chapter, verse_start)`` collapse
    to the first occurrence — overlapping ranges like
    "John 3:16-18" normalize to verse_start=16 and can repeat.
    """
    by_book = {}
    seen = set()
    for verse in verses:
        book_id = verse.get('book_id')
        if not isinstance(book_id, str) or not book_id:
            continue
        key = (
            book_id,
            verse.get('chapter'),
            verse.get('verse_start'),
        )
        if key in seen:
            continue
        seen.add(key)
        by_book.setdefault(book_id, []).append(verse)

    groups = []
    for book_id, items in by_book.items():
        items.sort(
            key=lambda v: (
                v.get('chapter') or 0,
                v.get('verse_start') or 0,
            )
        )
        groups.append({
            'book_id': book_id,
            'count': len(items),
            'verses': items,
        })
    groups.sort(key=lambda g: _book_order(g['book_id']))
    return groups


def fetch_dbt_matches(
    client, fileset_id, query, books, sort_by=None,
):
    """Fetch all DBT matches for a grouped search.

    ``limit`` is uncapped on DBT, so the first call usually
    returns everything; remaining pages are fetched until the
    provider-reported total is reached or ``MAX_RESULTS`` caps
    the fetch. ``sort_by`` is forwarded on every call — dropping
    it on page 2+ would let the provider reorder mid-pagination
    and skip/duplicate rows.

    Returns ``(verses, total, truncated)``.
    """
    # The canon restriction is pushed upstream — ``books=`` is
    # always sent (the caller's filter intersected with the
    # canon, or the full 66-id list) so provider-reported
    # ``total`` counts canonical matches only.
    books = books or CANONICAL_BOOKS_PARAM
    result = client.search(
        fileset_id, query,
        limit=MAX_RESULTS, sort_by=sort_by, books=books,
    )
    raw_verses = result.get('verses') or {}
    items = list(raw_verses.get('data') or [])
    raw_meta = result.get('meta') or raw_verses.get('meta') or {}
    pagination = raw_meta.get('pagination') or {}
    total = pagination.get('total') or len(items)

    # The provider may cap the actual page size below the
    # requested ``limit``. Follow-up pages must request the
    # *effective* page size observed on page 1, otherwise the
    # server offsets by its own cap and rows get skipped.
    per_page = min(
        pagination.get('per_page') or len(items),
        len(items),
    )

    page = 1
    while (
        per_page
        and len(items) < MAX_RESULTS
        and len(items) < total
    ):
        page += 1
        result = client.search(
            fileset_id, query,
            limit=per_page, page=page,
            sort_by=sort_by, books=books,
        )
        more = (result.get('verses') or {}).get('data') or []
        if not more:
            break
        items.extend(more)
    if len(items) > MAX_RESULTS:
        items = items[:MAX_RESULTS]

    # Post-filter as a safety net: if upstream ignored
    # ``books=``, apocryphal rows are dropped here (the
    # provider ``total`` may then overcount — inflated totals
    # are the accepted worst case, never leaked content).
    return (
        filter_canonical_verses(
            normalize_dbt_verse(v) for v in items
        ),
        total,
        total > len(items),
    )


def fetch_apibible_matches(client, bible_id, query):
    """Fetch all API.Bible matches for a grouped search.

    API.Bible paginates by ``offset``; pages of ``ESV_PAGE_SIZE``
    are fetched sequentially until the provider-reported
    ``total`` is reached or ``MAX_RESULTS`` caps the fetch.

    Returns ``(verses, total, truncated)``.
    """
    items = []
    total = 0
    offset = 0
    while len(items) < MAX_RESULTS:
        result = client.search(
            bible_id, query,
            offset=offset, limit=ESV_PAGE_SIZE,
        )
        data = result.get('data') or {}
        batch = data.get('verses') or []
        if not batch:
            break
        items.extend(batch)
        total = data.get('total') or len(items)
        if len(items) >= total:
            break
        offset += len(batch)
    if len(items) > MAX_RESULTS:
        items = items[:MAX_RESULTS]
    # API.Bible has no upstream book filter, so canon
    # enforcement is post-fetch only — a hypothetical
    # deuterocanonical bible would drop rows here while
    # ``total`` still counts them (registered bibles are
    # NIV-only today, so this stays hypothetical).
    return (
        filter_canonical_verses(
            normalize_apibible_verse(v) for v in items
        ),
        total,
        total > len(items),
    )


def fetch_esv_matches(client, query):
    """Fetch ESV matches across all result pages.

    Page 1 is fetched first to learn ``total_pages``; pages
    2..min(total_pages, MAX_ESV_PAGES) are fetched in parallel
    (ESV page-size hard-caps at 100 and has no grouping).

    Returns ``(verses, total, truncated)``.
    """
    first = client.search(query, 1, ESV_PAGE_SIZE)
    total = first.get('total_results', 0)
    total_pages = first.get('total_pages', 1)
    verses = parse_esv_results(first.get('results', []))

    last_page = min(total_pages, MAX_ESV_PAGES)
    if last_page > 1:
        # The client's shared requests.Session is only used for
        # read-only GETs here; urllib3 hands each worker its own
        # pooled connection, so no session state is mutated.
        with ThreadPoolExecutor(
            max_workers=ESV_PAGE_WORKERS
        ) as pool:
            pages = pool.map(
                lambda p: client.search(query, p, ESV_PAGE_SIZE),
                range(2, last_page + 1),
            )
            for result in pages:
                verses.extend(
                    parse_esv_results(result.get('results', []))
                )

    return verses, total, total_pages > MAX_ESV_PAGES


def sword_chapter_worklist(client, fileset_id, books):
    """Return the ordered ``[(book_id, chapter), ...]`` scan
    worklist for a fileset — the module's full chapter list,
    restricted to the ``books`` filter when given. Shared by
    ``scan_sword_matches`` and the grouped-search provider, which
    needs the total for cursor validation and ``meta.progress``.
    """
    chapters = client.list_chapters(fileset_id)

    if books:
        allowed = {
            b.strip().upper() for b in books.split(',')
        }
        chapters = [
            (b, c) for b, c in chapters
            if b.upper() in allowed
        ]
    return chapters


def scan_sword_matches(
    client, fileset_id, query, books, max_matches=None,
    deadline_s=None, start_index=0, chapters=None,
):
    """Scan SWORD chapters and return
    ``(matches, truncated, next_index)``.

    ``books`` (comma-separated USFM ids) restricts the chapters
    scanned. Matches stay in the chapter order the client
    returns; grouping sorts them canonically anyway.

    ``max_matches`` bounds the local scan: once that many matches
    are collected the remaining chapters are skipped and
    ``truncated`` is ``True`` (the match count is then a lower
    bound, not the true total). ``None`` scans everything.

    ``deadline_s`` is a wall-clock budget in seconds, checked
    between chapters: when it expires the scan stops and
    ``next_index`` carries the position of the first unscanned
    chapter so a follow-up request can resume there (the grouped
    endpoint emits it as ``meta.next_cursor``). ``start_index``
    resumes a previous window. ``chapters`` lets a caller pass a
    pre-built worklist so the same list serves cursor bounds
    checks and the scan; ``None`` builds it here.

    ``next_index`` is the position of the first unscanned
    chapter whenever the scan stopped early — deadline expiry
    or the ``max_matches`` cap — and ``None`` only on a full
    scan. ``max_matches`` bounds the matches one window collects
    (per-request work), not the cumulative total a client can
    accumulate across resumed windows; callers emitting a resume
    cursor must still check the stop reason — a capped window is
    reported as terminal truncation.

    ``truncated`` is ``True`` whenever the worklist was not
    fully scanned (cap or deadline) — ``total`` is then a lower
    bound.
    """
    if chapters is None:
        chapters = sword_chapter_worklist(
            client, fileset_id, books
        )

    needle = query.lower()
    matches = []
    truncated = False
    next_index = None
    deadline = (
        time.monotonic() + deadline_s
        if deadline_s is not None
        else None
    )
    total = len(chapters)
    # One Sentry event per scan is enough — a broken module
    # tends to fail on every chapter, and the fingerprint
    # already groups them; the log keeps the full list.
    unexpected_reported = False
    with sentry_span(
        op='sword.scan',
        description='scan_sword_matches',
        data={
            'fileset_id': fileset_id,
            'chapters': total,
        },
    ):
        for idx in range(start_index, total):
            if (
                deadline is not None
                and idx > start_index
                and time.monotonic() >= deadline
            ):
                # Budget spent between chapters: report the
                # matches collected so far plus the resume
                # position. At least one chapter always scans
                # (a zero/negative budget still makes progress).
                next_index = idx
                break
            book_id, chapter = chapters[idx]
            try:
                verses = client.get_chapter_verses(
                    fileset_id, book_id, chapter
                )
            except (ValueError, OSError) as e:
                # Expected coverage misses: the chapter is
                # outside the module's canon (ValueError /
                # PassageNotFoundError) or its data file is
                # absent (OSError) — skip it like the old bare
                # ``except``.
                logger.debug(
                    "Skipping SWORD chapter %s %s: %s",
                    book_id, chapter, e,
                )
                continue
            except Exception as e:
                # Anything else is a real bug or data
                # corruption — log it instead of swallowing
                # silently, but keep the scan going so one
                # bad chapter can't blank a whole search.
                logger.exception(
                    "Unexpected error scanning SWORD chapter "
                    "%s %s in %s",
                    book_id, chapter, fileset_id,
                )
                if not unexpected_reported:
                    unexpected_reported = True
                    capture_internal_exception(
                        e,
                        tags={
                            'fileset_id': fileset_id,
                            'provider': 'SwordProvider',
                            'endpoint': 'bible/search',
                        },
                        contexts={
                            'sword_chapter': {
                                'book_id': book_id,
                                'chapter': chapter,
                            },
                        },
                    )
                continue
            for v in verses:
                text = v.get('verse_text', '')
                if needle in text.lower():
                    matches.append({
                        'book_id': book_id,
                        'chapter': chapter,
                        'verse_start': v['verse_start'],
                        'verse_text': text,
                    })
            if (
                max_matches is not None
                and len(matches) >= max_matches
            ):
                # Only flag truncation when chapters remain
                # unscanned — reaching the cap on the final
                # chapter means the whole module was scanned
                # and the total is exact.
                truncated = idx < total - 1
                if truncated:
                    next_index = idx + 1
                matches = matches[:max_matches]
                break
    if next_index is not None:
        truncated = True
    return matches, truncated, next_index
