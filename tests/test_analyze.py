import calendar
import os
import sys
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from analyze import dedupe, local_start  # noqa: E402
from config import DATA_SINCE, in_scope  # noqa: E402


def act(id_, type_, start):
    return {"id": id_, "type": type_, "start": start}


class LocalStartTest(unittest.TestCase):
    def test_wall_clock_is_not_shifted_by_machine_timezone(self):
        old = os.environ.get("TZ")
        os.environ["TZ"] = "Asia/Jakarta"
        time.tzset()
        try:
            # an evening run stays on its own day instead of sliding to 00:24 the next
            evening = datetime(2026, 1, 15, 17, 24)
            self.assertEqual(local_start(calendar.timegm(evening.timetuple())), evening)
        finally:
            if old is None:
                os.environ.pop("TZ")
            else:
                os.environ["TZ"] = old
            time.tzset()


class ScopeTest(unittest.TestCase):
    def test_only_activities_from_data_since_are_kept(self):
        self.assertEqual(DATA_SINCE, datetime(2026, 1, 1))
        models = [
            {"id_str": "old", "start_date_local_raw": 1767225599},  # 2025-12-31 23:59:59 local
            {"id_str": "new", "start_date_local_raw": 1767225600},  # 2026-01-01 00:00:00 local
        ]
        self.assertEqual([m["id_str"] for m in in_scope(models)], ["new"])


class DedupeTest(unittest.TestCase):
    T = datetime(2026, 8, 7, 6, 33, 0)

    def test_phone_and_watch_copy_collapse_to_the_one_with_hr(self):
        phone = act("100", "Run", self.T)
        watch = act("200", "Run", self.T + timedelta(seconds=51))
        kept, merged = dedupe([phone, watch], has_hr=lambda a: a is watch)
        self.assertEqual(kept, [watch])
        self.assertEqual(merged, 1)

    def test_without_hr_the_earlier_upload_wins(self):
        a = act("200", "Run", self.T)
        b = act("100", "Run", self.T + timedelta(seconds=30))
        kept, _ = dedupe([a, b], has_hr=lambda a: False)
        self.assertEqual([k["id"] for k in kept], ["100"])

    def test_distinct_sessions_are_kept(self):
        acts = [
            act("1", "Run", self.T),
            act("2", "Walk", self.T + timedelta(seconds=20)),     # other sport
            act("3", "Run", self.T + timedelta(minutes=31)),      # back-to-back run
        ]
        kept, merged = dedupe(acts, has_hr=lambda a: False)
        self.assertEqual(sorted(k["id"] for k in kept), ["1", "2", "3"])
        self.assertEqual(merged, 0)

    def test_order_of_input_is_preserved(self):
        acts = [act("3", "Run", self.T + timedelta(days=2)), act("1", "Run", self.T)]
        kept, _ = dedupe(acts, has_hr=lambda a: False)
        self.assertEqual([k["id"] for k in kept], ["3", "1"])


if __name__ == "__main__":
    unittest.main()
