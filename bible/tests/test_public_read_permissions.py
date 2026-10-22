from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from bible.services.translation_service import TranslationService

# Prod settings.py gates every view behind IsAuthenticated
# unless the view opts out; replicate that here so anonymous
# access is actually exercised (conftest does not set
# REST_FRAMEWORK, which would silently default to AllowAny).
_PROD_PERMISSIONS = {
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
}


@override_settings(REST_FRAMEWORK=_PROD_PERMISSIONS)
class BibleAnonymousReadTests(TestCase):
    """Regression tests for the post-#85 401s on public reads.

    ``DeviceAndCountryMiddleware`` no longer auto-authenticates
    anonymous GETs on ``/api/v1/bible``, so every read endpoint
    must carry ``AllowAny`` itself — otherwise truly anonymous
    callers get 401 instead of reaching the view. Each test
    asserts the request passes permission checks (400/200) rather
    than being rejected with 401/403.
    """

    def setUp(self):
        self.client = APIClient()

    def test_passage_anonymous(self):
        resp = self.client.get(
            reverse('bible-passage'), {'fileset_id': 'LVSGLU8'}
        )
        # Missing ``passage`` — permission passed, validation 400.
        self.assertEqual(resp.status_code, 400)

    def test_timestamps_anonymous(self):
        resp = self.client.get(reverse('audio-timestamps'))
        self.assertEqual(resp.status_code, 400)

    def test_copyright_anonymous(self):
        resp = self.client.get(reverse('bible-copyright'))
        self.assertEqual(resp.status_code, 400)

    def test_search_anonymous(self):
        resp = self.client.get(reverse('bible-search'))
        self.assertEqual(resp.status_code, 400)

    @patch.object(
        TranslationService,
        'get_live_translations',
        return_value=([], False),
    )
    def test_translations_anonymous(self, _mock):
        resp = self.client.get(reverse('translation-list'))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.json(), {'results': [], 'degraded': False}
        )
