from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework import status
from rest_framework.authentication import TokenAuthentication
from rest_framework.test import APIClient

from users.views import current_user

User = get_user_model()

REGISTER_URL = '/api/v1/users/register/'
ME_URL = '/api/v1/users/me/'

VALID_PASSWORD = 'secure-pass-123'


class RegisterUserTest(TestCase):
    """Coverage for POST /api/v1/users/register/."""

    def setUp(self):
        self.client = APIClient()

    def _register(self, **overrides):
        payload = {
            'username': 'newuser',
            'email': 'new@example.com',
            'password': VALID_PASSWORD,
            'password_confirm': VALID_PASSWORD,
        }
        payload.update(overrides)
        return self.client.post(REGISTER_URL, payload, format='json')

    def test_register_success_201_shape(self):
        """Valid payload returns 201 with user, token, message."""
        response = self._register()

        self.assertEqual(
            response.status_code, status.HTTP_201_CREATED
        )
        self.assertEqual(
            set(response.data.keys()),
            {'user', 'token', 'message'},
        )
        self.assertEqual(
            set(response.data['user'].keys()),
            {'id', 'username', 'email', 'date_joined',
             'account_kind'},
        )
        self.assertEqual(
            response.data['user']['account_kind'], 'registered'
        )
        self.assertEqual(response.data['user']['username'], 'newuser')
        self.assertEqual(
            response.data['user']['email'], 'new@example.com'
        )
        self.assertIsInstance(response.data['token'], str)
        self.assertTrue(response.data['token'])
        self.assertNotIn('password', response.data)
        self.assertNotIn('password_confirm', response.data)

    def test_register_hashes_password(self):
        """The stored password is hashed, not plaintext."""
        response = self._register()

        self.assertEqual(
            response.status_code, status.HTTP_201_CREATED
        )
        user = User.objects.get(username='newuser')
        self.assertTrue(user.check_password(VALID_PASSWORD))
        self.assertNotEqual(user.password, VALID_PASSWORD)

    def test_register_issued_token_authenticates(self):
        """The returned token authenticates GET /me/."""
        response = self._register()
        self.assertEqual(
            response.status_code, status.HTTP_201_CREATED
        )

        # The pytest settings define no REST_FRAMEWORK defaults,
        # so the @api_view-wrapped view knows only Session +
        # Basic auth; patch in TokenAuthentication so the issued
        # token is really exercised through the request.
        with patch.object(
            current_user.cls,
            'authentication_classes',
            [TokenAuthentication],
        ):
            me = self.client.get(
                ME_URL,
                HTTP_AUTHORIZATION=(
                    'Token ' + response.data['token']
                ),
            )

        self.assertEqual(me.status_code, status.HTTP_200_OK)
        self.assertEqual(me.data['username'], 'newuser')

    def test_register_password_mismatch_400(self):
        """Differing password_confirm is rejected with 400."""
        response = self._register(password_confirm='other-pass-456')

        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST
        )
        self.assertIn('password_confirm', response.data)
        self.assertFalse(User.objects.filter(username='newuser'))

    def test_register_duplicate_username_400(self):
        """An existing username is rejected with 400."""
        User.objects.create_user(
            username='newuser', password='whatever-123'
        )

        response = self._register()

        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST
        )
        self.assertIn('username', response.data)

    def test_register_duplicate_username_iexact_400(self):
        """Uniqueness is case-insensitive: login uses iexact,
        so 'NewUser' must not coexist with 'newuser'."""
        User.objects.create_user(
            username='NewUser', password='whatever-123'
        )

        response = self._register()

        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST
        )
        self.assertIn('username', response.data)
        self.assertEqual(User.objects.count(), 1)

    def test_register_username_with_whitespace_400(self):
        """Usernames containing whitespace are rejected — they
        served the removed device-middleware impersonation and
        have no legitimate purpose."""
        for username in ('has space', 'lead user', 'trail\tuser'):
            with self.subTest(username=username):
                response = self._register(username=username)
                self.assertEqual(
                    response.status_code,
                    status.HTTP_400_BAD_REQUEST,
                )
                self.assertIn('username', response.data)
        self.assertFalse(User.objects.exists())

    def test_register_integrity_error_400_not_500(self):
        """A UniqueValidator→insert race hits the DB unique
        constraint — the IntegrityError maps to 400, not 500."""
        from django.db import IntegrityError

        with patch.object(
            User.objects, 'create_user',
            side_effect=IntegrityError('UNIQUE username'),
        ):
            response = self._register()

        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST
        )
        self.assertIn('username', response.data)

    def test_register_duplicate_email_400(self):
        """An existing email is rejected with 400."""
        User.objects.create_user(
            username='other',
            email='new@example.com',
            password='whatever-123',
        )

        response = self._register()

        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST
        )
        self.assertIn('email', response.data)

    def test_register_short_password_400(self):
        """Passwords shorter than 8 chars are rejected."""
        response = self._register(
            password='short1', password_confirm='short1'
        )

        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST
        )
        self.assertIn('password', response.data)

    def test_register_missing_username_400(self):
        """A payload without username is rejected with 400."""
        response = self.client.post(
            REGISTER_URL,
            {
                'password': VALID_PASSWORD,
                'password_confirm': VALID_PASSWORD,
            },
            format='json',
        )

        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST
        )
        self.assertIn('username', response.data)

    def test_register_numeric_only_password_400(self):
        """AUTH_PASSWORD_VALIDATORS now actually run: a purely
        numeric password fails NumericPasswordValidator even
        though it satisfies min_length=8."""
        response = self._register(
            password='12345678', password_confirm='12345678'
        )

        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST
        )
        self.assertIn('password', response.data)
        self.assertFalse(User.objects.filter(username='newuser'))

    def test_register_common_password_400(self):
        """``password1`` is on the common-password list — it
        passed before validate_password was wired in."""
        response = self._register(
            password='password1', password_confirm='password1'
        )

        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST
        )
        self.assertIn('password', response.data)

    def test_register_password_similar_to_username_400(self):
        """UserAttributeSimilarityValidator gets a real user
        instance so a password matching the username fails."""
        response = self._register(
            password='newuser123', password_confirm='newuser123'
        )

        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST
        )
        self.assertIn('password', response.data)

    def test_register_email_optional(self):
        """Email may be omitted entirely; register returns 201."""
        response = self.client.post(
            REGISTER_URL,
            {
                'username': 'noemail',
                'password': VALID_PASSWORD,
                'password_confirm': VALID_PASSWORD,
            },
            format='json',
        )

        self.assertEqual(
            response.status_code, status.HTTP_201_CREATED
        )
        self.assertEqual(response.data['user']['email'], '')

    def test_register_json_array_body_400_not_500(self):
        """A top-level JSON array body makes request.data a
        list — the logger's getattr guard must defer to the
        serializer's 400 instead of AttributeError-ing."""
        response = self.client.post(
            REGISTER_URL, ['not', 'a', 'dict'], format='json'
        )

        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST
        )


