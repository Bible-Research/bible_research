"""Pytest configuration: run the suite against production
settings.

The real ``bible_research.settings`` module is imported (no
hand-rolled parallel settings), so tests see the production
``REST_FRAMEWORK`` authentication/permission stack and
``MIDDLEWARE`` chain. Only the deltas a test run genuinely
requires are overridden:

- ``DATABASES`` → SQLite in-memory (the local config.yaml DB is
  a read-only production user and must never receive test
  writes).
- Provider keys/secrets → dummy test values (tests mock the
  provider clients; the keys only need to exist).
- Throttle rates → generous values: throttle history lives in
  the process-wide locmem cache shared across tests, so the
  real rates (e.g. ``register: 10/min``) would trip the suite —
  dedicated throttle tests shrink the rate via ``patch.dict``.
"""
import os

os.environ.setdefault(
    'DJANGO_SETTINGS_MODULE', 'bible_research.settings'
)

from django.conf import settings


def pytest_configure():
    """Apply the test-run deltas over production settings."""
    if settings.configured and getattr(
        settings, '_PYTEST_OVERRIDES_APPLIED', False
    ):
        return

    settings.DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': ':memory:',
        }
    }

    # Provider credentials — tests patch the clients, so these
    # only need to exist for clients constructed unpatched.
    settings.DBT_KEY = 'test-key'
    settings.ESV_KEY = 'test-key'
    settings.API_BIBLE_KEY = 'test-key'
    settings.GEMINI_API_KEYS = ['test-gemini-key']
    settings.SECRET_KEY = (
        settings.SECRET_KEY or 'a-test-secret-for-pytest'
    )

    # Deterministic GCS targets (never reached — GCS calls are
    # mocked in tests).
    settings.AUDIO_BUCKET_NAME = 'test-audio-bucket'
    settings.IMAGE_BUCKET_ORIGINALS = 'test-image-bucket'
    settings.IMAGE_BUCKET_THUMBNAILS = (
        'test-image-bucket-thumbs'
    )

    # Generous throttle rates — the shared locmem cache keeps
    # history across tests; the production rates would 429 the
    # suite. ``DEFAULT_THROTTLE_RATES`` is bound into
    # ``SimpleRateThrottle.THROTTLE_RATES`` at import time, so
    # the test rates must be set before pytest-django calls
    # ``django.setup()`` (this hook runs first).
    rest = dict(settings.REST_FRAMEWORK)
    rest['DEFAULT_THROTTLE_RATES'] = {
        'anon': '10000/min',
        'search': '1000/min',
        'token': '1000/min',
        'register': '1000/min',
    }
    settings.REST_FRAMEWORK = rest

    settings._PYTEST_OVERRIDES_APPLIED = True


def prod_auth_stack(cls):
    """Class decorator kept for backwards compatibility.

    With the test suite running on the real production
    ``REST_FRAMEWORK``/``MIDDLEWARE`` (see ``pytest_configure``),
    the production auth stack is already the default — this is
    now a no-op shim so existing decorated tests keep working.
    """
    return cls
