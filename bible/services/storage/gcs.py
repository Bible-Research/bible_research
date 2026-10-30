"""GCS helpers shared between the audio-generator job (writer) and the
Bible API (reader). Auth via ADC; no JSON keys."""
from __future__ import annotations

import datetime
import json
import logging
import threading
import time
from collections import OrderedDict
from io import BytesIO
from typing import Optional, Set, Tuple

import google.auth
from google.api_core import exceptions as _gax
from google.auth.transport.requests import Request
from google.cloud import storage
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

_GCS_UPLOAD_CONFLICT = (_gax.PreconditionFailed, _gax.Conflict)


def _utc_naive_now() -> datetime.datetime:
    """UTC 'now' as naive datetime (matches historical lock JSON
    timestamps)."""
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)

_client: Optional[storage.Client] = None
_client_lock = threading.Lock()


def get_default_client() -> storage.Client:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = storage.Client()
    return _client


# GCS object prefix per generation mode. ``verse`` keeps the
# historical ``audio/`` prefix so existing objects stay valid;
# ``chapter`` writes whole-chapter-synthesized artifacts under a
# parallel prefix so the two modes coexist without collisions.
# ``gemini`` holds artifacts synthesized by the Gemini free-tier
# pipeline (generate_gemini_audio --upload-gcs); nothing reads them
# yet — a future audio fileset id can map to mode="gemini" via
# sword.registry.audio_mode_for_fileset.
_MODE_PREFIXES = {
    "verse": "audio",
    "chapter": "audio_chapter",
    "gemini": "audio_gemini",
}


def _mode_prefix(mode: str) -> str:
    try:
        return _MODE_PREFIXES[mode]
    except KeyError:
        raise ValueError(f"Unknown audio mode: {mode!r}")


def chapter_object_paths(
    fileset_id: str,
    book_id: str,
    chapter: int,
    voice_name: str,
    mode: str = "verse",
) -> Tuple[str, str]:
    """Return ``(audio_path, timestamps_path)`` for a chapter."""
    base = (
        f"{_mode_prefix(mode)}/{fileset_id}/{voice_name}"
        f"/{book_id}/{int(chapter)}"
    )
    return f"{base}_audio.mp3", f"{base}_timestamps.json"


def _bucket():
    return get_default_client().bucket(settings.AUDIO_BUCKET_NAME)


def chapter_audio_exists(
    fileset_id: str,
    book_id: str,
    chapter: int,
    voice_name: str,
    mode: str = "verse",
) -> bool:
    audio_path, _ = chapter_object_paths(
        fileset_id, book_id, chapter, voice_name, mode=mode,
    )
    return _bucket().blob(audio_path).exists()


def list_completed_chapters(
    fileset_id: str, voice_name: str, mode: str = "verse",
) -> Set[Tuple[str, int]]:
    """Return the set of ``(book_id, chapter)`` pairs that have **both**
    ``_audio.mp3`` and ``_timestamps.json`` present under the mode's
    prefix."""
    prefix = f"{_mode_prefix(mode)}/{fileset_id}/{voice_name}/"
    audio: Set[Tuple[str, int]] = set()
    json_done: Set[Tuple[str, int]] = set()

    for blob in _bucket().list_blobs(prefix=prefix):
        # name = <prefix>/<fileset>/<voice>/<BOOK>/<CH>_audio.mp3
        rest = blob.name[len(prefix):]
        try:
            book_id, fname = rest.split("/", 1)
        except ValueError:
            continue
        if fname.endswith("_audio.mp3"):
            try:
                chap = int(fname[: -len("_audio.mp3")])
            except ValueError:
                continue
            audio.add((book_id, chap))
        elif fname.endswith("_timestamps.json"):
            try:
                chap = int(fname[: -len("_timestamps.json")])
            except ValueError:
                continue
            json_done.add((book_id, chap))

    return audio & json_done


