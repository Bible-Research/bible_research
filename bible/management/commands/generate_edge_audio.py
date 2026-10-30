"""Generate LVSGLU8 chapter audio via the Edge read-aloud TTS
endpoint (edge-tts), voice ``lv-LV-EveritaNeural`` by default.

Mirrors ``generate_gemini_audio``'s dual-mode shape:

- **Local** (default): writes MP3 + timestamps under ``tts-out/``
  and resumes via the same gitignored JSON state file.
- **GCS** (``--upload-gcs``, or auto-enabled when the
  ``CLOUD_RUN_JOB`` env var is set — i.e. inside the ``all-jobs``
  Cloud Run Job): uploads artifacts to the audio bucket under
  ``audio_edge/`` (GCS mode ``"edge"``) and derives completion
  from the object listing. A day-keyed run lock serializes
  executions and a successful run marks it complete.

There is no API key and no formal quota — the run is bounded by
``--max-chapters``/``--max-minutes`` so it always finishes inside
the Cloud Run Job's timeout; the daily scheduler re-triggers it
until the listing shows every chapter done.
"""
import logging
import os
import time
from pathlib import Path
from typing import Optional

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from bible.management.commands.generate_gemini_audio import (
    _default_output_dir,
    chapter_files_exist,
    write_chapter_artifacts,
)
from bible.services.edge_tts.client import (
    DEFAULT_VOICE_NAME,
    EdgeTTSError,
    EdgeTTSClient,
)
from bible.services.edge_tts.synthesizer import EdgeSynthesizer
from bible.services.gemini_tts.state import RunState, today_pacific
from bible.services.storage import gcs
from bible.services.sword.client import get_default_sword_client
from bible.services.sword.registry import canonical_sword_fileset_id

logger = logging.getLogger(__name__)

# A verse is one request (~1 s synth + pacing); 60 chapters is
# ~30-40 min, comfortably inside the Job's 5400 s timeout.
DEFAULT_MAX_CHAPTERS = 60
# Second safety bound so a slow streak can't outlive the timeout.
DEFAULT_MAX_MINUTES = 60.0
# Courtesy pacing — the endpoint is undocumented, bursts are the
# likeliest throttle trigger.
DEFAULT_SLEEP_SECONDS = 0.5


