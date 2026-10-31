"""Tests for the ``generate_gemini_audio`` management command.

The Gemini client is never exercised — ``GeminiSynthesizer`` is
patched — and SWORD's chapter worklist is stubbed, so nothing
touches the network, config.yaml, or the real API. State and
output dirs point at ``tmp_path``. GCS mode (``--upload-gcs``)
patches the ``gcs`` helper functions the way
``test_generate_chapter_audio_command`` does.
"""
import json
import os
from io import StringIO
from unittest.mock import DEFAULT, MagicMock, patch

import pytest
from django.conf import settings as django_settings
from django.core.management import call_command
from django.core.management.base import CommandError

from bible.services.gemini_tts.client import (
    DEFAULT_VOICE_NAME,
    QuotaExceeded,
)
from bible.services.gemini_tts.state import RunState


_CMD = "bible.management.commands.generate_gemini_audio"

WORKLIST = [("GEN", 1), ("GEN", 2), ("GEN", 3)]


def _artifacts():
    return MagicMock(
        mp3_bytes=b"\xff\xfb" + b"\x00" * 20,
        timestamps_payload={
            "duration_seconds": 1.0,
            "data": [{"verse_start": 1, "timestamp": 0.0}],
        },
        duration_seconds=1.0,
        chars_used=50,
    )


class _FakeSynth:
    """Captures the real client the command builds so the fake can
    spend its ``requests_made`` counter like the real synthesizer
    would. ``outcome`` is a class-level hook (callable or
    exception) tests install before invoking the command."""

    instances = []
    outcome = None

    def __init__(self, client=None, **kwargs):
        self.client = client
        self.kwargs = kwargs
        self.runs = []
        self.run_kwargs = []
        type(self).instances.append(self)

    def run(self, fileset_id, book_id, chapter, **kwargs):
        self.runs.append((book_id, chapter))
        self.run_kwargs.append(kwargs)
        self.client.requests_made += 1
        outcome = type(self).outcome
        if callable(outcome):
            return outcome(book_id, chapter)
        if isinstance(outcome, Exception):
            raise outcome
        return _artifacts()


@pytest.fixture
def worklist_client():
    sword = MagicMock()
    sword.list_chapters.return_value = list(WORKLIST)
    return sword


@pytest.fixture(autouse=True)
def _reset_fakes():
    _FakeSynth.instances = []
    _FakeSynth.outcome = None
    yield
    _FakeSynth.outcome = None


def _invoke(tmp_path, *extra):
    return call_command(
        "generate_gemini_audio",
        "--output-dir", str(tmp_path / "tts-out"),
        "--state-file", str(tmp_path / "state.json"),
        "--sleep-seconds", "0",
        *extra, stdout=StringIO(),
    )


def _invoke_out(tmp_path, *extra):
    out = StringIO()
    call_command(
        "generate_gemini_audio",
        "--output-dir", str(tmp_path / "tts-out"),
        "--state-file", str(tmp_path / "state.json"),
        "--sleep-seconds", "0",
        *extra, stdout=out,
    )
    return out.getvalue()


@patch(f"{_CMD}.GeminiSynthesizer", _FakeSynth)
@patch(f"{_CMD}.get_default_sword_client")
def test_generates_mp3_timestamps_and_state(
    sword_factory, tmp_path, worklist_client,
):
    sword_factory.return_value = worklist_client

    text = _invoke_out(tmp_path)

    out_dir = tmp_path / "tts-out" / "LVSGLU8"
    for book, chap in WORKLIST:
        assert (out_dir / book / f"{chap}.mp3").exists()
        ts_path = out_dir / book / f"{chap}_timestamps.json"
        payload = json.loads(ts_path.read_text())
        assert payload["data"][0]["verse_start"] == 1

    state = RunState(tmp_path / "state.json")
    for book, chap in WORKLIST:
        assert state.is_completed(book, chap)
    assert state.requests_today == 3
    assert "generated=3" in text
    assert "reason=all_done" in text


