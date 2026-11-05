"""Tests for bible.services.audio_repair and the
``repair_audio_files`` / ``audit_audio_files --report``
commands. GCS is replaced by fakes/mocks; the Synthesizer seam
is patched so no TTS or SWORD I/O happens."""
import json
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from bible.services import audio_repair
from bible.services.google_tts.budget import BudgetExceeded
from bible.services.storage import gcs

_CMD = "bible.management.commands.repair_audio_files"
_AUDIT_CMD = "bible.management.commands.audit_audio_files"
_GCS = "bible.services.storage.gcs"

VOICE = "lv-LV-Chirp3-HD-Sadachbia"
NUM24 = f"audio/LVSGLU8/{VOICE}/NUM/24"
NUM24_MP3 = f"{NUM24}_audio.mp3"
NUM24_JSON = f"{NUM24}_timestamps.json"
GEN1_CH = f"audio_chapter/LVSGLU8/{VOICE}/GEN/1"
GEN1_MP3 = f"{GEN1_CH}_audio.mp3"
GEMINI_MP3 = f"audio_gemini/LVSGLU8/{VOICE}/EXO/2_audio.mp3"
EDGE_MP3 = f"audio_edge/LVSGLU8/{VOICE}/LEV/3_audio.mp3"


def make_manifest(names, issues=None, orphans=None):
    """Audit-report-shaped manifest for ``names``; ``issues`` maps
    name -> [codes] for the results block."""
    issues = issues or {}
    return {
        "bucket": "test-audio-bucket",
        "prefixes": ["audio", "audio_chapter"],
        "metadata_only": False,
        "deep": True,
        "scanned": len(names),
        "results": [
            {
                "name": n,
                "size": 100,
                "issues": [
                    {"code": c, "detail": "d"}
                    for c in issues.get(n, [])
                ],
            }
            for n in names
        ],
        "orphan_sidecars": orphans or [],
        "suspects": list(names) + list(orphans or []),
    }


def write_manifest(tmp_path, manifest):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return str(path)


# ---------------------------------------------------------------
# audio_repair.parse_artifact_name — engine routing per prefix
# ---------------------------------------------------------------


def test_parse_verse_mode_artifact():
    e = audio_repair.parse_artifact_name(NUM24_MP3)
    assert e.engine == "google_tts"
    assert e.mode == "verse"
    assert e.fileset_id == "LVSGLU8"
    assert e.voice_name == VOICE
    assert e.book_id == "NUM"
    assert e.chapter == 24
    assert e.base == NUM24


def test_parse_chapter_mode_artifact():
    e = audio_repair.parse_artifact_name(GEN1_MP3)
    assert e.engine == "google_tts"
    assert e.mode == "chapter"
    assert e.base == GEN1_CH


def test_parse_sidecar_maps_to_same_base():
    mp3 = audio_repair.parse_artifact_name(NUM24_MP3)
    sidecar = audio_repair.parse_artifact_name(NUM24_JSON)
    assert sidecar.base == mp3.base


def test_parse_gemini_and_edge_prefixes():
    assert audio_repair.parse_artifact_name(
        GEMINI_MP3
    ).engine == "gemini"
    assert audio_repair.parse_artifact_name(
        EDGE_MP3
    ).engine == "edge"


@pytest.mark.parametrize("name", [
    "audio/LVSGLU8/voice/NUM/24.mp3",          # missing _audio
    "audio/LVSGLU8/voice/NUM/x_audio.mp3",     # non-int chapter
    "audio/LVSGLU8/voice/24_audio.mp3",        # missing book dir
    "timestamps_generated/X/Y/1_timestamps.json",
    "state/usage/LVSGLU8/2026-01.json",
    "random-name",
    "audio/LVSGLU8/voice/NUM/24_audio.MP3",
])
def test_parse_rejects_non_artifact_names(name):
    assert audio_repair.parse_artifact_name(name) is None


# ---------------------------------------------------------------
# manifest_entries
# ---------------------------------------------------------------


def test_manifest_entries_report_form_with_issues():
    manifest = make_manifest(
        [NUM24_MP3], issues={NUM24_MP3: ["clipped"]},
    )
    entries, invalid = audio_repair.manifest_entries(manifest)
    assert invalid == []
    assert [e.name for e in entries] == [NUM24_MP3]
    assert entries[0].issues == ["clipped"]


