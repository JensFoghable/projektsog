"""Tests for the app lifecycle (SPEC §13) with fakes: single instance and hand-off,
RUN_COMMAND, startup order, config listener, notification forwarding, exit sequence and the
pythonw hardening (the latter in a child process, as it changes process-wide state)."""

import contextlib
import ctypes
import gc
import importlib.util
import io
import json
import os
import queue
import socket
import struct
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
import uuid
import warnings
from ctypes import wintypes
from unittest import mock

from projektsog import config
from projektsog.config import Config
from projektsog.events import EventBus
from projektsog.server import Server
from tests import _app_fakes as fakes

app = None      # projektsog.app, imported once LOCALAPPDATA points to a temp dir
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_saved_env: dict[str, str | None] = {}
_tmp: tempfile.TemporaryDirectory | None = None


def setUpModule() -> None:
    global app, _tmp
    _tmp = tempfile.TemporaryDirectory()
    _saved_env["LOCALAPPDATA"] = os.environ.get("LOCALAPPDATA")
    os.environ["LOCALAPPDATA"] = _tmp.name
    app = fakes.import_app()


def tearDownModule() -> None:
    if _saved_env.get("LOCALAPPDATA") is None:
        os.environ.pop("LOCALAPPDATA", None)
    else:
        os.environ["LOCALAPPDATA"] = _saved_env["LOCALAPPDATA"]
    _tmp.cleanup()


_shell32 = ctypes.WinDLL("shell32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_shell32.CommandLineToArgvW.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int))
_shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
_kernel32.LocalFree.argtypes = (wintypes.HLOCAL,)
_kernel32.LocalFree.restype = wintypes.HLOCAL


def split_command_line(command: str) -> list[str]:
    """How Windows (CreateProcess/Run key) splits a command line into argv."""
    argc = ctypes.c_int()
    argv = _shell32.CommandLineToArgvW(command, ctypes.byref(argc))
    if not argv:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return [argv[i] for i in range(argc.value)]
    finally:
        _kernel32.LocalFree(ctypes.cast(argv, wintypes.HLOCAL))


class SingleInstanceTests(unittest.TestCase):
    def test_second_instance_is_detected(self) -> None:
        name = f"Local\\Projektsog-test-{uuid.uuid4().hex}"
        first, second, third = (app.SingleInstance(name) for _ in range(3))
        self.assertTrue(first.acquire())
        self.addCleanup(first.release)
        self.assertFalse(second.acquire())
        first.release()
        self.assertTrue(third.acquire())
        third.release()
        third.release()                                 # idempotent

    def test_mutex_name_is_per_user_session(self) -> None:
        self.assertEqual(app.mutex_name(), "Local\\Projektsog-" + os.environ["USERNAME"])

    def test_instance_file_round_trip_and_removal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "instance.json")
            app.write_instance_file(path, 1234, 47811)
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(json.load(fh), {"pid": 1234, "port": 47811})
            self.assertEqual(app.read_instance_file(path), {"pid": 1234, "port": 47811})
            self.assertEqual(os.listdir(tmp), ["instance.json"])    # no temp file left
            app.remove_instance_file(path, 999)                     # another process's file
            self.assertTrue(os.path.exists(path))
            app.remove_instance_file(path, 1234)
            self.assertFalse(os.path.exists(path))
            app.remove_instance_file(path, 1234)                    # already gone: fine

    def test_instance_file_rejects_garbage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "instance.json")
            self.assertIsNone(app.read_instance_file(path))
            for content in ("{bad", "[]", '{"pid": 1}', '{"pid": true, "port": 47811}',
                            '{"pid": 1, "port": 0}', '{"pid": 1, "port": "47811"}'):
                with self.subTest(content=content):
                    with open(path, "w", encoding="utf-8") as fh:
                        fh.write(content)
                    self.assertIsNone(app.read_instance_file(path))


class HandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.path = os.path.join(self.tmp, "instance.json")
        self.allowed: list[bool] = []
        self.acquire_calls = 0

    def allow(self) -> bool:
        self.allowed.append(True)
        return True

    def running_instance(self) -> tuple[fakes.FakeController, fakes.FakeIndexer]:
        """A real Server with fake collaborators, announced in instance.json."""
        controller, indexer = fakes.FakeController(), fakes.FakeIndexer()
        server = Server(Config(path=os.path.join(self.tmp, "config.json")), EventBus(),
                        indexer, fakes.FakeBridge(), controller,
                        web_dir=self.tmp, assets_dir=self.tmp)
        port = server.start(0)
        self.addCleanup(server.stop)
        app.write_instance_file(self.path, 4242, port)
        return controller, indexer

    def mutex_free_on(self, attempt: int):
        """Stand-in for SingleInstance.acquire: the other instance releases the mutex so that
        re-acquiring succeeds on call number ``attempt``."""
        def acquire() -> bool:
            self.acquire_calls += 1
            return self.acquire_calls >= attempt
        return acquire

    def handoff(self, **kwargs):
        kwargs.setdefault("background", False)
        kwargs.setdefault("allow_foreground", self.allow)
        return app.handoff_to_running_instance(self.path, **kwargs)

    def test_running_instance_shows_its_window(self) -> None:
        controller, indexer = self.running_instance()
        self.assertEqual(self.handoff(), 0)
        self.assertEqual(self.allowed, [True])
        self.assertEqual(controller.called("show_window"), [((None, "api"), {"panel": None})])
        self.assertEqual(indexer.called("scan_now"), [])

    def test_background_start_leaves_a_running_instance_alone(self) -> None:
        controller, indexer = self.running_instance()
        started = time.monotonic()
        self.assertEqual(self.handoff(background=True, reacquire=self.mutex_free_on(99)), 0)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual((self.allowed, controller.calls, indexer.calls), ([], [], []))

    def test_rescan_is_forwarded_to_the_running_instance(self) -> None:
        # DOC-1: "python -m projektsog --rescan" also works while Projektsøg runs.
        controller, indexer = self.running_instance()
        self.assertEqual(self.handoff(rescan=True), 0)
        self.assertEqual(indexer.called("scan_now"), [((None,), {"full": True})])
        self.assertEqual(len(controller.called("show_window")), 1)
        self.assertEqual(self.handoff(background=True, rescan=True), 0)
        self.assertEqual(len(indexer.called("scan_now")), 2)
        self.assertEqual(len(controller.called("show_window")), 1)     # background: no window

    def test_launch_during_exit_takes_over(self) -> None:
        # APP-2: the exiting instance answers 503; once it releases the mutex we run the app.
        controller, _ = self.running_instance()
        controller.exiting.set()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            self.assertIsNone(self.handoff(reacquire=self.mutex_free_on(3), timeout=10))
            gc.collect()
        self.assertEqual(controller.called("show_window"), [])
        self.assertEqual(self.acquire_calls, 3)
        # Known issue 7: every HTTPError is closed (no "Implicitly cleaning up" warning).
        self.assertEqual([w for w in caught if issubclass(w.category, ResourceWarning)], [])

    def test_missing_instance_file_waits_for_the_mutex(self) -> None:
        # An exiting instance deletes instance.json first; a --background start is not lost.
        for background in (False, True):
            with self.subTest(background=background):
                self.acquire_calls = 0
                self.assertIsNone(self.handoff(background=background, timeout=10,
                                               reacquire=self.mutex_free_on(2)))
                self.assertEqual(self.acquire_calls, 2)
        self.assertEqual(self.allowed, [True])
        started = time.monotonic()
        self.assertEqual(self.handoff(background=True, timeout=0.3,
                                      reacquire=self.mutex_free_on(99)), 0)
        self.assertLess(time.monotonic() - started, 2.0)

    def test_takes_over_the_real_mutex_once_the_old_instance_releases_it(self) -> None:
        name = f"Local\\Projektsog-test-{uuid.uuid4().hex}"
        old, new = app.SingleInstance(name), app.SingleInstance(name)
        self.assertTrue(old.acquire())
        self.addCleanup(old.release)
        self.assertFalse(new.acquire())
        self.addCleanup(new.release)
        releaser = threading.Timer(0.6, old.release)     # the end of the old exit sequence
        releaser.start()
        self.addCleanup(releaser.cancel)
        started = time.monotonic()
        self.assertIsNone(self.handoff(reacquire=new.acquire, timeout=10))
        self.assertGreaterEqual(time.monotonic() - started, 0.5)
        self.assertFalse(app.SingleInstance(name).acquire())   # the new launch holds it now

    def test_dropped_connections_are_retried(self) -> None:
        port = _resetting_listener(self)
        app.write_instance_file(self.path, 4242, port)
        with self.assertNoLogs("projektsog.app", "WARNING"):
            self.assertIsNone(self.handoff(reacquire=self.mutex_free_on(2), timeout=10))
        self.assertEqual(self.acquire_calls, 2)

    def test_other_http_errors_end_the_handoff(self) -> None:
        controller, _ = self.running_instance()
        controller.raises["show_window"] = ValueError("Nej")          # -> HTTP 400
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with self.assertLogs("projektsog.app", "WARNING"):
                started = time.monotonic()
                self.assertEqual(self.handoff(reacquire=self.mutex_free_on(99)), 1)
                self.assertLess(time.monotonic() - started, 2.0)
            gc.collect()
        self.assertEqual(self.acquire_calls, 0)
        self.assertEqual([w for w in caught if issubclass(w.category, ResourceWarning)], [])

    def test_gives_up_when_nothing_answers(self) -> None:
        with self.assertLogs("projektsog.app", "WARNING"):
            started = time.monotonic()
            self.assertEqual(app.handoff_to_running_instance(
                self.path, background=False, timeout=0.5, allow_foreground=self.allow), 1)
            with socket.socket() as probe:              # a port nobody listens on
                probe.bind(("127.0.0.1", 0))
                closed_port = probe.getsockname()[1]
            app.write_instance_file(self.path, 4242, closed_port)
            self.assertEqual(app.handoff_to_running_instance(
                self.path, background=False, timeout=0.5, allow_foreground=self.allow), 1)
        self.assertLess(time.monotonic() - started, 5.0)


