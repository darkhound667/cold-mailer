# -*- coding: utf-8 -*-
"""Conversation history for the dashboard (Ruturva, 2026-10-26).

For every professor who replied, keep the whole thread: our first email, their replies, and
whatever Ruturva wrote back from webmail, each with its attachments. This replaces the old rule
"reply bodies are never stored" (decision Q36): the text is stored only encrypted.

Storage, all AES-256-GCM under DASHBOARD_PASSPHRASE (crypto.encrypt_for_browser) so the static
page can open it after unlocking, and committed with the state:
  site/threads.enc.json    {generated_utc, att_bytes, threads: {id: {messages: [...]}}}
  site/att/<sha>.json      {name, ctype, size, data(base64)}, one per distinct attachment
A message is {mid, dir: "in"|"out", from, to, date_utc, subject, text,
atts: [{name, ctype, size, sha | null, note}]}. Attachments over ATT_MAX each, or past ATT_TOTAL
overall, are listed by name and size without the file.

Matching (hard rule 6 still holds: nothing outside these threads is kept): a message in INBOX
or Sent belongs to a thread when its From/To/Cc holds a professor's address, or when
In-Reply-To/References names a Message-ID already in that thread. Folders are opened read-only.
"""
import base64
import hashlib
import html
import json
import re
from datetime import datetime, timedelta, timezone
from email import message_from_bytes, policy
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from pathlib import Path

from runner import crypto, mailio
from runner.state import iso, parse

SITE = Path(__file__).resolve().parents[2] / "site"
ATT_MAX = 5 * 1024 * 1024
ATT_TOTAL = 50 * 1024 * 1024
SWEEP_EVERY = timedelta(minutes=5)
HEADERS = "(BODY.PEEK[HEADER.FIELDS (FROM TO CC MESSAGE-ID IN-REPLY-TO REFERENCES)])"
SKIP_SENDERS = ("mailer-daemon", "postmaster")


class ThreadStore:
    """site/threads.enc.json plus the attachment files, decrypted in memory."""

    def __init__(self, site: Path = SITE, passphrase: str = ""):
        self.site, self.passphrase = Path(site), passphrase
        self.data = {"generated_utc": None, "att_bytes": 0, "threads": {}}
        self.loaded = False
        self.last_sweep = None  # in memory only: writing it would commit the file every sweep

    def load(self) -> None:
        """Raises when the file exists but will not decrypt: never overwrite history we cannot read."""
        f = self.site / "threads.enc.json"
        if f.exists():
            self.data = crypto.decrypt_from_browser_format(json.loads(f.read_text()), self.passphrase)
        self.loaded = True

    def save(self, now: datetime) -> None:
        self.site.mkdir(parents=True, exist_ok=True)
        self.data["generated_utc"] = iso(now)
        (self.site / "threads.enc.json").write_text(json.dumps(crypto.encrypt_for_browser(self.data, self.passphrase)))

    def put_attachment(self, name: str, ctype: str, payload: bytes) -> str:
        sha = hashlib.sha256(payload).hexdigest()[:32]
        path = self.site / "att" / f"{sha}.json"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            obj = {"name": name, "ctype": ctype, "size": len(payload), "data": base64.b64encode(payload).decode()}
            path.write_text(json.dumps(crypto.encrypt_for_browser(obj, self.passphrase)))
            self.data["att_bytes"] += len(payload)
        return sha

    def mids(self) -> dict:
        return {m["mid"]: pid for pid, t in self.data["threads"].items() for m in t["messages"]}

    def add(self, pid: str, msg: dict) -> bool:
        t = self.data["threads"].setdefault(pid, {"messages": []})
        if any(m["mid"] == msg["mid"] for m in t["messages"]):
            return False
        t["messages"].append(msg)
        t["messages"].sort(key=lambda m: m["date_utc"])
        return True


def html_to_text(src: str) -> str:
    src = re.sub(r"(?is)<(script|style).*?</\1>", "", src)
    src = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>", "\n", src)
    text = html.unescape(re.sub(r"<[^>]+>", "", src))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def parse_message(raw: bytes) -> dict:
    """{mid, from, to, subject, date_utc, text, parts: [(name, ctype, bytes)]} from raw RFC 822."""
    m = message_from_bytes(raw, policy=policy.default)
    body = m.get_body(preferencelist=("plain", "html"))
    text = ""
    if body is not None:
        text = body.get_content()
        if body.get_content_type() == "text/html":
            text = html_to_text(text)
    parts = []
    for p in m.iter_attachments():
        payload = p.get_payload(decode=True)
        if payload is None:  # e.g. an attached message/rfc822: keep its bytes
            payload = p.as_bytes()
        name = p.get_filename() or ("forwarded-message.eml" if p.get_content_type() == "message/rfc822" else "attachment")
        if p.get_content_maintype() == "image" and not p.get_filename() and len(payload) < 100_000:
            continue  # unnamed signature image
        parts.append((name, p.get_content_type(), payload))
    try:
        date = parsedate_to_datetime(str(m["Date"])).astimezone(timezone.utc)
    except (TypeError, ValueError):
        date = datetime.now(timezone.utc)
    return {"mid": str(m["Message-ID"] or "").strip(), "from": str(m["From"] or ""), "to": str(m["To"] or ""),
            "subject": str(m["Subject"] or ""), "date_utc": iso(date), "text": text.strip(), "parts": parts}


def _addrs(*values) -> set:
    return {a.lower() for _, a in getaddresses([v for v in values if v]) if a}


