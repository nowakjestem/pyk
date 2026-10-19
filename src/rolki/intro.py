from __future__ import annotations

from fractions import Fraction
from pathlib import Path
from tempfile import TemporaryDirectory

from .config import Intro
from .errors import PermanentError
from .process import run_process

WIDTH, HEIGHT = 128, 72
FRAME_BYTES = WIDTH * HEIGHT * 3
MAX_FRAMES = 1800
MAX_LEAD_SECONDS = 0.35
MIN_CARD_SECONDS = 0.2
CONFIRM_SECONDS = 0.15
MIN_REMAINING_SECONDS = 0.3


def title_frame(frame: bytes, settings: Intro) -> tuple[bool, bool]:
    """Return (title layout, visible white text); compression need not be identical."""
    if len(frame) != FRAME_BYTES:
        raise PermanentError("Niepełna klatka podczas wykrywania planszy.")
    color = tuple(int(settings.background_color[i : i + 2], 16) for i in (1, 3, 5))
    background = white = border = border_background = 0
    for index, (red, green, blue) in enumerate(
        zip(frame[0::3], frame[1::3], frame[2::3], strict=True)
    ):
        is_background = (
            max(abs(red - color[0]), abs(green - color[1]), abs(blue - color[2]))
            <= settings.color_tolerance
        )
        is_white = (
            min(red, green, blue) >= 210 and max(red, green, blue) - min(red, green, blue) <= 30
        )
        background += is_background
        white += is_white
        x, y = index % WIDTH, index // WIDTH
        if x < WIDTH * 0.1 or x >= WIDTH * 0.9 or y < HEIGHT * 0.2 or y >= HEIGHT * 0.8:
            border += 1
            border_background += is_background
    pixels = WIDTH * HEIGHT
    layout = (
        background / pixels >= 0.75
        and (background + white) / pixels >= 0.96
        and border_background / border >= 0.95
    )
    return layout, white / pixels >= 0.002


def frame_times(path: Path):
    """Framecrc preserves each decoded frame's PTS, including variable frame rates."""
    time_base = None
    with path.open(encoding="utf-8") as source:
        for line in source:
            if line.startswith("#tb 0:"):
                time_base = Fraction(line.split(":", 1)[1].strip())
            elif line.strip() and not line.startswith("#"):
                fields = line.split(",")
                if time_base is None or len(fields) != 6 or int(fields[4]) != FRAME_BYTES:
                    raise PermanentError("Niepoprawne dane klatek podczas wykrywania planszy.")
                yield float(int(fields[2]) * time_base)


def find_intro_end(
    frames: Path, timestamps: Path, settings: Intro, duration: float
) -> tuple[float, str]:
    first_title = candidate = None
    count = 0
    previous_time = -1.0
    with frames.open("rb") as source:
        for time in frame_times(timestamps):
            count += 1
            if time < 0 or time < previous_time:
                raise PermanentError("Niepoprawne czasy klatek podczas wykrywania planszy.")
            previous_time = time
            layout, text = title_frame(source.read(FRAME_BYTES), settings)
            if first_title is None:
                if time > MAX_LEAD_SECONDS:
                    return 0.0, "no_title_at_start"
                if layout and text:
                    first_title = time
            elif layout:
                # Keep blank burgundy frames when the title text fades away.
                candidate = None
            else:
                if candidate is None:
                    candidate = time
                if time - candidate >= CONFIRM_SECONDS:
                    if candidate - first_title < MIN_CARD_SECONDS:
                        return 0.0, "title_too_short"
                    if duration - candidate < MIN_REMAINING_SECONDS:
                        return 0.0, "insufficient_content"
                    return candidate, "title_removed"
    if not count:
        raise PermanentError("Brak klatek podczas wykrywania planszy.")
    # Never guess if the card/transition extends beyond the analysis window.
    return 0.0, "unconfirmed_end" if first_title is not None else "no_title_at_start"


async def detect_intro(
    source: Path, chapter: dict, root: Path, settings: Intro, *, threads: int = 2
) -> dict:
    start, duration = chapter["start"], chapter["end"] - chapter["start"]
    if not settings.enabled:
        return {"start": start, "removed_seconds": 0.0, "reason": "disabled"}
    # Small raw frames go to disk, not a binary stdout buffer. Reuse the process
    # wrapper's timeout, cancellation and child-group cleanup; no CV dependency.
    with TemporaryDirectory(prefix="intro-", dir=root) as temporary:
        scratch = Path(temporary)
        await run_process(
            [
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-threads",
                str(threads),
                "-filter_threads",
                "1",
                "-ss",
                str(start),
                "-i",
                str(source.resolve()),
                "-t",
                str(min(duration, settings.max_scan_seconds)),
                "-map",
                "0:v:0",
                "-an",
                "-sn",
                "-dn",
                "-vf",
                f"scale={WIDTH}:{HEIGHT},format=rgb24",
                "-fps_mode",
                "passthrough",
                "-frames:v",
                str(MAX_FRAMES),
                "-c:v",
                "rawvideo",
                "-enc_time_base",
                "-1",
                "-threads",
                "1",
                "-f",
                "tee",
                "[f=rawvideo]frames.rgb|[f=framecrc]frames.txt",
            ],
            timeout=120,
            cwd=scratch,
        )
        removed, reason = find_intro_end(
            scratch / "frames.rgb", scratch / "frames.txt", settings, duration
        )
    return {"start": start + removed, "removed_seconds": removed, "reason": reason}
