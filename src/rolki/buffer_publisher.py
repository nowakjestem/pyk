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

from .buffer import AmbiguousResult, BufferClient, matches, post_input
from .buffer_store import BufferStore
from .config import Config
from .errors import PermanentError, TransientError

log = logging.getLogger(__name__)


async def reserve_plan(db, job, chapters, config):
    if not config.buffer.enabled or job.get("local_output") or not job.get("channel_id"):
        return
    store = BufferStore(db)
    if all(store.plan(job["id"], c["index"]) for c in chapters):
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
        key = (plan["job_id"], plan["chapter_index"], delivery["channel_id"])
        if (
            not post.get("id")
            or not matches(post, request)
            or post.get("schedulingType") != "automatic"
        ):
            self.store.status(
                *key,
                "unknown",
                "Buffer zwrócił inne dane niż żądany film, termin lub tryb publikacji.",
                post_id=post.get("id"),
            )
            return
        status = post.get("status")
        if status in ("scheduled", "sent", "error", "sending"):
            # Remote sending is acknowledged, local sending means ambiguous in-flight create.
            local_status = "scheduled" if status == "sending" else status
            self.store.status(*key, local_status, post_id=post["id"], retry_at=time.time() + 3600)
        else:
            self.store.status(
                *key,
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
        media = result.get("variants", {}).get(plan["variant"])
        for d in deliveries:
            if d["status"] in ("sending", "unknown") and d.get("request_json"):
                request = json.loads(d["request_json"])
                candidates = [p for p in external if matches(p, request)]
                if len(candidates) == 1:
                    self.record_post(plan, d, request, candidates[0])
                elif d["status"] == "sending":
                    self.store.status(
                        job_id,
                        index,
                        d["channel_id"],
                        "unknown",
                        "Wysyłka została przerwana. Sprawdź Buffer; ponowienie może utworzyć duplikat.",
                    )
            elif d["status"] == "scheduled":
                post = next((p for p in external if p["id"] == d["post_id"]), None)
                if post and post.get("status") in ("sent", "error"):
                    self.store.status(job_id, index, d["channel_id"], post["status"])
                elif post and d.get("request_json"):
                    self.record_post(plan, d, json.loads(d["request_json"]), post)
                elif not post:
                    self.store.status(
                        job_id,
                        index,
                        d["channel_id"],
                        "cancelled",
                        "Wpis nie jest już widoczny w kolejce Buffera.",
                    )
        pending = [
            d
            for d in self.store.deliveries(job_id, index)
            if d["status"] == "pending" and d["retry_at"] <= now
        ]
        if not pending or not chapter or not media:
            return
        if config.descriptions.enabled and "description" not in result:
            return
        due = plan["due_at"]
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
        if due is None or due < now + config.buffer.schedule.min_lead_minutes * 60 or collision:
            if not self.store.move(
                job_id, index, config.buffer, external, now=datetime.fromtimestamp(now, UTC)
            ):
                for d in pending:
                    self.store.status(
                        job_id,
                        index,
                        d["channel_id"],
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
            key = (job_id, index, d["channel_id"])
            target = next(c for c in config.buffer.channels if c.id == d["channel_id"])
            try:
                if chapter["end"] - chapter["start"] > target.max_video_seconds:
                    raise PermanentError(
                        f"Film przekracza skonfigurowany limit {target.max_video_seconds} s dla {target.platform}."
                    )
                if len(text) > target.max_text_chars:
                    raise PermanentError(
                        f"Opis przekracza limit {target.max_text_chars} znaków dla {target.platform}."
                    )
                expires = media.get("expires_at")
                if not expires or due + config.buffer.retention_margin_hours * 3600 >= expires:
                    raise PermanentError(
                        "Retencja S3 nie zapewnia dostępności filmu do terminu publikacji z zapasem."
                    )
                await self.check_media(media["url"])
                request = post_input(
                    target, url=media["url"], text=text, title=chapter["title"], due_at=due
                )
                # Persist before network I/O. A crash here conservatively leaves an unknown result.
                self.store.status(
                    *key, "sending", request_json=json.dumps(request), attempts=d["attempts"] + 1
                )
                post = await self.client.create(request)
                self.record_post(plan, d, request, post)
                external.append(post)
            except AmbiguousResult as exc:
                self.store.status(*key, "unknown", str(exc))
            except TransientError as exc:
                attempts = d["attempts"] + 1
                self.store.status(
                    *key,
                    "failed" if attempts >= 3 else "pending",
                    str(exc),
                    attempts=attempts,
                    retry_at=now + max(exc.retry_after, 2**attempts * 30),
                )
            except PermanentError as exc:
                self.store.status(*key, "failed", str(exc))
            except Exception as exc:
                # Protect Mattermost's task group and never expose raw upstream data.
                log.warning("Buffer delivery failure_type=%s", type(exc).__name__)
                current = next(
                    row
                    for row in self.store.deliveries(job_id, index)
                    if row["channel_id"] == d["channel_id"]
                )
                self.store.status(
                    *key,
                    "unknown" if current["status"] == "sending" else "failed",
                    "Nieoczekiwany błąd integracji. Sprawdź kolejkę Buffera przed ponowieniem.",
                )

    async def tick(self, *, now=None):
        now = time.time() if now is None else now
        cache = {}
        for plan in self.store.plans(accepted=True):
            config = Config.model_validate_json(plan["config_json"])
            deliveries = self.store.deliveries(plan["job_id"], plan["chapter_index"])
            actionable = any(d["status"] == "pending" and d["retry_at"] <= now for d in deliveries)
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
                or plan["variant"] not in result.get("variants", {})
                or (config.descriptions.enabled and "description" not in result)
            ):
                continue
            cache_key = config.buffer.model_dump_json()
            try:
                if cache_key not in cache:
                    await self.client.verify_channels(config.buffer)
                    cache[cache_key] = await self.client.posts(config.buffer)
                await self.process(plan, config, cache[cache_key], now)
                for d in self.store.deliveries(plan["job_id"], plan["chapter_index"]):
                    if d["status"] in ("scheduled", "unknown"):
                        self.store.status(
                            plan["job_id"],
                            plan["chapter_index"],
                            d["channel_id"],
                            d["status"],
                            d["detail"],
                            retry_at=now + config.buffer.status_poll_seconds,
                        )
            except (PermanentError, TransientError) as exc:
                log.warning("Buffer preflight failure_type=%s", type(exc).__name__)
                for d in deliveries:
                    if d["status"] == "pending":
                        attempts = d["attempts"] + 1
                        terminal = isinstance(exc, PermanentError) or attempts >= 3
                        self.store.status(
                            plan["job_id"],
                            plan["chapter_index"],
                            d["channel_id"],
                            "failed" if terminal else "pending",
                            str(exc),
                            attempts=attempts,
                            retry_at=now + max(getattr(exc, "retry_after", 0), 60),
                        )
                    elif d["status"] in ("unknown", "sending", "scheduled"):
                        self.store.status(
                            plan["job_id"],
                            plan["chapter_index"],
                            d["channel_id"],
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
                while True:
                    try:
                        await publisher.tick()
                    except Exception as exc:
                        log.warning("Buffer queue failure_type=%s", type(exc).__name__)
                    await asyncio.sleep(config.buffer.poll_seconds)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
