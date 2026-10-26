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
    capture_internal_exception,
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

    @patch('sentry_sdk.capture_exception')
    def test_internal_error_captured_at_error(self, mock_cap):
        """Genuine 500-class failures report at error level —
        real bugs, not the warning-level provider trend."""
        capture_internal_exception(RuntimeError('boom'))

        mock_cap.assert_called_once()
        self.assertIsInstance(
            mock_cap.call_args[0][0], RuntimeError
        )
        self.assertEqual(
            mock_cap.call_args[1].get('level'), 'error'
        )

    def test_capture_noops_without_sentry_sdk(self):
        """A missing SDK (or DSN) must not break the request path."""
        with patch.dict('sys.modules', {'sentry_sdk': None}):
            # Forces ImportError on the lazy import.
            capture_provider_exception(
                FakeProviderError(500), 'provider_error'
            )
            capture_internal_exception(RuntimeError('boom'))


class SentryWiringTests(TestCase):
    """Endpoints must report swallowed failures to Sentry.

    Both seams are caught exceptions Sentry's DjangoIntegration
    never sees; these tests pin that the explicit capture calls
    stay wired.
    """

    @patch('bible.views.capture_internal_exception')
    @patch('bible.views.get_passage_body')
    def test_passage_500_reports_internal_exception(
        self, mock_get_passage_body, mock_capture
    ):
        """An unexpected bug in the passage endpoint surfaces a
        500 *and* an error-level Sentry capture."""
        mock_get_passage_body.side_effect = RuntimeError('boom')

        response = APIClient().get(
            '/api/v1/bible/',
            {'passage': 'John 3', 'fileset_id': 'ENGESV'},
        )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(
            response.data['error_code'], 'internal_error'
        )
        mock_capture.assert_called_once()
        self.assertIsInstance(
            mock_capture.call_args[0][0], RuntimeError
        )

    @patch('bible.views.capture_provider_exception')
    @patch('bible.views.provider_for')
    def test_provider_failure_reports_to_sentry(
        self, mock_provider_for, mock_capture
    ):
        """A provider outage routed through
        ``provider_error_response`` is captured."""
        mock_provider = MagicMock()
        mock_provider.get_timestamps.side_effect = (
            FakeProviderError(500)
        )
        mock_provider_for.return_value = mock_provider

        response = APIClient().get(
            '/api/v1/bible/timestamps/',
            {
                'fileset_id': 'ENGESVN2DA',
                'book': 'John',
                'chapter': 3,
            },
        )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.data['error_code'], 'provider_error'
        )
        mock_capture.assert_called_once()
        args = mock_capture.call_args[0]
        self.assertIsInstance(args[0], FakeProviderError)
        self.assertEqual(args[1], 'provider_error')


class ProviderErrorContractTests(TestCase):
    """The capture wiring must not change the response contract."""

    @patch('bible.services.providers.provider_for')
    def test_provider_failure_returns_error_contract(
        self, mock_provider_for
    ):
        mock_provider = MagicMock()
        mock_provider.get_passage.side_effect = FakeProviderError(
            500
        )
        mock_provider_for.return_value = mock_provider

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

    def test_token_endpoint_without_slash_body_is_dropped(self):
        event = sentry_before_send(
            self._event('https://api.example.com/api/token'), None
        )

        self.assertNotIn('data', event['request'])

    def test_token_endpoint_query_string_body_is_dropped(self):
        event = sentry_before_send(
            self._event(
                'https://api.example.com/api/token/?next=/admin'
            ),
            None,
        )

        self.assertNotIn('data', event['request'])

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

    def test_non_dict_request_block_is_tolerated(self):
        event = {'request': 'not-a-dict'}

        self.assertIs(sentry_before_send(event, None), event)
