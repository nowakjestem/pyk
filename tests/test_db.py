import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from rolki.db import Database
from rolki.worker import worker_lock


def test_enqueue_atomic_dedup(db, config, enqueue):
    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(lambda _: enqueue(), range(12)))
    assert sum(item is not None for item in ids) == 1
    assert len(db.list_jobs()) == 1
    assert len(db.pending_notifications()) == 1
    restored = Database(config.paths.database)
    assert restored.claim()["video_id"] == "abcdefghijk"
    assert restored.claim() is None


def test_resume_preserves_checkpoints(db, enqueue):
    job_id = enqueue()
    db.claim()
    db.checkpoint(job_id, "uploaded:0:crop", {"results": {"0": {"variants": {"crop": "url"}}}})
    db.recover()
    job = db.claim()
    assert job["stage"] == "uploaded:0:crop"
    assert "crop" in job["checkpoint"]


def test_notification_failure_budget_and_retry(db, enqueue):
    job_id = enqueue()
    notification_id = db.pending_notifications()[0]["id"]
    for _ in range(3):
        db.notification_attempt(notification_id)
        db.notification_failed(notification_id)
    assert db.health()["failed_notifications"] == 1
    assert db.retry_notifications() == 1
    assert db.pending_notifications()[0]["attempts"] == 0
    db.finish(job_id, "failed", "safe error")
    db.retry(job_id)
    assert db.claim()["id"] == job_id


def test_exclusive_worker_lock(db):
    with worker_lock(db), pytest.raises(ValueError, match="worker"):
        with worker_lock(db):
            pass


def test_notification_insert_is_idempotent(db, enqueue):
    job_id = enqueue()
    db.notify(job_id, "complete", "done")
    db.notify(job_id, "complete", "done")
    assert len(db.pending_notifications()) == 2


def test_worker_does_not_take_local_cli_jobs(db, config, enqueue):
    local_id = enqueue(local_output=config.paths.output_dir)
    assert db.claim() is None
    assert db.claim(local_id)["id"] == local_id


def test_queue_position_counts_active_jobs_but_not_finished_or_local(db, config, enqueue):
    first = enqueue(post_id="first")
    db.claim()
    second = enqueue(post_id="second")
    db.finish(second, "waiting", retry_at=9999999999)
    finished = enqueue(post_id="done")
    db.finish(finished, "done")
    failed = enqueue(post_id="failed")
    db.finish(failed, "failed")
    enqueue(post_id="local", local_output=config.paths.output_dir)
    third = enqueue(post_id="third")
    assert json.loads(db.get(first)["checkpoint"])["queue_position"] == 1
    assert json.loads(db.get(second)["checkpoint"])["queue_position"] == 2
    assert json.loads(db.get(third)["checkpoint"])["queue_position"] == 3
    assert "**3**" in db.notification_for_event(third, "accepted")["message"]
    assert enqueue(post_id="third") is None


def test_old_outbox_migrates_and_preserves_pending_notifications(tmp_path):
    path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript("""
        CREATE TABLE outbox (
          id TEXT PRIMARY KEY, job_id TEXT, event_key TEXT, channel_id TEXT, root_id TEXT,
          message TEXT, status TEXT DEFAULT 'pending', attempts INTEGER DEFAULT 0,
          retry_at REAL DEFAULT 0, post_id TEXT, created_at REAL,
          UNIQUE(job_id, event_key)
        );
        INSERT INTO outbox (id, job_id, event_key, message, created_at)
          VALUES ('old', 'job', 'accepted', 'legacy', 0);
        """)
    with ThreadPoolExecutor(max_workers=4) as pool:
        databases = list(pool.map(lambda _: Database(path), range(8)))
    database = databases[0]
    Database(path)  # Migration is safe on a second startup.
    old = database.pending_notifications()[0]
    assert old["message"] == "legacy" and old["update_of"] is None
    assert old["after_event"] is None
