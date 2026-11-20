"""Helpers for describing upstream Bible provider failures.

The DBT OpenAPI client (``ApiException.status``), the
``requests``-based clients (``HTTPError.response.status_code``)
and the ``google.api_core`` GCS exceptions (``exc.code``) all
carry the upstream HTTP status, so a single helper can classify
failures from any provider.
"""
from contextlib import contextmanager
from typing import Dict, Optional


class PassageNotFoundError(ValueError):
    """The provider has no such book/chapter/passage.

    Raised for missing content detected without an upstream HTTP
    status (e.g. a SWORD book/chapter lookup, or a DBT response
    with empty ``data``) so serializers can report ``not_found``
    instead of a generic ``provider_error``. Subclasses
    ``ValueError`` for backward compatibility with callers that
    caught the previous exception type.

    ``client_message`` optionally carries a fixed, safe message
    the caller may surface in the response body (e.g. the SWORD
    generated-audio miss explains *why* the fileset lacks the
    chapter). ``None`` means the default coverage-miss message.
    """

    def __init__(self, message='', client_message=None):
        super().__init__(message)
        self.client_message = client_message


class ProviderConfigError(ValueError):
    """A provider's required credential/config is missing.

    Raised by provider clients at construction (e.g. an unset or
    empty ``API_BIBLE_KEY``/``ESV_KEY``/``DBT_KEY``). Subclasses
    ``ValueError`` for backward compatibility with callers that
    caught the previous type. A missing key is a deployment bug
    on our side, not an upstream outage — ``is_upstream_failure``
    already routes it to error-level ``internal_error``, and
    ``provider_error_fields`` surfaces a fixed message rather
    than the bare ``ValueError`` class name.
    """


class InvalidSearchCursorError(ValueError):
    """The ``cursor`` query param is not a decodable scan
    position — a client-input failure (400
    ``invalid_parameter``), deliberately NOT a provider error
    even though it is raised inside the provider layer.
    Subclasses ``ValueError`` for consistency with the other
    parameter-validation failures.
    """


class TimestampsNotGeneratedError(PassageNotFoundError):
    """The audio exists but its timestamp sidecar does not yet.

    Raised by the provider facade when a GCS ``_timestamps.json``
    artifact or an API.Bible ``timecodes`` list is missing — the
    timestamps endpoint maps it to its dedicated "not yet
    generated" 404 while every other caller still sees a plain
    ``PassageNotFoundError``.
    """


def upstream_status_code(exc: Exception) -> Optional[int]:
    """Extract the HTTP status an upstream provider returned.

    Returns ``None`` when the exception carries no status
    (e.g. network errors or client-side failures).
    """
    status = getattr(exc, 'status', None)
    if isinstance(status, int):
        return status
    response = getattr(exc, 'response', None)
    status_code = getattr(response, 'status_code', None)
    if isinstance(status_code, int):
        return status_code
    # google.api_core exceptions expose the HTTP status as
    # ``.code`` — an int for HTTP failures (e.g. a GCS
    # TooManyRequests carries 429). gRPC-sourced errors expose a
    # StatusCode enum instead, which is intentionally not
    # classified as an upstream HTTP status.
    code = getattr(exc, 'code', None)
    if isinstance(code, int):
        return code
    return None