def upload_chapter_artifacts(
    fileset_id: str,
    book_id: str,
    chapter: int,
    mp3_bytes: bytes,
    timestamps_payload: dict,
    voice_name: str,
    mode: str = "verse",
) -> None:
    """Write timestamps JSON first, then MP3, so an MP3 always implies a
    JSON exists (resume logic depends on this)."""
    audio_path, json_path = chapter_object_paths(
        fileset_id, book_id, chapter, voice_name, mode=mode,
    )
    bucket = _bucket()

    # Embed the MP3 size at generation time so the read path can
    # return it without a separate blob.metadata HEAD request.
    enriched_payload = dict(timestamps_payload)
    enriched_payload.setdefault("file_size_bytes", len(mp3_bytes))

    json_blob = bucket.blob(json_path)
    json_blob.upload_from_string(
        json.dumps(enriched_payload, ensure_ascii=False),
        content_type="application/json",
    )

    audio_blob = bucket.blob(audio_path)
    audio_blob.upload_from_file(
        BytesIO(mp3_bytes), content_type="audio/mpeg"
    )

    # Overwriting an existing artifact (re-generation) should
    # drop any cached download from read_timestamps_json. Note
    # this only reaches same-process readers: the cache is
    # process-local locmem and the sole caller is the Cloud Run
    # generation job, a different process/host from the App
    # Engine service — so a re-generated chapter can keep
    # serving the stale entry until the TTL expires or the
    # service restarts.
    cache.delete(
        _timestamps_cache_key(
            fileset_id, book_id, chapter, voice_name, mode,
        )
    )

    logger.info(
        "Uploaded chapter artifacts: gs://%s/%s",
        bucket.name, audio_path,
    )


# The ``_timestamps.json`` artifact is nominally immutable once
# generated: the monthly job only writes chapters that lack
# artifacts (see ``list_completed_chapters``). On the rare
# overwrite ``upload_chapter_artifacts`` deletes the locmem
# entry in *its own* process only — the writer runs in the
# Cloud Run job, not the App Engine serving process, so a
# re-generated chapter may serve stale JSON until the TTL
# below expires (or the service restarts). Caching removes a
# GCS download per timestamps request — and per passage-audio
# request, which reads the same object (S7).
TIMESTAMPS_CACHE_TTL_SECONDS = 24 * 60 * 60


def _timestamps_cache_key(
    fileset_id: str, book_id: str, chapter: int,
    voice_name: str, mode: str,
) -> str:
    """Django-cache key covering every input that selects the
    artifact — ``mode`` picks the ``audio/`` vs ``audio_chapter/``
    prefix and ``voice_name`` the generation voice."""
    return (
        f"v1:timestamps:{fileset_id}:{voice_name}"
        f":{book_id}:{int(chapter)}:{mode}"
    )


def read_timestamps_json(
    fileset_id: str, book_id: str, chapter: int,
    voice_name: str,
    mode: str = "verse",
) -> dict:
    cache_key = _timestamps_cache_key(
        fileset_id, book_id, chapter, voice_name, mode,
    )
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    _, json_path = chapter_object_paths(
        fileset_id, book_id, chapter, voice_name, mode=mode,
    )
    blob = _bucket().blob(json_path)
    payload = json.loads(blob.download_as_bytes())
    # Only successful reads are cached — a ``NotFound`` miss
    # ("audio exists but timestamps not generated yet") must
    # become visible as soon as the artifact is uploaded.
    cache.set(
        cache_key, payload, TIMESTAMPS_CACHE_TTL_SECONDS,
    )
    return payload


# Bounded LRU cache of live signed URLs keyed by ``(fileset_id,
# voice_name, book_id, chapter, mode)``. Each value is ``(url,
# expires_at_monotonic)``. Signing a URL does a round-trip to
# IAM's signBlob API, which is measurable latency and
# rate-limited; caching for slightly less than the TTL means
# clients that hit the same chapter repeatedly pay that cost at
# most once per TTL window per process. The bound mirrors
# ``annotations/services/image_storage.py``: evict the oldest
# entry when full so memory cannot grow without bound on
# long-running workers (S7).
_SIGNED_URL_SAFETY_MARGIN_SECONDS = 60
_SIGNED_URL_CACHE_MAX = 2048
_signed_url_cache: OrderedDict = OrderedDict()
_signed_url_cache_lock = threading.Lock()


