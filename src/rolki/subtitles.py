from __future__ import annotations

import json
import math
import re
import textwrap
from dataclasses import dataclass
from pathlib import Path

from .config import Subtitles, Video
from .errors import PermanentError


@dataclass(frozen=True)
class Word:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    text: str
    words: tuple[Word, ...] = ()


def cue_from_dict(data: dict) -> Cue:
    """Read both old phrase-only checkpoints and checkpoints with word timings."""
    return Cue(
        data["start"],
        data["end"],
        data["text"],
        tuple(Word(**word) for word in data.get("words", ())),
    )


def token_words(segment: dict, start: float, end: float, offset: float) -> tuple[Word, ...]:
    """Join BPE subwords and punctuation, without inventing missing timestamps."""
    groups: list[tuple[str, list[tuple[float, float]]]] = []
    try:
        for token in segment.get("tokens", []):
            text = token["text"]
            if re.fullmatch(r"\[_[^\]]*\]|<\|.*\|>", text) or not text:
                continue
            if text.isspace():
                # Whitespace-only tokens delimit the next word.
                groups.append(("", []))
                continue
            if len(text.split()) != 1:
                return ()  # One token covering multiple words cannot time each word.
            if text[0].isspace() or not groups:
                groups.append(("", []))
            word, times = groups[-1]
            groups[-1] = (word + text.strip(), times)
            # A comma/period may be timed at the end of a long pause: keep its text,
            # but do not extend the spoken word's highlight through that pause.
            if any(char.isalnum() for char in text):
                first = float(token["offsets"]["from"]) / 1000
                last = float(token["offsets"]["to"]) / 1000
                if not (math.isfinite(first) and math.isfinite(last) and 0 <= first <= last):
                    return ()
                times.append((first, last))
        groups = [(text, times) for text, times in groups if text]
        if " ".join(text for text, _ in groups) != " ".join(segment["text"].split()):
            return ()
        result = []
        previous_end = start
        for text, times in groups:
            if not times:
                return ()
            first = min(end, max(previous_end, min(t[0] for t in times)))
            last = max(first, min(end, max(t[1] for t in times)))
            result.append(Word(offset + first, offset + last, text))
            previous_end = last
        return tuple(result)
    except (ValueError, KeyError, TypeError, IndexError, AttributeError):
        # A valid phrase remains usable even if its optional token data is incomplete.
        return ()


def parse_whisper(path: Path, *, offset=0.0, duration: float) -> list[Cue]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        cues = []
        for segment in document["transcription"]:
            first = float(segment["offsets"]["from"]) / 1000
            last = float(segment["offsets"]["to"]) / 1000
            if not (math.isfinite(first) and math.isfinite(last)):
                raise ValueError("nonfinite timestamp")
            start = max(0.0, first)
            end = min(duration, last)
            text = " ".join(segment["text"].split())
            if text and end > start:
                cues.append(
                    Cue(
                        offset + start, offset + end, text, token_words(segment, start, end, offset)
                    )
                )
        return cues
    except (ValueError, KeyError, TypeError) as exc:
        raise PermanentError("Niepoprawny wynik transkrypcji whisper.cpp.") from exc


def timed_phrases(cue: Cue, config: Subtitles, start: float) -> list[Cue]:
    """Wrap whole words and change phrases at ASR word boundaries, not estimates."""
    result = []
    group: list[Word] = []

    def emit():
        first = max(start, group[0].start)
        last = min(cue.end, group[-1].end)
        if last > first:
            text = "\n".join(
                textwrap.wrap(
                    " ".join(word.text for word in group),
                    width=config.max_chars_per_line,
                    break_long_words=False,
                    break_on_hyphens=False,
                )
            )
            bounded = tuple(Word(max(first, w.start), min(last, w.end), w.text) for w in group)
            result.append(Cue(first, last, text, bounded))

    for word in cue.words:
        candidate = [*group, word]
        lines = textwrap.wrap(
            " ".join(w.text for w in candidate),
            width=config.max_chars_per_line,
            break_long_words=False,
            break_on_hyphens=False,
        )
        if group and (
            len(lines) > config.max_lines or word.end - group[0].start > config.max_phrase_seconds
        ):
            emit()
            group = []
        group.append(word)
    if group:
        emit()
    return result


