"""Tests for bible.services.audio_audit and the
``audit_audio_files`` management command. GCS is replaced by
fake blob/bucket objects; MP3 fixtures are synthetic MPEG-2
Layer III CBR streams (0xFFF344C4 = 32 kbps / 24 kHz / mono,
96-byte frames)."""
import json
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from bible.services import audio_audit
from bible.services.audio_audit import (
    AUDIO_SUFFIX,
    TIMESTAMPS_SUFFIX,
    audit_mp3,
    audit_timestamps,
    cross_check,
    walk_mp3_frames,
)

# 96-byte MPEG-2 L3 frame: 24 ms of audio.
FRAME = bytes([0xFF, 0xF3, 0x44, 0xC4]) + bytes(92)
VOICE = "lv-LV-Chirp3-HD-Sadachbia"
NUM24_MP3 = (
    f"audio/LVSGLU8/{VOICE}/NUM/24{AUDIO_SUFFIX}"
)
NUM24_JSON = (
    f"audio/LVSGLU8/{VOICE}/NUM/24{TIMESTAMPS_SUFFIX}"
)


def make_mp3(frames=100):
    return FRAME * frames


def make_sidecar(
    duration=2.4, size=None, timestamps=None,
):
    return json.dumps({
        "duration_seconds": duration,
        "file_size_bytes": size,
        "data": timestamps or [
            {"verse_start": 1, "timestamp": 0.0},
            {"verse_start": 2, "timestamp": 1.2},
        ],
    }).encode()


class FakeBlob:
    """Stand-in for google.cloud.storage.Blob."""

    def __init__(self, name, data):
        self.name = name
        self._data = data
        self.size = len(data)
        self.downloads = 0

    def download_as_bytes(self):
        self.downloads += 1
        return self._data


class FakeBucket:
    """list_blobs over a flat {name: bytes} mapping."""

    def __init__(self, objects):
        self.blobs = {
            name: FakeBlob(name, data)
            for name, data in objects.items()
        }
        self.name = "test-bucket"

    def list_blobs(self, prefix=None):
        return [
            b for b in self.blobs.values()
            if prefix is None or b.name.startswith(prefix)
        ]


class FakeClient:
    def __init__(self, bucket):
        self._bucket = bucket

    def bucket(self, name):
        return self._bucket


def call_audit(bucket, *argv):
    """Run the command against ``bucket``; returns stdout text."""
    out = StringIO()
    with patch(
        "bible.management.commands.audit_audio_files"
        ".gcs.get_default_client",
        return_value=FakeClient(bucket),
    ):
        call_command("audit_audio_files", *argv, stdout=out)
    return out.getvalue()


# ---------------------------------------------------------------
# walk_mp3_frames / audit_mp3
# ---------------------------------------------------------------


def test_walk_clean_stream():
    data = make_mp3(100)
    frames, desync, duration = walk_mp3_frames(data)
    assert frames == 100
    assert desync == 0
    assert duration == pytest.approx(2.4)


def test_walk_counts_garbage_and_truncated_tail():
    data = make_mp3(50) + b"XYZ" + make_mp3(10) + FRAME[:60]
    frames, desync, _ = walk_mp3_frames(data)
    assert frames == 60
    assert desync == 3 + 60


def test_walk_skips_leading_id3v2():
    # ID3v2 header: "ID3" + ver/flags + synchsafe size (20 bytes).
    tag = b"ID3\x04\x00\x00" + bytes([0, 0, 0, 20]) + bytes(20)
    frames, desync, _ = walk_mp3_frames(tag + make_mp3(10))
    assert frames == 10
    assert desync == 0


def test_audit_mp3_clean():
    check = audit_mp3("ok.mp3", make_mp3(100))
    assert check.ok
    assert check.duration_seconds == pytest.approx(2.4)


def test_audit_mp3_too_small():
    check = audit_mp3("tiny.mp3", b"\xff\xf3")
    assert [i.code for i in check.issues] == ["too_small"]


def test_audit_mp3_garbage():
    check = audit_mp3("junk.mp3", bytes(500))
    codes = {i.code for i in check.issues}
    assert "unparseable" in codes or "no_frames" in codes
    assert not check.ok


def test_audit_mp3_desync_mid_stream():
    check = audit_mp3("bad.mp3", make_mp3(50) + b"JUNK" + make_mp3(50))
    assert "desync" in {i.code for i in check.issues}


def test_audit_mp3_truncated_last_frame():
    check = audit_mp3("trunc.mp3", make_mp3(100)[:-50])
    assert "desync" in {i.code for i in check.issues}


# ---------------------------------------------------------------
# audit_timestamps
# ---------------------------------------------------------------


def test_timestamps_valid():
    payload, issues = audit_timestamps(make_sidecar(size=9600))
    assert issues == []
    assert payload["duration_seconds"] == 2.4


def test_timestamps_bad_json():
    payload, issues = audit_timestamps(b"{not json")
    assert payload is None
    assert issues[0].code == "timestamps_invalid_json"