def test_manifest_entries_bare_list_form():
    entries, invalid = audio_repair.manifest_entries([NUM24_MP3])
    assert invalid == []
    assert [e.name for e in entries] == [NUM24_MP3]


def test_manifest_entries_dedups_chapter_pair():
    # A manifest listing both halves of a pair repairs once.
    manifest = make_manifest(
        [NUM24_MP3],
        issues={NUM24_MP3: ["desync"]},
        orphans=[NUM24_JSON],
    )
    entries, _ = audio_repair.manifest_entries(manifest)
    assert len(entries) == 1
    assert entries[0].base == NUM24
    assert sorted(entries[0].issues) == [
        "desync", "orphan_sidecar",
    ]


def test_manifest_entries_orphan_gets_code():
    manifest = make_manifest([NUM24_MP3], orphans=[NUM24_JSON])
    entries, _ = audio_repair.manifest_entries(
        {**manifest, "suspects": [NUM24_JSON]},
    )
    assert entries[0].issues == ["orphan_sidecar"]


def test_manifest_entries_collects_invalid_names():
    manifest = make_manifest([NUM24_MP3, "bogus", "also/bad"])
    entries, invalid = audio_repair.manifest_entries(manifest)
    assert [e.name for e in entries] == [NUM24_MP3]
    assert invalid == ["bogus", "also/bad"]


def test_manifest_entries_rejects_bad_shapes():
    with pytest.raises(ValueError):
        audio_repair.manifest_entries({"no_suspects": []})
    with pytest.raises(ValueError):
        audio_repair.manifest_entries({"suspects": "nope"})
    with pytest.raises(ValueError):
        audio_repair.manifest_entries(42)


# ---------------------------------------------------------------
# gcs repair-state ledger
# ---------------------------------------------------------------


def test_repair_state_object_path():
    assert gcs.repair_state_object_path(NUM24) == (
        f"state/repair/{NUM24}.json"
    )


def test_list_repair_state_reads_ledger():
    class B:
        def __init__(self, name):
            self.name = name

    class FakeBucket:
        def list_blobs(self, prefix=None):
            names = [
                f"state/repair/{NUM24}.json",
                "state/repair/other/nojson",
                "state/lock/LVSGLU8/2026-01.json",
            ]
            return [
                B(n) for n in names
                if prefix is None or n.startswith(prefix)
            ]

    with patch(f"{_GCS}._bucket", return_value=FakeBucket()):
        assert gcs.list_repair_state() == {NUM24}


# ---------------------------------------------------------------
# repair_audio_files command — dry-run (default)
# ---------------------------------------------------------------


def _patches(repaired=(), usage=0):
    """Patch specs for the command's GCS + TTS seams; pass to
    ``_run`` and override entries (e.g. ``synth_cls``) first."""
    mocks = {
        "synth_cls": patch(f"{_CMD}.Synthesizer"),
        "list_state": patch(
            f"{_CMD}.gcs.list_repair_state",
            return_value=set(repaired),
        ),
        "read_usage": patch(
            f"{_CMD}.gcs.read_monthly_usage",
            return_value=usage,
        ),
        "increment": patch(
            f"{_CMD}.gcs.increment_monthly_usage"
        ),
        "upload": patch(f"{_CMD}.gcs.upload_chapter_artifacts"),
        "mark": patch(f"{_CMD}.gcs.mark_entry_repaired"),
    }
    return mocks


def _run(manifest_path, mocks, *argv):
    """Run the command; returns ``(stdout, started_mocks, exc)`` —
    ``exc`` is the raised exception or ``None``."""
    out = StringIO()
    exc = None
    started = {
        k: p.start() for k, p in mocks.items()
    }
    try:
        call_command(
            "repair_audio_files",
            "--manifest", manifest_path,
            *argv, stdout=out,
        )
    except Exception as e:  # noqa: BLE001
        exc = e
    finally:
        for p in mocks.values():
            p.stop()
    return out.getvalue(), started, exc


