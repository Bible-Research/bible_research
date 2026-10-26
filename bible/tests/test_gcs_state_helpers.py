import datetime
import json
from unittest.mock import patch

import pytest

from bible.services.storage import gcs


class _FakeBlob:
    def __init__(self, name, store):
        self.name = name
        self._store = store
        self.generation = 0

    def exists(self):
        return self.name in self._store

    def reload(self):
        self.generation = self._store[self.name]["gen"]

    def download_as_bytes(self):
        return self._store[self.name]["data"]

    def upload_from_string(
        self, data, content_type=None, if_generation_match=None,
    ):
        existing = self._store.get(self.name)
        if if_generation_match == 0 and existing is not None:
            from google.api_core import exceptions as gax
            raise gax.PreconditionFailed("exists")
        if (
            if_generation_match is not None
            and if_generation_match != 0
            and (existing is None or existing["gen"] != if_generation_match)
        ):
            from google.api_core import exceptions as gax
            raise gax.PreconditionFailed("gen mismatch")
        new_gen = (existing["gen"] + 1) if existing else 1
        self._store[self.name] = {
            "data": data.encode() if isinstance(data, str) else data,
            "gen": new_gen,
        }
        self.generation = new_gen

    def upload_from_file(self, file_obj, content_type=None):
        existing = self._store.get(self.name)
        new_gen = (existing["gen"] + 1) if existing else 1
        self._store[self.name] = {
            "data": file_obj.read(),
            "gen": new_gen,
        }
        self.generation = new_gen


class _FakeBucket:
    def __init__(self):
        self._store = {}
        self.name = "fake-bucket"

    def blob(self, name):
        return _FakeBlob(name, self._store)

    def list_blobs(self, prefix=None):
        return [
            type("ListedBlob", (), {"name": name})()
            for name in self._store
            if prefix is None or name.startswith(prefix)
        ]


@pytest.fixture
def fake_bucket():
    bucket = _FakeBucket()
    with patch.object(gcs, "_bucket", return_value=bucket):
        yield bucket


def test_acquire_lock_first_time_succeeds(fake_bucket):
    ok, reason = gcs.acquire_run_lock("LVSGLU8", stale_after_hours=24)
    assert ok and reason == "acquired"


def test_second_acquire_within_stale_window_fails(fake_bucket):
    gcs.acquire_run_lock("LVSGLU8", stale_after_hours=24)
    ok, reason = gcs.acquire_run_lock("LVSGLU8", stale_after_hours=24)
    assert not ok and reason == "active_run"


def test_completed_lock_blocks_reentry(fake_bucket):
    gcs.acquire_run_lock("LVSGLU8", stale_after_hours=24)
    gcs.mark_run_complete(
        "LVSGLU8", chars_used=10, chapters_generated=1, reason="all_done",
    )
    ok, reason = gcs.acquire_run_lock("LVSGLU8", stale_after_hours=24)
    assert not ok and reason == "already_completed"


def test_increment_monthly_usage_round_trip(fake_bucket):
    assert gcs.read_monthly_usage("LVSGLU8") == 0
    assert gcs.increment_monthly_usage("LVSGLU8", 1500) == 1500
    assert gcs.increment_monthly_usage("LVSGLU8", 250) == 1750
    assert gcs.read_monthly_usage("LVSGLU8") == 1750


def test_increment_monthly_usage_bounded_on_persistent_conflict(
    fake_bucket,
):
    """Regression: the retry loop must be bounded so a persistent
    precondition conflict (e.g. IAM regression, rogue writer) cannot
    spin the Cloud Run Job until its timeout fires. After
    ``max_retries + 1`` attempts we must raise
    ``UsageIncrementConflict`` rather than loop forever."""
    from google.api_core import exceptions as gax

    # Seed an existing usage object so we take the read-modify-write
    # branch (which is the branch that could spin forever).
    ym = gcs.get_current_year_month()
    path = f"state/usage/LVSGLU8/{ym}.json"
    fake_bucket._store[path] = {
        "data": json.dumps({"chars_used": 100}).encode(),
        "gen": 1,
    }

    def always_conflict(self, *_, **__):
        raise gax.PreconditionFailed("simulated persistent conflict")

    sleeps: list[float] = []
    original = _FakeBlob.upload_from_string
    _FakeBlob.upload_from_string = always_conflict
    try:
        with pytest.raises(gcs.UsageIncrementConflict):
            gcs.increment_monthly_usage(
                "LVSGLU8",
                50,
                max_retries=3,
                _sleep=sleeps.append,
            )
    finally:
        _FakeBlob.upload_from_string = original

    # Backoff should have been invoked ``max_retries`` times (no sleep
    # after the final attempt).
    assert len(sleeps) == 3
    # Exponential: base * 2**attempt, base = 0.1.
    assert sleeps == [0.1, 0.2, 0.4]


