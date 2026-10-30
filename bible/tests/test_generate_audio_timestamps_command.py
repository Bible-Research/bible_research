from contextlib import ExitStack
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from bible.services.google_stt.client import QuotaExceeded


_CMD = "bible.management.commands.generate_audio_timestamps"

_SETTINGS = dict(
    MONTHLY_STT_SECONDS_LIMIT=3300,
    LOCK_STALE_HOURS=24,
    STT_LANGUAGE_CODE="lv-LV",
    STT_MODEL="default",
    STT_LRO_TIMEOUT_SECONDS=60,
)

_ROWS = [
    {
        "book_id": "JHN",
        "chapter_start": 3,
        "path": "https://x/jhn3.mp3",
        "duration": 300,
        "filesize_in_bytes": 2500000,
    },
    {
        "book_id": "JHN",
        "chapter_start": 4,
        "path": "https://x/jhn4.mp3",
        "duration": 200,
        "filesize_in_bytes": 1600000,
    },
]

_STT_WORDS = [
    ("In", 0.0, 0.3), ("the", 0.3, 0.5),
    ("beginning", 0.5, 1.0), ("God", 1.0, 1.5),
    ("and", 2.0, 2.3), ("earth", 2.5, 3.0),
]


def _fake_dbt(rows=_ROWS, timestamps_data=None):
    """DBT mock: chapter-file rows, a timestamps answer, and a
    session whose ``get`` returns fake MP3 bytes."""
    dbt = MagicMock()
    dbt.get_fileset_files.return_value = {"data": rows}
    if timestamps_data is None:
        dbt.get_timestamps.return_value = {"data": []}
    else:
        dbt.get_timestamps.return_value = {
            "data": timestamps_data,
        }
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
    stt.long_running_recognize_uri.return_value = _STT_WORDS
    return stt


class _Run:
    """Patch every GCS helper + provider client the command
    touches, run it, and expose the mocks for assertions."""

    def __call__(self, argv, stt=None, dbt=None, sword=None):
        mocks = {}
        with ExitStack() as stack:
            for name in (
                "mark_stt_run_complete",
                "increment_stt_monthly_usage",
                "read_stt_monthly_usage",
                "acquire_stt_run_lock",
                "list_generated_chapters",
                "delete_stt_staging_audio",
                "upload_stt_staging_audio",
                "upload_generated_timestamps",
            ):
                mocks[name] = stack.enter_context(
                    patch(f"{_CMD}.gcs.{name}")
                )
            mocks["get_sword"] = stack.enter_context(
                patch(f"{_CMD}.get_default_sword_client")
            )
            mocks["get_stt"] = stack.enter_context(
                patch(f"{_CMD}.get_default_stt_client")
            )
            mocks["get_dbt"] = stack.enter_context(
                patch(f"{_CMD}.get_default_dbt_client")
            )
            mocks["acquire_stt_run_lock"].return_value = (
                True, "acquired",
            )
            mocks["read_stt_monthly_usage"].return_value = 0
            mocks["list_generated_chapters"].return_value = set()
            mocks["upload_stt_staging_audio"].return_value = (
                "gs://b/staging.mp3"
            )
            mocks["get_dbt"].return_value = (
                dbt if dbt is not None else _fake_dbt()
            )
            mocks["get_stt"].return_value = (
                stt if stt is not None else _fake_stt()
            )
            mocks["get_sword"].return_value = (
                sword if sword is not None else _fake_sword()
            )
            out = StringIO()
            with override_settings(**_SETTINGS):
                call_command(
                    "generate_audio_timestamps", *argv,
                    stdout=out,
                )
            return out.getvalue(), mocks


_run = _Run()


@override_settings(**_SETTINGS)
def test_unknown_fileset_id_is_a_command_error():
    with pytest.raises(CommandError):
        call_command(
            "generate_audio_timestamps",
            "--fileset-ids", "ENGESVN2DA",
        )


def test_happy_path_generates_and_persists_usage():
    text, m = _run(["--fileset-ids", "LATLVRN1DA"])
    assert m["upload_generated_timestamps"].call_count == 2
    # Both chapters billed at their DBT durations, persisted
    # via the global seconds counter.
    increments = m["increment_stt_monthly_usage"]
    assert increments.call_count == 2
    assert increments.call_args_list[0].args[0] == 300
    assert increments.call_args_list[1].args[0] == 200
    payload = (
        m["upload_generated_timestamps"]
        .call_args_list[0].args[3]
    )
    assert payload["data"][0]["verse_start"] == 1
    assert payload["meta"]["source"] == "stt_v1"
    assert payload["meta"]["reference_text"] == "LVSGLU8"
    assert payload["meta"]["low_confidence"] is False
    assert "reason=all_done" in text
    mark = m["mark_stt_run_complete"]
    mark.assert_called_once()
    assert mark.call_args.kwargs["seconds_used"] == 500
    # Staging lifecycle: uploaded before the LRO, deleted after.
    assert m["upload_stt_staging_audio"].call_count == 2
    assert m["delete_stt_staging_audio"].call_count == 2