def test_dry_run_prints_plan_and_writes_nothing(tmp_path):
    path = write_manifest(
        tmp_path,
        make_manifest(
            [NUM24_MP3, GEN1_MP3, GEMINI_MP3],
            issues={NUM24_MP3: ["clipped"]},
        ),
    )
    out, mocks, exc = _run(path, _patches())
    assert exc is None
    assert "REPAIR " + NUM24_MP3 in out
    assert "mode=verse" in out
    assert "mode=chapter" in out
    assert f"SKIP   {GEMINI_MP3}  unsupported_engine=gemini" in out
    assert "clipped" in out
    assert "dry-run" in out
    mocks["synth_cls"].assert_not_called()
    mocks["upload"].assert_not_called()
    mocks["mark"].assert_not_called()
    mocks["increment"].assert_not_called()


# ---------------------------------------------------------------
# --apply
# ---------------------------------------------------------------


def _synth_ok(chars=50):
    inst = MagicMock()

    def run(_fs, _book, _ch, budget=None, mode=None):
        if budget is not None:
            budget.consume(chars)
        return MagicMock(
            mp3_bytes=b"mp3",
            timestamps_payload={"data": []},
            duration_seconds=1.5,
            chars_used=chars,
        )

    inst.run.side_effect = run
    return inst


def test_apply_repairs_and_marks_state(tmp_path):
    path = write_manifest(
        tmp_path, make_manifest([NUM24_MP3, GEN1_MP3]),
    )
    inst = _synth_ok()
    mocks = _patches()
    mocks["synth_cls"] = patch(
        f"{_CMD}.Synthesizer", return_value=inst,
    )
    out, mocks_started, exc = _run(path, mocks, "--apply")
    assert exc is None
    assert f"FIXED  {NUM24_MP3}" in out
    assert f"FIXED  {GEN1_MP3}" in out
    assert "repaired=2" in out
    # The engine mode is threaded through per entry.
    modes = [c.kwargs["mode"] for c in inst.run.call_args_list]
    assert modes == ["verse", "chapter"]
    upload = mocks_started["upload"]
    assert upload.call_count == 2
    assert upload.call_args_list[0].kwargs["mode"] == "verse"
    assert upload.call_args_list[1].kwargs["mode"] == "chapter"
    mark = mocks_started["mark"]
    assert [c.args[0] for c in mark.call_args_list] == [
        NUM24, GEN1_CH,
    ]
    # Both chapters' billed chars hit the monthly counter.
    increment = mocks_started["increment"]
    assert [c.args[1] for c in increment.call_args_list] == [
        50, 50,
    ]


def test_apply_skips_unsupported_engines(tmp_path):
    path = write_manifest(
        tmp_path, make_manifest([GEMINI_MP3, EDGE_MP3]),
    )
    out, mocks, exc = _run(path, _patches(), "--apply")
    assert exc is None
    assert "unsupported_engine=gemini" in out
    assert "unsupported_engine=edge" in out
    assert "repaired=0" in out and "skipped=2" in out
    mocks["synth_cls"].assert_not_called()
    mocks["upload"].assert_not_called()


def test_apply_skips_already_repaired_and_force_overrides(
    tmp_path,
):
    path = write_manifest(tmp_path, make_manifest([NUM24_MP3]))
    inst = _synth_ok()
    mocks = _patches(repaired={NUM24})
    mocks["synth_cls"] = patch(
        f"{_CMD}.Synthesizer", return_value=inst,
    )
    out, _m, exc = _run(path, mocks, "--apply")
    assert exc is None
    assert f"SKIP   {NUM24_MP3}  already_repaired" in out
    inst.run.assert_not_called()

    inst2 = _synth_ok()
    mocks = _patches(repaired={NUM24})
    mocks["synth_cls"] = patch(
        f"{_CMD}.Synthesizer", return_value=inst2,
    )
    out, _m, exc = _run(path, mocks, "--apply", "--force")
    assert exc is None
    assert f"FIXED  {NUM24_MP3}" in out
    inst2.run.assert_called_once()