def signed_audio_url(
    fileset_id: str,
    book_id: str,
    chapter: int,
    voice_name: str,
    ttl_seconds: Optional[int] = None,
    mode: str = "verse",
) -> str:
    """Return a V4 signed URL for the chapter's MP3.

    Works without a JSON key on App Engine / Cloud Run by passing the
    runtime SA's email + an OAuth access token; GCS asks IAM to sign.

    The URL is cached per ``(fileset_id, voice_name, book_id, chapter,
    mode)`` until ``_SIGNED_URL_SAFETY_MARGIN_SECONDS`` before its real
    expiry so back-to-back requests for the same chapter don't each
    trigger an IAM signBlob call."""
    if ttl_seconds is None:
        ttl_seconds = settings.AUDIO_SIGNED_URL_TTL_SECONDS

    cache_key = (fileset_id, voice_name, book_id, chapter, mode)
    now = time.monotonic()
    with _signed_url_cache_lock:
        cached = _signed_url_cache.get(cache_key)
        if cached is not None and cached[1] > now:
            # Mark as most-recently-used.
            _signed_url_cache.move_to_end(cache_key)
            return cached[0]

    audio_path, _ = chapter_object_paths(
        fileset_id, book_id, chapter, voice_name, mode=mode,
    )
    blob = _bucket().blob(audio_path)

    credentials, _ = google.auth.default(
        scopes=[
            "https://www.googleapis.com/auth/devstorage.read_only",
            "https://www.googleapis.com/auth/iam",
        ]
    )
    credentials.refresh(Request())

    url = blob.generate_signed_url(
        version="v4",
        expiration=datetime.timedelta(seconds=ttl_seconds),
        method="GET",
        service_account_email=credentials.service_account_email,
        access_token=credentials.token,
    )
    expires_at = now + max(
        ttl_seconds - _SIGNED_URL_SAFETY_MARGIN_SECONDS, 0
    )
    with _signed_url_cache_lock:
        _signed_url_cache[cache_key] = (url, expires_at)
        _signed_url_cache.move_to_end(cache_key)
        while len(_signed_url_cache) > _SIGNED_URL_CACHE_MAX:
            _signed_url_cache.popitem(last=False)
    return url


def get_current_year_month() -> str:
    """Return ``YYYY-MM`` for the current UTC time."""
    return _utc_naive_now().strftime("%Y-%m")


def _state_object_path(
    kind: str, fileset_id: str, ym: str, mode: str | None = None,
) -> str:
    """Path of a per-month run-state object.

    ``mode=None`` keeps the shared (pre-modes) layout
    ``state/{kind}/{fileset}/{ym}.json``; a mode adds a level:
    ``state/{kind}/{fileset}/{mode}/{ym}.json``.

    Locks are per-mode so a completed verse-mode run must not block a
    chapter-mode run. Usage counters stay shared (``mode=None``):
    ``MONTHLY_TTS_CHAR_LIMIT`` is a spend ceiling on the whole fileset,
    not per artifact set, so both modes draw from the same counter —
    a combined verse+chapter run can never exceed 1x the cap."""
    if mode is None:
        return f"state/{kind}/{fileset_id}/{ym}.json"
    return f"state/{kind}/{fileset_id}/{mode}/{ym}.json"


def read_monthly_usage(fileset_id: str, ym: str | None = None) -> int:
    ym = ym or get_current_year_month()
    blob = _bucket().blob(_state_object_path("usage", fileset_id, ym))
    if not blob.exists():
        return 0
    try:
        return int(json.loads(blob.download_as_bytes()).get("chars_used", 0))
    except Exception:
        logger.exception("Failed to parse usage object %s", blob.name)
        return 0


_USAGE_MAX_CONFLICT_RETRIES = 8
_USAGE_RETRY_BASE_SLEEP_SECONDS = 0.1


