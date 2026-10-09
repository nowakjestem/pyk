import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp
import pytest
from aiohttp import web
from pydantic import ValidationError

from rolki.buffer import AmbiguousResult, BufferClient, post_input
from rolki.buffer_publisher import BufferPublisher
from rolki.buffer_store import BufferStore
from rolki.config import Buffer, BufferSchedule, Config
from rolki.db import Database
from rolki.errors import PermanentError, TransientError
from rolki.mattermost import Bot, MattermostClient
from rolki.scheduling import choose_time

NOW = datetime(2026, 10, 9, 12, tzinfo=UTC)


@pytest.fixture
def buffer_config(config):
    data = json.loads(config.model_dump_json())
    data["buffer"] = {
        "enabled": True,
        "organization_id": "org",
        "channels": [
            {"id": "ig", "platform": "instagram"},
            {"id": "tt", "platform": "tiktok"},
            {"id": "yt", "platform": "youtube"},
        ],
    }
    return Config.model_validate(data)


def prepare(db, config, *, post="source", count=1, approve=True):
    job_id = db.enqueue(
        post_id=post,
        video_id="abcdefghijk",
        url="https://youtube.com/watch?v=abcdefghijk",
        config=config,
        channel_id="private",
        root_id=post,
    )
    chapters = [
        {"index": i, "start": i * 10, "end": (i + 1) * 10, "title": f"Rozdział {i}"}
        for i in range(count)
    ]
    results = {
        str(i): {
            "cues": [{"text": "Tekst"}],
            "variants": {
                variant: {
                    "url": f"https://clips.example/{job_id}/{i}/{variant}.mp4",
                    "expires_at": NOW.timestamp() + 30 * 86400,
                }
                for variant in ("crop", "letterbox")
            },
        }
        for i in range(count)
    }
    db.checkpoint(job_id, "uploaded", {"chapters": chapters, "results": results})
    store = BufferStore(db)
    store.reserve(job_id, chapters, config.buffer, [], now=NOW)
    for i in range(count):
        db.notify(job_id, f"chapter:{i}", "Links")
        store.base_message(job_id, i, "Links")
        n = db.notification_for_event(job_id, f"chapter:{i}")
        db.notification_sent(n["id"], f"clip-{job_id}-{i}")
        if approve:
            assert store.accept(f"clip-{job_id}-{i}", "scissors", "any-member")
    return job_id


def test_defaults_and_validation(buffer_config):
    assert Config().buffer.enabled is False
    assert "allowed_user_ids" not in buffer_config.buffer.model_dump()
    with pytest.raises(ValidationError):
        Buffer(enabled=True)
    with pytest.raises(ValidationError):
        BufferSchedule(timezone="invalid/zone")
    with pytest.raises(ValidationError):
        BufferSchedule(window_start="20:00", window_end="10:00")
    with pytest.raises(ValidationError):
        Buffer(
            channels=[
                {"id": "same", "platform": "instagram"},
                {"id": "same", "platform": "youtube"},
            ]
        )


@pytest.mark.parametrize(
    "now",
    [
        NOW,
        datetime(2026, 10, 12, tzinfo=UTC),
        datetime(2026, 10, 11, 22, 30, tzinfo=UTC),
        datetime(2026, 10, 23, tzinfo=UTC),
    ],
)
def test_rolling_window_starts_today_or_next_day(now):
    settings = BufferSchedule()
    due = choose_time(settings, now, "rolling", ["ig"], [])
    local = datetime.fromtimestamp(due, ZoneInfo(settings.timezone))
    assert now.timestamp() + settings.min_lead_minutes * 60 <= due < now.timestamp() + 7 * 86400
    assert local.date() <= now.astimezone(ZoneInfo(settings.timezone)).date() + timedelta(days=1)


