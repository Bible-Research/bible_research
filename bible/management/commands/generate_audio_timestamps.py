"""Generate verse timestamps for DBT audio filesets that have
none, using Cloud Speech-to-Text (V1) + reference-text
alignment, and upload ``timestamps_generated/`` artifacts to
GCS. Designed to be invoked by the ``all-jobs`` Cloud Run Job
once per month, staying inside the STT free tier
(``MONTHLY_STT_SECONDS_LIMIT``).

Loop semantics mirror ``generate_chapter_audio``: per-fileset
monthly lock, a global seconds budget persisted to GCS per
chapter (billed spend is never lost), per-chapter failures log
+ continue, and the lock is left open when every attempted
chapter fails so a later run can retry.
"""
import logging
import math
from io import BytesIO

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from mutagen.mp3 import MP3

from bible.services.dbt.client import get_default_dbt_client
from bible.services.dbt.timestamps_registry import (
    generated_timestamps_base_id,
    generated_timestamps_meta,
)
from bible.services.google_stt.aligner import (
    MIN_ALIGNMENT_SCORE,
    align_chapter,
)
from bible.services.google_stt.budget import (
    BudgetExceeded,
    SecondsBudget,
)
from bible.services.google_stt.client import (
    QuotaExceeded,
    get_default_stt_client,
)
from bible.services.storage import gcs
from bible.services.sword.client import get_default_sword_client

logger = logging.getLogger(__name__)

_MP3_DOWNLOAD_TIMEOUT_SECONDS = 60


