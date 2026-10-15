"""Tests for image attachment endpoints.

GCS is mocked via unittest.mock.patch so no real bucket
credentials are needed.
"""
import io
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, transaction
from django.test.utils import CaptureQueriesContext
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from annotations.models import Comment, Image, Note
from annotations.services.image_storage import (
    FileTooLarge,
    upload_original,
)

User = get_user_model()

# Minimal valid 1x1 PNG (red pixel) produced by Pillow. The bytes
# here include correct IDAT/IEND CRCs so PIL.verify() accepts them
# under strict-verifying Pillow (>=11). Do not hand-edit.
_SMALL_PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde"
    b"\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0\x00\x00\x03\x01\x01\x00"
    b"\xc9\xfe\x92\xef"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _make_file(
    content=_SMALL_PNG,
    name="photo.png",
    content_type="image/png",
):
    """Build an in-memory file object suitable for DRF upload."""
    f = io.BytesIO(content)
    f.name = name
    f.content_type = content_type
    f.size = len(content)
    return f


def _mock_bucket(gs_uri="gs://test-bucket/originals/x/source.png"):
    """Return a context-manager patch for _originals_bucket."""
    bucket = MagicMock()
    bucket.name = "test-bucket"
    blob = MagicMock()
    bucket.blob.return_value = blob
    return patch(
        "annotations.services.image_storage._originals_bucket",
        return_value=bucket,
    )


