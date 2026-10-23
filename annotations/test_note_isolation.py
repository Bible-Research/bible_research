"""Tests for /api/v1/notes/ scoping, writes, and reordering.

Covers the review's §7 gaps: authenticated list scoping
(views.py:564-579), cross-user writes on public/private notes
(perform_update/perform_destroy at views.py:367-430), the
?ordering= variants (_apply_ordering, views.py:432-499), the
`untagged` sentinel on order-index, and the two-phase reorder
swap (_park_notes_for_renumber, views.py:72-93, used at :925).
"""
import datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from annotations.models import Image, Note, NoteVerse, Tag
from annotations.views import UNTAGGED_TAG_ID
from bible.models import Verse


class NoteIsolationTest(TestCase):
    """List scoping and cross-user writes on /api/v1/notes/."""

    LIST_URL = '/api/v1/notes/'
    ORDER_INDEX_URL = '/api/v1/notes/order-index/'

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username='note_owner',
            email='note_owner@example.com',
            password='pass',
        )
        self.intruder = User.objects.create_user(
            username='note_intruder',
            email='note_intruder@example.com',
            password='pass',
        )
        self.tag = Tag.objects.create(
            user=self.user, name='Owned',
        )
        self.own_note = Note.objects.create(
            user=self.user, tag=self.tag, note_text='mine',
        )
        self.foreign_public = Note.objects.create(
            user=self.intruder,
            note_text='foreign public',
            public=True,
        )
        self.foreign_private = Note.objects.create(
            user=self.intruder, note_text='foreign private',
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _list_ids(self, params=None):
        resp = self.client.get(self.LIST_URL, params or {})
        self.assertEqual(resp.status_code, 200)
        return [n['id'] for n in resp.json()['results']]

    def test_list_excludes_foreign_private_notes(self):
        """Authenticated list shows only the caller's notes."""
        ids = self._list_ids()
        self.assertIn(self.own_note.id, ids)
        self.assertNotIn(self.foreign_private.id, ids)

    def test_list_public_true_widens_to_foreign_public(self):
        """?public=true adds other users' public notes but
        never their private ones (views.py:566-573)."""
        ids = self._list_ids({'public': 'true'})
        self.assertIn(self.own_note.id, ids)
        self.assertIn(self.foreign_public.id, ids)
        self.assertNotIn(self.foreign_private.id, ids)

    def test_list_default_excludes_foreign_public(self):
        """Without ?public=true, foreign public notes are
        absent too (views.py:574-578)."""
        ids = self._list_ids()
        self.assertIn(self.own_note.id, ids)
        self.assertNotIn(self.foreign_public.id, ids)
        self.assertNotIn(self.foreign_private.id, ids)

    def test_patch_foreign_public_note_403(self):
        """PATCH on another user's PUBLIC note reaches
        perform_update (public notes are in the accessible
        queryset) and is denied with 403."""
        resp = self.client.patch(
            f'{self.LIST_URL}{self.foreign_public.id}/',
            {'note_text': 'hijacked'},
            format='json',
        )
        self.assertEqual(resp.status_code, 403)
        self.foreign_public.refresh_from_db()
        self.assertEqual(
            self.foreign_public.note_text, 'foreign public'
        )
        self.assertEqual(self.foreign_public.user, self.intruder)

    def test_delete_foreign_public_note_403(self):
        """DELETE on a foreign public note 403s and the note
        survives (perform_destroy, views.py:416-425)."""
        resp = self.client.delete(
            f'{self.LIST_URL}{self.foreign_public.id}/'
        )
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(
            Note.objects.filter(
                id=self.foreign_public.id
            ).exists()
        )

    def test_patch_foreign_private_note_404(self):
        """A foreign PRIVATE note is not in the accessible
        queryset at all -> 404, no existence leak."""
        resp = self.client.patch(
            f'{self.LIST_URL}{self.foreign_private.id}/',
            {'note_text': 'hijacked'},
            format='json',
        )
        self.assertEqual(resp.status_code, 404)
        self.foreign_private.refresh_from_db()
        self.assertEqual(
            self.foreign_private.note_text, 'foreign private'
        )

    def test_owner_delete_204_cleans_verses(self):
        """Owner DELETE -> 204; NoteVerse through rows and
        Image rows cascade away with the note (the verse row
        itself is PROTECT-ed on the other side and survives)."""
        verse, _ = Verse.objects.get_or_create(
            book='John', chapter=3, verse=16,
        )
        note = Note.objects.create(
            user=self.user, tag=self.tag, note_text='doomed',
        )
        NoteVerse.objects.create(note=note, verse=verse)
        image = Image.objects.create(
            note=note,
            storage_url='gs://test-bucket/originals/x/a.jpg',
            uploaded_by=self.user,
        )
        with patch(
            'annotations.services.image_storage.delete_original'
        ) as mock_del:
            resp = self.client.delete(f'{self.LIST_URL}{note.id}/')
        self.assertEqual(resp.status_code, 204)
        # post_delete receiver: cascade-deleted Image rows also
        # schedule their GCS blob for deletion.
        mock_del.assert_called_once_with(
            image.id, image.storage_url
        )
        self.assertFalse(Note.objects.filter(id=note.id).exists())
        self.assertEqual(
            NoteVerse.objects.filter(note_id=note.id).count(), 0
        )
        self.assertEqual(
            Image.objects.filter(id=image.id).count(), 0
        )
        self.assertTrue(
            Verse.objects.filter(id=verse.id).exists()
        )

    def test_ordering_variants(self):
        """?ordering=<key> sorts per _apply_ordering.

        Three-note fixture is deliberately diagonal so each
        ordering produces a distinguishable sequence:
        positions run gen>exo>jhn (3,2,1), creation order and
        verse order both run gen<exo<jhn.
        """
        tag = Tag.objects.create(user=self.user, name='OrderTag')
        base = datetime.datetime(
            2026, 1, 1, tzinfo=datetime.timezone.utc
        )
        notes = {}
        specs = [
            # (key, tag_position, book, chapter, verse, day)
            ('gen', 3.0, 'Genesis', 1, 1, 0),
            ('exo', 2.0, 'Exodus', 1, 1, 1),
            ('jhn', 1.0, 'John', 3, 16, 2),
        ]
        for key, pos, book, chapter, vnum, days in specs:
            note = Note.objects.create(
                user=self.user, tag=tag,
                note_text=f'order {key}', tag_position=pos,
            )
            verse, _ = Verse.objects.get_or_create(
                book=book, chapter=chapter, verse=vnum,
            )
            NoteVerse.objects.create(note=note, verse=verse)
            # created_at is auto_now_add; set it explicitly via
            # update() so `created`/`-created` are deterministic.
            Note.objects.filter(id=note.id).update(
                created_at=base + datetime.timedelta(days=days)
            )
            notes[key] = note

        expected = {
            'custom': ['jhn', 'exo', 'gen'],
            '-custom': ['gen', 'exo', 'jhn'],
            'created': ['gen', 'exo', 'jhn'],
            '-created': ['jhn', 'exo', 'gen'],
            'verse': ['gen', 'exo', 'jhn'],
            '-verse': ['jhn', 'exo', 'gen'],
        }
        # Keep the run offline: serializing notes with verses
        # would otherwise hit the DBT provider; the serializer
        # degrades to empty text on any exception.
        with patch(
            'bible.services.providers.get_default_dbt_client',
            side_effect=ValueError('DBT disabled in tests'),
        ):
            for ordering, keys in expected.items():
                resp = self.client.get(
                    self.LIST_URL,
                    {'tag_id': tag.id, 'ordering': ordering},
                )
                self.assertEqual(
                    resp.status_code, 200, f'ordering={ordering}'
                )
                ids = [n['id'] for n in resp.json()['results']]
                want = [notes[k].id for k in keys]
                self.assertEqual(ids, want, f'ordering={ordering}')

    def test_untagged_order_index(self):
        """GET order-index/?tag_id=untagged resolves the
        sentinel — notes with no tag, matching GET /notes/ and
        /comments/counts/ semantics."""
        tagged = Note.objects.create(
            user=self.user, tag=self.tag, note_text='tagged',
        )
        untagged = Note.objects.create(
            user=self.user, note_text='no tag',
        )
        resp = self.client.get(
            self.ORDER_INDEX_URL, {'tag_id': UNTAGGED_TAG_ID}
        )
        self.assertEqual(resp.status_code, 200)
        ids = {entry['id'] for entry in resp.json()}
        # The foreign PUBLIC note has no tag and is inside the
        # accessible scope (own ∪ public), so it legitimately
        # appears; the tagged own note and the foreign private
        # note must not.
        self.assertIn(untagged.id, ids)
        self.assertIn(self.foreign_public.id, ids)
        self.assertNotIn(tagged.id, ids)
        self.assertNotIn(self.foreign_private.id, ids)


class ReorderSwapTest(TestCase):
    """POST /api/v1/notes/reorder/ two-phase position updates."""

    REORDER_URL = '/api/v1/notes/reorder/'

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username='reorder_user',
            email='reorder@example.com',
            password='pass',
        )
        self.tag = Tag.objects.create(
            user=self.user, name='ReorderTag',
        )
        self.note_a = Note.objects.create(
            user=self.user, tag=self.tag,
            note_text='a', tag_position=1.0,
        )
        self.note_b = Note.objects.create(
            user=self.user, tag=self.tag,
            note_text='b', tag_position=2.0,
        )
        self.note_c = Note.objects.create(
            user=self.user, tag=self.tag,
            note_text='c', tag_position=3.0,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _positions(self):
        for note in (self.note_a, self.note_b, self.note_c):
            note.refresh_from_db()
        return (
            self.note_a.tag_position,
            self.note_b.tag_position,
            self.note_c.tag_position,
        )

    def test_reorder_swap_two_notes(self):
        """Swapping A(1.0) and C(3.0) exercises the two-phase
        parking (_park_notes_for_renumber): without it, a
        single-phase update would IntegrityError on
        unique_tag_position_per_tag."""
        resp = self.client.post(
            self.REORDER_URL,
            {
                'tag_id': self.tag.id,
                'updates': [
                    {'note_id': self.note_a.id, 'position': 3.0},
                    {'note_id': self.note_c.id, 'position': 1.0},
                ],
            },
            format='json',
        )
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(self._positions(), (3.0, 2.0, 1.0))

    def test_reorder_position_occupied_by_unlisted_note_409(self):
        """Moving A onto B's occupied 2.0 without listing B
        conflicts at the DB constraint -> 409
        position_conflict. Mirrors the contract of
        tests.py::test_position_already_occupied so it stays
        covered if that class is ever refactored."""
        resp = self.client.post(
            self.REORDER_URL,
            {
                'tag_id': self.tag.id,
                'updates': [
                    {'note_id': self.note_a.id, 'position': 2.0},
                ],
            },
            format='json',
        )
        self.assertEqual(resp.status_code, 409)
        data = resp.json()
        self.assertEqual(data['error'], 'position_conflict')
        self.assertEqual(
            data['constraint'], 'unique_tag_position_per_tag'
        )
        # The failed reorder must leave positions untouched.
        self.assertEqual(self._positions(), (1.0, 2.0, 3.0))

    def test_reorder_over_100_updates_400(self):
        """The view caps reorder batches at 100 updates
        (views.py:774-785) -> 400 validation_error."""
        updates = [
            {'note_id': f'NOTX{i}', 'position': float(i)}
            for i in range(101)
        ]
        resp = self.client.post(
            self.REORDER_URL,
            {'tag_id': self.tag.id, 'updates': updates},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)
        data = resp.json()
        self.assertEqual(data['error'], 'validation_error')
        self.assertIn('100', data['message'])
