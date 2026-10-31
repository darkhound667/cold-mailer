# -*- coding: utf-8 -*-
"""Give every scheduled email its own send minute, across ALL slots.

Ruturva, 2026-10-31: no two mails may leave in the same minute, and a mail whose minute is already
taken goes to the next valid day instead of being squeezed in or delayed. slots.py spaces mails inside
one 08:30 slot; slots in different timezones run side by side (a 100-mail slot lasts 100 minutes, the
next timezone starts 30-60 minutes later), so this module checks every minute against the whole queue.

Rules:
  1. A mail keeps the minute it already holds (`send_utc`, which must be `slot_utc` + 0..cap-1 minutes).
     Positions are sticky: when a mail leaves a slot, the ones behind it do NOT move up, because moving
     up would land them on the minutes other slots hold.
  2. If two mails hold the same minute, the one queued first (lower `queue_index`) keeps it and the other
     moves to the next valid day's slot.
  3. A new mail (no valid `send_utc`) takes the first free position after the last one used in its slot
     (`slot` + position minutes). If that position is past `cap` or its minute is taken, it moves to the
     next valid day's slot and tries again there.
Standard library plus schedule.scheduler.
"""
from collections import defaultdict
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo

from schedule.scheduler import next_slot

CAP = 100  # positions per slot (same as SLOT_CAP in queue_emails.py / tick.py)


def place_send_times(emails, gap=timedelta(minutes=1), cap=CAP):
    """`emails`: dicts with id, queue_index, timezone, country, slot_utc (aware) and optionally send_utc
    (aware). Returns a NEW list (input untouched) with slot_utc, send_utc, send_local, position and
    `moved` (True when the mail was pushed to a later day), sorted by send_utc then id."""
    items = sorted(emails, key=lambda e: (e["queue_index"], e["id"]))
    taken, used, placed, pending = set(), defaultdict(set), {}, []

    for e in items:
        slot = e["slot_utc"].astimezone(timezone.utc)
        sent = e.get("send_utc")
        k = (sent.astimezone(timezone.utc) - slot) / gap if sent is not None else -1
        if k == int(k) and 0 <= k < cap:  # it already holds a minute inside its slot
            minute = slot + int(k) * gap
            if minute not in taken:
                taken.add(minute)
                used[slot].add(int(k))
                placed[e["id"]] = (slot, int(k), False)
                continue
            pending.append((e, slot, True))  # its minute is held by an earlier-queued mail
        else:
            pending.append((e, slot, False))

    for e, slot, spill in pending:
        moved = spill
        if spill:
            slot = next_slot(e["timezone"], e["country"], slot)
        while True:
            pos = max(used[slot], default=-1) + 1
            if pos < cap and slot + pos * gap not in taken:
                break
            slot = next_slot(e["timezone"], e["country"], slot)
            moved = True
        taken.add(slot + pos * gap)
        used[slot].add(pos)
        placed[e["id"]] = (slot, pos, moved)

    out = []
    for e in emails:
        slot, pos, moved = placed[e["id"]]
        send = slot + pos * gap  # absolute-time arithmetic, immune to DST
        row = dict(e)
        row.update(slot_utc=slot, send_utc=send, send_local=send.astimezone(ZoneInfo(e["timezone"])),
                   position=pos, moved=moved)
        out.append(row)
    out.sort(key=lambda r: (r["send_utc"], r["id"]))
    return out