@patch(f"{_CMD}.GeminiSynthesizer", _FakeSynth)
@patch(f"{_CMD}.get_default_sword_client")
def test_second_run_resumes_from_state(
    sword_factory, tmp_path, worklist_client,
):
    sword_factory.return_value = worklist_client
    _invoke(tmp_path)

    synth = _FakeSynth.instances[-1]
    assert len(synth.runs) == 3

    # A fresh process (new state object from the same file) has
    # nothing left to do.
    _invoke(tmp_path)
    synth2 = _FakeSynth.instances[-1]
    assert synth2.runs == []


@patch(f"{_CMD}.GeminiSynthesizer", _FakeSynth)
@patch(f"{_CMD}.get_default_sword_client")
def test_files_on_disk_count_as_completed(
    sword_factory, tmp_path, worklist_client,
):
    """If the state file is lost but artifacts exist, the chapter
    is skipped instead of re-billed."""
    sword_factory.return_value = worklist_client
    gen_dir = tmp_path / "tts-out" / "LVSGLU8" / "GEN"
    gen_dir.mkdir(parents=True)
    (gen_dir / "1.mp3").write_bytes(b"\xff\xfb")
    (gen_dir / "1_timestamps.json").write_text("{}")

    _invoke(tmp_path)

    synth = _FakeSynth.instances[-1]
    assert synth.runs == [("GEN", 2), ("GEN", 3)]


@patch(f"{_CMD}.GeminiSynthesizer", _FakeSynth)
@patch(f"{_CMD}.get_default_sword_client")
def test_zero_byte_mp3_counts_as_pending(
    sword_factory, tmp_path, worklist_client,
):
    """A crash mid-write leaves an empty MP3 that must NOT mark
    the chapter done — it gets re-synthesized."""
    sword_factory.return_value = worklist_client
    gen_dir = tmp_path / "tts-out" / "LVSGLU8" / "GEN"
    gen_dir.mkdir(parents=True)
    (gen_dir / "1.mp3").write_bytes(b"")
    (gen_dir / "1_timestamps.json").write_text("{}")

    _invoke(tmp_path)

    synth = _FakeSynth.instances[-1]
    assert ("GEN", 1) in synth.runs


@patch(f"{_CMD}.GeminiSynthesizer", _FakeSynth)
@patch(f"{_CMD}.get_default_sword_client")
def test_corrupt_timestamps_json_counts_as_pending(
    sword_factory, tmp_path, worklist_client,
):
    sword_factory.return_value = worklist_client
    gen_dir = tmp_path / "tts-out" / "LVSGLU8" / "GEN"
    gen_dir.mkdir(parents=True)
    (gen_dir / "1.mp3").write_bytes(b"\xff\xfb")
    (gen_dir / "1_timestamps.json").write_text("{truncated")

    _invoke(tmp_path)

    synth = _FakeSynth.instances[-1]
    assert ("GEN", 1) in synth.runs


@patch(f"{_CMD}.GeminiSynthesizer", _FakeSynth)
@patch(f"{_CMD}.get_default_sword_client")
def test_request_budget_passes_remaining_headroom(
    sword_factory, tmp_path, worklist_client,
):
    """With 13/14 already spent today, the next chapter gets
    request_budget=1 so it can never overshoot the daily cap,
    and the run stops right after it."""
    sword_factory.return_value = worklist_client
    state = RunState(tmp_path / "state.json")
    state.add_requests(13)
    state.save()

    text = _invoke_out(tmp_path)

    synth = _FakeSynth.instances[-1]
    assert synth.runs == [("GEN", 1)]
    assert synth.run_kwargs == [{"request_budget": 1}]
    assert "reason=daily_request_cap" in text


@patch(f"{_CMD}.GeminiSynthesizer", _FakeSynth)
@patch(f"{_CMD}.get_default_sword_client")
def test_voice_option_reaches_synthesizer(
    sword_factory, tmp_path, worklist_client,
):
    sword_factory.return_value = worklist_client

    _invoke(tmp_path, "--voice", "Charon")

    synth = _FakeSynth.instances[-1]
    assert synth.kwargs["voice_name"] == "Charon"


