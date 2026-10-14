"""Per-chapter audio synthesis: SWORD verses -> Cloud TTS -> MP3
plus per-verse timestamps."""
import io
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from xml.sax.saxutils import escape

from mutagen.mp3 import MP3

from bible.services.sword.client import get_default_sword_client
from bible.services.sword.registry import canonical_sword_fileset_id

from .budget import BudgetExceeded, CharBudget
from .client import GoogleTTSClient, get_default_tts_client
from .registry import get_tts_config

# The synchronous ``synthesize_speech`` API caps input at 5000 bytes.
# Keep a safety margin under that cap so markup + multi-byte UTF-8
# characters cannot push a segment over the limit.
MAX_SSML_INPUT_BYTES = 4500

_SSML_OPEN = "<speak>"
_SSML_CLOSE = "</speak>"

logger = logging.getLogger(__name__)


@dataclass
class ChapterArtifacts:
    mp3_bytes: bytes
    timestamps_payload: dict
    duration_seconds: float
    chars_used: int


class Synthesizer:
    def __init__(self, tts_client: Optional[GoogleTTSClient] = None):
        self._tts = tts_client or get_default_tts_client()
        self._sword = get_default_sword_client()

    def run(
        self,
        fileset_id: str,
        book_id: str,
        chapter: int,
        budget: CharBudget | None = None,
        mode: str = "verse",
    ) -> ChapterArtifacts:
        canon = canonical_sword_fileset_id(fileset_id)
        if not canon:
            raise ValueError(f"Unknown SWORD fileset: {fileset_id!r}")
        cfg = get_tts_config(canon)

        # Validate the mode before touching SWORD so a bogus mode
        # fails fast instead of after expensive module I/O.
        if mode not in ("verse", "chapter"):
            raise ValueError(f"Unknown synthesis mode: {mode!r}")

        verses = self._sword.get_chapter_verses(canon, book_id, chapter)
        if not verses:
            raise ValueError(
                f"No verses for {book_id} {chapter} in {canon}"
            )

        if mode == "verse":
            return self._run_per_verse(verses, cfg, budget)
        return self._run_per_chapter(verses, cfg, budget)

    def _run_per_verse(
        self,
        verses: List[dict],
        cfg: dict,
        budget: CharBudget | None,
    ) -> ChapterArtifacts:
        """Synthesize each verse separately and concatenate the MP3
        chunks; verse timestamps come from cumulative mutagen
        durations."""
        chapter_chars = sum(len(v["verse_text"]) for v in verses)
        if budget is not None and not budget.can_afford(chapter_chars):
            raise BudgetExceeded(
                f"chapter needs {chapter_chars} chars, "
                f"only {budget.remaining} remain"
            )

        chunks: list[bytes] = []
        timestamps: list[dict] = []
        cumulative = 0.0
        # Consume budget per-verse immediately after each successful
        # ``synthesize_text`` call so ``budget.used`` reflects characters
        # Google has actually billed us for, even if a later verse in
        # this chapter raises or the upload fails. The caller is
        # responsible for persisting ``budget.used`` to durable storage.
        for v in verses:
            verse_chars = len(v["verse_text"])
            audio = self._tts.synthesize_text(
                text=v["verse_text"],
                language_code=cfg["language_code"],
                voice_name=cfg["voice_name"],
                sample_rate_hertz=cfg["sample_rate_hertz"],
            )
            if budget is not None:
                budget.consume(verse_chars)
            duration = float(MP3(io.BytesIO(audio)).info.length)
            timestamps.append({
                "verse_start": v["verse_start"],
                "timestamp": round(cumulative, 3),
            })
            cumulative += duration
            chunks.append(audio)

        return ChapterArtifacts(
            mp3_bytes=b"".join(chunks),
            timestamps_payload={
                "duration_seconds": round(cumulative, 3),
                "data": timestamps,
            },
            duration_seconds=cumulative,
            chars_used=chapter_chars,
        )

    def _run_per_chapter(
        self,
        verses: List[dict],
        cfg: dict,
        budget: CharBudget | None,
    ) -> ChapterArtifacts:
        """Synthesize the whole chapter in as few TTS calls as
        possible. Each verse is preceded by ``<mark name="vN"/>`` and
        ``enable_time_pointing=[SSML_MARK]`` gives us exact
        verse-level timestamps back from the API.

        Chapters whose SSML would exceed ``MAX_SSML_INPUT_BYTES``
        (e.g. Psalm 119) are split into consecutive-verse segments;
        each segment's mark timepoints are offset by the cumulative
        MP3 duration of the segments before it (mutagen, as in verse
        mode).

        Billed characters include the SSML markup itself, so the
        budget check and ``chars_used`` count the full SSML document,
        not just the verse text."""
        segments = self._build_ssml_segments(verses)
        total_chars = sum(len(ssml) for ssml, _ in segments)
        if budget is not None and not budget.can_afford(total_chars):
            raise BudgetExceeded(
                f"chapter needs {total_chars} billed SSML chars, "
                f"only {budget.remaining} remain"
            )

        chunks: list[bytes] = []
        timestamps: list[dict] = []
        cumulative = 0.0
        missing_marks: List[str] = []
        # Consume budget per-segment right after each billed
        # ``synthesize_ssml`` call, mirroring the per-verse mode.
        for ssml, seg_verses in segments:
            audio, timepoints = self._tts.synthesize_ssml(
                ssml=ssml,
                language_code=cfg["language_code"],
                voice_name=cfg["voice_name"],
                sample_rate_hertz=cfg["sample_rate_hertz"],
            )
            if budget is not None:
                budget.consume(len(ssml))
            duration = float(MP3(io.BytesIO(audio)).info.length)
            mark_seconds: Dict[str, float] = {
                tp["mark_name"]: tp["time_seconds"]
                for tp in timepoints
            }
            # If the voice drops a mark (unsupported SSML subset), a
            # verse falls back to the previous verse's timestamp
            # rather than disappearing from the payload entirely —
            # but the drop is counted and warned about below, since
            # the fallback produces plausible-looking wrong data.
            last_ts = 0.0
            for v in seg_verses:
                mark = f"v{v['verse_start']}"
                ts = mark_seconds.get(mark)
                if ts is None:
                    missing_marks.append(mark)
                    ts = last_ts
                timestamps.append({
                    "verse_start": v["verse_start"],
                    "timestamp": round(cumulative + ts, 3),
                })
                last_ts = ts
            cumulative += duration
            chunks.append(audio)

        if missing_marks:
            logger.warning(
                "Cloud TTS returned no timepoint for %d mark(s) "
                "(%s); those verses reuse the previous verse's "
                "timestamp and may be inaccurate.",
                len(missing_marks), ", ".join(missing_marks),
            )

        return ChapterArtifacts(
            mp3_bytes=b"".join(chunks),
            timestamps_payload={
                "duration_seconds": round(cumulative, 3),
                "data": timestamps,
            },
            duration_seconds=cumulative,
            chars_used=total_chars,
        )

    @staticmethod
    def _build_ssml_segments(
        verses: List[dict],
    ) -> List[Tuple[str, List[dict]]]:
        """Group consecutive verses into ``(ssml_doc, verses)`` pairs,
        each under ``MAX_SSML_INPUT_BYTES`` of UTF-8 SSML."""
        overhead = len(
            (_SSML_OPEN + _SSML_CLOSE).encode("utf-8")
        )
        segments: List[Tuple[str, List[dict]]] = []
        parts: List[str] = []
        seg_verses: List[dict] = []
        seg_bytes = overhead
        for v in verses:
            fragment = (
                f'<mark name="v{v["verse_start"]}"/>'
                f'{escape(v["verse_text"])}'
            )
            frag_bytes = len(fragment.encode("utf-8"))
            if frag_bytes + overhead > MAX_SSML_INPUT_BYTES:
                # A single verse alone busts the segment budget; it
                # is still emitted as its own over-budget segment
                # (the API hard-rejects >5000 bytes with a 400), so
                # make the anomaly loud in the logs.
                logger.warning(
                    "Verse %s fragment is %d bytes alone, over the "
                    "%d-byte segment budget; the API rejects SSML "
                    "inputs over 5000 bytes.",
                    v["verse_start"], frag_bytes,
                    MAX_SSML_INPUT_BYTES,
                )
            if seg_verses and (
                seg_bytes + frag_bytes > MAX_SSML_INPUT_BYTES
            ):
                segments.append((
                    _SSML_OPEN + "".join(parts) + _SSML_CLOSE,
                    seg_verses,
                ))
                parts = []
                seg_verses = []
                seg_bytes = overhead
            parts.append(fragment)
            seg_verses.append(v)
            seg_bytes += frag_bytes
        if seg_verses:
            segments.append((
                _SSML_OPEN + "".join(parts) + _SSML_CLOSE,
                seg_verses,
            ))
        return segments
