"""Run-state for resumable Gemini TTS generation.

Two interchangeable backends expose the same surface
(``requests_today``/``add_requests``/``is_completed``/
``mark_completed``/``save``):

- :class:`RunState` — local mode: completed ``(book, chapter)``
  pairs plus the per-day request counter in a gitignored JSON
  file.
- :class:`GcsRunState` — Cloud Run mode: completed chapters are
  derived from the artifact objects already uploaded to GCS, and
  the request counter lives in a per-day JSON object so a same-day
  job retry resumes the spend.

The free-tier daily quota resets at midnight Pacific, so the
counter rolls on the America/Los_Angeles date rather than UTC.
"""
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Set, Tuple
from zoneinfo import ZoneInfo

from bible.services.storage import gcs

logger = logging.getLogger(__name__)

PACIFIC = ZoneInfo("America/Los_Angeles")

DEFAULT_STATE_PATH = (
    Path(__file__).resolve().parents[3] / "gemini-tts-state.json"
)


def today_pacific() -> str:
    """Today's date in the Gemini-quota timezone (Pacific)."""
    return datetime.now(PACIFIC).date().isoformat()


class RunState:
    """Resume-safe view over the JSON state file.

    The file is rewritten atomically (tmp + replace) after each
    completed chapter so a crash mid-run loses at most the chapter
    in flight — its output files are only written once synthesis
    fully succeeds."""

    def __init__(self, path: Optional[Path] = None):
        self._path = Path(path or DEFAULT_STATE_PATH)
        self._completed: Dict[str, Set[int]] = {}
        self._day: Optional[str] = None
        self._requests_today = 0
        self._load()

    @staticmethod
    def _today() -> str:
        return today_pacific()

    def _roll_day(self) -> None:
        """Zero the daily counter once the Pacific date changes."""
        today = self._today()
        if self._day != today:
            self._day = today
            self._requests_today = 0

    @property
    def path(self) -> Path:
        return self._path

    @property
    def requests_today(self) -> int:
        self._roll_day()
        return self._requests_today

    def add_requests(self, n: int) -> None:
        self._roll_day()
        self._requests_today += int(n)

    def is_completed(self, book_id: str, chapter: int) -> bool:
        return int(chapter) in self._completed.get(book_id, set())

    def mark_completed(self, book_id: str, chapter: int) -> None:
        self._completed.setdefault(book_id, set()).add(
            int(chapter)
        )

    def completed_count(self) -> int:
        return sum(len(v) for v in self._completed.values())

    def save(self) -> None:
        self._roll_day()
        payload = {
            "day": self._day,
            "requests_today": self._requests_today,
            "completed": {
                book: sorted(chapters)
                for book, chapters in self._completed.items()
            },
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(
            json.dumps(payload, indent=2), encoding="utf-8",
        )
        tmp.replace(self._path)

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(
                self._path.read_text(encoding="utf-8")
            )
            if not isinstance(raw, dict):
                raise ValueError(
                    "state root is not an object"
                )
            completed = {
                book: {int(c) for c in chapters}
                for book, chapters in (
                    raw.get("completed") or {}
                ).items()
            }
            day = raw.get("day")
            requests_today = int(raw.get("requests_today", 0))
        except (
            OSError, ValueError, AttributeError, TypeError,
        ) as exc:
            # A corrupt or wrongly-shaped state file must not
            # wedge the run: start over (JSONDecodeError is a
            # ValueError; AttributeError/TypeError cover valid
            # JSON of the wrong shape). Completed chapters are
            # re-detected from the output dir anyway, so only
            # the request counter is really lost.
            logger.warning(
                "Could not parse state file %s (%s); starting "
                "fresh.", self._path, exc,
            )
            return
        self._completed = completed
        self._day = day
        self._requests_today = requests_today


class GcsRunState:
    """GCS-backed counterpart of :class:`RunState`.

    Used by ``generate_gemini_audio --upload-gcs`` in the Cloud Run
    Job, where the local filesystem is ephemeral:

    - Completed chapters come from ``list_completed_chapters`` on
      construction — a chapter whose MP3 + timestamps objects both
      exist counts as done, so no separate completion ledger is
      needed (same rule the local mode applies to files on disk).
    - The daily request counter persists in
      ``state/gemini/{fileset}/{voice}/{day}.json`` so a same-day
      job retry resumes the spend instead of re-counting from 0
      and overshooting the free-tier quota.

    A day rollover inside a run (past midnight Pacific) resets the
    in-memory counter to 0 exactly like ``RunState`` — the API
    quota also just reset, so this is the correct direction.
    """

    def __init__(self, fileset_id: str, voice_name: str):
        self._fileset_id = fileset_id
        self._voice_name = voice_name
        self._completed: Set[Tuple[str, int]] = (
            gcs.list_completed_chapters(
                fileset_id, voice_name, mode="gemini",
            )
        )
        self._day = today_pacific()
        self._requests_today = gcs.read_gemini_daily_requests(
            fileset_id, voice_name, self._day,
        )

    def _roll_day(self) -> None:
        """Zero the daily counter once the Pacific date changes."""
        today = today_pacific()
        if self._day != today:
            self._day = today
            self._requests_today = 0

    @property
    def requests_today(self) -> int:
        self._roll_day()
        return self._requests_today

    def add_requests(self, n: int) -> None:
        self._roll_day()
        self._requests_today += int(n)

    def is_completed(self, book_id: str, chapter: int) -> bool:
        return (book_id, int(chapter)) in self._completed

    def mark_completed(self, book_id: str, chapter: int) -> None:
        self._completed.add((book_id, int(chapter)))

    def save(self) -> None:
        """Persist the request counter to its GCS day object."""
        self._roll_day()
        gcs.write_gemini_daily_requests(
            self._fileset_id,
            self._voice_name,
            self._day,
            self._requests_today,
        )
