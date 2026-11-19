"""
Tests for the note-sharing feature: publish gate + published
directory (2.x), targeted note/tag sharing (3.x), bookmarks
and copying (4.x).

Covers the contract:
- published ⇒ public invariant (both directions) and the
  publish_note permission gate on every write path whose
  effective result is published=True;
- username resolution is silent (identical bodies for known
  and unknown names — no account-existence oracle);
- grants resolve live: tag subtrees gain coverage as notes
  arrive and lose it as notes move out;
- the copied_from provenance field is owner-only.
"""
import json

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from rest_framework.test import APIClient

from annotations.models import (
    Note, NoteShare, SavedNote, Tag, TagShare,
)
from annotations.services.note_text import note_text_to_plain

NOTES_URL = '/api/v1/notes/'
TAGS_URL = '/api/v1/tags/'
ME_URL = '/api/v1/users/me/'


def _tiptap(*paragraphs):
    """Build a serialized TipTap doc from plain paragraphs."""
    return json.dumps({
        'type': 'doc',
        'content': [
            {
                'type': 'paragraph',
                'content': [{'type': 'text', 'text': p}],
            }
            for p in paragraphs
        ],
    })


def _publisher(username):
    """Create a user holding annotations.publish_note."""
    User = get_user_model()
    user = User.objects.create_user(
        username=username, password='pass',
    )
    ct = ContentType.objects.get_for_model(Note)
    perm = Permission.objects.get(
        content_type=ct, codename='publish_note',
    )
    group = Group.objects.create(name=f'pub_{username}')
    group.permissions.add(perm)
    user.groups.add(group)
    return user


class PlainTextTest(TestCase):
    """note_text_to_plain — shared by serializer save paths,
    copy paths and the 0015 backfill migration."""

    def test_marks_paragraphs_and_verse_embeds(self):
        """Marks unwrap to their text, paragraphs become lines,
        scriptureRef atoms contribute nothing."""
        doc = json.dumps({
            'type': 'doc',
            'content': [
                {
                    'type': 'paragraph',
                    'content': [
                        {
                            'type': 'text',
                            'text': 'bold',
                            'marks': [{'type': 'bold'}],
                        },
                        {'type': 'text', 'text': ' and plain'},
                    ],
                },
                {
                    'type': 'scriptureRef',
                    'attrs': {'book': 'John', 'chapter': 3},
                },
                {
                    'type': 'paragraph',
                    'content': [
                        {'type': 'text', 'text': 'second line'},
                    ],
                },
            ],
        })
        self.assertEqual(
            note_text_to_plain(doc),
            'bold and plain\nsecond line',
        )

    def test_plain_string_passthrough(self):
        self.assertEqual(note_text_to_plain('hello'), 'hello')
        self.assertEqual(note_text_to_plain(''), '')
        self.assertEqual(note_text_to_plain(None), '')

    def test_serializer_maintains_field_on_write(self):
        owner = get_user_model().objects.create_user(
            username='pt_owner', password='pass',
        )
        tag = Tag.objects.create(user=owner, name='T')
        client = APIClient()
        client.force_authenticate(user=owner)
        resp = client.post(
            NOTES_URL,
            {
                'note_text': _tiptap('searchable words'),
                'tag': tag.id,
            },
            format='json',
        )
        self.assertEqual(resp.status_code, 201)
        note = Note.objects.get(pk=resp.json()['id'])
        self.assertEqual(
            note.note_text_plain, 'searchable words'
        )


