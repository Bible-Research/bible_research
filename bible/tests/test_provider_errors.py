"""Tests for provider failure surfacing in the passage endpoint.

When an upstream Bible provider call fails, the API must return
``error``/``error_code`` fields (and omit ``verses``) instead of
silently returning an empty chapter.
"""
from unittest.mock import MagicMock, patch

import requests
from django.test import TestCase
from rest_framework.test import APIClient

from bible.serializers import BiblePassageSerializer


class FakeProviderError(Exception):
    """Mimics the DBT OpenAPI ``ApiException`` (``exc.status``)."""

    def __init__(self, status):
        super().__init__(f'HTTP {status}')
        self.status = status


def _http_error(status_code):
    """Build a real ``requests.HTTPError`` like the ESV client
    raises (status lives on ``exc.response.status_code``)."""
    response = requests.Response()
    response.status_code = status_code
    return requests.HTTPError(
        f'{status_code} error', response=response
    )


def _passage_data(fileset_id='ENGESV'):
    return {
        'book': 'JHN',
        'book_name': 'John',
        'chapter': 3,
        'fileset_id': fileset_id,
        'response_format': 'text',
    }


def _rate_limited_client():
    mock_client = MagicMock()
    mock_client.get_verses.side_effect = FakeProviderError(429)
    return mock_client


class ProviderErrorSerializerTests(TestCase):
    """Serializer must omit ``verses`` and include error fields."""

    @patch('bible.serializers.get_default_dbt_client')
    def test_429_returns_rate_limited_fields(self, mock_get):
        mock_get.return_value = _rate_limited_client()

        data = _passage_data()
        result = BiblePassageSerializer(data).to_representation(
            data
        )

        self.assertNotIn('verses', result)
        self.assertNotIn('message', result)
        self.assertEqual(result['error_code'], 'rate_limited')
        self.assertIn('429', result['error'])

    @patch('bible.serializers.get_default_dbt_client')
    def test_other_failure_returns_provider_error(self, mock_get):
        mock_client = MagicMock()
        mock_client.get_verses.side_effect = FakeProviderError(500)
        mock_get.return_value = mock_client

        data = _passage_data()
        result = BiblePassageSerializer(data).to_representation(
            data
        )

        self.assertNotIn('verses', result)
        self.assertEqual(result['error_code'], 'provider_error')
        self.assertIn('500', result['error'])

    @patch('bible.serializers.get_default_esv_client')
    def test_esv_provider_429_returns_rate_limited(
        self, mock_get
    ):
        mock_client = MagicMock()
        mock_client.get_chapter_with_headings.side_effect = (
            _http_error(429)
        )
        mock_get.return_value = mock_client

        data = _passage_data(fileset_id='ENGESV_API')
        result = BiblePassageSerializer(data).to_representation(
            data
        )

        self.assertNotIn('verses', result)
        self.assertEqual(result['error_code'], 'rate_limited')

    @patch('bible.serializers.get_default_dbt_client')
    def test_statusless_error_does_not_leak_details(
        self, mock_get
    ):
        """Network failures stringify with the request URL —
        including DBT's ``?key=`` credential — so the raw
        exception text must never reach the response body."""
        mock_client = MagicMock()
        mock_client.get_verses.side_effect = ConnectionError(
            'Max retries exceeded with url: '
            '/api/filesets/ENGESV/JHN/3?key=SECRET'
        )
        mock_get.return_value = mock_client

        data = _passage_data()
        result = BiblePassageSerializer(data).to_representation(
            data
        )

        self.assertNotIn('verses', result)
        self.assertEqual(result['error_code'], 'provider_error')
        self.assertNotIn('SECRET', result['error'])
        self.assertNotIn('url:', result['error'])


class ProviderErrorViewTests(TestCase):
    """The view must map provider failures to real HTTP statuses."""

    @patch('bible.serializers.get_default_dbt_client')
    def test_view_returns_429_when_rate_limited(self, mock_get):
        mock_get.return_value = _rate_limited_client()

        response = APIClient().get(
            '/api/v1/bible/',
            {'passage': 'John 3', 'fileset_id': 'ENGESV'},
        )

        self.assertEqual(response.status_code, 429)
        self.assertNotIn('verses', response.data)
        self.assertEqual(
            response.data['error_code'], 'rate_limited'
        )

    @patch('bible.serializers.get_default_dbt_client')
    def test_view_returns_502_for_other_failures(self, mock_get):
        mock_client = MagicMock()
        mock_client.get_verses.side_effect = FakeProviderError(503)
        mock_get.return_value = mock_client

        response = APIClient().get(
            '/api/v1/bible/',
            {'passage': 'John 3', 'fileset_id': 'ENGESV'},
        )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.data['error_code'], 'provider_error'
        )
