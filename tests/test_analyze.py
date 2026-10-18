import calendar
import os
import sys
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from analyze import dedupe, local_start, pace_str  # noqa: E402
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
        kept, dup_of = dedupe([phone, watch], has_hr=lambda a: a is watch)
        self.assertEqual(kept, [watch])
        self.assertEqual(dup_of, {"100": "200"})

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
        kept, dup_of = dedupe(acts, has_hr=lambda a: False)
        self.assertEqual(sorted(k["id"] for k in kept), ["1", "2", "3"])
        self.assertEqual(dup_of, {})

    def test_order_of_input_is_preserved(self):
        acts = [act("3", "Run", self.T + timedelta(days=2)), act("1", "Run", self.T)]
        kept, _ = dedupe(acts, has_hr=lambda a: False)
        self.assertEqual([k["id"] for k in kept], ["3", "1"])

    def test_other_sport_in_between_does_not_split_a_duplicate(self):
        run = act("1", "Run", self.T)
        walk = act("2", "Walk", self.T + timedelta(seconds=20))
        dup = act("3", "Run", self.T + timedelta(seconds=51))
        kept, dup_of = dedupe([run, walk, dup], has_hr=lambda a: False)
        self.assertEqual(sorted(k["id"] for k in kept), ["1", "2"])
        self.assertEqual(dup_of, {"3": "1"})

    def test_dropped_upload_maps_to_its_own_group(self):
        # newest first at 190 s, 170 s, 0 s: 0 s and 170 s are one session, 190 s
        # starts a new one (190 s after the group's first upload)
        late = act("30", "Run", self.T + timedelta(seconds=190))
        mid = act("20", "Run", self.T + timedelta(seconds=170))
        first = act("10", "Run", self.T)
        kept, dup_of = dedupe([late, mid, first], has_hr=lambda a: False)
        self.assertEqual([k["id"] for k in kept], ["30", "10"])
        self.assertEqual(dup_of, {"20": "10"})


class PaceTest(unittest.TestCase):
    def test_seconds_carry_into_minutes(self):
        self.assertEqual(pace_str(359.6, 1000), "6:00 /km")
        self.assertEqual(pace_str(359.4, 1000), "5:59 /km")
        self.assertEqual(pace_str(0, 0), "-")


if __name__ == "__main__":
    unittest.main()
