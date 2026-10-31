"""Tests for the generate_edge_audio management command."""
import json
import os
from io import StringIO
from unittest.mock import DEFAULT, MagicMock, patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from bible.services.edge_tts.client import (
    EdgeSynthesisResult,
    EdgeTTSError,
)

_CMD = "bible.management.commands.generate_edge_audio"

WORKLIST = [("GEN", 1), ("GEN", 2), ("GEN", 3)]

VERSES = [
    {"verse_start": 1, "verse_text": "aaa"},
    {"verse_start": 2, "verse_text": "bbbb"},
    {"verse_start": 3, "verse_text": "cc"},
]


def _edge_result(text_len: int = 10):
    mp3 = b"\xff\xfb" + b"\x00" * (text_len * 100)
    from bible.services.edge_tts.client import mp3_duration_seconds
    return EdgeSynthesisResult(
        mp3_bytes=mp3,
        duration_seconds=mp3_duration_seconds(mp3),
    )


class _FakeEdgeSynth:
    """Captures runs; ``outcome`` is a class-level hook (callable
    or exception) tests install before invoking the command."""

    instances = []
    outcome = None

    def __init__(self, client=None, **kwargs):
        self.client = client
        self.kwargs = kwargs
        self.runs = []
        type(self).instances.append(self)

    def run(self, fileset_id, book_id, chapter):
        self.runs.append((book_id, chapter))
        if self.client is not None:
            self.client.requests_made += 1
        outcome = type(self).outcome
        if callable(outcome):
            return outcome(book_id, chapter)
        if isinstance(outcome, Exception):
            raise outcome
        return MagicMock(
            mp3_bytes=b"\xff\xfb" + b"\x00" * 20,
            timestamps_payload={
                "duration_seconds": 1.0,
                "data": [{"verse_start": 1, "timestamp": 0.0}],
            },
            duration_seconds=1.0,
            chars_used=50,
        )


@pytest.fixture
def worklist_client():
    sword = MagicMock()
    sword.list_chapters.return_value = list(WORKLIST)
    return sword


@pytest.fixture(autouse=True)
def _reset_fakes():
    _FakeEdgeSynth.instances = []
    _FakeEdgeSynth.outcome = None
    yield
    _FakeEdgeSynth.outcome = None


@pytest.fixture
def gcs_mocks():
    # patch.multiple only populates the yielded dict for
    # attributes patched with DEFAULT.
    with patch.multiple(
        f"{_CMD}.gcs",
        acquire_run_lock=DEFAULT,
        list_completed_chapters=DEFAULT,
        upload_chapter_artifacts=DEFAULT,
        mark_run_complete=DEFAULT,
    ) as mocks:
        mocks["acquire_run_lock"].return_value = (True, "acquired")
        mocks["list_completed_chapters"].return_value = set()
        yield mocks


def _invoke_out(tmp_path, *extra):
    out = StringIO()
    call_command(
        "generate_edge_audio",
        "--output-dir", str(tmp_path / "tts-out"),
        "--state-file", str(tmp_path / "state.json"),
        "--sleep-seconds", "0",
        *extra, stdout=out,
    )
    return out.getvalue()


def _invoke_gcs_out(*extra):
    out = StringIO()
    call_command(
        "generate_edge_audio",
        "--upload-gcs",
        "--sleep-seconds", "0",
        *extra, stdout=out,
    )
    return out.getvalue()


@patch(f"{_CMD}.EdgeSynthesizer", _FakeEdgeSynth)
@patch(f"{_CMD}.get_default_sword_client")
def test_local_mode_writes_artifacts(
    sword_factory, tmp_path, worklist_client,
):
    sword_factory.return_value = worklist_client

    text = _invoke_out(tmp_path)

    base = tmp_path / "tts-out" / "LVSGLU8" / "GEN"
    for ch in (1, 2, 3):
        assert (base / f"{ch}.mp3").stat().st_size > 0
        assert json.loads(
            (base / f"{ch}_timestamps.json").read_text()
        )
    assert "generated=3" in text
    assert "reason=all_done" in text


@patch(f"{_CMD}.EdgeSynthesizer", _FakeEdgeSynth)
@patch(f"{_CMD}.get_default_sword_client")
def test_local_resume_skips_completed(
    sword_factory, tmp_path, worklist_client,
):
    """State file + on-disk artifacts both count as done."""
    sword_factory.return_value = worklist_client

    _invoke_out(tmp_path)
    _FakeEdgeSynth.instances = []
    _invoke_out(tmp_path)

    assert _FakeEdgeSynth.instances[-1].runs == []


