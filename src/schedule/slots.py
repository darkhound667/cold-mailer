# -*- coding: utf-8 -*-
"""Per-minute send spacing for emails that share the same 08:30 slot.

Implements "SPEC.md amendment 12". Emails due at the same 08:30 must NOT all go out at
once; they go out one per minute. With 10 emails due at 08:30 the first leaves at 08:30,
the second at 08:31, ... the last at 08:39. With 100 (the maximum batch) the last leaves
at 10:09.

Rules:
  1. Grouping is by UTC instant. Emails whose slot start is the same instant form ONE
     sequence even if their timezones differ (Europe/Paris and Europe/Berlin both have
     08:30 at 07:30 UTC in winter). Different instants are spaced independently.
  2. Order inside a sequence is deterministic: `queue_index` ascending, ties by `id`.
  3. Email k (0-based) gets send_utc = slot_utc + k * gap (default gap = 1 minute), and
     send_local = send_utc in that email's own IANA timezone.
  4. The late window is per email: see `is_due`. Rolling "missed" emails to the next valid
     day is NOT done here; a separate module handles that.

All datetimes must be timezone-aware (naive ones raise ValueError). Standard library only.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


def _require_aware(value: datetime, name: str) -> datetime:
    """Return `value` unchanged, or raise ValueError if it is not a timezone-aware datetime."""
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime, got {value!r}")
    return value


def assign_send_times(emails, gap=timedelta(minutes=1)):
    """Spread emails over their shared slots, one per `gap`.

    `emails`: iterable of dicts with keys id (str), queue_index (int), timezone (IANA str)
    and slot_utc (aware datetime). Returns a NEW list of dicts (input untouched): the
    original keys plus send_utc, send_local (aware, in the email's zone) and position
    (0-based index within its sequence), sorted by send_utc then id.
    """
    if gap <= timedelta(0):
        raise ValueError("gap must be a positive timedelta")

    # Group by UTC instant, so equal instants expressed in different zones share a sequence.
    groups = {}
    for email in emails:
        slot = _require_aware(email["slot_utc"], "slot_utc").astimezone(timezone.utc)
        groups.setdefault(slot, []).append(email)

    out = []
    for slot, members in groups.items():
        members.sort(key=lambda e: (e["queue_index"], e["id"]))
        for position, email in enumerate(members):
            send_utc = slot + position * gap  # absolute-time arithmetic, immune to DST
            row = dict(email)
            row["send_utc"] = send_utc
            row["send_local"] = send_utc.astimezone(ZoneInfo(email["timezone"]))
            row["position"] = position
            out.append(row)

    out.sort(key=lambda e: (e["send_utc"], e["id"]))
    return out


def is_due(now_utc, send_utc, late_window=timedelta(hours=1)):
    """Classify one email against the clock: "wait", "send" or "missed".

    "wait" if now < send_utc; "send" if send_utc <= now <= send_utc + late_window
    (both ends inclusive); "missed" if now is later than that.
    """
    _require_aware(now_utc, "now_utc")
    _require_aware(send_utc, "send_utc")
    if now_utc < send_utc:
        return "wait"
    if now_utc <= send_utc + late_window:
        return "send"
    return "missed"
