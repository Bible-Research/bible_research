"""Manifest parsing and engine routing for audio repair.

Pure logic for the ``repair_audio_files`` management command — no
GCS or TTS client usage, so the planning half is unit-testable
with plain ``dict``/``str`` inputs. A *manifest* is the JSON
report produced by ``audit_audio_files --json`` (or ``--report``):
its ``suspects`` list names the blobs to regenerate and its
``results`` list carries the issue codes that flagged them.

Each suspect name is a chapter artifact —
``<prefix>/<FILESET>/<VOICE>/<BOOK>/<CH>_audio.mp3`` or the
``<CH>_timestamps.json`` sidecar — where ``prefix`` selects the
producing engine (see ``gcs._MODE_PREFIXES``):

- ``audio``         → Google Cloud TTS, verse-synthesis mode
- ``audio_chapter`` → Google Cloud TTS, chapter-synthesis mode
- ``audio_gemini``  → Gemini free-tier pipeline
- ``audio_edge``    → Edge read-aloud endpoint

Repair granularity is the whole chapter pair: regeneration
overwrites both ``*_audio.mp3`` and ``*_timestamps.json`` via
``gcs.upload_chapter_artifacts``. Only the Google TTS engines are
supported today; Gemini and Edge entries are reported as
``unsupported_engine`` rather than repaired.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from bible.services import audio_audit

# Engines a repair pass can actually regenerate for. Gemini and
# Edge artifacts are deliberately out of scope: their generation
# paths are request-capped (not char-budgeted) pipelines whose
# per-day quota handling doesn't fit this command's flow.
ENGINE_GOOGLE_TTS = "google_tts"
SUPPORTED_ENGINES = {ENGINE_GOOGLE_TTS}

# Artifact prefix -> (engine, google_tts synthesis mode or None).
# Must stay in sync with ``gcs._MODE_PREFIXES``.
PREFIX_ENGINES = {
    "audio": (ENGINE_GOOGLE_TTS, "verse"),
    "audio_chapter": (ENGINE_GOOGLE_TTS, "chapter"),
    "audio_gemini": ("gemini", None),
    "audio_edge": ("edge", None),
}

_ARTIFACT_RE = re.compile(
    r"^(?P<prefix>[a-z_]+)"
    r"/(?P<fileset>[^/]+)"
    r"/(?P<voice>[^/]+)"
    r"/(?P<book>[^/]+)"
    r"/(?P<chapter>\d+)"
    r"(?P<suffix>_audio\.mp3|_timestamps\.json)$"
)


@dataclass
class RepairEntry:
    """One repairable chapter unit parsed from a suspect name."""

    name: str            # the suspect blob name from the manifest
    prefix: str          # first path segment, e.g. "audio_chapter"
    fileset_id: str
    voice_name: str
    book_id: str
    chapter: int
    engine: str          # "google_tts" | "gemini" | "edge"
    mode: Optional[str]  # "verse" | "chapter" for google_tts
    issues: List[str] = field(default_factory=list)

    @property
    def base(self) -> str:
        """Artifact path without suffix — the repair unit key.

        ``audio/LVSGLU8/v/NUM/24`` identifies the chapter pair the
        ``*_audio.mp3`` suspect and an ``*_timestamps.json`` orphan
        both point at; the repair-state ledger is keyed on it."""
        for suffix in (
            audio_audit.AUDIO_SUFFIX,
            audio_audit.TIMESTAMPS_SUFFIX,
        ):
            if self.name.endswith(suffix):
                return self.name[: -len(suffix)]
        return self.name


def parse_artifact_name(name: str) -> Optional[RepairEntry]:
    """Parse a suspect blob name into a ``RepairEntry``.

    Returns ``None`` for names that do not match the chapter
    artifact layout (unknown prefix, missing segments, or a
    non-chapter filename) — the command reports those as
    ``invalid_name`` skips rather than aborting the run."""
    match = _ARTIFACT_RE.match(name.strip())
    if not match:
        return None
    prefix = match.group("prefix")
    engine_mode = PREFIX_ENGINES.get(prefix)
    if engine_mode is None:
        return None
    engine, mode = engine_mode
    return RepairEntry(
        name=name.strip(),
        prefix=prefix,
        fileset_id=match.group("fileset"),
        voice_name=match.group("voice"),
        book_id=match.group("book"),
        chapter=int(match.group("chapter")),
        engine=engine,
        mode=mode,
    )


def manifest_entries(
    manifest,
) -> Tuple[List[RepairEntry], List[str]]:
    """Extract repair entries from an audit report manifest.

    Accepts the full ``audit_audio_files --json`` report object
    (``suspects`` names the blobs; ``results`` supplies the issue
    codes) or a bare JSON list of blob names.

    Returns ``(entries, invalid)``: ``entries`` is the deduplicated
    (by chapter-pair base), sorted list of ``RepairEntry`` objects;
    ``invalid`` holds suspect names that failed to parse."""
    if isinstance(manifest, dict):
        suspects = manifest.get("suspects")
        if not isinstance(suspects, list):
            raise ValueError(
                "manifest object has no 'suspects' list — pass the "
                "audit report JSON or a bare list of blob names"
            )
        results = manifest.get("results") or []
        issues_by_name = {}
        for r in results:
            if not isinstance(r, dict) or not r.get("name"):
                continue
            codes = [
                i.get("code")
                for i in r.get("issues") or []
                if isinstance(i, dict) and i.get("code")
            ]
            issues_by_name[r["name"]] = codes
        # Orphan sidecars are suspects without a ``results`` row —
        # synthesize their code so the plan stays informative.
        for orphan in manifest.get("orphan_sidecars") or []:
            issues_by_name.setdefault(orphan, ["orphan_sidecar"])
    elif isinstance(manifest, list):
        suspects = manifest
        issues_by_name = {}
    else:
        raise ValueError(
            "manifest is neither a report object nor a list — "
            "pass the audit report JSON"
        )

    entries: dict = {}
    invalid: List[str] = []
    for raw in suspects:
        if not isinstance(raw, str) or not raw.strip():
            invalid.append(repr(raw))
            continue
        name = raw.strip()
        entry = parse_artifact_name(name)
        if entry is None:
            invalid.append(name)
            continue
        # Both suffixes of one chapter pair map to the same base:
        # merge rather than repairing the same unit twice.
        existing = entries.get(entry.base)
        if existing is not None:
            for code in issues_by_name.get(name, []):
                if code not in existing.issues:
                    existing.issues.append(code)
            continue
        entry.issues = list(issues_by_name.get(name, []))
        entries[entry.base] = entry
    return sorted(entries.values(), key=lambda e: e.base), invalid
