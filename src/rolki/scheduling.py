from __future__ import annotations

import hashlib
import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .config import BufferSchedule


def choose_time(
    settings: BufferSchedule,
    now: datetime,
    seed: str,
    channels: list[str],
    occupied: list[tuple[str, float]],
) -> float | None:
    """Choose a daytime minute within the next 168 hours for all target accounts."""
    zone = ZoneInfo(settings.timezone)
    first_day = now.astimezone(zone).date()
    window_end = now.timestamp() + 7 * 24 * 3600
    last_day = datetime.fromtimestamp(window_end, zone).date()
    rng = random.Random(int.from_bytes(hashlib.sha256(seed.encode()).digest(), "big"))
    by_channel = {c: [t for cid, t in occupied if cid == c] for c in channels}
    days = []
    for offset in range((last_day - first_day).days + 1):
        day = first_day + timedelta(days=offset)
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
            if not now.timestamp() + settings.min_lead_minutes * 60 <= timestamp < window_end:
                continue
            if all(
                abs(timestamp - t) >= settings.min_gap_minutes * 60
                for ts in by_channel.values()
                for t in ts
            ):
                return timestamp
    return None