class CurrentUserTest(TestCase):
    """Coverage for GET /api/v1/users/me/."""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username='meuser',
            email='me@example.com',
            password='password123',
        )

    def test_me_authenticated_200(self):
        """An authenticated user gets exactly the serializer
        fields."""
        self.client.force_authenticate(user=self.user)

        response = self.client.get(ME_URL)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            set(response.data.keys()),
            {'id', 'username', 'email', 'date_joined',
             'account_kind'},
        )
        self.assertEqual(response.data['username'], 'meuser')
        self.assertEqual(response.data['email'], 'me@example.com')
        self.assertEqual(
            response.data['account_kind'], 'registered'
        )

    def test_me_guest_account_kind(self):
        """The legacy 'guest' account reports
        ``account_kind: 'guest'`` so clients stop string-matching
        the username."""
        guest = User.objects.create_user(
            username='guest', password='whatever-123'
        )
        self.client.force_authenticate(user=guest)

        response = self.client.get(ME_URL)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['account_kind'], 'guest')

    def test_me_unauthenticated_401(self):
        """Anonymous requests are denied with 401 — the pytest
        settings now mirror production, where Token/Bearer
        authentication supplies the WWW-Authenticate header
        DRF needs to answer 401 instead of 403."""
        response = self.client.get(ME_URL)

        self.assertEqual(
            response.status_code,
            status.HTTP_401_UNAUTHORIZED,
        )
