# -*- coding: utf-8 -*-
"""Tests for scheduler.py (valid send days) and queue_emails.py (hash lock, caps, spacing).

Run from the repo root with the project venv (needs the `holidays` package):
    .venv/bin/python -m unittest discover -s tests -v
"""
import json
import sys
import tempfile
import unittest
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from schedule import queue_emails as qe  # noqa: E402
from schedule.scheduler import next_slot, reschedule_missed  # noqa: E402

UTC = timezone.utc


def local(dt_utc, tz):
    return dt_utc.astimezone(ZoneInfo(tz))


class NextSlotTests(unittest.TestCase):
    def test_friday_afternoon_rolls_to_monday(self):
        s = next_slot("America/New_York", "US", datetime(2026, 10, 23, 18, 0, tzinfo=UTC))  # Fri 14:00 ET
        self.assertEqual(local(s, "America/New_York").strftime("%a %Y-%m-%d %H:%M"), "Mon 2026-10-26 08:30")

    def test_us_thanksgiving_is_skipped(self):
        s = next_slot("America/New_York", "US", datetime(2026, 11, 25, 15, 0, tzinfo=UTC))  # Wed 10:00 ET
        self.assertEqual(local(s, "America/New_York").date().isoformat(), "2026-11-27")

    def test_japan_golden_week_is_skipped(self):
        s = next_slot("Asia/Tokyo", "JP", datetime(2027, 4, 30, 3, 0, tzinfo=UTC))  # Fri 12:00 JST
        self.assertEqual(local(s, "Asia/Tokyo").strftime("%Y-%m-%d %H:%M"), "2027-05-06 08:30")

    def test_us_dst_end_keeps_wall_clock_0830(self):
        s = next_slot("America/New_York", "US", datetime(2026, 10, 30, 14, 0, tzinfo=UTC))  # Fri, DST ends Sun 1 Nov
        self.assertEqual(s, datetime(2026, 11, 2, 13, 30, tzinfo=UTC))  # 08:30 EST = 13:30Z
        self.assertEqual(local(s, "America/New_York").utcoffset(), timedelta(hours=-5))

    def test_eu_dst_end_keeps_wall_clock_0830(self):
        s = next_slot("Europe/Berlin", "DE", datetime(2026, 10, 23, 10, 0, tzinfo=UTC))  # Fri, DST ends Sun 25 Oct
        self.assertEqual(s, datetime(2026, 10, 26, 7, 30, tzinfo=UTC))  # 08:30 CET

    def test_christmas_new_year_break_is_skipped(self):
        s = next_slot("Europe/London", "GB", datetime(2026, 12, 23, 12, 0, tzinfo=UTC))
        self.assertEqual(local(s, "Europe/London").date().isoformat(), "2027-01-04")

    def test_min_lead_pushes_to_next_day(self):
        # 07:00 local on a Tuesday: today's 08:30 is only 1.5 h away (< 2 h lead), so Wednesday.
        s = next_slot("Asia/Singapore", "SG", datetime(2026, 10, 19, 23, 0, tzinfo=UTC))  # Tue 07:00 SGT
        self.assertEqual(local(s, "Asia/Singapore").date().isoformat(), "2026-10-21")

    def test_naive_datetime_rejected(self):
        with self.assertRaises(ValueError):
            next_slot("Asia/Tokyo", "JP", datetime(2026, 10, 19, 0, 0))


class MissedSlotTests(unittest.TestCase):
    send = datetime(2026, 10, 26, 13, 30, tzinfo=UTC)  # Mon 08:30 ET

    def test_within_late_window_still_sends(self):
        self.assertIsNone(reschedule_missed(self.send, "America/New_York", "US", self.send + timedelta(minutes=60)))

    def test_after_late_window_rolls_to_next_valid_day(self):
        new = reschedule_missed(self.send, "America/New_York", "US", self.send + timedelta(minutes=61))
        self.assertEqual(local(new, "America/New_York").strftime("%Y-%m-%d %H:%M"), "2026-10-27 08:30")


