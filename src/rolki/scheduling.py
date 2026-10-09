from __future__ import annotations

import hashlib
import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .config import BufferSchedule


def next_week(now: datetime, zone: ZoneInfo):
    local = now.astimezone(zone)
    return local.date() + timedelta(days=7 - local.weekday())


def choose_time(
    settings: BufferSchedule,
    now: datetime,
    seed: str,
    channels: list[str],
    occupied: list[tuple[str, float]],
) -> float | None:
    """Choose a minute in the next full local week, respecting each target account."""
    zone = ZoneInfo(settings.timezone)
    monday = next_week(now, zone)
    rng = random.Random(int.from_bytes(hashlib.sha256(seed.encode()).digest(), "big"))
    by_channel = {c: [t for cid, t in occupied if cid == c] for c in channels}
    days = []
    for offset in range(7):
        day = monday + timedelta(days=offset)
        counts = [
            sum(datetime.fromtimestamp(t, zone).date() == day for t in ts)
            for ts in by_channel.values()
        ]
        if max(counts, default=0) < settings.max_posts_per_day:
            days.append((max(counts, default=0), offset, day))
    for _, _, day in sorted(days):
        start = datetime.combine(day, settings.window_start, zone)
        end = datetime.combine(day, settings.window_end, zone)
        candidates = list(range(int((end - start).total_seconds() // 60)))
        rng.shuffle(candidates)
        for minute in candidates:
            candidate = start + timedelta(minutes=minute)
            timestamp = candidate.timestamp()
            # Reject imaginary wall-clock times in a user-configured DST window.
            if datetime.fromtimestamp(timestamp, zone).replace(tzinfo=None) != candidate.replace(
                tzinfo=None
            ):
                continue
            if timestamp < now.timestamp() + settings.min_lead_minutes * 60:
                continue
            if all(
                abs(timestamp - t) >= settings.min_gap_minutes * 60
                for ts in by_channel.values()
                for t in ts
            ):
                return timestamp
    return None
