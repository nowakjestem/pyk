from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from zoneinfo import ZoneInfo


def current_output_date() -> str:
    return datetime.now(ZoneInfo("Europe/Warsaw")).date().isoformat()


def clip_filename(title: str, variant: str, output_date: str) -> str:
    label = {"crop": "crop", "letterbox": "letterboxed"}[variant]
    normalized = unicodedata.normalize("NFKD", title.lower().replace("ł", "l"))
    ascii_title = normalized.encode("ascii", errors="ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_title).strip("-")[:160].rstrip("-")
    slug = slug or "rozdzial"
    return f"{date.fromisoformat(output_date).isoformat()}-{slug}-{label}.mp4"
