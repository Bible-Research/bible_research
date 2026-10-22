from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from users.views import RegisterRateThrottle, TokenRateThrottle

User = get_user_model()

# Match production settings: the case-insensitive backend runs
# first, then Django's default ModelBackend.
AUTH_BACKENDS = [
    'users.backends.CaseInsensitiveAuthBackend',
    'django.contrib.auth.backends.ModelBackend',
]


@override_settings(AUTHENTICATION_BACKENDS=AUTH_BACKENDS)
class ObtainAuthTokenTestCase(TestCase):
    """Regression tests for POST /api/token/ (issue #53)."""

    def setUp(self):
        self.client = APIClient()
        self.url = reverse('api_token')

    def _post_token(self, username, password):
        return self.client.post(
            self.url,
            {'username': username, 'password': password},
            format='json',
        )

    def test_active_user_obtains_token(self):
        """A normal active user still gets a token."""
        user = User.objects.create_user(
            username='testuser', password='password123'
        )
        response = self._post_token('testuser', 'password123')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('token', response.data)
        self.assertEqual(
            Token.objects.get(key=response.data['token']).user,
            user,
        )

    def test_inactive_user_cannot_obtain_token(self):
        """is_active=False users must be rejected, not issued
        a token."""
        user = User.objects.create_user(
            username='inactive', password='password123'
        )
        user.is_active = False
        user.save()

        response = self._post_token('inactive', 'password123')

        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST
        )
        self.assertIn('non_field_errors', response.data)
        self.assertFalse(
            Token.objects.filter(user=user).exists()
        )

    def test_case_variant_usernames_do_not_500(self):
        """Two usernames differing only by case must not raise
        MultipleObjectsReturned; the exact-case match wins."""
        upper = User.objects.create_user(
            username='Alice', password='password-upper'
        )
        lower = User.objects.create_user(
            username='alice', password='password-lower'
        )

        response = self._post_token('Alice', 'password-upper')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            Token.objects.get(key=response.data['token']).user,
            upper,
        )

        response = self._post_token('alice', 'password-lower')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            Token.objects.get(key=response.data['token']).user,
            lower,
        )

    def test_case_variant_no_exact_match_does_not_500(self):
        """Logging in with a case variant matching no exact
        username falls back to the first iexact match instead
        of a 500."""
        User.objects.create_user(
            username='Alice', password='shared-pass'
        )
        User.objects.create_user(
            username='alice', password='shared-pass'
        )

        response = self._post_token('ALICE', 'shared-pass')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('token', response.data)

    def test_case_variant_wrong_password_rejected(self):
        """A case-variant login with the wrong password still
        returns 400, not a 500."""
        User.objects.create_user(
            username='Alice', password='password-upper'
        )
        User.objects.create_user(
            username='alice', password='password-lower'
        )

        response = self._post_token('ALICE', 'wrong-password')
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST
        )

    def test_json_array_body_400_not_500(self):
        """A top-level JSON array body makes request.data a
        list — the throttle's getattr guard must defer to the
        serializer's 400 instead of AttributeError-ing."""
        response = self.client.post(
            self.url, ['no', 'dict'], format='json'
        )
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST
        )


class AuthEndpointThrottlingTest(TestCase):
    """Scoped throttles on POST /api/token/ and register/.

    The pytest settings use a generous rate so ordinary tests
    never trip; these tests shrink it via ``patch.dict`` on the
    shared THROTTLE_RATES dict (the class attribute DRF binds
    once at import — override_settings cannot reach it).
    """

    TOKEN_URL = '/api/token/'
    REGISTER_URL = '/api/v1/users/register/'

    def setUp(self):
        # Throttle history lives in the process-wide locmem
        # cache shared by every test — clear it so history
        # from unrelated tests can't skew these counts.
        cache.clear()
        self.client = APIClient()

    def _register(self, username):
        return self.client.post(
            self.REGISTER_URL,
            {
                'username': username,
                'password': 'secure-pass-123',
                'password_confirm': 'secure-pass-123',
            },
            format='json',
        )

    @patch.dict(
        TokenRateThrottle.THROTTLE_RATES, {'token': '2/min'}
    )
    def test_token_endpoint_throttles_per_username(self):
        """The third token attempt for the same username inside
        the window is rejected with 429."""
        User.objects.create_user(
            username='limited', password='password123'
        )
        payload = {
            'username': 'limited', 'password': 'password123',
        }
        for _ in range(2):
            resp = self.client.post(
                self.TOKEN_URL, payload, format='json'
            )
            self.assertEqual(resp.status_code, 200)
        resp = self.client.post(
            self.TOKEN_URL, payload, format='json'
        )
        self.assertEqual(resp.status_code, 429)

    @patch.dict(
        TokenRateThrottle.THROTTLE_RATES, {'token': '1/min'}
    )
    def test_token_throttle_is_per_username_not_per_ip(self):
        """A different username keeps working after another
        account hits the cap — the key is the username."""
        User.objects.create_user(
            username='capped', password='password123'
        )
        User.objects.create_user(
            username='other', password='password123'
        )
        self.client.post(
            self.TOKEN_URL,
            {'username': 'capped', 'password': 'password123'},
            format='json',
        )
        resp = self.client.post(
            self.TOKEN_URL,
            {'username': 'capped', 'password': 'password123'},
            format='json',
        )
        self.assertEqual(resp.status_code, 429)
        resp = self.client.post(
            self.TOKEN_URL,
            {'username': 'other', 'password': 'password123'},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)

    @patch.dict(
        RegisterRateThrottle.THROTTLE_RATES, {'register': '2/min'}
    )
    def test_register_endpoint_throttles(self):
        """The third registration from the same client inside
        the window is rejected with 429."""
        resp = self._register('throttle_user1')
        self.assertEqual(resp.status_code, 201)
        resp = self._register('throttle_user2')
        self.assertEqual(resp.status_code, 201)
        resp = self._register('throttle_user3')
        self.assertEqual(resp.status_code, 429)

    @patch.dict(
        RegisterRateThrottle.THROTTLE_RATES, {'register': '1/min'}
    )
    def test_register_throttle_ignores_spoofed_xff_prefix(self):
        """NUM_PROXIES=2 keys the throttle ident on the
        second-to-last X-Forwarded-For entry — the client IP:
        GAE's load balancer appends CLIENT_IP plus a fixed
        forwarding-rule IP, so rotating a client-supplied XFF
        prefix cannot mint a fresh ident per request."""
        payload = {
            'username': 'xff_user1',
            'password': 'secure-pass-123',
            'password_confirm': 'secure-pass-123',
        }
        resp = self.client.post(
            self.REGISTER_URL,
            payload,
            format='json',
            HTTP_X_FORWARDED_FOR=(
                'spoofedA, 198.51.100.1, 203.0.113.9'
            ),
        )
        self.assertEqual(resp.status_code, 201)
        payload['username'] = 'xff_user2'
        resp = self.client.post(
            self.REGISTER_URL,
            payload,
            format='json',
            HTTP_X_FORWARDED_FOR=(
                'spoofedB, 198.51.100.1, 203.0.113.9'
            ),
        )
        self.assertEqual(resp.status_code, 429)
