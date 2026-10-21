"""Tests for ``GeminiSynthesizer``.

The Gemini client is faked (no network), ``lameenc`` encoding is
patched out, and ``BYTES_PER_SECOND`` is shrunk so tests can use
small PCM buffers: with BYTES_PER_SECOND=10 a fake returning
``len(text)`` PCM bytes yields 10 chars/sec — under the 15/s
plausibility ceiling, i.e. "not truncated".
"""
import pytest

from bible.services.gemini_tts import synthesizer as synth_module
from bible.services.gemini_tts.client import SynthesisResult
from bible.services.gemini_tts.synthesizer import (
    GeminiSynthesizer,
    RequestBudgetExceeded,
    SegmentTruncatedError,
    is_truncated,
    split_verses_into_segments,
)


@pytest.fixture(autouse=True)
def _small_pcm(monkeypatch):
    """Shrink PCM math so fake audio stays tiny."""
    monkeypatch.setattr(synth_module, "BYTES_PER_SECOND", 10)


@pytest.fixture(autouse=True)
def _no_lameenc(monkeypatch):
    monkeypatch.setattr(
        synth_module, "encode_pcm_to_mp3",
        lambda pcm, sample_rate=24000: b"MP3(" + pcm[:8],
    )


class _FakeSword:
    def __init__(self, verses):
        self._verses = verses

    def get_chapter_verses(self, _canon, _book, _chapter):
        return self._verses


class _FakeGemini:
    """Fake client: PCM sized ``pcm_per_char * len(text)`` so
    chars-per-second math is deterministic under the shrunk
    BYTES_PER_SECOND."""

    def __init__(self, pcm_per_char=1, output_tokens=100):
        self.calls = []
        self.pcm_per_char = pcm_per_char
        self.output_tokens = output_tokens
        self.requests_made = 0

    def synthesize(self, text, voice_name=None):
        self.calls.append(text)
        self.requests_made += 1
        tokens = self.output_tokens
        if callable(tokens):
            tokens = tokens(text)
        return SynthesisResult(
            pcm_bytes=b"\x00" * (len(text) * self.pcm_per_char),
            output_tokens=tokens,
        )


def _verses(n, text_len=10):
    return [
        {"verse_start": i, "verse_text": f"v{i} " + "x" * text_len}
        for i in range(1, n + 1)
    ]


def _synth(verses, client, **kwargs):
    synth = GeminiSynthesizer(
        client=client, style_prompt="", **kwargs,
    )
    synth._sword = _FakeSword(verses)
    return synth


def test_split_verses_packs_segments_up_to_char_cap():
    verses = _verses(5, text_len=10)  # 14 chars each incl. join space
    segments = split_verses_into_segments(
        verses, max_segment_chars=30,
    )
    assert [[v["verse_start"] for v in s] for s in segments] == [
        [1, 2], [3, 4], [5],
    ]


def test_single_oversized_verse_gets_own_segment():
    verses = [
        {"verse_start": 1, "verse_text": "x" * 500},
        {"verse_start": 2, "verse_text": "tiny"},
    ]
    segments = split_verses_into_segments(
        verses, max_segment_chars=30,
    )
    assert [[v["verse_start"] for v in s] for s in segments] == [
        [1], [2],
    ]


def test_is_truncated_flags_near_cap_token_count():
    result = SynthesisResult(
        pcm_bytes=b"\x00" * 100, output_tokens=16384,
    )
    assert is_truncated(result, char_count=50)


def test_is_truncated_flags_implausible_chars_per_sec():
    # 1000 chars over 1 byte of PCM -> way over 15 chars/sec.
    result = SynthesisResult(pcm_bytes=b"\x00", output_tokens=None)
    assert is_truncated(result, char_count=1000)


def test_is_truncated_passes_normal_response():
    result = SynthesisResult(
        pcm_bytes=b"\x00" * 100, output_tokens=200,
    )
    assert not is_truncated(result, char_count=100)


def test_run_concatenates_segments_and_anchors_timestamps():
    # 3 verses of 14 chars -> 2 segments at cap 30.
    client = _FakeGemini()
    synth = _synth(
        _verses(3), client, max_segment_chars=30,
    )

    artifacts = synth.run("LVSGLU8", "JHN", 3)

    assert len(client.calls) == 2
    # Segment texts hold the joined verse bodies.
    assert client.calls[0].startswith("v1 ")
    assert "v3 " in client.calls[1]
    data = artifacts.timestamps_payload["data"]
    assert [d["verse_start"] for d in data] == [1, 2, 3]
    # Fake PCM = len(text) bytes at 10 bytes/sec; verse 2 sits at
    # half of segment 1's verse chars (13/26).
    seg1_len = len(client.calls[0])
    assert data[0]["timestamp"] == 0.0
    assert data[1]["timestamp"] == round(
        0.5 * (seg1_len / 10), 3,
    )
    # Segment 2's first verse is an exact anchor at seg1's duration.
    assert data[2]["timestamp"] == round(seg1_len / 10, 3)
    assert artifacts.mp3_bytes.startswith(b"MP3(")
    assert artifacts.duration_seconds == round(
        (seg1_len + len(client.calls[1])) / 10, 3,
    )


