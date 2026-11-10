"""Latest eligible slots survive delayed dispatcher starts. Session dates come from XNYS."""

from datetime import datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

from .models import utc

JOBS = (
    "calendar_sync",
    "sec_poll",
    "news_scan",
    "crypto_scan",
    "event_check",
    "paper_weekly",
    "premarket_brief",
    "after_close",
    "source_health",
)


@lru_cache(maxsize=4)
def calendar(year):
    import exchange_calendars

    return exchange_calendars.get_calendar("XNYS", start=f"{year-1}-01-01", end=f"{year+1}-12-31")


def week_window(now, zone):
    local = now.astimezone(zone)
    end = datetime.combine(local.date() - timedelta(days=local.weekday()), time(), zone)
    return utc(end - timedelta(days=7)), utc(end)


def eligible(now, zone):
    local = now.astimezone(zone)
    day = local.date()
    start = datetime.combine(day, time(), zone)
    minutes = local.hour * 60 + local.minute
    slots = {}
    for job, interval in (("news_scan", 120), ("crypto_scan", 240)):
        slots[job] = utc(start + timedelta(minutes=minutes // interval * interval))
    for job, minute in (("calendar_sync", 450), ("source_health", 365)):
        if minutes >= minute:
            slots[job] = utc(start + timedelta(minutes=minute))
    # The research period is the preceding Mon–Sun. Catch up through this week.
    monday = start - timedelta(days=local.weekday()) + timedelta(hours=8)
    if local >= monday:
        slots["paper_weekly"] = utc(monday)
    # Market-linked jobs always follow New York wall time, even if the editorial zone changes.
    ny = now.astimezone(ZoneInfo("America/New_York"))
    market_minutes = ny.hour * 60 + ny.minute
    if ny.weekday() < 5 and 360 <= market_minutes < 1335:
        slot = min(1320, market_minutes // 15 * 15)
        slots["sec_poll"] = utc(ny.replace(hour=slot // 60, minute=slot % 60, second=0, microsecond=0))
    if calendar(ny.year).is_session(ny.date().isoformat()):
        for job, hour, minute in (("premarket_brief", 9, 10), ("after_close", 16, 40)):
            slot = ny.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if ny >= slot:
                slots[job] = utc(slot)
    return slots
