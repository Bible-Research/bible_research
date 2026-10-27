"""
Tests for tag publicity: POST /api/v1/tags/<pk>/set-public/
(cascade) and the plain-PATCH `public` flag (no cascade).

set-public flips the tag's `public` flag AND every note
directly under it, atomically — descendant tags and their
notes are untouched (scope is the clicked tag only). A plain
PATCH flips only the flag.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from annotations.models import Note, Tag


class SetPublicTest(TestCase):
    """POST /api/v1/tags/<pk>/set-public/"""

    BASE_URL = '/api/v1/tags/'

    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user(
            username='publicity_owner', password='pass',
        )
        self.intruder = User.objects.create_user(
            username='publicity_intruder', password='pass',
        )
        self.tag = Tag.objects.create(
            user=self.owner, name='Parent',
        )
        self.child = Tag.objects.create(
            user=self.owner, name='Child', parent_tag=self.tag,
        )
        self.note1 = Note.objects.create(
            user=self.owner, tag=self.tag, note_text='n1',
        )
        self.note2 = Note.objects.create(
            user=self.owner, tag=self.tag, note_text='n2',
        )
        self.child_note = Note.objects.create(
            user=self.owner, tag=self.child, note_text='child',
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)

    def _url(self, tag):
        return f'{self.BASE_URL}{tag.id}/set-public/'

    def test_set_public_flips_flag_and_own_notes(self):
        """{"public": true} flips the tag and its direct notes;
        the response carries the tag payload + notes_updated."""
        resp = self.client.post(
            self._url(self.tag), {'public': True}, format='json'
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body['tag']['public'], True)
        self.assertEqual(body['tag']['note_count'], 2)
        self.assertEqual(body['notes_updated'], 2)
        self.tag.refresh_from_db()
        self.assertTrue(self.tag.public)
        for note in (self.note1, self.note2):
            note.refresh_from_db()
            self.assertTrue(note.public)

    def test_set_private_flips_notes_back(self):
        """Toggling private is symmetric — every note under the
        tag goes private again; notes_updated reports rows
        matched by the UPDATE."""
        self.client.post(
            self._url(self.tag), {'public': True}, format='json'
        )
        resp = self.client.post(
            self._url(self.tag), {'public': False}, format='json'
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['notes_updated'], 2)
        self.tag.refresh_from_db()
        self.assertFalse(self.tag.public)
        for note in (self.note1, self.note2):
            note.refresh_from_db()
            self.assertFalse(note.public)

    def test_idempotent_repeat_call(self):
        """Repeating the same set-public leaves the same end
        state — the action is idempotent. notes_updated counts
        rows MATCHED by the UPDATE (not rows changed), so the
        repeat still reports the tag's full note count."""
        self.client.post(
            self._url(self.tag), {'public': True}, format='json'
        )
        resp = self.client.post(
            self._url(self.tag), {'public': True}, format='json'
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['notes_updated'], 2)
        self.tag.refresh_from_db()
        self.assertTrue(self.tag.public)
        for note in (self.note1, self.note2):
            note.refresh_from_db()
            self.assertTrue(note.public)

    def test_descendant_tag_and_notes_untouched(self):
        """The cascade covers the clicked tag's notes only —
        child tags and their notes keep their flags."""
        resp = self.client.post(
            self._url(self.tag), {'public': True}, format='json'
        )
        self.assertEqual(resp.status_code, 200)
        self.child.refresh_from_db()
        self.assertFalse(self.child.public)
        self.child_note.refresh_from_db()
        self.assertFalse(self.child_note.public)

    def test_foreign_tag_404(self):
        """set-public on another user's tag is 404 — scoping
        blocks it before any write."""
        foreign = Tag.objects.create(
            user=self.intruder, name='Foreign',
        )
        resp = self.client.post(
            self._url(foreign), {'public': True}, format='json'
        )
        self.assertEqual(resp.status_code, 404)
        foreign.refresh_from_db()
        self.assertFalse(foreign.public)

    def test_non_boolean_public_400(self):
        """{"public": "yes"} is a 400 validation_error, not a
        silently truthy flip."""
        resp = self.client.post(
            self._url(self.tag), {'public': 'yes'}, format='json'
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()['error'], 'validation_error')
        self.tag.refresh_from_db()
        self.assertFalse(self.tag.public)

    def test_missing_public_400(self):
        """A body without 'public' is a 400."""
        resp = self.client.post(
            self._url(self.tag), {}, format='json'
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()['error'], 'validation_error')

    def test_non_object_body_400(self):
        """A non-dict JSON body is a 400, never a 500."""
        resp = self.client.post(
            self._url(self.tag), 'just a string', format='json'
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()['error'], 'validation_error')

    def test_anonymous_401(self):
        """Anonymous writes are rejected before get_object."""
        client = APIClient()
        resp = client.post(
            self._url(self.tag), {'public': True}, format='json'
        )
        self.assertEqual(resp.status_code, 401)
        self.tag.refresh_from_db()
        self.assertFalse(self.tag.public)


class PatchPublicFlagTest(TestCase):
    """Plain PATCH {public: …} flips the flag only — the note
    cascade lives exclusively in set-public."""

    BASE_URL = '/api/v1/tags/'

    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user(
            username='patch_owner', password='pass',
        )
        self.tag = Tag.objects.create(
            user=self.owner, name='Tag',
        )
        self.note = Note.objects.create(
            user=self.owner, tag=self.tag, note_text='n',
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)

    def test_patch_public_flips_flag_only(self):
        """PATCH {public: true} flips the tag flag; existing
        notes keep their own publicity."""
        resp = self.client.patch(
            f'{self.BASE_URL}{self.tag.id}/',
            {'public': True},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['public'], True)
        self.tag.refresh_from_db()
        self.assertTrue(self.tag.public)
        self.note.refresh_from_db()
        self.assertFalse(self.note.public)

    def test_create_tag_with_public_flag(self):
        """public is writable on create too."""
        resp = self.client.post(
            self.BASE_URL,
            {'name': 'Born public', 'public': True},
            format='json',
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()['public'], True)
        self.assertEqual(resp.json()['note_count'], 0)
