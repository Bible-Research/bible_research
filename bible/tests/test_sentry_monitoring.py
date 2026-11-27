"""Tests for Sentry wiring around provider failures.

Sentry is optional: with no ``SENTRY_DSN`` configured the SDK is
uninitialized and every capture call must be a silent no-op that
leaves the ``error``/``error_code`` response contract untouched.
"""
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from rest_framework.test import APIClient

from bible.utils.provider_errors import (
    PassageNotFoundError,
    ProviderConfigError,
    capture_internal_exception,
    capture_provider_exception,
    is_upstream_failure,
    sentry_capture_message,
    sentry_span,
)
from bible_research.utils import (
    sentry_before_send,
    sentry_traces_sampler,
)


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


class UpstreamClassificationTests(TestCase):
    """``is_upstream_failure`` decides warning vs error stream."""

    def test_status_bearing_exception_is_upstream(self):
        self.assertTrue(is_upstream_failure(FakeProviderError(500)))

    def test_transport_family_is_upstream(self):
        import requests

        self.assertTrue(
            is_upstream_failure(requests.ConnectionError('x'))
        )

    def test_google_api_error_is_upstream(self):
        from google.api_core import exceptions as gax

        self.assertTrue(
            is_upstream_failure(gax.ServiceUnavailable('x'))
        )

    def test_internal_bug_is_not_upstream(self):
        """A ``TypeError`` in response shaping is our bug."""
        self.assertFalse(is_upstream_failure(TypeError('x')))
        self.assertFalse(
            is_upstream_failure(RuntimeError('x'))
        )

    def test_provider_config_error_is_not_upstream(self):
        """A missing credential is our deployment bug — it must
        stay error-level ``internal_error``, never warning-level
        provider trend noise."""
        self.assertFalse(
            is_upstream_failure(
                ProviderConfigError('API_BIBLE_KEY not configured.')
            )
        )

    @patch('sentry_sdk.new_scope')
    @patch('sentry_sdk.capture_exception')
    def test_internal_bug_rerouted_to_error_level(
        self, mock_cap, mock_new_scope
    ):
        """Non-upstream exceptions caught at the provider
        boundary report as ``internal_error`` at error level —
        never as warning provider blips."""
        scope = MagicMock()
        mock_new_scope.return_value.__enter__.return_value = (
            scope
        )

        capture_provider_exception(
            TypeError('shaping bug'), 'provider_error'
        )

        mock_cap.assert_called_once()
        self.assertEqual(
            mock_cap.call_args[1].get('level'), 'error'
        )
        scope.set_tag.assert_any_call(
            'error_code', 'internal_error'
        )

    @patch('sentry_sdk.new_scope')
    @patch('sentry_sdk.capture_exception')
    def test_upstream_override_forces_warning(
        self, mock_cap, mock_new_scope
    ):
        """``upstream=True`` keeps a status-less provider
        exception (e.g. job-level EdgeTTSError) on the warning
        stream."""
        scope = MagicMock()
        mock_new_scope.return_value.__enter__.return_value = (
            scope
        )

        class TypedProviderError(Exception):
            pass

        capture_provider_exception(
            TypedProviderError('endpoint down'),
            'provider_error',
            upstream=True,
        )

        mock_cap.assert_called_once()
        self.assertEqual(
            mock_cap.call_args[1].get('level'), 'warning'
        )
        scope.set_tag.assert_any_call(
            'error_code', 'provider_error'
        )


