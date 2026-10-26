import logging
import re

from django.core.cache import cache
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from bible.utils.bible_books import get_dbt_book_id
from bible.utils.error_responses import (
    MISSING_PARAMETER,
    bad_request,
    not_found,
)
from bible.utils.provider_errors import (
    PassageNotFoundError,
    TimestampsNotGeneratedError,
    capture_provider_exception,
    provider_error_fields,
)
from bible.utils.request_params import (
    is_valid_bible_id,
    is_valid_book_id_list,
    is_valid_book_name,
    is_valid_fileset_id,
    is_valid_language_iso,
)
from bible.services.providers import (
    get_passage_body,
    provider_for,
)
from .serializers import BiblePassageSerializer
from .services.translation_service import TranslationService
from .services.dbt.client import get_default_dbt_client

logger = logging.getLogger(__name__)

PROVIDER_ERROR_STATUSES = {
    'rate_limited': status.HTTP_429_TOO_MANY_REQUESTS,
    'not_found': status.HTTP_404_NOT_FOUND,
    'book_not_in_fileset': status.HTTP_404_NOT_FOUND,
}

# Trailing token of a ``passage`` value: "Chapter",
# "Chapter:Verse", "Chapter:Start-End", or a comma-separated
# mix — e.g. "3", "3:16", "3:16-18", "3:16-18,20".
_CHAPTER_PART_RE = re.compile(
    r"^(\d+)(?::(\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*))?$"
)

# Bounds for the ``passage`` input and its verse grammar. This
# endpoint is AllowAny: without caps a value like
# ``John 3:1-999999999`` expands into ~10^9 ints in memory
# (unauthenticated DoS). 200 covers the longest chapter in the
# canon (Psalms 119 has 176 verses).
_MAX_PASSAGE_LENGTH = 100
_MAX_VERSE_SEGMENTS = 25
_MAX_VERSE_NUMBER = 200

# Bound for the ``query`` param of ``BibleSearchView`` — upstream
# providers reject absurdly long queries anyway, and the local
# SWORD scan pays per character.
_MAX_SEARCH_QUERY_LENGTH = 200

# Response-cache TTLs (S7). Translations change rarely and every
# anonymous request used to cost a live DBT ``/bibles`` call;
# copyright text is static per (provider, bible_id). Both get
# ``Cache-Control: public, max-age=<ttl>`` so CDN/browser caches
# match. Degraded/error responses are never cached.
_TRANSLATIONS_CACHE_TTL_SECONDS = 6 * 60 * 60
_COPYRIGHT_CACHE_TTL_SECONDS = 24 * 60 * 60


def _parse_chapter_part(chapter_part):
    """Parse the trailing ``chapter[:verses]`` token of a passage.

    Returns ``(chapter, verses)`` where ``verses`` is a sorted
    list of requested verse numbers, or ``None`` when only a
    chapter was given (whole-chapter retrieval).
    """
    match = _CHAPTER_PART_RE.match(chapter_part)
    if not match:
        raise ValueError(
            "Invalid passage format. Use 'Book Chapter' "
            "(e.g., 'John 3') or 'Book Chapter:Verse[-End]' "
            "(e.g., 'John 3:16', 'John 3:16-18')"
        )
    chapter = int(match.group(1))
    if chapter <= 0:
        raise ValueError("Chapter must be a positive number")
    selector = match.group(2)
    if not selector:
        return chapter, None
    segments = selector.split(',')
    if len(segments) > _MAX_VERSE_SEGMENTS:
        raise ValueError(
            f"Too many verse segments "
            f"(max {_MAX_VERSE_SEGMENTS})"
        )
    verses = set()
    for segment in segments:
        bounds = segment.split('-')
        start = int(bounds[0])
        end = int(bounds[1]) if len(bounds) > 1 else start
        if start < 1 or end < start:
            raise ValueError(
                "Verse selectors must be positive numbers "
                "with Start <= End (e.g., '3:16-18')"
            )
        if end > _MAX_VERSE_NUMBER:
            raise ValueError(
                f"Verse numbers must be between 1 and "
                f"{_MAX_VERSE_NUMBER}"
            )
        verses.update(range(start, end + 1))
    return chapter, sorted(verses)