def test_style_prompt_is_prepended_to_segment_text():
    client = _FakeGemini()
    synth = GeminiSynthesizer(
        client=client, style_prompt="Read solemnly:",
        max_segment_chars=3000,
    )
    synth._sword = _FakeSword(_verses(1))

    synth.run("LVSGLU8", "JHN", 3)

    assert client.calls[0].startswith("Read solemnly:\nv1 ")


def test_truncated_segment_splits_in_half_and_retries():
    """Output tokens at the 16k cap mean truncation even though
    finishReason would say STOP — the segment halves and retries,
    and the split point becomes an exact timestamp anchor."""
    client = _FakeGemini(
        output_tokens=lambda text: (
            16384 if len(text) > 100 else 100
        ),
    )
    synth = _synth(
        _verses(4, text_len=30), client,
        max_segment_chars=10000,
    )

    artifacts = synth.run("LVSGLU8", "JHN", 3)

    # 1 truncated call on the whole chapter, then 1 per half.
    assert len(client.calls) == 3
    data = artifacts.timestamps_payload["data"]
    assert [d["verse_start"] for d in data] == [1, 2, 3, 4]
    # Verse 3 starts the second half — exact anchor at the left
    # half's measured duration (left text len / 10 bytes per sec).
    left_len = len(client.calls[1])
    assert data[2]["timestamp"] == round(left_len / 10, 3)


def test_retry_budget_bounds_requests_on_bad_chapter():
    client = _FakeGemini(output_tokens=16384)
    synth = _synth(
        _verses(8, text_len=30), client,
        max_segment_chars=10000, max_retries=2,
    )

    with pytest.raises(SegmentTruncatedError):
        synth.run("LVSGLU8", "JHN", 3)

    # 1 initial call + at most a few retries — the budget caps
    # total spend instead of exploring the full split tree.
    assert client.requests_made <= 7


def test_request_budget_caps_mid_chapter_spend():
    """The daily-cap backstop: a chapter that keeps splitting can
    never spend more API calls than ``request_budget`` allows,
    so remaining headroom is a hard bound, not a suggestion."""
    client = _FakeGemini(output_tokens=16384)  # always truncated
    synth = _synth(
        _verses(8, text_len=30), client,
        max_segment_chars=10000, max_retries=10,
    )

    with pytest.raises(RequestBudgetExceeded):
        synth.run("LVSGLU8", "JHN", 3, request_budget=3)

    assert client.requests_made == 3


def test_request_budget_spans_segments_not_just_splits():
    """The budget counts every API call — a plain multi-segment
    chapter stops mid-way too, not only during retry splits."""
    client = _FakeGemini()
    synth = _synth(
        _verses(6, text_len=10), client,
        max_segment_chars=30,  # 3 segments of 2 verses
    )

    with pytest.raises(RequestBudgetExceeded):
        synth.run("LVSGLU8", "JHN", 3, request_budget=2)

    assert client.requests_made == 2


def test_request_budget_zero_makes_no_calls():
    client = _FakeGemini()
    synth = _synth(
        _verses(2), client, max_segment_chars=30,
    )

    with pytest.raises(RequestBudgetExceeded):
        synth.run("LVSGLU8", "JHN", 3, request_budget=0)

    assert client.requests_made == 0


def test_default_max_retries_bounds_worst_case_chapter():
    """Regression for the review finding: the default retry cap
    must keep a pathological single-segment chapter near ~7
    requests (1 + 2 per split), not ~13."""
    from bible.services.gemini_tts import synthesizer as m
    assert m.DEFAULT_MAX_RETRIES == 3


def test_single_truncated_verse_cannot_split_raises():
    client = _FakeGemini(output_tokens=16384)
    synth = _synth(
        _verses(1, text_len=30), client,
        max_segment_chars=10000, max_retries=4,
    )

    with pytest.raises(SegmentTruncatedError):
        synth.run("LVSGLU8", "JHN", 3)
    assert client.requests_made == 1


def test_unknown_fileset_raises():
    synth = _synth(_verses(1), _FakeGemini())
    with pytest.raises(ValueError, match="Unknown SWORD fileset"):
        synth.run("NOTABIBLE", "JHN", 3)