class CaptureContextTests(TestCase):
    """``tags``/``contexts`` and fingerprints reach the scope."""

    def _scope(self, mock_new_scope):
        scope = MagicMock()
        mock_new_scope.return_value.__enter__.return_value = (
            scope
        )
        return scope

    @patch('sentry_sdk.new_scope')
    @patch('sentry_sdk.capture_exception')
    def test_tags_and_contexts_are_forwarded(
        self, mock_cap, mock_new_scope
    ):
        scope = self._scope(mock_new_scope)

        capture_internal_exception(
            RuntimeError('boom'),
            tags={'fileset_id': 'LVSGLU8', 'endpoint': 'x'},
            contexts={'sword_verse': {'chapter': 3}},
        )

        scope.set_tag.assert_any_call(
            'error_code', 'internal_error'
        )
        scope.set_tag.assert_any_call('fileset_id', 'LVSGLU8')
        scope.set_tag.assert_any_call('endpoint', 'x')
        scope.set_context.assert_called_once_with(
            'sword_verse', {'chapter': 3}
        )

    @patch('sentry_sdk.new_scope')
    @patch('sentry_sdk.capture_exception')
    def test_none_tag_values_are_skipped(
        self, mock_cap, mock_new_scope
    ):
        scope = self._scope(mock_new_scope)

        capture_internal_exception(
            RuntimeError('boom'), tags={'fileset_id': None}
        )

        self.assertNotIn(
            'fileset_id',
            [c[0][0] for c in scope.set_tag.call_args_list],
        )

    @patch('sentry_sdk.new_scope')
    @patch('sentry_sdk.capture_exception')
    def test_fingerprint_groups_on_error_code_and_fileset(
        self, mock_cap, mock_new_scope
    ):
        scope = self._scope(mock_new_scope)

        capture_provider_exception(
            FakeProviderError(500),
            'provider_error',
            tags={'fileset_id': 'ENGESVN2DA'},
        )

        self.assertEqual(
            scope.fingerprint,
            ['{{ default }}', 'provider_error', 'ENGESVN2DA'],
        )

    @patch('sentry_sdk.new_scope')
    @patch('sentry_sdk.capture_exception')
    def test_fingerprint_without_fileset(
        self, mock_cap, mock_new_scope
    ):
        scope = self._scope(mock_new_scope)

        capture_internal_exception(RuntimeError('boom'))

        self.assertEqual(
            scope.fingerprint,
            ['{{ default }}', 'internal_error'],
        )

    @patch('sentry_sdk.new_scope')
    @patch('sentry_sdk.capture_message')
    def test_capture_message_sends_tags(
        self, mock_msg, mock_new_scope
    ):
        scope = self._scope(mock_new_scope)

        sentry_capture_message(
            'jobs failed', tags={'job': 'run_all_jobs'}
        )

        mock_msg.assert_called_once_with(
            'jobs failed', level='error'
        )
        scope.set_tag.assert_called_once_with(
            'job', 'run_all_jobs'
        )


class SentrySpanTests(TestCase):
    """``sentry_span`` wraps start_span and no-ops without SDK."""

    @patch('sentry_sdk.start_span')
    def test_span_sets_data(self, mock_start_span):
        span = MagicMock()
        mock_start_span.return_value.__enter__.return_value = (
            span
        )

        with sentry_span(
            op='gcs.read', description='path',
            data={'fileset_id': 'X'},
        ) as s:
            pass

        self.assertIs(s, span)
        mock_start_span.assert_called_once_with(
            op='gcs.read', name='path'
        )
        span.set_data.assert_called_once_with(
            'fileset_id', 'X'
        )

    def test_span_yields_none_without_sdk(self):
        with patch.dict('sys.modules', {'sentry_sdk': None}):
            with sentry_span(op='x') as span:
                self.assertIsNone(span)


class BeforeSendFilteringTests(TestCase):
    """Scanner-noise exceptions are dropped, not reported."""

    def test_disallowed_host_event_is_dropped(self):
        from django.core.exceptions import DisallowedHost

        try:
            raise DisallowedHost('bad.host')
        except DisallowedHost:
            import sys
            event = sentry_before_send(
                {'request': {}},
                {'exc_info': sys.exc_info()},
            )

        self.assertIsNone(event)

    def test_other_exceptions_pass_through(self):
        import sys

        try:
            raise RuntimeError('boom')
        except RuntimeError:
            event = sentry_before_send(
                {'request': {}},
                {'exc_info': sys.exc_info()},
            )

        self.assertIsNotNone(event)


