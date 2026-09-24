import datetime
from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIRequestFactory, APIClient
from rest_framework.request import Request
from annotations.serializers import (
    TagSerializer,
    NoteSerializer,
    build_comment_tree,
)
from annotations.models import Comment, Note, Tag
from bible.models import Verse


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
        # Force authentication
        request.user = self.user
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

    def test_non_owner_cannot_delete(self):
        """DELETE by a different user returns 403."""
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

    def test_non_owner_cannot_update(self):
        """PATCH by a different user returns 403."""
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
        self.assertEqual(data['error'], 'conflict')

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
        """POST with a valid book name upserts a position."""
        resp = self.client.post(
            self.url,
            {'book': 'John', 'chapter': 3, 'verse': 16},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['book'], 'John')
        self.assertEqual(resp.json()['chapter'], 3)
        self.assertEqual(resp.json()['verse'], 16)

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

    @patch('annotations.serializers.get_default_dbt_client')
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

    @patch('annotations.serializers.get_default_dbt_client')
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
