"""GCS helpers for image attachments.

Mirrors bible/services/storage/gcs.py conventions.
Auth via ADC; no JSON keys.
"""
from __future__ import annotations

import datetime
import logging
import os
import threading
import time
from collections import OrderedDict
from typing import Optional, Tuple

import google.auth
from google.auth.transport.requests import Request
from google.cloud import storage
from PIL import Image as PILImage
from django.conf import settings
from rest_framework import status as drf_status
from rest_framework.exceptions import (
    APIException,
    UnsupportedMediaType,
)


class FileTooLarge(APIException):
    status_code = drf_status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
    default_detail = "File exceeds maximum allowed size."
    default_code = "file_too_large"


logger = logging.getLogger(__name__)

_ALLOWED_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

# PIL ``img.format`` → expected declared content_type. Used to
# verify the bytes actually match the client's claim — a PNG
# uploaded as 'image/gif' must not be stored/served as GIF.
_FORMAT_TO_CONTENT_TYPE = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
    "GIF": "image/gif",
}

# Timeout (seconds) for the GCS object upload — an unbounded
# blob.upload_from_file can pin a worker on a stalled socket.
_UPLOAD_TIMEOUT_SECONDS = 60


def _allowed_content_types():
    """Allowed content types: ``_ALLOWED_EXTENSIONS`` narrowed
    by the ``IMAGE_ALLOWED_CONTENT_TYPES`` setting (the setting
    used to be dead code — uploads only consulted the hardcoded
    map). Unknown/deactivated types keep their extension entry
    so the allowlist can be trimmed without a deploy.
    """
    configured = getattr(
        settings, "IMAGE_ALLOWED_CONTENT_TYPES", None
    )
    if configured is None:
        return _ALLOWED_EXTENSIONS
    return {
        ct: ext
        for ct, ext in _ALLOWED_EXTENSIONS.items()
        if ct in configured
    }

_SIGNING_SCOPES = [
    "https://www.googleapis.com/auth/devstorage.read_only",
    "https://www.googleapis.com/auth/iam",
]

# -- Module-level GCS client singleton (avoids per-request init) ---
_client: Optional[storage.Client] = None
_client_lock = threading.Lock()


def _get_client() -> storage.Client:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = storage.Client()
    return _client


def _originals_bucket() -> storage.Bucket:
    return _get_client().bucket(
        settings.IMAGE_BUCKET_ORIGINALS
    )


# -- Credential cache (one token refresh per expiry window) ---------
_credentials = None
_credentials_lock = threading.Lock()


def _get_credentials():
    global _credentials
    if _credentials is not None and _credentials.valid:
        return _credentials
    with _credentials_lock:
        if _credentials is not None and _credentials.valid:
            return _credentials
        creds, _ = google.auth.default(
            scopes=_SIGNING_SCOPES
        )
        creds.refresh(Request())
        _credentials = creds
        return _credentials


# -- Signed URL cache (avoids N+1 refreshes in list responses) -----
# Bounded LRU keyed by storage_url; evicts the oldest entry when full
# so memory cannot grow without bound on long-running workers.
_SIGNED_URL_SAFETY_MARGIN_SECONDS = 60
_SIGNED_URL_CACHE_MAX = 2048
_signed_url_cache: OrderedDict = OrderedDict()
_signed_url_cache_lock = threading.Lock()


