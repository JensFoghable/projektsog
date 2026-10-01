"""AppWindow logic against a fake desktop: every Win32 call is patched, nothing real is touched."""

import json
import logging
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from projektsog import config, window, winui
from projektsog.window import AppWindow, centred_position, disable_background_mode
from projektsog.winui import WindowInfo

_tmp: tempfile.TemporaryDirectory | None = None
_logger = logging.getLogger("projektsog.window")
_saved_level = _logger.level
_saved_watch = AppWindow.WATCH

OURS, OTHER_APP = 0x100, 0x9000
TITLE = "Projektsøg"
SELECT_APP_WINDOW = winui.find_app_window_info      # the real matcher, bound before patching


def setUpModule() -> None:
    global _tmp
    _tmp = tempfile.TemporaryDirectory()
    os.environ["LOCALAPPDATA"] = _tmp.name
    _logger.setLevel(logging.CRITICAL)
    # No watcher thread unless a test asks for one: a stray watcher outliving the patches could
    # otherwise reach the real desktop.
    AppWindow.WATCH = False


def tearDownModule() -> None:
    AppWindow.WATCH = _saved_watch
    _logger.setLevel(_saved_level)
    if _tmp is not None:
        _tmp.cleanup()


class FakeDesktop:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.windows: dict[int, WindowInfo] = {}
        self.visible: set[int] = {OTHER_APP}
        self.foreground = OTHER_APP
        self.calls: list[tuple] = []
        self.enumerations = 0

    def add_ours(self, hwnd: int = OURS, pid: int = 50, title: str = TITLE,
                 exe: str = "msedge.exe") -> None:
        with self.lock:
            self.windows[hwnd] = WindowInfo(hwnd, "Chrome_WidgetWin_1", title, pid, exe, False, None)

    def close_window(self, hwnd: int = OURS) -> None:
        """The user closed the window (X / Alt+F4) or Edge exited."""
        with self.lock:
            self.windows.pop(hwnd, None)
            self.visible.discard(hwnd)

    # patched functions ---------------------------------------------------------------------
    def window_info(self, hwnd):
        with self.lock:
            return self.windows.get(hwnd)

    def find_app_window_info(self, title, exe_name="msedge.exe", pid=None):
        with self.lock:
            self.enumerations += 1
            candidates = list(self.windows.values())
        return SELECT_APP_WINDOW(title, exe_name, pid, windows=candidates)

    def show_window_async(self, hwnd, cmd):
        with self.lock:
            self.calls.append(("show", hwnd, cmd))
            if cmd == window.SW_HIDE:
                self.visible.discard(hwnd)
            else:
                self.visible.add(hwnd)
        return True

    def force_foreground(self, hwnd):
        with self.lock:
            self.calls.append(("foreground", hwnd))
            self.foreground = hwnd
        return True

    def post_message(self, hwnd, msg, wparam, lparam):
        self.calls.append(("post", hwnd, msg))
        return True


class AppWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.desk = desk = FakeDesktop()
        patches = [
            mock.patch.object(winui, "window_info", desk.window_info),
            mock.patch.object(winui, "find_app_window_info", desk.find_app_window_info),
            mock.patch.object(winui, "foreground_window", lambda: desk.foreground),
            mock.patch.object(winui, "force_foreground", desk.force_foreground),
            mock.patch.object(winui, "edge_path", lambda: None),
            mock.patch.object(window, "_ShowWindowAsync", desk.show_window_async),
            mock.patch.object(window, "_PostMessageW", desk.post_message),
            mock.patch.object(window, "_IsWindowVisible", lambda h: h in desk.visible),
            mock.patch.object(window, "_IsIconic", lambda h: False),
            mock.patch.object(window, "_IsZoomed", lambda h: False),
            mock.patch.object(window, "_IsWindow",
                              lambda h: h == OTHER_APP or h in desk.windows),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.launches: list[bool] = []
        self.launch_times: list[float] = []
        self.on_launch = lambda: None             # e.g. make a window appear a bit later
        self.win = AppWindow("http://127.0.0.1:47811/", os.path.join(_tmp.name, "profile"))
        self.win._launch = self.fake_launch
        self.win._place = lambda hwnd, anchor: None
        self.addCleanup(self.stop_window, self.win)   # runs before the patches are undone

    def stop_window(self, win: AppWindow) -> None:
        win.close()
        if win._watcher is not None:
            win._watcher.join(2.0)
            self.assertFalse(win._watcher.is_alive(), "the watcher thread did not stop")

    def fake_launch(self, preload: bool) -> bool:
        self.launches.append(preload)
        self.launch_times.append(time.monotonic())
        self.win._launch_deadline = time.monotonic() + self.win.LAUNCH_TIMEOUT_S
        self.on_launch()
        return True

    def watch_fast(self) -> None:
        self.win.WATCH = True
        self.win.WATCH_INTERVAL_S = 0.02
        self.win.REPRELOAD_DELAY_S = 0.05

    def wait_for(self, predicate, timeout: float = 3.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return predicate()

    def later(self, seconds: float, fn) -> threading.Timer:
        timer = threading.Timer(seconds, fn)
        timer.start()
        self.addCleanup(timer.cancel)
        return timer

    def test_centred_position(self):
        self.assertEqual(centred_position((1180, 780), (0, 0, 1920, 1040)), (370, 130))
        self.assertEqual(centred_position((1180, 780), (1920, 0, 4480, 1400)), (2610, 310))
        self.assertEqual(centred_position((1180, 780), (-1920, 0, 0, 1080)), (-1550, 150))
        self.assertEqual(centred_position((2000, 1200), (0, 0, 1920, 1040)), (0, 0))

    def test_identification_is_cached_and_revalidated(self):
        self.desk.add_ours()
        with self.win._lock:
            self.assertEqual(self.win._window(), OURS)
            self.assertEqual(self.win._window(), OURS)
        self.assertEqual(self.desk.enumerations, 1)            # second use: validation only
        # The handle got reused by someone else's window: never touch it.
        self.desk.windows[OURS] = WindowInfo(OURS, "Chrome_WidgetWin_1",
                                             "Projektsøg – Google Chrome", 77, "chrome.exe",
                                             True, None)
        with self.win._lock:
            self.assertIsNone(self.win._window())
        self.desk.add_ours(hwnd=0x200, pid=51)
        with self.win._lock:
            self.assertEqual(self.win._window(), 0x200)
        # Same handle and title but another process: re-identified from scratch.
        self.desk.add_ours(hwnd=0x200, pid=52)
        with self.win._lock:
            enumerations = self.desk.enumerations
            self.assertEqual(self.win._window(), 0x200)
            self.assertEqual(self.win._pid, 52)
            self.assertEqual(self.desk.enumerations, enumerations + 1)

    def test_preload_launches_once_and_hides(self):
        self.later(0.15, self.desk.add_ours)
        self.win.preload()
        self.assertEqual(self.launches, [True])
        self.assertIn(("show", OURS, window.SW_HIDE), self.desk.calls)
        self.assertNotIn(("foreground", OURS), self.desk.calls)
        self.win.preload()                                       # window exists: nothing to do
        self.assertEqual(self.launches, [True])
        self.assertFalse(self.win.is_visible())

    def test_preload_hands_foreground_back_if_edge_kept_it(self):
        def edge_appears_and_activates_itself():
            self.desk.add_ours()
            self.desk.foreground = OURS            # Chromium's own Activate() succeeded
        self.later(0.1, edge_appears_and_activates_itself)
        self.win.preload()
        self.assertIn(("show", OURS, window.SW_HIDE), self.desk.calls)
        self.assertEqual(self.desk.calls[-1], ("foreground", OTHER_APP))

    def test_preload_leaves_focus_alone_when_windows_restored_it(self):
        self.later(0.1, self.desk.add_ours)        # foreground stays with the other app
        self.win.preload()
        self.assertNotIn(("foreground", OTHER_APP), self.desk.calls)

    def test_show_during_pending_preload_waits_instead_of_launching_again(self):
        results: dict[str, object] = {}
        preload = threading.Thread(target=lambda: results.setdefault("preload", self.win.preload()))
        preload.start()
        time.sleep(0.1)
        self.later(0.2, self.desk.add_ours)
        results["show"] = self.win.show()
        preload.join(5)
        self.assertEqual(self.launches, [True])                  # one Edge only
        self.assertIs(results["show"], True)
        self.assertNotIn(("show", OURS, window.SW_HIDE), self.desk.calls)
        self.assertIn(("foreground", OURS), self.desk.calls)
        self.assertTrue(self.win.is_foreground())

    def test_show_launches_when_nothing_is_pending(self):
        self.later(0.1, self.desk.add_ours)
        self.assertTrue(self.win.show())
        self.assertEqual(self.launches, [False])

    def test_show_gives_up_in_time(self):
        self.win.SHOW_TIMEOUT_S = 0.3
        started = time.monotonic()
        self.assertFalse(self.win.show())
        self.assertLess(time.monotonic() - started, 1.0)

    def test_show_without_edge_fails_cleanly(self):
        win = AppWindow("http://127.0.0.1:47811/", os.path.join(_tmp.name, "p2"))
        with mock.patch.object(window.subprocess, "Popen") as popen:
            self.assertFalse(win.show())
        popen.assert_not_called()

    def test_already_foreground_is_left_alone(self):
        self.desk.add_ours()
        self.desk.visible.add(OURS)
        self.desk.foreground = OURS
        self.assertTrue(self.win.show())
        self.assertEqual(self.desk.calls, [])

    def test_hide_restores_previous_window_only_when_asked(self):
        self.desk.add_ours()
        self.assertTrue(self.win.show())
        self.assertEqual(self.win._previous, OTHER_APP)
        self.desk.calls.clear()
        self.win.hide(restore_previous=True)
        self.assertEqual(self.desk.calls, [("foreground", OTHER_APP), ("show", OURS, window.SW_HIDE)])
        self.desk.calls.clear()
        self.win.hide()
        self.assertEqual(self.desk.calls, [("show", OURS, window.SW_HIDE)])
        self.desk.visible.discard(OTHER_APP)                    # previous window went away
        self.desk.calls.clear()
        self.win.hide(restore_previous=True)
        self.assertEqual(self.desk.calls, [("show", OURS, window.SW_HIDE)])

    def test_close_only_posts_to_a_validated_window(self):
        self.desk.add_ours()
        self.win.close()
        self.assertEqual(self.desk.calls, [("post", OURS, window.WM_CLOSE)])
        self.desk.calls.clear()
        self.desk.windows[OURS] = WindowInfo(OURS, "Chrome_WidgetWin_1", "Andet vindue", 9,
                                             "msedge.exe", True, None)
        self.win.close()
        self.assertEqual(self.desk.calls, [])

    # -- WIN-1 / SPEC §15.10 ------------------------------------------------------------------
    def test_needs_launch(self):
        self.assertTrue(self.win.needs_launch())                # nothing there: cold launch
        self.desk.add_ours()
        self.assertFalse(self.win.needs_launch())               # hidden window ready
        self.desk.close_window()
        self.assertTrue(self.win.needs_launch())
        self.win._launch_deadline = time.monotonic() + 5        # a launch is still pending:
        self.assertTrue(self.win.needs_launch())                # show() would have to wait

    def test_watcher_preloads_hidden_again_after_the_user_closed_the_window(self):
        self.watch_fast()
        self.later(0.05, self.desk.add_ours)
        self.win.preload()
        self.assertEqual(self.launches, [True])
        self.assertTrue(self.win.show())                        # the user uses it …
        self.desk.close_window()                                # … and closes it with X
        self.on_launch = lambda: self.later(0.05, lambda: self.desk.add_ours(0x300, pid=60))
        self.desk.calls.clear()
        self.assertTrue(self.wait_for(lambda: ("show", 0x300, window.SW_HIDE) in self.desk.calls))
        self.assertEqual(self.launches, [True, True])           # relaunched as a preload
        self.assertNotIn(("foreground", 0x300), self.desk.calls)
        self.assertFalse(self.win.needs_launch())               # the next hotkey is instant
        time.sleep(0.2)
        self.assertEqual(self.launches, [True, True])           # and only once

    def test_watcher_leaves_a_hidden_or_pending_window_alone(self):
        self.watch_fast()
        self.later(0.05, self.desk.add_ours)
        self.win.preload()                                      # hidden, but it exists
        time.sleep(0.3)
        self.assertEqual(self.launches, [True])
        self.desk.close_window()
        self.win._launch_deadline = time.monotonic() + 60       # e.g. show() launching
        time.sleep(0.3)
        self.assertEqual(self.launches, [True])

    def test_watcher_backs_off_while_edge_keeps_failing(self):
        self.watch_fast()
        self.win.LAUNCH_TIMEOUT_S = 0.05                        # Edge never shows a window
        self.win.RETRY_BASE_S, self.win.RETRY_MAX_S = 0.3, 0.6
        self.win.preload()
        time.sleep(1.75)
        self.win.close()
        attempts = self.launch_times[1:]                        # the watcher's relaunches
        gaps = [b - a for a, b in zip(attempts, attempts[1:])]
        self.assertGreaterEqual(len(attempts), 2, attempts)
        self.assertLessEqual(len(attempts), 5, gaps)            # not every 20 ms tick
        self.assertGreaterEqual(gaps[0], 0.28)                  # 0.3 s, then 0.6 s (capped)
        for gap in gaps[1:]:
            self.assertGreaterEqual(gap, 0.57)

    def test_back_off_resets_once_a_window_was_stable(self):
        self.watch_fast()
        self.win.RETRY_BASE_S, self.win.STABLE_S = 30.0, 0.1
        self.later(0.05, self.desk.add_ours)
        self.win.preload()
        self.desk.close_window()
        self.on_launch = lambda: self.later(0.05, lambda: self.desk.add_ours(0x300, pid=60))
        self.assertTrue(self.wait_for(lambda: len(self.launches) == 2))
        self.assertTrue(self.wait_for(lambda: not self.win.needs_launch()))
        time.sleep(0.3)                                         # stable for > STABLE_S
        self.desk.close_window(0x300)
        self.on_launch = lambda: self.later(0.05, lambda: self.desk.add_ours(0x400, pid=70))
        self.assertTrue(self.wait_for(lambda: len(self.launches) == 3, timeout=2.0),
                        "a stable window must not keep the 30 s back-off")

    def test_watcher_waits_until_the_old_edge_released_the_profile(self):
        self.watch_fast()
        self.win.RETRY_BASE_S = 0.1              # (the back-off is tested separately)
        os.makedirs(self.win.profile_dir, exist_ok=True)
        lockfile = os.path.join(self.win.profile_dir, window.LOCKFILE)
        self.addCleanup(lambda: os.path.exists(lockfile) and os.remove(lockfile))
        self.later(0.05, self.desk.add_ours)
        self.win.preload()
        with open(lockfile, "w"):
            pass                                # the closed Edge is still shutting down
        self.desk.close_window()
        time.sleep(0.4)
        self.assertEqual(self.launches, [True])
        os.remove(lockfile)                     # … now it has exited
        self.assertTrue(self.wait_for(lambda: len(self.launches) == 2))
        self.desk.add_ours(0x300, pid=60)
        self.assertTrue(self.wait_for(lambda: not self.win.needs_launch()))
        with open(lockfile, "w"):
            pass                                # a browser that never releases the profile
        self.win.PROFILE_WAIT_S = 0.3
        self.desk.close_window(0x300)
        self.assertTrue(self.wait_for(lambda: len(self.launches) == 3, timeout=3.0),
                        "the wait for the profile is bounded")

    def test_close_stops_the_watcher_and_any_further_preload(self):
        self.watch_fast()
        self.later(0.05, self.desk.add_ours)
        self.win.preload()
        watcher = self.win._watcher
        self.win.close()
        self.desk.close_window()                                # Edge exits after WM_CLOSE
        watcher.join(1.0)
        self.assertFalse(watcher.is_alive())
        time.sleep(0.2)
        self.win.preload()
        self.assertEqual(self.launches, [True])                 # nothing relaunched at exit

    def test_preload_finishing_after_close_closes_its_window(self):
        results: list[object] = []
        preload = threading.Thread(target=lambda: results.append(self.win.preload()))
        preload.start()
        time.sleep(0.05)
        with self.win._lock:                                    # no poll of the preload between
            self.win.close()                                    # app exit during the launch …
            self.desk.add_ours()                                # … and Edge shows up anyway
        preload.join(3)
        self.assertEqual(results, [None])
        self.assertIn(("post", OURS, window.WM_CLOSE), self.desk.calls)   # not left behind
        self.assertNotIn(("show", OURS, window.SW_HIDE), self.desk.calls)

    def test_launch_command_line(self):
        edge = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
        profile = os.path.join(_tmp.name, "edge profile")
        for preload, show_cmd in ((True, window.SW_SHOWMINNOACTIVE),
                                  (False, window.SW_SHOWNOACTIVATE)):
            with self.subTest(preload=preload):
                win = AppWindow("http://127.0.0.1:47811/", profile, edge=edge)
                with mock.patch.object(window.subprocess, "Popen") as popen:
                    popen.return_value.pid = 4242
                    with win._lock:
                        self.assertTrue(win._launch(preload=preload))
                    self.assertTrue(win._launch_pending())
                args, kwargs = popen.call_args
                self.assertEqual(args[0], [
                    edge, "--app=http://127.0.0.1:47811/", f"--user-data-dir={profile}",
                    "--window-size=1180,780", "--no-first-run", "--no-default-browser-check",
                    "--disable-features=Translate,msEdgeStartupBoost",
                    "--disable-renderer-backgrounding",
                    "--disable-background-timer-throttling",
                    "--disable-backgrounding-occluded-windows", "--hide-crash-restore-bubble",
                    "--disable-sync", "--window-position=-32000,-32000"])
                self.assertEqual(kwargs["cwd"], config.app_dir())
                self.assertEqual(kwargs["startupinfo"].wShowWindow, show_cmd)
                self.assertTrue(kwargs["startupinfo"].dwFlags & window.subprocess.STARTF_USESHOWWINDOW)
                # Background mode and startup boost are off before Edge starts.
                with open(os.path.join(profile, "Local State"), encoding="utf-8") as fh:
                    state = json.load(fh)
                self.assertEqual(state["background_mode"], {"enabled": False})
                self.assertEqual(state["startup_boost"], {"enabled": False})


class WatcherBackoffTests(unittest.TestCase):
    """The real AppWindow._watch loop on a fake clock (R2-WIN-2): only failed relaunches back
    off – a user who closes the window after each search always finds a hidden one ready."""

    END_S = 1000.0

    def setUp(self) -> None:
        test = self
        self.now = 0.0
        self.present = True             # the login preload's hidden window
        self.relaunch_gives_window = True
        self.vanish_unseen_after: float | None = None   # a relaunched Edge that dies by itself
        self.relaunches: list[float] = []
        self.closes: list[float] = []
        self.cold: list[float] = []     # hotkey presses that found no window (cold Edge start)
        self.actions: list[tuple[float, str]] = []

        class Stop:                      # each wait advances the fake clock
            def wait(self, timeout: float) -> bool:
                test.now += timeout
                test.run_actions()
                return test.now >= test.END_S

            def set(self) -> None:
                pass

        self.win = AppWindow("http://127.0.0.1:47811/", os.path.join(_tmp.name, "no-profile"))
        self.win._watch_stop = Stop()
        self.win._window = lambda: 1 if self.present else None
        self.win._launch_pending = lambda: False
        self.win.preload = self.fake_preload
        clock = mock.patch.object(window, "time", mock.Mock(monotonic=lambda: self.now))
        clock.start()
        self.addCleanup(clock.stop)

    def at(self, t: float, action: str) -> None:
        self.actions.append((t, action))
        self.actions.sort()

    def run_actions(self) -> None:
        while self.actions and self.actions[0][0] <= self.now:
            t, action = self.actions.pop(0)
            if action == "hotkey":               # show(): the user sees the window …
                if not self.present:
                    self.cold.append(t)
                self.present = True
                self.win._show_count += 1
            elif action == "close":              # … and closes it (X), or Edge dies
                self.present = False
                self.closes.append(t)

    def fake_preload(self) -> None:
        self.relaunches.append(self.now)
        self.present = self.relaunch_gives_window
        if self.present and self.vanish_unseen_after is not None:
            self.at(self.now + self.vanish_unseen_after, "close")

    def gaps(self) -> list[float]:
        return [b - a for a, b in zip(self.relaunches, self.relaunches[1:])]

    def test_closing_the_window_after_each_search_never_backs_off(self):
        for press in range(30, 350, 40):                  # 8 searches 40 s apart, X after 15 s
            self.at(press, "hotkey")
            self.at(press + 15, "close")
        self.win._watch()
        self.assertEqual(self.cold, [])                   # every press found a hidden window
        self.assertEqual(len(self.relaunches), len(self.closes))
        limit = self.win.REPRELOAD_DELAY_S + 2 * self.win.WATCH_INTERVAL_S
        for closed, relaunched in zip(self.closes, self.relaunches):
            self.assertLess(closed, relaunched)
            self.assertLessEqual(relaunched - closed, limit, (self.closes, self.relaunches))

    def test_windows_that_vanish_unseen_still_back_off(self):
        self.vanish_unseen_after = 1.0                    # Edge dies right after each start
        self.at(10, "close")
        self.END_S = 170.0
        self.win._watch()
        self.assertEqual(self.gaps(), [10.0, 20.0, 40.0, 80.0])

    def test_a_relaunch_without_window_backs_off_until_a_window_is_seen(self):
        self.relaunch_gives_window = False                # Edge does not start at all
        self.at(10, "close")
        self.at(50, "hotkey")                             # show() cold-starts Edge; it works
        self.at(60, "close")
        self.END_S = 100.0
        self.win._watch()
        self.assertEqual(self.cold, [50])
        self.assertEqual(self.relaunches[:3], [14.0, 24.0, 44.0])       # 10, 20 s back-off …
        self.assertLessEqual(self.relaunches[3] - 60,                   # … reset by the show
                             self.win.REPRELOAD_DELAY_S + 2 * self.win.WATCH_INTERVAL_S)
        self.assertEqual(self.gaps()[3:], [10.0, 20.0])                 # failing again: 10, 20 s


class BackgroundModeTests(unittest.TestCase):
    """Edge's 'keep running when closed' and startup boost switched off in the private
    profile's Local State (no msedge process may linger after the window closes)."""

    def setUp(self) -> None:
        self.profile = tempfile.mkdtemp(dir=_tmp.name)
        self.path = os.path.join(self.profile, "Local State")

    def read(self) -> dict:
        with open(self.path, encoding="utf-8") as fh:
            return json.load(fh)

    def write(self, text: str) -> None:
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_new_profile_gets_a_minimal_local_state(self):
        fresh = os.path.join(self.profile, "not yet created")
        self.assertTrue(disable_background_mode(fresh))
        with open(os.path.join(fresh, "Local State"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), {"background_mode": {"enabled": False},
                                             "startup_boost": {"enabled": False}})

    def test_existing_state_is_kept(self):
        state = {"os_crypt": {"encrypted_key": "RFBBUEk="}, "profile": {"info_cache": {"Default":
                 {"name": "Projektsøg"}}}, "background_mode": {"enabled": True, "other": 1},
                 "startup_boost": {"last_browser_open_time": "13435265628184294"},
                 "big": 13435265628184294}
        self.write(json.dumps(state))
        self.assertTrue(disable_background_mode(self.profile))
        expected = dict(state, background_mode={"enabled": False, "other": 1},
                        startup_boost={"last_browser_open_time": "13435265628184294",
                                       "enabled": False})
        self.assertEqual(self.read(), expected)
        mtime = os.stat(self.path).st_mtime_ns
        self.assertTrue(disable_background_mode(self.profile))     # already off: no rewrite
        self.assertEqual(os.stat(self.path).st_mtime_ns, mtime)
        self.assertEqual(os.listdir(self.profile), ["Local State"])  # no temp file left

    def test_a_profile_seeded_by_an_older_version_gets_startup_boost_off_too(self):
        self.write('{"background_mode": {"enabled": false}}')
        self.assertTrue(disable_background_mode(self.profile))
        self.assertEqual(self.read(), {"background_mode": {"enabled": False},
                                       "startup_boost": {"enabled": False}})

    def test_not_touched_while_edge_runs_or_when_unreadable(self):
        self.write('{"background_mode": {"enabled": true}}')
        with open(os.path.join(self.profile, "lockfile"), "w"):
            pass                                                     # Edge holds the profile
        self.assertFalse(disable_background_mode(self.profile))
        self.assertEqual(self.read(), {"background_mode": {"enabled": True}})
        os.remove(os.path.join(self.profile, "lockfile"))
        for broken in ("{not json", "[1, 2]", ""):
            with self.subTest(broken=broken):
                self.write(broken)
                self.assertFalse(disable_background_mode(self.profile))
                with open(self.path, encoding="utf-8") as fh:
                    self.assertEqual(fh.read(), broken)              # never "repaired"


if __name__ == "__main__":
    unittest.main()