def test_scheduler_distribution_capacity_and_determinism():
    settings = BufferSchedule()
    start = NOW.replace(hour=0)
    occupied = []
    times = []
    for i in range(14):
        due = choose_time(settings, start, str(i), ["ig", "tt"], occupied)
        assert due == choose_time(settings, start, str(i), ["ig", "tt"], occupied)
        local = datetime.fromtimestamp(due, ZoneInfo(settings.timezone))
        assert 10 <= local.hour < 20
        assert start.timestamp() <= due < start.timestamp() + 7 * 86400
        assert all(abs(due - previous) >= 180 * 60 for previous in times)
        times.append(due)
        occupied.extend((c, due) for c in ("ig", "tt"))
    assert (
        len({datetime.fromtimestamp(t, ZoneInfo(settings.timezone)).date() for t in times[:7]}) == 7
    )
    assert choose_time(settings, start, "overflow", ["ig", "tt"], occupied) is None


def test_partial_last_day_respects_exact_seven_day_cutoff():
    settings = BufferSchedule()
    zone = ZoneInfo(settings.timezone)
    first = NOW.astimezone(zone).date()
    occupied = [
        (
            "ig",
            datetime.combine(first + timedelta(days=day), settings.window_start, zone).timestamp(),
        )
        for day in range(7)
        for _ in range(settings.max_posts_per_day)
    ]
    due = choose_time(settings, NOW, "last-partial-day", ["ig"], occupied)
    assert datetime.fromtimestamp(due, zone).date() == first + timedelta(days=7)
    assert due < NOW.timestamp() + 7 * 86400
    occupied.extend(("ig", due) for _ in range(settings.max_posts_per_day))
    assert choose_time(settings, NOW, "full", ["ig"], occupied) is None


def test_evening_does_not_schedule_overnight():
    now = datetime(2026, 10, 9, 17, tzinfo=UTC)
    due = choose_time(BufferSchedule(), now, "evening", ["ig"], [])
    local = datetime.fromtimestamp(due, ZoneInfo("Europe/Warsaw"))
    assert local.date().isoformat() == "2026-10-10"
    assert 10 <= local.hour < 20


def test_replan_only_unapproved_preserves_accepted_plan(db, buffer_config):
    job_id = prepare(db, buffer_config, count=2, approve=False)
    store = BufferStore(db)
    store.accept(f"clip-{job_id}-0", "scissors", "member")
    accepted = store.plan(job_id, 0)
    with db.connect() as conn:
        conn.execute(
            "UPDATE buffer_plans SET due_at=? WHERE job_id=? AND chapter_index=1",
            (NOW.timestamp() + 10 * 86400, job_id),
        )
    assert not store.move(job_id, 0, buffer_config.buffer, [], now=NOW, unapproved_only=True)
    assert store.plan(job_id, 0) == accepted
    assert store.move(
        job_id, 1, buffer_config.buffer, [], now=NOW, unapproved_only=True, notice="Przeliczono"
    )
    assert NOW.timestamp() <= store.plan(job_id, 1)["due_at"] < NOW.timestamp() + 7 * 86400
    assert "Przeliczono" in store.message(job_id, 1)


async def test_replan_cli_uses_job_snapshot_and_only_unapproved(
    db, buffer_config, config, monkeypatch, capsys
):
    from rolki.cli import async_main, parser

    job_id = prepare(db, buffer_config, count=2, approve=False)
    store = BufferStore(db)
    store.accept(f"clip-{job_id}-0", "scissors", "member")
    accepted = store.plan(job_id, 0)
    monkeypatch.setenv("BUFFER_API_KEY", "test-key")

    class Client:
        def __init__(self, _session):
            pass

        async def verify_channels(self, settings):
            assert settings.organization_id == "org"

        async def posts(self, _settings):
            return []

    monkeypatch.setattr("rolki.buffer.BufferClient", Client)
    assert await async_main(parser().parse_args(["buffer-replan", job_id]), config) == 0
    assert "dla 1 niezatwierdzonych" in capsys.readouterr().out
    assert store.plan(job_id, 0) == accepted
    assert store.plan(job_id, 1)["notice"] == "Termin przeliczono na najbliższe 7 dni."


