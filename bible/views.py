import logging

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from google.api_core import exceptions as gcs_exceptions
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from bible.utils.bible_books import get_dbt_book_id
from bible.utils.provider_errors import provider_error_fields
from bible.services.apibible.registry import (
    get_apibible_meta,
    is_apibible_fileset,
)
from bible.services.google_tts.registry import get_tts_config
from bible.services.sword.client import get_default_sword_client
from bible.services.sword.registry import (
    canonical_sword_fileset_id,
    is_sword_fileset,
)
from bible.services.esv.registry import is_esv_fileset
from bible.services.search_grouping import (
    fetch_apibible_matches,
    fetch_dbt_matches,
    fetch_esv_matches,
    group_verses_by_book,
    normalize_apibible_verse,
    normalize_dbt_verse,
    parse_esv_results,
    scan_sword_matches,
)
from bible.services.storage import gcs
from .serializers import BiblePassageSerializer
from .services.translation_service import TranslationService
from .services.apibible.client import (
    get_default_apibible_client,
)
from .services.dbt.client import get_default_dbt_client
from .services.esv.client import (
    get_default_esv_client
)

logger = logging.getLogger(__name__)

PROVIDER_ERROR_STATUSES = {
    'rate_limited': status.HTTP_429_TOO_MANY_REQUESTS,
    'not_found': status.HTTP_404_NOT_FOUND,
}


def _apibible_timecode(item):
    """Map an API.Bible audio timecode to ``{verse_start,
    timestamp}``.

    Timecode entries carry a ``verseId`` like ``JHN.3.16`` (some
    payloads expose ``verse_start``/``verse``/``verse_id``
    directly); the trailing dotted segment is the verse number.
    """
    verse = (
        item.get('verse_start')
        or item.get('verse')
        or item.get('verse_id')
    )
    if verse is None:
        verse_id = item.get('verseId') or ''
        try:
            verse = int(str(verse_id).split('.')[-1])
        except ValueError:
            verse = None
    return {
        'verse_start': verse,
        'timestamp': item.get('timestamp'),
    }


def provider_error_response(exc):
    """Build an error ``Response`` for an upstream provider failure.

    Uses ``provider_error_fields`` so raw exception text — which can
    contain request URLs carrying credentials (e.g. DBT's ``?key=``
    query param) — never reaches the response body.
    """
    fields = provider_error_fields(exc)
    return Response(
        fields,
        status=PROVIDER_ERROR_STATUSES.get(
            fields['error_code'],
            status.HTTP_502_BAD_GATEWAY,
        ),
    )


