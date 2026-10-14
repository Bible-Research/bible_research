"""Tests for the `tag_id=untagged` sentinel on note endpoints.

`UNTAGGED_TAG_ID` filters notes whose `tag` FK is null; real tag
ids are `TAG…` strings and cannot collide with the sentinel.
"""
from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from annotations.models import Comment, Note, Tag
from annotations.views import UNTAGGED_TAG_ID


class UntaggedNotesFilterTest(TestCase):
    """GET /api/v1/notes/?tag_id=untagged"""

    URL = '/api/v1/notes/'

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username='untagged_user',
            email='untagged@example.com',
            password='pass',
        )
        self.other_user = User.objects.create_user(
            username='untagged_other',
            email='untagged_other@example.com',
            password='pass',
        )
        self.tag = Tag.objects.create(
            user=self.user, name='Tagged',
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_returns_only_untagged_notes(self):
        tagged = Note.objects.create(
            user=self.user, tag=self.tag, note_text='tagged',
        )
        untagged = Note.objects.create(
            user=self.user, note_text='no tag',
        )
        resp = self.client.get(
            self.URL, {'tag_id': UNTAGGED_TAG_ID}
        )
        self.assertEqual(resp.status_code, 200)
        ids = {n['id'] for n in resp.json()['results']}
        self.assertIn(untagged.id, ids)
        self.assertNotIn(tagged.id, ids)

    def test_excludes_other_users_private_untagged(self):
        foreign = Note.objects.create(
            user=self.other_user, note_text='foreign private',
        )
        resp = self.client.get(
            self.URL, {'tag_id': UNTAGGED_TAG_ID}
        )
        ids = {n['id'] for n in resp.json()['results']}
        self.assertNotIn(foreign.id, ids)

    def test_includes_other_users_public_untagged(self):
        foreign = Note.objects.create(
            user=self.other_user,
            note_text='foreign public',
            public=True,
        )
        resp = self.client.get(
            self.URL, {'tag_id': UNTAGGED_TAG_ID}
        )
        ids = {n['id'] for n in resp.json()['results']}
        self.assertIn(foreign.id, ids)


class UntaggedCommentCountsTest(TestCase):
    """GET /api/v1/comments/counts/?tag_id=untagged"""

    URL = '/api/v1/comments/counts/'

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username='cnt_untagged_user',
            email='cnt_untagged@example.com',
            password='pass',
        )
        self.tag = Tag.objects.create(
            user=self.user, name='Tagged',
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_counts_only_untagged_notes(self):
        tagged = Note.objects.create(
            user=self.user, tag=self.tag, note_text='tagged',
        )
        untagged = Note.objects.create(
            user=self.user, note_text='no tag',
        )
        Comment.objects.create(
            user=self.user, note=tagged, content='on tagged',
        )
        Comment.objects.create(
            user=self.user, note=untagged, content='on untagged',
        )
        resp = self.client.get(
            self.URL, {'tag_id': UNTAGGED_TAG_ID}
        )
        self.assertEqual(resp.status_code, 200)
        counts = resp.json()['counts']
        self.assertIn(untagged.id, counts)
        self.assertNotIn(tagged.id, counts)
