# -*- coding: utf-8 -*-
"""Refresh every unsent draft after a template or resume change (Ruturva, 2026-10-28).

1. Drafts (review/P####.md) of professors who are scheduled or not yet queued are rewritten from the
   current template, but only when the file still equals what the PREVIOUS template produced
   (a hand-edited draft is listed and left alone).
2. Scheduled emails in the repo state get the new body and a new SHA-256 lock, with their
   send times untouched, plus an `edited` event. Sent emails are never touched.
3. state/resume.pdf.enc is re-encrypted from the current resume PDF, so every email sent from now
   on carries it (the attachment name stays the same).
Only state/emails.json, events.jsonl and the resume files are written: nothing in private.json.enc,
so a rebase against the runner's commits does not hit a binary conflict. Commits and pushes the
state (Ruturva asked for it); re-run is safe (idempotent).

Usage:  python scripts/update_scheduled.py [--dry-run]
"""
import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from generate import drafts  # noqa: E402
from runner import crypto  # noqa: E402
from runner.state import State  # noqa: E402
from schedule import publish  # noqa: E402
from schedule.queue_emails import parse_draft, text_hash  # noqa: E402


def old_body(r: dict) -> str:
    """The template as it was before the noisy-label line (no NOISY sentence)."""
    second = drafts.DAC if r["env_domain"] in drafts.DAC_DOMAINS else drafts.CHENNAI
    parts = [drafts.salutation(r), "I hope this message finds you well.", drafts.INTRO.format(area=drafts.research_area(r)),
             f"{drafts.WILDLIFE} {second}", drafts.ASK, drafts.CLOSING]
    return "\n\n".join(parts) + "\n"


def sh(*args, check=True):
    return subprocess.run(["git", "-C", str(ROOT), *args], check=check, capture_output=True, text=True)


def refresh(dry: bool) -> dict:
    key = publish.local_key()
    st = State(ROOT / "state").load(key)
    recs = {r["id"]: r for r in st.emails}
    profs = {r["id"]: r for r in json.loads((ROOT / "data" / "professors.json").read_text())}
    now = datetime.now(timezone.utc)
    out = {"files": 0, "scheduled": 0, "hand_edited": [], "sent_skipped": 0, "unchanged": 0}
    for path in sorted((ROOT / "review").glob("P*.md")):
        pid = path.stem
        rec, p = recs.get(pid), profs.get(pid)
        if p is None or (rec is not None and rec["status"] != "scheduled"):
            out["sent_skipped"] += rec is not None
            continue
        d = parse_draft(path)
        new = drafts.body(p)
        if d["body"] == old_body(p):
            if not dry:
                text = path.read_text()
                head = text[4:].split("\n---\n", 1)[0]
                path.write_text("---\n" + head + "\n---\n\n" + new)
            out["files"] += 1
        elif d["body"] != new:
            out["hand_edited"].append(pid + (" (scheduled)" if rec else ""))
            continue
        else:
            out["unchanged"] += 1
        if rec is not None and rec.get("body") != new:
            d["body"] = new
            if not dry:
                rec["body"], rec["sha256"] = new, text_hash(d)
                st.event(rec, "edited", now, "draft refreshed: noisy-label project added; resume replaced")
            out["scheduled"] += 1
    if dry:
        return out
    (ROOT / "state" / "emails.json").write_text(json.dumps(st.emails, indent=1, ensure_ascii=False))
    queue = ROOT / "data" / "queue.json"
    if queue.exists():  # the local copy keeps its hash lock in step
        q = json.loads(queue.read_text())
        for item in q:
            r = recs.get(item["id"])
            if r and r["status"] == "scheduled" and item.get("sha256") != r["sha256"]:
                item["sha256"] = r["sha256"]
        queue.write_text(json.dumps(q, indent=1, ensure_ascii=False))
    resume = publish.DEFAULT_RESUME.read_bytes()
    digest = hashlib.sha256(resume).hexdigest()
    marker = ROOT / "state" / "resume.sha256"
    if not marker.exists() or marker.read_text().strip() != digest:
        (ROOT / "state" / "resume.pdf.enc").write_bytes(crypto.encrypt(resume, key))
        marker.write_text(digest + "\n")
        out["resume"] = "replaced"
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    sh("pull", "-q", "--rebase", "--autostash")
    if args.dry_run:
        print(refresh(True))
        return
    for attempt in range(4):
        out = refresh(False)
        print(out)
        sh("add", "state/emails.json", "state/events.jsonl", "state/resume.pdf.enc", "state/resume.sha256")
        if sh("diff", "--cached", "--quiet", check=False).returncode == 0:
            print("nothing to commit")
            return
        sh("commit", "-q", "-m", f"queue: refresh {out['scheduled']} scheduled email(s) and the resume")
        if sh("push", "-q", check=False).returncode == 0:
            print("pushed: the runner picks it up on its next refresh")
            return
        if sh("pull", "-q", "--rebase", "--autostash", check=False).returncode == 0 and \
                sh("push", "-q", check=False).returncode == 0:
            print("pushed after a rebase")
            return
        sh("rebase", "--abort", check=False)
        sh("reset", "-q", "--hard", "HEAD~1")  # drop only this commit; the runner's newer state wins, then redo
        sh("pull", "-q", "--rebase", "--autostash")
    raise SystemExit("could not push after 4 attempts; nothing was lost, re-run the script")


if __name__ == "__main__":
    main()
