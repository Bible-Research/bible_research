"""
Tests exercising the production auth/middleware stack.

The class is decorated with ``prod_auth_stack`` (see the root
conftest.py), which loads the prod MIDDLEWARE — including
DeviceAndCountryMiddleware, which auto-provisions a
``'<lang> <device>'`` user for anonymous requests — plus the
prod REST_FRAMEWORK authentication and permission classes.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.authentication import TokenAuthentication
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

import bible_research.middleware as device_middleware
from annotations.models import ReadingPosition
from bible_research.authentication import (
    BearerTokenAuthentication,
    CSRFExemptSessionAuthentication,
)
from conftest import PROD_MIDDLEWARE, prod_auth_stack
from users.views import current_user

ME_URL = '/api/v1/users/me/'
POSITIONS_URL = '/api/v1/reading-positions/'

# The middleware derives the username '<lang> <device>' from
# these headers: 'en-US' + 'Android 12; Mobile'.
ANDROID_HEADERS = {
    'HTTP_USER_AGENT': (
        'Mozilla/5.0 (Android 12; Mobile; rv:131.0)'
    ),
    'HTTP_ACCEPT_LANGUAGE': 'en-US,en;q=0.9',
}
DEVICE_USERNAME = 'en-US Android 12; Mobile'

# @api_view copies APIView.authentication_classes onto the
# generated WrappedAPIView at import time, so the class-level
# APIView patch in prod_auth_stack does not reach function
# views — patch current_user.cls directly where prod auth
# must be exercised on me/.
ME_AUTH_CLASSES = [
    TokenAuthentication,
    BearerTokenAuthentication,
    CSRFExemptSessionAuthentication,
]


@prod_auth_stack
class ProdAuthStackTest(TestCase):
    """Prove the prod middleware + DRF auth stack end-to-end."""

    def setUp(self):
        self.client = APIClient()
        self.User = get_user_model()

    def test_device_headers_provision_and_authenticate(self):
        """An anonymous request with device headers is
        provisioned a '<lang> <device>' user that reaches the
        view authenticated."""
        resp = self.client.get(ME_URL, **ANDROID_HEADERS)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.json()['username'], DEVICE_USERNAME
        )
        self.assertTrue(
            self.User.objects.filter(
                username__contains='Android 12'
            ).exists()
        )

    def test_token_authorization_header_authenticates(self):
        """Authorization: Token <key> authenticates the real
        user even though the middleware also auto-provisions
        a device user for the request."""
        user = self.User.objects.create_user(
            username='tokenuser', password='pass'
        )
        token = Token.objects.create(user=user)

        with patch.object(
            current_user.cls,
            'authentication_classes',
            ME_AUTH_CLASSES,
        ):
            resp = self.client.get(
                ME_URL,
                HTTP_AUTHORIZATION=f'Token {token.key}',
                **ANDROID_HEADERS,
            )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['username'], 'tokenuser')

    def test_bearer_authorization_header_authenticates(self):
        """Authorization: Bearer <key> authenticates via
        BearerTokenAuthentication."""
        user = self.User.objects.create_user(
            username='beareruser', password='pass'
        )
        token = Token.objects.create(user=user)

        with patch.object(
            current_user.cls,
            'authentication_classes',
            ME_AUTH_CLASSES,
        ):
            resp = self.client.get(
                ME_URL,
                HTTP_AUTHORIZATION=f'Bearer {token.key}',
                **ANDROID_HEADERS,
            )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.json()['username'], 'beareruser'
        )

    def test_guest_fallback_and_readonly(self):
        """When the device-user path raises, the middleware
        falls back to the shared 'guest' user; the real
        GuestReadOnly then blocks writes with 403 while reads
        still succeed."""
        real_login = device_middleware.login

        def fail_non_guest_login(request, user, backend=None):
            # The middleware catches exceptions inside
            # auto_authenticate and provisions 'guest' —
            # fail every login except the guest one so BOTH
            # requests below exercise the fallback branch.
            if user.username != 'guest':
                raise RuntimeError('forced login failure')
            return real_login(request, user, backend=backend)

        with patch.object(
            device_middleware, 'login', fail_non_guest_login
        ):
            post_resp = self.client.post(
                POSITIONS_URL,
                {'book': 'John', 'chapter': 3, 'verse': 1},
                format='json',
                **ANDROID_HEADERS,
            )
            get_resp = self.client.get(
                POSITIONS_URL, **ANDROID_HEADERS
            )

        self.assertEqual(post_resp.status_code, 403)
        self.assertEqual(get_resp.status_code, 200)
        self.assertTrue(
            self.User.objects.filter(username='guest').exists()
        )
        # The denied write really stored nothing.
        self.assertFalse(ReadingPosition.objects.exists())

    def test_session_user_replaced_by_device_user(self):
        """Pin current behavior: a session-authenticated user
        is silently re-authenticated as the device-derived
        user by the middleware."""
        real_user = self.User.objects.create_user(
            username='sessionuser', password='pass'
        )
        self.client.force_login(real_user)

        resp = self.client.get(ME_URL, **ANDROID_HEADERS)

        self.assertEqual(resp.status_code, 200)
        # FIXME: DeviceAndCountryMiddleware (middleware.py:22-27)
        # re-authenticates an already session-authenticated user
        # as the device-derived '<lang> <device>' user on every
        # path outside /admin and /api/v1/bible — the logged-in
        # user is silently replaced. Suspected prod bug, flagged
        # for a product decision; pinned here, not fixed.
        self.assertNotEqual(
            resp.json()['username'], 'sessionuser'
        )
        self.assertEqual(
            resp.json()['username'], DEVICE_USERNAME
        )

    @override_settings(
        MIDDLEWARE=[
            mw
            for mw in PROD_MIDDLEWARE
            if 'DeviceAndCountryMiddleware' not in mw
        ]
    )
    def test_default_permission_denies_when_unauthenticated(self):
        """With the device middleware removed, an anonymous
        request stays unauthenticated and the patched prod
        DEFAULT_PERMISSION_CLASSES (IsAuthenticated +
        GuestReadOnly) reject the write on TagViewSet, which
        relies on the APIView defaults."""
        client = APIClient()  # created inside the override
        resp = client.post(
            '/api/v1/tags/', {'name': 'nope'}, format='json'
        )
        self.assertEqual(resp.status_code, 401)
