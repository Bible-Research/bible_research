"""Tests for the ``run_all_jobs`` master command.

Schedule predicates are pure date functions and ``call_command`` is
mocked, so no DB, config.yaml, or provider credentials are needed.
"""
from datetime import date
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from bible.management.commands import run_all_jobs
from bible.management.commands.run_all_jobs import _resolve_schedule

_CMD = "bible.management.commands.run_all_jobs"


def _run(argv, today=None):
    """Invoke run_all_jobs with call_command mocked and, optionally,
    a fixed 'today'."""
    out = StringIO()
    patches = [patch(f"{_CMD}.call_command")]
    if today is not None:
        fake_date = patch(f"{_CMD}.date")
        patches.append(fake_date)
    else:
        fake_date = None
    mocks = []
    for p in patches:
        mocks.append(p.start())
    try:
        if fake_date is not None:
            mocks[1].today.return_value = today
        call_command("run_all_jobs", *argv, stdout=out)
    finally:
        for p in patches:
            p.stop()
    return out.getvalue(), mocks[0]


class TestSchedulePredicates:
    def test_daily_always_runs(self):
        pred = _resolve_schedule("daily")
        assert pred(date(2026, 1, 1))
        assert pred(date(2026, 12, 31))

    def test_weekly_matches_named_weekday(self):
        # 2026-10-28 is a Wednesday.
        assert _resolve_schedule("weekly:wed")(date(2026, 10, 28))
        assert not _resolve_schedule("weekly:mon")(date(2026, 10, 28))
        assert not _resolve_schedule("weekly:sun")(date(2026, 10, 28))

    def test_weekly_rejects_bad_weekday(self):
        with pytest.raises(CommandError):
            _resolve_schedule("weekly:fundag")

    def test_dom_odd_matches_odd_days_only(self):
        pred = _resolve_schedule("dom:odd")
        assert pred(date(2026, 10, 1))
        assert not pred(date(2026, 10, 2))
        assert pred(date(2026, 10, 31))

    def test_dom_n_matches_specific_day(self):
        pred = _resolve_schedule("dom:3")
        assert pred(date(2026, 10, 3))
        assert not pred(date(2026, 10, 4))

    def test_dom_rejects_out_of_range(self):
        with pytest.raises(CommandError):
            _resolve_schedule("dom:0")
        with pytest.raises(CommandError):
            _resolve_schedule("dom:32")
        with pytest.raises(CommandError):
            _resolve_schedule("dom:banana")

    def test_last_day_of_month(self):
        pred = _resolve_schedule("last_day_of_month")
        assert pred(date(2026, 1, 31))
        assert not pred(date(2026, 1, 30))
        assert pred(date(2026, 2, 28))  # 2026 is not a leap year
        assert pred(date(2024, 2, 29))  # 2024 is a leap year

    def test_manual_never_runs(self):
        pred = _resolve_schedule("manual")
        assert not pred(date(2026, 1, 1))
        assert not pred(date(2026, 10, 3))

    def test_unknown_schedule_raises(self):
        with pytest.raises(CommandError):
            _resolve_schedule("fortnightly")


class TestSelection:
    def test_schedule_gate_skips_non_matching_entries(self):
        # JOBS[0] is dom:3 — runs on the 3rd, skipped on the 4th.
        out, mock_call = _run([], today=date(2026, 10, 4))
        assert mock_call.call_count == 0
        assert "skipping: generate_chapter_audio" in out

    def test_schedule_gate_runs_matching_entry(self):
        out, mock_call = _run([], today=date(2026, 10, 3))
        mock_call.assert_called_once_with(
            "generate_chapter_audio",
            "--fileset-id", "LVSGLU8", "--mode", "both",
        )

    def test_dry_run_never_calls_sub_jobs(self):
        out, mock_call = _run(
            ["--dry-run"], today=date(2026, 10, 3),
        )
        assert mock_call.call_count == 0
        assert "would run: generate_chapter_audio" in out

    def test_dry_run_reports_when_nothing_scheduled(self):
        out, mock_call = _run(
            ["--dry-run"], today=date(2026, 10, 4),
        )
        assert mock_call.call_count == 0
        assert "nothing scheduled to run" in out

    def test_only_ignores_schedule(self):
        # A manual entry never matches a scheduled pass but --only
        # must still run it.
        jobs = [("some_job", ["--flag"], "manual")]
        with patch.object(run_all_jobs, "JOBS", jobs):
            out, mock_call = _run(
                ["--only", "some_job"], today=date(2026, 10, 4),
            )
        mock_call.assert_called_once_with("some_job", "--flag")

    def test_only_unknown_label_raises(self):
        with pytest.raises(CommandError):
            _run(["--only", "nope"], today=date(2026, 10, 3))

    def test_force_runs_manual_entries(self):
        jobs = [
            ("job_a", [], "manual"),
            ("job_b", [], "manual"),
        ]
        with patch.object(run_all_jobs, "JOBS", jobs):
            out, mock_call = _run(
                ["--force"], today=date(2026, 10, 4),
            )
        assert mock_call.call_count == 2

    def test_failed_entry_does_not_stop_later_entries(self):
        jobs = [
            ("job_a", [], "daily"),
            ("job_b", [], "daily"),
        ]
        with patch.object(run_all_jobs, "JOBS", jobs):
            with patch(f"{_CMD}.call_command") as mock_call:
                mock_call.side_effect = [RuntimeError("boom"), None]
                with pytest.raises(CommandError) as exc:
                    call_command("run_all_jobs")
        assert mock_call.call_count == 2
        assert "job_a" in str(exc.value)
        assert "job_b" not in str(exc.value)

    def test_system_exit_entry_does_not_stop_later_entries(self):
        """A sub-job calling sys.exit() must not abort the pass."""
        jobs = [
            ("job_a", [], "daily"),
            ("job_b", [], "daily"),
        ]
        with patch.object(run_all_jobs, "JOBS", jobs):
            with patch(f"{_CMD}.call_command") as mock_call:
                mock_call.side_effect = [SystemExit(1), None]
                with pytest.raises(CommandError) as exc:
                    call_command("run_all_jobs")
        assert mock_call.call_count == 2
        assert "job_a" in str(exc.value)
        assert "job_b" not in str(exc.value)
