"""Tests for the resumable ``RunState`` JSON file."""
import json

from bible.services.gemini_tts.state import RunState


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


def test_save_leaves_no_tmp_file(tmp_path):
    path = tmp_path / "state.json"
    state = RunState(path)
    state.save()

    assert path.exists()
    assert not (tmp_path / "state.json.tmp").exists()
