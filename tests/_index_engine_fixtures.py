"""Helpers for the index-engine tests: a fake machine/network on temporary folders.

Volumes are temporary folders (``FakeWorld.volume``), remote shares map to temporary folders
(``FakeWorld.share``), and the wall clock can be advanced.  The Indexer runs with the real scan
worker process unless a test passes ``worker_argv=fake_worker_argv()`` – a tiny scripted worker
that keeps jobs in flight, which makes cancel/progress/restart behaviour deterministic.
"""

from __future__ import annotations

import json
import os
import queue
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Callable
from typing import Any
from unittest import mock

from projektsog import config, discovery, events, indexer
from tests._index_store_fixtures import make_tree, module_env

__all__ = ["EngineTestCase", "FakeWorld", "fake_worker_argv", "forbid_fs_calls", "make_tree",
           "module_env", "project", "serial_worker_argv"]

# A stand-in worker: answers "ready", reports progress for each scan and keeps it running
# until it is cancelled (then "done" with aborted=True, like the real worker).
_FAKE_WORKER = r"""
import json, sys
def emit(ev):
    sys.stdout.write(json.dumps(ev) + "\n")
    sys.stdout.flush()
emit({"ev": "ready", "pid": 0})
jobs = {}
for line in sys.stdin:
    msg = json.loads(line)
    cmd = msg.get("cmd")
    if cmd == "scan":
        jobs[msg["job"]] = msg["source_id"]
        emit({"ev": "progress", "job": msg["job"], "source_id": msg["source_id"],
              "entries": 5, "dirs": 2, "units_done": 1, "units_total": 3})
    elif cmd == "cancel" and msg.get("job") in jobs:
        sid = jobs.pop(msg["job"])
        emit({"ev": "done", "job": msg["job"], "source_id": sid,
              "result": {"ok": False, "aborted": True, "error": None, "changed": 0,
                         "counts": None}})
    elif cmd == "quit":
        break
"""


def fake_worker_argv(script: str = _FAKE_WORKER) -> list[str]:
    return [sys.executable, "-c", script]


# The real scan worker, except that the volume serial of a path comes from the fake world's
# serials file (fake volumes are folders on the test drive; SPEC §15.5 expected_serial check).
_SERIAL_WORKER = r"""
import json, os, sys
from projektsog import scanworker
SERIALS = %r

def fake_serial(path):
    try:
        with open(SERIALS, encoding="utf-8") as fh:
            roots = json.load(fh)
    except (OSError, ValueError):
        roots = {}
    p = os.path.normcase(os.path.abspath(path)).rstrip("\\")
    best = None
    for root, serial in roots.items():
        r = os.path.normcase(os.path.abspath(root)).rstrip("\\")
        if (p == r or p.startswith(r + "\\")) and (best is None or len(r) > len(best[0])):
            best = (r, serial)
    return best[1] if best else None

scanworker.volume_serial = fake_serial
sys.exit(scanworker.main(sys.argv[1:]))
"""


def serial_worker_argv(serials_file: str) -> list[str]:
    """The real worker whose volume serials come from ``serials_file`` ({folder: serial})."""
    return [sys.executable, "-c", _SERIAL_WORKER % serials_file]


class FakeClock:
    """``time.time()`` plus an adjustable offset."""

    def __init__(self) -> None:
        self.offset = 0.0

    def __call__(self) -> float:
        return time.time() + self.offset

    def advance(self, seconds: float) -> None:
        self.offset += seconds


