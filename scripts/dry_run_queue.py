# -*- coding: utf-8 -*-
"""Phase 5 dry run: queue a few batch-01 drafts as TEST records addressed to Ruturva himself.

Each test record (id "DRY-P####") carries the real draft text but its To is the address given
with --to, so whatever happens no professor can receive it. Records are flagged "dry_run": the
runner sends them with a "[DRY RUN id]" subject, marks them `dry_run_sent`, saves the copy in
Sent, and keeps their Message-ID so replying to one tests the reply-Issue flow.

Usage (from the repo root):
    .venv/bin/python scripts/dry_run_queue.py --to YOU@iitb.ac.in [--n 3] [--in-minutes 12] [--push]
"""
import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from runner.state import iso, text_hash  # noqa: E402
from schedule import publish  # noqa: E402
from schedule.queue_emails import parse_draft  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", required=True, help="your own address: every test email goes here")
    ap.add_argument("--n", type=int, default=3)
    ap.add_argument("--in-minutes", type=int, default=12, help="first test email this many minutes from now")
    ap.add_argument("--push", action="store_true")
    args = ap.parse_args()

    profs = {r["id"]: r for r in json.loads((ROOT / "data" / "professors.json").read_text())}
    # batch-01 drafts are the ones without a "batch:" header line
    paths = sorted(p for p in (ROOT / "review").glob("P*.md") if "\nbatch: " not in p.read_text()[:300])[: args.n]
    now = datetime.now(timezone.utc)
    slot = (now + timedelta(minutes=args.in_minutes)).replace(second=0, microsecond=0)
    entries, drafts, info = [], {}, {}
    for i, path in enumerate(paths, 1):
        d, pid = parse_draft(path), path.stem
        tid = f"DRY-{pid}"
        drafts[tid], info[tid] = d, profs[pid]
        entries.append({"id": tid, "to": args.to, "sha256": text_hash(args.to, d["subject"], d["attachment"], d["body"]),
                        "queue_index": 900000 + i, "timezone": profs[pid]["timezone"], "slot_utc": iso(slot)})

    if args.push:
        publish.git("pull", "-q", "--rebase")
    key = publish.local_key()
    st = publish.export(entries, drafts, info, key, now=now)
    for e in entries:
        st.by_id(e["id"])["dry_run"] = True
    st.save(key)
    for e in entries:
        r = st.by_id(e["id"])
        print(f"  {r['id']}  {r['send_utc']}  ({r['timezone']} {r['send_local'][11:16]})  {r['subject'][:60]}")
    if args.push:
        publish.commit_and_push(len(entries), push=True)


if __name__ == "__main__":
    main()