class UsageIncrementConflict(Exception):
    """Raised when ``increment_monthly_usage`` cannot win the
    read-modify-write race within ``_USAGE_MAX_CONFLICT_RETRIES``
    attempts. The caller must decide whether to abort the run — in
    practice we expect zero conflicts because the run lock serializes
    writers, so repeated failures almost always indicate a real problem
    (IAM misconfig, another process running, etc.)."""


def increment_monthly_usage(
    fileset_id: str,
    delta: int,
    ym: str | None = None,
    max_retries: int = _USAGE_MAX_CONFLICT_RETRIES,
    _sleep=None,
) -> int:
    """Atomic-ish increment using ``if_generation_match``. Returns new total.

    Retries the read-modify-write loop on precondition conflicts with
    exponential backoff, but is bounded by ``max_retries`` so an
    IAM/config regression cannot spin the Cloud Run Job until its 3600s
    timeout fires. In normal operation the run lock guarantees a single
    writer, so conflicts should be 0."""
    sleep = _sleep or time.sleep
    ym = ym or get_current_year_month()
    path = _state_object_path("usage", fileset_id, ym)
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        blob = _bucket().blob(path)
        if blob.exists():
            blob.reload()
            current = int(
                json.loads(blob.download_as_bytes()).get("chars_used", 0)
            )
            new_total = current + delta
            payload = json.dumps({
                "chars_used": new_total,
                "updated_at": _utc_naive_now().isoformat(),
            })
            try:
                blob.upload_from_string(
                    payload,
                    content_type="application/json",
                    if_generation_match=blob.generation,
                )
                return new_total
            except _GCS_UPLOAD_CONFLICT as exc:
                last_exc = exc
        else:
            payload = json.dumps({
                "chars_used": delta,
                "updated_at": _utc_naive_now().isoformat(),
            })
            try:
                blob.upload_from_string(
                    payload,
                    content_type="application/json",
                    if_generation_match=0,
                )
                return delta
            except _GCS_UPLOAD_CONFLICT as exc:
                last_exc = exc
        if attempt < max_retries:
            sleep(_USAGE_RETRY_BASE_SLEEP_SECONDS * (2 ** attempt))

    raise UsageIncrementConflict(
        f"increment_monthly_usage for {fileset_id} {ym} failed after "
        f"{max_retries + 1} attempts: {last_exc}"
    )


def acquire_run_lock(
    fileset_id: str,
    stale_after_hours: int,
    ym: str | None = None,
    mode: str = "verse",
) -> Tuple[bool, str]:
    """Try to acquire the once-per-month run lock for ``mode``.

    Returns ``(acquired, reason)``. ``reason`` is one of:
      - ``"acquired"`` — fresh lock created; caller may proceed.
      - ``"stale_overridden"`` — previous lock was older than
        ``stale_after_hours`` and we replaced it; caller may proceed.
      - ``"already_completed"`` — this month's run already finished.
        Caller must exit without doing work.
      - ``"active_run"`` — another run is in progress and is fresh.
        Caller must exit without doing work."""
    ym = ym or get_current_year_month()
    path = _state_object_path("lock", fileset_id, ym, mode=mode)
    blob = _bucket().blob(path)
    now = _utc_naive_now()

    if blob.exists():
        blob.reload()
        existing = json.loads(blob.download_as_bytes())
        if existing.get("status") == "completed":
            return False, "already_completed"
        # ``fromisoformat`` returns an aware datetime iff the string
        # carries a tz offset. Historical lock objects stored naive
        # UTC (see ``_utc_naive_now``) but a future writer, a manual
        # edit, or a different runtime could persist an aware value.
        # Normalize to naive UTC before arithmetic; otherwise subtracting
        # from ``now`` (naive) would raise TypeError and crash the job.
        try:
            started_at = datetime.datetime.fromisoformat(
                existing["started_at"]
            )
            if started_at.tzinfo is not None:
                started_at = started_at.astimezone(
                    datetime.timezone.utc
                ).replace(tzinfo=None)
        except Exception:
            started_at = now  # malformed -> treat as fresh
        age = now - started_at
        if age < datetime.timedelta(hours=stale_after_hours):
            return False, "active_run"
        payload = json.dumps({
            "status": "running",
            "started_at": now.isoformat(),
            "overrode_stale": existing.get("started_at"),
        })
        try:
            blob.upload_from_string(
                payload,
                content_type="application/json",
                if_generation_match=blob.generation,
            )
            return True, "stale_overridden"
        except _GCS_UPLOAD_CONFLICT:
            return False, "active_run"

    payload = json.dumps({
        "status": "running",
        "started_at": now.isoformat(),
    })
    try:
        blob.upload_from_string(
            payload,
            content_type="application/json",
            if_generation_match=0,
        )
        return True, "acquired"
    except _GCS_UPLOAD_CONFLICT:
        return False, "active_run"