def targets(st) -> dict:
    """{id: (record, {professor addresses})} for every professor who has replied."""
    out = {}
    for r in st.emails:
        if r.get("dry_run"):
            continue
        if r["status"] != "replied" and not any(e["event"] in ("replied", "replied_again") for e in r.get("events", [])):
            continue
        to = st.private["contacts"].get(r["id"], {}).get("to", "")
        addrs = {a.strip().lower() for a in to.split(",") if a.strip()}
        if addrs:
            out[r["id"]] = (r, addrs)
    return out


def seed(store: ThreadStore, st, pid: str, rec: dict, resume_fn) -> bool:
    """Our automated first email (state holds its text; the resume is the attachment)."""
    contact = st.private["contacts"].get(pid, {})
    sent = next((e for e in rec.get("events", []) if e["event"] == "sent"), None)
    if not contact.get("message_id") or not sent:
        return False
    resume = resume_fn()
    sha = store.put_attachment(rec["attachment"], "application/pdf", resume)
    msg = {"mid": contact["message_id"], "dir": "out", "from": "Ruturva Vaghasia", "to": contact.get("to", ""),
           "date_utc": sent["ts_utc"], "subject": rec["subject"], "text": rec["body"].strip(),
           "atts": [{"name": rec["attachment"], "ctype": "application/pdf", "size": len(resume), "sha": sha}]}
    return store.add(pid, msg)


def _scan(imap, folder: str, direction: str, since: str, addr_pid: dict, mid_pid: dict, known: set) -> list:
    """[(uid, pid)] for messages in `folder` that belong to a tracked thread and are not stored yet."""
    typ, _ = imap.select(folder if folder == "INBOX" else f'"{folder}"', readonly=True)
    if typ != "OK":
        return []
    typ, data = imap.uid("SEARCH", None, f"SINCE {since}")
    uids = (data[0] or b"").split()
    found = []
    for i in range(0, len(uids), 200):
        typ, rows = imap.uid("FETCH", b",".join(uids[i:i + 200]).decode(), HEADERS)
        for row in rows or []:
            if not isinstance(row, tuple):
                continue
            uid = re.search(rb"UID (\d+)", row[0])
            h = message_from_bytes(row[1], policy=policy.default)
            mid = str(h["Message-ID"] or "").strip()
            sender = parseaddr(str(h["From"] or ""))[1].lower()
            if not uid or not mid or mid in known or sender.startswith(SKIP_SENDERS):
                continue
            refs = f"{h['In-Reply-To'] or ''} {h['References'] or ''}"
            people = {sender} if direction == "in" else _addrs(str(h["To"] or ""), str(h["Cc"] or ""))
            pid = next((addr_pid[a] for a in people if a in addr_pid), None)
            if pid is None:
                pid = next((mid_pid[m] for m in mid_pid if m in refs), None)
            if pid is not None:
                found.append((uid.group(1).decode(), pid))
    return found


def _fetch(imap, uid: str) -> bytes:
    typ, rows = imap.uid("FETCH", uid, "(BODY.PEEK[])")
    return b"".join(r[1] for r in rows or [] if isinstance(r, tuple))


def sync(store: ThreadStore, st, imap, now: datetime, resume_fn, force: bool = False) -> int:
    """Bring every replied professor's thread up to date. Returns the number of new messages."""
    tracked = targets(st)
    if not tracked:
        return 0
    fresh = [pid for pid in tracked if pid not in store.data["threads"]]
    if not (force or fresh or store.last_sweep is None or now - store.last_sweep >= SWEEP_EVERY):
        return 0
    added = 0
    for pid in fresh:
        store.data["threads"].setdefault(pid, {"messages": []})  # a thread that cannot be seeded is not "fresh" again
        added += seed(store, st, pid, tracked[pid][0], resume_fn)
    first = min(parse(e["ts_utc"]) for pid in tracked for e in tracked[pid][0].get("events", []) if e["event"] == "sent")
    since = first if (fresh or store.last_sweep is None) else store.last_sweep - timedelta(days=2)
    since = since.strftime("%d-%b-%Y")
    addr_pid = {a: pid for pid, (_, addrs) in tracked.items() for a in addrs}
    folders = []
    sent_folder = mailio.find_sent_folder(imap)
    if sent_folder:
        folders.append((sent_folder, "out"))  # Sent first: a reply may quote Ruturva's webmail message
    folders.append(("INBOX", "in"))
    for folder, direction in folders:
        known = store.mids()
        mid_pid = dict(known)
        for uid, pid in _scan(imap, folder, direction, since, addr_pid, mid_pid, set(known)):
            parsed = parse_message(_fetch(imap, uid))
            if not parsed["mid"] or parsed["mid"] in known:
                continue
            atts = []
            for name, ctype, payload in parsed["parts"]:
                entry = {"name": name, "ctype": ctype, "size": len(payload)}
                if len(payload) > ATT_MAX or store.data["att_bytes"] + len(payload) > ATT_TOTAL:
                    entry["sha"], entry["note"] = None, "too large to store"
                else:
                    entry["sha"] = store.put_attachment(name, ctype, payload)
                atts.append(entry)
            added += store.add(pid, {"mid": parsed["mid"], "dir": direction, "from": parsed["from"],
                                     "to": parsed["to"], "date_utc": parsed["date_utc"], "subject": parsed["subject"],
                                     "text": parsed["text"], "atts": atts})
            known[parsed["mid"]] = pid
    store.last_sweep = now
    if added:
        store.save(now)
    return added
