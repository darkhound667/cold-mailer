# -*- coding: utf-8 -*-
"""Phase 4: hand a queued batch to the GitHub runner (`queue_emails.py queue --push`).

Decision Q32: queuing shows a summary and asks once; that yes approves the queue AND the push
of its state to the public repo. What goes into the repo (hard rule 5):
  - state/emails.json: public-safe fields (name, university, country, domain, subject, body,
    status, times, events), never an email address;
  - state/private.json.enc: the To addresses (AES-256-GCM with DATA_KEY);
  - state/resume.pdf.enc: the resume, encrypted.
The repo state is the source of truth once pushed: the runner re-spaces slots across every
batch, so the final minute can differ from the local dry-run preview.
"""
import hashlib
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from runner import crypto
from runner.state import State
from runner.tick import respace

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RESUME = ROOT.parents[1] / "deliverables" / "resume" / "aryan-format" / "ruturva_vaghasia_resume_aryan_format.pdf"


def local_key() -> bytes:
    """DATA_KEY from the environment, else from the local .data_key file (scripts/make_key.py)."""
    if os.environ.get("DATA_KEY"):
        return crypto.load_key()
    path = ROOT / ".data_key"
    if not path.exists():
        raise SystemExit("No DATA_KEY: run scripts/make_key.py and upload it as a Secret first (Q34)")
    return crypto.load_key(path.read_text())


def git(*args, check=True):
    return subprocess.run(["git", "-C", str(ROOT), *args], check=check, capture_output=True, text=True)


def export(entries: list, drafts: dict, profs: dict, key: bytes, state_root: Path = None,
           resume_path: Path = None, now: datetime = None) -> State:
    """Merge newly queued entries into the repo state and save it (encrypting private parts)."""
    now = now or datetime.now(timezone.utc)
    st = State(state_root).load(key)
    for e in entries:
        p, d = profs[e["id"]], drafts[e["id"]]
        rec = next((r for r in st.emails if r["id"] == e["id"]), None)
        if rec is None:
            rec = {"id": e["id"], "events": []}
            st.emails.append(rec)
        rec.update({"name": p["name"], "university": p["university"], "country": p["country"],
                    "env_domain": p["env_domain"], "tier": p.get("tier", "A"), "subject": d["subject"],
                    "body": d["body"], "attachment": d["attachment"], "sha256": e["sha256"],
                    "timezone": e["timezone"], "queue_index": e["queue_index"], "slot_utc": e["slot_utc"],
                    "send_utc": e.get("send_utc"), "send_local": e.get("send_local"),
                    "status": "scheduled", "reply_label": rec.get("reply_label", ""), "issue": rec.get("issue")})
        st.contact(e["id"])["to"] = e["to"]
        st.event(rec, "queued", now, f"sha256={e['sha256'][:12]}")
    respace(st)  # spacing across every batch already in the repo
    for e in entries:
        rec = st.by_id(e["id"])
        st.event(rec, "scheduled", now, f"send_local={rec['send_local']}")
    resume = Path(resume_path or os.environ.get("RESUME_PATH") or DEFAULT_RESUME).read_bytes()
    enc = st.dir / "resume.pdf.enc"
    marker = st.dir / "resume.sha256"  # public: lets us skip re-encrypting an unchanged resume
    digest = hashlib.sha256(resume).hexdigest()
    st.save(key)
    if not enc.exists() or not marker.exists() or marker.read_text().strip() != digest:
        enc.write_bytes(crypto.encrypt(resume, key))
        marker.write_text(digest + "\n")
    return st


def commit_and_push(n: int, push: bool) -> None:
    git("add", "state")
    if git("diff", "--cached", "--quiet", check=False).returncode == 0:
        print("nothing new to commit")
        return
    git("commit", "-q", "-m", f"queue: {n} email(s) scheduled")
    if push:
        git("push", "-q")
        print("pushed: the runner will pick these up on its next tick")
    else:
        print("committed locally, not pushed")
