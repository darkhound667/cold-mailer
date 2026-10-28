# -*- coding: utf-8 -*-
"""Phase 3: queue reviewed drafts and give each one its send time.

Queuing IS the approval (SPEC amendment 2): whatever is queued is treated as read by Ruturva.
At queue time the exact text (to, subject, attachment name, body) is hashed with SHA-256.
If a queued draft file changes afterwards, `verify` pulls it from the queue until it is
queued again, so an unseen edit can never be sent (hard rule 3).

Scheduling: 08:30 local on the next valid day (scheduler.py), at most SLOT_CAP emails per
08:30 slot (amendment 1; a full slot spills to the next valid day), then one email per minute
inside each slot (slots.py, amendment 12). Every transition is appended to data/events.jsonl
with UTC and local timestamps.

Commands (run from the repo root, with the project venv):
    python src/schedule/queue_emails.py queue  [--limit 100] [--ids P0002,P0004] [--dry-run] [--push [--yes]]
    python src/schedule/queue_emails.py verify
    python src/schedule/queue_emails.py status
Nothing here sends email.
"""
import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from schedule.scheduler import next_slot  # noqa: E402
from schedule.slots import assign_send_times  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
DATA, REVIEW = ROOT / "data", ROOT / "review"
QUEUE, EVENTS = DATA / "queue.json", DATA / "events.jsonl"
SLOT_CAP = 100  # per 08:30 slot (amendment 1)
BATCH_CAP = 300  # per call: the daily batch is 300 since 2026-10-29


def parse_draft(path: Path) -> dict:
    """Header fields plus the exact body of a review/P####.md file."""
    text = path.read_text()
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        raise ValueError(f"{path.name}: missing --- header")
    head, body = text[4:].split("\n---\n", 1)
    fields = dict(line.split(": ", 1) for line in head.splitlines() if ": " in line)
    fields["body"] = body[1:] if body.startswith("\n") else body  # the blank line after the header
    return fields


