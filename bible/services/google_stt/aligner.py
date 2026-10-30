"""Pure STT-word -> verse alignment.

Cloud STT yields word-level time offsets; verse numbers come from
matching the transcript against a known chapter text (e.g. the
LVSGLU8 SWORD module). Everything here is a pure function over
plain data so it is fully unit-testable — no GCP, no Django.
"""
import difflib
from typing import Dict, List, Sequence, Tuple

# Artifacts scoring below this are still uploaded but flagged
# ``low_confidence`` so they can be inspected later. The registry
# may override it per fileset (LATBSLP* reference text is a
# different translation edition, so drift lowers scores even on
# correct alignment).
MIN_ALIGNMENT_SCORE = 0.5

# A chapter must anchor at least this many verses via matched
# words; fewer means the transcript cannot be trusted to map to
# verses at all (wrong audio, wrong reference, empty STT result).
MIN_ANCHORS = 2


class AlignmentError(Exception):
    """Raised when the transcript cannot be anchored to the
    reference text."""


def normalize_word(word: str) -> str:
    """Lowercase and drop every non-letter character.

    Keeps Latvian diacritics (they are unicode letters); strips
    punctuation and digits."""
    return "".join(ch for ch in word.lower() if ch.isalpha())


def _verse_tokens(verse_text: str) -> List[str]:
    return [
        tok
        for tok in (
            normalize_word(w) for w in (verse_text or "").split()
        )
        if tok
    ]


def align_chapter(
    stt_words: Sequence[Tuple[str, float, float]],
    reference_verses: Sequence[dict],
    min_anchors: int = MIN_ANCHORS,
) -> dict:
    """Map STT word offsets to verse start timestamps.

    Args:
        stt_words: ``[(word, start_s, end_s), ...]`` in audio
            order (``GoogleSTTClient.long_running_recognize_uri``
            output).
        reference_verses: ``[{verse_start, verse_text}, ...]``
            covering the chapter.
        min_anchors: minimum number of verses that must anchor on
            a matched word; below this the chapter fails.

    Returns:
        ``{"data": [{verse_start, timestamp}, ...],
          "alignment_score": float}`` where the score is matched
        reference words / total reference words.

    Raises:
        AlignmentError: fewer than ``min_anchors`` verses anchored.
    """
    # Only non-empty normalized STT tokens participate in
    # matching; ``match_stt_idx`` maps a match-list position back
    # to the original stt_words index (and its timestamp).
    match_tokens: List[str] = []
    match_stt_idx: List[int] = []
    for i, (word, _s, _e) in enumerate(stt_words):
        tok = normalize_word(word)
        if tok:
            match_tokens.append(tok)
            match_stt_idx.append(i)

    # Flatten the reference chapter into a parallel word list;
    # ``ref_verse_pos`` maps each token to its verse position.
    ref_tokens: List[str] = []
    ref_verse_pos: List[int] = []
    verse_word_counts: List[int] = []
    for pos, verse in enumerate(reference_verses):
        toks = _verse_tokens(verse.get("verse_text", ""))
        verse_word_counts.append(len(toks))
        ref_tokens.extend(toks)
        ref_verse_pos.extend([pos] * len(toks))
    if not ref_tokens:
        raise AlignmentError("reference text has no words")

    # ``SequenceMatcher`` matching blocks are the anchor points:
    # each verse's FIRST matched word fixes where that verse
    # starts in the audio. Leading spoken metadata ("Jāņa
    # evaņģēlijs, trešā nodaļa") simply never matches, so it
    # needs no special-casing.
    matcher = difflib.SequenceMatcher(
        None, ref_tokens, match_tokens, autojunk=False,
    )
    anchors: Dict[int, int] = {}
    matched_ref_words = 0
    for block in matcher.get_matching_blocks():
        matched_ref_words += block.size
        for k in range(block.size):
            pos = ref_verse_pos[block.a + k]
            if pos not in anchors:
                anchors[pos] = match_stt_idx[block.b + k]

    if len(anchors) < min_anchors:
        raise AlignmentError(
            f"only {len(anchors)} anchored verses "
            f"(min_anchors={min_anchors})"
        )
    alignment_score = matched_ref_words / len(ref_tokens)

    data = [
        {
            "verse_start": verse["verse_start"],
            "timestamp": round(
                _verse_timestamp(
                    pos, anchors, verse_word_counts, stt_words,
                ),
                3,
            ),
        }
        for pos, verse in enumerate(reference_verses)
    ]
    return {"data": data, "alignment_score": alignment_score}


def _verse_timestamp(
    pos: int,
    anchors: Dict[int, int],
    verse_word_counts: List[int],
    stt_words: Sequence[Tuple[str, float, float]],
) -> float:
    """Timestamp for the verse at ``pos``.

    Anchored verses read their first matched word's start time.
    Unanchored verses interpolate linearly between the nearest
    neighbouring anchored verses, proportional to reference word
    counts; verses outside the anchor range clamp to the nearest
    anchor's timestamp (there is nothing to interpolate
    against)."""
    if pos in anchors:
        return stt_words[anchors[pos]][1]

    prev_pos = max((p for p in anchors if p < pos), default=None)
    next_pos = min((p for p in anchors if p > pos), default=None)
    if prev_pos is None:
        return stt_words[anchors[next_pos]][1]
    if next_pos is None:
        return stt_words[anchors[prev_pos]][1]

    t_prev = stt_words[anchors[prev_pos]][1]
    t_next = stt_words[anchors[next_pos]][1]
    span_words = sum(
        verse_word_counts[prev_pos + 1: next_pos + 1]
    )
    mid_words = sum(
        verse_word_counts[prev_pos + 1: pos + 1]
    )
    frac = mid_words / span_words if span_words else 0.0
    return t_prev + frac * (t_next - t_prev)
