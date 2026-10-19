# -*- coding: utf-8 -*-
"""Phase 4 tests with fake SMTP, IMAP, GitHub Issues and a fake clock. No network, no real mail.

Run from the repo root:  .venv/bin/python -m unittest discover -s tests -v
"""
import email
import json
import os
import smtplib
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from runner import crypto, dashboard, mailio, tick  # noqa: E402
from runner.state import State, parse, text_hash  # noqa: E402
from schedule import publish  # noqa: E402

UTC = timezone.utc
SLOT = datetime(2026, 10, 26, 7, 30, tzinfo=UTC)  # Mon 08:30 Europe/Berlin (CET)


class Clock:
    def __init__(self, start):
        self.t = start

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += timedelta(seconds=seconds)


class FakeSmtp:
    """Records sends; can fail the n-th send with an exception (once)."""
    def __init__(self, clock, fail_on=None, exc=None):
        self.clock, self.fail_on, self.exc, self.sent, self.opened = clock, fail_on, exc, [], 0

    def factory(self):
        self.opened += 1
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def send(self, msg):
        if self.fail_on is not None and len(self.sent) + 1 == self.fail_on and self.exc is not None:
            exc, self.exc = self.exc, None
            raise exc
        self.sent.append((self.clock.now(), msg))


class FakeImap:
    def __init__(self):
        self.appended = []

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren \\Sent) "/" "Sent"']

    def append(self, folder, flags, date, data):
        self.appended.append(folder)
        return "OK", [b""]


class FakeIssues:
    def __init__(self):
        self.opened, self.labels = [], []

    def open(self, title, body, labels):
        self.opened.append((title, labels))
        return 100 + len(self.opened)

    def labelled(self, label):
        return self.labels


class Cfg:
    dry_run_to, sender_email, sender_name, chain = "", "me@iitb.example", "Ruturva Vaghasia", False


def make_state(tmp: Path, n=3, key=None, now=None):
    """Three Berlin emails in one 08:30 slot, built through publish.export like a real queue."""
    key = key or os.urandom(32)
    resume = tmp / "cv.pdf"
    resume.write_bytes(b"%PDF-1.4 fake")
    entries, drafts, profs = [], {}, {}
    for i in range(1, n + 1):
        pid, to = f"P{i:04d}", f"p{i}@uni.example"
        body = f"Dear Prof. {i},\n\nBody {i}.\n"
        drafts[pid] = {"subject": f"Subject {i}", "body": body, "attachment": "cv.pdf"}
        profs[pid] = {"name": f"Prof {i}", "university": "U", "country": "DE", "env_domain": "hydrology"}
        entries.append({"id": pid, "to": to, "sha256": text_hash(to, f"Subject {i}", "cv.pdf", body),
                        "queue_index": i, "timezone": "Europe/Berlin", "slot_utc": "2026-10-26T07:30:00Z"})
    st = publish.export(entries, drafts, profs, key, state_root=tmp / "state", resume_path=resume,
                        now=now or SLOT - timedelta(hours=3))
    return st, key


def run_send(st, key, clock, smtp, issues=None, imap=None, cfg=Cfg()):
    issues, imap = issues or FakeIssues(), imap or FakeImap()
    checkpoints = []
    n = tick.send_due(st, cfg, key, b"%PDF", smtp.factory, lambda: imap, issues, clock.now, clock.sleep,
                      lambda: checkpoints.append(clock.now()))
    return n, issues, imap, checkpoints


class SendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.st, self.key = make_state(self.tmp)
        self.clock = Clock(SLOT - timedelta(minutes=25))  # a tick starting 25 min early

    def test_one_per_minute_and_checkpoint_after_each(self):
        smtp = FakeSmtp(self.clock)
        n, issues, imap, cps = run_send(self.st, self.key, self.clock, smtp)
        self.assertEqual(n, 3)
        self.assertEqual([t for t, _ in smtp.sent], [SLOT, SLOT + timedelta(minutes=1), SLOT + timedelta(minutes=2)])
        self.assertEqual(len(cps), 3)
        self.assertTrue(all(r["status"] == "sent" for r in self.st.emails))
        self.assertTrue(all(self.st.contact(r["id"]).get("message_id") for r in self.st.emails))
        self.assertEqual(imap.appended, ['"Sent"'] * 3)
        self.assertEqual(smtp.sent[0][1]["To"], "p1@uni.example")
        self.assertEqual(issues.opened, [])

    def test_attachment_and_plain_body(self):
        smtp = FakeSmtp(self.clock)
        run_send(self.st, self.key, self.clock, smtp)
        msg = smtp.sent[0][1]
        parts = list(msg.iter_attachments())
        self.assertEqual([p.get_filename() for p in parts], ["cv.pdf"])
        self.assertIn("Body 1.", msg.get_body().get_content())

    def test_changed_text_is_pulled_not_sent(self):
        self.st.by_id("P0002")["body"] += "tampered"
        smtp = FakeSmtp(self.clock)
        _, issues, _, _ = run_send(self.st, self.key, self.clock, smtp)
        self.assertEqual(self.st.by_id("P0002")["status"], "pulled")
        self.assertEqual(len(smtp.sent), 2)
        self.assertEqual(issues.opened[0][1], ["needs-ruturva"])

    def test_smtp_rejection_stops_the_slot(self):
        smtp = FakeSmtp(self.clock, fail_on=2, exc=smtplib.SMTPDataError(550, b"rate limit"))
        n, issues, _, _ = run_send(self.st, self.key, self.clock, smtp)
        self.assertEqual(n, 1)
        self.assertEqual([r["status"] for r in self.st.emails], ["sent", "failed", "failed"])
        self.assertIn("550", issues.opened[0][0])

    def test_dropped_connection_reconnects_once(self):
        smtp = FakeSmtp(self.clock, fail_on=2, exc=smtplib.SMTPServerDisconnected("idle"))
        n, issues, _, _ = run_send(self.st, self.key, self.clock, smtp)
        self.assertEqual(n, 3)
        self.assertEqual(issues.opened, [])

    def test_dry_run_goes_to_own_address(self):
        cfg = Cfg()
        cfg.dry_run_to = "me@iitb.example"
        smtp = FakeSmtp(self.clock)
        run_send(self.st, self.key, self.clock, smtp, cfg=cfg)
        self.assertTrue(all(m["To"] == "me@iitb.example" and m["Subject"].startswith("[DRY RUN") for _, m in smtp.sent))
        self.assertTrue(all(r["status"] == "dry_run_sent" for r in self.st.emails))
        self.assertFalse(any(self.st.contact(r["id"]).get("message_id") for r in self.st.emails))

    def test_dry_run_records_are_flagged_and_keep_message_id(self):
        for r in self.st.emails:
            r["dry_run"] = True
        smtp = FakeSmtp(self.clock)
        run_send(self.st, self.key, self.clock, smtp)
        self.assertTrue(all(m["Subject"].startswith("[DRY RUN DRY") or m["Subject"].startswith("[DRY RUN P") for _, m in smtp.sent))
        self.assertTrue(all(r["status"] == "dry_run_sent" for r in self.st.emails))
        self.assertTrue(all(self.st.contact(r["id"]).get("message_id") for r in self.st.emails))

    def test_nothing_due_outside_lookahead(self):
        self.clock.t = SLOT - timedelta(hours=2)
        n, *_ = run_send(self.st, self.key, self.clock, FakeSmtp(self.clock))
        self.assertEqual(n, 0)

    def test_missed_by_over_an_hour_rolls_to_next_day(self):
        tick.reschedule_missed(self.st, SLOT + timedelta(minutes=125))
        self.assertTrue(all(r["send_local"].startswith("2026-10-27T08:3") for r in self.st.emails))
        self.assertTrue(all(r["events"][-1]["event"] == "rescheduled" for r in self.st.emails))