class FakeWorld:
    """Mutable machine and network state behind a :class:`indexer.DiscoveryEnv`."""

    def __init__(self, base: str, hostname: str = "TESTPC") -> None:
        self.base = base
        self.hostname = hostname
        self.volumes: list[dict[str, Any]] = []
        self.shares: list[dict[str, str]] = []
        self.mapped: dict[str, str] = {}
        self.remote: dict[str, list[str] | None] = {}      # HOST → share names (None = down)
        self.share_dirs: dict[tuple[str, str], str] = {}   # (HOST, share) → folder
        self.ips: dict[str, list[str]] = {}
        self.clock = FakeClock()
        self.remote_calls: list[str] = []
        self.probed: list[str] = []                        # paths, in probe order
        self.serials_file = os.path.join(base, "serials.json")
        self.serials: dict[str, str] = {}                  # volume folder → serial (worker)

    def env(self) -> indexer.DiscoveryEnv:
        return indexer.DiscoveryEnv(
            hostname=self.hostname,
            list_volumes=lambda: [dict(v) for v in self.volumes],
            local_shares=lambda: [dict(s) for s in self.shares],
            mapped_drives=lambda: dict(self.mapped),
            remote_shares=self._remote_shares,
            resolve_host_ips=lambda host: list(self.ips.get(host.upper(), [])),
            volume_info=lambda path: {"label": "", "serial": "8C5A3E61", "fs": "NTFS"},
            probe=self._probe,
            share_path=lambda host, share: self.share_dirs.get((host.upper(), share),
                                                               f"\\\\{host}\\{share}"),
            clock=self.clock)

    def _probe(self, path: str, cfg: Any, **kwargs: Any) -> tuple[bool, str, int]:
        self.probed.append(path)
        return discovery.probe(path, cfg, **kwargs)

    def probes_of(self, path: str) -> int:
        return sum(1 for p in list(self.probed) if os.path.normcase(p) == os.path.normcase(path))

    def volume(self, name: str, serial: str, *, label: str = "", drive: str = "X:",
               hotplug: bool = False, fs: str = "NTFS", tree: list[str] = ()) -> dict:
        """A new volume whose root is the folder ``base\\name`` (created with ``tree``)."""
        root = os.path.join(self.base, name)
        os.makedirs(root, exist_ok=True)
        make_tree(root, tree)
        vol = {"drive": drive, "root": root + "\\", "label": label, "serial": serial, "fs": fs,
               "drive_type": 2 if hotplug else 3, "is_system": False, "hotplug": hotplug,
               "size": 2_000_000_000_000}
        self.volumes.append(vol)
        self.set_serial(root, serial)
        return vol

    def set_serial(self, root: str, serial: str) -> None:
        """The serial the scan worker reads for paths in the folder ``root`` (a swapped
        medium: the same folder, another serial)."""
        self.serials[root] = serial
        tmp = self.serials_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.serials, fh)
        os.replace(tmp, self.serials_file)

    def share(self, host: str, share: str, tree: list[str] = ()) -> str:
        """A share of ``host`` whose content is the folder ``base\\host_share``."""
        folder = os.path.join(self.base, f"{host}_{share}")
        os.makedirs(folder, exist_ok=True)
        make_tree(folder, tree)
        self.share_dirs[(host.upper(), share)] = folder
        self.remote.setdefault(host.upper(), [])
        if self.remote[host.upper()] is not None:
            self.remote[host.upper()].append(share)
        return folder

    def _remote_shares(self, host: str) -> list[str] | None:
        self.remote_calls.append(host.upper())
        shares = self.remote.get(host.upper())
        return None if shares is None else list(shares)


PROJECT = ["Klip/", "Grafik/", "Speak/"]


def project(name: str, *files: str) -> list[str]:
    """Tree entries for a project folder ``name`` (template sub-folders + ``files``)."""
    return [f"{name}\\{d}" for d in PROJECT] + [f"{name}\\{f}" for f in files]


