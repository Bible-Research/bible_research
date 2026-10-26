"""Helpers for describing upstream Bible provider failures.

The DBT OpenAPI client (``ApiException.status``), the
``requests``-based clients (``HTTPError.response.status_code``)
and the ``google.api_core`` GCS exceptions (``exc.code``) all
carry the upstream HTTP status, so a single helper can classify
failures from any provider.
"""
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


def _sentry_capture(exc, error_code, level):
    """Send *exc* to Sentry tagged ``error_code`` at *level*.

    No-ops when sentry-sdk is not installed or no ``SENTRY_DSN``
    was configured at startup (the SDK is then uninitialized and
    ``capture_exception`` does nothing). The exception object is
    passed, never a formatted string — consistent with
    ``provider_error_fields`` keeping ``str(exc)`` out of
    responses because it can embed credential-bearing URLs.
    """
    try:
        import sentry_sdk
    except ImportError:
        return
    with sentry_sdk.new_scope() as scope:
        scope.set_tag('error_code', error_code)
        sentry_sdk.capture_exception(exc, level=level)


def capture_provider_exception(exc, error_code=None):
    """Send an upstream provider failure to Sentry, when enabled.

    ``rate_limited`` and ``provider_error`` failures are upstream
    outages worth trending, so they are tagged with ``error_code``
    and captured at ``warning`` level. Codes for routine
    user-input outcomes — ``not_found`` (``PassageNotFoundError``
    or upstream 404, i.e. typos) and ``book_not_in_fileset`` — are
    never sent.
    """
    if error_code is None:
        error_code = provider_error_fields(exc)['error_code']
    if error_code not in _CAPTURED_ERROR_CODES:
        return
    _sentry_capture(exc, error_code, 'warning')


def capture_internal_exception(exc):
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
    _sentry_capture(exc, 'internal_error', 'error')