class TracesSamplerTests(TestCase):
    """Route-level sampling decisions."""

    def _sample(self, path):
        return sentry_traces_sampler(
            {'wsgi_environ': {'PATH_INFO': path}}
        )

    def test_health_check_not_sampled(self):
        self.assertEqual(self._sample('/_ah/health'), 0.0)
        self.assertEqual(self._sample('/_ah/start'), 0.0)

    def test_docs_routes_not_sampled(self):
        self.assertEqual(self._sample('/api/v1/docs/'), 0.0)
        self.assertEqual(self._sample('/api/v1/redoc/'), 0.0)
        self.assertEqual(self._sample('/api/v1/schema/'), 0.0)

    def test_api_routes_sampled_at_point_three(self):
        self.assertEqual(self._sample('/api/v1/bible/'), 0.3)
        self.assertEqual(self._sample('/api/token/'), 0.3)

    def test_other_routes_at_floor_rate(self):
        self.assertEqual(self._sample('/admin/'), 0.1)
        self.assertEqual(self._sample('/'), 0.1)

    def test_missing_environ_is_tolerated(self):
        self.assertEqual(sentry_traces_sampler({}), 0.1)

    def test_profiles_sample_rate_pinned(self):
        """``profiles_sample_rate`` stays at the 0.1 the init
        block wires — profiling every transaction is too
        expensive even at this traffic level."""
        from bible_research.utils import PROFILES_SAMPLE_RATE

        self.assertEqual(PROFILES_SAMPLE_RATE, 0.1)


class SentryUserMiddlewareTests(TestCase):
    """``SentryUserMiddleware`` attaches only the user pk."""

    @patch('sentry_sdk.set_user')
    def test_token_request_sets_user_pk(self, mock_set_user):
        from django.contrib.auth import get_user_model
        from rest_framework.authtoken.models import Token

        user = get_user_model().objects.create_user(
            username='sentrymw', password='password123'
        )
        token = Token.objects.create(user=user)

        client = APIClient()
        client.credentials(
            HTTP_AUTHORIZATION=f'Token {token.key}'
        )
        client.get('/api/v1/bible/')

        mock_set_user.assert_called_once_with({'id': user.pk})

    @patch('sentry_sdk.set_user')
    def test_bearer_scheme_also_resolves(self, mock_set_user):
        from django.contrib.auth import get_user_model
        from rest_framework.authtoken.models import Token

        user = get_user_model().objects.create_user(
            username='sentrymw2', password='password123'
        )
        token = Token.objects.create(user=user)

        client = APIClient()
        client.credentials(
            HTTP_AUTHORIZATION=f'Bearer {token.key}'
        )
        client.get('/api/v1/bible/')

        mock_set_user.assert_called_once_with({'id': user.pk})

    @patch('sentry_sdk.set_user')
    def test_anonymous_request_sets_no_user(self, mock_set_user):
        APIClient().get('/api/v1/bible/')

        mock_set_user.assert_not_called()

    @patch('sentry_sdk.set_user')
    def test_invalid_token_sets_no_user(self, mock_set_user):
        client = APIClient()
        client.credentials(
            HTTP_AUTHORIZATION='Token deadbeefdeadbeef'
        )
        client.get('/api/v1/bible/')

        mock_set_user.assert_not_called()


class RunAllJobsSentryTests(TestCase):
    """Failed sub-jobs reach Sentry and are flushed on exit."""

    _MODULE = 'bible.management.commands.run_all_jobs'

    @patch(f'{_MODULE}.sentry_flush')
    @patch(f'{_MODULE}.sentry_capture_message')
    @patch(f'{_MODULE}.capture_internal_exception')
    @patch(f'{_MODULE}.call_command')
    def test_failed_subjob_captured_aggregated_flushed(
        self, mock_call, mock_capture, mock_message, mock_flush
    ):
        mock_call.side_effect = RuntimeError('boom')

        with self.assertRaises(CommandError):
            call_command(
                'run_all_jobs',
                '--only', 'generate_chapter_audio',
            )

        mock_capture.assert_called_once()
        args, kwargs = mock_capture.call_args
        self.assertIsInstance(args[0], RuntimeError)
        self.assertEqual(
            kwargs['tags'], {'job': 'generate_chapter_audio'}
        )
        mock_message.assert_called_once()
        self.assertIn(
            'generate_chapter_audio',
            mock_message.call_args[0][0],
        )
        mock_flush.assert_called_once_with(timeout=5)

    @patch(f'{_MODULE}.sentry_flush')
    @patch(f'{_MODULE}.sentry_capture_message')
    @patch(f'{_MODULE}.capture_internal_exception')
    @patch(f'{_MODULE}.call_command')
    def test_clean_systemexit_is_not_a_failure(
        self, mock_call, mock_capture, mock_message, mock_flush
    ):
        mock_call.side_effect = SystemExit(0)

        # Must NOT raise CommandError.
        call_command(
            'run_all_jobs', '--only', 'generate_chapter_audio',
        )

        mock_capture.assert_not_called()
        mock_message.assert_not_called()
        mock_flush.assert_called_once_with(timeout=5)

    @patch(f'{_MODULE}.sentry_flush')
    @patch(f'{_MODULE}.sentry_capture_message')
    @patch(f'{_MODULE}.capture_internal_exception')
    @patch(f'{_MODULE}.call_command')
    def test_nonzero_systemexit_is_captured(
        self, mock_call, mock_capture, mock_message, mock_flush
    ):
        mock_call.side_effect = SystemExit(2)

        with self.assertRaises(CommandError):
            call_command(
                'run_all_jobs',
                '--only', 'generate_chapter_audio',
            )

        mock_capture.assert_called_once()
        self.assertIsInstance(
            mock_capture.call_args[0][0], SystemExit
        )


