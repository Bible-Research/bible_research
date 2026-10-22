from django.conf import settings


def pytest_configure():
    if not settings.configured:
        settings.configure(
            DBT_KEY='test-key',
            GOOGLE_TTS_LANGUAGE_CODE='lv-LV',
            GOOGLE_TTS_VOICE_NAME='lv-LV-Chirp3-HD-Sadachbia',
            GOOGLE_TTS_SAMPLE_RATE_HERTZ=24000,
            AUDIO_BUCKET_NAME='test-audio-bucket',
            AUDIO_SIGNED_URL_TTL_SECONDS=3600,
            IMAGE_BUCKET_ORIGINALS='test-image-bucket',
            IMAGE_MAX_BYTES=10 * 1024 * 1024,
            IMAGE_SIGNED_URL_TTL_SECONDS=600,
            MONTHLY_TTS_CHAR_LIMIT=100000,
            LOCK_STALE_HOURS=24,
            GEMINI_API_KEYS=['test-gemini-key'],
            # A minimal set of settings required for DRF and Django to run
            # tests
            INSTALLED_APPS=[
                'django.contrib.admin',
                'django.contrib.auth',
                'django.contrib.contenttypes',
                'django.contrib.sessions',
                'django.contrib.messages',
                'rest_framework',
                'rest_framework.authtoken',
                'drf_spectacular',
                'bible',
                'annotations.apps.AnnotationsConfig',
                'users.apps.UsersConfig',
            ],
            DATABASES={
                'default': {
                    'ENGINE': 'django.db.backends.sqlite3',
                    'NAME': ':memory:',
                }
            },
            # Minimal middleware setup
            MIDDLEWARE=[
                'django.middleware.security.SecurityMiddleware',
                'django.contrib.sessions.middleware.SessionMiddleware',
                'django.middleware.common.CommonMiddleware',
                'django.middleware.csrf.CsrfViewMiddleware',
                'django.contrib.auth.middleware.AuthenticationMiddleware',
                'django.contrib.messages.middleware.MessageMiddleware',
                'django.middleware.clickjacking.XFrameOptionsMiddleware',
            ],
            TEMPLATES=[{
                'BACKEND': (
                    'django.template.backends'
                    '.django.DjangoTemplates'
                ),
                'DIRS': [],
                'APP_DIRS': True,
                'OPTIONS': {
                    'context_processors': [
                        'django.template.context_processors'
                        '.debug',
                        'django.template.context_processors'
                        '.request',
                        'django.contrib.auth'
                        '.context_processors.auth',
                        'django.contrib.messages'
                        '.context_processors.messages',
                    ],
                },
            }],
            # Full URL conf so comment API routes resolve
            ROOT_URLCONF='bible_research.urls',
            # Suppress warning about default secret key
            SECRET_KEY='a-test-secret-for-pytest',
        )


# Exact copy of the production MIDDLEWARE in
# bible_research/settings.py — includes CorsMiddleware and
# (unlike the pytest settings above) excludes
# CsrfViewMiddleware. Used only by tests that opt in via the
# ``prod_auth_stack`` class decorator below.
PROD_MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

# Exact copy of the production REST_FRAMEWORK in
# bible_research/settings.py — token auth only; the API no
# longer accepts cookie sessions.
PROD_REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework.authentication.TokenAuthentication',
        'bible_research.authentication.BearerTokenAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
        'bible_research.permissions.GuestReadOnly',
    ],
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
}


def prod_auth_stack(cls):
    """
    Class decorator: run a Django ``TestCase`` under the
    production request/auth stack — prod ``MIDDLEWARE`` and
    prod ``REST_FRAMEWORK`` authentication/permission
    defaults.

    Usage notes:

    - Instantiate ``APIClient()`` INSIDE the decorated test or
      ``setUp``: ``BaseHandler.load_middleware`` reads
      ``settings.MIDDLEWARE`` lazily per client, so the
      override must already be active when the client is
      created.
    - Anonymous requests ARE unauthenticated under this stack:
      nothing provisions an identity from headers anymore, so
      unauthenticated writes return 401.
    - ``APIView.authentication_classes`` and
      ``APIView.permission_classes`` were bound to concrete
      lists at DRF import time (no ``REST_FRAMEWORK`` in the
      pytest settings above), so ``override_settings`` alone
      cannot rebind them — this decorator patches both class
      attributes with the real prod classes, mirroring the
      ``TestGuestReadOnly`` patch pattern in
      ``annotations/tests.py``.
    - Function views wrapped by ``@api_view`` (e.g.
      ``users.views.current_user``) copied
      ``APIView.authentication_classes`` onto their generated
      ``WrappedAPIView`` at import time; to exercise prod auth
      on such a view also patch
      ``<view>.cls.authentication_classes`` locally (see
      ``users/test_auth_stack.py``).

    All imports are deferred to call time: ``conftest`` itself
    is imported before ``pytest_configure`` configures Django
    settings, so importing DRF at module level would raise
    ``ImproperlyConfigured``.
    """
    from unittest.mock import patch

    from django.test import override_settings
    from rest_framework.authentication import TokenAuthentication
    from rest_framework.permissions import IsAuthenticated
    from rest_framework.views import APIView

    from bible_research.authentication import (
        BearerTokenAuthentication,
    )
    from bible_research.permissions import GuestReadOnly

    # override_settings(REST_FRAMEWORK=...) does NOT rebind
    # APIView class attributes bound at import time — patch
    # them with the real prod classes.
    cls = patch.object(
        APIView,
        'authentication_classes',
        [
            TokenAuthentication,
            BearerTokenAuthentication,
        ],
    )(cls)
    cls = patch.object(
        APIView,
        'permission_classes',
        [IsAuthenticated, GuestReadOnly],
    )(cls)
    cls = override_settings(
        MIDDLEWARE=PROD_MIDDLEWARE,
        REST_FRAMEWORK=PROD_REST_FRAMEWORK,
    )(cls)
    return cls