def test_reaction_race_persists_one_variant(db, buffer_config):
    job_id = prepare(db, buffer_config, approve=False)
    store = BufferStore(db)

    def accept(i):
        return store.accept(
            f"clip-{job_id}-0", "scissors" if i % 2 else "frame_with_picture", f"member-{i}"
        )

    with ThreadPoolExecutor(max_workers=6) as pool:
        outcomes = list(pool.map(accept, range(12)))
    assert sum(outcomes) == 1
    assert len(store.deliveries(job_id, 0)) == 3
    assert store.plan(job_id, 0)["variant"] in ("crop", "letterbox")
    assert not store.accept(f"clip-{job_id}-0", "scissors", "another-member")


def test_reservations_across_jobs_and_restart(db, buffer_config):
    first = prepare(db, buffer_config, count=7, approve=False)
    second = prepare(db, buffer_config, post="second", count=7, approve=False)
    store = BufferStore(db)
    original = [p["due_at"] for p in store.plans()]
    assert len(original) == 14
    assert all(abs(a - b) >= 180 * 60 for i, a in enumerate(original) for b in original[i + 1 :])
    store.reserve(
        first,
        [{"index": i} for i in range(7)],
        buffer_config.buffer,
        [],
        now=datetime(2026, 11, 1, tzinfo=UTC),
    )
    assert [p["due_at"] for p in BufferStore(db).plans()] == original
    assert first != second


class FakeClient:
    def __init__(self):
        self.created, self.remote = [], []
        self.failure = None

    async def verify_channels(self, _settings):
        pass

    async def posts(self, _settings):
        return [dict(p) for p in self.remote]

    async def create(self, request):
        self.created.append(request)
        if self.failure:
            error = self.failure(request)
            if error:
                raise error
        p = {
            "id": f"remote-{len(self.created)}",
            "channelId": request["channelId"],
            "text": request["text"],
            "dueAt": request["dueAt"],
            "status": "scheduled",
            "schedulingType": "automatic",
            "assets": [{"source": request["assets"][0]["video"]["url"]}],
        }
        self.remote.append(p)
        return p


def publisher(db, monkeypatch):
    client = FakeClient()
    p = BufferPublisher(db, client)

    async def media(_url):
        pass

    monkeypatch.setattr(p, "check_media", media)
    return p, client


async def test_three_platforms_restart_and_variant(db, buffer_config, monkeypatch):
    job_id = prepare(db, buffer_config, approve=False)
    store = BufferStore(db)
    store.accept(f"clip-{job_id}-0", "frame_with_picture", "member")
    p, client = publisher(db, monkeypatch)
    await p.tick(now=NOW.timestamp())
    assert len(client.created) == 3
    assert all(r["assets"][0]["video"]["url"].endswith("letterbox.mp4") for r in client.created)
    assert client.created[0]["metadata"]["instagram"]["type"] == "reel"
    assert client.created[2]["metadata"]["youtube"]["title"] == "Rozdział 0"
    assert all(d["status"] == "scheduled" for d in store.deliveries(job_id, 0))
    await BufferPublisher(db, client).tick(now=NOW.timestamp() + 30)
    assert len(client.created) == 3


async def test_partial_success_only_retries_failed_account(db, buffer_config, monkeypatch):
    job_id = prepare(db, buffer_config)
    p, client = publisher(db, monkeypatch)
    client.failure = lambda r: PermanentError("Odrzucono") if r["channelId"] == "tt" else None
    await p.tick(now=NOW.timestamp())
    assert [d["status"] for d in p.store.deliveries(job_id, 0)] == [
        "scheduled",
        "failed",
        "scheduled",
    ]
    client.failure = None
    p.store.retry(job_id, 0, "tt")
    await p.tick(now=NOW.timestamp() + 30)
    assert [r["channelId"] for r in client.created] == ["ig", "tt", "yt", "tt"]