class SwordVerseFailureDedupeTests(TestCase):
    """One Sentry event per ``(fileset, book, chapter)``."""

    @patch(
        'bible.services.sword.client.capture_internal_exception'
    )
    def test_same_chapter_reports_once(self, mock_capture):
        from bible.services.sword.client import SwordClient

        client = SwordClient()
        for _ in range(3):
            client._report_verse_failure(
                'LVSGLU8', 'JHN', 3, 16, RuntimeError('x')
            )

        mock_capture.assert_called_once()

    @patch(
        'bible.services.sword.client.capture_internal_exception'
    )
    def test_different_chapters_each_report(self, mock_capture):
        from bible.services.sword.client import SwordClient

        client = SwordClient()
        client._report_verse_failure(
            'LVSGLU8', 'JHN', 3, 16, RuntimeError('x')
        )
        client._report_verse_failure(
            'LVSGLU8', 'JHN', 4, 1, RuntimeError('x')
        )

        self.assertEqual(mock_capture.call_count, 2)

    @patch(
        'bible.services.sword.client.capture_internal_exception'
    )
    def test_expected_misses_are_not_captured(self, mock_capture):
        """``ValueError``/``OSError`` coverage misses never reach
        Sentry — only unexpected exceptions do."""
        from bible.services.sword.client import SwordClient

        client = SwordClient()
        bible = MagicMock()
        bible.get.side_effect = ValueError(
            'Book "John", chapter 3, only have 36 verses.'
        )
        client._cache['LVSGLU8'] = (MagicMock(), bible)
        client._book_name_cache['LVSGLU8'] = {'JHN': 'John'}

        from bible.utils.provider_errors import (
            PassageNotFoundError as PNF,
        )
        with self.assertRaises(PNF):
            client.get_chapter_verses('LVSGLU8', 'JHN', 3)

        mock_capture.assert_not_called()

    @patch(
        'bible.services.sword.client.capture_internal_exception'
    )
    def test_unexpected_error_is_captured(self, mock_capture):
        from bible.services.sword.client import SwordClient

        client = SwordClient()
        bible = MagicMock()
        bible.get.side_effect = TypeError('corrupt module')
        client._cache['LVSGLU8'] = (MagicMock(), bible)
        client._book_name_cache['LVSGLU8'] = {'JHN': 'John'}

        from bible.utils.provider_errors import (
            PassageNotFoundError as PNF,
        )
        with self.assertRaises(PNF):
            client.get_chapter_verses('LVSGLU8', 'JHN', 3)

        mock_capture.assert_called_once()