class QueueTests(unittest.TestCase):
    """Runs the queue command against a throwaway data/ and review/ folder."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "data").mkdir()
        (self.tmp / "review").mkdir()
        self._saved = (qe.DATA, qe.REVIEW, qe.QUEUE, qe.EVENTS, qe.SLOT_CAP)
        qe.DATA, qe.REVIEW = self.tmp / "data", self.tmp / "review"
        qe.QUEUE, qe.EVENTS = qe.DATA / "queue.json", qe.DATA / "events.jsonl"
        profs = []
        for i, (tz, cc) in enumerate([("Europe/Berlin", "DE")] * 3 + [("Asia/Tokyo", "JP")], 1):
            pid = f"P{i:04d}"
            profs.append({"id": pid, "name": f"Prof {i}", "email": f"p{i}@uni.example", "sendable": True,
                          "timezone": tz, "country": cc})
            (qe.REVIEW / f"{pid}.md").write_text(
                f"---\nid: {pid}\nstatus: draft\nto: p{i}@uni.example\nname: Prof {i}\nuniversity: U\n"
                f"subject: Hello\nattachment: cv.pdf\n---\n\nDear Prof. {i},\n\nBody.\n")
        (qe.DATA / "professors.json").write_text(json.dumps(profs))

    def tearDown(self):
        qe.DATA, qe.REVIEW, qe.QUEUE, qe.EVENTS, qe.SLOT_CAP = self._saved

    def run_queue(self, **kw):
        qe.cmd_queue(Namespace(limit=kw.get("limit", 100), ids=kw.get("ids", ""), dry_run=kw.get("dry_run", False)))
        return json.loads(qe.QUEUE.read_text()) if qe.QUEUE.exists() else []

    def test_dry_run_writes_nothing(self):
        self.assertEqual(self.run_queue(dry_run=True), [])
        self.assertIn("status: draft", (qe.REVIEW / "P0001.md").read_text())

    def test_same_slot_is_spaced_one_minute_apart(self):
        q = self.run_queue()
        berlin = sorted((x for x in q if x["timezone"] == "Europe/Berlin"), key=lambda x: x["send_utc"])
        minutes = [x["send_local"][11:16] for x in berlin]
        self.assertEqual(minutes, ["08:30", "08:31", "08:32"])
        self.assertEqual(next(x for x in q if x["timezone"] == "Asia/Tokyo")["send_local"][11:16], "08:30")

    def test_queue_marks_draft_and_logs_events(self):
        self.run_queue(ids="P0001")
        self.assertIn("status: queued", (qe.REVIEW / "P0001.md").read_text())
        events = [json.loads(l)["event"] for l in qe.EVENTS.read_text().splitlines()]
        self.assertEqual(events, ["queued", "scheduled"])

    def test_edit_after_queue_is_pulled_and_status_change_is_not(self):
        self.run_queue()
        qe.cmd_verify(Namespace())  # only the header status changed: still scheduled
        self.assertTrue(all(x["status"] == "scheduled" for x in json.loads(qe.QUEUE.read_text())))
        p = qe.REVIEW / "P0002.md"
        p.write_text(p.read_text().replace("Body.", "Body, edited."))
        qe.cmd_verify(Namespace())
        q = {x["id"]: x for x in json.loads(qe.QUEUE.read_text())}
        self.assertEqual(q["P0002"]["status"], "pulled")
        self.assertIn("status: edited", p.read_text())
        # the rest of the Berlin slot keeps its minutes: the gap stays, so nobody lands on another slot's minute
        self.assertEqual(sorted(q[i]["send_local"][11:16] for i in ("P0001", "P0003")), ["08:30", "08:32"])

    def test_slot_cap_spills_to_next_valid_day(self):
        qe.SLOT_CAP = 2
        q = self.run_queue()
        berlin_days = sorted({x["send_local"][:10] for x in q if x["timezone"] == "Europe/Berlin"})
        self.assertEqual(len(berlin_days), 2)

    def test_limit_caps_one_call(self):
        self.assertEqual(len(self.run_queue(limit=2)), 2)

    def test_mismatched_email_is_skipped(self):
        profs = json.loads((qe.DATA / "professors.json").read_text())
        profs[0]["email"] = "other@uni.example"
        (qe.DATA / "professors.json").write_text(json.dumps(profs))
        self.assertNotIn("P0001", {x["id"] for x in self.run_queue()})


if __name__ == "__main__":
    unittest.main()


class PlacementTests(unittest.TestCase):
    """schedule/placement.py: one mail per minute across every slot, clashes go to the next valid day."""

    def row(self, pid, idx, tz, country, slot, send=None):
        return {"id": pid, "queue_index": idx, "timezone": tz, "country": country,
                "slot_utc": datetime.fromisoformat(slot), "send_utc": datetime.fromisoformat(send) if send else None}

    def test_overlapping_slots_never_share_a_minute(self):
        from schedule.placement import place_send_times
        rows = [self.row(f"A{i}", i, "Europe/Berlin", "DE", "2026-11-02T07:30:00+00:00") for i in range(60)]
        rows += [self.row(f"B{i}", 100 + i, "Europe/London", "GB", "2026-11-02T08:00:00+00:00") for i in range(60)]
        out = place_send_times(rows)
        self.assertEqual(len({r["send_utc"] for r in out}), 120)
        by = {r["id"]: r for r in out}
        self.assertFalse(by["A0"]["moved"])
        self.assertTrue(by["B30"]["moved"])  # 08:30Z is the 60th minute of A, B's 31st position
        self.assertGreater(by["B30"]["slot_utc"], datetime.fromisoformat("2026-11-02T08:00:00+00:00"))

    def test_existing_minutes_are_sticky_and_clash_goes_to_later_mail(self):
        from schedule.placement import place_send_times
        a = self.row("A", 1, "Europe/Berlin", "DE", "2026-11-02T07:30:00+00:00", "2026-11-02T08:00:00+00:00")
        b = self.row("B", 2, "Europe/London", "GB", "2026-11-02T08:00:00+00:00", "2026-11-02T08:00:00+00:00")
        out = {r["id"]: r for r in place_send_times([a, b])}
        self.assertEqual(out["A"]["send_utc"], a["send_utc"])
        self.assertTrue(out["B"]["moved"])
        self.assertEqual(out["B"]["slot_utc"].date().isoformat(), "2026-11-03")