class ImageModelTest(TestCase):
    """Unit tests for the Image model."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="img_model_user",
            email="img_model@example.com",
            password="pass",
        )
        self.note = Note.objects.create(
            user=self.user,
            note_text="Note for image tests",
        )
        self.comment = Comment.objects.create(
            user=self.user,
            note=self.note,
            content="Comment for image tests",
        )

    def test_id_has_img_prefix(self):
        """Image PK must start with 'IMG_'."""
        image = Image.objects.create(
            note=self.note,
            uploaded_by=self.user,
            storage_url="gs://b/originals/x/source.png",
        )
        self.assertTrue(
            image.id.startswith("IMG_"),
            f"Expected IMG_ prefix, got: {image.id}",
        )

    def test_id_max_length(self):
        """Image PK must be at most 18 characters."""
        image = Image.objects.create(
            note=self.note,
            uploaded_by=self.user,
            storage_url="gs://b/originals/x/source.png",
        )
        self.assertLessEqual(len(image.id), 18)

    def test_str_includes_parent_note(self):
        image = Image.objects.create(
            note=self.note,
            uploaded_by=self.user,
            storage_url="gs://b/originals/x/source.png",
        )
        self.assertIn("note", str(image))

    def test_str_includes_parent_comment(self):
        image = Image.objects.create(
            comment=self.comment,
            uploaded_by=self.user,
            storage_url=(
                "gs://b/originals/x/source.png"
            ),
        )
        self.assertIn("comment", str(image))

    def test_xor_constraint_rejects_both_parents(self):
        """CheckConstraint must reject an Image with both
        a note and a comment set."""
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Image.objects.create(
                    note=self.note,
                    comment=self.comment,
                    uploaded_by=self.user,
                    storage_url=(
                        "gs://b/originals/x/source.png"
                    ),
                )

    def test_xor_constraint_rejects_no_parents(self):
        """CheckConstraint must reject an Image with neither
        a note nor a comment set."""
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Image.objects.create(
                    uploaded_by=self.user,
                    storage_url=(
                        "gs://b/originals/x/source.png"
                    ),
                )


class UploadOriginalServiceTest(TestCase):
    """Unit tests for the upload_original service function."""

    def setUp(self):
        self._override = override_settings(
            IMAGE_MAX_BYTES=10 * 1024 * 1024,
            IMAGE_BUCKET_ORIGINALS="test-bucket",
        )
        self._override.enable()

    def tearDown(self):
        self._override.disable()

    def test_happy_path_returns_tuple(self):
        with _mock_bucket():
            gs_uri, size, ct = upload_original(
                "IMG_TESTID0000001",
                _make_file(),
            )
        self.assertTrue(gs_uri.startswith("gs://"))
        self.assertGreater(size, 0)
        self.assertEqual(ct, "image/png")

    def test_disallowed_content_type_raises_415(self):
        from rest_framework.exceptions import (
            UnsupportedMediaType,
        )
        bad_file = _make_file(
            content=b"fake",
            name="virus.exe",
            content_type="application/octet-stream",
        )
        with _mock_bucket():
            with self.assertRaises(UnsupportedMediaType):
                upload_original("IMG_X", bad_file)

    def test_oversized_file_raises_413(self):
        big = io.BytesIO(b"x" * (10 * 1024 * 1024 + 1))
        big.name = "big.png"
        big.content_type = "image/png"
        with _mock_bucket():
            with self.assertRaises(FileTooLarge):
                upload_original("IMG_X", big)

    def test_object_path_follows_convention(self):
        """GCS object path must be originals/<id>/source<ext>."""
        captured = {}

        def fake_bucket():
            bucket = MagicMock()
            bucket.name = "test-bucket"

            def capture_blob(name):
                captured["name"] = name
                blob = MagicMock()
                return blob

            bucket.blob.side_effect = capture_blob
            return bucket

        with patch(
            "annotations.services.image_storage"
            "._originals_bucket",
            side_effect=fake_bucket,
        ):
            upload_original(
                "IMG_PATHCHECK00001",
                _make_file(),
            )

        self.assertEqual(
            captured.get("name"),
            "originals/IMG_PATHCHECK00001/source.png",
        )

    def test_spoofed_content_type_raises_415(self):
        """Non-image bytes with image/ content-type must fail."""
        from rest_framework.exceptions import (
            UnsupportedMediaType,
        )
        evil = _make_file(
            content=b"not a real image at all",
            name="evil.png",
            content_type="image/png",
        )
        with _mock_bucket():
            with self.assertRaises(UnsupportedMediaType):
                upload_original("IMG_X", evil)


class NoteImageAPITest(TestCase):
    """Integration tests for NoteImageViewSet."""

    def setUp(self):
        self.owner = User.objects.create_user(
            username="note_img_owner",
            email="note_img_owner@example.com",
            password="pass",
        )
        self.other = User.objects.create_user(
            username="note_img_other",
            email="note_img_other@example.com",
            password="pass",
        )
        self.note = Note.objects.create(
            user=self.owner,
            note_text="Note for image API tests",
        )
        self.client = APIClient()

    def _upload_url(self):
        return (
            f"/api/v1/notes/{self.note.id}/images/"
        )

    def test_happy_path_upload_to_note(self):
        self.client.force_authenticate(user=self.owner)
        with _mock_bucket():
            resp = self.client.post(
                self._upload_url(),
                {"file": _make_file()},
                format="multipart",
            )
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertIn("id", resp.data)
        self.assertTrue(
            resp.data["id"].startswith("IMG_")
        )
        self.assertEqual(Image.objects.count(), 1)

    def test_non_owner_upload_returns_403(self):
        self.client.force_authenticate(user=self.other)
        with _mock_bucket():
            resp = self.client.post(
                self._upload_url(),
                {"file": _make_file()},
                format="multipart",
            )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(Image.objects.count(), 0)

    def test_disallowed_content_type_returns_415(self):
        self.client.force_authenticate(user=self.owner)
        bad_file = _make_file(
            content=b"fake",
            name="doc.pdf",
            content_type="application/pdf",
        )
        with _mock_bucket():
            resp = self.client.post(
                self._upload_url(),
                {"file": bad_file},
                format="multipart",
            )
        self.assertEqual(resp.status_code, 415)
        self.assertEqual(Image.objects.count(), 0)

    def test_oversized_file_returns_413(self):
        self.client.force_authenticate(user=self.owner)
        big = io.BytesIO(b"x" * (10 * 1024 * 1024 + 1))
        big.name = "big.png"
        big.content_type = "image/png"
        with _mock_bucket():
            resp = self.client.post(
                self._upload_url(),
                {"file": big},
                format="multipart",
            )
        self.assertEqual(resp.status_code, 413)
        self.assertEqual(Image.objects.count(), 0)

    def test_list_returns_images_for_accessible_note(self):
        Image.objects.create(
            note=self.note,
            uploaded_by=self.owner,
            storage_url="gs://b/originals/x/source.png",
        )
        self.client.force_authenticate(user=self.owner)
        resp = self.client.get(self._upload_url())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data), 1)

    def test_list_other_user_private_note_returns_403(self):
        self.client.force_authenticate(user=self.other)
        resp = self.client.get(self._upload_url())
        self.assertEqual(resp.status_code, 403)

    def test_unauthenticated_list_private_note_returns_403(self):
        resp = self.client.get(self._upload_url())
        self.assertEqual(resp.status_code, 403)

    def test_unauthenticated_list_public_note_returns_200(self):
        """Anon may read images on public notes (plan scope)."""
        self.note.public = True
        self.note.save(update_fields=['public'])
        Image.objects.create(
            note=self.note,
            uploaded_by=self.owner,
            storage_url="gs://b/originals/x/source.png",
        )
        resp = self.client.get(self._upload_url())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data), 1)

    def test_unauthenticated_upload_returns_403(self):
        with _mock_bucket():
            resp = self.client.post(
                self._upload_url(),
                {"file": _make_file()},
                format="multipart",
            )
        self.assertEqual(resp.status_code, 403)

    def test_upload_without_file_returns_400(self):
        self.client.force_authenticate(user=self.owner)
        resp = self.client.post(
            self._upload_url(),
            {},
            format="multipart",
        )
        self.assertEqual(resp.status_code, 400)

    def test_spoofed_content_type_returns_415(self):
        self.client.force_authenticate(user=self.owner)
        evil = _make_file(
            content=b"not a real image at all",
            name="evil.png",
            content_type="image/png",
        )
        with _mock_bucket():
            resp = self.client.post(
                self._upload_url(),
                {"file": evil},
                format="multipart",
            )
        self.assertEqual(resp.status_code, 415)
        self.assertEqual(Image.objects.count(), 0)


class CommentImageAPITest(TestCase):
    """Integration tests for CommentImageViewSet."""

    def setUp(self):
        self.owner = User.objects.create_user(
            username="cmnt_img_owner",
            email="cmnt_img_owner@example.com",
            password="pass",
        )
        self.other = User.objects.create_user(
            username="cmnt_img_other",
            email="cmnt_img_other@example.com",
            password="pass",
        )
        self.note = Note.objects.create(
            user=self.owner,
            note_text="Note for comment image tests",
        )
        self.comment = Comment.objects.create(
            user=self.owner,
            note=self.note,
            content="Comment for image tests",
        )
        self.client = APIClient()

    def _upload_url(self):
        return (
            f"/api/v1/notes/{self.note.id}"
            f"/comments/{self.comment.id}/images/"
        )

    def test_happy_path_upload_to_comment(self):
        self.client.force_authenticate(user=self.owner)
        with _mock_bucket():
            resp = self.client.post(
                self._upload_url(),
                {"file": _make_file()},
                format="multipart",
            )
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertTrue(
            resp.data["id"].startswith("IMG_")
        )
        img = Image.objects.get(pk=resp.data["id"])
        self.assertEqual(img.comment_id, self.comment.id)

    def test_non_author_upload_returns_403(self):
        self.client.force_authenticate(user=self.other)
        with _mock_bucket():
            resp = self.client.post(
                self._upload_url(),
                {"file": _make_file()},
                format="multipart",
            )
        self.assertEqual(resp.status_code, 403)


class ImageDeleteAPITest(TestCase):
    """Integration tests for ImageDestroyView."""

    def setUp(self):
        self.owner = User.objects.create_user(
            username="img_del_owner",
            email="img_del_owner@example.com",
            password="pass",
        )
        self.other = User.objects.create_user(
            username="img_del_other",
            email="img_del_other@example.com",
            password="pass",
        )
        self.note = Note.objects.create(
            user=self.owner,
            note_text="Note for delete tests",
        )
        self.image = Image.objects.create(
            note=self.note,
            uploaded_by=self.owner,
            storage_url=(
                "gs://test-bucket/originals"
                "/IMG_DELTEST00001/source.png"
            ),
        )
        self.client = APIClient()

    def _delete_url(self):
        return f"/api/v1/images/{self.image.id}/"

    def test_unauthenticated_delete_is_rejected(self):
        """Anon DELETE must be rejected and the row preserved.

        DRF returns 401 only when *all* configured authentication
        classes can challenge (i.e. set WWW-Authenticate). This
        project enables SessionAuthentication, which has no
        challenge, so anon requests surface as 403 instead. Either
        status proves the request was rejected before reaching the
        view's permission check; both are acceptable.
        """
        resp = self.client.delete(self._delete_url())
        self.assertIn(resp.status_code, (401, 403))
        self.assertTrue(
            Image.objects.filter(pk=self.image.id).exists()
        )

    def test_non_owner_delete_returns_403(self):
        self.client.force_authenticate(user=self.other)
        resp = self.client.delete(self._delete_url())
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(
            Image.objects.filter(pk=self.image.id).exists()
        )

    def test_owner_delete_removes_row_and_calls_gcs(self):
        self.client.force_authenticate(user=self.owner)
        mock_blob = MagicMock()
        mock_gcs_client = MagicMock()
        mock_gcs_client.bucket.return_value.blob.return_value = (
            mock_blob
        )
        with patch(
            "annotations.services.image_storage._get_client",
            return_value=mock_gcs_client,
        ):
            resp = self.client.delete(self._delete_url())

        self.assertEqual(resp.status_code, 204)
        self.assertFalse(
            Image.objects.filter(pk=self.image.id).exists()
        )
        mock_blob.delete.assert_called_once()

    def _make_comment_image(self):
        """Image attached to a comment by another user.

        Comment-attached images have note=NULL (XOR FK), so the
        parent note is only reachable via image.comment.note.
        """
        comment = Comment.objects.create(
            user=self.other,
            note=self.note,
            content="Comment with attached image",
        )
        return Image.objects.create(
            comment=comment,
            uploaded_by=self.other,
            storage_url=(
                "gs://test-bucket/originals"
                "/IMG_CMNTDEL00001/source.png"
            ),
        )

    def test_note_owner_can_delete_comment_image(self):
        """Note owner may delete an image another user attached
        via a comment on the owner's note (issue #56)."""
        image = self._make_comment_image()
        self.client.force_authenticate(user=self.owner)
        mock_blob = MagicMock()
        mock_gcs_client = MagicMock()
        mock_gcs_client.bucket.return_value.blob.return_value = (
            mock_blob
        )
        with patch(
            "annotations.services.image_storage._get_client",
            return_value=mock_gcs_client,
        ):
            resp = self.client.delete(
                f"/api/v1/images/{image.id}/"
            )

        self.assertEqual(resp.status_code, 204)
        self.assertFalse(
            Image.objects.filter(pk=image.id).exists()
        )
        mock_blob.delete.assert_called_once()

    def test_comment_author_can_delete_comment_image(self):
        """The comment author (uploader) may delete their image."""
        image = self._make_comment_image()
        self.client.force_authenticate(user=self.other)
        mock_blob = MagicMock()
        mock_gcs_client = MagicMock()
        mock_gcs_client.bucket.return_value.blob.return_value = (
            mock_blob
        )
        with patch(
            "annotations.services.image_storage._get_client",
            return_value=mock_gcs_client,
        ):
            resp = self.client.delete(
                f"/api/v1/images/{image.id}/"
            )

        self.assertEqual(resp.status_code, 204)
        self.assertFalse(
            Image.objects.filter(pk=image.id).exists()
        )

    def test_unrelated_user_cannot_delete_comment_image(self):
        """A user unrelated to note and comment gets 403."""
        image = self._make_comment_image()
        third = User.objects.create_user(
            username="img_del_third",
            email="img_del_third@example.com",
            password="pass",
        )
        self.client.force_authenticate(user=third)
        resp = self.client.delete(f"/api/v1/images/{image.id}/")

        self.assertEqual(resp.status_code, 403)
        self.assertTrue(
            Image.objects.filter(pk=image.id).exists()
        )


