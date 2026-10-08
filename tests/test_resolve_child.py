"""Unit tests for projektsog.resolve_child (the DaVinci Resolve scripting helper process)."""

from __future__ import annotations

import ast
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from typing import Any

from projektsog import resolve_bridge as rb
from projektsog import resolve_child as rc
from tests._resolve_fakes import (
    DB, DictOnlyClip, FakeClip, FakeClock, FakeFolder, FakeProject, FakeResolve,
    UnstableIdProject, clips_in, offline_clip, write_fake_resolve)

_tmp: tempfile.TemporaryDirectory | None = None


def setUpModule() -> None:
    global _tmp
    _tmp = tempfile.TemporaryDirectory()
    os.environ["LOCALAPPDATA"] = _tmp.name


def tearDownModule() -> None:
    if _tmp is not None:
        _tmp.cleanup()


def project() -> FakeProject:
    sub = FakeFolder("Klip", clips_in("D:\\P\\Klip", "a.mov", "b.mov"))
    root = FakeFolder("Master", [FakeClip(""), FakeClip("D:\\P\\Musik\\c.wav")], [sub])
    return FakeProject("Projekt Ø", root, uid="uid-1")


class SessionTests(unittest.TestCase):
    def session(self, resolve: Any = None, clock: Any = time.monotonic) -> rc.Session:
        resolve = resolve if resolve is not None else FakeResolve(project())
        session = rc.Session(connect=lambda: resolve, clock=clock)
        self.assertEqual(session.handle({"id": 1, "cmd": "connect"}), {"id": 1, "ok": True})
        return session

    def test_connect_outcomes(self) -> None:
        def missing() -> Any:
            raise ImportError("Could not locate module dependencies")

        def broken() -> Any:
            raise RuntimeError("fusionscript crashed")

        with self.assertLogs("projektsog.resolve_child", "INFO") as logs:
            answers = [rc.Session(connect=c).handle({"id": 7, "cmd": "connect"})
                       for c in (lambda: None, missing, broken)]
        self.assertEqual([(a["id"], a["ok"], a["error"]) for a in answers],
                         [(7, False, "refused"), (7, False, "module"), (7, False, "failed")])
        self.assertIn("RuntimeError: fusionscript crashed", answers[2]["detail"])
        self.assertTrue(any("could not be loaded" in line for line in logs.output))

    def test_requests_before_connect(self) -> None:
        session = rc.Session(connect=lambda: FakeResolve(project()))
        for cmd in ("poll", "uid", "walk"):
            self.assertEqual(session.handle({"id": 3, "cmd": cmd}),
                             {"id": 3, "ok": False, "error": "not_connected",
                              "detail": "connect first"})

    def test_poll(self) -> None:
        resolve = FakeResolve(project())
        session = self.session(resolve)
        # The fakes have no page/timeline/render getters: those fields stay empty.
        idle = {"page": "", "timeline": "", "timecode": "", "rendering": False}
        self.assertEqual(session.handle({"id": 2, "cmd": "poll"}), {
            "id": 2, "ok": True, "db": [DB["DbType"], DB["DbName"], DB["IpAddress"]],
            "database": DB["DbName"], "project": "Projekt Ø", "uid": "uid-1", **idle})
        resolve.pm.project = None
        resolve.pm.GetCurrentDatabase = lambda: None     # not a dict
        self.assertEqual(session.handle({"id": 3, "cmd": "poll"}),
                         {"id": 3, "ok": True, "db": ["", "", ""], "database": None,
                          "project": None, "uid": "", **idle})

    def test_poll_reports_what_the_editor_does(self) -> None:
        # For the time tracker: the open page, the timeline, the playhead and whether a render runs.
        proj = project()
        timeline = type("Timeline", (), {"GetCurrentTimecode": lambda self: "01:00:12:05",
                                         "GetName": lambda self: "Testimonial v3"})()
        proj.GetCurrentTimeline = lambda: timeline
        proj.IsRenderingInProgress = lambda: True
        resolve = FakeResolve(proj)
        resolve.GetCurrentPage = lambda: "color"
        answer = self.session(resolve).handle({"id": 4, "cmd": "poll"})
        self.assertEqual((answer["page"], answer["timeline"], answer["timecode"], answer["rendering"]),
                         ("color", "Testimonial v3", "01:00:12:05", True))
        # A timeline without a name getter still reports its playhead.
        proj.GetCurrentTimeline = lambda: type("Timeline", (), {"GetCurrentTimecode": lambda self: "01:00:00:00"})()
        answer = self.session(resolve).handle({"id": 5, "cmd": "poll"})
        self.assertEqual((answer["timeline"], answer["timecode"]), ("", "01:00:00:00"))
        proj.GetCurrentTimeline = lambda: None            # no timeline open
        proj.IsRenderingInProgress = lambda: (_ for _ in ()).throw(RuntimeError("busy"))
        answer = self.session(resolve).handle({"id": 6, "cmd": "poll"})
        self.assertEqual((answer["page"], answer["timeline"], answer["timecode"], answer["rendering"]),
                         ("color", "", "", False))

    def test_uid_through_a_fresh_proxy(self) -> None:
        resolve = FakeResolve(UnstableIdProject("P"))
        session = self.session(resolve)
        first = session.handle({"id": 2, "cmd": "poll"})["uid"]
        again = session.handle({"id": 3, "cmd": "uid"})
        self.assertEqual((again["ok"], again["project"]), (True, "P"))
        self.assertNotEqual(again["uid"], first)
        resolve.pm.project = None
        self.assertEqual(session.handle({"id": 4, "cmd": "uid"}),
                         {"id": 4, "ok": True, "project": None, "uid": ""})

    def test_resolve_stops_answering(self) -> None:
        resolve = FakeResolve(project())
        session = self.session(resolve)
        resolve.alive = False                        # GetProjectManager() -> None
        answer = session.handle({"id": 5, "cmd": "poll"})
        self.assertEqual((answer["ok"], answer["error"]), (False, "unavailable"))
        resolve.alive = True
        self.assertEqual(session.handle({"id": 6, "cmd": "poll"})["error"], "not_connected",
                         "the connection is dropped; the bridge connects again")

    def test_failing_proxy_call(self) -> None:
        resolve = FakeResolve(project())
        session = self.session(resolve)
        resolve.raise_on_pm = RuntimeError("broken pipe")
        with self.assertLogs("projektsog.resolve_child", "WARNING"):
            answer = session.handle({"id": 5, "cmd": "walk"})
        self.assertEqual(answer, {"id": 5, "ok": False, "error": "unavailable",
                                  "detail": "RuntimeError: broken pipe"})

    def test_bad_project_name(self) -> None:
        odd = project()
        odd.name = None
        answer = self.session(FakeResolve(odd)).handle({"id": 1, "cmd": "poll"})
        self.assertEqual((answer["ok"], answer["error"]), (False, "unavailable"))

    def test_walk(self) -> None:
        session = self.session()
        answer = session.handle({"id": 9, "cmd": "walk", "max_clips": 100, "max_seconds": 5})
        self.assertEqual(answer, {"id": 9, "ok": True, "clip_count": 4, "truncated": False,
                                  "paths": ["D:\\P\\Musik\\c.wav", "D:\\P\\Klip\\a.mov",
                                            "D:\\P\\Klip\\b.mov"]})
        json.dumps(answer)

    def test_walk_limits(self) -> None:
        root = FakeFolder("Master", clips_in("D:\\x", *[f"{i}.mov" for i in range(12)]))
        session = self.session(FakeResolve(FakeProject("P", root)))
        answer = session.handle({"id": 1, "cmd": "walk", "max_clips": 5})
        self.assertEqual((len(answer["paths"]), answer["clip_count"], answer["truncated"]),
                         (5, 5, True))
        clock = FakeClock()
        folders = [FakeFolder(f"F{i}", clips_in(f"D:\\f{i}", "a.mov"),
                              on_list=lambda: clock.advance(3.0)) for i in range(20)]
        session = self.session(FakeResolve(FakeProject("P", FakeFolder("Master", [], folders))),
                               clock)
        answer = session.handle({"id": 2, "cmd": "walk", "max_seconds": 20.0})
        self.assertTrue(answer["truncated"])
        self.assertLess(sum(1 for f in folders if f.listed), 20)

    def test_walk_parameters_fall_back_to_the_defaults(self) -> None:
        self.assertEqual(rc._positive_int(True, 7), 7)
        self.assertEqual(rc._positive_int(0, 7), 7)
        self.assertEqual(rc._positive_int("5", 7), 7)
        self.assertEqual(rc._positive_float(float("nan"), 2.0), 2.0)
        self.assertEqual(rc._positive_float(-1, 2.0), 2.0)
        self.assertEqual(rc._positive_float(3, 2.0), 3.0)
        self.assertEqual((rc.DEFAULT_MAX_CLIPS, rc.DEFAULT_MAX_SECONDS),
                         (rb.WALK_MAX_CLIPS, rb.WALK_MAX_SECONDS))

    def test_walk_old_and_new_api_styles(self) -> None:
        root = FakeFolder("Master", [DictOnlyClip("D:\\x\\a.mov"), FakeClip("D:\\x\\b.mov")],
                          as_dict=True)
        answer = self.session(FakeResolve(FakeProject("P", root))).handle(
            {"id": 1, "cmd": "walk"})
        self.assertEqual(answer["paths"], ["D:\\x\\a.mov", "D:\\x\\b.mov"])

    def test_walk_without_a_project_or_pool(self) -> None:
        resolve = FakeResolve(project())
        session = self.session(resolve)
        resolve.pm.project = None
        self.assertEqual(session.handle({"id": 1, "cmd": "walk"}),
                         {"id": 1, "ok": True, "paths": [], "clip_count": 0,
                          "truncated": False})
        resolve.pm.project = project()
        resolve.pm.project.pool.root = None
        self.assertEqual(session.handle({"id": 2, "cmd": "walk"})["error"], "unavailable")

    def test_unknown_requests(self) -> None:
        session = rc.Session(connect=lambda: None)
        for message in ({"id": 1, "cmd": "SetCurrentDatabase"}, {"id": 2}, {"id": 3, "cmd": 4}):
            with self.subTest(message=message):
                answer = session.handle(message)
                self.assertEqual((answer["id"], answer["ok"], answer["error"]),
                                 (message["id"], False, "bad_request"))