class InfraSwallowCaptureTests(TestCase):
    """GCS/image helpers report failures while degrading."""

    @patch(
        'bible.services.storage.gcs.capture_internal_exception'
    )
    @patch('bible.services.storage.gcs._bucket')
    def test_corrupt_usage_counter_reports_and_returns_zero(
        self, mock_bucket, mock_capture
    ):
        from bible.services.storage import gcs

        blob = MagicMock()
        blob.exists.return_value = True
        blob.download_as_bytes.return_value = b'not-json{'
        mock_bucket.return_value.blob.return_value = blob

        self.assertEqual(gcs.read_monthly_usage('FILESET'), 0)
        mock_capture.assert_called_once()
        tags = mock_capture.call_args[1]['tags']
        self.assertEqual(tags['component'], 'gcs-state')

    @patch(
        'bible.services.storage.gcs.capture_internal_exception'
    )
    @patch('bible.services.storage.gcs._bucket')
    def test_corrupt_gemini_counter_reports_and_returns_zero(
        self, mock_bucket, mock_capture
    ):
        from bible.services.storage import gcs

        blob = MagicMock()
        blob.exists.return_value = True
        blob.download_as_bytes.return_value = b'not-json{'
        mock_bucket.return_value.blob.return_value = blob

        self.assertEqual(
            gcs.read_gemini_daily_requests('F', 'v', 'd'), 0
        )
        mock_capture.assert_called_once()

    @patch(
        'annotations.services.image_storage'
        '.capture_internal_exception'
    )
    def test_malformed_storage_url_reports(
        self, mock_capture
    ):
        from annotations.services.image_storage import (
            signed_image_url,
        )

        url = signed_image_url('img-x', 'gs://no-slash-here')

        self.assertIsNone(url)
        mock_capture.assert_called_once()

    @patch(
        'annotations.services.image_storage'
        '.capture_internal_exception'
    )
    @patch('annotations.services.image_storage._get_client')
    def test_sign_failure_reports_and_degrades(
        self, mock_client, mock_capture
    ):
        from annotations.services.image_storage import (
            signed_image_url,
        )

        mock_client.return_value.bucket.side_effect = (
            RuntimeError('gcs down')
        )

        url = signed_image_url('img-y', 'gs://bkt/obj.jpg')

        self.assertIsNone(url)
        mock_capture.assert_called_once()

    @patch(
        'annotations.services.image_storage'
        '.capture_internal_exception'
    )
    @patch('annotations.services.image_storage._get_client')
    def test_delete_failure_reports_and_tolerates(
        self, mock_client, mock_capture
    ):
        from annotations.services.image_storage import (
            delete_original,
        )

        mock_client.return_value.bucket.side_effect = (
            RuntimeError('gcs down')
        )

        # Must not raise.
        delete_original('img-z', 'gs://bkt/obj.jpg')

        mock_capture.assert_called_once()

    @patch(
        'bible.services.providers.capture_internal_exception'
    )
    @patch(
        'bible.services.providers.generated_timestamps_base_id'
    )
    @patch('bible.services.providers.gcs')
    def test_generated_timestamps_read_failure_reports(
        self, mock_gcs, mock_base_id, mock_capture
    ):
        """A GCS failure reading the generated artifact degrades
        to ``None`` (like a missing artifact) and reports."""
        from bible.services.providers import DbtProvider

        mock_base_id.return_value = 'BASEID'
        mock_gcs.read_generated_timestamps_json.side_effect = (
            RuntimeError('gcs read blew up')
        )

        provider = DbtProvider('ENGNIVN2DA')
        result = provider._generated_timestamps('JHN', 3)

        self.assertIsNone(result)
        mock_capture.assert_called_once()


class MaskedDbtFailureCaptureTests(TestCase):
    """When the generated artifact masks a DBT outage the 200
    response still reports the upstream failure at warning
    level with provider routing tags."""

    @patch(
        'bible.services.providers.capture_provider_exception'
    )
    @patch(
        'bible.services.providers.generated_timestamps_base_id'
    )
    @patch('bible.services.providers.gcs')
    @patch('bible.services.providers.get_default_dbt_client')
    def test_dbt_failure_masked_by_artifact_is_captured(
        self, mock_get_client, mock_gcs, mock_base_id,
        mock_capture
    ):
        from bible.services.providers import DbtProvider

        mock_get_client.return_value.get_timestamps\
            .side_effect = FakeProviderError(500)
        mock_base_id.return_value = 'BASEID'
        mock_gcs.read_generated_timestamps_json.return_value = {
            'data': [{'verse_start': 1, 'timestamp': 0.0}],
        }

        provider = DbtProvider('ENGNIVN2DA')
        result = provider.get_timestamps('JHN', 3)

        self.assertEqual(
            result, [{'verse_start': 1, 'timestamp': 0.0}]
        )
        mock_capture.assert_called_once()
        kwargs = mock_capture.call_args.kwargs
        self.assertEqual(
            kwargs['tags']['fileset_id'], 'ENGNIVN2DA'
        )
        self.assertEqual(
            kwargs['tags']['endpoint'], 'bible/timestamps'
        )


