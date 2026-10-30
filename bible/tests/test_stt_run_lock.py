from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import override_settings


_CMD = "bible.management.commands.generate_audio_timestamps"


@override_settings(LOCK_STALE_HOURS=24)
@patch(f"{_CMD}.get_default_stt_client")
@patch(f"{_CMD}.get_default_dbt_client")
@patch(f"{_CMD}.gcs.list_generated_chapters")
@patch(f"{_CMD}.gcs.acquire_stt_run_lock")
def test_command_skips_when_lock_already_completed(
    acquire, list_done, get_dbt, get_stt,
):
    acquire.return_value = (False, "already_completed")
    out = StringIO()
    call_command(
        "generate_audio_timestamps",
        "--fileset-ids", "LATLVRN1DA", stdout=out,
    )
    assert "skipped" in out.getvalue()
    assert "already_completed" in out.getvalue()
    list_done.assert_not_called()
    get_dbt.return_value.get_fileset_files.assert_not_called()


@override_settings(LOCK_STALE_HOURS=24)
@patch(f"{_CMD}.get_default_stt_client")
@patch(f"{_CMD}.get_default_dbt_client")
@patch(f"{_CMD}.gcs.list_generated_chapters")
@patch(f"{_CMD}.gcs.acquire_stt_run_lock")
def test_command_skips_when_active_run_exists(
    acquire, list_done, get_dbt, get_stt,
):
    acquire.return_value = (False, "active_run")
    out = StringIO()
    call_command(
        "generate_audio_timestamps",
        "--fileset-ids", "LATLVRN1DA", stdout=out,
    )
    assert "skipped" in out.getvalue()
    assert "active_run" in out.getvalue()
    list_done.assert_not_called()
    get_dbt.return_value.get_fileset_files.assert_not_called()
