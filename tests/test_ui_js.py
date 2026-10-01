"""Runs the Node unit tests of the UI's pure helpers (tests/ui/*.test.js); skipped without Node.js."""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CREATE_NO_WINDOW = 0x08000000

_tmp: tempfile.TemporaryDirectory | None = None


def setUpModule() -> None:
    global _tmp
    _tmp = tempfile.TemporaryDirectory()
    os.environ["LOCALAPPDATA"] = _tmp.name


def tearDownModule() -> None:
    if _tmp is not None:
        _tmp.cleanup()


class NodeHelperTests(unittest.TestCase):
    def test_node_unit_tests_pass(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is not installed")
        files = sorted(glob.glob(os.path.join(REPO_ROOT, "tests", "ui", "*.test.js")))
        self.assertTrue(files, "no tests/ui/*.test.js files")
        result = subprocess.run([node, "--test", *files], cwd=REPO_ROOT, capture_output=True,
                                encoding="utf-8", errors="replace", timeout=60,
                                creationflags=CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stdout[-4000:] + result.stderr[-2000:])
        self.assertIn("# fail 0", result.stdout.replace("ℹ", "#"))


if __name__ == "__main__":
    unittest.main()