def test_timestamps_not_monotonic():
    raw = make_sidecar(timestamps=[
        {"verse_start": 1, "timestamp": 0.0},
        {"verse_start": 2, "timestamp": 1.5},
        {"verse_start": 3, "timestamp": 1.2},
    ])
    _, issues = audit_timestamps(raw)
    assert "timestamps_not_monotonic" in {i.code for i in issues}


def test_timestamps_out_of_range():
    raw = make_sidecar(duration=2.0, timestamps=[
        {"verse_start": 1, "timestamp": 0.0},
        {"verse_start": 2, "timestamp": 9.0},
    ])
    _, issues = audit_timestamps(raw)
    assert "timestamp_out_of_range" in {i.code for i in issues}


# ---------------------------------------------------------------
# cross_check
# ---------------------------------------------------------------


def test_cross_check_size_mismatch():
    check = audit_mp3("a.mp3", make_mp3(100))
    payload = json.loads(
        make_sidecar(duration=2.4, size=1234)
    )
    codes = {
        i.code for i in cross_check("a.mp3", 9600, check, payload)
    }
    assert "size_mismatch" in codes


def test_cross_check_duration_mismatch():
    check = audit_mp3("a.mp3", make_mp3(100))
    payload = json.loads(make_sidecar(duration=99.0))
    codes = {
        i.code for i in cross_check("a.mp3", 9600, check, payload)
    }
    assert "duration_mismatch" in codes


def test_cross_check_timestamp_beyond_truncated_mp3():
    # A truncated MP3 whose sidecar still describes the full
    # stream: verse offsets past the real end are flagged.
    check = audit_mp3("a.mp3", make_mp3(50))  # 1.2s
    payload = json.loads(make_sidecar(
        duration=2.4,
        timestamps=[
            {"verse_start": 1, "timestamp": 0.0},
            {"verse_start": 2, "timestamp": 2.0},
        ],
    ))
    codes = {
        i.code for i in cross_check("a.mp3", 4800, check, payload)
    }
    assert "duration_mismatch" in codes
    assert "timestamp_out_of_range" in codes


# ---------------------------------------------------------------
# command-level (fake bucket)
# ---------------------------------------------------------------


def good_objects():
    return {
        NUM24_MP3: make_mp3(100),
        NUM24_JSON: make_sidecar(duration=2.4, size=9600),
    }


def test_command_clean_bucket():
    out = call_audit(FakeBucket(good_objects()))
    assert "suspects=0" in out


def test_command_flags_corrupt_mp3_and_exits_nonzero():
    objects = good_objects()
    objects[NUM24_MP3] = make_mp3(50) + b"JUNKJUNK" + make_mp3(50)
    with pytest.raises(CommandError):
        call_audit(FakeBucket(objects))


def test_command_missing_sidecar_flagged():
    objects = good_objects()
    del objects[NUM24_JSON]
    with pytest.raises(CommandError) as exc:
        call_audit(FakeBucket(objects))
    assert "suspect" in str(exc.value)


def test_command_metadata_only_downloads_no_mp3():
    bucket = FakeBucket(good_objects())
    out = call_audit(bucket, "--metadata-only")
    assert "suspects=0" in out
    assert bucket.blobs[NUM24_MP3].downloads == 0
    assert bucket.blobs[NUM24_JSON].downloads == 1


def test_command_metadata_only_still_catches_size_mismatch():
    objects = {
        NUM24_MP3: make_mp3(100),
        NUM24_JSON: make_sidecar(duration=2.4, size=42),
    }
    with pytest.raises(CommandError):
        call_audit(FakeBucket(objects), "--metadata-only")


def test_command_fileset_and_book_filters():
    other_mp3 = NUM24_MP3.replace("/NUM/", "/GEN/")
    other_json = NUM24_JSON.replace("/NUM/", "/GEN/")
    objects = good_objects()
    objects[other_mp3] = b"junk"  # would flag if scanned
    objects[other_json] = make_sidecar(duration=2.4, size=4)
    out = call_audit(
        FakeBucket(objects),
        "--fileset-id", "LVSGLU8", "--book", "NUM",
    )
    assert "suspects=0" in out
    assert "scanned=1" in out


def test_command_orphan_sidecar_flagged():
    objects = good_objects()
    orphan = NUM24_JSON.replace("/24_", "/25_")
    objects[orphan] = make_sidecar(duration=2.4, size=9600)
    with pytest.raises(CommandError):
        call_audit(FakeBucket(objects))


def test_command_json_dir_without_mp3s_not_orphaned():
    # A directory holding only sidecar JSONs (the
    # timestamps_generated/ layout) must not produce orphans.
    objects = good_objects()
    objects["timestamps_generated/LATLVRN1DA/GEN/1_timestamps.json"] = (
        make_sidecar(duration=2.4)
    )
    out = call_audit(FakeBucket(objects))
    assert "suspects=0" in out


def test_command_limit_caps_full_checks():
    objects = good_objects()
    extra_mp3 = NUM24_MP3.replace("/24_", "/25_")
    extra_json = NUM24_JSON.replace("/24_", "/25_")
    objects[extra_mp3] = make_mp3(80)
    objects[extra_json] = make_sidecar(duration=1.92, size=7680)
    out = call_audit(FakeBucket(objects), "--limit", "1")
    assert "fully_checked=1" in out
