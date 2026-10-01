"""Index engine: scan scheduling, dispatch constraints and worker supervision (SPEC §2, §8)."""

import os
import sys
import time
from types import SimpleNamespace
from unittest import mock

from projektsog import db, indexer
from tests._index_engine_fixtures import EngineTestCase, fake_worker_argv, module_env, project

_env = None


def setUpModule():
    global _env
    _env = module_env()


def tearDownModule():
    _env.cleanup()


def _source(sid, *, kind="local", host="TESTPC", serial="V1", **fields):
    values = {"id": sid, "key": f"k{sid}", "kind": kind, "host": host,
              "share": f"S{sid}" if kind == "share" else None, "display_name": f"src{sid}",
              "current_path": f"C:\\src{sid}", "volume_serial": serial if kind == "local" else None,
              "online": 1, "auto_include": 1, "last_scan_end": 1.0, "last_shallow_scan": 1.0,
              "last_full_scan": time.time()}
    values.update(fields)
    return indexer._Source(**values)


class DispatchRulesTest(EngineTestCase):
    """``_dispatch`` on hand-made registries (no threads, no worker process)."""

    def setUp(self):
        super().setUp()
        self.ix = indexer.Indexer(self.cfg, self.bus, db_path=self.db_path,
                                  env=self.world.env(), start_worker=False)
        self.ix._worker = SimpleNamespace(ready=True)

    def add(self, *sources):
        for src in sources:
            self.ix._sources[src.id] = src

    def dispatch(self):
        with self.ix._lock:
            return self.ix._dispatch(time.monotonic(), time.time())

    def finish(self, command):
        with self.ix._lock:
            job = self.ix._jobs.pop(command["job"])
            self.ix._sources[job.source_id].job = None

    def test_one_deep_per_device_smallest_first_and_a_free_slot(self):
        self.add(_source(1, scan_seconds=30.0), _source(2, scan_seconds=3.0),
                 _source(3, kind="share", host="NAS", scan_seconds=400.0),
                 _source(4, kind="share", host="NAS", scan_seconds=4.0),
                 _source(5, kind="share", host="OTHER", scan_seconds=9.0),
                 _source(6, serial="V2", scan_seconds=1.0))
        with self.ix._lock:
            for src in self.ix._sources.values():
                self.ix._request(src, "deep")
        first = self.dispatch()
        # max_parallel_scans 4: at most 3 deep jobs, so one slot stays free for shallow scans
        self.assertEqual([c["source_id"] for c in first], [6, 2, 4])
        self.assertTrue(all(c["kind"] == "deep" for c in first))
        self.assertEqual(self.dispatch(), [])
        self.finish(first[0])                                   # V2 done: 5 (OTHER) may start
        self.assertEqual([c["source_id"] for c in self.dispatch()], [5])
        self.finish(first[1])                                   # V1 free: source 1 next
        self.assertEqual([c["source_id"] for c in self.dispatch()], [1])
        self.finish(first[2])                                   # NAS free: the big one last
        self.assertEqual([c["source_id"] for c in self.dispatch()], [3])

    def test_first_run_orders_by_the_shallow_dir_count(self):
        self.add(_source(1, kind="share", host="NAS", scan_seconds=None, last_scan_end=None,
                         dir_count=900),
                 _source(2, kind="share", host="NAS", scan_seconds=None, last_scan_end=None,
                         dir_count=40),
                 _source(3, kind="share", host="NAS", scan_seconds=12.0))
        with self.ix._lock:
            for src in self.ix._sources.values():
                self.ix._request(src, "deep")
        order = []
        for _ in range(3):
            (command,) = self.dispatch()
            order.append(command["source_id"])
            self.finish(command)
        self.assertEqual(order, [2, 1, 3])       # never-scanned first, small before big

    def test_window_rounds_visit_one_network_share_at_a_time(self):
        self.cfg.update({"max_parallel_scans": 8})
        self.add(_source(1, kind="share", host="NAS"), _source(2, kind="share", host="NAS"),
                 _source(3, kind="share", host="PC2"), _source(4), _source(5, serial="V2"))
        with self.ix._lock:
            for src in self.ix._sources.values():
                self.ix._request(src, "shallow", max_listings=200, window=src.kind == "share")
        rounds = []
        while True:
            sent = self.dispatch()
            if not sent:
                break
            rounds.append(sorted(c["source_id"] for c in sent))
            for command in sent:
                self.finish(command)
        # local shallow scans run at once; window shallow scans: one network host at a time
        self.assertEqual(rounds, [[1, 4, 5], [2], [3]])

    def test_a_first_time_shallow_always_precedes_the_deep_scan(self):
        self.add(_source(1, kind="share", host="NAS", last_scan_end=None, last_shallow_scan=None),
                 _source(2, kind="share", host="NAS", last_scan_end=None, last_shallow_scan=None))
        with self.ix._lock:
            for src in self.ix._sources.values():
                self.ix._request_deep(src)
        rounds = []
        running = []
        for _ in range(10):
            sent = self.dispatch()
            rounds.append(sorted((c["source_id"], c["kind"]) for c in sent))
            running += sent
            if not running:
                break
            self.finish(running.pop(0))
        # First-time shallow scans of one host run side by side; its deep scans one by one.
        self.assertEqual(rounds[:4], [[(1, "shallow"), (2, "shallow")], [(1, "deep")], [],
                                      [(2, "deep")]])

    def test_pending_work_of_offline_or_excluded_sources_is_dropped(self):
        self.add(_source(1, online=0), _source(2, auto_include=0), _source(3, persisted=False))
        with self.ix._lock:
            for src in self.ix._sources.values():
                self.ix._request(src, "deep")
        self.assertEqual(self.dispatch(), [])
        self.assertEqual([bool(s.pending) for s in self.ix._sources.values()],
                         [False, False, True])     # 3 waits until its row is saved

    def test_intervals(self):
        local = _source(1)
        share = _source(2, kind="share", host="NAS", scan_seconds=442.0)
        small = _source(3, kind="share", host="NAS", scan_seconds=30.0)
        self.assertEqual(self.ix._deep_interval(local), 180.0)
        self.assertEqual(self.ix._deep_interval(share), 4 * 442.0)      # adaptive
        self.assertEqual(self.ix._deep_interval(small), 600.0)          # at least 10 min
        self.cfg.update({"scan_interval_network_min": 60})
        self.assertEqual(self.ix._deep_interval(share), 3600.0)
        now = time.time()
        self.assertFalse(self.ix._full_due(local, now))
        self.assertTrue(self.ix._full_due(local, now + 25 * 3600))
        local.needs_full = True
        self.assertTrue(self.ix._full_due(local, now))


