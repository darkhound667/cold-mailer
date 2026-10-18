# -*- coding: utf-8 -*-
"""Phase 3: pick the day an email goes out.

Each email goes at 08:30 in the professor's local time on the earliest valid day:
  - not a Saturday or Sunday;
  - not a national public holiday in the professor's country (`holidays` library);
  - not inside the Christmas–New Year break, 24 Dec to 1 Jan (decision Q9b);
  - far enough ahead that the sender can pick it up (MIN_LEAD).
The minute inside a busy slot (08:30, 08:31, ...) is assigned afterwards by slots.py.

Missed slots (SPEC amendment 6): an email may still go out up to one hour after its own
scheduled minute; later than that it rolls to the next valid day (`reschedule_missed`).

Only national holidays are used: state or provincial holidays (e.g. in the US, Canada,
Australia, Germany) are not skipped.
"""
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import holidays

SEND_TIME = time(8, 30)
MIN_LEAD = timedelta(hours=2)        # never schedule a slot that starts sooner than this
LATE_WINDOW = timedelta(hours=1)     # amendment 6
SEARCH_DAYS = 60                     # give up (and flag) if no valid day in this window
_cache = {}


def _holidays(country: str, year: int):
    key = (country, year)
    if key not in _cache:
        _cache[key] = holidays.country_holidays(country, years=[year])
    return _cache[key]


def is_valid_day(day: date, country: str) -> bool:
    """True if `day` is a working day for sending to `country`."""
    if day.weekday() >= 5:
        return False
    if (day.month == 12 and day.day >= 24) or (day.month == 1 and day.day == 1):
        return False
    return day not in _holidays(country, day.year)


def next_slot(tz_name: str, country: str, after_utc: datetime) -> datetime:
    """The first 08:30 local (as an aware UTC datetime) on a valid day, at least MIN_LEAD
    after `after_utc`. Raises ValueError if none is found within SEARCH_DAYS."""
    if after_utc.tzinfo is None:
        raise ValueError("after_utc must be timezone-aware")
    tz = ZoneInfo(tz_name)
    earliest = after_utc + MIN_LEAD
    day = earliest.astimezone(tz).date()
    for _ in range(SEARCH_DAYS):
        if is_valid_day(day, country):
            # Building the local time from the date handles DST: 08:30 is always wall-clock 08:30.
            slot = datetime.combine(day, SEND_TIME, tzinfo=tz)
            if slot >= earliest:
                return slot.astimezone(timezone.utc)
        day += timedelta(days=1)
    raise ValueError(f"no valid send day within {SEARCH_DAYS} days for {country} / {tz_name}")


def reschedule_missed(send_utc: datetime, tz_name: str, country: str, now_utc: datetime):
    """For an unsent email at `now_utc`: None if it can still go (on time or within the late
    window), else the new slot on the next valid day."""
    if now_utc <= send_utc + LATE_WINDOW:
        return None
    return next_slot(tz_name, country, now_utc)
