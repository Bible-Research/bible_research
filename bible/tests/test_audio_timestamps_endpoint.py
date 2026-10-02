from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient


@pytest.fixture
def auth_client(db):
    User = get_user_model()
    user, _ = User.objects.get_or_create(username="tsendpoint")
    user.set_password("x")
    user.save()
    c = APIClient()
    c.force_authenticate(user=user)
    return c


@patch("bible.views.get_tts_config")
@patch("bible.views.gcs.read_timestamps_json")
@pytest.mark.django_db
def test_sword_timestamps_served_from_gcs(
    read_ts, tts_cfg, auth_client,
):
    tts_cfg.return_value = {
        "voice_name": "lv-LV-Chirp3-HD-Sadachbia",
    }
    read_ts.return_value = {
        "duration_seconds": 60.0,
        "data": [
            {"verse_start": 1, "timestamp": 0.0},
            {"verse_start": 2, "timestamp": 5.34},
        ],
    }
    resp = auth_client.get(
        "/api/v1/bible/timestamps/",
        {"fileset_id": "GLU8", "book": "John", "chapter": "3"},
    )
    assert resp.status_code == 200, resp.content
    assert resp.json()["data"][1]["timestamp"] == 5.34


@patch("bible.views.get_tts_config")
@patch("bible.views.gcs.read_timestamps_json")
@pytest.mark.django_db
def test_sword_timestamps_gcs_failure_surfaces_provider_error(
    read_ts, tts_cfg, auth_client,
):
    """A non-NotFound GCS failure returns provider error fields —
    raw exception text must not leak into the body."""
    tts_cfg.return_value = {
        "voice_name": "lv-LV-Chirp3-HD-Sadachbia",
    }
    read_ts.side_effect = Exception(
        "https://storage.googleapis.com/x?token=SECRET"
    )
    resp = auth_client.get(
        "/api/v1/bible/timestamps/",
        {"fileset_id": "GLU8", "book": "John", "chapter": "3"},
    )
    assert resp.status_code == 502
    body = resp.json()
    assert body["error_code"] == "provider_error"
    assert "SECRET" not in body["error"]