def text_hash(d: dict) -> str:
    """SHA-256 of exactly what would be sent. The header's `status` line is not part of it."""
    canon = f"to:{d['to']}\nsubject:{d['subject']}\nattachment:{d['attachment']}\n\n{d['body']}"
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def set_status(path: Path, status: str) -> None:
    lines = path.read_text().split("\n")
    for i, line in enumerate(lines[:12]):  # header only
        if line.startswith("status: "):
            lines[i] = f"status: {status}"
            break
    path.write_text("\n".join(lines))


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log_event(pid: str, event: str, when: datetime, tz: str, detail: str = "") -> None:
    rec = {"ts_utc": iso(when), "ts_local": when.astimezone(ZoneInfo(tz)).isoformat(),
           "id": pid, "event": event, "detail": detail}
    with open(EVENTS, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def load_queue() -> list:
    return json.loads(QUEUE.read_text()) if QUEUE.exists() else []


def respace(queue: list) -> None:
    """Recompute the per-minute send times of every scheduled email (stable by queue order)."""
    active = [q for q in queue if q["status"] == "scheduled"]
    spaced = assign_send_times([{"id": q["id"], "queue_index": q["queue_index"], "timezone": q["timezone"],
                                 "slot_utc": datetime.fromisoformat(q["slot_utc"].replace("Z", "+00:00"))}
                                for q in active])
    by_id = {s["id"]: s for s in spaced}
    for q in active:
        s = by_id[q["id"]]
        q.update(send_utc=iso(s["send_utc"]), send_local=s["send_local"].isoformat(), position=s["position"])


def cmd_queue(args) -> None:
    now = datetime.now(timezone.utc)
    queue = load_queue()
    in_queue = {q["id"] for q in queue if q["status"] == "scheduled"}
    profs = {r["id"]: r for r in json.loads((DATA / "professors.json").read_text())}
    wanted = set(args.ids.split(",")) if args.ids else None

    picked, skipped = [], []
    for path in sorted(REVIEW.glob("P*.md")):
        pid = path.stem
        if pid in in_queue or (wanted and pid not in wanted):
            continue
        d, p = parse_draft(path), profs.get(pid)
        if not p or not p.get("sendable"):
            skipped.append((pid, "not sendable in professors.json"))
        elif d["to"] != ", ".join([p["email"], *p.get("email_alt", [])]):  # extra printed addresses ride in To:
            skipped.append((pid, "To: differs from the verified email"))
        else:
            picked.append((path, d, p))
        if len(picked) == min(args.limit, BATCH_CAP):
            break

    per_slot = Counter(q["slot_utc"] for q in queue if q["status"] == "scheduled")
    next_index = max((q["queue_index"] for q in queue), default=0) + 1
    new = []
    for path, d, p in picked:
        slot = next_slot(p["timezone"], p["country"], now)
        while per_slot[iso(slot)] >= SLOT_CAP:  # full slot: spill to the next valid day
            slot = next_slot(p["timezone"], p["country"], slot)
        per_slot[iso(slot)] += 1
        new.append({"id": d["id"], "to": d["to"], "subject": d["subject"], "attachment": d["attachment"],
                    "sha256": text_hash(d), "queue_index": next_index, "queued_utc": iso(now),
                    "timezone": p["timezone"], "country": p["country"], "slot_utc": iso(slot),
                    "status": "scheduled", "_path": path})
        next_index += 1

    queue = [q for q in queue if not (q["id"] in {n["id"] for n in new})] + new
    respace(queue)
    for pid, why in skipped:
        print(f"  skipped {pid}: {why}")
    print(f"{'DRY RUN: ' if args.dry_run else ''}queued {len(new)} (cap {BATCH_CAP} per call, {SLOT_CAP} per slot)")
    for n in sorted(new, key=lambda q: q["send_utc"]):
        print(f"  {n['id']}  {n['send_local'][:16].replace('T', ' ')} {n['timezone']:<22} ({n['send_utc'][:16]}Z)  {profs[n['id']]['name']}")
    if args.dry_run:
        return
    if getattr(args, "push", False):
        if not new:
            print("nothing to push")
            return
        # Decision Q32: one confirmation approves both the queue and the push to the public repo.
        if not args.yes and input(f"Queue these {len(new)} and push the encrypted state to GitHub? [y/N] ").strip().lower() != "y":
            print("not queued, nothing written")
            return
        from schedule import publish
        publish.git("pull", "-q", "--rebase")  # the runner may have committed state meanwhile
        drafts = {n["id"]: parse_draft(n["_path"]) for n in new}
        publish.export([{k: v for k, v in n.items() if k != "_path"} for n in new], drafts, profs, publish.local_key())
    for n in new:
        set_status(n.pop("_path"), "queued")
        log_event(n["id"], "queued", now, n["timezone"], f"sha256={n['sha256'][:12]}")
        log_event(n["id"], "scheduled", now, n["timezone"], f"send_local={n['send_local']}")
    QUEUE.write_text(json.dumps(queue, indent=1, ensure_ascii=False))
    if getattr(args, "push", False):
        publish.commit_and_push(len(new), push=True)


def cmd_verify(_args) -> None:
    """Pull any scheduled email whose draft text no longer matches its locked hash."""
    now = datetime.now(timezone.utc)
    queue = load_queue()
    pulled = 0
    for q in queue:
        if q["status"] != "scheduled":
            continue
        path = REVIEW / f"{q['id']}.md"
        if not path.exists() or text_hash(parse_draft(path)) != q["sha256"]:
            q["status"] = "pulled"
            pulled += 1
            log_event(q["id"], "pulled", now, q["timezone"], "draft changed after queuing: re-queue to approve")
            if path.exists():
                set_status(path, "edited")
    respace(queue)
    QUEUE.write_text(json.dumps(queue, indent=1, ensure_ascii=False))
    print(f"verified {sum(q['status'] == 'scheduled' for q in queue)} scheduled; pulled {pulled} edited draft(s)")


def cmd_status(_args) -> None:
    queue = load_queue()
    print("by status:", dict(Counter(q["status"] for q in queue)))
    slots = defaultdict(list)
    for q in queue:
        if q["status"] == "scheduled":
            slots[(q["slot_utc"], q["timezone"])].append(q)
    for (slot, tz), qs in sorted(slots.items())[:15]:
        first = min(q["send_local"] for q in qs)
        last = max(q["send_local"] for q in qs)
        print(f"  {slot}  {tz:<22} {len(qs):3d} emails  {first[11:16]}–{last[11:16]} local")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    q = sub.add_parser("queue")
    q.add_argument("--limit", type=int, default=BATCH_CAP)
    q.add_argument("--ids", default="")
    q.add_argument("--dry-run", action="store_true")
    q.add_argument("--push", action="store_true", help="also hand the batch to the GitHub runner (asks first)")
    q.add_argument("--yes", action="store_true", help="answer the push question with yes (only on Ruturva's go)")
    sub.add_parser("verify")
    sub.add_parser("status")
    args = ap.parse_args()
    {"queue": cmd_queue, "verify": cmd_verify, "status": cmd_status}[args.cmd](args)


if __name__ == "__main__":
    main()
