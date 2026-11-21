"""
Client for the Digital Bible Platform (DBT) API using the generated
OpenAPI client.
"""
import logging
import os
import sys
import threading
import requests
from typing import Any, Dict, List, Optional
from django.conf import settings

# Set up logging
logger = logging.getLogger(__name__)
# Add the generated client to the Python path
client_path = os.path.join(os.path.dirname(__file__), 'dbt_client')
sys.path.append(client_path)
# Only BiblesApi is used through the generated client — the
# search/annotations/audio-timing endpoints are called via raw
# ``requests`` below (the generated deserializers for them are
# buggy), so their Api classes were removed from the vendored
# package.
from openapi_client.api.bibles_api import BiblesApi
from openapi_client.configuration import Configuration
from openapi_client.api_client import ApiClient
from openapi_client.exceptions import ApiException

from bible.utils.provider_errors import is_upstream_failure


# Bound every upstream call so a stalled b4.dbt.io socket cannot
# pin a GAE worker indefinitely. Matches the ESV/API.Bible clients.
_REQUEST_TIMEOUT_SECONDS = 10

# Transport failures where the request never got a definitive
# upstream answer — safe to replay on a read-only GET. A
# received error response (HTTPError from raise_for_status)
# is NOT transient: the upstream answered, so do not retry.
_TRANSIENT_REQUEST_ERRORS = (
    requests.exceptions.Timeout,
    requests.exceptions.ConnectionError,
)


def _log_request_error(context, exc):
    """Log a failed upstream call without exception text.

    ``requests``/``ApiException`` objects stringify with the full
    request URL — including the ``?key=`` credential this client
    sends as a query param — so only the exception type and HTTP
    status are safe to write to logs.
    """
    status_code = getattr(exc, 'status', None)
    if status_code is None:
        response = getattr(exc, 'response', None)
        if response is not None:
            status_code = getattr(response, 'status_code', None)
    logger.error(
        "%s: %s (status=%s)",
        context, type(exc).__name__, status_code,
    )


def _reraise_credential_free(context, exc):
    """Re-raise *exc* without the request URL in ``str()``.

    ``requests``/urllib3 exceptions stringify the full request
    URL — including the ``?key=`` credential — and they
    propagate into view logs and Sentry events. Re-raise a
    same-type copy that keeps ``response`` so
    ``upstream_status_code`` still sees the HTTP status.
    ``JSONDecodeError`` cannot be reconstructed (its
    ``__init__`` takes json's (msg, doc, pos) signature) so it
    degrades to a plain ``RequestException``; non-``requests``
    transport errors (urllib3) become ``ConnectionError``,
    which stays inside the upstream exception families.
    """
    response = getattr(exc, 'response', None)
    status = (
        response.status_code if response is not None else 'n/a'
    )
    if isinstance(exc, requests.exceptions.JSONDecodeError):
        raise requests.exceptions.RequestException(
            f"{context} ({type(exc).__name__}, status={status})",
            response=response,
        ) from None
    if isinstance(exc, requests.exceptions.RequestException):
        raise type(exc)(
            f"{context} ({type(exc).__name__}, status={status})",
            response=response,
        ) from None
    raise requests.exceptions.ConnectionError(
        f"{context} ({type(exc).__name__})"
    ) from None


