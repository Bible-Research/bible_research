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

import pytest
from django.conf import settings
from django.core.cache import cache

from bible.services import chapter_cache


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
    # An empty SECRET_KEY raises ImproperlyConfigured on
    # attribute access, so it cannot be probed with ``or`` —
    # set it outright; the real key is irrelevant under
    # SQLite-backed tests.
    settings.SECRET_KEY = 'a-test-secret-for-pytest'

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


@pytest.fixture(autouse=True)
def _isolated_locmem_cache():
    """Give every test an empty locmem cache.

    The default cache is process-wide and shared: it holds DRF
    throttle history plus the provider-response caches
    (translations, copyright, GCS timestamps JSON). Without
    clearing, a cached response from an earlier test would make
    later tests see stale data or skip mocked upstream calls.
    """
    cache.clear()
    yield
    cache.clear()


@pytest.fixture(autouse=True)
def _isolated_chapter_cache():
    """Give every test an empty in-process chapter cache.

    ``chapter_cache`` holds provider chapter payloads
    process-wide — like locmem, a hit from an earlier test would
    skip a later test's mocked upstream call, and a
    negative-cached ``PassageNotFoundError`` would keep
    surfacing a stale coverage miss.
    """
    chapter_cache.clear()
    yield
    chapter_cache.clear()
