import asyncio
import json
import struct
import wave

import pytest
from aiohttp import web
from test_openai_asr import server

from rolki import media
from rolki.config import ASR
from rolki.errors import PermanentError, TransientError
from rolki.openai_asr import parse_text, transcribe_audio
from rolki.subtitles import Cue, write_subtitles


def pcm(path, seconds, pauses=()):
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        for tick in range(round(seconds * 50)):
            time = tick / 50
            value = 0 if any(start <= time < end for start, end in pauses) else 1000
            audio.writeframesraw(struct.pack("<320h", *([value] * 320)))


async def test_gpt_request_has_only_text_parameters(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    audio = tmp_path / "audio.wav"
    pcm(audio, 1)
    fields = []

    async def handler(request):
        reader = await request.multipart()
        while part := await reader.next():
            fields.append((part.name, bytes(await part.read())))
        return web.json_response({"text": "Zażółć gęślą jaźń.", "languages": [{"code": "pl"}]})

    async with server(handler, monkeypatch):
        doc = await transcribe_audio(audio, ASR(provider="openai", model="gpt-transcribe"))
    assert doc["text"] == "Zażółć gęślą jaźń."
    assert ("model", b"gpt-transcribe") in fields
    assert ("response_format", b"json") in fields
    assert ("languages[]", b"pl") in fields
    assert not any(name in ("language", "timestamp_granularities[]") for name, _ in fields)


def test_text_has_window_bounds_and_never_word_times(tmp_path, config):
    cues = parse_text(
        {"text": " Żółć   i gęś! ", "words": [{"word": "fake", "start": 0, "end": 1}]},
        start=300,
        end=308,
    )
    assert cues == [Cue(300, 308, "Żółć i gęś!")]
    assert parse_text({"text": "  "}, start=0, end=8) == []
    write_subtitles(
        cues,
        tmp_path,
        config.subtitles.model_copy(
            update={"background": config.subtitles.background.model_copy(update={"mode": "line"})}
        ),
        config.video,
    )
    for variant in ("crop", "letterbox"):
        ass = (tmp_path / f"{variant}.ass").read_text()
        assert "Żółć" in ass and ",Box,," in ass
        assert r"\3a&HFF&" not in ass  # No per-word switching.


@pytest.mark.parametrize("document", [None, {}, {"text": []}, {"text": 3}])
def test_invalid_text_is_rejected(document):
    with pytest.raises(PermanentError):
        parse_text(document, start=0, end=8)


def test_windows_use_pause_and_cover_each_sample_once(tmp_path):
    audio = tmp_path / "speech.wav"
    pcm(audio, 20, pauses=[(7, 7.4)])
    windows = media.text_audio_windows(audio, 8)
    assert windows[0] == (0, 115200)  # Center of the pause at 7.2 s.
    assert windows[-1][1] == 20 * 16000
    assert all(first < last for first, last in windows)
    assert all(left[1] == right[0] for left, right in zip(windows, windows[1:], strict=False))


def test_continuous_speech_has_bounded_windows_and_no_tiny_tail(tmp_path):
    audio = tmp_path / "speech.wav"
    pcm(audio, 17)
    assert media.text_audio_windows(audio, 8) == [(0, 128000), (128000, 272000)]


async def test_gpt_chunks_resume_without_whisper_cache_or_paid_duplicates(
    tmp_path, config, monkeypatch
):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    api = config.asr.model_copy(
        update={"provider": "openai", "model": "gpt-transcribe", "text_chunk_seconds": 8}
    )
    config = config.model_copy(update={"asr": api})
    audio = tmp_path / "audio.wav"
    pcm(audio, 17)
    # A legacy cached Whisper response must not replace the GPT transcript.
    (tmp_path / "openai-00000.json").write_text(json.dumps({"text": "old whisper"}))
    calls = 0
    failing = True

    async def request(path, _config):
        nonlocal calls
        calls += 1
        assert path.name.startswith("text-window-")
        if calls == 2 and failing:
            raise TransientError("temporary")
        return {"text": "Nowy tekst GPT."}

    monkeypatch.setattr(media, "transcribe_audio", request)
    with pytest.raises(TransientError):
        await media.transcribe_text_windows(audio, tmp_path, config, 300, 17)
    assert not list(tmp_path.glob("text-window-*.wav"))
    failing = False
    cues = await media.transcribe_text_windows(audio, tmp_path, config, 300, 17)
    assert calls == 3
    assert cues == [Cue(300, 308, "Nowy tekst GPT."), Cue(308, 317, "Nowy tekst GPT.")]
    assert not list(tmp_path.glob("text-window-*.wav"))


async def test_gpt_pipeline_dispatch_does_not_use_whisper_parser(tmp_path, config, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    config = config.model_copy(
        update={
            "asr": config.asr.model_copy(update={"provider": "openai", "model": "gpt-transcribe"})
        }
    )

    async def extract(_source, destination, _start, _duration):
        pcm(destination, 8)

    async def request(*_):
        return {"text": "Tylko tekst."}

    def never_whisper(*_, **__):
        pytest.fail("GPT must not require timestamped Whisper JSON")

    monkeypatch.setattr(media, "extract_audio", extract)
    monkeypatch.setattr(media, "transcribe_audio", request)
    monkeypatch.setattr(media, "parse_openai", never_whisper)
    cues = await media.transcribe(
        tmp_path / "source.mp4", {"start": 12, "end": 20}, tmp_path, config
    )
    assert cues == [Cue(0, 8, "Tylko tekst.")]
    assert not (tmp_path / "audio.wav").exists()


async def test_silent_windows_are_cached_without_api(tmp_path, config, monkeypatch):
    config = config.model_copy(
        update={"asr": config.asr.model_copy(update={"model": "gpt-transcribe"})}
    )
    audio = tmp_path / "silent.wav"
    pcm(audio, 8, pauses=[(0, 8)])

    async def never(*_):
        pytest.fail("Silence must not be uploaded")

    monkeypatch.setattr(media, "transcribe_audio", never)
    assert await media.transcribe_text_windows(audio, tmp_path, config, 0, 8) == []
    assert json.loads((tmp_path / "gpt-transcribe-0000000000.json").read_text()) == {"text": ""}


async def test_parallel_windows_limit_order_and_isolated_audio(tmp_path, config, monkeypatch):
    config = config.model_copy(update={"asr": config.asr.model_copy(update={"concurrency": 2})})
    audio = tmp_path / "audio.wav"
    with wave.open(str(audio), "wb") as output:
        output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        for index in range(4):
            output.writeframesraw(struct.pack("<h", 1000 * (index + 1)) * 128000)
    second_done, release_first = asyncio.Event(), asyncio.Event()
    active = peak = 0
    completed = []

    async def request(path, _):
        nonlocal active, peak
        index = int(path.stem.rsplit("-", 1)[1]) // 128000
        active += 1
        peak = max(peak, active)
        assert len(list(tmp_path.glob("text-window-*.wav"))) <= 2
        try:
            if index == 0:
                await second_done.wait()
                await release_first.wait()
            elif index == 1:
                second_done.set()
            elif index == 2:
                release_first.set()
            with wave.open(str(path), "rb") as segment:
                assert segment.getnframes() == 128000
                assert struct.unpack("<h", segment.readframes(1))[0] == 1000 * (index + 1)
            completed.append(index)
            return {"text": f"Fragment {index}."}
        finally:
            active -= 1

    monkeypatch.setattr(media, "transcribe_audio", request)
    cues = await asyncio.wait_for(media.transcribe_text_windows(audio, tmp_path, config, 0, 32), 2)
    assert peak == 2 and completed[0] == 1
    assert cues == [Cue(index * 8, (index + 1) * 8, f"Fragment {index}.") for index in range(4)]
    assert len(list(tmp_path.glob("gpt-transcribe-*.json"))) == 4
    assert not list(tmp_path.glob("text-window-*.wav"))


async def test_parallel_failure_drains_tasks_and_reuses_completed_cache(
    tmp_path, config, monkeypatch
):
    config = config.model_copy(update={"asr": config.asr.model_copy(update={"concurrency": 3})})
    audio = tmp_path / "audio.wav"
    pcm(audio, 24)
    second_done, cancelled = asyncio.Event(), asyncio.Event()

    async def request(path, _):
        index = int(path.stem.rsplit("-", 1)[1]) // 128000
        if index == 1:
            second_done.set()
            return {"text": "Fragment 1."}
        await second_done.wait()
        if index == 2:
            raise PermanentError("API failure")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(media, "transcribe_audio", request)
    with pytest.raises(PermanentError, match="API failure"):
        await asyncio.wait_for(media.transcribe_text_windows(audio, tmp_path, config, 0, 24), 2)
    assert cancelled.is_set()
    assert not list(tmp_path.glob("text-window-*.wav"))
    assert len(list(tmp_path.glob("gpt-transcribe-*.json"))) == 1
    requested = []

    async def retry(path, _):
        index = int(path.stem.rsplit("-", 1)[1]) // 128000
        requested.append(index)
        return {"text": f"Fragment {index}."}

    monkeypatch.setattr(media, "transcribe_audio", retry)
    cues = await media.transcribe_text_windows(audio, tmp_path, config, 0, 24)
    assert requested == [0, 2]  # The second fragment completed before the first.
    assert cues == [Cue(index * 8, (index + 1) * 8, f"Fragment {index}.") for index in range(3)]


async def test_parallel_cancellation_waits_for_cleanup(tmp_path, config, monkeypatch):
    config = config.model_copy(update={"asr": config.asr.model_copy(update={"concurrency": 2})})
    audio = tmp_path / "audio.wav"
    pcm(audio, 24)
    both_started = asyncio.Event()
    active = 0

    async def request(*_):
        nonlocal active
        active += 1
        if active == 2:
            both_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            active -= 1

    monkeypatch.setattr(media, "transcribe_audio", request)
    task = asyncio.create_task(media.transcribe_text_windows(audio, tmp_path, config, 0, 24))
    try:
        await asyncio.wait_for(both_started.wait(), 2)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert active == 0
    assert not list(tmp_path.glob("text-window-*.wav"))
    assert not list(tmp_path.glob("gpt-transcribe-*.json"))
