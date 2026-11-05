"""Regenerate audio artifacts an audit manifest flags as defective.

The manifest is the JSON report ``audit_audio_files`` emits
(``--json`` stdout, or ``--report PATH`` written to a file /
``gs://`` URI): its ``suspects`` list names the blobs to fix. A
bare JSON list of blob names also works.

**Dry-run by default** — the planned action per manifest entry
prints and nothing is synthesized or written; ``--apply``
performs the repairs.

For each suspect chapter pair the command resolves the producing
engine from the artifact prefix (``audio_repair.PREFIX_ENGINES``)
and regenerates *in place* through the same code path the
generation jobs use: ``google_tts.Synthesizer`` +
``gcs.upload_chapter_artifacts`` — the sidecar JSON is written
first, then the MP3 overwrites the bad blob, so a failed
regeneration never loses the existing artifact (it stays
flagged-but-servable, and stays un-repaired in the ledger).

Free-tier guard: characters synthesized are drawn from the same
per-fileset monthly usage counter ``generate_chapter_audio``
spends (``state/usage/{FILESET}/{YYYY-MM}.json``,
``settings.MONTHLY_TTS_CHAR_LIMIT``), so a repair pass can never
push the month past the cap the generation job already enforces.
Per-chapter deltas are persisted after every attempt — billed
characters survive a crash.

Idempotency: each repaired chapter pair is recorded under
``state/repair/<artifact-base>.json``; re-running the same
manifest skips those entries (``--force`` overrides). Unlike the
generation jobs there is no monthly run lock — repair is a
manual job whose ledger is per-artifact, not per-run.

Engine scope: only the Google TTS modes (``audio/``,
``audio_chapter/``) regenerate; ``audio_gemini/`` and
``audio_edge/`` entries are reported ``unsupported_engine`` —
their pipelines are request-capped rather than char-budgeted and
are left to their own generation commands.
"""
import json
import logging

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from bible.services import audio_repair
from bible.services.google_tts.budget import BudgetExceeded, CharBudget
from bible.services.google_tts.client import QuotaExceeded
from bible.services.google_tts.synthesizer import Synthesizer
from bible.services.storage import gcs

logger = logging.getLogger(__name__)

# Conventional manifest object the ``manual`` run_all_jobs entry
# uses when no --manifest is given.
DEFAULT_MANIFEST_OBJECT = "manifests/audio-repair.json"