@patch(f"{_CMD}.GeminiSynthesizer", _FakeSynth)
@patch(f"{_CMD}.get_default_sword_client")
def test_quota_exceeded_stops_run(
    sword_factory, tmp_path, worklist_client,
):
    """GEN 1 succeeds, GEN 2 raises QuotaExceeded (all keys 429),
    GEN 3 is never attempted and stays pending for tomorrow."""
    sword_factory.return_value = worklist_client

    def outcome(book_id, chapter):
        if chapter == 2:
            raise QuotaExceeded("429 on every key")
        return _artifacts()

    _FakeSynth.outcome = outcome
    text = _invoke_out(tmp_path)

    synth = _FakeSynth.instances[-1]
    assert synth.runs == [("GEN", 1), ("GEN", 2)]
    # GEN 2's failed call still counted against the daily counter.
    state = RunState(tmp_path / "state.json")
    assert state.requests_today == 2
    assert state.is_completed("GEN", 1)
    assert not state.is_completed("GEN", 2)
    assert not (
        tmp_path / "tts-out" / "LVSGLU8" / "GEN" / "3.mp3"
    ).exists()
    assert "reason=quota_exhausted" in text


@patch(f"{_CMD}.GeminiSynthesizer", _FakeSynth)
@patch(f"{_CMD}.get_default_sword_client")
def test_daily_request_cap_stops_run(
    sword_factory, tmp_path, worklist_client,
):
    """--max-requests 2: after two requests the run stops even
    though work remains."""
    sword_factory.return_value = worklist_client

    text = _invoke_out(tmp_path, "--max-requests", "2")

    synth = _FakeSynth.instances[-1]
    assert synth.runs == [("GEN", 1), ("GEN", 2)]
    assert "reason=daily_request_cap" in text
    assert RunState(
        tmp_path / "state.json"
    ).requests_today == 2


@patch(f"{_CMD}.GeminiSynthesizer", _FakeSynth)
@patch(f"{_CMD}.get_default_sword_client")
def test_spent_yesterdays_quota_does_not_block(
    sword_factory, tmp_path, worklist_client,
):
    """A state file from a previous day must not block today's run
    — the Pacific-midday reset is in RunState."""
    sword_factory.return_value = worklist_client
    (tmp_path / "state.json").write_text(json.dumps({
        "day": "2000-01-01",
        "requests_today": 14,
        "completed": {"GEN": [1]},
    }))

    text = _invoke_out(tmp_path, "--max-requests", "1")

    synth = _FakeSynth.instances[-1]
    assert synth.runs == [("GEN", 2)]
    assert "reason=daily_request_cap" in text


@patch(f"{_CMD}.get_default_sword_client")
def test_missing_api_keys_raises_command_error(
    sword_factory, tmp_path, worklist_client,
):
    sword_factory.return_value = worklist_client
    with patch.object(django_settings, "GEMINI_API_KEYS", []):
        with pytest.raises(CommandError, match="GEMINI_API_KEY"):
            _invoke(tmp_path)


@patch(f"{_CMD}.get_default_sword_client")
def test_dry_run_reports_without_calling_api(
    sword_factory, tmp_path, worklist_client,
):
    sword_factory.return_value = worklist_client

    text = _invoke_out(tmp_path, "--dry-run")

    assert "would generate 3 chapters" in text
    assert not (tmp_path / "tts-out").exists()
    assert _FakeSynth.instances == []


@pytest.fixture
def gcs_mocks():
    """Patch every ``gcs`` helper the GCS mode touches.

    The command module and ``gemini_tts.state`` both hold the same
    ``gcs`` module object, so patching attributes here covers the
    real :class:`GcsRunState` too."""
    # patch.multiple only populates the yielded dict for
    # attributes patched with DEFAULT — explicit MagicMock()
    # replacements patch the attribute but never appear in it.
    with patch.multiple(
        f"{_CMD}.gcs",
        acquire_run_lock=DEFAULT,
        list_completed_chapters=DEFAULT,
        read_gemini_daily_requests=DEFAULT,
        write_gemini_daily_requests=DEFAULT,
        upload_chapter_artifacts=DEFAULT,
        mark_run_complete=DEFAULT,
    ) as mocks:
        mocks["acquire_run_lock"].return_value = (True, "acquired")
        mocks["list_completed_chapters"].return_value = set()
        mocks["read_gemini_daily_requests"].return_value = 0
        yield mocks


