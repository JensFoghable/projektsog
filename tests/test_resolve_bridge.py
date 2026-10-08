"""Unit tests for projektsog.resolve_bridge (fake Resolve, helper, indexer, winui and clock).

Most tests run the helper's real request handling (projektsog.resolve_child.Session) in-process
behind the bridge's helper interface; ``HelperProcessTests`` start the real helper process
against a stand-in DaVinciResolveScript module (no Resolve needed).
"""

from __future__ import annotations

import ast
import os
import queue
import sys
import tempfile
import threading
import time
import types
import unittest
from typing import Any, Callable
from unittest import mock

from projektsog import resolve_bridge as rb
from projektsog.config import Config
from projektsog.events import EventBus
from tests._resolve_fakes import (
    DB, STUDIO_UNC, ChildFactory, DictOnlyClip, FakeClip, FakeClock, FakeFolder, FakeIndexer,
    FakeProject, FakeResolve, FakeWinui, UnstableIdProject, clips_in, folder_entry, indexed_file,
    offline_clip, source_ref, source_row, suggestion, update_fake_resolve, write_fake_resolve)

_tmp: tempfile.TemporaryDirectory | None = None


def setUpModule() -> None:
    global _tmp
    _tmp = tempfile.TemporaryDirectory()
    os.environ["LOCALAPPDATA"] = _tmp.name


def tearDownModule() -> None:
    if _tmp is not None:
        _tmp.cleanup()


STATE_KEYS = {"enabled", "running", "connected", "error", "project", "database", "clip_count",
              "updated", "folders", "other_dirs", "suggestions", "primary", "offline_clips",
              "offline_disks"}
WALL = 1_700_000_123.0
RIKKE = STUDIO_UNC + "\\Rikke Lindholm"
DISK_ROOT = "H:\\2024 Disk Sølv"
PIXELBRO = DISK_ROOT + "\\Pixelbro Radio"
DISK_HINT = "Tilslut disken ‘2024 Disk Sølv’"


def rikke_project(name: str = "Rikke Lindholm - Testimonial", uid: str | None = None) -> FakeProject:
    fx9 = FakeFolder("FX9", clips_in(RIKKE + "\\Klip\\FX9", "FX9_7912.MXF", "FX9_7913.MXF"))
    musik = FakeFolder("Musik", clips_in(RIKKE + "\\Musik", "song.wav"))
    root = FakeFolder("Master", [FakeClip(""), FakeClip("C:\\Github\\undertekster\\a.srt")],
                      [FakeFolder("Klip", [], [fx9]), musik])
    return FakeProject(name, root, uid)


def rikke_mapping(online: bool = True, *, count: int = 3) -> dict[str, Any]:
    src = source_ref(online=online, disk_name="2024 Disk Sølv" if not online else None)
    return {"folders": [folder_entry("Rikke Lindholm", RIKKE, count, src)],
            "other_dirs": [{"path": "C:\\Github\\undertekster", "count": 1, "online": True}],
            "total": count + 1}


def pixelbro_project() -> FakeProject:
    return FakeProject("Pixelbro Radio",
                       FakeFolder("Master", clips_in(PIXELBRO + "\\Klip", "a.mov", "b.mov")))


class DiskWorld:
    """The Indexer's live registry and mapping for the portable disk '2024 Disk Sølv'."""

    def __init__(self, online: bool, root: str = DISK_ROOT) -> None:
        self.online = online
        self.root = root
        self.scan_end = 1_700_000_000.0
        self.shallow_scan: float | None = None
        self.root_project = False
        self.volume_present: bool | None = None       # None: like online
        self.others: list[dict[str, Any]] = []

    def source(self) -> dict[str, Any]:
        return source_row(5, "2024 Disk Sølv", self.root, online=self.online,
                          disk_name="2024 Disk Sølv", last_scan_end=self.scan_end,
                          last_shallow_scan=self.shallow_scan,
                          root_is_project=self.root_project, volume_present=self.volume_present)

    def mapping(self, paths: list[str]) -> dict[str, Any]:
        ref = source_ref(5, "2024 Disk Sølv", online=self.online, drive=self.root[:2],
                         disk_name="2024 Disk Sølv", volume_present=self.volume_present)
        entry = folder_entry("Pixelbro Radio", self.root + "\\Pixelbro Radio", len(paths), ref)
        return {"folders": [entry], "other_dirs": [], "total": len(paths)}

    def indexer(self) -> FakeIndexer:
        return FakeIndexer(self.mapping, sources=lambda: [self.source(), *self.others])


class Harness:
    """A bridge wired to fakes; ``tick()`` runs one Resolve-thread iteration synchronously.

    The helper "process" is projektsog.resolve_child.Session run in-process (``children``)."""

    def __init__(self, test: unittest.TestCase, *, project: FakeProject | None = None,
                 indexer: FakeIndexer | None = None, follow: str = "notify",
                 connect: Callable[[], Any] | None = None,
                 settings: dict[str, Any] | None = None) -> None:
        folder = tempfile.mkdtemp(dir=_tmp.name)
        self.cfg = Config(path=os.path.join(folder, "config.json"))
        self.cfg.update({"resolve_follow": follow, "resolve_poll_s": 3, **(settings or {})})
        self.bus = EventBus()
        self.events = self.bus.subscribe()
        self.clock = FakeClock()
        self.winui = FakeWinui()
        self.resolve = FakeResolve(project if project is not None else rikke_project())
        self.indexer = indexer or FakeIndexer(rikke_mapping())
        self.connects = 0

        def default_connect() -> Any:
            self.connects += 1
            return self.resolve

        self.children = ChildFactory(connect or default_connect, self.clock)
        self.bridge = rb.ResolveBridge(
            self.cfg, self.bus, self.indexer,
            process_running=self.winui.process_running, process_uptime=self.winui.process_uptime,
            open_folder=self.winui.open_folder, explorer_window_for=self.winui.explorer_window_for,
            call_with_timeout=self.winui.call_with_timeout, spawn_child=self.children,
            clock=self.clock, wall_clock=lambda: WALL)
        test.addCleanup(self.bridge.stop)

    def tick(self, advance: float = 0.0) -> float:
        self.clock.advance(advance)
        return self.bridge._tick()

    @property
    def state(self) -> dict[str, Any]:
        return self.bridge.state()

    def drain(self, kind: str) -> list[Any]:
        out = []
        while True:
            try:
                etype, data, _ts = self.events.get_nowait()
            except queue.Empty:
                return out
            if etype == kind:
                out.append(data)


# --------------------------------------------------------------------------------------

class GatingTests(unittest.TestCase):
    def test_not_running_never_connects(self) -> None:
        h = Harness(self)
        h.winui.running = False
        h.tick()
        st = h.state
        self.assertEqual((st["running"], st["connected"], st["error"]), (False, False, None))
        self.assertEqual(h.connects, 0)
        self.assertEqual(h.children.children, [], "no helper while Resolve is not running")
        self.assertEqual(h.winui.exe_names[-1], "Resolve.exe")

    def test_waits_until_resolve_has_run_15_s(self) -> None:
        h = Harness(self)
        h.winui.uptime = 5.0
        delay = h.tick()
        self.assertEqual(h.connects, 0)
        self.assertEqual(h.children.children, [], "no helper during the start-up grace")
        self.assertEqual((h.state["running"], h.state["connected"]), (True, False))
        self.assertLessEqual(delay, 10.0)
        h.winui.uptime = 15.5
        h.tick(10.5)
        self.assertEqual(h.connects, 1)
        self.assertEqual(len(h.children.running), 1)
        self.assertTrue(h.state["connected"])

    def test_unknown_uptime_counts_from_first_sighting(self) -> None:
        h = Harness(self)
        h.winui.uptime = None
        h.tick()
        h.tick(14.0)
        self.assertEqual(h.connects, 0)
        h.tick(1.5)
        self.assertEqual(h.connects, 1)

    def test_scriptapp_none_asks_for_external_scripting_and_backs_off(self) -> None:
        calls = []
        h = Harness(self, connect=lambda: calls.append(1))
        h.tick()
        st = h.state
        self.assertEqual(st["error"], "Slå ekstern scripting til i DaVinci Resolve: Preferences ▸ "
                                      "System ▸ General ▸ External scripting using = Local")
        self.assertEqual((st["running"], st["connected"]), (True, False))
        h.tick(3.0)
        self.assertEqual(len(calls), 1, "retried before the back-off expired")
        self.assertEqual(h.state["error"], rb.ERR_EXTERNAL_SCRIPTING)
        h.tick(rb.CONNECT_RETRY_S)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(h.children.children), 1, "the helper is reused for the retry")

    def test_missing_scripting_module(self) -> None:
        def fail() -> Any:
            raise ImportError("Could not locate module dependencies")

        h = Harness(self, connect=fail)
        with self.assertLogs(level="WARNING") as logs:
            h.tick()
        self.assertEqual({r.name for r in logs.records},
                         {"projektsog.resolve_bridge", "projektsog.resolve_child"})
        self.assertEqual(h.state["error"], rb.ERR_MODULE)
        self.assertFalse(h.state["connected"])

    def test_disabled_does_nothing_but_report(self) -> None:
        h = Harness(self, settings={"resolve_enabled": False})
        h.tick()
        st = h.state
        self.assertEqual(set(st), STATE_KEYS)
        self.assertEqual((st["enabled"], st["running"], st["connected"]), (False, True, False))
        self.assertEqual(h.connects, 0)
        self.assertEqual(h.children.children, [])
        self.assertEqual(h.indexer.map_calls, [])

    def test_lost_connection_reconnects(self) -> None:
        h = Harness(self)
        h.tick()
        self.assertTrue(h.state["connected"])
        h.resolve.alive = False
        h.tick(3.0)
        self.assertEqual((h.state["connected"], h.state["error"]), (False, rb.ERR_NO_RESPONSE))
        self.assertIsNone(h.state["project"])
        h.resolve.alive = True
        h.tick(3.0)
        self.assertEqual(h.connects, 2)
        self.assertEqual(h.state["project"], "Rikke Lindholm - Testimonial")

    def test_proxy_exception_counts_as_not_answering(self) -> None:
        h = Harness(self)
        h.tick()
        h.resolve.raise_on_pm = RuntimeError("broken pipe")
        with self.assertLogs("projektsog.resolve_child", "WARNING"):
            h.tick(3.0)
        self.assertEqual(h.state["error"], rb.ERR_NO_RESPONSE)

    def test_resolve_quits(self) -> None:
        h = Harness(self)
        h.tick()
        h.winui.running = False
        h.tick(3.0)
        st = h.state
        self.assertEqual((st["running"], st["connected"], st["project"]), (False, False, None))
        self.assertEqual(st["folders"], [])
        self.assertEqual(h.children.running, [], "the helper stops with Resolve")

    def test_winui_failure_counts_as_not_running(self) -> None:
        h = Harness(self)

        def broken(exe: str) -> bool:
            raise OSError("snapshot failed")

        h.bridge._funcs["process_running"] = broken
        with self.assertLogs("projektsog.resolve_bridge", "WARNING"):
            h.tick()
        self.assertFalse(h.state["running"])


class HelperLifecycleTests(unittest.TestCase):
    """RES-3: the scripting helper runs only while Resolve runs and is replaced when it fails."""

    def test_helper_runs_only_while_resolve_runs(self) -> None:
        h = Harness(self)
        h.tick()
        first, = h.children.children
        self.assertTrue(first.alive())
        h.winui.running = False
        h.tick(3.0)
        self.assertTrue(first.closed)
        h.winui.running, h.winui.uptime = True, 2.0     # Resolve started again
        h.tick(3.0)
        self.assertEqual(h.children.running, [], "not during the start-up grace")
        h.winui.uptime = 20.0
        h.tick(13.0)
        self.assertEqual(len(h.children.running), 1)
        self.assertIsNot(h.children.running[0], first)
        self.assertEqual(h.state["project"], "Rikke Lindholm - Testimonial")

    def test_disabling_the_integration_stops_the_helper(self) -> None:
        h = Harness(self)
        h.tick()
        h.cfg.update({"resolve_enabled": False})
        h.tick(3.0)
        self.assertEqual(h.children.running, [])
        self.assertFalse(h.state["enabled"])

    def test_stop_closes_the_helper(self) -> None:
        h = Harness(self)
        h.tick()
        child, = h.children.children
        h.bridge.stop()
        self.assertTrue(child.closed)

    def test_crashed_helper_is_restarted_after_a_back_off(self) -> None:
        h = Harness(self, follow="off")
        h.tick()
        first, = h.children.children
        first.crash()
        with self.assertLogs("projektsog.resolve_bridge", "WARNING"):
            delay = h.tick(3.0)
        st = h.state
        self.assertEqual((st["connected"], st["error"], st["project"]),
                         (False, rb.ERR_NO_RESPONSE, None))
        self.assertLessEqual(delay, rb.CHILD_BACKOFF_S[0])
        h.tick(0.5)
        self.assertEqual(len(h.children.children), 1, "restarted before the back-off expired")
        h.tick(0.6)
        self.assertEqual(len(h.children.children), 2)
        self.assertEqual((h.state["connected"], h.state["project"]),
                         (True, "Rikke Lindholm - Testimonial"))
        self.assertEqual(h.state["clip_count"], 4)

    def test_hung_helper_is_replaced(self) -> None:
        h = Harness(self, follow="off")
        h.children.fail["walk"] = "timeout"
        with self.assertLogs("projektsog.resolve_bridge", "WARNING"):
            h.tick()
        first, = h.children.children
        self.assertTrue(first.closed, "a hung helper is stopped (killed)")
        self.assertEqual(h.state["error"], rb.ERR_NO_RESPONSE)
        h.tick(rb.CHILD_BACKOFF_S[0])
        self.assertEqual(len(h.children.running), 1)
        self.assertEqual(h.state["clip_count"], 4)

    def test_back_off_grows_while_the_helper_cannot_start(self) -> None:
        h = Harness(self)
        h.children.spawn_error = OSError("pythonw.exe is missing")
        with self.assertLogs("projektsog.resolve_bridge", "ERROR"):
            delays = [h.tick(), h.tick(rb.CHILD_BACKOFF_S[0]), h.tick(rb.CHILD_BACKOFF_S[1])]
        self.assertEqual(h.state["error"], rb.ERR_HELPER)
        self.assertEqual(delays, [min(3.0, d) for d in rb.CHILD_BACKOFF_S[:3]])
        self.assertEqual(h.connects, 0)
        h.children.spawn_error = None
        h.tick(rb.CHILD_BACKOFF_S[2])
        self.assertTrue(h.state["connected"])

    def test_helper_that_does_not_start_is_replaced(self) -> None:
        h = Harness(self)
        h.children.fail["ready"] = "timeout"
        with self.assertLogs("projektsog.resolve_bridge", "ERROR"):
            h.tick()
        self.assertTrue(h.children.children[0].closed)
        self.assertEqual(h.state["error"], rb.ERR_HELPER)
        h.tick(rb.CHILD_BACKOFF_S[0])
        self.assertTrue(h.state["connected"])

    def test_requests_and_their_time_limits(self) -> None:
        h = Harness(self)
        h.tick()
        child, = h.children.children
        self.assertEqual([m["cmd"] for m in child.requests], ["connect", "poll", "walk", "poll"])
        walk = child.requests[2]
        self.assertEqual((walk["max_clips"], walk["max_seconds"]),
                         (rb.WALK_MAX_CLIPS, rb.WALK_MAX_SECONDS))
        self.assertEqual(h.children.timeouts, [
            ("connect", rb.CHILD_CONNECT_TIMEOUT_S), ("poll", rb.CHILD_CALL_TIMEOUT_S),
            ("walk", rb.WALK_MAX_SECONDS + rb.CHILD_WALK_MARGIN_S),
            ("poll", rb.CHILD_CALL_TIMEOUT_S)])


