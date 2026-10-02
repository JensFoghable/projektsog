"""Klippe plays (SPEC §18.4): when nobody has used the mouse or the keyboard for a while, the
pet breaks out of the widget and plays with the mouse pointer on the widget's monitor.

This is the main-process side. It decides *when*, renders the pet's sprite sheet (the widget
page drawn by headless Edge on a transparent background) and starts the helper process
``projektsog.petplay_child``, which draws the pet outside its box and moves the pointer. The
widget page hears about it through ``pet`` events: "out" → it hides its pet, "home" → it shows
it again.

A game starts only when

* Klippe is shown (``widget_enabled``) and may play (``widget_play``), and the widget page has
  told how the pet looks right now (its stage and outfit and where it sits),
* nobody has touched the mouse or the keyboard for ``widget_play_idle_minutes`` – once per
  such pause: the next game needs the user back first. The pet's age decides whether it feels
  like playing at all: an egg never, a baby always, a junior every other pause, a pro rarely,
* the screen is not locked, nothing runs in full screen on that monitor (nor a presentation or
  a game, as Windows sees it), and Windows does not activate windows under the mouse,
* Resolve is not playing back (a render is fine – Klippe gets bored waiting too) and no camera
  card is being transferred (Klippe is busy carrying files then).

"Vis legen nu" (``play_now``) starts a game as soon as the mouse has been still for a moment,
whatever the pause, the age (an egg plays as the baby it will be) or Resolve.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import logging
import math
import os
import random
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from ctypes import wintypes
from typing import Any

from . import config, winui
from .config import Config

log = logging.getLogger(__name__)

POSES = ("normal", "happy", "cheer", "oops")      # the cells of the sprite sheet
CELL_CSS = 240                                     # a cell (CSS pixels); the SVG is 200 in the middle
SHEET_SCALE = 2                                    # the sheet is rendered at 2× for crisp edges
STAGE_CHANCE = {"egg": 0.0, "baby": 1.0, "junior": 0.5, "pro": 0.2, "legend": 0.25}
OUTFITS = ("none", "color", "fusion", "audio", "deliver")
POLL_S = 1.0
MANUAL_POLL_S = 0.2
MANUAL_STILL_S = 1.5          # "Vis legen nu" waits until the mouse has been still this long …
MANUAL_WAIT_S = 20.0          # … at most this long
PLAYBACK_RECENT_S = 8.0       # the playhead moved this recently: Resolve is playing back
RESOLVE_FRESH_S = 12.0        # an older answer from Resolve does not tell whether it plays back
QUIT_WAIT_S = 2.0
SPRITE_TIMEOUT_S = 45.0
CHILD_MODULE = "projektsog.petplay_child"
TRANSFER_STATES = frozenset({"copying", "verifying", "deleting"})

MESSAGES = {
    "off": "Slå Klippe til først",
    "no-widget": "Klippe er ikke vist endnu – vent et øjeblik og prøv igen",
    "no-look": "Klippe er ikke klar endnu – vent et øjeblik og prøv igen",
    "locked": "Skærmen er låst",
    "focus-follows-mouse": ("Klippe kan ikke lege, når Windows aktiverer vinduer ved at holde musen "
                            "over dem"),
    "still": "Musen lå ikke stille – klik igen, og slip musen et par sekunder",
    "sprites": "Klippe kunne ikke tegnes – se loggen",
}

_CREATE_NO_WINDOW = 0x08000000
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --------------------------------------------------------------------------------------
# The desktop: last input, locked screen, full screen
# --------------------------------------------------------------------------------------

class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_shell32 = ctypes.WinDLL("shell32")


def _declare(dll: Any, name: str, restype: Any, *argtypes: Any) -> Any:
    fn = getattr(dll, name)
    fn.restype = restype
    fn.argtypes = list(argtypes)
    return fn


_GetLastInputInfo = _declare(_user32, "GetLastInputInfo", wintypes.BOOL, ctypes.POINTER(_LASTINPUTINFO))
_GetTickCount = _declare(_kernel32, "GetTickCount", wintypes.DWORD)
_OpenInputDesktop = _declare(_user32, "OpenInputDesktop", wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL,
                             wintypes.DWORD)
_CloseDesktop = _declare(_user32, "CloseDesktop", wintypes.BOOL, wintypes.HANDLE)
_GetUserObjectInformationW = _declare(_user32, "GetUserObjectInformationW", wintypes.BOOL, wintypes.HANDLE,
                                      ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                      ctypes.POINTER(wintypes.DWORD))
_SystemParametersInfoW = _declare(_user32, "SystemParametersInfoW", wintypes.BOOL, wintypes.UINT,
                                  wintypes.UINT, ctypes.c_void_p, wintypes.UINT)
_SHQueryUserNotificationState = _declare(_shell32, "SHQueryUserNotificationState", ctypes.c_long,
                                         ctypes.POINTER(ctypes.c_int))
_MonitorFromWindow = _declare(_user32, "MonitorFromWindow", wintypes.HANDLE, wintypes.HWND, wintypes.DWORD)
_GetMonitorInfoW = _declare(_user32, "GetMonitorInfoW", wintypes.BOOL, wintypes.HANDLE,
                            ctypes.POINTER(_MONITORINFO))
_GetWindowRect = _declare(_user32, "GetWindowRect", wintypes.BOOL, wintypes.HWND, ctypes.POINTER(wintypes.RECT))
_IsWindow = _declare(_user32, "IsWindow", wintypes.BOOL, wintypes.HWND)
_IsWindowVisible = _declare(_user32, "IsWindowVisible", wintypes.BOOL, wintypes.HWND)
_IsIconic = _declare(_user32, "IsIconic", wintypes.BOOL, wintypes.HWND)

UOI_NAME = 2
DESKTOP_READOBJECTS = 0x0001
SPI_GETACTIVEWINDOWTRACKING = 0x1000
MONITOR_DEFAULTTONEAREST = 2
QUNS_BUSY, QUNS_RUNNING_D3D_FULL_SCREEN, QUNS_PRESENTATION_MODE = 2, 3, 4
SHELL_CLASSES = frozenset({"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"})


def last_input_tick() -> int:
    """The tick count of the last keyboard or mouse input in this session (``GetLastInputInfo``).
    Moving the pointer from a program (``SetCursorPos``) does not change it."""
    info = _LASTINPUTINFO(cbSize=ctypes.sizeof(_LASTINPUTINFO))
    if not _GetLastInputInfo(ctypes.byref(info)):
        return 0
    return int(info.dwTime)


def idle_seconds() -> float:
    return ((_GetTickCount() - last_input_tick()) & 0xFFFFFFFF) / 1000.0   # wraps after 49.7 days


def desktop_locked() -> bool:
    """True while the screen is locked (or another desktop, such as a UAC prompt, has the input)."""
    handle = _OpenInputDesktop(0, False, DESKTOP_READOBJECTS)
    if not handle:
        return True
    try:
        name = ctypes.create_unicode_buffer(64)
        needed = wintypes.DWORD()
        if not _GetUserObjectInformationW(handle, UOI_NAME, name, ctypes.sizeof(name), ctypes.byref(needed)):
            return True
        return name.value.casefold() != "default"
    finally:
        _CloseDesktop(handle)


def focus_follows_mouse() -> bool:
    """Windows' "activate a window by hovering over it": moving the pointer would move the focus."""
    value = wintypes.BOOL()
    if not _SystemParametersInfoW(SPI_GETACTIVEWINDOWTRACKING, 0, ctypes.byref(value), 0):
        return False
    return bool(value.value)