class Command(BaseCommand):
    help = (
        "Generate verse timestamps for DBT audio filesets via "
        "Cloud STT."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--fileset-ids", required=True,
            help=(
                "Comma-separated DBT audio fileset ids registered "
                "in GENERATED_TIMESTAMPS_FILESETS (e.g. "
                "LATLVRN1DA,LATLVRP1DA)."
            ),
        )
        parser.add_argument(
            "--book", default=None,
            help="USFM book id filter (e.g. JHN).",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help=(
                "Print the worklist and remaining budget; do not "
                "call STT or write GCS."
            ),
        )
        parser.add_argument(
            "--max-chapters", type=int, default=None,
            help="Cap the number of chapters processed per fileset.",
        )

    def handle(self, *args, **options):
        raw_ids = [
            s.strip()
            for s in options["fileset_ids"].split(",")
            if s.strip()
        ]
        fileset_ids = []
        unknown = []
        for fid in raw_ids:
            base = generated_timestamps_base_id(fid)
            if base is None:
                unknown.append(fid)
            elif base not in fileset_ids:
                fileset_ids.append(base)
        if unknown:
            raise CommandError(
                "fileset ids not in GENERATED_TIMESTAMPS_FILESETS: "
                + ", ".join(unknown)
            )
        if not fileset_ids:
            raise CommandError("no usable --fileset-ids given")

        book_filter = (
            options["book"].strip().upper()
            if options["book"]
            else None
        )
        dbt = get_default_dbt_client()
        stt = (
            None
            if options["dry_run"]
            else get_default_stt_client()
        )
        for fileset_id in fileset_ids:
            self._run_fileset(
                fileset_id,
                dbt=dbt,
                stt=stt,
                book_filter=book_filter,
                dry_run=options["dry_run"],
                max_chapters=options["max_chapters"],
            )

    # ---------------------------------------------------------
    # Worklist
    # ---------------------------------------------------------

    def _worklist(self, dbt, fileset_id):
        """``[(book_id, chapter, row), ...]`` from the fileset's
        real coverage — one row per chapter audio file, each
        carrying ``path`` (signed MP3 URL) and ``duration``."""
        result = dbt.get_fileset_files(fileset_id)
        rows = (result or {}).get("data") or []
        out = []
        seen = set()
        for row in rows:
            book_id = row.get("book_id")
            chapter = row.get("chapter_start")
            if not book_id or chapter is None:
                continue
            key = (book_id, int(chapter))
            if key in seen:
                continue
            seen.add(key)
            out.append((key[0], key[1], row))
        return out

    # ---------------------------------------------------------
    # Per-chapter steps
    # ---------------------------------------------------------

    def _download_mp3(self, dbt, url):
        response = dbt.session.get(
            url, timeout=_MP3_DOWNLOAD_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return response.content

    @staticmethod
    def _mp3_duration(mp3_bytes):
        return float(MP3(BytesIO(mp3_bytes)).info.length)

    def _dbt_has_timestamps(self, dbt, fileset_id, book_id, chap):
        """True when DBT already serves timestamps — never spend
        STT budget where DBT coverage exists. A failed check
        (exception or non-200) means "no coverage": these
        filesets are verified to lack timestamps, so we log and
        proceed rather than skipping the chapter every run."""
        try:
            result = dbt.get_timestamps(fileset_id, book_id, chap)
        except Exception:  # noqa: BLE001
            logger.warning(
                "DBT timestamps check failed for %s %s %s; "
                "proceeding to generate.",
                fileset_id, book_id, chap, exc_info=True,
            )
            return False
        return bool((result or {}).get("data"))

    # ---------------------------------------------------------
    # Run loop
    # ---------------------------------------------------------

    def _pending(
        self, dbt, fileset_id, book_filter, max_chapters,
    ):
        """``(worklist, completed, pending)`` — the fileset's
        real chapter coverage minus already-generated artifacts,
        filtered by ``--book``/``--max-chapters``."""
        worklist = self._worklist(dbt, fileset_id)
        completed = gcs.list_generated_chapters(fileset_id)
        pending = [
            w for w in worklist
            if (w[0], w[1]) not in completed
        ]
        if book_filter:
            pending = [w for w in pending if w[0] == book_filter]
        if max_chapters is not None:
            pending = pending[:max_chapters]
        return worklist, completed, pending

    def _run_fileset(
        self, fileset_id, dbt, stt, book_filter, dry_run,
        max_chapters,
    ):
        meta = generated_timestamps_meta(fileset_id)
        min_score = meta.get(
            "min_alignment_score", MIN_ALIGNMENT_SCORE,
        )
        cap = int(settings.MONTHLY_STT_SECONDS_LIMIT)

        if dry_run:
            worklist, completed, pending = self._pending(
                dbt, fileset_id, book_filter, max_chapters,
            )
            already_used = gcs.read_stt_monthly_usage()
            remaining = max(0, cap - already_used)
            self.stdout.write(
                f"fileset={fileset_id} total={len(worklist)} "
                f"completed={len(completed)} "
                f"pending={len(pending)} "
                f"seconds_remaining={remaining} (dry-run)"
            )
            return

        acquired, reason = gcs.acquire_stt_run_lock(
            fileset_id, stale_after_hours=settings.LOCK_STALE_HOURS,
        )
        if not acquired:
            logger.warning(
                "Skipping STT run for %s %s: %s",
                fileset_id, gcs.get_current_year_month(), reason,
            )
            self.stdout.write(
                f"fileset={fileset_id} skipped reason={reason}"
            )
            return

        already_used = gcs.read_stt_monthly_usage()
        remaining = max(0, cap - already_used)
        budget = SecondsBudget(remaining=remaining)
        logger.info(
            "fileset=%s cap=%d already_used=%d remaining=%d",
            fileset_id, cap, already_used, remaining,
        )

        _worklist, _completed, pending = self._pending(
            dbt, fileset_id, book_filter, max_chapters,
        )

        sword = get_default_sword_client()
        generated = 0
        skipped_dbt = 0
        attempted = 0
        complete_reason = "all_done"

        for book_id, chap, row in pending:
            if self._dbt_has_timestamps(
                dbt, fileset_id, book_id, chap,
            ):
                skipped_dbt += 1
                logger.info(
                    "%s %s %s already has DBT timestamps; skipped.",
                    fileset_id, book_id, chap,
                )
                continue

            attempted += 1
            # Persist the delta in ``finally`` so seconds Cloud
            # STT has already billed get written to GCS even if
            # the chapter errors mid-way or the upload fails.
            chapter_start_used = budget.used
            try:
                mp3_bytes = None
                duration = row.get("duration")
                if duration is None:
                    mp3_bytes = self._download_mp3(
                        dbt, row["path"],
                    )
                    duration = self._mp3_duration(mp3_bytes)
                seconds = math.ceil(float(duration))
                if not budget.can_afford(seconds):
                    complete_reason = "budget_exhausted"
                    break

                if mp3_bytes is None:
                    mp3_bytes = self._download_mp3(
                        dbt, row["path"],
                    )
                gs_uri = gcs.upload_stt_staging_audio(
                    fileset_id, book_id, chap, mp3_bytes,
                )
                try:
                    words = stt.long_running_recognize_uri(
                        gs_uri,
                        language_code=settings.STT_LANGUAGE_CODE,
                        model=settings.STT_MODEL,
                        timeout_seconds=(
                            settings.STT_LRO_TIMEOUT_SECONDS
                        ),
                    )
                finally:
                    try:
                        gcs.delete_stt_staging_audio(
                            fileset_id, book_id, chap,
                        )
                    except Exception:  # noqa: BLE001
                        logger.warning(
                            "stt_staging cleanup failed for %s "
                            "%s %s; the lifecycle rule is the "
                            "backstop.",
                            fileset_id, book_id, chap,
                            exc_info=True,
                        )
                # The LRO completed — the full chapter duration
                # is billed even when the transcript is empty.
                budget.consume(seconds)

                ref_verses = sword.get_chapter_verses(
                    meta["reference"], book_id, chap,
                )
                aligned = align_chapter(words, ref_verses)
                score = aligned["alignment_score"]
                payload = {
                    "data": aligned["data"],
                    "duration_seconds": float(duration),
                    "file_size_bytes": len(mp3_bytes),
                    "meta": {
                        "source": "stt_v1",
                        "reference_text": meta["reference"],
                        "alignment_score": round(score, 4),
                        "low_confidence": score < min_score,
                        "generated_at": (
                            gcs._utc_naive_now().isoformat()
                        ),
                    },
                }
                gcs.upload_generated_timestamps(
                    fileset_id, book_id, chap, payload,
                )
                generated += 1
                logger.info(
                    "Generated timestamps %s %s %s "
                    "seconds=%d score=%.3f",
                    fileset_id, book_id, chap, seconds, score,
                )
            except BudgetExceeded as exc:
                logger.warning(
                    "Monthly STT budget exhausted at %s %s: %s. "
                    "Resuming next month.",
                    book_id, chap, exc,
                )
                complete_reason = "budget_exhausted"
            except QuotaExceeded as exc:
                logger.warning(
                    "Cloud STT quota exhausted at %s %s: %s.",
                    book_id, chap, exc,
                )
                complete_reason = "quota_exhausted"
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "Failed to generate timestamps %s %s %s: %s",
                    fileset_id, book_id, chap, exc,
                )
                continue
            finally:
                delta = budget.used - chapter_start_used
                if delta > 0:
                    try:
                        gcs.increment_stt_monthly_usage(delta)
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "Failed to persist STT usage "
                            "delta=%d for %s %s; in-memory "
                            "budget still reflects the spend "
                            "but the GCS counter is stale.",
                            delta, book_id, chap,
                        )
            if complete_reason != "all_done":
                break

        # If every attempted chapter hit the generic
        # ``except -> continue`` path, marking the run complete
        # would lock the month out with zero artifacts. Leave the
        # lock "running"; it goes stale after LOCK_STALE_HOURS.
        if (
            attempted and generated == 0
            and complete_reason == "all_done"
        ):
            complete_reason = "all_failed"
            logger.error(
                "fileset=%s all %d attempted chapters failed; "
                "leaving the run lock un-completed so a later "
                "run retries.",
                fileset_id, attempted,
            )
        else:
            gcs.mark_stt_run_complete(
                fileset_id,
                seconds_used=budget.used,
                chapters_generated=generated,
                reason=complete_reason,
            )
        self.stdout.write(
            f"fileset={fileset_id} done generated={generated} "
            f"skipped_dbt={skipped_dbt} "
            f"seconds_used={budget.used} "
            f"reason={complete_reason}"
        )