def mark_run_complete(
    fileset_id: str,
    chars_used: int,
    chapters_generated: int,
    reason: str,
    ym: str | None = None,
    mode: str = "verse",
) -> None:
    """Mark the current month's lock for ``mode`` as completed.
    Idempotent."""
    ym = ym or get_current_year_month()
    path = _state_object_path("lock", fileset_id, ym, mode=mode)
    blob = _bucket().blob(path)
    if not blob.exists():
        return
    blob.reload()
    existing = json.loads(blob.download_as_bytes())
    existing.update({
        "status": "completed",
        "completed_at": _utc_naive_now().isoformat(),
        "chars_used": chars_used,
        "chapters_generated": chapters_generated,
        "reason": reason,
    })
    try:
        blob.upload_from_string(
            json.dumps(existing),
            content_type="application/json",
            if_generation_match=blob.generation,
        )
    except _GCS_UPLOAD_CONFLICT:
        logger.warning(
            "Lock object changed concurrently; cannot mark complete."
        )


def _gemini_requests_path(
    fileset_id: str, voice_name: str, day: str,
) -> str:
    """Path of the per-day Gemini request counter object.

    ``state/gemini/{fileset}/{voice}/{YYYY-MM-DD}.json`` — keyed by
    the caller-provided day string (the command passes the
    America/Los_Angeles date, matching the free-tier quota reset at
    midnight Pacific). A new day means a new object, so a stale
    counter can never block a fresh day's run; leftover objects are
    a few hundred bytes each."""
    return f"state/gemini/{fileset_id}/{voice_name}/{day}.json"


def read_gemini_daily_requests(
    fileset_id: str, voice_name: str, day: str,
) -> int:
    """Requests already spent on ``day``; 0 when absent/corrupt.

    A corrupt counter degrades to 0 — the run may then overshoot
    the free-tier cap, which the API itself enforces with 429s
    (surfaced as ``QuotaExceeded`` and a clean exit), so this is
    the safe failure direction."""
    blob = _bucket().blob(
        _gemini_requests_path(fileset_id, voice_name, day)
    )
    if not blob.exists():
        return 0
    try:
        return int(
            json.loads(blob.download_as_bytes()).get(
                "requests_today", 0
            )
        )
    except Exception:
        logger.exception(
            "Failed to parse gemini requests object %s", blob.name,
        )
        return 0


def write_gemini_daily_requests(
    fileset_id: str,
    voice_name: str,
    day: str,
    requests_today: int,
) -> None:
    """Persist the day's request spend.

    Plain overwrite, no generation match: the Cloud Scheduler
    triggers one run per day and the day-keyed lock object
    (``acquire_run_lock`` with ``ym=<day>``, ``mode="gemini"``)
    serializes same-day executions, so a lost update requires two
    concurrent writers that cannot normally exist."""
    blob = _bucket().blob(
        _gemini_requests_path(fileset_id, voice_name, day)
    )
    blob.upload_from_string(
        json.dumps({
            "day": day,
            "requests_today": int(requests_today),
            "updated_at": _utc_naive_now().isoformat(),
        }),
        content_type="application/json",
    )
