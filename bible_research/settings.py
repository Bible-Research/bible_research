"""
Django settings for bible_research project.

For more information on this file, see
https://docs.djangoproject.com/en/6.1/topics/settings/

For the full list of settings and their values, see
https://docs.djangoproject.com/en/6.1/ref/settings/
"""

import logging
import os
import sys
from pathlib import Path

import dj_database_url
from corsheaders.defaults import default_headers

from .utils import sentry_before_send

from .dotenv import load_dotenv, load_dotenv_file

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',

    # Third-party apps
    'rest_framework',
    'rest_framework.authtoken',
    'corsheaders',
    'drf_spectacular',

    # My apps
    'bible.apps.BibleConfig',
    'annotations.apps.AnnotationsConfig',
    'users.apps.UsersConfig',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

CORS_ALLOW_ALL_ORIGINS = True

# Let Sentry's distributed-tracing headers through CORS preflight
# so frontend errors correlate with backend traces (the default
# header safelist does not include them).
CORS_ALLOW_HEADERS = (
    *default_headers,
    'sentry-trace',
    'baggage',
)

# Allow all Vercel automatically deployed PR preview origins
CORS_ALLOWED_ORIGIN_REGEXES = [
    r"^https://.*-vincent-uches-projects\.vercel\.app$",
]

ROOT_URLCONF = 'bible_research.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'bible_research.wsgi.application'


# --- Environment-Specific Configuration ---

# Config sources, in precedence order:
#   1. ``DOTENV`` env var — its VALUE is full .env contents. Cloud Run
#      injects the consolidated ``env-file`` secret this way
#      (--set-secrets=DOTENV=env-file:latest), needing no API call.
#   2. ``DOTENV_PATH`` env var — path to a .env file to load.
#   3. GCP branch — a single ``env-file`` Secret Manager entry fetched
#      below when neither DOTENV nor DOTENV_PATH was provided.
#   4. Local dev — ``.env`` next to manage.py, else the legacy
#      ``config.yaml`` (kept for backwards compatibility).
# Parsed values use setdefault semantics — real env vars always win.

IS_GCP_ENVIRONMENT = (
    os.environ.get('GAE_APPLICATION') is not None
    or os.environ.get('K_SERVICE') is not None
    or os.environ.get('CLOUD_RUN_JOB') is not None
)

_dotenv_loaded = False
if os.environ.get('DOTENV'):
    load_dotenv(os.environ['DOTENV'])
    _dotenv_loaded = True
elif os.environ.get('DOTENV_PATH'):
    load_dotenv_file(os.environ['DOTENV_PATH'])
    _dotenv_loaded = True

# Where a PEM from DB_SSL_CERT/capem is materialised at runtime.
CA_PEM_PATH = '/tmp/bible-research-postgres-ca.pem'


def _write_ca_pem(pem_source):
    """Write the Postgres CA cert to CA_PEM_PATH.

    Accepts a PEM stored with real newlines, ``\\n`` escape sequences,
    or collapsed onto a single line with spaces.
    """
    if os.path.exists(CA_PEM_PATH):
        return
    pem_source = pem_source.replace('\\n', '\n')
    if '\n' in pem_source:
        pem_content = pem_source
    else:
        lines = pem_source.replace(
            "-----BEGIN CERTIFICATE----- ",
            "-----BEGIN CERTIFICATE-----\n"
        ).replace(
            " -----END CERTIFICATE-----",
            "\n-----END CERTIFICATE-----"
        )
        parts = lines.split("\n")
        if len(parts) >= 3:
            base64_content = ''.join(parts[1:-1]).replace(' ', '')
            formatted_content = textwrap.fill(base64_content, 64)
            pem_content = (
                "-----BEGIN CERTIFICATE-----\n"
                f"{formatted_content}\n"
                "-----END CERTIFICATE-----"
            )
        else:
            pem_content = pem_source
    with open(CA_PEM_PATH, 'w') as f:
        f.write(pem_content)


def _db_config_from_env():
    """Build the Django ``default`` database dict from env vars.

    ``DATABASE_URL`` wins; discrete ``DB_*`` parts are the next
    fallback; local SQLite is the last resort. A CA PEM provided via
    ``DB_SSL_CERT`` (or its ``capem`` alias) is written to CA_PEM_PATH
    and wired up as sslmode=require + sslrootcert.
    """
    database_url = os.environ.get('DATABASE_URL')
    if database_url:
        db_config = dj_database_url.parse(database_url)
    elif os.environ.get('DB_NAME'):
        db_config = {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': os.environ.get('DB_NAME'),
            'USER': os.environ.get('DB_USER'),
            'PASSWORD': os.environ.get('DB_PASSWORD'),
            'HOST': os.environ.get('DB_HOST'),
            'PORT': os.environ.get('DB_PORT', '5432'),
        }
    else:
        db_config = {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': str(BASE_DIR / 'db.sqlite3'),
        }

    cert = os.environ.get('DB_SSL_CERT') or os.environ.get('capem')
    if cert:
        _write_ca_pem(cert)
        db_config.setdefault('OPTIONS', {})
        db_config['OPTIONS']['sslmode'] = 'require'
        db_config['OPTIONS']['sslrootcert'] = CA_PEM_PATH
    return db_config


