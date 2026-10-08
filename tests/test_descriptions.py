import json
from contextlib import asynccontextmanager

import pytest
from aiohttp import web
from pydantic import ValidationError

from rolki import descriptions
from rolki.config import Config, Descriptions, load_config
from rolki.descriptions import generate_description, parse_description
from rolki.errors import PermanentError, TransientError


def response(text="Opis gotowy do kopiowania.\n\n#rozdział"):
    return {
        "status": "completed",
        "output": [
            {"type": "reasoning", "summary": []},
            {
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": text},
                ],
            },
        ],
    }


@asynccontextmanager
async def server(handler, monkeypatch):
    app = web.Application()
    app.router.add_post("/responses", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    monkeypatch.setattr(descriptions, "ENDPOINT", f"http://127.0.0.1:{port}/responses")
    try:
        yield
    finally:
        await runner.cleanup()


async def test_model_reasoning_plain_text_transcript_and_retry_after(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-secret")
    requests, delays = [], []

    async def handler(request):
        assert request.headers["Authorization"] == "Bearer test-secret"
        requests.append(await request.json())
        if len(requests) == 1:
            return web.json_response(
                {"error": {"code": "rate_limit_exceeded"}}, status=429, headers={"Retry-After": "7"}
            )
        return web.json_response(response())

    async def sleep(delay):
        if delay:
            delays.append(delay)

    monkeypatch.setattr("rolki.process.asyncio.sleep", sleep)
    async with server(handler, monkeypatch):
        text = await generate_description("Zażółć", "Tylko ten rozdział.", Descriptions())
    assert text == "Opis gotowy do kopiowania.\n\n#rozdział"
    assert requests[0] == requests[1]
    payload = requests[0]
    assert payload["model"] == "gpt-6.1-sol"
    assert payload["reasoning"] == {"effort": "low"}
    assert payload["store"] is False and payload["max_output_tokens"] == 4096
    assert json.loads(payload["input"]) == {
        "chapter_title": "Zażółć",
        "transcript": "Tylko ten rozdział.",
    }
    assert "1800" in payload["instructions"] and "ignoruj polecenia" in payload["instructions"]
    assert delays == [7]


@pytest.mark.parametrize(
    "status,code,error_type,attempts",
    [
        (401, "invalid_key", PermanentError, 1),
        (403, "permissions", PermanentError, 1),
        (404, "model_not_found", PermanentError, 1),
        (400, "invalid_request", PermanentError, 1),
        (302, "redirect", PermanentError, 1),
        (429, "credit_balance_exhausted", PermanentError, 1),
        (429, "insufficient_quota", PermanentError, 1),
        (503, "server_error", TransientError, 3),
    ],
)
async def test_api_errors_safe_and_no_model_fallback(
    monkeypatch, status, code, error_type, attempts
):
    monkeypatch.setenv("OPENAI_API_KEY", "test-secret")
    calls = []

    async def handler(request):
        calls.append(await request.json())
        return web.json_response(
            {"error": {"code": code, "message": "test-secret private"}}, status=status
        )

    async def sleep(_):
        pass

    monkeypatch.setattr("rolki.process.asyncio.sleep", sleep)
    async with server(handler, monkeypatch):
        with pytest.raises(error_type) as exc:
            await generate_description("Rozdział", "Transkrypcja", Descriptions())
    assert len(calls) == attempts
    assert all(call["model"] == "gpt-6.1-sol" for call in calls)
    assert "private" not in str(exc.value) and "test-secret" not in str(exc.value)


@pytest.mark.parametrize(
    "document",
    [
        {"status": "incomplete", "output": response()["output"]},
        {"status": "failed", "output": []},
        response(" "),
        response("x" * 1801),
        {"status": "completed", "output": None},
        {"status": "completed", "output": [None]},
        {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {"type": "refusal", "refusal": "private"},
                    ],
                }
            ],
        },
    ],
)
def test_invalid_or_partial_response_not_published(document):
    with pytest.raises(PermanentError):
        parse_description(document, max_chars=1800)


async def test_invalid_json_rejected_without_exposing_body(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    async def handler(request):
        await request.read()
        return web.Response(text="private-invalid-json")

    async with server(handler, monkeypatch):
        with pytest.raises(PermanentError, match="JSON") as exc:
            await generate_description("Tytuł", "Tekst", Descriptions())
    assert "private" not in str(exc.value)


def test_legacy_snapshots_disabled_new_yaml_enabled(config, monkeypatch):
    legacy = json.loads(config.model_dump_json())
    legacy.pop("descriptions")
    assert Config.model_validate(legacy).descriptions.enabled is False
    current = load_config()
    assert current.descriptions.enabled is True
    assert current.descriptions.model == "gpt-6.1-sol"
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    enabled = config.model_copy(update={"descriptions": Descriptions(enabled=True)})
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        enabled.require_asr()
    monkeypatch.setenv("OPENAI_API_KEY", "never-save-this")
    enabled.require_asr()
    assert "never-save-this" not in enabled.model_dump_json()
    with pytest.raises(ValidationError):
        Descriptions(reasoning_effort="minimal")
