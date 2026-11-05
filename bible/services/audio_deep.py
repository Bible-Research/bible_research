"""Content-level audio checks for ``audit_audio_files --deep``.

The structural checks in ``audio_audit`` verify the MP3
container; this module hears the *content* — TTS defects such
as clipped speech onsets (NUM.24:14 carries ~310 consecutive
full-scale samples), dead-silent verses and loudness outliers —
by decoding the stream to PCM and analysing each verse segment
delimited by the ``*_timestamps.json`` sidecar.

Decoding needs ``miniaudio``, a dev-only dependency
(``requirements-dev.txt``) deliberately kept out of the
production image. Every analysis function takes plain int16 PCM
arrays so tests can synthesize signals without a decoder.
"""
from __future__ import annotations

import math
import statistics
import tempfile
from array import array
from typing import Optional, Sequence, Tuple

from bible.services.audio_audit import Issue

# --- clipping -------------------------------------------------
# |sample| at/above this fraction of int16 full scale counts as
# "at the rail" (0.998 * 32767 ~ 32704).
CLIP_THRESHOLD = 0.998
# A rail-hugging run this long is a hard clip baked into the
# synthesis, not MP3 encoder overshoot (isolated peaks top out
# at a few samples; NUM.24:14's clipped onset ran ~310).
CLIP_MIN_RUN = 8
# Also flag a segment whose total rail hits exceed this even
# without a single long run (intermittent clipping).
CLIP_MIN_TOTAL = 128

# --- silence --------------------------------------------------
# Peak below this fraction of full scale (~-40 dBFS) counts as
# digital silence.
SILENCE_PEAK = 0.01
# A verse segment this long under the peak is missing speech;
# normal inter-verse pauses are shorter than a whole segment.
SILENCE_MIN_SECONDS = 0.75
# Whole-file fallback: interior gaps need a bigger margin so
# verse pauses do not trip the check.
FILE_SILENCE_MIN_SECONDS = 2.5
# Block width for the fallback silence scan.
FILE_SILENCE_BLOCK_SECONDS = 0.05

# --- loudness -------------------------------------------------
LOUDNESS_HIGH = 3.0
LOUDNESS_LOW = 0.3
# A median needs a few segments to mean anything.
LOUDNESS_MIN_SEGMENTS = 4
# Segments shorter than this are skipped entirely (back-to-back
# timestamps can produce near-empty spans).
MIN_SEGMENT_SECONDS = 0.1

_DECODER_HINT = (
    "--deep needs an MP3 decoder that is a dev-only dependency: "
    "run `pip install -r requirements-dev.txt` (miniaudio) — "
    "it is deliberately not in requirements.txt"
)


class DecoderUnavailableError(RuntimeError):
    """Raised when --deep is requested without a PCM decoder."""


def ensure_decoder() -> None:
    """Raise DecoderUnavailableError unless miniaudio imports."""
    try:
        import miniaudio  # noqa: F401
    except ImportError as exc:
        raise DecoderUnavailableError(_DECODER_HINT) from exc


def decode_mp3(path: str) -> Tuple[array, int]:
    """Decode an MP3 file to mono int16 PCM at its native rate.

    Returns ``(samples, sample_rate)`` where ``samples`` is an
    ``array('h')``. Raises ``DecoderUnavailableError`` when the
    dev-only ``miniaudio`` package is missing; decode failures
    surface as whatever the decoder raises."""
    ensure_decoder()
    import miniaudio

    info = miniaudio.get_file_info(path)
    decoded = miniaudio.decode_file(
        path,
        output_format=miniaudio.SampleFormat.SIGNED16,
        nchannels=1,
        sample_rate=info.sample_rate,
    )
    return decoded.samples, decoded.sample_rate


def decode_mp3_bytes(data: bytes) -> Tuple[array, int]:
    """``decode_mp3`` over a bytes blob via a temp file."""
    with tempfile.NamedTemporaryFile(suffix=".mp3") as tmp:
        tmp.write(data)
        tmp.flush()
        return decode_mp3(tmp.name)


