"""Chapter -> verse-boundary segments -> Gemini TTS -> MP3.

Splits each chapter into consecutive-verse segments under a char
cap, synthesizes each segment, concatenates the LINEAR16 PCM, and
encodes one MP3 per chapter via ``lameenc``. Gemini TTS has no
SSML-mark timepoints, so verse timestamps are char-proportional
estimates anchored exactly at measured segment boundaries.
"""
import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple

from bible.services.sword.client import get_default_sword_client
from bible.services.sword.registry import canonical_sword_fileset_id

from .client import (
    GeminiTTSClient,
    SynthesisResult,
    get_default_client,
)

logger = logging.getLogger(__name__)

# Gemini TTS returns LINEAR16 PCM at 24 kHz mono (2 bytes/sample).
SAMPLE_RATE_HERTZ = 24000
BYTES_PER_SECOND = SAMPLE_RATE_HERTZ * 2

DEFAULT_MAX_SEGMENT_CHARS = 3000

# The API hard-caps audio output at 16,384 tokens (~655 s); a
# response that lands anywhere near the cap was almost certainly
# truncated mid-verse. ``finishReason`` cannot be trusted — the
# API returns STOP even on truncated output.
TRUNCATED_OUTPUT_TOKENS = 16000

# A reverent read runs ~5 chars/sec; speech faster than this means
# the model skipped text (or the audio is corrupt), so treat it as
# truncated and re-split.
MAX_PLAUSIBLE_CHARS_PER_SECOND = 15.0

# Cap on truncation-triggered segment splits per chapter so one
# pathological chapter cannot burn the whole daily request quota.
DEFAULT_MAX_RETRIES = 6

MP3_BIT_RATE = 64

# Prepended to each segment's text so the model picks a solemn,
# measured delivery; tune once sample output has been compared.
DEFAULT_STYLE_PROMPT = (
    "Read the following Latvian Bible passage aloud in a calm, "
    "reverent, measured pace, without commentary:"
)


class SegmentTruncatedError(Exception):
    """A segment stayed truncated after the retry budget ran out."""


@dataclass
class GeminiChapterArtifacts:
    mp3_bytes: bytes
    timestamps_payload: dict
    duration_seconds: float
    chars_used: int


def pcm_duration_seconds(pcm_bytes: bytes) -> float:
    return len(pcm_bytes) / BYTES_PER_SECOND


def is_truncated(result: SynthesisResult, char_count: int) -> bool:
    """Decide whether a segment response was truncated.

    Primary signal: ``usageMetadata`` output tokens near the
    16,384-token hard cap. Fallback: a chars/sec sanity band for
    when the token count is absent — audio implausibly short for
    its text means the model cannot have read it all. Never use
    ``finishReason`` (it reports STOP even when truncated)."""
    if (
        result.output_tokens is not None
        and result.output_tokens >= TRUNCATED_OUTPUT_TOKENS
    ):
        return True
    duration = pcm_duration_seconds(result.pcm_bytes)
    if duration <= 0:
        return True
    return (
        char_count / duration > MAX_PLAUSIBLE_CHARS_PER_SECOND
    )


def split_verses_into_segments(
    verses: List[dict],
    max_segment_chars: int,
) -> List[List[dict]]:
    """Group consecutive verses into segments whose joined text
    stays under ``max_segment_chars``. An oversized single verse
    still gets its own segment (the API input cap is far larger —
    the cap only bounds output duration)."""
    segments: List[List[dict]] = []
    current: List[dict] = []
    current_chars = 0
    for verse in verses:
        verse_chars = len(verse["verse_text"]) + 1  # join space
        if current and current_chars + verse_chars > (
            max_segment_chars
        ):
            segments.append(current)
            current = []
            current_chars = 0
        current.append(verse)
        current_chars += verse_chars
    if current:
        segments.append(current)
    return segments


def encode_pcm_to_mp3(
    pcm_bytes: bytes,
    sample_rate: int = SAMPLE_RATE_HERTZ,
) -> bytes:
    """Encode raw LINEAR16 PCM to MP3 via lameenc (no ffmpeg)."""
    import lameenc

    encoder = lameenc.Encoder()
    encoder.set_bit_rate(MP3_BIT_RATE)
    encoder.set_in_sample_rate(sample_rate)
    encoder.set_channels(1)
    encoder.set_quality(2)
    return bytes(encoder.encode(pcm_bytes) + encoder.flush())


class _RetryBudget:
    """Shared countdown of truncation splits allowed per chapter."""

    def __init__(self, retries: int):
        self.left = retries

    def spend(self) -> bool:
        if self.left <= 0:
            return False
        self.left -= 1
        return True