def phrase_cues(cues: list[Cue], config: Subtitles) -> list[Cue]:
    """Use word boundaries when available; retain legacy phrase-only transcripts."""
    result = []
    last_end = 0.0
    for cue in sorted(cues, key=lambda item: item.start):
        start = max(last_end, cue.start)
        if cue.end <= start:
            continue
        if (
            cue.words
            and [w.text for w in cue.words] == cue.text.split()
            and all(w.end > w.start and len(w.text) <= config.max_chars_per_line for w in cue.words)
        ):
            result.extend(timed_phrases(cue, config, start))
            last_end = cue.end
            continue
        lines = textwrap.wrap(
            cue.text,
            width=config.max_chars_per_line,
            break_long_words=True,
            break_on_hyphens=False,
        )
        groups = [lines[i : i + config.max_lines] for i in range(0, len(lines), config.max_lines)]
        if not groups:
            continue
        # A sparse, very long segment also needs bounded display time per phrase.
        minimum = math.ceil((cue.end - start) / config.max_phrase_seconds)
        if len(groups) < minimum:
            words = cue.text.split()
            size = max(1, math.ceil(len(words) / minimum))
            groups = []
            for i in range(0, len(words), size):
                wrapped = textwrap.wrap(
                    " ".join(words[i : i + size]),
                    width=config.max_chars_per_line,
                    break_on_hyphens=False,
                )
                groups.extend(
                    wrapped[j : j + config.max_lines]
                    for j in range(0, len(wrapped), config.max_lines)
                )
        weights = [max(1, len(" ".join(group))) for group in groups]
        total = sum(weights)
        elapsed = 0
        for group, weight in zip(groups, weights, strict=True):
            group_start = start + (cue.end - start) * elapsed / total
            elapsed += weight
            group_end = start + (cue.end - start) * elapsed / total
            result.append(
                Cue(
                    group_start,
                    min(group_end, group_start + config.max_phrase_seconds),
                    "\n".join(group),
                )
            )
        last_end = cue.end
    return result


def timestamp(seconds: float, *, ass=False) -> str:
    factor = 100 if ass else 1000
    ticks = max(0, round(seconds * factor))
    hours, rest = divmod(ticks, 3600 * factor)
    minutes, rest = divmod(rest, 60 * factor)
    seconds, fractions = divmod(rest, factor)
    return (
        f"{hours}:{minutes:02}:{seconds:02}.{fractions:02}"
        if ass
        else f"{hours:02}:{minutes:02}:{seconds:02},{fractions:03}"
    )


def ass_color(color: str, *, opacity: float = 1) -> str:
    alpha = round((1 - opacity) * 255)
    return f"&H{alpha:02X}{color[5:7]}{color[3:5]}{color[1:3]}"


def ass_text(text: str) -> str:
    # ASR text is untrusted: prevent override tags and injected ASS escapes.
    return (
        text.replace("\\", "＼")
        .replace("{", "｛")
        .replace("}", "｝")
        .replace("\r", "")
        .replace("\n", r"\N")
    )


def dialogue(cue: Cue | Word, text: str, *, background=False) -> str:
    return (
        f"Dialogue: {0 if background else 1},{timestamp(cue.start, ass=True)},"
        f"{timestamp(cue.end, ass=True)},{'Box' if background else 'Default'},,0,0,0,,{text}"
    )


def background_events(cue: Cue, style: Subtitles) -> list[str]:
    background = style.background
    if background.mode == "none" or background.opacity == 0:
        return []
    matches = list(re.finditer(r"\S+", cue.text))
    if (
        background.mode == "line"
        or not cue.words
        or any(w.end <= w.start for w in cue.words)
        or [match.group() for match in matches] != [w.text for w in cue.words]
    ):
        return [dialogue(cue, ass_text(cue.text), background=True)]
    # Same full phrase in both layers keeps font shaping, kerning and positioning
    # identical. libass's opaque-box style draws only the visible override run.
    # The foreground is a single stable event with its independent text outline.
    alpha = round((1 - background.opacity) * 255)
    result = []
    for word, match in zip(cue.words, matches, strict=True):
        if timestamp(word.start, ass=True) == timestamp(word.end, ass=True):
            continue
        text = (
            r"{\3a&HFF&}"
            + ass_text(cue.text[: match.start()])
            + rf"{{\3a&H{alpha:02X}&}}"
            + ass_text(match.group())
            + r"{\3a&HFF&}"
            + ass_text(cue.text[match.end() :])
        )
        result.append(dialogue(word, text, background=True))
    return result


def write_subtitles(cues: list[Cue], root: Path, style: Subtitles, video: Video):
    root.mkdir(parents=True, exist_ok=True)
    cues = phrase_cues(cues, style)
    srt = "\n\n".join(
        f"{i}\n{timestamp(c.start)} --> {timestamp(c.end)}\n{c.text}" for i, c in enumerate(cues, 1)
    )
    (root / "captions.srt").write_text(srt + "\n", encoding="utf-8")
    for variant in ("crop", "letterbox"):
        position = getattr(style, variant)
        header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {video.width}
PlayResY: {video.height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{style.font},{style.font_size},{ass_color(style.primary_color)},&H000000FF,{ass_color(style.outline_color)},&H80000000,{-1 if style.bold else 0},0,0,0,100,100,0,0,1,{style.outline},{style.shadow},{position.alignment},{style.margin_x},{style.margin_x},{position.margin_v},1
Style: Box,{style.font},{style.font_size},&HFF000000,&HFF000000,{ass_color(style.background.color, opacity=style.background.opacity)},&HFF000000,{-1 if style.bold else 0},0,0,0,100,100,0,0,3,{style.background.padding},0,{position.alignment},{style.margin_x},{style.margin_x},{position.margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
        events = "\n".join(
            event
            for cue in cues
            for event in (
                *background_events(cue, style),
                dialogue(cue, ass_text(cue.text)),
            )
        )
        (root / f"{variant}.ass").write_text(header + events + "\n", encoding="utf-8")


def safe_markdown(text: str) -> str:
    return re.sub(r"([\\`*_{}\[\]()<>#!|])", r"\\\1", " ".join(text.split()))[:200]