# ------------------------------------------------------------------
# Analysis — pure PCM, no decoder needed
# ------------------------------------------------------------------


def _segment_stats(seg: Sequence[int], rail: int) -> dict:
    """One pass over int16 samples: peak, RMS and rail stats.

    ``clip_samples`` counts samples at/above ``rail``;
    ``clip_max_run`` is the longest *consecutive* such stretch —
    the discriminator between encoder overshoot and real
    clipping."""
    peak = 0
    square_sum = 0
    clip_samples = 0
    run = 0
    max_run = 0
    for s in seg:
        a = -s if s < 0 else s
        if a > peak:
            peak = a
        square_sum += s * s
        if a >= rail:
            clip_samples += 1
            run += 1
            if run > max_run:
                max_run = run
        else:
            run = 0
    n = len(seg)
    rms = math.sqrt(square_sum / n) if n else 0.0
    return {
        "peak": peak / 32768.0,
        "rms": rms / 32768.0,
        "clip_samples": clip_samples,
        "clip_max_run": max_run,
    }


def _clip_issue(where: str, stats: dict) -> Issue:
    return Issue(
        "clipped",
        f"{where}: {stats['clip_samples']} near-full-scale "
        f"sample(s), longest run {stats['clip_max_run']}",
    )


def _is_clipped(stats, clip_min_run, clip_min_total) -> bool:
    return (
        stats["clip_max_run"] >= clip_min_run
        or stats["clip_samples"] >= clip_min_total
    )


def _verse_bounds(
    payload: dict,
    total_samples: int,
    sample_rate: int,
) -> list:
    """``[(verse, start_idx, end_idx)]`` from a sidecar payload.

    Each ``data`` entry opens a segment at its ``timestamp``;
    the segment ends at the next entry or at the sidecar's
    ``duration_seconds`` (PCM end when that is absent)."""
    entries = []
    for entry in payload.get("data") or []:
        ts = entry.get("timestamp") if isinstance(entry, dict) \
            else None
        if isinstance(ts, (int, float)) and ts >= 0:
            entries.append((entry.get("verse_start"), float(ts)))
    if not entries:
        return []
    entries.sort(key=lambda e: e[1])
    end_s = payload.get("duration_seconds")
    if not isinstance(end_s, (int, float)) or end_s <= 0:
        end_s = total_samples / sample_rate
    bounds = []
    for i, (verse, ts) in enumerate(entries):
        nxt = entries[i + 1][1] if i + 1 < len(entries) else end_s
        start = max(0, min(int(ts * sample_rate), total_samples))
        stop = max(start, min(int(nxt * sample_rate),
                              total_samples))
        bounds.append((verse, start, stop))
    return bounds


def analyze_pcm(
    samples: Sequence[int],
    sample_rate: int,
    payload: Optional[dict] = None,
    *,
    clip_threshold: float = CLIP_THRESHOLD,
    clip_min_run: int = CLIP_MIN_RUN,
    clip_min_total: int = CLIP_MIN_TOTAL,
    silence_peak: float = SILENCE_PEAK,
    silence_min_seconds: float = SILENCE_MIN_SECONDS,
    loudness_high: float = LOUDNESS_HIGH,
    loudness_low: float = LOUDNESS_LOW,
) -> list:
    """Content-check decoded PCM; returns a list of Issues.

    With a valid timestamps ``payload`` each verse segment is
    checked for clipping, dead silence and loudness outliers
    versus the chapter median. Without one, only whole-file
    clipping and interior-silence stats are produced."""
    if not len(samples) or sample_rate <= 0:
        return []
    rail = int(clip_threshold * 32767)
    bounds = (
        _verse_bounds(payload, len(samples), sample_rate)
        if payload else []
    )
    if not bounds:
        return _analyze_whole_file(
            samples, sample_rate, rail,
            clip_min_run=clip_min_run,
            clip_min_total=clip_min_total,
            silence_peak=silence_peak,
        )

    issues = []
    min_len = int(MIN_SEGMENT_SECONDS * sample_rate)
    stats_by_verse = []
    for verse, start, stop in bounds:
        seg = samples[start:stop]
        if len(seg) < min_len:
            continue
        stats = _segment_stats(seg, rail)
        stats_by_verse.append((verse, stats))
        where = f"verse {verse}"
        if _is_clipped(stats, clip_min_run, clip_min_total):
            issues.append(_clip_issue(where, stats))
        duration = len(seg) / sample_rate
        if (
            stats["peak"] <= silence_peak
            and duration >= silence_min_seconds
        ):
            issues.append(Issue(
                "silent_segment",
                f"{where}: {duration:.2f}s at peak "
                f"{stats['peak']:.4f}",
            ))
    issues.extend(_loudness_issues(
        stats_by_verse, silence_peak,
        loudness_high, loudness_low,
    ))
    return issues


