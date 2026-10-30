"""Thin wrapper around google.cloud.speech (V1).

V1 is deliberate: the 60 min/month free tier exists only on the
V1 API (``speech.SpeechClient``) — V2/Chirp has no free tier.
"""
import threading
from typing import List, Optional, Tuple

from google.api_core import exceptions as gax
from google.cloud import speech


class QuotaExceeded(Exception):
    """Raised when Cloud STT reports the project's quota is depleted."""


def _seconds(duration) -> float:
    """Convert a protobuf Duration to float seconds."""
    return duration.seconds + duration.nanos / 1e9


class GoogleSTTClient:
    def __init__(
        self,
        client: Optional[speech.SpeechClient] = None,
    ):
        self._client = client or speech.SpeechClient()

    def long_running_recognize_uri(
        self,
        gs_uri: str,
        language_code: str,
        model: str,
        timeout_seconds: Optional[int] = None,
    ) -> List[Tuple[str, float, float]]:
        """Transcribe a ``gs://`` MP3; return ``[(word, start_s,
        end_s), ...]`` in audio order.

        Chapters exceed the 1-minute sync limit, so this always
        uses ``long_running_recognize`` with a GCS-staged object.
        ``sample_rate_hertz`` is intentionally unset — STT infers
        it from the MP3 header (an explicit wrong rate breaks
        recognition).

        Raises ``QuotaExceeded`` on ``ResourceExhausted``; every
        other failure propagates unchanged so the caller can
        apply its generic per-chapter handling. Billing counts
        every *successfully processed* request — including an
        empty result — so the caller consumes the chapter's full
        duration once the LRO completes.
        """
        config = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.MP3,
            language_code=language_code,
            enable_word_time_offsets=True,
            model=model,
        )
        audio = speech.RecognitionAudio(uri=gs_uri)
        try:
            operation = self._client.long_running_recognize(
                config=config, audio=audio,
            )
            response = operation.result(timeout=timeout_seconds)
        except gax.ResourceExhausted as exc:
            raise QuotaExceeded(str(exc)) from exc

        words: List[Tuple[str, float, float]] = []
        for result in response.results or []:
            alternatives = result.alternatives or []
            if not alternatives:
                continue
            for w in alternatives[0].words or []:
                words.append(
                    (w.word, _seconds(w.start_time),
                     _seconds(w.end_time))
                )
        return words


_default: Optional[GoogleSTTClient] = None
_default_lock = threading.Lock()


def get_default_stt_client() -> GoogleSTTClient:
    global _default
    if _default is None:
        with _default_lock:
            if _default is None:
                _default = GoogleSTTClient()
    return _default
