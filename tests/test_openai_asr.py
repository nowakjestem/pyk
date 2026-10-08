import asyncio
import json
from contextlib import asynccontextmanager

import pytest
from aiohttp import web
from pydantic import ValidationError

from rolki import media, openai_asr
from rolki.config import ASR, Config
from rolki.errors import PermanentError, TransientError
from rolki.openai_asr import parse_openai, transcribe_audio
from rolki.subtitles import Cue, Word, cue_from_dict, write_subtitles


def transcript():
    return {
        "text": "Zażółć gęślą. Jaźń!",
        "segments": [
            {"start": 0, "end": 2, "text": " Zażółć gęślą."},
            {"start": 3, "end": 4, "text": "Jaźń!"},
        ],
        "words": [
            {"start": 0.1, "end": 0.5, "word": "Zażółć"},
            {"start": 0.8, "end": 1.5, "word": "gęślą"},
            {"start": 3.1, "end": 4.2, "word": "Jaźń"},
        ],
    }


@asynccontextmanager
async def server(handler, monkeypatch):
    app = web.Application()
    app.router.add_post("/audio/transcriptions", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    monkeypatch.setattr(openai_asr, "ENDPOINT", f"http://127.0.0.1:{port}/audio/transcriptions")
    try:
        yield
    finally:
        await runner.cleanup()


def test_openai_polish_punctuation_offsets_and_clip_boundary(tmp_path, config):
    cues = parse_openai(transcript(), offset=300, duration=3.8)
    assert cues == [
        Cue(
            300, 302, "Zażółć gęślą.", (Word(300.1, 300.5, "Zażółć"), Word(300.8, 301.5, "gęślą."))
        ),
        Cue(303, 303.8, "Jaźń!", (Word(303.1, 303.8, "Jaźń!"),)),
    ]
    style = config.subtitles.model_copy(
        update={"background": config.subtitles.background.model_copy(update={"mode": "word"})}
    )
    write_subtitles(cues, tmp_path, style, config.video)
    for variant in ("crop", "letterbox"):
        ass = (tmp_path / f"{variant}.ass").read_text()
        assert "Lato" in ass and "Zażółć" in ass
        assert "0:05:00.10,0:05:00.50,Box" in ass
        assert "0:05:03.10,0:05:03.80,Box" in ass


@pytest.mark.parametrize(
    "words", [None, [{"word": "bad"}], [{"word": "x", "start": 0, "end": float("nan")}], []]
)
def test_invalid_words_preserve_segment_text(words):
    document = transcript() | {"words": words}
    assert parse_openai(document, duration=4) == [Cue(0, 2, "Zażółć gęślą."), Cue(3, 4, "Jaźń!")]


def test_zero_word_duration_keeps_phrase():
    document = {
        "text": "test",
        "segments": [{"start": 0, "end": 1, "text": "test"}],
        "words": [{"start": 0, "end": 0, "word": "test"}],
    }
    assert parse_openai(document, duration=1) == [Cue(0, 1, "test", (Word(0, 0, "test"),))]


def test_words_only_response_and_silence():
    document = transcript()
    document.pop("segments")
    cue = parse_openai(document, duration=4)[0]
    assert cue.words[-1] == Word(3.1, 4, "Jaźń!")
    assert parse_openai({"text": "", "segments": [], "words": []}, duration=2) == []


@pytest.mark.parametrize(
    "document",
    [
        None,
        [],
        {},
        {"text": "speech"},
        {"text": "speech", "segments": [{"start": 0, "end": float("inf"), "text": "speech"}]},
        {"text": "speech", "segments": [{"start": 3, "end": 1, "text": "speech"}]},
    ],
)
def test_invalid_response_never_invents_timing(document):
    with pytest.raises(PermanentError, match="czasy"):
        parse_openai(document, duration=4)


async def test_multipart_fields_and_retries_reopen_same_audio(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-api-key")
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"audio-body")
    requests = []

    async def handler(request):
        reader = await request.multipart()
        fields = []
        while part := await reader.next():
            fields.append((part.name, bytes(await part.read()), part.filename))
        requests.append((request.headers["Authorization"], fields))
        return web.json_response(transcript(), status=503 if len(requests) < 3 else 200)

    async def no_delay(_):
        pass

    monkeypatch.setattr("rolki.process.asyncio.sleep", no_delay)
    async with server(handler, monkeypatch):
        result = await transcribe_audio(audio, ASR(provider="openai", prompt="Zażółć"))
    assert result == transcript() and len(requests) == 3
    assert all(item == requests[0] for item in requests)
    auth, fields = requests[0]
    assert auth == "Bearer test-api-key"
    assert ("file", b"audio-body", "audio.wav") in fields
    assert ("model", b"whisper-1", None) in fields
    assert ("response_format", b"verbose_json", None) in fields
    assert ("language", b"pl", None) in fields
    assert ("timestamp_granularities[]", b"word", None) in fields
    assert ("timestamp_granularities[]", b"segment", None) in fields
    assert ("prompt", "Zażółć".encode(), None) in fields


@pytest.mark.parametrize(
    "status,code,error_type,attempts",
    [
        (401, "bad_key", PermanentError, 1),
        (403, "bad_key", PermanentError, 1),
        (400, "bad_request", PermanentError, 1),
        (404, "model_not_found", PermanentError, 1),
        (302, "redirect", PermanentError, 1),
        (429, "insufficient_quota", PermanentError, 1),
        (429, "rate_limit_exceeded", TransientError, 3),
        (500, "server_error", TransientError, 3),
    ],
)
async def test_errors_retry_only_transient_without_leaking_api_body(
    tmp_path, monkeypatch, status, code, error_type, attempts
):
    monkeypatch.setenv("OPENAI_API_KEY", "test-secret")
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"audio")
    calls = 0

    async def handler(request):
        nonlocal calls
        await request.read()
        calls += 1
        return web.json_response(
            {"error": {"code": code, "message": "test-secret private content"}}, status=status
        )

    async def no_delay(_):
        pass

    monkeypatch.setattr("rolki.process.asyncio.sleep", no_delay)
    async with server(handler, monkeypatch):
        with pytest.raises(error_type) as exc:
            await transcribe_audio(audio, ASR(provider="openai"))
    assert calls == attempts
    assert "test-secret" not in str(exc.value) and "private content" not in str(exc.value)


