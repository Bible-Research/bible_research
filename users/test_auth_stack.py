"""
Tests exercising the production auth/middleware stack.

The class is decorated with ``prod_auth_stack`` (see the root
conftest.py), which loads the prod MIDDLEWARE plus the prod
REST_FRAMEWORK authentication and permission classes —
Token/Bearer auth only. DeviceAndCountryMiddleware is gone:
anonymous requests reach the views as a real ``AnonymousUser``,
no user rows are minted from headers, and unauthenticated
writes are clean 401s.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.authentication import TokenAuthentication
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from annotations.models import Comment, Note, Tag
from bible_research.authentication import BearerTokenAuthentication
from conftest import prod_auth_stack
from users.views import current_user

ME_URL = '/api/v1/users/me/'
REGISTER_URL = '/api/v1/users/register/'
TOKEN_URL = '/api/token/'
NOTES_URL = '/api/v1/notes/'
TAGS_URL = '/api/v1/tags/'
POSITIONS_URL = '/api/v1/reading-positions/'
COMMENT_COUNTS_URL = '/api/v1/comments/counts/'

# The middleware that used to mint a per-device user from
# headers like these is gone; sending them must now change
# nothing — the tests below prove it.
DEVICE_HEADERS = {
    'HTTP_USER_AGENT': (
        'Mozilla/5.0 (Android 12; Mobile; rv:131.0)'
    ),
    'HTTP_ACCEPT_LANGUAGE': 'en-US,en;q=0.9',
}

# @api_view copies APIView.authentication_classes onto the
# generated WrappedAPIView at import time, so the class-level
# APIView patch in prod_auth_stack does not reach function
# views — patch current_user.cls directly where prod auth
# must be exercised on me/.
ME_AUTH_CLASSES = [
    TokenAuthentication,
    BearerTokenAuthentication,
]


@prod_auth_stack
class ProdAuthStackTest(TestCase):
    """Prove the prod middleware + DRF auth stack end-to-end."""

    def setUp(self):
        self.client = APIClient()
        self.User = get_user_model()

    def test_anonymous_me_returns_401_and_mints_no_user(self):
        """Anonymous /me/ is a real 401 — no device user is
        provisioned to satisfy it, so no User row appears."""
        with patch.object(
            current_user.cls,
            'authentication_classes',
            ME_AUTH_CLASSES,
        ):
            resp = self.client.get(ME_URL, **DEVICE_HEADERS)

        self.assertEqual(resp.status_code, 401)
        self.assertEqual(self.User.objects.count(), 0)

    def test_token_authorization_header_authenticates(self):
        """Authorization: Token <key> authenticates the real
        user."""
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
                **DEVICE_HEADERS,
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
                **DEVICE_HEADERS,
            )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.json()['username'], 'beareruser'
        )

    def test_register_creates_exactly_one_user(self):
        """POST register/ creates only the registered account —
        the removed middleware used to mint a second device
        user on the very same request."""
        resp = self.client.post(
            REGISTER_URL,
            {
                'username': 'newuser',
                'email': 'new@example.com',
                'password': 'secure-pass-123',
                'password_confirm': 'secure-pass-123',
            },
            format='json',
            **DEVICE_HEADERS,
        )

        self.assertEqual(resp.status_code, 201)
        users = list(self.User.objects.all())
        self.assertEqual(len(users), 1)
        self.assertEqual(users[0].username, 'newuser')

    def test_token_endpoint_creates_no_users(self):
        """POST /api/token/ only issues a token — it must not
        mint a device User row on the side."""
        self.User.objects.create_user(
            username='loginuser', password='pass'
        )
        resp = self.client.post(
            TOKEN_URL,
            {'username': 'loginuser', 'password': 'pass'},
            format='json',
            **DEVICE_HEADERS,
        )

        self.assertEqual(resp.status_code, 200)
        self.assertIn('token', resp.json())
        self.assertEqual(self.User.objects.count(), 1)

    def test_anonymous_writes_return_401(self):
        """Anonymous writes to notes, tags, comments and
        reading positions are all rejected with 401 — no
        silent auto-provisioned account, nothing saved."""
        owner = self.User.objects.create_user(
            username='owner', password='pass'
        )
        note = Note.objects.create(
            user=owner, note_text='public note', public=True,
        )
        writes = [
            (TAGS_URL, {'name': 'hijack'}),
            (NOTES_URL, {'note_text': 'hijack'}),
            (
                f'/api/v1/notes/{note.id}/comments/',
                {'content': 'hijack'},
            ),
            (
                POSITIONS_URL,
                {'book': 'John', 'chapter': 1, 'verse': 1},
            ),
        ]
        for url, payload in writes:
            with self.subTest(url=url):
                resp = self.client.post(
                    url, payload, format='json',
                    **DEVICE_HEADERS,
                )
                self.assertEqual(resp.status_code, 401)

        self.assertEqual(self.User.objects.count(), 1)
        self.assertEqual(Note.objects.count(), 1)
        self.assertEqual(Comment.objects.count(), 0)
        self.assertFalse(
            Tag.objects.filter(name='hijack').exists()
        )

    def test_anonymous_comment_counts_public_only(self):
        """Anonymous GET /comments/counts/ must keep working
        (share pages call it without a token) while counting
        only public notes — private note ids stay absent."""
        owner = self.User.objects.create_user(
            username='owner', password='pass'
        )
        tag = Tag.objects.create(user=owner, name='share tag')
        public_note = Note.objects.create(
            user=owner, tag=tag,
            note_text='public', public=True,
        )
        private_note = Note.objects.create(
            user=owner, tag=tag,
            note_text='private', public=False,
        )
        Comment.objects.create(
            user=owner, note=public_note, content='hi',
        )
        Comment.objects.create(
            user=owner, note=private_note, content='secret',
        )

        resp = self.client.get(
            COMMENT_COUNTS_URL,
            {'tag_id': tag.id},
            **DEVICE_HEADERS,
        )

        self.assertEqual(resp.status_code, 200)
        counts = resp.json()['counts']
        self.assertEqual(counts.get(public_note.id), 1)
        self.assertNotIn(private_note.id, counts)
