# -*- coding: utf-8 -*-
"""Phase 4: one unattended run of the sender, started by GitHub Actions every 10 minutes.

Order of work in a tick:
  1. heartbeat: if the previous workflow run finished more than 40 min ago (asked from GitHub),
     open a `needs-ruturva` Issue;
  2. reschedule: an email more than 1 h past its minute rolls to the next valid day
     (amendment 6), respecting the 100-per-slot cap, then slots are re-spaced;
  3. send: everything due within the next 30 minutes, waiting until each email's own minute
     (amendment 12: one per minute). Before each send the text is re-hashed and must match the
     hash locked at queue time (hard rule 3). After each send the state is saved and committed
     at once (`checkpoint`), so a crashed or cancelled run can never send the same email twice.
     The first SMTP error stops the slot: that email and the rest of its slot become `failed`
     and a `needs-ruturva` Issue is opened (hard rule 8). Nothing is retried silently;
  4. inbox: poll replies to our threads (mailio.poll), tag bounces and auto-replies, and open an
     opaque `New reply #N` Issue per human reply; copy Ruturva's positive/negative labels back;
  5. save (and let the workflow commit) only when something changed, plus an hourly
     heartbeat commit that also keeps the scheduled workflow from being auto-disabled.

DRY_RUN_TO (Phase 5): every email goes to that address instead, with the subject prefixed,
and is marked `dry_run_sent` rather than `sent`.
"""
import os
import smtplib
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runner import crypto, mailio  # noqa: E402
from runner.state import State, iso, parse, text_hash  # noqa: E402
from schedule.scheduler import LATE_WINDOW, next_slot  # noqa: E402
from schedule.slots import assign_send_times  # noqa: E402

LOOKAHEAD = timedelta(minutes=30)
HEARTBEAT_GAP = timedelta(minutes=40)
HEARTBEAT_COMMIT_EVERY = timedelta(hours=1)
SLOT_CAP = 100
SMTP_IDLE = 120  # seconds; reconnect rather than reuse an idle SMTP session
REPLY_ISSUE_BODY = ("A reply arrived for one of the sent emails. Read it in your inbox, then add the "
                    "label `positive` or `negative` to this issue. (No names or reply text are posted here.)")


class Config:
    def __init__(self, env=os.environ):
        self.sender_email = env.get("SENDER_EMAIL", "")
        self.sender_name = env.get("SENDER_NAME") or "Ruturva Vaghasia"
        self.user = env.get("SMTP_USER") or self.sender_email
        self.token = env.get("SENDER_APP_TOKEN", "")
        self.smtp_host, self.smtp_port = env.get("SMTP_SERVER") or "smtp-auth.iitb.ac.in", int(env.get("SMTP_PORT") or 587)
        self.imap_host, self.imap_port = env.get("IMAP_SERVER") or "imap.iitb.ac.in", int(env.get("IMAP_PORT") or 993)
        self.dry_run_to = env.get("DRY_RUN_TO", "").strip()
        self.repo = env.get("GITHUB_REPOSITORY", "darkhound667/cold-mailer")
        self.git_push = env.get("GIT_PUSH") == "1"


def git_checkpoint(st: State, key: bytes, cfg: Config) -> None:
    """Save state and, on the runner, commit + push it immediately."""
    st.save(key)
    if not cfg.git_push:
        return
    subprocess.run(["git", "add", "state"], check=True)
    if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
        return
    subprocess.run(["git", "commit", "-q", "-m", "tick: state update"], check=True)
    for attempt in range(3):  # the laptop may have pushed a new queue meanwhile
        if subprocess.run(["git", "push", "-q"]).returncode == 0:
            return
        subprocess.run(["git", "pull", "-q", "--rebase"], check=True)
    raise RuntimeError("could not push state after 3 attempts")


def respace(st: State) -> None:
    active = [r for r in st.emails if r["status"] == "scheduled"]
    spaced = assign_send_times([{"id": r["id"], "queue_index": r["queue_index"], "timezone": r["timezone"],
                                 "slot_utc": parse(r["slot_utc"])} for r in active])
    by_id = {s["id"]: s for s in spaced}
    for r in active:
        s = by_id[r["id"]]
        new = (iso(s["send_utc"]), s["send_local"].isoformat())
        if (r.get("send_utc"), r.get("send_local")) != new:
            r["send_utc"], r["send_local"], r["position"] = new[0], new[1], s["position"]
            st.changed = True


