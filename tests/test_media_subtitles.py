import json
from dataclasses import asdict

import pytest
from pydantic import ValidationError

from rolki.config import SubtitleBackground, Subtitles
from rolki.errors import PermanentError
from rolki.media import validate_chapters, video_filter
from rolki.subtitles import (
    Cue,
    Word,
    ass_color,
    cue_from_dict,
    parse_whisper,
    phrase_cues,
    timestamp,
    write_subtitles,
)


def test_chapters_last_end():
    chapters = validate_chapters(
        {
            "duration": 30,
            "chapters": [
                {"start_time": 0, "title": "Początek"},
                {"start_time": 10, "title": "Koniec"},
            ],
        },
        60,
    )
    assert chapters == [
        {"index": 0, "title": "Początek", "start": 0, "end": 10},
        {"index": 1, "title": "Koniec", "start": 10, "end": 30},
    ]


@pytest.mark.parametrize(
    "info",
    [
        {"duration": 30},
        {"duration": 30, "is_live": True},
        {"duration": 100},
        {"duration": float("nan")},
        {"duration": 30, "availability": "private"},
        {"duration": 30, "chapters": [{"start_time": 10, "end_time": 5}]},
        {"duration": 30, "chapters": [{"start_time": 0, "end_time": 20}, {"start_time": 10}]},
    ],
)
def test_bad_metadata(info):
    with pytest.raises(PermanentError):
        validate_chapters(info, 60)


def test_whisper_offsets_and_polish(tmp_path):
    path = tmp_path / "whisper.json"
    path.write_text(
        json.dumps(
            {"transcription": [{"offsets": {"from": 1000, "to": 4000}, "text": " Żółć i gęś "}]}
        )
    )
    assert parse_whisper(path, offset=300, duration=3) == [Cue(301, 303, "Żółć i gęś")]


def test_subtitles_escape_and_wrap(tmp_path, config):
    text = "Zażółć gęślą jaźń i pokaż bardzo długie zdanie, które ma się zmieścić na ekranie. {\\pos(1,2)}"
    cues = [Cue(0, 8, text)]
    wrapped = phrase_cues(cues, config.subtitles)
    assert all(len(c.text.splitlines()) <= 2 for c in wrapped)
    assert all(len(line) <= 26 for c in wrapped for line in c.text.splitlines())
    assert wrapped[0].start == 0 and wrapped[-1].end == 8
    write_subtitles(cues, tmp_path, config.subtitles, config.video)
    ass = (tmp_path / "crop.ass").read_text()
    assert "Zażółć gęślą jaźń" in ass
    assert "｛＼pos" in ass and "{\\pos" not in ass
    assert "130,1" in ass
    assert "230,1" in (tmp_path / "letterbox.ass").read_text()
    assert (tmp_path / "captions.srt").read_text().startswith("1\n00:00:00,000")


def test_time_rounding_and_long_word():
    assert timestamp(59.9999) == "00:01:00,000"
    assert timestamp(3599.9999, ass=True) == "1:00:00.00"
    assert phrase_cues([Cue(0, 2, "x" * 100)], Subtitles())[-1].end == 2


def test_filters_apply_captions_after_geometry(config):
    for variant in ("crop", "letterbox"):
        filters = video_filter(config.video, variant)
        assert filters.endswith(f"subtitles=filename={variant}.ass")
    assert "pad=" in video_filter(config.video, "letterbox")


def whisper_token(text, start, end):
    return {"text": text, "offsets": {"from": start, "to": end}}


