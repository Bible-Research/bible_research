"""Fetch-and-group helpers for ``group_by=book`` search results.

Used by ``bible.views.BibleSearchView`` when a request carries
``group_by=book``: fetch every matching verse from the provider,
then bucket them into per-book groups in canonical order.
"""

import logging
from concurrent.futures import ThreadPoolExecutor

from bible.utils.bible_books import (
    BOOK_ORDER_MAP,
    get_book_name_from_id,
    get_dbt_book_id,
)

logger = logging.getLogger(__name__)

# Safety caps so a pathological query can't fetch unbounded data.
MAX_RESULTS = 5000
MAX_ESV_PAGES = 30
ESV_PAGE_SIZE = 100
ESV_PAGE_WORKERS = 4


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


def normalize_dbt_verse(item):
    """Normalize one DBT ``/search`` verse item."""
    return {
        'book_id': item.get('book_id'),
        'chapter': item.get('chapter'),
        'verse_start': item.get('verse_start'),
        'verse_text': item.get('verse_text'),
    }


def _book_order(book_id):
    """Canonical sort key for a DBT book id (unknowns last)."""
    if not book_id:
        return len(BOOK_ORDER_MAP) + 1
    try:
        name = get_book_name_from_id(book_id)
    except ValueError:
        return len(BOOK_ORDER_MAP) + 1
    return BOOK_ORDER_MAP.get(name, len(BOOK_ORDER_MAP) + 1)


def group_verses_by_book(verses):
    """Bucket normalized verse dicts into per-book groups.

    Returns ``[{book_id, count, verses: [...]}, ...]`` with the
    groups sorted in canonical book order and the verses inside
    each group sorted by ``(chapter, verse_start)``.
    """
    by_book = {}
    for verse in verses:
        by_book.setdefault(verse.get('book_id'), []).append(verse)

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


def fetch_dbt_matches(client, fileset_id, query, books):
    """Fetch all DBT matches for a grouped search.

    ``limit`` is uncapped on DBT, so the first call usually
    returns everything; remaining pages are fetched until the
    provider-reported total is reached or ``MAX_RESULTS`` caps
    the fetch.

    Returns ``(verses, total, truncated)``.
    """
    result = client.search(
        fileset_id, query, limit=MAX_RESULTS, books=books,
    )
    raw_verses = result.get('verses') or {}
    items = list(raw_verses.get('data') or [])
    raw_meta = result.get('meta') or raw_verses.get('meta') or {}
    pagination = raw_meta.get('pagination') or {}
    total = pagination.get('total') or len(items)

    page = 1
    while len(items) < MAX_RESULTS and len(items) < total:
        page += 1
        result = client.search(
            fileset_id, query,
            limit=MAX_RESULTS, page=page, books=books,
        )
        more = (result.get('verses') or {}).get('data') or []
        if not more:
            break
        items.extend(more)
    if len(items) > MAX_RESULTS:
        items = items[:MAX_RESULTS]

    return (
        [normalize_dbt_verse(v) for v in items],
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


def scan_sword_matches(client, fileset_id, query, books):
    """Scan all SWORD chapters and return the full match list.

    ``books`` (comma-separated USFM ids) restricts the chapters
    scanned. Matches stay in the chapter order the client
    returns; grouping sorts them canonically anyway.
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

    needle = query.lower()
    matches = []
    for book_id, chapter in chapters:
        try:
            verses = client.get_chapter_verses(
                fileset_id, book_id, chapter
            )
        except Exception:
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
    return matches
