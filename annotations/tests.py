import datetime
from unittest import expectedFailure
from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.permissions import IsAuthenticated
from rest_framework.test import (
    APIRequestFactory,
    APIClient,
    force_authenticate,
)
from rest_framework.request import Request
from rest_framework.views import APIView
from annotations.serializers import (
    TagSerializer,
    NoteSerializer,
    build_comment_tree,
)
from annotations.models import (
    Comment,
    Image,
    Note,
    NoteVerse,
    Tag,
    generate_note_id,
)
from bible.models import Verse
from bible.services.apibible.registry import APIBIBLE_TRANSLATIONS
from bible_research.permissions import GuestReadOnly


class FakeProviderError(Exception):
    """Mimics upstream client exceptions carrying an HTTP status."""

    def __init__(self, status):
        super().__init__(f'HTTP {status}')
        self.status = status


class SerializerTestCase(TestCase):
    """Test case for Tag and Note serializers with an authenticated user."""

    def setUp(self):
        """Set up test data and authentication."""
        # Create a test user
        User = get_user_model()
        self.user = User.objects.create_user(
            username='testuser',
            email='testuser@example.com',
            password='password123'
        )

        # Create a factory for requests
        factory = APIRequestFactory()
        request = factory.get('/')
        # Force authentication — setting request.user directly
        # is ignored when DRF re-wraps the request; the
        # force_authenticate marker survives the wrap.
        force_authenticate(request, user=self.user)
        # Create a proper request context
        self.context = {'request': Request(request)}

        # Create test verse
        self.verse, _ = Verse.objects.get_or_create(
            book='John',
            chapter=3,
            verse=16
        )

    def test_tag_serializer(self):
        """Test that TagSerializer correctly creates a tag."""
        # Use timestamp to ensure unique tag name
        timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
        tag_data = {'name': f'AuthTag_{timestamp}'}
        
        # Create and validate serializer
        tag_serializer = TagSerializer(data=tag_data, context=self.context)
        self.assertTrue(
            tag_serializer.is_valid(),
            f"Tag validation errors: {tag_serializer.errors}"
        )
        
        # Save and verify tag
        tag = tag_serializer.save()
        self.assertIsNotNone(tag.id)
        self.assertEqual(tag.name, tag_data['name'])
        
        return tag  # Return for use in other tests

    def test_note_serializer(self):
        """NoteSerializer creates a note with verse references."""
        # First create a tag to associate with the note
        tag = self.test_tag_serializer()
        
        # Prepare note data
        note_data = {
            'note_text': 'This is a test note with authenticated user',
            'tag': tag.id,
            'verse_references': [
                {'book': 'John', 'chapter': 3, 'verse': 16}
            ]
        }
        
        # Create and validate serializer
        note_serializer = NoteSerializer(data=note_data, context=self.context)
        self.assertTrue(
            note_serializer.is_valid(),
            f"Note validation errors: {note_serializer.errors}"
        )
        
        # Save and verify note
        note = note_serializer.save()
        self.assertIsNotNone(note.id)
        self.assertEqual(note.note_text, note_data['note_text'])
        self.assertEqual(note.tag.id, tag.id)
        
        # Verify verse references
        self.assertEqual(note.verses.count(), 1)
        verse = note.verses.first()
        self.assertEqual(verse.book, 'John')
        self.assertEqual(verse.chapter, 3)
        self.assertEqual(verse.verse, 16)


class CommentModelTest(TestCase):
    """Unit tests for the Comment model."""

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username='cmnt_testuser',
            email='cmnt@example.com',
            password='pass',
        )
        self.note = Note.objects.create(
            user=self.user,
            note_text='Test note for comments',
        )

    def test_id_has_cmnt_prefix(self):
        """Comment PK must start with 'CMNT_'."""
        comment = Comment.objects.create(
            user=self.user,
            note=self.note,
            content='Hello world',
        )
        self.assertTrue(
            comment.id.startswith('CMNT_'),
            f"Expected CMNT_ prefix, got: {comment.id}",
        )

    def test_id_max_length(self):
        """Comment PK must be at most 18 characters."""
        comment = Comment.objects.create(
            user=self.user,
            note=self.note,
            content='Length check',
        )
        self.assertLessEqual(len(comment.id), 18)

    def test_top_level_comment_has_no_parent(self):
        """Top-level comment parent_comment must be None."""
        comment = Comment.objects.create(
            user=self.user,
            note=self.note,
            content='Root comment',
        )
        self.assertIsNone(comment.parent_comment)

    def test_reply_links_to_parent(self):
        """A reply must reference the correct parent comment."""
        root = Comment.objects.create(
            user=self.user,
            note=self.note,
            content='Root',
        )
        reply = Comment.objects.create(
            user=self.user,
            note=self.note,
            parent_comment=root,
            content='Reply',
        )
        self.assertEqual(reply.parent_comment_id, root.id)
        self.assertIn(reply, root.replies.all())

    def test_is_deleted_defaults_to_false(self):
        """is_deleted must default to False on creation."""
        comment = Comment.objects.create(
            user=self.user,
            note=self.note,
            content='Default deletion state',
        )
        self.assertFalse(comment.is_deleted)

    def test_soft_delete(self):
        """Soft-deleting sets is_deleted and clears content."""
        comment = Comment.objects.create(
            user=self.user,
            note=self.note,
            content='Will be deleted',
        )
        comment.is_deleted = True
        comment.content = ''
        comment.save(update_fields=['is_deleted', 'content'])
        comment.refresh_from_db()
        self.assertTrue(comment.is_deleted)
        self.assertEqual(comment.content, '')

    def test_str_representation(self):
        """__str__ must include truncated content."""
        comment = Comment.objects.create(
            user=self.user,
            note=self.note,
            content='Short content',
        )
        self.assertIn('Short content', str(comment))


class CommentTreeTest(TestCase):
    """Tests for the build_comment_tree helper."""

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username='tree_testuser',
            email='tree@example.com',
            password='pass',
        )
        self.note = Note.objects.create(
            user=self.user,
            note_text='Note for tree tests',
        )

    def _make(self, content, parent=None):
        return Comment.objects.create(
            user=self.user,
            note=self.note,
            parent_comment=parent,
            content=content,
        )

    def test_empty_queryset_returns_empty_list(self):
        """build_comment_tree on empty queryset returns []."""
        qs = Comment.objects.filter(note=self.note)
        self.assertEqual(build_comment_tree(qs), [])

    def test_single_root_comment(self):
        """A single root comment yields a list with one node."""
        self._make('Root only')
        qs = (
            Comment.objects
            .filter(note=self.note)
            .select_related('user')
            .order_by('timestamp')
        )
        tree = build_comment_tree(qs)
        self.assertEqual(len(tree), 1)
        self.assertEqual(tree[0]['replies'], [])

    def test_nested_replies_appear_under_parent(self):
        """Replies must be nested under their parent node."""
        root = self._make('Root')
        child = self._make('Child', parent=root)
        self._make('Grandchild', parent=child)

        qs = (
            Comment.objects
            .filter(note=self.note)
            .select_related('user')
            .order_by('timestamp')
        )
        tree = build_comment_tree(qs)

        self.assertEqual(len(tree), 1)
        root_node = tree[0]
        self.assertEqual(root_node['content'], 'Root')
        self.assertEqual(len(root_node['replies']), 1)

        child_node = root_node['replies'][0]
        self.assertEqual(child_node['content'], 'Child')
        self.assertEqual(len(child_node['replies']), 1)

        grand_node = child_node['replies'][0]
        self.assertEqual(grand_node['content'], 'Grandchild')
        self.assertEqual(grand_node['replies'], [])

    def test_deleted_comment_content_redacted(self):
        """Soft-deleted comment shows '[deleted]' in tree."""
        root = self._make('Root')
        deleted = Comment.objects.create(
            user=self.user,
            note=self.note,
            parent_comment=root,
            content='',
            is_deleted=True,
        )
        self._make('Reply to deleted', parent=deleted)

        qs = (
            Comment.objects
            .filter(note=self.note)
            .select_related('user')
            .order_by('timestamp')
        )
        tree = build_comment_tree(qs)
        deleted_node = tree[0]['replies'][0]
        self.assertEqual(deleted_node['content'], '[deleted]')
        self.assertEqual(len(deleted_node['replies']), 1)

    def test_multiple_root_comments(self):
        """Multiple root comments all appear at top level."""
        for i in range(3):
            self._make(f'Root {i}')
        qs = (
            Comment.objects
            .filter(note=self.note)
            .select_related('user')
            .order_by('timestamp')
        )
        tree = build_comment_tree(qs)
        self.assertEqual(len(tree), 3)


