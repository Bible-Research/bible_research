"""HTTP-level coverage of ``BiblePassageView`` provider routing.

ESV and API.Bible passages were previously exercised only at the
serializer/client level; these tests drive them through the real
``bible-passage`` URL so routing, the error contract, and response
shapes are pinned end to end.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from bible.services.apibible.registry import APIBIBLE_TRANSLATIONS

User = get_user_model()

BIBLE_ID = APIBIBLE_TRANSLATIONS['ENGNIV_API']['bible_id']

_FUMS_META = {
    'fums': "<script>_BAPI.t('abc123')</script>",
    'fumsId': 'abc123',
    'fumsJsInclude': (
        'https://cdn.scripture.api.bible/fums/fumsv2.min.js'
    ),
    'fumsNoScript': '<img src="https://x.example.com/x.png">',
}


class PassageProviderHTTPTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='provideruser',
            email='provider@test.local',
            password='x',
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.url = reverse('bible-passage')

    @patch('bible.services.providers.get_default_esv_client')
    def test_esv_passage_via_real_url(self, mock_get_client):
        """``ENGESV_API`` routes to the ESV client; verses and
        headings come back normalized at the HTTP layer."""
        client = mock_get_client.return_value
        client.get_chapter_with_headings.return_value = {
            'verses': [
                {'verse_start': 16, 'verse_text': 'For God...'},
            ],
            'headings': [
                {'before_verse': 16, 'text': 'God So Loved'},
            ],
        }

        resp = self.client.get(
            self.url,
            {'passage': 'John 3', 'fileset_id': 'ENGESV_API'},
        )

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body['format'], 'text')
        self.assertEqual(body['book'], 'JHN')
        self.assertEqual(body['book_name'], 'John')
        self.assertEqual(body['chapter'], 3)
        self.assertEqual(
            body['verses'],
            [{'verse': 16, 'text': 'For God...'}],
        )
        self.assertEqual(
            body['headings'],
            [{'before_verse': 16, 'text': 'God So Loved'}],
        )
        client.get_chapter_with_headings.assert_called_once_with(
            'JHN', 3
        )

    @patch('bible.services.providers.get_default_apibible_client')
    def test_apibible_passage_includes_fums_meta(
        self, mock_get_client
    ):
        """``ENGNIV_API`` routes to API.Bible; the FUMS ``meta``
        block must survive to the HTTP response — reporting it is
        an API.Bible license requirement."""
        client = mock_get_client.return_value
        client.get_chapter.return_value = {
            'verses': [
                {'verse_start': 16, 'verse_text': 'For God...'},
            ],
            'headings': [
                {'before_verse': 16, 'text': "God's Love"},
            ],
            'fums': _FUMS_META,
        }

        resp = self.client.get(
            self.url,
            {'passage': 'John 3', 'fileset_id': 'ENGNIV_API'},
        )

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body['format'], 'text')
        self.assertEqual(
            body['verses'],
            [{'verse': 16, 'text': 'For God...'}],
        )
        self.assertEqual(body['meta'], _FUMS_META)
        client.get_chapter.assert_called_once_with(
            BIBLE_ID, 'JHN', 3
        )

    @patch('bible.services.providers.get_default_dbt_client')
    def test_dbt_audio_success_shape(self, mock_get_client):
        """``response_format=audio`` on a DBT fileset returns the
        audio body: ``audio_url``, ``duration_seconds`` and
        ``file_size_bytes`` mapped off the raw DBT row keys."""
        client = mock_get_client.return_value
        client.get_verses.return_value = {
            'data': [
                {
                    'path': 'https://cdn.example.com/JHN3.mp3',
                    'duration': 180.5,
                    'filesize_in_bytes': 2345678,
                },
            ],
        }

        resp = self.client.get(
            self.url,
            {
                'passage': 'John 3',
                'fileset_id': 'ENGESVN1DA',
                'response_format': 'audio',
            },
        )

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(
            set(body),
            {
                'book', 'book_name', 'chapter', 'format',
                'audio_url', 'duration_seconds', 'file_size_bytes',
            },
        )
        self.assertEqual(body['format'], 'audio')
        self.assertEqual(
            body['audio_url'], 'https://cdn.example.com/JHN3.mp3'
        )
        self.assertEqual(body['duration_seconds'], 180.5)
        self.assertEqual(body['file_size_bytes'], 2345678)
        client.get_verses.assert_called_once_with(
            'JHN', '3', bible_id='ENGESVN1DA'
        )

    def test_invalid_response_format_400(self):
        resp = self.client.get(
            self.url,
            {
                'passage': 'John 3',
                'fileset_id': 'ENGESV',
                'response_format': 'json',
            },
        )

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(
            resp.json(),
            {
                "error": "Invalid format. Use 'text' or 'audio'",
                "error_code": "invalid_parameter",
            },
        )

    def test_unknown_book_400(self):
        resp = self.client.get(
            self.url,
            {
                'passage': 'UnknownBook 1',
                'fileset_id': 'ENGESV',
            },
        )

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(
            resp.json()['error'], 'Unknown book: UnknownBook'
        )

    @patch('bible.services.providers.get_default_dbt_client')
    def test_omitted_fileset_id_defaults_to_engesv(
        self, mock_get_client
    ):
        """With no ``fileset_id`` the view falls back to the DBT
        text fileset ``ENGESV`` — pin the default so a change
        can't silently reroute clients."""
        client = mock_get_client.return_value
        client.get_verses.return_value = {
            'data': [
                {
                    'verse_start': 16,
                    'verse_text': 'For God...',
                },
            ],
        }

        resp = self.client.get(self.url, {'passage': 'John 3'})

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body['format'], 'text')
        self.assertEqual(
            body['verses'],
            [{'verse': 16, 'text': 'For God...'}],
        )
        # Text passages compose over the chapter cache, whose
        # DBT fetch passes the chapter as int.
        client.get_verses.assert_called_once_with(
            'JHN', 3, bible_id='ENGESV'
        )