def upload_original(
    image_id: str,
    django_uploaded_file,
) -> Tuple[str, int, str]:
    """Upload a Django UploadedFile to GCS originals bucket.

    Returns ``(gs_uri, size_bytes, content_type)``.

    Raises:
        UnsupportedMediaType: content-type not in allowlist or
            file content fails magic-bytes validation.
        FileTooLarge: file exceeds IMAGE_MAX_BYTES.
    """
    content_type = getattr(
        django_uploaded_file, "content_type", ""
    ) or ""
    allowed = _allowed_content_types()
    if content_type not in allowed:
        raise UnsupportedMediaType(
            content_type,
            detail=(
                f"Unsupported image type '{content_type}'. "
                f"Allowed: "
                f"{', '.join(allowed)}."
            ),
        )

    max_bytes = settings.IMAGE_MAX_BYTES
    django_uploaded_file.seek(0, os.SEEK_END)
    size_bytes = django_uploaded_file.tell()
    django_uploaded_file.seek(0)

    if size_bytes > max_bytes:
        raise FileTooLarge(
            f"File too large ({size_bytes} bytes). "
            f"Maximum allowed: {max_bytes} bytes."
        )

    django_uploaded_file.seek(0)
    try:
        img = PILImage.open(django_uploaded_file)
        img.verify()
    except PILImage.DecompressionBombError:
        # Legit format, hostile size — the right status is
        # 413 (payload too large), not 415 (bad media type).
        raise FileTooLarge(
            "Image dimensions exceed the decompression "
            "bomb limit."
        )
    except Exception:
        raise UnsupportedMediaType(
            content_type,
            detail=(
                "File content is not a valid image. "
                "Allowed: "
                f"{', '.join(allowed)}."
            ),
        )
    django_uploaded_file.seek(0)

    # Verify the declared MIME type against the actual image
    # format — the stored/served content_type must describe
    # the real bytes.
    actual = _FORMAT_TO_CONTENT_TYPE.get(img.format or "")
    if actual != content_type:
        raise UnsupportedMediaType(
            content_type,
            detail=(
                f"Content-Type '{content_type}' does not "
                f"match the image bytes "
                f"({img.format or 'unknown'})."
            ),
        )

    ext = allowed[content_type]
    object_name = f"originals/{image_id}/source{ext}"

    bucket = _originals_bucket()
    blob = bucket.blob(object_name)
    blob.upload_from_file(
        django_uploaded_file,
        content_type=content_type,
        timeout=_UPLOAD_TIMEOUT_SECONDS,
    )

    gs_uri = f"gs://{bucket.name}/{object_name}"
    logger.info(
        "Uploaded image original: %s (%d bytes)",
        gs_uri,
        size_bytes,
    )
    return gs_uri, size_bytes, content_type


def signed_image_url(
    image_id: str,
    storage_url: str,
    ttl_seconds: int | None = None,
) -> str | None:
    """Return a V4 signed GET URL for an image original.

    Derives the object path from *storage_url*
    (``gs://<bucket>/<object>``).

    Returns ``None`` on any failure so clients can detect the
    error rather than receiving a raw ``gs://`` URI that
    browsers cannot fetch. Results are cached with a TTL to
    avoid N+1 credential refreshes in list responses.
    """
    if ttl_seconds is None:
        ttl_seconds = settings.IMAGE_SIGNED_URL_TTL_SECONDS

    now = time.monotonic()
    with _signed_url_cache_lock:
        cached = _signed_url_cache.get(storage_url)
        if cached is not None and cached[1] > now:
            # Mark as most-recently-used.
            _signed_url_cache.move_to_end(storage_url)
            return cached[0]

    try:
        without_scheme = storage_url[len("gs://"):]
        bucket_name, object_name = without_scheme.split(
            "/", 1
        )
    except (ValueError, IndexError):
        logger.error(
            "Cannot parse storage_url for image %s: %r",
            image_id,
            storage_url,
        )
        return None

    try:
        blob = _get_client().bucket(bucket_name).blob(
            object_name
        )
        creds = _get_credentials()
        url = blob.generate_signed_url(
            version="v4",
            expiration=datetime.timedelta(
                seconds=ttl_seconds
            ),
            method="GET",
            service_account_email=(
                creds.service_account_email
            ),
            access_token=creds.token,
        )
    except Exception:
        logger.exception(
            "Failed to sign URL for image %s "
            "(storage_url=%r)",
            image_id,
            storage_url,
        )
        return None

    expires_at = now + max(
        ttl_seconds - _SIGNED_URL_SAFETY_MARGIN_SECONDS, 0
    )
    with _signed_url_cache_lock:
        _signed_url_cache[storage_url] = (url, expires_at)
        _signed_url_cache.move_to_end(storage_url)
        while len(_signed_url_cache) > _SIGNED_URL_CACHE_MAX:
            _signed_url_cache.popitem(last=False)
    return url


def delete_original(
    image_id: str, storage_url: str
) -> None:
    """Best-effort delete of the GCS object; 404 is tolerated."""
    try:
        without_scheme = storage_url[len("gs://"):]
        bucket_name, object_name = without_scheme.split(
            "/", 1
        )
        blob = _get_client().bucket(bucket_name).blob(
            object_name
        )
        blob.delete()
        logger.info(
            "Deleted image original for %s: %s",
            image_id,
            storage_url,
        )
    except Exception:
        logger.exception(
            "Failed to delete GCS object for image %s "
            "(storage_url=%r) — tolerated.",
            image_id,
            storage_url,
        )