class LongRunTests(unittest.TestCase):
    """The self-chaining loop: one run lives for hours, sends on the minute, then hands over."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.st, self.key = make_state(self.tmp)
        self._poll, mailio.poll = mailio.poll, lambda *a, **k: []
        self._budget = tick.RUN_BUDGET

    def tearDown(self):
        mailio.poll, tick.RUN_BUDGET = self._poll, self._budget

    def go(self, start, budget=None):
        if budget:
            tick.RUN_BUDGET = budget
        clock, smtp, issues = Clock(start), None, FakeIssues()
        smtp = FakeSmtp(clock)
        cfg = Cfg()
        cfg.chain = True
        cps, dash = [], []
        tick.run(self.st, cfg, self.key, issues, smtp.factory, lambda: FakeImapWithLogout(), clock.now,
                 clock.sleep, lambda: cps.append(clock.now()), lambda: dash.append(clock.now()))
        return clock, smtp, cps, dash

    def test_waits_hours_then_sends_on_the_minute(self):
        clock, smtp, cps, dash = self.go(SLOT - timedelta(hours=3))
        self.assertEqual([t for t, _ in smtp.sent], [SLOT + timedelta(minutes=i) for i in range(3)])
        self.assertTrue(all(r["status"] == "sent" for r in self.st.emails))
        self.assertGreaterEqual(clock.t, SLOT - timedelta(hours=3) + tick.RUN_BUDGET - tick.POLL_EVERY)
        self.assertTrue(dash)  # dashboard data rewritten after changes

    def test_stops_at_budget_mid_slot_and_leaves_the_rest(self):
        clock, smtp, *_ = self.go(SLOT - timedelta(minutes=20), budget=timedelta(minutes=21, seconds=30))
        self.assertEqual(len(smtp.sent), 2)  # 08:30 and 08:31; 08:32 is past this run's stop time
        self.assertEqual([r["status"] for r in self.st.emails], ["sent", "sent", "scheduled"])

    def test_work_left_keeps_the_chain_alive(self):
        self.assertTrue(tick.work_left(self.st, SLOT))  # scheduled emails
        for r in self.st.emails:
            r["status"] = "sent"
            r["events"].append({"event": "sent", "ts_utc": "2026-10-26T07:30:00Z"})
        self.assertTrue(tick.work_left(self.st, SLOT + timedelta(days=29)))   # still watching replies
        self.assertFalse(tick.work_left(self.st, SLOT + timedelta(days=31)))  # done


class FakeImapWithLogout(FakeImap):
    def logout(self):
        pass


class FolderTests(unittest.TestCase):
    class Lister:
        def __init__(self, lines):
            self.lines = lines

        def list(self):
            return "OK", [l.encode() for l in self.lines]

    def test_bare_dotted_names(self):  # e.g. Dovecot: (\HasNoChildren) "." INBOX.Sent
        imap = self.Lister(['(\\HasNoChildren) "." INBOX', '(\\HasNoChildren) "." INBOX.Sent'])
        self.assertEqual(mailio.find_sent_folder(imap), "INBOX.Sent")

    def test_sent_flag_wins(self):
        imap = self.Lister(['(\\HasNoChildren) "/" "Archive"', '(\\HasNoChildren \\Sent) "/" "Sent Items"'])
        self.assertEqual(mailio.find_sent_folder(imap), "Sent Items")

    def test_no_sent_folder_reports_names(self):
        class Imap(self.Lister):
            def append(self, *a):
                raise AssertionError("must not append without a Sent folder")
        why = mailio.append_sent(Imap(['(\\HasNoChildren) "/" INBOX', '(\\HasNoChildren) "/" Trash']), mailio.EmailMessage())
        self.assertIn("folders: INBOX, Trash", why)


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.st, self.key = make_state(self.tmp)
        clock = Clock(SLOT - timedelta(minutes=5))
        run_send(self.st, self.key, clock, FakeSmtp(clock))
        self.ids = {self.st.contact(r["id"])["message_id"]: r["id"] for r in self.st.emails}

    def test_reply_kinds(self):
        mids = list(self.ids)
        found = [{"msgid": "<r1@x>", "kind": "human", "id": self.ids[mids[0]]},
                 {"msgid": "<r2@x>", "kind": "bounce", "id": self.ids[mids[1]]},
                 {"msgid": "<r3@x>", "kind": "auto_reply", "id": self.ids[mids[2]]}]
        original, mailio.poll = mailio.poll, lambda *a, **k: found
        try:
            issues = FakeIssues()
            tick.poll_inbox(self.st, None, issues, SLOT + timedelta(hours=2))
        finally:
            mailio.poll = original
        r1, r2, r3 = (self.st.by_id(f"P000{i}") for i in (1, 2, 3))
        self.assertEqual((r1["status"], r1["reply_label"], r1["issue"]), ("replied", "unclear", 101))
        self.assertEqual(issues.opened, [("New reply #1", ["reply"])])  # opaque title, no name
        self.assertEqual((r2["status"], r2["reply_label"]), ("bounced", "bounce"))
        self.assertEqual((r3["status"], r3["reply_label"]), ("sent", "auto_reply"))
        self.assertEqual(len(self.st.private["seen"]), 3)

    def test_dry_run_records_never_match_by_sender(self):
        for r in self.st.emails:
            r["dry_run"] = True
        captured = {}
        original = mailio.poll
        mailio.poll = lambda imap, ids, addrs, since, seen: captured.update(ids=ids, addrs=addrs) or []
        try:
            tick.poll_inbox(self.st, None, FakeIssues(), SLOT + timedelta(hours=1))
        finally:
            mailio.poll = original
        self.assertEqual(captured["addrs"], {})       # own address is never used to match
        self.assertEqual(len(captured["ids"]), 3)     # but replies to the test emails still match

    def test_labels_sync_back(self):
        self.st.by_id("P0001").update(issue=101, reply_label="unclear", status="replied")
        issues = FakeIssues()
        issues.labels = [{"number": 101, "labels": ["reply", "positive"]}]
        tick.sync_labels(self.st, issues, SLOT)
        self.assertEqual(self.st.by_id("P0001")["reply_label"], "positive")


class HeartbeatTests(unittest.TestCase):
    def test_gap_alert_once_and_hourly_commit(self):
        st, issues = State(Path(tempfile.mkdtemp())), FakeIssues()
        now = SLOT
        tick.heartbeat(st, now, issues, now - timedelta(minutes=55))
        tick.heartbeat(st, now + timedelta(minutes=10), issues, now - timedelta(minutes=55))
        self.assertEqual(len(issues.opened), 1)
        st.changed = False
        tick.heartbeat(st, now + timedelta(minutes=20), issues, now + timedelta(minutes=10))
        self.assertFalse(st.changed)  # no gap, and the hourly heartbeat commit isn't due yet
        tick.heartbeat(st, now + timedelta(minutes=61), issues, now + timedelta(minutes=55))
        self.assertTrue(st.changed)


def hdr(**h):
    return email.message_from_string("".join(f"{k.replace('_', '-')}: {v}\n" for k, v in h.items()) + "\n")


class FakePollImap:
    """Answers UID SEARCH / FETCH with canned header blocks (and one bounce body)."""
    def __init__(self, messages, bounce_body=b""):
        self.messages, self.bounce_body, self.fetched_bodies = messages, bounce_body, 0

    def select(self, box, readonly=False):
        assert readonly, "the poll must never open the inbox read-write"
        return "OK", [b"3"]

    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            return "OK", [b" ".join(str(i).encode() for i in range(1, len(self.messages) + 1))]
        spec = args[1]
        if "TEXT" in spec:
            self.fetched_bodies += 1
            return "OK", [(f"{args[0]} (UID {args[0]} BODY[TEXT]".encode(), self.bounce_body)]
        rows = []
        for uid in args[0].split(","):
            rows.append((f"{uid} (UID {uid} BODY[HEADER]".encode(), self.messages[int(uid) - 1].encode()))
            rows.append(b")")
        return "OK", rows


class PollTests(unittest.TestCase):
    ours = {"<sent1@iitb>": "P0001", "<sent2@iitb>": "P0002"}
    addrs = {"p1@uni.example": "P0001", "p2@uni.example": "P0002", "p3@uni.example": "P0003"}

    def test_only_our_threads_are_kept(self):
        msgs = [
            "Message-ID: <a@x>\nIn-Reply-To: <sent1@iitb>\nFrom: P1 <p1@uni.example>\nSubject: Re: hi\n\n",
            "Message-ID: <b@x>\nFrom: Newsletter <news@shop.example>\nSubject: Sale\n\n",          # unrelated
            "Message-ID: <c@x>\nFrom: p3@uni.example\nSubject: hello\n\n",                         # by sender
            "Message-ID: <d@x>\nFrom: MAILER-DAEMON@iitb.example\nContent-Type: multipart/report; "
            "report-type=delivery-status\nSubject: Undelivered\n\n",
        ]
        imap = FakePollImap(msgs, bounce_body=b"Original-Message-ID: <sent2@iitb>")
        got = mailio.poll(imap, self.ours, self.addrs, "26-Oct-2026", set())
        self.assertEqual({(g["msgid"], g["kind"], g["id"]) for g in got},
                         {("<a@x>", "human", "P0001"), ("<c@x>", "human", "P0003"), ("<d@x>", "bounce", "P0002")})
        self.assertEqual(imap.fetched_bodies, 1)  # only the delivery report's body was opened

    def test_seen_messages_are_skipped(self):
        msgs = ["Message-ID: <a@x>\nIn-Reply-To: <sent1@iitb>\nFrom: p1@uni.example\n\n"]
        self.assertEqual(mailio.poll(FakePollImap(msgs), self.ours, self.addrs, "26-Oct-2026", {"<a@x>"}), [])

    def test_classify(self):
        self.assertEqual(mailio.classify(hdr(From="postmaster@x.org", Subject="Delivery failure")), "bounce")
        self.assertEqual(mailio.classify(hdr(From="a@u.edu", Auto_Submitted="auto-replied")), "auto_reply")
        self.assertEqual(mailio.classify(hdr(From="a@u.edu", Subject="Out of office until Monday")), "auto_reply")
        self.assertEqual(mailio.classify(hdr(From="a@u.edu", Subject="Re: Research internship")), "human")


class PublicDataTests(unittest.TestCase):
    def test_no_addresses_in_public_state_or_dashboard(self):
        tmp = Path(tempfile.mkdtemp())
        st, key = make_state(tmp)
        public = (tmp / "state" / "emails.json").read_text()
        self.assertNotIn("@uni.example", public)
        private = json.loads(crypto.decrypt((tmp / "state" / "private.json.enc").read_bytes(), key))
        self.assertEqual(private["contacts"]["P0001"]["to"], "p1@uni.example")
        self.assertEqual(st.resume(key), b"%PDF-1.4 fake")
        data = dashboard.build(tmp / "state")
        self.assertNotIn("@uni.example", json.dumps(data))
        blob = crypto.encrypt_for_browser(data, "pass phrase")
        self.assertEqual(crypto.decrypt_from_browser_format(blob, "pass phrase")["counts"], {"scheduled": 3})
        with self.assertRaises(Exception):
            crypto.decrypt_from_browser_format(blob, "wrong")

    def test_publish_spaces_one_minute(self):
        st, _ = make_state(Path(tempfile.mkdtemp()))
        self.assertEqual(sorted(r["send_local"][11:16] for r in st.emails), ["08:30", "08:31", "08:32"])


if __name__ == "__main__":
    unittest.main()