class RenderPollTests(unittest.TestCase):
    """``poll`` with ``render`` reads the render queue (SPEC §22.1)."""

    def setUp(self) -> None:
        self.clock = FakeClock()
        self.proj = project()
        self.session = rc.Session(connect=lambda: FakeResolve(self.proj), clock=self.clock)
        self.session.handle({"id": 1, "cmd": "connect"})

    def poll(self, **render: Any) -> dict[str, Any]:
        return self.session.handle({"id": 2, "cmd": "poll", "render": render})

    def test_without_render_nothing_changes(self) -> None:
        self.proj.add_job("j1", "Rendering")
        answer = self.session.handle({"id": 2, "cmd": "poll"})
        self.assertNotIn("jobs", answer)
        self.assertNotIn("jobs_truncated", answer)
        self.assertEqual(self.proj.status_calls, [])

    def test_scan_reads_the_newest_jobs(self) -> None:
        self.proj.add_job("old", "Complete", file="gammel.mov")
        self.proj.add_job("j2", "Rendering", file="Portræt_v3.mp4", folder="D:\\P\\Final")
        self.proj.set_status("j2", "Rendering", pct=47, EstimatedTimeRemainingInMs=180_000)
        self.proj.rendering = True
        answer = self.poll(scan=True, watch=[])
        self.assertTrue(answer["rendering"])
        self.assertFalse(answer["jobs_truncated"])
        self.assertEqual([j["id"] for j in answer["jobs"]], ["j2", "old"])
        self.assertEqual(answer["jobs"][0], {
            "id": "j2", "name": "Job j2", "timeline": "Portræt v3", "dir": "D:\\P\\Final",
            "file": "Portræt_v3.mp4", "mode": "Single clip", "preset": "H.264 Master",
            "status": "Rendering", "pct": 47, "eta_ms": 180_000, "took_ms": None, "error": ""})
        json.dumps(answer)

    def test_watch_reads_only_the_watched_jobs(self) -> None:
        for job_id in ("a", "b", "c"):
            self.proj.add_job(job_id, "Complete")
        self.proj.set_status("b", "Failed", Error="Disk full")
        answer = self.poll(scan=False, watch=["b", "gone", 7])
        self.assertEqual([(j["id"], j["status"], j["error"]) for j in answer["jobs"]],
                         [("b", "Failed", "Disk full")])
        self.assertEqual(self.proj.status_calls, ["b"])
        self.assertFalse(answer["jobs_truncated"], "a watched job that is gone is just absent")

    def test_at_most_25_jobs(self) -> None:
        for i in range(30):
            self.proj.add_job(f"j{i:02}", "Complete")
        answer = self.poll(scan=True, watch=["j00"])
        self.assertEqual(len(answer["jobs"]), rc.MAX_RENDER_JOBS)
        self.assertTrue(answer["jobs_truncated"])
        self.assertEqual([j["id"] for j in answer["jobs"][:3]], ["j00", "j29", "j28"],
                         "watched jobs first, then the newest")

    def test_time_budget(self) -> None:
        for i in range(10):
            self.proj.add_job(f"j{i}", "Complete")
        self.proj.on_status = lambda: self.clock.advance(2.0)
        answer = self.poll(scan=True)
        self.assertTrue(answer["jobs_truncated"])
        self.assertLess(len(answer["jobs"]), 10)
        self.assertLessEqual(len(self.proj.status_calls), 4)

    def test_failing_getters(self) -> None:
        self.proj.add_job("j1", "Rendering")
        self.proj.GetRenderJobStatus = lambda job_id: (_ for _ in ()).throw(RuntimeError("busy"))
        answer = self.poll(scan=True)
        self.assertEqual([(j["id"], j["status"], j["pct"]) for j in answer["jobs"]], [("j1", "", None)])
        self.proj.list_error = RuntimeError("no queue")
        self.assertEqual({k: self.poll(scan=True)[k] for k in ("ok", "jobs", "jobs_truncated")},
                         {"ok": True, "jobs": [], "jobs_truncated": True})

    def test_no_project(self) -> None:
        session = rc.Session(connect=lambda: FakeResolve(None))
        session.handle({"id": 1, "cmd": "connect"})
        answer = session.handle({"id": 2, "cmd": "poll", "render": {"scan": True}})
        self.assertEqual((answer["project"], answer["jobs"], answer["jobs_truncated"]),
                         (None, [], False))

    def test_numbers(self) -> None:
        self.assertEqual([rc._int_or_none(v) for v in (5, 5.7, "12", True, "x", float("nan"), None)],
                         [5, 5, 12, None, None, None, None])


