from __future__ import annotations

import asyncio
import email.utils
import json
import logging
import os
import time
from urllib.parse import urlsplit, urlunsplit

import aiohttp
from aiohttp import web

from .config import Config
from .db import Database
from .errors import PermanentError, TransientError
from .process import retry_network
from .urls import youtube_links

log = logging.getLogger(__name__)


class MattermostClient:
    def __init__(self, session: aiohttp.ClientSession, base_url: str):
        self.session = session
        self.base_url = base_url.rstrip("/")
        self.server_time_ms = None

    async def request(self, method, path, **kwargs):
        try:
            async with self.session.request(
                method, self.base_url + "/api/v4" + path, **kwargs
            ) as response:
                if response.headers.get("Date"):
                    self.server_time_ms = int(
                        email.utils.parsedate_to_datetime(response.headers["Date"]).timestamp()
                        * 1000
                    )
                if response.status == 429 or response.status >= 500:
                    raise TransientError("Tymczasowy błąd Mattermosta.")
                if response.status >= 400:
                    raise PermanentError(
                        f"Mattermost odrzucił operację (HTTP {response.status}). Sprawdź uprawnienia bota."
                    )
                return await response.json()
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise TransientError("Błąd połączenia z Mattermostem.") from exc

    async def get(self, path, **kwargs):
        return await retry_network(lambda: self.request("GET", path, **kwargs))

    @property
    def websocket_url(self):
        parts = urlsplit(self.base_url)
        return urlunsplit(
            (
                "wss" if parts.scheme == "https" else "ws",
                parts.netloc,
                parts.path + "/api/v4/websocket",
                "",
                "",
            )
        )


