# -*- coding: utf-8 -*-
"""Print the ids of the next daily batch of drafts to queue, comma-separated.

The rule (Ruturva, 2026-10-20; changed 2026-10-31): queue one batch every weekday, skipping nothing.
Since 2026-10-31 a day has TWO send windows on Ruturva's clock (IST), 500 mails each:
    AM window  00:00-11:59 IST  ->  Asia-Pacific professors (their 08:30 local falls here)
    PM window  12:00-23:59 IST  ->  Europe first, then the Americas (their 08:30 local falls here)
Every mail still goes at 08:30 in its professor's own timezone (src/schedule/scheduler.py); the window
a professor belongs to is simply where that 08:30 lands on the IST clock, so nobody is moved to a
different hour. The 500 of each window keep the 1 : 2 environmental : pure AI/ML ratio (a short track is
topped up from the other).

Which window-day: the PM window is the earliest PM half-day any unqueued professor can still be mailed
in; the AM window is the first AM half-day after it. Mails already scheduled in that half-day (including
missed-slot carry-over, which the runner reschedules) use up its 500.

Capacity: a send slot (all professors whose 08:30 is the same UTC instant) holds SLOT_CAP mails (100,
one per minute); a professor whose slot is full waits for a later day instead of spilling forward, so a
window can come out under 500 when a region has fewer people or slots (Europe has three slots: UTC+0,
UTC+1, UTC+2). The AM window is never topped up from the PM side.

Order inside a window and track: Europe first (2026-10-31), `priority` (University of Bristol), the
university's CSRankings rank (best first), draft batch, id. A draft is skipped if it is already in the
repo state or the local queue, no longer sendable, or has an `email_alt` (two recipients need the
email_alt code pushed to the public repo first).

Usage:  python scripts/next_batch_ids.py [--per-window 500] [--pure-share 0.667] [--am-date YYYY-MM-DD] [--now ISO]
Prints nothing when nothing can be queued. A summary goes to stderr.
"""
import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "source"))
from schedule.scheduler import next_slot  # noqa: E402
from timezones import EUROPE  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
SLOT_CAP = 100  # per 08:30 slot, same as src/schedule/queue_emails.py


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def window_of(slot_utc: datetime):
    """(IST date, 'AM' or 'PM') of a send slot."""
    t = slot_utc.astimezone(IST)
    return t.date(), "AM" if t.hour < 12 else "PM"


def load_rows() -> tuple:
    """(ids already in the repo state or local queue, {id: slot_utc} of the ones still scheduled)."""
    queued, sched = set(), {}
    state = ROOT / "state" / "emails.json"
    if state.exists():
        data = json.loads(state.read_text())
        for r in (data["emails"] if isinstance(data, dict) else data):
            queued.add(r["id"])
            if r["status"] == "scheduled" and not r.get("dry_run") and r.get("slot_utc"):
                sched[r["id"]] = r["slot_utc"]
    local = ROOT / "data" / "queue.json"
    if local.exists():
        for q in json.loads(local.read_text()):
            queued.add(q["id"])
            if q["status"] == "scheduled":
                sched[q["id"]] = q["slot_utc"]
    return queued, sched


def fill(env: list, pure: list, room: int, per_slot: Counter, pure_share: float) -> list:
    """Up to `room` ids from the two priority-ordered lists of (id, slot), interleaved in the pure_share
    ratio; anyone whose slot is already full is passed over (they wait for a later day)."""
    ratio = pure_share / (1 - pure_share)  # pure mails per environmental mail
    picked, n_pure, n_env = [], 0, 0
    while len(picked) < room:
        prefer_pure = n_pure < ratio * n_env + 1
        pid = None
        for lst, is_pure in ((pure, True), (env, False)) if prefer_pure else ((env, False), (pure, True)):
            while lst:
                cand, slot = lst.pop(0)
                if per_slot[slot] < SLOT_CAP:
                    per_slot[slot] += 1
                    pid = cand
                    break
            if pid:
                n_pure += is_pure
                n_env += not is_pure
                break
        if pid is None:
            break
        picked.append(pid)
    return picked


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-window", type=int, default=500)
    ap.add_argument("--pure-share", type=float, default=2 / 3, help="share of each window that is pure AI/ML")
    ap.add_argument("--now", default="", help="pretend it is this UTC time (ISO, for dry runs)")
    ap.add_argument("--am-date", default="", help="force the AM window to this IST date (YYYY-MM-DD)")
    args = ap.parse_args()
    now = datetime.fromisoformat(args.now.replace("Z", "+00:00")) if args.now else datetime.now(timezone.utc)

    profs = {r["id"]: r for r in json.loads((ROOT / "data" / "professors.json").read_text())}
    queued, sched = load_rows()

    cands = []  # (sort key, id, slot, window)
    for path in (ROOT / "review").glob("P*.md"):
        pid = path.stem
        p = profs.get(pid)
        if pid in queued or not p or not p.get("sendable") or p.get("email_alt"):
            continue
        m = re.search(r"(?m)^batch: (\d+)", path.read_text())
        try:
            slot = next_slot(p["timezone"], p["country"], now)
        except ValueError:
            continue
        europe = p["country"] in EUROPE
        key = (0 if europe else 1, -p.get("priority", 0), p.get("csr_rank", 9999), int(m.group(1)) if m else 1, pid)
        cands.append((key, pid, slot, window_of(slot)))
    cands.sort(key=lambda c: c[0])

    per_slot = Counter(sched.values())  # mails already in each slot
    used = Counter(window_of(datetime.fromisoformat(s.replace("Z", "+00:00"))) for s in sched.values())

    pm_day = min((w[0] for *_, w in cands if w[1] == "PM"), default=None)
    am_day = min((w[0] for *_, w in cands if w[1] == "AM" and (pm_day is None or w[0] > pm_day)), default=None)

    if args.am_date:
        am_day = datetime.strptime(args.am_date, "%Y-%m-%d").date()
    picked, summary = [], []
    for day, half in ((pm_day, "PM"), (am_day, "AM")):
        if day is None:
            continue
        here = [c for c in cands if c[3] == (day, half)]
        env = [(pid, iso(slot)) for _, pid, slot, _ in here if profs[pid].get("track") != "pure_ml"]
        pure = [(pid, iso(slot)) for _, pid, slot, _ in here if profs[pid].get("track") == "pure_ml"]
        got = fill(env, pure, max(0, args.per_window - used[(day, half)]), per_slot, args.pure_share)
        picked += got
        n_pure = sum(profs[i].get("track") == "pure_ml" for i in got)
        summary.append(f"{half} {day}: {len(got)} new (+{used[(day, half)]} already scheduled), "
                       f"Europe {sum(profs[i]['country'] in EUROPE for i in got)}, pure AI/ML {n_pure}, environmental {len(got) - n_pure}")
    print("; ".join(summary) if summary else "nothing to queue", file=sys.stderr)
    print(",".join(picked))


if __name__ == "__main__":
    main()
