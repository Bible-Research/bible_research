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
