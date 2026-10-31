"""Chapter -> per-verse edge-tts calls -> concatenated MP3.

Synthesizes each verse with its own request and byte-concatenates
the MP3 frames (edge-tts output is 48 kbps CBR, and MP3 frames are
self-contained, so plain concatenation decodes as one stream).
Because every verse is a standalone request, verse timestamps are
EXACT — each verse's start offset is the cumulative stream
duration of the MP3 bytes before it, not a char-proportional
estimate like the Gemini pipeline has to fall back on.
"""
import logging
import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from bible.services.sword.client import get_default_sword_client
from bible.services.sword.registry import canonical_sword_fileset_id

from .client import (
    DEFAULT_VOICE_NAME,
    EdgeSynthesisResult,
    EdgeTTSClient,
)

logger = logging.getLogger(__name__)


@dataclass
class EdgeChapterArtifacts:
    mp3_bytes: bytes
    timestamps_payload: dict
    duration_seconds: float
    chars_used: int


class EdgeSynthesizer:
    """Synthesize a whole chapter to MP3 + exact verse timestamps
    via the Edge read-aloud endpoint."""

    def __init__(
        self,
        client: Optional[EdgeTTSClient] = None,
        voice_name: Optional[str] = None,
        min_interval_seconds: float = 0.0,
        sleep_fn: Callable[[float], None] = time.sleep,
        monotonic_fn: Callable[[], float] = time.monotonic,
    ):
        self._client = client or EdgeTTSClient()
        self._voice_name = voice_name or DEFAULT_VOICE_NAME
        self._sword = get_default_sword_client()
        self._min_interval = min_interval_seconds
        self._sleep = sleep_fn
        self._monotonic = monotonic_fn
        self._last_request_at: Optional[float] = None

    def run(
        self,
        fileset_id: str,
        book_id: str,
        chapter: int,
    ) -> EdgeChapterArtifacts:
        """Synthesize one chapter; raises on the first failed
        verse (the caller logs and moves to the next chapter)."""
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

        rendered: List[Tuple[dict, EdgeSynthesisResult]] = []
        for verse in verses:
            self._pace()
            result = self._client.synthesize(
                text=verse["verse_text"],
                voice_name=self._voice_name,
            )
            rendered.append((verse, result))

        mp3_bytes = b"".join(r.mp3_bytes for _v, r in rendered)
        duration = sum(r.duration_seconds for _v, r in rendered)
        return EdgeChapterArtifacts(
            mp3_bytes=mp3_bytes,
            timestamps_payload={
                "duration_seconds": round(duration, 3),
                "data": self._build_timestamps(rendered),
                "file_size_bytes": len(mp3_bytes),
            },
            duration_seconds=duration,
            chars_used=sum(len(v["verse_text"]) for v in verses),
        )

    def _pace(self) -> None:
        """Courtesy spacing between requests — the endpoint is
        undocumented, so bursts are the likeliest throttle trigger."""
        if self._min_interval <= 0 or self._last_request_at is None:
            self._last_request_at = self._monotonic()
            return
        wait = (
            self._min_interval
            - (self._monotonic() - self._last_request_at)
        )
        if wait > 0:
            self._sleep(wait)
        self._last_request_at = self._monotonic()

    @staticmethod
    def _build_timestamps(
        rendered: List[Tuple[dict, EdgeSynthesisResult]],
    ) -> List[dict]:
        """Exact per-verse offsets from measured stream
        durations — no estimation needed."""
        timestamps: List[dict] = []
        cumulative = 0.0
        for verse, result in rendered:
            timestamps.append({
                "verse_start": verse["verse_start"],
                "timestamp": round(cumulative, 3),
            })
            cumulative += result.duration_seconds
        return timestamps
