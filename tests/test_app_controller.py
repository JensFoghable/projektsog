"""Tests for projektsog.app.Controller (SPEC §11 open_path, §13 window/hotkey rules) – fakes only."""

import ntpath
import os
import queue
import string
import tempfile
import time
import unittest
from unittest import mock

from projektsog.config import Config
from projektsog.events import EventBus
from tests import _app_fakes as fakes

app = None      # projektsog.app, imported once LOCALAPPDATA points to a temp dir
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


def _location(online: bool, **source) -> dict:
    ref = {"id": 5, "name": "Kunder 2026 (STUDIO)", "host": "STUDIO-PC", "kind": "local",
           "online": online, "drive": "C:", "disk_name": "Lokal disk", "volume_label": None,
           "last_seen": None}
    ref.update(source)
    return {"source": ref, "rel_path": "Rikke Lindholm", "path": "C:\\x", "unc_path": None,
            "online": online, "entry": None, "project": None}


class ControllerTestBase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.cfg = Config(path=os.path.join(self.tmp, "config.json"))
        self.bus = EventBus()
        self.events = self.bus.subscribe()
        self.journal: list[str] = []
        self.indexer = fakes.FakeIndexer(self.journal, "indexer")
        self.bridge = fakes.FakeBridge(self.journal, "bridge")
        self.window = fakes.FakeWindow(self.journal, "window")
        self.hotkeys = fakes.FakeHotkeys(self.journal, "hotkeys")
        self.stat_calls: list[tuple[str, float]] = []
        self.stat_result: tuple[str, object] | None = None      # None = really run the probe
        self.opened: list[tuple] = []
        self.open_ok = True
        self.run_key: list[tuple[bool, str]] = []
        self.run_key_enabled = False
        self.exit_requests = 0
        self.controller = app.Controller(
            self.cfg, self.bus, self.indexer, self.bridge, window=self.window,
            hotkeys=self.hotkeys, request_exit=self._request_exit,
            call_with_timeout=self._call_with_timeout, open_folder=self._open_folder,
            reveal=self._reveal, open_file=self._open_file,
            get_run_at_login=lambda: self.run_key_enabled,
            set_run_at_login=self._set_run_at_login, format_hotkey=self._format_hotkey,
            run_command='"C:\\Py\\pythonw.exe" "C:\\Repo\\Projektsøg.pyw" --background')

    # -- fakes of winfs / winui / hotkey functions -----------------------------------------
    def _call_with_timeout(self, key: str, fn, timeout: float) -> tuple[str, object]:
        self.stat_calls.append((key, timeout))
        self.journal.append("stat")
        return self.stat_result if self.stat_result is not None else ("ok", fn())

    def _open_folder(self, path: str, activate: bool = True) -> bool:
        self.journal.append("open_folder")
        self.opened.append(("folder", path, activate))
        return self.open_ok

    def _reveal(self, path: str, activate: bool = True) -> bool:
        self.journal.append("reveal")
        self.opened.append(("reveal", path, activate))
        return self.open_ok

    def _open_file(self, path: str) -> bool:
        self.journal.append("open_file")
        self.opened.append(("file", path))
        return self.open_ok

    def _set_run_at_login(self, enabled: bool, command: str) -> None:
        self.run_key.append((enabled, command))
        self.run_key_enabled = enabled

    def _format_hotkey(self, spec: str) -> str:
        if spec == "bogus":
            raise ValueError("Ugyldig genvejstast")
        return {"shift+space": "Shift+Mellemrum"}.get(spec, spec.upper())

    def _request_exit(self) -> None:
        self.exit_requests += 1

    def published(self, event_type: str) -> list:
        items = []
        while True:
            try:
                kind, data, _ts = self.events.get_nowait()
            except queue.Empty:
                return items
            if kind == event_type:
                items.append(data)