def test_dbt_covered_chapters_are_skipped():
    """When DBT already serves timestamps for a chapter, the run
    must not spend STT budget on it."""
    dbt = _fake_dbt(
        timestamps_data=[{"verse_start": 1, "timestamp": 0.0}],
    )
    text, m = _run(["--fileset-ids", "LATLVRN1DA"], dbt=dbt)
    assert m["upload_generated_timestamps"].call_count == 0
    stt = m["get_stt"].return_value
    stt.long_running_recognize_uri.assert_not_called()
    assert (
        m["increment_stt_monthly_usage"].call_count == 0
    )
    assert "skipped_dbt=2" in text
    assert "reason=all_done" in text


def test_dbt_check_failure_proceeds_to_generate():
    """A non-200/exception on the DBT timestamps check means
    'proceed to generate' — a raise must not silently skip the
    chapter every run."""
    dbt = _fake_dbt()
    dbt.get_timestamps.side_effect = RuntimeError("dbt 500")
    text, m = _run(["--fileset-ids", "LATLVRN1DA"], dbt=dbt)
    assert m["upload_generated_timestamps"].call_count == 2
    assert "reason=all_done" in text


def test_quota_exhausted_stops_the_run():
    stt = _fake_stt()
    stt.long_running_recognize_uri.side_effect = QuotaExceeded(
        "over",
    )
    text, m = _run(
        ["--fileset-ids", "LATLVRN1DA"], stt=stt,
    )
    assert m["upload_generated_timestamps"].call_count == 0
    assert "reason=quota_exhausted" in text
    mark = m["mark_stt_run_complete"]
    mark.assert_called_once()
    assert mark.call_args.kwargs["reason"] == "quota_exhausted"


def test_generic_chapter_failure_continues():
    stt = _fake_stt()
    stt.long_running_recognize_uri.side_effect = [
        RuntimeError("boom"),
        _STT_WORDS,
    ]
    text, m = _run(
        ["--fileset-ids", "LATLVRN1DA"], stt=stt,
    )
    assert m["upload_generated_timestamps"].call_count == 1
    assert "generated=1" in text
    assert "reason=all_done" in text


def test_all_attempted_failed_leaves_lock_open():
    """If every attempted chapter hits the generic
    except->continue path, the lock stays 'running' so a later
    run retries instead of locking the month out."""
    stt = _fake_stt()
    stt.long_running_recognize_uri.side_effect = RuntimeError(
        "boom",
    )
    text, m = _run(
        ["--fileset-ids", "LATLVRN1DA"], stt=stt,
    )
    m["mark_stt_run_complete"].assert_not_called()
    assert "reason=all_failed" in text


def test_dry_run_prints_worklist_without_gcs_writes():
    """--dry-run reports the worklist and remaining budget and
    never acquires the lock or writes artifacts; the STT client
    is not even constructed."""
    text, m = _run(
        ["--fileset-ids", "LATLVRN1DA", "--dry-run"],
    )
    assert "pending=2" in text
    assert "seconds_remaining=3300" in text
    m["acquire_stt_run_lock"].assert_not_called()
    m["upload_generated_timestamps"].assert_not_called()
    m["get_stt"].assert_not_called()


def test_max_chapters_caps_the_worklist():
    text, m = _run(
        ["--fileset-ids", "LATLVRN1DA", "--max-chapters", "1"],
    )
    assert m["upload_generated_timestamps"].call_count == 1
    assert "generated=1" in text


def test_book_filter_selects_one_book():
    rows = _ROWS + [
        {
            "book_id": "LUK",
            "chapter_start": 1,
            "path": "https://x/luk1.mp3",
            "duration": 100,
            "filesize_in_bytes": 800000,
        },
    ]
    text, m = _run(
        ["--fileset-ids", "LATLVRN1DA", "--book", "LUK"],
        dbt=_fake_dbt(rows=rows),
    )
    assert m["upload_generated_timestamps"].call_count == 1
    args = (
        m["upload_generated_timestamps"].call_args_list[0].args
    )
    assert args[1] == "LUK"


def test_opus16_fileset_id_resolves_to_base():
    """An -opus16 id passed on the command line generates into
    the base fileset's artifact prefix (the same recordings)."""
    text, m = _run(
        ["--fileset-ids", "LATLVRN1DA-opus16"],
    )
    assert m["upload_generated_timestamps"].call_count == 2
    args = (
        m["upload_generated_timestamps"].call_args_list[0].args
    )
    assert args[0] == "LATLVRN1DA"
