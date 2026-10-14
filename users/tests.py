from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

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