def _resetting_listener(test: unittest.TestCase) -> int:
    """A port that accepts connections and resets them at once (a server going down)."""
    listener = socket.create_server(("127.0.0.1", 0))
    listener.settimeout(0.1)
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("hh", 1, 0))
            conn.close()                                # linger 0 -> RST

    thread = threading.Thread(target=serve, name="resetting-listener", daemon=True)
    thread.start()
    test.addCleanup(listener.close)
    test.addCleanup(thread.join, 2)
    test.addCleanup(stop.set)
    return listener.getsockname()[1]


class MainTests(unittest.TestCase):
    """main()'s single-instance decision, with every process-wide effect patched out."""

    def run_main(self, argv: list[str], handoff_result):
        runs: list[object] = []
        handoffs: list[dict] = []
        exits: list[int] = []

        class Instance:
            def __init__(self, name: str) -> None:
                self.name = name

            def acquire(self) -> bool:
                return False                        # another instance holds the mutex

        class FakeApp:
            def __init__(self, args, instance) -> None:
                runs.append(instance)

            def run(self) -> int:
                runs.append("run")
                return 0

        def handoff(path: str, **kwargs):
            handoffs.append(kwargs)
            return handoff_result

        with mock.patch.object(app, "harden_process"), \
                mock.patch.object(app, "SingleInstance", Instance), \
                mock.patch.object(app, "handoff_to_running_instance", handoff), \
                mock.patch.object(app, "App", FakeApp), \
                mock.patch.object(app.logging, "shutdown"), \
                mock.patch.object(app.os, "_exit", exits.append):
            code = app.main(argv)
        return code, runs, handoffs, exits

    def test_handed_over_launch_exits(self) -> None:
        code, runs, handoffs, exits = self.run_main([], 0)
        self.assertEqual((code, runs, exits), (0, [], []))
        self.assertEqual((handoffs[0]["background"], handoffs[0]["rescan"]), (False, False))

    def test_takes_over_when_the_running_instance_exited(self) -> None:
        # APP-2: the hand-off got the mutex (None) -> this process runs the app.
        code, runs, handoffs, exits = self.run_main(["--background", "--rescan"], None)
        self.assertEqual(runs[1:], ["run"])
        self.assertEqual(exits, [0])
        instance = runs[0]
        self.assertEqual(handoffs[0]["reacquire"], instance.acquire)
        self.assertEqual((handoffs[0]["background"], handoffs[0]["rescan"]), (True, True))


class CommandLineTests(unittest.TestCase):
    def test_build_run_command_quotes_both_paths(self) -> None:
        self.assertEqual(
            app.build_run_command("C:\\Program Files\\Python314\\pythonw.exe",
                                  "C:\\Mine projekter\\Search\\Projektsøg.pyw"),
            '"C:\\Program Files\\Python314\\pythonw.exe" '
            '"C:\\Mine projekter\\Search\\Projektsøg.pyw" --background')

    def test_run_command_splits_back_into_pythonw_launcher_background(self) -> None:
        argv = split_command_line(app.RUN_COMMAND)
        self.assertEqual(argv, [app.pythonw_path(), app.LAUNCHER, "--background"])
        self.assertEqual(os.path.dirname(argv[0]), os.path.dirname(sys.executable))
        self.assertEqual(os.path.basename(argv[0]).lower(), "pythonw.exe")
        self.assertEqual(argv[1], os.path.join(REPO, "Projektsøg.pyw"))
        self.assertTrue(os.path.isfile(argv[1]))

    def test_parse_args(self) -> None:
        defaults = app.parse_args([])
        self.assertEqual((defaults.background, defaults.no_window, defaults.port,
                          defaults.debug, defaults.rescan), (False, False, None, False, False))
        flags = app.parse_args(["--background", "--no-window", "--port", "48000", "--debug",
                                "--rescan"])
        self.assertEqual((flags.background, flags.no_window, flags.port, flags.debug,
                          flags.rescan), (True, True, 48000, True, True))
        with contextlib.redirect_stderr(io.StringIO()):
            for bad in (["--port", "70000"], ["--port", "x"], ["--bogus"]):
                with self.subTest(argv=bad):
                    with self.assertRaises(SystemExit):
                        app.parse_args(bad)

    def test_import_helper_leaves_no_stand_ins_behind(self) -> None:
        for short in ("hotkey", "indexer", "resolve_bridge", "tray", "window", "winfs", "winui"):
            name = f"projektsog.{short}"
            if importlib.util.find_spec(name) is None:
                self.assertNotIn(name, sys.modules)