class DBTClient:
    """
    Client for the Digital Bible Platform (DBT) API using the generated
    OpenAPI client. Provides a simplified interface for common operations.
    """

    def __init__(self, api_key: Optional[str] = None):
        """
        Initialize the DBT client with API key.

        Args:
            api_key: The DBT API key. If not provided, will try to get from
                environment.
        """

        api_key = api_key or getattr(settings, 'DBT_KEY', None)

        if not api_key:
            raise ValueError("DBT_KEY not found in settings.")

        self.api_key = api_key
        self.base_url = "https://b4.dbt.io/api"

        config = Configuration(
            host=self.base_url,
            api_key={'key': api_key}
        )

        self.api_client = ApiClient(config)

        self.bibles_api = BiblesApi(self.api_client)

        # Shared HTTP session so raw requests calls reuse TCP/TLS
        # connections across requests (DBT cold connect ~800ms,
        # warm ~300ms).
        self.session = requests.Session()

    def _make_request(self, request_func, *args, **kwargs):
        """
        Make a request to the DBT API with error handling.

        Args:
            request_func: Function to call
            *args: Arguments to pass to the function
            **kwargs: Additional query parameters

        Returns:
            API response data
        """
        # Ensure version is set
        if 'v' not in kwargs:
            kwargs['v'] = 4
        # Bound the generated client's socket wait — its default
        # ``_request_timeout`` is None (no timeout).
        kwargs.setdefault(
            '_request_timeout', _REQUEST_TIMEOUT_SECONDS
        )

        try:
            response = request_func(*args, **kwargs)
            return response
        except ApiException as e:
            _log_request_error("DBT API error", e)
            if e.status == 401:
                logger.error("Authentication failed. Check your API key.")
            raise
        except Exception as e:
            _log_request_error("DBT unexpected error", e)
            if is_upstream_failure(e):
                _reraise_credential_free("DBT request failed", e)
            raise

    def get_bibles(self, language: str = None, **kwargs) -> Dict[str, Any]:
        """
        Get available Bibles, optionally filtered by language.

        Args:
            language: ISO 639-3 language code (optional)
            **kwargs: Additional query parameters

        Returns:
            Dictionary with Bible data
        """
        params = kwargs.copy()
        params['v'] = 4
        params['key'] = self.api_key

        if language:
            params['language_code'] = language

        try:
            response = self.session.get(
                f"{self.base_url}/bibles", params=params,
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            _log_request_error("DBT bibles request error", e)
            if e.response is not None and (
                e.response.status_code == 401
            ):
                logger.error("Authentication failed. Check your API key.")
            _reraise_credential_free(
                "DBT bibles request failed", e
            )

    def get_bible(self, bible_id: str, **kwargs) -> Dict[str, Any]:
        """
        Get details for a specific Bible.

        Args:
            bible_id: Bible ID
            **kwargs: Additional query parameters

        Returns:
            Dictionary with Bible details
        """
        return self._make_request(
            self.bibles_api.v4_bible_one,
            bible_id,
            **kwargs
        )

    def get_books(self, bible_id: str, **kwargs) -> Dict[str, Any]:
        """
        Get books for a specific Bible.

        Uses requests directly — the generated OpenAPI client
        deserializes ``v4_bible_books`` into a ``V4BibleBooksData``
        model whose ``openapi_types`` reference phantom classes
        (``Id``, ``Name``, ``Chapters``…) that were never
        generated, so every call dies in deserialization with
        ``AttributeError: module 'openapi_client.models' has no
        attribute 'Id'``. The raw JSON body is also what callers
        expect (``result.get('data')``), unlike the model objects
        ``_make_request`` would return.

        Args:
            bible_id: Bible ID
            **kwargs: Additional query parameters
                (``verify_content``, ``book_id``, …)

        Returns:
            Dictionary with book data
        """
        params = kwargs.copy()
        params['v'] = 4
        params['key'] = self.api_key

        try:
            response = self.session.get(
                f"{self.base_url}/bibles/{bible_id}/book",
                params=params,
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.JSONDecodeError:
            # ``response.json()`` failures carry no URL in
            # ``str()`` and re-raising ``type(e)(...)`` below
            # would crash — its ``__init__`` takes json's
            # (msg, doc, pos) signature, not ``response=``.
            raise
        except requests.exceptions.RequestException as e:
            _log_request_error("DBT books request error", e)
            if e.response is not None and (
                e.response.status_code == 401
            ):
                logger.error(
                    "Authentication failed. Check your API key."
                )
            _reraise_credential_free(
                "DBT books request failed", e
            )

    def get_book(
        self,
        bible_id: str,
        book_id: str,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Get details for a specific book.

        Args:
            bible_id: Bible ID
            book_id: Book ID
            **kwargs: Additional query parameters

        Returns:
            Dictionary with book details
        """
        # Note: There is no direct endpoint for a specific book
        # Use the books endpoint and filter the results
        books = self.get_books(bible_id, **kwargs)
        for book in books.get('data') or []:
            if (
                isinstance(book, dict)
                and book.get('book_id') == book_id
            ):
                return {'data': book}
        return {'data': None}

    def get_chapters(
        self,
        bible_id: str,
        book_id: str,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Get chapters for a specific book.

        Args:
            bible_id: Bible ID
            book_id: Book ID
            **kwargs: Additional query parameters

        Returns:
            Dictionary with chapter data
        """
        # Note: Need to get the chapters from the book endpoint
        books = self.get_books(bible_id, **kwargs)
        for book in books.get('data') or []:
            if (
                isinstance(book, dict)
                and book.get('book_id') == book_id
                and 'chapters' in book
            ):
                return {'data': book['chapters']}
        return {'data': []}

    def get_verses(
        self,
        book: str,
        chapter: str,
        bible_id: str = "ENGESV",
        **kwargs
    ) -> Dict[str, Any]:
        """
        Get verses for a specific chapter.

        Args:
            bible_id: Bible ID (called fileset_id in the DBT API)
            book: Book ID
            chapter: Chapter ID
            **kwargs: Additional query parameters

        Returns:
            Dictionary with verse data
        """
        return self._make_request(
                self.bibles_api.v4_bible_filesets_show_chapter,
                bible_id,
                book,
                chapter,
                **kwargs
            )

    def get_fileset_files(
        self,
        fileset_id: str,
        **kwargs
    ) -> Dict[str, Any]:
        """
        List the audio files a fileset actually covers.

        ``GET /bibles/filesets/{fileset_id}`` returns one row per
        chapter file for the fileset's *own* coverage (unlike
        ``/bibles/{id}/book``, which is keyed on the bible id and
        unions every sibling fileset). Each row carries
        ``book_id``, ``chapter_start``, ``path`` (signed MP3 URL)
        and ``duration`` — the generate_audio_timestamps worklist
        needs exactly this.

        Uses requests directly — the generated OpenAPI client has
        no method for this route.

        Args:
            fileset_id: Audio fileset ID (e.g. 'LATLVRN1DA')
            **kwargs: Additional query parameters

        Returns:
            Dictionary with file rows under 'data'
        """
        params = kwargs.copy()
        params['v'] = 4
        params['key'] = self.api_key

        url = f"{self.base_url}/bibles/filesets/{fileset_id}"

        try:
            response = self.session.get(
                url, params=params,
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            _log_request_error(
                "DBT fileset files request error", e
            )
            _reraise_credential_free(
                "DBT fileset files request failed", e
            )

    def get_timestamps(
        self,
        fileset_id: str,
        book: str,
        chapter: str,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Get audio timestamps for a chapter.

        Uses requests directly instead of the generated
        API client to avoid a deserialization bug in the
        V4AudioTimestampsData model (missing BookId type).

        Args:
            fileset_id: Audio fileset ID
            book: Book ID (e.g. 'JHN')
            chapter: Chapter number
            **kwargs: Additional query parameters

        Returns:
            Dictionary with timestamp data
        """
        params = kwargs.copy()
        params['v'] = 4
        params['key'] = self.api_key

        url = (
            f"{self.base_url}/timestamps/"
            f"{fileset_id}/{book}/{chapter}"
        )

        # One immediate replay absorbs a transient ReadTimeout —
        # DBT occasionally stalls on an otherwise healthy route,
        # and a 502 here only degrades verse highlighting anyway.
        # The call is a read-only GET, so replaying is safe.
        for attempt in range(2):
            try:
                response = self.session.get(
                    url, params=params,
                    timeout=_REQUEST_TIMEOUT_SECONDS,
                )
                response.raise_for_status()
                return response.json()
            except requests.exceptions.RequestException as e:
                _log_request_error(
                    "DBT timestamps request error", e
                )
                if (
                    attempt == 0
                    and isinstance(e, _TRANSIENT_REQUEST_ERRORS)
                ):
                    continue
                _reraise_credential_free(
                    "DBT timestamps request failed", e
                )

    def get_copyright(
        self,
        bible_id: str,
        **kwargs
    ) -> List[Dict[str, Any]]:
        """
        Get copyright info for a Bible.

        Uses requests directly instead of the generated
        API client to avoid a deserialization bug
        (missing Id model in openapi_client.models).

        Args:
            bible_id: Bible ID (e.g. 'ENGESV')
            **kwargs: Additional query parameters

        Returns:
            List of fileset copyright objects
        """
        params = kwargs.copy()
        params['v'] = 4
        params['key'] = self.api_key

        url = f"{self.base_url}/bibles/{bible_id}/copyright"

        try:
            response = self.session.get(
                url, params=params,
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            _log_request_error(
                "DBT copyright request error", e
            )
            _reraise_credential_free(
                "DBT copyright request failed", e
            )

    def search(
        self,
        bible_id: str,
        query: str,
        limit: int = None,
        page: int = None,
        sort_by: str = None,
        books: str = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Search for text in a specific Bible.

        Uses requests directly instead of the generated
        API client to avoid a deserialization bug
        (missing Id model in openapi_client.models).

        Args:
            bible_id: Bible ID
            query: Search query
            limit: Max results per page
            page: Page number
            sort_by: Field to sort by
            books: Comma-separated USFM book IDs
            **kwargs: Additional query parameters

        Returns:
            Dictionary with search results
        """
        params = kwargs.copy()
        params['v'] = 4
        params['key'] = self.api_key
        params['query'] = query
        params['fileset_id'] = bible_id
        if limit is not None:
            params['limit'] = limit
        if page is not None:
            params['page'] = page
        if sort_by is not None:
            params['sort_by'] = sort_by
        if books is not None:
            params['books'] = books

        url = f"{self.base_url}/search"

        try:
            response = self.session.get(
                url, params=params,
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            _log_request_error(
                "DBT search request error", e
            )
            _reraise_credential_free(
                "DBT search request failed", e
            )

    def get_languages(
        self,
        language_name: str = None,
        language_code: str = None,
        page: int = None,
        limit: int = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        List languages that have Bible content in DBT.

        Uses requests directly, like ``get_bibles``/``search``:
        the endpoint only lists content-bearing languages and
        returns ``meta.pagination`` for page traversal.

        Args:
            language_name: Case-insensitive substring match on
                the language name (optional)
            language_code: Exact ISO 639-3 code lookup (optional)
            page: Result page number (optional)
            limit: Max results per page (optional)
            **kwargs: Additional query parameters

        Returns:
            Dictionary with language data and meta.pagination
        """
        params = kwargs.copy()
        params['v'] = 4
        params['key'] = self.api_key
        if language_name:
            params['language_name'] = language_name
        if language_code:
            params['language_code'] = language_code
        if page is not None:
            params['page'] = page
        if limit is not None:
            params['limit'] = limit

        url = f"{self.base_url}/languages"

        try:
            response = self.session.get(
                url, params=params,
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            _log_request_error(
                "DBT languages request error", e
            )
            _reraise_credential_free(
                "DBT languages request failed", e
            )


_default_client: Optional["DBTClient"] = None
_default_client_lock = threading.Lock()


def get_default_dbt_client() -> "DBTClient":
    """Return a process-wide shared DBTClient.

    Reusing a single client (and its underlying urllib3 PoolManager /
    requests.Session) across requests keeps the HTTPS connection to
    b4.dbt.io warm. Measured effect: chapter fetch drops from
    ~850-900ms (new TCP+TLS every call) to ~300ms (warm keep-alive).
    """
    global _default_client
    if _default_client is None:
        with _default_client_lock:
            if _default_client is None:
                _default_client = DBTClient()
    return _default_client
