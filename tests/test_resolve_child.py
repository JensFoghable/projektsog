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
    UnstableIdProject, clips_in, write_fake_resolve)

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