class SchedulingTest(EngineTestCase):
    def test_first_time_shallow_then_full_deep(self):
        self.world.volume("vol", "A0000001", tree=project("Kunder\\Rikke Lindholm", "Klip\\a.mov"))
        ix = self.start()
        self.settled(ix)
        sid = self.source(ix, "Kunder")["id"]
        shallow, deep = self.scans(sid)
        self.assertEqual((shallow["kind"], shallow["first_time"], shallow["max_listings"]),
                         ("shallow", True, 2000))
        self.assertEqual((deep["kind"], deep["full"], deep["is_network"], deep["fs"]),
                         ("deep", True, False, "NTFS"))
        shallow_done = next(t for t, job, _ in self.finished if job == shallow["job"])
        deep_sent = next(t for t, m in self.sent if m.get("job") == deep["job"])
        self.assertLess(shallow_done, deep_sent)
        src = ix._sources[sid]
        self.assertIsNotNone(src.last_shallow_scan)
        self.assertEqual((src.last_scan_ok, src.last_error), (1, None))
        self.assertIsNotNone(src.last_full_scan)
        self.assertEqual(ix.status()["sources_ready"], 1)
        self.assertTrue(self.events_of("index_updated"))

    def test_deep_scans_run_one_per_host_smallest_first(self):
        folders = {name: self.world.share("NAS", name, project(f"P{name}"))
                   for name in ("A", "B", "C")}
        seconds = {"A": 50.0, "B": 5.0, "C": 20.0}
        conn = db.connect(self.db_path, writer=True)
        db.ensure_schema(conn)
        now = time.time()
        ids = {}
        for name, folder in folders.items():
            ids[name] = db.upsert_source(conn, {
                "key": f"unc:NAS\\{name}", "kind": "share", "host": "NAS", "share": name,
                "display_name": name, "current_path": folder, "unc_path": folder, "fs": "NTFS",
                "auto_include": 1, "auto_reason": "1 projektmappe fundet", "probed_at": now,
                "last_scan_end": now - 3600, "last_scan_ok": 1, "last_full_scan": now - 60,
                "last_shallow_scan": now - 3600, "scan_seconds": seconds[name]})
        conn.close()
        self.cfg.update({"hosts": ["NAS"]})
        ix = self.start()
        self.wait_until(lambda: len(self.scans(kind="deep")) == 3
                        and not ix.status()["scanning"], message="three deep scans")
        deep = self.scans(kind="deep")
        self.assertEqual([m["source_id"] for m in deep], [ids["B"], ids["C"], ids["A"]])
        self.assertTrue(all(m["is_network"] and not m["full"] for m in deep))  # incremental
        for earlier, later in zip(deep, deep[1:]):
            done = next(t for t, job, _ in self.finished if job == earlier["job"])
            sent = next(t for t, m in self.sent if m.get("job") == later["job"])
            self.assertLess(done, sent)

    def test_periodic_deep_scans_follow_the_clock(self):
        self.world.volume("vol", "A0000002", tree=project("Kunder\\Rikke Lindholm"))
        self.world.share("NAS", "Delt", project("Pixelbro"))
        self.cfg.update({"hosts": ["NAS"]})
        ix = self.start()
        self.settled(ix)
        local = self.source(ix, "Kunder")["id"]
        share = self.source(ix, "Delt")["id"]
        counts = lambda sid: len(self.scans(sid, "deep"))            # noqa: E731
        self.assertEqual((counts(local), counts(share)), (1, 1))

        # Every clock jump happens while nothing runs: a scan that ends after a jump would
        # (correctly) record the later time and postpone its next period.
        self.world.clock.advance(2 * 60)
        time.sleep(0.7)
        self.assertEqual((counts(local), counts(share)), (1, 1))
        self.world.clock.advance(61)                                   # local: 3 min
        self.wait_until(lambda: counts(local) == 2, message="the periodic local scan")
        self.settled(ix)
        self.assertEqual(counts(share), 1)
        self.world.clock.advance(8 * 60)                               # network: 10 min
        self.wait_until(lambda: counts(share) == 2, message="the periodic network scan")
        self.settled(ix)
        self.assertFalse(self.scans(share, "deep")[-1]["full"])
        self.world.clock.advance(25 * 3600)                            # full_rescan_hours
        self.wait_until(lambda: counts(share) == 3, message="the full rescan")
        self.assertTrue(self.scans(share, "deep")[-1]["full"])

    def test_on_window_shown_merges_calls_and_skips_fresh_sources(self):
        self.world.volume("vol", "A0000003", tree=project("Kunder\\Rikke Lindholm"))
        self.world.share("NAS", "Delt", project("Pixelbro"))
        self.cfg.update({"hosts": ["NAS"]})
        ix = self.start()
        self.settled(ix)
        before = len(self.scans(kind="shallow"))
        deep_before = len(self.scans(kind="deep"))
        ix.on_window_shown()                     # everything was scanned seconds ago
        time.sleep(0.4)
        self.assertEqual(len(self.scans(kind="shallow")), before)

        self.world.clock.advance(31)
        for _ in range(3):
            ix.on_window_shown()
        self.wait_until(lambda: len(self.scans(kind="shallow")) == before + 2,
                        message="window shallow scans")
        time.sleep(0.4)
        window = self.scans(kind="shallow")[before:]
        self.assertEqual(len(window), 2)                       # merged: one round only
        listings = {ix._sources[m["source_id"]].kind: m["max_listings"] for m in window}
        self.assertEqual(listings, {"local": 2000, "share": 200})
        self.assertFalse(any(m["first_time"] for m in window))
        self.assertEqual(len(self.scans(kind="deep")), deep_before)  # never deep scans

    def test_path_missing_is_rate_limited(self):
        root = os.path.join(self.tmp, "vol")
        self.world.volume("vol", "A0000004", tree=project("Kunder\\Rikke Lindholm"))
        ix = self.start()
        self.settled(ix)
        sid = self.source(ix, "Kunder")["id"]
        gone = os.path.join(root, "Kunder", "Rikke Lindholm", "Slettet")
        ix.path_missing(gone)
        ix.path_missing(gone)
        self.settled(ix)
        self.assertEqual((len(self.scans(sid, "shallow")), len(self.scans(sid, "deep"))), (2, 2))
        ix.path_missing("Q:\\ukendt\\sti")
        ix.path_missing(os.path.join(root, "Kunder", "andet"))
        time.sleep(0.3)
        self.assertEqual(len(self.scans(sid)), 4)
        self.world.clock.advance(3 * 60 + 1)
        ix.path_missing(gone)
        self.wait_until(lambda: len(self.scans(sid, "shallow")) == 3, message="a new round")
        self.world.volumes.clear()
        self.rediscover(ix)
        self.world.clock.advance(3 * 60 + 1)
        ix.path_missing(gone)                                   # offline: ignored
        time.sleep(0.3)
        self.assertEqual(len(self.scans(sid, "shallow")), 3)