def test_budget_guard_stops_run_midway(tmp_path):
    """The monthly char cap must stop the pass — remaining
    entries in the same fileset are skipped, not retried."""
    path = write_manifest(
        tmp_path,
        make_manifest([NUM24_MP3, GEN1_MP3]),
    )
    inst = MagicMock()

    def run(_fs, _book, _ch, budget=None, mode=None):
        # First chapter bills 60 of the 100-char headroom, the
        # second needs more than the 40 left -> BudgetExceeded
        # is raised by the real CharBudget.consume.
        if budget is not None:
            budget.consume(60)
        if mode == "chapter":
            raise BudgetExceeded("no headroom")
        return MagicMock(
            mp3_bytes=b"mp3",
            timestamps_payload={"data": []},
            duration_seconds=1.0,
            chars_used=60,
        )

    inst.run.side_effect = run
    mocks = _patches(usage=0)
    mocks["synth_cls"] = patch(
        f"{_CMD}.Synthesizer", return_value=inst,
    )
    # Cap = 100; chapter 1 spends 60, chapter 2's 60-char request
    # busts the 40-char headroom -> fileset marked exhausted.
    with override_settings(MONTHLY_TTS_CHAR_LIMIT=100):
        out, mocks_started, exc = _run(path, mocks, "--apply")
    assert exc is None
    assert f"FIXED  {NUM24_MP3}" in out
    assert f"SKIP   {GEN1_MP3}  budget_exhausted" in out
    assert "repaired=1" in out
    increment = mocks_started["increment"]
    # Only the 60 billed chars were persisted.
    assert [c.args[1] for c in increment.call_args_list] == [60]


def test_failed_regen_leaves_blob_and_ledger_untouched(tmp_path):
    """A failed regeneration must not delete/overwrite the bad
    blob and must not mark the entry repaired."""
    path = write_manifest(tmp_path, make_manifest([NUM24_MP3]))
    inst = MagicMock()
    inst.run.side_effect = RuntimeError("tts exploded")
    mocks = _patches()
    mocks["synth_cls"] = patch(
        f"{_CMD}.Synthesizer", return_value=inst,
    )
    out, started, exc = _run(path, mocks, "--apply")
    assert isinstance(exc, CommandError)
    assert "failed" in str(exc)
    assert "FAILED " + NUM24_MP3 in out
    assert "regen_failed" in out
    started["upload"].assert_not_called()
    started["mark"].assert_not_called()
    started["increment"].assert_not_called()


def test_upload_failure_fails_entry_but_persists_billed_chars(
    tmp_path,
):
    path = write_manifest(tmp_path, make_manifest([NUM24_MP3]))
    inst = _synth_ok(chars=75)
    mocks = _patches()
    mocks["synth_cls"] = patch(
        f"{_CMD}.Synthesizer", return_value=inst,
    )
    mocks["upload"] = patch(
        f"{_CMD}.gcs.upload_chapter_artifacts",
        side_effect=RuntimeError("gcs 503"),
    )
    out, started, exc = _run(path, mocks, "--apply")
    assert isinstance(exc, CommandError)
    assert "upload_failed" in out
    started["mark"].assert_not_called()
    # The 75 characters Google billed still land in the counter.
    assert [c.args[1] for c in started[
        "increment"
    ].call_args_list] == [75]


# ---------------------------------------------------------------
# manifest loading: local + gs:// + defaults
# ---------------------------------------------------------------


class _FakeBlob:
    def __init__(self, name=None, data=None):
        self.name = name
        self._data = data
        self.size = len(data) if data is not None else 0
        self.uploads = []

    def download_as_bytes(self):
        if self._data is None:
            from google.api_core import exceptions as gax
            raise gax.NotFound("missing")
        return self._data

    def upload_from_string(self, text, content_type=None):
        self.uploads.append(text)


class _FakeBucket:
    def __init__(self, blobs=None):
        self.blobs = blobs or {}
        self.created = {}

    def blob(self, name):
        return self.blobs.get(name) or self.created.setdefault(
            name, _FakeBlob(name)
        )

    def list_blobs(self, prefix=None):
        return [
            b for b in self.blobs.values()
            if prefix is None or b.name.startswith(prefix)
        ]


class _FakeClient:
    def __init__(self, bucket):
        self._bucket = bucket

    def bucket(self, name):
        return self._bucket