async def test_ambiguous_never_recreates_and_can_reconcile(db, buffer_config, monkeypatch):
    job_id = prepare(db, buffer_config)
    p, client = publisher(db, monkeypatch)
    client.failure = lambda r: (
        AmbiguousResult("Utracono odpowiedź") if r["channelId"] == "ig" else None
    )
    await p.tick(now=NOW.timestamp())
    assert p.store.deliveries(job_id, 0)[0]["status"] == "unknown"
    with pytest.raises(ValueError):
        p.store.retry(job_id, 0, "ig")
    await p.tick(now=NOW.timestamp() + 4000)
    assert len(client.created) == 3
    req = client.created[0]
    client.remote.append(
        {
            "id": "lost-response",
            "channelId": "ig",
            "text": req["text"],
            "dueAt": req["dueAt"],
            "status": "scheduled",
            "schedulingType": "automatic",
            "assets": [{"source": req["assets"][0]["video"]["url"]}],
        }
    )
    await p.tick(now=NOW.timestamp() + 8000)
    assert p.store.deliveries(job_id, 0)[0]["post_id"] == "lost-response"
    assert len(client.created) == 3


async def test_description_waits_without_api_calls(db, buffer_config, monkeypatch):
    data = json.loads(buffer_config.model_dump_json())
    data["descriptions"] = {"enabled": True}
    config = Config.model_validate(data)
    job_id = prepare(db, config)
    p, client = publisher(db, monkeypatch)
    await p.tick(now=NOW.timestamp())
    assert not client.created
    state = json.loads(db.get(job_id)["checkpoint"])
    state["results"]["0"]["description"] = "Opis #test"
    db.checkpoint(job_id, "described", state)
    await p.tick(now=NOW.timestamp())
    assert all(r["text"] == "Opis #test" for r in client.created)


@pytest.mark.parametrize("problem", ["expired", "duration", "caption"])
async def test_invalid_media_not_scheduled(db, buffer_config, monkeypatch, problem):
    job_id = prepare(db, buffer_config)
    state = json.loads(db.get(job_id)["checkpoint"])
    if problem == "expired":
        state["results"]["0"]["variants"]["crop"]["expires_at"] = NOW.timestamp() + 86400
    elif problem == "duration":
        state["chapters"][0]["end"] = 500
    else:
        data = json.loads(buffer_config.model_dump_json())
        data["descriptions"]["enabled"] = True
        with db.connect() as conn:
            conn.execute("UPDATE jobs SET config_json=? WHERE id=?", (json.dumps(data), job_id))
        state["results"]["0"]["description"] = "x" * 2001
    db.checkpoint(job_id, "uploaded", state)
    p, client = publisher(db, monkeypatch)
    await p.tick(now=NOW.timestamp())
    assert not client.created
    assert all(d["status"] == "failed" for d in p.store.deliveries(job_id, 0))


async def test_late_reaction_moves_within_next_seven_days(db, buffer_config, monkeypatch):
    job_id = prepare(db, buffer_config)
    p, client = publisher(db, monkeypatch)
    late = datetime(2026, 10, 19, tzinfo=UTC).timestamp()
    await p.tick(now=late)
    assert late + 120 * 60 <= p.store.plan(job_id, 0)["due_at"] < late + 7 * 86400
    assert len(client.created) == 3
    assert "skorygowano" in p.store.message(job_id, 0)


class MattermostFake:
    def __init__(self, reactions):
        self.reactions = reactions
        self.sent = []

    async def get(self, path, **_kwargs):
        if path.endswith("/reactions"):
            return self.reactions
        if path.startswith("/users/"):
            return {"is_bot": False}
        raise AssertionError(path)

    async def request(self, method, path, **kwargs):
        self.sent.append(kwargs["json"])
        return {}


async def test_any_member_and_reconnect_reactions(db, buffer_config):
    job_id = prepare(db, buffer_config, approve=False)
    reaction = {
        "post_id": f"clip-{job_id}-0",
        "user_id": "arbitrary-member",
        "emoji_name": "scissors",
        "create_at": 1,
    }
    client = MattermostFake([reaction])
    bot = Bot(buffer_config, db, client, "own-bot")
    await bot.handle_reaction({**reaction, "post_id": "unrelated"})
    assert not BufferStore(db).plan(job_id, 0)["variant"]
    await bot.reconcile_reactions()
    assert BufferStore(db).plan(job_id, 0)["accepted_by"] == "arbitrary-member"
    await bot.handle_reaction(
        {**reaction, "user_id": "second-member", "emoji_name": "frame_with_picture"}
    )
    assert BufferStore(db).plan(job_id, 0)["variant"] == "crop"


