"""
Tests for GET /api/v1/notes/?tag_id=<pk>&include_descendants=
true — the merged subtree listing — and the read-only
`note_count` annotation on tag payloads.

Merged mode expands the tag_id filter to the tag's whole
subtree (subtree_tag_ids, bounded to the tag owner). Custom
orderings fall back to recency there since tag_position is
per-tag; the flag is ignored for tag_id=untagged and absent
tag_id. Visibility is unchanged — anonymous callers still see
public notes only.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from annotations.models import Note, Tag


class SubtreeNotesTest(TestCase):
    """?include_descendants=true on GET /api/v1/notes/"""

    BASE_URL = '/api/v1/notes/'

    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user(
            username='subtree_owner', password='pass',
        )
        self.intruder = User.objects.create_user(
            username='subtree_intruder', password='pass',
        )
        self.parent = Tag.objects.create(
            user=self.owner, name='Parent',
        )
        self.child = Tag.objects.create(
            user=self.owner, name='Child', parent_tag=self.parent,
        )
        self.grandchild = Tag.objects.create(
            user=self.owner, name='Grandchild',
            parent_tag=self.child,
        )
        self.other = Tag.objects.create(
            user=self.owner, name='Other',
        )
        self.n_p = Note.objects.create(
            user=self.owner, tag=self.parent,
            note_text='parent', tag_position=1.0,
        )
        self.n_c = Note.objects.create(
            user=self.owner, tag=self.child,
            note_text='child', tag_position=2.0,
        )
        self.n_g = Note.objects.create(
            user=self.owner, tag=self.grandchild,
            note_text='grandchild', tag_position=3.0,
        )
        self.n_other = Note.objects.create(
            user=self.owner, tag=self.other, note_text='other',
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)

    def _ids(self, resp):
        return [n['id'] for n in resp.json()['results']]

    def test_merged_returns_own_and_descendant_notes(self):
        """tag_id=P + include_descendants merges P, C and G
        notes into one paginated list."""
        resp = self.client.get(
            f'{self.BASE_URL}?tag_id={self.parent.id}'
            f'&include_descendants=true'
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['count'], 3)
        self.assertEqual(
            set(self._ids(resp)),
            {self.n_p.id, self.n_c.id, self.n_g.id},
        )

    def test_without_flag_single_tag(self):
        """Absent flag keeps today's single-tag behavior."""
        resp = self.client.get(
            f'{self.BASE_URL}?tag_id={self.parent.id}'
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            set(self._ids(resp)), {self.n_p.id}
        )

    def test_merged_from_mid_subtree(self):
        """Merging from C returns C+G — siblings and parents
        are not pulled in."""
        resp = self.client.get(
            f'{self.BASE_URL}?tag_id={self.child.id}'
            f'&include_descendants=true'
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            set(self._ids(resp)),
            {self.n_c.id, self.n_g.id},
        )

    def test_merged_pagination(self):
        """count is the merged total and page boundaries apply
        to the merged set."""
        resp = self.client.get(
            f'{self.BASE_URL}?tag_id={self.parent.id}'
            f'&include_descendants=true&page_size=2'
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['count'], 3)
        self.assertEqual(len(resp.json()['results']), 2)
        self.assertIsNotNone(resp.json()['next'])

        resp = self.client.get(
            resp.json()['next'].replace(
                'http://testserver', ''
            )
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()['results']), 1)

    def test_missing_tag_empty_not_500(self):
        """A nonexistent tag id yields an empty list."""
        resp = self.client.get(
            f'{self.BASE_URL}?tag_id=TAGNOPE'
            f'&include_descendants=true'
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['count'], 0)
        self.assertEqual(resp.json()['results'], [])

    def test_flag_ignored_for_untagged(self):
        """include_descendants is ignored (not an error) for
        tag_id=untagged."""
        Note.objects.create(
            user=self.owner, tag=None, note_text='loose',
        )
        resp = self.client.get(
            f'{self.BASE_URL}?tag_id=untagged'
            f'&include_descendants=true'
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['count'], 1)
        self.assertEqual(
            resp.json()['results'][0]['note_text'], 'loose'
        )

    def test_flag_ignored_without_tag_id(self):
        """include_descendants without tag_id changes nothing —
        the plain own-notes list comes back."""
        resp = self.client.get(
            f'{self.BASE_URL}?include_descendants=true'
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['count'], 4)

    def test_custom_ordering_falls_back_to_recency(self):
        """In merged mode ordering=custom degrades to
        -created_at: tag_position (n_p=1, n_c=2, n_g=3) would
        order ascending, the fallback is newest-first."""
        resp = self.client.get(
            f'{self.BASE_URL}?tag_id={self.parent.id}'
            f'&include_descendants=true&ordering=custom'
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            self._ids(resp),
            [self.n_g.id, self.n_c.id, self.n_p.id],
        )

    def test_reverse_custom_ordering_falls_back(self):
        """ordering=-custom degrades to created_at
        (oldest-first) in merged mode."""
        resp = self.client.get(
            f'{self.BASE_URL}?tag_id={self.parent.id}'
            f'&include_descendants=true&ordering=-custom'
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            self._ids(resp),
            [self.n_p.id, self.n_c.id, self.n_g.id],
        )

    def test_created_ordering_unchanged_in_merged_mode(self):
        """-created is not remapped — only the custom orderings
        fall back."""
        resp = self.client.get(
            f'{self.BASE_URL}?tag_id={self.parent.id}'
            f'&include_descendants=true&ordering=-created'
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            self._ids(resp),
            [self.n_g.id, self.n_c.id, self.n_p.id],
        )

    def test_anonymous_sees_only_public_descendant_notes(self):
        """Anonymous callers on a shared tag get only public
        notes — even in the merged subtree view."""
        self.n_p.public = False
        self.n_p.save()
        pub_p = Note.objects.create(
            user=self.owner, tag=self.parent,
            note_text='pub p', public=True,
        )
        pub_c = Note.objects.create(
            user=self.owner, tag=self.child,
            note_text='pub c', public=True,
        )
        client = APIClient()
        resp = client.get(
            f'{self.BASE_URL}?tag_id={self.parent.id}'
            f'&include_descendants=true'
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            set(self._ids(resp)), {pub_p.id, pub_c.id}
        )

    def test_foreign_tag_subtree_notes_hidden(self):
        """The intruder's public notes under their own tag are
        not merged into the owner's view; a foreign tag id
        simply resolves to nothing the owner can see."""
        foreign = Tag.objects.create(
            user=self.intruder, name='Foreign',
        )
        foreign_child = Tag.objects.create(
            user=self.intruder, name='Foreign child',
            parent_tag=foreign,
        )
        foreign_note = Note.objects.create(
            user=self.intruder, tag=foreign_child,
            note_text='foreign', public=True,
        )
        resp = self.client.get(
            f'{self.BASE_URL}?tag_id={foreign.id}'
            f'&include_descendants=true'
        )
        self.assertEqual(resp.status_code, 200)
        # Public notes ARE visible to any authenticated caller
        # via get_accessible_notes_qs; the subtree expansion
        # itself must not leak private foreign notes.
        self.assertEqual(
            set(self._ids(resp)), {foreign_note.id}
        )


