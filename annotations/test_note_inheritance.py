"""
Tests for note publicity defaults on write.

When `public` is OMITTED on note creation the note inherits
`tag.public` (an untagged note defaults to False). An explicit
`public: false` under a public tag is honored — per-note
opt-out stays possible. The default applies at creation only:
PATCH writes an explicit flag and moving a note between tags
never flips it. `POST /api/v1/notes/bulk/` items inherit the
same way.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from annotations.models import Note, Tag


class NoteCreateInheritanceTest(TestCase):
    """POST /api/v1/notes/ — omitted `public` inherits
    tag.public."""

    BASE_URL = '/api/v1/notes/'

    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user(
            username='inherit_owner', password='pass',
        )
        self.public_tag = Tag.objects.create(
            user=self.owner, name='Public tag', public=True,
        )
        self.private_tag = Tag.objects.create(
            user=self.owner, name='Private tag', public=False,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)

    def test_omitted_public_inherits_public_tag(self):
        """No `public` key under a public tag → public note."""
        resp = self.client.post(
            self.BASE_URL,
            {'note_text': 'hi', 'tag': self.public_tag.id},
            format='json',
        )
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(resp.json()['public'])
        note = Note.objects.get(pk=resp.json()['id'])
        self.assertTrue(note.public)

    def test_explicit_false_opt_out(self):
        """Explicit `public: false` under a public tag stays
        private — the per-note opt-out is honored."""
        resp = self.client.post(
            self.BASE_URL,
            {
                'note_text': 'hi',
                'tag': self.public_tag.id,
                'public': False,
            },
            format='json',
        )
        self.assertEqual(resp.status_code, 201)
        self.assertFalse(resp.json()['public'])
        note = Note.objects.get(pk=resp.json()['id'])
        self.assertFalse(note.public)

    def test_omitted_public_under_private_tag(self):
        """No `public` key under a private tag → private note."""
        resp = self.client.post(
            self.BASE_URL,
            {'note_text': 'hi', 'tag': self.private_tag.id},
            format='json',
        )
        self.assertEqual(resp.status_code, 201)
        self.assertFalse(resp.json()['public'])

    def test_omitted_public_untagged(self):
        """tag=None, no `public` key → False (no tag to inherit
        from). An omitted tag key itself 400s — the
        unique_tag_position_per_tag validator requires the key
        to be present (pre-existing contract)."""
        resp = self.client.post(
            self.BASE_URL,
            {'note_text': 'hi', 'tag': None},
            format='json',
        )
        self.assertEqual(resp.status_code, 201)
        self.assertFalse(resp.json()['public'])

    def test_explicit_true_under_private_tag(self):
        """Explicit `public: true` under a private tag is
        honored too."""
        resp = self.client.post(
            self.BASE_URL,
            {
                'note_text': 'hi',
                'tag': self.private_tag.id,
                'public': True,
            },
            format='json',
        )
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(resp.json()['public'])


class NotePatchPublicTest(TestCase):
    """PATCH /api/v1/notes/<pk>/ — the tag default never
    applies to updates."""

    BASE_URL = '/api/v1/notes/'

    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user(
            username='patch_note_owner', password='pass',
        )
        self.public_tag = Tag.objects.create(
            user=self.owner, name='Public tag', public=True,
        )
        self.private_tag = Tag.objects.create(
            user=self.owner, name='Private tag', public=False,
        )
        self.note = Note.objects.create(
            user=self.owner,
            tag=self.private_tag,
            note_text='n',
            public=False,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)

    def test_patch_public_toggles(self):
        """PATCH writes an explicit flag both directions."""
        resp = self.client.patch(
            f'{self.BASE_URL}{self.note.id}/',
            {'public': True},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.note.refresh_from_db()
        self.assertTrue(self.note.public)

        resp = self.client.patch(
            f'{self.BASE_URL}{self.note.id}/',
            {'public': False},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.note.refresh_from_db()
        self.assertFalse(self.note.public)

    def test_move_into_public_tag_keeps_flag(self):
        """Moving a private note into a public tag via PATCH
        does NOT flip its publicity — the default applies at
        creation only."""
        resp = self.client.patch(
            f'{self.BASE_URL}{self.note.id}/',
            {'tag': self.public_tag.id},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.note.refresh_from_db()
        self.assertEqual(self.note.tag, self.public_tag)
        self.assertFalse(self.note.public)


class BulkNoteInheritanceTest(TestCase):
    """POST /api/v1/notes/bulk/ — per-item `public` stays
    optional but an omitted flag inherits tag.public."""

    BASE_URL = '/api/v1/notes/bulk/'

    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user(
            username='bulk_owner', password='pass',
        )
        self.public_tag = Tag.objects.create(
            user=self.owner, name='Public tag', public=True,
        )
        self.private_tag = Tag.objects.create(
            user=self.owner, name='Private tag', public=False,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)

    def test_bulk_omitted_inherits_public_tag(self):
        """Omitted flags inherit tag.public; explicit flags win."""
        resp = self.client.post(
            self.BASE_URL,
            {
                'tag_id': self.public_tag.id,
                'notes': [
                    {'note_text': 'inherited'},
                    {'note_text': 'opted out', 'public': False},
                    {'note_text': 'explicit', 'public': True},
                ],
            },
            format='json',
        )
        self.assertEqual(resp.status_code, 201)
        flags = {
            n['note_text']: n['public'] for n in resp.json()
        }
        self.assertEqual(
            flags,
            {
                'inherited': True,
                'opted out': False,
                'explicit': True,
            },
        )

    def test_bulk_omitted_under_private_tag(self):
        """Omitted flags under a private tag land False —
        explicit true still wins."""
        resp = self.client.post(
            self.BASE_URL,
            {
                'tag_id': self.private_tag.id,
                'notes': [
                    {'note_text': 'inherited'},
                    {'note_text': 'explicit', 'public': True},
                ],
            },
            format='json',
        )
        self.assertEqual(resp.status_code, 201)
        flags = {
            n['note_text']: n['public'] for n in resp.json()
        }
        self.assertEqual(
            flags, {'inherited': False, 'explicit': True}
        )