def _invoke_gcs_out(*extra):
    out = StringIO()
    call_command(
        "generate_gemini_audio",
        "--upload-gcs",
        "--sleep-seconds", "0",
        *extra, stdout=out,
    )
    return out.getvalue()


@patch(f"{_CMD}.GeminiSynthesizer", _FakeSynth)
@patch(f"{_CMD}.get_default_sword_client")
def test_gcs_mode_uploads_artifacts_and_completes(
    sword_factory, tmp_path, worklist_client, gcs_mocks,
):
    sword_factory.return_value = worklist_client

    text = _invoke_gcs_out()

    upload = gcs_mocks["upload_chapter_artifacts"]
    assert upload.call_count == 3
    first = upload.call_args_list[0]
    assert first.args[:3] == ("LVSGLU8", "GEN", 1)
    # args: fileset, book, chapter, mp3, payload, voice_name.
    assert first.args[5] == DEFAULT_VOICE_NAME
    assert first.kwargs["mode"] == "gemini"

    acquire = gcs_mocks["acquire_run_lock"]
    assert acquire.call_args.kwargs["mode"] == "gemini"
    assert acquire.call_args.kwargs["ym"]  # day key, not month

    mark = gcs_mocks["mark_run_complete"]
    mark.assert_called_once()
    assert mark.call_args.kwargs["mode"] == "gemini"
    assert mark.call_args.kwargs["reason"] == "all_done"
    assert mark.call_args.kwargs["chapters_generated"] == 3
    assert mark.call_args.kwargs["ym"] == (
        acquire.call_args.kwargs["ym"]
    )

    # The request counter persisted per chapter (3 calls total:
    # one per finally-spend + mark_completed save pairs collapse).
    assert gcs_mocks["write_gemini_daily_requests"].call_count >= 3

    # Nothing local was written.
    assert not (tmp_path / "tts-out").exists()
    assert not (tmp_path / "state.json").exists()
    assert "generated=3" in text
    assert "reason=all_done" in text


@patch(f"{_CMD}.GeminiSynthesizer", _FakeSynth)
@patch(f"{_CMD}.get_default_sword_client")
def test_gcs_completed_listing_skips_chapters(
    sword_factory, tmp_path, worklist_client, gcs_mocks,
):
    """Completion comes from the GCS artifact listing, not a
    state file — listed chapters are never re-synthesized."""
    sword_factory.return_value = worklist_client
    gcs_mocks["list_completed_chapters"].return_value = {
        ("GEN", 1), ("GEN", 2),
    }

    _invoke_gcs_out()

    synth = _FakeSynth.instances[-1]
    assert synth.runs == [("GEN", 3)]
    # The listing was scoped to the gemini prefix + voice.
    assert (
        gcs_mocks["list_completed_chapters"].call_args.kwargs[
            "mode"
        ]
        == "gemini"
    )


@patch(f"{_CMD}.GeminiSynthesizer", _FakeSynth)
@patch(f"{_CMD}.get_default_sword_client")
def test_gcs_day_counter_resumes_same_day_spend(
    sword_factory, tmp_path, worklist_client, gcs_mocks,
):
    """A same-day job retry reads the persisted counter: with
    13/14 already spent only one more chapter is allowed."""
    sword_factory.return_value = worklist_client
    gcs_mocks["read_gemini_daily_requests"].return_value = 13

    text = _invoke_gcs_out()

    synth = _FakeSynth.instances[-1]
    assert synth.runs == [("GEN", 1)]
    assert "reason=daily_request_cap" in text