class TagNoteCountTest(TestCase):
    """`note_count` annotation on tag payloads counts DIRECT
    notes only."""

    BASE_URL = '/api/v1/tags/'

    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user(
            username='count_owner', password='pass',
        )
        self.parent = Tag.objects.create(
            user=self.owner, name='Parent',
        )
        self.child = Tag.objects.create(
            user=self.owner, name='Child', parent_tag=self.parent,
        )
        Note.objects.create(
            user=self.owner, tag=self.parent, note_text='p',
        )
        Note.objects.create(
            user=self.owner, tag=self.child, note_text='c',
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)

    def test_note_count_direct_only(self):
        """The parent's badge counts only its own note — the
        child's note is aggregated client-side, not here."""
        resp = self.client.get(
            f'{self.BASE_URL}{self.parent.id}/'
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['note_count'], 1)

    def test_note_count_on_list(self):
        """List payloads carry the annotation too."""
        resp = self.client.get(self.BASE_URL)
        self.assertEqual(resp.status_code, 200)
        counts = {
            t['name']: t['note_count'] for t in resp.json()
        }
        self.assertEqual(
            counts, {'Parent': 1, 'Child': 1}
        )

    def test_anonymous_retrieve_counts_public_only(self):
        """A shared tag's badge must not leak private notes:
        anonymous retrieve counts public notes only."""
        Note.objects.create(
            user=self.owner, tag=self.child,
            note_text='pub', public=True,
        )
        client = APIClient()
        resp = client.get(f'{self.BASE_URL}{self.child.id}/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['note_count'], 1)

    def test_anonymous_retrieve_private_only_tag_404(self):
        """A tag with no public notes stays invisible to
        anonymous callers — count included."""
        client = APIClient()
        resp = client.get(f'{self.BASE_URL}{self.parent.id}/')
        self.assertEqual(resp.status_code, 404)
