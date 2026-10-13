"""Thin wrapper around google.cloud.texttospeech."""
import threading
from typing import List, Optional, Tuple

from google.api_core import exceptions as gax
from google.cloud import texttospeech


class QuotaExceeded(Exception):
    """Raised when Cloud TTS reports the project's quota is depleted."""


class GoogleTTSClient:
    def __init__(
        self,
        client: Optional[texttospeech.TextToSpeechClient] = None,
    ):
        self._client = client or texttospeech.TextToSpeechClient()

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

        Requests ``SSML_MARK`` time pointing so every
        ``<mark name="..."/>`` in the input comes back in
        ``response.timepoints`` as a ``mark_name -> time_seconds``
        offset. Timepoints are returned as plain dicts so tests can
        fake them without the proto-plus types."""
        request = texttospeech.SynthesizeSpeechRequest(
            input=texttospeech.SynthesisInput(ssml=ssml),
            voice=texttospeech.VoiceSelectionParams(
                language_code=language_code,
                name=voice_name,
            ),
            audio_config=texttospeech.AudioConfig(
                audio_encoding=texttospeech.AudioEncoding.MP3,
                sample_rate_hertz=sample_rate_hertz,
                enable_time_pointing=[
                    texttospeech.AudioConfig.TimepointType.SSML_MARK,
                ],
            ),
        )
        try:
            response = self._client.synthesize_speech(request=request)
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
