# -*- coding: utf-8 -*-
"""Phase 4: the unattended sender on GitHub Actions.

GitHub throttles scheduled workflows on quiet repos (the dry run saw hours between cron runs),
so each run is long-lived: it loops every POLL_EVERY for up to RUN_BUDGET (under GitHub's 6 h
limit) and then starts its own successor with workflow_dispatch. The 10-minute cron is only a
backup restart. One run at a time (workflow concurrency group). Each loop does:
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

Dry runs (Phase 5): records with "dry_run": true (scripts/dry_run_queue.py) are addressed to
Ruturva himself; they, and every email when DRY_RUN_TO is set, get a "[DRY RUN id]" subject and
the status `dry_run_sent` rather than `sent`.
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
POLL_EVERY = timedelta(minutes=1)     # inbox poll / send check interval inside one long run (Ruturva, 2026-10-20: every minute)
RUN_BUDGET = timedelta(hours=5, minutes=40)  # stop before GitHub's 6 h job limit, then hand over
REPLY_WATCH_DAYS = 30                 # keep polling for replies this long after the last send
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
        self.chain = env.get("CHAIN") == "1"  # long-lived self-chaining mode (the workflow sets it)


def git_checkpoint(st: State, key: bytes, cfg: Config) -> None:
    """Save state and, on the runner, commit + push it immediately."""
    st.save(key)
    if not cfg.git_push:
        return
    subprocess.run(["git", "add", "state", "site/data.enc.json"], check=False)
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


def send_due(st, cfg, key, resume, smtp_factory, imap_factory, issues, now_fn, sleep_fn, checkpoint, stop_at=None):
    """Send every email due within LOOKAHEAD, one at its own minute. Returns the number sent."""
    sent, smtp, imap, smtp_used = 0, None, None, None
    try:
        while True:
            due = sorted((r for r in st.emails if r["status"] == "scheduled"
                          and parse(r["send_utc"]) <= now_fn() + LOOKAHEAD), key=lambda r: r["send_utc"])
            if not due:
                return sent
            rec = due[0]
            if stop_at is not None and parse(rec["send_utc"]) >= stop_at:
                return sent  # hand over: the successor run sends it (within the 1 h late window)
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
            # Phase 5 test records (scripts/dry_run_queue.py) are addressed to Ruturva himself.
            dry = bool(cfg.dry_run_to) or bool(rec.get("dry_run"))
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
                if rec.get("dry_run"):  # keep the Message-ID so a reply to it can test the Issue flow
                    st.contact(rec["id"])["message_id"] = msg["Message-ID"]
                st.event(rec, "dry_run_sent", now, "sent to the dry-run address")
            else:
                rec["status"] = "sent"
                st.contact(rec["id"])["message_id"] = msg["Message-ID"]
                st.event(rec, "sent", now)
            try:
                if imap is None:
                    imap = imap_factory()
                why = mailio.append_sent(imap, msg)
                if why:
                    st.event(rec, "sent_copy_failed", now, why)
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
    recs = {r["id"]: r for r in st.emails}  # contacts without a record yet (mid-sync) are skipped
    contacts = {pid: c for pid, c in st.private["contacts"].items() if pid in recs}
    our_ids = {c["message_id"]: pid for pid, c in contacts.items() if c.get("message_id")}
    # Test records go to Ruturva's own address: never match on that sender, or all his own mail
    # would look like replies. They still match on In-Reply-To/References.
    our_addrs = {a.strip().lower(): pid for pid, c in contacts.items()
                 if c.get("to") and recs[pid]["status"] in sent_like and not recs[pid].get("dry_run")
                 for a in c["to"].split(",")}  # "to" may hold two comma-separated addresses
    if not our_ids:
        return
    first = min(parse(e["ts_utc"]) for r in st.emails for e in r.get("events", [])
                if e["event"] in ("sent", "dry_run_sent"))
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


def work_left(st: State, now: datetime) -> bool:
    """Keep the chain alive while anything is scheduled, or replies may still come in (30 days)."""
    if any(r["status"] == "scheduled" for r in st.emails):
        return True
    sent = [parse(e["ts_utc"]) for r in st.emails for e in r.get("events", []) if e["event"] in ("sent", "dry_run_sent")]
    return bool(sent) and now - max(sent) < timedelta(days=REPLY_WATCH_DAYS)


def git_refresh(st: State, key: bytes, cfg: Config) -> None:
    """Pull the repo and reload the state in place, so a batch queued from the laptop while this
    run is alive is picked up at the next loop (not only by the successor run)."""
    if not cfg.git_push:
        return
    if st.changed:
        git_checkpoint(st, key, cfg)
        st.changed = False
    if subprocess.run(["git", "pull", "-q", "--rebase"]).returncode != 0:
        print("git pull failed; continuing with the state already loaded")
        return
    fresh = State(st.dir).load(key)
    st.emails, st.private, st.heartbeat = fresh.emails, fresh.private, fresh.heartbeat


def run(st, cfg, key, issues, smtp_factory, imap_factory, now_fn, sleep_fn, checkpoint, dashboard_fn,
        prev_run_utc=None, refresh=None) -> None:
    """One long-lived run: every POLL_EVERY, reload the state, reschedule, send what is due, poll
    the inbox and commit; stop before GitHub's job limit. With cfg.chain the caller then starts
    the successor."""
    start = now_fn()
    stop_at = start + RUN_BUDGET
    heartbeat(st, start, issues, prev_run_utc)
    while True:
        if refresh:
            refresh()
        resume = None  # re-read each loop: a new queue may bring a new resume
        reschedule_missed(st, now_fn())
        if any(r["status"] == "scheduled" for r in st.emails):
            resume = resume or st.resume(key)
            send_due(st, cfg, key, resume, smtp_factory, imap_factory, issues, now_fn, sleep_fn, checkpoint, stop_at)
        if st.private["contacts"] and not cfg.dry_run_to:
            try:
                imap = imap_factory()
            except OSError as e:  # DNS/network blip: skip this poll, the next loop tries again
                print(f"IMAP connect failed, poll skipped: {e}")
                imap = None
            if imap is not None:
                try:
                    poll_inbox(st, imap, issues, now_fn())
                finally:
                    try:
                        imap.logout()
                    except Exception:
                        pass
                st.heartbeat["last_poll_utc"] = iso(now_fn())
                sync_labels(st, issues, now_fn())
        hb = st.heartbeat.get("last_committed_utc")
        if not hb or now_fn() - parse(hb) >= HEARTBEAT_COMMIT_EVERY:  # hourly heartbeat commit
            st.heartbeat["last_committed_utc"] = iso(now_fn())
            st.changed = True
        if st.changed:
            dashboard_fn()
            checkpoint()
            st.changed = False
        if not cfg.chain or now_fn() + POLL_EVERY >= stop_at or not work_left(st, now_fn()):
            return
        sleep_fn(POLL_EVERY.total_seconds())


def dispatch_successor(cfg: Config) -> None:
    """Start the next run (workflow_dispatch from GITHUB_TOKEN is allowed to trigger a run)."""
    subprocess.run(["gh", "workflow", "run", "tick.yml", "--ref", "main", "-R", cfg.repo], check=True)
    print("successor run dispatched")


def main() -> None:
    from runner import dashboard
    from runner.ghissues import GhIssues

    cfg = Config()
    root = Path(__file__).resolve().parents[2]
    if not (root / "state" / "emails.json").exists():
        print("nothing queued yet: tick has nothing to do")  # cron keeps checking; no chain yet
        return
    key = crypto.load_key()
    st = State().load(key)
    issues = GhIssues(cfg.repo)
    now_fn = lambda: datetime.now(timezone.utc)
    run(st, cfg, key, issues,
        lambda: mailio.Smtp(cfg.smtp_host, cfg.smtp_port, cfg.user, cfg.token),
        lambda: mailio.imap_login(cfg.imap_host, cfg.imap_port, cfg.user, cfg.token),
        now_fn, time.sleep, lambda: git_checkpoint(st, key, cfg),
        lambda: dashboard.write(st.dir), issues.previous_run_utc(), lambda: git_refresh(st, key, cfg))
    if cfg.chain and work_left(st, now_fn()):
        dispatch_successor(cfg)
    print("run done")


if __name__ == "__main__":
    main()
