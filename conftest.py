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