class EngineTestCase(unittest.TestCase):
    """A temporary folder, config, event bus and helpers to run an Indexer."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="engine-")
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.db_path = os.path.join(self.tmp, "index.db")
        self.world = FakeWorld(self.tmp)
        self.cfg = config.Config(path=os.path.join(self.tmp, "config.json"))
        self.cfg.update({"hosts": [], "discovery_interval_local_s": 3600,
                         "discovery_interval_network_s": 3600})
        self.bus = events.EventBus(maxsize=10_000)
        self.queue = self.bus.subscribe()
        self.events: list[tuple[str, Any, float]] = []
        for name, value in (("HOST_RETRY_DELAY_S", 0.01), ("WINDOW_SHOWN_DELAY_S", 0.1),
                            ("WORKER_BACKOFF_S", (0.05,))):
            patcher = mock.patch.object(indexer, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.sent: list[tuple[float, dict]] = []
        self.finished: list[tuple[float, int, str]] = []
        self._record_worker_io()

    def _record_worker_io(self) -> None:
        original_send = indexer._WorkerProcess.send
        original_finish = indexer.Indexer._finish_scan
        lock = threading.Lock()

        def send(worker: Any, message: dict) -> bool:
            with lock:
                self.sent.append((time.monotonic(), message))
            return original_send(worker, message)

        def finish(ix: Any, src: Any, job: Any, event: dict) -> None:
            with lock:
                self.finished.append((time.monotonic(), job.id, event.get("ev")))
            original_finish(ix, src, job, event)

        for target, name, new in ((indexer._WorkerProcess, "send", send),
                                  (indexer.Indexer, "_finish_scan", finish)):
            patcher = mock.patch.object(target, name, new)
            patcher.start()
            self.addCleanup(patcher.stop)

    # -- running --------------------------------------------------------------------------
    def start(self, **kwargs: Any) -> indexer.Indexer:
        kwargs.setdefault("worker_argv", serial_worker_argv(self.world.serials_file))
        ix = indexer.Indexer(self.cfg, self.bus, db_path=self.db_path, env=self.world.env(),
                             **kwargs)
        ix.start()
        self.addCleanup(ix.stop, 5)
        return ix

    def wait_until(self, predicate: Callable[[], Any], timeout: float = 15.0,
                   message: str = "condition") -> Any:
        deadline = time.monotonic() + timeout
        while True:
            value = predicate()
            if value:
                return value
            if time.monotonic() > deadline:
                self.fail(f"timed out waiting for {message}")
            time.sleep(0.01)

    def rediscover(self, ix: indexer.Indexer, *hosts: str) -> None:
        """Run at least one complete local (and host) pass that started after this call."""
        for _round in range(2):
            local = ix._local_passes
            before = {h: ix._hosts[h].passes for h in hosts}
            ix._kick_discovery()
            self.wait_until(lambda: ix._local_passes > local
                            and all(ix._hosts[h].passes > n for h, n in before.items()),
                            message="a discovery pass")

    def settled(self, ix: indexer.Indexer) -> dict:
        """Wait until discovery, probes and every scan are done; returns the status."""
        def idle() -> dict | None:
            status = ix.status()
            done = status["initial_scan_done"] and not status["scanning"] and not status["queued"]
            return status if done else None
        return self.wait_until(idle, message="initial scans")

    def source(self, ix: indexer.Indexer, name: str) -> dict:
        matches = [s for s in ix.list_sources() if s["display_name"] == name]
        self.assertEqual(len(matches), 1, f"sources named {name!r}: {ix.list_sources()}")
        return matches[0]

    # -- events and worker commands -----------------------------------------------------
    def drain(self) -> list[tuple[str, Any, float]]:
        """All events published so far (the bus queue is moved into ``self.events``)."""
        while True:
            try:
                self.events.append(self.queue.get_nowait())
            except queue.Empty:
                return self.events

    def events_of(self, kind: str) -> list[Any]:
        return [data for event, data, _ts in self.drain() if event == kind]

    def commands(self, cmd: str | None = None) -> list[dict]:
        return [m for _t, m in list(self.sent) if cmd is None or m.get("cmd") == cmd]

    def scans(self, source_id: int | None = None, kind: str | None = None) -> list[dict]:
        return [m for m in self.commands("scan")
                if (source_id is None or m["source_id"] == source_id)
                and (kind is None or m["kind"] == kind)]

    def db_rows(self, sql: str, *params: Any) -> list[tuple]:
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()


class forbid_fs_calls:
    """Fail when the calling thread touches the file system through ``os``."""

    _NAMES = (("os", "scandir"), ("os", "stat"), ("os", "lstat"), ("os", "listdir"),
              ("os.path", "isdir"), ("os.path", "exists"), ("os.path", "isfile"),
              ("os.path", "getsize"))

    def __enter__(self) -> forbid_fs_calls:
        caller = threading.current_thread()
        self._patchers = []
        for module_name, attr in self._NAMES:
            module = os if module_name == "os" else os.path
            original = getattr(module, attr)

            def guard(*args: Any, _original: Any = original, _name: str = attr,
                      **kwargs: Any) -> Any:
                if threading.current_thread() is caller:
                    raise AssertionError(f"file system access: {_name}{args!r}")
                return _original(*args, **kwargs)

            patcher = mock.patch.object(module, attr, guard)
            patcher.start()
            self._patchers.append(patcher)
        return self

    def __exit__(self, *exc: object) -> None:
        for patcher in reversed(self._patchers):
            patcher.stop()
