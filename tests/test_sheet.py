import json
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from sheet import (F_DT, F_HMS, F_PCT, Formula, check, decoupling, dur, fastest, payload,  # noqa: E402
                   serial, split_parts)

T0 = datetime(2026, 8, 5, 7, 55)


def pts(*rows):
    """rows: (d, s, hr) per point."""
    return [{"d": d, "s": s, "hr": hr} for d, s, hr in rows]


class SplitPartsTest(unittest.TestCase):
    def test_two_point_track_has_no_last_part(self):
        # the 60% point is the final point: the last 40% has no distance or time
        r = {"pts": pts((0, 0, None), (100, 60, None))}
        first, last, h1, h2 = split_parts(r)
        self.assertAlmostEqual(first, 600)
        self.assertIsNone(last)

    def test_hr_only_in_one_part(self):
        r = {"pts": pts((0, 0, 140), (300, 90, 141), (600, 180, None), (800, 240, None), (1000, 300, None))}
        first, last, h1, h2 = split_parts(r)
        self.assertAlmostEqual(first, 300)
        self.assertAlmostEqual(last, 300)
        self.assertAlmostEqual(h1, 140.5)
        self.assertIsNone(h2)


class DecouplingTest(unittest.TestCase):
    def test_hr_only_in_first_half_is_none(self):
        p = [{"d": i * 10.0, "s": i * 3.0, "hr": 140 if i < 400 else None} for i in range(1001)]
        self.assertIsNone(decoupling({"pts": p, "has_hr": True, "total_s": 3000}))

    def test_steady_run_is_zero(self):
        p = [{"d": i * 10.0, "s": i * 3.0, "hr": 140} for i in range(1001)]
        self.assertAlmostEqual(decoupling({"pts": p, "has_hr": True, "total_s": 3000}), 0)


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
                     Formula('=HYPERLINK("https://www.strava.com/activities/46202","buka")', "buka")]],
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


class FormulaTest(unittest.TestCase):
    TAB = {"name": "Aktivitas", "header": ["activity_id", "nama", "link"],
           "rows": [[1, "=1+1", Formula('=HYPERLINK("x"&A2,"buka")', "buka")]], "formats": {}}

    def test_names_stay_values_and_only_built_formulas_are_formulas(self):
        t = json.loads(json.dumps(payload([self.TAB], "sheet-id")))["tabs"][0]
        self.assertEqual(t["values"], [["activity_id", "nama", "link"], [1, "=1+1", ""]])
        self.assertEqual(t["formulas"], [[2, 3, '=HYPERLINK("x"&A2,"buka")']])

    def test_literal_equals_text_is_checked_as_text(self):
        self.assertEqual(check([self.TAB], {"Aktivitas": [self.TAB["header"], [1, "=1+1", "buka"]]}), [])
        msgs = check([self.TAB], {"Aktivitas": [self.TAB["header"], [1, 2, "buka"]]})
        self.assertEqual(len(msgs), 1)


if __name__ == "__main__":
    unittest.main()
