"""
Cross-user isolation and serializer-contract tests for
/api/v1/tags/.

TagViewSet (annotations/views.py:115) has no ownership checks in
perform_update/perform_destroy — the user filter in get_queryset
(views.py:123-163) is the sole isolation barrier. These tests pin
that barrier plus the TagSerializer response shape.
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
        """A second tag with the same name and no parent is
        silently created — pinned 201.

        FIXME: prod bug — unique_together
        ('user', 'name', 'parent_tag') (annotations/models.py:65)
        does NOT protect same-name tags when parent_tag is NULL
        (UNIQUE indexes treat NULLs as distinct) and no
        serializer layer returns a clean 400, so the duplicate
        is created. With a non-null parent the same request does
        get a clean 400 from UniqueTogetherValidator. Flagged
        for a product decision.
        """
        resp = self.client.post(
            self.BASE_URL, {'name': 'Owner tag'}, format='json'
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(
            Tag.objects.filter(
                user=self.owner, name='Owner tag'
            ).count(),
            2,
        )

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