def test_acquire_lock_tolerates_aware_started_at(fake_bucket):
    """Regression: if a lock object stores ``started_at`` with a tz
    offset (e.g. ``+00:00``), ``acquire_run_lock`` must not crash with
    ``TypeError: can't subtract offset-naive and offset-aware``. An
    old aware timestamp should be treated as stale and overridden."""
    ym = gcs.get_current_year_month()
    path = f"state/lock/LVSGLU8/verse/{ym}.json"
    stale_aware = (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(hours=48)
    )
    fake_bucket._store[path] = {
        "data": json.dumps({
            "status": "running",
            "started_at": stale_aware.isoformat(),
        }).encode(),
        "gen": 1,
    }

    ok, reason = gcs.acquire_run_lock("LVSGLU8", stale_after_hours=24)

    assert ok is True
    assert reason == "stale_overridden"


def test_signed_audio_url_caches_within_ttl(fake_bucket, monkeypatch):
    """Regression: signed URLs are cached per (fileset, book, chapter)
    within the TTL window so back-to-back requests for the same
    chapter don't each trigger an IAM signBlob call."""
    # Reset cache so ordering with other tests can't pollute results.
    gcs._signed_url_cache.clear()

    class _FakeCreds:
        service_account_email = "svc@example.iam.gserviceaccount.com"
        token = "tok"

        def refresh(self, _request):
            pass

    refresh_calls = {"n": 0}

    def fake_default(scopes=None):
        refresh_calls["n"] += 1
        return _FakeCreds(), "proj"

    monkeypatch.setattr(gcs.google.auth, "default", fake_default)

    sign_calls = {"n": 0}

    def fake_generate_signed_url(self, **_):
        sign_calls["n"] += 1
        return f"https://signed/{sign_calls['n']}"

    # Patch the fake blob used by this test module's _FakeBucket.
    _FakeBlob.generate_signed_url = fake_generate_signed_url

    url1 = gcs.signed_audio_url(
        "LVSGLU8", "JHN", 3, "lv-LV-Chirp3-HD-Sadachbia",
        ttl_seconds=300,
    )
    url2 = gcs.signed_audio_url(
        "LVSGLU8", "JHN", 3, "lv-LV-Chirp3-HD-Sadachbia",
        ttl_seconds=300,
    )
    url3 = gcs.signed_audio_url(
        "LVSGLU8", "JHN", 4, "lv-LV-Chirp3-HD-Sadachbia",
        ttl_seconds=300,
    )

    # Same chapter -> second call served from cache, no new signing.
    assert url1 == url2
    # Different chapter -> fresh signing.
    assert url3 != url1
    assert sign_calls["n"] == 2
    assert refresh_calls["n"] == 2


def test_acquire_lock_tolerates_fresh_aware_started_at(fake_bucket):
    """Companion to the stale case: a *fresh* aware timestamp must
    also be comparable without crashing and correctly block re-entry."""
    ym = gcs.get_current_year_month()
    path = f"state/lock/LVSGLU8/verse/{ym}.json"
    fresh_aware = datetime.datetime.now(datetime.timezone.utc)
    fake_bucket._store[path] = {
        "data": json.dumps({
            "status": "running",
            "started_at": fresh_aware.isoformat(),
        }).encode(),
        "gen": 1,
    }

    ok, reason = gcs.acquire_run_lock("LVSGLU8", stale_after_hours=24)

    assert ok is False
    assert reason == "active_run"


