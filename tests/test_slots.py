# -*- coding: utf-8 -*-
"""Tests for src/schedule/slots.py (SPEC.md amendment 12).

Run from the repo root:  python3 -m unittest discover -s tests -v
"""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from schedule.slots import assign_send_times, is_due  # noqa: E402


def slot(tz, y, m, d, hh=8, mm=30):
    """The aware instant hh:mm on y-m-d in the given IANA zone."""
    return datetime(y, m, d, hh, mm, tzinfo=ZoneInfo(tz))


def make(i, tz, slot_utc, queue_index=None, id_=None):
    """Build one input email dict."""
    return {
        "id": id_ if id_ is not None else f"e{i:03d}",
        "queue_index": i if queue_index is None else queue_index,
        "timezone": tz,
        "slot_utc": slot_utc,
    }


class AssignSendTimesTests(unittest.TestCase):
    def test_ten_emails_one_per_minute(self):
        s = slot("America/New_York", 2026, 3, 3)
        out = assign_send_times([make(i, "America/New_York", s) for i in range(10)])
        self.assertEqual([r["position"] for r in out], list(range(10)))
        for k, r in enumerate(out):
            self.assertEqual((r["send_local"].hour, r["send_local"].minute), (8, 30 + k))
            self.assertEqual(r["send_local"].tzinfo, ZoneInfo("America/New_York"))
            self.assertEqual(r["send_utc"], s + timedelta(minutes=k))

    def test_hundred_emails_last_at_1009(self):
        s = slot("America/New_York", 2026, 3, 3)
        out = assign_send_times([make(i, "America/New_York", s) for i in range(100)])
        last = out[-1]
        self.assertEqual(last["position"], 99)
        self.assertEqual((last["send_local"].hour, last["send_local"].minute), (10, 9))

    def test_paris_and_berlin_share_one_sequence(self):
        # Winter: both zones are UTC+1, so 08:30 local is the same 07:30 UTC instant.
        sp = slot("Europe/Paris", 2026, 1, 12)
        sb = slot("Europe/Berlin", 2026, 1, 12)
        self.assertEqual(sp, sb)
        emails = [
            make(0, "Europe/Paris", sp, id_="p0"),
            make(1, "Europe/Berlin", sb, id_="b1"),
            make(2, "Europe/Paris", sp, id_="p2"),
            make(3, "Europe/Berlin", sb, id_="b3"),
        ]
        out = assign_send_times(emails)
        self.assertEqual([r["id"] for r in out], ["p0", "b1", "p2", "b3"])
        self.assertEqual([r["position"] for r in out], [0, 1, 2, 3])
        self.assertEqual([r["send_local"].minute for r in out], [30, 31, 32, 33])
        self.assertTrue(all(r["send_local"].hour == 8 for r in out))
        self.assertEqual(out[1]["send_local"].tzinfo, ZoneInfo("Europe/Berlin"))

    def test_different_instants_are_independent_sequences(self):
        st = slot("Asia/Tokyo", 2026, 3, 3)
        sl = slot("Europe/London", 2026, 3, 3)
        self.assertNotEqual(st, sl)
        emails = [
            make(0, "Asia/Tokyo", st, id_="t0"),
            make(1, "Europe/London", sl, id_="l1"),
            make(2, "Asia/Tokyo", st, id_="t2"),
            make(3, "Europe/London", sl, id_="l3"),
        ]
        by_id = {r["id"]: r for r in assign_send_times(emails)}
        self.assertEqual((by_id["t0"]["position"], by_id["t2"]["position"]), (0, 1))
        self.assertEqual((by_id["l1"]["position"], by_id["l3"]["position"]), (0, 1))
        self.assertEqual(by_id["t0"]["send_utc"], st)
        self.assertEqual(by_id["l1"]["send_utc"], sl)
        self.assertEqual(by_id["l3"]["send_local"].minute, 31)

    def test_output_sorted_by_send_utc_then_id(self):
        st = slot("Asia/Tokyo", 2026, 3, 3)          # 23:30 UTC the day before
        sl = slot("Europe/London", 2026, 3, 3)       # 08:30 UTC
        out = assign_send_times([make(0, "Europe/London", sl), make(1, "Asia/Tokyo", st)])
        self.assertEqual([r["send_utc"] for r in out], sorted(r["send_utc"] for r in out))
        self.assertEqual(out[0]["timezone"], "Asia/Tokyo")

    def test_queue_index_tie_broken_by_id(self):
        s = slot("Europe/London", 2026, 3, 3)
        emails = [
            make(0, "Europe/London", s, queue_index=5, id_="b"),
            make(0, "Europe/London", s, queue_index=5, id_="a"),
            make(0, "Europe/London", s, queue_index=1, id_="z"),
        ]
        out = assign_send_times(emails)
        self.assertEqual([r["id"] for r in out], ["z", "a", "b"])
        self.assertEqual([r["position"] for r in out], [0, 1, 2])

    def test_dst_day_after_us_fall_back(self):
        # US DST ended 2026-11-01, so on Nov 2 New York is EST (UTC-5): 08:30 = 13:30 UTC.
        s = slot("America/New_York", 2026, 11, 2)
        self.assertEqual(s.astimezone(timezone.utc), datetime(2026, 11, 2, 13, 30, tzinfo=timezone.utc))
        out = assign_send_times([make(0, "America/New_York", s), make(1, "America/New_York", s)])
        first = out[0]["send_local"]
        self.assertEqual((first.hour, first.minute), (8, 30))
        self.assertEqual(first.utcoffset(), timedelta(hours=-5))
        self.assertEqual(out[1]["send_local"].minute, 31)

    def test_custom_gap(self):
        s = slot("Europe/London", 2026, 3, 3)
        out = assign_send_times([make(i, "Europe/London", s) for i in range(3)], gap=timedelta(minutes=2))
        self.assertEqual([r["send_local"].minute for r in out], [30, 32, 34])

    def test_input_not_mutated(self):
        s = slot("Europe/London", 2026, 3, 3)
        emails = [make(1, "Europe/London", s), make(0, "Europe/London", s)]
        snapshot = [dict(e) for e in emails]
        out = assign_send_times(emails)
        self.assertEqual(emails, snapshot)
        self.assertTrue(all("send_utc" not in e for e in emails))
        self.assertIsNot(out[0], emails[0])

    def test_empty_input(self):
        self.assertEqual(assign_send_times([]), [])

    def test_naive_slot_rejected(self):
        naive = datetime(2026, 3, 3, 8, 30)
        with self.assertRaises(ValueError):
            assign_send_times([make(0, "Europe/London", naive)])

    def test_non_positive_gap_rejected(self):
        s = slot("Europe/London", 2026, 3, 3)
        with self.assertRaises(ValueError):
            assign_send_times([make(0, "Europe/London", s)], gap=timedelta(0))