class BiblePassageView(APIView):
    """
    API endpoint to retrieve Bible passages.

    Query Parameters:
        - passage: Book and chapter (e.g., '2 Chronicles 14')
        - response_format: 'text' or 'audio' (default: 'text')
        - fileset_id: DBT fileset ID (e.g. ENGESV) or bundled SWORD id
          (e.g. LVSGLU8). SWORD translations also accept listing ``abbr``
          (e.g. GLU8 for Latvian Glück).

    Example:
        /api/v1/bible/?passage=John+3&fileset_id=ENGESV
        /api/v1/bible/?passage=John+3&fileset_id=LVSGLU8   # LV
        /api/v1/bible/?passage=Luke+20&fileset_id=GLU8
            &response_format=audio   # LV audio
    """

    def get(self, request, format=None):
        passage = request.query_params.get('passage')
        response_format = request.query_params.get(
            'response_format', 'text'
        )
        fileset_id = request.query_params.get('fileset_id', 'ENGESV')

        logger.info(
            f"BiblePassageView.get called with passage: {passage}, "
            f"format: {response_format}, fileset_id: {fileset_id}"
        )

        if not passage:
            logger.warning("Request missing required 'passage' parameter")
            return Response(
                {
                    "error": "Passage parameter is required. "
                             "Example: ?passage=John+3:16"
                },
                status=status.HTTP_400_BAD_REQUEST
            )

        if response_format not in ['text', 'audio']:
            logger.warning(
                f"Invalid response_format: {response_format}"
            )
            return Response(
                {"error": "Invalid format. Use 'text' or 'audio'"},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            logger.debug(f"Parsing passage: {passage}")
            parts = passage.split()
            if len(parts) < 2:
                logger.error(
                    f"Invalid passage format: {passage} "
                    f"(parts: {parts})"
                )
                raise ValueError(
                    "Invalid passage format. "
                    "Use 'Book Chapter' (e.g., 'John 3')"
                )

            # The book name might have spaces (e.g., "1 John")
            chapter_part = parts[-1]
            book_name = ' '.join(parts[:-1])
            logger.debug(
                f"Extracted book_name: {book_name}, "
                f"chapter_part: {chapter_part}"
            )

            # Convert book name to standard book ID
            book_id = get_dbt_book_id(book_name)
            if not book_id:
                logger.error(f"Unknown book name: {book_name}")
                raise ValueError(f"Unknown book: {book_name}")
            logger.debug(f"Resolved book_id: {book_id}")

            try:
                chapter = int(chapter_part)
                if chapter <= 0:
                    logger.error(
                        f"Invalid chapter number: {chapter} "
                        f"(must be positive)"
                    )
                    raise ValueError(
                        "Chapter must be a positive number"
                    )
                logger.debug(f"Parsed chapter number: {chapter}")
            except ValueError as ve:
                logger.error(
                    f"Failed to parse chapter: {chapter_part} - {ve}"
                )
                raise ValueError("Chapter must be a valid number")

            data = {
                'book': book_id,
                'book_name': book_name,
                'chapter': chapter,
                # Key name matches the query-param name to keep the
                # view/serializer boundary unambiguous; the serializer
                # output still surfaces the result as ``format`` for
                # backwards-compatible API consumers.
                'response_format': response_format,
                'fileset_id': fileset_id,
            }
            logger.debug(f"Prepared data for serializer: {data}")

            serializer = BiblePassageSerializer(data=data)
            if serializer.is_valid():
                logger.info(
                    f"Successfully retrieved passage: "
                    f"{book_name} {chapter} ({fileset_id})"
                )
                body = serializer.to_representation(data)
                # Provider failures come back with ``error`` /
                # ``error_code`` fields — surface them with a real
                # HTTP status so clients can branch on it.
                if 'error' in body:
                    return Response(
                        body,
                        status=PROVIDER_ERROR_STATUSES.get(
                            body.get('error_code'),
                            status.HTTP_502_BAD_GATEWAY,
                        ),
                    )
                # Surface "audio requested but not generated yet" as
                # a proper 404 instead of a 200 with ``audio_url: None``
                # so clients can reliably branch on status code.
                if (
                    body.get('format') == 'audio'
                    and body.get('audio_url') is None
                ):
                    return Response(
                        body, status=status.HTTP_404_NOT_FOUND
                    )
                return Response(body)

            logger.error(
                f"Serializer validation failed: {serializer.errors}"
            )
            return Response(
                serializer.errors,
                status=status.HTTP_400_BAD_REQUEST
            )

        except Exception as e:
            logger.exception(
                f"Error processing Bible passage request: {passage} - {e}"
            )
            # Provider failures never reach this handler — they
            # are classified inside the serializer and returned
            # above. Only client-input errors (bad passage
            # format, unknown book, invalid chapter) land here,
            # so embedding str(e) in the 400 body is safe.
            return Response(
                {"error": str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )


class AudioTimestampView(APIView):
    """Return audio timestamps for a chapter."""

    def get(self, request, format=None):
        fileset_id = request.query_params.get('fileset_id')
        book = request.query_params.get('book')
        chapter = request.query_params.get('chapter')

        if not all([fileset_id, book, chapter]):
            return Response(
                {
                    "error":
                    "fileset_id, book, and chapter "
                    "are required."
                },
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            # Convert book name to DBT book ID
            # (e.g. "John" -> "JHN")
            book_id = get_dbt_book_id(book)
            if not book_id:
                return Response(
                    {"error": f"Unknown book: {book}"},
                    status=status.HTTP_400_BAD_REQUEST
                )

            if is_sword_fileset(fileset_id):
                canon = canonical_sword_fileset_id(fileset_id)
                voice_name = get_tts_config(canon)["voice_name"]
                try:
                    payload = gcs.read_timestamps_json(
                        canon, book_id, int(chapter), voice_name,
                    )
                except gcs_exceptions.NotFound:
                    return Response(
                        {
                            "error": (
                                "Timestamps not yet generated for "
                                "this chapter."
                            ),
                        },
                        status=status.HTTP_404_NOT_FOUND,
                    )
                except Exception as exc:  # noqa: BLE001
                    # Auth / network / malformed-JSON errors are not
                    # a missing resource -- surface them as 502 so
                    # clients don't mistake them for a 404.
                    logger.exception(
                        "Failed to read timestamps for %s %s %s",
                        canon, book_id, chapter,
                    )
                    return provider_error_response(exc)
                return Response({"data": payload.get("data", [])})

            if is_apibible_fileset(fileset_id):
                meta = get_apibible_meta(fileset_id)
                audio = (
                    get_default_apibible_client()
                    .get_chapter_audio(
                        meta["audio_bible_id"],
                        book_id,
                        int(chapter),
                    )
                )
                timecodes = audio.get("timecodes") or []
                if not timecodes:
                    return Response(
                        {
                            "error": (
                                "Timestamps not yet generated for "
                                "this chapter."
                            ),
                        },
                        status=status.HTTP_404_NOT_FOUND,
                    )
                return Response({
                    "data": [
                        _apibible_timecode(tc)
                        for tc in timecodes
                    ]
                })

            dbt_client = get_default_dbt_client()
            result = dbt_client.get_timestamps(
                fileset_id, book_id, chapter
            )
            timestamps = [
                {
                    "verse_start": item.get(
                        "verse_start"
                    ),
                    "timestamp": item.get(
                        "timestamp"
                    ),
                }
                for item in result.get("data", [])
            ]
            return Response({"data": timestamps})
        except Exception as e:
            logger.exception(
                "Error fetching timestamps: %s", e
            )
            return provider_error_response(e)


class CopyrightView(APIView):
    """Return copyright info for a Bible translation."""

    def get(self, request, format=None):
        bible_id = request.query_params.get('bible_id')

        if not bible_id:
            return Response(
                {"error": "bible_id is required."},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            if is_apibible_fileset(bible_id):
                meta = get_apibible_meta(bible_id)
                bible = (
                    get_default_apibible_client()
                    .get_bible(meta['bible_id'])
                )
                copyright_text = (
                    bible.get('copyrightStatement') or ''
                )
                return Response({
                    "data": [{
                        "id": bible_id,
                        "type": "text_plain",
                        "size": "C",
                        "copyright": copyright_text,
                        "copyright_date": "",
                        "copyright_description": copyright_text,
                    }]
                })

            dbt_client = get_default_dbt_client()
            result = dbt_client.get_copyright(bible_id)

            filesets = []
            for item in (result or []):
                cr = item.get("copyright") or {}
                filesets.append({
                    "id": item.get("id"),
                    "type": item.get("type"),
                    "size": item.get("size"),
                    "copyright": cr.get("copyright", ""),
                    "copyright_date": cr.get(
                        "copyright_date", ""
                    ),
                    "copyright_description": cr.get(
                        "copyright_description", ""
                    ),
                })
            return Response({"data": filesets})
        except Exception as e:
            logger.exception(
                "Error fetching copyright: %s", e
            )
            return provider_error_response(e)


@extend_schema(
    parameters=[
        OpenApiParameter(
            name='query',
            type=OpenApiTypes.STR,
            location=OpenApiParameter.QUERY,
            description='Word or phrase to search for.',
            required=True,
        ),
        OpenApiParameter(
            name='fileset_id',
            type=OpenApiTypes.STR,
            location=OpenApiParameter.QUERY,
            description=(
                'DBT or SWORD fileset ID '
                '(e.g. ENGESV, LVSGLU8). '
                'Use ENGESV_API for ESV API search.'
            ),
            required=True,
        ),
        OpenApiParameter(
            name='limit',
            type=OpenApiTypes.INT,
            location=OpenApiParameter.QUERY,
            description=(
                'Max results per page (default 15). '
                'Response includes meta.pagination.total_pages '
                'so callers know how many pages exist.'
            ),
            required=False,
        ),
        OpenApiParameter(
            name='page',
            type=OpenApiTypes.INT,
            location=OpenApiParameter.QUERY,
            description=(
                'Result page number (default 1). '
                'Increment up to meta.pagination.total_pages '
                'to retrieve subsequent pages.'
            ),
            required=False,
        ),
        OpenApiParameter(
            name='sort_by',
            type=OpenApiTypes.STR,
            location=OpenApiParameter.QUERY,
            description='Sort field (DBT only).',
            required=False,
        ),
        OpenApiParameter(
            name='books',
            type=OpenApiTypes.STR,
            location=OpenApiParameter.QUERY,
            description=(
                'Comma-separated USFM book IDs '
                '(e.g. JHN,ROM).'
            ),
            required=False,
        ),
        OpenApiParameter(
            name='group_by',
            type=OpenApiTypes.STR,
            location=OpenApiParameter.QUERY,
            description=(
                "Use 'book' to return every match grouped "
                'by book instead of a paginated list.'
            ),
            required=False,
        ),
    ],
)
class BibleSearchView(APIView):
    """
    Search the Bible for a word or phrase.

    Returns a paginated list of matching verses. The response
    includes a ``meta.pagination`` object with ``total``,
    ``count``, ``per_page``, ``current_page``, and
    ``total_pages`` fields. To retrieve subsequent pages,
    repeat the request with an incremented ``page`` parameter
    (e.g. ``?query=Jesus&fileset_id=ENGESV&page=2``).

    With ``group_by=book`` pagination is skipped: every match
    is fetched and returned as ``data.groups`` (per-book
    ``book_id``/``count``/``verses`` groups in canonical book
    order) plus ``data.meta`` = ``{total, truncated}``.

    Query Parameters:
        - query: Word/phrase to search (required)
        - fileset_id: DBT or SWORD fileset ID (required)
                     Use ENGESV_API for ESV API search
        - limit: Max results per page (default 15)
        - page: Result page number (default 1)
        - sort_by: Sort field (DBT only)
        - books: Comma-separated USFM book IDs
        - group_by: 'book' returns all matches grouped by book

    Examples:
        - ESV API search: ?query=love&fileset_id=ENGESV_API
        - DBT search: ?query=Jesus&fileset_id=ENGESV
        - SWORD search: ?query=faith&fileset_id=LVSGLU8
    """

    def get(self, request, format=None):
        query = request.query_params.get('query')
        fileset_id = request.query_params.get('fileset_id')

        if not query:
            return Response(
                {"error": "query parameter is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not fileset_id:
            return Response(
                {"error": "fileset_id parameter is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # ``group_by`` is validated and dispatched before
        # limit/page parsing: pagination params are meaningless
        # in grouped mode, so e.g. ``page=abc`` must not 400.
        group_by = request.query_params.get('group_by')
        if group_by and group_by != 'book':
            return Response(
                {"error": "group_by must be 'book'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        sort_by = request.query_params.get('sort_by')
        books = request.query_params.get('books')

        try:
            if group_by == 'book':
                return self._grouped_search(
                    fileset_id, query, books
                )

            try:
                limit = int(
                    request.query_params.get('limit', 15)
                )
            except (TypeError, ValueError):
                return Response(
                    {"error": "limit must be an integer."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            try:
                page = int(
                    request.query_params.get('page', 1)
                )
            except (TypeError, ValueError):
                return Response(
                    {"error": "page must be an integer."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if is_esv_fileset(fileset_id):
                return self._esv_search(query, limit, page)
            if is_apibible_fileset(fileset_id):
                return self._apibible_search(
                    fileset_id, query, limit, page
                )
            if is_sword_fileset(fileset_id):
                return self._sword_search(
                    fileset_id, query, limit, page, books
                )
            return self._dbt_search(
                fileset_id, query, limit, page,
                sort_by, books,
            )
        except Exception as e:
            logger.exception(
                "Error in BibleSearchView: %s", e
            )
            return provider_error_response(e)

    def _esv_search(self, query, limit, page):
        """Search using ESV API passage search endpoint."""
        esv_client = get_default_esv_client()

        # Call ESV API search
        result = esv_client.search(query, page, limit)

        # Transform ESV API response to match our format
        verses = parse_esv_results(result.get('results', []))

        # Build pagination metadata
        pagination = {
            'total': result.get('total_results', 0),
            'count': len(verses),
            'per_page': limit,
            'current_page': result.get('page', page),
            'total_pages': result.get('total_pages', 1),
        }

        return Response({
            'data': {
                'verses': verses,
                'meta': {
                    'pagination': pagination
                }
            }
        })

    def _apibible_search(self, fileset_id, query, limit, page):
        """Search using API.Bible's offset-based search endpoint."""
        meta = get_apibible_meta(fileset_id)
        client = get_default_apibible_client()
        offset = (page - 1) * limit
        result = client.search(
            meta['bible_id'], query,
            offset=offset, limit=limit,
        )
        data = result.get('data') or {}
        verses = [
            normalize_apibible_verse(v)
            for v in data.get('verses') or []
        ]
        total = data.get('total') or 0
        total_pages = (
            (total + limit - 1) // limit if limit else 1
        )
        return Response({
            'data': {
                'verses': verses,
                'meta': {
                    'pagination': {
                        'total': total,
                        'count': len(verses),
                        'per_page': limit,
                        'current_page': page,
                        'total_pages': total_pages,
                    }
                },
            }
        })

    def _grouped_search(self, fileset_id, query, books):
        """Return every match grouped by book (``group_by=book``).

        Response shape: ``data.groups`` = per-book groups in
        canonical order; ``data.meta`` = ``{total, truncated}``
        where ``truncated`` is True when a safety cap cut
        results off.
        """
        if is_esv_fileset(fileset_id):
            verses, total, truncated = fetch_esv_matches(
                get_default_esv_client(), query
            )
        elif is_apibible_fileset(fileset_id):
            meta = get_apibible_meta(fileset_id)
            verses, total, truncated = fetch_apibible_matches(
                get_default_apibible_client(),
                meta['bible_id'], query,
            )
        elif is_sword_fileset(fileset_id):
            verses = scan_sword_matches(
                get_default_sword_client(),
                fileset_id, query, books,
            )
            total, truncated = len(verses), False
        else:
            verses, total, truncated = fetch_dbt_matches(
                get_default_dbt_client(),
                fileset_id, query, books,
            )
        return Response({
            'data': {
                'groups': group_verses_by_book(verses),
                'meta': {
                    'total': total,
                    'truncated': truncated,
                },
            }
        })

    def _dbt_search(
        self, fileset_id, query, limit, page, sort_by, books
    ):
        dbt_client = get_default_dbt_client()
        result = dbt_client.search(
            fileset_id, query,
            limit=limit, page=page,
            sort_by=sort_by, books=books,
        )
        raw_verses = result.get('verses') or {}
        verse_items = raw_verses.get('data') or []
        normalized = [
            normalize_dbt_verse(v)
            for v in verse_items
        ]
        # Pagination lives in result['meta'] (documented schema) or
        # result['verses']['meta'] (observed b4.dbt.io behaviour).
        # Normalise to a clean { pagination: { ... } } shape and
        # strip internal DBT 'links' so callers only ever use our
        # own API to paginate.
        raw_meta = (
            result.get('meta')
            or raw_verses.get('meta')
            or {}
        )
        pagination = raw_meta.get('pagination') or {}
        meta = {
            'pagination': {
                'total': pagination.get('total'),
                'count': pagination.get('count'),
                'per_page': pagination.get('per_page'),
                'current_page': (
                    pagination.get('current_page')
                ),
                'total_pages': (
                    pagination.get('total_pages')
                ),
            }
        } if pagination else {}
        return Response({
            'data': {
                'verses': normalized,
                'meta': meta,
            }
        })

    def _sword_search(
        self, fileset_id, query, limit, page, books
    ):
        matches = scan_sword_matches(
            get_default_sword_client(),
            fileset_id, query, books,
        )
        total = len(matches)
        start = (page - 1) * limit
        page_items = matches[start:start + limit]
        total_pages = (
            (total + limit - 1) // limit if limit else 1
        )
        return Response({
            'data': {
                'verses': page_items,
                'meta': {
                    'pagination': {
                        'total': total,
                        'count': len(page_items),
                        'per_page': limit,
                        'current_page': page,
                        'total_pages': total_pages,
                    }
                },
            }
        })


class TranslationListView(APIView):
    """
    Lists available Bible translations by fetching live from DBT API.

    Query Parameters:
        - language_iso: Filter by ISO language code
                       (e.g., 'eng', 'lvs')

    Example:
        /api/v1/translations/
        /api/v1/translations/?language_iso=eng
    """

    def get(self, request, *args, **kwargs):
        language_iso = request.query_params.get('language_iso')
        logger.info(
            f"TranslationListView.get called with "
            f"language_iso: {language_iso or 'all'}"
        )

        try:
            translations = TranslationService.get_live_translations(
                language_iso
            )
            logger.info(
                f"Found {len(translations)} translations "
                f"for language_iso: {language_iso or 'all'}"
            )

            response_data = [
                {
                    'abbr': t['abbr'],
                    'name': t['name'],
                    'language': t['language'],
                    'language_iso': t['iso'],
                    'filesets': t['filesets'],
                }
                for t in translations
            ]
            logger.debug(
                f"Returning {len(response_data)} translations"
            )
            return Response({'results': response_data})

        except Exception as e:
            logger.exception(
                f"Error fetching translations for "
                f"language_iso: {language_iso} - {e}"
            )
            return provider_error_response(e)