class GeminiSynthesizer:
    """Synthesize a whole chapter to MP3 + estimated verse
    timestamps via the Gemini free-tier TTS endpoint."""

    def __init__(
        self,
        client: Optional[GeminiTTSClient] = None,
        max_segment_chars: int = DEFAULT_MAX_SEGMENT_CHARS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        style_prompt: str = DEFAULT_STYLE_PROMPT,
        voice_name: Optional[str] = None,
    ):
        self._client = client or get_default_client()
        self._sword = get_default_sword_client()
        self._max_segment_chars = max_segment_chars
        self._max_retries = max_retries
        self._style_prompt = style_prompt
        self._voice_name = voice_name

    def run(
        self,
        fileset_id: str,
        book_id: str,
        chapter: int,
    ) -> GeminiChapterArtifacts:
        canon = canonical_sword_fileset_id(fileset_id)
        if not canon:
            raise ValueError(f"Unknown SWORD fileset: {fileset_id!r}")
        verses = self._sword.get_chapter_verses(
            canon, book_id, chapter,
        )
        if not verses:
            raise ValueError(
                f"No verses for {book_id} {chapter} in {canon}"
            )

        segments = split_verses_into_segments(
            verses, self._max_segment_chars,
        )
        retry_budget = _RetryBudget(self._max_retries)

        # (verses, PCM) pairs in synthesis order — every actual
        # request boundary is an exact timestamp anchor, including
        # sub-segments created by truncation splits.
        rendered: List[Tuple[List[dict], bytes]] = []
        for segment in segments:
            rendered.extend(
                self._synthesize_segment(segment, retry_budget)
            )

        full_pcm = b"".join(pcm for _seg, pcm in rendered)
        mp3_bytes = encode_pcm_to_mp3(full_pcm)
        duration = pcm_duration_seconds(full_pcm)
        chars_used = sum(len(v["verse_text"]) for v in verses)

        return GeminiChapterArtifacts(
            mp3_bytes=mp3_bytes,
            timestamps_payload={
                "duration_seconds": round(duration, 3),
                "data": self._build_timestamps(rendered),
                "file_size_bytes": len(mp3_bytes),
            },
            duration_seconds=duration,
            chars_used=chars_used,
        )

    def _segment_text(self, segment: List[dict]) -> str:
        body = " ".join(v["verse_text"] for v in segment)
        if self._style_prompt:
            return f"{self._style_prompt}\n{body}"
        return body

    def _synthesize_segment(
        self,
        segment: List[dict],
        retry_budget: _RetryBudget,
    ) -> List[Tuple[List[dict], bytes]]:
        """Synthesize one segment; on truncation split it in half
        and retry each half, bounded by the shared retry budget.

        Returns ``(verses, pcm)`` pairs — one per actual API call —
        so every synthesized boundary can anchor timestamps."""
        text = self._segment_text(segment)
        result = self._client.synthesize(
            text=text, voice_name=self._voice_name,
        )
        if not is_truncated(result, len(text)):
            return [(segment, result.pcm_bytes)]

        if len(segment) == 1 or not retry_budget.spend():
            raise SegmentTruncatedError(
                f"segment starting at verse "
                f"{segment[0]['verse_start']} stayed truncated "
                f"(retries left: {retry_budget.left})"
            )
        mid = len(segment) // 2
        logger.warning(
            "Segment of %d verses was truncated; splitting at "
            "verse %d and retrying.",
            len(segment), segment[mid]["verse_start"],
        )
        left = self._synthesize_segment(
            segment[:mid], retry_budget,
        )
        right = self._synthesize_segment(
            segment[mid:], retry_budget,
        )
        return left + right

    @staticmethod
    def _build_timestamps(
        rendered: List[Tuple[List[dict], bytes]],
    ) -> List[dict]:
        """Char-proportional verse timestamps per segment.

        Each segment's start is an exact anchor (its PCM duration
        is measured); verses inside a segment are spread across the
        segment's duration in proportion to their char length."""
        timestamps: List[dict] = []
        cumulative = 0.0
        for segment, pcm in rendered:
            seg_duration = pcm_duration_seconds(pcm)
            seg_chars = sum(
                len(v["verse_text"]) for v in segment
            )
            offset_chars = 0
            for verse in segment:
                fraction = (
                    offset_chars / seg_chars if seg_chars else 0.0
                )
                timestamps.append({
                    "verse_start": verse["verse_start"],
                    "timestamp": round(
                        cumulative + fraction * seg_duration, 3,
                    ),
                })
                offset_chars += len(verse["verse_text"])
            cumulative += seg_duration
        return timestamps
