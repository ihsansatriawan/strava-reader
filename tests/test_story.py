import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import story  # noqa: E402

GPX = """<?xml version="1.0"?><gpx><trk><trkseg>{}</trkseg></trk></gpx>"""


def trkpt(lat, lon, sec, hr=None, ele=None):
    body = f"<time>2026-08-05T07:{sec // 60:02d}:{sec % 60:02d}Z</time>"
    if ele is not None:
        body += f"<ele>{ele}</ele>"
    if hr is not None:
        body += f"<extensions><gpxtpx:TrackPointExtension><gpxtpx:hr>{hr}</gpxtpx:hr></gpxtpx:TrackPointExtension></extensions>"
    return f'<trkpt lat="{lat}" lon="{lon}">{body}</trkpt>'


class AnalyseTest(unittest.TestCase):
    def analyse(self, points):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "1.gpx").write_text(GPX.format("".join(points)))
            with mock.patch.object(story, "GPX_DIR", Path(d)):
                return story.analyse({"id": "1", "start": datetime(2026, 8, 5, 7, 0)})

    def test_empty_gpx_is_no_track(self):
        self.assertIsNone(self.analyse([]))

    def test_single_point_is_no_track(self):
        self.assertIsNone(self.analyse([trkpt(-6.2, 106.8, 0, 140)]))

    def test_track_without_elevation_still_charts(self):
        # ~1.1 km north in 6 min, HR but no <ele>
        r = self.analyse([trkpt(-6.2 + k * 0.001, 106.8, k * 36, 140 + k) for k in range(11)])
        self.assertIsNotNone(r)
        sp = story.splits(r["pts"])
        self.assertEqual([s["km"] for s in sp], [1])
        svg = story.chart_longest(r, sp)
        self.assertIn("detak jantung", svg)
        self.assertNotIn("elevasi ·", svg)

    def test_sub_kilometre_run_charts_without_splits(self):
        r = self.analyse([trkpt(-6.2 + k * 0.0005, 106.8, k * 15, 140) for k in range(11)])
        self.assertEqual(story.splits(r["pts"]), [])
        svg = story.chart_longest(r, [])
        self.assertNotIn("waktu per km", svg)
        self.assertIn("detak jantung", svg)


class SplitsTest(unittest.TestCase):
    @staticmethod
    def pts(*rows):
        return [{"d": d, "s": s, "hr": None, "ele": None} for d, s in rows]

    def test_crossings_are_interpolated(self):
        # constant 5:00 /km, sampled off the kilometre marks
        sp = story.splits(self.pts((0, 0), (900, 270), (1100, 330), (2000, 600)))
        self.assertEqual([s["km"] for s in sp], [1, 2])
        self.assertEqual([round(s["s"], 6) for s in sp], [300, 300])

    def test_several_kilometres_between_two_samples(self):
        sp = story.splits(self.pts((0, 0), (500, 150), (3500, 1050)))
        self.assertEqual([s["km"] for s in sp], [1, 2, 3])
        self.assertEqual([round(s["s"], 6) for s in sp], [300, 300, 300])

    def test_hr_is_the_mean_of_real_samples_in_the_km(self):
        p = [{"d": d, "s": s, "hr": hr, "ele": None} for d, s, hr in
             ((0, 0, 130), (500, 150, 140), (1100, 330, 160), (2000, 600, 170))]
        sp = story.splits(p)
        self.assertEqual(sp[0]["hr"], 135)
        self.assertEqual(sp[1]["hr"], 165)


class HoursTest(unittest.TestCase):
    def test_evening_run_gets_a_bar(self):
        svg = story.chart_hours([{"start": datetime(2026, 8, 5, 21, 0)}])
        self.assertIn("mulai jam 21.00", svg)


if __name__ == "__main__":
    unittest.main()