async def test_delayed_message_update_uses_current_status(db, buffer_config):
    job_id = prepare(db, buffer_config)
    store = BufferStore(db)
    old = next(n for n in db.pending_notifications() if n["event_key"].startswith("buffer:"))
    store.status(job_id, 0, "ig", "scheduled", post_id="remote")
    bot = Bot(buffer_config, db, MattermostFake([]), "own-bot")
    await bot.deliver_notification(old)
    message = bot.client.sent[-1]["message"]
    assert message.startswith("Links")
    assert "instagram: zaplanowano" in message
    assert "tiktok: oczekuje" in message


@asynccontextmanager
async def http_server(handler):
    app = web.Application()
    app.router.add_post("/", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}/"
    finally:
        await runner.cleanup()


@pytest.mark.parametrize(
    "kind,error",
    [
        ("typed", PermanentError),
        ("auth", PermanentError),
        ("rate", TransientError),
        ("server", AmbiguousResult),
        ("invalid", AmbiguousResult),
    ],
)
async def test_real_graphql_errors(kind, error, monkeypatch):
    monkeypatch.setenv("BUFFER_API_KEY", "secret-test-only")

    async def handler(request):
        assert request.headers["Authorization"] == "Bearer secret-test-only"
        body = await request.json()
        assert body["variables"]["input"]["channelId"] == "ig"
        if kind == "typed":
            return web.json_response(
                {
                    "data": {
                        "createPost": {
                            "__typename": "InvalidInputError",
                            "message": "secret-test-only",
                        }
                    }
                }
            )
        if kind in ("auth", "rate"):
            return web.json_response(
                {
                    "errors": [
                        {
                            "message": "secret-test-only",
                            "extensions": {
                                "code": "UNAUTHORIZED" if kind == "auth" else "RATE_LIMIT_EXCEEDED"
                            },
                        }
                    ]
                }
            )
        if kind == "server":
            return web.Response(status=503)
        return web.Response(text="not JSON")

    async with http_server(handler) as endpoint, aiohttp.ClientSession() as session:
        with pytest.raises(error) as exc:
            await BufferClient(session, endpoint=endpoint).create({"channelId": "ig"})
        assert "secret-test-only" not in str(exc.value)


async def test_timeout_is_ambiguous():
    async def handler(_request):
        await asyncio.sleep(0.05)
        return web.json_response({"data": {}})

    async with (
        http_server(handler) as endpoint,
        aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=0.01)) as session,
    ):
        with pytest.raises(AmbiguousResult):
            await BufferClient(session, endpoint=endpoint).create({"channelId": "ig"})


async def test_real_graphql_scheduled_request(buffer_config):
    target = buffer_config.buffer.channels[0]
    payload = post_input(
        target,
        url="https://clips.example/video.mp4",
        text="Opis",
        title="Tytuł",
        due_at=NOW.timestamp(),
    )

    async def handler(request):
        body = await request.json()
        inp = body["variables"]["input"]
        assert inp == payload
        assert inp["mode"] == "customScheduled"
        assert inp["schedulingType"] == "automatic"
        return web.json_response(
            {
                "data": {
                    "createPost": {
                        "__typename": "PostActionSuccess",
                        "post": {"id": "post", "status": "scheduled"},
                    }
                }
            }
        )

    async with http_server(handler) as endpoint, aiohttp.ClientSession() as session:
        assert (await BufferClient(session, endpoint=endpoint).create(payload))["id"] == "post"


