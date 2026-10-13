from io import StringIO
from unittest.mock import MagicMock, patch

from django.core.management import call_command

from bible.services.google_tts.client import QuotaExceeded


_CMD = "bible.management.commands.generate_chapter_audio"


@patch(f"{_CMD}.gcs.mark_run_complete")
@patch(f"{_CMD}.gcs.increment_monthly_usage")
@patch(f"{_CMD}.gcs.read_monthly_usage")
@patch(f"{_CMD}.gcs.acquire_run_lock")
@patch(f"{_CMD}.gcs.upload_chapter_artifacts")
@patch(f"{_CMD}.gcs.list_completed_chapters")
@patch(f"{_CMD}.Synthesizer")
def test_command_stops_cleanly_on_quota_exceeded(
    synth_cls, list_done, upload, acquire, read_usage, increment,
    mark_complete,
):
    acquire.return_value = (True, "acquired")
    read_usage.return_value = 0
    list_done.return_value = set()
    inst = MagicMock()
    artifacts = MagicMock(
        mp3_bytes=b"x", timestamps_payload={}, duration_seconds=1.0,
        chars_used=50,
    )

    # The real synthesizer consumes budget per-verse as Cloud TTS
    # bills us; the command now relies on that side effect to compute
    # the per-chapter usage delta it persists to GCS.
    call_outcomes = [artifacts, artifacts, QuotaExceeded("over")]

    def fake_run(_fileset, _book, _chap, budget=None, mode="verse"):
        outcome = call_outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        if budget is not None:
            budget.consume(outcome.chars_used)
        return outcome

    inst.run.side_effect = fake_run
    synth_cls.return_value = inst

    out = StringIO()
    call_command(
        "generate_chapter_audio", "--fileset-id", "LVSGLU8", stdout=out,
    )

    assert upload.call_count == 2
    assert increment.call_count == 2
    assert "quota_exhausted" in out.getvalue()
    mark_complete.assert_called_once()
    assert mark_complete.call_args.kwargs["reason"] == "quota_exhausted"


@patch(f"{_CMD}.gcs.mark_run_complete")
@patch(f"{_CMD}.gcs.increment_monthly_usage")
@patch(f"{_CMD}.gcs.read_monthly_usage")
@patch(f"{_CMD}.gcs.acquire_run_lock")
@patch(f"{_CMD}.gcs.upload_chapter_artifacts")
@patch(f"{_CMD}.gcs.list_completed_chapters")
@patch(f"{_CMD}.Synthesizer")
def test_partial_chapter_billed_chars_are_persisted_on_quota_error(
    synth_cls, list_done, upload, acquire, read_usage, increment,
    mark_complete,
):
    """Regression: if Cloud TTS raises QuotaExceeded partway through a
    chapter, the characters it already billed us for (reflected in
    ``budget.used``) must still be persisted to GCS. Otherwise the
    next run would read a stale counter and exceed the monthly cap."""
    acquire.return_value = (True, "acquired")
    read_usage.return_value = 0
    list_done.return_value = set()

    inst = MagicMock()

    def run_side_effect(_fileset, _book, _chap, budget=None, mode="verse"):
        # Simulate 3 billed verses, then TTS quota dies on verse 4.
        for _ in range(3):
            budget.consume(40)
        raise QuotaExceeded("mid-chapter")

    inst.run.side_effect = run_side_effect
    synth_cls.return_value = inst

    out = StringIO()
    call_command(
        "generate_chapter_audio", "--fileset-id", "LVSGLU8", stdout=out,
    )

    # Nothing uploaded (chapter never completed), but the 120 chars
    # Google already billed MUST be written to the GCS usage counter.
    assert upload.call_count == 0
    assert increment.call_count == 1
    assert increment.call_args.args[1] == 120
    assert "quota_exhausted" in out.getvalue()
    mark_complete.assert_called_once()
    assert mark_complete.call_args.kwargs["chars_used"] == 120


