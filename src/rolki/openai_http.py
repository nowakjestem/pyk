from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import aiohttp

from .errors import PermanentError, TransientError


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


async def read_response(response: aiohttp.ClientResponse, *, operation: str) -> dict:
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
            retry_after=retry_after_seconds(response.headers.get("Retry-After")),
        )
    if response.status in (401, 403):
        raise PermanentError("OpenAI odrzuciło klucz API lub jego uprawnienia.")
    if response.status != 200:
        raise PermanentError(f"OpenAI odrzuciło {operation} (HTTP {response.status}).")
    # Bound response size and never include raw API errors in logs/posts.
    raw = bytearray()
    async for chunk in response.content.iter_chunked(65536):
        raw.extend(chunk)
        if len(raw) > 8 * 1024**2:
            raise PermanentError("OpenAI przekracza limit rozmiaru odpowiedzi.")
    try:
        document = json.loads(raw)
        if not isinstance(document, dict):
            raise ValueError("invalid response")
        return document
    except ValueError as exc:
        raise PermanentError("OpenAI zwróciło niepoprawny JSON odpowiedzi.") from exc