class FakeInstance:
    def __init__(self, journal: list[str]) -> None:
        self.journal = journal

    def acquire(self) -> bool:
        return True

    def release(self) -> None:
        self.journal.append("instance.release")


class FakeUpdater:
    def __init__(self) -> None:
        self.started = self.closed = False

    def start(self) -> None:
        self.started = True

    def close(self) -> None:
        self.closed = True


class AppHarness:
    """An App whose collaborators are fakes that write into one shared journal."""

    def __init__(self, test: unittest.TestCase, *flags: str, hotkeys_start: bool = True,
                 settings: dict | None = None, exit_deadline_s: float = 5.0,
                 server_error: Exception | None = None, hotkey_recheck_s: float = 0.3,
                 hotkeys_ready_late: bool = False) -> None:
        tmp = tempfile.TemporaryDirectory()
        test.addCleanup(tmp.cleanup)
        self.config_path = os.path.join(tmp.name, "config.json")
        if settings:
            Config(path=self.config_path).update(settings)
        self.journal: list[str] = []
        self.hotkeys_start = hotkeys_start
        self.hotkeys_ready_late = hotkeys_ready_late
        self.server_error = server_error
        self.managers: list[fakes.FakeHotkeys] = []
        self.hotkey_args: list[tuple] = []
        self.run_key: list[bool] = []
        self.instance = FakeInstance(self.journal)
        components = app.Components(
            config=self._config, indexer=self._indexer, bridge=self._bridge,
            tracker=self._tracker, importer=self._importer,
            controller=self._controller, server=self._server, window=self._window, widget=self._widget,
            petplay=self._petplay, tray=self._tray, hotkeys=self._hotkeys, updater=self._updater)
        self.app = app.App(app.parse_args(list(flags)), self.instance, components=components,
                           exit_deadline_s=exit_deadline_s, hotkey_recheck_s=hotkey_recheck_s)
        test.addCleanup(self.app.exit_sequence)     # stops the app's threads

    def _config(self) -> Config:
        self.journal.append("config")
        self.cfg = Config(path=self.config_path)
        return self.cfg

    def _indexer(self, cfg, bus) -> fakes.FakeIndexer:
        self.journal.append("indexer.create")
        self.indexer = fakes.FakeIndexer(self.journal, "indexer")
        return self.indexer

    def _bridge(self, cfg, bus, indexer) -> fakes.FakeBridge:
        self.journal.append("bridge.create")
        self.bridge = fakes.FakeBridge(self.journal, "bridge")
        return self.bridge

    def _tracker(self, cfg, bridge) -> fakes.FakeTracker:
        self.journal.append("tracker.create")
        assert bridge is self.bridge                 # the tracker reads Resolve via the bridge
        self.tracker = fakes.FakeTracker(self.journal, "tracker")
        return self.tracker

    def _importer(self, cfg, bus, indexer, bridge, tracker, controller) -> fakes.FakeImporter:
        self.journal.append("importer.create")
        assert (indexer, bridge, tracker, controller) == (self.indexer, self.bridge, self.tracker,
                                                          self.controller)
        self.importer = fakes.FakeImporter(self.journal, "importer")
        return self.importer

    def _controller(self, cfg, bus, indexer, bridge, *, request_exit):
        self.journal.append("controller.create")
        self.controller = app.Controller(
            cfg, bus, indexer, bridge, request_exit=request_exit,
            call_with_timeout=lambda key, fn, timeout: ("ok", fn()),
            open_folder=lambda path, activate=True: True, reveal=lambda path, activate=True: True,
            open_file=lambda path: True, get_run_at_login=lambda: bool(self.run_key[-1:] == [True]),
            set_run_at_login=lambda enabled, command: self.run_key.append(enabled),
            format_hotkey=lambda spec: spec.title())
        return self.controller

    def _server(self, cfg, bus, indexer, bridge, controller, *, parse_hotkey, tracker, importer):
        self.journal.append("server.create")
        assert tracker is self.tracker               # the server reports the tracker's time
        assert importer is self.importer             # and serves the import helper
        if self.server_error is not None:
            raise self.server_error
        self.server = fakes.FakeServer(self.journal, "server")
        return self.server

    def _window(self, url: str, profile_dir: str) -> fakes.FakeWindow:
        self.journal.append("window.create")
        self.window_args = (url, profile_dir)
        self.instance_file_at_window = app.read_instance_file(config.instance_path())
        self.window = fakes.FakeWindow(self.journal, "window")
        return self.window

    def _widget(self, cfg, url: str) -> fakes.FakeWidget:
        self.journal.append("widget.create")
        assert url.endswith("/widget.html")
        self.widget = fakes.FakeWidget(self.journal, "widget")
        return self.widget

    def _petplay(self, cfg, bus, *, widget, bridge, importer, base_url: str, wardrobe, on_game) -> fakes.FakePetPlay:
        self.journal.append("petplay.create")
        assert (widget, bridge, importer) == (self.widget, self.bridge, self.importer)
        assert base_url == "http://127.0.0.1:4711"
        self.petplay = fakes.FakePetPlay(self.journal, "petplay")
        return self.petplay

    def _updater(self, cfg, bus, *, repo_dir: str, data_dir: str, autostart) -> "FakeUpdater":
        assert repo_dir == app.REPO_DIR and autostart == self.controller.get_run_at_login
        self.updater = FakeUpdater()          # never asks GitHub from a test
        return self.updater

    def _tray(self, icon_path: str, tooltip: str, **callbacks) -> fakes.FakeTray:
        self.journal.append("tray.create")
        self.tray_args = (icon_path, tooltip)
        self.tray_callbacks = callbacks
        self.tray = fakes.FakeTray(self.journal, "tray")
        return self.tray

    def _hotkeys(self, spec: str, callback, **options) -> fakes.FakeHotkeys:
        self.journal.append("hotkeys.create")
        self.hotkey_args.append((spec, callback, options))
        manager = fakes.FakeHotkeys(self.journal, "hotkeys", active=self.hotkeys_start)
        manager.returns["start"] = self.hotkeys_start
        if self.hotkeys_ready_late:     # start() timed out, the helper got ready right after
            manager.returns["start"] = lambda: setattr(manager, "active", True) or False
        self.managers.append(manager)
        return manager

    def wait_for(self, entry: str, timeout: float = 3.0) -> None:
        if not fakes.wait_until(lambda: entry in self.journal, timeout):
            raise AssertionError(f"{entry!r} never happened: {self.journal}")