def reschedule_missed(st: State, now: datetime) -> None:
    per_slot = Counter(r["slot_utc"] for r in st.emails if r["status"] == "scheduled")
    for r in sorted((r for r in st.emails if r["status"] == "scheduled"), key=lambda r: r["queue_index"]):
        if now <= parse(r["send_utc"]) + LATE_WINDOW:
            continue
        per_slot[r["slot_utc"]] -= 1
        slot = next_slot(r["timezone"], r["country"], now)
        while per_slot[iso(slot)] >= SLOT_CAP:
            slot = next_slot(r["timezone"], r["country"], slot)
        per_slot[iso(slot)] += 1
        old = r["send_local"]
        r["slot_utc"] = iso(slot)
        st.event(r, "rescheduled", now, f"missed {old} by more than 1 h")
    respace(st)


def send_due(st, cfg, key, resume, smtp_factory, imap_factory, issues, now_fn, sleep_fn, checkpoint):
    """Send every email due within LOOKAHEAD, one at its own minute. Returns the number sent."""
    sent, smtp, imap, smtp_used = 0, None, None, None
    try:
        while True:
            due = sorted((r for r in st.emails if r["status"] == "scheduled"
                          and parse(r["send_utc"]) <= now_fn() + LOOKAHEAD), key=lambda r: r["send_utc"])
            if not due:
                return sent
            rec = due[0]
            wait = (parse(rec["send_utc"]) - now_fn()).total_seconds()
            if wait > 0:
                sleep_fn(wait)
            to = st.contact(rec["id"]).get("to", "")
            if text_hash(to, rec["subject"], rec["attachment"], rec["body"]) != rec["sha256"]:
                rec["status"] = "pulled"
                st.event(rec, "pulled", now_fn(), "text differs from the approved version")
                issues.open(f"Email {rec['id']} changed after approval", "Not sent. Re-queue it to approve the new text.",
                            ["needs-ruturva"])
                checkpoint()
                continue
            dry = bool(cfg.dry_run_to)
            msg = mailio.build_message(cfg.dry_run_to or to,
                                       f"[DRY RUN {rec['id']}] {rec['subject']}" if dry else rec["subject"],
                                       rec["body"], rec["attachment"], resume, cfg.sender_name, cfg.sender_email)
            try:
                if smtp is not None and (now_fn() - smtp_used).total_seconds() > SMTP_IDLE:
                    smtp.__exit__(None, None, None)
                    smtp = None
                if smtp is None:
                    smtp = smtp_factory().__enter__()
                try:
                    smtp.send(msg)
                except smtplib.SMTPServerDisconnected:  # a dropped idle connection, not a rejection
                    smtp = smtp_factory().__enter__()
                    smtp.send(msg)
                smtp_used = now_fn()
            except Exception as e:
                code = getattr(e, "smtp_code", "")
                now = now_fn()
                rec["status"] = "failed"
                st.event(rec, "failed", now, f"{type(e).__name__} {code}".strip())
                for other in (r for r in st.emails if r["status"] == "scheduled" and r["slot_utc"] == rec["slot_utc"]):
                    other["status"] = "failed"
                    st.event(other, "failed", now, f"slot stopped after the error on {rec['id']}")
                issues.open(f"Sending stopped: {type(e).__name__} {code}".strip(),
                            f"The slot {rec['slot_utc']} was stopped at {rec['id']} and its remaining emails "
                            "were marked failed. Nothing will be retried until you decide (re-queue them).",
                            ["needs-ruturva"])
                checkpoint()
                return sent
            now = now_fn()
            if dry:
                rec["status"] = "dry_run_sent"
                st.event(rec, "dry_run_sent", now, "sent to the dry-run address")
            else:
                rec["status"] = "sent"
                st.contact(rec["id"])["message_id"] = msg["Message-ID"]
                st.event(rec, "sent", now)
            try:
                if imap is None:
                    imap = imap_factory()
                if not mailio.append_sent(imap, msg):
                    st.event(rec, "sent_copy_failed", now, "no Sent folder found")
            except Exception as e:
                st.event(rec, "sent_copy_failed", now, type(e).__name__)
                imap = None
            sent += 1
            checkpoint()  # persist before the next email: a crash can never cause a re-send
    finally:
        if smtp is not None:
            smtp.__exit__(None, None, None)


