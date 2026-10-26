# -*- coding: utf-8 -*-
"""Conversation sync tests with a fake IMAP server. No network, no real mail.

Run from the repo root:  .venv/bin/python -m unittest discover -s tests -v
"""
import base64
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from runner import crypto, threads  # noqa: E402
from runner.state import State  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
PASS = "test-passphrase"
RESUME = b"%PDF-1.4 resume"


def mail(mid, frm, to, subject, body, date, refs="", attach=None, html=False):
    m = EmailMessage()
    m["Message-ID"], m["From"], m["To"], m["Subject"], m["Date"] = mid, frm, to, subject, date
    if refs:
        m["In-Reply-To"] = refs
    if html:
        m.set_content(body, subtype="html")
    else:
        m.set_content(body)
    if attach:
        name, data = attach
        m.add_attachment(data, maintype="application", subtype="pdf", filename=name)
    return m.as_bytes()


class FakeImap:
    def __init__(self, folders):
        self.folders, self.cur = folders, None  # {name: [raw bytes]}

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren \\Sent) "/" "Sent"']

    def select(self, name, readonly=False):
        self.cur = name.strip('"')
        return "OK", [b""]

    def uid(self, cmd, *args):
        msgs = self.folders[self.cur]
        if cmd == "SEARCH":
            return "OK", [b" ".join(str(i + 1).encode() for i in range(len(msgs)))]
        chunk, what = args
        rows = []
        for u in chunk.split(","):
            raw = msgs[int(u) - 1]
            if "HEADER.FIELDS" in what:
                head = raw.split(b"\n\n", 1)[0] + b"\n\n"
                rows += [(f"{u} (UID {u} BODY[HEADER.FIELDS] {{{len(head)}}}".encode(), head), b")"]
            else:
                rows += [(f"{u} (UID {u} BODY[] {{{len(raw)}}}".encode(), raw), b")"]
        return "OK", rows


def make_state():
    st = State(Path(tempfile.mkdtemp()))
    st.emails = [
        {"id": "P1", "status": "replied", "subject": "Research internship inquiry", "body": "Dear Prof. A,\n\nHello.\n",
         "attachment": "Resume.pdf", "timezone": "UTC",
         "events": [{"event": "sent", "ts_utc": "2026-10-20T08:00:00Z"}, {"event": "replied", "ts_utc": "2026-10-21T08:00:00Z"}]},
        {"id": "P2", "status": "sent", "subject": "x", "body": "x", "attachment": "Resume.pdf", "timezone": "UTC",
         "events": [{"event": "sent", "ts_utc": "2026-10-20T08:00:00Z"}]},
    ]
    st.private = {"contacts": {"P1": {"to": "prof@uni.edu, prof2@uni.edu", "message_id": "<first@iitb>"},
                               "P2": {"to": "silent@uni.edu", "message_id": "<second@iitb>"}}, "seen": []}
    return st


def inbox_and_sent():
    reply = mail("<r1@uni>", "Prof A <prof@uni.edu>", "me@iitb.ac.in", "Re: Research internship inquiry",
                 "Please send your transcript.\n\nOn Mon, Ruturva wrote:\n> Hello", "Wed, 21 Oct 2026 09:00:00 +0000",
                 refs="<first@iitb>", attach=("form.pdf", b"%PDF form"))
    other = mail("<x@else>", "Someone <someone@else.org>", "me@iitb.ac.in", "Hi", "unrelated", "Wed, 21 Oct 2026 10:00:00 +0000")
    bounce = mail("<b@md>", "Mail Delivery <mailer-daemon@iitb.ac.in>", "me@iitb.ac.in", "Undelivered", "failed",
                  "Wed, 21 Oct 2026 10:30:00 +0000", refs="<first@iitb>")
    sent = mail("<s1@iitb>", "Ruturva <me@iitb.ac.in>", "prof@uni.edu", "Re: Research internship inquiry",
                "Attached is my transcript.", "Thu, 22 Oct 2026 09:00:00 +0000", refs="<r1@uni>",
                attach=("transcript.pdf", b"%PDF transcript"))
    auto_first = mail("<first@iitb>", "Ruturva <me@iitb.ac.in>", "prof@uni.edu", "Research internship inquiry", "dup",
                      "Tue, 20 Oct 2026 08:00:00 +0000")
    return {"INBOX": [reply, other, bounce], "Sent": [sent, auto_first]}


