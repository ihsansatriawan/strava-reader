import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from analyze import dedupe  # noqa: E402
from sheet import F_DT, F_HMS, F_PCT, check, dur, duplicates_of, fastest, serial  # noqa: E402

T0 = datetime(2026, 8, 5, 7, 55)


def act(id_, type_, start, hr=False):
    return {"id": id_, "type": type_, "start": start, "hr": hr}


class DuplicatesOfTest(unittest.TestCase):
    def test_dropped_upload_points_at_the_kept_copy(self):
        acts = [act("1", "Run", T0), act("2", "Run", T0 + timedelta(seconds=10), hr=True),
                act("3", "Walk", T0 + timedelta(seconds=20)), act("4", "Run", T0 + timedelta(days=1))]
        kept, _ = dedupe(acts, has_hr=lambda a: a["hr"])
        self.assertEqual(duplicates_of(acts, kept), {"1": "2"})


class FastestTest(unittest.TestCase):
    def test_best_stretch_of_the_track(self):
        # 100 m every 30 s, except one quick 100 m in 20 s
        pts, d, s = [], 0, 0
        for i in range(20):
            pts.append({"d": d, "s": s})
            d, s = d + 100, s + (20 if i == 5 else 30)
        self.assertEqual(fastest(pts, 1000), 290)
        self.assertIsNone(fastest(pts, 5000))


class CellTest(unittest.TestCase):
    def test_spreadsheet_serials(self):
        self.assertEqual(serial(datetime(2026, 6, 29).date()), 46202)
        self.assertAlmostEqual(dur(3253) * 86400, 3253)
        self.assertEqual(dur(None), "")


class CheckTest(unittest.TestCase):
    TAB = {"name": "T", "header": ["activity_id", "mulai", "waktu_bergerak", "rasio_beban", "dup", "link"],
           "rows": [[46202, serial(datetime(2026, 8, 5, 7, 55)), dur(3253), 0.06, "19831511699",
                     '=HYPERLINK("https://www.strava.com/activities/46202","buka")']],
           "formats": {"B": F_DT, "C": F_HMS, "D": F_PCT}}

    def readback(self, mulai, bergerak, rasio=0.06):
        return {"T": [self.TAB["header"], [46202, mulai, bergerak, rasio, 19831511699, "buka"]]}

    def test_readback_matches(self):
        self.assertEqual(check([self.TAB], self.readback(serial(datetime(2026, 8, 5, 7, 55)), dur(3253))), [])

    def test_second_of_wobble_is_tolerated(self):
        self.assertEqual(check([self.TAB],
                                self.readback(serial(datetime(2026, 8, 5, 7, 55)) + 1 / 86400,
                                               dur(3253) - 1 / 86400)), [])

    def test_missing_tab_is_reported(self):
        self.assertEqual(check([self.TAB], {}), ["T: not read back"])

    def test_wrong_cell_is_reported(self):
        msgs = check([self.TAB], self.readback(serial(datetime(2026, 8, 5, 7, 55)), dur(3253), rasio=0.07))
        self.assertEqual(len(msgs), 1)
        self.assertTrue(msgs[0].startswith("T row 2: column D expected"), msgs[0])


if __name__ == "__main__":
    unittest.main()
