"""Tests for /api/v1/reading-positions/ — ReadingPositionViewSet.

Covers the upsert semantics of ``create()`` (HTTP 200 on both the
create and update pass, never 201), per-user scoping of
list/retrieve/update/destroy, the ``?book=`` filter, the ``bulk/``
action, guest read-only enforcement, and pins the case-variant
duplicate-book behaviour.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from annotations.models import ReadingPosition


class ReadingPositionApiTest(TestCase):
    """API tests for ReadingPositionViewSet."""

    URL = '/api/v1/reading-positions/'
    BULK_URL = '/api/v1/reading-positions/bulk/'

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username='rp_user',
            email='rp_user@example.com',
            password='pass',
        )
        self.other = User.objects.create_user(
            username='rp_other',
            email='rp_other@example.com',
            password='pass',
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _position(self, user, book, chapter=1, verse=1):
        """Create a ReadingPosition row directly."""
        return ReadingPosition.objects.create(
            user=user, book=book, chapter=chapter, verse=verse,
        )

    def test_upsert_second_post_updates_in_place(self):
        """Second POST for the same book updates the row, 200 both
        times (never 201), leaving exactly one row."""
        resp = self.client.post(
            self.URL,
            {'book': 'John', 'chapter': 3, 'verse': 16},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        resp = self.client.post(
            self.URL,
            {'book': 'John', 'chapter': 5, 'verse': 1},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        positions = ReadingPosition.objects.filter(
            user=self.user, book='John',
        )
        self.assertEqual(positions.count(), 1)
        position = positions.get()
        self.assertEqual(position.chapter, 5)
        self.assertEqual(position.verse, 1)

    def test_list_scoped_to_user(self):
        """GET list returns only the requester's positions."""
        own = self._position(self.user, 'John')
        foreign = self._position(self.other, 'Genesis')
        resp = self.client.get(self.URL)
        self.assertEqual(resp.status_code, 200)
        ids = {p['id'] for p in resp.json()}
        self.assertIn(own.id, ids)
        self.assertNotIn(foreign.id, ids)

    def test_retrieve_foreign_position_404(self):
        """GET on another user's position id returns 404."""
        foreign = self._position(self.other, 'John')
        resp = self.client.get(f'{self.URL}{foreign.id}/')
        self.assertEqual(resp.status_code, 404)

    def test_update_and_destroy_foreign_404(self):
        """PATCH and DELETE on a foreign position id both 404 and
        leave the row untouched."""
        foreign = self._position(self.other, 'John')
        url = f'{self.URL}{foreign.id}/'
        resp = self.client.patch(
            url, {'chapter': 9}, format='json',
        )
        self.assertEqual(resp.status_code, 404)
        resp = self.client.delete(url)
        self.assertEqual(resp.status_code, 404)
        foreign.refresh_from_db()
        self.assertEqual(foreign.chapter, 1)

    def test_book_query_param_filters(self):
        """?book= restricts the list to that book only."""
        self._position(self.user, 'John')
        self._position(self.user, 'Genesis')
        resp = self.client.get(self.URL, {'book': 'John'})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]['book'], 'John')

    def test_bulk_returns_map_with_nulls(self):
        """bulk/ returns a map keyed by each requested book, with
        None for books that have no stored position."""
        self._position(self.user, 'John', chapter=3, verse=16)
        self._position(self.user, 'Genesis', chapter=1, verse=1)
        resp = self.client.post(
            self.BULK_URL,
            {'books': ['John', 'Genesis', 'Nonexistent']},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(
            set(data), {'John', 'Genesis', 'Nonexistent'}
        )
        self.assertEqual(data['John'], {'chapter': 3, 'verse': 16})
        self.assertEqual(
            data['Genesis'], {'chapter': 1, 'verse': 1}
        )
        self.assertIsNone(data['Nonexistent'])

    def test_bulk_rejects_non_list(self):
        """bulk/ with a non-array ``books`` value returns 400 with
        the documented error body."""
        resp = self.client.post(
            self.BULK_URL, {'books': 'John'}, format='json',
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(
            resp.json(), {'error': 'books must be an array'}
        )

    def test_bulk_scoped_to_user(self):
        """bulk/ never surfaces another user's positions."""
        self._position(self.other, 'John', chapter=7, verse=7)
        resp = self.client.post(
            self.BULK_URL, {'books': ['John']}, format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.json()['John'])

    def test_response_includes_id_and_last_accessed(self):
        """Read-only ``id`` and ``last_accessed`` are serialized on
        both the create response and a subsequent retrieve."""
        resp = self.client.post(
            self.URL,
            {'book': 'John', 'chapter': 3, 'verse': 16},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn('id', data)
        self.assertIn('last_accessed', data)
        detail = self.client.get(f"{self.URL}{data['id']}/")
        self.assertEqual(detail.status_code, 200)
        self.assertIn('id', detail.json())
        self.assertIn('last_accessed', detail.json())

    def test_unauthenticated_write_denied(self):
        """POST with no credentials is rejected — observed 403:
        DRF coerces NotAuthenticated to 403 because the first
        test-default authenticator (SessionAuthentication)
        advertises no WWW-Authenticate header."""
        client = APIClient()
        resp = client.post(
            self.URL,
            {'book': 'John', 'chapter': 3, 'verse': 16},
            format='json',
        )
        self.assertEqual(resp.status_code, 403)

    def test_case_variant_books_create_separate_rows(self):
        """'John' and 'john' currently upsert into TWO rows.

        Pinned current behaviour — not desired behaviour.
        """
        # FIXME: case-variant book names bypass the upsert —
        # validate_book stores the raw value while the
        # (user, book) unique_together is case-sensitive, so
        # 'John' and 'john' never merge into one position.
        resp = self.client.post(
            self.URL,
            {'book': 'John', 'chapter': 3, 'verse': 16},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        resp = self.client.post(
            self.URL,
            {'book': 'john', 'chapter': 5, 'verse': 1},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        positions = ReadingPosition.objects.filter(user=self.user)
        self.assertEqual(positions.count(), 2)
        self.assertEqual(
            {p.book for p in positions}, {'John', 'john'}
        )

    def test_guest_patch_delete_bulk_403(self):
        """GuestReadOnly rejects every write by the shared 'guest'
        account — PATCH, DELETE, and POST bulk/ all return 403."""
        User = get_user_model()
        guest = User.objects.create_user(
            username='guest',
            email='rp_guest@example.com',
            password=None,
        )
        position = self._position(self.user, 'John')
        url = f'{self.URL}{position.id}/'
        self.client.force_authenticate(user=guest)
        resp = self.client.patch(
            url, {'chapter': 2}, format='json',
        )
        self.assertEqual(resp.status_code, 403)
        resp = self.client.delete(url)
        self.assertEqual(resp.status_code, 403)
        resp = self.client.post(
            self.BULK_URL, {'books': ['John']}, format='json',
        )
        self.assertEqual(resp.status_code, 403)