class SearchRateThrottle(UserRateThrottle):
    """Scoped throttle for ``BibleSearchView``.

    Search is the most expensive anonymous endpoint — grouped
    mode fans out to ~30 upstream provider calls per request —
    so it gets a tighter scope than the generous global
    ``anon`` rate. ``UserRateThrottle.get_cache_key`` already
    covers both callers: authenticated key on their pk,
    anonymous on the (proxy-aware) client IP.
    """
    scope = 'search'


def provider_error_response(exc, not_found_message=None):
    """Build an error ``Response`` for an upstream provider failure.

    Uses ``provider_error_fields`` so raw exception text — which can
    contain request URLs carrying credentials (e.g. DBT's ``?key=``
    query param) — never reaches the response body.

    ``not_found_message`` overrides the default "Passage not found"
    wording for endpoints that look up something else (e.g.
    copyright info) — the ``error_code`` contract is unchanged.
    """
    fields = provider_error_fields(
        exc, not_found_message=not_found_message
    )
    # Trend upstream outages in Sentry (warning-level, tagged
    # with error_code); no-ops for not_found and when Sentry
    # is off.
    capture_provider_exception(exc, fields['error_code'])
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
        - passage: Book and chapter (e.g., '2 Chronicles 14'),
          optionally with a verse selector
          (e.g., 'John 3:16', 'John 3:16-18', 'John 3:16,18')
        - response_format: 'text' or 'audio' (default: 'text')
        - fileset_id: DBT fileset ID (e.g. ENGESV) or bundled SWORD id
          (e.g. LVSGLU8). SWORD translations also accept listing ``abbr``
          (e.g. GLU8 for Latvian Glück).

    Examples:
        /api/v1/bible/?passage=John+3&fileset_id=ENGESV
        /api/v1/bible/?passage=John+3&fileset_id=LVSGLU8 (LV text)
        # LV audio:
        /api/v1/bible/?passage=Luke+20&fileset_id=GLU8&response_format=audio
    """

    permission_classes = [AllowAny]

    def get(self, request, format=None):
        passage = request.query_params.get('passage')
        response_format = request.query_params.get(
            'response_format', 'text'
        )
        # An explicit empty ``?fileset_id=`` falls back to the
        # default rather than validating as garbage.
        fileset_id = (
            request.query_params.get('fileset_id') or 'ENGESV'
        )

        logger.info(
            f"BiblePassageView.get called with passage: {passage}, "
            f"format: {response_format}, fileset_id: {fileset_id}"
        )

        if not passage:
            logger.warning("Request missing required 'passage' parameter")
            return bad_request(
                "Passage parameter is required. "
                "Example: ?passage=John+3",
                code=MISSING_PARAMETER,
            )

        if len(passage) > _MAX_PASSAGE_LENGTH:
            logger.warning(
                "Passage parameter too long: %d chars",
                len(passage),
            )
            return bad_request(
                f"Passage parameter is too long "
                f"(max {_MAX_PASSAGE_LENGTH} characters)."
            )

        if response_format not in ['text', 'audio']:
            logger.warning(
                f"Invalid response_format: {response_format}"
            )
            return bad_request(
                "Invalid format. Use 'text' or 'audio'"
            )

        # ``fileset_id`` selects the provider and is interpolated
        # into upstream URL paths on the DBT branch — reject
        # anything outside the id charset before routing.
        if not is_valid_fileset_id(fileset_id):
            logger.warning(
                "Invalid fileset_id charset: %r", fileset_id
            )
            return bad_request(
                "fileset_id contains invalid characters."
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

            # The book name might have spaces (e.g., "1 John");
            # the final token is 'Chapter' or
            # 'Chapter:Verse[-End][,...]'.
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
                chapter, verse_numbers = _parse_chapter_part(
                    chapter_part
                )
                logger.debug(
                    f"Parsed chapter {chapter}, "
                    f"verse selector: {verse_numbers}"
                )
            except ValueError as ve:
                logger.error(
                    f"Failed to parse chapter part: "
                    f"{chapter_part} - {ve}"
                )
                raise

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
            if verse_numbers:
                data['verses'] = verse_numbers
            logger.debug(f"Prepared data for serializer: {data}")

            serializer = BiblePassageSerializer(data=data)
            if serializer.is_valid():
                logger.info(
                    f"Successfully retrieved passage: "
                    f"{book_name} {chapter} ({fileset_id})"
                )
                # Provider dispatch + fetch + shaping live in
                # the service layer — the serializer only
                # validates the request boundary.
                body = get_passage_body(serializer.validated_data)
                # Provider failures come back with ``error`` /
                # ``error_code`` fields — surface them with a real
                # HTTP status so clients can branch on it. This
                # includes ``book_not_in_fileset`` (404) so clients
                # can fall back to another fileset.
                if 'error' in body:
                    return Response(
                        body,
                        status=PROVIDER_ERROR_STATUSES.get(
                            body.get('error_code'),
                            status.HTTP_502_BAD_GATEWAY,
                        ),
                    )
                # Defensive: a serializer path reporting an audio
                # miss as ``audio_url: None`` without the canonical
                # error body still answers 404 with the stable
                # error code and the same body shape.
                if (
                    body.get('format') == 'audio'
                    and body.get('audio_url') is None
                ):
                    return Response(
                        {
                            'book': body.get('book'),
                            'book_name': body.get('book_name'),
                            'chapter': body.get('chapter'),
                            'error': body.get(
                                'message',
                                'Audio not available for this '
                                'passage',
                            ),
                            'error_code': 'book_not_in_fileset',
                        },
                        status=status.HTTP_404_NOT_FOUND,
                    )
                return Response(body)

            logger.error(
                f"Serializer validation failed: {serializer.errors}"
            )
            return bad_request(
                "Invalid request parameters."
            )

        except PassageNotFoundError:
            # ``PassageNotFoundError`` subclasses ValueError, so
            # it must be caught before the generic handler: a
            # provider "no such passage" is a 404, and its text
            # must never land in a ``bad_request`` body if
            # provider calls move into this try block.
            return not_found("Passage not found.")
        except ValueError as e:
            # Every ValueError reaching this handler carries one
            # of the fixed client-input messages raised above or
            # inside ``_parse_chapter_part`` — provider failures
            # are classified inside the serializer or caught
            # above — so the text holds no upstream URLs or
            # credentials.
            return bad_request(str(e))
        except Exception:
            logger.exception(
                "Unexpected error processing passage request: %s",
                passage,
            )
            return Response(
                {
                    "error": (
                        "Unexpected error processing the passage."
                    ),
                    "error_code": "internal_error",
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


@extend_schema(
    parameters=[
        OpenApiParameter(
            name='fileset_id',
            type=OpenApiTypes.STR,
            location=OpenApiParameter.QUERY,
            description=(
                'Audio fileset ID (e.g. ENGESVN2DA, '
                'LVSGLU8C1DA, ENGNIVC1DA).'
            ),
            required=True,
        ),
        OpenApiParameter(
            name='book',
            type=OpenApiTypes.STR,
            location=OpenApiParameter.QUERY,
            description=(
                'Book name (e.g. John) — resolved to a '
                'USFM book id.'
            ),
            required=True,
        ),
        OpenApiParameter(
            name='chapter',
            type=OpenApiTypes.INT,
            location=OpenApiParameter.QUERY,
            description='Chapter number (positive integer).',
            required=True,
        ),
    ],
)
class AudioTimestampView(APIView):
    """Return audio timestamps for a chapter."""

    permission_classes = [AllowAny]

    def get(self, request, format=None):
        fileset_id = request.query_params.get('fileset_id')
        book = request.query_params.get('book')
        chapter = request.query_params.get('chapter')

        if not all([fileset_id, book, chapter]):
            return bad_request(
                "fileset_id, book, and chapter are required.",
                code=MISSING_PARAMETER,
            )

        # ``fileset_id`` selects the provider and ``book`` is
        # resolved to a book id interpolated upstream — validate
        # both charsets before routing.
        if not is_valid_fileset_id(fileset_id):
            return bad_request(
                "fileset_id contains invalid characters."
            )
        if not is_valid_book_name(book):
            return bad_request(
                "book contains invalid characters."
            )

        # A non-numeric chapter is a client error — reject with
        # 400 instead of letting ValueError surface as a 502
        # provider_error downstream.
        try:
            chapter_num = int(chapter)
        except (TypeError, ValueError):
            chapter_num = 0
        if chapter_num < 1:
            return bad_request(
                "chapter must be a positive integer."
            )

        try:
            # Convert book name to DBT book ID
            # (e.g. "John" -> "JHN")
            book_id = get_dbt_book_id(book)
            if not book_id:
                return bad_request(f"Unknown book: {book}")

            # Provider dispatch lives in the facade: SWORD reads
            # the generated GCS sidecar, API.Bible maps its
            # timecodes, everything else hits DBT timestamps.
            data = provider_for(fileset_id).get_timestamps(
                book_id, chapter_num
            )
            return Response({"data": data})
        except TimestampsNotGeneratedError:
            # The audio exists but its timestamps artifact was
            # not generated yet (GCS sidecar missing, or an
            # empty API.Bible timecodes list).
            return not_found(
                "Timestamps not yet generated for "
                "this chapter."
            )
        except Exception as e:
            logger.exception(
                "Error fetching timestamps: %s", e
            )
            return provider_error_response(e)


@extend_schema(
    parameters=[
        OpenApiParameter(
            name='bible_id',
            type=OpenApiTypes.STR,
            location=OpenApiParameter.QUERY,
            description=(
                'Bible/fileset id (e.g. ENGESV, LVSGLU8, '
                'ENGNIV_API) or a listing abbr (GLU8, NIV). '
                'Named ``bible_id`` for historical reasons — '
                'it accepts the same id space as '
                '``fileset_id`` on the other endpoints.'
            ),
            required=True,
        ),
    ],
)
class CopyrightView(APIView):
    """Return copyright info for a Bible translation.

    Successful responses are cached for
    ``_COPYRIGHT_CACHE_TTL_SECONDS`` — copyright text is static —
    and carry ``Cache-Control: public`` for CDN/browser caches.
    Error and client-input responses are never cached.
    """

    permission_classes = [AllowAny]

    def get(self, request, format=None):
        bible_id = request.query_params.get('bible_id')

        if not bible_id:
            return bad_request(
                "bible_id is required.",
                code=MISSING_PARAMETER,
            )
        # ``bible_id`` is interpolated into upstream URL paths
        # on the DBT branch — validate the charset first.
        if not is_valid_bible_id(bible_id):
            return bad_request(
                "bible_id contains invalid characters."
            )

        try:
            # Non-DBT ids resolve through the facade: ESV/SWORD
            # (and abbrs like GLU8/NIV) answer from registry
            # license metadata instead of 404ing through DBT;
            # API.Bible still fetches the live copyright
            # statement; everything else proxies DBT.
            provider = provider_for(bible_id)
            # The cache key covers every input that changes the
            # output: the resolved provider and the raw id.
            cache_key = (
                f"v1:copyright:"
                f"{type(provider).__name__}:{bible_id}"
            )
            cached = cache.get(cache_key)
            if cached is not None:
                response = Response(cached)
            else:
                data = provider.get_copyright()
                body = {"data": data}
                cache.set(
                    cache_key, body,
                    _COPYRIGHT_CACHE_TTL_SECONDS,
                )
                response = Response(body)
            response['Cache-Control'] = (
                f"public, max-age="
                f"{_COPYRIGHT_CACHE_TTL_SECONDS}"
            )
            return response
        except Exception as e:
            logger.exception(
                "Error fetching copyright: %s", e
            )
            return provider_error_response(
                e, not_found_message='Copyright not found'
            )


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

    permission_classes = [AllowAny]
    # Replaces the global anon throttle: grouped mode fans out to
    # ~30 upstream calls per request, so search gets a tighter
    # scope covering both anonymous (per-IP) and authenticated
    # (per-user) callers.
    throttle_classes = [SearchRateThrottle]

    def get(self, request, format=None):
        query = request.query_params.get('query')
        fileset_id = request.query_params.get('fileset_id')

        # A whitespace-only query must not pass the required check:
        # ``' ' in text`` matches ~31k verses per SWORD chapter and
        # turns the local scan into a whole-Bible crawl.
        query = (query or '').strip()
        if not query:
            return bad_request(
                "query parameter is required.",
                code=MISSING_PARAMETER,
            )
        if len(query) > _MAX_SEARCH_QUERY_LENGTH:
            return bad_request(
                f"query must be at most "
                f"{_MAX_SEARCH_QUERY_LENGTH} characters."
            )
        if not fileset_id:
            return bad_request(
                "fileset_id parameter is required.",
                code=MISSING_PARAMETER,
            )
        # ``fileset_id`` selects the provider and reaches
        # upstream URL paths on the DBT/API.Bible branches.
        if not is_valid_fileset_id(fileset_id):
            return bad_request(
                "fileset_id contains invalid characters."
            )

        # ``group_by`` is validated and dispatched before
        # limit/page parsing: pagination params are meaningless
        # in grouped mode, so e.g. ``page=abc`` must not 400.
        group_by = request.query_params.get('group_by')
        if group_by and group_by != 'book':
            return bad_request("group_by must be 'book'.")

        sort_by = request.query_params.get('sort_by')
        books = request.query_params.get('books')
        # ``books`` is a comma-separated USFM id list forwarded
        # to providers — reject anything else.
        if books and not is_valid_book_id_list(books):
            return bad_request(
                "books must be comma-separated 3-letter "
                "book ids (e.g. JHN,ROM)."
            )

        try:
            # Provider dispatch lives in the facade — grouped
            # mode skips pagination parsing entirely (``page``
            # is meaningless there).
            if group_by == 'book':
                payload = provider_for(
                    fileset_id
                ).search_grouped(
                    query, books, sort_by=sort_by
                )
                return Response({'data': payload})

            try:
                limit = int(
                    request.query_params.get('limit', 15)
                )
            except (TypeError, ValueError):
                return bad_request(
                    "limit must be an integer."
                )
            try:
                page = int(
                    request.query_params.get('page', 1)
                )
            except (TypeError, ValueError):
                return bad_request(
                    "page must be an integer."
                )
            # Non-positive pagination params are client errors —
            # 400 instead of sending e.g. a negative ``offset``
            # to API.Bible.
            if limit < 1 or page < 1:
                return bad_request(
                    "limit and page must be positive integers."
                )

            # Each provider returns the normalized
            # ``{verses, meta}`` payload.
            payload = provider_for(fileset_id).search(
                query, limit, page,
                sort_by=sort_by, books=books,
            )
            return Response({'data': payload})
        except Exception as e:
            logger.exception(
                "Error in BibleSearchView: %s", e
            )
            return provider_error_response(e)


@extend_schema(
    parameters=[
        OpenApiParameter(
            name='language_iso',
            type=OpenApiTypes.STR,
            location=OpenApiParameter.QUERY,
            description=(
                'Filter by ISO 639-2/639-3 language code '
                '(e.g. eng, lvs). Omit to list all '
                'translations.'
            ),
            required=False,
        ),
    ],
)
class TranslationListView(APIView):
    """
    Lists available Bible translations, merging the live DBT
    ``/bibles`` list with the bundled registry translations.

    Successful responses are cached per ``language_iso`` for
    ``_TRANSLATIONS_CACHE_TTL_SECONDS`` and carry
    ``Cache-Control: public`` — an anonymous request used to cost
    a live DBT call every time.

    A DBT outage does not fail the request: the service falls
    back to the static registry entries and the response carries
    ``"degraded": true`` (never cached). A failure outside the
    DBT fetch still surfaces as 502 ``provider_error``.

    Query Parameters:
        - language_iso: Filter by ISO language code
                       (e.g., 'eng', 'lvs')

    Example:
        /api/v1/bible/translations/
        /api/v1/bible/translations/?language_iso=eng
    """

    permission_classes = [AllowAny]

    def get(self, request, *args, **kwargs):
        language_iso = request.query_params.get('language_iso')
        logger.info(
            f"TranslationListView.get called with "
            f"language_iso: {language_iso or 'all'}"
        )
        # ISO codes are 2-3 letters; anything else is garbage
        # that would be forwarded to the DBT language filter.
        if language_iso and not is_valid_language_iso(language_iso):
            return bad_request(
                "language_iso must be a 2-3 letter ISO code."
            )

        # The cache key covers every input that changes the
        # output: the iso filter (case-insensitive; ''/missing
        # both mean "all").
        cache_key = (
            f"v1:translations:"
            f"{(language_iso or '').lower() or 'all'}"
        )
        cached = cache.get(cache_key)
        if cached is not None:
            response = Response(cached)
            response['Cache-Control'] = (
                f"public, max-age="
                f"{_TRANSLATIONS_CACHE_TTL_SECONDS}"
            )
            return response

        try:
            translations, dbt_failed = (
                TranslationService.get_live_translations(
                    language_iso
                )
            )
            logger.info(
                f"Found {len(translations)} translations "
                f"for language_iso: {language_iso or 'all'}"
            )

            # Defensive mapping: a malformed service row must
            # degrade to null fields, not KeyError → a
            # misleading 502 provider_error.
            response_data = [
                {
                    'abbr': t.get('abbr'),
                    'name': t.get('name'),
                    'language': t.get('language'),
                    'language_iso': t.get('iso'),
                    'filesets': t.get('filesets') or [],
                    'text_options': t.get('text_options') or [],
                    'audio_options': t.get('audio_options') or [],
                }
                for t in translations
            ]
            logger.debug(
                f"Returning {len(response_data)} translations"
            )
            body = {
                'results': response_data,
                'degraded': dbt_failed,
            }
            # Degraded responses are intentionally cached neither
            # server-side nor downstream — they must recover as
            # soon as DBT does.
            response = Response(body)
            if dbt_failed:
                response['Cache-Control'] = 'no-store'
            else:
                cache.set(
                    cache_key, body,
                    _TRANSLATIONS_CACHE_TTL_SECONDS,
                )
                response['Cache-Control'] = (
                    f"public, max-age="
                    f"{_TRANSLATIONS_CACHE_TTL_SECONDS}"
                )
            return response

        except Exception as e:
            logger.exception(
                f"Error fetching translations for "
                f"language_iso: {language_iso} - {e}"
            )
            return provider_error_response(e)


class LanguageListView(APIView):
    """
    Lists languages that have Bible content, fetched live from
    the DBT API.

    Query Parameters:
        - search: Case-insensitive substring match on the
          language name (e.g. 'russ' -> Russian, Buryat)
        - code: Exact ISO 639-3 language code lookup
          (e.g. 'rus')
        - page: Result page number (DBT default 1)
        - limit: Max results per page (DBT clamps to ~150)

    Each result is ``{iso, name, autonym, bibles, filesets}``
    and the response carries the whitelisted fields of DBT's
    ``meta.pagination`` (``{}`` when absent) so callers can
    page through the full list.

    Example:
        /api/v1/bible/languages/?search=russ
        /api/v1/bible/languages/?code=rus
        /api/v1/bible/languages/?page=2&limit=50
    """

    permission_classes = [AllowAny]

    def get(self, request, *args, **kwargs):
        search = request.query_params.get('search')
        code = request.query_params.get('code')
        logger.info(
            f"LanguageListView.get called with "
            f"search: {search or '-'}, code: {code or '-'}"
        )

        try:
            page = request.query_params.get('page')
            if page is not None:
                try:
                    page = int(page)
                except (TypeError, ValueError):
                    return bad_request(
                        "page must be an integer."
                    )
            limit = request.query_params.get('limit')
            if limit is not None:
                try:
                    limit = int(limit)
                except (TypeError, ValueError):
                    return bad_request(
                        "limit must be an integer."
                    )
            # Non-positive pagination params are client errors —
            # 400 instead of sending e.g. a negative ``page`` to
            # DBT (matches BibleSearchView).
            if (
                (page is not None and page < 1)
                or (limit is not None and limit < 1)
            ):
                return bad_request(
                    "limit and page must be positive integers."
                )

            dbt_client = get_default_dbt_client()
            result = dbt_client.get_languages(
                language_name=search,
                language_code=code,
                page=page,
                limit=limit,
            )

            results = [
                {
                    'iso': item.get('iso'),
                    'name': item.get('name'),
                    'autonym': item.get('autonym'),
                    'bibles': item.get('bibles'),
                    'filesets': item.get('filesets'),
                }
                for item in (result.get('data') or [])
            ]
            # Normalise to a clean { pagination: { ... } } shape
            # and whitelist the documented fields, like the
            # facade's DBT search, so a missing meta.pagination
            # emits {} rather than null.
            pagination = (result.get('meta') or {}).get(
                'pagination'
            ) or {}
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
            logger.info(
                f"Returning {len(results)} languages "
                f"(page: {page or 'default'})"
            )
            return Response({
                'results': results,
                'meta': meta,
            })

        except Exception as e:
            logger.exception(
                f"Error fetching languages "
                f"(search: {search}, code: {code}) - {e}"
            )
            return provider_error_response(e)
