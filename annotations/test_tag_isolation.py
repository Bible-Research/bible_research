"""
Cross-user isolation and serializer-contract tests for
/api/v1/tags/.

The user filter in get_queryset is the primary isolation
barrier; perform_update/perform_destroy now also carry explicit
ownership checks mirroring NoteViewSet (defense-in-depth).
These tests pin the barrier plus the TagSerializer response
shape, the parent-cycle guards, and top-level name uniqueness.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from annotations.models import Tag


class TagIsolationTest(TestCase):
    """owner/intruder isolation for tag list/detail endpoints."""

    BASE_URL = '/api/v1/tags/'

    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user(
            username='tag_owner', password='pass',
        )
        self.intruder = User.objects.create_user(
            username='tag_intruder', password='pass',
        )
        self.guest = User.objects.create_user(
            username='guest', password='pass',
        )
        self.owner_tag = Tag.objects.create(
            user=self.owner, name='Owner tag',
        )
        self.intruder_tag = Tag.objects.create(
            user=self.intruder, name='Intruder tag',
        )
        self.guest_tag = Tag.objects.create(
            user=self.guest, name='Guest tag',
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)

    def test_list_excludes_other_users_tags(self):
        """GET list returns only the authenticated user's tags."""
        resp = self.client.get(self.BASE_URL)
        self.assertEqual(resp.status_code, 200)
        names = {item['name'] for item in resp.json()}
        self.assertEqual(names, {'Owner tag'})

    def test_retrieve_foreign_tag_404(self):
        """Detail GET on another user's tag is 404, NOT 403.

        get_queryset scoping makes get_object() raise 404 before
        any permission check could produce 403 — pinned.
        """
        resp = self.client.get(
            f'{self.BASE_URL}{self.intruder_tag.id}/'
        )
        self.assertEqual(resp.status_code, 404)

    def test_update_foreign_tag_404(self):
        """PATCH on another user's tag is 404 — scoping blocks
        the request before perform_update is ever reached."""
        resp = self.client.patch(
            f'{self.BASE_URL}{self.intruder_tag.id}/',
            {'name': 'Hijacked'},
            format='json',
        )
        self.assertEqual(resp.status_code, 404)
        self.intruder_tag.refresh_from_db()
        self.assertEqual(self.intruder_tag.name, 'Intruder tag')

    def test_destroy_foreign_tag_404(self):
        """DELETE on another user's tag is 404 and the tag
        survives — perform_destroy is never reached."""
        resp = self.client.delete(
            f'{self.BASE_URL}{self.intruder_tag.id}/'
        )
        self.assertEqual(resp.status_code, 404)
        self.assertTrue(
            Tag.objects.filter(pk=self.intruder_tag.pk).exists()
        )

    def test_owner_can_update_own_tag(self):
        """PATCH on the owner's own tag succeeds (200)."""
        resp = self.client.patch(
            f'{self.BASE_URL}{self.owner_tag.id}/',
            {'name': 'Renamed tag'},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.owner_tag.refresh_from_db()
        self.assertEqual(self.owner_tag.name, 'Renamed tag')

    def test_owner_can_retrieve_own_tag(self):
        """Detail GET returns 200 with the pinned serializer
        field set; 'user' is a HiddenField and NOT exposed."""
        resp = self.client.get(
            f'{self.BASE_URL}{self.owner_tag.id}/'
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            set(resp.json()),
            {
                'id', 'name', 'parent_tag',
                'created_at', 'updated_at',
            },
        )
        self.assertNotIn('user', resp.json())
        self.assertEqual(resp.json()['name'], 'Owner tag')

    def test_parent_tag_rejects_foreign_tag(self):
        """A foreign tag as parent_tag is rejected with 400 —
        pinned. TagSerializer.__init__ scopes the parent_tag
        queryset to request.user, so the intruder's pk fails
        PrimaryKeyRelatedField validation."""
        resp = self.client.post(
            self.BASE_URL,
            {'name': 'Child', 'parent_tag': self.intruder_tag.id},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn('parent_tag', resp.json())

    def test_duplicate_name_same_user(self):
        """A second top-level tag with the same name is rejected
        with a clean 400.

        unique_together ('user', 'name', 'parent_tag') cannot
        cover NULL parents (UNIQUE treats NULLs as distinct), so
        TagSerializer.validate enforces top-level (user, name)
        uniqueness application-side — backed by the conditional
        ``unique_top_level_tag_name_per_user`` model constraint
        once its migration lands.
        """
        resp = self.client.post(
            self.BASE_URL, {'name': 'Owner tag'}, format='json'
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn('name', resp.json())
        self.assertEqual(
            Tag.objects.filter(
                user=self.owner, name='Owner tag'
            ).count(),
            1,
        )

    def test_duplicate_name_rename_400(self):
        """PATCHing a top-level tag onto an existing name is a
        400 — the same uniqueness rule covers updates."""
        other = Tag.objects.create(
            user=self.owner, name='Other tag',
        )
        resp = self.client.patch(
            f'{self.BASE_URL}{other.id}/',
            {'name': 'Owner tag'},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)
        other.refresh_from_db()
        self.assertEqual(other.name, 'Other tag')

    def test_same_name_under_parent_allowed(self):
        """The top-level rule does not fire under a parent —
        (user, name, parent_tag) unique_together governs."""
        resp = self.client.post(
            self.BASE_URL,
            {'name': 'Owner tag',
             'parent_tag': self.owner_tag.id},
            format='json',
        )
        self.assertEqual(resp.status_code, 201)

    def test_parent_tag_rejects_self(self):
        """PATCH {parent_tag: <self>} is a 400 — the tag cannot
        be its own parent (was silently accepted before)."""
        resp = self.client.patch(
            f'{self.BASE_URL}{self.owner_tag.id}/',
            {'parent_tag': self.owner_tag.id},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn('parent_tag', resp.json())
        self.owner_tag.refresh_from_db()
        self.assertIsNone(self.owner_tag.parent_tag)

    def test_parent_tag_rejects_descendant(self):
        """Moving a tag under its own descendant is a 400 —
        it would silently corrupt the hierarchy."""
        child = Tag.objects.create(
            user=self.owner, name='Child',
            parent_tag=self.owner_tag,
        )
        grandchild = Tag.objects.create(
            user=self.owner, name='Grandchild',
            parent_tag=child,
        )
        resp = self.client.patch(
            f'{self.BASE_URL}{self.owner_tag.id}/',
            {'parent_tag': grandchild.id},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn('parent_tag', resp.json())
        self.owner_tag.refresh_from_db()
        self.assertIsNone(self.owner_tag.parent_tag)

    def test_parent_tag_accepts_own_non_descendant(self):
        """Re-parenting under a tag outside the subtree still
        works (happy path)."""
        sibling = Tag.objects.create(
            user=self.owner, name='Sibling',
        )
        resp = self.client.patch(
            f'{self.BASE_URL}{self.owner_tag.id}/',
            {'parent_tag': sibling.id},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.owner_tag.refresh_from_db()
        self.assertEqual(self.owner_tag.parent_tag, sibling)

    def test_child_tag_under_foreign_parent_via_patch_400(self):
        """PATCH parent_tag to a foreign tag is a 400 — the
        scoped queryset rejects it like the POST path."""
        resp = self.client.patch(
            f'{self.BASE_URL}{self.owner_tag.id}/',
            {'parent_tag': self.intruder_tag.id},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)

    def test_name_too_long_400(self):
        """name longer than the model's max_length=100 -> 400."""
        resp = self.client.post(
            self.BASE_URL, {'name': 'x' * 101}, format='json'
        )
        self.assertEqual(resp.status_code, 400)

    def test_bad_pk_404(self):
        """A nonexistent tag pk on detail GET -> 404."""
        resp = self.client.get(f'{self.BASE_URL}NOPE/')
        self.assertEqual(resp.status_code, 404)

    def test_unauthenticated_list_returns_empty(self):
        """An anonymous request gets 200 with an empty list —
        get_queryset returns no tags for anonymous `list`
        (views.py:169); anonymous `retrieve` is open only for
        tags carrying a public note (:165-168). In prod the
        middleware keeps safe reads on /api/v1/tags truly
        anonymous rather than provisioning a device user."""
        client = APIClient()
        resp = client.get(self.BASE_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [])
