import re
from unittest.mock import patch

from bible.services.google_tts import synthesizer as synth_module
from bible.services.google_tts.synthesizer import Synthesizer


class _FakeMP3Info:
    def __init__(self, length):
        self.length = length


class _FakeMP3:
    def __init__(self, _stream):
        self.info = _FakeMP3Info(2.0)


class _FakeSword:
    """Stand-in for SwordClient.get_chapter_verses."""

    def __init__(self, verses):
        self._verses = verses

    def get_chapter_verses(self, _canon, _book, _chapter):
        return self._verses


def _verses(n, text_len=50):
    return [
        {"verse_start": i, "verse_text": f"v{i} " + "x" * text_len}
        for i in range(1, n + 1)
    ]


class _FakeChapterTTS:
    """Fake Cloud TTS SSML endpoint: returns each <mark name="vN"/>
    as a timepoint one second apart within the segment."""

    def __init__(self):
        self.ssml_calls = []

    def synthesize_ssml(self, ssml, **_):
        self.ssml_calls.append(ssml)
        marks = re.findall(r'<mark name="(v\d+)"/>', ssml)
        timepoints = [
            {"mark_name": name, "time_seconds": float(i)}
            for i, name in enumerate(marks)
        ]
        return b"\xff\xfb" + b"\x00" * 100, timepoints


def _chapter_synth(verses, tts):
    synth = Synthesizer(tts_client=tts)
    synth._sword = _FakeSword(verses)
    return synth


@patch("bible.services.google_tts.synthesizer.MP3", _FakeMP3)
def test_synthesizer_builds_cumulative_timestamps():
    fake_audio = b"\xff\xfb" + b"\x00" * 100  # opaque bytes; MP3 mocked

    class FakeTTS:
        def synthesize_text(self, text, **_):
            return fake_audio

    synth = Synthesizer(tts_client=FakeTTS())
    artifacts = synth.run("LVSGLU8", "JHN", 3)

    payload = artifacts.timestamps_payload
    assert payload["data"][0]["verse_start"] == 1
    assert payload["data"][0]["timestamp"] == 0.0
    # Each verse = 2.0s by the MP3 mock; verse 2 starts at 2.0s
    assert payload["data"][1]["timestamp"] == 2.0
    # MP3 bytes are concatenated
    assert artifacts.mp3_bytes.startswith(fake_audio)
    assert len(artifacts.mp3_bytes) == len(fake_audio) * len(payload["data"])
    assert artifacts.chars_used > 0
    assert isinstance(artifacts.chars_used, int)


@patch("bible.services.google_tts.synthesizer.MP3", _FakeMP3)
def test_chapter_mode_maps_ssml_marks_to_verse_timestamps():
    tts = _FakeChapterTTS()
    synth = _chapter_synth(_verses(3), tts)

    artifacts = synth.run("LVSGLU8", "JHN", 3, mode="chapter")

    # One SSML doc for a small chapter, one mark per verse.
    assert len(tts.ssml_calls) == 1
    ssml = tts.ssml_calls[0]
    assert ssml.startswith("<speak><mark name=\"v1\"/>")
    assert '<mark name="v3"/>' in ssml
    assert ssml.endswith("</speak>")

    data = artifacts.timestamps_payload["data"]
    assert [d["verse_start"] for d in data] == [1, 2, 3]
    assert [d["timestamp"] for d in data] == [0.0, 1.0, 2.0]
    assert artifacts.duration_seconds == 2.0  # single 2s MP3 mock
    # Billed chars include the SSML markup, not just verse text.
    assert artifacts.chars_used == len(ssml)


@patch("bible.services.google_tts.synthesizer.MP3", _FakeMP3)
def test_chapter_mode_escapes_verse_text_in_ssml():
    tts = _FakeChapterTTS()
    verses = [{
        "verse_start": 1,
        "verse_text": 'a < b & "c" > d',
    }]
    synth = _chapter_synth(verses, tts)

    synth.run("LVSGLU8", "PS", 23, mode="chapter")

    ssml = tts.ssml_calls[0]
    assert "&lt;" in ssml and "&amp;" in ssml and "&gt;" in ssml
    assert "a < b" not in ssml


@patch("bible.services.google_tts.synthesizer.MP3", _FakeMP3)
def test_chapter_mode_segments_oversized_chapter_with_offsets(
    monkeypatch,
):
    """Chapters over the SSML byte budget split into consecutive-verse
    segments; later segments' mark timepoints are offset by the
    cumulative MP3 duration of prior segments."""
    # ~68 bytes per verse fragment + 15 bytes <speak></speak> overhead
    # -> exactly 2 verses per 200-byte segment, 4 verses = 2 segments.
    monkeypatch.setattr(synth_module, "MAX_SSML_INPUT_BYTES", 200)

    tts = _FakeChapterTTS()
    synth = _chapter_synth(_verses(4), tts)

    artifacts = synth.run("LVSGLU8", "JHN", 3, mode="chapter")

    assert len(tts.ssml_calls) == 2
    assert '<mark name="v3"/>' in tts.ssml_calls[1]
    assert '<mark name="v3"/>' not in tts.ssml_calls[0]

    # Segment 2 timestamps are offset by segment 1's 2.0s duration.
    data = artifacts.timestamps_payload["data"]
    assert [d["verse_start"] for d in data] == [1, 2, 3, 4]
    assert [d["timestamp"] for d in data] == [0.0, 1.0, 2.0, 3.0]
    assert artifacts.duration_seconds == 4.0
    assert artifacts.chars_used == sum(
        len(s) for s in tts.ssml_calls
    )


@patch("bible.services.google_tts.synthesizer.MP3", _FakeMP3)
def test_chapter_mode_missing_mark_falls_back_to_previous_timestamp():
    """If the voice drops a <mark> (partial SSML support), the verse
    keeps a sane timestamp instead of disappearing."""

    class DropMarkTTS(_FakeChapterTTS):
        def synthesize_ssml(self, ssml, **_):
            audio, timepoints = super().synthesize_ssml(ssml, **_)
            return audio, [
                tp for tp in timepoints if tp["mark_name"] != "v2"
            ]

    tts = DropMarkTTS()
    synth = _chapter_synth(_verses(3), tts)

    artifacts = synth.run("LVSGLU8", "JHN", 3, mode="chapter")

    data = artifacts.timestamps_payload["data"]
    assert [d["verse_start"] for d in data] == [1, 2, 3]
    # v2 inherits v1's timestamp; v3's real mark still wins.
    assert [d["timestamp"] for d in data] == [0.0, 0.0, 2.0]
