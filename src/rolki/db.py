from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .config import Config

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
 id TEXT PRIMARY KEY, post_id TEXT NOT NULL, video_id TEXT NOT NULL,
 url TEXT NOT NULL, channel_id TEXT NOT NULL, root_id TEXT NOT NULL, local_output TEXT,
 status TEXT NOT NULL DEFAULT 'queued', stage TEXT NOT NULL DEFAULT 'queued',
 config_json TEXT NOT NULL, config_revision TEXT NOT NULL,
 checkpoint TEXT NOT NULL DEFAULT '{}', error TEXT,
 created_at REAL NOT NULL, updated_at REAL NOT NULL, retry_at REAL NOT NULL DEFAULT 0,
 UNIQUE(post_id, video_id)
);
CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status, retry_at, created_at);
CREATE TABLE IF NOT EXISTS outbox (
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id), event_key TEXT NOT NULL,
 channel_id TEXT NOT NULL, root_id TEXT NOT NULL, message TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, update_of TEXT,
 retry_at REAL NOT NULL DEFAULT 0, post_id TEXT, created_at REAL NOT NULL,
 UNIQUE(job_id, event_key)
);
CREATE TABLE IF NOT EXISTS cursors (channel_id TEXT PRIMARY KEY, since_ms INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS runtime (name TEXT PRIMARY KEY, heartbeat REAL NOT NULL, detail TEXT NOT NULL);
PRAGMA user_version=3;
"""


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            db.execute("BEGIN IMMEDIATE")  # Bot and worker may migrate together at startup.
            if "local_output" not in {row[1] for row in db.execute("PRAGMA table_info(jobs)")}:
                db.execute("ALTER TABLE jobs ADD COLUMN local_output TEXT")
            if "update_of" not in {row[1] for row in db.execute("PRAGMA table_info(outbox)")}:
                db.execute("ALTER TABLE outbox ADD COLUMN update_of TEXT")

    @contextmanager
    def connect(self, immediate=False):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=30000")
        try:
            if immediate:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _notify(db, job_id, event_key, channel_id, root_id, message, update_of=None):
        if not channel_id:
            return
        db.execute(
            """INSERT OR IGNORE INTO outbox
            (id,job_id,event_key,channel_id,root_id,message,created_at,update_of) VALUES (?,?,?,?,?,?,?,?)""",
            (
                uuid.uuid4().hex,
                job_id,
                event_key,
                channel_id,
                root_id,
                message,
                time.time(),
                update_of,
            ),
        )

    def enqueue(
        self,
        *,
        post_id,
        video_id,
        url,
        config: Config,
        channel_id="",
        root_id="",
        local_output=None,
    ):
        job_id = uuid.uuid4().hex
        with self.connect(immediate=True) as db:
            now = time.time()
            position = (
                1
                + db.execute(
                    """SELECT count(*) FROM jobs WHERE status IN ('queued','running','waiting')
                   AND local_output IS NULL"""
                ).fetchone()[0]
            )
            checkpoint = {"queue_position": position} if channel_id else {}
            inserted = db.execute(
                """INSERT OR IGNORE INTO jobs
                (id,post_id,video_id,url,channel_id,root_id,config_json,config_revision,created_at,updated_at,local_output,checkpoint)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    job_id,
                    post_id,
                    video_id,
                    url,
                    channel_id,
                    root_id,
                    config.model_dump_json(),
                    config.revision,
                    now,
                    now,
                    str(local_output) if local_output else None,
                    json.dumps(checkpoint),
                ),
            ).rowcount
            if not inserted:
                return None
            self._notify(
                db,
                job_id,
                "accepted",
                channel_id,
                root_id,
                acceptance_message(job_id, position),
            )
        return job_id

    def notify(self, job_id: str, event_key: str, message: str, *, update_of: str | None = None):
        with self.connect(immediate=True) as db:
            job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            self._notify(
                db, job_id, event_key, job["channel_id"], job["root_id"], message, update_of
            )

    def notification_for_event(self, job_id: str, event_key: str):
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM outbox WHERE job_id=? AND event_key=?", (job_id, event_key)
            ).fetchone()
            return dict(row) if row else None

    def claim(self, job_id=None):
        with self.connect(immediate=True) as db:
            job = db.execute(
                """SELECT * FROM jobs WHERE status IN ('queued','waiting') AND retry_at<=?
                AND (? IS NULL OR id=?) AND (? IS NOT NULL OR local_output IS NULL)
                ORDER BY created_at LIMIT 1""",
                (time.time(), job_id, job_id, job_id),
            ).fetchone()
            if not job:
                return None
            db.execute(
                "UPDATE jobs SET status='running',updated_at=? WHERE id=?", (time.time(), job["id"])
            )
            return dict(job) | {"status": "running"}

    def recover(self):
        # Called only after acquiring the exclusive worker lock.
        with self.connect(immediate=True) as db:
            db.execute("UPDATE jobs SET status='queued',retry_at=0 WHERE status='running'")

    def checkpoint(self, job_id, stage, data):
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET stage=?,checkpoint=?,updated_at=? WHERE id=?",
                (stage, json.dumps(data, ensure_ascii=False), time.time(), job_id),
            )

    def finish(self, job_id, status, error=None, retry_at=0):
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET status=?,error=?,retry_at=?,updated_at=? WHERE id=?",
                (status, error, retry_at, time.time(), job_id),
            )

    def get(self, job_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise ValueError("Nie znaleziono zadania; podaj pełny identyfikator.")
            return dict(row)

    def list_jobs(self):
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT id,video_id,status,stage,error,created_at,updated_at,config_revision FROM jobs ORDER BY created_at DESC LIMIT 100"
                )
            ]

    def retry(self, job_id):
        with self.connect(immediate=True) as db:
            row = db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None or row["status"] not in ("failed", "waiting"):
                raise ValueError("Ponowić można zadanie failed lub waiting.")
            db.execute(
                "UPDATE jobs SET status='queued',error=NULL,retry_at=0,updated_at=? WHERE id=?",
                (time.time(), job_id),
            )
            db.execute(
                "UPDATE outbox SET status='pending',attempts=0,retry_at=0 WHERE job_id=? AND status='failed'",
                (job_id,),
            )

    def cursor(self, channel_id):
        with self.connect() as db:
            row = db.execute(
                "SELECT since_ms FROM cursors WHERE channel_id=?", (channel_id,)
            ).fetchone()
            return row[0] if row else None

    def set_cursor(self, channel_id, since_ms):
        with self.connect() as db:
            db.execute(
                """INSERT INTO cursors VALUES (?,?) ON CONFLICT(channel_id)
                       DO UPDATE SET since_ms=max(cursors.since_ms,excluded.since_ms)""",
                (channel_id, since_ms),
            )

    def pending_notifications(self):
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM outbox WHERE status='pending' AND retry_at<=? ORDER BY created_at LIMIT 20",
                    (time.time(),),
                )
            ]

    def notification_attempt(self, notification_id):
        with self.connect() as db:
            db.execute("UPDATE outbox SET attempts=attempts+1 WHERE id=?", (notification_id,))

    def notification_sent(self, notification_id, post_id):
        with self.connect() as db:
            db.execute(
                "UPDATE outbox SET status='sent',post_id=? WHERE id=?", (post_id, notification_id)
            )

    def notification_failed(self, notification_id, permanent=False):
        with self.connect() as db:
            row = db.execute(
                "SELECT attempts FROM outbox WHERE id=?", (notification_id,)
            ).fetchone()
            attempts = row[0]
            db.execute(
                "UPDATE outbox SET status=?,retry_at=? WHERE id=?",
                (
                    "failed" if permanent or attempts >= 3 else "pending",
                    time.time() + 2**attempts,
                    notification_id,
                ),
            )

    def retry_notifications(self):
        with self.connect() as db:
            return db.execute(
                "UPDATE outbox SET status='pending',attempts=0,retry_at=0 WHERE status='failed'"
            ).rowcount

    def heartbeat(self, name, detail="ok"):
        with self.connect() as db:
            db.execute(
                """INSERT INTO runtime VALUES (?,?,?) ON CONFLICT(name)
                       DO UPDATE SET heartbeat=excluded.heartbeat,detail=excluded.detail""",
                (name, time.time(), detail),
            )

    def health(self):
        with self.connect() as db:
            runtime = {row["name"]: dict(row) for row in db.execute("SELECT * FROM runtime")}
            failed_notifications = db.execute(
                "SELECT count(*) FROM outbox WHERE status='failed'"
            ).fetchone()[0]
        return {"runtime": runtime, "failed_notifications": failed_notifications}


def acceptance_message(job_id: str, position: int | None = None) -> str:
    place = f" Miejsce w kolejce przy przyjęciu: **{position}**." if position else ""
    return f"Przyjęto film do kolejki.{place} Zadanie `{job_id[:8]}`."
