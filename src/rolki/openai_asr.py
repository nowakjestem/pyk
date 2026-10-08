from __future__ import annotations

import json
import math
import os
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

import aiohttp

from .config import ASR
from .errors import PermanentError, TransientError
from .process import retry_network
from .subtitles import Cue, Word

ENDPOINT = "https://api.openai.com/v1/audio/transcriptions"
MAX_UPLOAD_BYTES = 24_000_000


def retry_after_seconds(value: str | None) -> float:
    if not value:
        return 0
    try:
        delay = float(value)
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            delay = (date - datetime.now(UTC)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return 0
    return max(0, delay) if math.isfinite(delay) else 0


def _times(item: dict, duration: float) -> tuple[float, float]:
    start, end = float(item["start"]), float(item["end"])
    if not (math.isfinite(start) and math.isfinite(end) and 0 <= start <= end):
        raise ValueError("invalid timestamps")
    return min(start, duration), min(end, duration)


def _match_words(text: str, words: tuple[Word, ...]) -> tuple[Word, ...]:
    """Keep segment punctuation even if the API's word list omits it."""
    tokens = text.split()

    def normalize(token):
        return re.sub(r"[^\w]", "", token).casefold()

    matched = []
    index = 0
    for token in tokens:
        expected = normalize(token)
        if not expected:
            return ()
        pieces = []
        combined = ""
        while index < len(words) and combined != expected:
            word = words[index]
            index += 1
            value = normalize(word.text)
            if not value:
                continue  # Punctuation must not extend a spoken word through a pause.
            combined += value
            if not expected.startswith(combined):
                return ()
            pieces.append(word)
        if combined != expected or not pieces:
            return ()
        # OpenAI may split a hyphenated surname into two timed entries.
        matched.append(Word(pieces[0].start, pieces[-1].end, token))
    if any(normalize(word.text) for word in words[index:]):
        return ()
    return tuple(matched)


def parse_openai(document: dict, *, offset: float = 0, duration: float) -> list[Cue]:
    """Convert verbose_json seconds to chapter-relative cues used by ASS/SRT."""
    try:
        if not isinstance(document, dict) or not isinstance(document.get("text"), str):
            raise ValueError("missing transcript")
        words = []
        try:
            previous_end = 0.0
            for item in document.get("words", []):
                start, end = _times(item, duration)
                text = item["word"].strip()
                if len(text.split()) != 1:
                    raise ValueError("invalid word")
                start = max(previous_end, start)
                end = max(start, end)
                words.append(Word(offset + start, offset + end, text))
                previous_end = end
        except (KeyError, ValueError, TypeError, AttributeError):
            words = []  # Valid segment timings still support whole-line backgrounds.

        segments = document.get("segments")
        if not segments:
            text = " ".join(document["text"].split())
            if not text:
                return []
            if not words or words[-1].end <= words[0].start:
                raise ValueError("missing timestamps")
            return [Cue(words[0].start, words[-1].end, text, _match_words(text, tuple(words)))]

        cues = []
        next_word = 0
        for segment in segments:
            start, end = _times(segment, duration)
            text = " ".join(segment["text"].split())
            if not text or end <= start:
                continue
            selected = []
            while next_word < len(words):
                word = words[next_word]
                if word.start >= offset + end:
                    break
                next_word += 1
                if word.end >= offset + start:
                    selected.append(
                        Word(
                            max(offset + start, word.start), min(offset + end, word.end), word.text
                        )
                    )
            cues.append(
                Cue(offset + start, offset + end, text, _match_words(text, tuple(selected)))
            )
        if document["text"].strip() and not cues:
            raise ValueError("missing valid segments")
        return cues
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise PermanentError("OpenAI zwróciło niepoprawną transkrypcję lub czasy napisów.") from exc


async def transcribe_audio(audio: Path, config: ASR) -> dict:
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        raise PermanentError("Brak OPENAI_API_KEY w .env.")
    if audio.stat().st_size > MAX_UPLOAD_BYTES:
        raise PermanentError(
            "Fragment audio przekracza limit uploadu OpenAI; zmniejsz max_chunk_seconds."
        )

    async def request():
        # A new file handle and multipart body are required for every retry.
        with audio.open("rb") as source:
            form = aiohttp.FormData()
            form.add_field("file", source, filename="audio.wav", content_type="audio/wav")
            form.add_field("model", config.model)
            if config.model == "gpt-transcribe":
                form.add_field("response_format", "json")
            else:
                form.add_field("response_format", "verbose_json")
                form.add_field("timestamp_granularities[]", "word")
                form.add_field("timestamp_granularities[]", "segment")
            if config.language != "auto":
                field = "languages[]" if config.model == "gpt-transcribe" else "language"
                form.add_field(field, config.language)
            if config.prompt:
                form.add_field("prompt", config.prompt)
            try:
                timeout = aiohttp.ClientTimeout(total=config.request_timeout_seconds)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(
                        ENDPOINT,
                        data=form,
                        headers={"Authorization": f"Bearer {key}"},
                        allow_redirects=False,
                    ) as response:
                        if response.status == 429:
                            error = {}
                            raw_error = bytearray()
                            async for chunk in response.content.iter_chunked(1024):
                                raw_error.extend(chunk)
                                if len(raw_error) > 4096:
                                    break
                            try:
                                if len(raw_error) <= 4096:
                                    error = json.loads(raw_error).get("error", {})
                            except (ValueError, AttributeError):
                                pass
                            if isinstance(error, dict):
                                billing_errors = {
                                    "credit_balance_exhausted": "Brak środków na koncie OpenAI. Doładuj saldo API.",
                                    "organization_spend_limit_exceeded": "Osiągnięto limit wydatków organizacji OpenAI.",
                                    "project_spend_limit_exceeded": "Osiągnięto limit wydatków projektu OpenAI.",
                                    "organization_usage_limit_exceeded": "Osiągnięto limit użycia konta OpenAI.",
                                    "billing_hard_limit_reached": "Osiągnięto limit wydatków konta OpenAI.",
                                    "insufficient_quota": "Brak środków lub przekroczony budżet konta OpenAI.",
                                }
                                message = billing_errors.get(error.get("code"))
                                if not message and error.get("type") == "insufficient_quota":
                                    message = billing_errors["insufficient_quota"]
                                if message:
                                    raise PermanentError(message)
                        if response.status in (408, 409, 429) or response.status >= 500:
                            raise TransientError(
                                f"OpenAI jest chwilowo niedostępne (HTTP {response.status}).",
                                retry_after=retry_after_seconds(
                                    response.headers.get("Retry-After")
                                ),
                            )
                        if response.status in (401, 403):
                            raise PermanentError("OpenAI odrzuciło klucz API lub jego uprawnienia.")
                        if response.status != 200:
                            raise PermanentError(
                                f"OpenAI odrzuciło transkrypcję (HTTP {response.status})."
                            )
                        # Bound response size and never include raw API errors in logs/posts.
                        raw = bytearray()
                        async for chunk in response.content.iter_chunked(65536):
                            raw.extend(chunk)
                            if len(raw) > 8 * 1024**2:
                                raise PermanentError(
                                    "Transkrypcja OpenAI przekracza limit rozmiaru odpowiedzi."
                                )
                        try:
                            document = json.loads(raw)
                            if not isinstance(document, dict):
                                raise ValueError("invalid response")
                            return document
                        except ValueError as exc:
                            raise PermanentError(
                                "OpenAI zwróciło niepoprawny JSON transkrypcji."
                            ) from exc
            except (aiohttp.ClientError, TimeoutError) as exc:
                raise TransientError("Błąd połączenia podczas transkrypcji OpenAI.") from exc

    return await retry_network(request)


def parse_text(document: dict, *, start: float, end: float) -> list[Cue]:
    """GPT supplies text only; these approximate bounds come from the audio window."""
    if not isinstance(document, dict) or not isinstance(document.get("text"), str):
        raise PermanentError("OpenAI zwróciło niepoprawny tekst transkrypcji.")
    text = " ".join(document["text"].split())
    return [Cue(start, end, text)] if text else []
