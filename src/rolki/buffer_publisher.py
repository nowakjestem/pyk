from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import time
from urllib.parse import urlsplit

import aiohttp

from .buffer import (
    AmbiguousResult,
    BufferClient,
    QueueFull,
    matches,
    post_input,
    remote_time,
)
from .buffer_store import BufferStore
from .config import Config
from .errors import PermanentError, TransientError
from .segments import clips

log = logging.getLogger(__name__)


async def reserve_plan(db, job, chapters, config):
    if not config.buffer.enabled or job.get("local_output") or not job.get("channel_id"):
        return
    store = BufferStore(db)
    if all(store.plan(job["id"], c["index"]) for c in chapters):
        return
    store.reserve(job["id"], chapters, config.buffer, [])


class BufferPublisher:
    def __init__(self, db, client):
        self.db, self.client, self.store = db, client, BufferStore(db)

    def delivery_status(self, plan, delivery, status, detail="", **values):
        return self.store.status(
            plan["job_id"],
            plan["chapter_index"],
            delivery["channel_id"],
            status,
            detail,
            part_index=delivery["part_index"],
            **values,
        )

    async def check_media(self, url):
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.username or parts.password:
            raise PermanentError("Film dla Buffera wymaga publicznego HTTPS URL.")
        try:
            async with self.client.session.head(url, allow_redirects=False) as response:
                if response.status != 200:
                    raise PermanentError(
                        "Film S3 nie jest dostępny publicznie pod bezpośrednim URL."
                    )
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise TransientError("Nie udało się sprawdzić filmu S3.") from exc

    def record_post(self, plan, delivery, request, post):
        if (
            not post.get("id")
            or (delivery.get("post_id") and delivery["post_id"] != post.get("id"))
            or not matches(post, request)
            or post.get("schedulingType") != "automatic"
            or (
                request.get("mode") != "shareNow"
                and post.get("status") in ("scheduled", "sending")
                and remote_time(post) is None
            )
        ):
            self.delivery_status(
                plan,
                delivery,
                "unknown",
                "Buffer zwrócił inne dane niż żądany film, termin lub tryb publikacji.",
                post_id=delivery.get("post_id") or post.get("id"),
            )
            return
        status = post.get("status")
        if status in ("scheduled", "sent", "error", "sending"):
            # Remote sending is acknowledged, local sending means ambiguous in-flight create.
            local_status = "scheduled" if status == "sending" else status
            due = remote_time(post)
            result = (
                json.loads(plan["checkpoint"])
                .get("results", {})
                .get(str(plan["chapter_index"]), {})
            )
            expires = (
                clips(result)[delivery["part_index"]]
                .get("variants", {})
                .get(plan["variant"], {})
                .get("expires_at")
            )
            settings = Config.model_validate_json(plan["config_json"]).buffer
            detail = ""
            if (
                request.get("mode") != "shareNow"
                and status in ("scheduled", "sending")
                and (not expires or due + settings.retention_margin_hours * 3600 >= expires)
            ):
                detail = "Termin w Bufferze przekracza retencję filmu. Przyspiesz publikację lub przedłuż dostępność pliku w S3."
            self.delivery_status(
                plan,
                delivery,
                local_status,
                detail,
                post_id=post["id"],
                due_at=due,
                retry_at=0,
            )
        else:
            self.delivery_status(
                plan,
                delivery,
                "unknown",
                "Buffer nie potwierdził automatycznego zaplanowania publikacji.",
                post_id=post["id"],
            )

    async def process(self, plan, config, external, now):
        job_id, index = plan["job_id"], plan["chapter_index"]
        deliveries = self.store.deliveries(job_id, index)
        state = json.loads(plan["checkpoint"])
        result = state.get("results", {}).get(str(index), {})
        chapter = next((c for c in state.get("chapters", []) if c["index"] == index), None)
        pending = [
            d
            for d in self.store.deliveries(job_id, index)
            if d["status"] in ("pending", "waiting_capacity") and d["retry_at"] <= now
        ]
        if (
            not pending
            or not chapter
            or any(plan["variant"] not in clip.get("variants", {}) for clip in clips(result))
        ):
            return
        if config.descriptions.enabled and "description" not in result:
            return
        # A persisted custom request fixes the mode for the rest of this chapter.
        immediate = bool(plan["immediate"])
        custom = not immediate and (
            config.buffer.scheduling_mode == "customScheduled"
            or any(
                d["request_json"] and json.loads(d["request_json"]).get("mode") == "customScheduled"
                for d in deliveries
            )
        )
        due = None
        part_times = {}
        blocked = set()
        text = (
            result.get("description")
            if any(c.get("text", "").strip() for c in result.get("cues", []))
            else chapter["title"]
        )
        text = text or chapter["title"]
        for d in pending:
            if immediate and any(
                row["part_index"] < d["part_index"] and row["status"] not in ("scheduled", "sent")
                for row in self.store.deliveries(job_id, index)
            ):
                continue
            if custom:
                part_index = d["part_index"]
                rows = self.store.deliveries(job_id, index)
                if part_index in blocked or any(
                    row["part_index"] < part_index and row["status"] not in ("scheduled", "sent")
                    for row in rows
                ):
                    continue
                if part_index not in part_times:
                    group = [row for row in rows if row["part_index"] == part_index]
                    # A sleeping retry, failed or ambiguous account holds this part together.
                    if any(
                        row["status"] not in ("pending", "waiting_capacity", "scheduled", "sent")
                        or (
                            row["status"] in ("pending", "waiting_capacity")
                            and row["retry_at"] > now
                        )
                        for row in group
                    ):
                        blocked.add(part_index)
                        continue
                    unsent = [
                        row for row in group if row["status"] in ("pending", "waiting_capacity")
                    ]
                    try:
                        part_times[part_index] = self.store.schedule_part(
                            job_id, index, part_index, config.buffer, external, now=now
                        )
                    except PermanentError as exc:
                        for row in unsent:
                            self.delivery_status(plan, row, "failed", str(exc))
                        blocked.add(part_index)
                        continue
                due = part_times[part_index]
            if any(
                row["channel_id"] == d["channel_id"]
                and row["part_index"] < d["part_index"]
                and row["status"] not in ("scheduled", "sent")
                for row in self.store.deliveries(job_id, index)
            ):
                continue
            clip = clips(result)[d["part_index"]]
            media = clip["variants"][plan["variant"]]
            title = chapter["title"] + (f" — part {clip['number']}" if "number" in clip else "")
            caption = text + (
                f"\n\npart {clip['number']} / {len(clips(result))}" if "number" in clip else ""
            )
            duration = (
                clip["end"] - clip["start"]
                if "number" in clip
                else chapter["end"] - result.get("intro", {}).get("start", chapter["start"])
            )
            target = next(c for c in config.buffer.channels if c.id == d["channel_id"])
            try:
                if duration > target.max_video_seconds:
                    raise PermanentError(
                        f"Film przekracza skonfigurowany limit {target.max_video_seconds} s dla {target.platform}."
                    )
                if len(caption) > target.max_text_chars:
                    raise PermanentError(
                        f"Opis przekracza limit {target.max_text_chars} znaków dla {target.platform}."
                    )
                expires = media.get("expires_at")
                margin = config.buffer.retention_margin_hours * 3600
                if not expires or expires <= now + margin or (custom and due + margin >= expires):
                    raise PermanentError(
                        "Retencja S3 nie zapewnia dostępności filmu do terminu publikacji z zapasem."
                    )
                await self.check_media(media["url"])
                request = post_input(
                    target,
                    url=media["url"],
                    text=caption,
                    title=title,
                    due_at=due,
                    immediate=immediate,
                )
                # Persist before network I/O. A crash here conservatively leaves an unknown result.
                if not self.delivery_status(
                    plan,
                    d,
                    "sending",
                    request_json=json.dumps(request),
                    attempts=d["attempts"] + 1,
                    expected_immediate=immediate,
                ):
                    return  # A reaction switched this plan to Share Now during media preflight.
                post = (
                    await self.client.publish_now(d["post_id"])
                    if immediate and d["post_id"]
                    else await self.client.create(request)
                )
                self.record_post(plan, d, request, post)
                external[:] = [p for p in external if p["id"] != post.get("id")]
                external.append(post)
            except AmbiguousResult as exc:
                self.delivery_status(plan, d, "unknown", str(exc))
            except QueueFull as exc:
                self.delivery_status(
                    plan,
                    d,
                    "waiting_capacity",
                    str(exc),
                    attempts=d["attempts"],
                    request_json=json.dumps(request) if immediate and d["post_id"] else None,
                    retry_at=now
                    + (
                        config.buffer.poll_seconds
                        if immediate
                        else config.buffer.status_poll_seconds
                    ),
                )
                if custom:
                    # A concurrent write in Buffer can fill a queue after preflight.
                    blocked.add(d["part_index"])
                    for row in self.store.deliveries(job_id, index):
                        if row["part_index"] == d["part_index"] and row["status"] == "pending":
                            self.delivery_status(
                                plan,
                                row,
                                "waiting_capacity",
                                "Klip czeka na miejsce dla wszystkich kont; zachowuje wspólny termin.",
                                retry_at=now + config.buffer.status_poll_seconds,
                            )
            except TransientError as exc:
                attempts = d["attempts"] + 1
                self.delivery_status(
                    plan,
                    d,
                    "failed" if attempts >= 3 else "pending",
                    str(exc),
                    attempts=attempts,
                    retry_at=now + max(exc.retry_after, 2**attempts * 30),
                )
            except PermanentError as exc:
                self.delivery_status(plan, d, "failed", str(exc))
            except Exception as exc:
                # Protect Mattermost's task group and never expose raw upstream data.
                log.warning("Buffer delivery failure_type=%s", type(exc).__name__)
                current = next(
                    row
                    for row in self.store.deliveries(job_id, index)
                    if row["channel_id"] == d["channel_id"] and row["part_index"] == d["part_index"]
                )
                self.delivery_status(
                    plan,
                    d,
                    "unknown" if current["status"] == "sending" else "failed",
                    "Nieoczekiwany błąd integracji. Sprawdź kolejkę Buffera przed ponowieniem.",
                )

    async def tick(self, *, now=None):
        now = time.time() if now is None else now
        for plan in sorted(
            self.store.plans(accepted=True),
            key=lambda p: (not p["immediate"], p["accepted_at"], p["job_id"], p["chapter_index"]),
        ):
            config = Config.model_validate_json(plan["config_json"])
            deliveries = self.store.deliveries(plan["job_id"], plan["chapter_index"])
            for delivery in deliveries:
                if delivery["status"] == "sending":
                    self.delivery_status(
                        plan,
                        delivery,
                        "unknown",
                        "Wysyłka została przerwana. Sprawdź Buffer przed ręcznym ponowieniem.",
                    )
            actionable = any(
                delivery["status"] in ("pending", "waiting_capacity")
                and delivery["retry_at"] <= now
                for delivery in deliveries
            )
            if actionable:
                await self.process(plan, config, [], now)


async def run_publisher(config, db):
    config.require_buffer()
    with db.path.with_suffix(".buffer.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Inny proces obsługuje już kolejkę Buffera.") from exc
        try:
            async with aiohttp.ClientSession(
                trust_env=True, timeout=aiohttp.ClientTimeout(total=60, connect=15)
            ) as session:
                publisher = BufferPublisher(db, BufferClient(session))
                publisher.store.activate_queue()
                while True:
                    try:
                        await publisher.tick()
                    except Exception as exc:
                        log.warning("Buffer queue failure_type=%s", type(exc).__name__)
                    await asyncio.sleep(config.buffer.poll_seconds)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