def windows_quiet() -> bool:
    """Windows itself holds back notifications: a full-screen program, a game, presentation mode."""
    state = ctypes.c_int()
    if _SHQueryUserNotificationState(ctypes.byref(state)) != 0:
        return False
    return state.value in (QUNS_BUSY, QUNS_RUNNING_D3D_FULL_SCREEN, QUNS_PRESENTATION_MODE)


def fullscreen_beside(hwnd: int) -> bool:
    """True when the window in front covers the whole monitor ``hwnd`` is on (a video, a game,
    Resolve's cinema viewer …)."""
    from .widget import _PhysicalPixels
    front = winui.foreground_window()
    if not front or front == hwnd:
        return False
    info = winui.window_info(front)
    if info is None or info.cls in SHELL_CLASSES:
        return False
    with _PhysicalPixels():
        monitor = _MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
        if not monitor or monitor != _MonitorFromWindow(front, MONITOR_DEFAULTTONEAREST):
            return False
        mi = _MONITORINFO(cbSize=ctypes.sizeof(_MONITORINFO))
        rect = wintypes.RECT()
        if not _GetMonitorInfoW(monitor, ctypes.byref(mi)) or not _GetWindowRect(front, ctypes.byref(rect)):
            return False
    m = mi.rcMonitor
    return rect.left <= m.left and rect.top <= m.top and rect.right >= m.right and rect.bottom >= m.bottom


