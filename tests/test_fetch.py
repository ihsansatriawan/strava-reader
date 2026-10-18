import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fetch  # noqa: E402
from test_fit import huawei_style  # noqa: E402

GPX = b'<?xml version="1.0"?><gpx></gpx>'
MODEL = {"id_str": "7", "name": "Lari pagi", "start_date": "2026-08-05T00:00:00Z"}


class Strava:
    """Answers per export kind, records every URL asked for."""

    def __init__(self, fit, gpx):
        self.answers, self.asked = {"export_original": fit, "export_gpx": gpx}, []

    def __call__(self, cookie, url, xhr=False):
        kind = url.rsplit("/", 1)[1]
        self.asked.append(kind)
        return self.answers[kind]


class DownloadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        (d / "fit").mkdir()
        (d / "gpx").mkdir()
        self.patches = [mock.patch.object(fetch, "FIT_DIR", d / "fit"), mock.patch.object(fetch, "GPX_DIR", d / "gpx"),
                        mock.patch.object(fetch, "DOWNLOADS_FILE", d / "downloads.json"),
                        mock.patch.object(fetch.time, "sleep")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def sync(self, strava):
        with mock.patch.object(fetch, "get", strava), mock.patch("builtins.print"):
            fetch.download_files("c", [MODEL])
        return json.loads(fetch.DOWNLOADS_FILE.read_text())["7"]

    def test_failed_gpx_is_retried_next_sync_alone(self):
        rec = self.sync(Strava((404, b""), (500, b"oops")))
        self.assertEqual(rec["state"], "error")
        again = Strava((404, b""), (200, GPX))
        rec = self.sync(again)
        self.assertEqual(again.asked, ["export_gpx"])
        self.assertEqual(rec["state"], "ok")
        idle = Strava((404, b""), (200, GPX))
        self.sync(idle)
        self.assertEqual(idle.asked, [])

    def test_truncated_fit_is_not_saved_and_stays_retryable(self):
        rec = self.sync(Strava((200, huawei_style()[:40]), (200, GPX)))
        self.assertEqual(rec["fit"], "error:truncated")
        self.assertEqual(rec["state"], "error")
        self.assertFalse((fetch.FIT_DIR / "7.fit").exists())
        again = Strava((200, huawei_style()), (200, GPX))
        rec = self.sync(again)
        self.assertEqual(again.asked, ["export_original"])
        self.assertEqual(rec["state"], "ok")


class SummariesTest(unittest.TestCase):
    def test_server_error_is_not_an_expired_session(self):
        for answer in ((500, b"<html>error</html>"), (200, b"<html>maintenance</html>")):
            with mock.patch.object(fetch, "get", return_value=answer), self.assertRaises(fetch.UpstreamError):
                fetch.fetch_summaries("c")

    def test_login_form_is_an_expired_session(self):
        with mock.patch.object(fetch, "http_get", return_value=(200, b'<form id="login_form">')), \
                self.assertRaises(fetch.SessionExpired):
            fetch.get("c", "https://www.strava.com/x")


if __name__ == "__main__":
    unittest.main()
