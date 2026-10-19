# -*- coding: utf-8 -*-
"""Mail I/O for the runner: SMTP sending, a Sent-folder copy, and the inbox poll.

IITB specifics (verified 2026-10-18): login = bare LDAP ID + SSO Access Token;
smtp-auth.iitb.ac.in:587 STARTTLS; imap.iitb.ac.in:993 TLS with one legacy cipher.

The poll reads HEADERS ONLY, read-only, and keeps a message only if it belongs to one of our
sent threads (In-Reply-To/References = our Message-ID, or From = an address we wrote to;
hard rule 6). The one exception: a delivery-status report from mailer-daemon/postmaster is
opened (first 20 kB only) to look for one of our Message-IDs, because a bounce names the
original message only in its body.
"""
import email
import imaplib
import re
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr

SENT_CANDIDATES = ("Sent", "Sent Items", "Sent Messages", "Sent Mail", "Sent-Mail")
AUTO_SUBJECT = re.compile(r"(?i)^(auto(matic)?[ -]?reply|out of (the )?office|abwesen|absence|"
                          r"on leave|away from|vacation|autosvar|autorespon|r[ée]ponse automatique)")
HEADER_FIELDS = ("MESSAGE-ID IN-REPLY-TO REFERENCES FROM SUBJECT DATE AUTO-SUBMITTED "
                 "X-AUTOREPLY X-AUTORESPOND PRECEDENCE CONTENT-TYPE")


def imap_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.set_ciphers("DEFAULT:AES256-GCM-SHA384")  # IITB IMAP offers only this TLS 1.2 cipher
    return ctx


def build_message(to: str, subject: str, body: str, attachment_name: str, attachment: bytes,
                  sender_name: str, sender_email: str) -> EmailMessage:
    """Plain-text email with the resume attached and a fresh Message-ID."""
    msg = EmailMessage()
    msg["From"] = formataddr((sender_name, sender_email))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain=sender_email.split("@")[1])
    msg.set_content(body)
    msg.add_attachment(attachment, maintype="application", subtype="pdf", filename=attachment_name)
    return msg


class Smtp:
    def __init__(self, host: str, port: int, user: str, token: str):
        self.host, self.port, self.user, self.token, self.conn = host, port, user, token, None

    def __enter__(self):
        self.conn = smtplib.SMTP(self.host, self.port, timeout=60)
        self.conn.starttls(context=ssl.create_default_context())
        self.conn.login(self.user, self.token)
        return self

    def send(self, msg: EmailMessage) -> None:
        refused = self.conn.send_message(msg)
        if refused:  # some recipients refused while others accepted
            raise smtplib.SMTPRecipientsRefused(refused)

    def __exit__(self, *exc):
        try:
            self.conn.quit()
        except Exception:
            pass


def imap_login(host: str, port: int, user: str, token: str) -> imaplib.IMAP4_SSL:
    imap = imaplib.IMAP4_SSL(host, port, ssl_context=imap_context(), timeout=60)
    imap.login(user, token)
    return imap


LIST_LINE = re.compile(r'^\((?P<flags>[^)]*)\)\s+(?P<delim>"(?:[^"\\]|\\.)*"|NIL)\s+(?P<name>.+)$')


def list_folders(imap) -> list:
    """[(name, flags)] from IMAP LIST. Names may be quoted or bare (e.g. INBOX.Sent)."""
    typ, data = imap.list()
    out = []
    for raw in data or []:
        line = (raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)).strip()
        m = LIST_LINE.match(line)
        if m:
            name = m.group("name").strip()
            if len(name) >= 2 and name[0] == name[-1] == '"':
                name = name[1:-1].replace('\\"', '"')
            out.append((name, m.group("flags")))
    return out


def find_sent_folder(imap) -> str:
    """The Sent folder: the one flagged \\Sent, else a well-known name (any case, any parent)."""
    folders = list_folders(imap)
    for name, flags in folders:
        if "\\sent" in flags.lower():
            return name
    wanted = {c.lower() for c in SENT_CANDIDATES}
    for name, _ in folders:
        leaf = re.split(r"[./]", name)[-1].lower()
        if name.lower() in wanted or leaf in wanted:
            return name
    return ""


def append_sent(imap, msg: EmailMessage) -> str:
    """Copy a sent message into the Sent folder (decision Q31). Returns "" on success, else why not
    (with the folder names, so a miss can be fixed; folder names hold no mail content)."""
    folder = find_sent_folder(imap)
    if not folder:
        return "no Sent folder; folders: " + ", ".join(n for n, _ in list_folders(imap))[:300]
    typ, _ = imap.append(f'"{folder}"', "\\Seen", imaplib.Time2Internaldate(time.time()), msg.as_bytes())
    return "" if typ == "OK" else f"APPEND to {folder} returned {typ}"


def classify(h) -> str:
    """'bounce', 'auto_reply' or 'human' from a reply's headers."""
    sender = parseaddr(h.get("From", ""))[1].lower()
    ctype = (h.get("Content-Type") or "").lower()
    if sender.startswith(("mailer-daemon", "postmaster")) or "report-type=delivery-status" in ctype:
        return "bounce"
    auto = (h.get("Auto-Submitted") or "no").lower()
    if (auto != "no" or h.get("X-Autoreply") or h.get("X-Autorespond")
            or (h.get("Precedence") or "").lower() in ("auto_reply", "bulk", "junk")
            or AUTO_SUBJECT.search((h.get("Subject") or "").strip())):
        return "auto_reply"
    return "human"


def poll(imap, our_ids: dict, our_addrs: dict, since: str, seen: set) -> list:
    """New replies to our emails. our_ids: Message-ID -> email id; our_addrs: address -> id.
    Returns [{msgid, kind, id}] and never marks anything as read."""
    imap.select("INBOX", readonly=True)
    typ, data = imap.uid("SEARCH", None, f"SINCE {since}")
    uids = (data[0] or b"").split()
    out = []
    for i in range(0, len(uids), 200):
        chunk = b",".join(uids[i:i + 200]).decode()
        typ, rows = imap.uid("FETCH", chunk, f"(BODY.PEEK[HEADER.FIELDS ({HEADER_FIELDS})])")
        for row in rows or []:
            if not isinstance(row, tuple):
                continue
            uid = re.search(rb"UID (\d+)", row[0])
            h = email.message_from_bytes(row[1])
            mid = (h.get("Message-ID") or "").strip()
            if not mid or mid in seen:
                continue
            thread = f"{h.get('In-Reply-To', '')} {h.get('References', '')}"
            pid = next((our_ids[m] for m in our_ids if m in thread), None)
            if pid is None:
                pid = our_addrs.get(parseaddr(h.get("From", ""))[1].lower())
            kind = classify(h)
            if pid is None and kind == "bounce" and uid:
                pid = _bounce_owner(imap, uid.group(1).decode(), our_ids)
            if pid is not None:
                out.append({"msgid": mid, "kind": kind, "id": pid})
    return out


def _bounce_owner(imap, uid: str, our_ids: dict):
    typ, rows = imap.uid("FETCH", uid, "(BODY.PEEK[TEXT]<0.20000>)")
    text = b"".join(r[1] for r in rows or [] if isinstance(r, tuple)).decode(errors="replace")
    return next((our_ids[m] for m in our_ids if m in text), None)
