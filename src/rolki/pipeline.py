from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sys
from dataclasses import asdict
from pathlib import Path

from .config import Config
from .db import Database
from .errors import PermanentError, ResourceWait, TransientError
from .media import chapters_for_source, probe, render, transcribe, validate_chapters
from .process import retry_network, run_process
from .resources import check_resources
from .storage import LocalStorage, S3Storage
from .subtitles import cue_from_dict, safe_markdown, write_subtitles

log = logging.getLogger(__name__)


class Pipeline:
    def __init__(self, db: Database, *, local_output: Path | None = None):
        self.db = db
        self.local_output = local_output

    async def run(self, job: dict):
        config = Config.model_validate_json(job["config_json"])
        state = json.loads(job["checkpoint"])
        root = config.paths.work_dir / job["id"]
        root.mkdir(parents=True, exist_ok=True)
        storage = LocalStorage(self.local_output) if self.local_output else S3Storage(config.s3)
        if not self.local_output:
            config.require_storage()

        def save(stage):
            self.db.checkpoint(job["id"], stage, state)
            log.info("job=%s stage=%s", job["id"], stage)

        async def youtube(operation):
            check_resources(config)
            save(operation)
            args = [
                sys.executable,
                "-m",
                "rolki.youtube",
                operation,
                job["url"],
                str(root),
                "--height",
                str(config.video.source_max_height),
                "--work-root",
                str(config.paths.work_dir),
                "--max-bytes",
                str(config.limits.max_work_bytes),
                "--min-free",
                str(config.limits.min_disk_free_bytes),
            ]
            await retry_network(
                lambda: run_process(
                    args,
                    timeout=config.limits.download_timeout_seconds,
                    error_types={65: PermanentError, 70: TransientError, 75: ResourceWait},
                )
            )
            return json.loads((root / f"{operation}.json").read_text())

        if "chapters" not in state:
            metadata = await youtube("metadata")
            state["chapters"] = validate_chapters(metadata, config.limits.max_video_seconds)
            state["title"] = str(metadata.get("title") or job["video_id"])
            state["duration"] = metadata["duration"]
            state["results"] = {}
            save("metadata_done")
            self.db.notify(
                job["id"],
                "metadata",
                f"Film: {safe_markdown(state['title'])}. Rozdziałów: {len(state['chapters'])}. Rozpoczynam przetwarzanie.",
            )

        pending = any(
            len(state["results"].get(str(c["index"]), {}).get("variants", {})) < 2
            for c in state["chapters"]
        )
        source = root / state.get("source", "source.mkv")
        source_info = None
        if pending and not source.is_file():
            downloaded = await youtube("download")
            source = root / downloaded["source"]
            source_info = await probe(source)
            if not any(s["codec_type"] == "audio" for s in source_info["streams"]):
                raise PermanentError("Film nie zawiera ścieżki audio.")
            state["source"] = source.name
            save("download_done")

        if pending and "source_duration" not in state:
            source_info = source_info or await probe(source)
            state["chapters"], state["source_duration"] = chapters_for_source(
                state["chapters"], source_info, state["duration"]
            )
            save("source_verified")

        for chapter in state["chapters"]:
            index = str(chapter["index"])
            result = state["results"].setdefault(index, {"variants": {}})
            chapter_root = root / f"chapter-{chapter['index']:03}"
            if len(result["variants"]) < 2:
                chapter_root.mkdir(parents=True, exist_ok=True)
                if "cues" not in result:
                    save(f"transcribing:{index}")
                    cues = await transcribe(source, chapter, chapter_root, config)
                    result["cues"] = [asdict(cue) for cue in cues]
                    save(f"transcribed:{index}")
                cues = [cue_from_dict(cue) for cue in result["cues"]]
                write_subtitles(cues, chapter_root, config.subtitles, config.video)
                for variant in ("crop", "letterbox"):
                    if variant in result["variants"]:
                        continue
                    check_resources(config)
                    save(f"rendering:{index}:{variant}")
                    output = chapter_root / f"{variant}.mp4"
                    if not output.is_file():
                        output = await render(source, chapter, chapter_root, config, variant)
                    save(f"uploading:{index}:{variant}")
                    key = f"{config.s3.prefix}/{job['id']}/{chapter['index']:03}/{variant}.mp4"
                    url = await retry_network(
                        lambda output=output, key=key: storage.upload(output, key)
                    )
                    result["variants"][variant] = {"key": key, "url": url}
                    save(f"uploaded:{index}:{variant}")
                    output.unlink(missing_ok=True)

            variants = result["variants"]
            title = safe_markdown(chapter["title"])
            self.db.notify(
                job["id"],
                f"chapter:{index}",
                f"**{chapter['index'] + 1}. {title}**\n\n"
                f"[9:16 — wycięty kadr]({variants['crop']['url']}) · "
                f"[9:16 — pełny obraz z pasami]({variants['letterbox']['url']})\n\n"
                f"Pliki są przechowywane przez {config.s3.retention_days} dni.",
            )
            if chapter_root.exists():
                shutil.rmtree(chapter_root)

        save("complete")
        self.db.notify(
            job["id"],
            "complete",
            f"Zadanie `{job['id'][:8]}` zakończone. Gotowych filmów: {len(state['chapters']) * 2}.",
        )
        shutil.rmtree(root)


async def monitored_run(pipeline: Pipeline, job: dict):
    """Check disk while tools run, not just between stages."""
    config = Config.model_validate_json(job["config_json"])

    async def guard():
        while True:
            await asyncio.sleep(2)
            check_resources(config, memory=False)

    task = asyncio.create_task(pipeline.run(job))
    monitor = asyncio.create_task(guard())
    try:
        done, _ = await asyncio.wait([task, monitor], return_when=asyncio.FIRST_COMPLETED)
        if monitor in done:
            await monitor
        return await task
    finally:
        for item in (task, monitor):
            item.cancel()
        await asyncio.gather(task, monitor, return_exceptions=True)
