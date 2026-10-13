"""Request-construction tests for ``GoogleTTSClient``.

Chapter mode depends on the v1beta1 request-level
``enable_time_pointing=[SSML_MARK]`` field — the v1 API has no
equivalent and its response has no ``timepoints`` — so the client
must build a v1beta1 request and call the v1beta1 client. These
tests pin that contract plus the ``response.timepoints`` parsing.
"""
from unittest.mock import MagicMock

from google.cloud import texttospeech_v1beta1

from bible.services.google_tts.client import GoogleTTSClient


def test_synthesize_ssml_requests_v1beta1_mark_timepoints():
    v1_client = MagicMock()
    beta_client = MagicMock()
    tp = MagicMock(mark_name="v1", time_seconds=1.5)
    beta_client.synthesize_speech.return_value = MagicMock(
        audio_content=b"\xff\xfb" + b"\x00" * 10,
        timepoints=[tp],
    )
    client = GoogleTTSClient(
        client=v1_client, beta_client=beta_client,
    )

    audio, timepoints = client.synthesize_ssml(
        ssml='<speak><mark name="v1"/>Sveiki</speak>',
        language_code="lv-LV",
        voice_name="lv-LV-Chirp3-HD-Sadachbia",
        sample_rate_hertz=24000,
    )

    v1_client.synthesize_speech.assert_not_called()
    request = beta_client.synthesize_speech.call_args.kwargs[
        "request"
    ]
    assert isinstance(
        request, texttospeech_v1beta1.SynthesizeSpeechRequest
    )
    # ``enable_time_pointing`` is a request-level field in
    # v1beta1, not an AudioConfig field.
    assert list(request.enable_time_pointing) == [
        texttospeech_v1beta1.SynthesizeSpeechRequest
        .TimepointType.SSML_MARK
    ]
    assert request.input.ssml.startswith("<speak>")
    assert request.audio_config.audio_encoding == (
        texttospeech_v1beta1.AudioEncoding.MP3
    )
    assert request.audio_config.sample_rate_hertz == 24000

    assert audio.startswith(b"\xff\xfb")
    assert timepoints == [
        {"mark_name": "v1", "time_seconds": 1.5},
    ]