class Command(BaseCommand):
    help = (
        "Generate chapter audio via the Edge read-aloud endpoint, "
        "resumable via GCS listing (cloud) or JSON state (local)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--fileset-id", default="LVSGLU8",
            help="Canonical SWORD fileset id (default: LVSGLU8).",
        )
        parser.add_argument(
            "--max-chapters", type=int,
            default=DEFAULT_MAX_CHAPTERS,
            help=(
                "Max chapters to generate this run "
                f"(default: {DEFAULT_MAX_CHAPTERS})."
            ),
        )
        parser.add_argument(
            "--max-minutes", type=float,
            default=DEFAULT_MAX_MINUTES,
            help=(
                "Wall-clock bound per run "
                f"(default: {DEFAULT_MAX_MINUTES})."
            ),
        )
        parser.add_argument(
            "--sleep-seconds", type=float,
            default=DEFAULT_SLEEP_SECONDS,
            help=(
                "Minimum spacing between API calls "
                f"(default: {DEFAULT_SLEEP_SECONDS})."
            ),
        )
        parser.add_argument(
            "--output-dir", default=None,
            help="Local output root (default: <repo>/tts-out).",
        )
        parser.add_argument(
            "--state-file", default=None,
            help=(
                "Override the JSON state file path "
                "(default: <repo>/gemini-tts-state.json)."
            ),
        )
        parser.add_argument(
            "--voice", default=None,
            help=(
                "Edge neural voice name (default: "
                f"{DEFAULT_VOICE_NAME})."
            ),
        )
        parser.add_argument(
            "--upload-gcs", action="store_true",
            help=(
                "Upload artifacts to the audio bucket under "
                "audio_edge/ and derive completion from the "
                "listing (Cloud Run Job mode; also enabled "
                "automatically when CLOUD_RUN_JOB is set)."
            ),
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="List pending chapters; do not call the API.",
        )

    def handle(self, *args, **options):
        fileset_id = canonical_sword_fileset_id(
            options["fileset_id"]
        )
        if not fileset_id:
            raise CommandError(
                f"Unknown fileset_id: {options['fileset_id']!r}"
            )
        upload_gcs = (
            options["upload_gcs"]
            or bool(os.environ.get("CLOUD_RUN_JOB"))
        )
        voice_name = options["voice"] or DEFAULT_VOICE_NAME

        if upload_gcs:
            output_dir: Optional[Path] = None
            completed = set(
                gcs.list_completed_chapters(
                    fileset_id, voice_name, mode="edge",
                )
            )
            state = None
        else:
            output_dir = Path(
                options["output_dir"] or _default_output_dir()
            )
            state = RunState(
                Path(options["state_file"])
                if options["state_file"] else None
            )
            completed = set()

        worklist = get_default_sword_client().list_chapters(
            fileset_id
        )
        pending = self._pending(
            worklist, state, completed, output_dir, fileset_id,
        )
        logger.info(
            "fileset=%s total=%d completed=%d pending=%d",
            fileset_id, len(worklist),
            len(worklist) - len(pending), len(pending),
        )
        if options["dry_run"]:
            self.stdout.write(
                f"would generate {len(pending)} chapters "
                f"(max_chapters={options['max_chapters']})"
            )
            return

        lock_day = None
        if upload_gcs:
            # Day-keyed run lock: serializes same-day executions,
            # and a completed run makes later same-day triggers
            # exit early (``already_completed``). A crashed run
            # leaves a ``running`` lock that goes stale after
            # LOCK_STALE_HOURS; tomorrow uses a new day key.
            lock_day = today_pacific()
            acquired, lock_reason = gcs.acquire_run_lock(
                fileset_id,
                stale_after_hours=settings.LOCK_STALE_HOURS,
                ym=lock_day,
                mode="edge",
            )
            if not acquired:
                logger.info(
                    "Skipping edge run for %s on %s: %s",
                    fileset_id, lock_day, lock_reason,
                )
                self.stdout.write(f"skipped reason={lock_reason}")
                return

        client = EdgeTTSClient(voice_name=voice_name)
        synthesizer = EdgeSynthesizer(
            client=client,
            voice_name=voice_name,
            min_interval_seconds=options["sleep_seconds"],
        )

        generated = 0
        chars_used = 0
        reason = "all_done"
        started = time.monotonic()
        max_chapters = options["max_chapters"]
        max_seconds = options["max_minutes"] * 60

        for book_id, chap in pending:
            if generated >= max_chapters:
                reason = "chapter_cap"
                logger.info(
                    "Chapter cap reached (%d); next run resumes "
                    "from the listing.", generated,
                )
                break
            if time.monotonic() - started > max_seconds:
                reason = "time_cap"
                logger.info(
                    "Time cap reached; next run resumes from the "
                    "listing.",
                )
                break
            try:
                artifacts = synthesizer.run(
                    fileset_id, book_id, chap,
                )
            except EdgeTTSError as exc:
                logger.warning(
                    "edge-tts failed at %s %s: %s",
                    book_id, chap, exc,
                )
                continue
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "Failed to generate %s %s: %s",
                    book_id, chap, exc,
                )
                continue

            try:
                if upload_gcs:
                    gcs.upload_chapter_artifacts(
                        fileset_id, book_id, chap,
                        artifacts.mp3_bytes,
                        artifacts.timestamps_payload,
                        voice_name,
                        mode="edge",
                    )
                else:
                    write_chapter_artifacts(
                        output_dir, fileset_id, book_id, chap,
                        artifacts.mp3_bytes,
                        artifacts.timestamps_payload,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "Failed to persist %s %s artifacts: %s",
                    book_id, chap, exc,
                )
                continue

            if state is not None:
                state.mark_completed(book_id, chap)
                state.save()
            else:
                completed.add((book_id, int(chap)))
            generated += 1
            chars_used += artifacts.chars_used
            logger.info(
                "Generated %s %s chars=%d duration=%.2fs "
                "requests=%d",
                book_id, chap, artifacts.chars_used,
                artifacts.duration_seconds, client.requests_made,
            )

        if upload_gcs:
            # Mirror generate_gemini_audio: a run where every
            # pending chapter failed must not record a
            # zero-artifact "completed" lock — a real outage would
            # then look done. Leave it ``running`` so the stale
            # window allows a retry.
            if (
                pending and generated == 0
                and reason == "all_done"
            ):
                reason = "all_failed"
                logger.error(
                    "All %d pending chapters failed; leaving "
                    "the day lock un-completed.",
                    len(pending),
                )
            else:
                gcs.mark_run_complete(
                    fileset_id,
                    chars_used=chars_used,
                    chapters_generated=generated,
                    reason=reason,
                    ym=lock_day,
                    mode="edge",
                )

        self.stdout.write(
            f"done generated={generated} "
            f"requests={client.requests_made} "
            f"reason={reason}"
        )

    @staticmethod
    def _pending(
        worklist,
        state: Optional[RunState],
        completed: set,
        output_dir: Optional[Path],
        fileset_id: str,
    ) -> list:
        """Worklist minus completed chapters.

        Local mode checks the JSON state plus files on disk (a
        state-deleted chapter with intact files is marked complete
        without re-synthesizing). GCS mode checks the artifact
        listing — the objects themselves are the ledger."""
        pending = []
        for book_id, chap in worklist:
            if (book_id, int(chap)) in completed:
                continue
            if state is not None and state.is_completed(
                book_id, chap,
            ):
                continue
            if (
                output_dir is not None
                and chapter_files_exist(
                    output_dir, fileset_id, book_id, chap,
                )
            ):
                if state is not None:
                    state.mark_completed(book_id, chap)
                continue
            pending.append((book_id, chap))
        return pending
