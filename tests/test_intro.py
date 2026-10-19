import shutil
import subprocess
import wave
from pathlib import Path

import pytest

from rolki.config import Intro
from rolki.intro import FRAME_BYTES, HEIGHT, WIDTH, detect_intro, find_intro_end, title_frame
from rolki.media import extract_audio, probe, render
from rolki.process import run_process
from rolki.subtitles import Cue, write_subtitles


def rgb_frame(color=(113, 21, 79), *, text=True):
    frame = bytearray(bytes(color) * (WIDTH * HEIGHT))
    if text:
        for y in range(30, 40):
            for x in range(30, 100):
                index = (y * WIDTH + x) * 3
                frame[index : index + 3] = bytes((250, 248, 249))
    return bytes(frame)


def sequence(tmp_path, entries):
    frames, times = tmp_path / "frames.rgb", tmp_path / "times.txt"
    frames.write_bytes(b"".join(frame for _, frame in entries))
    times.write_text(
        "#tb 0: 1/1000\n"
        + "".join(f"0, {time}, {time}, 1, {FRAME_BYTES}, 0x0\n" for time, _ in entries)
    )
    return frames, times


@pytest.mark.parametrize("color", [(113, 21, 79), (103, 17, 83), (122, 30, 65)])
def test_design_tolerates_compression_and_color_shifts(color):
    assert title_frame(rgb_frame(color), Intro()) == (True, True)


@pytest.mark.parametrize(
    "color,text", [((113, 21, 79), False), ((0, 0, 0), True), ((255, 0, 0), True)]
)
def test_background_or_white_text_alone_is_not_a_title(color, text):
    layout, white = title_frame(rgb_frame(color, text=text), Intro())
    assert not (layout and white)


def test_variable_timestamps_and_single_frame_glitch(tmp_path):
    card, content = rgb_frame(), rgb_frame((10, 90, 10), text=False)
    entries = [
        (0, card),
        (40, card),
        (120, card),
        (250, content),
        (290, card),
        (450, rgb_frame(text=False)),  # Text fades before the background disappears.
        (700, content),
        (780, content),
        (910, content),
    ]
    frames, times = sequence(tmp_path, entries)
    assert find_intro_end(frames, times, Intro(), 3) == (0.7, "title_removed")


@pytest.mark.parametrize("case", ["short_flash", "later_title", "no_end", "too_little_content"])
def test_uncertain_detection_preserves_original_start(tmp_path, case):
    card, content = rgb_frame(), rgb_frame((10, 90, 10), text=False)
    cases = {
        "short_flash": [(0, card), (80, content), (300, content)],
        "later_title": [(0, content), (400, content), (500, card), (800, content)],
        "no_end": [(0, card), (300, card), (600, card)],
        "too_little_content": [(0, card), (400, card), (800, content), (990, content)],
    }
    frames, times = sequence(tmp_path, cases[case])
    assert find_intro_end(frames, times, Intro(), 1)[0] == 0


@pytest.fixture
def ffmpeg():
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg is not installed")


async def make_video(tmp_path, segments, *, fps=30, timestamp_filter=""):
    source = tmp_path / "source.mp4"
    inputs = []
    for index, (duration, card) in enumerate(segments):
        image = f"color=c={'0x71154F' if card else 'green'}:s=320x180:r={fps}:d={duration}"
        if card:
            image += ",drawbox=x=80:y=75:w=160:h=25:color=white:t=fill"
        inputs.append(f"{image}[v{index}]")
    graph = ";".join(inputs) + ";" + "".join(f"[v{i}]" for i in range(len(inputs)))
    graph += f"concat=n={len(inputs)}:v=1:a=0"
    if timestamp_filter:
        graph += f",{timestamp_filter}"
    await run_process(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            graph,
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:sample_rate=16000:duration={sum(d for d, _ in segments)}",
            "-c:v",
            "libx264",
            "-crf",
            "28",
            "-threads",
            "2",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-fps_mode",
            "passthrough",
            "-enc_time_base",
            "-1",
            "-shortest",
            str(source),
        ],
        timeout=60,
    )
    return source