class ChangeDetectionTests(unittest.TestCase):
    def test_walks_on_change_only(self) -> None:
        h = Harness(self)
        h.tick()
        h.tick(3.0)
        h.tick(3.0)
        self.assertEqual(len(h.indexer.map_calls), 1)
        h.resolve.pm.project = rikke_project("Andet projekt")
        h.tick(3.0)
        self.assertEqual(len(h.indexer.map_calls), 2)
        self.assertEqual(h.state["project"], "Andet projekt")

    def test_database_change_with_same_project_name(self) -> None:
        h = Harness(self)
        h.tick()
        h.resolve.pm.db = {"DbType": "Disk", "DbName": "Local Database"}
        h.tick(3.0)
        self.assertEqual(len(h.indexer.map_calls), 2)
        self.assertEqual(h.state["database"], "Local Database")

    def test_same_name_other_project_id(self) -> None:
        h = Harness(self, project=rikke_project(uid="a"))
        h.tick()
        h.resolve.pm.project = rikke_project(uid="b")
        h.tick(3.0)
        self.assertEqual(len(h.indexer.map_calls), 2)

    def test_unstable_project_id_is_ignored(self) -> None:
        h = Harness(self, project=UnstableIdProject("Rikke Lindholm - Testimonial",
                                                    rikke_project().pool.root))
        with self.assertLogs("projektsog.resolve_bridge", "WARNING"):
            for _ in range(4):
                h.tick(3.0)
        self.assertEqual(len(h.indexer.map_calls), 1)
        self.assertEqual(h.state["clip_count"], 4)

    def test_project_switch_during_walk_discards_the_old_walk(self) -> None:
        h = Harness(self)
        other = rikke_project("Andet projekt")
        h.resolve.pm.project.pool.root.on_list = lambda: setattr(h.resolve.pm, "project", other)
        delay = h.tick()
        self.assertEqual(delay, 0.0)
        self.assertEqual(h.indexer.map_calls, [])
        published = [s["project"] for s in h.drain("resolve") if s["updated"] is not None]
        self.assertEqual(published, [])
        h.tick()
        self.assertEqual(h.state["project"], "Andet projekt")
        self.assertEqual(len(h.indexer.map_calls), 1)

    def test_no_open_project(self) -> None:
        h = Harness(self)
        h.resolve.pm.project = None
        h.tick()
        st = h.state
        self.assertEqual((st["connected"], st["project"], st["database"]),
                         (True, None, "Kunder 2026 (Projektserver)"))
        self.assertEqual(h.indexer.map_calls, [])


class WalkTests(unittest.TestCase):
    def test_collects_file_paths_recursively(self) -> None:
        h = Harness(self)
        h.tick()
        self.assertEqual(h.indexer.map_calls, [[
            "C:\\Github\\undertekster\\a.srt",
            RIKKE + "\\Klip\\FX9\\FX9_7912.MXF", RIKKE + "\\Klip\\FX9\\FX9_7913.MXF",
            RIKKE + "\\Musik\\song.wav"]])
        self.assertEqual(h.state["clip_count"], 4)
        self.assertEqual(h.state["updated"], WALL)

    def test_all_properties_api_and_old_dict_lists(self) -> None:
        root = FakeFolder("Master", [DictOnlyClip("D:\\x\\a.mov"), FakeClip("D:\\x\\b.mov")],
                          as_dict=True)
        h = Harness(self, project=FakeProject("P", root))
        h.tick()
        self.assertEqual(h.indexer.map_calls, [["D:\\x\\a.mov", "D:\\x\\b.mov"]])

    def test_clip_limit(self) -> None:
        root = FakeFolder("Master", clips_in("D:\\x", *[f"{i}.mov" for i in range(12)]))
        h = Harness(self, project=FakeProject("P", root))
        with mock.patch.object(rb, "WALK_MAX_CLIPS", 5):
            h.tick()
        self.assertEqual(len(h.indexer.map_calls[0]), 5)
        self.assertEqual(h.state["clip_count"], 5)

    def test_time_limit(self) -> None:
        h = Harness(self)
        folders = [FakeFolder(f"F{i}", clips_in(f"D:\\f{i}", "a.mov"),
                              on_list=lambda: h.clock.advance(3.0)) for i in range(20)]
        h.resolve.pm.project = FakeProject("P", FakeFolder("Master", [], folders))
        h.tick()
        walked = sum(1 for f in folders if f.listed)
        self.assertLess(walked, 20)
        self.assertGreaterEqual(walked, 6)
        self.assertEqual(h.state["clip_count"], len(h.indexer.map_calls[0]))


class PrimaryTests(unittest.TestCase):
    def test_media_primary_most_clips_then_online(self) -> None:
        a = folder_entry("A", "D:\\A", 5, source_ref(2, online=False, disk_name="Disk A"), iid=1)
        b = folder_entry("B", "C:\\B", 5, source_ref(3), iid=2)
        c = folder_entry("C", "C:\\C", 2, source_ref(4), iid=3)
        h = Harness(self, indexer=FakeIndexer({"folders": [c, a, b], "other_dirs": []}))
        h.tick()
        st = h.state
        self.assertEqual([f["project"]["name"] for f in st["folders"]], ["B", "A", "C"])
        self.assertEqual((st["primary"]["name"], st["primary"]["match"]), ("B", "media"))
        self.assertEqual(st["primary"]["id"], 2)
        self.assertEqual(h.indexer.suggest_calls, [])

    def test_folder_without_item(self) -> None:
        src = source_ref()
        entry = folder_entry("Rikke Lindholm", RIKKE, 3, src, with_item=False)
        h = Harness(self, indexer=FakeIndexer({"folders": [entry], "other_dirs": []}))
        h.tick()
        p = h.state["primary"]
        self.assertEqual((p["path"], p["open_path"], p["kind"], p["match"]),
                         (RIKKE, RIKKE, "project", "media"))
        self.assertIs(p["source"], src)

    def test_name_suggestion_when_no_media_match(self) -> None:
        low = suggestion("Rikke", "C:\\Rikke", 0.55, iid=7)
        high = suggestion("Rikke Lindholm", RIKKE, 0.8, iid=8)
        h = Harness(self, indexer=FakeIndexer(suggestions=[low, high]))
        h.tick()
        st = h.state
        self.assertEqual(h.indexer.suggest_calls, ["Rikke Lindholm - Testimonial"])
        self.assertEqual([s["score"] for s in st["suggestions"]], [0.8, 0.55])
        self.assertEqual((st["primary"]["name"], st["primary"]["match"]), ("Rikke Lindholm", "name"))

    def test_weak_suggestion_is_listed_but_not_primary(self) -> None:
        h = Harness(self, indexer=FakeIndexer(suggestions=[suggestion("Rikke", "C:\\L", 0.59)]))
        h.tick()
        self.assertIsNone(h.state["primary"])
        self.assertEqual(len(h.state["suggestions"]), 1)

    def test_untitled_project_gets_no_name_suggestions(self) -> None:
        h = Harness(self, project=FakeProject("Untitled Project 2"),
                    indexer=FakeIndexer(suggestions=[suggestion("X", "C:\\X", 0.9)]))
        h.tick()
        self.assertEqual(h.indexer.suggest_calls, [])
        self.assertIsNone(h.state["primary"])

    def test_other_dirs_are_capped_largest_first(self) -> None:
        dirs = [{"path": f"D:\\d{i}", "count": i, "online": True} for i in range(250)]
        h = Harness(self, indexer=FakeIndexer({"folders": [], "other_dirs": dirs}))
        h.tick()
        got = h.state["other_dirs"]
        self.assertEqual(len(got), rb.MAX_OTHER_DIRS)
        self.assertEqual(got[0]["count"], 249)

    def test_indexer_failure_leaves_an_empty_mapping(self) -> None:
        def boom(paths: list[str]) -> dict[str, Any]:
            raise RuntimeError("db locked")

        for mapping in (boom, lambda paths: ["not", "a", "mapping"]):
            with self.subTest(mapping=mapping):
                h = Harness(self, indexer=FakeIndexer(mapping))
                with self.assertLogs("projektsog.resolve_bridge", "ERROR"):
                    h.tick()
                st = h.state
                self.assertEqual((st["connected"], st["clip_count"], st["folders"]),
                                 (True, 4, []))


class StateShapeTests(unittest.TestCase):
    def test_idle_shape(self) -> None:
        h = Harness(self)
        h.winui.running = False
        h.tick()
        self.assertEqual(h.state, {
            "enabled": True, "running": False, "connected": False, "error": None,
            "project": None, "database": None, "clip_count": 0, "updated": None,
            "folders": [], "other_dirs": [], "suggestions": [], "primary": None,
            "offline_clips": 0, "offline_disks": []})

    def test_connected_shape(self) -> None:
        h = Harness(self)
        h.tick()
        st = h.state
        self.assertEqual(set(st), STATE_KEYS)
        self.assertEqual((st["enabled"], st["running"], st["connected"], st["error"]),
                         (True, True, True, None))
        self.assertEqual((st["project"], st["database"]),
                         ("Rikke Lindholm - Testimonial", DB["DbName"]))
        self.assertEqual(st["folders"], rikke_mapping()["folders"])
        self.assertEqual(st["other_dirs"], rikke_mapping()["other_dirs"])
        self.assertEqual((st["offline_clips"], st["offline_disks"]), (0, []))

    def test_offline_summary(self) -> None:
        disk = folder_entry("A", "H:\\A", 12, source_ref(2, online=False, drive="H:",
                                                         disk_name="2024 Disk Sølv"))
        share = folder_entry("B", "\\\\MEDIESERVER\\2025Arkiv\\B", 3,
                             source_ref(3, "2025Arkiv", host="MEDIESERVER",
                                        kind="share", online=False, drive=None))
        fine = folder_entry("C", "C:\\C", 100, source_ref(4))
        other = [{"path": "E:\\x", "count": 2, "online": False},
                 {"path": "Q:\\y", "count": 5, "online": None}]
        h = Harness(self, indexer=FakeIndexer({"folders": [disk, share, fine],
                                               "other_dirs": other}))
        h.tick()
        st = h.state
        self.assertEqual((st["offline_clips"], st["offline_disks"]), (17, ["2024 Disk Sølv"]))

    def test_state_returns_a_copy(self) -> None:
        h = Harness(self)
        h.tick()
        h.state["project"] = "changed"
        self.assertEqual(h.state["project"], "Rikke Lindholm - Testimonial")

    def test_resolve_events_only_on_change(self) -> None:
        h = Harness(self)
        h.tick()
        first = h.drain("resolve")
        self.assertEqual([s["updated"] for s in first], [None, WALL])  # project, then mapping
        h.tick(3.0)
        h.tick(3.0)
        self.assertEqual(h.drain("resolve"), [])
        h.winui.running = False
        h.tick(3.0)
        self.assertEqual([s["running"] for s in h.drain("resolve")], [False])