class IsDueTests(unittest.TestCase):
    SEND = datetime(2026, 3, 3, 8, 30, tzinfo=timezone.utc)
    ONE_SEC = timedelta(seconds=1)

    def test_one_second_before_is_wait(self):
        self.assertEqual(is_due(self.SEND - self.ONE_SEC, self.SEND), "wait")

    def test_exactly_at_send_is_send(self):
        self.assertEqual(is_due(self.SEND, self.SEND), "send")

    def test_exactly_at_end_of_window_is_send(self):
        self.assertEqual(is_due(self.SEND + timedelta(hours=1), self.SEND), "send")

    def test_one_second_after_window_is_missed(self):
        self.assertEqual(is_due(self.SEND + timedelta(hours=1) + self.ONE_SEC, self.SEND), "missed")

    def test_custom_late_window(self):
        late = timedelta(minutes=10)
        self.assertEqual(is_due(self.SEND + late, self.SEND, late), "send")
        self.assertEqual(is_due(self.SEND + late + self.ONE_SEC, self.SEND, late), "missed")

    def test_accepts_non_utc_aware_inputs(self):
        now = self.SEND.astimezone(ZoneInfo("Asia/Tokyo"))
        self.assertEqual(is_due(now, self.SEND), "send")

    def test_naive_rejected(self):
        with self.assertRaises(ValueError):
            is_due(datetime(2026, 3, 3, 8, 30), self.SEND)
        with self.assertRaises(ValueError):
            is_due(self.SEND, datetime(2026, 3, 3, 8, 30))


if __name__ == "__main__":
    unittest.main()