class EventLog:
    """Collects bus events for assertions."""

    def __init__(self, bus: EventBus) -> None:
        self._queue = bus.subscribe()
        self._items: list[tuple[str, object]] = []

    def of(self, event_type: str) -> list:
        while True:
            try:
                kind, data, _ts = self._queue.get_nowait()
            except queue.Empty:
                break
            self._items.append((kind, data))
        return [data for kind, data in self._items if kind == event_type]

    def wait(self, event_type: str, predicate=lambda data: True, timeout: float = 3.0) -> bool:
        return fakes.wait_until(lambda: any(predicate(d) for d in self.of(event_type)), timeout)


class AppStartupTests(unittest.TestCase):
    STARTUP = ["config", "indexer.create", "indexer.start", "bridge.create", "bridge.start",
               "tracker.create", "tracker.start",
               "controller.create", "importer.create", "server.create", "server.start",
               "window.create", "widget.create", "petplay.create", "widget.start", "petplay.start",
               "tray.create", "tray.start", "hotkeys.create", "hotkeys.start",
               "importer.start"]

    def test_startup_order_and_wiring(self) -> None:
        h = AppHarness(self)
        h.app.start()
        h.wait_for("bridge.on_window_shown")
        self.assertEqual(h.journal[:len(self.STARTUP)], self.STARTUP)
        self.assertEqual(h.journal[len(self.STARTUP):],
                         ["window.show", "hotkeys.end_capture", "indexer.on_window_shown",
                          "bridge.on_window_shown"])
        self.assertEqual(h.server.called("start"), [((None,), {})])
        self.assertEqual(h.window_args, ("http://127.0.0.1:4711/", config.edge_profile_dir()))
        self.assertEqual(h.instance_file_at_window, {"pid": os.getpid(), "port": 4711})
        self.assertEqual(h.tray_args, (app.ICON_PATH, "Projektsøg"))
        self.assertEqual(sorted(h.tray_callbacks), [
            "menu_state", "on_exit", "on_scan_all", "on_set_autostart", "on_set_follow",
            "on_settings", "on_show"])
        spec, callback, options = h.hotkey_args[0]
        self.assertEqual(spec, "shift+space")
        self.assertEqual(callback, h.controller.on_hotkey)
        self.assertEqual(options, {"passthrough_apps": [], "typing_guard_ms": 300,
                                   "double_tap_ms": 400, "enabled": True})
        self.assertIs(h.controller.window, h.window)
        self.assertIs(h.controller.hotkeys, h.managers[0])

    def test_background_preloads_instead_of_showing(self) -> None:
        h = AppHarness(self, "--background")
        h.app.start()
        h.wait_for("window.preload")
        self.assertNotIn("window.show", h.journal)

    def test_no_window_mode_is_headless(self) -> None:
        h = AppHarness(self, "--no-window", "--rescan", "--port", "48000")
        h.app.start()
        for absent in ("window.create", "tray.create", "hotkeys.create"):
            self.assertNotIn(absent, h.journal)
        self.assertEqual(h.indexer.called("scan_now"), [((None,), {"full": True})])
        self.assertLess(h.journal.index("indexer.start"), h.journal.index("indexer.scan_now"))
        self.assertEqual(h.server.called("start"), [((48000,), {})])
        self.assertEqual(app.read_instance_file(config.instance_path()),
                         {"pid": os.getpid(), "port": 4711})

    def test_disabled_hotkey_is_not_started(self) -> None:
        h = AppHarness(self, "--background", settings={"hotkey_enabled": False})
        h.app.start()
        self.assertNotIn("hotkeys.create", h.journal)
        self.assertEqual(h.controller.hotkey_status()["active"], False)

    def test_hotkey_failure_is_notified_once(self) -> None:
        h = AppHarness(self, "--background", hotkeys_start=False)
        with self.assertLogs("projektsog.app", "WARNING"):
            h.app.start()
            self.assertTrue(h.tray.notified.wait(3))
            (title, text, level), _ = h.tray.called("notify")[0]
            self.assertEqual((title, level), ("Projektsøg", "warn"))
            self.assertIn("Genvejstasten Shift+Space kunne ikke aktiveres", text)
            h.cfg.update({"hotkey_enabled": False})
            h.wait_for("hotkeys.stop")
            h.cfg.update({"hotkey_enabled": True})
            self.assertTrue(fakes.wait_until(lambda: len(h.managers) == 2))
            self.assertTrue(fakes.wait_until(lambda: h.managers[1].called("start")))
            time.sleep(0.6)                             # past the second re-check
        self.assertEqual(len(h.tray.called("notify")), 1)

    def test_inactive_hotkey_is_rechecked_before_notifying(self) -> None:
        # Known issue 5 / SPEC §15.11: the helper may come up late (e.g. right after login).
        h = AppHarness(self, "--background", hotkeys_start=False, hotkey_recheck_s=0.6)
        started = time.monotonic()
        with self.assertLogs("projektsog.app", "WARNING") as logs:
            h.app.start()
            self.assertTrue(h.tray.notified.wait(3))
        self.assertGreaterEqual(time.monotonic() - started, 0.55)   # not before the re-check
        self.assertEqual(len(h.tray.called("notify")), 1)
        self.assertIn("still not active", "\n".join(logs.output))

    def test_hotkey_that_comes_up_late_is_not_notified(self) -> None:
        # start() gave up waiting (False), but the helper is ready by the re-check.
        h = AppHarness(self, "--background", hotkeys_start=False, hotkeys_ready_late=True,
                       hotkey_recheck_s=0.3)
        with self.assertLogs("projektsog.app", "INFO") as logs:
            h.app.start()
            self.assertTrue(fakes.wait_until(
                lambda: any("became active" in line for line in logs.output)))
        self.assertEqual(h.tray.called("notify"), [])
        self.assertNotIn("still not active", "\n".join(logs.output))

    def test_run_returns_1_and_cleans_up_when_startup_fails(self) -> None:
        h = AppHarness(self, server_error=OSError(10048, "port in use"))
        with self.assertLogs("projektsog.app", "ERROR"):
            self.assertEqual(h.app.run(), 1)
        for entry in ("bridge.stop", "indexer.stop", "instance.release"):
            self.assertIn(entry, h.journal)
        self.assertIsNone(app.read_instance_file(config.instance_path()))