@patch(f"{_CMD}.EdgeSynthesizer", _FakeEdgeSynth)
@patch(f"{_CMD}.get_default_sword_client")
def test_max_chapters_bounds_run(
    sword_factory, tmp_path, worklist_client,
):
    sword_factory.return_value = worklist_client

    text = _invoke_out(tmp_path, "--max-chapters", "2")

    synth = _FakeEdgeSynth.instances[-1]
    assert len(synth.runs) == 2
    assert "reason=chapter_cap" in text


@patch(f"{_CMD}.EdgeSynthesizer", _FakeEdgeSynth)
@patch(f"{_CMD}.get_default_sword_client")
def test_default_pacing_reaches_synthesizer(
    sword_factory, tmp_path, worklist_client,
):
    """Omitting --sleep-seconds passes the (raised) default to
    the synthesizer's courtesy pacing."""
    from bible.management.commands.generate_edge_audio import (
        DEFAULT_SLEEP_SECONDS,
    )
    sword_factory.return_value = worklist_client

    out = StringIO()
    call_command(
        "generate_edge_audio",
        "--output-dir", str(tmp_path / "tts-out"),
        "--state-file", str(tmp_path / "state.json"),
        stdout=out,
    )

    synth = _FakeEdgeSynth.instances[-1]
    assert synth.kwargs["min_interval_seconds"] == (
        DEFAULT_SLEEP_SECONDS
    )
    assert DEFAULT_SLEEP_SECONDS > 0.5


@patch(f"{_CMD}.EdgeSynthesizer", _FakeEdgeSynth)
@patch(f"{_CMD}.get_default_sword_client")
def test_edge_tts_error_continues(
    sword_factory, tmp_path, worklist_client,
):
    """A verse/chapter that fails after retries is skipped, not
    fatal to the run."""
    sword_factory.return_value = worklist_client

    def outcome(book_id, chapter):
        if chapter == 2:
            raise EdgeTTSError("no audio")
        return MagicMock(
            mp3_bytes=b"\xff\xfb" + b"\x00" * 20,
            timestamps_payload={"duration_seconds": 1.0, "data": []},
            duration_seconds=1.0,
            chars_used=50,
        )

    _FakeEdgeSynth.outcome = outcome
    text = _invoke_out(tmp_path)

    synth = _FakeEdgeSynth.instances[-1]
    assert synth.runs == [
        ("GEN", 1), ("GEN", 2), ("GEN", 3),
    ]
    assert "generated=2" in text


@patch(f"{_CMD}.EdgeSynthesizer", _FakeEdgeSynth)
@patch(f"{_CMD}.get_default_sword_client")
def test_unknown_fileset_rejected(
    sword_factory, tmp_path, worklist_client,
):
    sword_factory.return_value = worklist_client
    with pytest.raises(CommandError):
        _invoke_out(tmp_path, "--fileset-id", "NOPE")


@patch(f"{_CMD}.EdgeSynthesizer", _FakeEdgeSynth)
@patch(f"{_CMD}.get_default_sword_client")
def test_dry_run_generates_nothing(
    sword_factory, tmp_path, worklist_client,
):
    sword_factory.return_value = worklist_client

    text = _invoke_out(tmp_path, "--dry-run")

    assert "would generate 3 chapters" in text
    assert _FakeEdgeSynth.instances == []


@patch(f"{_CMD}.EdgeSynthesizer", _FakeEdgeSynth)
@patch(f"{_CMD}.get_default_sword_client")
def test_gcs_mode_uploads_and_completes(
    sword_factory, tmp_path, worklist_client, gcs_mocks,
):
    sword_factory.return_value = worklist_client

    text = _invoke_gcs_out()

    upload = gcs_mocks["upload_chapter_artifacts"]
    assert upload.call_count == 3
    first = upload.call_args_list[0]
    assert first.args[:3] == ("LVSGLU8", "GEN", 1)
    assert first.args[5] == "lv-LV-EveritaNeural"
    assert first.kwargs["mode"] == "edge"

    acquire = gcs_mocks["acquire_run_lock"]
    assert acquire.call_args.kwargs["mode"] == "edge"
    assert acquire.call_args.kwargs["ym"]

    mark = gcs_mocks["mark_run_complete"]
    mark.assert_called_once()
    assert mark.call_args.kwargs["reason"] == "all_done"
    assert mark.call_args.kwargs["mode"] == "edge"

    # Completion derives from the listing — nothing local.
    assert not (tmp_path / "tts-out").exists()
    assert "generated=3" in text