class CommentInlineImagesTest(TestCase):
    """Tests for images inlined in the comment tree response."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="inline_img_user",
            email="inline_img@example.com",
            password="pass",
        )
        self.note = Note.objects.create(
            user=self.user,
            note_text="Note for inline image tests",
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _comments_url(self):
        return f"/api/v1/notes/{self.note.id}/comments/"

    def _make_comment(self, content="Test comment", parent=None):
        return Comment.objects.create(
            user=self.user,
            note=self.note,
            content=content,
            parent_comment=parent,
        )

    def _make_image(self, comment):
        return Image.objects.create(
            comment=comment,
            uploaded_by=self.user,
            storage_url=(
                "gs://test-bucket/originals/x/source.png"
            ),
            content_type="image/png",
            size_bytes=100,
        )

    @patch(
        "annotations.services.image_storage.signed_image_url",
        return_value="https://signed.example.com/img",
    )
    def test_inline_images_happy_path(self, _mock_sign):
        """Comment with 2 images returns images array of length 2."""
        comment = self._make_comment()
        self._make_image(comment)
        self._make_image(comment)
        resp = self.client.get(self._comments_url())
        self.assertEqual(resp.status_code, 200)
        tree = resp.json()
        self.assertEqual(len(tree), 1)
        images = tree[0]["images"]
        self.assertEqual(len(images), 2)
        for key in (
            "id", "signed_url", "content_type",
            "size_bytes", "created_at",
        ):
            self.assertIn(key, images[0])

    def test_empty_images_array_for_comment_without_attachments(
        self,
    ):
        """Comment without attachments must have images: []."""
        self._make_comment()
        resp = self.client.get(self._comments_url())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()[0]["images"], [])

    @patch(
        "annotations.services.image_storage.signed_image_url",
        return_value="https://signed.example.com/img",
    )
    def test_nested_replies_have_own_images(self, _mock_sign):
        """Replies include their own images arrays."""
        root = self._make_comment("Root")
        reply = self._make_comment("Reply", parent=root)
        self._make_image(reply)
        resp = self.client.get(self._comments_url())
        self.assertEqual(resp.status_code, 200)
        tree = resp.json()
        self.assertEqual(tree[0]["images"], [])
        self.assertEqual(
            len(tree[0]["replies"][0]["images"]), 1
        )

    def test_soft_deleted_comment_returns_empty_images(self):
        """Soft-deleted comment returns images: [] and [deleted]."""
        comment = self._make_comment()
        self._make_image(comment)
        comment.is_deleted = True
        comment.content = ""
        comment.save(update_fields=["is_deleted", "content"])
        resp = self.client.get(self._comments_url())
        self.assertEqual(resp.status_code, 200)
        node = resp.json()[0]
        self.assertEqual(node["content"], "[deleted]")
        self.assertEqual(node["images"], [])

    def test_query_budget_does_not_grow_with_images(self):
        """Exactly 1 comment query + 1 images prefetch query,
        regardless of how many images are attached. Counted by
        filtering captured SQL to business tables only so that
        session/device-tracking middleware overhead is excluded.
        """
        comment = self._make_comment()
        for _ in range(3):
            self._make_image(comment)
        with CaptureQueriesContext(connection) as ctx:
            resp = self.client.get(self._comments_url())
        self.assertEqual(resp.status_code, 200)
        business = [
            q for q in ctx.captured_queries
            if '"annotations_comment"' in q['sql']
            or '"annotations_image"' in q['sql']
        ]
        self.assertEqual(len(business), 2)

    def test_write_path_ignores_images_key(self):
        """POST with an 'images' key in payload is silently ignored;
        the images field is read-only.
        """
        resp = self.client.post(
            self._comments_url(),
            {
                "content": "Test",
                "images": [{"id": "IMG_FAKE"}],
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["images"], [])


class CommentImageNestedScopingTest(TestCase):
    """Nested-route scoping for CommentImageViewSet.

    ``_get_comment`` filters Comment by BOTH ``comment_pk`` and
    the URL's ``note_pk`` (views.py:1688-1693), so a comment
    addressed under the wrong note must 404 even when that note
    is accessible to the requester.
    """

    def setUp(self):
        self.owner = User.objects.create_user(
            username="cimg_scope_owner",
            email="cimg_scope_owner@example.com",
            password="pass",
        )
        self.note_a = Note.objects.create(
            user=self.owner,
            note_text="Note A (holds no comments)",
        )
        self.note_b = Note.objects.create(
            user=self.owner,
            note_text="Note B (holds the comment)",
        )
        self.comment = Comment.objects.create(
            user=self.owner,
            note=self.note_b,
            content="Comment living under note B",
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)

    def _images_url(self, note_pk, comment_pk):
        return (
            f"/api/v1/notes/{note_pk}"
            f"/comments/{comment_pk}/images/"
        )

    def test_comment_image_routes_wrong_note_404(self):
        """Comment C lives under note B; addressing it via the
        accessible note A must 404 on both GET and POST — the
        note_pk in the URL does not match comment.note."""
        url = self._images_url(self.note_a.id, self.comment.id)
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 404)
        # _get_comment 404s before any GCS call (views.py:1688-
        # 1693 vs :1735), so no bucket mock is needed here.
        resp = self.client.post(
            url,
            {"file": _make_file()},
            format="multipart",
        )
        self.assertEqual(resp.status_code, 404)

    def test_comment_images_nonexistent_comment_404(self):
        """A comment pk that does not exist under the note
        404s on the nested images route."""
        url = self._images_url(
            self.note_a.id, "CMNT_DOESNOTEXIST0"
        )
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 404)

    @patch(
        "annotations.services.image_storage.signed_image_url",
        return_value="https://signed.example.com/img",
    )
    def test_comment_image_list(self, _mock_sign):
        """GET nested images for a comment with 2 images → 200
        with 2 items carrying the ImageSerializer keys."""
        for i in (1, 2):
            Image.objects.create(
                comment=self.comment,
                uploaded_by=self.owner,
                storage_url=(
                    f"gs://b/originals/x{i}/source.png"
                ),
                content_type="image/png",
                size_bytes=100 + i,
            )
        resp = self.client.get(
            self._images_url(self.note_b.id, self.comment.id)
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(len(data), 2)
        for item in data:
            for key in (
                "id", "storage_url", "signed_url",
                "content_type", "size_bytes", "created_at",
            ):
                self.assertIn(key, item)
            self.assertTrue(item["id"].startswith("IMG_"))
            self.assertEqual(
                item["signed_url"],
                "https://signed.example.com/img",
            )


class GuestImageWriteTest(TestCase):
    """The shared 'guest' account is read-only: writes → 403."""

    def setUp(self):
        self.guest = User.objects.create_user(
            username="guest",
            email="guest@example.com",
            password="pass",
        )
        self.owner = User.objects.create_user(
            username="guest_img_owner",
            email="guest_img_owner@example.com",
            password="pass",
        )
        self.note = Note.objects.create(
            user=self.owner,
            note_text="Note for guest image tests",
        )
        self.comment = Comment.objects.create(
            user=self.owner,
            note=self.note,
            content="Comment for guest image tests",
        )
        self.image = Image.objects.create(
            note=self.note,
            uploaded_by=self.owner,
            storage_url="gs://b/originals/x/source.png",
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.guest)

    def test_guest_comment_image_upload_403(self):
        url = (
            f"/api/v1/notes/{self.note.id}"
            f"/comments/{self.comment.id}/images/"
        )
        with _mock_bucket():
            resp = self.client.post(
                url,
                {"file": _make_file()},
                format="multipart",
            )
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(
            Image.objects.filter(
                comment=self.comment
            ).exists()
        )

    def test_guest_image_destroy_403(self):
        resp = self.client.delete(
            f"/api/v1/images/{self.image.id}/"
        )
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(
            Image.objects.filter(pk=self.image.id).exists()
        )


class NoteImageEdgeCasesTest(TestCase):
    """404/403 oracles, upload cap, response shape, GCS orphan."""

    def setUp(self):
        self.owner = User.objects.create_user(
            username="nimg_edge_owner",
            email="nimg_edge_owner@example.com",
            password="pass",
        )
        self.other = User.objects.create_user(
            username="nimg_edge_other",
            email="nimg_edge_other@example.com",
            password="pass",
        )
        self.note = Note.objects.create(
            user=self.owner,
            note_text="Private note for edge tests",
        )
        self.client = APIClient()

    def _images_url(self, note_pk):
        return f"/api/v1/notes/{note_pk}/images/"

    def test_note_images_nonexistent_note_404(self):
        """GET /notes/<missing>/images/ → 404 (_get_note)."""
        self.client.force_authenticate(user=self.owner)
        resp = self.client.get(
            self._images_url("NOTDOESNOTEXIST01")
        )
        self.assertEqual(resp.status_code, 404)

    def test_image_destroy_nonexistent_404(self):
        """DELETE /images/<missing>/ → 404."""
        self.client.force_authenticate(user=self.owner)
        resp = self.client.delete(
            "/api/v1/images/IMG_DOESNOTEXIST/"
        )
        self.assertEqual(resp.status_code, 404)

    def test_upload_to_foreign_public_note_403(self):
        """Public notes are readable by everyone, but only the
        owner may upload: create checks note.user != user
        (views.py:1608-1611)."""
        self.note.public = True
        self.note.save(update_fields=["public"])
        self.client.force_authenticate(user=self.other)
        with _mock_bucket():
            resp = self.client.post(
                self._images_url(self.note.id),
                {"file": _make_file()},
                format="multipart",
            )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(Image.objects.count(), 0)

    def test_note_images_list_private_foreign_403(self):
        """PIN: GET images on another user's PRIVATE note → 403.

        FIXME: existence oracle — NoteImageViewSet._get_note is
        unscoped (views.py:1587-1590), so list() reaches the
        accessibility check and raises PermissionDenied (403),
        revealing the note exists. CommentViewSet._get_note
        scopes to get_accessible_notes_qs and 404s instead
        (views.py:1344-1353). Flagged for product decision.
        """
        self.client.force_authenticate(user=self.other)
        resp = self.client.get(self._images_url(self.note.id))
        self.assertEqual(resp.status_code, 403)
        # A truly nonexistent note 404s — the 403 above leaks
        # that THIS note exists.
        resp = self.client.get(
            self._images_url("NOTDOESNOTEXIST01")
        )
        self.assertEqual(resp.status_code, 404)

    @override_settings(IMAGE_MAX_PER_NOTE=2)
    def test_note_image_cap_and_locked_recheck(self):
        """At IMAGE_MAX_PER_NOTE images the next POST → 400 with
        a 'file' error (cap lookup views.py:1615-1617). The
        locked re-check inside the transaction (:1640-1647)
        shares this assertion — bypassing it needs real
        concurrency and is not testable here."""
        for i in (1, 2):
            Image.objects.create(
                note=self.note,
                uploaded_by=self.owner,
                storage_url=(
                    f"gs://b/originals/c{i}/source.png"
                ),
            )
        self.client.force_authenticate(user=self.owner)
        with _mock_bucket():
            resp = self.client.post(
                self._images_url(self.note.id),
                {"file": _make_file()},
                format="multipart",
            )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("file", resp.data)
        self.assertEqual(Image.objects.count(), 2)

    @patch(
        "annotations.services.image_storage.signed_image_url",
        return_value="https://signed.example.com/img",
    )
    def test_create_response_shape(self, _mock_sign):
        """201 body pins the ImageSerializer contract."""
        self.client.force_authenticate(user=self.owner)
        with _mock_bucket():
            resp = self.client.post(
                self._images_url(self.note.id),
                {"file": _make_file()},
                format="multipart",
            )
        self.assertEqual(resp.status_code, 201, resp.data)
        data = resp.data
        self.assertEqual(
            set(data),
            {
                "id", "storage_url", "signed_url",
                "content_type", "size_bytes",
                "comment", "note", "uploaded_by",
                "created_at",
            },
        )
        self.assertTrue(data["id"].startswith("IMG_"))
        self.assertTrue(
            data["storage_url"].startswith("gs://")
        )
        self.assertEqual(
            data["signed_url"],
            "https://signed.example.com/img",
        )
        self.assertEqual(data["content_type"], "image/png")
        self.assertEqual(data["size_bytes"], len(_SMALL_PNG))
        self.assertEqual(data["note"], self.note.id)
        self.assertIsNone(data["comment"])
        self.assertIsNotNone(data["created_at"])

    def test_cascade_delete_orphans_gcs_blob(self):
        """PIN: deleting a note removes its Image rows via
        row-level CASCADE but never calls delete_original —
        the GCS blob is orphaned.

        FIXME: CODE bug — no post_delete receiver exists for
        Image (models.py:303-318 CASCADE FKs); the only
        delete_original callers are the view rollback and
        destroy paths (views.py:1657, 1763, 1817). Flagged for
        product decision; do not fix here. The pin patches
        delete_original at both seams — the views alias and
        its image_storage origin — so a signal-based fix
        trips it too.
        """
        self.client.force_authenticate(user=self.owner)
        with _mock_bucket():
            resp = self.client.post(
                self._images_url(self.note.id),
                {"file": _make_file()},
                format="multipart",
            )
        self.assertEqual(resp.status_code, 201, resp.data)
        image_id = resp.data["id"]

        with patch(
            "annotations.views.delete_original"
        ) as mock_view_delete, patch(
            "annotations.services.image_storage.delete_original"
        ) as mock_svc_delete:
            self.note.delete()
        mock_view_delete.assert_not_called()
        mock_svc_delete.assert_not_called()
        self.assertFalse(
            Image.objects.filter(pk=image_id).exists()
        )