class OpenPathTests(ControllerTestBase):
    def existing_dir(self) -> str:
        path = os.path.join(self.tmp, "Rikke Lindholm")
        os.makedirs(os.path.join(path, "Klip"), exist_ok=True)
        return path

    def existing_file(self) -> str:
        path = os.path.join(self.existing_dir(), "FX9_7912.MXF")
        with open(path, "wb") as fh:
            fh.write(b"\0")
        return path

    def test_rejects_unknown_actions_and_non_absolute_paths(self) -> None:
        for action in ("run", "", None, "FOLDER"):
            with self.subTest(action=action):
                with self.assertRaises(ValueError):
                    self.controller.open_path("C:\\Kunder", action)
        for path in ("Kunder\\Rikke", "C:Kunder", "\\Kunder", "", None, 5):
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    self.controller.open_path(path, "folder")
        self.assertEqual(self.opened, [])

    def test_offline_disk_gives_hint_without_filesystem_access(self) -> None:
        self.indexer.returns["locate"] = _location(False, kind="local",
                                                   disk_name="2024 Disk Sølv", drive=None)
        result = self.controller.open_path("H:\\2024 Disk Sølv\\Pixelbro Radio", "folder")
        self.assertEqual(result, {"ok": False, "error": "Tilslut disken ‘2024 Disk Sølv’"})
        self.assertEqual(self.stat_calls, [])
        self.assertEqual(self.opened, [])
        self.assertEqual(self.indexer.called("path_missing"), [])
        self.assertEqual(self.window.called("hide"), [])

    def test_offline_share_gives_host_hint(self) -> None:
        self.indexer.returns["locate"] = _location(False, kind="share", host="GRAFIK-PC")
        result = self.controller.open_path("\\\\GRAFIK-PC\\Forår 2026 (HDD)\\Klar Tand",
                                           "reveal")
        self.assertEqual(result, {"ok": False,
                                  "error": "Computeren GRAFIK-PC svarer ikke – er den tændt?"})
        self.assertEqual(self.stat_calls, [])

    def test_offline_folder_on_a_present_volume_says_the_folder_is_gone(self) -> None:
        # SPEC §15.12: the disk is mounted (the computer answers) – asking to connect the disk
        # (e.g. 'Systemdisk') would mislead; the folder itself was moved, renamed or deleted.
        for source in ({"kind": "local", "disk_name": "Systemdisk", "is_system": True},
                       {"kind": "local", "disk_name": "Forår 2026 RØD", "drive": "D:"},
                       {"kind": "share", "host": "GRAFIK-PC"}):
            with self.subTest(source=source):
                self.indexer.returns["locate"] = _location(False, volume_present=True, **source)
                self.assertEqual(self.controller.open_path("C:\\Kunder 2026 (STUDIO)\\Rikke",
                                                           "folder"),
                                 {"ok": False, "error": "Mappen findes ikke længere"})
        self.indexer.returns["locate"] = _location(False, volume_present=False, kind="local",
                                                   disk_name="2024 Disk Sølv")
        self.assertEqual(self.controller.open_path("H:\\Pixelbro", "folder"),
                         {"ok": False, "error": "Tilslut disken ‘2024 Disk Sølv’"})
        self.assertEqual(self.stat_calls, [])
        self.assertEqual((self.opened, self.indexer.called("path_missing")), ([], []))

    def test_missing_path_is_reported_to_the_indexer(self) -> None:
        path = os.path.join(self.existing_dir(), "Slettet mappe")
        self.indexer.returns["locate"] = _location(True)
        result = self.controller.open_path(path, "folder")
        self.assertEqual(result, {"ok": False, "error": "Findes ikke længere – indekset opdateres"})
        self.assertEqual(self.indexer.called("path_missing"), [((path,), {})])
        self.assertEqual(self.opened, [])
        self.assertEqual(self.window.called("hide"), [])

    def test_unresponsive_location_is_not_reported_missing(self) -> None:
        path = self.existing_dir()
        for outcome in (("timeout", None), ("busy", None), ("error", None), ("ok", "error")):
            with self.subTest(outcome=outcome):
                self.stat_result = outcome
                self.assertEqual(self.controller.open_path(path, "folder"),
                                 {"ok": False, "error": "Placeringen svarer ikke"})
        self.assertEqual(self.indexer.called("path_missing"), [])
        self.assertEqual(self.opened, [])

    def test_missing_drive_counts_as_unreachable(self) -> None:
        used = {d[0].upper() for d in os.listdrives()}
        free = next(letter for letter in reversed(string.ascii_uppercase[3:])
                    if letter not in used)
        result = self.controller.open_path(f"{free}:\\Kunder 2026\\Rikke Lindholm", "folder")
        self.assertEqual(result, {"ok": False, "error": "Placeringen svarer ikke"})
        self.assertEqual(self.indexer.called("path_missing"), [])

    def test_open_folder_then_hide(self) -> None:
        path = self.existing_dir()
        self.indexer.returns["locate"] = _location(True)
        self.assertEqual(self.controller.open_path(path, "folder"), {"ok": True, "path": path})
        self.assertEqual(self.opened, [("folder", path, True)])
        self.assertEqual(self.stat_calls, [("open:source:5", 3.0)])
        self.assertEqual(self.journal, ["indexer.locate", "stat", "open_folder", "window.hide"])
        self.assertEqual(self.window.called("hide"), [((), {"restore_previous": False})])

    def test_unknown_path_is_keyed_by_drive(self) -> None:
        path = self.existing_dir()
        self.controller.open_path(path, "folder")
        self.assertEqual(self.stat_calls,
                         [("open:" + ntpath.splitdrive(path)[0].casefold(), 3.0)])

    def test_no_hide_when_disabled_or_when_opening_failed(self) -> None:
        path = self.existing_dir()
        self.cfg.update({"hide_after_open": False})
        self.assertTrue(self.controller.open_path(path, "folder")["ok"])
        self.cfg.update({"hide_after_open": True})
        self.open_ok = False
        self.assertEqual(self.controller.open_path(path, "folder"),
                         {"ok": False, "error": "Mappen kunne ikke åbnes"})
        self.assertEqual(self.window.called("hide"), [])

    def test_reveal_and_file_actions(self) -> None:
        file_path = self.existing_file()
        self.assertTrue(self.controller.open_path(file_path, "reveal")["ok"])
        self.assertTrue(self.controller.open_path(file_path, "file")["ok"])
        self.assertEqual(self.opened, [("reveal", file_path, True), ("file", file_path)])

    def test_folder_action_never_opens_a_file(self) -> None:
        file_path = self.existing_file()
        self.assertTrue(self.controller.open_path(file_path, "folder")["ok"])
        self.assertEqual(self.opened, [("reveal", file_path, True)])

    def test_file_action_on_a_folder_opens_the_folder(self) -> None:
        path = self.existing_dir()
        self.assertTrue(self.controller.open_path(path, "file")["ok"])
        self.assertEqual(self.opened, [("folder", path, True)])

    def test_missing_error_classification(self) -> None:
        def err(winerror: int) -> OSError:
            return OSError(None, "x", "p", winerror)
        self.assertTrue(app.is_missing_error(err(2)))           # ERROR_FILE_NOT_FOUND
        self.assertTrue(app.is_missing_error(err(3)))           # ERROR_PATH_NOT_FOUND
        self.assertFalse(app.is_missing_error(err(53)))         # ERROR_BAD_NETPATH
        self.assertFalse(app.is_missing_error(err(67)))         # ERROR_BAD_NET_NAME
        self.assertFalse(app.is_missing_error(err(21)))         # ERROR_NOT_READY
        self.assertFalse(app.is_missing_error(err(5)))          # access denied
        self.assertTrue(app.is_missing_error(FileNotFoundError("x")))

    def test_long_paths_are_statted_in_extended_form(self) -> None:
        long_local = "C:\\" + "\\".join(["Mappe med et langt navn"] * 12)
        long_unc = "\\\\GRAFIK-PC\\Forår 2026 (HDD)\\" + "\\".join(["Undermappe"] * 25)
        self.assertEqual(app._extended_path(long_local), "\\\\?\\" + long_local)
        self.assertEqual(app._extended_path(long_unc), "\\\\?\\UNC\\" + long_unc[2:])
        self.assertEqual(app._extended_path("C:\\Kunder"), "C:\\Kunder")