class RemapTests(unittest.TestCase):
    """XMC-2 / RES-1: the published mapping follows the live registry without media pool walks."""

    def harness(self, world: DiskWorld, follow: str = "off") -> Harness:
        h = Harness(self, project=pixelbro_project(), indexer=world.indexer(), follow=follow)
        h.tick()
        return h

    def walks(self, h: Harness) -> int:
        return h.resolve.pm.project.pool.root.listed

    def test_disk_plugged_in_after_the_walk(self) -> None:
        world = DiskWorld(online=False)
        h = self.harness(world)
        st = h.state
        self.assertFalse(st["primary"]["source"]["online"])
        self.assertEqual((st["offline_clips"], st["offline_disks"]), (2, ["2024 Disk Sølv"]))
        h.drain("resolve")
        world.online = True
        h.tick(3.0)                       # noticed, but the last mapping is only 3 s old
        self.assertFalse(h.state["primary"]["source"]["online"])
        h.tick(2.0)
        st = h.state
        self.assertTrue(st["primary"]["source"]["online"])
        self.assertEqual((st["offline_clips"], st["offline_disks"]), (0, []))
        self.assertEqual((st["updated"], st["clip_count"]), (WALL, 2))
        self.assertEqual(self.walks(h), 1, "no media pool walk")
        self.assertEqual(len(h.indexer.map_calls), 2)
        published = h.drain("resolve")
        self.assertEqual([s["primary"]["source"]["online"] for s in published], [True])

    def test_disk_unplugged_after_the_walk(self) -> None:
        world = DiskWorld(online=True)
        h = self.harness(world)
        world.online = False
        h.tick(5.0)
        st = h.state
        self.assertFalse(st["primary"]["source"]["online"])
        self.assertEqual(st["offline_disks"], ["2024 Disk Sølv"])
        self.assertEqual(self.walks(h), 1)

    def test_disk_back_under_another_drive_letter(self) -> None:
        world = DiskWorld(online=False)
        h = self.harness(world)
        world.online, world.root = True, "I:\\2024 Disk Sølv"
        h.tick(5.0)
        self.assertEqual(h.state["primary"]["path"], "I:\\2024 Disk Sølv\\Pixelbro Radio")

    def test_remaps_at_most_every_5_s(self) -> None:
        world = DiskWorld(online=True)
        h = self.harness(world)                                    # mapped at t=0
        world.online = False
        h.tick(5.0)                                                # t=5: re-mapped
        self.assertEqual(len(h.indexer.map_calls), 2)
        world.online = True
        self.assertEqual(h.tick(1.0), 3.0)                         # t=6: wait (poll is 3 s)
        self.assertEqual(h.tick(3.0), 1.0)                         # t=9: 1 s to go
        self.assertEqual(len(h.indexer.map_calls), 2)
        h.tick(1.0)                                                # t=10
        self.assertEqual(len(h.indexer.map_calls), 3)
        self.assertTrue(h.state["primary"]["source"]["online"])

    def test_no_remap_without_a_relevant_change(self) -> None:
        world = DiskWorld(online=True)
        world.others = [source_row(9, "Andet", "C:\\Andet", last_scan_end=1.0)]
        h = self.harness(world)
        for _ in range(5):
            h.tick(3.0)
        self.assertEqual(len(h.indexer.map_calls), 1)
        world.others = [source_row(9, "Andet", "C:\\Andet", last_scan_end=2.0)]
        h.tick(3.0)                            # another location finished a scan: irrelevant
        self.assertEqual(len(h.indexer.map_calls), 1)
        world.scan_end += 60                   # the disk the project lives on was rescanned
        h.tick(3.0)
        self.assertEqual(len(h.indexer.map_calls), 2)
        world.others = [source_row(9, "Andet", "C:\\Andet", online=False, last_scan_end=2.0)]
        h.tick(5.0)                            # any location going offline may matter
        self.assertEqual(len(h.indexer.map_calls), 3)
        self.assertEqual(self.walks(h), 1)

    def test_newly_indexed_disk_maps_the_clips(self) -> None:
        clips = "E:\\Kunde X\\Klip"
        registry: list[dict[str, Any]] = []

        def mapping(paths: list[str]) -> dict[str, Any]:
            src = registry[0] if registry else None
            if src is None:
                return {"folders": [], "total": 2,
                        "other_dirs": [{"path": clips, "count": 2, "online": None}]}
            if src["last_scan_end"] is None:   # known, not indexed yet
                return {"folders": [], "total": 2,
                        "other_dirs": [{"path": clips, "count": 2, "online": True}]}
            ref = source_ref(7, "Ny disk", drive="E:", disk_name="Ny disk")
            return {"folders": [folder_entry("Kunde X", "E:\\Kunde X", 2, ref)],
                    "other_dirs": [], "total": 2}

        project = FakeProject("Kunde X", FakeFolder("Master", clips_in(clips, "a.mp4", "b.mp4")))
        h = Harness(self, project=project, follow="off",
                    indexer=FakeIndexer(mapping, sources=lambda: registry))
        h.tick()
        self.assertIsNone(h.state["primary"])
        registry.append(source_row(7, "Ny disk", "E:\\", disk_name="Ny disk",
                                   last_scan_end=None))
        h.tick(5.0)                           # the disk appeared: its paths are in a location
        self.assertIsNone(h.state["primary"])
        self.assertEqual(h.state["other_dirs"][0]["online"], True)
        registry[0] = dict(registry[0], last_scan_end=1_700_000_500.0)
        h.tick(5.0)                           # ... whose first scan just finished
        self.assertEqual((h.state["primary"]["name"], h.state["primary"]["match"]),
                         ("Kunde X", "media"))
        self.assertEqual(project.pool.root.listed, 1)

    def test_shallow_scan_alone_remaps(self) -> None:
        """RES2-1: the shallow scan after the window is shown (or of a new disk) indexes a new
        project folder; of the location's Source only ``last_shallow_scan`` changes."""
        disk = "F:\\Kunder 2026"
        old, new = disk + "\\Gammel Kunde - Film", disk + "\\Ny Kunde - Reklame"
        ref = source_ref(5, "Kunder 2026", drive="F:")
        world: dict[str, Any] = {"indexed": False, "shallow": None}

        def mapping(paths: list[str]) -> dict[str, Any]:
            n_new = sum(p.startswith(new + "\\") for p in paths)
            old_entry = folder_entry("Gammel Kunde - Film", old, len(paths) - n_new, ref, iid=1)
            if world["indexed"]:
                return {"folders": [folder_entry("Ny Kunde - Reklame", new, n_new, ref, iid=2),
                                    old_entry], "other_dirs": [], "total": len(paths)}
            return {"folders": [old_entry], "total": len(paths),
                    "other_dirs": [{"path": new + "\\Klip", "count": n_new, "online": True}]}

        project = FakeProject("Ny Kunde - Reklame", FakeFolder("Master", [
            FakeClip(old + "\\Musik\\signatur.wav"),
            *clips_in(new + "\\Klip", "C1.MXF", "C2.MXF")]))
        indexer = FakeIndexer(mapping, sources=lambda: [
            source_row(5, "Kunder 2026", disk, last_shallow_scan=world["shallow"])])
        h = Harness(self, project=project, follow="off", indexer=indexer)
        h.tick()
        self.assertEqual((h.state["primary"]["name"], h.state["primary"]["match"]),
                         ("Gammel Kunde - Film", "media"))
        world["indexed"] = True               # the shallow scan has applied the new folder ...
        h.tick(5.0)
        self.assertEqual(len(indexer.map_calls), 1, "nothing tells the bridge yet")
        world["shallow"] = 1_700_000_600.0    # ... and finishes: its Source says so
        h.tick(3.0)
        self.assertEqual((h.state["primary"]["name"], h.state["primary"]["match"]),
                         ("Ny Kunde - Reklame", "media"))
        self.assertEqual(len(indexer.map_calls), 2)
        self.assertEqual(project.pool.root.listed, 1, "no media pool walk")

    def test_root_becoming_a_project_remaps(self) -> None:
        world = DiskWorld(online=True)
        h = self.harness(world)
        world.root_project = True              # re-derived after a scan: clips map to the root
        h.tick(5.0)
        self.assertEqual(len(h.indexer.map_calls), 2)
        self.assertEqual(self.walks(h), 1)

    def test_scan_of_a_location_left_out_of_the_published_other_dirs(self) -> None:
        """The state lists only the MAX_OTHER_DIRS largest other folders; a scan of the
        location of a smaller one still counts."""
        arkiv = source_ref(3, "Arkiv", drive="C:")
        many = [{"path": f"C:\\Arkiv\\d{i}", "count": 2, "online": True}
                for i in range(rb.MAX_OTHER_DIRS)]
        small = {"path": "E:\\Ny disk\\Ny Kunde\\Klip", "count": 1, "online": True}
        registry = [source_row(3, "Arkiv", "C:\\Arkiv"), source_row(7, "Ny disk", "E:\\Ny disk")]
        indexer = FakeIndexer({"folders": [folder_entry("P", "C:\\Arkiv\\P", 3, arkiv)],
                               "other_dirs": [small, *many], "total": 404},
                              sources=lambda: registry)
        h = Harness(self, project=pixelbro_project(), follow="off", indexer=indexer)
        h.tick()
        self.assertNotIn(small, h.state["other_dirs"])
        registry[1] = dict(registry[1], last_shallow_scan=1_700_000_600.0)
        h.tick(5.0)
        self.assertEqual(len(indexer.map_calls), 2)

    def test_name_suggestions_follow_scans_of_every_location(self) -> None:
        """Without a media match the mapping falls back on the project name, and a matching
        project folder can appear on any location: every finished scan counts."""
        registry = [source_row(3, "Kunder 2026", "C:\\Kunder 2026"),
                    source_row(4, "Arkiv", "D:\\Arkiv")]
        indexer = FakeIndexer(sources=lambda: registry)
        h = Harness(self, project=FakeProject("Ny Kunde - Reklame"), follow="off",
                    indexer=indexer)                                   # new and still empty
        h.tick()
        h.tick(5.0)
        self.assertEqual((h.state["primary"], indexer.suggest_calls),
                         (None, ["Ny Kunde - Reklame"]))
        indexer.suggestions = [suggestion("Ny Kunde - Reklame", "D:\\Arkiv\\Ny Kunde - Reklame",
                                          0.95, source_ref(4, "Arkiv", drive="D:"))]
        registry[1] = dict(registry[1], last_shallow_scan=1_700_000_600.0)
        h.tick(3.0)
        self.assertEqual((h.state["primary"]["name"], h.state["primary"]["match"]),
                         ("Ny Kunde - Reklame", "name"))
        self.assertEqual(len(indexer.suggest_calls), 2)

    def test_remap_does_not_repeat_the_follow_notification(self) -> None:
        world = DiskWorld(online=False)
        h = self.harness(world, follow="notify")
        h.tick(6.0)
        self.assertEqual(len(h.drain("notify")), 1)
        world.online = True
        h.tick(5.0)
        self.assertTrue(h.state["primary"]["source"]["online"])
        h.tick(6.0)
        self.assertEqual(h.drain("notify"), [])

    def test_real_thread_follows_the_registry(self) -> None:
        world = DiskWorld(online=False)
        h = Harness(self, project=pixelbro_project(), indexer=world.indexer(), follow="off",
                    settings={"resolve_poll_s": 1})
        h.bridge._clock = time.monotonic
        with mock.patch.object(rb, "REMAP_MIN_INTERVAL_S", 0.3):
            h.bridge.start()
            deadline = time.monotonic() + 5
            while h.state["updated"] is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(h.state["primary"]["source"]["online"])
            world.online = True
            deadline = time.monotonic() + 5
            while (not h.state["primary"]["source"]["online"]
                   and time.monotonic() < deadline):
                time.sleep(0.02)
        self.assertTrue(h.state["primary"]["source"]["online"])
        self.assertEqual(self.walks(h), 1)