def window_shown(hwnd: int) -> bool:
    return bool(_IsWindow(hwnd) and _IsWindowVisible(hwnd) and not _IsIconic(hwnd))


class DesktopProbe:
    """The desktop as the rules see it (tests replace it)."""

    idle_s = staticmethod(idle_seconds)
    input_tick = staticmethod(last_input_tick)
    locked = staticmethod(desktop_locked)
    focus_follows_mouse = staticmethod(focus_follows_mouse)
    window_shown = staticmethod(window_shown)

    @staticmethod
    def fullscreen(hwnd: int) -> bool:
        return windows_quiet() or fullscreen_beside(hwnd)


# --------------------------------------------------------------------------------------
# The rules
# --------------------------------------------------------------------------------------

def play_blocker(*, enabled: bool, widget: bool, look: bool, locked: bool, focus_follows: bool,
                 idle_s: float, threshold_s: float, played: bool, transfer: bool, playback: bool,
                 fullscreen: bool, manual: bool) -> str | None:
    """Why no game may start now (None: it may). ``manual``: "Vis legen nu" – it only waits for
    a still mouse."""
    if not enabled:
        return "off"
    if not widget:
        return "no-widget"
    if not look:
        return "no-look"
    if locked:
        return "locked"
    if focus_follows:
        return "focus-follows-mouse"
    if manual:
        return None if idle_s >= MANUAL_STILL_S else "busy"
    if idle_s < threshold_s:
        return "busy"
    if played:
        return "played"
    if transfer:
        return "transfer"
    if playback:
        return "playback"
    if fullscreen:
        return "fullscreen"
    return None


def _box(value: Any, keys: tuple[str, ...]) -> tuple[float, ...] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("Ugyldigt udseende")
    out = []
    for key in keys:
        number = value.get(key)
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) \
                or abs(number) > 100_000:
            raise ValueError(f"Ugyldig værdi: {key}")
        out.append(round(float(number), 1))
    if any(v <= 0 for k, v in zip(keys, out) if k in ("w", "h")):
        raise ValueError("Ugyldig størrelse")
    return tuple(out)


def clean_look(data: Any) -> dict[str, Any]:
    """The widget page's report: ``{"stage", "outfit", "pet": {x, y, w, h}, "view": {w, h}}``."""
    if not isinstance(data, dict):
        raise ValueError("Ugyldigt udseende")
    stage = data.get("stage")
    if stage not in STAGE_CHANCE:
        raise ValueError("Ugyldig værdi: stage")
    outfit = data.get("outfit", "none")
    if outfit not in OUTFITS:
        raise ValueError("Ugyldig værdi: outfit")
    return {"stage": stage, "outfit": outfit, "pet": _box(data.get("pet"), ("x", "y", "w", "h")),
            "view": _box(data.get("view"), ("w", "h"))}


# --------------------------------------------------------------------------------------
# The sprite sheet
# --------------------------------------------------------------------------------------

def sprite_signature(web_dir: str) -> str:
    """Changes whenever the pet's drawing (widget.html/css/js) or the sheet layout changes."""
    digest = hashlib.sha1(f"{POSES}|{CELL_CSS}|{SHEET_SCALE}".encode())
    for name in ("widget.html", "widget.css", "widget.js"):
        try:
            with open(os.path.join(web_dir, name), "rb") as fh:
                digest.update(fh.read())
        except OSError:
            digest.update(b"-")
    return digest.hexdigest()[:12]


def png_size(path: str) -> tuple[int, int] | None:
    try:
        with open(path, "rb") as fh:
            head = fh.read(24)
    except OSError:
        return None
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        return None
    return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")