def offline_project() -> FakeProject:
    klip = FakeFolder("Klip", [offline_clip("H:\\Disk\\P\\Klip\\a.mov", uid="u-a"),
                               FakeClip("H:\\Disk\\P\\Klip\\b.mov", uid="u-b")])
    root = FakeFolder("Master", [FakeClip("", uid="timeline"),
                                 offline_clip("H:\\Disk\\P\\Musik\\song.wav", "Sangen", uid="u-s")],
                      [klip])
    return FakeProject("P", root, uid="p-uid")


class OfflineTests(unittest.TestCase):
    def session(self, proj: FakeProject, clock: Any = time.monotonic) -> rc.Session:
        session = rc.Session(connect=lambda: FakeResolve(proj), clock=clock)
        session.handle({"id": 1, "cmd": "connect"})
        return session

    def test_offline_clips(self) -> None:
        proj = offline_project()
        answer = self.session(proj).handle({"id": 3, "cmd": "offline", "max_clips": 100})
        self.assertEqual((answer["ok"], answer["scanned"], answer["truncated"]), (True, 4, False))
        self.assertEqual(answer["clips"], [
            {"uid": "u-s", "name": "Sangen", "path": "H:\\Disk\\P\\Musik\\song.wav",
             "dir": "H:\\Disk\\P\\Musik", "type": "Video + Audio", "frames": "250", "fps": 25.0,
             "resolution": "1920x1080", "status": "Offline"},
            {"uid": "u-a", "name": "a.mov", "path": "H:\\Disk\\P\\Klip\\a.mov",
             "dir": "H:\\Disk\\P\\Klip", "type": "Video + Audio", "frames": "250", "fps": 25.0,
             "resolution": "1920x1080", "status": "Offline"}])
        clips = proj.pool.root.clips + proj.pool.root.subfolders[0].clips
        self.assertEqual([c.property_calls for c in clips], [1, 1, 1, 1],
                         "one GetClipProperty() per clip")

    def test_offline_limits_and_no_project(self) -> None:
        root = FakeFolder("Master", [offline_clip(f"H:\\x\\{i}.mov") for i in range(8)])
        answer = self.session(FakeProject("P", root)).handle({"id": 1, "cmd": "offline",
                                                              "max_clips": 3})
        self.assertEqual((len(answer["clips"]), answer["scanned"], answer["truncated"]), (3, 3, True))
        resolve = FakeResolve(None)
        session = rc.Session(connect=lambda: resolve)
        self.assertEqual(session.handle({"id": 1, "cmd": "offline"})["error"], "not_connected")
        session.handle({"id": 2, "cmd": "connect"})
        self.assertEqual(session.handle({"id": 3, "cmd": "offline"}),
                         {"id": 3, "ok": True, "clips": [], "scanned": 0, "truncated": False})