class AnnotationDbFailureCaptureTests(TestCase):
    """Non-position IntegrityError/OperationalError in the
    reorder/move handlers report to Sentry; the expected
    position-conflict 409 stays silent."""

    def setUp(self):
        from django.contrib.auth import get_user_model
        from annotations.models import Note, Tag

        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username='sentry-tester', password='pw',
        )
        self.tag = Tag.objects.create(
            user=self.user, name='T',
        )
        self.notes = [
            Note.objects.create(
                user=self.user, tag=self.tag,
                note_text=f'n{i}', tag_position=float(i),
            )
            for i in range(1, 4)
        ]
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _reorder(self):
        return self.client.post(
            '/api/v1/notes/reorder/',
            {
                'tag_id': self.tag.id,
                'updates': [
                    {
                        'note_id': n.id,
                        'position': float(4 - i),
                    }
                    for i, n in enumerate(self.notes)
                ],
            },
            format='json',
        )

    def _patch_objects(self, mock_objects, bulk_update_exc):
        """Make the mocked ``Note.objects`` delegate reads to the
        real manager while ``bulk_update`` raises the given
        exception inside the reorder transaction."""
        from annotations.models import Note

        real = Note._base_manager
        mock_objects.select_for_update.side_effect = (
            real.select_for_update
        )
        mock_objects.filter.side_effect = real.filter
        mock_objects.bulk_update.side_effect = bulk_update_exc

    @patch(
        'annotations.views.capture_internal_exception'
    )
    @patch('annotations.views.Note.objects')
    def test_reorder_integrity_error_reports(
        self, mock_objects, mock_capture
    ):
        from django.db import IntegrityError

        self._patch_objects(
            mock_objects,
            IntegrityError('some other constraint'),
        )

        resp = self._reorder()

        self.assertEqual(resp.status_code, 409)
        self.assertEqual(
            resp.json()['error'], 'database_error'
        )
        mock_capture.assert_called_once()
        self.assertEqual(
            mock_capture.call_args.kwargs['tags'],
            {'endpoint': 'notes/reorder'},
        )

    @patch(
        'annotations.views.capture_internal_exception'
    )
    @patch('annotations.views.Note.objects')
    def test_reorder_position_conflict_stays_silent(
        self, mock_objects, mock_capture
    ):
        from django.db import IntegrityError

        self._patch_objects(
            mock_objects,
            IntegrityError(
                'UNIQUE constraint failed: '
                'annotations_note.tag_id, '
                'annotations_note.tag_position'
            ),
        )

        resp = self._reorder()

        self.assertEqual(resp.status_code, 409)
        self.assertEqual(
            resp.json()['error'], 'position_conflict'
        )
        mock_capture.assert_not_called()

    @patch(
        'annotations.views.capture_internal_exception'
    )
    @patch('annotations.views.Note.objects')
    def test_move_operational_error_reports(
        self, mock_objects, mock_capture
    ):
        from django.db import OperationalError

        mock_objects.select_for_update.side_effect = (
            type(self.notes[0])._base_manager.select_for_update
        )
        n0, n1 = self.notes[0], self.notes[1]

        with patch.object(
            type(n0), 'save',
            side_effect=OperationalError('deadlock detected'),
        ):
            resp = self.client.post(
                '/api/v1/notes/move/',
                {
                    'tag_id': self.tag.id,
                    'note_id': n0.id,
                    'target_note_id': n1.id,
                    'placement': 'after',
                },
                format='json',
            )

        self.assertEqual(resp.status_code, 409)
        self.assertEqual(
            resp.json()['error'], 'database_error'
        )
        mock_capture.assert_called_once()
        self.assertEqual(
            mock_capture.call_args.kwargs['tags'],
            {'endpoint': 'notes/move'},
        )