def provider_error_fields(
    exc: Exception,
    not_found_message: Optional[str] = None,
) -> Dict[str, str]:
    """Build ``error``/``error_code`` fields for a provider failure.

    ``error_code`` is ``rate_limited`` for upstream HTTP 429,
    ``not_found`` when the passage does not exist upstream
    (``PassageNotFoundError`` or upstream HTTP 404), and
    ``provider_error`` for any other failure.

    ``not_found_message`` overrides the "Passage not found" text
    for endpoints whose lookup is not a passage (e.g.
    ``copyright``) — it only rewords ``error``; the ``not_found``
    code stays stable for the frontend.

    ``str(exc)`` is never embedded in ``error``: exception text can
    contain request URLs with credentials (e.g. DBT's ``?key=``
    query param in urllib3 ``MaxRetryError`` messages) or upstream
    response bodies. Only the exception class name is surfaced;
    the full exception is already logged by the callers.
    """
    status = upstream_status_code(exc)
    if status == 429:
        return {
            'error':
                'Bible provider rate limit exceeded (HTTP 429)',
            'error_code': 'rate_limited',
        }
    if isinstance(exc, PassageNotFoundError) or status == 404:
        detail = f' (HTTP {status})' if status else ''
        return {
            'error': (
                f'{not_found_message or "Passage not found"}'
                f'{detail}'
            ),
            'error_code': 'not_found',
        }
    if status:
        return {
            'error': f'Bible provider error (HTTP {status})',
            'error_code': 'provider_error',
        }
    if isinstance(exc, ProviderConfigError):
        # A missing credential is a deployment-side outage, not
        # a failed request — say so instead of leaking the bare
        # ``ValueError`` class name the client used to raise.
        return {
            'error': 'Bible provider is not configured',
            'error_code': 'provider_error',
        }
    return {
        'error': (
            'Bible provider request failed '
            f'({type(exc).__name__})'
        ),
        'error_code': 'provider_error',
    }


# Only upstream outages are worth trending in Sentry; codes for
# routine user-input outcomes are deliberately excluded.
_CAPTURED_ERROR_CODES = frozenset({'rate_limited', 'provider_error'})


def _upstream_exception_families():
    """Exception classes that mean "the upstream service failed".

    Transport/SDK errors that can only be raised while talking to
    a provider — ``requests``/``urllib3`` failures, the DBT
    OpenAPI ``ApiException``, and ``google.api_core`` errors.
    Everything importable lazily so a missing optional dependency
    degrades to status-based classification only (a vendored-API
    ``ApiException`` still carries ``.status``).
    """
    families = []
    try:
        import requests
        families.append(requests.RequestException)
    except ImportError:
        pass
    try:
        import urllib3
        families.append(urllib3.exceptions.HTTPError)
    except ImportError:
        pass
    try:
        from google.api_core import exceptions as _gax
        families.append(_gax.GoogleAPIError)
    except ImportError:
        pass
    try:
        # Importable only after dbt/client.py has appended its
        # vendored ``dbt_client`` dir to ``sys.path``.
        from openapi_client.exceptions import ApiException
        families.append(ApiException)
    except Exception:
        pass
    return tuple(families)


def is_upstream_failure(exc: Exception) -> bool:
    """True when *exc* was caused by the upstream service.

    Upstream-caused means the exception carries an upstream HTTP
    status or belongs to a transport/SDK family that can only
    fail while calling the provider. Everything else — e.g. a
    ``TypeError`` in response shaping — is our bug and must not
    be reported as a routine provider blip.
    """
    if upstream_status_code(exc) is not None:
        # google.api_core/gemini-style ``.code`` values in the
        # 400-403 range are our-side config/IAM/request bugs,
        # not upstream outages — report them as internal.
        code = getattr(exc, 'code', None)
        if isinstance(code, int) and 400 <= code < 404:
            return False
        return True
    families = _upstream_exception_families()
    if not families or not isinstance(exc, families):
        return False
    # A handful of ``requests`` exceptions are client-side bugs
    # (a malformed URL we built), not upstream failures.
    try:
        import requests
        client_bugs = (
            requests.exceptions.MissingSchema,
            requests.exceptions.InvalidSchema,
            requests.exceptions.InvalidURL,
            requests.exceptions.InvalidHeader,
            requests.exceptions.InvalidProxyURL,
        )
        if isinstance(exc, client_bugs):
            return False
    except ImportError:
        pass
    return True


def _sentry_capture(exc, error_code, level, tags=None,
                    contexts=None):
    """Send *exc* to Sentry tagged ``error_code`` at *level*.

    ``tags`` adds triage keys (``fileset_id``, ``provider``,
    ``endpoint``, ``job``); ``contexts`` attaches named context
    blocks. None values are skipped. The fingerprint keys on
    ``{{ default }}`` + ``error_code`` (+ ``fileset_id`` when
    known) so one bad fileset cannot spawn unbounded issue
    groups. No-ops when sentry-sdk is not installed or no
    ``SENTRY_DSN`` was configured at startup (the SDK is then
    uninitialized and ``capture_exception`` does nothing). The
    exception object is passed, never a formatted string —
    consistent with ``provider_error_fields`` keeping
    ``str(exc)`` out of responses because it can embed
    credential-bearing URLs.
    """
    try:
        import sentry_sdk
    except ImportError:
        return
    with sentry_sdk.new_scope() as scope:
        scope.set_tag('error_code', error_code)
        for key, value in (tags or {}).items():
            if value is not None:
                scope.set_tag(key, str(value))
        for name, context in (contexts or {}).items():
            scope.set_context(name, context)
        fingerprint = ['{{ default }}', error_code]
        fileset_id = (tags or {}).get('fileset_id')
        if fileset_id:
            fingerprint.append(str(fileset_id))
        scope.fingerprint = fingerprint
        sentry_sdk.capture_exception(exc, level=level)


