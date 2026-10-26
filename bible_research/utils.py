"""Shared helpers for the ``bible_research`` project package."""
from urllib.parse import urlparse

# Auth endpoints whose request bodies carry credentials (token
# login at ``/api/token/``, registration under ``/api/v1/users/``).
# ``sentry_before_send`` strips the parsed body for these paths so
# passwords can never reach Sentry.
_SENSITIVE_PATH_PREFIXES = (
    '/api/token/',
    '/api/v1/users/',
)


def sentry_before_send(event, hint):
    """Scrub sensitive request data before a Sentry event sends.

    Drops the parsed request body (``request.data``) for auth
    endpoints — those bodies contain passwords. Defense in depth
    on top of ``send_default_pii=False`` and Sentry's default
    denylist/server-side scrubbers. URL, method, and headers are
    kept so the event stays debuggable.
    """
    request = event.get('request') or {}
    path = urlparse(request.get('url') or '').path
    if path.startswith(_SENSITIVE_PATH_PREFIXES):
        request.pop('data', None)
    return event