@pytest.mark.parametrize("length,fps", [(0.8, 24), (1.73, 30), (2.37, 60)])
async def test_real_encoded_video_uses_actual_title_end(tmp_path, ffmpeg, length, fps):
    source = await make_video(tmp_path, [(length, True), (1, False)], fps=fps)
    duration = float((await probe(source))["format"]["duration"])
    result = await detect_intro(
        source, {"start": 0, "end": duration}, tmp_path, Intro(enabled=True)
    )
    assert result["reason"] == "title_removed"
    assert abs(result["start"] - length) <= 1 / fps + 0.001
    assert not list(tmp_path.glob("intro-*"))


async def test_real_variable_frame_timestamps_are_not_rounded_to_nominal_fps(tmp_path, ffmpeg):
    source = await make_video(
        tmp_path,
        [(1.7, True), (1, False)],
        timestamp_filter="settb=1/1000,setpts='PTS+if(gte(T,1.7),13,0)'",
    )
    result = await detect_intro(source, {"start": 0, "end": 2.713}, tmp_path, Intro(enabled=True))
    assert result["start"] == pytest.approx(1.713, abs=0.001)


async def test_real_video_nonzero_chapter_start_and_audio(tmp_path, ffmpeg):
    source = await make_video(tmp_path, [(1, False), (1.7, True), (1, False)])
    chapter = {"start": 0.9, "end": 3.7}
    result = await detect_intro(source, chapter, tmp_path, Intro(enabled=True))
    assert result["start"] == pytest.approx(2.7, abs=0.035)
    assert result["removed_seconds"] == pytest.approx(1.8, abs=0.035)
    audio = tmp_path / "audio.wav"
    await extract_audio(source, audio, result["start"], chapter["end"] - result["start"])
    with wave.open(str(audio)) as stream:
        assert stream.getnframes() / stream.getframerate() == pytest.approx(1, abs=0.035)


@pytest.mark.parametrize(
    "segments,scan",
    [([(2, False)], 6), ([(1, False), (1, True), (1, False)], 6), ([(2, True), (1, False)], 1)],
)
async def test_real_video_without_initial_title_or_confirmed_end_is_untouched(
    tmp_path, ffmpeg, segments, scan
):
    source = await make_video(tmp_path, segments)
    result = await detect_intro(
        source,
        {"start": 0, "end": sum(d for d, _ in segments)},
        tmp_path,
        Intro(enabled=True, max_scan_seconds=scan),
    )
    assert result["start"] == 0 and result["removed_seconds"] == 0


async def test_disabled_detector_does_not_read_source(tmp_path):
    result = await detect_intro(Path("absent.mp4"), {"start": 5, "end": 10}, tmp_path, Intro())
    assert result == {"start": 5, "removed_seconds": 0.0, "reason": "disabled"}


async def test_real_render_starts_with_content_in_both_variants(config, tmp_path, ffmpeg):
    filters = subprocess.run(
        ["ffmpeg", "-hide_banner", "-filters"], capture_output=True, check=True
    ).stdout
    if b"subtitles" not in filters:
        pytest.skip("FFmpeg does not include libass; run the Docker test target")
    source = await make_video(tmp_path, [(1.7, True), (1, False)])
    result = await detect_intro(source, {"start": 0, "end": 2.7}, tmp_path, Intro(enabled=True))
    clip = {"start": result["start"], "end": 2.7}
    write_subtitles([Cue(0.1, 0.8, "Pierwsze słowa")], tmp_path, config.subtitles, config.video)
    for variant in ("crop", "letterbox"):
        output = await render(source, clip, tmp_path, config, variant)
        # Analyze a small central area of the first output frame, before captions.
        raw = tmp_path / f"{variant}.rgb"
        await run_process(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-y",
                "-i",
                str(output),
                "-frames:v",
                "1",
                "-vf",
                "crop=20:20",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                str(raw),
            ],
            timeout=30,
        )
        frame = raw.read_bytes()
        assert sum(frame[1::3]) / 400 > 100
        assert sum(frame[0::3]) / 400 < 20
