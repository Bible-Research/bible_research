"""GCS state-helper tests for the STT timestamps job — same
fake-bucket pattern as ``test_gcs_state_helpers``."""
import json

import pytest

from bible.services.storage import gcs
from bible.tests.test_gcs_state_helpers import (  # noqa: F401
    _FakeBucket,
    fake_bucket,
)


def test_stt_usage_round_trip_is_global(fake_bucket):
    """The STT seconds counter is global (STT bills per
    account): the object lives at state/stt-usage/{ym}.json
    with no fileset segment, keyed by ``seconds_used``."""
    ym = gcs.get_current_year_month()
    assert gcs.read_stt_monthly_usage() == 0
    assert gcs.increment_stt_monthly_usage(300) == 300
    assert gcs.increment_stt_monthly_usage(200) == 500
    assert gcs.read_stt_monthly_usage() == 500

    path = f"state/stt-usage/{ym}.json"
    stored = json.loads(fake_bucket._store[path]["data"])
    assert stored["seconds_used"] == 500


def test_stt_lock_acquire_and_complete(fake_bucket):
    ok, reason = gcs.acquire_stt_run_lock(
        "LATLVRN1DA", stale_after_hours=24,
    )
    assert ok and reason == "acquired"

    ok, reason = gcs.acquire_stt_run_lock(
        "LATLVRN1DA", stale_after_hours=24,
    )
    assert not ok and reason == "active_run"

    gcs.mark_stt_run_complete(
        "LATLVRN1DA", seconds_used=500,
        chapters_generated=2, reason="all_done",
    )
    ok, reason = gcs.acquire_stt_run_lock(
        "LATLVRN1DA", stale_after_hours=24,
    )
    assert not ok and reason == "already_completed"

    ym = gcs.get_current_year_month()
    path = f"state/stt-lock/LATLVRN1DA/{ym}.json"
    stored = json.loads(fake_bucket._store[path]["data"])
    assert stored["seconds_used"] == 500
    assert stored["reason"] == "all_done"


def test_stt_locks_are_isolated_per_fileset(fake_bucket):
    """A completed LATLVRN1DA run must not block the other
    registered filesets — the lock is per fileset."""
    gcs.acquire_stt_run_lock("LATLVRN1DA", stale_after_hours=24)
    gcs.mark_stt_run_complete(
        "LATLVRN1DA", seconds_used=10,
        chapters_generated=1, reason="all_done",
    )
    ok, reason = gcs.acquire_stt_run_lock(
        "LATLVRP1DA", stale_after_hours=24,
    )
    assert ok and reason == "acquired"


def test_generated_timestamps_object_path():
    path = gcs.generated_timestamps_object_path(
        "LATLVRN1DA", "JHN", 3,
    )
    assert path == (
        "timestamps_generated/LATLVRN1DA/JHN/3_timestamps.json"
    )
    path = gcs.stt_staging_object_path("LATLVRN1DA", "JHN", 3)
    assert path == "stt_staging/LATLVRN1DA/JHN/3.mp3"


def test_upload_and_read_generated_timestamps(fake_bucket):
    payload = {
        "data": [{"verse_start": 1, "timestamp": 0.0}],
        "meta": {"source": "stt_v1"},
    }
    gcs.upload_generated_timestamps(
        "LATLVRN1DA", "JHN", 3, payload,
    )
    assert gcs.read_generated_timestamps_json(
        "LATLVRN1DA", "JHN", 3,
    ) == payload


def test_list_generated_chapters(fake_bucket):
    for book, chap in (("JHN", 3), ("JHN", 4), ("MAT", 5)):
        path = gcs.generated_timestamps_object_path(
            "LATLVRN1DA", book, chap,
        )
        fake_bucket._store[path] = {"data": b"{}", "gen": 1}
    # Non-artifact objects under the prefix are ignored.
    fake_bucket._store[
        "timestamps_generated/LATLVRN1DA/JHN/notes.txt"
    ] = {"data": b"x", "gen": 1}
    fake_bucket._store[
        "timestamps_generated/LATBSLP1DA/JHN/3_timestamps.json"
    ] = {"data": b"{}", "gen": 1}

    done = gcs.list_generated_chapters("LATLVRN1DA")
    assert done == {("JHN", 3), ("JHN", 4), ("MAT", 5)}


def test_generated_timestamps_read_is_cached(fake_bucket):
    """Like the SWORD timestamps sidecar, the generated artifact
    read is locmem-cached — a second read does not re-download."""
    path = gcs.generated_timestamps_object_path(
        "LATLVRN1DA", "JHN", 3,
    )
    fake_bucket._store[path] = {
        "data": json.dumps({"data": [{"a": 1}]}).encode(),
        "gen": 1,
    }
    first = gcs.read_generated_timestamps_json(
        "LATLVRN1DA", "JHN", 3,
    )
    fake_bucket._store[path]["data"] = b"corrupt"
    second = gcs.read_generated_timestamps_json(
        "LATLVRN1DA", "JHN", 3,
    )
    assert first == second == {"data": [{"a": 1}]}
