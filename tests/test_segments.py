import math

import pytest

from rolki.segments import slice_cues, split_chapter
from rolki.subtitles import Cue, Word


@pytest.mark.parametrize("duration", [10, 180, 180.01, 300, 360, 361, 540, 1200.3])
def test_equal_parts_cover_entire_chapter_with_no_overflow(duration):
    parts = split_chapter({"start": 200, "end": 200 + duration}, [])
    assert len(parts) == math.ceil(duration / 180)
    assert parts[0]["start"] == 0
    assert parts[-1]["end"] == duration
    assert all(0 < p["end"] - p["start"] <= 180 for p in parts)
    assert all(a["end"] == b["start"] for a, b in zip(parts, parts[1:], strict=False))
    assert (
        max(p["end"] - p["start"] for p in parts) - min(p["end"] - p["start"] for p in parts) < 1e-8
    )


def test_sentence_boundary_preferred_near_balanced_cut():
    cues = [
        Cue(
            140,
            165,
            "Pierwsze zdanie. Drugie",
            (Word(140, 145, "Pierwsze"), Word(145, 148, "zdanie."), Word(149, 165, "Drugie")),
        )
    ]
    parts = split_chapter({"start": 0, "end": 300}, cues)
    assert [p["end"] - p["start"] for p in parts] == [148, 152]
    assert parts[0]["cues"][0]["text"] == "Pierwsze zdanie."
    assert parts[1]["cues"][0]["text"] == "Drugie"
    assert parts[1]["cues"][0]["start"] == 1


def test_sentence_shift_never_makes_later_parts_exceed_limit():
    parts = split_chapter({"start": 0, "end": 360}, [Cue(170, 179, "Koniec.")])
    assert [p["end"] - p["start"] for p in parts] == [180, 180]


def test_pause_fallback_and_phrase_only_timestamps():
    cues = [Cue(130, 146, "Bez kropki"), Cue(148, 165, "Kolejne słowa")]
    parts = split_chapter({"start": 0, "end": 300}, cues)
    assert parts[0]["end"] == 147
    assert parts[1]["cues"][0]["start"] == 1
    assert slice_cues([Cue(0, 10, "raz dwa trzy cztery")], 5, 10)[0].text == "trzy cztery"