async def test_restart_after_remote_success_before_ack(db, buffer_config, monkeypatch):
    job_id = prepare(db, buffer_config)
    p, client = publisher(db, monkeypatch)
    create = client.create

    async def interrupted(request):
        await create(request)
        raise asyncio.CancelledError

    client.create = interrupted
    with pytest.raises(asyncio.CancelledError):
        await p.tick(now=NOW.timestamp())
    assert p.store.deliveries(job_id, 0)[0]["status"] == "sending"
    client.create = create
    await p.tick(now=NOW.timestamp() + 30)
    assert len(client.created) == 3
    assert all(d["status"] == "scheduled" for d in p.store.deliveries(job_id, 0))


async def test_new_manual_post_collision_replans(db, buffer_config, monkeypatch):
    job_id = prepare(db, buffer_config)
    p, client = publisher(db, monkeypatch)
    due = p.store.plan(job_id, 0)["due_at"]
    manual = {
        "id": "manual",
        "channelId": "ig",
        "dueAt": datetime.fromtimestamp(due, UTC).isoformat(),
        "status": "scheduled",
    }
    client.remote.append(manual)
    await p.tick(now=NOW.timestamp())
    moved = p.store.plan(job_id, 0)["due_at"]
    assert moved != due
    assert abs(moved - due) >= 180 * 60
    assert len(client.created) == 3


def test_capacity_overflow_is_visible(db, buffer_config):
    job_id = prepare(db, buffer_config, count=20, approve=False)
    store = BufferStore(db)
    assert store.plan(job_id, 19)["due_at"] is None
    assert "Brak wolnego terminu" in store.message(job_id, 19)


def test_remote_move_changes_occupied_slot(db, buffer_config):
    job_id = prepare(db, buffer_config)
    store = BufferStore(db)
    due = store.plan(job_id, 0)["due_at"]
    store.status(job_id, 0, "ig", "scheduled", post_id="existing")
    external = [
        {
            "id": "existing",
            "channelId": "ig",
            "dueAt": datetime.fromtimestamp(due + 3600, UTC).isoformat(),
            "status": "scheduled",
        }
    ]
    with db.connect() as conn:
        occupied = store.occupied(conn, buffer_config.buffer, external)
    assert ("ig", due + 3600) in occupied
    assert ("ig", due) not in occupied


async def test_http_rate_limit_has_delay():
    async def handler(_request):
        return web.Response(status=429, headers={"Retry-After": "321"})

    async with http_server(handler) as endpoint, aiohttp.ClientSession() as session:
        with pytest.raises(TransientError) as exc:
            await BufferClient(session, endpoint=endpoint).create({"channelId": "ig"})
        assert exc.value.retry_after == 321


async def test_paginated_posts_and_channel_contract(buffer_config):
    cursors = []

    async def handler(request):
        body = await request.json()
        if "ChannelsInput" in body["query"]:
            assert body["variables"]["input"] == {"organizationId": "org"}
            return web.json_response(
                {
                    "data": {
                        "channels": [
                            {"id": c.id, "service": c.platform}
                            for c in buffer_config.buffer.channels
                        ]
                    }
                }
            )
        cursor = body["variables"]["after"]
        cursors.append(cursor)
        return web.json_response(
            {
                "data": {
                    "posts": {
                        "edges": [{"node": {"id": "first" if cursor is None else "second"}}],
                        "pageInfo": {"hasNextPage": cursor is None, "endCursor": "next"},
                    }
                }
            }
        )

    async with http_server(handler) as endpoint, aiohttp.ClientSession() as session:
        client = BufferClient(session, endpoint=endpoint)
        await client.verify_channels(buffer_config.buffer)
        posts = await client.posts(buffer_config.buffer)
        assert [p["id"] for p in posts] == ["first", "second"]
        assert cursors == [None, "next"]


async def test_success_with_graphql_warning_not_retried():
    async def handler(_request):
        return web.json_response(
            {
                "data": {
                    "createPost": {"__typename": "PostActionSuccess", "post": {"id": "created"}}
                },
                "errors": [{"extensions": {"code": "UNEXPECTED"}}],
            }
        )

    async with http_server(handler) as endpoint, aiohttp.ClientSession() as session:
        assert (await BufferClient(session, endpoint=endpoint).create({"channelId": "ig"}))[
            "id"
        ] == "created"


