"""Tests for bible.services.audio_audit and the
``audit_audio_files`` management command. GCS is replaced by
fake blob/bucket objects; MP3 fixtures are synthetic MPEG-2
Layer III CBR streams (0xFFF344C4 = 32 kbps / 24 kHz / mono,
96-byte frames). The --deep content checks are exercised on
synthesized int16 PCM (and one lameenc->miniaudio round-trip)
so no network or real MP3 fixture is needed."""
import json
import math
import sys
from array import array
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from bible.services import audio_audit
from bible.services import audio_deep
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


# ---------------------------------------------------------------
# audio_deep — content checks on synthesized PCM
# ---------------------------------------------------------------

SR = 24000


def pcm_sine(seconds, amp=0.5):
    """Clean int16 sine at ``amp`` of full scale."""
    n = int(seconds * SR)
    return array("h", (
        int(amp * 32767 * math.sin(2 * math.pi * 220 * i / SR))
        for i in range(n)
    ))


def pcm_silence(seconds):
    return array("h", [0]) * int(seconds * SR)


def pcm_clipped(seconds):
    """Sine driven past the rail — long consecutive clip runs."""
    out = array("h")
    for i in range(int(seconds * SR)):
        v = int(1.8 * 32767 * math.sin(2 * math.pi * 220 * i / SR))
        out.append(max(-32768, min(32767, v)))
    return out


def join_pcm(*parts):
    out = array("h")
    for p in parts:
        out.extend(p)
    return out


def verse_payload(verses):
    """Sidecar-shaped payload; ``verses`` = [(verse, ts), ...]
    plus an explicit chapter duration."""
    return {
        "duration_seconds": verses[-1][1] + 1.0,
        "data": [
            {"verse_start": v, "timestamp": ts}
            for v, ts in verses
        ],
    }


def codes(issues):
    return {i.code for i in issues}


def test_deep_clean_chapter_passes():
    pcm = join_pcm(*(pcm_sine(1.0) for _ in range(3)))
    payload = verse_payload([(1, 0.0), (2, 1.0), (3, 2.0)])
    assert audio_deep.analyze_pcm(pcm, SR, payload) == []


def test_deep_flags_clipped_verse_with_number():
    # Mirror NUM.24:14 — verse 14's onset is hard-clipped while
    # the surrounding verses are clean.
    pcm = join_pcm(
        pcm_sine(1.0),
        join_pcm(pcm_clipped(0.3), pcm_sine(0.7)),
        pcm_sine(1.0),
    )
    payload = verse_payload([(13, 0.0), (14, 1.0), (15, 2.0)])
    issues = audio_deep.analyze_pcm(pcm, SR, payload)
    clipped = [i for i in issues if i.code == "clipped"]
    assert len(clipped) == 1
    assert "verse 14" in clipped[0].detail


def test_deep_flags_silent_verse():
    pcm = join_pcm(
        pcm_sine(1.0), pcm_silence(1.0), pcm_sine(1.0),
    )
    payload = verse_payload([(1, 0.0), (2, 1.0), (3, 2.0)])
    issues = audio_deep.analyze_pcm(pcm, SR, payload)
    silent = [i for i in issues if i.code == "silent_segment"]
    assert len(silent) == 1
    assert "verse 2" in silent[0].detail


def test_deep_flags_loudness_outlier():
    # Five verses, verse 3 ~8 dB quieter than the rest.
    pcm = join_pcm(
        pcm_sine(1.0, amp=0.5), pcm_sine(1.0, amp=0.5),
        pcm_sine(1.0, amp=0.05), pcm_sine(1.0, amp=0.5),
        pcm_sine(1.0, amp=0.5),
    )
    payload = verse_payload(
        [(i, float(i - 1)) for i in range(1, 6)]
    )
    issues = audio_deep.analyze_pcm(pcm, SR, payload)
    outlier = [
        i for i in issues if i.code == "loudness_outlier"
    ]
    assert len(outlier) == 1
    assert "verse 3" in outlier[0].detail


def test_deep_loud_sine_below_rail_is_clean():
    # 0.9 full scale is loud but never at the rail.
    pcm = pcm_sine(1.0, amp=0.9)
    issues = audio_deep.analyze_pcm(pcm, SR)
    assert "clipped" not in codes(issues)


def test_deep_clip_thresholds_configurable():
    pcm = pcm_clipped(0.5)
    assert "clipped" in codes(audio_deep.analyze_pcm(pcm, SR))
    issues = audio_deep.analyze_pcm(
        pcm, SR, clip_min_run=10**6, clip_min_total=10**6,
    )
    assert "clipped" not in codes(issues)


def test_deep_short_silence_not_flagged():
    # A 0.4 s quiet verse is under SILENCE_MIN_SECONDS.
    pcm = join_pcm(
        pcm_sine(1.0), pcm_silence(0.4), pcm_sine(0.6),
        pcm_sine(1.0),
    )
    payload = verse_payload([(1, 0.0), (2, 1.0), (3, 2.0)])
    issues = audio_deep.analyze_pcm(pcm, SR, payload)
    assert "silent_segment" not in codes(issues)


def test_deep_no_sidecar_whole_file_fallback():
    pcm = join_pcm(pcm_sine(0.5), pcm_clipped(0.5))
    issues = audio_deep.analyze_pcm(pcm, SR)
    clipped = [i for i in issues if i.code == "clipped"]
    assert clipped and "whole file" in clipped[0].detail