def _loudness_issues(
    stats_by_verse: list,
    silence_peak: float,
    loudness_high: float,
    loudness_low: float,
) -> list:
    """Flag verses whose RMS departs from the chapter median."""
    audible = [
        (v, s) for v, s in stats_by_verse
        if s["peak"] > silence_peak
    ]
    if len(audible) < LOUDNESS_MIN_SEGMENTS:
        return []
    median = statistics.median(s["rms"] for _v, s in audible)
    if median <= 0:
        return []
    issues = []
    for verse, stats in audible:
        rms = stats["rms"]
        if (
            rms > loudness_high * median
            or rms < loudness_low * median
        ):
            ratio = rms / median
            issues.append(Issue(
                "loudness_outlier",
                f"verse {verse}: rms {rms:.4f} is {ratio:.2f}x "
                f"chapter median {median:.4f}",
            ))
    return issues


def _analyze_whole_file(
    samples: Sequence[int],
    sample_rate: int,
    rail: int,
    *,
    clip_min_run: int,
    clip_min_total: int,
    silence_peak: float,
) -> list:
    """Fallback stats when no sidecar delimits the verses."""
    issues = []
    stats = _segment_stats(samples, rail)
    if _is_clipped(stats, clip_min_run, clip_min_total):
        issues.append(_clip_issue("whole file", stats))
    if stats["peak"] <= silence_peak:
        issues.append(Issue(
            "silent_segment",
            f"whole file at peak {stats['peak']:.4f}",
        ))
        return issues
    issues.extend(_interior_silence(samples, sample_rate,
                                    silence_peak))
    return issues


def _interior_silence(
    samples: Sequence[int],
    sample_rate: int,
    silence_peak: float,
) -> list:
    """Scan for mid-file dead air; edges are natural pauses."""
    rail = int(silence_peak * 32768)
    block = max(1, int(FILE_SILENCE_BLOCK_SECONDS * sample_rate))
    n = len(samples)
    issues = []
    run_start = None
    for off in range(0, n, block):
        blk = samples[off:off + block]
        quiet = max(blk, default=0) <= rail \
            and min(blk, default=0) >= -rail
        if quiet and run_start is None:
            run_start = off
        elif not quiet and run_start is not None:
            _maybe_flag_gap(
                issues, run_start, off, n, sample_rate)
            run_start = None
    if run_start is not None:
        _maybe_flag_gap(issues, run_start, n, n, sample_rate)
    return issues


def _maybe_flag_gap(issues, start, stop, total, sample_rate):
    """Append a silent_region issue for a long interior gap."""
    if start == 0 or stop >= total:
        return  # leading/trailing quiet is a boundary pause
    gap = (stop - start) / sample_rate
    if gap >= FILE_SILENCE_MIN_SECONDS:
        issues.append(Issue(
            "silent_region",
            f"interior gap {gap:.2f}s at ~"
            f"{start / sample_rate:.2f}s",
        ))
