# -*- coding: utf-8 -*-
"""Repo-held state for the unattended sender (Phase 4).

Public (safe to commit, hard rule 5): state/emails.json, one record per queued email:
  id, name, university, country, env_domain, tier, subject, body, attachment, sha256,
  timezone, queue_index, slot_utc, send_utc, send_local, status, reply_label, issue,
  events: [{event, ts_utc, ts_local, detail}]
plus the append-only state/events.jsonl and state/heartbeat.json.
Private (AES-256-GCM, crypto.py): state/private.json.enc
  {"contacts": {id: {"to": address, "message_id": "<...>"}}, "seen": [reply Message-IDs]}
and state/resume.pdf.enc. Reply bodies are never stored anywhere.

Statuses: scheduled -> sent -> replied | bounced, plus failed and pulled.
"""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from runner import crypto

ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = ROOT / "state"


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def text_hash(to: str, subject: str, attachment: str, body: str) -> str:
    """Same canonical form as schedule/queue_emails.py, so the lock made at queue time holds."""
    canon = f"to:{to}\nsubject:{subject}\nattachment:{attachment}\n\n{body}"
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


class State:
    def __init__(self, root: Path = None):
        self.dir = Path(root) if root else STATE_DIR
        self.emails, self.private, self.heartbeat = [], {"contacts": {}, "seen": []}, {}
        self.changed = False

    # ---- files -------------------------------------------------------------
    def load(self, key: bytes) -> "State":
        d = self.dir
        if (d / "emails.json").exists():
            self.emails = json.loads((d / "emails.json").read_text())
        if (d / "private.json.enc").exists():
            self.private = json.loads(crypto.decrypt((d / "private.json.enc").read_bytes(), key))
        if (d / "heartbeat.json").exists():
            self.heartbeat = json.loads((d / "heartbeat.json").read_text())
        return self

    def save(self, key: bytes) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "emails.json").write_text(json.dumps(self.emails, indent=1, ensure_ascii=False))
        (self.dir / "private.json.enc").write_bytes(
            crypto.encrypt(json.dumps(self.private).encode("utf-8"), key))
        (self.dir / "heartbeat.json").write_text(json.dumps(self.heartbeat, indent=1))

    def resume(self, key: bytes) -> bytes:
        return crypto.decrypt((self.dir / "resume.pdf.enc").read_bytes(), key)

    # ---- records -----------------------------------------------------------
    def by_id(self, pid: str) -> dict:
        return next(r for r in self.emails if r["id"] == pid)

    def contact(self, pid: str) -> dict:
        return self.private["contacts"].setdefault(pid, {})

    def event(self, rec: dict, event: str, now: datetime, detail: str = "") -> None:
        """Record a transition on the email and in the append-only log (UTC + local time)."""
        ev = {"event": event, "ts_utc": iso(now),
              "ts_local": now.astimezone(ZoneInfo(rec["timezone"])).isoformat(timespec="seconds"),
              "detail": detail}
        rec.setdefault("events", []).append(ev)
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.dir / "events.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"id": rec["id"], **ev}, ensure_ascii=False) + "\n")
        self.changed = True
