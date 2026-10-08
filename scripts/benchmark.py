"""Run inside the Docker test image. Optional --source uses a real Polish sample.

docker run --rm --memory=768m --memory-swap=768m --cpus=2 ... scripts/benchmark.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import resource
import shutil
import sys
import time
from pathlib import Path

from rolki.config import load_config
from rolki.media import probe, render, transcribe
from rolki.process import run_process
from rolki.subtitles import write_subtitles


async def benchmark(args):
    config = load_config(args.config)
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=True)
    config = config.model_copy(
        update={
            "limits": config.limits.model_copy(
                update={
                    "min_available_memory_bytes": 0,
                    "min_container_headroom_bytes": 0,
                    "min_disk_free_bytes": 0,
                }
            )
        }
    )
    if args.source:
        source = args.source.resolve()
    else:
        speech = root / "speech.wav"
        await run_process(
            [
                "espeak-ng",
                "-v",
                "pl",
                "-s",
                "135",
                "-w",
                str(speech),
                "To jest polska próbka testowa. Tworzymy dwa pionowe filmy z napisami. Zażółć gęślą jaźń.",
            ],
            timeout=30,
        )
        source = root / "source.mp4"
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
                "testsrc2=size=640x360:rate=30",
                "-i",
                str(speech),
                "-c:v",
                "libx264",
                "-threads",
                "2",
                "-c:a",
                "aac",
                "-shortest",
                str(source),
            ],
            timeout=120,
        )
    info = await probe(source)
    duration = min(float(info["format"]["duration"]), 120)
    chapter = {"start": 0.0, "end": duration}
    start = time.monotonic()
    cues = await transcribe(source, chapter, root, config)
    asr_time = time.monotonic() - start
    if not cues:
        raise RuntimeError("Speech sample produced an empty transcript")
    write_subtitles(cues, root, config.subtitles, config.video)
    timings = {}
    for variant in ("crop", "letterbox"):
        start = time.monotonic()
        await render(source, chapter, root, config, variant)
        timings[variant] = round(time.monotonic() - start, 2)
    maxrss = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    maxrss_bytes = maxrss if sys.platform == "darwin" else maxrss * 1024
    report = {
        "sample": "user-provided" if args.source else "synthetic Polish espeak-ng",
        "duration_seconds": duration,
        "asr_seconds": round(asr_time, 2),
        "render_seconds": timings,
        "max_child_rss_mib": round(maxrss_bytes / 1024**2, 2),
        "transcript": " ".join(c.text for c in cues),
        "timed_words": sum(len(c.words) for c in cues),
        "font": config.subtitles.font,
        "background_mode": config.subtitles.background.mode,
    }
    (root / "benchmark.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    shutil.copyfile(root / "crop.mp4", root / "preview-crop.mp4")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--source", type=Path)
    parser.add_argument("--output", type=Path, default=Path("data/benchmark"))
    asyncio.run(benchmark(parser.parse_args()))
