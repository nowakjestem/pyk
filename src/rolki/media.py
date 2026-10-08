from __future__ import annotations

import json
import math
import struct
import wave
from dataclasses import asdict
from pathlib import Path

from .config import Config, Video
from .errors import PermanentError
from .openai_asr import parse_openai, transcribe_audio
from .process import run_process
from .resources import check_resources
from .subtitles import Cue, parse_whisper


def validate_chapters(info: dict, max_duration: int) -> list[dict]:
    duration = info.get("duration")
    if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming", "post_live"):
        raise PermanentError("Aktywne lub nieprzetworzone transmisje nie są obsługiwane.")
    if not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
        raise PermanentError("Brak prawidłowej długości filmu.")
    if duration > max_duration:
        raise PermanentError(f"Film przekracza limit {max_duration // 60} minut.")
    if info.get("availability") in ("private", "premium_only", "subscriber_only", "needs_auth"):
        raise PermanentError("Film wymaga logowania lub nie jest publicznie dostępny.")
    raw = info.get("chapters")
    if not raw:
        raise PermanentError("Film nie ma rozdziałów. Dodaj je na YouTube i wyślij link ponownie.")
    chapters = []
    previous_end = 0.0
    for index, item in enumerate(raw):
        try:
            start = float(item["start_time"])
            next_start = raw[index + 1]["start_time"] if index + 1 < len(raw) else duration
            end = min(float(item.get("end_time") or next_start), duration)
        except (ValueError, KeyError, TypeError) as exc:
            raise PermanentError("Niepoprawne granice rozdziałów.") from exc
        if (
            not math.isfinite(start)
            or not math.isfinite(end)
            or start < previous_end - 0.01
            or end <= start
        ):
            raise PermanentError("Rozdziały mają niepoprawne lub nakładające się granice.")
        chapters.append(
            {
                "index": index,
                "title": str(item.get("title") or f"Rozdział {index + 1}"),
                "start": start,
                "end": end,
            }
        )
        previous_end = end
    return chapters


async def probe(path: Path) -> dict:
    output = await run_process(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        timeout=30,
    )
    try:
        return json.loads(output)
    except ValueError as exc:
        raise PermanentError("Nie udało się odczytać parametrów filmu.") from exc


