from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient


@pytest.fixture
def auth_client(db):
    User = get_user_model()
    user, _ = User.objects.get_or_create(username="audiotest")
    user.set_password("x")
    user.save()
    c = APIClient()
    c.force_authenticate(user=user)
    return c


@patch("bible.serializers.get_tts_config")
@patch("bible.serializers.gcs.read_timestamps_json")
@patch("bible.serializers.gcs.signed_audio_url")
@patch("bible.serializers.gcs.chapter_audio_exists")
@patch("bible.serializers.gcs.get_default_client")
@pytest.mark.django_db
def test_audio_response_returns_signed_url(
    get_client, exists, signed, read_ts, tts_cfg, auth_client,
):
    tts_cfg.return_value = {
        "voice_name": "lv-LV-Chirp3-HD-Sadachbia",
    }
    exists.return_value = True
    signed.return_value = "https://signed.example/audio.mp3"
    read_ts.return_value = {"duration_seconds": 412.87, "data": []}

    blob = type("B", (), {"size": 1234567})()
    bucket = type("Bk", (), {"get_blob": lambda self, p: blob})()
    client = type("Cl", (), {"bucket": lambda self, n: bucket})()
    get_client.return_value = client

    resp = auth_client.get(
        "/api/v1/bible/",
        {"passage": "Luke 20", "fileset_id": "GLU8",
         "response_format": "audio"},
    )
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["format"] == "audio"
    assert body["audio_url"] == "https://signed.example/audio.mp3"
    assert body["duration_seconds"] == 412.87
    assert body["file_size_bytes"] == 1234567