class WindowAndHotkeyTests(ControllerTestBase):
    def test_show_window_sequence_and_focus_event(self) -> None:
        self.assertTrue(self.controller.show_window("Resolve.exe", "hotkey"))
        self.assertEqual(self.journal, ["window.needs_launch", "window.show",
                                        "hotkeys.end_capture", "indexer.on_window_shown",
                                        "bridge.on_window_shown"])
        self.assertEqual(self.hotkeys.called("end_capture"), [((True,), {})])
        self.assertEqual(self.hotkeys.called("extend_capture"), [])     # warm window
        self.assertEqual(self.published("focus"), [{"from_app": "Resolve.exe", "reason": "hotkey"}])

    def test_cold_launch_keeps_the_captured_keys_first(self) -> None:
        # Known issue 6 / SPEC §15.10: Edge must start → extend the capture before show().
        self.window.returns["needs_launch"] = True
        self.assertTrue(self.controller.show_window(None, "hotkey"))
        self.assertEqual(self.journal[:4], ["window.needs_launch", "hotkeys.extend_capture",
                                            "window.show", "hotkeys.end_capture"])
        self.assertEqual(self.hotkeys.called("extend_capture"), [((app.CAPTURE_EXTEND_S,), {})])
        self.assertEqual(self.hotkeys.called("end_capture"), [((True,), {})])

    def test_a_slow_cold_show_keeps_extending_the_capture(self) -> None:
        # R2-WIN-1: one extension reaches only CAPTURE_EXTEND_S ahead, show() may take 10 s.
        self.window.returns["needs_launch"] = True
        self.window.delays["show"] = 0.35
        with mock.patch.object(app, "CAPTURE_HEARTBEAT_S", 0.1):
            self.assertTrue(self.controller.show_window(None, "hotkey"))
            time.sleep(0.3)                                     # nothing after end_capture
        extends = self.hotkeys.called("extend_capture")
        self.assertGreaterEqual(len(extends), 3)
        self.assertEqual(extends, [((app.CAPTURE_EXTEND_S,), {})] * len(extends))
        self.assertEqual(self.hotkeys.calls[-1], ("end_capture", (True,), {}))
        self.assertLessEqual(app.CAPTURE_EXTEND_S, 5)          # the hook child's cap per request
        self.assertLess(app.CAPTURE_HEARTBEAT_S, app.CAPTURE_EXTEND_S - 2)

    def test_the_heartbeat_stops_when_the_capture_could_not_last_anyway(self) -> None:
        # The child ends every capture 12 s after the fire; a show() stuck longer than that
        # gets no endless heartbeat thread.
        self.window.returns["needs_launch"] = True
        self.window.delays["show"] = 0.6
        stamps: list[float] = []
        self.hotkeys.returns["extend_capture"] = lambda seconds: stamps.append(time.monotonic())
        with mock.patch.multiple(app, CAPTURE_HEARTBEAT_S=0.05, CAPTURE_HEARTBEAT_MAX_S=0.2):
            started = time.monotonic()
            self.controller.show_window(None, "hotkey")
        self.assertGreaterEqual(len(stamps), 2)
        self.assertLess(stamps[-1] - started, 0.2 + 0.15)

    def test_replay_waits_for_the_shown_page(self) -> None:
        # Known issue 6: ~100 ms between a successful show and end_capture(True).
        stamps: dict[str, float] = {}
        self.window.returns["show"] = lambda: stamps.setdefault("show", time.monotonic()) > 0
        self.hotkeys.returns["end_capture"] = (
            lambda ok: stamps.setdefault("end_capture", time.monotonic()))
        self.controller.show_window(None, "hotkey")
        self.assertGreaterEqual(stamps["end_capture"] - stamps["show"], 0.09)

    def test_failed_or_non_hotkey_shows_do_not_wait_or_extend(self) -> None:
        self.window.returns["needs_launch"] = True
        with mock.patch.object(app, "time", wraps=time) as clock:
            for reason in ("tray", "api", "launch"):
                self.controller.show_window(reason=reason)
            self.window.returns["show"] = False
            self.assertFalse(self.controller.show_window(None, "hotkey"))
        self.assertEqual(clock.sleep.call_args_list, [])                 # no replay delay
        self.assertEqual(len(self.window.called("needs_launch")), 1)     # the hotkey show only
        self.assertEqual(len(self.hotkeys.called("extend_capture")), 1)
        self.assertEqual(self.hotkeys.called("end_capture")[-1], ((False,), {}))

    def test_needs_launch_errors_do_not_block_the_show(self) -> None:
        self.window.raises["needs_launch"] = AttributeError("needs_launch")
        with self.assertLogs("projektsog.app", "ERROR"):
            self.assertTrue(self.controller.show_window(None, "hotkey"))
        self.assertEqual(self.hotkeys.called("extend_capture"), [])
        self.assertEqual(self.hotkeys.called("end_capture"), [((True,), {})])

    def test_no_show_while_exiting(self) -> None:
        # APP-2: once "Afslut" runs, nothing pops the window up again.
        self.controller.exiting.set()
        self.assertFalse(self.controller.show_window(None, "hotkey"))
        self.assertEqual(self.window.called("show"), [])
        self.assertEqual(self.hotkeys.called("end_capture"), [((False,), {})])
        self.assertEqual(self.published("focus"), [])

    def test_show_window_failure_drops_captured_keys(self) -> None:
        self.window.returns["show"] = False
        self.assertFalse(self.controller.show_window())
        self.assertEqual(self.hotkeys.called("end_capture"), [((False,), {})])
        self.assertEqual(self.published("focus"), [{"from_app": None, "reason": "api"}])

    def test_show_window_error_still_ends_capture(self) -> None:
        self.window.raises["show"] = RuntimeError("Edge vanished")
        with self.assertRaises(RuntimeError):
            self.controller.show_window(reason="tray")
        self.assertEqual(self.hotkeys.called("end_capture"), [((False,), {})])

    def test_show_settings_panel(self) -> None:
        self.controller.show_window(reason="tray", panel="settings")
        self.assertEqual(self.published("focus"),
                         [{"from_app": None, "reason": "tray", "panel": "settings"}])

    def test_show_without_window_or_hotkeys(self) -> None:
        self.controller.window = None
        self.controller.hotkeys = None
        self.assertFalse(self.controller.show_window(reason="launch"))
        self.assertEqual(self.published("focus"), [{"from_app": None, "reason": "launch"}])

    def test_collaborator_errors_do_not_hide_the_focus_event(self) -> None:
        self.indexer.raises["on_window_shown"] = RuntimeError("boom")
        with self.assertLogs("projektsog.app", "ERROR"):
            self.controller.show_window(reason="api")
        self.assertEqual(len(self.bridge.called("on_window_shown")), 1)
        self.assertEqual(len(self.published("focus")), 1)

    def test_hotkey_hides_when_window_is_in_front(self) -> None:
        self.window.returns.update(is_visible=True, is_foreground=True)
        self.controller.on_hotkey({"from_app": None})
        self.assertEqual(self.window.called("hide"), [((), {"restore_previous": True})])
        self.assertEqual(self.hotkeys.called("end_capture"), [((False,), {})])
        self.assertEqual(self.window.called("show"), [])
        self.assertEqual(self.published("focus"), [])

    def test_hotkey_shows_when_hidden_or_behind(self) -> None:
        for visible, foreground in ((False, False), (True, False)):
            with self.subTest(visible=visible, foreground=foreground):
                self.window.returns.update(is_visible=visible, is_foreground=foreground)
                self.controller.on_hotkey({"from_app": "Resolve.exe"})
                self.assertEqual(self.published("focus"),
                                 [{"from_app": "Resolve.exe", "reason": "hotkey"}])
        self.assertEqual(len(self.window.called("show")), 2)
        self.assertEqual(self.window.called("hide"), [])
        self.assertEqual(self.hotkeys.called("end_capture"), [((True,), {}), ((True,), {})])

    def test_hotkey_callback_never_raises(self) -> None:
        self.window.raises["is_visible"] = RuntimeError("window gone")
        with self.assertLogs("projektsog.app", "ERROR"):
            self.controller.on_hotkey({"from_app": None})

    def test_a_press_queued_during_the_show_keeps_the_window(self) -> None:
        # R2-APP-1 safety net: the HotkeyManager queues a press made while the callback runs
        # (e.g. typed during a cold start without the hook's capture). It must not hide the
        # window that callback has just shown.
        state = {"shown": False}
        self.window.returns.update(show=lambda: state.update(shown=True) or True,
                                   hide=lambda restore_previous=False: state.update(shown=False),
                                   is_visible=lambda: state["shown"],
                                   is_foreground=lambda: state["shown"])
        pressed = time.monotonic()
        self.controller.on_hotkey({"from_app": "Resolve.exe", "fired_at": pressed})
        self.controller.on_hotkey({"from_app": "Resolve.exe", "fired_at": pressed + 0.001})
        self.assertEqual(self.window.called("hide"), [])
        self.assertEqual(len(self.window.called("show")), 1)
        self.assertEqual(self.hotkeys.called("end_capture"), [((True,), {}), ((True,), {})])
        self.controller.on_hotkey({"from_app": None, "fired_at": time.monotonic()})
        self.assertEqual(self.window.called("hide"), [((), {"restore_previous": True})])
        self.assertEqual(self.hotkeys.called("end_capture")[-1], ((False,), {}))
        self.controller.on_hotkey({"from_app": None})       # no arrival time: a plain toggle
        self.assertEqual(len(self.window.called("show")), 2)

    def test_a_press_queued_during_a_failed_show_tries_again(self) -> None:
        self.window.returns["show"] = False
        pressed = time.monotonic()
        self.controller.on_hotkey({"from_app": None, "fired_at": pressed})
        self.controller.on_hotkey({"from_app": None, "fired_at": pressed + 0.001})
        self.assertEqual(len(self.window.called("show")), 2)

    def test_two_presses_during_a_cold_show_leave_the_window_shown(self) -> None:
        # The real HotkeyManager dispatcher: fire, a second fire 0.5 s later while the cold
        # show still runs (1 s) – the window stays, no hide(), no end_capture(False).
        sent: list[dict] = []

        class Pipe:                               # the hook child, as the manager sees it
            def send(self, message: dict) -> bool:
                sent.append(message)
                return True

            def alive(self) -> bool:
                return True

            def close(self, timeout: float) -> None:
                pass

        state = {"shown": False}

        def cold_show() -> bool:
            time.sleep(1.0)
            state["shown"] = True
            return True

        self.window.returns.update(needs_launch=True, show=cold_show,
                                   is_visible=lambda: state["shown"],
                                   is_foreground=lambda: state["shown"])
        manager = app.hotkey.HotkeyManager("shift+space", self.controller.on_hotkey)
        pipe = Pipe()
        with manager._lock:
            manager._child, manager._running = pipe, True
            manager._ensure_dispatcher()
        self.addCleanup(manager.stop)
        self.controller.hotkeys = manager
        manager._on_child_event(pipe, {"ev": "fire", "from_app": "Resolve.exe"})
        time.sleep(0.5)
        manager._on_child_event(pipe, {"ev": "fire", "from_app": "Resolve.exe"})
        self.assertTrue(fakes.wait_until(
            lambda: [m.get("ok") for m in sent if m["cmd"] == "end_capture"] == [True, True]))
        time.sleep(0.2)
        self.assertEqual(self.window.called("hide"), [])
        self.assertEqual([m.get("ok") for m in sent if m["cmd"] == "end_capture"], [True, True])
        self.assertTrue(state["shown"])
        manager._on_child_event(pipe, {"ev": "fire", "from_app": None})    # a later press
        self.assertTrue(fakes.wait_until(lambda: self.window.called("hide")))
        self.assertEqual(sent[-1], {"cmd": "end_capture", "ok": False})    # … toggles again

    def test_hide_window(self) -> None:
        self.controller.hide_window(restore_previous=True)
        self.controller.hide_window()
        self.assertEqual(self.window.called("hide"), [((), {"restore_previous": True}),
                                                       ((), {"restore_previous": False})])


