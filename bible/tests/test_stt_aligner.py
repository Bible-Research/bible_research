import pytest

from bible.services.google_stt.aligner import (
    AlignmentError,
    align_chapter,
    normalize_word,
)


def test_normalize_word_keeps_diacritics_drops_punct_digits():
    assert normalize_word("Jāņa,") == "jāņa"
    assert normalize_word("evaņģēlijs!") == "evaņģēlijs"
    assert normalize_word("3.") == ""
    assert normalize_word("TĀTARU-KĀRI") == "tātarukāri"


def test_exact_match_anchors_every_verse():
    stt = [
        ("In", 0.0, 0.3), ("the", 0.3, 0.5),
        ("beginning", 0.5, 1.0), ("God", 1.0, 1.5),
        ("and", 2.0, 2.3), ("the", 2.3, 2.5),
        ("earth", 2.5, 3.0), ("was", 3.0, 3.4),
    ]
    ref = [
        {"verse_start": 1, "verse_text": "In the beginning God"},
        {"verse_start": 2, "verse_text": "and the earth was"},
    ]
    out = align_chapter(stt, ref)
    assert out["data"] == [
        {"verse_start": 1, "timestamp": 0.0},
        {"verse_start": 2, "timestamp": 2.0},
    ]
    assert out["alignment_score"] == pytest.approx(1.0)


def test_unanchored_verse_interpolates_by_word_count():
    """Verse 2 shares no words with the transcript; its
    timestamp interpolates between the anchored verses 1 and 3
    proportional to reference word counts."""
    stt = [
        ("alpha", 0.0, 0.5), ("beta", 0.5, 1.0),
        ("omega", 10.0, 10.5),
    ]
    ref = [
        # verse 1 anchors at t=0 (words alpha, beta match)
        {"verse_start": 1, "verse_text": "alpha beta"},
        # verse 2 has no matching words
        {"verse_start": 2, "verse_text": "gamma delta"},
        # verse 3 anchors at t=10
        {"verse_start": 3, "verse_text": "omega"},
    ]
    out = align_chapter(stt, ref)
    ts = {d["verse_start"]: d["timestamp"] for d in out["data"]}
    assert ts[1] == 0.0
    assert ts[3] == 10.0
    # Anchors are verse-start times: verse 2 starts after verse
    # 1's 2 words, inside the 4-word span covered by verses 1+2
    # (the words spoken between the v1 and v3 anchors).
    assert ts[2] == pytest.approx((2 / 4) * 10.0, abs=0.001)
    # score = 3 matched of 5 reference words.
    assert out["alignment_score"] == pytest.approx(3 / 5)


def test_interpolation_uses_verse_start_window():
    """Regression: the interpolation window counts the words of
    the verses *between* the anchors — an off-by-one that adds
    the target verse's own words shifts every interpolated
    timestamp late."""
    stt = [
        ("a", 0.0, 0.5), ("b", 0.5, 1.0),
        ("c", 1.0, 1.5), ("d", 1.5, 2.0),
        ("omega", 10.0, 10.5),
    ]
    ref = [
        # verse 1 anchors at t=0 — 4 words
        {"verse_start": 1, "verse_text": "a b c d"},
        # verses 2-3 share no words — 2 and 6 words
        {"verse_start": 2, "verse_text": "e f"},
        {"verse_start": 3, "verse_text": "g h i j k l"},
        # verse 4 anchors at t=10 — 4 words
        {"verse_start": 4, "verse_text": "omega x y z"},
    ]
    out = align_chapter(stt, ref)
    ts = {d["verse_start"]: d["timestamp"] for d in out["data"]}
    assert ts[1] == 0.0
    assert ts[4] == 10.0
    # span = words of verses 1..3 = 4+2+6 = 12.
    assert ts[2] == pytest.approx((4 / 12) * 10.0, abs=0.001)
    assert ts[3] == pytest.approx((6 / 12) * 10.0, abs=0.001)


def test_leading_metadata_never_matches():
    """Spoken metadata before the chapter text ("Jāņa
    evaņģēlijs, trešā nodaļa") must not shift the anchors."""
    stt = [
        ("Jāņa", 0.0, 0.4), ("evaņģēlijs", 0.4, 1.0),
        ("trešā", 1.0, 1.4), ("nodaļa", 1.4, 2.0),
        ("In", 5.0, 5.3), ("the", 5.3, 5.5),
        ("beginning", 5.5, 6.0), ("God", 6.0, 6.5),
        ("and", 7.0, 7.3), ("earth", 7.5, 8.0),
    ]
    ref = [
        {"verse_start": 1, "verse_text": "In the beginning God"},
        {"verse_start": 2, "verse_text": "and the earth was"},
    ]
    out = align_chapter(stt, ref)
    ts = {d["verse_start"]: d["timestamp"] for d in out["data"]}
    assert ts[1] == 5.0
    assert ts[2] == 7.0


def test_fewer_than_two_anchors_raises():
    stt = [("alpha", 0.0, 0.5)]
    ref = [
        {"verse_start": 1, "verse_text": "alpha beta"},
        {"verse_start": 2, "verse_text": "gamma delta"},
    ]
    with pytest.raises(AlignmentError):
        align_chapter(stt, ref)


def test_empty_transcript_raises():
    ref = [
        {"verse_start": 1, "verse_text": "alpha beta"},
        {"verse_start": 2, "verse_text": "gamma delta"},
    ]
    with pytest.raises(AlignmentError):
        align_chapter([], ref)


def test_empty_reference_raises():
    stt = [("alpha", 0.0, 0.5)]
    with pytest.raises(AlignmentError):
        align_chapter(stt, [{"verse_start": 1, "verse_text": ""}])


def test_alignment_score_counts_matched_reference_words():
    stt = [
        ("alpha", 0.0, 0.5), ("omega", 5.0, 5.5),
    ]
    ref = [
        {"verse_start": 1, "verse_text": "alpha beta gamma"},
        {"verse_start": 2, "verse_text": "omega"},
    ]
    out = align_chapter(stt, ref)
    # 2 of 4 reference words matched.
    assert out["alignment_score"] == pytest.approx(0.5)


def test_verse_before_first_anchor_clamps_to_anchor():
    """A verse preceding the first anchored verse cannot
    interpolate backwards — it clamps to the first anchor's
    timestamp rather than inventing a time."""
    stt = [
        ("beta", 3.0, 3.5), ("omega", 8.0, 8.5),
    ]
    ref = [
        {"verse_start": 1, "verse_text": "zzz"},
        {"verse_start": 2, "verse_text": "beta"},
        {"verse_start": 3, "verse_text": "omega"},
    ]
    out = align_chapter(stt, ref)
    ts = {d["verse_start"]: d["timestamp"] for d in out["data"]}
    assert ts[1] == 3.0
    assert ts[2] == 3.0
    assert ts[3] == 8.0
