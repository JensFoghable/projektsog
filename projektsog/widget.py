"""Klippe – the pet widget: a small window at the side of the second monitor (SPEC §18).

The window is an Edge ``--app`` window showing ``/widget.html`` with its own private profile,
so it is independent of the search window (its own process; closing one never affects the
other). It is placed at the bottom right of the chosen monitor's work area (``widget_monitor``:
"auto" = the first monitor that is not the primary one, else the primary), or where the user
dragged it last (``widget_position``), and kept on top of other windows (``widget_on_top``).

A thread follows ``widget_enabled``: on → the window is launched (without taking the focus),
off → it is closed. Closing the window with its X turns ``widget_enabled`` off, so it stays
closed until it is switched on again.
"""

from __future__ import annotations

import ctypes
import logging
import subprocess
import threading
import time
from collections.abc import Callable
from ctypes import wintypes
from typing import Any, NamedTuple

from . import config, winui
from .config import Config
from .window import OFFSCREEN, SW_SHOWMINNOACTIVE, disable_background_mode

log = logging.getLogger(__name__)

TITLE = "Klippe – Projektsøg"       # the page title: how the window is found
SIZE = (260, 480)                    # CSS pixels (scaled with the monitor's DPI when placed)
MARGIN = 16
POLL_S = 2.0
LAUNCH_TIMEOUT_S = 15.0
SAVE_POSITION_AFTER_S = 3.0          # a dragged window is remembered once it stood still

_user32 = ctypes.WinDLL("user32", use_last_error=True)


def _declare(name: str, restype: Any, *argtypes: Any) -> Any:
    fn = getattr(_user32, name)
    fn.restype = restype
    fn.argtypes = list(argtypes)
    return fn


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


_MONITORENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HANDLE, wintypes.HDC,
                                      ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
_EnumDisplayMonitors = _declare("EnumDisplayMonitors", wintypes.BOOL, wintypes.HDC,
                                ctypes.POINTER(wintypes.RECT), _MONITORENUMPROC, wintypes.LPARAM)
_GetMonitorInfoW = _declare("GetMonitorInfoW", wintypes.BOOL, wintypes.HANDLE,
                            ctypes.POINTER(_MONITORINFO))
_SetWindowPos = _declare("SetWindowPos", wintypes.BOOL, wintypes.HANDLE, wintypes.HANDLE,
                         ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT)
_GetWindowRect = _declare("GetWindowRect", wintypes.BOOL, wintypes.HANDLE,
                          ctypes.POINTER(wintypes.RECT))
_ShowWindowAsync = _declare("ShowWindowAsync", wintypes.BOOL, wintypes.HANDLE, ctypes.c_int)
_PostMessageW = _declare("PostMessageW", wintypes.BOOL, wintypes.HANDLE, wintypes.UINT,
                         wintypes.WPARAM, wintypes.LPARAM)
_IsIconic = _declare("IsIconic", wintypes.BOOL, wintypes.HANDLE)
try:
    _GetDpiForWindow = _declare("GetDpiForWindow", wintypes.UINT, wintypes.HANDLE)
except AttributeError:          # Windows < 10 1607
    _GetDpiForWindow = None
try:
    _SetThreadDpiAwarenessContext = _declare("SetThreadDpiAwarenessContext", wintypes.HANDLE,
                                             wintypes.HANDLE)
except AttributeError:
    _SetThreadDpiAwarenessContext = None

MONITORINFOF_PRIMARY = 1
SW_SHOWNOACTIVATE = 4
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE, SWP_ASYNCWINDOWPOS = 0x1, 0x2, 0x10, 0x4000
HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
WM_CLOSE = 0x0010


class Monitor(NamedTuple):
    """A monitor's work area (physical pixels) and whether it is the primary one."""
    left: int
    top: int
    right: int
    bottom: int
    primary: bool


class _PhysicalPixels:
    """Work in physical pixels on this thread (per-monitor DPI aware)."""

    def __enter__(self) -> None:
        self._previous = None
        if _SetThreadDpiAwarenessContext is not None:
            self._previous = _SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))

    def __exit__(self, *exc: object) -> None:
        if self._previous:
            _SetThreadDpiAwarenessContext(ctypes.c_void_p(self._previous))


