import asyncio
import json
import time

import aiohttp
import pytest
from aiohttp import web

from rolki.errors import PermanentError, TransientError
from rolki.mattermost import Bot, MattermostClient


def post(index, **changes):
    return {
        "id": str(index),
        "create_at": index * 1000,
        "channel_id": "private",
        "user_id": "human",
        "message": "https://youtu.be/abcdefghijk",
        "type": "",
        "props": {},
    } | changes


class Client:
    server_time_ms = 100000

    def __init__(self, posts=None):
        self.posts = posts or []
        self.sent = []
        self.fail_after_send = False
        self.edits = []

    async def get(self, path, params=None):
        if path.startswith("/users/"):
            return {"is_bot": path.endswith("otherbot")}
        if path.endswith("/thread"):
            return {"posts": {p["id"]: p for p in self.sent}}
        if path == "/channels/private":
            return {"id": "private"}
        ordered = sorted(self.posts, key=lambda p: p["create_at"], reverse=True)
        if params.get("before"):
            boundary = next(p["create_at"] for p in ordered if p["id"] == params["before"])
            ordered = [p for p in ordered if p["create_at"] < boundary]
        selected = ordered[: int(params["per_page"])]
        return {"order": [p["id"] for p in selected], "posts": {p["id"]: p for p in selected}}

    async def request(self, method, path, json):
        if method == "PUT":
            post_id = path.split("/")[2]
            result = next(p for p in self.sent if p["id"] == post_id)
            result.update(json)
            self.edits.append((path, json))
            if self.fail_after_send:
                raise TransientError("Connection lost after editing")
            return result
        result = {"id": f"reply-{len(self.sent)}"} | json
        self.sent.append(result)
        if self.fail_after_send:
            raise TransientError("Connection lost after posting")
        return result


async def test_private_channel_thread_and_dedup(db, config):
    bot = Bot(config, db, Client(), "ownbot")
    await bot.handle_post(post(1, root_id="existing-thread"))
    await bot.handle_post(post(1))
    await bot.handle_post(post(1))
    assert len(db.list_jobs()) == 1
    assert db.pending_notifications()[0]["root_id"] == "1"


@pytest.mark.parametrize(
    "changes",
    [
        {"user_id": "ownbot"},
        {"user_id": "otherbot"},
        {"channel_id": "unrelated"},
        {"props": {"from_webhook": "true"}},
        {"type": "system_join_channel"},
        {"delete_at": 1},
        {"root_id": "thread-root"},
    ],
)
async def test_ignore_bots_and_unrelated_posts(db, config, changes):
    bot = Bot(config, db, Client(), "ownbot")
    await bot.handle_post(post(1, **changes))
    assert db.list_jobs() == []


async def test_first_start_skips_history_then_recovers_gap(db, config):
    client = Client([post(1)])
    bot = Bot(config, db, client, "ownbot")
    await bot.initialize_channel("private")
    await bot.reconcile_channel("private")
    assert db.list_jobs() == []
    client.posts.append(post(2))
    await bot.reconcile_channel("private")
    await bot.reconcile_channel("private")
    assert [job["video_id"] for job in db.list_jobs()] == ["abcdefghijk"]
    assert db.cursor("private") == 2000


async def test_backfill_more_than_one_page(db, config):
    client = Client([post(i) for i in range(1, 251)])
    db.set_cursor("private", 1000)
    bot = Bot(config, db, client, "ownbot")
    await bot.reconcile_channel("private")
    with db.connect() as connection:
        assert connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 250
    assert db.cursor("private") == 250000


async def test_websocket_out_of_order_cannot_skip_backfill(db, config):
    client = Client([post(2), post(3)])
    db.set_cursor("private", 1000)
    bot = Bot(config, db, client, "ownbot")
    await bot.handle_post(post(3))
    assert db.cursor("private") == 1000
    await bot.reconcile_channel("private")
    assert len(db.list_jobs()) == 2


async def test_backfill_ignores_thread_replies_and_advances_cursor(db, config):
    client = Client([post(2, root_id="thread-root"), post(3)])
    db.set_cursor("private", 1000)
    bot = Bot(config, db, client, "ownbot")
    await bot.reconcile_channel("private")
    assert len(db.list_jobs()) == 1
    assert db.get(db.list_jobs()[0]["id"])["post_id"] == "3"
    assert db.cursor("private") == 3000


