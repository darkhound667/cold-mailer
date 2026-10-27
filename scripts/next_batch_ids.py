# -*- coding: utf-8 -*-
"""Print the ids of the next batch of drafts to queue (the weekday rule), comma-separated.

The rule (Ruturva, 2026-10-20; raised 2026-10-27): queue one batch every weekday, skipping nothing.
Since 2026-10-27 a batch is 150: 100 environmental + 50 pure AI/ML (ratio 100 : 50).
Order: draft batch number, then id, alternating environmental and `track == "pure_ml"` rows. A draft is skipped only if it is already in the repo
state or the local queue, if it is no longer sendable, or if the professor has an `email_alt`
(two recipients need the email_alt code to be pushed to the public repo first).

Usage:  python scripts/next_batch_ids.py [--limit 150] [--pure 50]
Carry-over (Ruturva, 2026-10-26): mails that missed their slot are rescheduled by the runner and
stay `scheduled` with a `rescheduled` event. They count toward the batch (they are not queued
twice), and the track shares (100 env : 50 pure-ML) include them.
Prints nothing when every draft is already queued.
"""
import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=150)
    ap.add_argument("--pure", type=int, default=50, help="how many of the batch are pure AI/ML (the rest are environmental)")
    args = ap.parse_args()

    profs = {r["id"]: r for r in json.loads((ROOT / "data" / "professors.json").read_text())}
    queued, carry = set(), []
    state = ROOT / "state" / "emails.json"
    if state.exists():
        data = json.loads(state.read_text())
        rows = data["emails"] if isinstance(data, dict) else data
        queued |= {r["id"] for r in rows}
        carry = [r["id"] for r in rows if r["status"] == "scheduled" and not r.get("dry_run")
                 and any(e["event"] == "rescheduled" for e in r.get("events", []))]
    local = ROOT / "data" / "queue.json"
    if local.exists():
        queued |= {q["id"] for q in json.loads(local.read_text())}

    todo = []  # (batch, id) of every queueable draft
    for path in (ROOT / "review").glob("P*.md"):
        pid = path.stem
        m = re.search(r"(?m)^batch: (\d+)", path.read_text())
        batch = int(m.group(1)) if m else 1
        p = profs.get(pid)
        if pid in queued or not p or not p.get("sendable") or p.get("email_alt"):
            continue
        todo.append((batch, pid))
    # Alternate environmental and pure AI/ML professors (Ruturva, 2026-10-21); when one track runs
    # out, the other fills the batch. Within a track: draft batch order, then id (pure-ML ids
    # run from the best-ranked university down).
    env = [pid for _, pid in sorted(todo) if profs[pid].get("track") != "pure_ml"]
    pure = [pid for _, pid in sorted(todo) if profs[pid].get("track") == "pure_ml"]
    carry_pure = sum(1 for pid in carry if profs.get(pid, {}).get("track") == "pure_ml")
    n_new = max(0, args.limit - len(carry))
    want_pure = min(len(pure), max(0, args.pure - carry_pure))
    want_env = min(len(env), max(0, (args.limit - args.pure) - (len(carry) - carry_pure)))
    # one track short: the other fills the batch, so a day is never left under its total
    short = n_new - want_pure - want_env
    if short > 0:
        extra_env = min(len(env) - want_env, short)
        want_env += extra_env
        want_pure = min(len(pure), want_pure + short - extra_env)
    picked, e, p = [], env[:want_env], pure[:want_pure]
    while e or p:  # interleave in the batch's own ratio (2 environmental : 1 pure-ML)
        for _ in range(2):
            if e:
                picked.append(e.pop(0))
        if p:
            picked.append(p.pop(0))
    print(",".join(picked))


if __name__ == "__main__":
    main()
