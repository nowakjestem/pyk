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