def chapters_for_source(
    chapters: list[dict], info: dict, metadata_duration: float
) -> tuple[list[dict], float]:
    """YouTube rounds duration to whole seconds; retain the real final frame/audio."""
    try:
        duration = float(info["format"]["duration"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PermanentError("Brak prawidłowej długości pobranego filmu.") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise PermanentError("Brak prawidłowej długości pobranego filmu.")
    if metadata_duration - duration > 1:
        raise PermanentError("Pobrany film jest krótszy niż metadane YouTube o ponad sekundę.")
    bounded = [{**chapter, "end": min(chapter["end"], duration)} for chapter in chapters]
    if any(chapter["end"] <= chapter["start"] for chapter in bounded):
        raise PermanentError("Rozdział wykracza poza koniec pobranego filmu.")
    return bounded, duration


async def extract_audio(source: Path, destination: Path, start: float, duration: float):
    await run_process(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-threads",
            "2",
            "-ss",
            str(start),
            "-i",
            str(source),
            "-t",
            str(duration),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(destination),
        ],
        timeout=300,
    )


def silent_audio(path: Path) -> bool:
    # Stream PCM; do not retain audio arrays or load a second model for silence detection.
    with wave.open(str(path), "rb") as audio:
        total = count = 0
        while data := audio.readframes(16000):
            samples = struct.unpack(f"<{len(data) // 2}h", data)
            total += sum(value * value for value in samples)
            count += len(samples)
    return count == 0 or math.sqrt(total / count) / 32768 < 0.0005


async def transcribe(source: Path, chapter: dict, root: Path, config: Config) -> list[Cue]:
    if config.asr.provider == "local" and not config.asr.model_path.is_file():
        raise PermanentError("Brak modelu ASR. Uruchom komendę rolki model-download.")
    if config.asr.provider == "openai":
        try:
            config.require_asr()
        except ValueError as exc:
            raise PermanentError(str(exc)) from exc
    cues = []
    duration = chapter["end"] - chapter["start"]
    for offset in range(0, math.ceil(duration), config.asr.max_chunk_seconds):
        chunk_duration = min(config.asr.max_chunk_seconds, duration - offset)
        check_resources(config)
        audio = root / "audio.wav"
        await extract_audio(source, audio, chapter["start"] + offset, chunk_duration)
        try:
            if silent_audio(audio):
                continue
            if config.asr.provider == "openai":
                cached = root / f"openai-{offset:05}.json"
                if cached.is_file():
                    try:
                        document = json.loads(cached.read_text(encoding="utf-8"))
                    except ValueError as exc:
                        raise PermanentError("Niepoprawny zapis transkrypcji OpenAI na dysku.") from exc
                else:
                    document = await transcribe_audio(audio, config.asr)
                chunk_cues = parse_openai(document, offset=offset, duration=chunk_duration)
                if not cached.is_file():
                    temporary = cached.with_suffix(".part.json")
                    temporary.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
                    temporary.replace(cached)
                cues.extend(chunk_cues)
                continue
            prefix = root / "whisper"
            await run_process(
                [
                    config.asr.binary,
                    "-m",
                    str(config.asr.model_path),
                    "-f",
                    str(audio),
                    "-l",
                    config.asr.language,
                    "-t",
                    str(config.asr.threads),
                    "-ng",
                    "-ojf",
                    "-of",
                    str(prefix),
                    "-ml",
                    str(config.subtitles.max_chars_per_line * config.subtitles.max_lines),
                    "-sow",
                    "-sns",
                ],
                timeout=config.asr.timeout_seconds,
            )
            cues.extend(
                parse_whisper(prefix.with_suffix(".json"), offset=offset, duration=chunk_duration)
            )
        finally:
            audio.unlink(missing_ok=True)
            (root / "whisper.json").unlink(missing_ok=True)
    (root / "transcript.json").write_text(
        json.dumps([asdict(cue) for cue in cues], ensure_ascii=False), encoding="utf-8"
    )
    return cues


def video_filter(video: Video, variant: str) -> str:
    normal = "scale=trunc(iw*sar/2)*2:ih,setsar=1"
    if variant == "crop":
        geometry = (
            f"crop=w='floor(min(iw,ih*9/16)/2)*2':h='floor(min(ih,iw*16/9)/2)*2':"
            f"x='(iw-ow)*{video.crop_x}':y='(ih-oh)*{video.crop_y}',"
            f"scale={video.width}:{video.height}"
        )
    elif variant == "letterbox":
        geometry = (
            f"scale={video.width}:{video.height}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
            f"pad={video.width}:{video.height}:(ow-iw)/2:(oh-ih)/2:black"
        )
    else:
        raise ValueError("unknown variant")
    # ASS file names are controlled constants in cwd, so spaces/quotes in root paths are safe.
    return f"{normal},{geometry},setsar=1,fps={video.fps},subtitles=filename={variant}.ass"


async def render(source: Path, chapter: dict, root: Path, config: Config, variant: str) -> Path:
    destination = root / f"{variant}.mp4"
    temporary = root / f"{variant}.part.mp4"
    await run_process(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-threads",
            str(config.video.threads),
            "-filter_threads",
            "1",
            "-ss",
            str(chapter["start"]),
            "-i",
            str(source),
            "-t",
            str(chapter["end"] - chapter["start"]),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-vf",
            video_filter(config.video, variant),
            "-c:v",
            "libx264",
            "-preset",
            config.video.preset,
            "-crf",
            str(config.video.crf),
            "-threads",
            str(config.video.threads),
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            str(temporary),
        ],
        timeout=config.video.timeout_seconds,
        cwd=root,
    )
    await verify_output(temporary, config.video, chapter["end"] - chapter["start"])
    temporary.replace(destination)
    return destination


async def verify_output(path: Path, video: Video, duration: float):
    info = await probe(path)
    visual = next((s for s in info["streams"] if s["codec_type"] == "video"), {})
    audio = next((s for s in info["streams"] if s["codec_type"] == "audio"), {})
    actual = float(info.get("format", {}).get("duration", 0))
    if (
        visual.get("width") != video.width
        or visual.get("height") != video.height
        or visual.get("codec_name") != "h264"
        or visual.get("pix_fmt") != "yuv420p"
        or visual.get("sample_aspect_ratio") != "1:1"
        or audio.get("codec_name") != "aac"
        or abs(actual - duration) > 0.25
    ):
        raise PermanentError("Wyrenderowany plik ma niepoprawne parametry lub długość.")
    # Metadata alone cannot detect a truncated/corrupt MP4.
    await run_process(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-xerror",
            "-threads",
            "2",
            "-i",
            str(path),
            "-f",
            "null",
            "-",
        ],
        timeout=max(120, duration * 2),
    )