def test_deep_no_sidecar_interior_silence_flagged():
    pcm = join_pcm(
        pcm_sine(1.0), pcm_silence(3.0), pcm_sine(1.0),
    )
    issues = audio_deep.analyze_pcm(pcm, SR)
    assert "silent_region" in codes(issues)


def test_deep_no_sidecar_edge_silence_not_flagged():
    # Quiet head/tail padding is normal — not an interior gap.
    pcm = join_pcm(
        pcm_silence(3.0), pcm_sine(1.0), pcm_silence(3.0),
    )
    issues = audio_deep.analyze_pcm(pcm, SR)
    assert "silent_region" not in codes(issues)


def test_deep_fully_silent_file_flagged():
    issues = audio_deep.analyze_pcm(pcm_silence(5.0), SR)
    assert "silent_segment" in codes(issues)


def test_decode_mp3_missing_decoder_hint(monkeypatch):
    monkeypatch.setitem(sys.modules, "miniaudio", None)
    with pytest.raises(audio_deep.DecoderUnavailableError):
        audio_deep.ensure_decoder()
    with pytest.raises(
        audio_deep.DecoderUnavailableError, match="dev"
    ):
        audio_deep.decode_mp3("x.mp3")


def test_decode_mp3_roundtrip_flags_clip(tmp_path):
    """Real decode path: lameenc-encoded clipped sine -> MP3."""
    pytest.importorskip("miniaudio")
    import lameenc

    pcm = join_pcm(
        pcm_sine(1.0), join_pcm(
            pcm_clipped(0.3), pcm_sine(0.7),
        ), pcm_sine(1.0),
    )
    enc = lameenc.Encoder()
    enc.set_bit_rate(48)
    enc.set_in_sample_rate(SR)
    enc.set_channels(1)
    enc.set_quality(2)
    mp3 = enc.encode(pcm.tobytes()) + enc.flush()
    path = tmp_path / "clipped.mp3"
    path.write_bytes(mp3)
    samples, rate = audio_deep.decode_mp3(str(path))
    payload = verse_payload([(1, 0.0), (2, 1.0), (3, 2.0)])
    # LAME rescales peaks below the int16 rail (decoded peak
    # ~0.97), so this synthetic clip is detected one step below
    # CLIP_THRESHOLD — the real TTS artifacts sit at full scale.
    issues = audio_deep.analyze_pcm(
        samples, rate, payload, clip_threshold=0.95,
    )
    clipped = [i for i in issues if i.code == "clipped"]
    assert clipped and "verse 2" in clipped[0].detail


# ---------------------------------------------------------------
# --deep at the command level
# ---------------------------------------------------------------


def test_command_deep_rejects_metadata_only():
    with pytest.raises(CommandError, match="metadata-only"):
        call_audit(
            FakeBucket(good_objects()),
            "--metadata-only", "--deep",
        )


def test_command_deep_requires_decoder(monkeypatch):
    monkeypatch.setitem(sys.modules, "miniaudio", None)
    with pytest.raises(CommandError, match="dev"):
        call_audit(FakeBucket(good_objects()), "--deep")


def deep_pcm(clip_second_verse=False):
    """PCM the patched decoder returns for any blob."""
    second = (
        join_pcm(pcm_clipped(0.3), pcm_sine(0.7))
        if clip_second_verse else pcm_sine(1.0)
    )
    pcm = join_pcm(pcm_sine(1.0), second, pcm_sine(1.0))
    return pcm, SR


def call_audit_deep(bucket, pcm_pair, out, *argv):
    """--deep run with the decoder patched to canned PCM;
    writes into the caller's ``out`` buffer."""
    decode = patch(
        "bible.management.commands.audit_audio_files"
        ".audio_deep.decode_mp3_bytes",
        return_value=pcm_pair,
    )
    with patch(
        "bible.management.commands.audit_audio_files"
        ".gcs.get_default_client",
        return_value=FakeClient(bucket),
    ), decode:
        call_command("audit_audio_files", *argv, stdout=out)


def test_command_deep_flags_clipped_verse():
    objects = {
        NUM24_MP3: make_mp3(100),
        NUM24_JSON: make_sidecar(
            duration=3.0, size=9600, timestamps=[
                {"verse_start": 13, "timestamp": 0.0},
                {"verse_start": 14, "timestamp": 1.0},
                {"verse_start": 15, "timestamp": 2.0},
            ],
        ),
    }
    out = StringIO()
    with pytest.raises(CommandError):
        call_audit_deep(
            FakeBucket(objects),
            deep_pcm(clip_second_verse=True),
            out, "--deep",
        )
    text = out.getvalue()
    assert "clipped" in text
    assert "verse 14" in text
    assert "deep=True" in text


def test_command_deep_clean_audio_passes():
    out = StringIO()
    call_audit_deep(
        FakeBucket(good_objects()),
        deep_pcm(), out, "--deep",
    )
    assert "suspects=0" in out.getvalue()


def test_command_deep_decode_failure_is_issue_not_crash():
    out = StringIO()
    with patch(
        "bible.management.commands.audit_audio_files"
        ".gcs.get_default_client",
        return_value=FakeClient(FakeBucket(good_objects())),
    ), patch(
        "bible.management.commands.audit_audio_files"
        ".audio_deep.decode_mp3_bytes",
        side_effect=ValueError("boom"),
    ):
        with pytest.raises(CommandError):
            call_command(
                "audit_audio_files", "--deep", stdout=out,
            )
    assert "decode_failed" in out.getvalue()