def test_manifest_loads_from_gs_uri(tmp_path):
    manifest = make_manifest([NUM24_MP3])
    blob = _FakeBlob(
        "manifests/m.json", json.dumps(manifest).encode(),
    )
    client = _FakeClient(_FakeBucket({"manifests/m.json": blob}))
    mocks = _patches()
    try:
        for p in mocks.values():
            p.start()
        with patch(
            f"{_GCS}.get_default_client", return_value=client,
        ):
            out = StringIO()
            call_command(
                "repair_audio_files",
                "--manifest", "gs://b/manifests/m.json",
                stdout=out,
            )
    finally:
        for p in mocks.values():
            p.stop()
    assert "REPAIR " + NUM24_MP3 in out.getvalue()


def test_missing_explicit_manifest_errors(tmp_path):
    with pytest.raises(CommandError, match="manifest not found"):
        call_command(
            "repair_audio_files",
            "--manifest", str(tmp_path / "nope.json"),
            stdout=StringIO(),
        )


def test_missing_default_manifest_exits_cleanly():
    mocks = {"list_state": patch(
        f"{_CMD}.gcs.list_repair_state", return_value=set(),
    )}
    try:
        for p in mocks.values():
            p.start()
        with patch(
            f"{_GCS}.get_default_client",
            return_value=_FakeClient(_FakeBucket()),
        ):
            out = StringIO()
            call_command("repair_audio_files", stdout=out)
    finally:
        for p in mocks.values():
            p.stop()
    assert "nothing to repair" in out.getvalue()


def test_invalid_json_manifest_errors(tmp_path):
    path = tmp_path / "m.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(CommandError, match="not valid JSON"):
        call_command(
            "repair_audio_files", "--manifest", str(path),
            stdout=StringIO(),
        )


def test_report_manifest_shape_errors(tmp_path):
    path = write_manifest(tmp_path, {"no_suspects": []})
    with pytest.raises(CommandError, match="suspects"):
        call_command(
            "repair_audio_files", "--manifest", path,
            stdout=StringIO(),
        )


# ---------------------------------------------------------------
# audit_audio_files --report
# ---------------------------------------------------------------


def test_audit_report_writes_manifest_file(tmp_path):
    from bible.tests.test_audio_audit import (
        FakeBucket, FakeClient, good_objects,
    )

    report_path = tmp_path / "report.json"
    out = StringIO()
    with patch(
        f"{_AUDIT_CMD}.gcs.get_default_client",
        return_value=FakeClient(FakeBucket(good_objects())),
    ):
        call_command(
            "audit_audio_files",
            "--report", str(report_path),
            stdout=out,
        )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["suspects"] == []
    assert report["scanned"] == 1


def test_audit_report_written_before_nonzero_exit(tmp_path):
    from bible.tests.test_audio_audit import (
        FakeBucket, FakeClient, NUM24_JSON, NUM24_MP3,
        make_mp3, make_sidecar,
    )

    objects = {
        NUM24_MP3: make_mp3(50) + b"JUNK" + make_mp3(50),
        NUM24_JSON: make_sidecar(duration=2.4, size=9648),
    }
    report_path = tmp_path / "report.json"
    with patch(
        f"{_AUDIT_CMD}.gcs.get_default_client",
        return_value=FakeClient(FakeBucket(objects)),
    ):
        with pytest.raises(CommandError):
            call_command(
                "audit_audio_files",
                "--report", str(report_path),
                stdout=StringIO(),
            )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["suspects"] == [NUM24_MP3]
    # The written file is a consumable repair manifest.
    entries, invalid = audio_repair.manifest_entries(report)
    assert invalid == []
    assert entries[0].base == NUM24


def test_audit_report_supports_gs_uri():
    from bible.tests.test_audio_audit import (
        FakeClient, make_mp3, make_sidecar,
    )

    blob = _FakeBlob("manifests/report.json")
    bucket = _FakeBucket({
        "manifests/report.json": blob,
        NUM24_MP3: _FakeBlob(NUM24_MP3, make_mp3(100)),
        NUM24_JSON: _FakeBlob(
            NUM24_JSON, make_sidecar(duration=2.4, size=9600),
        ),
    })
    client = _FakeClient(bucket)
    with patch(
        f"{_AUDIT_CMD}.gcs.get_default_client",
        return_value=client,
    ):
        call_command(
            "audit_audio_files",
            "--report", "gs://b/manifests/report.json",
            stdout=StringIO(),
        )
    assert blob.uploads
    report = json.loads(blob.uploads[0])
    assert report["scanned"] == 1
