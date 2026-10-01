"""The Edge app-mode window that hosts the UI (SPEC §10.2).

Identification is exact – top-level, class ``Chrome_WidgetWin_1``, process ``msedge.exe``,
title exactly the page title – because Chrome, VS Code and other Electron windows share the
class. The window handle and process id are cached and re-validated before every use; a
window that fails validation is never touched.

The window is pre-launched hidden (``preload``) so Shift+Space is instant. Every launch
starts off-screen and minimised without activation; ``show`` then centres the window on the
monitor of the previously active window and brings it to the front. (Measured: Chromium still
restores and activates its first window itself when Windows allows it – e.g. after a long
idle period – so ``preload`` hides it as soon as it is identified and hands the foreground
back if Windows does not.)

SPEC §15.10: after the first ``preload``/``show`` a watcher thread keeps a hidden window ready:
when our window disappears (the user closed it, Edge exited or crashed) it preloads a new one
(hidden) a few seconds later, with exponential back-off while that keeps failing.
``needs_launch()`` tells the Controller that ``show`` will be a cold launch. Edge's background
mode and startup boost are switched off for the private profile (``Local State`` prefs plus
``--disable-features=msEdgeStartupBoost``), so no msedge process keeps running after the window
closes.
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import logging
import os
import subprocess
import threading
import time
from ctypes import wintypes
from typing import Any, Iterator

from . import APP_NAME, config, winui

log = logging.getLogger(__name__)

_user32 = ctypes.WinDLL("user32", use_last_error=True)

HANDLE = wintypes.HANDLE


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


class _WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [("length", wintypes.UINT), ("flags", wintypes.UINT), ("showCmd", wintypes.UINT),
                ("ptMinPosition", wintypes.POINT), ("ptMaxPosition", wintypes.POINT),
                ("rcNormalPosition", wintypes.RECT)]


def _declare(name: str, restype: Any, *argtypes: Any) -> Any:
    fn = getattr(_user32, name)
    fn.restype = restype
    fn.argtypes = list(argtypes)
    return fn


_ShowWindowAsync = _declare("ShowWindowAsync", wintypes.BOOL, HANDLE, ctypes.c_int)
_SetWindowPos = _declare("SetWindowPos", wintypes.BOOL, HANDLE, HANDLE, ctypes.c_int,
                         ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT)
_GetWindowRect = _declare("GetWindowRect", wintypes.BOOL, HANDLE, ctypes.POINTER(wintypes.RECT))
_GetWindowPlacement = _declare("GetWindowPlacement", wintypes.BOOL, HANDLE,
                               ctypes.POINTER(_WINDOWPLACEMENT))
_IsWindow = _declare("IsWindow", wintypes.BOOL, HANDLE)
_IsWindowVisible = _declare("IsWindowVisible", wintypes.BOOL, HANDLE)
_IsIconic = _declare("IsIconic", wintypes.BOOL, HANDLE)
_IsZoomed = _declare("IsZoomed", wintypes.BOOL, HANDLE)
_MonitorFromWindow = _declare("MonitorFromWindow", HANDLE, HANDLE, wintypes.DWORD)
_MonitorFromPoint = _declare("MonitorFromPoint", HANDLE, wintypes.POINT, wintypes.DWORD)
_GetMonitorInfoW = _declare("GetMonitorInfoW", wintypes.BOOL, HANDLE,
                            ctypes.POINTER(_MONITORINFO))
_GetCursorPos = _declare("GetCursorPos", wintypes.BOOL, ctypes.POINTER(wintypes.POINT))
_PostMessageW = _declare("PostMessageW", wintypes.BOOL, HANDLE, wintypes.UINT, wintypes.WPARAM,
                         wintypes.LPARAM)
try:
    _SetThreadDpiAwarenessContext = _declare("SetThreadDpiAwarenessContext", HANDLE, HANDLE)
except AttributeError:          # Windows < 10 1607
    _SetThreadDpiAwarenessContext = None

SW_HIDE, SW_SHOWNOACTIVATE, SW_SHOW, SW_SHOWMINNOACTIVE = 0, 4, 5, 7
SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE, SWP_ASYNCWINDOWPOS = 0x1, 0x4, 0x10, 0x4000
MONITOR_DEFAULTTONEAREST = 2
WM_CLOSE = 0x0010
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
OFFSCREEN = -32000


@contextlib.contextmanager
def _physical_pixels() -> Iterator[None]:
    """Work in physical pixels on this thread, whatever the process DPI awareness is."""
    previous = None
    if _SetThreadDpiAwarenessContext is not None:
        previous = _SetThreadDpiAwarenessContext(
            ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2))
    try:
        yield
    finally:
        if previous:
            _SetThreadDpiAwarenessContext(ctypes.c_void_p(previous))


def centred_position(size: tuple[int, int], work: tuple[int, int, int, int]) -> tuple[int, int]:
    """Top-left corner that centres ``size`` in ``work`` (left, top, right, bottom); a window
    larger than the work area is aligned to its top-left corner."""
    width, height = size
    left, top, right, bottom = work
    return left + max(0, (right - left - width) // 2), top + max(0, (bottom - top - height) // 2)


def _work_area(anchor: int | None) -> tuple[int, int, int, int]:
    if anchor and _IsWindow(anchor):
        monitor = _MonitorFromWindow(anchor, MONITOR_DEFAULTTONEAREST)
    else:
        point = wintypes.POINT()
        _GetCursorPos(ctypes.byref(point))
        monitor = _MonitorFromPoint(point, MONITOR_DEFAULTTONEAREST)
    info = _MONITORINFO()
    info.cbSize = ctypes.sizeof(info)
    if not monitor or not _GetMonitorInfoW(monitor, ctypes.byref(info)):
        return 0, 0, 1280, 720
    r = info.rcWork
    return r.left, r.top, r.right, r.bottom


def _window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    rect = wintypes.RECT()
    if not _GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    return rect.left, rect.top, rect.right, rect.bottom


def _normal_size(hwnd: int) -> tuple[int, int] | None:
    """Size of the window when restored (also valid while it is minimised)."""
    placement = _WINDOWPLACEMENT()
    placement.length = ctypes.sizeof(placement)
    if not _GetWindowPlacement(hwnd, ctypes.byref(placement)):
        return None
    r = placement.rcNormalPosition
    return r.right - r.left, r.bottom - r.top


LOCAL_STATE = "Local State"
LOCKFILE = "lockfile"          # exists exactly while an Edge browser process uses the profile
# Local State prefs that keep Edge running without a window: "Continue running background
# extensions and apps when Microsoft Edge is closed" and startup boost. Measured with Edge 154:
# closing the app window made Edge start a startup-boost browser ("--no-startup-window") for
# the *default* profile, which stayed; with startup_boost.enabled = false (or the
# msEdgeStartupBoost feature disabled) every process exits within ~0.3 s.
_BACKGROUND_PREFS = ("background_mode", "startup_boost")


def disable_background_mode(profile_dir: str) -> bool:
    """Seed ``background_mode.enabled = false`` and ``startup_boost.enabled = false`` into the
    profile's ``Local State`` so no msedge process lingers after the window closes. Edge 154
    has no command-line switch for background mode (the string ``disable-background-mode`` does
    not occur in msedge.dll); it keeps the seeded values.

    Best effort, never raises; True when both are (now) off. Skipped while Edge runs with this
    profile (its ``lockfile`` exists): Edge rewrites the file from memory, and a write racing
    with Edge's own must not lose its data. A file that is not a JSON object is left alone (it
    holds e.g. the profile's encryption key)."""
    path = os.path.join(profile_dir, LOCAL_STATE)
    try:
        with open(path, encoding="utf-8") as fh:
            state = json.load(fh)
    except FileNotFoundError:
        state = {}
    except (OSError, ValueError) as exc:
        log.info("Edge's Local State is unreadable – background mode left as is: %s", exc)
        return False
    if not isinstance(state, dict):
        return False
    if all(isinstance(state.get(key), dict) and state[key].get("enabled") is False
           for key in _BACKGROUND_PREFS):
        return True
    if os.path.exists(os.path.join(profile_dir, LOCKFILE)):
        return False                        # Edge runs with this profile right now
    for key in _BACKGROUND_PREFS:
        current = state.get(key)
        state[key] = {**(current if isinstance(current, dict) else {}), "enabled": False}
    temp = path + ".projektsog-tmp"
    try:
        os.makedirs(profile_dir, exist_ok=True)
        with open(temp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, separators=(",", ":"))
        os.replace(temp, path)
    except OSError as exc:
        log.info("could not switch off Edge's background mode: %s", exc)
        with contextlib.suppress(OSError):
            os.remove(temp)
        return False
    return True