class AppRuntimeTests(unittest.TestCase):
    def test_config_changes_reach_the_hotkey_manager(self) -> None:
        h = AppHarness(self, "--background")
        h.app.start()
        events = EventLog(h.app.bus)
        manager = h.managers[0]
        h.cfg.update({"hotkey": "ctrl+space"})
        self.assertTrue(fakes.wait_until(lambda: manager.called("update")))
        self.assertEqual(manager.called("update"), [((), {"spec": "ctrl+space"})])
        self.assertTrue(events.wait("hotkey", lambda s: s["spec"] == "ctrl+space"
                                    and s["label"] == "Ctrl+Space"))
        settings = events.of("settings")
        self.assertEqual(settings[-1]["hotkey"], "ctrl+space")
        self.assertIs(settings[-1]["run_at_login"], False)

        h.cfg.update({"hotkey_passthrough_apps": ["Resolve.exe", "Fusion.exe"]})
        self.assertTrue(fakes.wait_until(lambda: len(manager.called("update")) == 2))
        self.assertEqual(manager.called("update")[1],
                         ((), {"passthrough_apps": ["Resolve.exe", "Fusion.exe"]}))

        h.cfg.update({"hotkey_enabled": False})
        h.wait_for("hotkeys.stop")
        self.assertTrue(events.wait("hotkey", lambda s: not s["enabled"] and not s["active"]))
        self.assertIsNone(h.controller.hotkeys)

        h.cfg.update({"hotkey_enabled": True})
        self.assertTrue(fakes.wait_until(lambda: len(h.managers) == 2))
        self.assertTrue(fakes.wait_until(lambda: h.managers[1].called("start")))
        self.assertEqual(h.hotkey_args[1][0], "ctrl+space")
        self.assertEqual(h.hotkey_args[1][2]["passthrough_apps"], ["Resolve.exe", "Fusion.exe"])

    def test_refused_hotkey_update_is_notified(self) -> None:
        h = AppHarness(self, "--background")
        h.app.start()
        h.managers[0].raises["update"] = ValueError("typing_guard_ms skal være et ikke-negativt "
                                                    "antal millisekunder")
        with self.assertLogs("projektsog.app", "WARNING"):
            h.cfg.update({"hotkey_typing_guard_ms": 250})
            self.assertTrue(h.tray.notified.wait(3))
        self.assertEqual(h.managers[0].called("update"), [((), {"typing_guard_ms": 250})])

    def test_unrelated_setting_only_publishes_settings(self) -> None:
        h = AppHarness(self, "--background")
        h.app.start()
        events = EventLog(h.app.bus)
        h.cfg.update({"show_offline": False})
        self.assertTrue(events.wait("settings", lambda s: s["show_offline"] is False))
        self.assertEqual(h.managers[0].called("update"), [])

    def test_notify_events_go_to_the_tray(self) -> None:
        h = AppHarness(self, "--background")
        h.app.start()
        h.app.bus.publish("status", {"sources_online": 3})
        h.app.bus.publish("notify", {"title": "DaVinci Resolve: Rikke Lindholm - Testimonial",
                                     "text": "Projektmappe: Rikke Lindholm", "level": "info"})
        self.assertTrue(h.tray.notified.wait(3))
        self.assertEqual(h.tray.called("notify"), [(
            ("DaVinci Resolve: Rikke Lindholm - Testimonial", "Projektmappe: Rikke Lindholm", "info"),
            {})])

    def test_tray_callbacks_return_at_once_and_act_in_the_background(self) -> None:
        h = AppHarness(self, "--background")
        h.app.start()
        events = EventLog(h.app.bus)
        cb = h.tray_callbacks
        h.window.delays["show"] = 0.3
        started = time.monotonic()
        cb["on_show"]()
        cb["on_settings"]()
        cb["on_scan_all"]()
        cb["on_set_follow"]("open")
        cb["on_set_autostart"](True)
        self.assertLess(time.monotonic() - started, 0.05)
        self.assertTrue(fakes.wait_until(lambda: h.run_key == [True]))
        self.assertEqual(h.cfg["resolve_follow"], "open")
        self.assertEqual(h.indexer.called("scan_now"), [((None,), {"full": False})])
        self.assertEqual(events.of("focus"), [
            {"from_app": None, "reason": "tray"},
            {"from_app": None, "reason": "tray", "panel": "settings"}])
        self.assertEqual(cb["menu_state"](), {"hotkey_label": "Shift+Space", "follow": "open",
                                              "autostart": True})
        cb["on_exit"]()
        waiter = threading.Thread(target=h.app.wait_for_exit)
        waiter.start()
        waiter.join(3)
        self.assertFalse(waiter.is_alive())

    def test_quit_request_ends_run(self) -> None:
        h = AppHarness(self)
        result: list[int] = []
        runner = threading.Thread(target=lambda: result.append(h.app.run()))
        runner.start()
        h.wait_for("window.show")
        h.controller.request_exit()                     # what POST /api/quit does
        runner.join(5)
        self.assertEqual(result, [0])
        self.assertIn("window.close", h.journal)