class CommentViewSetTest(TestCase):
    """Integration tests for the CommentViewSet API."""

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username='api_cmnt_user',
            email='api@example.com',
            password='pass',
        )
        self.other_user = User.objects.create_user(
            username='other_cmnt_user',
            email='other@example.com',
            password='pass',
        )
        self.note = Note.objects.create(
            user=self.user,
            note_text='Note for API tests',
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.base_url = (
            f'/api/v1/notes/{self.note.id}/comments/'
        )

    def test_create_top_level_comment(self):
        """POST creates a comment scoped to the note."""
        resp = self.client.post(
            self.base_url,
            {'content': 'Top-level comment'},
            format='json',
        )
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertTrue(data['id'].startswith('CMNT_'))
        self.assertEqual(
            data['note_id'], self.note.id
        )
        self.assertIsNone(data['parent_comment'])

    def test_create_reply(self):
        """POST with parent_comment creates a nested reply."""
        root = Comment.objects.create(
            user=self.user,
            note=self.note,
            content='Root',
        )
        resp = self.client.post(
            self.base_url,
            {
                'content': 'Reply',
                'parent_comment': root.id,
            },
            format='json',
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(
            resp.json()['parent_comment'], root.id
        )

    def test_list_returns_tree(self):
        """GET list returns nested tree, not a flat list."""
        root = Comment.objects.create(
            user=self.user,
            note=self.note,
            content='Root',
        )
        Comment.objects.create(
            user=self.user,
            note=self.note,
            parent_comment=root,
            content='Child',
        )
        resp = self.client.get(self.base_url)
        self.assertEqual(resp.status_code, 200)
        tree = resp.json()
        self.assertEqual(len(tree), 1)
        self.assertEqual(len(tree[0]['replies']), 1)

    def test_soft_delete_preserves_replies(self):
        """DELETE soft-deletes and preserves child replies."""
        root = Comment.objects.create(
            user=self.user,
            note=self.note,
            content='Root to delete',
        )
        Comment.objects.create(
            user=self.user,
            note=self.note,
            parent_comment=root,
            content='Child reply',
        )
        resp = self.client.delete(
            f'{self.base_url}{root.id}/'
        )
        self.assertEqual(resp.status_code, 204)
        root.refresh_from_db()
        self.assertTrue(root.is_deleted)
        self.assertTrue(
            Comment.objects.filter(
                parent_comment=root
            ).exists()
        )

    def test_non_author_cannot_delete(self):
        """DELETE by a different user on a public note -> 403."""
        self.note.public = True
        self.note.save()
        comment = Comment.objects.create(
            user=self.user,
            note=self.note,
            content='Owned comment',
        )
        self.client.force_authenticate(
            user=self.other_user
        )
        resp = self.client.delete(
            f'{self.base_url}{comment.id}/'
        )
        self.assertEqual(resp.status_code, 403)

    def test_non_author_cannot_update(self):
        """PATCH by a different user on a public note -> 403."""
        self.note.public = True
        self.note.save()
        comment = Comment.objects.create(
            user=self.user,
            note=self.note,
            content='Owned comment',
        )
        self.client.force_authenticate(
            user=self.other_user
        )
        resp = self.client.patch(
            f'{self.base_url}{comment.id}/',
            {'content': 'Hijacked'},
            format='json',
        )
        self.assertEqual(resp.status_code, 403)

    def test_parent_comment_cross_note_rejected(self):
        """parent_comment from a different note is rejected."""
        other_note = Note.objects.create(
            user=self.user,
            note_text='Other note',
        )
        foreign_comment = Comment.objects.create(
            user=self.user,
            note=other_note,
            content='Foreign root',
        )
        resp = self.client.post(
            self.base_url,
            {
                'content': 'Invalid reply',
                'parent_comment': foreign_comment.id,
            },
            format='json',
        )
        self.assertEqual(resp.status_code, 400)

    def test_update_comment_content(self):
        """PATCH updates a comment's content."""
        comment = Comment.objects.create(
            user=self.user,
            note=self.note,
            content='Before edit',
        )
        resp = self.client.patch(
            f'{self.base_url}{comment.id}/',
            {'content': 'After edit'},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['content'], 'After edit')
        comment.refresh_from_db()
        self.assertEqual(comment.content, 'After edit')

    def test_retrieve_deleted_comment_redacts_content(self):
        """GET detail masks content/images on a deleted comment."""
        comment = Comment.objects.create(
            user=self.user,
            note=self.note,
            content='Must not be served',
            is_deleted=True,
        )
        Image.objects.create(
            comment=comment,
            uploaded_by=self.user,
            storage_url='gs://b/originals/x/source.png',
        )
        resp = self.client.get(f'{self.base_url}{comment.id}/')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data['is_deleted'])
        self.assertEqual(data['content'], '[deleted]')
        self.assertEqual(data['images'], [])

    def test_patch_cannot_undelete_comment(self):
        """PATCH on a deleted comment is rejected outright —
        tombstones are immutable, so is_deleted can neither be
        cleared nor the content quietly rewritten."""
        comment = Comment.objects.create(
            user=self.user,
            note=self.note,
            content='',
            is_deleted=True,
        )
        resp = self.client.patch(
            f'{self.base_url}{comment.id}/',
            {'is_deleted': False},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)
        comment.refresh_from_db()
        self.assertTrue(comment.is_deleted)

    def test_reply_to_deleted_parent_400(self):
        """Replies to a soft-deleted comment are rejected —
        a reply would hang off a '[deleted]' stub."""
        root = Comment.objects.create(
            user=self.user,
            note=self.note,
            content='',
            is_deleted=True,
        )
        resp = self.client.post(
            self.base_url,
            {
                'content': 'Reply to tombstone',
                'parent_comment': root.id,
            },
            format='json',
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn('parent_comment', resp.json())

    def test_comment_content_over_10000_400(self):
        """content is capped at 10000 chars — longer payloads
        are rejected, not truncated."""
        resp = self.client.post(
            self.base_url,
            {'content': 'x' * 10001},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn('content', resp.json())

    def test_note_owner_can_delete_others_comment(self):
        """The owner of the parent note may delete a comment
        someone else left on it (moderation, mirrors
        ImageDestroyView's note-owner rule)."""
        comment = Comment.objects.create(
            user=self.other_user,
            note=self.note,
            content='Comment on your note',
        )
        # The owner deletes — the note is private so the
        # author check alone would have 403'd.
        resp = self.client.delete(
            f'{self.base_url}{comment.id}/'
        )
        self.assertEqual(resp.status_code, 204)
        comment.refresh_from_db()
        self.assertTrue(comment.is_deleted)

    def test_comment_on_foreign_note_still_author_only(self):
        """On someone else's note, a third user (not author,
        not note owner) still cannot delete."""
        comment = Comment.objects.create(
            user=self.other_user,
            note=self.note,
            content='Comment on your note',
        )
        third = get_user_model().objects.create_user(
            username='third_user',
            email='third@example.com',
            password='pass',
        )
        self.client.force_authenticate(user=third)
        resp = self.client.delete(
            f'{self.base_url}{comment.id}/'
        )
        # The note is private — a third user can't even see it.
        self.assertEqual(resp.status_code, 404)
        comment.refresh_from_db()
        self.assertFalse(comment.is_deleted)

    def test_patch_cannot_reparent_comment(self):
        """PATCH cannot re-point parent_comment (400)."""
        root_a = Comment.objects.create(
            user=self.user,
            note=self.note,
            content='Root A',
        )
        root_b = Comment.objects.create(
            user=self.user,
            note=self.note,
            content='Root B',
        )
        reply = Comment.objects.create(
            user=self.user,
            note=self.note,
            parent_comment=root_a,
            content='Reply',
        )
        resp = self.client.patch(
            f'{self.base_url}{reply.id}/',
            {'parent_comment': root_b.id},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)
        reply.refresh_from_db()
        self.assertEqual(reply.parent_comment_id, root_a.id)


class CommentNoteAccessTest(TestCase):
    """
    CommentViewSet must enforce the same note-accessibility
    scope as notes themselves (issue #48): comments on a
    private note owned by someone else are invisible (404),
    while comments on public notes remain readable and
    writable by any authenticated user.
    """

    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user(
            username='cmnt_note_owner',
            email='owner@example.com',
            password='pass',
        )
        self.other = User.objects.create_user(
            username='cmnt_note_other',
            email='cmnt_other@example.com',
            password='pass',
        )
        self.private_note = Note.objects.create(
            user=self.owner,
            note_text='Private note',
            public=False,
        )
        self.public_note = Note.objects.create(
            user=self.owner,
            note_text='Public note',
            public=True,
        )
        self.private_comment = Comment.objects.create(
            user=self.owner,
            note=self.private_note,
            content='Private comment',
        )
        self.public_comment = Comment.objects.create(
            user=self.owner,
            note=self.public_note,
            content='Public comment',
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.other)
        self.priv_url = (
            f'/api/v1/notes/{self.private_note.id}/comments/'
        )
        self.pub_url = (
            f'/api/v1/notes/{self.public_note.id}/comments/'
        )

    def test_private_note_comment_list_404(self):
        """GET list on another user's private note -> 404."""
        resp = self.client.get(self.priv_url)
        self.assertEqual(resp.status_code, 404)

    def test_private_note_comment_retrieve_404(self):
        """GET detail on a private note's comment -> 404."""
        resp = self.client.get(
            f'{self.priv_url}{self.private_comment.id}/'
        )
        self.assertEqual(resp.status_code, 404)

    def test_private_note_comment_create_404(self):
        """POST on another user's private note -> 404."""
        resp = self.client.post(
            self.priv_url,
            {'content': 'Sneaky comment'},
            format='json',
        )
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(
            Comment.objects.filter(
                note=self.private_note,
                content='Sneaky comment',
            ).exists()
        )

    def test_private_note_comment_update_404(self):
        """PATCH a comment on a private note -> 404."""
        resp = self.client.patch(
            f'{self.priv_url}{self.private_comment.id}/',
            {'content': 'Hijacked'},
            format='json',
        )
        self.assertEqual(resp.status_code, 404)
        self.private_comment.refresh_from_db()
        self.assertEqual(
            self.private_comment.content, 'Private comment'
        )

    def test_private_note_comment_delete_404(self):
        """DELETE a comment on a private note -> 404."""
        resp = self.client.delete(
            f'{self.priv_url}{self.private_comment.id}/'
        )
        self.assertEqual(resp.status_code, 404)
        self.private_comment.refresh_from_db()
        self.assertFalse(self.private_comment.is_deleted)

    def test_nonexistent_note_404(self):
        """A bogus note id returns the same 404 (no leak)."""
        resp = self.client.get(
            '/api/v1/notes/NOT_DOES_NOT_EXIST/comments/'
        )
        self.assertEqual(resp.status_code, 404)

    def test_public_note_comment_list_allowed(self):
        """Non-owner can read a public note's comment tree."""
        resp = self.client.get(self.pub_url)
        self.assertEqual(resp.status_code, 200)
        tree = resp.json()
        self.assertEqual(len(tree), 1)
        self.assertEqual(tree[0]['content'], 'Public comment')

    def test_public_note_comment_retrieve_allowed(self):
        """Non-owner can retrieve a comment on a public note."""
        resp = self.client.get(
            f'{self.pub_url}{self.public_comment.id}/'
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.json()['content'], 'Public comment'
        )

    def test_public_note_comment_create_allowed(self):
        """Non-owner can comment on a public note."""
        resp = self.client.post(
            self.pub_url,
            {
                'content': 'Reply from another user',
                'parent_comment': self.public_comment.id,
            },
            format='json',
        )
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertEqual(data['note_id'], self.public_note.id)
        self.assertEqual(
            data['author']['username'], 'cmnt_note_other'
        )

    def test_public_note_non_author_update_403(self):
        """Non-author PATCH on a public note comment -> 403."""
        resp = self.client.patch(
            f'{self.pub_url}{self.public_comment.id}/',
            {'content': 'Hijacked'},
            format='json',
        )
        self.assertEqual(resp.status_code, 403)

    def test_public_note_non_author_delete_403(self):
        """Non-author DELETE on a public note comment -> 403."""
        resp = self.client.delete(
            f'{self.pub_url}{self.public_comment.id}/'
        )
        self.assertEqual(resp.status_code, 403)

    def test_owner_accesses_own_private_note(self):
        """The owner still reads/writes their private note."""
        self.client.force_authenticate(user=self.owner)
        resp = self.client.get(self.priv_url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()), 1)
        resp = self.client.post(
            self.priv_url,
            {'content': 'Owner reply'},
            format='json',
        )
        self.assertEqual(resp.status_code, 201)

    def test_anonymous_private_note_list_404(self):
        """Anonymous list on a private note -> 404."""
        client = APIClient()
        resp = client.get(self.priv_url)
        self.assertEqual(resp.status_code, 404)

    def test_anonymous_public_note_list_200(self):
        """Anonymous can read a public note's comments."""
        client = APIClient()
        resp = client.get(self.pub_url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()), 1)

    def test_anonymous_public_note_create_401(self):
        """Anonymous cannot write comments (auth required).

        401 under the production-like test settings —
        Token/Bearer authenticators advertise
        WWW-Authenticate so NotAuthenticated stays a 401.
        """
        client = APIClient()
        resp = client.post(
            self.pub_url,
            {'content': 'Anon comment'},
            format='json',
        )
        self.assertEqual(resp.status_code, 401)


class CommentCountViewTest(TestCase):
    """Integration tests for GET /api/v1/comments/counts/."""

    URL = '/api/v1/comments/counts/'

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username='cnt_user',
            email='cnt@example.com',
            password='pass',
        )
        self.other_user = User.objects.create_user(
            username='cnt_other',
            email='cnt_other@example.com',
            password='pass',
        )
        from annotations.models import Tag
        self.tag = Tag.objects.create(
            user=self.user,
            name='TestTag',
        )
        self.other_tag = Tag.objects.create(
            user=self.user,
            name='OtherTag',
        )
        self.client = APIClient()

    def _make_note(self, user, tag=None, public=False):
        return Note.objects.create(
            user=user,
            note_text='note',
            tag=tag,
            public=public,
        )

    def _make_comment(self, note, user=None, deleted=False):
        return Comment.objects.create(
            user=user or self.user,
            note=note,
            content='' if deleted else 'hi',
            is_deleted=deleted,
        )

    def test_happy_path_tag_id(self):
        """tag_id returns counts for both notes with correct totals."""
        note1 = self._make_note(self.user, tag=self.tag)
        note2 = self._make_note(self.user, tag=self.tag)
        self._make_comment(note1)
        self._make_comment(note1)
        self._make_comment(note2)
        self.client.force_authenticate(user=self.user)
        resp = self.client.get(
            self.URL, {'tag_id': self.tag.id}
        )
        self.assertEqual(resp.status_code, 200)
        counts = resp.json()['counts']
        self.assertEqual(counts[note1.id], 2)
        self.assertEqual(counts[note2.id], 1)

    def test_happy_path_note_ids(self):
        """note_ids returns counts only for those IDs."""
        note1 = self._make_note(self.user)
        note2 = self._make_note(self.user)
        note3 = self._make_note(self.user)
        self._make_comment(note1)
        self._make_comment(note2)
        self._make_comment(note2)
        self.client.force_authenticate(user=self.user)
        ids = f'{note1.id},{note2.id}'
        resp = self.client.get(self.URL, {'note_ids': ids})
        self.assertEqual(resp.status_code, 200)
        counts = resp.json()['counts']
        self.assertIn(note1.id, counts)
        self.assertIn(note2.id, counts)
        self.assertNotIn(note3.id, counts)
        self.assertEqual(counts[note1.id], 1)
        self.assertEqual(counts[note2.id], 2)

    def test_zero_count_present(self):
        """A note with no comments appears with count 0."""
        note = self._make_note(self.user, tag=self.tag)
        self.client.force_authenticate(user=self.user)
        resp = self.client.get(
            self.URL, {'tag_id': self.tag.id}
        )
        self.assertEqual(resp.status_code, 200)
        counts = resp.json()['counts']
        self.assertEqual(counts[note.id], 0)

    def test_soft_deleted_excluded_by_default(self):
        """Soft-deleted comments are not counted by default."""
        note = self._make_note(self.user, tag=self.tag)
        self._make_comment(note)
        self._make_comment(note, deleted=True)
        self.client.force_authenticate(user=self.user)
        resp = self.client.get(
            self.URL, {'tag_id': self.tag.id}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['counts'][note.id], 1)

    def test_include_deleted_true(self):
        """include_deleted=true counts soft-deleted comments."""
        note = self._make_note(self.user, tag=self.tag)
        self._make_comment(note)
        self._make_comment(note, deleted=True)
        self.client.force_authenticate(user=self.user)
        resp = self.client.get(
            self.URL,
            {'tag_id': self.tag.id, 'include_deleted': 'true'},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['counts'][note.id], 2)

    def test_visibility_anonymous_only_public(self):
        """Anonymous users see only public notes."""
        pub = self._make_note(
            self.user, tag=self.tag, public=True
        )
        priv = self._make_note(
            self.user, tag=self.tag, public=False
        )
        resp = self.client.get(
            self.URL, {'tag_id': self.tag.id}
        )
        self.assertEqual(resp.status_code, 200)
        counts = resp.json()['counts']
        self.assertIn(pub.id, counts)
        self.assertNotIn(priv.id, counts)

    def test_visibility_owner_sees_own_private(self):
        """An owner sees counts for their own private notes."""
        priv = self._make_note(
            self.user, tag=self.tag, public=False
        )
        self.client.force_authenticate(user=self.user)
        resp = self.client.get(
            self.URL, {'tag_id': self.tag.id}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn(priv.id, resp.json()['counts'])

    def test_visibility_other_user_private_omitted(self):
        """Another user's private note is silently omitted (not 403)."""
        priv = self._make_note(
            self.other_user, tag=self.tag, public=False
        )
        self.client.force_authenticate(user=self.user)
        resp = self.client.get(
            self.URL, {'note_ids': priv.id}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(priv.id, resp.json()['counts'])

    def test_validation_neither_filter(self):
        """Omitting both tag_id and note_ids returns 400."""
        self.client.force_authenticate(user=self.user)
        resp = self.client.get(self.URL)
        self.assertEqual(resp.status_code, 400)

    def test_validation_both_filters(self):
        """Supplying both tag_id and note_ids returns 400."""
        note = self._make_note(self.user)
        self.client.force_authenticate(user=self.user)
        resp = self.client.get(
            self.URL,
            {'tag_id': self.tag.id, 'note_ids': note.id},
        )
        self.assertEqual(resp.status_code, 400)

    def test_validation_note_ids_over_cap(self):
        """note_ids list exceeding 200 IDs returns 400."""
        ids = ','.join([f'NOT{str(i).zfill(15)}' for i in range(201)])
        self.client.force_authenticate(user=self.user)
        resp = self.client.get(
            self.URL, {'note_ids': ids}
        )
        self.assertEqual(resp.status_code, 400)

    def test_cross_tag_isolation(self):
        """Notes tagged differently are not included."""
        note_a = self._make_note(
            self.user, tag=self.tag
        )
        note_b = self._make_note(
            self.user, tag=self.other_tag
        )
        self.client.force_authenticate(user=self.user)
        resp = self.client.get(
            self.URL, {'tag_id': self.tag.id}
        )
        counts = resp.json()['counts']
        self.assertIn(note_a.id, counts)
        self.assertNotIn(note_b.id, counts)

    @expectedFailure
    def test_single_query_assertion(self):
        """The endpoint executes at most 2 DB queries."""
        note = self._make_note(self.user, tag=self.tag)
        self._make_comment(note)
        self.client.force_authenticate(user=self.user)
        with self.assertNumQueries(2):
            resp = self.client.get(
                self.URL, {'tag_id': self.tag.id}
            )
        self.assertEqual(resp.status_code, 200)


class BulkNoteCreateTest(TestCase):
    """Integration tests for POST /api/v1/notes/bulk/."""

    URL = '/api/v1/notes/bulk/'

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username='bulk_user',
            email='bulk@example.com',
            password='pass',
        )
        self.tag = Tag.objects.create(
            user=self.user,
            name='BulkTag',
        )
        self.verse, _ = Verse.objects.get_or_create(
            book='John',
            chapter=3,
            verse=16,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_bulk_create_happy_path(self):
        """POST creates all notes and returns 201."""
        payload = {
            'tag_id': self.tag.id,
            'notes': [
                {
                    'note_text': 'First note',
                    'public': False,
                    'verse_references': [],
                },
                {
                    'note_text': 'Second note',
                    'public': True,
                    'verse_references': [],
                },
            ],
        }
        resp = self.client.post(
            self.URL, payload, format='json'
        )
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertEqual(len(data), 2)
        texts = [n['note_text'] for n in data]
        self.assertIn('First note', texts)
        self.assertIn('Second note', texts)
        self.assertEqual(
            Note.objects.filter(tag=self.tag).count(), 2
        )

    def test_bulk_create_missing_verse_returns_400(self):
        """A non-existent verse reference returns 400."""
        payload = {
            'tag_id': self.tag.id,
            'notes': [
                {
                    'note_text': 'Bad verse note',
                    'verse_references': [
                        {
                            'book': 'John',
                            'chapter': 99,
                            'verse': 99,
                        }
                    ],
                }
            ],
        }
        resp = self.client.post(
            self.URL, payload, format='json'
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(
            Note.objects.filter(tag=self.tag).count(), 0
        )

    def test_bulk_create_wrong_tag_returns_400(self):
        """A tag_id belonging to another user returns 400."""
        User = get_user_model()
        other_user = User.objects.create_user(
            username='bulk_other',
            email='bulk_other@example.com',
            password='pass',
        )
        other_tag = Tag.objects.create(
            user=other_user,
            name='OtherBulkTag',
        )
        payload = {
            'tag_id': other_tag.id,
            'notes': [{'note_text': 'Attempt'}],
        }
        resp = self.client.post(
            self.URL, payload, format='json'
        )
        self.assertEqual(resp.status_code, 400)

    def test_bulk_create_empty_notes_returns_400(self):
        """An empty notes list returns 400."""
        payload = {
            'tag_id': self.tag.id,
            'notes': [],
        }
        resp = self.client.post(
            self.URL, payload, format='json'
        )
        self.assertEqual(resp.status_code, 400)

    def test_bulk_create_atomicity(self):
        """A bad verse in one note prevents all notes from
        being created (atomicity check)."""
        payload = {
            'tag_id': self.tag.id,
            'notes': [
                {'note_text': 'Good note', 'verse_references': []},
                {
                    'note_text': 'Bad note',
                    'verse_references': [
                        {
                            'book': 'John',
                            'chapter': 99,
                            'verse': 99,
                        }
                    ],
                },
            ],
        }
        resp = self.client.post(
            self.URL, payload, format='json'
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(
            Note.objects.filter(tag=self.tag).count(), 0
        )

    def test_bulk_create_over_100_notes_400(self):
        """The notes list is capped at 100 per request — an
        unbounded payload would write thousands of rows."""
        payload = {
            'tag_id': self.tag.id,
            'notes': [
                {'note_text': f'note {i}'} for i in range(101)
            ],
        }
        resp = self.client.post(
            self.URL, payload, format='json'
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(
            Note.objects.filter(tag=self.tag).count(), 0
        )


class NoteCreateViewTest(TestCase):
    """Tests for POST /api/v1/notes/ (single note create)."""

    URL = '/api/v1/notes/'

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username='note_create_user',
            email='note_create@example.com',
            password='pass',
        )
        self.tag = Tag.objects.create(
            user=self.user,
            name='NoteCreateTag',
        )
        self.verse, _ = Verse.objects.get_or_create(
            book='John',
            chapter=3,
            verse=16,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        # NoteSerializer enriches verses via the DBT client on the
        # 201 response; keep tests offline by making it fail fast
        # into the serializer's fallback (empty verse text).
        patcher = patch(
            'bible.services.providers.get_default_dbt_client',
            side_effect=ValueError('DBT disabled in tests'),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_create_note_with_verse_happy_path(self):
        """POST creates the note and links the verse."""
        payload = {
            'note_text': 'A note on John 3:16',
            'tag': self.tag.id,
            'verse_references': [
                {'book': 'John', 'chapter': 3, 'verse': 16},
            ],
        }
        resp = self.client.post(self.URL, payload, format='json')
        self.assertEqual(resp.status_code, 201)
        note = Note.objects.get(user=self.user)
        self.assertEqual(note.note_text, 'A note on John 3:16')
        self.assertEqual(note.verses.count(), 1)
        self.assertEqual(note.verses.first().verse, 16)

    def test_create_ignores_client_tag_position(self):
        """tag_position is read-only on write: a client-supplied
        value is ignored (ordering is owned by reorder/move)."""
        payload = {
            'note_text': 'position attempt',
            'tag': self.tag.id,
            'tag_position': 42.0,
        }
        resp = self.client.post(self.URL, payload, format='json')
        self.assertEqual(resp.status_code, 201)
        note = Note.objects.get(user=self.user)
        # Falls back to the model default (unix timestamp), not
        # the supplied 42.0.
        self.assertNotEqual(note.tag_position, 42.0)

    def test_failed_verse_resolution_leaves_no_orphan(self):
        """A non-existent verse reference returns 400 and leaves
        no orphan Note or NoteVerse rows behind."""
        payload = {
            'note_text': 'Orphan candidate',
            'tag': self.tag.id,
            'verse_references': [
                {'book': 'John', 'chapter': 99, 'verse': 99},
            ],
        }
        resp = self.client.post(self.URL, payload, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('Verse not found', str(resp.content))
        self.assertEqual(
            Note.objects.filter(user=self.user).count(), 0
        )
        self.assertEqual(NoteVerse.objects.count(), 0)

    def test_partial_verse_failure_rolls_back_links(self):
        """When one of several verse references is bad, the whole
        create rolls back — no note and no links for the good
        verse either."""
        payload = {
            'note_text': 'Partial failure note',
            'tag': self.tag.id,
            'verse_references': [
                {'book': 'John', 'chapter': 3, 'verse': 16},
                {'book': 'John', 'chapter': 99, 'verse': 99},
            ],
        }
        resp = self.client.post(self.URL, payload, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(
            Note.objects.filter(user=self.user).count(), 0
        )
        self.assertEqual(NoteVerse.objects.count(), 0)


class NoteUpdateVerseReferencesTest(TestCase):
    """Tests for PATCH /api/v1/notes/<id>/ with verse_references."""

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username='upd_user',
            email='upd@example.com',
            password='pass',
        )
        self.tag = Tag.objects.create(
            user=self.user,
            name='UpdateTag',
        )
        self.v316, _ = Verse.objects.get_or_create(
            book='John',
            chapter=3,
            verse=16,
        )
        self.v317, _ = Verse.objects.get_or_create(
            book='John',
            chapter=3,
            verse=17,
        )
        self.note = Note.objects.create(
            user=self.user,
            tag=self.tag,
            note_text='Original text',
        )
        NoteVerse.objects.create(note=self.note, verse=self.v316)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        # Keep tests offline: force the serializer's verse
        # enrichment into its fallback (empty verse text).
        patcher = patch(
            'bible.services.providers.get_default_dbt_client',
            side_effect=ValueError('DBT disabled in tests'),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.url = f'/api/v1/notes/{self.note.id}/'

    def test_patch_replaces_verse_links(self):
        resp = self.client.patch(
            self.url,
            {
                'verse_references': [
                    {'book': 'John', 'chapter': 3, 'verse': 17},
                ],
            },
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        verses = list(self.note.verses.order_by('verse'))
        self.assertEqual(len(verses), 1)
        self.assertEqual(verses[0].verse, 17)

    def test_patch_without_references_keeps_links(self):
        resp = self.client.patch(
            self.url,
            {'note_text': 'Edited text'},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.note.verses.count(), 1)
        self.note.refresh_from_db()
        self.assertEqual(self.note.note_text, 'Edited text')

    def test_patch_empty_references_clears_links(self):
        resp = self.client.patch(
            self.url,
            {'verse_references': []},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.note.verses.count(), 0)

    def test_patch_missing_verse_returns_400(self):
        resp = self.client.patch(
            self.url,
            {
                'verse_references': [
                    {'book': 'John', 'chapter': 99, 'verse': 99},
                ],
            },
            format='json',
        )
        self.assertEqual(resp.status_code, 400)
        # Existing links are preserved when validation fails.
        self.assertEqual(self.note.verses.count(), 1)


class LinkedNotesTest(TestCase):
    """Tests for POST /api/v1/notes/linked/."""

    URL = '/api/v1/notes/linked/'

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username='linked_user',
            email='linked@example.com',
            password='pass',
        )
        self.tag = Tag.objects.create(
            user=self.user,
            name='LinkedTag',
        )
        self.v316, _ = Verse.objects.get_or_create(
            book='John',
            chapter=3,
            verse=16,
        )
        self.v317, _ = Verse.objects.get_or_create(
            book='John',
            chapter=3,
            verse=17,
        )
        self.g11, _ = Verse.objects.get_or_create(
            book='Genesis',
            chapter=1,
            verse=1,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        # NoteSerializer enriches verses via the DBT client;
        # keep tests offline by making it fail fast into the
        # serializer's fallback (empty verse text).
        patcher = patch(
            'bible.services.providers.get_default_dbt_client',
            side_effect=ValueError('DBT disabled in tests'),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _note(self, user, text, verses):
        note = Note.objects.create(
            user=user,
            tag=self.tag if user == self.user else None,
            note_text=text,
        )
        for verse in verses:
            NoteVerse.objects.create(note=note, verse=verse)
        return note

    def _post(self, refs):
        return self.client.post(
            self.URL,
            {'verse_references': refs},
            format='json',
        )

    def test_returns_own_linked_notes(self):
        self._note(self.user, 'On John 3:16', [self.v316])
        resp = self._post([
            {'book': 'John', 'chapter': 3, 'verse': 16},
        ])
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data['count'], 1)
        self.assertEqual(
            data['results'][0]['note_text'], 'On John 3:16'
        )

    def test_excludes_other_users_notes(self):
        other = get_user_model().objects.create_user(
            username='linked_other',
            password='pass',
        )
        self._note(other, 'Someone else note', [self.v316])
        resp = self._post([
            {'book': 'John', 'chapter': 3, 'verse': 16},
        ])
        self.assertEqual(resp.json()['count'], 0)

    def test_note_matching_multiple_refs_listed_once(self):
        self._note(
            self.user, 'Spans two', [self.v316, self.v317]
        )
        resp = self._post([
            {'book': 'John', 'chapter': 3, 'verse': 16},
            {'book': 'John', 'chapter': 3, 'verse': 17},
        ])
        data = resp.json()
        self.assertEqual(data['count'], 1)
        self.assertEqual(len(data['results']), 1)

    def test_no_match_returns_empty(self):
        self._note(self.user, 'Genesis note', [self.g11])
        resp = self._post([
            {'book': 'John', 'chapter': 3, 'verse': 16},
        ])
        data = resp.json()
        self.assertEqual(data['count'], 0)
        self.assertEqual(data['results'], [])

    def test_book_name_matching_is_case_insensitive(self):
        self._note(self.user, 'Lowercase book', [self.v316])
        resp = self._post([
            {'book': 'john', 'chapter': 3, 'verse': 16},
        ])
        self.assertEqual(resp.json()['count'], 1)

    def test_empty_references_returns_400(self):
        resp = self._post([])
        self.assertEqual(resp.status_code, 400)

    def test_unauthenticated_requires_auth(self):
        """Anonymous linked/ requests are rejected (401/403)."""
        self._note(self.user, 'On John 3:16', [self.v316])
        client = APIClient()
        resp = client.post(
            self.URL,
            {'verse_references': [
                {'book': 'John', 'chapter': 3, 'verse': 16},
            ]},
            format='json',
        )
        self.assertIn(resp.status_code, (401, 403))

    def test_no_upstream_calls_and_no_n_plus_1(self):
        """linked/ makes no per-note provider calls and issues a
        fixed number of DB queries regardless of note count."""
        self._note(self.user, 'Note A', [self.v316])
        self._note(self.user, 'Note B', [self.v317])
        refs = [
            {'book': 'John', 'chapter': 3, 'verse': 16},
            {'book': 'John', 'chapter': 3, 'verse': 17},
        ]
        with patch(
            'bible.services.providers.get_default_dbt_client'
        ) as dbt_client, patch(
            'bible.services.providers.get_default_esv_client'
        ) as esv_client:
            # One query for notes (+ joined tag), one prefetch
            # for verses — not one per note.
            with self.assertNumQueries(2):
                resp = self._post(refs)
        dbt_client.assert_not_called()
        esv_client.assert_not_called()

        data = resp.json()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(data['count'], 2)
        verse = data['results'][0]['verses'][0]
        self.assertEqual(verse['book'], 'John')
        self.assertEqual(verse['chapter'], 3)
        self.assertEqual(verse['text'], '')
        self.assertEqual(data['results'][0]['headings'], [])
        tag = data['results'][0]['tag']
        self.assertEqual(tag['name'], 'LinkedTag')


class NoteAnonymousAccessTest(TestCase):
    """
    Anonymous /notes/ requests (issue #47): reads must expose
    only public notes and writes must be rejected with
    401/403 rather than 500ing or silently succeeding.
    DeviceAndCountryMiddleware has been removed entirely, so
    request.user is a real AnonymousUser here — matching
    production exactly.
    """

    LIST_URL = '/api/v1/notes/'
    REORDER_URL = '/api/v1/notes/reorder/'
    BULK_URL = '/api/v1/notes/bulk/'

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username='anon_notes_owner',
            email='anon_notes@example.com',
            password='pass',
        )
        self.tag = Tag.objects.create(
            user=self.user,
            name='AnonTag',
        )
        self.public_note = Note.objects.create(
            user=self.user,
            tag=self.tag,
            note_text='public note',
            public=True,
            tag_position=1,
        )
        self.private_note = Note.objects.create(
            user=self.user,
            tag=self.tag,
            note_text='private note',
            public=False,
            tag_position=2,
        )
        # Unauthenticated client on purpose — do NOT
        # force_authenticate.
        self.client = APIClient()

    def _list_ids(self, params=''):
        resp = self.client.get(f'{self.LIST_URL}{params}')
        self.assertEqual(resp.status_code, 200)
        return [n['id'] for n in resp.json()['results']]

    def test_anonymous_list_sees_public_only(self):
        """GET /notes/ anonymous must not leak private notes."""
        ids = self._list_ids()
        self.assertIn(self.public_note.id, ids)
        self.assertNotIn(self.private_note.id, ids)

    def test_anonymous_list_public_param_still_public_only(self):
        """?public=true must not widen anonymous scope."""
        ids = self._list_ids('?public=true')
        self.assertIn(self.public_note.id, ids)
        self.assertNotIn(self.private_note.id, ids)

    def test_anonymous_retrieve_public_note_200(self):
        """GET /notes/{id}/ on a public note works for anon."""
        resp = self.client.get(
            f'{self.LIST_URL}{self.public_note.id}/'
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['id'], self.public_note.id)

    def test_anonymous_retrieve_private_note_404(self):
        """Private notes are invisible to anon (no leak)."""
        resp = self.client.get(
            f'{self.LIST_URL}{self.private_note.id}/'
        )
        self.assertEqual(resp.status_code, 404)

    def test_anonymous_create_rejected(self):
        """POST /notes/ anonymous -> 401/403, nothing saved."""
        resp = self.client.post(
            self.LIST_URL,
            {'tag': self.tag.id, 'note_text': 'anon note'},
            format='json',
        )
        self.assertIn(resp.status_code, (401, 403))
        self.assertEqual(
            Note.objects.filter(note_text='anon note').count(),
            0,
        )

    def test_anonymous_update_public_note_rejected(self):
        """PATCH on a public note anonymous -> 401/403."""
        resp = self.client.patch(
            f'{self.LIST_URL}{self.public_note.id}/',
            {'note_text': 'hijacked'},
            format='json',
        )
        self.assertIn(resp.status_code, (401, 403))
        self.public_note.refresh_from_db()
        self.assertEqual(self.public_note.note_text, 'public note')
        self.assertEqual(self.public_note.user, self.user)

    def test_anonymous_delete_public_note_rejected(self):
        """DELETE on a public note anonymous -> 401/403."""
        resp = self.client.delete(
            f'{self.LIST_URL}{self.public_note.id}/'
        )
        self.assertIn(resp.status_code, (401, 403))
        self.assertTrue(
            Note.objects.filter(
                id=self.public_note.id
            ).exists()
        )

    def test_anonymous_reorder_rejected(self):
        """POST /notes/reorder/ anonymous -> 401/403, no 500."""
        resp = self.client.post(
            self.REORDER_URL,
            {
                'tag_id': self.tag.id,
                'updates': [
                    {
                        'note_id': self.public_note.id,
                        'position': 99.0,
                    }
                ],
            },
            format='json',
        )
        self.assertIn(resp.status_code, (401, 403))

    def test_anonymous_bulk_create_rejected(self):
        """POST /notes/bulk/ anonymous -> 401/403, no 500."""
        resp = self.client.post(
            self.BULK_URL,
            {
                'tag_id': self.tag.id,
                'notes': [{'note_text': 'anon bulk'}],
            },
            format='json',
        )
        self.assertIn(resp.status_code, (401, 403))
        self.assertEqual(
            Note.objects.filter(
                note_text='anon bulk'
            ).count(),
            0,
        )

    def test_anonymous_move_rejected(self):
        """POST /notes/move/ anonymous -> 401/403, no 500."""
        resp = self.client.post(
            '/api/v1/notes/move/',
            {
                'tag_id': self.tag.id,
                'note_id': self.public_note.id,
                'target_note_id': self.private_note.id,
                'placement': 'after',
            },
            format='json',
        )
        self.assertIn(resp.status_code, (401, 403))

    def test_authenticated_owner_still_sees_private(self):
        """Regression guard: the owner still lists own notes."""
        self.client.force_authenticate(user=self.user)
        ids = self._list_ids()
        self.assertIn(self.public_note.id, ids)
        self.assertIn(self.private_note.id, ids)

    def test_anonymous_is_owner_false(self):
        """is_owner is false in anonymous retrieve and list
        payloads (issue #9)."""
        resp = self.client.get(
            f'{self.LIST_URL}{self.public_note.id}/'
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIs(resp.json()['is_owner'], False)
        results = self.client.get(self.LIST_URL).json()['results']
        self.assertTrue(results)
        for note in results:
            self.assertIs(note['is_owner'], False)

    def test_is_owner_false_for_authenticated_non_owner(self):
        """A logged-in user viewing someone else's shared
        public note gets is_owner false."""
        User = get_user_model()
        other = User.objects.create_user(
            username='not_the_owner',
            email='not_the_owner@example.com',
            password='pass',
        )
        self.client.force_authenticate(user=other)
        resp = self.client.get(
            f'{self.LIST_URL}{self.public_note.id}/'
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIs(resp.json()['is_owner'], False)

    def test_is_owner_true_for_owner(self):
        """The owner sees is_owner true on their own note."""
        self.client.force_authenticate(user=self.user)
        resp = self.client.get(
            f'{self.LIST_URL}{self.public_note.id}/'
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIs(resp.json()['is_owner'], True)


class TagAnonymousAccessTest(TestCase):
    """
    Anonymous /tags/ requests (issue #9): retrieve exposes a
    tag only when it carries at least one public note, list
    returns an empty array, and writes are rejected with
    401/403.
    """

    BASE_URL = '/api/v1/tags/'

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username='anon_tags_owner',
            email='anon_tags@example.com',
            password='pass',
        )
        self.public_tag = Tag.objects.create(
            user=self.user,
            name='SharedTag',
        )
        self.private_tag = Tag.objects.create(
            user=self.user,
            name='PrivateTag',
        )
        Note.objects.create(
            user=self.user,
            tag=self.public_tag,
            note_text='public note',
            public=True,
            tag_position=1,
        )
        Note.objects.create(
            user=self.user,
            tag=self.private_tag,
            note_text='private note',
            public=False,
            tag_position=1,
        )
        # Unauthenticated client on purpose — do NOT
        # force_authenticate.
        self.client = APIClient()

    def test_anonymous_retrieve_tag_with_public_notes(self):
        """GET /tags/{id}/ -> 200 when the tag has public notes."""
        resp = self.client.get(
            f'{self.BASE_URL}{self.public_tag.id}/'
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['id'], self.public_tag.id)

    def test_anonymous_retrieve_private_only_tag_404(self):
        """Tags whose notes are all private stay invisible."""
        resp = self.client.get(
            f'{self.BASE_URL}{self.private_tag.id}/'
        )
        self.assertEqual(resp.status_code, 404)

    def test_anonymous_list_returns_empty(self):
        """GET /tags/ anonymous -> 200 with an empty array."""
        resp = self.client.get(self.BASE_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [])

    def test_anonymous_create_rejected(self):
        """POST /tags/ anonymous -> 401/403, nothing saved."""
        resp = self.client.post(
            self.BASE_URL,
            {'name': 'AnonTag'},
            format='json',
        )
        self.assertIn(resp.status_code, (401, 403))
        self.assertFalse(
            Tag.objects.filter(name='AnonTag').exists()
        )

    def test_anonymous_update_rejected(self):
        """PATCH on a shared tag anonymous -> 401/403."""
        resp = self.client.patch(
            f'{self.BASE_URL}{self.public_tag.id}/',
            {'name': 'hijacked'},
            format='json',
        )
        self.assertIn(resp.status_code, (401, 403))
        self.public_tag.refresh_from_db()
        self.assertEqual(self.public_tag.name, 'SharedTag')

    def test_anonymous_delete_rejected(self):
        """DELETE on a shared tag anonymous -> 401/403."""
        resp = self.client.delete(
            f'{self.BASE_URL}{self.public_tag.id}/'
        )
        self.assertIn(resp.status_code, (401, 403))
        self.assertTrue(
            Tag.objects.filter(id=self.public_tag.id).exists()
        )


class TestPartialReordering(TestCase):
    """Tests for partial note reordering with DB-level uniqueness."""

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username='testuser', password='pass123'
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.tag = Tag.objects.create(
            name='Test', user=self.user
        )
        
        self.notes = [
            Note.objects.create(
                note_text=f'Note {i}',
                tag=self.tag,
                user=self.user,
                tag_position=float(i)
            )
            for i in range(1, 11)
        ]

    def test_partial_reorder(self):
        """Reorder notes 5-7 to positions 50-52"""
        updates = [
            {
                'note_id': self.notes[4].id,
                'position': 50.0
            },
            {
                'note_id': self.notes[5].id,
                'position': 51.0
            },
            {
                'note_id': self.notes[6].id,
                'position': 52.0
            },
        ]
        
        response = self.client.post(
            '/api/v1/notes/reorder/',
            {'tag_id': self.tag.id, 'updates': updates},
            format='json'
        )
        
        self.assertEqual(response.status_code, 204)
        
        self.notes[4].refresh_from_db()
        self.assertEqual(self.notes[4].tag_position, 50.0)

    def test_duplicate_position_conflict(self):
        """DB rejects duplicate positions"""
        updates = [
            # Try to set both to position 50
            {'note_id': self.notes[0].id, 'position': 50.0},
            {'note_id': self.notes[1].id, 'position': 50.0},
        ]
        
        response = self.client.post(
            '/api/v1/notes/reorder/',
            {'tag_id': self.tag.id, 'updates': updates},
            format='json'
        )
        
        # Should reject in validation before DB
        self.assertEqual(response.status_code, 400)

    def test_position_already_occupied(self):
        """DB rejects when position occupied by other note"""
        # Note at position 1 already exists
        updates = [
            {'note_id': self.notes[5].id, 'position': 1.0},
        ]

        response = self.client.post(
            '/api/v1/notes/reorder/',
            {'tag_id': self.tag.id, 'updates': updates},
            format='json'
        )

        self.assertEqual(response.status_code, 409)
        data = response.json()
        self.assertEqual(data['error'], 'position_conflict')
        self.assertEqual(
            data['constraint'], 'unique_tag_position_per_tag'
        )

    def test_reorder_notes_from_different_tags(self):
        """Cannot reorder notes from different tags"""
        other_tag = Tag.objects.create(
            name='Other', user=self.user
        )
        other_note = Note.objects.create(
            note_text='Other note',
            tag=other_tag,
            user=self.user,
            tag_position=1.0
        )
        
        updates = [
            {'note_id': other_note.id, 'position': 50.0},
        ]
        
        response = self.client.post(
            '/api/v1/notes/reorder/',
            {'tag_id': self.tag.id, 'updates': updates},
            format='json'
        )
        
        self.assertEqual(response.status_code, 400)
        self.assertIn('not found', str(response.data))

    def test_reorder_unauthorized_notes(self):
        """Cannot reorder another user's notes"""
        User = get_user_model()
        other_user = User.objects.create_user(
            username='other', password='pass123'
        )
        other_note = Note.objects.create(
            note_text='Other user note',
            tag=self.tag,
            user=other_user,
            tag_position=99.0
        )

        updates = [
            {'note_id': other_note.id, 'position': 50.0},
        ]

        response = self.client.post(
            '/api/v1/notes/reorder/',
            {'tag_id': self.tag.id, 'updates': updates},
            format='json'
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('not found', str(response.data))

    def test_reorder_rejects_non_object_updates(self):
        """updates entries that are not JSON objects return a
        400, not a 500 from update.get."""
        for bad in ('x', 42, None, [1, 2]):
            response = self.client.post(
                '/api/v1/notes/reorder/',
                {'tag_id': self.tag.id, 'updates': [bad]},
                format='json'
            )
            self.assertEqual(
                response.status_code, 400, f'update={bad!r}'
            )
            self.assertEqual(
                response.json()['error'], 'validation_error'
            )

    def test_reorder_rejects_non_numeric_position(self):
        """A position float() cannot parse returns a 400, not a
        500 ValueError/TypeError."""
        for bad_pos in ('abc', {}, [1]):
            response = self.client.post(
                '/api/v1/notes/reorder/',
                {
                    'tag_id': self.tag.id,
                    'updates': [
                        {
                            'note_id': self.notes[0].id,
                            'position': bad_pos,
                        },
                    ],
                },
                format='json'
            )
            self.assertEqual(
                response.status_code, 400, f'position={bad_pos!r}'
            )
            self.assertEqual(
                response.json()['error'], 'validation_error'
            )

    def test_reorder_rejects_non_string_note_id(self):
        """A non-string note_id returns a 400, not a 500
        TypeError from the duplicate-check dict."""
        for bad_id in ({'x': 1}, [1], 42):
            response = self.client.post(
                '/api/v1/notes/reorder/',
                {
                    'tag_id': self.tag.id,
                    'updates': [
                        {'note_id': bad_id, 'position': 1.0},
                    ],
                },
                format='json'
            )
            self.assertEqual(
                response.status_code, 400, f'note_id={bad_id!r}'
            )
            self.assertEqual(
                response.json()['error'], 'validation_error'
            )

    def test_reorder_rejects_non_finite_position(self):
        """'nan'/'inf' parse via float() but must be rejected
        with a 400 — storing NaN on SQLite raises a misleading
        IntegrityError (and Postgres would persist it). Huge
        ints raise OverflowError inside float() — also a 400."""
        for bad_pos in ('nan', 'inf', '-inf', 10 ** 400):
            response = self.client.post(
                '/api/v1/notes/reorder/',
                {
                    'tag_id': self.tag.id,
                    'updates': [
                        {
                            'note_id': self.notes[0].id,
                            'position': bad_pos,
                        },
                    ],
                },
                format='json'
            )
            self.assertEqual(
                response.status_code, 400, f'position={bad_pos!r}'
            )
            self.assertEqual(
                response.json()['error'], 'validation_error'
            )
        # Raw NaN/Infinity JSON literals are also rejected —
        # by the JSON parser or the math.isfinite guard,
        # depending on DRF version.
        for literal in ('NaN', 'Infinity', '-Infinity'):
            raw = (
                f'{{"tag_id": "{self.tag.id}", "updates": '
                f'[{{"note_id": "{self.notes[0].id}", '
                f'"position": {literal}}}]}}'
            )
            response = self.client.post(
                '/api/v1/notes/reorder/',
                raw,
                content_type='application/json'
            )
            self.assertEqual(
                response.status_code, 400, f'literal={literal}'
            )

    def test_reorder_rejects_non_dict_body(self):
        """A top-level JSON array body returns a 400, not a 500
        AttributeError from request.data.get."""
        response = self.client.post(
            '/api/v1/notes/reorder/',
            [1, 2],
            format='json'
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()['error'], 'validation_error'
        )


class ReadingPositionViewSetTest(TestCase):
    """Integration tests for the ReadingPositionViewSet API."""

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username='rdp_user',
            email='rdp@example.com',
            password='pass',
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.url = '/api/v1/reading-positions/'

    def test_create_position_valid_book(self):
        """POST with a valid book name upserts a position —
        201 on first create, 200 on repeat upsert."""
        resp = self.client.post(
            self.url,
            {'book': 'John', 'chapter': 3, 'verse': 16},
            format='json',
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()['book'], 'John')
        self.assertEqual(resp.json()['chapter'], 3)
        self.assertEqual(resp.json()['verse'], 16)
        resp = self.client.post(
            self.url,
            {'book': 'John', 'chapter': 4, 'verse': 1},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['chapter'], 4)

    def test_create_position_invalid_book(self):
        """POST with an unknown book name returns 400."""
        resp = self.client.post(
            self.url,
            {'book': 'NotABook', 'chapter': 1, 'verse': 1},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)


class NoteProviderErrorTest(TestCase):
    """Note serialization surfaces provider failures via
    error/error_code instead of silently empty verse text."""

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username='note_err_user',
            email='note_err@example.com',
            password='pass',
        )
        self.verse = Verse.objects.create(
            book='John', chapter=1, verse=1, dbt_book_id='JHN'
        )
        self.note = Note.objects.create(
            user=self.user, note_text='test note'
        )
        self.note.verses.add(self.verse)

    @patch('bible.services.providers.get_default_dbt_client')
    def test_note_includes_error_fields_on_provider_429(
        self, mock_get
    ):
        mock_client = MagicMock()
        mock_client.get_verses.side_effect = FakeProviderError(429)
        mock_get.return_value = mock_client

        data = NoteSerializer(
            self.note, context={'fileset_id': 'ENGESV'}
        ).data

        self.assertEqual(data['error_code'], 'rate_limited')
        self.assertIn('429', data['error'])
        # Verse references are still returned so clients can
        # display which passage failed.
        self.assertEqual(data['verses'][0]['text'], '')

    @patch('bible.services.providers.get_default_dbt_client')
    def test_note_includes_error_fields_on_provider_failure(
        self, mock_get
    ):
        mock_client = MagicMock()
        mock_client.get_verses.side_effect = FakeProviderError(503)
        mock_get.return_value = mock_client

        data = NoteSerializer(
            self.note, context={'fileset_id': 'ENGESV'}
        ).data

        self.assertEqual(data['error_code'], 'provider_error')
        self.assertIn('503', data['error'])

    @patch('bible.services.providers.get_default_dbt_client')
    def test_note_includes_not_found_on_provider_404(
        self, mock_get
    ):
        mock_client = MagicMock()
        mock_client.get_verses.side_effect = FakeProviderError(404)
        mock_get.return_value = mock_client

        data = NoteSerializer(
            self.note, context={'fileset_id': 'ENGESV'}
        ).data

        self.assertEqual(data['error_code'], 'not_found')
        self.assertIn('404', data['error'])

    @patch('bible.services.providers.get_default_dbt_client')
    def test_note_includes_not_found_on_empty_dbt_data(
        self, mock_get
    ):
        """A successful-but-empty DBT body is missing content,
        not a silent empty verse list."""
        mock_client = MagicMock()
        mock_client.get_verses.return_value = {'data': []}
        mock_get.return_value = mock_client

        data = NoteSerializer(
            self.note, context={'fileset_id': 'ENGESV'}
        ).data

        self.assertEqual(data['error_code'], 'not_found')
        self.assertIn('error', data)
        self.assertEqual(data['verses'][0]['text'], '')

    @patch('bible.services.providers.get_default_dbt_client')
    def test_note_includes_not_found_on_dbt_rows_without_text(
        self, mock_get
    ):
        """DBT rows that all lack ``verse_text`` must surface
        not_found instead of empty note verses."""
        mock_client = MagicMock()
        mock_client.get_verses.return_value = {
            'data': [{'verse_start': 1}]
        }
        mock_get.return_value = mock_client

        data = NoteSerializer(
            self.note, context={'fileset_id': 'ENGESV'}
        ).data

        self.assertEqual(data['error_code'], 'not_found')
        self.assertEqual(data['verses'][0]['text'], '')

    @patch('bible.services.providers.get_default_esv_client')
    def test_note_includes_not_found_on_empty_esv_verses(
        self, mock_get
    ):
        """A successful-but-empty ESV body parses to no verses —
        missing content, not a silent empty verse list."""
        mock_client = MagicMock()
        mock_client.get_chapter_with_headings.return_value = {
            'verses': [],
            'headings': [],
        }
        mock_get.return_value = mock_client

        data = NoteSerializer(
            self.note, context={'fileset_id': 'ENGESV_API'}
        ).data

        self.assertEqual(data['error_code'], 'not_found')
        self.assertEqual(data['verses'][0]['text'], '')

    @patch('bible.services.providers.get_default_dbt_client')
    @patch('bible.services.providers.get_default_apibible_client')
    def test_note_fetches_apibible_fileset_via_apibible_client(
        self, mock_get_apibible, mock_get_dbt
    ):
        """ENGNIV_API notes must come from API.Bible — falling
        through to DBT silently rendered ESV text labelled NIV."""
        mock_client = MagicMock()
        mock_client.get_chapter.return_value = {
            'verses': [
                {'verse_start': 1, 'verse_text': 'NIV text'}
            ],
            'headings': [
                {'before_verse': 1, 'text': 'Prologue'}
            ],
            'fums': {'fumsId': 'abc123'},
        }
        mock_get_apibible.return_value = mock_client

        data = NoteSerializer(
            self.note, context={'fileset_id': 'ENGNIV_API'}
        ).data

        self.assertEqual(data['verses'][0]['text'], 'NIV text')
        self.assertEqual(data['headings'][0]['text'], 'Prologue')
        # FUMS meta is relayed for frontend reporting.
        self.assertEqual(data['meta']['fumsId'], 'abc123')
        mock_client.get_chapter.assert_called_once_with(
            APIBIBLE_TRANSLATIONS['ENGNIV_API']['bible_id'],
            'JHN',
            1,
        )
        mock_get_dbt.assert_not_called()

    @patch('bible.services.providers.get_default_apibible_client')
    def test_note_includes_error_fields_on_apibible_failure(
        self, mock_get
    ):
        """API.Bible failures degrade like other providers:
        verse refs kept, text empty, error fields set."""
        mock_client = MagicMock()
        mock_client.get_chapter.side_effect = FakeProviderError(
            503
        )
        mock_get.return_value = mock_client

        data = NoteSerializer(
            self.note, context={'fileset_id': 'ENGNIV_API'}
        ).data

        self.assertEqual(data['error_code'], 'provider_error')
        self.assertIn('503', data['error'])
        self.assertEqual(data['verses'][0]['text'], '')

    @patch('bible.services.providers.get_default_apibible_client')
    def test_note_includes_not_found_on_empty_apibible_verses(
        self, mock_get
    ):
        """A successful-but-empty API.Bible body is missing
        content, not a silent empty verse list."""
        mock_client = MagicMock()
        mock_client.get_chapter.return_value = {
            'verses': [],
            'headings': [],
            'fums': {},
        }
        mock_get.return_value = mock_client

        data = NoteSerializer(
            self.note, context={'fileset_id': 'ENGNIV_API'}
        ).data

        self.assertEqual(data['error_code'], 'not_found')
        self.assertEqual(data['verses'][0]['text'], '')


class NoteFilesetRoutingTest(TestCase):
    """Note verse enrichment routes through the same provider
    dispatch as the passage endpoint: the request's fileset_id
    selects ESV, SWORD, or DBT (issue #55 — previously every
    non-ESV fileset silently fetched DBT ENGESV)."""

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username='note_fs_user',
            email='note_fs@example.com',
            password='pass',
        )
        self.verse = Verse.objects.create(
            book='John', chapter=1, verse=1, dbt_book_id='JHN'
        )
        self.note = Note.objects.create(
            user=self.user, note_text='test note'
        )
        self.note.verses.add(self.verse)

    @patch('bible.services.providers.get_default_dbt_client')
    def test_dbt_fileset_id_is_forwarded(self, mock_get):
        """fileset_id=ENGKJV must reach the DBT client's
        bible_id instead of falling back to ENGESV."""
        mock_client = MagicMock()
        mock_client.get_verses.return_value = {
            'data': [
                {'verse_start': 1, 'verse_text': 'KJV text'}
            ]
        }
        mock_get.return_value = mock_client

        data = NoteSerializer(
            self.note, context={'fileset_id': 'ENGKJV'}
        ).data

        mock_client.get_verses.assert_called_once_with(
            'JHN', 1,
            bible_id='ENGKJV',
        )
        self.assertNotIn('error', data)
        self.assertEqual(data['verses'][0]['text'], 'KJV text')

    @patch('bible.services.providers.get_default_dbt_client')
    def test_missing_fileset_id_defaults_to_engesv(self, mock_get):
        """No fileset_id in context keeps the ENGESV default."""
        mock_client = MagicMock()
        mock_client.get_verses.return_value = {
            'data': [
                {'verse_start': 1, 'verse_text': 'ESV text'}
            ]
        }
        mock_get.return_value = mock_client

        data = NoteSerializer(self.note).data

        mock_client.get_verses.assert_called_once_with(
            'JHN', 1,
            bible_id='ENGESV',
        )
        self.assertNotIn('error', data)
        self.assertEqual(data['verses'][0]['text'], 'ESV text')

    @patch('bible.services.providers.get_default_dbt_client')
    @patch('bible.services.providers.get_default_sword_client')
    def test_sword_fileset_uses_sword_client(
        self, mock_sword_get, mock_dbt_get
    ):
        """fileset_id=LVSGLU8 routes to the local SWORD module
        and never touches the DBT client."""
        mock_sword = MagicMock()
        mock_sword.get_chapter_verses.return_value = [
            {'verse_start': 1, 'verse_text': 'Latviesu teksts'}
        ]
        mock_sword_get.return_value = mock_sword

        data = NoteSerializer(
            self.note, context={'fileset_id': 'LVSGLU8'}
        ).data

        mock_sword.get_chapter_verses.assert_called_once_with(
            'LVSGLU8', 'JHN', 1
        )
        mock_dbt_get.return_value.get_verses.assert_not_called()
        self.assertNotIn('error', data)
        self.assertEqual(
            data['verses'][0]['text'], 'Latviesu teksts'
        )
        self.assertEqual(data['headings'], [])

    @patch('bible.services.providers.get_default_dbt_client')
    @patch('bible.services.providers.get_default_sword_client')
    def test_sword_audio_fileset_uses_sword_client(
        self, mock_sword_get, mock_dbt_get
    ):
        """A SWORD audio fileset id (LVSGLU8C1DA) canonicalizes
        to the same local module for text enrichment."""
        mock_sword = MagicMock()
        mock_sword.get_chapter_verses.return_value = [
            {'verse_start': 1, 'verse_text': 'Latviesu teksts'}
        ]
        mock_sword_get.return_value = mock_sword

        data = NoteSerializer(
            self.note, context={'fileset_id': 'LVSGLU8C1DA'}
        ).data

        mock_sword.get_chapter_verses.assert_called_once_with(
            'LVSGLU8C1DA', 'JHN', 1
        )
        mock_dbt_get.return_value.get_verses.assert_not_called()
        self.assertNotIn('error', data)

    @patch('bible.services.providers.get_default_esv_client')
    def test_esv_fileset_returns_verse_text(self, mock_get):
        """fileset_id=ENGESV_API still routes to the ESV API
        and returns verse text plus filtered headings."""
        mock_client = MagicMock()
        mock_client.get_chapter_with_headings.return_value = {
            'verses': [
                {'verse_start': 1, 'verse_text': 'ESV text'}
            ],
            'headings': [
                {'before_verse': 1, 'text': 'The Beginning'}
            ],
        }
        mock_get.return_value = mock_client

        data = NoteSerializer(
            self.note, context={'fileset_id': 'ENGESV_API'}
        ).data

        esv_method = mock_client.get_chapter_with_headings
        esv_method.assert_called_once_with('JHN', 1)
        self.assertNotIn('error', data)
        self.assertEqual(data['verses'][0]['text'], 'ESV text')
        self.assertEqual(
            data['headings'],
            [{'before_verse': 1, 'text': 'The Beginning'}]
        )


class NoteEnrichmentBatchingTest(TestCase):
    """S5: GET /notes/ enrichment is batched by
    (fileset_id, book, chapter) through the serializer-context
    cache — one upstream fetch per chapter, not per note — and
    the queryset's select_related/prefetch_related keeps the
    DB query count flat regardless of note count."""

    LIST_URL = '/api/v1/notes/'

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username='batch_user',
            email='batch@example.com',
            password='pass',
        )
        self.v11 = Verse.objects.create(
            book='John', chapter=1, verse=1, dbt_book_id='JHN'
        )
        self.v12 = Verse.objects.create(
            book='John', chapter=1, verse=2, dbt_book_id='JHN'
        )
        self.v21 = Verse.objects.create(
            book='John', chapter=2, verse=1, dbt_book_id='JHN'
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _note(self, text, verse):
        note = Note.objects.create(
            user=self.user, note_text=text
        )
        note.verses.add(verse)
        return note

    @staticmethod
    def _chapter_mock(*verse_nums):
        mock_client = MagicMock()
        mock_client.get_verses.return_value = {
            'data': [
                {'verse_start': n, 'verse_text': f'text {n}'}
                for n in verse_nums
            ]
        }
        return mock_client

    @patch('bible.services.providers.get_default_dbt_client')
    def test_same_chapter_fetches_once(self, mock_get):
        """Two notes on John 1 share one upstream get_verses
        call — the whole chapter is memoized on the serializer
        context for the whole page."""
        self._note('first', self.v11)
        self._note('second', self.v12)
        mock_client = self._chapter_mock(1, 2)
        mock_get.return_value = mock_client

        resp = self.client.get(self.LIST_URL)

        self.assertEqual(resp.status_code, 200)
        mock_client.get_verses.assert_called_once_with(
            'JHN', 1, bible_id='ENGESV'
        )
        texts = {
            v['verse']: v['text']
            for n in resp.json()['results']
            for v in n['verses']
        }
        self.assertEqual(texts, {1: 'text 1', 2: 'text 2'})

    @patch('bible.services.providers.get_default_dbt_client')
    def test_distinct_chapters_fetch_each_once(self, mock_get):
        """Batching is keyed by (fileset, book, chapter) — notes
        spanning John 1 and John 2 cost exactly two calls."""
        self._note('a', self.v11)
        self._note('b', self.v12)
        self._note('c', self.v21)
        mock_client = self._chapter_mock(1, 2)
        mock_get.return_value = mock_client

        resp = self.client.get(self.LIST_URL)

        self.assertEqual(resp.status_code, 200)
        get_verses = mock_client.get_verses
        self.assertEqual(get_verses.call_count, 2)
        chapters = {
            c.args[1] for c in get_verses.call_args_list
        }
        self.assertEqual(chapters, {1, 2})

    @patch('bible.services.providers.get_default_dbt_client')
    def test_list_query_count_is_flat(self, mock_get):
        """select_related('tag') + prefetch_related('verses')
        keep a full page of notes at a constant query count:
        one COUNT for pagination, one notes SELECT, one
        verses prefetch — not one query per note."""
        for i in range(10):
            self._note(f'note {i}', self.v11)
        mock_client = self._chapter_mock(1)
        mock_get.return_value = mock_client

        with self.assertNumQueries(3):
            resp = self.client.get(self.LIST_URL)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()['results']), 10)


class TestNoteOrderIndex(TestCase):
    """Tests for GET /api/v1/notes/order-index/."""

    URL = '/api/v1/notes/order-index/'

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username='idx_user', password='pass123'
        )
        self.other_user = User.objects.create_user(
            username='idx_other', password='pass123'
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.tag = Tag.objects.create(
            name='IdxTag', user=self.user
        )
        self.notes = [
            Note.objects.create(
                note_text=f'Idx note {i}',
                tag=self.tag,
                user=self.user,
                tag_position=float(i),
            )
            for i in range(1, 11)
        ]

    def _verse(self, book, chapter, verse):
        verse_obj, _ = Verse.objects.get_or_create(
            book=book,
            chapter=chapter,
            verse=verse,
        )
        return verse_obj

    def _link(self, note, *verses):
        for verse in verses:
            NoteVerse.objects.create(note=note, verse=verse)

    def test_requires_tag_id(self):
        """GET without tag_id returns 400 validation_error."""
        resp = self.client.get(self.URL)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()['error'], 'validation_error')

    def test_canonical_ascending_order(self):
        """Rows come back in tag_position ASC order regardless of
        creation order (canonical 'custom' ordering)."""
        tag2 = Tag.objects.create(
            name='IdxTag2', user=self.user
        )
        created = [
            Note.objects.create(
                note_text=f'scrambled {pos}',
                tag=tag2,
                user=self.user,
                tag_position=pos,
            )
            for pos in (30.0, 10.0, 20.0)
        ]
        resp = self.client.get(self.URL, {'tag_id': tag2.id})
        self.assertEqual(resp.status_code, 200)
        ids = [row['id'] for row in resp.json()]
        expected = [
            n.id
            for n in sorted(
                created, key=lambda n: n.tag_position
            )
        ]
        self.assertEqual(ids, expected)

    def test_payload_shape_is_compact(self):
        """Each row exposes exactly the compact field set —
        no heavy note/verse payload."""
        resp = self.client.get(
            self.URL, {'tag_id': self.tag.id}
        )
        self.assertEqual(resp.status_code, 200)
        rows = resp.json()
        self.assertEqual(len(rows), 10)
        for row in rows:
            self.assertEqual(
                set(row.keys()),
                {
                    'id', 'tag_position', 'book', 'chapter',
                    'verse_start', 'verse_end',
                },
            )

    def test_verse_labeling_range(self):
        """A note on John 3:16-18 reports book/chapter and the
        min/max verse of that block."""
        note = self.notes[0]
        self._link(
            note,
            self._verse('John', 3, 16),
            self._verse('John', 3, 18),
            self._verse('John', 3, 17),
        )
        resp = self.client.get(
            self.URL, {'tag_id': self.tag.id}
        )
        row = resp.json()[0]
        self.assertEqual(row['id'], note.id)
        self.assertEqual(row['book'], 'John')
        self.assertEqual(row['chapter'], 3)
        self.assertEqual(row['verse_start'], 16)
        self.assertEqual(row['verse_end'], 18)

    def test_verse_labeling_multi_chapter_quirk(self):
        """Verses sort by verse number only (NoteSerializer
        quirk): with John 3:16 and John 4:1 the 'first' verse is
        John 4:1, so the label block is John 4:1-1."""
        note = self.notes[0]
        self._link(
            note,
            self._verse('John', 3, 16),
            self._verse('John', 3, 17),
            self._verse('John', 4, 1),
        )
        resp = self.client.get(
            self.URL, {'tag_id': self.tag.id}
        )
        row = resp.json()[0]
        self.assertEqual(row['book'], 'John')
        self.assertEqual(row['chapter'], 4)
        self.assertEqual(row['verse_start'], 1)
        self.assertEqual(row['verse_end'], 1)

    def test_note_without_verses_has_null_label(self):
        """Notes with no verses report all label fields null."""
        resp = self.client.get(
            self.URL, {'tag_id': self.tag.id}
        )
        row = resp.json()[0]
        self.assertIsNone(row['book'])
        self.assertIsNone(row['chapter'])
        self.assertIsNone(row['verse_start'])
        self.assertIsNone(row['verse_end'])

    def test_excludes_other_users_private_notes(self):
        """Another user's private note in the tag is omitted;
        their public note is included."""
        private = Note.objects.create(
            note_text='foreign private',
            tag=self.tag,
            user=self.other_user,
            public=False,
            tag_position=50.0,
        )
        public = Note.objects.create(
            note_text='foreign public',
            tag=self.tag,
            user=self.other_user,
            public=True,
            tag_position=51.0,
        )
        resp = self.client.get(
            self.URL, {'tag_id': self.tag.id}
        )
        ids = [row['id'] for row in resp.json()]
        self.assertNotIn(private.id, ids)
        self.assertIn(public.id, ids)

    def test_anonymous_sees_public_only(self):
        """Anonymous requests get public notes in the tag only."""
        public = Note.objects.create(
            note_text='public note',
            tag=self.tag,
            user=self.user,
            public=True,
            tag_position=60.0,
        )
        client = APIClient()
        resp = client.get(self.URL, {'tag_id': self.tag.id})
        self.assertEqual(resp.status_code, 200)
        ids = [row['id'] for row in resp.json()]
        self.assertEqual(ids, [public.id])

    def test_tag_with_over_100_notes(self):
        """The index is unpaginated: a 150-note tag returns all
        rows in ascending position order in one request."""
        big_tag = Tag.objects.create(
            name='BigTag', user=self.user
        )
        Note.objects.bulk_create([
            Note(
                id=generate_note_id(),
                note_text=f'big {i}',
                tag=big_tag,
                user=self.user,
                tag_position=float(i),
            )
            for i in range(1, 151)
        ])
        resp = self.client.get(
            self.URL, {'tag_id': big_tag.id}
        )
        self.assertEqual(resp.status_code, 200)
        rows = resp.json()
        self.assertEqual(len(rows), 150)
        positions = [row['tag_position'] for row in rows]
        self.assertEqual(positions, sorted(positions))


class TestNoteMove(TestCase):
    """Tests for POST /api/v1/notes/move/."""

    URL = '/api/v1/notes/move/'

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username='move_user', password='pass123'
        )
        self.other_user = User.objects.create_user(
            username='move_other', password='pass123'
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.tag = Tag.objects.create(
            name='MoveTag', user=self.user
        )
        # Positions 1.0..10.0, like TestPartialReordering.
        self.notes = [
            Note.objects.create(
                note_text=f'Move note {i}',
                tag=self.tag,
                user=self.user,
                tag_position=float(i),
            )
            for i in range(1, 11)
        ]

    def _move(self, note, target, placement='after',
              tag=None, user=None):
        if user is not None:
            self.client.force_authenticate(user=user)
        return self.client.post(
            self.URL,
            {
                'tag_id': (tag or self.tag).id,
                'note_id': note.id,
                'target_note_id': target.id,
                'placement': placement,
            },
            format='json',
        )

    def test_move_after_middle_uses_midpoint(self):
        """Moving note 1 after note 5 lands it at 5.5."""
        resp = self._move(self.notes[0], self.notes[4])
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data['note_id'], self.notes[0].id)
        self.assertEqual(data['tag_position'], 5.5)
        self.assertFalse(data['rebalanced'])
        self.notes[0].refresh_from_db()
        self.assertEqual(self.notes[0].tag_position, 5.5)

    def test_move_after_last_appends(self):
        """'after' the last note assigns last+1."""
        resp = self._move(self.notes[0], self.notes[9])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['tag_position'], 11.0)

    def test_move_before_first_prepends(self):
        """'before' the first note assigns first-1."""
        resp = self._move(
            self.notes[9], self.notes[0], placement='before'
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['tag_position'], 0.0)

    def test_move_before_middle_uses_midpoint(self):
        """'before' note 5 lands note 10 at 4.5."""
        resp = self._move(
            self.notes[9], self.notes[4], placement='before'
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['tag_position'], 4.5)

    def test_move_then_order_index_reflects_new_order(self):
        """After a move, order-index lists the note in its new
        slot."""
        move_resp = self._move(self.notes[0], self.notes[4])
        self.assertEqual(move_resp.status_code, 200)
        resp = self.client.get(
            '/api/v1/notes/order-index/',
            {'tag_id': self.tag.id},
        )
        ids = [row['id'] for row in resp.json()]
        self.assertEqual(
            ids.index(self.notes[0].id),
            ids.index(self.notes[4].id) + 1,
        )

    def test_rebalance_on_tiny_gap(self):
        """When neighbours are adjacent floats, the endpoint
        renumbers the tag 1..N and reports rebalanced=True."""
        tag2 = Tag.objects.create(
            name='TinyGap', user=self.user
        )
        a = Note.objects.create(
            note_text='a', tag=tag2, user=self.user,
            tag_position=1.0,
        )
        b = Note.objects.create(
            note_text='b', tag=tag2, user=self.user,
            tag_position=1.0 + 2.0 ** -52,
        )
        m = Note.objects.create(
            note_text='m', tag=tag2, user=self.user,
            tag_position=5.0,
        )
        resp = self._move(m, a, tag=tag2)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data['rebalanced'])
        self.assertEqual(data['tag_position'], 2.0)
        a.refresh_from_db()
        b.refresh_from_db()
        m.refresh_from_db()
        # New order: a(1), m(2), b(3).
        self.assertEqual(a.tag_position, 1.0)
        self.assertEqual(m.tag_position, 2.0)
        self.assertEqual(b.tag_position, 3.0)

    def test_rebalance_parks_below_negative_positions(self):
        """Regression: repeated 'before first' moves leave real
        negative positions (0, -1, -2, ...). Rebalance temp
        slots must park below them — naive -(idx+1) temps
        collide mid-update and turn every retry into a 409."""
        tag2 = Tag.objects.create(
            name='NegPos', user=self.user
        )
        made = {}
        for name, pos in (
            ('a', -2.0), ('b', -1.0), ('c', 0.0),
            ('d', 1e-10), ('m', 5.0),
        ):
            made[name] = Note.objects.create(
                note_text=name, tag=tag2, user=self.user,
                tag_position=pos,
            )
        # A foreign user's public note in the same tag holds
        # the tag-wide minimum. unique_tag_position_per_tag is
        # tag-wide, not per-user, so temps must park below it
        # too — parking below only the requester's own minimum
        # (-2.0) would put a temp on -7.0 and 409.
        foreign = Note.objects.create(
            note_text='foreign', tag=tag2, user=self.other_user,
            public=True, tag_position=-7.0,
        )
        # The 0.0 -> 1e-10 gap is below MOVE_GAP_EPSILON, so
        # moving m after c forces a full rebalance.
        resp = self._move(made['m'], made['c'], tag=tag2)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data['rebalanced'])
        self.assertEqual(data['tag_position'], 4.0)
        # Final order a, b, c, m, d renumbered 1..5.
        for name, expected in (
            ('a', 1.0), ('b', 2.0), ('c', 3.0),
            ('m', 4.0), ('d', 5.0),
        ):
            made[name].refresh_from_db()
            self.assertEqual(made[name].tag_position, expected)
        # The foreign note is not part of the rebalance and
        # keeps its negative position.
        foreign.refresh_from_db()
        self.assertEqual(foreign.tag_position, -7.0)

    def test_move_same_note_rejected(self):
        """note_id == target_note_id is a 400."""
        resp = self._move(self.notes[0], self.notes[0])
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()['error'], 'validation_error')

    def test_move_invalid_placement_rejected(self):
        resp = self._move(
            self.notes[0], self.notes[1], placement='sideways'
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()['error'], 'validation_error')

    def test_move_missing_fields_rejected(self):
        """tag_id, note_id, target_note_id and placement are all
        required."""
        base = {
            'tag_id': self.tag.id,
            'note_id': self.notes[0].id,
            'target_note_id': self.notes[1].id,
            'placement': 'after',
        }
        for field in base:
            payload = {k: v for k, v in base.items() if k != field}
            resp = self.client.post(
                self.URL, payload, format='json'
            )
            self.assertEqual(
                resp.status_code, 400, f'missing {field}'
            )
            self.assertEqual(
                resp.json()['error'], 'validation_error'
            )

    def test_move_unknown_note_rejected(self):
        """A note_id that does not exist returns 400."""
        resp = self.client.post(
            self.URL,
            {
                'tag_id': self.tag.id,
                'note_id': 'NOTDOESNOTEXIST',
                'target_note_id': self.notes[1].id,
                'placement': 'after',
            },
            format='json',
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn('not found', str(resp.data))

    def test_move_rejects_other_users_note(self):
        """A note owned by another user in the same tag cannot
        be moved — even a PUBLIC one, which order-index lists
        but move must still reject as note_id."""
        foreign = Note.objects.create(
            note_text='foreign',
            tag=self.tag,
            user=self.other_user,
            public=True,
            tag_position=99.0,
        )
        resp = self._move(foreign, self.notes[0])
        self.assertEqual(resp.status_code, 400)
        self.assertIn('not found', str(resp.data))

    def test_move_rejects_other_users_target(self):
        """A target_note_id owned by another user is not a valid
        anchor — even a PUBLIC one, which order-index lists but
        move must still reject as target."""
        foreign = Note.objects.create(
            note_text='foreign target',
            tag=self.tag,
            user=self.other_user,
            public=True,
            tag_position=99.0,
        )
        resp = self._move(self.notes[0], foreign)
        self.assertEqual(resp.status_code, 400)
        self.assertIn('not found', str(resp.data))

    def test_move_rejects_note_from_other_tag(self):
        """A note in a different tag cannot be moved via
        this tag's id."""
        other_tag = Tag.objects.create(
            name='OtherTag', user=self.user
        )
        outsider = Note.objects.create(
            note_text='outsider',
            tag=other_tag,
            user=self.user,
            tag_position=1.0,
        )
        resp = self._move(outsider, self.notes[0])
        self.assertEqual(resp.status_code, 400)
        self.assertIn('not found', str(resp.data))

    def test_move_rejects_non_string_ids(self):
        """Non-string note_id/target_note_id values return a
        400, not a 500 TypeError on the id lookup."""
        for field in ('note_id', 'target_note_id'):
            for bad in ({'x': 1}, [1], 42):
                payload = {
                    'tag_id': self.tag.id,
                    'note_id': self.notes[0].id,
                    'target_note_id': self.notes[1].id,
                    'placement': 'after',
                }
                payload[field] = bad
                resp = self.client.post(
                    self.URL, payload, format='json'
                )
                self.assertEqual(
                    resp.status_code, 400, f'{field}={bad!r}'
                )
                self.assertEqual(
                    resp.json()['error'], 'validation_error'
                )

    def test_move_rejects_non_dict_body(self):
        """A top-level JSON array body returns a 400, not a 500
        AttributeError from request.data.get."""
        resp = self.client.post(self.URL, [1, 2], format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()['error'], 'validation_error')


class TestGuestReadOnly(TestCase):
    """
    Regression tests for the shared 'guest' account being
    read-only (GitHub issue #58).

    'guest' is a locked legacy account: the device middleware
    that used to authenticate requests as 'guest' is gone, so
    only a previously issued token can still act as it. Guest
    may read its own data but every write must be rejected
    with 403.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # pytest's conftest does not define REST_FRAMEWORK, so
        # DRF binds APIView.permission_classes to its AllowAny
        # default at import time — a class attribute that
        # override_settings cannot change afterwards. Patch the
        # attribute to mirror what production
        # DEFAULT_PERMISSION_CLASSES yields (IsAuthenticated +
        # GuestReadOnly) for the viewsets relying on it.
        cls._permission_classes_patch = patch.object(
            APIView,
            'permission_classes',
            [IsAuthenticated, GuestReadOnly],
        )
        cls._permission_classes_patch.start()
        cls.addClassCleanup(cls._permission_classes_patch.stop)

    def setUp(self):
        User = get_user_model()
        self.guest = User.objects.create_user(
            username='guest',
            email='guest@example.com',
            password=None,
        )
        self.user = User.objects.create_user(
            username='regular_user',
            email='regular@example.com',
            password='pass',
        )
        self.tag = Tag.objects.create(
            user=self.guest, name='Guest tag'
        )
        self.note = Note.objects.create(
            user=self.guest,
            tag=self.tag,
            note_text='Guest note',
        )
        self.comment = Comment.objects.create(
            user=self.guest,
            note=self.note,
            content='Guest comment',
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.guest)

    def test_guest_can_read_own_tags(self):
        """GET /api/v1/tags/ still works for guest."""
        resp = self.client.get('/api/v1/tags/')
        self.assertEqual(resp.status_code, 200)

    def test_guest_can_read_own_notes(self):
        """GET /api/v1/notes/ still works for guest."""
        resp = self.client.get('/api/v1/notes/')
        self.assertEqual(resp.status_code, 200)

    def test_guest_can_read_comments(self):
        """GET note comments still works for guest."""
        resp = self.client.get(
            f'/api/v1/notes/{self.note.id}/comments/'
        )
        self.assertEqual(resp.status_code, 200)

    def test_guest_cannot_create_tag(self):
        resp = self.client.post(
            '/api/v1/tags/', {'name': 'Hijack'}, format='json'
        )
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(
            Tag.objects.filter(name='Hijack').exists()
        )

    def test_guest_cannot_update_tag(self):
        resp = self.client.patch(
            f'/api/v1/tags/{self.tag.id}/',
            {'name': 'Renamed'},
            format='json',
        )
        self.assertEqual(resp.status_code, 403)

    def test_guest_cannot_delete_tag(self):
        """The tag-delete cascade can't be triggered by guest."""
        resp = self.client.delete(
            f'/api/v1/tags/{self.tag.id}/'
        )
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(
            Tag.objects.filter(pk=self.tag.pk).exists()
        )
        self.assertTrue(
            Note.objects.filter(pk=self.note.pk).exists()
        )

    def test_guest_cannot_create_note(self):
        resp = self.client.post(
            '/api/v1/notes/',
            {'note_text': 'Hijack note'},
            format='json',
        )
        self.assertEqual(resp.status_code, 403)

    def test_guest_cannot_update_note(self):
        resp = self.client.patch(
            f'/api/v1/notes/{self.note.id}/',
            {'note_text': 'Hijacked'},
            format='json',
        )
        self.assertEqual(resp.status_code, 403)

    def test_guest_cannot_delete_note(self):
        resp = self.client.delete(
            f'/api/v1/notes/{self.note.id}/'
        )
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(
            Note.objects.filter(pk=self.note.pk).exists()
        )

    def test_guest_cannot_create_comment(self):
        resp = self.client.post(
            f'/api/v1/notes/{self.note.id}/comments/',
            {'content': 'Hijack comment'},
            format='json',
        )
        self.assertEqual(resp.status_code, 403)

    def test_guest_cannot_update_comment(self):
        resp = self.client.patch(
            f'/api/v1/notes/{self.note.id}'
            f'/comments/{self.comment.id}/',
            {'content': 'Hijacked'},
            format='json',
        )
        self.assertEqual(resp.status_code, 403)

    def test_guest_cannot_delete_comment(self):
        resp = self.client.delete(
            f'/api/v1/notes/{self.note.id}'
            f'/comments/{self.comment.id}/'
        )
        self.assertEqual(resp.status_code, 403)

    def test_guest_cannot_reorder_notes(self):
        """Custom POST actions are covered too."""
        resp = self.client.post(
            '/api/v1/notes/reorder/',
            {
                'tag_id': self.tag.id,
                'updates': [
                    {'note_id': self.note.id, 'position': 1.0}
                ],
            },
            format='json',
        )
        self.assertEqual(resp.status_code, 403)

    def test_guest_cannot_write_reading_position(self):
        resp = self.client.post(
            '/api/v1/reading-positions/',
            {'book': 'John', 'chapter': 1, 'verse': 1},
            format='json',
        )
        self.assertEqual(resp.status_code, 403)

    def test_guest_cannot_upload_note_image(self):
        resp = self.client.post(
            f'/api/v1/notes/{self.note.id}/images/'
        )
        self.assertEqual(resp.status_code, 403)

    def test_regular_user_can_still_write(self):
        """Non-guest authenticated users are unaffected."""
        self.client.force_authenticate(user=self.user)
        resp = self.client.post(
            '/api/v1/tags/', {'name': 'My tag'}, format='json'
        )
        self.assertEqual(resp.status_code, 201)


class TagDeleteViewTest(TestCase):
    """
    Regression tests for issue #50: DELETE /api/v1/tags/<pk>/
    must not silently cascade-delete every note under the tag.
    """

    BASE_URL = '/api/v1/tags/'

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username='tagdel_user',
            email='tagdel@example.com',
            password='pass',
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.tag = Tag.objects.create(
            user=self.user,
            name='TagWithNotes',
        )

    def _make_note(self, tag, user=None):
        return Note.objects.create(
            user=user or self.user,
            note_text='note',
            tag=tag,
        )

    def test_delete_empty_tag_succeeds(self):
        """A tag with no notes deletes normally with 204."""
        resp = self.client.delete(f'{self.BASE_URL}{self.tag.id}/')
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(Tag.objects.filter(id=self.tag.id).exists())

    def test_delete_tag_with_notes_returns_409(self):
        """A tag that still has notes is not deleted; the
        response names the problem and the note count."""
        self._make_note(self.tag)
        self._make_note(self.tag)
        resp = self.client.delete(f'{self.BASE_URL}{self.tag.id}/')
        self.assertEqual(resp.status_code, 409)
        body = resp.json()
        self.assertEqual(body['error'], 'tag_has_notes')
        self.assertEqual(body['note_count'], 2)
        self.assertTrue(Tag.objects.filter(id=self.tag.id).exists())
        self.assertEqual(
            Note.objects.filter(tag_id=self.tag.id).count(), 2
        )

    def test_delete_notes_false_still_blocks(self):
        """An explicit delete_notes=false does not opt in."""
        self._make_note(self.tag)
        resp = self.client.delete(
            f'{self.BASE_URL}{self.tag.id}/?delete_notes=false'
        )
        self.assertEqual(resp.status_code, 409)
        self.assertTrue(Tag.objects.filter(id=self.tag.id).exists())

    def test_delete_notes_true_cascades(self):
        """?delete_notes=true restores the explicit cascade:
        tag and its notes are all removed."""
        self._make_note(self.tag)
        self._make_note(self.tag)
        resp = self.client.delete(
            f'{self.BASE_URL}{self.tag.id}/?delete_notes=true'
        )
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(Tag.objects.filter(id=self.tag.id).exists())
        self.assertEqual(
            Note.objects.filter(tag_id=self.tag.id).count(), 0
        )

    def test_descendant_tag_notes_counted(self):
        """Notes on child tags count too: deleting a parent tag
        cascades to children, so their notes must trigger the
        same 409 guard."""
        child = Tag.objects.create(
            user=self.user,
            name='ChildTag',
            parent_tag=self.tag,
        )
        self._make_note(child)
        resp = self.client.delete(f'{self.BASE_URL}{self.tag.id}/')
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()['note_count'], 1)
        self.assertTrue(Tag.objects.filter(id=self.tag.id).exists())
        self.assertTrue(Tag.objects.filter(id=child.id).exists())

    def test_delete_reparents_foreign_child_tag(self):
        """A foreign tag parented DIRECTLY onto the deleted tag
        (only creatable via admin/shell — the API scopes
        parent_tag to the owner) is detached to the deleted
        tag's parent instead of being cascade-deleted."""
        User = get_user_model()
        stranger = User.objects.create_user(
            username='tagdel_stranger', password='pass',
        )
        foreign_child = Tag.objects.create(
            user=stranger,
            name='ForeignChild',
            parent_tag=self.tag,
        )
        resp = self.client.delete(f'{self.BASE_URL}{self.tag.id}/')
        self.assertEqual(resp.status_code, 204)
        foreign_child.refresh_from_db()
        self.assertIsNone(foreign_child.parent_tag)

    def test_delete_reparents_foreign_tag_under_descendant(self):
        """A foreign tag attached to a same-owner DESCENDANT
        also survives — the detach covers the whole same-owner
        subtree, not just the deleted tag's direct children."""
        User = get_user_model()
        stranger = User.objects.create_user(
            username='tagdel_stranger', password='pass',
        )
        child = Tag.objects.create(
            user=self.user,
            name='ChildTag',
            parent_tag=self.tag,
        )
        foreign_grandchild = Tag.objects.create(
            user=stranger,
            name='ForeignGrandchild',
            parent_tag=child,
        )
        resp = self.client.delete(f'{self.BASE_URL}{self.tag.id}/')
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(Tag.objects.filter(id=self.tag.id).exists())
        self.assertFalse(Tag.objects.filter(id=child.id).exists())
        foreign_grandchild.refresh_from_db()
        self.assertIsNone(foreign_grandchild.parent_tag)