@patch(f"{_CMD}.EdgeSynthesizer", _FakeEdgeSynth)
@patch(f"{_CMD}.get_default_sword_client")
def test_gcs_listing_skips_completed(
    sword_factory, tmp_path, worklist_client, gcs_mocks,
):
    sword_factory.return_value = worklist_client
    gcs_mocks["list_completed_chapters"].return_value = {
        ("GEN", 1), ("GEN", 3),
    }

    _invoke_gcs_out()

    synth = _FakeEdgeSynth.instances[-1]
    assert synth.runs == [("GEN", 2)]
    assert gcs_mocks[
        "list_completed_chapters"
    ].call_args.kwargs["mode"] == "edge"


@patch(f"{_CMD}.EdgeSynthesizer", _FakeEdgeSynth)
@patch(f"{_CMD}.get_default_sword_client")
def test_gcs_lock_not_acquired_skips_run(
    sword_factory, tmp_path, worklist_client, gcs_mocks,
):
    sword_factory.return_value = worklist_client
    gcs_mocks["acquire_run_lock"].return_value = (
        False, "already_completed",
    )

    text = _invoke_gcs_out()

    assert "skipped reason=already_completed" in text
    assert _FakeEdgeSynth.instances == []
    gcs_mocks["upload_chapter_artifacts"].assert_not_called()
    gcs_mocks["mark_run_complete"].assert_not_called()


@patch(f"{_CMD}.EdgeSynthesizer", _FakeEdgeSynth)
@patch(f"{_CMD}.get_default_sword_client")
def test_gcs_all_failed_leaves_lock_uncompleted(
    sword_factory, tmp_path, worklist_client, gcs_mocks,
):
    sword_factory.return_value = worklist_client
    _FakeEdgeSynth.outcome = RuntimeError("boom")

    text = _invoke_gcs_out()

    assert "reason=all_failed" in text
    gcs_mocks["mark_run_complete"].assert_not_called()


@patch(f"{_CMD}.EdgeSynthesizer", _FakeEdgeSynth)
@patch(f"{_CMD}.get_default_sword_client")
def test_cloud_run_job_env_var_enables_gcs_mode(
    sword_factory, tmp_path, worklist_client, gcs_mocks,
):
    """Inside the Cloud Run Job container CLOUD_RUN_JOB is always
    set — GCS mode engages even without the flag."""
    sword_factory.return_value = worklist_client

    with patch.dict(os.environ, {"CLOUD_RUN_JOB": "all-jobs"}):
        text = _invoke_out(tmp_path)

    gcs_mocks["acquire_run_lock"].assert_called_once()
    assert "generated=3" in text


@patch(f"{_CMD}.EdgeSynthesizer", _FakeEdgeSynth)
@patch(f"{_CMD}.get_default_sword_client")
def test_gcs_dry_run_never_acquires_lock(
    sword_factory, tmp_path, worklist_client, gcs_mocks,
):
    sword_factory.return_value = worklist_client

    text = _invoke_gcs_out("--dry-run")

    assert "would generate 3 chapters" in text
    gcs_mocks["acquire_run_lock"].assert_not_called()
    assert _FakeEdgeSynth.instances == []


class TestEdgeSynthesizerTimestamps:
    """Verse timestamps come from measured per-call stream
    durations — exact boundaries, no estimation."""

    @patch(
        "bible.services.edge_tts.synthesizer"
        ".get_default_sword_client"
    )
    def test_exact_verse_offsets(self, sword_factory):
        from bible.services.edge_tts.synthesizer import (
            EdgeSynthesizer,
        )
        sword = MagicMock()
        sword.get_chapter_verses.return_value = list(VERSES)
        sword_factory.return_value = sword

        client = MagicMock()
        client.synthesize.side_effect = [
            _edge_result(10), _edge_result(20), _edge_result(5),
        ]
        synth = EdgeSynthesizer(client=client, voice_name="v")

        artifacts = synth.run("LVSGLU8", "GEN", 1)

        from bible.services.edge_tts.client import (
            mp3_duration_seconds,
        )
        mp3s = [
            b"\xff\xfb" + b"\x00" * (n * 100)
            for n in (10, 20, 5)
        ]
        durs = [mp3_duration_seconds(m) for m in mp3s]

        data = artifacts.timestamps_payload["data"]
        assert data[0]["timestamp"] == 0.0
        assert data[1]["timestamp"] == round(durs[0], 3)
        assert data[2]["timestamp"] == round(
            durs[0] + durs[1], 3
        )
        assert artifacts.duration_seconds == pytest.approx(
            sum(durs)
        )
        assert artifacts.mp3_bytes == b"".join(mp3s)
        assert artifacts.timestamps_payload[
            "file_size_bytes"
        ] == len(artifacts.mp3_bytes)

        calls = [
            c.kwargs["text"]
            for c in client.synthesize.call_args_list
        ]
        assert calls == ["aaa", "bbbb", "cc"]
