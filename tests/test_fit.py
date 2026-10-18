import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fit  # noqa: E402
from analyze import use_device  # noqa: E402

UINT16, UINT32 = 0x84, 0x86


def fit_bytes(messages):
    """messages: [(global_num, [(field_num, base_type, value), ...]), ...]"""
    body, local = b"", {}
    for gnum, fields in messages:
        key = (gnum, tuple(f[:2] for f in fields))
        if key not in local:
            local[key] = len(local)
            defs = b"".join(struct.pack("<BBB", n, 2 if bt == UINT16 else 4, bt) for n, bt, _ in fields)
            body += struct.pack("<BBBHB", 0x40 | local[key], 0, 0, gnum, len(fields)) + defs
        body += bytes([local[key]]) + b"".join(
            struct.pack("<H" if bt == UINT16 else "<I", v) for _, bt, v in fields)
    return struct.pack("<BBHI4s", 12, 0x10, 2093, len(body), b".FIT") + body + b"\0\0"


def huawei_style(manufacturer=255):
    """20 s session, 50 m; four speed samples all stamped with the same time."""
    msgs = [(0, [(1, UINT16, manufacturer)]),
            (18, [(2, UINT32, 1000), (7, UINT32, 20000), (9, UINT32, 5000)])]
    msgs += [(20, [(253, UINT32, 1000), (6, UINT16, s)]) for s in (2000, 0, 3000, 2500)]
    return fit_bytes(msgs)


class FitTest(unittest.TestCase):
    def summary(self, data):
        with tempfile.NamedTemporaryFile(suffix=".fit") as f:
            f.write(data)
            f.flush()
            return fit.summary(Path(f.name))

    def test_bunched_speed_series_is_spread_over_the_session(self):
        fs = self.summary(huawei_style())
        self.assertEqual(fs["manufacturer"], 255)
        self.assertAlmostEqual(fs["km"], 0.05)
        self.assertEqual([t for t, _ in fs["speed"]], [0, 5, 10, 15])
        self.assertEqual(fit.moving_seconds(fs, 1.0), 15)
        self.assertAlmostEqual(fit.distance_curve(fs)[-1][1], 50.0)

    def test_xml_upload_is_not_a_fit_file(self):
        self.assertIsNone(self.summary(b'<?xml version="1.0"?><gpx/>'))


class UseDeviceTest(unittest.TestCase):
    ACT = {"id": "1", "type": "Run", "km": 4.2, "moving_s": 2295}

    def test_watch_distance_and_moving_time_replace_stravas(self):
        fs = FitTest.summary(None, huawei_style())
        a = use_device(dict(self.ACT), fs)
        self.assertAlmostEqual(a["km"], 0.05)
        self.assertEqual(a["strava_km"], 4.2)
        self.assertEqual(a["moving_s"], 15)

    def test_strava_phone_recording_is_left_alone(self):
        fs = FitTest.summary(None, huawei_style(manufacturer=fit.STRAVA_APP))
        self.assertEqual(use_device(dict(self.ACT), fs), self.ACT)


if __name__ == "__main__":
    unittest.main()
