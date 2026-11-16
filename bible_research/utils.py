"""Shared helpers for the ``bible_research`` project package."""
import os
import re
from urllib.parse import urlparse

from django.core.exceptions import DisallowedHost

# Auth endpoints whose request bodies carry credentials (token
# login at ``/api/token``, registration under ``/api/v1/users/``).
# ``sentry_before_send`` strips the parsed body for these paths so
# passwords can never reach Sentry. ``/api/token`` is matched as a
# bare prefix so both the slash-less and trailing-slash variants
# (and any nested token endpoints) are covered.
_SENSITIVE_PATH_PREFIXES = (
    '/api/token',
    '/api/v1/users/',
)

# Exception types that are pure scanner noise — an automated probe
# hitting the API with a bogus ``Host`` header produces a 400, not
# a bug. ``sentry_before_send`` drops the event entirely.
_SCANNER_NOISE_EXCEPTIONS = (DisallowedHost,)

# DBT (and signed-URL) credentials ride in ``?key=``/``&key=``
# query params; transport exceptions stringify the full URL into
# the event's exception value. Defense in depth on top of the
# credential-free re-raises in ``dbt/client.py`` — a missed path
# still cannot leak the key.
_CREDENTIAL_QUERY_RE = re.compile(
    r'([?&](?:key|X-Amz-Signature|sig|token)=)[^&\s"\']+'
)

# Interactive docs/schema routes serve humans, not API traffic —
# excluded from tracing so they don't crowd the sampled pool.
_DOCS_PATH_PREFIXES = (
    '/api/v1/docs',
    '/api/v1/redoc',
    '/api/v1/schema',
)

# Sample rates: low-traffic API (single F1), so a generous share
# of real API calls is kept; everything else stays at the floor.
_API_TRACES_SAMPLE_RATE = 0.3
_DEFAULT_TRACES_SAMPLE_RATE = 0.1
PROFILES_SAMPLE_RATE = 0.1


def sentry_release():
    """The release value for ``sentry_sdk.init``.

    ``SENTRY_RELEASE`` wins when set (Cloud Run jobs carry no
    ``GAE_VERSION`` — an image SHA or scheduler tag can be
    injected there); ``GAE_VERSION`` is the per-deploy GAE
    fallback. ``None`` leaves events release-less.
    """
    return (
        os.environ.get('SENTRY_RELEASE')
        or os.environ.get('GAE_VERSION')
    )


def sentry_before_send(event, hint):
    """Filter/scrub a Sentry event before it leaves the process.

    Drops pure scanner noise (``DisallowedHost`` and friends — a
    bogus ``Host`` header is a 400, not a defect).

    Then drops the parsed request body (``request.data``) for
    auth endpoints — those bodies contain passwords. Defense in
    depth on top of ``send_default_pii=False`` and Sentry's
    default denylist/server-side scrubbers. URL, method, and
    headers are kept so the event stays debuggable.
    """
    exc_info = hint.get('exc_info') if hint else None
    if (
        exc_info
        and exc_info[1] is not None
        and isinstance(exc_info[1], _SCANNER_NOISE_EXCEPTIONS)
    ):
        return None

    for value in (
        event.get('exception', {}).get('values') or ()
    ):
        text = value.get('value')
        if isinstance(text, str):
            value['value'] = _CREDENTIAL_QUERY_RE.sub(
                r'\1<redacted>', text,
            )

    request = event.get('request')
    if not isinstance(request, dict):
        return event
    path = urlparse(request.get('url') or '').path
    if path.startswith(_SENSITIVE_PATH_PREFIXES):
        request.pop('data', None)
    return event


def sentry_traces_sampler(sampling_context):
    """Per-route trace sample rate for ``traces_sampler``.

    ``0`` for GAE health checks (``/_ah/``) and the docs/schema
    routes — they add traffic, not signal. ``0.3`` for ``/api/``
    endpoints (traffic is low enough to keep a real share);
    ``0.1`` for anything else (admin, the root redirect).
    """
    environ = sampling_context.get('wsgi_environ') or {}
    path = environ.get('PATH_INFO') or ''
    if path.startswith('/_ah/'):
        return 0.0
    if path.startswith(_DOCS_PATH_PREFIXES):
        return 0.0
    if path.startswith('/api/'):
        return _API_TRACES_SAMPLE_RATE
    return _DEFAULT_TRACES_SAMPLE_RATE