def test_chapter_object_paths_per_mode():
    audio, ts = gcs.chapter_object_paths(
        "LVSGLU8", "JHN", 3, "lv-LV-Chirp3-HD-Sadachbia",
    )
    assert audio == (
        "audio/LVSGLU8/lv-LV-Chirp3-HD-Sadachbia/JHN/3_audio.mp3"
    )
    assert ts == (
        "audio/LVSGLU8/lv-LV-Chirp3-HD-Sadachbia/JHN/3_timestamps.json"
    )

    audio, ts = gcs.chapter_object_paths(
        "LVSGLU8", "JHN", 3, "lv-LV-Chirp3-HD-Sadachbia",
        mode="chapter",
    )
    assert audio.startswith("audio_chapter/LVSGLU8/")
    assert ts.startswith("audio_chapter/LVSGLU8/")

    with pytest.raises(ValueError):
        gcs.chapter_object_paths(
            "LVSGLU8", "JHN", 3, "voice", mode="bogus",
        )


def test_run_locks_are_isolated_per_mode(fake_bucket):
    """A completed verse-mode lock must not block the chapter-mode
    run for the same month."""
    ok, reason = gcs.acquire_run_lock(
        "LVSGLU8", stale_after_hours=24, mode="verse",
    )
    assert ok and reason == "acquired"
    gcs.mark_run_complete(
        "LVSGLU8", chars_used=10, chapters_generated=1,
        reason="all_done", mode="verse",
    )

    ok, reason = gcs.acquire_run_lock(
        "LVSGLU8", stale_after_hours=24, mode="verse",
    )
    assert not ok and reason == "already_completed"

    ok, reason = gcs.acquire_run_lock(
        "LVSGLU8", stale_after_hours=24, mode="chapter",
    )
    assert ok and reason == "acquired"


def test_lock_state_path_includes_mode(fake_bucket):
    gcs.acquire_run_lock(
        "LVSGLU8", stale_after_hours=24, mode="chapter",
    )
    ym = gcs.get_current_year_month()
    assert f"state/lock/LVSGLU8/chapter/{ym}.json" in fake_bucket._store


def test_usage_counter_is_shared_across_modes(fake_bucket):
    """Monthly usage stays on the shared (mode-less) path: the char
    cap is a per-fileset spend ceiling, not per artifact set."""
    gcs.increment_monthly_usage("LVSGLU8", 500)
    ym = gcs.get_current_year_month()
    assert f"state/usage/LVSGLU8/{ym}.json" in fake_bucket._store
    assert gcs.read_monthly_usage("LVSGLU8") == 500


def test_list_completed_chapters_isolated_between_modes(fake_bucket):
    """Chapters generated in verse mode must not mark chapter-mode
    work as done (different GCS prefixes)."""
    verse_audio, verse_ts = gcs.chapter_object_paths(
        "LVSGLU8", "JHN", 3, "voice", mode="verse",
    )
    fake_bucket._store[verse_audio] = {"data": b"mp3", "gen": 1}
    fake_bucket._store[verse_ts] = {"data": b"{}", "gen": 1}

    verse_done = gcs.list_completed_chapters(
        "LVSGLU8", "voice", mode="verse",
    )
    chapter_done = gcs.list_completed_chapters(
        "LVSGLU8", "voice", mode="chapter",
    )

    assert verse_done == {("JHN", 3)}
    assert chapter_done == set()


def test_signed_url_cache_keys_include_mode(fake_bucket, monkeypatch):
    """Verse and chapter artifacts for the same chapter are different
    objects — their signed URLs must not share a cache entry."""
    gcs._signed_url_cache.clear()

    class _FakeCreds:
        service_account_email = "svc@example.iam.gserviceaccount.com"
        token = "tok"

        def refresh(self, _request):
            pass

    monkeypatch.setattr(
        gcs.google.auth, "default",
        lambda scopes=None: (_FakeCreds(), "proj"),
    )

    sign_calls = {"n": 0}

    def fake_generate_signed_url(self, **_):
        sign_calls["n"] += 1
        return f"https://signed/{sign_calls['n']}"

    _FakeBlob.generate_signed_url = fake_generate_signed_url

    url_verse = gcs.signed_audio_url(
        "LVSGLU8", "JHN", 3, "v", ttl_seconds=300, mode="verse",
    )
    url_chapter = gcs.signed_audio_url(
        "LVSGLU8", "JHN", 3, "v", ttl_seconds=300, mode="chapter",
    )

    assert url_verse != url_chapter
    assert sign_calls["n"] == 2


