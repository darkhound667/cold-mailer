# -*- coding: utf-8 -*-
"""The sender must survive what actually stopped it on 2026-10-26: a rejected push whose rebase
conflicts, a DNS failure on the IMAP host, and a failing `gh` call. Real git repos in a temp dir,
fake mail and GitHub. No network.

Run from the repo root:  .venv/bin/python -m unittest discover -s tests -v
"""
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from runner import tick  # noqa: E402
from runner.state import State  # noqa: E402
from test_runner import SLOT, Cfg, Clock, FakeIssues, FakeSmtp, make_state  # noqa: E402

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, **GIT_ENV}).stdout


class ConflictingPushTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.key = os.urandom(32)
        self.remote = self.tmp / "remote.git"
        git(self.tmp, "init", "-q", "--bare", "-b", "main", str(self.remote))
        self.runner, self.laptop = self.tmp / "runner", self.tmp / "laptop"
        git(self.tmp, "init", "-q", "-b", "main", str(self.runner))
        git(self.runner, "remote", "add", "origin", str(self.remote))
        make_state(self.runner, n=2, key=self.key)  # writes runner/state through publish.export
        (self.runner / "site").mkdir()
        (self.runner / "site" / "threads.enc.json").write_text("{}")
        git(self.runner, "add", "-A")
        git(self.runner, "commit", "-q", "-m", "init")
        git(self.runner, "push", "-q", "-u", "origin", "main")
        git(self.tmp, "clone", "-q", str(self.remote), str(self.laptop))

    def test_rebase_conflict_keeps_both_sides(self):
        # the laptop queues a new professor and pushes (private.json.enc and emails.json change)
        lap = State(self.laptop / "state").load(self.key)
        lap.emails.append({"id": "P0099", "status": "scheduled", "events": [], "sha256": "x", "timezone": "UTC", "queue_index": 99})
        lap.contact("P0099")["to"] = "new@uni.example"
        lap.save(self.key)
        git(self.laptop, "add", "-A")
        git(self.laptop, "commit", "-q", "-m", "queue: 1 email(s) scheduled")
        git(self.laptop, "push", "-q", "origin", "main")
        # meanwhile the runner sends P0001 from its older checkout
        st = State(self.runner / "state").load(self.key)
        st.by_id("P0001")["status"] = "sent"
        st.event(st.by_id("P0001"), "sent", SLOT)
        st.contact("P0001")["message_id"] = "<m1@iitb>"
        old = Path.cwd()
        os.chdir(self.runner)
        try:
            with mock.patch.dict(os.environ, GIT_ENV):
                tick.git_checkpoint(st, self.key, tick.Config({"GIT_PUSH": "1"}))
        finally:
            os.chdir(old)
        check = self.tmp / "check"
        git(self.tmp, "clone", "-q", str(self.remote), str(check))
        merged = State(check / "state").load(self.key)
        self.assertEqual(merged.by_id("P0001")["status"], "sent")        # the send survived
        self.assertEqual(merged.contact("P0001")["message_id"], "<m1@iitb>")
        self.assertIn("P0099", [r["id"] for r in merged.emails])          # so did the laptop's queue
        self.assertEqual(merged.contact("P0099")["to"], "new@uni.example")
        self.assertTrue((check / "site" / "threads.enc.json").exists())
        self.assertIn("sent", (check / "state" / "events.jsonl").read_text())


class FlakyDependenciesTest(unittest.TestCase):
    def test_loop_survives_imap_dns_failure_gh_failure_and_dashboard_failure(self):
        tmp = Path(tempfile.mkdtemp())
        st, key = make_state(tmp)
        clock = Clock(SLOT - timedelta(minutes=25))
        smtp = FakeSmtp(clock)

        class BrokenIssues(FakeIssues):
            def labelled(self, label):
                raise subprocess.CalledProcessError(1, ["gh", "issue", "list"])

        def no_imap():
            raise socket.gaierror(-2, "Name or service not known")

        def broken_dashboard():
            raise RuntimeError("boom")

        tick.run(st, Cfg(), key, BrokenIssues(), smtp.factory, no_imap, clock.now, clock.sleep,
                 lambda: None, broken_dashboard)
        self.assertEqual(len(smtp.sent), 3)  # sending went on regardless

    def test_failed_issue_call_does_not_swallow_the_reply(self):
        tmp = Path(tempfile.mkdtemp())
        st, key = make_state(tmp, n=1)
        rec = st.emails[0]
        rec["status"] = "sent"
        st.event(rec, "sent", SLOT)
        st.contact(rec["id"])["message_id"] = "<m1@iitb>"

        class FailingOpen(FakeIssues):
            def open(self, title, body, labels):
                raise subprocess.CalledProcessError(1, ["gh", "issue", "create"])

        reply = [{"msgid": "<r1@uni>", "kind": "human", "id": rec["id"]}]
        with mock.patch.object(tick.mailio, "poll", return_value=reply):
            with self.assertRaises(subprocess.CalledProcessError):
                tick.poll_inbox(st, object(), FailingOpen(), SLOT + timedelta(hours=1))
            self.assertNotIn("<r1@uni>", st.private["seen"])  # it will be picked up again
            tick.poll_inbox(st, object(), FakeIssues(), SLOT + timedelta(hours=2))
        self.assertEqual(rec["status"], "replied")
        self.assertIn("<r1@uni>", st.private["seen"])


if __name__ == "__main__":
    unittest.main()