def test_explicit_resolution_of_unknown(db, buffer_config):
    job_id = prepare(db, buffer_config)
    store = BufferStore(db)
    store.status(job_id, 0, "ig", "unknown")
    store.retry(job_id, 0, "ig", confirmed_not_created=True)
    assert store.deliveries(job_id, 0)[0]["status"] == "pending"
    store.status(job_id, 0, "ig", "unknown", post_id="known-write")
    with pytest.raises(ValueError):
        store.retry(job_id, 0, "ig", confirmed_not_created=True)


def test_upgrade_preserves_existing_job_and_outbox(db, config):
    job_id = db.enqueue(
        post_id="old",
        video_id="abcdefghijk",
        url="https://youtube.com/watch?v=abcdefghijk",
        config=config,
        channel_id="private",
        root_id="old",
    )
    db.checkpoint(job_id, "complete", {"results": {"0": {"description": "Istniejący opis"}}})
    snapshot = json.loads(config.model_dump_json())
    snapshot.pop("buffer")
    with db.connect() as conn:
        conn.execute("DROP TABLE buffer_deliveries")
        conn.execute("DROP TABLE buffer_plans")
        conn.execute("PRAGMA user_version=4")
        conn.execute("UPDATE jobs SET config_json=? WHERE id=?", (json.dumps(snapshot), job_id))
    migrated = Database(db.path)
    assert (
        json.loads(migrated.get(job_id)["checkpoint"])["results"]["0"]["description"]
        == "Istniejący opis"
    )
    assert migrated.notification_for_event(job_id, "accepted")["message"]
    assert not Config.model_validate_json(migrated.get(job_id)["config_json"]).buffer.enabled
    assert BufferStore(migrated).plans() == []


async def test_inaccessible_reaction_post_does_not_stop_reconcile(db, buffer_config):
    prepare(db, buffer_config, approve=False)

    class DeletedPost(MattermostFake):
        async def get(self, path, **kwargs):
            if path.endswith("/reactions"):
                raise PermanentError("Post nie jest dostępny.")
            return await super().get(path, **kwargs)

    await Bot(buffer_config, db, DeletedPost([]), "own-bot").reconcile_reactions()


async def test_reaction_event_through_real_websocket(db, buffer_config):
    job_id = prepare(db, buffer_config, approve=False)
    reaction = {
        "post_id": f"clip-{job_id}-0",
        "emoji_name": "frame_with_picture",
        "user_id": "member",
    }
    received = asyncio.Event()

    async def socket_handler(request):
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        await socket.send_json(
            {"event": "reaction_added", "data": {"reaction": json.dumps(reaction)}}
        )
        await received.wait()
        return socket

    async def user_handler(_request):
        return web.json_response({"is_bot": False})

    app = web.Application()
    app.router.add_get("/api/v4/websocket", socket_handler)
    app.router.add_get("/api/v4/users/member", user_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        async with aiohttp.ClientSession() as session:
            bot = Bot(
                buffer_config, db, MattermostClient(session, f"http://127.0.0.1:{port}"), "own-bot"
            )
            task = asyncio.create_task(bot.websocket_loop())
            try:
                async with asyncio.timeout(2):
                    while not BufferStore(db).plan(job_id, 0)["variant"]:
                        await asyncio.sleep(0.01)
                assert BufferStore(db).plan(job_id, 0)["variant"] == "letterbox"
            finally:
                received.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    finally:
        received.set()
        await runner.cleanup()


@pytest.mark.parametrize(
    "now", [datetime(2026, 3, 22, tzinfo=UTC), datetime(2026, 10, 18, tzinfo=UTC)]
)
def test_dst_week_produces_daytime_local_times(now):
    settings = BufferSchedule()
    zone = ZoneInfo(settings.timezone)
    occupied = []
    for i in range(7):
        due = choose_time(settings, now, str(i), ["ig"], occupied)
        local = datetime.fromtimestamp(due, zone)
        assert 10 <= local.hour < 20
        assert now.timestamp() + settings.min_lead_minutes * 60 <= due < now.timestamp() + 7 * 86400
        occupied.append(("ig", due))