class SpriteSheets:
    """Renders and caches one sprite sheet per stage and outfit:
    ``<folder>/klippe-<stage>-<outfit>-<signature>.png``."""

    def __init__(self, base_url: str, folder: str | None = None, *, web_dir: str | None = None,
                 edge: str | None = None, profile_dir: str | None = None,
                 run: Callable[..., Any] = subprocess.run) -> None:
        self.base_url = base_url.rstrip("/")
        self.folder = folder or os.path.join(config.app_dir(), "pet")
        self.web_dir = web_dir or os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
        self._edge = edge
        self.profile_dir = profile_dir or os.path.join(config.app_dir(), "edge-sprites")
        self._run = run

    def url(self, stage: str, outfit: str) -> str:
        return (f"{self.base_url}/widget.html?sprites={','.join(POSES)}&stage={stage}"
                f"&outfit={outfit}&cell={CELL_CSS}")

    def path(self, stage: str, outfit: str) -> str:
        return os.path.join(self.folder, f"klippe-{stage}-{outfit}-{sprite_signature(self.web_dir)}.png")

    def expected_size(self) -> tuple[int, int]:
        return CELL_CSS * len(POSES) * SHEET_SCALE, CELL_CSS * SHEET_SCALE

    def ensure(self, stage: str, outfit: str) -> str | None:
        """The sheet's path – rendered first if needed; None when it cannot be rendered."""
        path = self.path(stage, outfit)
        if png_size(path) == self.expected_size():
            return path
        os.makedirs(self.folder, exist_ok=True)
        self._remove_old(os.path.basename(path).rsplit("-", 1)[1])
        temp = path + ".part.png"
        if not self._render(self.url(stage, outfit), temp):
            return None
        if png_size(temp) != self.expected_size():
            log.warning("the sprite sheet came out wrong: %s", png_size(temp))
            _remove(temp)
            return None
        os.replace(temp, path)
        log.info("rendered Klippe's sprites (%s, %s)", stage, outfit)
        return path

    def _render(self, url: str, out: str) -> bool:
        edge = self._edge or winui.edge_path()
        if not edge:
            log.error("Microsoft Edge was not found – cannot draw Klippe's sprites")
            return False
        _remove(out)
        width, height = CELL_CSS * len(POSES), CELL_CSS
        args = [edge, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--mute-audio",
                "--no-first-run", "--no-default-browser-check", "--disable-extensions", "--disable-sync",
                "--default-background-color=00000000", f"--force-device-scale-factor={SHEET_SCALE}",
                f"--user-data-dir={self.profile_dir}", f"--window-size={width},{height}",
                f"--screenshot={out}", url]
        try:
            self._run(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                      timeout=SPRITE_TIMEOUT_S, creationflags=_CREATE_NO_WINDOW, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("rendering Klippe's sprites failed: %s", exc)
            return False
        return os.path.isfile(out)

    def _remove_old(self, keep_suffix: str) -> None:
        try:
            names = os.listdir(self.folder)
        except OSError:
            return
        for name in names:
            if name.startswith("klippe-") and name.endswith(".png") and not name.endswith(keep_suffix):
                _remove(os.path.join(self.folder, name))


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


# --------------------------------------------------------------------------------------
# The helper process
# --------------------------------------------------------------------------------------

def child_argv() -> list[str]:
    """``[pythonw.exe next to sys.executable (else sys.executable), -m, CHILD_MODULE]``."""
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return [pythonw if os.path.isfile(pythonw) else sys.executable, "-m", CHILD_MODULE]


class _Child:
    """One running game: events from its stdout; ``stop()`` asks it to end (and kills it if it
    does not)."""

    def __init__(self, argv: list[str], on_out: Callable[[], None],
                 on_exit: Callable[[str], None]) -> None:
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(p for p in (_REPO_ROOT, env.get("PYTHONPATH")) if p)
        self._on_out = on_out
        self._on_exit = on_exit
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, cwd=config.app_dir(), env=env,
                                     creationflags=_CREATE_NO_WINDOW, close_fds=True)
        threading.Thread(target=self._read, name=f"petplay-{self.proc.pid}", daemon=True).start()

    def _read(self) -> None:
        reason: str | None = None
        stdout = self.proc.stdout
        try:
            for raw in stdout:                         # type: ignore[union-attr]
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                if message.get("event") == "out":
                    self._on_out()
                elif message.get("event") == "home":
                    reason = str(message.get("reason") or "done")
        except (OSError, ValueError):
            pass
        finally:
            try:
                self.proc.wait(5.0)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self._on_exit(reason or "error")

    def alive(self) -> bool:
        return self.proc.poll() is None

    def stop(self, wait: float = QUIT_WAIT_S) -> None:
        try:
            if self.proc.stdin is not None:
                self.proc.stdin.write(b"quit\n")
                self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(wait)
        except subprocess.TimeoutExpired:
            log.warning("the game did not stop – ending it")
            self.proc.kill()