class AppWindow:
    """The UI window: an Edge ``--app`` window with a private profile."""

    LAUNCH_TIMEOUT_S = 15.0
    SHOW_TIMEOUT_S = 10.0
    _POLL_S = 0.02            # a waiting preload hides the new window within ~20 ms
    # Watcher (SPEC §15.10): re-preload a hidden window after ours disappeared.
    WATCH_INTERVAL_S = 2.0    # how often the watcher checks that our window still exists
    REPRELOAD_DELAY_S = 3.0   # grace period after it vanished (lets the old Edge exit first)
    RETRY_BASE_S = 10.0       # after a failed relaunch the next one waits 10, 20, 40 … s
    RETRY_MAX_S = 300.0
    STABLE_S = 60.0           # a window that lived this long resets the back-off (so does a show)
    PROFILE_WAIT_S = 30.0     # wait at most this long for the old Edge to release the profile
    WATCH = True              # tests switch the watcher off where they do not exercise it

    def __init__(self, url: str, profile_dir: str, title: str = APP_NAME,
                 edge: str | None = None) -> None:
        self.url = url
        self.profile_dir = profile_dir
        self.title = title
        self._edge = edge
        self._lock = threading.RLock()
        self._hwnd: int | None = None
        self._pid: int | None = None
        self._proc: subprocess.Popen | None = None
        self._launch_deadline = 0.0          # monotonic; a launch is pending until then
        self._show_count = 0                 # lets a preload notice a show() in the meantime
        self._previous: int | None = None    # window that was active before our last show()
        self._closed = False                 # close() was called (app exit): no more preloads
        self._watch_stop = threading.Event()
        self._watcher: threading.Thread | None = None

    # -- public API ----------------------------------------------------------------------------
    def needs_launch(self) -> bool:
        """True when ``show()`` cannot use an existing window: it has to start Edge or wait for
        a launch in progress (a cold start, typically 0.5–3 s). The Controller then extends the
        hotkey's key capture (SPEC §15.10)."""
        with self._lock:
            return self._window() is None

    def preload(self) -> None:
        """Launch Edge hidden and off-screen, without activation, if no window exists yet.
        Blocks until the window is there and hidden (≤ 15 s). Also starts the watcher that
        preloads again after the window was closed."""
        previous = winui.foreground_window()
        with self._lock:
            if self._closed:
                return
            self._ensure_watcher()
            if self._window() is not None or self._launch_pending():
                return
            if not self._launch(preload=True):
                return
            shows = self._show_count
        hwnd = self._wait_for_window(self.LAUNCH_TIMEOUT_S)
        if hwnd is None:
            log.warning("the Edge window did not appear within %.0f s", self.LAUNCH_TIMEOUT_S)
            return
        with self._lock:
            if self._closed:                # the app is exiting: do not leave this Edge behind
                _PostMessageW(hwnd, WM_CLOSE, 0, 0)
                return
            if self._show_count != shows:   # someone asked to see it meanwhile
                return
            _ShowWindowAsync(hwnd, SW_HIDE)
            pid = self._pid
        self._hand_back_foreground(previous, pid, shows)

    def _hand_back_foreground(self, previous: int | None, pid: int | None, shows: int) -> None:
        """Chromium activates a new window by itself (it restores and calls SetForegroundWindow
        after the minimised first show), which Windows permits when no other process has
        received input recently. Hiding the window normally lets Windows re-activate the
        previous one; if the foreground is still inside our Edge afterwards, hand it back."""
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            info = winui.window_info(winui.foreground_window())
            if info is None or info.pid != pid or self._show_count != shows:
                return
            time.sleep(self._POLL_S)
        if (previous and _IsWindow(previous) and _IsWindowVisible(previous)
                and not _IsIconic(previous) and self._show_count == shows):
            log.info("Edge kept the foreground after the preload – giving it back")
            winui.force_foreground(previous)

    def show(self) -> bool:
        """Show, centre and activate the window (launching Edge if needed); True when our
        window ended up in the foreground. Returns within 10 s."""
        deadline = time.monotonic() + self.SHOW_TIMEOUT_S
        foreground = winui.foreground_window()
        with self._lock:
            self._show_count += 1
            self._ensure_watcher()
            hwnd = self._window()
            if hwnd is not None and foreground == hwnd and self._is_shown(hwnd):
                return True
            if foreground is not None and foreground != hwnd:
                self._previous = foreground
            anchor = self._previous
            if hwnd is None and not self._launch_pending() and not self._launch(preload=False):
                return False
        if hwnd is None:
            hwnd = self._wait_for_window(deadline - time.monotonic())
            if hwnd is None:
                log.warning("the Edge window did not appear in time")
                return False
        size = self._place(hwnd, anchor)
        _ShowWindowAsync(hwnd, SW_SHOW)
        ok = winui.force_foreground(hwnd)
        if size is not None:
            self._settle(hwnd, anchor, size, deadline)
        return ok and self.is_foreground()

    def hide(self, restore_previous: bool = False) -> None:
        """Hide the window; with ``restore_previous`` re-activate the window that was active
        before the last ``show()`` (Esc, hotkey toggle) if it still exists."""
        with self._lock:
            hwnd = self._window()
            previous = self._previous
        if hwnd is None:
            return
        if (restore_previous and previous and previous != hwnd and _IsWindow(previous)
                and _IsWindowVisible(previous) and not _IsIconic(previous)):
            winui.force_foreground(previous)
        _ShowWindowAsync(hwnd, SW_HIDE)

    def is_visible(self) -> bool:
        with self._lock:
            hwnd = self._window()
        return hwnd is not None and self._is_shown(hwnd)

    def is_foreground(self) -> bool:
        with self._lock:
            hwnd = self._window()
        return hwnd is not None and winui.foreground_window() == hwnd

    def close(self) -> None:
        """Close the window (app exit) and stop the watcher; no preload happens afterwards.
        Does not wait for Edge to exit."""
        with self._lock:
            self._closed = True
            self._watch_stop.set()
            hwnd = self._window()
            proc = self._proc
            self._hwnd = self._pid = None
            self._launch_deadline = 0.0
        if hwnd is not None:
            _PostMessageW(hwnd, WM_CLOSE, 0, 0)
        elif proc is not None and proc.poll() is None:
            proc.terminate()                 # our own launch that never produced a window

    # -- watcher (SPEC §15.10) -----------------------------------------------------------------
    def _ensure_watcher(self) -> None:
        """Start the watcher once (lock held by the caller)."""
        if self._watcher is None and not self._closed and self.WATCH:
            self._watcher = threading.Thread(target=self._watch, name="AppWindow-watch",
                                             daemon=True)
            self._watcher.start()

    def _watch(self) -> None:
        """Keep a hidden window ready: when ours has disappeared – closed by the user (X,
        Alt+F4, Ctrl+W) or Edge exited/crashed – and no launch is pending, preload a new one
        after ``REPRELOAD_DELAY_S``, once the old Edge has released the profile (its
        ``lockfile`` is gone; at most ``PROFILE_WAIT_S`` – a new launch must not race a browser
        that is still shutting down). Only failed relaunches back off (Edge keeps failing or
        exiting): when the relaunch produced no window, or its window vanished before anyone
        saw it, the next one waits ``RETRY_BASE_S`` · 2ⁿ, at most ``RETRY_MAX_S``. A window
        that was shown since the relaunch, or that lived ``STABLE_S``, resets the back-off – the
        user closing it after a search is the everyday case, not a failure (R2-WIN-2)."""
        missing_since: float | None = None
        present_since: float | None = None
        attempts = 0                         # relaunches since a window last proved fine
        last_attempt = 0.0
        with self._lock:
            shows_at_relaunch = self._show_count
        while not self._watch_stop.wait(self.WATCH_INTERVAL_S):
            with self._lock:
                if self._closed:
                    return
                present = self._window() is not None or self._launch_pending()
                shows = self._show_count
            now = time.monotonic()
            if present:
                missing_since = None
                present_since = present_since if present_since is not None else now
                if now - present_since >= self.STABLE_S:
                    attempts = 0
                continue
            present_since = None
            if missing_since is None:
                missing_since = now
                if shows != shows_at_relaunch:
                    attempts = 0             # it was shown since the relaunch: the user closed it
                log.info("the Edge window is gone – a hidden one will be prepared")
            backoff = (0.0 if attempts == 0
                       else min(self.RETRY_BASE_S * 2 ** (attempts - 1), self.RETRY_MAX_S))
            if now - missing_since < self.REPRELOAD_DELAY_S or now - last_attempt < backoff:
                continue
            if (os.path.exists(os.path.join(self.profile_dir, LOCKFILE))
                    and now - missing_since < self.PROFILE_WAIT_S):
                continue                     # the old Edge is still shutting down
            attempts += 1
            last_attempt = now
            shows_at_relaunch = shows
            try:
                self.preload()
            except Exception:
                log.exception("preloading the Edge window failed")

    # -- identification ------------------------------------------------------------------------
    def _exe_name(self) -> str:
        return os.path.basename(self._edge) if self._edge else "msedge.exe"

    def _window(self) -> int | None:
        """Validated handle of our window, or None. Lock held by the caller."""
        if self._hwnd is not None:
            info = winui.window_info(self._hwnd)
            if info is not None and winui.is_app_window(info, self.title, self._exe_name(),
                                                        self._pid):
                return self._hwnd
            self._hwnd = self._pid = None
        info = winui.find_app_window_info(self.title, self._exe_name())
        if info is None:
            return None
        self._hwnd, self._pid = info.hwnd, info.pid
        self._launch_deadline = 0.0
        return info.hwnd

    @staticmethod
    def _is_shown(hwnd: int) -> bool:
        return bool(_IsWindowVisible(hwnd)) and not _IsIconic(hwnd)

    def _launch_pending(self) -> bool:
        return time.monotonic() < self._launch_deadline

    def _wait_for_window(self, timeout: float) -> int | None:
        end = time.monotonic() + max(0.0, timeout)
        while True:
            with self._lock:
                hwnd = self._window()
                pending = self._launch_pending()
            if hwnd is not None:
                return hwnd
            if not pending or time.monotonic() >= end:
                return None
            time.sleep(self._POLL_S)

    # -- launching and placement ---------------------------------------------------------------
    def _launch(self, preload: bool) -> bool:
        """Start Edge off-screen without activation (lock held by the caller)."""
        edge = self._edge or winui.edge_path()
        if not edge:
            log.error("Microsoft Edge was not found – cannot show the window")
            return False
        self._edge = edge
        disable_background_mode(self.profile_dir)
        # SPEC §10.2 flags, plus: --disable-sync (Edge signs a new profile in with the Windows
        # account and shows an on-screen "we are now syncing your data" dialog – measured on
        # this PC; the private app profile must neither sync nor pop up dialogs), startup boost
        # off (SPEC §15.10: no msedge process may linger after the window closes – see
        # disable_background_mode) and an off-screen position for every launch (show() moves
        # the window into view).
        args = [edge, f"--app={self.url}", f"--user-data-dir={self.profile_dir}",
                "--window-size=1180,780", "--no-first-run", "--no-default-browser-check",
                "--disable-features=Translate,msEdgeStartupBoost",
                "--disable-renderer-backgrounding",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows", "--hide-crash-restore-bubble",
                "--disable-sync", f"--window-position={OFFSCREEN},{OFFSCREEN}"]
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = SW_SHOWMINNOACTIVE if preload else SW_SHOWNOACTIVATE
        try:
            self._proc = subprocess.Popen(args, cwd=config.app_dir(), startupinfo=startup,
                                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL, close_fds=True)
        except OSError as exc:
            log.error("could not start Edge: %s", exc)
            return False
        self._launch_deadline = time.monotonic() + self.LAUNCH_TIMEOUT_S
        log.info("Edge started (pid %s, %s)", self._proc.pid, "preload" if preload else "show")
        return True

    def _place(self, hwnd: int, anchor: int | None) -> tuple[int, int] | None:
        """Centre the (restored) window on the work area of ``anchor``'s monitor, keeping its
        size. All requests are asynchronous, so a hung Edge cannot block us. Returns the size
        used, or None when the window was left where it is (maximised)."""
        if _IsZoomed(hwnd):
            return None
        with _physical_pixels():
            work = _work_area(anchor)
            if _IsIconic(hwnd):
                size = _normal_size(hwnd)
                _ShowWindowAsync(hwnd, SW_SHOWNOACTIVATE)      # restore at its off-screen spot
            else:
                rect = _window_rect(hwnd)
                size = (rect[2] - rect[0], rect[3] - rect[1]) if rect else None
            if size is None:
                return None
            x, y = centred_position(size, work)
            _SetWindowPos(hwnd, None, x, y, 0, 0,
                          SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_ASYNCWINDOWPOS)
        return size

    def _settle(self, hwnd: int, anchor: int | None, size: tuple[int, int],
                deadline: float) -> None:
        """Moving to a monitor with another scale factor makes Edge resize itself; centre
        again with the new size once the move has been applied."""
        end = min(deadline, time.monotonic() + 0.5)
        while time.monotonic() < end:
            time.sleep(self._POLL_S)
            with _physical_pixels():
                rect = _window_rect(hwnd)
                if rect is None:
                    return
                current = (rect[2] - rect[0], rect[3] - rect[1])
                target = centred_position(current, _work_area(anchor))
                if (rect[0], rect[1]) == target:
                    return
                if current != size:
                    _SetWindowPos(hwnd, None, target[0], target[1], 0, 0,
                                  SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_ASYNCWINDOWPOS)
                    return
