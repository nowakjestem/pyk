from __future__ import annotations

import asyncio
import fcntl
import logging
import shutil
import time
from contextlib import contextmanager

from .config import Config
from .db import Database
from .errors import JobError, ResourceWait
from .pipeline import Pipeline, monitored_run

log = logging.getLogger(__name__)


@contextmanager
def worker_lock(db: Database):
    with db.path.with_suffix(".worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Inny worker już korzysta z tej kolejki.") from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def cleanup(db: Database):
    with db.connect() as connection:
        jobs = [
            dict(row)
            for row in connection.execute("SELECT * FROM jobs WHERE status IN ('done','failed')")
        ]
    for job in jobs:
        config = Config.model_validate_json(job["config_json"])
        if (
            job["status"] == "failed"
            and time.time() - job["updated_at"] < config.limits.failed_retention_hours * 3600
        ):
            continue
        root = config.paths.work_dir / job["id"]
        if root.exists() and not root.is_symlink():
            shutil.rmtree(root)


async def execute(db: Database, pipeline: Pipeline, job: dict):
    started = time.monotonic()
    try:
        await monitored_run(pipeline, job)
        db.finish(job["id"], "done")
    except ResourceWait as exc:
        config = Config.model_validate_json(job["config_json"])
        db.finish(
            job["id"], "waiting", str(exc), time.time() + config.limits.resource_retry_seconds
        )
        db.notify(job["id"], "resource_wait", str(exc))
    except asyncio.CancelledError:
        db.finish(job["id"], "queued")
        raise
    except Exception as exc:
        # Only our error classes are safe to publish; exception messages may contain secrets.
        message = (
            str(exc)
            if isinstance(exc, JobError)
            else "Nieoczekiwany błąd przetwarzania. Sprawdź logi i konfigurację."
        )
        db.finish(job["id"], "failed", message)
        db.notify(
            job["id"],
            f"failed:{time.time_ns()}",
            f"Zadanie `{job['id'][:8]}` nie powiodło się: {message} Ukończone klipy pozostają dostępne.",
        )
        log.error("job=%s failure_type=%s", job["id"], type(exc).__name__)
    finally:
        log.info(
            "job=%s elapsed_seconds=%.1f status=%s",
            job["id"],
            time.monotonic() - started,
            db.get(job["id"])["status"],
        )


async def run_worker(config: Config):
    config.require_storage()
    db = Database(config.paths.database)
    with worker_lock(db):
        db.recover()
        pipeline = Pipeline(db)

        async def heartbeats():
            while True:
                db.heartbeat("worker")
                await asyncio.sleep(5)

        heartbeat = asyncio.create_task(heartbeats())
        try:
            while True:
                cleanup(db)
                job = db.claim()
                if job:
                    await execute(db, pipeline, job)
                else:
                    await asyncio.sleep(config.worker.poll_seconds)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            db.heartbeat("worker", "stopped")
