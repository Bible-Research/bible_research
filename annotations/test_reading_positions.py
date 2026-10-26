"""Tests for /api/v1/reading-positions/ — ReadingPositionViewSet.

Covers the upsert semantics of ``create()`` (HTTP 200 on both the
create and update pass, never 201), per-user scoping of
list/retrieve/update/destroy, the ``?book=`` filter, the ``bulk/``
action, guest read-only enforcement, and book-name
canonicalization — case variants of a book collapse into one
canonical row instead of forking duplicates.
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
        """The upsert reports 201 on first create, then 200 when
        the second POST updates the same row in place — exactly
        one row is left."""
        resp = self.client.post(
            self.URL,
            {'book': 'John', 'chapter': 3, 'verse': 16},
            format='json',
        )
        self.assertEqual(resp.status_code, 201)
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
            {'books': ['John', 'Genesis', 'Mark']},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(
            set(data), {'John', 'Genesis', 'Mark'}
        )
        self.assertEqual(data['John'], {'chapter': 3, 'verse': 16})
        self.assertEqual(
            data['Genesis'], {'chapter': 1, 'verse': 1}
        )
        self.assertIsNone(data['Mark'])

    def test_bulk_canonicalizes_book_names(self):
        """bulk/ resolves names through the same tolerant lookup
        as create: 'john' finds the canonical 'John' row."""
        self._position(self.user, 'John', chapter=3, verse=16)
        resp = self.client.post(
            self.BULK_URL, {'books': ['john']}, format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.json()['john'], {'chapter': 3, 'verse': 16}
        )

    def test_bulk_rejects_unresolvable_book_400(self):
        """A book name the tolerant lookup cannot resolve is a
        400, not a silent null — typos must not pass silently."""
        resp = self.client.post(
            self.BULK_URL,
            {'books': ['NotABook']},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()['error'], 'validation_error')

    def test_bulk_rejects_non_list(self):
        """bulk/ with a non-array ``books`` value returns 400 in
        the annotations ``{'error', 'message'}`` shape."""
        resp = self.client.post(
            self.BULK_URL, {'books': 'John'}, format='json',
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()['error'], 'validation_error')
        self.assertIn('array', resp.json()['message'])

    def test_bulk_rejects_non_object_body(self):
        """A top-level JSON array body parses into a list — the
        view must 400 instead of AttributeError-ing on
        ``request.data.get`` into a 500."""
        resp = self.client.post(
            self.BULK_URL, ['John'], format='json',
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()['error'], 'validation_error')

    def test_bulk_rejects_over_100_books(self):
        """The books array is capped at 100 entries."""
        resp = self.client.post(
            self.BULK_URL,
            {'books': ['John'] * 101},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()['error'], 'validation_error')

    def test_bulk_rejects_non_string_entries(self):
        """Non-string book entries are a 400, not a 500."""
        resp = self.client.post(
            self.BULK_URL,
            {'books': ['John', 42, None]},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)

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
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertIn('id', data)
        self.assertIn('last_accessed', data)
        detail = self.client.get(f"{self.URL}{data['id']}/")
        self.assertEqual(detail.status_code, 200)
        self.assertIn('id', detail.json())
        self.assertIn('last_accessed', detail.json())

    def test_unauthenticated_write_denied(self):
        """POST with no credentials is rejected with 401 —
        the production-like test settings' Token/Bearer
        authenticators advertise WWW-Authenticate, so
        NotAuthenticated stays a 401 rather than a 403."""
        client = APIClient()
        resp = client.post(
            self.URL,
            {'book': 'John', 'chapter': 3, 'verse': 16},
            format='json',
        )
        self.assertEqual(resp.status_code, 401)

    def test_chapter_verse_zero_or_negative_400(self):
        """chapter/verse are 1-indexed — 0 or negative values
        are a 400, not silently stored."""
        for body in (
            {'book': 'John', 'chapter': 0, 'verse': 1},
            {'book': 'John', 'chapter': 1, 'verse': -3},
        ):
            resp = self.client.post(self.URL, body, format='json')
            self.assertEqual(
                resp.status_code, 400, f'body={body}'
            )
        self.assertFalse(
            ReadingPosition.objects.filter(
                user=self.user
            ).exists()
        )

    def test_chapter_and_verse_required_on_create(self):
        """POST must carry both coordinates — omitting either is
        a 400, not a silent 1:1. PATCH stays partial-friendly:
        required=True is skipped under partial=True."""
        for body in (
            {'book': 'John'},
            {'book': 'John', 'chapter': 3},
            {'book': 'John', 'verse': 16},
        ):
            resp = self.client.post(self.URL, body, format='json')
            self.assertEqual(
                resp.status_code, 400, f'body={body}'
            )
        position = self._position(self.user, 'John')
        resp = self.client.patch(
            f'{self.URL}{position.id}/',
            {'chapter': 7},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        position.refresh_from_db()
        self.assertEqual(position.chapter, 7)
        self.assertEqual(position.verse, 1)

    def test_case_variant_books_upsert_one_row(self):
        """'John' and 'john' canonicalize to the same stored
        book and upsert into ONE row — validate_book returns
        the title-cased canonical name so the case-sensitive
        (user, book) unique_together can't be forked."""
        resp = self.client.post(
            self.URL,
            {'book': 'John', 'chapter': 3, 'verse': 16},
            format='json',
        )
        self.assertEqual(resp.status_code, 201)
        resp = self.client.post(
            self.URL,
            {'book': 'john', 'chapter': 5, 'verse': 1},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        positions = ReadingPosition.objects.filter(user=self.user)
        self.assertEqual(positions.count(), 1)
        position = positions.get()
        self.assertEqual(position.book, 'John')
        self.assertEqual(position.chapter, 5)
        self.assertEqual(position.verse, 1)

    def test_patch_book_is_rejected(self):
        """book is the upsert key and immutable after create:
        PATCHing it to another book returns 400 instead of
        colliding with (or forking) a second row."""
        position = self._position(self.user, 'John')
        resp = self.client.patch(
            f'{self.URL}{position.id}/',
            {'book': 'Mark'},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn('book', resp.json())
        position.refresh_from_db()
        self.assertEqual(position.book, 'John')

    def test_patch_same_book_canonical_form_allowed(self):
        """PATCH carrying the same book (in any accepted
        variant) is a no-op on the key and still 200s."""
        position = self._position(self.user, 'John')
        resp = self.client.patch(
            f'{self.URL}{position.id}/',
            {'book': 'john', 'chapter': 7},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        position.refresh_from_db()
        self.assertEqual(position.book, 'John')
        self.assertEqual(position.chapter, 7)

    def test_guest_patch_delete_403_bulk_reads_ok(self):
        """GuestReadOnly rejects guest PATCH/DELETE with 403 —
        but bulk/ is a read implemented as POST, so the guest
        account is exempted there to match GET ?book=."""
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
        self.assertEqual(resp.status_code, 200)
        # Guest's own data only — the position belongs to
        # self.user, so the guest sees a null entry.
        self.assertIsNone(resp.json()['John'])
