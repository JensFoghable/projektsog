"""Scan worker (SPEC §5.3): job scheduling in-process and JSON-lines IPC as a subprocess."""

import ctypes
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from ctypes import wintypes

from projektsog import scanner, scanworker
from tests._index_store_fixtures import TempIndex, cfg, make_tree, module_env, settle

_env = None
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CREATE_NO_WINDOW = 0x08000000
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_GetVolumeNameForVolumeMountPointW = _kernel32.GetVolumeNameForVolumeMountPointW
_GetVolumeNameForVolumeMountPointW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR,
                                               wintypes.DWORD]
_GetVolumeNameForVolumeMountPointW.restype = wintypes.BOOL


def setUpModule():
    global _env
    _env = module_env()


def tearDownModule():
    _env.cleanup()


class EventLog:
    """Thread-safe event sink with waiting helpers."""

    def __init__(self):
        self.events = []
        self._cond = threading.Condition()

    def __call__(self, event):
        with self._cond:
            self.events.append(event)
            self._cond.notify_all()

    def wait_for(self, predicate, timeout=10.0):
        with self._cond:
            if not self._cond.wait_for(lambda: any(predicate(e) for e in self.events), timeout):
                raise AssertionError(f"event not seen; got {self.events}")
            return next(e for e in self.events if predicate(e))

    def done(self, job, timeout=10.0):
        return self.wait_for(lambda e: e.get("ev") == "done" and e.get("job") == job, timeout)

    def index(self, predicate):
        with self._cond:
            return next(i for i, e in enumerate(self.events) if predicate(e))


class GateLister:
    """Blocks every listing until released; reports which job threads entered."""

    def __init__(self, log):
        self.log = log
        self.release = threading.Event()

    def __call__(self, path):
        self.log({"ev": "enter", "thread": threading.current_thread().name})
        self.release.wait(10)
        return scanner.list_dir(path)

    def entered(self, job):
        return lambda e: e.get("ev") == "enter" and e["thread"].startswith(f"job-{job}-")


class WorkerTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.index = TempIndex(self.tmp)
        self.addCleanup(self.index.close)
        self.roots, self.ids = {}, {}
        for name in ("A", "B"):
            root = os.path.join(self.tmp, name)
            make_tree(root, [f"P{i}\\Klip\\c{j}.mxf" for i in range(3) for j in range(3)]
                      + ["P0\\Final\\", "loose.txt"])
            settle(root)
            self.roots[name] = root
            self.ids[name] = self.index.add_source(root, key=f"test:{name}")
        self.log = EventLog()

    def worker(self, lister=None, **overrides):
        worker = scanworker.Worker(self.index.conn, self.log, cfg=cfg(**overrides), lister=lister)
        self.addCleanup(worker.shutdown, 5)
        return worker

    def scan(self, job, name, kind="deep", **extra):
        return {"cmd": "scan", "job": job, "source_id": self.ids[name],
                "root_path": self.roots[name], "kind": kind, "full": False, "first_time": False,
                "max_listings": 2000, "is_network": False, "fs": "NTFS", **extra}