class RelinkCommandTests(unittest.TestCase):
    """``relink``: the one writing call, MediaPool.RelinkClips (SPEC §1 rule 2, §22.2)."""

    def setUp(self) -> None:
        self.proj = offline_project()
        self.session = rc.Session(connect=lambda: FakeResolve(self.proj))
        self.session.handle({"id": 1, "cmd": "connect"})

    def relink(self, groups: Any, **extra: Any) -> dict[str, Any]:
        return self.session.handle({"id": 9, "cmd": "relink", "groups": groups, **extra})

    def test_relinks_once_per_folder(self) -> None:
        answer = self.relink([
            {"folder": "\\\\SERVER\\Arkiv\\P\\Klip", "uids": ["u-a"],
             "expect": {"u-a": "H:\\Disk\\P\\Klip\\a.mov"}},
            {"folder": "\\\\server\\arkiv\\P\\klip", "uids": ["u-s"],
             "expect": {"u-s": "h:\\disk\\p\\musik\\SONG.wav"}}],
            project={"name": "P", "uid": "p-uid", "database": DB["DbName"]})
        self.assertEqual(self.proj.pool.relinks, [(["u-a", "u-s"], "\\\\SERVER\\Arkiv\\P\\Klip")])
        self.assertEqual(answer["results"], [
            {"uid": "u-a", "ok": True, "path": "\\\\SERVER\\Arkiv\\P\\Klip\\a.mov",
             "status": "Online", "why": None},
            {"uid": "u-s", "ok": True, "path": "\\\\SERVER\\Arkiv\\P\\Klip\\song.wav",
             "status": "Online", "why": None}])
        self.assertEqual((answer["truncated"], answer["project_changed"]), (False, False))

    def test_leaves_clips_alone_that_changed(self) -> None:
        self.proj.pool.missing = {"song.wav"}
        answer = self.relink([
            {"folder": "E:\\Ny", "uids": ["u-b", "u-a", "nope", "u-s"],
             "expect": {"u-b": "H:\\Disk\\P\\Klip\\b.mov", "u-a": "H:\\Andet\\a.mov",
                        "nope": "H:\\x.mov", "u-s": "H:\\Disk\\P\\Musik\\song.wav"}}])
        self.assertEqual([(r["uid"], r["ok"], r["why"]) for r in answer["results"]],
                         [("u-b", False, "online"), ("u-a", False, "changed"),
                          ("nope", False, "not_found"), ("u-s", False, "offline")])
        self.assertEqual(self.proj.pool.relinks, [(["u-s"], "E:\\Ny")])

    def test_relink_failure(self) -> None:
        self.proj.pool.raises = RuntimeError("relink broke")
        with self.assertLogs("projektsog.resolve_child", "WARNING"):
            answer = self.relink([{"folder": "E:\\Ny", "uids": ["u-a"],
                                   "expect": {"u-a": "H:\\Disk\\P\\Klip\\a.mov"}}])
        self.assertEqual([(r["ok"], r["why"], r["status"]) for r in answer["results"]],
                         [(False, "relink_failed", "Offline")])

    def test_another_project_is_left_alone(self) -> None:
        for project in ({"name": "Andet", "uid": "", "database": ""},
                        {"name": "P", "uid": "other-uid", "database": ""},
                        {"name": "P", "uid": "p-uid", "database": "Anden database"}):
            with self.subTest(project=project):
                answer = self.relink([{"folder": "E:\\Ny", "uids": ["u-a"],
                                       "expect": {"u-a": "H:\\Disk\\P\\Klip\\a.mov"}}],
                                     project=project)
                self.assertEqual((answer["ok"], answer["results"], answer["project_changed"]),
                                 (True, [], True))
        self.assertEqual(self.proj.pool.relinks, [])

    def test_bad_requests(self) -> None:
        for groups in (None, [{"folder": "", "uids": ["u-a"]}], [{"folder": "E:\\x", "uids": "u-a"}],
                       [{"folder": "E:\\x", "uids": [""]}], [], ["x"]):
            with self.subTest(groups=groups):
                self.assertEqual(self.relink(groups)["error"], "bad_request")
        self.assertEqual(self.proj.pool.relinks, [])

    def test_relink_clips_is_the_only_writing_call(self) -> None:
        """Every method the helper calls on a Resolve object is a getter - except RelinkClips."""
        with open(rc.__file__, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        modules = {alias.asname or alias.name.split(".")[0] for node in ast.walk(tree)
                   if isinstance(node, (ast.Import, ast.ImportFrom))
                   for alias in node.names} | {"_kernel32"}

        def root(node: ast.AST) -> str | None:
            while isinstance(node, ast.Attribute):
                node = node.value
            return node.id if isinstance(node, ast.Name) else None

        names = {node.attr for node in ast.walk(tree)       # calls, and getters passed uncalled
                 if isinstance(node, ast.Attribute) and node.attr[:1].isupper()
                 and root(node) not in modules}
        self.assertIn("GetClipProperty", names)
        self.assertEqual({n for n in names if not n.startswith(("Get", "Is"))}, {"RelinkClips"})


class ServeTests(unittest.TestCase):
    def run_serve(self, lines: list[bytes]) -> tuple[list[dict[str, Any]], list[str]]:
        answers: list[dict[str, Any]] = []
        events: list[str] = []
        stdin = io.BytesIO(b"".join(lines))
        session = rc.Session(connect=lambda: FakeResolve(project()))
        rc.serve(stdin, answers.append, session, on_eof=lambda: events.append("eof"))
        return answers, events

    def test_answers_in_order_until_quit(self) -> None:
        with self.assertLogs("projektsog.resolve_child", "WARNING") as logs:
            answers, _ = self.run_serve([b'{"id": 1, "cmd": "connect"}\n', b"\n",
                                         b"not json\n", b"[1, 2]\n",
                                         b'{"id": 2, "cmd": "poll"}\n', b'{"cmd": "quit"}\n',
                                         b'{"id": 3, "cmd": "poll"}\n'])
        self.assertEqual(len(logs.records), 2, "the malformed and the non-object line")
        self.assertEqual([a["id"] for a in answers], [1, 2])
        self.assertEqual(answers[1]["project"], "Projekt Ø")

    def test_end_of_input(self) -> None:
        answers, events = self.run_serve([b'{"id": 1, "cmd": "connect"}\n'])
        self.assertEqual([a["id"] for a in answers], [1])
        self.assertEqual(events, ["eof"])

    def test_end_of_input_is_noticed_during_a_hanging_call(self) -> None:
        release = threading.Event()
        eof = threading.Event()

        class Hanging(rc.Session):
            def handle(self, message: Any) -> dict[str, Any]:
                release.wait(5)
                return {"id": message.get("id"), "ok": True}

        read_fd, write_fd = os.pipe()
        with os.fdopen(read_fd, "rb") as stdin:
            worker = threading.Thread(target=rc.serve,
                                      args=(stdin, lambda a: None, Hanging(), eof.set))
            worker.start()
            with os.fdopen(write_fd, "wb") as out:
                out.write(b'{"id": 1, "cmd": "walk"}\n')
            self.assertTrue(eof.wait(5), "EOF must be seen while the call still runs")
            release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())