class Command(BaseCommand):
    help = (
        "Repair audio artifacts listed in an audit manifest by "
        "regenerating them in place (dry-run unless --apply)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--manifest", metavar="PATH",
            help=(
                "Audit report JSON — local file or gs:// URI. "
                "Default: gs://{AUDIO_BUCKET_NAME}/"
                f"{DEFAULT_MANIFEST_OBJECT}."
            ),
        )
        parser.add_argument(
            "--apply", action="store_true",
            help=(
                "Actually regenerate + overwrite the artifacts. "
                "Without it this is a dry-run that only prints "
                "the planned actions."
            ),
        )
        parser.add_argument(
            "--force", action="store_true",
            help=(
                "Repair entries already recorded under "
                "state/repair/ (e.g. a chapter that broke again "
                "after a previous repair)."
            ),
        )
        parser.add_argument(
            "--limit", type=int, default=0,
            help="Repair at most N entries (0 = no limit).",
        )

    def handle(self, *args, **options):
        manifest_uri = options["manifest"] or (
            f"gs://{settings.AUDIO_BUCKET_NAME}/"
            f"{DEFAULT_MANIFEST_OBJECT}"
        )
        explicit = options["manifest"] is not None
        try:
            raw = gcs.read_text_uri(manifest_uri)
        except FileNotFoundError:
            if explicit:
                raise CommandError(
                    f"manifest not found: {manifest_uri}"
                )
            self.stdout.write(
                f"no manifest at {manifest_uri}; nothing to repair"
            )
            return
        except Exception as exc:  # noqa: BLE001
            raise CommandError(
                f"failed to read manifest {manifest_uri}: "
                f"{exc.__class__.__name__}"
            )
        try:
            manifest = json.loads(raw)
        except ValueError:
            raise CommandError(
                f"manifest {manifest_uri} is not valid JSON"
            )
        try:
            entries, invalid = audio_repair.manifest_entries(
                manifest
            )
        except ValueError as exc:
            raise CommandError(str(exc))

        if isinstance(manifest, dict):
            m_bucket = manifest.get("bucket")
            if (
                m_bucket
                and m_bucket != settings.AUDIO_BUCKET_NAME
            ):
                logger.warning(
                    "Manifest names bucket %r but repairs write to "
                    "%r — proceeding against the configured bucket.",
                    m_bucket, settings.AUDIO_BUCKET_NAME,
                )

        limit = options["limit"]
        if limit and len(entries) > limit:
            entries = entries[:limit]

        already = gcs.list_repair_state()
        plan = self._build_plan(
            entries, invalid, already, options["force"],
        )

        if not options["apply"]:
            self._print_plan(plan, manifest_uri)
            return
        self._apply(plan, manifest_uri)

    # ---------------------------------------------------------
    # Planning
    # ---------------------------------------------------------

    @staticmethod
    def _build_plan(entries, invalid, already, force):
        """Annotate each entry with its skip reason (or None)."""
        plan = []
        for name in invalid:
            plan.append((None, "invalid_name", name))
        for entry in entries:
            if entry.engine not in audio_repair.SUPPORTED_ENGINES:
                reason = f"unsupported_engine={entry.engine}"
            elif not force and entry.base in already:
                reason = "already_repaired"
            else:
                reason = None
            plan.append((entry, reason, entry.name))
        return plan

    def _print_plan(self, plan, manifest_uri):
        self.stdout.write(f"manifest={manifest_uri} (dry-run)")
        actionable = 0
        for entry, reason, name in plan:
            if entry is None:
                self.stdout.write(f"SKIP   {name}  {reason}")
            elif reason:
                self.stdout.write(f"SKIP   {name}  {reason}")
            else:
                actionable += 1
                issues = ",".join(entry.issues) or "unspecified"
                self.stdout.write(
                    f"REPAIR {name}  engine={entry.engine} "
                    f"mode={entry.mode} fileset={entry.fileset_id} "
                    f"voice={entry.voice_name} "
                    f"book={entry.book_id} "
                    f"chapter={entry.chapter} issues={issues}"
                )
        for fileset_id in self._planned_filesets(plan):
            used = gcs.read_monthly_usage(fileset_id)
            cap = int(settings.MONTHLY_TTS_CHAR_LIMIT)
            self.stdout.write(
                f"budget fileset={fileset_id} "
                f"chars_used={used} cap={cap} "
                f"remaining={max(0, cap - used)}"
            )
        self.stdout.write(
            f"summary: entries={len(plan)} to_repair={actionable} "
            "(dry-run — pass --apply to execute)"
        )

    @staticmethod
    def _planned_filesets(plan):
        return sorted({
            e.fileset_id
            for e, reason, _name in plan
            if e is not None and reason is None
        })

    # ---------------------------------------------------------
    # Apply
    # ---------------------------------------------------------

    def _apply(self, plan, manifest_uri):
        cap = int(settings.MONTHLY_TTS_CHAR_LIMIT)
        budgets = {}
        exhausted = set()
        synthesizer = None
        repaired = skipped = failed = 0

        for entry, reason, name in plan:
            if entry is None or reason:
                self.stdout.write(f"SKIP   {name}  {reason}")
                skipped += 1
                continue
            fs = entry.fileset_id
            if fs in exhausted:
                self.stdout.write(
                    f"SKIP   {name}  budget_exhausted"
                )
                skipped += 1
                continue
            if fs not in budgets:
                used = gcs.read_monthly_usage(fs)
                budgets[fs] = CharBudget(
                    remaining=max(0, cap - used)
                )
            budget = budgets[fs]
            if synthesizer is None:
                synthesizer = Synthesizer()

            chapter_start_used = budget.used
            ok = False
            try:
                try:
                    artifacts = synthesizer.run(
                        fs, entry.book_id, entry.chapter,
                        budget=budget, mode=entry.mode,
                    )
                except BudgetExceeded as exc:
                    logger.warning(
                        "Monthly char budget exhausted at %s: %s",
                        name, exc,
                    )
                    self.stdout.write(
                        f"SKIP   {name}  budget_exhausted"
                    )
                    exhausted.add(fs)
                    skipped += 1
                    continue
                except QuotaExceeded as exc:
                    logger.warning(
                        "Cloud TTS quota exhausted at %s: %s",
                        name, exc,
                    )
                    self.stdout.write(
                        f"SKIP   {name}  quota_exhausted"
                    )
                    exhausted.add(fs)
                    skipped += 1
                    continue
                except Exception as exc:  # noqa: BLE001
                    logger.exception(
                        "Regeneration failed for %s: %s", name, exc,
                    )
                    self.stdout.write(
                        f"FAILED {name}  regen_failed: "
                        f"{exc.__class__.__name__}"
                    )
                    failed += 1
                    continue

                try:
                    gcs.upload_chapter_artifacts(
                        fs, entry.book_id, entry.chapter,
                        artifacts.mp3_bytes,
                        artifacts.timestamps_payload,
                        entry.voice_name,
                        mode=entry.mode,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.exception(
                        "Upload failed for %s: %s", name, exc,
                    )
                    self.stdout.write(
                        f"FAILED {name}  upload_failed: "
                        f"{exc.__class__.__name__}"
                    )
                    failed += 1
                    continue
                ok = True
            finally:
                # Billed characters get persisted even when the
                # chapter failed mid-way — mirrors
                # generate_chapter_audio so a crash cannot push
                # the month past MONTHLY_TTS_CHAR_LIMIT.
                delta = budget.used - chapter_start_used
                if delta > 0:
                    try:
                        gcs.increment_monthly_usage(fs, delta)
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "Failed to persist usage delta=%d "
                            "for %s; GCS counter is stale.",
                            delta, name,
                        )
            if not ok:
                continue
            try:
                gcs.mark_entry_repaired(entry.base, {
                    "blob_name": entry.name,
                    "issues": entry.issues,
                    "engine": entry.engine,
                    "mode": entry.mode,
                    "manifest": manifest_uri,
                    "chars_used": artifacts.chars_used,
                })
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Failed to write repair ledger for %s; the "
                    "artifact IS repaired but a re-run would "
                    "regenerate it again.",
                    name,
                )
            repaired += 1
            self.stdout.write(
                f"FIXED  {name}  chars_used={artifacts.chars_used} "
                f"duration={artifacts.duration_seconds:.2f}s"
            )

        self.stdout.write(
            f"summary: repaired={repaired} skipped={skipped} "
            f"failed={failed}"
        )
        if failed:
            raise CommandError(
                f"{failed} manifest entrie(s) failed to repair — "
                "re-run after fixing the cause"
            )