def _apply_env_config():
    """Populate app config from os.environ.

    Shared by every environment whose config arrives as a .env payload
    (GCP ``env-file`` secret, DOTENV injection, local .env) or as
    plain environment variables.
    """
    global SECRET_KEY, DBT_KEY, ESV_KEY, API_BIBLE_KEY, DEBUG
    global DATABASES, _audio_settings_source

    SECRET_KEY = os.environ.get('SECRET_KEY')
    DBT_KEY = os.environ.get('DBT_KEY')
    ESV_KEY = os.environ.get('ESV_KEY')
    API_BIBLE_KEY = os.environ.get('API_BIBLE_KEY')
    DEBUG = os.environ.get('DEBUG', 'False') == 'True'
    DATABASES = {'default': _db_config_from_env()}
    _audio_settings_source = os.environ


if IS_GCP_ENVIRONMENT:
    # --- Production on Google Cloud (App Engine / Cloud Run) ---
    if not _dotenv_loaded:
        from .gcp import get_secret
        load_dotenv(get_secret('env-file'))
    _apply_env_config()

else:
    # --- Local Development ---
    if not _dotenv_loaded:
        env_file = BASE_DIR / '.env'
        if env_file.exists():
            load_dotenv_file(env_file)
            _dotenv_loaded = True

    if _dotenv_loaded:
        _apply_env_config()
    else:
        import yaml
        CONFIG_FILE = BASE_DIR / 'config.yaml'
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, 'r') as f:
                config = yaml.safe_load(f)
        elif 'pytest' in sys.modules:
            # pytest runs against production-like settings with no
            # secrets file: conftest overrides DATABASES (SQLite
            # in-memory) and the provider keys with test values.
            config = {}
        else:
            raise FileNotFoundError(
                '.env not found. Copy .env.example to .env and fill '
                'in your values (the legacy config.yaml is still '
                'supported).'
            )

        SECRET_KEY = config.get('SECRET_KEY')
        DBT_KEY = config.get('DBT_KEY')
        ESV_KEY = config.get('ESV_KEY')
        API_BIBLE_KEY = config.get('API_BIBLE_KEY')
        DEBUG = config.get('DEBUG', True)
        DATABASES = config.get('DATABASES')

        if DATABASES and 'default' in DATABASES:
            opts = DATABASES['default'].get('OPTIONS', {})
            if 'sslrootcert' in opts:
                cert_path = opts['sslrootcert']
                opts['sslrootcert'] = str(BASE_DIR / cert_path)

        _audio_settings_source = config

# Tests always run on a local throwaway sqlite DB: the configured
# connection is a read-only user against production that cannot
# create the test database.
if 'test' in sys.argv:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }

# --- Sentry error monitoring ---
# The DSN is optional in every environment: absent or empty leaves
# the SDK uninitialized (a complete no-op), so local dev, CI, and a
# production deploy made before the secret exists all stay silent.
SENTRY_DSN = None
if IS_GCP_ENVIRONMENT:
    # Env var first, then Secret Manager — the same graceful
    # pattern as API_BIBLE_KEY. The SENTRY_DSN secret may not
    # exist yet, so the lookup must never break startup.
    SENTRY_DSN = os.environ.get('SENTRY_DSN')
    if not SENTRY_DSN:
        try:
            from .gcp import get_secret
            SENTRY_DSN = get_secret('SENTRY_DSN')
        except Exception:
            SENTRY_DSN = None
else:
    SENTRY_DSN = _audio_settings_source.get('SENTRY_DSN')

if SENTRY_DSN:
    import sentry_sdk
    from sentry_sdk.integrations.django import DjangoIntegration
    from sentry_sdk.integrations.logging import LoggingIntegration

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[
            DjangoIntegration(),
            # INFO+ log records become breadcrumbs but are NOT
            # turned into events — existing code logs routine
            # user-input validation at ERROR, which would flood
            # the project with non-bugs. Real failures reach
            # Sentry via DjangoIntegration (unhandled 500s) and
            # explicit capture_exception calls.
            LoggingIntegration(
                level=logging.INFO,
                event_level=None,
            ),
        ],
        # Auth-endpoint request bodies carry passwords —
        # sentry_before_send drops them; send_default_pii=False
        # keeps IPs/usernames off events as well.
        before_send=sentry_before_send,
        send_default_pii=False,
        # GCP is the only production deploy path; every other
        # environment (local dev, CI) reports 'development'.
        environment=(
            'production' if IS_GCP_ENVIRONMENT else 'development'
        ),
        # GAE sets GAE_VERSION per deploy — free release tracking
        # with no CI changes.
        release=os.environ.get('GAE_VERSION'),
        traces_sample_rate=0.1,
    )

