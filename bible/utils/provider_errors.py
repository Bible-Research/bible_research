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


def provider_error_fields(exc: Exception) -> Dict[str, str]:
    """Build ``error``/``error_code`` fields for a provider failure.

    ``error_code`` is ``rate_limited`` for upstream HTTP 429,
    ``not_found`` when the passage does not exist upstream
    (``PassageNotFoundError`` or upstream HTTP 404), and
    ``provider_error`` for any other failure.

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
            'error': f'Passage not found{detail}',
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
