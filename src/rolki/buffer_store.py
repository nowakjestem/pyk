from __future__ import annotations

import time
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from .buffer import remote_time
from .config import Config
from .scheduling import choose_time

SCHEMA = """
CREATE TABLE IF NOT EXISTS buffer_plans (
 job_id TEXT NOT NULL REFERENCES jobs(id), chapter_index INTEGER NOT NULL,
 due_at REAL, variant TEXT, accepted_by TEXT, accepted_at REAL,
 base_message TEXT NOT NULL DEFAULT '', notice TEXT NOT NULL DEFAULT '',
 revision INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(job_id,chapter_index)
);
CREATE TABLE IF NOT EXISTS buffer_deliveries (
 job_id TEXT NOT NULL, chapter_index INTEGER NOT NULL, channel_id TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', post_id TEXT, request_json TEXT,
 attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
 detail TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL DEFAULT 0,
 PRIMARY KEY(job_id,chapter_index,channel_id),
 FOREIGN KEY(job_id,chapter_index) REFERENCES buffer_plans(job_id,chapter_index)
);
CREATE INDEX IF NOT EXISTS buffer_pending ON buffer_deliveries(status,retry_at);
"""


class BufferStore:
    def __init__(self, db):
        self.db = db

    def plans(self, *, accepted=None):
        with self.db.connect() as db:
            rows = db.execute(
                "SELECT p.*, j.config_json, j.checkpoint, j.channel_id AS mattermost_channel FROM buffer_plans p JOIN jobs j ON j.id=p.job_id ORDER BY p.job_id,p.chapter_index"
            ).fetchall()
            return [dict(r) for r in rows if accepted is None or bool(r["variant"]) == accepted]

    def plan(self, job_id, index):
        with self.db.connect() as db:
            row = db.execute(
                "SELECT * FROM buffer_plans WHERE job_id=? AND chapter_index=?", (job_id, index)
            ).fetchone()
            return dict(row) if row else None

    @staticmethod
    def occupied(db, settings, external, *, exclude=None):
        remote = {p["id"]: p for p in external}
        known_ids = {
            r[0]
            for r in db.execute("SELECT post_id FROM buffer_deliveries WHERE post_id IS NOT NULL")
        }
        occupied = [
            (p["channelId"], remote_time(p))
            for p in external
            if p.get("id") not in known_ids
            and p.get("status") in ("scheduled", "sending")
            and remote_time(p) is not None
        ]
        for row in db.execute(
            "SELECT p.job_id,p.chapter_index,p.due_at,j.config_json FROM buffer_plans p JOIN jobs j ON j.id=p.job_id WHERE p.due_at IS NOT NULL"
        ):
            if exclude == (row["job_id"], row["chapter_index"]):
                continue
            other = Config.model_validate_json(row["config_json"]).buffer
            if other.organization_id == settings.organization_id:
                deliveries = {
                    d["channel_id"]: d
                    for d in db.execute(
                        "SELECT channel_id,post_id FROM buffer_deliveries WHERE job_id=? AND chapter_index=?",
                        (row["job_id"], row["chapter_index"]),
                    )
                }
                for c in other.channels:
                    delivery = deliveries.get(c.id)
                    post = remote.get(delivery["post_id"]) if delivery else None
                    if post:
                        if (
                            post.get("status") in ("scheduled", "sending")
                            and remote_time(post) is not None
                        ):
                            occupied.append((c.id, remote_time(post)))
                    else:
                        occupied.append((c.id, row["due_at"]))
        return occupied

    def reserve(self, job_id, chapters, settings, external, *, now=None):
        now = now or datetime.now(UTC)
        with self.db.connect(immediate=True) as db:
            occupied = self.occupied(db, settings, external)
            for chapter in chapters:
                index = chapter["index"]
                if db.execute(
                    "SELECT 1 FROM buffer_plans WHERE job_id=? AND chapter_index=?", (job_id, index)
                ).fetchone():
                    continue
                due = choose_time(
                    settings.schedule,
                    now,
                    f"{job_id}:{index}:{now.date()}",
                    [c.id for c in settings.channels],
                    occupied,
                )
                db.execute(
                    "INSERT INTO buffer_plans(job_id,chapter_index,due_at) VALUES(?,?,?)",
                    (job_id, index, due),
                )
                if due:
                    occupied.extend((c.id, due) for c in settings.channels)

    def accept(self, post_id, emoji, user_id, *, now=None):
        if not user_id:
            return False
        with self.db.connect(immediate=True) as db:
            row = db.execute(
                "SELECT o.job_id,o.event_key,j.config_json FROM outbox o JOIN jobs j ON j.id=o.job_id WHERE o.post_id=? AND o.status='sent' AND o.event_key GLOB 'chapter:[0-9]*'",
                (post_id,),
            ).fetchone()
            if not row:
                return False
            settings = Config.model_validate_json(row["config_json"]).buffer
            variant = settings.reactions.get(emoji)
            if not settings.enabled or not variant:
                return False
            index = int(row["event_key"].split(":")[1])
            changed = db.execute(
                "UPDATE buffer_plans SET variant=?,accepted_by=?,accepted_at=? WHERE job_id=? AND chapter_index=? AND variant IS NULL",
                (variant, user_id, now or time.time(), row["job_id"], index),
            ).rowcount
            if not changed:
                return False
            for c in settings.channels:
                db.execute(
                    "INSERT INTO buffer_deliveries(job_id,chapter_index,channel_id) VALUES(?,?,?)",
                    (row["job_id"], index, c.id),
                )
            self._changed(db, row["job_id"], index)
            return True

    def _changed(self, db, job_id, index):
        db.execute(
            "UPDATE buffer_plans SET revision=revision+1 WHERE job_id=? AND chapter_index=?",
            (job_id, index),
        )
        revision = db.execute(
            "SELECT revision FROM buffer_plans WHERE job_id=? AND chapter_index=?", (job_id, index)
        ).fetchone()[0]
        job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        self.db._notify(
            db,
            job_id,
            f"buffer:{index}:{revision}",
            job["channel_id"],
            job["root_id"],
            "",
            update_of=f"chapter:{index}",
        )

    def base_message(self, job_id, index, message):
        with self.db.connect() as db:
            db.execute(
                "UPDATE buffer_plans SET base_message=? WHERE job_id=? AND chapter_index=?",
                (message, job_id, index),
            )

    def message(self, job_id, index):
        plan = self.plan(job_id, index)
        if not plan:
            return ""
        settings = Config.model_validate_json(self.db.get(job_id)["config_json"]).buffer
        if plan["due_at"]:
            due = datetime.fromtimestamp(
                plan["due_at"], ZoneInfo(settings.schedule.timezone)
            ).strftime("%d.%m.%Y %H:%M")
            summary = f"Termin: **{due} ({settings.schedule.timezone})**."
        else:
            summary = "Brak wolnego terminu w ciągu najbliższych 7 dni."
        with self.db.connect() as db:
            deliveries = {
                r["channel_id"]: dict(r)
                for r in db.execute(
                    "SELECT * FROM buffer_deliveries WHERE job_id=? AND chapter_index=?",
                    (job_id, index),
                )
            }
        if plan["variant"]:
            summary += f" Wybrano: **{plan['variant']}**."
            labels = {
                "pending": "oczekuje",
                "sending": "wysyłanie",
                "scheduled": "zaplanowano",
                "unknown": "wynik nieznany — sprawdź Buffer",
                "failed": "błąd",
                "sent": "opublikowano",
                "error": "błąd publikacji",
                "cancelled": "usunięto w Bufferze",
            }
            summary += "\n" + " · ".join(
                f"{c.platform}: {labels.get(deliveries.get(c.id, {}).get('status'), 'oczekuje')}"
                for c in settings.channels
            )
            details = sorted({d["detail"] for d in deliveries.values() if d["detail"]})
            if details:
                summary += "\n" + "\n".join(details)
        else:
            summary += "\nPlatformy: " + ", ".join(c.platform for c in settings.channels)
            summary += (
                "\nDodaj "
                + " lub ".join(
                    f":{emoji}: ({variant})" for emoji, variant in settings.reactions.items()
                )
                + ", aby zaplanować publikację."
            )
        if plan["notice"]:
            summary += "\n" + plan["notice"]
        return plan["base_message"] + "\n\n" + summary

    def move(
        self,
        job_id,
        index,
        settings,
        external,
        *,
        now=None,
        unapproved_only=False,
        notice="Termin skorygowano po późnej akceptacji lub wykryciu kolizji.",
    ):
        now = now or datetime.now(UTC)
        with self.db.connect(immediate=True) as db:
            plan = db.execute(
                "SELECT variant FROM buffer_plans WHERE job_id=? AND chapter_index=?",
                (job_id, index),
            ).fetchone()
            if not plan or (unapproved_only and plan["variant"] is not None):
                return False
            if db.execute(
                "SELECT 1 FROM buffer_deliveries WHERE job_id=? AND chapter_index=? AND status NOT IN ('pending','failed')",
                (job_id, index),
            ).fetchone():
                return False
            due = choose_time(
                settings.schedule,
                now,
                f"late:{job_id}:{index}:{now.date()}",
                [c.id for c in settings.channels],
                self.occupied(db, settings, external, exclude=(job_id, index)),
            )
            db.execute(
                "UPDATE buffer_plans SET due_at=?,notice=? WHERE job_id=? AND chapter_index=?",
                (
                    due,
                    notice if due else "Brak wolnego terminu; ponów po zmianie kalendarza.",
                    job_id,
                    index,
                ),
            )
            self._changed(db, job_id, index)
            return due is not None

    def deliveries(self, job_id, index):
        with self.db.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM buffer_deliveries WHERE job_id=? AND chapter_index=?",
                    (job_id, index),
                )
            ]

    def status(self, job_id, index, channel_id, status, detail="", **values):
        with self.db.connect(immediate=True) as db:
            current = dict(
                db.execute(
                    "SELECT * FROM buffer_deliveries WHERE job_id=? AND chapter_index=? AND channel_id=?",
                    (job_id, index, channel_id),
                ).fetchone()
            )
            values = {
                **current,
                **values,
                "status": status,
                "detail": detail,
                "updated_at": time.time(),
            }
            db.execute(
                "UPDATE buffer_deliveries SET status=:status,detail=:detail,post_id=:post_id,request_json=:request_json,attempts=:attempts,retry_at=:retry_at,updated_at=:updated_at WHERE job_id=:job_id AND chapter_index=:chapter_index AND channel_id=:channel_id",
                values,
            )
            if current["status"] != status or current["detail"] != detail:
                self._changed(db, job_id, index)

    def retry(self, job_id, index, channel_id, *, confirmed_not_created=False):
        with self.db.connect(immediate=True) as db:
            statuses = ("failed", "unknown") if confirmed_not_created else ("failed",)
            current = db.execute(
                "SELECT * FROM buffer_deliveries WHERE job_id=? AND chapter_index=? AND channel_id=?",
                (job_id, index, channel_id),
            ).fetchone()
            if not current or current["status"] not in statuses or current["post_id"]:
                raise ValueError(
                    "Ponowić można wysyłkę bez znanego wpisu w Bufferze; unknown wymaga potwierdzenia nieutworzenia."
                )
            changed = db.execute(
                "UPDATE buffer_deliveries SET status='pending',attempts=0,retry_at=0,detail='',request_json=NULL WHERE job_id=? AND chapter_index=? AND channel_id=?",
                (job_id, index, channel_id),
            ).rowcount
            if not changed:
                raise ValueError(
                    "Ponowić można tylko jednoznacznie nieudaną wysyłkę, bez wpisu w Bufferze."
                )
            self._changed(db, job_id, index)
