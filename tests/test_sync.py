import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SYNC = Path(__file__).resolve().parents[1] / "scripts" / "sync.sh"


class SyncExitTest(unittest.TestCase):
    """sync.sh run on a copy of itself, with PY replaced by a stub."""

    def run_sync(self, py):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "scripts").mkdir()
            shutil.copy(SYNC, Path(root) / "scripts" / "sync.sh")
            r = subprocess.run(["bash", str(Path(root) / "scripts" / "sync.sh")],
                               env={"PATH": "/usr/bin:/bin", "PY": py}, capture_output=True, text=True)
            return r.returncode, (Path(root) / "logs" / "cron.log").read_text()

    def test_all_stages_failing_exits_nonzero(self):
        rc, log = self.run_sync("false")
        self.assertEqual(rc, 1)
        self.assertIn("fetch=1 analyze=1 progress=1 story=1 sheet=1", log)

    def test_all_stages_passing_exits_zero(self):
        rc, log = self.run_sync("true")
        self.assertEqual(rc, 0)
        self.assertIn("fetch=0 analyze=0 progress=0 story=0 sheet=0", log)

    def test_fetch_failure_still_runs_later_stages(self):
        with tempfile.TemporaryDirectory() as d:
            stub = Path(d) / "py"
            stub.write_text('#!/bin/bash\n[ "$2" = scripts/fetch.py ] && exit 2\nexit 0\n')
            stub.chmod(0o755)
            rc, log = self.run_sync(str(stub))
        self.assertEqual(rc, 1)
        self.assertIn("fetch=2 analyze=0 progress=0 story=0 sheet=0", log)


if __name__ == "__main__":
    unittest.main()