class LineWriterTests(unittest.TestCase):
    def test_ascii_lines(self) -> None:
        out = io.BytesIO()
        rc.LineWriter(out)({"id": 1, "ok": True, "paths": ["D:\\Forår\\ø.mov"]})
        line = out.getvalue()
        self.assertTrue(line.endswith(b"\n"))
        line.decode("ascii")
        self.assertEqual(json.loads(line)["paths"], ["D:\\Forår\\ø.mov"])

    def test_broken_pipe(self) -> None:
        class Broken(io.BytesIO):
            def write(self, data: bytes) -> int:
                raise BrokenPipeError(32, "The pipe is being closed")

        calls: list[int] = []
        writer = rc.LineWriter(Broken(), on_broken=lambda: calls.append(1))
        with self.assertLogs("projektsog.resolve_child", "INFO"):
            writer({"id": 1, "ok": True})
        writer({"id": 2, "ok": True})
        self.assertEqual((calls, writer.broken), ([1], True))


class EnvironmentTests(unittest.TestCase):
    def test_script_environment(self) -> None:
        environ: dict[str, str] = {}
        path: list[str] = []
        modules = rc.prepare_script_environment(environ, path)
        self.assertTrue(environ["RESOLVE_SCRIPT_API"].endswith(
            "Blackmagic Design\\DaVinci Resolve\\Support\\Developer\\Scripting"))
        self.assertTrue(environ["RESOLVE_SCRIPT_LIB"].endswith(
            "Blackmagic Design\\DaVinci Resolve\\fusionscript.dll"))
        self.assertEqual(path, [modules])
        self.assertEqual(modules, environ["RESOLVE_SCRIPT_API"] + "\\Modules")
        rc.prepare_script_environment(environ, path)
        self.assertEqual(path, [modules])
        custom = tempfile.mkdtemp(dir=_tmp.name)
        environ = {"RESOLVE_SCRIPT_API": custom}
        path = []
        self.assertEqual(rc.prepare_script_environment(environ, path), custom + "\\Modules")

    def test_imports_only_the_shared_base(self) -> None:
        with open(rc.__file__, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.level:
                self.assertIn(node.module, {"config", "events", "textutil"}, ast.dump(node))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn("projektsog", alias.name)
                    self.assertNotEqual(alias.name, "DaVinciResolveScript",
                                        "only imported on connect")


class ProcessTests(unittest.TestCase):
    """``python -m projektsog.resolve_child`` over pipes, with a stand-in scripting module."""

    def start(self, **world: Any) -> subprocess.Popen:
        folder = tempfile.mkdtemp(dir=_tmp.name)
        spec = {"project": {"name": "Forår – Ø", "uid": "u9", "clips": ["E:\\x\\a.mov"]},
                **world}
        env = dict(os.environ, **write_fake_resolve(folder, spec))
        env["PYTHONPATH"] = rb._REPO_ROOT
        proc = subprocess.Popen(rb.default_child_argv(), stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                cwd=folder, env=env, creationflags=rb._CREATE_NO_WINDOW,
                                close_fds=True)

        def cleanup() -> None:
            if proc.poll() is None:
                proc.kill()
                proc.wait(5)
            for stream in (proc.stdin, proc.stdout):
                try:
                    stream.close()
                except OSError:
                    pass

        self.addCleanup(cleanup)
        return proc

    @staticmethod
    def ask(proc: subprocess.Popen, message: dict[str, Any]) -> dict[str, Any]:
        proc.stdin.write(json.dumps(message).encode("ascii") + b"\n")
        proc.stdin.flush()
        return json.loads(proc.stdout.readline())

    def test_protocol_over_pipes_with_a_noisy_library(self) -> None:
        proc = self.start(noise=True)
        ready = json.loads(proc.stdout.readline())
        self.assertEqual(ready, {"ev": "ready", "pid": proc.pid})
        self.assertEqual(self.ask(proc, {"id": 1, "cmd": "connect"}), {"id": 1, "ok": True})
        poll = self.ask(proc, {"id": 2, "cmd": "poll"})
        self.assertEqual((poll["project"], poll["uid"]), ("Forår – Ø", "u9"))
        walk = self.ask(proc, {"id": 3, "cmd": "walk", "max_clips": 10, "max_seconds": 5})
        self.assertEqual(walk["paths"], ["E:\\x\\a.mov"])
        proc.stdin.write(b'{"cmd": "quit"}\n')
        proc.stdin.flush()
        self.assertEqual(proc.wait(10), 0)
        self.assertEqual(proc.stdout.read(), b"", "nothing but answers on the protocol pipe")
        log_file = os.path.join(_tmp.name, "Projektsog", "logs", rc.LOG_FILE)
        with open(log_file, encoding="utf-8") as fh:
            self.assertIn("Connected to DaVinci Resolve", fh.read())

    def test_refused_connection(self) -> None:
        proc = self.start(refuse=True)
        proc.stdout.readline()
        self.assertEqual(self.ask(proc, {"id": 1, "cmd": "connect"})["error"], "refused")
        proc.stdin.close()
        self.assertEqual(proc.wait(10), 0)

    def test_exits_at_once_on_end_of_input(self) -> None:
        env = dict(os.environ, PYTHONPATH=rb._REPO_ROOT)
        proc = subprocess.run([sys.executable, "-m", "projektsog.resolve_child"],
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, env=env, timeout=30,
                              creationflags=rb._CREATE_NO_WINDOW)
        self.assertEqual(proc.returncode, 0, "EOF right away: nothing to do")


if __name__ == "__main__":
    unittest.main()