class WorkerSupervisionTest(EngineTestCase):
    def test_offline_source_cancels_its_running_job(self):
        self.world.volume("vol", "B0000001", tree=project("Kunder\\Rikke Lindholm"))
        ix = self.start(worker_argv=fake_worker_argv())
        (running,) = self.wait_until(lambda: self.scans(kind="shallow"), message="a job")
        self.world.volumes.clear()
        self.rediscover(ix)
        self.wait_until(lambda: {"cmd": "cancel", "job": running["job"]} in self.commands(),
                        message="the cancel")
        self.wait_until(lambda: not ix.status()["scanning"], message="the job to end")
        src = ix._sources[running["source_id"]]
        self.assertEqual((src.last_scan_end, src.last_shallow_scan, src.pending), (None, None, {}))

    def test_crashed_worker_is_restarted_and_its_jobs_requeued(self):
        self.world.volume("vol", "B0000002", tree=project("Kunder\\Rikke Lindholm"))
        ix = self.start(worker_argv=fake_worker_argv())
        (job,) = self.wait_until(lambda: self.scans(kind="shallow"), message="a job")
        first_pid = ix._worker.pid
        ix._worker.proc.kill()
        again = self.wait_until(lambda: self.scans(kind="shallow")[1:], message="a re-sent job")
        self.assertEqual((again[0]["source_id"], again[0]["first_time"]),
                         (job["source_id"], True))
        status = ix.status()["worker"]
        self.assertEqual(status, {"running": True, "restarts": 1})
        self.assertNotEqual(ix._worker.pid, first_pid)

    def test_restart_budget(self):
        with mock.patch.object(indexer, "WORKER_MAX_RESTARTS", 2):
            ix = self.start(worker_argv=[sys.executable, "-c", "pass"])
            self.wait_until(lambda: ix.status()["worker"]["restarts"] == 2
                            and ix._worker is None, message="two restarts")
            time.sleep(0.5)
            self.assertEqual(ix.status()["worker"], {"running": False, "restarts": 2})

    def test_stop_is_bounded_even_with_a_hung_worker(self):
        ix = self.start(worker_argv=[sys.executable, "-c", "import time; time.sleep(60)"])
        proc = self.wait_until(lambda: ix._worker and ix._worker.proc, message="the worker")
        started = time.monotonic()
        ix.stop(timeout=1.0)
        self.assertLess(time.monotonic() - started, 1.8)
        self.assertIsNotNone(proc.poll())
