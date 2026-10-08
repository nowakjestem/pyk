from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import signal
import sys
import time
import uuid
from pathlib import Path

import aiohttp
from pydantic import ValidationError

from .config import Config, load_config
from .db import Database
from .errors import JobError, TransientError
from .pipeline import Pipeline
from .process import retry_network, run_process
from .storage import policies
from .urls import youtube_links
from .worker import execute, run_worker, worker_lock


async def download_model(config: Config):
    target = config.asr.model_path
    if target.is_file():
        print(f"Model jest dostępny: {target}")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    source = target.with_suffix(".download")
    quantized = target.with_suffix(".quantized")

    async def fetch():
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=1800)) as session:
                async with session.get(config.asr.model_url) as response:
                    if response.status >= 400:
                        raise TransientError(
                            f"Pobranie modelu nie powiodło się (HTTP {response.status})."
                        )
                    with source.open("wb") as output:
                        async for chunk in response.content.iter_chunked(1024**2):
                            output.write(chunk)
                            if shutil.disk_usage(target.parent).free < 512 * 1024**2:
                                raise JobError("Za mało miejsca na pobranie modelu.")
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise TransientError("Błąd połączenia podczas pobierania modelu.") from exc

    try:
        await retry_network(fetch)
        if source.stat().st_size < 1024**2:
            raise JobError("Pobrany model jest niepoprawny.")
        await run_process(
            [config.asr.quantizer, str(source), str(quantized), config.asr.quantization],
            timeout=300,
        )
        quantized.replace(target)
        print(f"Model przygotowany: {target}")
    finally:
        source.unlink(missing_ok=True)
        quantized.unlink(missing_ok=True)


async def stoppable(coroutine, grace):
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    service = asyncio.create_task(coroutine)
    signal_task = asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait([service, signal_task], return_when=asyncio.FIRST_COMPLETED)
        if service in done:
            return await service
        service.cancel()
        try:
            await asyncio.wait_for(service, grace)
        except (asyncio.CancelledError, TimeoutError):
            pass
    finally:
        signal_task.cancel()
        await asyncio.gather(signal_task, return_exceptions=True)


def parser():
    command = argparse.ArgumentParser(prog="rolki")
    command.add_argument("--config", type=Path, default=Path("config.yaml"))
    commands = command.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="Walidacja konfiguracji i zależności")
    check.add_argument("--integrations", action="store_true")
    check.add_argument("--tools", action="store_true")
    commands.add_parser("bot")
    commands.add_parser("worker")
    commands.add_parser("queue")
    job = commands.add_parser("job")
    job.add_argument("id")
    retry = commands.add_parser("retry")
    retry.add_argument("id")
    resume = commands.add_parser("resume", help="Wznowienie zadania CLI, także lokalnego")
    resume.add_argument("id")
    commands.add_parser("retry-notifications")
    health = commands.add_parser("health")
    health.add_argument("--worker", action="store_true")
    commands.add_parser("model-download")
    commands.add_parser("s3-policies", help="Generuje JSON; nie zmienia bucketa")
    run = commands.add_parser("run", help="Pipeline z CLI, bez Mattermosta")
    run.add_argument("url")
    run.add_argument("--local-output", action="store_true")
    return command