@patch(f"{_CMD}.gcs.mark_run_complete")
@patch(f"{_CMD}.gcs.increment_monthly_usage")
@patch(f"{_CMD}.gcs.read_monthly_usage")
@patch(f"{_CMD}.gcs.acquire_run_lock")
@patch(f"{_CMD}.gcs.upload_chapter_artifacts")
@patch(f"{_CMD}.gcs.list_completed_chapters")
@patch(f"{_CMD}.Synthesizer")
def test_billed_chars_persisted_when_upload_fails(
    synth_cls, list_done, upload, acquire, read_usage, increment,
    mark_complete,
):
    """Regression: a successfully-synthesized chapter whose upload
    fails still cost us Cloud TTS characters. Those chars must land in
    the monthly usage counter so a persistent upload outage cannot
    silently drive us over ``MONTHLY_TTS_CHAR_LIMIT``."""
    acquire.return_value = (True, "acquired")
    read_usage.return_value = 0
    list_done.return_value = set()
    upload.side_effect = RuntimeError("gcs 503")

    inst = MagicMock()

    def run_side_effect(_fileset, _book, _chap, budget=None, mode="verse"):
        budget.consume(75)
        return MagicMock(
            mp3_bytes=b"x", timestamps_payload={"data": []},
            duration_seconds=1.0, chars_used=75,
        )

    # First chapter synthesizes successfully (but upload fails below);
    # second call raises QuotaExceeded to terminate the run promptly.
    outcomes = [run_side_effect, QuotaExceeded("stop")]

    def dispatch(fileset, book, chap, budget=None, mode="verse"):
        nxt = outcomes.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt(fileset, book, chap, budget=budget)

    inst.run.side_effect = dispatch
    synth_cls.return_value = inst

    call_command("generate_chapter_audio", "--fileset-id", "LVSGLU8")

    # Upload was attempted and failed, but the 75 billed chars must
    # still be persisted.
    assert upload.call_count == 1
    assert increment.call_count == 1
    assert increment.call_args.args[1] == 75


@patch(f"{_CMD}.gcs.mark_run_complete")
@patch(f"{_CMD}.gcs.increment_monthly_usage")
@patch(f"{_CMD}.gcs.read_monthly_usage")
@patch(f"{_CMD}.gcs.acquire_run_lock")
@patch(f"{_CMD}.gcs.upload_chapter_artifacts")
@patch(f"{_CMD}.gcs.list_completed_chapters")
@patch(f"{_CMD}.Synthesizer")
def test_chapter_mode_passes_mode_through_all_seams(
    synth_cls, list_done, upload, acquire, read_usage, increment,
    mark_complete,
):
    """--mode chapter must thread mode through every GCS helper and
    the synthesizer so chapter artifacts/locks stay isolated from
    verse-mode state."""
    acquire.return_value = (True, "acquired")
    read_usage.return_value = 0
    list_done.return_value = set()

    inst = MagicMock()

    artifacts = MagicMock(
        mp3_bytes=b"x", timestamps_payload={"data": []},
        duration_seconds=1.0, chars_used=10,
    )
    outcomes = [artifacts, QuotaExceeded("stop")]

    def run_side_effect(_fileset, _book, _chap, budget=None, mode=None):
        assert mode == "chapter"
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        budget.consume(10)
        return outcome

    inst.run.side_effect = run_side_effect
    synth_cls.return_value = inst

    out = StringIO()
    call_command(
        "generate_chapter_audio", "--fileset-id", "LVSGLU8",
        "--mode", "chapter", stdout=out,
    )

    assert acquire.call_args.kwargs["mode"] == "chapter"
    assert list_done.call_args.kwargs["mode"] == "chapter"
    assert upload.call_args.kwargs["mode"] == "chapter"
    assert mark_complete.call_args.kwargs["mode"] == "chapter"
    assert "mode=chapter" in out.getvalue()