@patch(f"{_CMD}.GeminiSynthesizer", _FakeSynth)
@patch(f"{_CMD}.get_default_sword_client")
def test_gcs_lock_not_acquired_skips_run(
    sword_factory, tmp_path, worklist_client, gcs_mocks,
):
    """A live same-day lock (concurrent/duplicate trigger) makes
    the run a clean no-op."""
    sword_factory.return_value = worklist_client
    gcs_mocks["acquire_run_lock"].return_value = (
        False, "already_completed",
    )

    text = _invoke_gcs_out()

    assert "skipped reason=already_completed" in text
    assert _FakeSynth.instances == []
    gcs_mocks["upload_chapter_artifacts"].assert_not_called()
    gcs_mocks["mark_run_complete"].assert_not_called()


@patch(f"{_CMD}.GeminiSynthesizer", _FakeSynth)
@patch(f"{_CMD}.get_default_sword_client")
def test_gcs_quota_exhausted_still_marks_run_complete(
    sword_factory, tmp_path, worklist_client, gcs_mocks,
):
    """Quota exhaustion is a *successful* daily outcome — the lock
    is marked complete so same-day reruns don't retry."""
    sword_factory.return_value = worklist_client

    def outcome(book_id, chapter):
        if chapter == 2:
            raise QuotaExceeded("429 on every key")
        return _artifacts()

    _FakeSynth.outcome = outcome
    text = _invoke_gcs_out()

    assert "reason=quota_exhausted" in text
    mark = gcs_mocks["mark_run_complete"]
    assert mark.call_args.kwargs["reason"] == "quota_exhausted"


@patch(f"{_CMD}.GeminiSynthesizer", _FakeSynth)
@patch(f"{_CMD}.get_default_sword_client")
def test_gcs_all_failed_leaves_lock_uncompleted(
    sword_factory, tmp_path, worklist_client, gcs_mocks,
):
    """Every chapter failing must not record a zero-artifact
    'completed' run — mirrors generate_chapter_audio."""
    sword_factory.return_value = worklist_client
    _FakeSynth.outcome = RuntimeError("synthetic boom")

    text = _invoke_gcs_out()

    assert "reason=all_failed" in text
    gcs_mocks["mark_run_complete"].assert_not_called()


@patch(f"{_CMD}.GeminiSynthesizer", _FakeSynth)
@patch(f"{_CMD}.get_default_sword_client")
def test_cloud_run_job_env_var_enables_gcs_mode(
    sword_factory, tmp_path, worklist_client, gcs_mocks,
):
    """Inside the Cloud Run Job container CLOUD_RUN_JOB is always
    set — GCS mode must engage even without the flag, so a
    forgotten ``--upload-gcs`` can never silently burn quota into
    ephemeral disk."""
    sword_factory.return_value = worklist_client

    with patch.dict(os.environ, {"CLOUD_RUN_JOB": "all-jobs"}):
        text = _invoke_out(tmp_path)

    gcs_mocks["acquire_run_lock"].assert_called_once()
    assert "generated=3" in text


@patch(f"{_CMD}.GeminiSynthesizer", _FakeSynth)
@patch(f"{_CMD}.get_default_sword_client")
def test_gcs_dry_run_never_acquires_lock(
    sword_factory, tmp_path, worklist_client, gcs_mocks,
):
    """--dry-run must not write a 'running' lock object that would
    block the real scheduled run."""
    sword_factory.return_value = worklist_client

    text = _invoke_gcs_out("--dry-run")

    assert "would generate 3 chapters" in text
    gcs_mocks["acquire_run_lock"].assert_not_called()
    assert _FakeSynth.instances == []


@patch(f"{_CMD}.GeminiSynthesizer", _FakeSynth)
@patch(f"{_CMD}.get_default_sword_client")
def test_local_mode_never_touches_gcs(
    sword_factory, tmp_path, worklist_client, gcs_mocks,
):
    """Backward compatibility: without --upload-gcs (and no
    CLOUD_RUN_JOB) every gcs helper stays uncalled."""
    sword_factory.return_value = worklist_client
    # Guard against a polluted ambient environment: patch.dict
    # restores os.environ on exit.
    with patch.dict(os.environ) as env:
        env.pop("CLOUD_RUN_JOB", None)
        _invoke_out(tmp_path)

    for name, mock in gcs_mocks.items():
        assert not mock.called, f"gcs.{name} called in local mode"
