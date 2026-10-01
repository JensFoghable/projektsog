"""tests/_ui_browser.Edge leaves no Edge process behind – after close() and when its Python
process dies without closing (skipped without Edge; ~4 s).

Every process of the headless test browser lives in a kill-on-close job object. Earlier runs
that were cut short (a timeout, a killed test runner) left headless Edge instances running.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from tests._ui_browser import EDGE_FLAGS, CDPError, Edge, descendants, find_edge, process_alive

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CREATE_NO_WINDOW = 0x08000000
# A test process that starts Edge and then dies without closing it.
CHILD = """
import json, time
from tests._ui_browser import Edge
edge = Edge()
page = edge.start()
page.navigate("data:text/html,<p>x</p>")
print(json.dumps({"pids": edge.pids(), "profile": edge.profile}), flush=True)
time.sleep(120)
"""

_tmp: tempfile.TemporaryDirectory | None = None


def setUpModule() -> None:
    global _tmp
    _tmp = tempfile.TemporaryDirectory()
    os.environ["LOCALAPPDATA"] = _tmp.name
    reason = None
    if os.environ.get("PROJEKTSOG_SKIP_UI_BROWSER") == "1":
        reason = "PROJEKTSOG_SKIP_UI_BROWSER=1"
    elif find_edge() is None:
        reason = "Microsoft Edge is not installed"
    if reason:
        _tmp.cleanup()
        raise unittest.SkipTest(reason)


def tearDownModule() -> None:
    if _tmp is not None:
        _tmp.cleanup()


def _wait_gone(pids: list[int], timeout: float = 8.0) -> list[int]:
    deadline = time.monotonic() + timeout
    while True:
        alive = [pid for pid in pids if process_alive(pid)]
        if not alive or time.monotonic() > deadline:
            return alive


class EdgeCleanupTests(unittest.TestCase):
    def test_no_background_mode_or_crash_reporting(self) -> None:
        for flag in ("--headless=new", "--disable-background-mode", "--disable-breakpad",
                     "--disable-crash-reporter", "--no-first-run", "--disable-component-update"):
            self.assertIn(flag, EDGE_FLAGS)

    def test_close_ends_every_process_of_the_tree(self) -> None:
        edge = Edge()
        try:
            page = edge.start()
        except (CDPError, OSError) as exc:
            edge.close()
            self.skipTest(f"headless Edge unavailable: {exc}")
        page.navigate("data:text/html,<p>x</p>")
        browser = edge.proc.pid
        tree = descendants(browser, exe="msedge.exe")  # before the job list: children only join
        pids = edge.pids()
        profile = edge.profile
        self.assertIn(browser, pids)
        self.assertGreater(len(tree), 1, "renderer, GPU/utility processes, crashpad handler")
        escaped = [pid for pid in sorted(set(tree) - set(pids)) if process_alive(pid)]
        self.assertEqual(escaped, [], "every process Edge starts is in the job")
        edge.close()
        self.assertEqual(_wait_gone(pids + tree, timeout=2.0), [])
        self.assertFalse(os.path.exists(profile))
        edge.close()  # idempotent

    def test_edge_dies_with_the_python_process_that_started_it(self) -> None:
        child = subprocess.Popen([sys.executable, "-c", CHILD], cwd=REPO_ROOT, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                 creationflags=CREATE_NO_WINDOW)
        info = None
        try:
            line = child.stdout.readline()
            info = json.loads(line) if line.strip() else None
            if info is None:
                self.skipTest("headless Edge unavailable in the child process")
            self.assertTrue(info["pids"])
            self.assertTrue(all(process_alive(pid) for pid in info["pids"]))
        finally:
            child.kill()  # like a test runner killed by a timeout: Edge.close() never runs
            child.wait(timeout=10)
            child.stdout.close()
        try:
            self.assertEqual(_wait_gone(info["pids"]), [], "Windows ends the job with its last handle")
        finally:
            for _ in range(20):
                shutil.rmtree(info["profile"], ignore_errors=True)
                if not os.path.exists(info["profile"]):
                    break
                time.sleep(0.25)


if __name__ == "__main__":
    unittest.main()
