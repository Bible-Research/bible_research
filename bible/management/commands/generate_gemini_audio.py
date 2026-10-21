"""Generate LVSGLU8 chapter audio via the Gemini API free tier.

Designed to run locally on each boot: it drips through the ~15
requests/day free quota (resetting at midnight Pacific), writes
MP3 + timestamps under ``tts-out/``, and resumes where it left
off via a gitignored JSON state file. No GCS upload yet — quality
first, ``--upload-gcs`` comes later.
"""
import json
import logging
from pathlib import Path
from typing import Tuple

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from bible.services.gemini_tts.client import (
    GeminiTTSClient,
    QuotaExceeded,
)
from bible.services.gemini_tts.state import RunState
from bible.services.gemini_tts.synthesizer import (
    DEFAULT_MAX_SEGMENT_CHARS,
    GeminiSynthesizer,
)
from bible.services.sword.client import get_default_sword_client
from bible.services.sword.registry import canonical_sword_fileset_id

logger = logging.getLogger(__name__)

# ~15 requests/day per free-tier project; leave one in reserve.
DEFAULT_MAX_REQUESTS = 14
# Free-tier RPM is ~3: ~21 s between calls keeps us under it.
DEFAULT_SLEEP_SECONDS = 21.0


def _default_output_dir() -> Path:
    # Resolved lazily: settings.BASE_DIR is absent under pytest's
    # minimal settings.configure(), so it cannot be a module
    # constant.
    base = getattr(settings, "BASE_DIR", Path.cwd())
    return Path(base) / "tts-out"


def chapter_output_paths(
    output_dir: Path,
    fileset_id: str,
    book_id: str,
    chapter: int,
) -> Tuple[Path, Path]:
    """``(mp3_path, timestamps_path)`` for a chapter on disk."""
    base = Path(output_dir) / fileset_id / book_id
    return (
        base / f"{int(chapter)}.mp3",
        base / f"{int(chapter)}_timestamps.json",
    )


def chapter_files_exist(
    output_dir: Path,
    fileset_id: str,
    book_id: str,
    chapter: int,
) -> bool:
    """Both artifacts present and usable.

    A zero-byte MP3 or unparseable JSON left by a crash mid-write
    must not count as done — the chapter gets re-synthesized."""
    mp3_path, json_path = chapter_output_paths(
        output_dir, fileset_id, book_id, chapter,
    )
    if not mp3_path.is_file() or mp3_path.stat().st_size == 0:
        return False
    try:
        json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return True


def write_chapter_artifacts(
    output_dir: Path,
    fileset_id: str,
    book_id: str,
    chapter: int,
    mp3_bytes: bytes,
    timestamps_payload: dict,
) -> None:
    """Write timestamps JSON first, then MP3, so a present MP3
    always implies a JSON exists (resume logic depends on this)."""
    mp3_path, json_path = chapter_output_paths(
        output_dir, fileset_id, book_id, chapter,
    )
    mp3_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(timestamps_payload, ensure_ascii=False),
        encoding="utf-8",
    )
    mp3_path.write_bytes(mp3_bytes)


