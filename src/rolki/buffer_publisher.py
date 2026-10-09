from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import time
from datetime import UTC, datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import aiohttp

from .buffer import (
    AmbiguousResult,
    BufferClient,
    QueueFull,
    matches,
    post_input,
    queue_fits_retention,
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
    if config.buffer.scheduling_mode == "addToQueue":
        store.reserve(job["id"], chapters, config.buffer, [])
        return
    config.require_buffer()
    async with aiohttp.ClientSession(
        trust_env=True, timeout=aiohttp.ClientTimeout(total=60)
    ) as session:
        client = BufferClient(session)
        await client.verify_channels(config.buffer)
        external = await client.posts(config.buffer)
    store.reserve(job["id"], chapters, config.buffer, external)


class BufferPublisher:
    def __init__(self, db, client):
        self.db, self.client, self.store = db, client, BufferStore(db)

    def delivery_status(self, plan, delivery, status, detail="", **values):
        self.store.status(
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
            or not matches(post, request)
            or post.get("schedulingType") != "automatic"
            or (post.get("status") in ("scheduled", "sending") and remote_time(post) is None)
        ):
            self.delivery_status(
                plan,
                delivery,
                "unknown",
                "Buffer zwrócił inne dane niż żądany film, termin lub tryb publikacji.",
                post_id=post.get("id"),
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
            if status in ("scheduled", "sending") and (
                not expires or due + settings.retention_margin_hours * 3600 >= expires
            ):
                detail = "Termin w Bufferze przekracza retencję filmu. Przyspiesz publikację lub przedłuż dostępność pliku w S3."
            self.delivery_status(
                plan,
                delivery,
                local_status,
                detail,
                post_id=post["id"],
                due_at=due,
                retry_at=time.time() + 3600,
            )
        else:
            self.delivery_status(
                plan,
                delivery,
                "unknown",
                "Buffer nie potwierdził automatycznego zaplanowania publikacji.",
                post_id=post["id"],
            )

    async def process(self, plan, config, external, now, channels=None, queue_limit=None):
        job_id, index = plan["job_id"], plan["chapter_index"]
        deliveries = self.store.deliveries(job_id, index)
        state = json.loads(plan["checkpoint"])
        result = state.get("results", {}).get(str(index), {})
        chapter = next((c for c in state.get("chapters", []) if c["index"] == index), None)
        for d in deliveries:
            if d["status"] in ("sending", "unknown") and d.get("request_json"):
                request = json.loads(d["request_json"])
                candidates = [
                    p
                    for p in external
                    if matches(p, request) and (not d["post_id"] or p["id"] == d["post_id"])
                ]
                if len(candidates) == 1:
                    self.record_post(plan, d, request, candidates[0])
                elif d["status"] == "sending":
                    self.delivery_status(
                        plan,
                        d,
                        "unknown",
                        "Wysyłka została przerwana. Sprawdź Buffer; ponowienie może utworzyć duplikat.",
                    )
            elif d["status"] == "scheduled":
                post = next((p for p in external if p["id"] == d["post_id"]), None)
                if post and d.get("request_json"):
                    self.record_post(plan, d, json.loads(d["request_json"]), post)
                elif post and post.get("status") in ("sent", "error"):
                    self.delivery_status(plan, d, post["status"], due_at=remote_time(post))
                elif not post:
                    self.delivery_status(
                        plan,
                        d,
                        "cancelled",
                        "Wpis nie jest już widoczny w kolejce Buffera.",
                    )
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
        custom = config.buffer.scheduling_mode == "customScheduled" or any(
            d["request_json"] and json.loads(d["request_json"]).get("mode") == "customScheduled"
            for d in deliveries
        )
        due = plan["due_at"] if custom else None
        with self.db.connect() as connection:
            occupied = self.store.occupied(
                connection, config.buffer, external, exclude=(job_id, index)
            )
        zone = ZoneInfo(config.buffer.schedule.timezone)
        collision = due is not None and any(
            abs(t - due) < config.buffer.schedule.min_gap_minutes * 60
            or sum(
                cid == channel_id
                and datetime.fromtimestamp(other, zone).date()
                == datetime.fromtimestamp(due, zone).date()
                for cid, other in occupied
            )
            >= config.buffer.schedule.max_posts_per_day
            for channel_id, t in occupied
            if channel_id in {c.id for c in config.buffer.channels}
        )
        if custom and (
            due is None or due < now + config.buffer.schedule.min_lead_minutes * 60 or collision
        ):
            if not self.store.move(
                job_id, index, config.buffer, external, now=datetime.fromtimestamp(now, UTC)
            ):
                for d in pending:
                    self.delivery_status(
                        plan,
                        d,
                        "failed",
                        "Brak wolnego terminu lub część kont ma już zapisany termin.",
                    )
                return
            due = self.store.plan(job_id, index)["due_at"]
        text = (
            result.get("description")
            if any(c.get("text", "").strip() for c in result.get("cues", []))
            else chapter["title"]
        )
        text = text or chapter["title"]
        for d in pending:
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
                else chapter["end"] - chapter["start"]
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
                used = self.store.capacity_used(config.buffer, external, target.id)
                if queue_limit is not None and used >= queue_limit:
                    self.delivery_status(
                        plan,
                        d,
                        "waiting_capacity",
                        f"Kolejka Buffera pełna ({used}/{queue_limit}); klip pozostaje w Pyk i zostanie wysłany po zwolnieniu miejsca.",
                        retry_at=now + config.buffer.status_poll_seconds,
                    )
                    continue
                if not custom:
                    account = (channels or {}).get(target.id)
                    if (
                        not account
                        or not account.get("timezone")
                        or "postingSchedule" not in account
                    ):
                        raise PermanentError(
                            "Buffer nie zwrócił harmonogramu konta; nie można sprawdzić retencji filmu."
                        )
                    if not queue_fits_retention(
                        account, external, now=now, deadline=expires - margin
                    ):
                        raise PermanentError(
                            "Brak slotu Buffera przed końcem retencji filmu z zapasem. Zmień harmonogram lub retencję S3."
                        )
                await self.check_media(media["url"])
                request = post_input(
                    target, url=media["url"], text=caption, title=title, due_at=due
                )
                # Persist before network I/O. A crash here conservatively leaves an unknown result.
                self.delivery_status(
                    plan, d, "sending", request_json=json.dumps(request), attempts=d["attempts"] + 1
                )
                post = await self.client.create(request)
                self.record_post(plan, d, request, post)
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
                    request_json=None,
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
        cache = {}
        limits = {}
        for plan in sorted(
            self.store.plans(accepted=True),
            key=lambda p: (p["accepted_at"], p["job_id"], p["chapter_index"]),
        ):
            config = Config.model_validate_json(plan["config_json"])
            deliveries = self.store.deliveries(plan["job_id"], plan["chapter_index"])
            actionable = any(
                d["status"] in ("pending", "waiting_capacity")
                and d["retry_at"] <= now
                and not any(
                    previous["channel_id"] == d["channel_id"]
                    and previous["part_index"] < d["part_index"]
                    and previous["status"] not in ("scheduled", "sent")
                    for previous in deliveries
                )
                for d in deliveries
            )
            refresh = any(
                d["status"] in ("scheduled", "unknown", "sending") and d["retry_at"] <= now
                for d in deliveries
            )
            result = (
                json.loads(plan["checkpoint"])
                .get("results", {})
                .get(str(plan["chapter_index"]), {})
            )
            if not refresh and (
                not actionable
                or any(plan["variant"] not in clip.get("variants", {}) for clip in clips(result))
                or (config.descriptions.enabled and "description" not in result)
            ):
                continue
            cache_key = config.buffer.model_dump_json()
            try:
                if cache_key not in cache:
                    channels = await self.client.verify_channels(config.buffer)
                    cache[cache_key] = (channels, await self.client.posts(config.buffer))
                if config.buffer.organization_id not in limits:
                    limits[config.buffer.organization_id] = await self.client.queue_limit(
                        config.buffer
                    )
                channels, external = cache[cache_key]
                await self.process(
                    plan, config, external, now, channels, limits[config.buffer.organization_id]
                )
                for d in self.store.deliveries(plan["job_id"], plan["chapter_index"]):
                    if d["status"] in ("scheduled", "unknown"):
                        self.delivery_status(
                            plan,
                            d,
                            d["status"],
                            d["detail"],
                            retry_at=now + config.buffer.status_poll_seconds,
                        )
            except (PermanentError, TransientError) as exc:
                log.warning("Buffer preflight failure_type=%s", type(exc).__name__)
                for d in deliveries:
                    if d["status"] == "waiting_capacity":
                        self.delivery_status(
                            plan,
                            d,
                            "failed" if isinstance(exc, PermanentError) else "waiting_capacity",
                            str(exc),
                            retry_at=now + config.buffer.status_poll_seconds,
                        )
                    elif d["status"] == "pending":
                        attempts = d["attempts"] + 1
                        terminal = isinstance(exc, PermanentError) or attempts >= 3
                        self.delivery_status(
                            plan,
                            d,
                            "failed" if terminal else "pending",
                            str(exc),
                            attempts=attempts,
                            retry_at=now + max(getattr(exc, "retry_after", 0), 60),
                        )
                    elif d["status"] in ("unknown", "sending", "scheduled"):
                        self.delivery_status(
                            plan,
                            d,
                            d["status"],
                            d["detail"],
                            retry_at=now + config.buffer.status_poll_seconds,
                        )


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
