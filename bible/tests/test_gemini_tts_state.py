"""Tests for the resumable run-state backends.

``RunState`` covers the local JSON file; ``GcsRunState`` covers
the Cloud Run mode (completion from the artifact listing, daily
counter in a GCS object). GCS calls are patched — no bucket is
touched.
"""
import json
from unittest.mock import patch

import pytest

from bible.services.gemini_tts.state import GcsRunState, RunState

_STATE = "bible.services.gemini_tts.state"


def test_fresh_state_when_file_missing(tmp_path):
    state = RunState(tmp_path / "state.json")

    assert state.requests_today == 0
    assert state.completed_count() == 0
    assert not state.is_completed("GEN", 1)


def test_mark_completed_and_requests_round_trip(tmp_path):
    path = tmp_path / "state.json"
    state = RunState(path)
    state.mark_completed("GEN", 1)
    state.mark_completed("GEN", 3)
    state.mark_completed("JHN", 3)
    state.add_requests(5)
    state.save()

    reloaded = RunState(path)

    assert reloaded.is_completed("GEN", 1)
    assert reloaded.is_completed("GEN", 3)
    assert reloaded.is_completed("JHN", 3)
    assert not reloaded.is_completed("GEN", 2)
    assert reloaded.requests_today == 5
    assert reloaded.completed_count() == 3


def test_request_counter_resets_on_new_pacific_day(tmp_path):
    """A stored counter from a previous day (e.g. yesterday, the
    free-tier reset at midnight Pacific) must not carry over."""
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "day": "2000-01-01",
        "requests_today": 99,
        "completed": {"GEN": [1]},
    }))

    state = RunState(path)

    assert state.requests_today == 0
    # Completed chapters survive the day rollover.
    assert state.is_completed("GEN", 1)
    state.add_requests(2)
    assert state.requests_today == 2


def test_corrupt_state_file_starts_fresh(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json")

    state = RunState(path)

    assert state.requests_today == 0
    assert state.completed_count() == 0
    # ... and the file can be overwritten cleanly afterwards.
    state.mark_completed("GEN", 1)
    state.save()
    assert RunState(path).is_completed("GEN", 1)


@pytest.mark.parametrize("payload", [
    {"completed": 5},                 # .items() on an int
    {"completed": {"GEN": "abc"}},    # int("a") ValueError
    {"completed": {"GEN": 5}},        # iterating an int
    {"requests_today": {"bad": 1}},   # int(dict) TypeError
    [1, 2, 3],                        # valid JSON, wrong root
    "just a string",
    42,
])
def test_wrong_shape_state_file_starts_fresh(tmp_path, payload):
    """Valid-JSON-but-wrong-shape files must not wedge the run —
    they are treated exactly like unparseable ones."""
    path = tmp_path / "state.json"
    path.write_text(json.dumps(payload))

    state = RunState(path)

    assert state.requests_today == 0
    assert state.completed_count() == 0
    # ... and the file can be overwritten cleanly afterwards.
    state.mark_completed("GEN", 1)
    state.save()
    assert RunState(path).is_completed("GEN", 1)


def test_save_leaves_no_tmp_file(tmp_path):
    path = tmp_path / "state.json"
    state = RunState(path)
    state.save()

    assert path.exists()
    assert not (tmp_path / "state.json.tmp").exists()


class TestGcsRunState:
    def test_completed_comes_from_artifact_listing(self):
        with patch(f"{_STATE}.gcs") as mgcs:
            mgcs.list_completed_chapters.return_value = {
                ("GEN", 1), ("GEN", 2),
            }
            mgcs.read_gemini_daily_requests.return_value = 5
            state = GcsRunState("LVSGLU8", "Sadachbia")

        mgcs.list_completed_chapters.assert_called_once_with(
            "LVSGLU8", "Sadachbia", mode="gemini",
        )
        assert state.is_completed("GEN", 1)
        assert state.is_completed("GEN", 2)
        assert not state.is_completed("GEN", 3)
        assert state.requests_today == 5

    def test_save_persists_counter_to_day_object(self):
        with patch(f"{_STATE}.gcs") as mgcs:
            mgcs.list_completed_chapters.return_value = set()
            mgcs.read_gemini_daily_requests.return_value = 0
            state = GcsRunState("LVSGLU8", "Sadachbia")
            state.add_requests(3)
            state.mark_completed("GEN", 1)
            state.save()

        mgcs.write_gemini_daily_requests.assert_called_once_with(
            "LVSGLU8", "Sadachbia", state._day, 3,
        )
        # mark_completed only updates the in-memory view — the
        # artifact upload itself is what makes a chapter done.
        assert state.is_completed("GEN", 1)

    def test_counter_rolls_on_new_pacific_day(self):
        """Crossing midnight Pacific mid-run resets the counter to
        0 — the API quota reset does the same."""
        with patch(f"{_STATE}.gcs") as mgcs, patch(
            f"{_STATE}.today_pacific",
        ) as mday:
            mday.return_value = "2026-10-29"
            mgcs.list_completed_chapters.return_value = set()
            mgcs.read_gemini_daily_requests.return_value = 14
            state = GcsRunState("LVSGLU8", "Sadachbia")
            assert state.requests_today == 14

            mday.return_value = "2026-10-30"
            assert state.requests_today == 0
            state.add_requests(2)
            state.save()
            assert (
                mgcs.write_gemini_daily_requests.call_args.args[2:]
                == ("2026-10-30", 2)
            )