def monitors() -> list[Monitor]:
    """The work areas of all monitors, in physical pixels."""
    found: list[Monitor] = []

    def callback(handle: int, _hdc: int, _rect: Any, _data: int) -> bool:
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(info)
        if _GetMonitorInfoW(handle, ctypes.byref(info)):
            r = info.rcWork
            found.append(Monitor(r.left, r.top, r.right, r.bottom,
                                 bool(info.dwFlags & MONITORINFOF_PRIMARY)))
        return True

    with _PhysicalPixels():
        _EnumDisplayMonitors(None, None, _MONITORENUMPROC(callback), 0)
    return found


def choose_monitor(found: list[Monitor], preference: str = "auto") -> Monitor:
    """``auto``: the first secondary monitor (left to right), else the primary; ``primary``:
    the primary one."""
    if not found:
        return Monitor(0, 0, 1280, 720, True)
    primary = next((m for m in found if m.primary), found[0])
    if preference == "primary":
        return primary
    others = sorted((m for m in found if not m.primary), key=lambda m: (m.left, m.top))
    return others[0] if others else primary


def bottom_right(work: Monitor, size: tuple[int, int], margin: int = MARGIN) -> tuple[int, int]:
    width, height = size
    return (max(work.left, work.right - width - margin),
            max(work.top, work.bottom - height - margin))


def scaled_size(hwnd: int | None, size: tuple[int, int] = SIZE) -> tuple[int, int]:
    """``size`` (CSS pixels) in physical pixels for the window's monitor (125 % → ×1.25)."""
    dpi = _GetDpiForWindow(hwnd) if (_GetDpiForWindow is not None and hwnd) else 0
    scale = dpi / 96 if dpi else 1.0
    return round(size[0] * scale), round(size[1] * scale)


def parse_position(text: str, found: list[Monitor]) -> tuple[int, int] | None:
    """A saved ``"x,y"`` – only when that point still lies on a monitor."""
    try:
        x, y = (int(part) for part in str(text or "").split(","))
    except ValueError:
        return None
    if any(m.left <= x < m.right and m.top <= y < m.bottom for m in found):
        return x, y
    return None