class Command(BaseCommand):
    help = (
        "Generate missing chapter audio via the Gemini free-tier "
        "TTS endpoint, resumable across daily quota resets."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--fileset-id", default="LVSGLU8",
            help="Canonical SWORD fileset id (default: LVSGLU8).",
        )
        parser.add_argument(
            "--max-requests", type=int,
            default=DEFAULT_MAX_REQUESTS,
            help=(
                "Max Gemini API requests to spend per day "
                f"(default: {DEFAULT_MAX_REQUESTS})."
            ),
        )
        parser.add_argument(
            "--max-segment-chars", type=int,
            default=DEFAULT_MAX_SEGMENT_CHARS,
            help=(
                "Char cap per synthesized segment "
                f"(default: {DEFAULT_MAX_SEGMENT_CHARS})."
            ),
        )
        parser.add_argument(
            "--sleep-seconds", type=float,
            default=DEFAULT_SLEEP_SECONDS,
            help=(
                "Minimum spacing between API calls "
                f"(default: {DEFAULT_SLEEP_SECONDS}, ~3 RPM)."
            ),
        )
        parser.add_argument(
            "--output-dir", default=None,
            help=(
                "Local output root (default: <repo>/tts-out)."
            ),
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
            help="Gemini prebuilt voice name (default: Sadachbia).",
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
        output_dir = Path(
            options["output_dir"] or _default_output_dir()
        )
        state = RunState(
            Path(options["state_file"])
            if options["state_file"] else None
        )

        worklist = get_default_sword_client().list_chapters(
            fileset_id
        )
        pending = self._pending(
            worklist, state, output_dir, fileset_id,
        )
        logger.info(
            "fileset=%s total=%d completed=%d pending=%d "
            "requests_today=%d",
            fileset_id, len(worklist),
            len(worklist) - len(pending), len(pending),
            state.requests_today,
        )
        if options["dry_run"]:
            self.stdout.write(
                f"would generate {len(pending)} chapters "
                f"(requests_today={state.requests_today}, "
                f"max_requests={options['max_requests']})"
            )
            return

        api_keys = list(getattr(settings, "GEMINI_API_KEYS", []))
        if not api_keys:
            raise CommandError(
                "No Gemini API keys configured. Add "
                "GEMINI_API_KEY (a string or a list) to "
                "config.yaml."
            )
        client = GeminiTTSClient(
            api_keys=api_keys,
            min_interval_seconds=options["sleep_seconds"],
        )
        synthesizer = GeminiSynthesizer(
            client=client,
            max_segment_chars=options["max_segment_chars"],
            voice_name=options["voice"],
        )

        generated = 0
        reason = "all_done"
        max_requests = options["max_requests"]
        for book_id, chap in pending:
            if state.requests_today >= max_requests:
                reason = "daily_request_cap"
                logger.info(
                    "Daily request cap reached (%d/%d); resume "
                    "after the Pacific-midnight quota reset.",
                    state.requests_today, max_requests,
                )
                break
            before = client.requests_made
            try:
                # Bound the chapter's spend by the remaining daily
                # headroom: synthesis aborts mid-chapter
                # (RequestBudgetExceeded) rather than letting one
                # pathological chapter overrun the cap.
                artifacts = synthesizer.run(
                    fileset_id, book_id, chap,
                    request_budget=(
                        max_requests - state.requests_today
                    ),
                )
            except QuotaExceeded as exc:
                logger.warning(
                    "Gemini quota exhausted at %s %s: %s",
                    book_id, chap, exc,
                )
                reason = "quota_exhausted"
                break
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "Failed to generate %s %s: %s",
                    book_id, chap, exc,
                )
                continue
            finally:
                # Requests spent on a failed chapter still count
                # against the daily quota — persist them either way.
                spent = client.requests_made - before
                if spent > 0:
                    state.add_requests(spent)
                    state.save()

            try:
                write_chapter_artifacts(
                    output_dir, fileset_id, book_id, chap,
                    artifacts.mp3_bytes,
                    artifacts.timestamps_payload,
                )
            except OSError as exc:
                logger.exception(
                    "Failed to write %s %s artifacts: %s",
                    book_id, chap, exc,
                )
                continue

            state.mark_completed(book_id, chap)
            state.save()
            generated += 1
            logger.info(
                "Generated %s %s chars=%d duration=%.2fs "
                "requests_today=%d",
                book_id, chap, artifacts.chars_used,
                artifacts.duration_seconds, state.requests_today,
            )

        self.stdout.write(
            f"done generated={generated} "
            f"requests_today={state.requests_today} "
            f"reason={reason}"
        )

    @staticmethod
    def _pending(
        worklist,
        state: RunState,
        output_dir: Path,
        fileset_id: str,
    ) -> list:
        """Worklist minus completed chapters.

        A chapter with both files already on disk but missing from
        the state file (e.g. state was deleted) is marked complete
        without re-synthesizing."""
        pending = []
        for book_id, chap in worklist:
            if state.is_completed(book_id, chap):
                continue
            if chapter_files_exist(
                output_dir, fileset_id, book_id, chap,
            ):
                state.mark_completed(book_id, chap)
                continue
            pending.append((book_id, chap))
        return pending
