from __future__ import annotations

import json
import os
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import aiohttp

from .config import Buffer, BufferChannel
from .errors import JobError, PermanentError, TransientError
from .openai_http import retry_after_seconds

ENDPOINT = "https://api.buffer.com"
POST_FIELDS = "id channelId dueAt status schedulingType text assets { source }"


class AmbiguousResult(JobError):
    """A mutation may have succeeded; never automatically repeat it."""


class BufferClient:
    def __init__(self, session: aiohttp.ClientSession, *, endpoint: str = ENDPOINT):
        self.session, self.endpoint = session, endpoint

    async def request(self, query, variables=None, *, mutation=False):
        try:
            async with self.session.post(
                self.endpoint,
                headers={"Authorization": f"Bearer {os.environ.get('BUFFER_API_KEY', '')}"},
                json={"query": query, "variables": variables or {}},
            ) as response:
                if response.status == 429:
                    raise TransientError(
                        "Limit API Buffera.",
                        retry_after=retry_after_seconds(response.headers.get("Retry-After")),
                    )
                if response.status >= 500:
                    if mutation:
                        raise AmbiguousResult("Nieznany wynik wysyłki do Buffera; sprawdź kolejkę.")
                    raise TransientError("Buffer chwilowo niedostępny.")
                if response.status in (401, 403):
                    raise PermanentError("Buffer odrzucił klucz API lub uprawnienia.")
                if response.status != 200:
                    raise PermanentError(f"Buffer odrzucił żądanie (HTTP {response.status}).")
                raw = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    raw.extend(chunk)
                    if len(raw) > 4 * 1024**2:
                        raise ValueError("response too large")
                result = json.loads(raw)
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            if mutation:
                raise AmbiguousResult(
                    "Nieznany wynik wysyłki do Buffera; sprawdź kolejkę."
                ) from exc
            raise TransientError("Nie udało się odczytać odpowiedzi Buffera.") from exc
        if not isinstance(result, dict):
            if mutation:
                raise AmbiguousResult("Nieznany wynik wysyłki do Buffera.")
            raise TransientError("Niepoprawna odpowiedź Buffera.")
        if result.get("errors"):
            if (
                mutation
                and isinstance(result.get("data"), dict)
                and result["data"].get("createPost", {}).get("post")
            ):
                # A created post with warnings is still a write. Never retry it.
                return result["data"]
            codes = {e.get("extensions", {}).get("code") for e in result["errors"]}
            if codes <= {"RATE_LIMIT_EXCEEDED"}:
                raise TransientError("Limit API Buffera.", retry_after=60)
            if mutation and not codes <= {
                "UNAUTHORIZED",
                "FORBIDDEN",
                "GRAPHQL_VALIDATION_FAILED",
                "BAD_USER_INPUT",
            }:
                raise AmbiguousResult("Nieznany wynik wysyłki do Buffera.")
            raise PermanentError("Buffer odrzucił operację; sprawdź konto i uprawnienia.")
        if not isinstance(result.get("data"), dict):
            if mutation:
                raise AmbiguousResult("Nieznany wynik wysyłki do Buffera.")
            raise TransientError("Brak danych w odpowiedzi Buffera.")
        return result["data"]

    async def organizations(self):
        return (await self.request("query { account { organizations { id } } }"))["account"][
            "organizations"
        ]

    async def channels(self, organization_id):
        return (
            await self.request(
                "query($input: ChannelsInput!) { channels(input: $input) { id name service isDisconnected isLocked isQueuePaused timezone postingSchedule { day paused times } } }",
                {"input": {"organizationId": organization_id}},
            )
        )["channels"]

    async def verify_channels(self, settings: Buffer):
        available = {c["id"]: c for c in await self.channels(settings.organization_id)}
        for target in settings.channels:
            c = available.get(target.id)
            if not c or c.get("service") != target.platform:
                raise PermanentError("Nieprawidłowe konto lub platforma Buffera.")
            if c.get("isDisconnected") or c.get("isLocked") or c.get("isQueuePaused"):
                raise PermanentError(
                    "Konto Buffera jest rozłączone, zablokowane lub ma wstrzymaną kolejkę."
                )
        return available

    async def posts(self, settings: Buffer):
        items, cursor = [], None
        while True:
            data = await self.request(
                "query($input: PostsInput!, $after: String) { posts(input: $input, first: 100, after: $after) { edges { node { "
                + POST_FIELDS
                + " } } pageInfo { hasNextPage endCursor } } }",
                {
                    "input": {
                        "organizationId": settings.organization_id,
                        "filter": {
                            "channelIds": [c.id for c in settings.channels],
                            "status": [
                                "draft",
                                "needs_approval",
                                "scheduled",
                                "sending",
                                "sent",
                                "error",
                            ],
                        },
                    },
                    "after": cursor,
                },
            )
            connection = data["posts"]
            items.extend(e["node"] for e in connection["edges"])
            page = connection["pageInfo"]
            if not page["hasNextPage"]:
                return items
            if not page["endCursor"] or page["endCursor"] == cursor:
                raise PermanentError("Buffer nie przesuwa strony kolejki.")
            cursor = page["endCursor"]

    async def create(self, request):
        data = await self.request(
            "mutation($input: CreatePostInput!) { createPost(input: $input) { __typename ... on PostActionSuccess { post { "
            + POST_FIELDS
            + " } } ... on MutationError { message } } }",
            {"input": request},
            mutation=True,
        )
        payload = data.get("createPost", {})
        if payload.get("__typename") == "PostActionSuccess" and isinstance(
            payload.get("post"), dict
        ):
            return payload["post"]
        if payload.get("message"):
            raise PermanentError(
                "Buffer odrzucił publikację; sprawdź format, uprawnienia i limity konta."
            )
        raise AmbiguousResult("Nieznany wynik tworzenia wpisu w Bufferze.")