def capture_provider_exception(exc, error_code=None, tags=None,
                               contexts=None, upstream=None):
    """Send an upstream provider failure to Sentry, when enabled.

    ``rate_limited`` and ``provider_error`` failures caused by
    the upstream service are outages worth trending, so they are
    tagged with ``error_code`` and captured at ``warning`` level.
    Codes for routine user-input outcomes — ``not_found``
    (``PassageNotFoundError`` or upstream 404, i.e. typos) and
    ``book_not_in_fileset`` — are never sent. An exception that
    is NOT upstream-caused (``is_upstream_failure``) is a bug in
    our own code that happened to be caught at the provider
    boundary — it routes to ``capture_internal_exception`` so it
    reaches the error-level queue instead of hiding among
    warnings.

    ``upstream`` overrides classification: pass ``True`` for a
    typed provider exception that carries no HTTP status but can
    only mean the upstream failed (e.g. a job-level
    ``EdgeTTSError`` after its internal retries); ``False``
    forces error-level ``internal_error``.
    """
    if error_code is None:
        error_code = provider_error_fields(exc)['error_code']
    if error_code not in _CAPTURED_ERROR_CODES:
        return
    if upstream is None:
        upstream = is_upstream_failure(exc)
    if not upstream:
        capture_internal_exception(exc, tags=tags,
                                   contexts=contexts)
        return
    _sentry_capture(exc, error_code, 'warning',
                    tags=tags, contexts=contexts)


def capture_internal_exception(exc, tags=None, contexts=None):
    """Send a genuine server-side failure to Sentry, when enabled.

    ``DjangoIntegration`` only reports *uncaught* exceptions, so
    a bug caught by a view's catch-all — or our own
    infrastructure failing behind a degraded field (e.g. GCS
    signed-URL generation) — stays invisible unless reported
    explicitly. These are real bugs and outages, not routine
    upstream blips, so they are captured at ``error`` level under
    the ``internal_error`` code the 500 catch-all already
    returns.
    """
    _sentry_capture(exc, 'internal_error', 'error',
                    tags=tags, contexts=contexts)


def sentry_capture_message(message, level='error', tags=None):
    """Send a free-form message to Sentry, when enabled.

    Used by the Cloud Run job for aggregate failures — processes
    whose lifecycle has no request context and whose
    ``CommandError``/``SystemExit`` exits bypass every
    exception-based integration.
    """
    try:
        import sentry_sdk
    except ImportError:
        return
    with sentry_sdk.new_scope() as scope:
        for key, value in (tags or {}).items():
            if value is not None:
                scope.set_tag(key, str(value))
        sentry_sdk.capture_message(message, level=level)


def sentry_flush(timeout=5):
    """Flush pending Sentry events before a short-lived process
    exits. No-ops when sentry-sdk is not installed."""
    try:
        import sentry_sdk
    except ImportError:
        return
    sentry_sdk.flush(timeout=timeout)


@contextmanager
def sentry_span(op, description=None, data=None):
    """``sentry_sdk.start_span`` as an optional-dependency-safe
    context manager.

    Yields the span (a ``NoOpSpan`` when the SDK is
    uninitialized) or ``None`` when sentry-sdk is not installed,
    so instrumented code paths work identically with monitoring
    off.
    """
    try:
        import sentry_sdk
    except ImportError:
        yield None
        return
    with sentry_sdk.start_span(
        op=op, name=description,
    ) as span:
        for key, value in (data or {}).items():
            span.set_data(key, value)
        yield span