@patch(f"{_CMD}.gcs.mark_run_complete")
@patch(f"{_CMD}.gcs.increment_monthly_usage")
@patch(f"{_CMD}.gcs.read_monthly_usage")
@patch(f"{_CMD}.gcs.acquire_run_lock")
@patch(f"{_CMD}.gcs.upload_chapter_artifacts")
@patch(f"{_CMD}.gcs.list_completed_chapters")
@patch(f"{_CMD}.Synthesizer")
def test_mode_both_runs_verse_pass_then_chapter_pass(
    synth_cls, list_done, upload, acquire, read_usage, increment,
    mark_complete,
):
    """--mode both runs two independent passes, each with its own
    per-mode lock; the shared usage counter is re-read between passes
    so verse-mode spend counts against the chapter budget."""
    acquire.return_value = (True, "acquired")
    # Chapter pass re-reads usage and sees the verse pass's spend.
    read_usage.side_effect = [0, 999]
    list_done.return_value = set()

    inst = MagicMock()
    seen_modes = []

    def run_side_effect(_fileset, _book, _chap, budget=None, mode=None):
        seen_modes.append(mode)
        budget.consume(10)
        raise QuotaExceeded("stop after first chapter")

    inst.run.side_effect = run_side_effect
    synth_cls.return_value = inst

    out = StringIO()
    call_command(
        "generate_chapter_audio", "--fileset-id", "LVSGLU8",
        "--mode", "both", stdout=out,
    )

    assert [c.kwargs["mode"] for c in acquire.call_args_list] == [
        "verse", "chapter",
    ]
    assert [c.kwargs["mode"] for c in mark_complete.call_args_list] == [
        "verse", "chapter",
    ]
    assert seen_modes == ["verse", "chapter"]
    assert out.getvalue().count("quota_exhausted next=") == 2
    # Usage is shared: the second read happened after the verse pass
    # persisted its delta.
    assert read_usage.call_count == 2


@patch(f"{_CMD}.get_default_sword_client")
@patch(f"{_CMD}.gcs.mark_run_complete")
@patch(f"{_CMD}.gcs.increment_monthly_usage")
@patch(f"{_CMD}.gcs.read_monthly_usage")
@patch(f"{_CMD}.gcs.acquire_run_lock")
@patch(f"{_CMD}.gcs.upload_chapter_artifacts")
@patch(f"{_CMD}.gcs.list_completed_chapters")
@patch(f"{_CMD}.Synthesizer")
def test_run_lock_left_open_when_every_chapter_fails(
    synth_cls, list_done, upload, acquire, read_usage, increment,
    mark_complete, sword_client,
):
    """If every pending chapter hits the generic
    ``except -> continue`` path (e.g. a client-side crash before
    any TTS call), marking the run complete would lock the month
    out with zero artifacts. The lock must stay "running" so a
    later run retries after it goes stale."""
    sword_client.return_value.list_chapters.return_value = [
        ("GEN", 1), ("GEN", 2),
    ]
    acquire.return_value = (True, "acquired")
    read_usage.return_value = 0
    list_done.return_value = set()

    inst = MagicMock()
    inst.run.side_effect = RuntimeError("synthetic boom")
    synth_cls.return_value = inst

    out = StringIO()
    call_command(
        "generate_chapter_audio", "--fileset-id", "LVSGLU8",
        stdout=out,
    )

    mark_complete.assert_not_called()
    assert "all_failed" in out.getvalue()


@patch(f"{_CMD}.gcs.acquire_run_lock")
def test_dry_run_reports_each_mode_without_touching_gcs(
    acquire,
):
    out = StringIO()
    call_command(
        "generate_chapter_audio", "--fileset-id", "LVSGLU8",
        "--mode", "both", "--dry-run", stdout=out,
    )
    text = out.getvalue()
    assert "mode=verse" in text
    assert "mode=chapter" in text
    acquire.assert_not_called()