class WorkerTests(WorkerTestCase):
    def test_scan_reports_progress_commits_and_result(self):
        worker = self.worker()
        self.assertTrue(worker.handle(self.scan(1, "A")))
        done = self.log.done(1)
        result = done["result"]
        self.assertEqual(done["source_id"], self.ids["A"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["counts"]["entry_count"], len(self.index.rows(self.ids["A"])))
        commits = [e for e in self.log.events if e["ev"] == "committed"]
        self.assertEqual(sum(e["changed"] for e in commits), result["changed"])
        progress = [e for e in self.log.events if e["ev"] == "progress"]
        self.assertTrue(progress)
        self.assertEqual(set(progress[-1]), {"ev", "job", "source_id", "entries", "dirs",
                                             "units_done", "units_total"})
        self.assertTrue(worker.handle(self.scan(2, "A", kind="shallow")))
        self.assertEqual(self.log.done(2)["result"]["changed"], 0)

    def test_parallel_limit_and_one_job_per_source(self):
        gate = GateLister(self.log)
        worker = self.worker(gate, max_parallel_scans=2)
        worker.handle(self.scan(1, "A"))
        worker.handle(self.scan(2, "A", kind="shallow"))
        worker.handle(self.scan(3, "B"))
        self.log.wait_for(gate.entered(1))
        self.log.wait_for(gate.entered(3))
        self.assertFalse(any(gate.entered(2)(e) for e in self.log.events))
        gate.release.set()
        for job in (1, 2, 3):
            self.assertTrue(self.log.done(job)["result"]["ok"])
        self.assertGreater(self.log.index(gate.entered(2)),
                           self.log.index(lambda e: e.get("ev") == "done" and e["job"] == 1))

    def test_config_changes_the_parallel_limit(self):
        gate = GateLister(self.log)
        worker = self.worker(gate, max_parallel_scans=1)
        worker.handle(self.scan(1, "A"))
        worker.handle(self.scan(2, "B"))
        self.log.wait_for(gate.entered(1))
        self.assertFalse(any(gate.entered(2)(e) for e in self.log.events))
        worker.handle({"cmd": "config", "cfg": cfg(max_parallel_scans=2)})
        self.log.wait_for(gate.entered(2))
        gate.release.set()
        self.log.done(1)
        self.log.done(2)

    def test_cancel_running_and_queued_jobs(self):
        gate = GateLister(self.log)
        worker = self.worker(gate, max_parallel_scans=1)
        worker.handle(self.scan(1, "A"))
        worker.handle(self.scan(2, "B"))
        self.log.wait_for(gate.entered(1))
        worker.handle({"cmd": "cancel", "job": 2})
        queued = self.log.done(2)["result"]
        self.assertEqual((queued["aborted"], queued["counts"]), (True, None))
        worker.handle({"cmd": "cancel", "job": 1})
        gate.release.set()
        running = self.log.done(1)["result"]
        self.assertEqual((running["ok"], running["aborted"]), (False, True))
        self.assertFalse(any(gate.entered(2)(e) for e in self.log.events))

    def test_cancel_source(self):
        gate = GateLister(self.log)
        worker = self.worker(gate, max_parallel_scans=1)
        worker.handle(self.scan(1, "A"))
        worker.handle(self.scan(2, "A", kind="shallow"))
        self.log.wait_for(gate.entered(1))
        worker.handle({"cmd": "cancel_source", "source_id": self.ids["A"]})
        self.assertTrue(self.log.done(2)["result"]["aborted"])
        gate.release.set()
        self.assertTrue(self.log.done(1)["result"]["aborted"])

    def test_forget_deletes_all_entries_of_the_source(self):
        worker = self.worker()
        worker.handle(self.scan(1, "A"))
        worker.handle(self.scan(2, "B"))
        entries = self.log.done(1)["result"]["counts"]["entry_count"]
        self.log.done(2)
        worker.handle({"cmd": "forget", "job": 3, "source_id": self.ids["A"]})
        result = self.log.done(3)["result"]
        self.assertEqual((result["ok"], result["deleted"], result["counts"]["entry_count"]),
                         (True, entries, 0))
        self.assertIn({"ev": "committed", "job": 3, "source_id": self.ids["A"],
                       "changed": entries}, self.log.events)
        self.assertEqual(self.index.rows(self.ids["A"]), {})
        self.assertTrue(self.index.rows(self.ids["B"]))
        self.index.check_fts()

    def test_database_error_fails_the_job_and_rolls_back(self):
        worker = self.worker()
        worker.handle({**self.scan(1, "A"), "source_id": 999})      # no such source row
        failed = self.log.wait_for(lambda e: e.get("job") == 1)
        self.assertEqual(failed["ev"], "failed")
        self.assertTrue(failed["error"].startswith("Scanningen fejlede"))
        self.assertFalse(self.index.conn.in_transaction)
        worker.handle(self.scan(2, "A"))                             # the worker keeps going
        self.assertTrue(self.log.done(2)["result"]["ok"])

    def test_invalid_commands(self):
        worker = self.worker()
        cases = [(self.scan(1, "A", root_path=None), "Ugyldig scanningsopgave"),
                 (self.scan(2, "A", root_path="relative\\dir"), "Ugyldig scanningsopgave"),
                 (self.scan(3, "A", kind="full"), "Ugyldig scanningsopgave"),
                 ({"cmd": "forget", "job": 4}, "Ugyldig opgave")]
        for msg, error in cases:
            self.assertTrue(worker.handle(msg))
            failed = self.log.wait_for(lambda e, j=msg["job"]: e.get("job") == j)
            self.assertEqual((failed["ev"], failed["error"]), ("failed", error))
        self.assertTrue(worker.handle({"cmd": "nonsense"}))
        self.assertTrue(worker.handle({"cmd": "config", "cfg": "not a dict"}))
        self.assertFalse(worker.handle({"cmd": "quit"}))

    def test_scan_of_a_swapped_disk_writes_nothing(self):
        # IDX-3: another disk (serial BBBB2222) now sits where AAAA1111 was expected.
        reads = []

        def serial_reader(path):
            reads.append(path)
            return "BBBB2222"

        worker = scanworker.Worker(self.index.conn, self.log, cfg=cfg(), lister=None,
                                   serial_reader=serial_reader)
        self.addCleanup(worker.shutdown, 5)
        for job, kind in ((1, "deep"), (2, "shallow")):
            worker.handle(self.scan(job, "A", kind=kind, expected_serial="aaaa1111"))
            result = self.log.done(job)["result"]
            self.assertEqual((result["ok"], result["aborted"], result["error"],
                              result["volume_changed"], result["changed"]),
                             (False, True, "Disken er skiftet", True, 0))
        self.assertEqual(reads, [self.roots["A"]] * 2)
        self.assertEqual(self.index.rows(self.ids["A"]), {})
        self.assertFalse([e for e in self.log.events if e["ev"] == "committed"])

    def test_disk_swapped_during_a_scan(self):
        serials = iter(["AAAA1111", "AAAA1111", "AAAA1111"])      # then: unreadable (removed)
        worker = scanworker.Worker(self.index.conn, self.log, cfg=cfg(),
                                   serial_reader=lambda path: next(serials, None))
        self.addCleanup(worker.shutdown, 5)
        worker.handle(self.scan(1, "A", expected_serial="AAAA1111"))
        result = self.log.done(1)["result"]
        self.assertEqual((result["aborted"], result["error"], result["units_done"]),
                         (True, "Disken er skiftet", 2))       # root files + unit P0 only
        rows = self.index.rows(self.ids["A"])
        self.assertIn("loose.txt", rows)
        self.assertIn("P0\\Klip\\c0.mxf", rows)
        self.assertNotIn("P1", rows)

    def test_expected_serial_matches_or_is_absent(self):
        reads = []
        worker = scanworker.Worker(self.index.conn, self.log, cfg=cfg(),
                                   serial_reader=lambda path: reads.append(path) or "5E3A0B21")
        self.addCleanup(worker.shutdown, 5)
        worker.handle(self.scan(1, "A", expected_serial="5e3a0b21"))
        self.assertTrue(self.log.done(1)["result"]["ok"])
        self.assertEqual(len(reads), 1 + 4)            # before the listing + 4 transactions
        worker.handle(self.scan(2, "B"))               # shares/old commands: no check
        self.assertTrue(self.log.done(2)["result"]["ok"])
        self.assertEqual(len(reads), 5)

    def test_volume_serial_of_a_real_folder(self):
        serial = scanworker.volume_serial(self.roots["A"])
        self.assertRegex(serial, r"^[0-9A-F]{8}$")
        self.assertEqual(scanworker.volume_serial(self.roots["A"].lower() + "\\P0"), serial)
        self.assertEqual(scanworker.volume_root("h:/2024 Disk Sølv"), "H:\\")
        self.assertEqual(scanworker.volume_root("\\\\?\\H:\\2024 Disk Sølv"), "H:\\")
        system = os.environ.get("SystemDrive", "C:") + "\\"
        self.assertEqual(scanworker.volume_root(system.lower()), system.upper())
        buf = ctypes.create_unicode_buffer(64)
        if _GetVolumeNameForVolumeMountPointW(system, buf, len(buf)):   # \\?\Volume{…}\
            self.assertEqual(scanworker.volume_serial(buf.value + "Windows"),
                             scanworker.volume_serial(system))

    def test_shutdown_cancels_everything(self):
        gate = GateLister(self.log)
        worker = scanworker.Worker(self.index.conn, self.log, cfg=cfg(max_parallel_scans=1),
                                   lister=gate)
        worker.handle(self.scan(1, "A"))
        worker.handle(self.scan(2, "B"))
        self.log.wait_for(gate.entered(1))
        threading.Timer(0.2, gate.release.set).start()
        self.assertTrue(worker.shutdown(5))
        self.assertTrue(self.log.done(1)["result"]["aborted"])
        self.assertTrue(self.log.done(2)["result"]["aborted"])
        worker.handle(self.scan(3, "A"))                 # after shutdown: refused at once
        self.assertTrue(self.log.done(3)["result"]["aborted"])


class WorkerProcessTests(WorkerTestCase):
    """The real process: ``python -m projektsog.scanworker --db <path>`` over pipes."""

    def start(self):
        env = dict(os.environ, LOCALAPPDATA=self.tmp, PYTHONPATH=REPO_ROOT)
        stderr = open(os.path.join(self.tmp, "stderr.txt"), "wb")
        self.addCleanup(stderr.close)
        proc = subprocess.Popen([sys.executable, "-m", "projektsog.scanworker",
                                 "--db", self.index.path],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
                                cwd=REPO_ROOT, env=env, creationflags=CREATE_NO_WINDOW)
        self.addCleanup(self._stop, proc)
        events = queue.Queue()

        def pump():
            for line in proc.stdout:
                events.put(json.loads(line))

        threading.Thread(target=pump, daemon=True).start()
        return proc, events

    @staticmethod
    def _stop(proc):
        if proc.poll() is None:
            proc.kill()
            proc.wait(10)
        for stream in (proc.stdin, proc.stdout):
            try:
                stream.close()
            except OSError:          # pipe already broken
                pass

    @staticmethod
    def send(proc, message):
        proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        proc.stdin.flush()

    @staticmethod
    def expect(events, predicate, seen, timeout=20.0):
        while True:
            event = events.get(timeout=timeout)
            seen.append(event)
            if predicate(event):
                return event

    def test_json_lines_session_and_exit_on_stdin_eof(self):
        proc, events = self.start()
        seen = []
        ready = self.expect(events, lambda e: e["ev"] == "ready", seen)
        self.assertEqual(ready["pid"], proc.pid)
        self.send(proc, {"cmd": "config", "cfg": cfg(max_parallel_scans=2)})
        self.send(proc, self.scan(1, "A"))
        self.send(proc, self.scan(2, "B", kind="shallow", first_time=True))
        done = {}
        for _ in range(2):
            event = self.expect(events, lambda e: e["ev"] == "done", seen)
            done[event["job"]] = event["result"]
        self.assertTrue(done[1]["ok"] and done[2]["ok"])
        self.assertEqual(done[1]["counts"]["entry_count"], len(self.index.rows(self.ids["A"])))
        self.assertTrue(any(e["ev"] == "committed" and e["job"] == 1 for e in seen))
        self.assertTrue(any(e["ev"] == "progress" and e["job"] == 1 for e in seen))

        self.send(proc, "not json")
        self.send(proc, {"cmd": "forget", "job": 3, "source_id": self.ids["A"]})
        forget = self.expect(events, lambda e: e["ev"] == "done" and e["job"] == 3, seen)
        self.assertEqual(forget["result"]["deleted"], done[1]["counts"]["entry_count"])
        self.assertEqual(self.index.rows(self.ids["A"]), {})

        proc.stdin.close()                                   # parent gone → worker exits
        self.assertEqual(proc.wait(15), 0)
        log_file = os.path.join(self.tmp, "Projektsog", "logs", "scanworker.log")
        with open(log_file, encoding="utf-8") as fh:
            self.assertIn("ready", fh.read())

    def test_quit_command(self):
        proc, events = self.start()
        self.expect(events, lambda e: e["ev"] == "ready", [])
        self.send(proc, {"cmd": "quit"})
        self.assertEqual(proc.wait(15), 0)


if __name__ == "__main__":
    unittest.main()