class ScanSwordMatchesCaptureTests(TestCase):
    """Grouped-search chapter scan: unexpected per-chapter
    failures keep partial results and report once."""

    @patch(
        'bible.services.search_grouping'
        '.capture_internal_exception'
    )
    def test_unexpected_failure_keeps_partial_results(
        self, mock_capture
    ):
        from bible.services.search_grouping import (
            scan_sword_matches,
        )

        client = MagicMock()
        client.list_chapters.return_value = [
            ('GEN', 1), ('GEN', 2), ('GEN', 3),
        ]

        def verses(_fs, book_id, chapter):
            if chapter >= 2:
                raise TypeError('corrupt module data')
            return [
                {
                    'verse_start': 1,
                    'verse_text': 'In the beginning God',
                },
            ]

        client.get_chapter_verses.side_effect = verses

        matches, truncated, _ = scan_sword_matches(
            client, 'LVSGLU8', 'beginning', 'GEN',
        )

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]['book_id'], 'GEN')
        self.assertFalse(truncated)
        # Two failing chapters, one event — the log keeps the
        # full list, Sentry gets the first only.
        mock_capture.assert_called_once()
        kwargs = mock_capture.call_args.kwargs
        self.assertEqual(
            kwargs['contexts']['sword_chapter'],
            {'book_id': 'GEN', 'chapter': 2},
        )

    @patch(
        'bible.services.search_grouping'
        '.capture_internal_exception'
    )
    def test_expected_misses_stay_silent(self, mock_capture):
        from bible.services.search_grouping import (
            scan_sword_matches,
        )

        client = MagicMock()
        client.list_chapters.return_value = [('GEN', 1)]
        client.get_chapter_verses.side_effect = OSError(
            'missing data file'
        )

        matches, truncated, _ = scan_sword_matches(
            client, 'LVSGLU8', 'beginning', 'GEN',
        )

        self.assertEqual(matches, [])
        self.assertFalse(truncated)
        mock_capture.assert_not_called()


class TracedProviderSpanTests(TestCase):
    """``_TracedProvider`` wraps capability calls in provider
    spans; ``provider_for`` keeps ``_CachingProvider``
    outermost."""

    @patch('bible.services.providers.sentry_span')
    def test_capability_call_is_traced(self, mock_span):
        from bible.services.providers import _TracedProvider

        mock_span.return_value.__enter__.return_value = None
        inner = MagicMock(fileset_id='LVSGLU8')
        inner.get_chapter.return_value = {'verses': []}
        traced = _TracedProvider(inner)

        traced.get_chapter('GEN', 1)

        mock_span.assert_called_once()
        kwargs = mock_span.call_args.kwargs
        self.assertEqual(kwargs['op'], 'provider')
        self.assertEqual(
            kwargs['data'], {'fileset_id': 'LVSGLU8'}
        )
        inner.get_chapter.assert_called_once_with('GEN', 1)

    def test_provider_for_cache_outside_trace(self):
        from bible.services.providers import (
            _CachingProvider,
            _TracedProvider,
            provider_for,
        )

        provider = provider_for('LVSGLU8')
        self.assertIsInstance(provider, _CachingProvider)
        self.assertIsInstance(
            provider._inner, _TracedProvider
        )


class ImportEsvVersesFailureTests(TestCase):
    """The import command logs/captures and exits nonzero."""

    @patch(
        'bible.management.commands.import_esv_verses'
        '.capture_internal_exception'
    )
    @patch(
        'bible.management.commands.import_esv_verses'
        '.populate_verses'
    )
    def test_failure_raises_command_error_and_reports(
        self, mock_populate, mock_capture
    ):
        mock_populate.side_effect = RuntimeError('db gone')

        with self.assertRaises(CommandError):
            call_command('import_esv_verses')

        mock_capture.assert_called_once()
        self.assertEqual(
            mock_capture.call_args.kwargs['tags'],
            {'job': 'import_esv_verses'},
        )


class SentryReleaseTests(TestCase):
    """``SENTRY_RELEASE`` beats ``GAE_VERSION``; neither set
    leaves the release unset."""

    def test_sentry_release_wins(self):
        from bible_research.utils import sentry_release

        with patch.dict(
            'os.environ',
            {
                'SENTRY_RELEASE': 'job-sha',
                'GAE_VERSION': 'v123',
            },
        ):
            self.assertEqual(sentry_release(), 'job-sha')

    def test_gae_version_fallback(self):
        import os

        from bible_research.utils import sentry_release

        env = dict(os.environ)
        env.pop('SENTRY_RELEASE', None)
        env['GAE_VERSION'] = 'v123'
        with patch.dict('os.environ', env, clear=True):
            self.assertEqual(sentry_release(), 'v123')

    def test_unset_returns_none(self):
        from bible_research.utils import sentry_release

        with patch.dict('os.environ', {}, clear=True):
            self.assertIsNone(sentry_release())