class FollowTests(unittest.TestCase):
    def notes(self, h: Harness) -> list[dict[str, Any]]:
        return h.drain("notify")

    def test_notifies_once_stable_for_5_s(self) -> None:
        h = Harness(self)
        delay = h.tick()
        self.assertLessEqual(delay, rb.FOLLOW_STABLE_S)
        self.assertEqual(self.notes(h), [])
        h.tick(4.9)
        self.assertEqual(self.notes(h), [])
        h.tick(0.2)
        self.assertEqual(self.notes(h), [{"title": "DaVinci Resolve: Rikke Lindholm - Testimonial",
                                          "text": "Projektmappe: Rikke Lindholm", "level": "info"}])
        h.tick(3.0)
        self.assertEqual(self.notes(h), [])

    def test_once_per_project_per_30_min(self) -> None:
        a, b = rikke_project(), rikke_project("B-projekt")
        h = Harness(self, project=a)
        h.tick()
        h.tick(6.0)
        h.resolve.pm.project = b
        h.tick(60.0)
        h.tick(6.0)
        h.resolve.pm.project = a
        h.tick(60.0)
        h.tick(6.0)
        titles = [n["title"] for n in self.notes(h)]
        self.assertEqual(titles, ["DaVinci Resolve: Rikke Lindholm - Testimonial",
                                  "DaVinci Resolve: B-projekt"])
        h.resolve.pm.project = b
        h.tick(rb.FOLLOW_REPEAT_S)
        h.resolve.pm.project = a
        h.tick(3.0)
        h.tick(6.0)
        self.assertEqual([n["title"] for n in self.notes(h)],
                         ["DaVinci Resolve: Rikke Lindholm - Testimonial"])

    def test_quick_switch_notifies_only_the_settled_project(self) -> None:
        h = Harness(self)
        h.tick()
        h.resolve.pm.project = rikke_project("B-projekt")
        h.tick(3.0)
        h.tick(3.0)
        self.assertEqual(self.notes(h), [])
        h.tick(3.0)
        self.assertEqual([n["title"] for n in self.notes(h)], ["DaVinci Resolve: B-projekt"])

    def test_no_follow_for_untitled_empty_or_off(self) -> None:
        cases = [dict(project=rikke_project("Untitled Project")),
                 dict(project=FakeProject("Tomt projekt")),
                 dict(follow="off")]
        for kwargs in cases:
            with self.subTest(**{k: getattr(v, "name", v) for k, v in kwargs.items()}):
                h = Harness(self, **kwargs)
                h.tick()
                h.tick(10.0)
                self.assertEqual(self.notes(h), [])
                self.assertEqual(h.winui.opened, [])

    def test_offline_primary_gets_the_disk_hint_and_is_not_opened(self) -> None:
        h = Harness(self, follow="open", indexer=FakeIndexer(rikke_mapping(online=False, count=12)))
        h.tick()
        h.tick(6.0)
        self.assertEqual(self.notes(h), [{
            "title": "DaVinci Resolve: Rikke Lindholm - Testimonial",
            "text": "Projektmappe: Rikke Lindholm\n12 klip ligger på disken ‘2024 Disk Sølv’, "
                    "som ikke er tilsluttet",
            "level": "warn"}])
        self.assertEqual(h.winui.opened, [])

    def test_offline_share_hint(self) -> None:
        share = folder_entry("B", "\\\\GRAFIK-PC\\Forår 2026 (HDD)\\B", 1234,
                             source_ref(3, "Forår 2026 (HDD)", host="GRAFIK-PC", kind="share",
                                        online=False, drive=None))
        h = Harness(self, indexer=FakeIndexer({"folders": [share], "other_dirs": []}))
        h.tick()
        h.tick(6.0)
        self.assertEqual(self.notes(h)[0]["text"],
                         "Projektmappe: B\n1.234 klip ligger på GRAFIK-PC, som ikke svarer")

    def test_folder_gone_from_the_connected_disk(self) -> None:
        """R3-RES-1 (SPEC §15.12): the folder vanished while its disk stays plugged in - the
        notification says so, like the app's Resolve bar and open_primary(), and names no disk
        to connect."""
        world = DiskWorld(online=True)
        h = Harness(self, project=pixelbro_project(), indexer=world.indexer())
        h.tick()
        world.online, world.volume_present = False, True   # renamed before the project settled
        h.tick(6.0)                                        # re-mapped, then followed
        self.assertEqual(self.notes(h), [{
            "title": "DaVinci Resolve: Pixelbro Radio",
            "text": "Projektmappe: Pixelbro Radio\n2 klip ligger i mappen ‘2024 Disk Sølv’, "
                    "som ikke findes længere",
            "level": "warn"}])
        self.assertEqual((h.state["offline_clips"], h.state["offline_disks"]), (2, []))
        self.assertEqual(h.bridge.open_primary()["error"], rb.ERR_GONE)
        world.volume_present = False                       # now the disk is unplugged as well
        h.tick(5.0)
        self.assertEqual((h.state["offline_clips"], h.state["offline_disks"]),
                         (2, ["2024 Disk Sølv"]))

    def test_offline_hint_wording(self) -> None:
        def share(host: str, count: int) -> dict[str, Any]:
            return folder_entry(host, f"\\\\{host}\\s\\p", count,
                                source_ref(name="s", host=host, kind="share", online=False))

        def state(clips: int, disks: list[str], folders: list[dict[str, Any]]) -> dict[str, Any]:
            return {"offline_clips": clips, "offline_disks": disks, "folders": folders}

        self.assertIsNone(rb._offline_hint(state(0, [], [])))
        self.assertEqual(rb._offline_hint(state(5, ["A", "B"], [])),
                         "5 klip ligger på diskene ‘A’ og ‘B’, som ikke er tilsluttet")
        hosts = [share("H1", 3), share("H2", 2), share("H3", 1)]
        self.assertEqual(rb._offline_hint(state(6, [], hosts)),
                         "6 klip ligger på H1, H2 og H3, som ikke svarer")
        self.assertEqual(rb._offline_hint(state(7, ["A"], hosts[:1])),
                         "7 klip ligger på placeringer, som ikke er tilgængelige (‘A’, H1)")
        self.assertEqual(rb._offline_hint(state(2, [], [])),
                         "2 klip ligger på placeringer, som ikke er tilgængelige")

    def test_open_mode_opens_without_focus_once(self) -> None:
        h = Harness(self, follow="open")
        h.tick()
        h.tick(6.0)
        self.assertEqual(h.winui.opened, [(RIKKE, False)])
        self.assertEqual(len(self.notes(h)), 1)
        h.bridge.refresh(wait=False)
        h.tick(3.0)
        h.tick(6.0)
        self.assertEqual(h.winui.opened, [(RIKKE, False)])
        self.assertEqual(self.notes(h), [])

    def test_open_mode_reuses_an_existing_explorer_window(self) -> None:
        h = Harness(self, follow="open")
        h.winui.windows[RIKKE] = 0x1234
        h.tick()
        h.tick(6.0)
        self.assertEqual(h.winui.opened, [])
        self.assertEqual(len(self.notes(h)), 1)

    def test_open_mode_skips_an_unreachable_folder(self) -> None:
        h = Harness(self, follow="open")
        h.winui.stat_result = ("timeout", None)
        h.tick()
        h.tick(6.0)
        self.assertEqual(h.winui.opened, [])
        self.assertEqual(len(self.notes(h)), 1)

    def test_open_mode_uses_the_live_location(self) -> None:
        world = DiskWorld(online=True)
        h = Harness(self, project=pixelbro_project(), indexer=world.indexer(), follow="open")
        h.tick()
        world.online = False                   # unplugged before the project settled
        h.tick(6.0)
        self.assertEqual(h.winui.opened, [])

    def test_name_match_is_announced_but_never_opened(self) -> None:
        h = Harness(self, follow="open",
                    indexer=FakeIndexer(suggestions=[suggestion("Rikke Lindholm", RIKKE, 0.9)]))
        h.tick()
        h.tick(6.0)
        self.assertEqual(self.notes(h)[0]["text"], "Muligt match: Rikke Lindholm")
        self.assertEqual(h.winui.opened, [])


def _gone_local(count: int = 3, name: str = "Kunde X", iid: int = 1) -> dict[str, Any]:
    """A folder of 'Kunder 2025', renamed away on the disk 'Arbejdsdisk' that is connected."""
    return folder_entry(name, "D:\\Kunder 2025\\" + name, count,
                        source_ref(6, "Kunder 2025", online=False, drive="D:",
                                   disk_name="Arbejdsdisk", volume_present=True), iid=iid)


def _gone_share(count: int = 4) -> dict[str, Any]:
    """A folder of the share 'Projekter', which NAS no longer shares - NAS still answers."""
    return folder_entry("Kunde Y", "\\\\NAS\\Projekter\\Kunde Y", count,
                        source_ref(7, "Projekter", host="NAS", kind="share", online=False,
                                   drive=None, volume_present=True))


def _unplugged(count: int = 12) -> dict[str, Any]:
    return folder_entry("Pixelbro Radio", PIXELBRO, count,
                        source_ref(5, "2024 Disk Sølv", online=False, drive="H:",
                                   disk_name="2024 Disk Sølv", volume_present=False))


def _host_off(count: int = 1234) -> dict[str, Any]:
    return folder_entry("B", "\\\\GRAFIK-PC\\Forår 2026 (HDD)\\B", count,
                        source_ref(3, "Forår 2026 (HDD)", host="GRAFIK-PC", kind="share",
                                   online=False, drive=None, volume_present=False))


class GoneFolderHintTests(unittest.TestCase):
    """R3-RES-1 (SPEC §15.12): an offline folder whose disk is mounted, or whose computer
    answers, was moved, renamed or deleted. _follow_message() words it like the UI's Resolve
    bar - no disk to connect, no computer to switch on - and ``offline_disks`` leave it out."""

    @staticmethod
    def state(*folders: dict[str, Any], other_dirs: tuple = ()) -> dict[str, Any]:
        """The state _map() publishes for these map_paths folders (given most clips first)."""
        clips, disks = rb._offline_summary(list(folders), list(other_dirs))
        return {"project": "Kunde X", "folders": list(folders), "other_dirs": list(other_dirs),
                "primary": rb._choose_primary(list(folders), []), "offline_clips": clips,
                "offline_disks": disks}

    def test_folder_gone_from_a_connected_disk(self) -> None:
        state = self.state(_gone_local())
        self.assertEqual((state["offline_clips"], state["offline_disks"]), (3, []))
        self.assertEqual(rb._follow_message(state), {
            "title": "DaVinci Resolve: Kunde X",
            "text": "Projektmappe: Kunde X\n"
                    "3 klip ligger i mappen ‘Kunder 2025’, som ikke findes længere",
            "level": "warn"})

    def test_share_gone_from_a_computer_that_answers(self) -> None:
        state = self.state(_gone_share())
        self.assertEqual((state["offline_clips"], state["offline_disks"]), (4, []))
        self.assertEqual(rb._offline_hosts(state["folders"]), [])
        text = rb._follow_message(state)["text"]
        self.assertEqual(text, "Projektmappe: Kunde Y\n"
                               "4 klip ligger i mappen ‘Projekter’, som ikke findes længere")
        self.assertNotIn("svarer", text)

    def test_unplugged_disk_and_switched_off_computer_keep_their_hints(self) -> None:
        state = self.state(_unplugged())
        self.assertEqual((state["offline_clips"], state["offline_disks"]),
                         (12, ["2024 Disk Sølv"]))
        self.assertEqual(rb._follow_message(state)["text"],
                         "Projektmappe: Pixelbro Radio\n"
                         "12 klip ligger på disken ‘2024 Disk Sølv’, som ikke er tilsluttet")
        self.assertEqual(rb._follow_message(self.state(_host_off()))["text"],
                         "Projektmappe: B\n1.234 klip ligger på GRAFIK-PC, som ikke svarer")

    def test_gone_folders_next_to_unreachable_ones(self) -> None:
        """Gone folders make one line (one name per location), the disks/computers that are
        not there another; the line with the most clips comes first, as in the UI."""
        state = self.state(_unplugged(12), _gone_local(3))
        self.assertEqual((state["offline_clips"], state["offline_disks"]),
                         (15, ["2024 Disk Sølv"]))
        self.assertEqual(rb._follow_message(state)["text"],
                         "Projektmappe: Pixelbro Radio\n"
                         "12 klip ligger på disken ‘2024 Disk Sølv’, som ikke er tilsluttet\n"
                         "3 klip ligger i mappen ‘Kunder 2025’, som ikke findes længere")
        state = self.state(_gone_local(30), _host_off(5), _gone_share(4),
                           _gone_local(2, "Kunde Z", iid=2))
        self.assertEqual(rb._follow_message(state)["text"],
                         "Projektmappe: Kunde X\n"
                         "36 klip ligger i mapperne ‘Kunder 2025’ og ‘Projekter’, som ikke "
                         "findes længere\n5 klip ligger på GRAFIK-PC, som ikke svarer")
        lost = {"path": "E:\\Løse klip", "count": 1, "online": False}
        self.assertEqual(rb._offline_hint(self.state(_gone_local(), other_dirs=(lost,))),
                         "3 klip ligger i mappen ‘Kunder 2025’, som ikke findes længere\n"
                         "1 klip ligger på placeringer, som ikke er tilgængelige")


class RequestTests(unittest.TestCase):
    def test_refresh_rewalks_and_waits_on_the_real_thread(self) -> None:
        h = Harness(self, settings={"resolve_poll_s": 60})
        h.bridge._clock = time.monotonic
        h.bridge.start()
        deadline = time.monotonic() + 5
        while h.state["updated"] is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(h.state["clip_count"], 4)
        h.resolve.pm.project.pool.root.clips.append(FakeClip("D:\\ny\\klip.mov"))
        started = time.monotonic()
        st = h.bridge.refresh(wait=True)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(st["clip_count"], 5)
        self.assertEqual(threading.current_thread().name, "MainThread")
        self.assertEqual(h.bridge._thread.name, "resolve-bridge")

    def test_refresh_without_a_thread_returns_at_once(self) -> None:
        h = Harness(self)
        started = time.monotonic()
        st = h.bridge.refresh(wait=True)
        self.assertLess(time.monotonic() - started, 1)
        self.assertFalse(st["connected"])

    def test_refresh_when_resolve_is_not_running_returns_promptly(self) -> None:
        h = Harness(self, settings={"resolve_poll_s": 60})
        h.winui.running = False
        h.bridge.start()
        started = time.monotonic()
        st = h.bridge.refresh(wait=True)
        self.assertLess(time.monotonic() - started, 2)
        self.assertFalse(st["running"])

    def test_on_window_shown_rewalks_only_after_30_s(self) -> None:
        h = Harness(self)
        h.tick()
        h.bridge.on_window_shown()
        h.tick(10.0)
        self.assertEqual(len(h.indexer.map_calls), 1)
        h.bridge.on_window_shown()
        h.tick(21.0)
        self.assertEqual(len(h.indexer.map_calls), 2)
        h.tick(40.0)  # no pending request: no walk
        self.assertEqual(len(h.indexer.map_calls), 2)

    def test_config_change_wakes_the_thread(self) -> None:
        h = Harness(self, settings={"resolve_poll_s": 60})
        h.bridge.start()
        deadline = time.monotonic() + 5
        while not h.state["connected"] and time.monotonic() < deadline:
            time.sleep(0.01)
        h.cfg.update({"resolve_enabled": False})
        deadline = time.monotonic() + 2
        while h.state["enabled"] and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(h.state["enabled"])
        self.assertFalse(h.state["connected"])

    def test_stop_is_prompt_and_idempotent(self) -> None:
        h = Harness(self, settings={"resolve_poll_s": 60})
        h.bridge.start()
        started = time.monotonic()
        h.bridge.stop()
        h.bridge.stop()
        self.assertLess(time.monotonic() - started, 1)
        self.assertFalse(h.bridge._thread.is_alive())
        self.assertEqual(h.children.running, [])