def poll_inbox(st: State, imap, issues, now: datetime) -> None:
    sent_like = {"sent", "replied", "bounced"}
    our_ids = {c["message_id"]: pid for pid, c in st.private["contacts"].items() if c.get("message_id")}
    our_addrs = {c["to"].lower(): pid for pid, c in st.private["contacts"].items()
                 if c.get("to") and st.by_id(pid)["status"] in sent_like}
    if not our_ids:
        return
    first = min(parse(e["ts_utc"]) for r in st.emails for e in r.get("events", []) if e["event"] == "sent")
    seen = set(st.private.setdefault("seen", []))
    for f in mailio.poll(imap, our_ids, our_addrs, first.strftime("%d-%b-%Y"), seen):
        st.private["seen"].append(f["msgid"])
        st.changed = True
        rec = st.by_id(f["id"])
        if f["kind"] == "bounce":
            rec["status"], rec["reply_label"] = "bounced", "bounce"
            st.event(rec, "bounced", now)
        elif f["kind"] == "auto_reply":
            rec["reply_label"] = rec.get("reply_label") or "auto_reply"
            st.event(rec, "auto_reply", now)
        elif rec.get("issue"):
            st.event(rec, "replied_again", now)
        else:
            n = 1 + sum(1 for r in st.emails if r.get("issue"))
            rec["issue"] = issues.open(f"New reply #{n}", REPLY_ISSUE_BODY, ["reply"])
            rec["status"], rec["reply_label"] = "replied", "unclear"
            st.event(rec, "replied", now)


def sync_labels(st: State, issues, now: datetime) -> None:
    by_issue = {r["issue"]: r for r in st.emails if r.get("issue")}
    for i in issues.labelled("reply"):
        rec = by_issue.get(i["number"])
        label = next((l for l in ("positive", "negative") if l in i["labels"]), None)
        if rec and label and rec.get("reply_label") != label:
            rec["reply_label"] = label
            st.event(rec, f"labelled_{label}", now)


def heartbeat(st: State, now: datetime, issues, prev_run_utc) -> None:
    """Gap check against the previous workflow run's start time (from GitHub, needs no commit),
    plus the hourly heartbeat commit that keeps the schedule from being auto-disabled."""
    hb = st.heartbeat
    if prev_run_utc and now - prev_run_utc > HEARTBEAT_GAP and hb.get("gap_reported") != iso(prev_run_utc):
        issues.open("Runner gap", f"No tick ran between {iso(prev_run_utc)} and {iso(now)}. Emails that missed "
                    "their slot by more than 1 h are being moved to the next valid day.", ["needs-ruturva"])
        hb["gap_reported"] = iso(prev_run_utc)
        st.changed = True
    last_commit = hb.get("last_committed_utc")
    if not last_commit or now - parse(last_commit) >= HEARTBEAT_COMMIT_EVERY:
        hb["last_committed_utc"] = iso(now)
        st.changed = True


def main() -> None:
    from runner.ghissues import GhIssues

    cfg = Config()
    key = crypto.load_key()
    st = State().load(key)
    issues = GhIssues(cfg.repo)
    now_fn = lambda: datetime.now(timezone.utc)
    checkpoint = lambda: git_checkpoint(st, key, cfg)

    heartbeat(st, now_fn(), issues, issues.previous_run_utc())
    reschedule_missed(st, now_fn())
    if any(r["status"] == "scheduled" for r in st.emails):
        send_due(st, cfg, key, st.resume(key),
                 lambda: mailio.Smtp(cfg.smtp_host, cfg.smtp_port, cfg.user, cfg.token),
                 lambda: mailio.imap_login(cfg.imap_host, cfg.imap_port, cfg.user, cfg.token),
                 issues, now_fn, time.sleep, checkpoint)
    if st.private["contacts"] and not cfg.dry_run_to:
        imap = mailio.imap_login(cfg.imap_host, cfg.imap_port, cfg.user, cfg.token)
        try:
            poll_inbox(st, imap, issues, now_fn())
        finally:
            imap.logout()
        st.heartbeat["last_poll_utc"] = iso(now_fn())
        sync_labels(st, issues, now_fn())
    if st.changed:
        checkpoint()
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"changed={'1' if st.changed else '0'}\n")
    print(f"tick done: changed={st.changed}")


if __name__ == "__main__":
    main()