class CredentialScrubTests(TestCase):
    """``before_send`` strips query-string credentials from
    exception values — a missed sanitize path still cannot leak
    ``?key=`` into Sentry."""

    def test_dbt_key_is_redacted(self):
        event = {
            'exception': {
                'values': [{
                    'type': 'HTTPError',
                    'value': (
                        '500 Server Error for url: '
                        'https://b4.dbt.io/api/bibles?key=SECRET'
                    ),
                }],
            },
        }

        result = sentry_before_send(event, {})

        value = result['exception']['values'][0]['value']
        self.assertNotIn('SECRET', value)
        self.assertIn('key=<redacted>', value)

    def test_signed_url_credentials_are_redacted(self):
        event = {
            'exception': {
                'values': [{
                    'type': 'HTTPError',
                    'value': (
                        '403 for url: https://x/y.mp3?'
                        'X-Amz-Signature=ABC123&sig=ZZ'
                    ),
                }],
            },
        }

        result = sentry_before_send(event, {})

        value = result['exception']['values'][0]['value']
        self.assertNotIn('ABC123', value)
        self.assertNotIn('sig=ZZ', value)

    def test_frame_vars_and_breadcrumbs_are_scrubbed(self):
        event = {
            'exception': {
                'values': [{
                    'type': 'HTTPError',
                    'value': 'boom',
                    'stacktrace': {
                        'frames': [{
                            'vars': {
                                'params': {
                                    'key': 'SECRET',
                                    'v': '4',
                                },
                                'e': (
                                    'HTTPError(url: '
                                    'https://x?key=SECRET)'
                                ),
                                'harmless': 42,
                            },
                        }],
                    },
                }],
            },
            'breadcrumbs': {
                'values': [{
                    'message': (
                        'Failed for url: '
                        'https://x/y.mp3?sig=SECRET'
                    ),
                }],
            },
        }

        result = sentry_before_send(event, {})

        frame_vars = (
            result['exception']['values'][0]
            ['stacktrace']['frames'][0]['vars']
        )
        self.assertEqual(frame_vars['params']['key'], '<redacted>')
        self.assertEqual(frame_vars['params']['v'], '4')
        self.assertNotIn('SECRET', frame_vars['e'])
        self.assertEqual(frame_vars['harmless'], 42)
        crumb = result['breadcrumbs']['values'][0]
        self.assertNotIn('SECRET', crumb['message'])

    def test_missing_exception_block_is_tolerated(self):
        event = {'request': {'url': 'https://x/api/v1/bible/'}}
        result = sentry_before_send(event, {})
        self.assertIsNotNone(result)


class RequestsClientBugClassificationTests(TestCase):
    """``requests`` exceptions that can only be our bug (bad URL
    construction) are NOT upstream failures."""

    def test_missing_schema_is_internal(self):
        import requests

        self.assertFalse(is_upstream_failure(
            requests.exceptions.MissingSchema('no scheme')
        ))

    def test_transport_timeout_is_upstream(self):
        import requests

        self.assertTrue(is_upstream_failure(
            requests.exceptions.ConnectTimeout('timed out')
        ))


class TransactionScrubTests(TestCase):
    """Transaction events bypass ``before_send`` — the dedicated
    hook must apply the same credential scrubs to spans,
    breadcrumbs, and the request block."""

    def test_transaction_credentials_are_scrubbed(self):
        from bible_research.utils import (
            sentry_before_send_transaction,
        )

        event = {
            'spans': [{
                'data': {
                    'url': 'https://b4.dbt.io/api?key=SECRET',
                },
            }],
            'breadcrumbs': {
                'values': [{
                    'data': {
                        'url': 'https://x/y.mp3?sig=ZZ',
                    },
                }],
            },
            'request': {
                'url': 'https://x/api/token',
                'data': {'password': 'pw'},
            },
        }

        result = sentry_before_send_transaction(event, {})

        self.assertNotIn(
            'SECRET', str(result['spans'])
        )
        self.assertNotIn(
            'sig=ZZ', str(result['breadcrumbs'])
        )
        self.assertNotIn('data', result['request'])
