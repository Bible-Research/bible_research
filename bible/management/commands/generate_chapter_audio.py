"""Generate Cloud TTS audio + timestamps for missing chapters and upload
them to GCS. Designed to be invoked by a Cloud Run Job once per month."""
import logging

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from bible.services.google_tts.budget import BudgetExceeded, CharBudget
from bible.services.google_tts.client import QuotaExceeded
from bible.services.google_tts.registry import get_tts_config
from bible.services.google_tts.synthesizer import Synthesizer
from bible.services.storage import gcs
from bible.services.sword.client import get_default_sword_client
from bible.services.sword.registry import canonical_sword_fileset_id

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Generate missing chapter audio + timestamps via Cloud TTS."

    def add_arguments(self, parser):
        parser.add_argument(
            "--fileset-id", default="LVSGLU8",
            help="Canonical SWORD fileset id (default: LVSGLU8).",
        )
        parser.add_argument(
            "--mode", choices=["verse", "chapter", "both"],
            default="verse",
            help=(
                "Generation mode. 'verse' (default) synthesizes each "
                "verse separately and concatenates — current behavior, "
                "artifacts under audio/. 'chapter' synthesizes whole "
                "chapters with SSML <mark> verse timestamps, artifacts "
                "under audio_chapter/. 'both' runs a verse pass then a "
                "chapter pass over the same worklist; each mode has "
                "its own run lock but shares the monthly char budget."
            ),
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="List what would be generated; do not call TTS or write GCS.",
        )

    def handle(self, *args, **options):
        fileset_id = canonical_sword_fileset_id(options["fileset_id"])
        if not fileset_id:
            raise CommandError(
                f"Unknown fileset_id: {options['fileset_id']!r}"
            )
        modes = (
            ["verse", "chapter"]
            if options["mode"] == "both"
            else [options["mode"]]
        )

        # Worklist comes straight from the SWORD module so we never
        # assume ESV/KJV versification (e.g. Glück 1877 may have
        # different book / chapter counts).
        worklist = get_default_sword_client().list_chapters(fileset_id)

        for mode in modes:
            self._run_mode(
                fileset_id, mode, worklist, options["dry_run"],
            )

    def _run_mode(
        self,
        fileset_id: str,
        mode: str,
        worklist,
        dry_run: bool,
    ) -> None:
        if dry_run:
            self.stdout.write(
                f"mode={mode} would generate {len(worklist)} "
                f"chapters total (dry-run)"
            )
            return

        acquired, reason = gcs.acquire_run_lock(
            fileset_id, stale_after_hours=settings.LOCK_STALE_HOURS,
            mode=mode,
        )
        if not acquired:
            logger.warning(
                "Skipping %s-mode run for %s %s: %s",
                mode, fileset_id, gcs.get_current_year_month(), reason,
            )
            self.stdout.write(f"mode={mode} skipped reason={reason}")
            return

        # The monthly usage counter is intentionally shared across
        # modes (see gcs._state_object_path): the cap is a spend
        # ceiling, not a per-artifact-set allowance. For ``both`` the
        # chapter pass re-reads the counter here, so characters the
        # verse pass just billed are already deducted.
        already_used = gcs.read_monthly_usage(fileset_id)
        cap = int(settings.MONTHLY_TTS_CHAR_LIMIT)
        remaining = max(0, cap - already_used)
        budget = CharBudget(remaining=remaining)
        logger.info(
            "mode=%s fileset=%s cap=%d already_used=%d remaining=%d",
            mode, fileset_id, cap, already_used, remaining,
        )

        cfg = get_tts_config(fileset_id)
        voice_name = cfg["voice_name"]

        completed = gcs.list_completed_chapters(
            fileset_id, voice_name, mode=mode,
        )
        pending = [bc for bc in worklist if bc not in completed]
        logger.info(
            "mode=%s total=%d completed=%d pending=%d",
            mode, len(worklist), len(completed), len(pending),
        )

        synthesizer = Synthesizer()
        generated = 0
        complete_reason = "all_done"

        for book_id, chap in pending:
            # Persist the delta in ``finally`` so characters that Cloud
            # TTS has already billed us for get written to GCS even if
            # the chapter errors mid-way or the upload fails. Without
            # this, a persistent upload failure would silently let us
            # exceed ``MONTHLY_TTS_CHAR_LIMIT`` on the next run.
            chapter_start_used = budget.used
            try:
                try:
                    artifacts = synthesizer.run(
                        fileset_id, book_id, chap,
                        budget=budget, mode=mode,
                    )
                except BudgetExceeded as exc:
                    logger.warning(
                        "Monthly char budget exhausted at %s %s: %s. "
                        "Resuming next month.",
                        book_id, chap, exc,
                    )
                    self.stdout.write(
                        f"mode={mode} budget_exhausted "
                        f"next={book_id} {chap} "
                        f"generated={generated} chars_used={budget.used}"
                    )
                    complete_reason = "budget_exhausted"
                    break
                except QuotaExceeded as exc:
                    logger.warning(
                        "Cloud TTS quota exhausted at %s %s: %s.",
                        book_id, chap, exc,
                    )
                    self.stdout.write(
                        f"mode={mode} quota_exhausted "
                        f"next={book_id} {chap} "
                        f"generated={generated} chars_used={budget.used}"
                    )
                    complete_reason = "quota_exhausted"
                    break
                except Exception as exc:  # noqa: BLE001
                    logger.exception(
                        "Failed to generate %s %s: %s",
                        book_id, chap, exc,
                    )
                    continue

                try:
                    gcs.upload_chapter_artifacts(
                        fileset_id, book_id, chap,
                        artifacts.mp3_bytes,
                        artifacts.timestamps_payload,
                        voice_name,
                        mode=mode,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.exception(
                        "Failed to upload %s %s: %s",
                        book_id, chap, exc,
                    )
                    continue

                generated += 1
                logger.info(
                    "Generated %s %s mode=%s chars=%d duration=%.2fs",
                    book_id, chap, mode, artifacts.chars_used,
                    artifacts.duration_seconds,
                )
            finally:
                delta = budget.used - chapter_start_used
                if delta > 0:
                    try:
                        gcs.increment_monthly_usage(fileset_id, delta)
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "Failed to persist usage delta=%d for "
                            "%s %s; in-memory budget still reflects "
                            "the spend but GCS counter is stale.",
                            delta, book_id, chap,
                        )

        gcs.mark_run_complete(
            fileset_id,
            chars_used=budget.used,
            chapters_generated=generated,
            reason=complete_reason,
            mode=mode,
        )
        self.stdout.write(
            f"mode={mode} done generated={generated} "
            f"chars_used={budget.used} "
            f"reason={complete_reason}"
        )