# Audio / Cloud TTS settings are resolved from a single source per
# environment (os.environ in cloud branches, the parsed config.yaml
# dict locally). Both support .get(key, default), so one block covers
# all three environments without duplication.
_default_bucket = (
    f"{os.environ.get('GOOGLE_CLOUD_PROJECT', 'bible-research-489314')}"
    "-bible-audio"
)
GOOGLE_TTS_LANGUAGE_CODE = _audio_settings_source.get(
    "GOOGLE_TTS_LANGUAGE_CODE", "lv-LV"
)
GOOGLE_TTS_VOICE_NAME = _audio_settings_source.get(
    "GOOGLE_TTS_VOICE_NAME", "lv-LV-Chirp3-HD-Sadachbia"
)
GOOGLE_TTS_SAMPLE_RATE_HERTZ = int(
    _audio_settings_source.get("GOOGLE_TTS_SAMPLE_RATE_HERTZ", 24000)
)
AUDIO_BUCKET_NAME = _audio_settings_source.get(
    "AUDIO_BUCKET_NAME", _default_bucket
)
AUDIO_SIGNED_URL_TTL_SECONDS = int(
    _audio_settings_source.get("AUDIO_SIGNED_URL_TTL_SECONDS", 3600)
)
MONTHLY_TTS_CHAR_LIMIT = int(
    _audio_settings_source.get("MONTHLY_TTS_CHAR_LIMIT", 100000)
)
LOCK_STALE_HOURS = int(
    _audio_settings_source.get("LOCK_STALE_HOURS", 24)
)

# Gemini API key(s) for the free-tier generateContent TTS endpoint.
# config.yaml may set GEMINI_API_KEY to a single string or a YAML
# list (one key per free-tier project); a .env value is a
# comma-separated string. Normalized to a list here.
_gemini_api_key = _audio_settings_source.get("GEMINI_API_KEY", [])
if isinstance(_gemini_api_key, str):
    _gemini_api_key = _gemini_api_key.split(',')
GEMINI_API_KEYS = [
    str(k).strip() for k in (_gemini_api_key or [])
    if str(k).strip()
]

_gcp_project = os.environ.get(
    "GOOGLE_CLOUD_PROJECT", "bible-research-489314"
)
IMAGE_BUCKET_ORIGINALS = os.environ.get(
    "IMAGE_BUCKET_ORIGINALS",
    f"{_gcp_project}-images-originals",
)
IMAGE_BUCKET_THUMBNAILS = os.environ.get(
    "IMAGE_BUCKET_THUMBNAILS",
    f"{_gcp_project}-images-thumbnails",
)
IMAGE_MAX_BYTES = int(
    os.environ.get("IMAGE_MAX_BYTES", 10 * 1024 * 1024)
)
IMAGE_SIGNED_URL_TTL_SECONDS = int(
    os.environ.get("IMAGE_SIGNED_URL_TTL_SECONDS", 600)
)
IMAGE_ALLOWED_CONTENT_TYPES = (
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
)
IMAGE_MAX_PER_NOTE = int(
    os.environ.get("IMAGE_MAX_PER_NOTE", 10)
)
IMAGE_MAX_PER_COMMENT = int(
    os.environ.get("IMAGE_MAX_PER_COMMENT", 5)
)

# Ensure DBT_KEY is available as an environment variable if it exists
if DBT_KEY:
    os.environ['DBT_KEY'] = DBT_KEY


# Password validation
# https://docs.djangoproject.com/en/6.1/ref/settings/#auth-password-validators

# Authentication backends
AUTHENTICATION_BACKENDS = [
    'users.backends.CaseInsensitiveAuthBackend',
    'django.contrib.auth.backends.ModelBackend',
]

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': (
            'django.contrib.auth.password_validation.'
            'UserAttributeSimilarityValidator'
        ),
    },
    {
        'NAME': (
            'django.contrib.auth.password_validation.'
            'MinimumLengthValidator'
        ),
    },
    {
        'NAME': (
            'django.contrib.auth.password_validation.'
            'CommonPasswordValidator'
        ),
    },
    {
        'NAME': (
            'django.contrib.auth.password_validation.'
            'NumericPasswordValidator'
        ),
    },
]