class ExitSequenceTests(unittest.TestCase):
    EXIT = ["hotkeys.stop", "tray.stop", "importer.stop", "tracker.stop", "bridge.stop", "indexer.stop",
            "server.stop", "petplay.close", "widget.close", "window.close", "instance.release"]

    def test_exit_order_and_cleanup(self) -> None:
        h = AppHarness(self)
        h.app.start()
        h.wait_for("bridge.on_window_shown")
        mark = len(h.journal)
        self.assertTrue(h.updater.started)
        h.app.request_exit()
        h.app.exit_sequence()
        self.assertEqual(h.journal[mark:], self.EXIT)
        self.assertTrue(h.updater.closed)
        (_, kwargs), = h.indexer.called("stop")
        self.assertAlmostEqual(kwargs["timeout"], 1.5)
        self.assertIsNone(h.controller.hotkeys)
        self.assertFalse(os.path.exists(config.instance_path()))

    def test_exiting_app_stops_announcing_itself_first(self) -> None:
        # APP-2: instance.json goes before any step runs, the Controller refuses new shows,
        # and the mutex is released last.
        h = AppHarness(self)
        h.app.start()
        h.wait_for("bridge.on_window_shown")
        self.assertTrue(os.path.exists(config.instance_path()))
        seen: list[bool] = []
        h.managers[0].returns["stop"] = lambda: seen.append(os.path.exists(config.instance_path()))
        h.app.request_exit()
        self.assertTrue(h.controller.exiting.is_set())
        h.app.exit_sequence()
        self.assertEqual(seen, [False])
        self.assertEqual(h.journal[-1], "instance.release")
        self.assertFalse(h.controller.show_window(reason="api"))

    def test_a_hanging_step_cannot_block_the_rest(self) -> None:
        h = AppHarness(self, "--background", exit_deadline_s=1.0)
        h.app.start()
        h.indexer.delays["stop"] = 3.0
        started = time.monotonic()
        with self.assertLogs("projektsog.app", "WARNING"):
            h.app.exit_sequence()
        self.assertLess(time.monotonic() - started, 1.5)
        for entry in ("server.stop", "window.close", "instance.release"):
            self.assertIn(entry, h.journal)
        self.assertFalse(os.path.exists(config.instance_path()))
        del h.indexer.delays["stop"]                    # the cleanup runs the sequence again