# --------------------------------------------------------------------------------------
# The scheduler
# --------------------------------------------------------------------------------------

class PetPlay:
    """Watches the rules and starts a game when they allow one."""

    def __init__(self, cfg: Config, bus: Any, *, widget: Any = None, bridge: Any = None,
                 importer: Any = None, base_url: str = "", probe: Any = None,
                 sprites: Any = None, spawn: Callable[..., Any] | None = None,
                 rng: random.Random | None = None, clock: Callable[[], float] = time.monotonic,
                 log_file: str | None = None) -> None:
        self.cfg = cfg
        self.bus = bus
        self._widget = widget
        self._bridge = bridge
        self._importer = importer
        self._probe = probe or DesktopProbe()
        self._sprites = sprites or SpriteSheets(base_url)
        self._spawn = spawn or _Child
        self._rng = rng or random.Random()
        self._clock = clock
        self._log_file = log_file
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._look: dict[str, Any] | None = None
        self._child: Any = None
        self._child_manual = False
        self._child_out = False
        self._state = "ready"                 # ready | waiting | out
        self._message = ""
        self._manual_until: float | None = None
        self._played_tick: int | None = None  # the pause that already had its game
        self._rolled: tuple[int, bool] | None = None   # (pause, feels like playing?)
        self._timecode: tuple[Any, str] | None = None
        self._moved_at = -math.inf

    # -- lifecycle -------------------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self.cfg.on_change(lambda _snapshot: self._wake.set())
        self._thread = threading.Thread(target=self._run, name="petplay", daemon=True)
        self._thread.start()

    def close(self) -> None:
        """App exit (or Klippe switched off): end a running game – the pointer is put back."""
        self._stop.set()
        self._wake.set()
        with self._lock:
            child = self._child
        if child is not None:
            child.stop()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(2.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.step()
            except Exception:
                log.exception("petplay step failed")
            with self._lock:
                waiting = self._manual_until is not None
            self._wake.wait(MANUAL_POLL_S if waiting else POLL_S)
            self._wake.clear()

    # -- from the page and the settings ----------------------------------------------------
    def set_look(self, data: Any) -> dict[str, Any]:
        look = clean_look(data)
        with self._lock:
            self._look = look
        return {"ok": True}

    def play_now(self) -> dict[str, Any]:
        """"Vis legen nu": a game as soon as the mouse has been still for a moment."""
        with self._lock:
            if not self.cfg.get("widget_enabled", False):
                raise ValueError(MESSAGES["off"])
            if self._child is not None:
                raise ValueError("Klippe leger allerede 🎈")
            self._manual_until = self._clock() + MANUAL_WAIT_S
            self._set_state("waiting", "")
        self._wake.set()
        return self.status()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {"state": self._state, "message": self._message,
                    "enabled": bool(self.cfg.get("widget_play", True))}

    def _set_state(self, state: str, message: str = "", reason: str | None = None) -> None:
        self._state, self._message = state, message
        self.bus.publish("pet", {"state": state, "message": message, "reason": reason})

    # -- one step --------------------------------------------------------------------------
    def step(self) -> None:
        now = self._clock()
        self._watch_playhead(now)
        with self._lock:
            child = self._child
            manual = self._manual_until is not None
            expired = manual and now > self._manual_until
        if child is not None:
            if not self._may_go_on():
                log.info("Klippe was switched off during a game")
                child.stop()
            return
        if expired:
            self._end_manual(MESSAGES["still"])
            return
        hwnd = getattr(self._widget, "hwnd", None)
        shown = bool(hwnd) and self._probe.window_shown(hwnd)
        idle = self._probe.idle_s()
        tick = self._probe.input_tick()
        enabled = bool(self.cfg.get("widget_enabled", False))
        if not manual:
            enabled = enabled and bool(self.cfg.get("widget_play", True))
        threshold = max(1, int(self.cfg.get("widget_play_idle_minutes", 5))) * 60.0
        with self._lock:
            look = self._look
            played = self._played_tick == tick
        quiet = not manual and idle >= threshold and not played
        reason = play_blocker(
            enabled=enabled, widget=shown, look=look is not None, locked=self._probe.locked(),
            focus_follows=self._probe.focus_follows_mouse(), idle_s=idle, threshold_s=threshold,
            played=played, transfer=quiet and self._transfer_busy(), playback=quiet and self._playing(now),
            fullscreen=quiet and bool(hwnd) and self._probe.fullscreen(hwnd), manual=manual)
        if reason is not None:
            if manual and reason != "busy":
                self._end_manual(MESSAGES.get(reason, MESSAGES["still"]))
            return
        assert look is not None and hwnd
        stage = look["stage"]
        if not manual:
            if self._rolled is None or self._rolled[0] != tick:
                self._rolled = (tick, self._rng.random() < STAGE_CHANCE.get(stage, 0.0))
            if not self._rolled[1]:
                return
        self._launch(look, hwnd, tick, manual)

    def _may_go_on(self) -> bool:
        if self._stop.is_set() or not self.cfg.get("widget_enabled", False):
            return False
        return self._child_manual or bool(self.cfg.get("widget_play", True))

    def _end_manual(self, message: str) -> None:
        with self._lock:
            self._manual_until = None
            self._set_state("ready", message)

    def _transfer_busy(self) -> bool:
        if self._importer is None:
            return False
        try:
            job = self._importer.job()
        except Exception:
            return False
        return bool(job) and job.get("state") in TRANSFER_STATES

    def _watch_playhead(self, now: float) -> None:
        act = self._activity()
        if not act or act.get("rendering"):
            self._timecode = None
            return
        current = (act.get("project"), act.get("timecode") or "")
        if (self._timecode is not None and current[1] and current[0] == self._timecode[0]
                and current[1] != self._timecode[1]):
            self._moved_at = now
        self._timecode = current

    def _activity(self) -> dict[str, Any] | None:
        if self._bridge is None:
            return None
        try:
            return self._bridge.activity(max_age=120.0)
        except Exception:
            return None

    def _playing(self, now: float) -> bool:
        """Resolve plays back – or might: an old answer from a busy Resolve does not tell."""
        if now - self._moved_at < PLAYBACK_RECENT_S:
            return True
        act = self._activity()
        return bool(act) and not act.get("rendering") and float(act.get("age") or 0) > RESOLVE_FRESH_S

    # -- a game ----------------------------------------------------------------------------
    def _launch(self, look: dict[str, Any], hwnd: int, tick: int, manual: bool) -> None:
        stage = "baby" if look["stage"] == "egg" else look["stage"]
        with self._lock:
            self._played_tick = tick                 # one game per pause, whatever happens
        sheet = self._sprites.ensure(stage, look["outfit"])
        if sheet is None:
            if manual:
                self._end_manual(MESSAGES["sprites"])
            return
        argv = child_argv() + [
            "--sprites", sheet, "--poses", ",".join(POSES), "--cell", str(CELL_CSS),
            "--sheet-scale", str(SHEET_SCALE), "--widget", str(int(hwnd)), "--stage", stage,
            "--seed", str(self._rng.randrange(1 << 30)), "--input-tick", str(tick),
            "--log-file", self._log_file or os.path.join(config.log_dir(), "petplay.log")]
        if look.get("pet"):
            argv += ["--pet", ",".join(str(v) for v in look["pet"])]
        if look.get("view"):
            argv += ["--view", ",".join(str(v) for v in look["view"])]
        with self._lock:
            if self._stop.is_set():
                return
            try:
                self._child = self._spawn(argv, self._on_out, self._on_exit)
            except OSError as exc:
                log.error("could not start Klippe's game: %s", exc)
                if manual:
                    self._manual_until = None
                    self._set_state("ready", MESSAGES["sprites"])
                return
            self._child_manual = manual
            self._child_out = False
            self._manual_until = None
        log.info("Klippe plays (%s%s)", stage, ", Vis legen nu" if manual else "")

    def _on_out(self) -> None:
        with self._lock:
            self._child_out = True
            self._set_state("out")

    def _on_exit(self, reason: str) -> None:
        with self._lock:
            message = "" if reason in ("done", "touched", "quit") else (
                "Skærmen blev låst" if reason == "locked" else "Legen stoppede – se loggen")
            if reason == "touched" and self._child_manual and not self._child_out:
                message = MESSAGES["still"]
            self._child = None
            self._child_manual = self._child_out = False
            self._set_state("ready", message, reason)
        log.info("Klippe is home (%s)", reason)
        self._wake.set()
