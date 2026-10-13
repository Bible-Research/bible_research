"""Thin wrapper around google.cloud.texttospeech (v1) and
texttospeech_v1beta1 (SSML-mark timepoints)."""
import threading
from typing import List, Optional, Tuple

from google.api_core import exceptions as gax
from google.cloud import texttospeech
from google.cloud import texttospeech_v1beta1


class QuotaExceeded(Exception):
    """Raised when Cloud TTS reports the project's quota is depleted."""


class GoogleTTSClient:
    def __init__(
        self,
        client: Optional[texttospeech.TextToSpeechClient] = None,
        beta_client: Optional[
            texttospeech_v1beta1.TextToSpeechClient
        ] = None,
    ):
        self._client = client or texttospeech.TextToSpeechClient()
        # Built lazily: the v1beta1 client is only needed for
        # ``synthesize_ssml`` (v1 has no ``enable_time_pointing``),
        # so verse-only runs should not pay for a second client.
        self._beta_client = beta_client

    def _beta(self) -> texttospeech_v1beta1.TextToSpeechClient:
        if self._beta_client is None:
            self._beta_client = (
                texttospeech_v1beta1.TextToSpeechClient()
            )
        return self._beta_client

    def synthesize_text(
        self,
        text: str,
        language_code: str,
        voice_name: str,
        sample_rate_hertz: int = 24000,
    ) -> bytes:
        request = texttospeech.SynthesizeSpeechRequest(
            input=texttospeech.SynthesisInput(text=text),
            voice=texttospeech.VoiceSelectionParams(
                language_code=language_code,
                name=voice_name,
            ),
            audio_config=texttospeech.AudioConfig(
                audio_encoding=texttospeech.AudioEncoding.MP3,
                sample_rate_hertz=sample_rate_hertz,
            ),
        )
        try:
            response = self._client.synthesize_speech(request=request)
        except gax.ResourceExhausted as exc:
            raise QuotaExceeded(str(exc)) from exc
        return response.audio_content

    def synthesize_ssml(
        self,
        ssml: str,
        language_code: str,
        voice_name: str,
        sample_rate_hertz: int = 24000,
    ) -> Tuple[bytes, List[dict]]:
        """Synthesize an SSML document; return ``(mp3, timepoints)``.

        Uses the v1beta1 API: ``enable_time_pointing`` is a
        request-level field that exists only there (the v1
        ``AudioConfig`` has no such field and the v1 response has
        no ``timepoints``). With ``SSML_MARK`` every
        ``<mark name="..."/>`` in the input comes back in
        ``response.timepoints`` as a ``mark_name -> time_seconds``
        offset. Timepoints are returned as plain dicts so tests can
        fake them without the proto-plus types."""
        beta = texttospeech_v1beta1
        request = beta.SynthesizeSpeechRequest(
            input=beta.SynthesisInput(ssml=ssml),
            voice=beta.VoiceSelectionParams(
                language_code=language_code,
                name=voice_name,
            ),
            audio_config=beta.AudioConfig(
                audio_encoding=beta.AudioEncoding.MP3,
                sample_rate_hertz=sample_rate_hertz,
            ),
            enable_time_pointing=[
                beta.SynthesizeSpeechRequest.TimepointType.SSML_MARK,
            ],
        )
        try:
            response = self._beta().synthesize_speech(request=request)
        except gax.ResourceExhausted as exc:
            raise QuotaExceeded(str(exc)) from exc
        timepoints = [
            {
                "mark_name": tp.mark_name,
                "time_seconds": tp.time_seconds,
            }
            for tp in response.timepoints
        ]
        return response.audio_content, timepoints


_default: Optional[GoogleTTSClient] = None
_default_lock = threading.Lock()


def get_default_tts_client() -> GoogleTTSClient:
    global _default
    if _default is None:
        with _default_lock:
            if _default is None:
                _default = GoogleTTSClient()
    return _default
