"""Tests for Sentry wiring around provider failures.

Sentry is optional: with no ``SENTRY_DSN`` configured the SDK is
uninitialized and every capture call must be a silent no-op that
leaves the ``error``/``error_code`` response contract untouched.
"""
from unittest.mock import MagicMock, patch

from django.test import TestCase
from rest_framework.test import APIClient

from bible.utils.provider_errors import (
    PassageNotFoundError,
    capture_provider_exception,
)
from bible_research.utils import sentry_before_send


class FakeProviderError(Exception):
    """Mimics the DBT OpenAPI ``ApiException`` (``exc.status``)."""

    def __init__(self, status):
        super().__init__(f'HTTP {status}')
        self.status = status


class CaptureProviderExceptionTests(TestCase):
    """``capture_provider_exception`` trends upstream outages."""

    @patch('sentry_sdk.capture_exception')
    def test_provider_error_captured_at_warning(self, mock_cap):
        capture_provider_exception(
            FakeProviderError(500), 'provider_error'
        )

        mock_cap.assert_called_once()
        exc = mock_cap.call_args[0][0]
        self.assertIsInstance(exc, FakeProviderError)
        self.assertEqual(
            mock_cap.call_args[1].get('level'), 'warning'
        )

    @patch('sentry_sdk.capture_exception')
    def test_rate_limited_is_captured(self, mock_cap):
        capture_provider_exception(
            FakeProviderError(429), 'rate_limited'
        )

        mock_cap.assert_called_once()

    @patch('sentry_sdk.capture_exception')
    def test_not_found_is_never_captured(self, mock_cap):
        """``not_found`` outcomes are user typos, not bugs;
        ``book_not_in_fileset`` is a normal fallback."""
        capture_provider_exception(PassageNotFoundError('nope'))
        capture_provider_exception(
            FakeProviderError(404), 'book_not_in_fileset'
        )

        mock_cap.assert_not_called()

    def test_capture_noops_without_sentry_sdk(self):
        """A missing SDK (or DSN) must not break the request path."""
        with patch.dict('sys.modules', {'sentry_sdk': None}):
            # Forces ImportError on the lazy import.
            capture_provider_exception(
                FakeProviderError(500), 'provider_error'
            )


class ProviderErrorContractTests(TestCase):
    """The capture wiring must not change the response contract."""

    @patch('bible.serializers.get_default_dbt_client')
    def test_provider_failure_returns_error_contract(
        self, mock_get
    ):
        mock_client = MagicMock()
        mock_client.get_verses.side_effect = FakeProviderError(500)
        mock_get.return_value = mock_client

        response = APIClient().get(
            '/api/v1/bible/',
            {'passage': 'John 3', 'fileset_id': 'ENGESV'},
        )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.data['error_code'], 'provider_error'
        )
        self.assertIn('error', response.data)


class SentryBeforeSendTests(TestCase):
    """Auth-endpoint request bodies must never leave the server."""

    def _event(self, url):
        return {
            'request': {
                'url': url,
                'method': 'POST',
                'data': {'username': 'u', 'password': 'secret'},
            }
        }

    def test_token_endpoint_body_is_dropped(self):
        event = sentry_before_send(
            self._event('https://api.example.com/api/token/'), None
        )

        self.assertNotIn('data', event['request'])
        self.assertEqual(event['request']['method'], 'POST')

    def test_user_register_body_is_dropped(self):
        event = sentry_before_send(
            self._event(
                'https://api.example.com/api/v1/users/register/'
            ),
            None,
        )

        self.assertNotIn('data', event['request'])

    def test_other_paths_keep_the_body(self):
        body = {'username': 'u', 'password': 'secret'}
        event = sentry_before_send(
            self._event('https://api.example.com/api/v1/bible/'),
            None,
        )

        self.assertEqual(event['request']['data'], body)

    def test_missing_request_block_is_tolerated(self):
        self.assertEqual(sentry_before_send({}, None), {})