async def test_auto_language_and_malformed_json(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"audio")
    names = []

    async def handler(request):
        reader = await request.multipart()
        while part := await reader.next():
            names.append(part.name)
            await part.read()
        return web.Response(text="invalid-json")

    async with server(handler, monkeypatch):
        with pytest.raises(PermanentError, match="JSON"):
            await transcribe_audio(audio, ASR(provider="openai", language="auto"))
    assert "language" not in names and "prompt" not in names


async def test_timeout_is_transient(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"audio")

    async def handler(request):
        await request.read()
        await asyncio.sleep(0.05)
        return web.json_response(transcript())

    # Avoid retry sleeps without changing aiohttp's running event loop.
    async def one_attempt(operation):
        return await operation()

    monkeypatch.setattr(openai_asr, "retry_network", one_attempt)
    async with server(handler, monkeypatch):
        with pytest.raises(TransientError, match="połączenia"):
            await transcribe_audio(
                audio, ASR(provider="openai").model_copy(update={"request_timeout_seconds": 0.01})
            )


async def test_missing_key_and_upload_limit_fail_before_request(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(PermanentError, match="OPENAI_API_KEY"):
        await transcribe_audio(tmp_path / "nonexistent", ASR(provider="openai"))
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    audio = tmp_path / "big.wav"
    with audio.open("wb") as file:
        file.truncate(openai_asr.MAX_UPLOAD_BYTES + 1)
    with pytest.raises(PermanentError, match="limit uploadu"):
        await transcribe_audio(audio, ASR(provider="openai"))


async def test_resume_caches_paid_chunks_and_preserves_relative_word_times(
    tmp_path, config, monkeypatch
):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    config = config.model_copy(
        update={
            "asr": config.asr.model_copy(update={"provider": "openai", "max_chunk_seconds": 30})
        }
    )
    calls = 0
    fail = True

    async def extract(_source, audio, start, duration):
        audio.write_bytes(f"{start}:{duration}".encode())

    async def request(_audio, _config):
        nonlocal calls
        calls += 1
        if fail and calls == 2:
            raise TransientError("temporary")
        return {
            "text": "słowo",
            "segments": [{"start": 0, "end": 1, "text": "słowo"}],
            "words": [{"start": 0.1, "end": 0.5, "word": "słowo"}],
        }

    monkeypatch.setattr(media, "extract_audio", extract)
    monkeypatch.setattr(media, "silent_audio", lambda _: False)
    monkeypatch.setattr(media, "transcribe_audio", request)
    with pytest.raises(TransientError):
        await media.transcribe(tmp_path / "source", {"start": 12, "end": 44}, tmp_path, config)
    assert (tmp_path / "openai-00000.json").is_file()
    assert not (tmp_path / "audio.wav").exists()
    fail = False
    cues = await media.transcribe(tmp_path / "source", {"start": 12, "end": 44}, tmp_path, config)
    assert calls == 3  # The first paid chunk was not requested again.
    assert cues[1].words == (Word(30.1, 30.5, "słowo"),)
    assert cue_from_dict(json.loads((tmp_path / "transcript.json").read_text())[1]) == cues[1]
    assert not (tmp_path / "audio.wav").exists()
    assert not (tmp_path / "whisper.json").exists()


async def test_openai_silence_does_not_call_api(tmp_path, config, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    config = config.model_copy(update={"asr": config.asr.model_copy(update={"provider": "openai"})})

    async def extract(*_):
        pass

    async def never_call(*_):
        pytest.fail("silent audio must not be uploaded")

    monkeypatch.setattr(media, "extract_audio", extract)
    monkeypatch.setattr(media, "silent_audio", lambda _: True)
    monkeypatch.setattr(media, "transcribe_audio", never_call)
    assert (
        await media.transcribe(tmp_path / "source", {"start": 0, "end": 1}, tmp_path, config) == []
    )


def test_provider_validation_legacy_jobs_and_secret_snapshot(config, db, monkeypatch):
    assert Config.model_validate_json(config.model_dump_json()).asr.provider == "local"
    with pytest.raises(ValidationError):
        ASR(provider="openai", model="gpt-transcribe")  # Does not supply word timestamps.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    api_config = config.model_copy(
        update={"asr": config.asr.model_copy(update={"provider": "openai"})}
    )
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        api_config.require_asr()
    monkeypatch.setenv("OPENAI_API_KEY", "never-in-database")
    api_config.require_asr()
    job_id = db.enqueue(
        post_id="p", video_id="abcdefghijk", url="https://youtu.be/abcdefghijk", config=api_config
    )
    snapshot = db.get(job_id)["config_json"]
    assert "never-in-database" not in snapshot
    assert Config.model_validate_json(snapshot).asr.provider == "openai"


async def test_tools_check_openai_does_not_need_local_model(config, monkeypatch, capsys):
    from rolki import cli

    api_config = config.model_copy(
        update={"asr": config.asr.model_copy(update={"provider": "openai"})}
    )
    tools = []

    def which(binary):
        tools.append(binary)
        assert binary not in (api_config.asr.binary, api_config.asr.quantizer)
        return f"/usr/bin/{binary}"

    async def process(args, **_):
        return "subtitles" if "-filters" in args else "Lato\n"

    monkeypatch.setattr(cli.shutil, "which", which)
    monkeypatch.setattr(cli, "run_process", process)
    args = cli.parser().parse_args(["check", "--tools"])
    assert await cli.async_main(args, api_config) == 0
    assert "Konfiguracja poprawna" in capsys.readouterr().out
    assert tools == ["ffmpeg", "ffprobe", "deno", "fc-list"]