def post_input(channel: BufferChannel, *, url: str, text: str, title: str, due_at=None):
    metadata = {}
    if channel.platform == "instagram":
        metadata = {
            "instagram": {"type": "reel", "shouldShareToFeed": channel.should_share_to_feed}
        }
    elif channel.platform == "youtube":
        metadata = {
            "youtube": {
                "title": title[:100],
                "categoryId": channel.category_id,
                "privacy": channel.privacy,
                "madeForKids": channel.made_for_kids,
            }
        }
    request = {
        "channelId": channel.id,
        "text": text,
        "schedulingType": "automatic",
        "mode": "addToQueue" if due_at is None else "customScheduled",
        "assets": [{"video": {"url": url}}],
        "metadata": metadata,
        "saveToDraft": False,
        "needsApproval": False,
    }
    if due_at is not None:
        request["dueAt"] = datetime.fromtimestamp(due_at, UTC).isoformat().replace("+00:00", "Z")
    return request


def remote_time(post):
    return (
        datetime.fromisoformat(post["dueAt"].replace("Z", "+00:00")).timestamp()
        if post.get("dueAt")
        else None
    )


def matches(post, request):
    return (
        post.get("channelId") == request["channelId"]
        and post.get("text") == request["text"]
        and (request.get("mode") == "addToQueue" or remote_time(post) == remote_time(request))
        and request["assets"][0]["video"]["url"]
        in [a.get("source") for a in post.get("assets", [])]
    )


def queue_fits_retention(channel, external, *, now, deadline):
    """Conservative check only: Buffer still chooses the actual slot.

    A free slot after the latest queued post bounds both filling a gap and
    appending to the queue. Do not guess or send this timestamp to Buffer.
    """
    zone = ZoneInfo(channel["timezone"])
    tail = max(
        [now]
        + [
            remote_time(p)
            for p in external
            if p.get("channelId") == channel["id"]
            and p.get("status") in ("scheduled", "sending")
            and remote_time(p) is not None
        ]
    )
    if tail >= deadline:
        return False
    schedules = {s["day"]: s for s in channel["postingSchedule"] if not s["paused"]}
    day = datetime.fromtimestamp(tail, zone).date()
    last_day = datetime.fromtimestamp(deadline, zone).date()
    while day <= last_day:
        for clock in schedules.get(
            ("mon", "tue", "wed", "thu", "fri", "sat", "sun")[day.weekday()], {}
        ).get("times", []):
            wall = datetime.combine(day, time.fromisoformat(clock), zone)
            # Test both folds; skip nonexistent times during a DST transition.
            for fold in (0, 1):
                candidate = wall.replace(fold=fold).timestamp()
                if datetime.fromtimestamp(candidate, zone).replace(tzinfo=None) != wall.replace(
                    tzinfo=None
                ):
                    continue
                if tail < candidate < deadline:
                    return True
        day += timedelta(days=1)
    return False