class OpenPrimaryTests(unittest.TestCase):
    def test_reasons_without_a_primary(self) -> None:
        h = Harness(self, settings={"resolve_enabled": False})
        h.tick()
        self.assertEqual(h.bridge.open_primary(),
                         {"ok": False, "path": None, "error": rb.ERR_DISABLED})
        h = Harness(self)
        h.winui.running = False
        h.tick()
        self.assertEqual(h.bridge.open_primary()["error"], "DaVinci Resolve kører ikke")
        h = Harness(self, indexer=FakeIndexer())
        h.tick()
        self.assertEqual(h.bridge.open_primary()["error"],
                         "Ingen projektmappe fundet for ‘Rikke Lindholm - Testimonial’")
        self.assertEqual(h.winui.opened, [])

    def test_mapping_in_progress(self) -> None:
        h = Harness(self)
        h.bridge._set_state(h.bridge._connected_state(rb._Snapshot(("k",), "P", "DB")))
        self.assertEqual(h.bridge.open_primary()["error"],
                         "Projektmappen for ‘P’ er ved at blive fundet – prøv igen om et øjeblik")

    def test_refuses_offline_folders(self) -> None:
        h = Harness(self, indexer=FakeIndexer(rikke_mapping(online=False)))
        h.tick()
        self.assertEqual(h.bridge.open_primary(),
                         {"ok": False, "path": RIKKE, "error": DISK_HINT})
        share = folder_entry("B", "\\\\MEDIESERVER\\2025Arkiv\\B", 3,
                             source_ref(3, "2025Arkiv", host="MEDIESERVER",
                                        kind="share", online=False, drive=None))
        h = Harness(self, indexer=FakeIndexer({"folders": [share], "other_dirs": []}))
        h.tick()
        self.assertEqual(h.bridge.open_primary()["error"],
                         "Computeren MEDIESERVER svarer ikke – er den tændt?")
        self.assertEqual(h.winui.opened, [])

    def test_opens_with_activation(self) -> None:
        h = Harness(self, follow="off")
        h.tick()
        self.assertEqual(h.bridge.open_primary(), {"ok": True, "path": RIKKE, "error": None})
        self.assertEqual(h.winui.opened, [(RIKKE, True)])

    def test_open_failure(self) -> None:
        h = Harness(self, follow="off")
        h.tick()
        h.winui.open_result = False
        self.assertEqual(h.bridge.open_primary(),
                         {"ok": False, "path": RIKKE, "error": rb.ERR_OPEN_FAILED})

        def boom(path: str, activate: bool = True) -> bool:
            raise OSError("shell thread gone")

        h.bridge._funcs["open_folder"] = boom
        with self.assertLogs("projektsog.resolve_bridge", "ERROR"):
            self.assertFalse(h.bridge.open_primary()["ok"])