async def test_start_message_is_edited_not_posted_twice(db, config, enqueue):
    job_id = enqueue()
    client = Client()
    bot = Bot(config, db, client, "ownbot")
    original = db.notification_for_event(job_id, "accepted")
    await bot.deliver_notification(original)
    db.notify(job_id, "metadata", "Film: Zażółć. Rozdziałów: 2.", update_of="accepted")
    edit = db.notification_for_event(job_id, "metadata")
    client.fail_after_send = True
    await bot.deliver_notification(edit)
    client.fail_after_send = False
    retry = db.notification_for_event(job_id, "metadata")
    await bot.deliver_notification(retry)
    assert len(client.sent) == 1
    assert client.sent[0]["message"] == "Film: Zażółć. Rozdziałów: 2."
    assert client.sent[0]["root_id"] == "root"
    assert client.sent[0]["props"]["rolki_event"] == original["id"]
    assert db.notification_for_event(job_id, "metadata")["post_id"] == client.sent[0]["id"]
    assert db.pending_notifications() == []


async def test_edit_waits_for_ambiguous_confirmation_recovery(db, config, enqueue):
    job_id = enqueue()
    client = Client()
    bot = Bot(config, db, client, "ownbot")
    client.fail_after_send = True
    await bot.deliver_notification(db.notification_for_event(job_id, "accepted"))
    db.notify(job_id, "metadata", "Full metadata", update_of="accepted")
    await bot.deliver_notification(db.notification_for_event(job_id, "metadata"))
    assert client.edits == []
    assert db.notification_for_event(job_id, "metadata")["attempts"] == 0
    client.fail_after_send = False
    await bot.deliver_notification(db.notification_for_event(job_id, "accepted"))
    await bot.deliver_notification(db.notification_for_event(job_id, "metadata"))
    assert len(client.sent) == 1 and client.sent[0]["message"] == "Full metadata"
    assert db.pending_notifications() == []


async def test_edit_failure_never_falls_back_to_new_post(db, config, enqueue):
    job_id = enqueue()
    client = Client()
    bot = Bot(config, db, client, "ownbot")
    original = db.notification_for_event(job_id, "accepted")
    db.notification_attempt(original["id"])
    db.notification_failed(original["id"], permanent=True)
    db.notify(job_id, "metadata", "Full metadata", update_of="accepted")
    await bot.deliver_notification(db.notification_for_event(job_id, "metadata"))
    assert client.sent == [] and client.edits == []
    assert db.notification_for_event(job_id, "metadata")["status"] == "failed"


async def test_notification_recovers_ambiguous_post(db, config, enqueue):
    enqueue()
    client = Client()
    client.fail_after_send = True
    bot = Bot(config, db, client, "ownbot")
    notification = db.pending_notifications()[0]
    await bot.deliver_notification(notification)
    with db.connect() as connection:
        retry = dict(
            connection.execute("SELECT * FROM outbox WHERE id=?", (notification["id"],)).fetchone()
        )
    client.fail_after_send = False
    await bot.deliver_notification(retry)
    assert len(client.sent) == 1
    assert db.pending_notifications() == []


@pytest.mark.parametrize(
    "status,exception",
    [(401, PermanentError), (403, PermanentError), (429, TransientError), (503, TransientError)],
)
async def test_http_errors_do_not_expose_tokens(status, exception):
    async def response(_request):
        return web.json_response({"error": "secret-content"}, status=status)

    app = web.Application()
    app.router.add_get("/api/v4/test", response)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        async with aiohttp.ClientSession() as session:
            client = MattermostClient(session, f"http://127.0.0.1:{port}")
            with pytest.raises(exception) as failure:
                await client.request("GET", "/test")
            assert "secret-content" not in str(failure.value)
    finally:
        await runner.cleanup()


async def test_health_requires_worker_and_websocket(db, config):
    bot = Bot(config, db, Client(), "ownbot")
    assert (await bot.health(None)).status == 503
    db.heartbeat("worker")
    bot.connected = True
    bot.last_reconcile = time.monotonic()
    assert (await bot.health(None)).status == 200


async def test_real_websocket_reconnect_deduplicates_posts(db, config):
    connections = 0

    async def websocket(request):
        nonlocal connections
        connections += 1
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        await socket.send_json({"event": "posted", "data": {"post": json.dumps(post(1))}})
        await asyncio.sleep(0.02)
        await socket.close()
        return socket

    async def user(_request):
        return web.json_response({"is_bot": False})

    app = web.Application()
    app.router.add_get("/api/v4/websocket", websocket)
    app.router.add_get("/api/v4/users/human", user)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    configuration = config.model_copy(
        update={"mattermost": config.mattermost.model_copy(update={"reconnect_seconds": 1})}
    )
    try:
        async with aiohttp.ClientSession() as session:
            client = MattermostClient(session, f"http://127.0.0.1:{port}")
            bot = Bot(configuration, db, client, "ownbot")
            task = asyncio.create_task(bot.websocket_loop())
            try:
                async with asyncio.timeout(5):
                    while connections < 2:
                        await asyncio.sleep(0.05)
                assert len(db.list_jobs()) == 1
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    finally:
        await runner.cleanup()