def test_signed_url_cache_is_bounded_lru(fake_bucket, monkeypatch):
    """The audio signed-URL cache mirrors the image one: a hard
    bound evicts the least-recently-used entry so memory cannot
    grow without limit on long-running workers (S7)."""
    gcs._signed_url_cache.clear()
    monkeypatch.setattr(gcs, "_SIGNED_URL_CACHE_MAX", 2)

    class _FakeCreds:
        service_account_email = "svc@example.iam.gserviceaccount.com"
        token = "tok"

        def refresh(self, _request):
            pass

    monkeypatch.setattr(
        gcs.google.auth, "default",
        lambda scopes=None: (_FakeCreds(), "proj"),
    )
    sign_calls = {"n": 0}

    def fake_generate_signed_url(self, **_):
        sign_calls["n"] += 1
        return f"https://signed/{sign_calls['n']}"

    _FakeBlob.generate_signed_url = fake_generate_signed_url

    url_ch1 = gcs.signed_audio_url(
        "LVSGLU8", "JHN", 1, "v", ttl_seconds=300,
    )
    gcs.signed_audio_url(
        "LVSGLU8", "JHN", 2, "v", ttl_seconds=300,
    )
    # Third distinct key evicts chapter 1 (LRU).
    gcs.signed_audio_url(
        "LVSGLU8", "JHN", 3, "v", ttl_seconds=300,
    )
    assert len(gcs._signed_url_cache) == 2

    url_ch1_again = gcs.signed_audio_url(
        "LVSGLU8", "JHN", 1, "v", ttl_seconds=300,
    )
    # Evicted -> re-signed, not the cached URL.
    assert url_ch1_again != url_ch1
    assert sign_calls["n"] == 4


def test_read_timestamps_json_caches_download(fake_bucket):
    """The immutable ``_timestamps.json`` payload is cached: a
    second read is served without another GCS download (S7)."""
    _, ts_path = gcs.chapter_object_paths(
        "LVSGLU8", "JHN", 3, "v", mode="verse",
    )
    payload = {"data": [{"verse_start": 1, "timestamp": 0.0}]}
    fake_bucket._store[ts_path] = {
        "data": json.dumps(payload).encode(),
        "gen": 1,
    }

    first = gcs.read_timestamps_json("LVSGLU8", "JHN", 3, "v")
    # Corrupt the stored object — a cached read must not see it.
    fake_bucket._store[ts_path]["data"] = b"corrupt"
    second = gcs.read_timestamps_json("LVSGLU8", "JHN", 3, "v")

    assert first == payload
    assert second == payload


def test_upload_chapter_artifacts_invalidates_cache(fake_bucket):
    """Re-generating a chapter must drop the cached timestamps
    payload so readers never see the stale artifact."""
    _, ts_path = gcs.chapter_object_paths(
        "LVSGLU8", "JHN", 3, "v", mode="verse",
    )
    fake_bucket._store[ts_path] = {
        "data": json.dumps({"data": "old"}).encode(),
        "gen": 1,
    }
    assert gcs.read_timestamps_json(
        "LVSGLU8", "JHN", 3, "v"
    ) == {"data": "old"}

    gcs.upload_chapter_artifacts(
        "LVSGLU8", "JHN", 3, b"mp3-bytes",
        {"data": "new"}, "v", mode="verse",
    )

    # The upload enriches the payload with ``file_size_bytes`` —
    # assert on the key that proves the re-download happened.
    assert gcs.read_timestamps_json(
        "LVSGLU8", "JHN", 3, "v"
    )["data"] == "new"