class PublishGateTest(TestCase):
    """2.1/2.2 — the publish_note gate plus flag invariants."""

    def setUp(self):
        User = get_user_model()
        self.plain = User.objects.create_user(
            username='plain_user', password='pass',
        )
        self.publisher = _publisher('publisher_user')
        self.plain_client = APIClient()
        self.plain_client.force_authenticate(user=self.plain)
        self.pub_client = APIClient()
        self.pub_client.force_authenticate(user=self.publisher)
        self.plain_tag = Tag.objects.create(
            user=self.plain, name='T',
        )
        self.pub_tag = Tag.objects.create(
            user=self.publisher, name='T',
        )

    def test_create_published_requires_permission(self):
        """A plain user posting published=true gets a 400 in
        the annotations shape — not 403."""
        resp = self.plain_client.post(
            NOTES_URL,
            {
                'note_text': 'x',
                'published': True,
                'tag': self.plain_tag.id,
            },
            format='json',
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(
            resp.json()['error'], 'validation_error'
        )
        self.assertEqual(Note.objects.count(), 0)

    def test_publisher_can_publish(self):
        """A user with the permission publishes; published
        coerces public=true."""
        resp = self.pub_client.post(
            NOTES_URL,
            {
                'note_text': 'x',
                'published': True,
                'tag': self.pub_tag.id,
            },
            format='json',
        )
        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        self.assertTrue(body['published'])
        self.assertTrue(body['public'])

    def test_published_and_public_false_is_coerced(self):
        """published=true + public=false is unreachable —
        coercion forces public on."""
        resp = self.pub_client.post(
            NOTES_URL,
            {
                'note_text': 'x',
                'published': True,
                'public': False,
                'tag': self.pub_tag.id,
            },
            format='json',
        )
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(resp.json()['public'])
        self.assertTrue(resp.json()['published'])

    def test_patch_gate_and_downward_invariant(self):
        """PATCH published=true is gated; clearing public
        clears published; unpublishing leaves public."""
        note = Note.objects.create(
            user=self.publisher,
            note_text='n',
            public=True,
            published=True,
        )
        # Non-publisher owner can't even edit a published
        # note — its effective result stays published.
        note.user = self.plain
        note.save()
        resp = self.plain_client.patch(
            f'{NOTES_URL}{note.id}/',
            {'note_text': 'edit'},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)
        # But unpublishing works — effective result is False.
        resp = self.plain_client.patch(
            f'{NOTES_URL}{note.id}/',
            {'published': False},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        note.refresh_from_db()
        self.assertFalse(note.published)
        self.assertTrue(note.public)  # unpublish leaves public
        # Clearing public clears published.
        note.published = True
        note.save()
        resp = self.plain_client.patch(
            f'{NOTES_URL}{note.id}/',
            {'public': False},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        note.refresh_from_db()
        self.assertFalse(note.public)
        self.assertFalse(note.published)

    def test_tag_published_inheritance_is_gated(self):
        """A note created under a published tag inherits
        published — the inherited write is still gated, so a
        non-publisher can't inherit a published flag."""
        tag = Tag.objects.create(
            user=self.publisher, name='IH', published=True,
        )
        tag.user = self.plain
        tag.save()
        resp = self.plain_client.post(
            NOTES_URL,
            {'note_text': 'x', 'tag': tag.id},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(Note.objects.count(), 0)

    def test_bulk_create_publish_gate(self):
        """Bulk items inherit tag.published — gated; explicit
        published items are gated too."""
        tag = Tag.objects.create(user=self.plain, name='B')
        resp = self.plain_client.post(
            f'{NOTES_URL}bulk/',
            {
                'tag_id': tag.id,
                'notes': [{'note_text': 'a', 'published': True}],
            },
            format='json',
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(Note.objects.count(), 0)
        pub_tag = Tag.objects.create(
            user=self.plain, name='PB', published=True,
        )
        resp = self.plain_client.post(
            f'{NOTES_URL}bulk/',
            {'tag_id': pub_tag.id, 'notes': [{'note_text': 'a'}]},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(Note.objects.count(), 0)

    def test_set_published_gate_and_cascade(self):
        """set-published=true is gated and coerces notes to
        public; set-published=false clears published only."""
        tag = Tag.objects.create(user=self.plain, name='SP2')
        note = Note.objects.create(
            user=self.plain, tag=tag, note_text='n',
        )
        resp = self.plain_client.post(
            f'{TAGS_URL}{tag.id}/set-published/',
            {'published': True},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)
        # Publisher cascades published + public.
        tag.user = self.publisher
        tag.save()
        note.user = self.publisher
        note.save()
        resp = self.pub_client.post(
            f'{TAGS_URL}{tag.id}/set-published/',
            {'published': True},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        tag.refresh_from_db()
        note.refresh_from_db()
        self.assertTrue(tag.published)
        self.assertTrue(tag.public)
        self.assertTrue(note.published)
        self.assertTrue(note.public)
        # Downward: published cleared, public untouched.
        resp = self.pub_client.post(
            f'{TAGS_URL}{tag.id}/set-published/',
            {'published': False},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        note.refresh_from_db()
        self.assertFalse(note.published)
        self.assertTrue(note.public)

    def test_set_public_false_clears_published(self):
        """The set-public cascade clears published too when
        going private."""
        tag = Tag.objects.create(
            user=self.publisher, name='SP', published=True,
        )
        note = Note.objects.create(
            user=self.publisher, tag=tag, note_text='n',
            public=True, published=True,
        )
        resp = self.pub_client.post(
            f'{TAGS_URL}{tag.id}/set-public/',
            {'public': False},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        tag.refresh_from_db()
        note.refresh_from_db()
        self.assertFalse(tag.public)
        self.assertFalse(tag.published)
        self.assertFalse(note.public)
        self.assertFalse(note.published)

    def test_me_reports_can_publish(self):
        """/me/ exposes can_publish — group-granted True,
        plain False, guest False."""
        client = APIClient()
        client.force_authenticate(user=self.publisher)
        resp = client.get(ME_URL)
        self.assertTrue(resp.json()['can_publish'])
        client.force_authenticate(user=self.plain)
        resp = client.get(ME_URL)
        self.assertFalse(resp.json()['can_publish'])
        User = get_user_model()
        guest = User.objects.create_user(
            username='guest', password='x',
        )
        client.force_authenticate(user=guest)
        resp = client.get(ME_URL)
        self.assertFalse(resp.json()['can_publish'])

    def test_guest_cannot_publish(self):
        """The read-only guest account gets GuestReadOnly's
        403 before the gate can even run."""
        User = get_user_model()
        guest = User.objects.create_user(
            username='guest', password='x',
        )
        client = APIClient()
        client.force_authenticate(user=guest)
        resp = client.post(
            NOTES_URL,
            {'note_text': 'x', 'published': True},
            format='json',
        )
        self.assertEqual(resp.status_code, 403)


class DirectoryTest(TestCase):
    """2.4 — the published feed and ?q= search."""

    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user(
            username='dir_owner', password='pass',
        )
        self.client = APIClient()
        self.published_note = Note.objects.create(
            user=self.owner,
            note_text='b',
            note_text_plain='alpha omega',
            public=True,
            published=True,
        )
        Note.objects.create(
            user=self.owner,
            note_text='c',
            note_text_plain='hidden beta',
            public=True,
            published=False,
        )
        Note.objects.create(
            user=self.owner,
            note_text='d',
            note_text_plain='private alpha',
        )

    def test_published_feed_anonymous(self):
        """?published=true returns published notes only, open
        to anonymous callers, newest first."""
        resp = self.client.get(
            NOTES_URL, {'published': 'true'},
        )
        self.assertEqual(resp.status_code, 200)
        results = resp.json()['results']
        self.assertEqual(
            [n['id'] for n in results], [self.published_note.id]
        )
        self.assertEqual(
            results[0]['author']['username'], 'dir_owner'
        )

    def test_q_search_matches_plain_text(self):
        """?q= forces published scope + icontains on
        note_text_plain — private notes matching the term do
        not leak."""
        resp = self.client.get(NOTES_URL, {'q': 'alpha'})
        self.assertEqual(resp.status_code, 200)
        results = resp.json()['results']
        self.assertEqual(
            [n['id'] for n in results], [self.published_note.id]
        )

    def test_q_with_tag_id_is_400(self):
        resp = self.client.get(
            NOTES_URL, {'q': 'a', 'tag_id': 'TAGx'},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(
            resp.json()['error'], 'validation_error'
        )

    def test_q_on_detail_is_400(self):
        resp = self.client.get(
            f'{NOTES_URL}{self.published_note.id}/', {'q': 'a'},
        )
        self.assertEqual(resp.status_code, 400)

    def test_public_true_preserved(self):
        """Legacy ?public=true behavior is unchanged."""
        resp = self.client.get(NOTES_URL, {'public': 'true'})
        self.assertEqual(resp.status_code, 200)
        ids = {n['id'] for n in resp.json()['results']}
        self.assertEqual(len(ids), 2)


class ShareEndpointsTest(TestCase):
    """3.1 — grant/revoke/list endpoints and the silent-200
    username oracle guard."""

    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user(
            username='share_owner', password='pass',
        )
        self.recipient = User.objects.create_user(
            username='ShareRecipient', password='pass',
        )
        self.note = Note.objects.create(
            user=self.owner, note_text='n',
        )
        self.tag = Tag.objects.create(
            user=self.owner, name='T',
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)

    def test_share_silent_200_known_and_unknown(self):
        """Known and unknown usernames receive identical
        status/body — the response can't be probed for
        account existence."""
        resp_known = self.client.post(
            f'{NOTES_URL}{self.note.id}/share/',
            {'username': 'sharerecipient'},
            format='json',
        )
        resp_unknown = self.client.post(
            f'{NOTES_URL}{self.note.id}/share/',
            {'username': 'nosuchuser'},
            format='json',
        )
        self.assertEqual(resp_known.status_code, 200)
        self.assertEqual(resp_unknown.status_code, 200)
        self.assertEqual(resp_known.json(), resp_unknown.json())
        self.assertTrue(
            NoteShare.objects.filter(
                note=self.note, shared_with=self.recipient,
            ).exists()
        )

    def test_self_share_noop(self):
        resp = self.client.post(
            f'{NOTES_URL}{self.note.id}/share/',
            {'username': 'share_owner'},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(NoteShare.objects.exists())

    def test_non_owner_share_is_404(self):
        client = APIClient()
        client.force_authenticate(user=self.recipient)
        resp = client.post(
            f'{NOTES_URL}{self.note.id}/share/',
            {'username': 'share_owner'},
            format='json',
        )
        self.assertEqual(resp.status_code, 404)

    def test_shares_listing_owner_only(self):
        NoteShare.objects.create(
            note=self.note,
            owner=self.owner,
            shared_with=self.recipient,
        )
        resp = self.client.get(f'{NOTES_URL}{self.note.id}/shares/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.json()[0]['shared_with'], 'ShareRecipient'
        )
        client = APIClient()
        client.force_authenticate(user=self.recipient)
        resp = client.get(f'{NOTES_URL}{self.note.id}/shares/')
        self.assertEqual(resp.status_code, 404)

    def test_revoke_note_share(self):
        NoteShare.objects.create(
            note=self.note,
            owner=self.owner,
            shared_with=self.recipient,
        )
        resp = self.client.delete(
            f'{NOTES_URL}{self.note.id}/share/sharerecipient/'
        )
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(NoteShare.objects.exists())

    def test_tag_share_upserts_include_descendants(self):
        """Re-sharing the same tag+user flips
        include_descendants instead of duplicating."""
        self.client.post(
            f'{TAGS_URL}{self.tag.id}/share/',
            {'username': 'ShareRecipient'},
            format='json',
        )
        share = TagShare.objects.get()
        self.assertFalse(share.include_descendants)
        resp = self.client.post(
            f'{TAGS_URL}{self.tag.id}/share/',
            {
                'username': 'ShareRecipient',
                'include_descendants': True,
            },
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(TagShare.objects.count(), 1)
        share.refresh_from_db()
        self.assertTrue(share.include_descendants)

    def test_tag_share_and_revoke(self):
        resp = self.client.post(
            f'{TAGS_URL}{self.tag.id}/share/',
            {'username': 'ShareRecipient'},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        resp = self.client.get(f'{TAGS_URL}{self.tag.id}/shares/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()), 1)
        resp = self.client.delete(
            f'{TAGS_URL}{self.tag.id}/share/ShareRecipient/'
        )
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(TagShare.objects.exists())


class SharedVisibilityTest(TestCase):
    """2.3/3.2 — grants widen the central visibility funnel."""

    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user(
            username='vis_owner', password='pass',
        )
        self.sharee = User.objects.create_user(
            username='vis_sharee', password='pass',
        )
        self.other = User.objects.create_user(
            username='vis_other', password='pass',
        )
        self.tag = Tag.objects.create(user=self.owner, name='T')
        self.child = Tag.objects.create(
            user=self.owner, name='C', parent_tag=self.tag,
        )
        self.sharee_client = APIClient()
        self.sharee_client.force_authenticate(user=self.sharee)

    def _note(self, text='n', tag=None):
        return Note.objects.create(
            user=self.owner, tag=tag, note_text=text,
        )

    def test_direct_note_share_visible(self):
        note = self._note()
        resp = self.sharee_client.get(f'{NOTES_URL}{note.id}/')
        self.assertEqual(resp.status_code, 404)
        NoteShare.objects.create(
            note=note,
            owner=self.owner,
            shared_with=self.sharee,
        )
        resp = self.sharee_client.get(f'{NOTES_URL}{note.id}/')
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()['is_owner'])
        self.assertEqual(
            resp.json()['author'], {
                'id': self.owner.id, 'username': 'vis_owner',
            }
        )

    def test_tag_share_covers_notes_and_live_subtree(self):
        """A subtree grant covers existing notes AND notes
        added to descendant tags later; moving a note out of
        the subtree removes access."""
        direct = self._note(tag=self.tag)
        child_note = self._note(tag=self.child)
        TagShare.objects.create(
            tag=self.tag,
            owner=self.owner,
            shared_with=self.sharee,
            include_descendants=True,
        )
        for note in (direct, child_note):
            resp = self.sharee_client.get(
                f'{NOTES_URL}{note.id}/'
            )
            self.assertEqual(resp.status_code, 200)
        # Live resolution: a note added to the child tag later
        # is covered without touching the grant.
        later = self._note(tag=self.child)
        resp = self.sharee_client.get(f'{NOTES_URL}{later.id}/')
        self.assertEqual(resp.status_code, 200)
        # Moving out of the shared subtree revokes access.
        other_tag = Tag.objects.create(
            user=self.owner, name='Outside',
        )
        child_note.tag = other_tag
        child_note.save()
        resp = self.sharee_client.get(
            f'{NOTES_URL}{child_note.id}/'
        )
        self.assertEqual(resp.status_code, 404)

    def test_tag_share_without_descendants(self):
        """include_descendants=False covers the tag's own
        notes only."""
        direct = self._note(tag=self.tag)
        child_note = self._note(tag=self.child)
        TagShare.objects.create(
            tag=self.tag,
            owner=self.owner,
            shared_with=self.sharee,
            include_descendants=False,
        )
        self.assertEqual(
            self.sharee_client.get(
                f'{NOTES_URL}{direct.id}/'
            ).status_code,
            200,
        )
        self.assertEqual(
            self.sharee_client.get(
                f'{NOTES_URL}{child_note.id}/'
            ).status_code,
            404,
        )

    def test_shared_with_me_payload(self):
        """GET shared-with-me returns granted notes plus the
        tag grants; strangers see nothing."""
        note = self._note(tag=self.tag)
        NoteShare.objects.create(
            note=note,
            owner=self.owner,
            shared_with=self.sharee,
        )
        TagShare.objects.create(
            tag=self.tag,
            owner=self.owner,
            shared_with=self.sharee,
            include_descendants=True,
        )
        resp = self.sharee_client.get(
            f'{NOTES_URL}shared-with-me/'
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(
            [n['id'] for n in body['notes']], [note.id]
        )
        self.assertEqual(len(body['tag_shares']), 1)
        grant = body['tag_shares'][0]
        self.assertEqual(grant['name'], 'T')
        self.assertTrue(grant['include_descendants'])
        client = APIClient()
        client.force_authenticate(user=self.other)
        resp = client.get(f'{NOTES_URL}shared-with-me/')
        self.assertEqual(resp.json(), {'notes': [], 'tag_shares': []})

    def test_shared_tag_retrieve(self):
        """GET /tags/<id>/ resolves for sharees with a
        scoped note_count; strangers still 404."""
        self._note(tag=self.tag)
        TagShare.objects.create(
            tag=self.tag,
            owner=self.owner,
            shared_with=self.sharee,
        )
        resp = self.sharee_client.get(f'{TAGS_URL}{self.tag.id}/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['note_count'], 1)
        client = APIClient()
        client.force_authenticate(user=self.other)
        resp = client.get(f'{TAGS_URL}{self.tag.id}/')
        self.assertEqual(resp.status_code, 404)


class BookmarkTest(TestCase):
    """4.1 — SavedNote bookmarks."""

    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user(
            username='bm_owner', password='pass',
        )
        self.saver = User.objects.create_user(
            username='bm_saver', password='pass',
        )
        self.public_note = Note.objects.create(
            user=self.owner, note_text='n', public=True,
        )
        self.private_note = Note.objects.create(
            user=self.owner, note_text='p',
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.saver)

    def test_bookmark_public_note(self):
        resp = self.client.post(
            f'{NOTES_URL}{self.public_note.id}/bookmark/'
        )
        self.assertEqual(resp.status_code, 201)
        # Idempotent: a second POST returns success, one row.
        resp = self.client.post(
            f'{NOTES_URL}{self.public_note.id}/bookmark/'
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(SavedNote.objects.count(), 1)

    def test_bookmark_own_note_400(self):
        own = Note.objects.create(
            user=self.saver, note_text='mine', public=True,
        )
        resp = self.client.post(
            f'{NOTES_URL}{own.id}/bookmark/'
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(
            resp.json()['error'], 'validation_error'
        )

    def test_bookmark_inaccessible_404(self):
        resp = self.client.post(
            f'{NOTES_URL}{self.private_note.id}/bookmark/'
        )
        self.assertEqual(resp.status_code, 404)

    def test_bookmarks_listing_and_unavailable(self):
        """The listing embeds available notes; a bookmark that
        loses accessibility stays listed as unavailable."""
        SavedNote.objects.create(
            user=self.saver, note=self.public_note,
        )
        resp = self.client.get(f'{NOTES_URL}bookmarks/')
        self.assertEqual(resp.status_code, 200)
        row = resp.json()[0]
        self.assertTrue(row['available'])
        self.assertEqual(row['note']['id'], self.public_note.id)
        # Revoke access — row survives, marked unavailable.
        self.public_note.public = False
        self.public_note.save()
        resp = self.client.get(f'{NOTES_URL}bookmarks/')
        row = resp.json()[0]
        self.assertFalse(row['available'])
        self.assertEqual(row['note_id'], self.public_note.id)
        self.assertNotIn('note', row)
        # Removal still works once access is gone.
        resp = self.client.delete(
            f'{NOTES_URL}{self.public_note.id}/bookmark/'
        )
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(SavedNote.objects.exists())

    def test_copied_from_hidden_on_shared_output(self):
        """A published copy serialized to a non-owner does
        not carry copied_from."""
        copy = Note.objects.create(
            user=self.saver,
            note_text='c',
            public=True,
            published=True,
            copied_from=self.public_note,
        )
        resp = APIClient().get(f'{NOTES_URL}{copy.id}/')
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('copied_from', resp.json())


class CopyTest(TestCase):
    """4.2 — note and whole-tag copying."""

    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user(
            username='cp_owner', password='pass',
        )
        self.copier = User.objects.create_user(
            username='cp_copier', password='pass',
        )
        self.source_tag = Tag.objects.create(
            user=self.owner, name='Src',
        )
        self.target_tag = Tag.objects.create(
            user=self.copier, name='Dst',
        )
        self.source = Note.objects.create(
            user=self.owner,
            tag=self.source_tag,
            note_text='copy me',
            note_text_plain='copy me',
            public=True,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.copier)

    def test_copy_note_into_tag(self):
        resp = self.client.post(
            f'{NOTES_URL}{self.source.id}/copy/',
            {'tag_id': self.target_tag.id},
            format='json',
        )
        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        self.assertEqual(body['copied_from'], self.source.id)
        self.assertFalse(body['public'])
        self.assertFalse(body['published'])
        new_note = Note.objects.get(pk=body['id'])
        self.assertEqual(new_note.tag_id, self.target_tag.id)
        self.assertEqual(new_note.user_id, self.copier.id)
        self.assertEqual(new_note.note_text_plain, 'copy me')

    def test_copy_own_note_400(self):
        own = Note.objects.create(
            user=self.copier, note_text='mine', public=True,
        )
        resp = self.client.post(
            f'{NOTES_URL}{own.id}/copy/', {}, format='json',
        )
        self.assertEqual(resp.status_code, 400)

    def test_copy_inaccessible_404(self):
        private = Note.objects.create(
            user=self.owner, note_text='x',
        )
        resp = self.client.post(
            f'{NOTES_URL}{private.id}/copy/', {}, format='json',
        )
        self.assertEqual(resp.status_code, 404)

    def test_copy_foreign_tag_id_400(self):
        resp = self.client.post(
            f'{NOTES_URL}{self.source.id}/copy/',
            {'tag_id': self.source_tag.id},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)

    def test_tag_copy(self):
        """Whole-tag copy duplicates every accessible note
        atomically into the caller's tag."""
        second = Note.objects.create(
            user=self.owner,
            tag=self.source_tag,
            note_text='two',
            public=True,
        )
        private = Note.objects.create(
            user=self.owner,
            tag=self.source_tag,
            note_text='secret',
        )
        resp = self.client.post(
            f'{TAGS_URL}{self.source_tag.id}/copy/',
            {'tag_id': self.target_tag.id},
            format='json',
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()['copied'], 2)
        created = Note.objects.filter(
            user=self.copier, tag=self.target_tag,
        )
        self.assertEqual(created.count(), 2)
        texts = {n.note_text for n in created}
        self.assertEqual(texts, {'copy me', 'two'})
        # Positions appended and unique.
        positions = [n.tag_position for n in created]
        self.assertEqual(len(set(positions)), 2)
        private.refresh_from_db()
        self.assertEqual(private.user_id, self.owner.id)

    def test_tag_copy_over_cap_400(self):
        for i in range(101):
            Note.objects.create(
                user=self.owner,
                tag=self.source_tag,
                note_text=f'n{i}',
                public=True,
            )
        resp = self.client.post(
            f'{TAGS_URL}{self.source_tag.id}/copy/',
            {'tag_id': self.target_tag.id},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(
            Note.objects.filter(user=self.copier).exists()
        )