class ThreadSyncTest(unittest.TestCase):
    def setUp(self):
        self.site = Path(tempfile.mkdtemp())
        self.store = threads.ThreadStore(self.site, PASS)
        self.store.load()
        self.st = make_state()

    def run_sync(self, imap, force=False, now=NOW):
        return threads.sync(self.store, self.st, imap, now, lambda: RESUME, force)

    def test_builds_whole_thread_with_attachments(self):
        self.assertEqual(self.run_sync(FakeImap(inbox_and_sent())), 3)
        msgs = self.store.data["threads"]["P1"]["messages"]
        self.assertEqual([(m["dir"], m["mid"]) for m in msgs],
                         [("out", "<first@iitb>"), ("in", "<r1@uni>"), ("out", "<s1@iitb>")])
        self.assertNotIn("P2", self.store.data["threads"])  # silent professors are not tracked
        self.assertEqual(msgs[0]["atts"][0]["name"], "Resume.pdf")
        self.assertIn("transcript", msgs[1]["text"])
        # the file on disk is encrypted and opens with the passphrase
        att = msgs[1]["atts"][0]
        blob = json.loads((self.site / "att" / f"{att['sha']}.json").read_text())
        self.assertEqual(base64.b64decode(crypto.decrypt_from_browser_format(blob, PASS)["data"]), b"%PDF form")
        raw = (self.site / "threads.enc.json").read_text()
        self.assertNotIn("transcript", raw)
        self.assertEqual(len(crypto.decrypt_from_browser_format(json.loads(raw), PASS)["threads"]["P1"]["messages"]), 3)

    def test_unrelated_and_bounce_mail_is_not_kept(self):
        self.run_sync(FakeImap(inbox_and_sent()))
        mids = {m["mid"] for t in self.store.data["threads"].values() for m in t["messages"]}
        self.assertNotIn("<x@else>", mids)
        self.assertNotIn("<b@md>", mids)

    def test_new_webmail_message_is_picked_up_later(self):
        folders = inbox_and_sent()
        imap = FakeImap(folders)
        self.run_sync(imap)
        folders["Sent"].append(mail("<s2@iitb>", "Ruturva <me@iitb.ac.in>", "prof@uni.edu", "Re: again", "Thanks!",
                                    "Fri, 23 Oct 2026 09:00:00 +0000", refs="<r1@uni>"))
        self.assertEqual(self.run_sync(imap, now=NOW + timedelta(minutes=1)), 0)  # throttled: next sweep is 5 min on
        self.assertEqual(self.run_sync(imap, now=NOW + timedelta(minutes=6)), 1)
        self.assertEqual(self.store.data["threads"]["P1"]["messages"][-1]["mid"], "<s2@iitb>")

    def test_reply_from_another_address_joins_by_in_reply_to(self):
        folders = inbox_and_sent()
        imap = FakeImap(folders)
        self.run_sync(imap)
        folders["INBOX"].append(mail("<r2@uni>", "Student <student@uni.edu>", "me@iitb.ac.in", "Re: Re", "I am the postdoc.",
                                     "Fri, 23 Oct 2026 10:00:00 +0000", refs="<s1@iitb>"))
        self.assertEqual(self.run_sync(imap, force=True, now=NOW + timedelta(minutes=1)), 1)

    def test_html_only_reply_becomes_text(self):
        folders = {"INBOX": [mail("<h@uni>", "Prof <prof@uni.edu>", "me", "Re", "<p>Hello<br>there &amp; welcome</p>",
                                  "Wed, 21 Oct 2026 09:00:00 +0000", refs="<first@iitb>", html=True)], "Sent": []}
        self.run_sync(FakeImap(folders))
        self.assertIn("Hello\nthere & welcome", self.store.data["threads"]["P1"]["messages"][1]["text"])

    def test_oversized_attachment_is_listed_without_the_file(self):
        with mock.patch.object(threads, "ATT_MAX", 5):
            self.run_sync(FakeImap(inbox_and_sent()))
        att = self.store.data["threads"]["P1"]["messages"][1]["atts"][0]
        self.assertIsNone(att["sha"])
        self.assertEqual(att["note"], "too large to store")

    def test_wrong_passphrase_never_overwrites_history(self):
        self.run_sync(FakeImap(inbox_and_sent()))
        with self.assertRaises(Exception):
            threads.ThreadStore(self.site, "wrong").load()

    def test_state_without_replies_does_nothing(self):
        self.st.emails[0]["status"], self.st.emails[0]["events"] = "sent", self.st.emails[0]["events"][:1]
        self.assertEqual(self.run_sync(FakeImap(inbox_and_sent())), 0)
        self.assertFalse((self.site / "threads.enc.json").exists())


if __name__ == "__main__":
    unittest.main()