async def async_main(args, config):
    if args.command == "check":
        if args.integrations:
            config.require_integrations()
        if args.tools:
            for binary in ("ffmpeg", "ffprobe", "deno", config.asr.binary, config.asr.quantizer):
                if not shutil.which(binary):
                    raise JobError(f"Brak programu: {binary}.")
            filters = await run_process(["ffmpeg", "-hide_banner", "-filters"], timeout=30)
            if "subtitles" not in filters:
                raise JobError("FFmpeg nie ma filtra subtitles/libass.")
            if not shutil.which("fc-list"):
                raise JobError("Brak programu fc-list (pakiet fontconfig).")
            families = await run_process(["fc-list", "-f", "%{family}\n"], timeout=30)
            installed = {
                name.strip().casefold()
                for line in families.splitlines()
                for name in line.split(",")
            }
            if config.subtitles.font.casefold() not in installed:
                raise JobError(f"Font {config.subtitles.font!r} nie jest zainstalowany.")
            if not config.asr.model_path.is_file():
                raise JobError("Brak modelu ASR. Uruchom rolki model-download.")
        print(f"Konfiguracja poprawna. Wersja: {config.revision}")
        return 0
    if args.command == "model-download":
        await download_model(config)
        return 0
    if args.command == "s3-policies":
        config.require_storage()
        print(json.dumps(policies(config.s3), ensure_ascii=False, indent=2))
        return 0
    db = Database(config.paths.database)
    if args.command == "queue":
        print(json.dumps(db.list_jobs(), ensure_ascii=False, indent=2))
    elif args.command == "job":
        job = db.get(args.id)
        job["checkpoint"] = json.loads(job["checkpoint"])
        job.pop("config_json")
        print(json.dumps(job, ensure_ascii=False, indent=2))
    elif args.command == "retry":
        db.retry(args.id)
        print("Zadanie dodane ponownie do kolejki.")
    elif args.command == "retry-notifications":
        print(f"Ponowiono powiadomienia: {db.retry_notifications()}")
    elif args.command == "health":
        state = db.health()
        if args.worker:
            worker = state["runtime"].get("worker", {})
            return (
                0
                if time.time() - worker.get("heartbeat", 0) < 30 and worker.get("detail") == "ok"
                else 1
            )
        print(json.dumps(state, indent=2))
        return 0 if state["failed_notifications"] == 0 else 1
    elif args.command == "run":
        links = youtube_links(args.url)
        if len(links) != 1:
            raise ValueError("Podaj pojedynczy link do filmu YouTube.")
        if not args.local_output:
            config.require_storage()
        with worker_lock(db):
            video_id, url = links[0]
            job_id = db.enqueue(
                post_id="cli:" + uuid.uuid4().hex,
                video_id=video_id,
                url=url,
                config=config,
                local_output=config.paths.output_dir if args.local_output else None,
            )
            job = db.claim(job_id)
            await execute(
                db,
                Pipeline(db, local_output=config.paths.output_dir if args.local_output else None),
                job,
            )
            print(
                json.dumps(
                    {
                        "id": job_id,
                        "status": db.get(job_id)["status"],
                        "error": db.get(job_id)["error"],
                        "results": json.loads(db.get(job_id)["checkpoint"]).get("results", {}),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0 if db.get(job_id)["status"] == "done" else 1
    elif args.command == "resume":
        with worker_lock(db):
            job = db.get(args.id)
            if job["status"] in ("failed", "waiting"):
                db.retry(args.id)
            elif job["status"] != "queued":
                raise ValueError("Wznowić można zadanie queued, waiting lub failed.")
            job = db.claim(args.id)
            output = Path(job["local_output"]) if job["local_output"] else None
            await execute(db, Pipeline(db, local_output=output), job)
            print(json.dumps({"id": args.id, "status": db.get(args.id)["status"]}, indent=2))
            return 0 if db.get(args.id)["status"] == "done" else 1
    elif args.command == "worker":
        await stoppable(run_worker(config), config.worker.shutdown_grace_seconds)
    elif args.command == "bot":
        from .mattermost import run_bot

        await stoppable(run_bot(config), config.worker.shutdown_grace_seconds)
    return 0


def main():
    args = parser().parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # Libraries may log URLs/headers; app logs use only controlled values.
    for name in ("aiohttp", "botocore", "boto3", "urllib3", "s3transfer"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    try:
        config = load_config(args.config)
        return asyncio.run(async_main(args, config))
    except ValidationError as exc:
        # Pydantic's default repr includes input values; print field names/messages only.
        for error in exc.errors(include_input=False, include_url=False):
            print(
                f"Konfiguracja: {'.'.join(map(str, error['loc']))}: {error['msg']}", file=sys.stderr
            )
        return 2
    except (JobError, ValueError, FileNotFoundError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        print(
            f"Błąd uruchomienia ({type(exc).__name__}); sprawdź konfigurację i dostęp usług.",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
