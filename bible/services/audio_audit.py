"""Structural health checks for generated audio artifacts.

Pure validation logic for the ``audit_audio_files`` management
command — no GCS client usage, so the checks are unit-testable
with plain ``bytes`` inputs. Two artifact shapes are validated:

- ``*_audio.mp3`` objects (per-chapter synthesized audio), and
- ``*_timestamps.json`` sidecars (per-verse offsets + chapter
  ``duration_seconds``/``file_size_bytes`` metadata written by
  ``gcs.upload_chapter_artifacts``).

The checks catch *container-level* corruption — unparseable or
empty files, desynced MP3 frame streams (truncation, stray
bytes), absurd durations/bitrates, and disagreement between the
MP3 and its sidecar. They cannot hear content defects such as
clipping or TTS voice glitches; that needs a real decoder and is
intentionally out of scope so the audit stays dependency-free.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from io import BytesIO
from typing import Optional, Tuple

from mutagen.mp3 import MP3
from mutagen import MutagenError

# Smallest plausible artifact: a single MPEG-2 L3 frame at the
# lowest bitrate Google TTS emits is 72 bytes + header.
MIN_MP3_BYTES = 96
# One chapter should never approach an hour.
MAX_DURATION_SECONDS = 3600.0
# Implied bitrate sanity band (size*8/duration). TTS artifacts
# are 32 kbps; the band only rejects absurd mismatches.
MIN_IMPLIED_BITRATE = 4_000
MAX_IMPLIED_BITRATE = 512_000
# |mp3 duration - sidecar duration_seconds| tolerance. mutagen
# measures the same concatenated stream the generator measured,
# so agreement is normally exact.
DURATION_TOLERANCE_SECONDS = 0.75

AUDIO_SUFFIX = "_audio.mp3"
TIMESTAMPS_SUFFIX = "_timestamps.json"


@dataclass
class Issue:
    """One finding against an artifact."""

    code: str
    detail: str

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


@dataclass
class AudioCheck:
    """Validation result for one ``*_audio.mp3`` blob."""

    name: str
    size: int
    duration_seconds: Optional[float] = None
    issues: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues


# ------------------------------------------------------------------
# MP3 frame-stream walk
#
# ``mutagen`` parses headers and computes duration but tolerates
# junk between frames, so a naive byte walk is used to detect
# desync — bytes that belong to no frame (truncation, partial
# uploads, or corruption inside the stream).
# ------------------------------------------------------------------

# kbps tables for Layer III, bitrate-index -> kbps.
_MPEG1_L3_KBPS = {
    1: 32, 2: 40, 3: 48, 4: 56, 5: 64, 6: 80, 7: 96,
    8: 112, 9: 128, 10: 160, 11: 192, 12: 224, 13: 256, 14: 320,
}
_MPEG2_L3_KBPS = {
    1: 8, 2: 16, 3: 24, 4: 32, 5: 40, 6: 48, 7: 56,
    8: 64, 9: 80, 10: 96, 11: 112, 12: 128, 13: 144, 14: 160,
}
_MPEG1_SAMPLE_RATES = {0: 44100, 1: 48000, 2: 32000}
_MPEG2_SAMPLE_RATES = {0: 22050, 1: 24000, 2: 16000}


def _frame_info(header: bytes) -> Optional[Tuple[int, float]]:
    """Parse a 4-byte MPEG audio header.

    Returns ``(frame_length_bytes, frame_duration_seconds)`` for
    a Layer III frame, or ``None`` when the bytes are not a valid
    L3 frame header."""
    if len(header) < 4:
        return None
    b1, b2, b3, _b4 = header
    if b1 != 0xFF or (b2 & 0xE0) != 0xE0:
        return None
    version = (b2 >> 3) & 0x3
    layer = (b2 >> 1) & 0x3
    bitrate_idx = (b3 >> 4) & 0xF
    rate_idx = (b3 >> 2) & 0x3
    padding = (b3 >> 1) & 0x1
    if layer != 1 or bitrate_idx in (0, 15) or rate_idx == 3:
        return None
    if version == 3:  # MPEG-1
        bitrate = _MPEG1_L3_KBPS[bitrate_idx] * 1000
        sample_rate = _MPEG1_SAMPLE_RATES[rate_idx]
        frame_len = int(144 * bitrate / sample_rate + padding)
        return frame_len, 1152 / sample_rate
    if version == 0:  # MPEG-2.5 — treated like MPEG-2 (8 kHz rates)
        sample_rate = {0: 11025, 1: 12000, 2: 8000}[rate_idx]
    elif version == 2:  # MPEG-2
        sample_rate = _MPEG2_SAMPLE_RATES[rate_idx]
    else:
        return None
    bitrate = _MPEG2_L3_KBPS[bitrate_idx] * 1000
    frame_len = int(72 * bitrate / sample_rate + padding)
    return frame_len, 576 / sample_rate


def _skip_id3v2(data: bytes) -> int:
    """Return the offset just past a leading ID3v2 tag, or 0."""
    if len(data) < 10 or data[:3] != b"ID3":
        return 0
    # Synchsafe size: 4 bytes, 7 bits each.
    size = 0
    for b in data[6:10]:
        if b & 0x80:
            return 0
        size = (size << 7) | b
    return min(10 + size, len(data))


def walk_mp3_frames(data: bytes) -> Tuple[int, int, float]:
    """Walk the frame stream. Returns ``(frames, desync_bytes,
    duration_seconds)``.

    ``desync_bytes`` counts bytes belonging to no valid frame —
    garbage inside the stream or a truncated tail."""
    pos = _skip_id3v2(data)
    frames = 0
    desync = 0
    duration = 0.0
    while pos <= len(data) - 4:
        info = _frame_info(data[pos:pos + 4])
        if info is None:
            desync += 1
            pos += 1
            continue
        frame_len, frame_dur = info
        if pos + frame_len > len(data):
            desync += len(data) - pos
            pos = len(data)
            break
        frames += 1
        duration += frame_dur
        pos += frame_len
    # Bytes left after the loop's window (< 4 bytes) are desync too.
    desync += len(data) - pos
    return frames, desync, duration


# ------------------------------------------------------------------
# Artifact validation
# ------------------------------------------------------------------


def audit_mp3(name: str, data: bytes) -> AudioCheck:
    """Validate one MP3 blob's bytes. Never raises on bad input —
    malformed data becomes issues on the returned check."""
    check = AudioCheck(name=name, size=len(data))
    if len(data) < MIN_MP3_BYTES:
        check.issues.append(Issue(
            "too_small",
            f"{len(data)} bytes (< {MIN_MP3_BYTES})",
        ))
        return check
    try:
        info = MP3(BytesIO(data)).info
        check.duration_seconds = float(info.length)
    except (MutagenError, ValueError, ZeroDivisionError) as exc:
        check.issues.append(Issue(
            "unparseable", f"mutagen: {exc.__class__.__name__}",
        ))
    frames, desync, _walk_dur = walk_mp3_frames(data)
    if frames == 0:
        check.issues.append(Issue("no_frames", "no valid MP3 frames"))
        return check
    if desync:
        check.issues.append(Issue(
            "desync", f"{desync} byte(s) outside MP3 frames",
        ))
    if check.duration_seconds is not None:
        duration = check.duration_seconds
        if not 0 < duration <= MAX_DURATION_SECONDS:
            check.issues.append(Issue(
                "bad_duration", f"{duration:.1f}s",
            ))
        else:
            implied = check.size * 8 / duration
            if not MIN_IMPLIED_BITRATE <= implied <= MAX_IMPLIED_BITRATE:
                check.issues.append(Issue(
                    "bad_bitrate", f"implied {implied / 1000:.1f} kbps",
                ))
    return check


def audit_timestamps(raw: bytes) -> Tuple[Optional[dict], list]:
    """Validate a ``*_timestamps.json`` sidecar's bytes.

    Returns ``(payload, issues)`` — ``payload`` is the parsed dict
    (``None`` when the JSON could not be parsed at all) so the
    caller can still cross-check a partially valid document."""
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        return None, [Issue(
            "timestamps_invalid_json", str(exc),
        )]
    issues = []
    if not isinstance(payload, dict):
        return None, [Issue("timestamps_invalid", "not a JSON object")]
    duration = payload.get("duration_seconds")
    if not isinstance(duration, (int, float)) or duration <= 0:
        issues.append(Issue(
            "timestamps_bad_duration", repr(duration),
        ))
        duration = None
    entries = payload.get("data")
    if not isinstance(entries, list) or not entries:
        issues.append(Issue(
            "timestamps_empty", "missing or empty 'data' list",
        ))
        return payload, issues
    previous = -1.0
    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            issues.append(Issue(
                "timestamps_bad_entry", f"entry {idx} not an object",
            ))
            continue
        ts = entry.get("timestamp")
        if not isinstance(ts, (int, float)):
            issues.append(Issue(
                "timestamps_bad_entry", f"entry {idx} bad timestamp",
            ))
            continue
        if ts < previous:
            issues.append(Issue(
                "timestamps_not_monotonic",
                f"entry {idx} ts={ts} < {previous}",
            ))
            break
        previous = ts
        if duration is not None and ts > duration + 0.5:
            issues.append(Issue(
                "timestamp_out_of_range",
                f"entry {idx} ts={ts} > duration {duration}",
            ))
            break
    return payload, issues


def cross_check(
    name: str,
    blob_size: int,
    audio: AudioCheck,
    payload: dict,
) -> list:
    """Issues from comparing the MP3 against its sidecar JSON."""
    issues = []
    declared_size = payload.get("file_size_bytes")
    if (
        isinstance(declared_size, int)
        and declared_size != blob_size
    ):
        issues.append(Issue(
            "size_mismatch",
            f"sidecar={declared_size} blob={blob_size}",
        ))
    declared_dur = payload.get("duration_seconds")
    if (
        audio.duration_seconds is not None
        and isinstance(declared_dur, (int, float))
        and abs(audio.duration_seconds - declared_dur)
        > DURATION_TOLERANCE_SECONDS
    ):
        issues.append(Issue(
            "duration_mismatch",
            f"mp3={audio.duration_seconds:.3f}s "
            f"sidecar={declared_dur}s",
        ))
    # Any verse offset past the real end means the stream was
    # truncated after the sidecar was written.
    if audio.duration_seconds is not None:
        for entry in payload.get("data") or []:
            ts = entry.get("timestamp") if isinstance(entry, dict) \
                else None
            if (
                isinstance(ts, (int, float))
                and ts > audio.duration_seconds + 0.5
            ):
                issues.append(Issue(
                    "timestamp_out_of_range",
                    f"verse {entry.get('verse_start')} ts={ts} "
                    f"> mp3 duration {audio.duration_seconds:.3f}",
                ))
                break
    return issues