def test_whisper_joins_polish_subwords_and_ignores_punctuation_times(tmp_path):
    path = tmp_path / "whisper.json"
    path.write_text(
        json.dumps(
            {
                "transcription": [
                    {
                        "text": " Zażółć gęślą, jaźń!",
                        "offsets": {"from": 100, "to": 4000},
                        "tokens": [
                            whisper_token("[_BEG_]", 0, 0),
                            whisper_token(" Za", 100, 200),
                            whisper_token("żółć", 200, 500),
                            whisper_token(" gę", 1000, 1200),
                            whisper_token("ślą", 1200, 1600),
                            whisper_token(",", 2900, 3000),
                            whisper_token(" jaźń", 3000, 5000),
                            whisper_token("!", 5000, 5000),
                            {"text": "[_TT_250]"},
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    cue = parse_whisper(path, offset=300, duration=3.5)[0]
    assert cue.words == (
        Word(300.1, 300.5, "Zażółć"),
        Word(301, 301.6, "gęślą,"),
        Word(303, 303.5, "jaźń!"),
    )
    assert cue.end == 303.5
    assert cue_from_dict(json.loads(json.dumps(asdict(cue)))) == cue
    assert cue_from_dict({"start": 0, "end": 1, "text": "stary zapis"}) == Cue(0, 1, "stary zapis")


@pytest.mark.parametrize(
    "tokens",
    [
        [{"text": " słowo"}],
        [whisper_token(" słowo", -10, 100)],
        [whisper_token(" słowo", 100, 50)],
        [whisper_token(" słowo", 0, float("nan"))],
        [whisper_token(" inne", 0, 100)],
        [whisper_token(" dwa słowa", 0, 100)],
    ],
)
def test_incomplete_token_data_retains_phrase_for_line_fallback(tmp_path, tokens):
    path = tmp_path / "whisper.json"
    path.write_text(
        json.dumps(
            {
                "transcription": [
                    {
                        "text": "słowo",
                        "offsets": {"from": 0, "to": 1000},
                        "tokens": tokens,
                    }
                ]
            }
        )
    )
    assert parse_whisper(path, duration=1) == [Cue(0, 1, "słowo")]


def test_timed_wrapping_uses_word_boundaries_not_equal_time_slices():
    words = (
        Word(0.2, 0.4, "pierwsze"),
        Word(0.6, 1, "słowo"),
        Word(3, 3.3, "następne"),
        Word(3.4, 4, "słowo"),
    )
    style = Subtitles(max_chars_per_line=8, max_lines=2, max_phrase_seconds=2)
    result = phrase_cues([Cue(0, 5, "pierwsze słowo następne słowo", words)], style)
    assert result == [
        Cue(0.2, 1, "pierwsze\nsłowo", words[:2]),
        Cue(3, 4, "następne\nsłowo", words[2:]),
    ]


def test_word_boxes_have_exact_times_stable_foreground_and_safe_text(tmp_path, config):
    cue = Cue(0, 2, "Zażółć gęślą", (Word(0.1, 0.5, "Zażółć"), Word(0.9, 1.8, "gęślą")))
    style = Subtitles(background=SubtitleBackground(mode="word", color="#123456", opacity=0.8))
    write_subtitles([cue], tmp_path, style, config.video)
    ass = (tmp_path / "crop.ass").read_text()
    events = [line for line in ass.splitlines() if line.startswith("Dialogue:")]
    assert len(events) == 3
    assert "0:00:00.10,0:00:00.50,Box" in events[0]
    assert r"{\3a&H33&}Zażółć{\3a&HFF&} gęślą" in events[0]
    assert "0:00:00.90,0:00:01.80,Box" in events[1]
    assert r"Zażółć {\3a&H33&}gęślą{\3a&HFF&}" in events[1]
    assert events[2].endswith(",Default,,0,0,0,,Zażółć gęślą")
    assert "&H33563412" in ass


@pytest.mark.parametrize(
    "mode,opacity,boxes", [("none", 1, 0), ("line", 1, 1), ("word", 1, 1), ("line", 0, 0)]
)
def test_background_modes_and_legacy_fallback(tmp_path, config, mode, opacity, boxes):
    style = Subtitles(background=SubtitleBackground(mode=mode, opacity=opacity))
    write_subtitles([Cue(0, 1, "Zażółć gęślą jaźń")], tmp_path, style, config.video)
    ass = (tmp_path / "letterbox.ass").read_text()
    assert ass.count(",Box,,") == boxes
    assert ",Default,," in ass
    assert "Lato" in ass


def test_zero_duration_word_does_not_drop_transcription(tmp_path, config):
    style = Subtitles(background=SubtitleBackground(mode="word"))
    write_subtitles([Cue(0, 1, "test", (Word(0, 0, "test"),))], tmp_path, style, config.video)
    assert (tmp_path / "crop.ass").read_text().count(",Box,,") == 1
    assert "test" in (tmp_path / "captions.srt").read_text()


@pytest.mark.parametrize(
    "settings",
    [
        {"mode": "karaoke"},
        {"opacity": -0.1},
        {"opacity": 1.1},
        {"padding": -1},
        {"padding": 40},
        {"color": "red"},
    ],
)
def test_background_settings_are_validated(settings):
    with pytest.raises(ValidationError):
        SubtitleBackground(**settings)


def test_background_alpha_endpoints():
    assert ass_color("#ABCDEF", opacity=0) == "&HFFEFCDAB"
    assert ass_color("#ABCDEF", opacity=1) == "&H00EFCDAB"


async def test_transcribe_requests_full_json_and_preserves_chunk_offsets(
    tmp_path, config, monkeypatch
):
    from rolki import media

    model = tmp_path / "model.bin"
    model.touch()
    config = config.model_copy(
        update={
            "asr": config.asr.model_copy(
                update={"model_path": model, "max_chunk_seconds": 30},
            )
        }
    )
    commands = []

    async def audio(*_args):
        pass

    async def process(args, **_kwargs):
        commands.append(args)
        prefix = tmp_path / "whisper.json"
        prefix.write_text(
            json.dumps(
                {
                    "transcription": [
                        {
                            "text": " słowo",
                            "offsets": {"from": 100, "to": 500},
                            "tokens": [whisper_token(" słowo", 100, 500)],
                        }
                    ]
                }
            )
        )

    monkeypatch.setattr(media, "extract_audio", audio)
    monkeypatch.setattr(media, "silent_audio", lambda _: False)
    monkeypatch.setattr(media, "run_process", process)
    cues = await media.transcribe(
        tmp_path / "source.mp4", {"start": 12, "end": 44}, tmp_path, config
    )
    assert len(commands) == 2 and all("-ojf" in args for args in commands)
    assert cues[1].words == (Word(30.1, 30.5, "słowo"),)
    assert cue_from_dict(json.loads((tmp_path / "transcript.json").read_text())[1]) == cues[1]
    assert not (tmp_path / "whisper.json").exists()
