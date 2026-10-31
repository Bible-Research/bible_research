"""Unit tests for google_stt.client internals — no real GCP
calls; ``SpeechClient`` is replaced by mocks."""
import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from bible.services.google_stt.client import (
    BilledRecognizeError,
    GoogleSTTClient,
    _seconds,
)


class _ProtoDuration:
    """Old-style protobuf Duration stand-in (``.seconds`` +
    ``.nanos``)."""
    seconds = 5
    nanos = 250000000


def test_seconds_accepts_timedelta():
    """google-cloud-speech returns ``datetime.timedelta`` for
    word offsets — the shape observed in production."""
    delta = datetime.timedelta(seconds=2, microseconds=500000)
    assert _seconds(delta) == 2.5


def test_seconds_accepts_protobuf_duration():
    assert _seconds(_ProtoDuration()) == 5.25


def test_parse_error_raises_billed_recognize_error():
    """A word row whose offsets cannot be converted surfaces as
    ``BilledRecognizeError`` — the LRO was created (and billed),
    so the caller must meter the chapter's seconds."""
    operation = MagicMock()
    bad_word = SimpleNamespace(
        word="x", start_time=object(), end_time=object(),
    )
    operation.result.return_value = SimpleNamespace(
        results=[
            SimpleNamespace(
                alternatives=[SimpleNamespace(words=[bad_word])],
            ),
        ],
    )
    speech_client = MagicMock()
    speech_client.long_running_recognize.return_value = operation
    client = GoogleSTTClient(client=speech_client)
    with pytest.raises(BilledRecognizeError):
        client.long_running_recognize_uri(
            "gs://b/x.mp3",
            language_code="lv-LV",
            model="default",
            timeout_seconds=1,
        )