class Bot:
    def __init__(self, config: Config, db: Database, client: MattermostClient, own_id: str):
        self.config, self.db, self.client, self.own_id = config, db, client, own_id
        self.connected = False
        self.last_reconcile = 0.0
        self.users = {}

    async def handle_post(self, post: dict):
        if post.get("channel_id") not in self.config.mattermost.channel_ids:
            return
        if (
            post.get("user_id") == self.own_id
            or post.get("delete_at")
            or post.get("type")
            or post.get("props", {}).get("from_webhook")
            or post.get("props", {}).get("rolki_event")
        ):
            return
        links = youtube_links(post.get("message", ""))
        if not links:
            return
        user_id = post.get("user_id")
        if user_id not in self.users:
            user = await self.client.get(f"/users/{user_id}")
            if len(self.users) >= 1000:
                self.users.pop(next(iter(self.users)))
            self.users[user_id] = bool(user.get("is_bot"))
        if self.users[user_id]:
            return
        for video_id, url in links:
            self.db.enqueue(
                post_id=post["id"],
                video_id=video_id,
                url=url,
                config=self.config,
                channel_id=post["channel_id"],
                root_id=post.get("root_id") or post["id"],
            )
        # Only reconciliation advances the cursor. Advancing it here could skip earlier
        # messages if WebSocket frames arrive out of order or while backfill is running.

    async def initialize_channel(self, channel_id):
        await self.client.get(f"/channels/{channel_id}")  # Fail early if bot isn't a member.
        if self.db.cursor(channel_id) is None:
            latest = await self.client.get(f"/channels/{channel_id}/posts", params={"per_page": 1})
            boundary = max(
                (p["create_at"] for p in latest.get("posts", {}).values()),
                default=self.client.server_time_ms or int(time.time() * 1000),
            )
            # Inclusive replay is used during recovery; on first activation the
            # newest existing post must also be excluded, even if it has a link.
            self.db.set_cursor(channel_id, boundary + 1)

    async def reconcile_channel(self, channel_id):
        since = self.db.cursor(channel_id)
        if since is None:
            await self.initialize_channel(channel_id)
            since = self.db.cursor(channel_id)
        collected = {}
        before = None
        newest = since
        # Stable before-ID pagination avoids the 1000-post limit and holes of `since`.
        while True:
            params = {"per_page": 100}
            if before:
                params["before"] = before
            result = await self.client.get(f"/channels/{channel_id}/posts", params=params)
            ordered = [result["posts"][key] for key in result.get("order", [])]
            if not ordered:
                break
            newest = max(newest, max(p["create_at"] for p in ordered))
            for post in result.get("posts", {}).values():
                if post["create_at"] >= since:
                    collected[post["id"]] = post
            oldest = min(ordered, key=lambda p: p["create_at"])
            if oldest["create_at"] < since or len(ordered) < 100:
                break
            if before == oldest["id"]:
                raise TransientError("Mattermost nie przesuwa strony synchronizacji.")
            before = oldest["id"]
        for post in sorted(collected.values(), key=lambda p: (p["create_at"], p["id"])):
            # Equal timestamps are replayed safely thanks to the DB's unique key.
            await self.handle_post(post)
        self.db.set_cursor(channel_id, newest)

    async def reconcile_loop(self):
        while True:
            try:
                for channel_id in self.config.mattermost.channel_ids:
                    await self.reconcile_channel(channel_id)
                self.last_reconcile = time.monotonic()
            except TransientError:
                log.warning("mattermost reconciliation temporarily unavailable")
            await asyncio.sleep(self.config.mattermost.reconcile_seconds)

    async def deliver_notification(self, notification: dict):
        try:
            self.db.notification_attempt(notification["id"])
            # Recover the crash window between remote POST success and local acknowledgement.
            if notification["attempts"] > 0:
                thread = await self.client.get(f"/posts/{notification['root_id']}/thread")
                for post in thread.get("posts", {}).values():
                    if post.get("props", {}).get("rolki_event") == notification["id"]:
                        self.db.notification_sent(notification["id"], post["id"])
                        return
            result = await self.client.request(
                "POST",
                "/posts",
                json={
                    "channel_id": notification["channel_id"],
                    "root_id": notification["root_id"],
                    "message": notification["message"],
                    "props": {"rolki_event": notification["id"]},
                },
            )
            self.db.notification_sent(notification["id"], result["id"])
        except (TransientError, PermanentError) as exc:
            self.db.notification_failed(notification["id"], isinstance(exc, PermanentError))
            log.warning("notification=%s failure_type=%s", notification["id"], type(exc).__name__)

    async def outbox_loop(self):
        while True:
            for notification in self.db.pending_notifications():
                await self.deliver_notification(notification)
            await asyncio.sleep(1)

    async def websocket_loop(self):
        while True:
            try:
                async with self.client.session.ws_connect(
                    self.client.websocket_url,
                    heartbeat=20,
                    max_msg_size=2 * 1024**2,
                ) as socket:
                    self.connected = True
                    async for message in socket:
                        if message.type == aiohttp.WSMsgType.TEXT:
                            event = json.loads(message.data)
                            if event.get("event") == "posted":
                                post = event.get("data", {}).get("post")
                                await self.handle_post(
                                    json.loads(post) if isinstance(post, str) else post
                                )
                        elif message.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                            break
            except (
                aiohttp.ClientError,
                TimeoutError,
                TransientError,
                ValueError,
                KeyError,
                TypeError,
            ) as exc:
                log.warning("mattermost websocket disconnected failure_type=%s", type(exc).__name__)
            finally:
                self.connected = False
            await asyncio.sleep(self.config.mattermost.reconnect_seconds)

    async def health(self, _request):
        status = self.db.health()
        worker = status["runtime"].get("worker", {})
        ready = (
            self.connected
            and time.monotonic() - self.last_reconcile
            < max(180, self.config.mattermost.reconcile_seconds * 3)
            and time.time() - worker.get("heartbeat", 0) < 30
            and worker.get("detail") == "ok"
            and status["failed_notifications"] == 0
        )
        return web.json_response(
            {
                "ok": ready,
                "websocket": self.connected,
                "failed_notifications": status["failed_notifications"],
            },
            status=200 if ready else 503,
        )


async def run_bot(config: Config):
    config.require_integrations()
    db = Database(config.paths.database)
    timeout = aiohttp.ClientTimeout(total=60, connect=15)
    async with aiohttp.ClientSession(
        timeout=timeout, headers={"Authorization": f"Bearer {os.environ['MATTERMOST_BOT_TOKEN']}"}
    ) as session:
        client = MattermostClient(session, config.mattermost.url)
        me = await client.get("/users/me")
        if not me.get("is_bot"):
            raise PermanentError("Token musi należeć do konta bota Mattermost.")
        bot = Bot(config, db, client, me["id"])
        for channel_id in config.mattermost.channel_ids:
            await bot.initialize_channel(channel_id)
        app = web.Application()
        app.router.add_get("/healthz", bot.health)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        # Bind to the container interface; Compose publishes only host loopback.
        await web.TCPSite(runner, "0.0.0.0", config.mattermost.health_port).start()
        try:
            async with asyncio.TaskGroup() as group:
                group.create_task(bot.websocket_loop())
                group.create_task(bot.reconcile_loop())
                group.create_task(bot.outbox_loop())
        finally:
            await runner.cleanup()
