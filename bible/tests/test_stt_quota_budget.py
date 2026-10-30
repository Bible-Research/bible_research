from io import StringIO
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.test import override_settings

from bible.services.google_stt.budget import (
    BudgetExceeded,
    SecondsBudget,
)


_CMD = "bible.management.commands.generate_audio_timestamps"


def test_seconds_budget_basic():
    b = SecondsBudget(remaining=100)
    assert b.can_afford(60)
    b.consume(60)
    assert b.remaining == 40
    assert b.used == 60
    with pytest.raises(BudgetExceeded):
        b.consume(50)


def _fake_dbt(durations=(300,)):
    """DBT client mock: ``rows`` of chapter files with the given
    durations, no DBT timestamps, one MP3 download stub."""
    dbt = MagicMock()
    dbt.get_fileset_files.return_value = {
        "data": [
            {
                "book_id": "JHN",
                "chapter_start": i + 1,
                "path": f"https://x/jhn{i + 1}.mp3",
                "duration": dur,
                "filesize_in_bytes": 1000,
            }
            for i, dur in enumerate(durations)
        ]
    }
    dbt.get_timestamps.return_value = {"data": []}
    resp = MagicMock()
    resp.content = b"mp3"
    dbt.session.get.return_value = resp
    return dbt


def _fake_sword():
    sword = MagicMock()
    sword.get_chapter_verses.return_value = [
        {"verse_start": 1, "verse_text": "In the beginning God"},
        {"verse_start": 2, "verse_text": "and the earth was"},
    ]
    return sword


def _fake_stt():
    stt = MagicMock()
    stt.long_running_recognize_uri.return_value = [
        ("In", 0.0, 0.3), ("the", 0.3, 0.5),
        ("beginning", 0.5, 1.0), ("God", 1.0, 1.5),
        ("and", 2.0, 2.3), ("earth", 2.5, 3.0),
    ]
    return stt


@override_settings(
    MONTHLY_STT_SECONDS_LIMIT=350,
    LOCK_STALE_HOURS=24,
    STT_LANGUAGE_CODE="lv-LV",
    STT_MODEL="default",
    STT_LRO_TIMEOUT_SECONDS=60,
)
@patch(f"{_CMD}.gcs.mark_stt_run_complete")
@patch(f"{_CMD}.gcs.increment_stt_monthly_usage")
@patch(f"{_CMD}.gcs.read_stt_monthly_usage")
@patch(f"{_CMD}.gcs.acquire_stt_run_lock")
@patch(f"{_CMD}.gcs.list_generated_chapters")
@patch(f"{_CMD}.gcs.delete_stt_staging_audio")
@patch(f"{_CMD}.gcs.upload_stt_staging_audio")
@patch(f"{_CMD}.gcs.upload_generated_timestamps")
@patch(f"{_CMD}.get_default_sword_client")
@patch(f"{_CMD}.get_default_stt_client")
@patch(f"{_CMD}.get_default_dbt_client")
def test_command_stops_when_budget_too_small_for_next_chapter(
    get_dbt, get_stt, get_sword, upload_ts, stage, unstage,
    list_done, acquire, read_usage, increment, mark_done,
):
    """First chapter fits (300 <= 350), the second (300) does not
    fit the remaining 50s — the run stops budget_exhausted and
    the billed seconds are persisted."""
    get_dbt.return_value = _fake_dbt(durations=(300, 300))
    get_stt.return_value = _fake_stt()
    get_sword.return_value = _fake_sword()
    acquire.return_value = (True, "acquired")
    read_usage.return_value = 0
    list_done.return_value = set()
    stage.return_value = "gs://b/stt_staging/x.mp3"

    out = StringIO()
    call_command(
        "generate_audio_timestamps",
        "--fileset-ids", "LATLVRN1DA", stdout=out,
    )

    assert upload_ts.call_count == 1
    assert increment.call_count == 1
    assert increment.call_args.args[0] == 300
    assert "budget_exhausted" in out.getvalue()
    mark_done.assert_called_once()
    assert (
        mark_done.call_args.kwargs["reason"] == "budget_exhausted"
    )