class StatusAndSettingsTests(ControllerTestBase):
    def test_hotkey_status(self) -> None:
        self.assertEqual(self.controller.hotkey_status(), {
            "spec": "shift+space", "label": "Shift+Mellemrum", "enabled": True,
            "active": True, "mode": "ll"})
        self.controller.hotkeys = None
        self.cfg.update({"hotkey_enabled": False})
        self.assertEqual(self.controller.hotkey_status(), {
            "spec": "shift+space", "label": "Shift+Mellemrum", "enabled": False,
            "active": False, "mode": None})

    def test_unparsable_hotkey_is_shown_as_typed(self) -> None:
        self.cfg.update({"hotkey": "bogus"})
        self.assertEqual(self.controller.hotkey_status()["label"], "bogus")

    def test_set_run_at_login_writes_the_run_command_and_publishes_settings(self) -> None:
        self.controller.set_run_at_login(True)
        self.assertEqual(self.run_key,
                         [(True, '"C:\\Py\\pythonw.exe" "C:\\Repo\\Projektsøg.pyw" --background')])
        settings = self.published("settings")
        self.assertEqual(len(settings), 1)
        self.assertTrue(settings[0]["run_at_login"])
        self.assertEqual(settings[0]["hotkey"], self.cfg["hotkey"])
        self.assertTrue(self.controller.get_run_at_login())

    def test_set_run_at_login_errors(self) -> None:
        with self.assertRaises(ValueError):
            self.controller.set_run_at_login("yes")

        def failing(enabled: bool, command: str) -> None:
            raise PermissionError(5, "Adgang nægtet")
        controller = app.Controller(self.cfg, self.bus, self.indexer, self.bridge,
                                    set_run_at_login=failing,
                                    get_run_at_login=self._raise_oserror)
        with self.assertLogs("projektsog.app", "WARNING"):
            with self.assertRaises(ValueError):
                controller.set_run_at_login(True)
            self.assertFalse(controller.get_run_at_login())

    @staticmethod
    def _raise_oserror() -> bool:
        raise OSError("registry unavailable")

    def test_request_exit(self) -> None:
        self.controller.request_exit()
        self.assertEqual(self.exit_requests, 1)
        without = app.Controller(self.cfg, self.bus, self.indexer, self.bridge)
        with self.assertRaises(ValueError):
            without.request_exit()

    def test_offline_hint_fallbacks(self) -> None:
        self.assertEqual(app.offline_hint({"kind": "local", "volume_label": "ARKIV"}),
                         "Tilslut disken ‘ARKIV’")
        self.assertEqual(app.offline_hint({}), "Computeren ? svarer ikke – er den tændt?")
        self.assertEqual(app.offline_hint({"kind": "share", "host": "NAS",
                                           "volume_present": False}),
                         "Computeren NAS svarer ikke – er den tændt?")
        self.assertEqual(app.offline_hint({"kind": "share", "host": "NAS",
                                           "volume_present": True}),
                         "Mappen findes ikke længere")


if __name__ == "__main__":
    unittest.main()
