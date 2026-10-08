import shutil
import subprocess
import wave

import pytest

from rolki.config import SubtitleBackground
from rolki.media import extract_audio, render, silent_audio
from rolki.process import run_process
from rolki.subtitles import Cue, Word, write_subtitles


@pytest.fixture
def ffmpeg_available():
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg is not installed")
    filters = subprocess.run(
        ["ffmpeg", "-hide_banner", "-filters"], capture_output=True, check=True
    ).stdout
    if b"subtitles" not in filters:
        pytest.skip("FFmpeg does not include libass; run the Docker test target")


async def test_real_render_captions_geometry_and_chapter_duration(
    config, tmp_path, ffmpeg_available
):
    source = tmp_path / "source.mp4"
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
            "color=c=red:s=640x360:r=30:d=4",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=16000:duration=4",
            "-c:v",
            "libx264",
            "-threads",
            "2",
            "-c:a",
            "aac",
            "-shortest",
            str(source),
        ],
        timeout=60,
    )
    chapter = {"start": 0.7, "end": 2.4}
    write_subtitles([Cue(0.1, 1.6, "Zażółć gęślą jaźń")], tmp_path, config.subtitles, config.video)
    for variant in ("crop", "letterbox"):
        output = await render(source, chapter, tmp_path, config, variant)
        frame = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-ss",
                "0.5",
                "-i",
                str(output),
                "-frames:v",
                "1",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "-",
            ],
            check=True,
            capture_output=True,
        ).stdout
        width, height = config.video.width, config.video.height
        assert len(frame) == width * height * 3

        def pixel(x, y, width=width, frame=frame):
            index = (y * width + x) * 3
            return frame[index : index + 3]

        middle = pixel(width // 2, height // 2)
        assert middle[0] > 200 and middle[1] < 30
        top = pixel(width // 2, 20)
        if variant == "letterbox":
            assert max(top) < 10
        else:
            assert top[0] > 200 and top[1] < 30
        # Caption pixels exist below the picture in the letterbox version.
        assert any(
            min(pixel(x, y)) > 180
            for x in range(40, width - 40, 3)
            for y in range(900, height - 60, 3)
        )
    audio = tmp_path / "chapter.wav"
    await extract_audio(source, audio, chapter["start"], chapter["end"] - chapter["start"])
    with wave.open(str(audio)) as stream:
        assert abs(stream.getnframes() / stream.getframerate() - 1.7) < 0.01
    assert not silent_audio(audio)


def test_silent_chunk(tmp_path):
    audio = tmp_path / "silence.wav"
    with wave.open(str(audio), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b"\x00\x00" * 16000)
    assert silent_audio(audio)


def subtitle_frame(root, variant, time):
    return subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=720x1280:r=30:d=2",
            "-vf",
            f"subtitles=filename={variant}.ass",
            "-ss",
            str(time),
            "-frames:v",
            "1",
            "-threads",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ],
        cwd=root,
        check=True,
        capture_output=True,
    ).stdout


def colored_pixels(frame, *, white=False):
    pixels = set()
    for index in range(0, len(frame), 3):
        red, green, blue = frame[index : index + 3]
        if min(red, green, blue) > 250 if white else green > 180 and red < 40 and blue < 40:
            pixels.add(index // 3)
    return pixels


@pytest.mark.parametrize("variant", ["crop", "letterbox"])
@pytest.mark.parametrize("width", [26, 8])
def test_word_background_moves_without_moving_text(
    config, tmp_path, ffmpeg_available, variant, width
):
    style = config.subtitles.model_copy(
        update={
            "max_chars_per_line": width,
            "background": SubtitleBackground(mode="word", color="#00FF00", opacity=1),
        }
    )
    cue = Cue(0.1, 1.5, "Zażółć gęślą", (Word(0.1, 0.55, "Zażółć"), Word(0.85, 1.5, "gęślą")))
    write_subtitles([cue], tmp_path, style, config.video)
    frames = [subtitle_frame(tmp_path, variant, time) for time in (0.3, 0.7, 1.2)]
    backgrounds = [colored_pixels(frame) for frame in frames]
    assert len(backgrounds[0]) > 300 and len(backgrounds[2]) > 300
    assert not backgrounds[1]  # A genuine gap between spoken words has no highlight.
    if width == 26:
        assert sum(p % 720 for p in backgrounds[0]) / len(backgrounds[0]) < 360
        assert sum(p % 720 for p in backgrounds[2]) / len(backgrounds[2]) > 360
    else:
        assert max(p // 720 for p in backgrounds[0]) < max(p // 720 for p in backgrounds[2])
    foregrounds = [colored_pixels(frame, white=True) for frame in frames]
    assert len(set.intersection(*foregrounds)) / len(set.union(*foregrounds)) > 0.95


@pytest.mark.parametrize("variant", ["crop", "letterbox"])
def test_line_background_is_visible_behind_both_lines(config, tmp_path, ffmpeg_available, variant):
    style = config.subtitles.model_copy(
        update={
            "max_chars_per_line": 8,
            "background": SubtitleBackground(mode="line", color="#00FF00", opacity=1, padding=8),
        }
    )
    write_subtitles([Cue(0, 1.5, "Zażółć gęślą")], tmp_path, style, config.video)
    frame = subtitle_frame(tmp_path, variant, 0.5)
    background = colored_pixels(frame)
    foreground = colored_pixels(frame, white=True)
    assert len(background) > 1000 and len(foreground) > 100
    assert min(p // 720 for p in background) < min(p // 720 for p in foreground)
    assert max(p // 720 for p in background) > max(p // 720 for p in foreground)