class HardenProcessTests(unittest.TestCase):
    def test_pythonw_conditions_are_handled(self) -> None:
        script = textwrap.dedent("""
            import json, logging, os, sys, threading
            sys.path.insert(0, sys.argv[1])
            from tests._app_fakes import import_app
            app = import_app()
            sys.stdout = sys.stderr = None              # as under pythonw.exe
            app.harden_process(debug=False)
            print("stdout is usable")                  # must not raise
            logging.getLogger("probe").info("hello from the probe")
            worker = threading.Thread(target=lambda: 1 / 0, name="doomed")
            worker.start()
            worker.join()
            with open(sys.argv[2], "w", encoding="utf-8") as fh:
                json.dump({"cwd": os.getcwd(),
                           "streams": [sys.stdout is not None, sys.stderr is not None],
                           "error_mode": app._kernel32.GetErrorMode()}, fh)
        """)
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "result.json")
            env = dict(os.environ, LOCALAPPDATA=tmp)
            subprocess.run([sys.executable, "-c", script, REPO, out], env=env, check=True,
                           capture_output=True, timeout=60,
                           creationflags=subprocess.CREATE_NO_WINDOW)
            with open(out, encoding="utf-8") as fh:
                result = json.load(fh)
            app_dir = os.path.join(tmp, "Projektsog")
            self.assertEqual(os.path.normcase(result["cwd"]), os.path.normcase(app_dir))
            self.assertEqual(result["streams"], [True, True])
            self.assertEqual(result["error_mode"] & 0x8001, 0x8001)
            with open(os.path.join(app_dir, "logs", "projektsog.log"), encoding="utf-8") as fh:
                log_text = fh.read()
            self.assertIn("hello from the probe", log_text)
            self.assertIn("Unhandled exception in thread doomed", log_text)
            self.assertIn("ZeroDivisionError", log_text)
            with open(os.path.join(app_dir, "logs", "crash.log"), encoding="utf-8") as fh:
                self.assertIn("Projektsøg 1.0.0 started", fh.read())


if __name__ == "__main__":
    unittest.main()
