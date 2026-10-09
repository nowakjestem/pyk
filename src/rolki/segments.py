from __future__ import annotations

import math
import re
from dataclasses import asdict

from .subtitles import Cue, Word

MAX_PART_SECONDS = 180


def clips(result):
    """Legacy single clips and split chapters share the same upload structure."""
    return result.get("parts") or [result]


def ready(result):
    return all(len(clip.get("variants", {})) == 2 for clip in clips(result))


def slice_cues(cues, first, last):
    result = []
    for cue in cues:
        start, end = max(first, cue.start), min(last, cue.end)
        if end <= start:
            continue
        if cue.words:
            words = tuple(
                Word(max(start, w.start) - first, min(end, w.end) - first, w.text)
                for w in cue.words
                if w.end > start and w.start < end
            )
            text = " ".join(w.text for w in words)
            if words:
                start, end = words[0].start + first, words[-1].end + first
        else:
            words = ()
            # Phrase-only ASR cannot locate words exactly. Split the text by its
            # relative position only when a hard duration limit forces a cut.
            tokens = cue.text.split()
            a = round(len(tokens) * (start - cue.start) / (cue.end - cue.start))
            b = round(len(tokens) * (end - cue.start) / (cue.end - cue.start))
            text = " ".join(tokens[a:b])
        if text:
            result.append(Cue(start - first, end - first, text, words))
    return result


def split_chapter(chapter, cues, *, max_seconds=MAX_PART_SECONDS):
    duration = chapter["end"] - chapter["start"]
    count = math.ceil(duration / max_seconds)
    sentences, pauses = [], []
    for cue in cues:
        tokens = cue.words or [cue]
        sentences.extend(w.end for w in tokens if re.search(r"[.!?…][\"'»”’)]*$", w.text))
    for left, right in zip(cues, cues[1:], strict=False):
        if right.start - left.end >= 0.4:
            pauses.append((left.end + right.start) / 2)
    boundaries = [0.0]
    for number in range(1, count):
        first = boundaries[-1]
        remaining = count - number
        target = first + (duration - first) / (remaining + 1)
        lower, upper = (
            max(first + 0.01, duration - remaining * max_seconds),
            min(duration - 0.01, first + max_seconds),
        )
        tolerance = min(15, (target - first) * 0.1)
        options = []
        for candidates in (sentences, pauses):
            options = [
                t for t in candidates if lower <= t <= upper and abs(t - target) <= tolerance
            ]
            if options:
                break
        boundaries.append(min(options, key=lambda t: (abs(t - target), t)) if options else target)
    boundaries.append(duration)
    return [
        {
            "number": number,
            "start": first,
            "end": last,
            "cues": [asdict(c) for c in slice_cues(cues, first, last)],
            "variants": {},
        }
        for number, (first, last) in enumerate(zip(boundaries, boundaries[1:], strict=False), 1)
    ]
