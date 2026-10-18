# -*- coding: utf-8 -*-
"""Standalone batch sender — no notebook/IDE dependency, safe to run from `cron` /
Windows Task Scheduler for a scheduled send. All credentials come from environment
variables (see .env.example) — never hardcode a token or address here.

CSV columns expected: send (yes/no), name, email, subject, email_body. Rows with
send != yes, or a missing subject/email_body, are skipped. Already-sent rows (tracked
in sent_log_<batch>.csv) are skipped automatically, so re-running never double-sends.

Usage:
    python send_batch.py --batch data/batch_01.csv [--delay 60] [--limit N]

Before raising --delay below 60s on a real institutional mail relay: check your inbox
for bounces after the first 5-10 sends, not just this script's own success/fail count.
SMTP accepting a message only means the relay took it — a bad address still bounces
back to your inbox minutes later as a separate message.
"""
import argparse
import csv
import os
import smtplib
import ssl
import sys
import time
from datetime import date, datetime
from email.charset import QP, Charset
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

UTF8_QP = Charset("utf-8")
UTF8_QP.body_encoding = QP


def require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        sys.exit(f"Missing required environment variable: {name} (see .env.example)")
    return val


def build_html(body: str) -> str:
    import html as _h
    paras = [p.strip() for p in body.split("\n\n") if p.strip()]
    out = "".join(f"<p>{_h.escape(p).replace(chr(10), '<br>')}</p>" for p in paras)
    return f'<div style="font-family:sans-serif;font-size:14px;line-height:1.5">{out}</div>'


def build_message(row: dict, sender_name: str, sender_email: str, resume_path: Path):
    attach = resume_path if resume_path and resume_path.exists() else None
    if attach:
        msg, alt = MIMEMultipart("mixed"), MIMEMultipart("alternative")
    else:
        msg = MIMEMultipart("alternative")
        alt = msg

    msg["From"] = formataddr((sender_name, sender_email))
    msg["To"] = row["email"]
    msg["Subject"] = row["subject"]
    msg["Reply-To"] = sender_email
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()

    body = row["email_body"]
    if not body.endswith("\n"):
        body += "\n"
    alt.attach(MIMEText(body, "plain", UTF8_QP))
    alt.attach(MIMEText(build_html(body), "html", UTF8_QP))

    if attach:
        msg.attach(alt)
        part = MIMEApplication(attach.read_bytes(), _subtype="pdf")
        part.add_header("Content-Disposition", "attachment", filename=attach.name)
        msg.attach(part)
    return msg


def log_send(log_path: Path, row: dict, batch: str, status: str, error: str = "") -> None:
    exists = log_path.exists()
    fields = ["timestamp", "date", "batch", "name", "email", "subject", "status", "error"]
    with open(log_path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if not exists:
            w.writeheader()
        w.writerow({
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "date": date.today().isoformat(), "batch": batch,
            "name": row.get("name", ""), "email": row["email"],
            "subject": row["subject"], "status": status, "error": error,
        })


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--batch", required=True, help="path to the batch CSV")
    ap.add_argument("--delay", type=int, default=60, help="seconds between sends (default 60)")
    ap.add_argument("--limit", type=int, default=None, help="send only the first N queued rows")
    args = ap.parse_args()

    sender_name = require_env("SENDER_NAME")
    sender_email = require_env("SENDER_EMAIL")
    sender_token = require_env("SENDER_APP_TOKEN")
    smtp_server = require_env("SMTP_SERVER")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    resume_path = Path(os.environ.get("RESUME_PATH", "./resume.pdf"))

    batch_path = Path(args.batch)
    batch_name = batch_path.stem
    log_path = batch_path.parent / f"sent_log_{batch_name}.csv"

    already_sent = set()
    if log_path.exists():
        with open(log_path, newline="", encoding="utf-8") as f:
            already_sent = {r["email"] for r in csv.DictReader(f) if r.get("status") == "sent"}

    queue = []
    with open(batch_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("send", "").strip().lower() != "yes":
                continue
            if row["email"] in already_sent:
                continue
            if not (row.get("subject") and row.get("email_body")):
                continue
            queue.append(row)
    if args.limit:
        queue = queue[: args.limit]

    print(f"Queued: {len(queue)}")
    if not queue:
        return

    ctx = ssl.create_default_context()
    smtp = smtplib.SMTP(smtp_server, smtp_port, timeout=30)
    smtp.ehlo()
    smtp.starttls(context=ctx)
    smtp.ehlo()
    smtp.login(sender_email, sender_token)
    print(f"Connected as {sender_email}.")

    ok = fail = 0
    for i, row in enumerate(queue, 1):
        print(f"[{i}/{len(queue)}] {row.get('name', ''):<32} {row['email']}")
        try:
            msg = build_message(row, sender_name, sender_email, resume_path)
            smtp.send_message(msg)
            ok += 1
            print("  sent")
            log_send(log_path, row, batch_name, "sent")
        except smtplib.SMTPRecipientsRefused as e:
            fail += 1
            print(f"  refused: {e.recipients}")
            log_send(log_path, row, batch_name, "refused", str(e.recipients))
        except Exception as e:
            fail += 1
            print(f"  failed: {type(e).__name__}: {e}")
            log_send(log_path, row, batch_name, "failed", str(e))

        if i < len(queue):
            time.sleep(args.delay)

    smtp.quit()
    print(f"Done. sent={ok} failed={fail}")


if __name__ == "__main__":
    main()