class PetWindow:
    """Shows and hides the widget window following ``widget_enabled``."""

    def __init__(self, cfg: Config, url: str, profile_dir: str | None = None, *,
                 edge: str | None = None,
                 monitors_fn: Callable[[], list[Monitor]] = monitors) -> None:
        self.cfg = cfg
        self.url = url
        self.profile_dir = profile_dir or config.widget_profile_dir()
        self._edge = edge
        self._monitors = monitors_fn
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._hwnd: int | None = None
        self._proc: subprocess.Popen | None = None
        self._closing = False                 # we closed it (setting off / app exit)
        self._top: bool | None = None
        self._last_rect: tuple[int, int, int, int] | None = None
        self._rect_since = 0.0
        self._saved_rect: tuple[int, int, int, int] | None = None

    @property
    def hwnd(self) -> int | None:
        """The widget window (None while it is not shown)."""
        return self._hwnd

    # -- lifecycle -------------------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self.cfg.on_change(lambda _snapshot: self._wake.set())
        self._thread = threading.Thread(target=self._run, name="pet-widget", daemon=True)
        self._thread.start()

    def close(self) -> None:
        """App exit: close the window and stop following the setting."""
        self._stop.set()
        self._wake.set()
        self._close_window()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.step()
            except Exception:
                log.exception("pet widget step failed")
            self._wake.wait(POLL_S)
            self._wake.clear()

    # -- one step --------------------------------------------------------------------------
    def step(self) -> None:
        wanted = bool(self.cfg.get("widget_enabled", False))
        hwnd = self._window()
        if not wanted:
            if hwnd is not None:
                self._close_window()
            return
        if hwnd is None:
            if self._hwnd is not None and not self._closing:
                # It was there and we did not close it: the user closed it (X). Stay closed.
                log.info("the pet widget was closed by the user")
                self._hwnd = None
                self.cfg.update({"widget_enabled": False})
                return
            self._hwnd = None
            self._closing = False
            self._launch_and_place()
            return
        self._hwnd = hwnd               # (also adopts a window left by an earlier run)
        self._apply_on_top(hwnd)
        self._remember_position(hwnd)

    def _window(self) -> int | None:
        info = winui.find_app_window_info(TITLE, "msedge.exe")
        return info.hwnd if info is not None else None

    def _launch_and_place(self) -> None:
        edge = self._edge or winui.edge_path()
        if not edge:
            log.error("Microsoft Edge was not found – cannot show the pet widget")
            return
        previous = winui.foreground_window()
        disable_background_mode(self.profile_dir)
        width, height = SIZE
        args = [edge, f"--app={self.url}", f"--user-data-dir={self.profile_dir}",
                f"--window-size={width},{height}", f"--window-position={OFFSCREEN},{OFFSCREEN}",
                "--no-first-run", "--no-default-browser-check", "--disable-sync",
                "--disable-features=Translate,msEdgeStartupBoost", "--hide-crash-restore-bubble",
                "--disable-renderer-backgrounding", "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows"]
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = SW_SHOWMINNOACTIVE
        try:
            self._proc = subprocess.Popen(args, cwd=config.app_dir(), startupinfo=startup,
                                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL, close_fds=True)
        except OSError as exc:
            log.error("could not start Edge for the pet widget: %s", exc)
            return
        deadline = time.monotonic() + LAUNCH_TIMEOUT_S
        hwnd = None
        while hwnd is None and time.monotonic() < deadline and not self._stop.is_set():
            time.sleep(0.1)
            hwnd = self._window()
        if hwnd is None:
            log.warning("the pet widget window did not appear")
            return
        self._hwnd = hwnd
        self._top = None
        self._place(hwnd)
        _ShowWindowAsync(hwnd, SW_SHOWNOACTIVATE)
        self._apply_on_top(hwnd)
        # Chromium may activate its new window by itself: give the focus back.
        time.sleep(0.4)
        if winui.foreground_window() == hwnd and previous and previous != hwnd:
            winui.force_foreground(previous)
        log.info("pet widget shown")

    def _place(self, hwnd: int) -> None:
        with _PhysicalPixels():
            found = self._monitors()
            saved = parse_position(self.cfg.get("widget_position", ""), found)
            if _IsIconic(hwnd):
                _ShowWindowAsync(hwnd, SW_SHOWNOACTIVATE)
                time.sleep(0.1)
            for _ in range(3):        # a move to a monitor with another scale resizes the window
                rect = wintypes.RECT()
                if not _GetWindowRect(hwnd, ctypes.byref(rect)):
                    return
                size = scaled_size(hwnd)  # Edge may remember an older size: always ours
                target = saved or bottom_right(choose_monitor(found, self.cfg.get("widget_monitor", "auto")), size)
                if (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top) == (*target, *size):
                    break
                _SetWindowPos(hwnd, None, target[0], target[1], size[0], size[1], SWP_NOACTIVATE)
                time.sleep(0.25)
            self._saved_rect = self._last_rect = self._rect(hwnd)

    def _apply_on_top(self, hwnd: int) -> None:
        top = bool(self.cfg.get("widget_on_top", True))
        if top != self._top:
            _SetWindowPos(hwnd, HWND_TOPMOST if top else HWND_NOTOPMOST, 0, 0, 0, 0,
                          SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_ASYNCWINDOWPOS)
            self._top = top

    def _rect(self, hwnd: int) -> tuple[int, int, int, int] | None:
        rect = wintypes.RECT()
        with _PhysicalPixels():
            if not _GetWindowRect(hwnd, ctypes.byref(rect)):
                return None
        return rect.left, rect.top, rect.right, rect.bottom

    def _remember_position(self, hwnd: int) -> None:
        """A window the user dragged somewhere else is placed there next time."""
        if _IsIconic(hwnd):
            return
        rect = self._rect(hwnd)
        if rect is None or rect[0] <= OFFSCREEN + 100:
            return
        now = time.monotonic()
        if rect != self._last_rect:
            self._last_rect, self._rect_since = rect, now
            return
        if rect != self._saved_rect and now - self._rect_since >= SAVE_POSITION_AFTER_S:
            self._saved_rect = rect
            self.cfg.update({"widget_position": f"{rect[0]},{rect[1]}"})

    def _close_window(self) -> None:
        hwnd = self._window()
        self._closing = True
        self._hwnd = None
        if hwnd is not None:
            _PostMessageW(hwnd, WM_CLOSE, 0, 0)
        elif self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()


def widget_url(base_url: str) -> str:
    return base_url.rstrip("/") + "/widget.html"
