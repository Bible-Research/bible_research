"""Comment endpoint isolation and residual coverage.

- The ``note_id=note_pk`` clause in
  ``CommentViewSet.get_queryset`` (views.py) is the only thing
  binding a comment to the note named in its URL; the
  cross-note detail tests pin the resulting 404.
- Private-note detail coverage already lives in
  ``CommentNoteAccessTest`` (annotations/tests.py) and is not
  duplicated here.
- Residual ``CommentCountView`` gaps: a foreign PUBLIC note via
  ``note_ids`` while authenticated, replies included in the
  count, and the 200-id boundary.
"""
from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from annotations.models import Comment, Note


class CommentCrossNoteTest(TestCase):
    """A comment addressed via the wrong note URL returns 404.

    Comment lives under accessible public note B; every request
    uses the URL of a *different* accessible note A. Without the
    ``note_id=note_pk`` filter these requests would resolve the
    comment and succeed.
    """

    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user(
            username='xnote_owner',
            email='xnote_owner@example.com',
            password='pass',
        )
        self.other = User.objects.create_user(
            username='xnote_other',
            email='xnote_other@example.com',
            password='pass',
        )
        # Both notes are public so note A is accessible to the
        # requester; only the note_id=note_pk clause can 404.
        self.note_a = Note.objects.create(
            user=self.other,
            note_text='Note A',
            public=True,
        )
        self.note_b = Note.objects.create(
            user=self.owner,
            note_text='Note B',
            public=True,
        )
        self.comment = Comment.objects.create(
            user=self.owner,
            note=self.note_b,
            content='Lives under B',
        )
        self.client = APIClient()
        # Authenticate as the comment author so PATCH/DELETE
        # would otherwise succeed — the 404 must come from the
        # note scoping, not the author check.
        self.client.force_authenticate(user=self.owner)

    def _wrong_url(self):
        return (
            f'/api/v1/notes/{self.note_a.id}'
            f'/comments/{self.comment.id}/'
        )

    def _right_url(self):
        return (
            f'/api/v1/notes/{self.note_b.id}'
            f'/comments/{self.comment.id}/'
        )

    def test_retrieve_comment_via_wrong_note_404(self):
        """GET /notes/<A>/comments/<C_B>/ -> 404."""
        resp = self.client.get(self._wrong_url())
        self.assertEqual(resp.status_code, 404)
        # Control: the same comment resolves under its own note.
        resp = self.client.get(self._right_url())
        self.assertEqual(resp.status_code, 200)

    def test_patch_comment_via_wrong_note_404(self):
        """PATCH via the wrong note -> 404, content unchanged."""
        resp = self.client.patch(
            self._wrong_url(),
            {'content': 'Hijacked'},
            format='json',
        )
        self.assertEqual(resp.status_code, 404)
        self.comment.refresh_from_db()
        self.assertEqual(self.comment.content, 'Lives under B')

    def test_delete_comment_via_wrong_note_404(self):
        """DELETE via the wrong note -> 404, comment kept."""
        resp = self.client.delete(self._wrong_url())
        self.assertEqual(resp.status_code, 404)
        self.comment.refresh_from_db()
        self.assertFalse(self.comment.is_deleted)
        self.assertEqual(self.comment.content, 'Lives under B')


class CommentShapeTest(TestCase):
    """CommentSerializer wire shape and create validation."""

    EXPECTED_KEYS = {
        'id',
        'author',
        'note_id',
        'parent_comment',
        'content',
        'timestamp',
        'is_deleted',
        'images',
    }

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username='shape_user',
            email='shape@example.com',
            password='pass',
        )
        self.note = Note.objects.create(
            user=self.user,
            note_text='Shape note',
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.base_url = (
            f'/api/v1/notes/{self.note.id}/comments/'
        )

    def test_comment_response_shape(self):
        """POST and GET detail return the exact serializer keys."""
        resp = self.client.post(
            self.base_url,
            {'content': 'Shaped comment'},
            format='json',
        )
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertEqual(set(data), self.EXPECTED_KEYS)
        self.assertEqual(set(data['author']), {'id', 'username'})
        self.assertIsNone(data['parent_comment'])
        self.assertFalse(data['is_deleted'])
        self.assertEqual(data['images'], [])

        resp = self.client.get(f"{self.base_url}{data['id']}/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(set(resp.json()), self.EXPECTED_KEYS)

    def test_create_missing_content_400(self):
        """POST without content -> 400."""
        resp = self.client.post(
            self.base_url,
            {},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)

    def test_create_bad_parent_comment_pk_400(self):
        """POST with a nonexistent parent_comment pk -> 400."""
        resp = self.client.post(
            self.base_url,
            {
                'content': 'Orphan reply',
                'parent_comment': 'CMNT_DOES_NOT_EXIST',
            },
            format='json',
        )
        self.assertEqual(resp.status_code, 400)


class CommentCountsResidualTest(TestCase):
    """Residual GET /api/v1/comments/counts/ gaps."""

    URL = '/api/v1/comments/counts/'

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username='cntres_user',
            email='cntres@example.com',
            password='pass',
        )
        self.other = User.objects.create_user(
            username='cntres_other',
            email='cntres_other@example.com',
            password='pass',
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_counts_foreign_public_note_when_authenticated(self):
        """note_ids may name another user's PUBLIC note."""
        note = Note.objects.create(
            user=self.other,
            note_text='Foreign public note',
            public=True,
        )
        Comment.objects.create(
            user=self.other,
            note=note,
            content='Foreign comment',
        )
        resp = self.client.get(self.URL, {'note_ids': note.id})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['counts'][note.id], 1)

    def test_counts_include_replies(self):
        """Count('comments') counts replies, not just roots."""
        note = Note.objects.create(
            user=self.user,
            note_text='Threaded note',
        )
        root = Comment.objects.create(
            user=self.user,
            note=note,
            content='Root',
        )
        for text in ('Reply one', 'Reply two'):
            Comment.objects.create(
                user=self.user,
                note=note,
                parent_comment=root,
                content=text,
            )
        resp = self.client.get(self.URL, {'note_ids': note.id})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['counts'][note.id], 3)

    def test_counts_exactly_200_ids_ok(self):
        """Exactly 200 note_ids is accepted (boundary)."""
        note = Note.objects.create(
            user=self.user,
            note_text='Boundary note',
        )
        Comment.objects.create(
            user=self.user,
            note=note,
            content='hi',
        )
        fake_ids = [f'NOT{str(i).zfill(15)}' for i in range(199)]
        ids = ','.join([note.id] + fake_ids)
        self.assertEqual(len(ids.split(',')), 200)
        resp = self.client.get(self.URL, {'note_ids': ids})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['counts'][note.id], 1)