class OpenPrimaryLiveTests(unittest.TestCase):
    """RES-1 / XMC-2: open_primary() decides with the live registry and a timed stat."""

    def harness(self, world: DiskWorld) -> Harness:
        h = Harness(self, project=pixelbro_project(), indexer=world.indexer(), follow="off")
        h.tick()
        return h

    def test_opens_once_the_disk_is_plugged_in(self) -> None:
        world = DiskWorld(online=False)
        h = self.harness(world)
        self.assertEqual(h.bridge.open_primary(),
                         {"ok": False, "path": PIXELBRO, "error": DISK_HINT})
        world.online = True                    # no poll, no re-map, no walk since
        self.assertEqual(h.bridge.open_primary(), {"ok": True, "path": PIXELBRO, "error": None})
        self.assertEqual(h.winui.opened, [(PIXELBRO, True)])
        self.assertEqual(h.winui.stat_calls, [("open:source:5", rb.STAT_TIMEOUT_S)])

    def test_refuses_once_the_disk_is_unplugged(self) -> None:
        world = DiskWorld(online=True)
        h = self.harness(world)
        world.online = False
        self.assertEqual(h.bridge.open_primary(),
                         {"ok": False, "path": PIXELBRO, "error": DISK_HINT})
        self.assertEqual((h.winui.opened, h.winui.stat_calls), ([], []))

    def test_opens_at_the_new_drive_letter(self) -> None:
        world = DiskWorld(online=False)
        h = self.harness(world)
        world.online, world.root = True, "I:\\2024 Disk Sølv"
        self.assertEqual(h.bridge.open_primary()["path"], "I:\\2024 Disk Sølv\\Pixelbro Radio")
        self.assertEqual(h.winui.opened, [("I:\\2024 Disk Sølv\\Pixelbro Radio", True)])

    def test_folder_gone_while_its_disk_is_connected(self) -> None:
        """SPEC §15.12: offline although its disk is there (renamed or deleted folder) - the
        same hint as Controller.open_path, not 'Tilslut disken'."""
        world = DiskWorld(online=True)
        h = self.harness(world)
        world.online, world.volume_present = False, True
        self.assertEqual(h.bridge.open_primary(),
                         {"ok": False, "path": PIXELBRO, "error": "Mappen findes ikke længere"})
        world.volume_present = False
        self.assertEqual(h.bridge.open_primary()["error"], DISK_HINT)
        self.assertEqual(h.winui.opened, [])

    def test_share_hint_from_the_live_source(self) -> None:
        share = source_ref(3, "Forår 2026 (HDD)", host="GRAFIK-PC", kind="share", drive=None)
        root = "\\\\GRAFIK-PC\\Forår 2026 (HDD)"
        live = {"online": True}
        indexer = FakeIndexer(
            {"folders": [folder_entry("B", root + "\\B", 3, share)], "other_dirs": []},
            sources=lambda: [source_row(3, "Forår 2026 (HDD)", root, kind="share",
                                        host="GRAFIK-PC", unc_path=root,
                                        online=live["online"])])
        h = Harness(self, indexer=indexer, follow="off")
        h.tick()
        live["online"] = False
        self.assertEqual(h.bridge.open_primary(),
                         {"ok": False, "path": root + "\\B",
                          "error": "Computeren GRAFIK-PC svarer ikke – er den tændt?"})

    def test_timed_stat_outcomes(self) -> None:
        world = DiskWorld(online=True)
        cases = [(("ok", "missing"), rb.ERR_MISSING, [PIXELBRO]),
                 (("timeout", None), rb.ERR_NOT_RESPONDING, []),
                 (("busy", None), rb.ERR_NOT_RESPONDING, []),
                 (("error", None), rb.ERR_NOT_RESPONDING, []),
                 (("ok", "file"), rb.ERR_OPEN_FAILED, [])]
        for result, error, missing in cases:
            with self.subTest(result=result):
                h = self.harness(world)
                h.winui.stat_result = result
                self.assertEqual(h.bridge.open_primary(),
                                 {"ok": False, "path": PIXELBRO, "error": error})
                self.assertEqual(h.winui.opened, [])
                self.assertEqual(h.indexer.missing, missing)
        self.assertEqual(rb.ERR_MISSING, "Findes ikke længere – indekset opdateres")

    def test_probe_folder_on_disk(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            open(os.path.join(root, "fil.txt"), "w").close()
            self.assertEqual(rb._probe_folder(root), "dir")
            self.assertEqual(rb._probe_folder(os.path.join(root, "fil.txt")), "file")
            self.assertEqual(rb._probe_folder(os.path.join(root, "mangler")), "missing")


class OpenPrimaryIdentityTests(unittest.TestCase):
    """RES-2 (+ the name-match part of RES-1): only the project open in Resolve is opened."""

    def test_other_project_is_left_to_the_script(self) -> None:
        h = Harness(self, follow="off")
        h.tick()
        self.assertEqual(h.bridge.open_primary(project="Pixelbro - Radio spot",
                                               database=DB["DbName"]), {
            "ok": False, "path": None,
            "error": "Projektmappen for ‘Pixelbro - Radio spot’ er ved at blive fundet – "
                     "prøv igen om et øjeblik"})
        self.assertEqual(h.winui.opened, [])
        self.assertEqual(len(h.indexer.map_calls), 1)
        h.tick()                                      # the queued refresh re-walks
        self.assertEqual(len(h.indexer.map_calls), 2)

    def test_project_switched_before_the_next_poll(self) -> None:
        h = Harness(self, follow="off")
        h.tick()
        h.resolve.pm.project = FakeProject("Pixelbro - Radio spot")
        started = time.monotonic()
        result = h.bridge.open_primary(project="Pixelbro - Radio spot", database=DB["DbName"])
        self.assertLess(time.monotonic() - started, 1, "never waits for a walk")
        self.assertEqual((result["ok"], result["path"]), (False, None))
        self.assertEqual(h.winui.opened, [], "the previous project's folder is not opened")

    def test_other_database_is_left_to_the_script(self) -> None:
        h = Harness(self, follow="off")
        h.tick()
        result = h.bridge.open_primary(project="Rikke Lindholm - Testimonial",
                                       database="Local Database")
        self.assertEqual((result["ok"], result["path"]), (False, None))
        self.assertEqual(h.winui.opened, [])

    def test_same_project_opens(self) -> None:
        h = Harness(self, follow="off")
        h.tick()
        self.assertEqual(h.bridge.open_primary(project="Rikke Lindholm - Testimonial",
                                               database=DB["DbName"]),
                         {"ok": True, "path": RIKKE, "error": None})
        self.assertEqual(h.bridge.open_primary("Rikke Lindholm - Testimonial")["ok"], True)
        self.assertEqual(h.bridge.open_primary(database=DB["DbName"])["ok"], True)

    def test_invalid_identity(self) -> None:
        h = Harness(self)
        for kwargs in ({"project": 5}, {"database": ["x"]}, {"project": True}, {"uid": 7}):
            with self.subTest(**kwargs), self.assertRaises(ValueError):
                h.bridge.open_primary(**kwargs)

    def test_same_name_other_project_id(self) -> None:
        """RES2-2: two projects named alike (other Project Manager folders) are told apart by
        Project.GetUniqueId() before the next poll has seen the switch."""
        first = FakeProject("Reklame", FakeFolder("Master", clips_in(RIKKE + "\\Klip", "a.mxf")),
                            uid="uid-1")
        second = FakeProject("Reklame", FakeFolder("Master", clips_in("D:\\Andet\\Klip", "b.mxf")),
                             uid="uid-2")
        h = Harness(self, project=first, follow="off")
        h.tick()
        self.assertTrue(h.bridge.open_primary("Reklame", DB["DbName"], uid="uid-1")["ok"])
        h.resolve.pm.project = second                  # switched; not polled yet
        result = h.bridge.open_primary("Reklame", DB["DbName"], uid="uid-2")
        self.assertEqual((result["ok"], result["path"]), (False, None))
        self.assertEqual(h.winui.opened, [(RIKKE, True)], "the other project's folder stays shut")
        h.tick()                                       # the refused request queued a re-walk
        self.assertEqual(len(h.indexer.map_calls), 2)
        self.assertTrue(h.bridge.open_primary("Reklame", DB["DbName"], uid="uid-2")["ok"])
        self.assertFalse(h.bridge.open_primary("Reklame", DB["DbName"], uid="uid-1")["ok"])
        self.assertTrue(h.bridge.open_primary("Reklame", DB["DbName"])["ok"], "no id: no check")

    def test_untrusted_project_id_is_not_compared(self) -> None:
        h = Harness(self, follow="off", project=UnstableIdProject("Rikke Lindholm - Testimonial",
                                                                 rikke_project().pool.root))
        with self.assertLogs("projektsog.resolve_bridge", "WARNING"):
            h.tick()
            h.tick(3.0)                               # a second read reveals the unstable id
        self.assertEqual(h.bridge.open_primary(project="Rikke Lindholm - Testimonial",
                                               uid="volatile-99"),
                         {"ok": True, "path": RIKKE, "error": None})

    def test_name_match_is_left_to_the_script(self) -> None:
        h = Harness(self, follow="off",
                    indexer=FakeIndexer(suggestions=[suggestion("Rikke Lindholm", RIKKE, 0.9)]))
        h.tick()
        self.assertEqual(h.state["primary"]["match"], "name")
        result = h.bridge.open_primary(project="Rikke Lindholm - Testimonial")
        self.assertEqual((result["ok"], result["path"]), (False, None))
        self.assertIn("muligt match: ‘Rikke Lindholm’", result["error"])
        self.assertEqual(h.winui.opened, [])

    def test_imported_clips_are_found_after_the_script_asked(self) -> None:
        new = "\\\\GRAFIK-PC\\Kunder 2026 (Grafik)\\Klar Tand 2026\\Klar Tand - Silkeborg Voxpop"
        old = "\\\\MEDIESERVER\\2025Arkiv\\Klar Tand - Voxpop Silkeborg"

        def mapping(paths: list[str]) -> dict[str, Any]:
            folders = [folder_entry("Klar Tand - Silkeborg Voxpop", new, len(paths),
                                    source_ref(8, "Kunder 2026 (Grafik)", host="GRAFIK-PC",
                                               kind="share", drive=None))] if paths else []
            return {"folders": folders, "other_dirs": [], "total": len(paths)}

        project = FakeProject("Klar Tand - Silkeborg Voxpop")
        h = Harness(self, project=project, follow="off", indexer=FakeIndexer(
            mapping, suggestions=[suggestion("Klar Tand - Voxpop Silkeborg", old, 1.0)]))
        h.tick()                              # a new project: the pool is still empty
        self.assertEqual(h.state["primary"]["match"], "name")
        project.pool.root.clips.extend(clips_in(new + "\\Klip", "C1859.MP4", "C1860.MP4"))
        h.tick(40.0)                          # importing clips does not trigger a walk
        result = h.bridge.open_primary(project="Klar Tand - Silkeborg Voxpop")
        self.assertEqual((result["ok"], result["path"], h.winui.opened), (False, None, []))
        h.tick()                              # the request queued a re-walk (last one > 30 s)
        self.assertEqual(h.bridge.open_primary(project="Klar Tand - Silkeborg Voxpop"),
                         {"ok": True, "path": new, "error": None})

    def test_recent_walk_is_not_repeated(self) -> None:
        h = Harness(self, follow="off")
        h.tick()
        h.bridge.open_primary()
        h.tick(10.0)
        self.assertEqual(len(h.indexer.map_calls), 1)


class DefaultsTests(unittest.TestCase):
    def test_winui_is_imported_lazily(self) -> None:
        import projektsog

        fake = types.ModuleType("projektsog.winui")
        fake.process_running = lambda exe: False
        fake.process_uptime = lambda exe: None
        cfg = Config(path=os.path.join(tempfile.mkdtemp(dir=_tmp.name), "config.json"))

        def no_helper() -> Any:
            raise AssertionError("no helper while Resolve is not running")

        bridge = rb.ResolveBridge(cfg, EventBus(), FakeIndexer(), spawn_child=no_helper)
        with mock.patch.dict(sys.modules, {"projektsog.winui": fake}), \
                mock.patch.object(projektsog, "winui", fake, create=True):
            bridge._tick()
        self.assertFalse(bridge.state()["running"])

    def test_no_module_level_imports_of_other_agents(self) -> None:
        with open(rb.__file__, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        allowed = {"config", "events", "textutil", "relink", None}   # relink.py: own (SPEC §22.2)
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.level:
                self.assertIn(node.module, allowed, ast.dump(node))
                if node.module is None:
                    self.assertEqual([a.name for a in node.names], ["textutil"])
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn("projektsog", alias.name)

    def test_the_main_process_never_loads_resolve_scripting(self) -> None:
        """RES-3: resolve_bridge imports neither DaVinciResolveScript nor fusionscript, not
        even inside a function; from its helper module it takes only the request handling
        (Session, for the in-process ``connect`` test seam), never the connect function."""
        with open(rb.__file__, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        banned = {"DaVinciResolveScript", "fusionscript", "importlib", "imp",
                  "connect_resolve", "prepare_script_environment"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""] + [a.name for a in node.names]
                if node.module == "resolve_child":
                    self.assertEqual([a.name for a in node.names], ["Session"])
            else:
                names = []
            for name in names:
                self.assertNotIn(name.split(".")[0], banned, ast.dump(node))
            if isinstance(node, ast.Name):
                self.assertNotIn(node.id, {"__import__", "connect_resolve"})
            if isinstance(node, ast.Attribute):
                self.assertNotIn(node.attr, {"scriptapp", "load_dynamic", "import_module",
                                             "connect_resolve"})
        self.assertEqual(rb.default_child_argv()[1:], ["-m", "projektsog.resolve_child"])
        self.assertTrue(rb.default_child_argv()[0].lower().endswith(("pythonw.exe",
                                                                     "python.exe")))


def render_project() -> FakeProject:
    proj = rikke_project()
    proj.add_job("old", "Complete", file="Gammel.mp4")      # rendered before Projektsøg looked
    proj.add_job("j1", "Ready")
    return proj


def render_polls(h: Harness) -> list[Any]:
    """The ``render`` parameter of every poll the bridge sent (None: a plain poll)."""
    return [m.get("render") for c in h.children.children for m in c.requests if m["cmd"] == "poll"]


class RenderTests(unittest.TestCase):
    """Klippe watches renders (SPEC §22.1)."""

    def setUp(self) -> None:
        self.h = Harness(self, project=render_project(), follow="off")
        self.proj = self.h.resolve.pm.project
        self.h.tick()                                   # connect, poll, walk
        self.h.drain("render")

    def start_render(self, pct: int = 10) -> None:
        self.proj.rendering = True
        self.proj.set_status("j1", "Rendering", pct, EstimatedTimeRemainingInMs=180_000)

    def test_idle_polls_never_read_the_queue(self) -> None:
        self.h.tick(3.0)
        self.h.tick(3.0)
        self.assertEqual(set(map(repr, render_polls(self.h))), {"None"})
        self.assertEqual(self.proj.status_calls, [])
        self.assertEqual(self.h.bridge.render_state(), rb._idle_render())
        self.assertEqual(self.h.drain("render"), [])

    def test_a_render_from_start_to_done(self) -> None:
        self.start_render()
        self.h.tick(3.0)                                # rising edge: one more poll with a scan
        self.assertEqual(render_polls(self.h)[-2:], [None, {"scan": True, "watch": []}])
        started = {"aktiv": True, "pct": 10, "eta_s": 180, "navn": "Portræt_v3.mp4",
                   "tidslinje": "Portræt v3", "projekt": "Rikke Lindholm - Testimonial",
                   "af_claude": None, "faerdig": None}
        self.assertEqual(self.h.drain("render"), [started])
        self.assertEqual(self.h.bridge.render_state(), started)
        self.h.tick(3.0)
        self.assertEqual(render_polls(self.h)[-1], {"scan": False, "watch": ["j1"]})
        self.assertEqual(self.h.drain("render"), [], "nothing changed: nothing published")
        self.proj.set_status("j1", "Rendering", 47, EstimatedTimeRemainingInMs=61_400)
        self.h.tick(3.0)
        self.assertEqual(self.h.drain("render"), [{**started, "pct": 47, "eta_s": 61}])
        self.proj.rendering = False
        self.proj.set_status("j1", "Complete", 100, TimeTakenToRenderInMs=120_000)
        self.h.tick(3.0)
        done = {"udfald": "done", "fil": "Portræt_v3.mp4",
                "sti": "D:\\Rikke Lindholm\\Final\\Portræt_v3.mp4",
                "mappe": "D:\\Rikke Lindholm\\Final", "levering": True, "fejl": None, "seq": 1}
        self.assertEqual(self.h.drain("render"), [
            {**started, "aktiv": False, "pct": None, "eta_s": None, "faerdig": done}])
        self.assertEqual(self.h.indexer.refreshed, ["D:\\Rikke Lindholm\\Final"])
        self.h.tick(3.0)
        self.h.tick(3.0)
        self.assertEqual(render_polls(self.h)[-2:], [None, None], "the watch is over")
        self.assertEqual(self.h.drain("render"), [])
        self.assertEqual(self.h.bridge.render_state()["faerdig"], done, "kept for a reloaded widget")
        self.assertEqual(self.proj.status_calls, ["j1", "old", "j1", "j1", "j1"],
                         "the scan, then only the watched job")

    def test_failed_gone_and_cancelled_jobs_in_a_queue(self) -> None:
        self.proj.add_job("j2", "Ready", file="b.mov", folder="D:\\Eksport", mode="Individual clips")
        self.proj.add_job("j3", "Ready", file="c.mov", folder="D:\\Eksport")
        self.start_render()
        self.h.tick(3.0)
        self.h.drain("render")
        self.proj.set_status("j1", "Failed", Error="Disken er fuld")
        self.proj.set_status("j2", "Rendering", 5)
        self.h.tick(3.0)
        (failed,) = self.h.drain("render")
        self.assertEqual(failed["faerdig"], {
            "udfald": "failed", "fil": "Portræt_v3.mp4",
            "sti": "D:\\Rikke Lindholm\\Final\\Portræt_v3.mp4", "mappe": "D:\\Rikke Lindholm\\Final",
            "levering": True, "fejl": "Disken er fuld", "seq": 1})
        self.assertEqual((failed["aktiv"], failed["navn"]), (True, "b.mov"))
        self.proj.render_jobs = [j for j in self.proj.render_jobs if j["JobId"] != "j2"]
        self.proj.set_status("j3", "Rendering", 1)
        self.h.tick(3.0)
        (gone,) = self.h.drain("render")
        self.assertEqual((gone["faerdig"]["udfald"], gone["faerdig"]["sti"], gone["faerdig"]["seq"],
                          gone["faerdig"]["levering"]), ("gone", None, 2, False))
        self.proj.rendering = False
        self.proj.set_status("j3", "Cancelled")
        self.h.tick(3.0)
        (cancelled,) = self.h.drain("render")
        self.assertEqual((cancelled["aktiv"], cancelled["faerdig"]["udfald"], cancelled["faerdig"]["seq"]),
                         (False, "cancelled", 3))
        self.assertEqual(self.h.indexer.refreshed, [], "only a done render rescans its folder")

    def test_the_queue_holder_at_the_rising_edge(self) -> None:
        holders = [{"navn": "Mette", "projekt": "Rikke", "opgave": "byg", "siden": 1.0}]
        self.h.bridge.queue_holder = lambda: holders[0]
        self.start_render()
        self.h.tick(3.0)
        self.assertEqual(self.h.bridge.render_state()["af_claude"], "Mette")
        holders[0] = None
        self.h.tick(3.0)
        self.assertEqual(self.h.bridge.render_state()["af_claude"], "Mette", "set at the rising edge")

    def test_a_status_that_lags_behind_the_end_of_the_render(self) -> None:
        self.start_render()
        self.h.tick(3.0)
        self.proj.rendering = False                     # the job still says Rendering
        self.h.tick(3.0)
        self.assertIsNone(self.h.bridge.render_state()["faerdig"])
        self.proj.set_status("j1", "Complete", 100)
        self.h.tick(3.0)                                # the one look after the render
        self.assertEqual(render_polls(self.h)[-1], {"scan": False, "watch": ["j1"]})
        self.assertEqual(self.h.bridge.render_state()["faerdig"]["udfald"], "done")

    def test_a_job_still_rendering_after_the_look_never_fires_later(self) -> None:
        self.start_render()
        self.h.tick(3.0)
        self.proj.rendering = False
        self.h.tick(3.0)
        self.h.tick(3.0)                                # still "Rendering": forgotten
        self.assertEqual(render_polls(self.h)[-1], {"scan": False, "watch": ["j1"]})
        self.h.tick(3.0)
        self.assertIsNone(render_polls(self.h)[-1])
        self.proj.set_status("j1", "Complete", 100)
        self.proj.add_job("j9", "Rendering")
        self.proj.rendering = True                      # the next render
        self.h.tick(3.0)
        self.assertIsNone(self.h.bridge.render_state()["faerdig"])

    def test_connecting_during_a_render_takes_a_baseline(self) -> None:
        proj = render_project()
        proj.rendering = True
        proj.set_status("j1", "Rendering", 30)
        h = Harness(self, project=proj, follow="off")
        h.tick()
        self.assertEqual((h.bridge.render_state()["aktiv"], h.bridge.render_state()["pct"]), (True, 30))
        proj.rendering = False
        proj.set_status("j1", "Complete", 100)
        h.tick(3.0)
        self.assertEqual(h.bridge.render_state()["faerdig"]["fil"], "Portræt_v3.mp4",
                         "the old Complete job never fires; the running one does")

    def test_a_render_without_a_queue_job(self) -> None:
        self.proj.render_jobs = []
        self.proj.rendering = True                      # Quick Export: nothing in the queue
        self.h.tick(3.0)
        self.assertEqual({k: self.h.bridge.render_state()[k] for k in ("aktiv", "pct", "navn")},
                         {"aktiv": True, "pct": None, "navn": None})
        self.h.tick(3.0)
        self.assertIsNone(render_polls(self.h)[-1], "no scan in every poll")
        self.h.tick(rb.RENDER_RESCAN_S)
        self.assertEqual(render_polls(self.h)[-1], {"scan": True, "watch": []})

    def test_resolve_quits_during_a_render(self) -> None:
        self.start_render()
        self.h.tick(3.0)
        self.h.drain("render")
        self.h.winui.running = False
        self.h.tick(3.0)
        (event,) = self.h.drain("render")
        self.assertEqual((event["aktiv"], event["pct"], event["faerdig"]), (False, None, None))

    def test_a_truncated_answer_never_makes_a_job_gone(self) -> None:
        self.proj.add_job("j2", "Ready")
        self.start_render()
        self.h.tick(3.0)
        with mock.patch("projektsog.resolve_child.MAX_RENDER_JOBS", 1):
            self.proj.render_jobs.reverse()
            self.h.tick(3.0)
        self.assertIsNone(self.h.bridge.render_state()["faerdig"])
        self.assertEqual(sorted(self.h.bridge._rw.watch), ["j1", "j2"])

    def test_delivery_folders_and_outcomes(self) -> None:
        self.assertTrue(rb._is_delivery("\\\\server\\Kunder\\Rikke\\FINAL\\Web"))
        self.assertFalse(rb._is_delivery("D:\\Finale\\x"))
        self.assertFalse(rb._is_delivery(None))
        self.assertEqual([rb._render_outcome(s) for s in ("Complete", "Failed", "Cancelled",
                                                          "Background Render Cancelled", "Ready")],
                         ["done", "failed", "cancelled", "cancelled", None])
        self.assertTrue(rb._render_active("Ready for background render"))
        finished = rb._finished({"id": "x", "dir": "D:\\Final", "file": "clip_%d.mov",
                                 "mode": "Individual clips", "error": "nope"}, "done", 4)
        self.assertEqual((finished["sti"], finished["fejl"], finished["levering"]), (None, None, True))


REAL_LIST_NAMES = rb._list_names
PIXEL_OLD = PIXELBRO + "\\Klip"
ARKIV = "\\\\MEDIESERVER\\2026Arkiv"
PIXEL_NEW = ARKIV + "\\Pixelbro Radio\\Klip"
MUSIK_NEW = ARKIV + "\\Pixelbro Radio\\Musik"
METTE_HOLDS = {"navn": "Mette", "projekt": "P", "opgave": "", "siden": None}


def offline_pixelbro() -> FakeProject:
    clips = [offline_clip(PIXEL_OLD + "\\a.mov", uid="u-a"),
             offline_clip(PIXEL_OLD + "\\b.mov", uid="u-b"),
             offline_clip(PIXELBRO + "\\Musik\\ukendt.wav", uid="u-x"),
             FakeClip(PIXELBRO + "\\Grafik\\logo.png", uid="u-online")]
    proj = FakeProject("Pixelbro Radio", FakeFolder("Master", clips[:2], [FakeFolder("Rest", clips[2:])]))
    proj.indexed = [indexed_file(PIXEL_NEW + "\\a.mov", sid=7, root=ARKIV, project="Pixelbro Radio"),
                    indexed_file(PIXEL_NEW + "\\b.mov", sid=7, root=ARKIV, project="Pixelbro Radio")]
    return proj


class OfflinePlanTests(unittest.TestCase):
    """Find and relink offline media (SPEC §22.2)."""

    def setUp(self) -> None:
        proj = offline_pixelbro()
        self.h = Harness(self, project=proj, follow="off")
        self.h.indexer.files = proj.indexed
        self.pool = proj.pool
        self.listings = {PIXEL_NEW.casefold(): {"a.mov", "b.mov", "c.mov"}}
        patcher = mock.patch.object(rb, "_list_names", self.list_names)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.h.tick()
        self.h.drain("resolve")

    def list_names(self, folder: str) -> set[str]:
        names = self.listings.get(folder.casefold())
        if names is None:
            raise FileNotFoundError(folder)
        return names

    def call(self, fn: Callable[[], Any]) -> Any:
        """Run ``fn`` on another thread (like an HTTP request) while the test thread plays the
        Resolve thread."""
        outcome: dict[str, Any] = {}

        def target() -> None:
            try:
                outcome["value"] = fn()
            except Exception as exc:     # noqa: BLE001 - handed to the test thread
                outcome["error"] = exc

        with mock.patch.object(self.h.bridge, "_thread_alive", return_value=True):
            thread = threading.Thread(target=target)
            thread.start()
            deadline = time.monotonic() + 10.0
            while thread.is_alive() and time.monotonic() < deadline:
                if self.h.bridge._jobs:
                    self.h.tick(0.5)
                else:
                    time.sleep(0.002)
            thread.join(5.0)
        if "error" in outcome:
            raise outcome["error"]
        return outcome["value"]

    def plan(self) -> dict[str, Any]:
        return self.call(self.h.bridge.offline_plan)

    def relink(self, body: Any) -> dict[str, Any]:
        return self.call(lambda: self.h.bridge.relink(body))

    def test_the_plan(self) -> None:
        plan = self.plan()
        self.assertEqual(plan, {
            "project": "Pixelbro Radio", "database": DB["DbName"], "uid": "uid-Pixelbro Radio",
            "scanned": 4, "truncated": False, "blocked": None,
            "groups": [{"from": PIXEL_OLD, "to": PIXEL_NEW, "to_display": PIXEL_NEW, "online": True,
                        "clips": [{"uid": "u-a", "name": "a.mov", "old_path": PIXEL_OLD + "\\a.mov"},
                                  {"uid": "u-b", "name": "b.mov", "old_path": PIXEL_OLD + "\\b.mov"}],
                        "auto": True, "alternatives": []}],
            "not_found": [{"uid": "u-x", "name": "ukendt.wav",
                           "old_path": PIXELBRO + "\\Musik\\ukendt.wav"}]})
        self.assertEqual(self.h.indexer.find_calls, [["a.mov", "b.mov", "ukendt.wav"]])
        self.assertEqual(self.h.drain("resolve"), [], "the plan is never part of the state")
        self.assertEqual(self.pool.relinks, [], "making a plan writes nothing")

    def test_relink(self) -> None:
        plan = self.plan()
        result = self.relink({"uid": plan["uid"], "groups": [{"to": PIXEL_NEW, "uids": ["u-a", "u-b"]}]})
        self.assertEqual(result, {"relinked": 2, "still_offline": 1, "failed": [], "error": None})
        self.assertEqual(self.pool.relinks, [(["u-a", "u-b"], PIXEL_NEW)])
        self.assertEqual(self.h.winui.list_calls,
                         [("relink:\\\\medieserver\\2026arkiv", rb.LIST_TIMEOUT_S)])
        self.assertTrue(self.h.bridge._take_requests()[1], "a re-walk is queued")
        again = self.relink({"uid": plan["uid"], "groups": [{"to": PIXEL_NEW, "uids": ["u-a"]}]})
        self.assertEqual((again["relinked"], again["failed"]),
                         (0, [{"uid": "u-a", "name": "a.mov", "why": "Klippet er allerede online"}]))

    def test_what_is_checked_before_resolve_is_asked(self) -> None:
        plan = self.plan()
        self.listings[PIXEL_NEW.casefold()] = {"a.mov"}
        result = self.relink({"uid": plan["uid"], "groups": [
            {"to": PIXEL_NEW, "uids": ["u-a", "u-b", "u-nope"]},
            {"to": "E:\\Andet", "uids": ["u-a"]}]})
        self.assertEqual(result["failed"], [
            {"uid": "u-nope", "name": "u-nope", "why": rb.WHY_NOT_IN_PLAN},
            {"uid": "u-a", "name": "a.mov", "why": rb.WHY_NOT_OFFERED},
            {"uid": "u-b", "name": "b.mov", "why": "Filen ‘b.mov’ ligger ikke i mappen"}])
        self.assertEqual((result["relinked"], self.pool.relinks), (1, [(["u-a"], PIXEL_NEW)]))
        self.h.winui.list_status = "timeout"
        result = self.relink({"uid": plan["uid"], "groups": [{"to": PIXEL_NEW, "uids": ["u-b"]}]})
        self.assertEqual(result["failed"], [{"uid": "u-b", "name": "b.mov", "why": rb.WHY_NO_ANSWER}])
        self.assertEqual(len(self.pool.relinks), 1, "nothing to send: Resolve is not asked")

    def test_resolve_cannot_find_the_file(self) -> None:
        plan = self.plan()
        self.pool.missing = {"b.mov"}
        result = self.relink({"uid": plan["uid"], "groups": [{"to": PIXEL_NEW, "uids": ["u-a", "u-b"]}]})
        self.assertEqual((result["relinked"], result["still_offline"], result["failed"]),
                         (1, 2, [{"uid": "u-b", "name": "b.mov",
                                  "why": "Klippet er stadig offline efter genlink"}]))

    def test_never_while_a_claude_session_holds_resolve(self) -> None:
        self.h.bridge.queue_holder = lambda: {"navn": "Mette", "projekt": "P", "opgave": "", "siden": None}
        plan = self.plan()
        self.assertEqual(plan["blocked"], "Mette bygger i Resolve lige nu – genlink, når den er færdig")
        result = self.relink({"uid": plan["uid"], "groups": [{"to": PIXEL_NEW, "uids": ["u-a"]}]})
        self.assertEqual(result, {"relinked": 0, "still_offline": 3, "failed": [],
                                  "error": plan["blocked"]})
        self.assertEqual((self.pool.relinks, self.h.winui.list_calls), ([], []))

    def test_never_while_resolve_renders(self) -> None:
        plan = self.plan()
        project = self.h.resolve.pm.project
        project.rendering = True
        self.h.tick(3.0)
        self.assertEqual(self.plan()["blocked"], rb.ERR_RENDERING)
        result = self.relink({"uid": plan["uid"], "groups": [{"to": PIXEL_NEW, "uids": ["u-a"]}]})
        self.assertEqual(result["error"], rb.ERR_RENDERING)
        self.assertEqual(self.pool.relinks, [])

    def test_a_render_that_starts_after_the_click_is_seen_on_the_resolve_thread(self) -> None:
        plan = self.plan()
        self.h.resolve.pm.project.rendering = True       # the poll before the job sees it
        result = self.relink({"uid": plan["uid"], "groups": [{"to": PIXEL_NEW, "uids": ["u-a"]}]})
        self.assertEqual(result["error"], rb.ERR_RENDERING)
        self.assertEqual(self.pool.relinks, [])

    def test_only_in_the_project_the_plan_was_made_for(self) -> None:
        plan = self.plan()
        body = {"uid": plan["uid"], "groups": [{"to": PIXEL_NEW, "uids": ["u-a"]}]}
        self.assertEqual(self.relink({**body, "uid": "another"})["error"], rb.ERR_PROJECT_CHANGED)
        self.h.resolve.pm.project = pixelbro_project()   # another project with the same name
        self.h.resolve.pm.project.uid = "uid-other"
        self.assertEqual(self.relink(body)["error"], rb.ERR_PROJECT_CHANGED)
        self.assertEqual(self.pool.relinks, [])

    def test_requests_that_cannot_be_served(self) -> None:
        with self.assertRaisesRegex(ValueError, rb.ERR_NO_PLAN):
            self.relink({"uid": "", "groups": [{"to": PIXEL_NEW, "uids": ["u-a"]}]})
        plan = self.plan()
        for body in (None, [], {"uid": 5, "groups": []}, {"uid": plan["uid"], "groups": []},
                     {"groups": [{"to": "relativ\\mappe", "uids": ["u-a"]}]},
                     {"groups": [{"to": PIXEL_NEW, "uids": [""]}]},
                     {"groups": [{"to": PIXEL_NEW}]}):
            with self.subTest(body=body):
                with self.assertRaisesRegex(ValueError, rb.ERR_BAD_RELINK):
                    self.relink(body)
        self.h.clock.advance(rb.PLAN_MAX_AGE_S + 1)
        with self.assertRaisesRegex(ValueError, rb.ERR_NO_PLAN):
            self.relink({"uid": plan["uid"], "groups": [{"to": PIXEL_NEW, "uids": ["u-a"]}]})
        self.h.winui.running = False
        self.h.tick(3.0)
        with self.assertRaisesRegex(ValueError, rb.ERR_NOT_RUNNING):
            self.plan()

    def test_a_real_folder_listing(self) -> None:
        target = tempfile.mkdtemp(dir=_tmp.name)
        for name in ("a.mov", "B.MOV"):
            with open(os.path.join(target, name), "wb"):
                pass
        self.h.indexer.files = [indexed_file(os.path.join(target, n), sid=3, project=None)
                                for n in ("a.mov", "b.mov")]
        plan = self.plan()
        self.assertEqual((plan["groups"][0]["to"], plan["groups"][0]["auto"]), (target, True))
        with mock.patch.object(rb, "_list_names", REAL_LIST_NAMES):
            result = self.relink({"uid": plan["uid"], "groups": [{"to": target, "uids": ["u-a", "u-b"]}]})
        self.assertEqual((result["relinked"], result["failed"]), (2, []))

    def test_image_sequences_are_found_by_their_first_frame(self) -> None:
        self.assertTrue(rb._file_there("shot_[0001-0100].exr", {"shot_0001.exr"}))
        self.assertFalse(rb._file_there("shot_[0001-0100].exr", {"shot_0002.exr"}))
        self.assertTrue(rb._file_there("A.MOV", {"a.mov"}))

    # -- one helper request per folder; a relink that started is never "busy" ---------------
    def two_folders(self) -> dict[str, Any]:
        """A plan with two target folders (a.mov + b.mov → Klip, ukendt.wav → Musik); returns
        the body that relinks both, Klip first."""
        self.h.indexer.files = [*self.h.indexer.files,
                                indexed_file(MUSIK_NEW + "\\ukendt.wav", sid=7, root=ARKIV,
                                             project="Pixelbro Radio")]
        self.listings[MUSIK_NEW.casefold()] = {"ukendt.wav"}
        plan = self.plan()
        self.assertEqual(sorted(g["to"] for g in plan["groups"]), [PIXEL_NEW, MUSIK_NEW])
        return {"uid": plan["uid"], "groups": [{"to": PIXEL_NEW, "uids": ["u-a", "u-b"]},
                                               {"to": MUSIK_NEW, "uids": ["u-x"]}]}

    def sent(self) -> list[dict[str, Any]]:
        return [m for c in self.h.children.children for m in c.requests]

    def test_the_relink_timeout_grows_with_the_clips(self) -> None:
        base = rb._relink_timeout(0)
        self.assertGreaterEqual(base, rb.WALK_MAX_SECONDS + rb.CHILD_WALK_MARGIN_S)
        self.assertLess(rb._relink_timeout(10), rb._relink_timeout(1000))
        self.assertGreater(rb._relink_timeout(1000), 5 * 60.0,
                           "1000 clips on a slow share get far more than the old fixed 60 s")
        self.assertEqual(rb._relink_timeout(rb.MAX_RELINK_UIDS), rb.CHILD_RELINK_MAX_S)
        self.assertGreater(rb._relink_run_s(1), rb._relink_timeout(1) + rb.CHILD_CALL_TIMEOUT_S,
                           "a started job may take its fresh poll and the relink")

    def test_one_request_per_folder_after_a_fresh_poll(self) -> None:
        body = self.two_folders()
        result = self.relink(body)
        self.assertEqual(result, {"relinked": 3, "still_offline": 0, "failed": [], "error": None})
        self.assertEqual(self.pool.relinks, [(["u-a", "u-b"], PIXEL_NEW), (["u-x"], MUSIK_NEW)])
        self.assertEqual([t for t in self.h.children.timeouts if t[0] == "relink"],
                         [("relink", rb._relink_timeout(2)), ("relink", rb._relink_timeout(1))])
        sent = self.sent()
        relinks = [i for i, m in enumerate(sent) if m["cmd"] == "relink"]
        self.assertEqual([[g["folder"] for g in sent[i]["groups"]] for i in relinks],
                         [[PIXEL_NEW], [MUSIK_NEW]])
        self.assertEqual([sent[i - 1]["cmd"] for i in relinks], ["poll", "poll"])

    def test_a_claude_session_that_starts_between_folders_stops_the_rest(self) -> None:
        body = self.two_folders()

        def first_done(uids: list[str], folder: str) -> None:
            self.pool.on_relink = None
            self.h.bridge.queue_holder = lambda: METTE_HOLDS

        self.pool.on_relink = first_done
        result = self.relink(body)
        self.assertEqual(result, {
            "relinked": 2, "still_offline": 1, "error": None,
            "failed": [{"uid": "u-x", "name": "ukendt.wav",
                        "why": "Mette bygger i Resolve lige nu – genlink, når den er færdig"}]})
        self.assertEqual(self.pool.relinks, [(["u-a", "u-b"], PIXEL_NEW)])

    def test_a_render_that_starts_between_folders_stops_the_rest(self) -> None:
        body = self.two_folders()

        def first_done(uids: list[str], folder: str) -> None:
            self.pool.on_relink = None
            self.h.resolve.pm.project.rendering = True

        self.pool.on_relink = first_done
        result = self.relink(body)
        self.assertEqual((result["relinked"], result["error"], result["failed"]),
                         (2, None, [{"uid": "u-x", "name": "ukendt.wav", "why": rb.ERR_RENDERING}]))
        self.assertEqual(self.pool.relinks, [(["u-a", "u-b"], PIXEL_NEW)])

    def test_a_helper_that_dies_during_relink_may_have_relinked(self) -> None:
        plan = self.plan()
        self.h.children.fail["relink"] = "late"   # RelinkClips ran, the answer never came
        with self.assertLogs("projektsog.resolve_bridge", "WARNING") as logs:
            result = self.relink({"uid": plan["uid"],
                                  "groups": [{"to": PIXEL_NEW, "uids": ["u-a", "u-b"]}]})
        self.assertTrue(any("outcome is not known" in line for line in logs.output))
        self.assertEqual(result, {
            "relinked": 0, "still_offline": 1, "error": rb.ERR_RELINK_UNKNOWN.format(n=2),
            "failed": [{"uid": "u-a", "name": "a.mov", "why": rb.WHY_UNKNOWN},
                       {"uid": "u-b", "name": "b.mov", "why": rb.WHY_UNKNOWN}]})
        self.assertIn("2 klip kan være genlinket", result["error"])
        self.assertNotEqual(result["error"], rb.ERR_BUSY, "never 'svarer ikke' as if nothing happened")
        self.assertEqual(self.pool.relinks, [(["u-a", "u-b"], PIXEL_NEW)])
        self.assertEqual(self.h.children.running, [], "the helper is stopped (and restarted later)")
        self.assertEqual(self.h.state["error"], rb.ERR_NO_RESPONSE)

    def test_no_folder_is_sent_after_one_whose_outcome_is_unknown(self) -> None:
        body = self.two_folders()
        self.h.children.fail["relink"] = "late"
        with self.assertLogs("projektsog.resolve_bridge", "WARNING"):
            result = self.relink(body)
        self.assertEqual(result, {
            "relinked": 0, "still_offline": 1, "error": rb.ERR_RELINK_UNKNOWN.format(n=2),
            "failed": [{"uid": "u-a", "name": "a.mov", "why": rb.WHY_UNKNOWN},
                       {"uid": "u-b", "name": "b.mov", "why": rb.WHY_UNKNOWN},
                       {"uid": "u-x", "name": "ukendt.wav", "why": rb.ERR_BUSY}]})
        self.assertEqual(self.pool.relinks, [(["u-a", "u-b"], PIXEL_NEW)])

    def test_the_folders_relinked_before_a_failure_are_reported(self) -> None:
        body = self.two_folders()

        def first_done(uids: list[str], folder: str) -> None:
            self.pool.on_relink = None
            self.h.children.fail["relink"] = "late"     # the next request's answer never comes

        self.pool.on_relink = first_done
        with self.assertLogs("projektsog.resolve_bridge", "WARNING"):
            result = self.relink(body)
        self.assertEqual(result, {
            "relinked": 2, "still_offline": 0,
            "error": "2 klip er genlinket. " + rb.ERR_RELINK_UNKNOWN.format(n=1),
            "failed": [{"uid": "u-x", "name": "ukendt.wav", "why": rb.WHY_UNKNOWN}]})
        self.assertEqual(self.pool.relinks, [(["u-a", "u-b"], PIXEL_NEW), (["u-x"], MUSIK_NEW)])

    def test_a_started_relink_is_waited_for_beyond_the_submit_wait(self) -> None:
        plan = self.plan()
        self.pool.on_relink = lambda uids, folder: time.sleep(0.8)    # a slow share
        with mock.patch.object(rb, "SUBMIT_WAIT_S", 0.3):
            result = self.relink({"uid": plan["uid"],
                                  "groups": [{"to": PIXEL_NEW, "uids": ["u-a", "u-b"]}]})
        self.assertEqual(result, {"relinked": 2, "still_offline": 1, "failed": [], "error": None})

    def test_a_relink_still_running_is_not_reported_as_nothing_done(self) -> None:
        body = self.two_folders()
        self.pool.on_relink = lambda uids, folder: time.sleep(1.0)
        with mock.patch.object(rb, "SUBMIT_WAIT_S", 0.3), \
                mock.patch.object(rb, "_relink_run_s", return_value=0.2), \
                self.assertLogs("projektsog.resolve_bridge", "WARNING") as logs:
            result = self.relink(body)
        self.assertTrue(any("still working on a request" in line for line in logs.output))
        self.assertEqual(result, {
            "relinked": 0, "still_offline": 1, "error": rb.ERR_RELINK_RUNNING.format(n=2),
            "failed": [{"uid": "u-a", "name": "a.mov", "why": rb.WHY_RUNNING},
                       {"uid": "u-b", "name": "b.mov", "why": rb.WHY_RUNNING},
                       {"uid": "u-x", "name": "ukendt.wav", "why": rb.WHY_WAITING}]})
        self.assertIn("genlinker stadig", result["error"])
        self.assertEqual(self.pool.relinks, [(["u-a", "u-b"], PIXEL_NEW)], "the rest waits")
        self.assertEqual(self.h.bridge._plan.relinked, {"u-a", "u-b"},
                         "what the job relinked after the answer still counts")

    def test_a_relink_the_resolve_thread_never_started_is_dropped(self) -> None:
        plan = self.plan()
        body = {"uid": plan["uid"], "groups": [{"to": PIXEL_NEW, "uids": ["u-a", "u-b"]}]}
        with mock.patch.object(rb, "SUBMIT_WAIT_S", 0.05), \
                mock.patch.object(self.h.bridge, "_thread_alive", return_value=True), \
                self.assertLogs("projektsog.resolve_bridge", "WARNING"):
            with self.assertRaisesRegex(ValueError, rb.ERR_BUSY):
                self.h.bridge.relink(body)          # nothing was sent: "busy" is the truth
        self.h.tick(3.0)
        self.assertEqual(self.pool.relinks, [], "a dropped job never runs late")
        self.assertNotIn("relink", [m["cmd"] for m in self.sent()])

    def test_a_later_folder_the_resolve_thread_never_gets_to(self) -> None:
        body = self.two_folders()
        ticked = threading.Event()
        submits: list[Any] = []
        real_submit = self.h.bridge._submit

        def submit(fn: Any, *args: Any, **kwargs: Any) -> Any:
            submits.append(fn)
            if len(submits) == 2:
                ticked.wait(5.0)           # the second folder is queued after that one tick
            return real_submit(fn, *args, **kwargs)

        outcome: dict[str, Any] = {}
        with mock.patch.object(rb, "SUBMIT_WAIT_S", 0.3), \
                mock.patch.object(self.h.bridge, "_thread_alive", return_value=True), \
                mock.patch.object(self.h.bridge, "_submit", submit), \
                self.assertLogs("projektsog.resolve_bridge", "WARNING"):
            thread = threading.Thread(target=lambda: outcome.update(value=self.h.bridge.relink(body)))
            thread.start()
            deadline = time.monotonic() + 5.0
            while not self.h.bridge._jobs and time.monotonic() < deadline:
                time.sleep(0.002)
            self.h.tick(0.5)                   # the first folder only
            ticked.set()
            thread.join(5.0)
        self.assertEqual(outcome["value"], {
            "relinked": 2, "still_offline": 1, "error": None,
            "failed": [{"uid": "u-x", "name": "ukendt.wav", "why": rb.ERR_BUSY}]})
        self.h.tick(3.0)
        self.assertEqual(self.pool.relinks, [(["u-a", "u-b"], PIXEL_NEW)])


class ConnectSeamTests(unittest.TestCase):
    def test_connect_runs_the_request_handling_in_process(self) -> None:
        """The ``connect`` test seam (used by repro scripts) still works: no process starts."""
        resolve = FakeResolve(rikke_project())
        winui = FakeWinui()
        cfg = Config(path=os.path.join(tempfile.mkdtemp(dir=_tmp.name), "config.json"))
        cfg.update({"resolve_follow": "off"})
        bridge = rb.ResolveBridge(cfg, EventBus(), FakeIndexer(rikke_mapping()),
                                  process_running=winui.process_running,
                                  process_uptime=winui.process_uptime,
                                  open_folder=winui.open_folder,
                                  explorer_window_for=winui.explorer_window_for,
                                  call_with_timeout=winui.call_with_timeout,
                                  connect=lambda: resolve)
        self.addCleanup(bridge.stop)
        with mock.patch.object(rb, "_ChildProcess", side_effect=AssertionError("no process")):
            bridge._tick()
        st = bridge.state()
        self.assertEqual((st["project"], st["clip_count"]), ("Rikke Lindholm - Testimonial", 4))
        self.assertEqual(bridge.open_primary(), {"ok": True, "path": RIKKE, "error": None})
        self.assertNotIn("DaVinciResolveScript", sys.modules)


class HelperProcessTests(unittest.TestCase):
    """RES-3 end to end: the real helper process (``python -m projektsog.resolve_child``) against
    a stand-in DaVinciResolveScript module - never Resolve itself, which need not run."""

    CLIPS = ["D:\\Kunder\\Rikke Lindholm\\Klip\\a.mxf", "D:\\Kunder\\Rikke Lindholm\\Klip\\b.mxf",
             "D:\\Kunder\\Rikke Lindholm\\Musik\\c.wav"]

    def setUp(self) -> None:
        folder = tempfile.mkdtemp(dir=_tmp.name)
        self.world = {"project": {"name": "Hjælper-projekt", "uid": "u1", "clips": self.CLIPS},
                      "db": {"DbType": "Disk", "DbName": "Local Database"}, "noise": True}
        env = write_fake_resolve(folder, self.world)
        self.spec_path = env["PROJEKTSOG_FAKE_RESOLVE"]
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.spawned: list[Any] = []

    def spawn(self) -> Any:
        child = rb._ChildProcess(rb.default_child_argv())
        self.spawned.append(child)
        self.addCleanup(child.close, 1.0)
        return child

    def change_world(self, **changes: Any) -> None:
        self.world.update(changes)
        update_fake_resolve(self.spec_path, self.world)

    def bridge(self, winui: FakeWinui, indexer: FakeIndexer) -> rb.ResolveBridge:
        cfg = Config(path=os.path.join(tempfile.mkdtemp(dir=_tmp.name), "config.json"))
        cfg.update({"resolve_follow": "off", "resolve_poll_s": 1})
        bridge = rb.ResolveBridge(
            cfg, EventBus(), indexer, process_running=winui.process_running,
            process_uptime=winui.process_uptime, open_folder=winui.open_folder,
            explorer_window_for=winui.explorer_window_for,
            call_with_timeout=winui.call_with_timeout, spawn_child=self.spawn)
        self.addCleanup(bridge.stop)
        return bridge

    @staticmethod
    def wait_for(condition: Callable[[], bool], seconds: float = 10.0) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if condition():
                return True
            time.sleep(0.02)
        return condition()

    def test_bridge_uses_the_helper_and_stops_it_with_resolve(self) -> None:
        winui = FakeWinui()
        indexer = FakeIndexer()
        bridge = self.bridge(winui, indexer)
        bridge.start()
        self.assertTrue(self.wait_for(lambda: bridge.state()["updated"] is not None),
                        bridge.state())
        st = bridge.state()
        self.assertEqual((st["connected"], st["project"], st["database"], st["clip_count"]),
                         (True, "Hjælper-projekt", "Local Database", 3))
        self.assertEqual(indexer.map_calls, [self.CLIPS])     # the noise did not get in the way
        self.assertNotIn("DaVinciResolveScript", sys.modules)
        self.assertNotIn("fusionscript", sys.modules)
        helper, = self.spawned
        self.assertTrue(helper.alive())
        winui.running = False                                  # Resolve quits
        self.assertTrue(self.wait_for(lambda: not helper.alive(), 5.0),
                        "the helper must exit when Resolve exits")
        self.assertTrue(self.wait_for(lambda: not bridge.state()["running"], 5.0))

    def test_hung_helper_is_killed_and_restarted(self) -> None:
        self.change_world(on_poll="hang")
        winui = FakeWinui()
        indexer = FakeIndexer()
        bridge = self.bridge(winui, indexer)
        with mock.patch.object(rb, "CHILD_CALL_TIMEOUT_S", 1.0), \
                mock.patch.object(rb, "CHILD_BACKOFF_S", (0.2,)), \
                self.assertLogs("projektsog.resolve_bridge", "WARNING"):
            bridge.start()
            self.assertTrue(self.wait_for(lambda: bool(self.spawned)
                                          and not self.spawned[0].alive()),
                            "a helper stuck in a scripting call is killed")
            # (the state is set right after the kill - wait for it rather than race it)
            self.assertTrue(self.wait_for(lambda: bridge.state()["error"] == rb.ERR_NO_RESPONSE),
                            bridge.state())
            self.change_world(on_poll=None)
            self.assertTrue(self.wait_for(lambda: bridge.state()["updated"] is not None),
                            bridge.state())
        self.assertGreaterEqual(len(self.spawned), 2)
        self.assertEqual(bridge.state()["project"], "Hjælper-projekt")

    def test_helper_exits_on_end_of_input_even_while_a_call_hangs(self) -> None:
        self.change_world(on_walk="hang")
        helper = self.spawn()
        helper.wait_ready(10.0)
        self.assertEqual(helper.request("connect", 10.0), {"id": 1, "ok": True})
        with self.assertRaises(rb.ChildError):
            helper.request("walk", 0.5, max_clips=10, max_seconds=5.0)
        helper._proc.stdin.close()                             # the app is gone
        self.assertEqual(helper._proc.wait(5.0), 0)

    def test_crashing_helper_is_reported(self) -> None:
        self.change_world(on_walk="crash")
        helper = self.spawn()
        helper.wait_ready(10.0)
        helper.request("connect", 10.0)
        with self.assertRaisesRegex(rb.ChildError, "exited"):
            helper.request("walk", 10.0)
        self.assertFalse(helper.alive())
        with self.assertRaises(rb.ChildError):
            helper.request("poll", 1.0)


if __name__ == "__main__":
    unittest.main()
