"""Client for the Edge browser read-aloud TTS endpoint (edge-tts).

Free and keyless: ``edge_tts.Communicate`` opens a WebSocket to the
same endpoint the Edge browser's read-aloud feature uses. Output is
fixed to ``audio-24khz-48kbitrate-mono-mp3`` (48 kbps CBR); the API
ignores other outputFormat values, so callers always get MP3 frames.
CBR makes byte length a reliable duration estimate.
"""
import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)

DEFAULT_VOICE_NAME = "lv-LV-EveritaNeural"
DEFAULT_MAX_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 2.0
# edge-tts output is a 48 kbps CBR MP3 stream — byte length maps
# linearly to playback duration.
MP3_BITRATE_BPS = 48000


class EdgeTTSError(Exception):
    """The endpoint rejected the request or returned no audio."""


@dataclass
class EdgeSynthesisResult:
    """One successful call: MP3 bytes plus the stream duration."""
    mp3_bytes: bytes
    duration_seconds: float


def mp3_duration_seconds(mp3_bytes: bytes) -> float:
    """CBR byte-length duration estimate (±a frame or two)."""
    return len(mp3_bytes) * 8 / MP3_BITRATE_BPS


async def _stream_once(
    text: str,
    voice_name: str,
) -> EdgeSynthesisResult:
    """Single edge-tts request; returns the MP3 byte stream."""
    import edge_tts

    communicate = edge_tts.Communicate(text, voice_name)
    mp3 = b""
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            mp3 += chunk["data"]
    if not mp3:
        raise EdgeTTSError("edge-tts returned no audio chunks")
    return EdgeSynthesisResult(
        mp3_bytes=mp3,
        duration_seconds=mp3_duration_seconds(mp3),
    )


class EdgeTTSClient:
    """Thin retry wrapper over ``edge_tts.Communicate``.

    The unofficial endpoint occasionally answers a well-formed
    request with ``NoAudioReceived`` or drops the WebSocket, so
    every call retries a few times with a short backoff. There is
    no API key and no formal quota — courtesy pacing lives in the
    synthesizer (``min_interval_seconds``), not here.
    """

    def __init__(
        self,
        voice_name: str = DEFAULT_VOICE_NAME,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        sleep_fn: Callable[[float], None] = time.sleep,
    ):
        self._voice_name = voice_name
        self._max_attempts = max_attempts
        self._sleep = sleep_fn
        # Requests actually sent — retries included — so callers
        # can log real spend against endpoint throttling.
        self.requests_made = 0

    def synthesize(
        self,
        text: str,
        voice_name: Optional[str] = None,
    ) -> EdgeSynthesisResult:
        """Synthesize ``text`` to MP3 bytes + stream duration.

        Raises :class:`EdgeTTSError` after ``max_attempts`` failed
        tries."""
        last_exc: Optional[Exception] = None
        for attempt in range(1, self._max_attempts + 1):
            self.requests_made += 1
            try:
                return asyncio.run(
                    _stream_once(
                        text, voice_name or self._voice_name,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                # edge-tts raises a grab-bag (NoAudioReceived,
                # aiohttp/WSS errors); all warrant a retry.
                last_exc = exc
                logger.warning(
                    "edge-tts attempt %d/%d failed: %s",
                    attempt, self._max_attempts,
                    type(exc).__name__,
                )
                if attempt < self._max_attempts:
                    self._sleep(RETRY_BACKOFF_SECONDS)
        raise EdgeTTSError(
            f"edge-tts failed after {self._max_attempts} "
            f"attempts: {type(last_exc).__name__}"
        ) from last_exc