# Internationalization
# https://docs.djangoproject.com/en/6.1/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/6.1/howto/static-files/

STATIC_URL = '/static/'
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles', 'static')

MEDIA_URL = '/media/'
MEDIA_ROOT = os.path.join(BASE_DIR, 'media')

# Default primary key field type
# https://docs.djangoproject.com/en/6.1/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Process-local cache. The app runs a single-instance App Engine
# F1, so locmem is sufficient: it backs DRF throttling plus the
# provider-response caches in ``bible/views.py`` and
# ``bible/services/storage/gcs.py`` (translations, copyright, the
# immutable GCS timestamps artifact, signed audio URLs). Cache
# keys carry a ``v1:`` namespace so a format change can bump the
# version instead of needing a flush. If a second instance or a
# shared cache is ever needed, swap in Memcached/Redis.
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'bible-research-locmem',
    }
}

# REST Framework settings
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework.authentication.TokenAuthentication',
        'bible_research.authentication.BearerTokenAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
        'bible_research.permissions.GuestReadOnly',
    ],
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
    # GAE's fronting load balancer appends TWO IPs to
    # X-Forwarded-For: the client IP, then the fixed
    # GLOBAL_FORWARDING_RULE_IP. NUM_PROXIES=2 makes DRF key
    # throttle idents on the second-to-last entry — the real
    # client IP — not the shared forwarding-rule IP or the
    # client-spoofable XFF prefix.
    'NUM_PROXIES': 2,
    # Global anonymous throttle — generous on purpose: it bounds
    # credential-free abuse of endpoints that spend paid
    # provider quota without impeding reading/paging. Views with
    # their own ``throttle_classes`` (search, token, register)
    # replace this list entirely.
    'DEFAULT_THROTTLE_CLASSES': [
        'rest_framework.throttling.AnonRateThrottle',
    ],
    'DEFAULT_THROTTLE_RATES': {
        'anon': '300/min',
        # BibleSearchView opts in via throttle_classes — grouped
        # mode fans out to ~30 upstream calls per request.
        'search': '30/min',
        'token': '5/min',
        'register': '10/min',
    },
}

SPECTACULAR_SETTINGS = {
    'TITLE': 'Bible Research API',
    'DESCRIPTION': 'API for the Bible Research project',
    'VERSION': '1.0.0',
    'SERVE_INCLUDE_SCHEMA': False,
    'SWAGGER_UI_SETTINGS': {
        'persistAuthorization': True,
    },
}

# Logging configuration for production and development.
# This sends logs to the console, where platforms like GCP can
# capture them. File logging only applies to local development —
# cloud filesystems are read-only.

def create_log_handler(
    handler_name, level, filename=None, max_bytes=10485760,
    backup_count=10
):
    # Only configure file logging for local development.
    # GAE has a read-only filesystem.
    is_local_dev = not IS_GCP_ENVIRONMENT
    if DEBUG and filename and is_local_dev:
        logs_dir = os.path.join(BASE_DIR, 'logs')
        if not os.path.exists(logs_dir):
            os.makedirs(logs_dir)

        return {
            'level': level,
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': os.path.join(BASE_DIR, 'logs', filename),
            'maxBytes': max_bytes,  # 10MB
            'backupCount': backup_count,
            'formatter': 'verbose',
        }
    else:
        return {
            'level': level,
            'class': 'logging.NullHandler',
        }


LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': (
                '{levelname} {asctime} {module} {process:d} '
                '{thread:d} {message}'
            ),
            'style': '{',
        },
        'simple': {
            'format': '{levelname} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'simple',
        },
        'file_debug': create_log_handler('file_debug', 'DEBUG', 'debug.log'),
        'file_error': create_log_handler('file_error', 'ERROR', 'error.log'),
    },
    'loggers': {
        'django': {
            'handlers': ['console', 'file_debug', 'file_error'],
            'level': 'INFO',
            'propagate': True,
        },
        'bible': {
            'handlers': ['console', 'file_debug', 'file_error'],
            'level': os.getenv('DJANGO_LOG_LEVEL', 'INFO'),
            'propagate': False,
        },
        'annotations': {
            'handlers': ['console', 'file_debug', 'file_error'],
            'level': os.getenv('DJANGO_LOG_LEVEL', 'INFO'),
            # propagate=False — records already land on console
            # + file handlers here; propagating to root would
            # emit each line twice.
            'propagate': False,
        },
        'users': {
            'handlers': ['console', 'file_debug', 'file_error'],
            'level': os.getenv('DJANGO_LOG_LEVEL', 'INFO'),
            'propagate': False,
        },
    },
}
