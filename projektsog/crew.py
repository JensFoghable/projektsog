"""The robot crew (SPEC §21.2, §21.3): while a Claude session builds in DaVinci Resolve, Klippe
directs a swarm of little robots that edit a timeline beside the widget.

This is the main-process side:

* ``KoeWatch`` reads the Claude sessions' Resolve queue (``koe.py``, outside this repo): which
  session holds Resolve right now. The queue's state file is found through its registered link
  handler (``resolvekoe:``) – never a fixed path – and it is only ever read.
* ``Crew`` follows the builds and tells the widget page (SSE ``bygger``: the mini robots in the
  box, the megaphone, the fireworks when a build is done). When nobody has touched the mouse or
  the keyboard for a few seconds it starts the helper ``projektsog.crew_child``, which lets the
  robots out of the box onto the widget's monitor; any touch sends them back (the helper sees
  the input itself) and they come out again after the next moment of stillness. With the AWP
  Klippe shoots a naughty robot now and then: the helper's ``aim``/``shot``/``aim-end`` become
  SSE ``robot`` for the widget, where Klippe takes aim.
* A demo build ("🤖 Vis robotterne"), or a demo call that rings like a real session ("📞 Prøv
  telefonen"), shows it all without a session.
"""

from __future__ import annotations

import ctypes
import json
import logging
import math
import os
import random
import subprocess
import threading
import time
import winreg
from collections.abc import Callable
from ctypes import wintypes
from typing import Any

from . import config, petplay
from .config import Config

log = logging.getLogger(__name__)

ROBOT_POSES = ("robot-a", "robot-b", "robot-baer", "robot-klip", "robot-hop", "robot-fraek", "robot-panik")
ROBOT_CELL_CSS = 120                       # a robot's cell on the sprite sheet (CSS pixels)
ROBOTS = {"egg": 4, "baby": 5, "junior": 7, "pro": 9, "legend": 12}   # how many, by Klippe's stage
POLL_S = 0.5
CREW_IDLE_S = 3.0             # the robots come out after this much stillness …
BUILD_SETTLE_S = 2.0          # … once the build has run this long (the "Byg nu" click is input too)
DEMO_S = 45.0
CLOSE_WAIT_S = 0.3            # app exit: the helper is told to quit, and ends on stdin EOF anyway
STOP_WAIT_S = 2.0
RENDER_RETRY_S = 600.0        # the sprites could not be drawn: try again this much later
CHILD_MODULE = "projektsog.crew_child"

DEMO_URI = "projektsog:demo"
DEMO_TAG = "demo:opkald"
DEMO_NAME = "Demo"
DEMO_TEXT = "Robotterne øver sig"

KOE_READ_S = 2.0              # the queue's state is read this often …
KOE_LOOKUP_S = 60.0           # … and its link handler looked up again this often while missing
KOE_STALE_S = 3600.0          # a holder the queue has not heard from for an hour is gone
KOE_MAX_BYTES = 4 * 1024 * 1024
HANDLER_KEYS = ((winreg.HKEY_CURRENT_USER, r"Software\Classes\resolvekoe\shell\open\command"),
                (winreg.HKEY_CLASSES_ROOT, r"resolvekoe\shell\open\command"))
TEXT_MAX = 120

MESSAGES = {
    "off": "Slå Klippe til først",
    "building": "En Claude-session bygger allerede – robotterne er i gang 🤖",
    "no-board": "Beskederne er ikke startet",
    "uri": "Ukendt handling",
}

# --------------------------------------------------------------------------------------
# Windows: processes, command lines
# --------------------------------------------------------------------------------------

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_shell32 = ctypes.WinDLL("shell32", use_last_error=True)


def _declare(dll: Any, name: str, restype: Any, *argtypes: Any) -> Any:
    fn = getattr(dll, name)
    fn.restype = restype
    fn.argtypes = list(argtypes)
    return fn


_OpenProcess = _declare(_kernel32, "OpenProcess", wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_GetExitCodeProcess = _declare(_kernel32, "GetExitCodeProcess", wintypes.BOOL, wintypes.HANDLE,
                               ctypes.POINTER(wintypes.DWORD))
_CloseHandle = _declare(_kernel32, "CloseHandle", wintypes.BOOL, wintypes.HANDLE)
_LocalFree = _declare(_kernel32, "LocalFree", wintypes.HLOCAL, wintypes.HLOCAL)
_CommandLineToArgvW = _declare(_shell32, "CommandLineToArgvW", ctypes.POINTER(wintypes.LPWSTR), wintypes.LPCWSTR,
                               ctypes.POINTER(ctypes.c_int))

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259
ERROR_ACCESS_DENIED = 5


def pid_alive(pid: int) -> bool:
    """True while process ``pid`` runs (``OpenProcess`` + ``GetExitCodeProcess``)."""
    if not 0 < pid <= 0xFFFFFFFF:
        return False
    handle = _OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # Access denied: it is there, it is just not ours to look into (another user, a service).
        return ctypes.get_last_error() == ERROR_ACCESS_DENIED
    try:
        code = wintypes.DWORD()
        if not _GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == STILL_ACTIVE
    finally:
        _CloseHandle(handle)


def read_koe_state(path: str) -> Any:
    """The queue's state file, parsed (OSError / ValueError when it cannot be read now)."""
    with open(path, "rb") as fh:
        data = fh.read(KOE_MAX_BYTES + 1)
    if len(data) > KOE_MAX_BYTES:
        raise ValueError(f"{path} is larger than {KOE_MAX_BYTES} bytes")
    return json.loads(data.decode("utf-8-sig"))


def split_command_line(command: str) -> list[str]:
    """``command`` split into arguments the way Windows does it (``CommandLineToArgvW``)."""
    if not command or not command.strip():
        return []                  # (an empty line would give this program's own path)
    count = ctypes.c_int()
    argv = _CommandLineToArgvW(command, ctypes.byref(count))
    if not argv:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return [argv[i] for i in range(count.value)]
    finally:
        _LocalFree(ctypes.cast(argv, wintypes.HLOCAL))


def koe_state_path(command: str) -> str | None:
    """``<folder of koe.py>\\state\\koe.json`` from the link handler's command line."""
    try:
        args = split_command_line(command)
    except OSError:
        return None
    for arg in args:
        if arg.casefold().endswith("koe.py") and os.path.isabs(arg):
            return os.path.join(os.path.dirname(arg), "state", "koe.json")
    return None


def registry_default(root: int, subkey: str) -> str | None:
    """The (default) value of ``root\\subkey`` (environment variables expanded), or None."""
    try:
        with winreg.OpenKey(root, subkey) as key:
            value, kind = winreg.QueryValueEx(key, "")
    except OSError:
        return None
    if not isinstance(value, str):
        return None
    return winreg.ExpandEnvironmentStrings(value) if kind == winreg.REG_EXPAND_SZ else value


def find_koe_state(query: Callable[[int, str], str | None] = registry_default) -> str | None:
    """Where the queue keeps its state: found through its registered ``resolvekoe:`` handler
    (the user's own registration first, then the merged view)."""
    for root, subkey in HANDLER_KEYS:
        command = query(root, subkey)
        if command and command.strip():
            path = koe_state_path(command)
            if path is not None:
                return path
    return None


# --------------------------------------------------------------------------------------
# Who builds (the queue's holder)
# --------------------------------------------------------------------------------------

def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _pid(value: Any) -> int | None:
    """The holder's process id (an int or a str of digits); None: there is none to check."""
    if isinstance(value, bool) or not value:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _short(value: Any) -> str:
    return " ".join(value.split())[:TEXT_MAX] if isinstance(value, str) else ""


def holder_of(state: Any) -> dict[str, Any] | None:
    """The queue state's ``holder`` (ValueError when the state is not what the queue writes)."""
    if not isinstance(state, dict):
        raise ValueError("the queue's state is not an object")
    holder = state.get("holder")
    return holder if isinstance(holder, dict) else None


def build_of(holder: dict[str, Any] | None, now: float, alive: Callable[[int], bool]) -> dict[str, Any] | None:
    """A build, ``{navn, projekt, opgave, siden}``, when ``holder`` holds Resolve now: it has a
    name, the queue heard from it within the hour and its process (if it gave one) runs."""
    if not isinstance(holder, dict):
        return None
    name = holder.get("navn")
    if not isinstance(name, str) or not name.strip():
        return None
    updated = _number(holder.get("opdateret"))
    if updated is None or now - updated > KOE_STALE_S:
        return None
    pid = _pid(holder.get("pid"))
    if pid is not None and not alive(pid):
        return None
    return {"navn": _short(name), "projekt": _short(holder.get("projekt")),
            "opgave": _short(holder.get("opgave")), "siden": _number(holder.get("siden"))}


class KoeWatch:
    """Which Claude session holds Resolve now, read from the queue's own state file (read-only,
    no mutex: a read that fails while the queue replaces the file keeps the last answer).

    The file is opened only when it has changed (``os.stat`` needs no open handle on Windows 11):
    while any handle on it is open, the queue's own ``os.replace`` of it would fail."""

    def __init__(self, *, path_finder: Callable[[], str | None] = find_koe_state,
                 reader: Callable[[str], Any] = read_koe_state, alive: Callable[[int], bool] = pid_alive,
                 clock: Callable[[], float] = time.time,
                 stat: Callable[[str], os.stat_result] = os.stat) -> None:
        self._path_finder = path_finder
        self._reader = reader
        self._alive = alive
        self._clock = clock
        self._stat = stat
        self._lock = threading.Lock()
        self._path: str | None = None
        self._looked_at: float | None = None
        self._read_at: float | None = None
        self._stamp: tuple[int, int, int] | None = None    # the file that was read last
        self._holder: dict[str, Any] | None = None
        self._answer: dict[str, Any] | None = None

    def current(self) -> dict[str, Any] | None:
        """``{navn, projekt, opgave, siden}`` of the build going on now, or None."""
        now = self._clock()
        with self._lock:
            if self._read_at is not None and 0 <= now - self._read_at < KOE_READ_S:
                return self._copy()
            self._read_at = now
            path = self._find(now)
            if path is None:
                self._holder = None
            else:
                self._read(path)
            build = build_of(self._holder, now, self._alive)      # (also ages a holder kept from before)
            if (build or {}).get("navn") != (self._answer or {}).get("navn"):
                if build is not None:
                    log.info("%s builds in Resolve (%s)", build["navn"], build["projekt"] or "?")
                else:
                    log.info("nobody builds in Resolve")
            self._answer = build
            return self._copy()

    def _read(self, path: str) -> None:
        try:
            st = self._stat(path)
            stamp = (st.st_ino, st.st_mtime_ns, st.st_size)
            if stamp == self._stamp:
                return                                   # the same file as last time
            self._holder = holder_of(self._reader(path))
            self._stamp = stamp
        except FileNotFoundError:
            # No queue state (yet), or the queue moved: look its handler up again later.
            self._holder = self._path = self._stamp = None
        except (OSError, ValueError) as exc:
            log.debug("could not read the queue's state now: %s", exc)

    def _copy(self) -> dict[str, Any] | None:
        return dict(self._answer) if self._answer is not None else None

    def _find(self, now: float) -> str | None:
        if self._path is None and (self._looked_at is None or not 0 <= now - self._looked_at < KOE_LOOKUP_S):
            self._looked_at = now
            try:
                self._path = self._path_finder()
            except Exception:
                log.exception("looking for the Resolve queue failed")
                self._path = None
            if self._path is not None:
                log.info("the Resolve queue keeps its state in %s", self._path)
        return self._path


# --------------------------------------------------------------------------------------
# The robots' sprite sheet
# --------------------------------------------------------------------------------------

class RobotSheets:
    """Renders and caches the robots' sprite sheet: ``<folder>/robot-<signature>.png``, every
    pose of ROBOT_POSES in a ROBOT_CELL_CSS cell, facing right, drawn by the widget page."""

    def __init__(self, base_url: str, folder: str | None = None, *, web_dir: str | None = None,
                 edge: str | None = None, profile_dir: str | None = None,
                 run: Callable[..., Any] = subprocess.run) -> None:
        self.base_url = base_url.rstrip("/")
        self.folder = folder or os.path.join(config.app_dir(), "pet")
        self.web_dir = web_dir or os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
        self._edge = edge
        # Not Klippe's profile: both sheets may be drawn at the same time.
        self.profile_dir = profile_dir or os.path.join(config.app_dir(), "edge-robots")
        self._run = run

    def url(self) -> str:
        return f"{self.base_url}/widget.html?sprites={','.join(ROBOT_POSES)}&cell={ROBOT_CELL_CSS}"

    def path(self) -> str:
        layout = f"{ROBOT_POSES}|{ROBOT_CELL_CSS}|{petplay.SHEET_SCALE}"
        return os.path.join(self.folder, f"robot-{petplay.sprite_signature(self.web_dir, layout)}.png")

    def expected_size(self) -> tuple[int, int]:
        return (ROBOT_CELL_CSS * len(ROBOT_POSES) * petplay.SHEET_SCALE, ROBOT_CELL_CSS * petplay.SHEET_SCALE)

    def ensure(self) -> str | None:
        """The sheet's path – rendered first if needed; None when it cannot be rendered."""
        path = self.path()
        if petplay.png_size(path) == self.expected_size():
            return path
        os.makedirs(self.folder, exist_ok=True)
        petplay.remove_old(self.folder, "robot-", os.path.basename(path))
        if not petplay.render_sheet(self.url(), path, self.expected_size(), self._render):
            return None
        log.info("rendered the robots' sprites")
        return path

    def _render(self, url: str, out: str) -> bool:
        return petplay.render_page(url, out, (ROBOT_CELL_CSS * len(ROBOT_POSES), ROBOT_CELL_CSS),
                                   edge=self._edge, profile_dir=self.profile_dir, run=self._run,
                                   what="the robots' sprites")


# --------------------------------------------------------------------------------------
# The rules
# --------------------------------------------------------------------------------------

def crew_blocker(*, enabled: bool, widget: bool, look: bool, demo: bool, settled: bool, idle_s: float,
                 locked: bool, fullscreen: bool, game: bool, sprites: bool) -> str | None:
    """Why the robots may not come out now (None: they may). A demo build needs only the
    switches, the widget and its look, the stillness, an unlocked screen and the sprites."""
    if not enabled:
        return "off"
    if not widget:
        return "no-widget"
    if not look:
        return "no-look"
    if not demo and not settled:
        return "settling"
    if idle_s < CREW_IDLE_S:
        return "busy"
    if locked:
        return "locked"
    if not demo and fullscreen:
        return "fullscreen"
    if not demo and game:
        return "game"
    if not sprites:
        return "sprites"
    return None


def _spawn_child(argv: list[str], on_out: Callable[[], None], on_exit: Callable[[str], None],
                 on_event: Callable[[dict[str, Any]], None]) -> Any:
    return petplay._Child(argv, on_out, on_exit, on_event, name="crew")


def demo_call() -> dict[str, Any]:
    """The demo call: it rings like a session that wants Resolve; its button starts the demo."""
    return {"tag": DEMO_TAG, "titel": "🎬 Demo vil bruge Resolve",
            "tekst": "Robotterne vil vise, hvad de kan (ca. 1 min).",
            "knapper": [{"tekst": "Byg nu", "uri": DEMO_URI}], "session": DEMO_NAME,
            "udloeber": 600, "lyd": True, "visning": "kort", "prioritet": "normal"}


# --------------------------------------------------------------------------------------
# The crew
# --------------------------------------------------------------------------------------

class Crew:
    """Follows the builds and lets the robots out when the rules allow it."""

    def __init__(self, cfg: Config, bus: Any, *, widget: Any, watch: Any,
                 look: Callable[[], dict[str, Any] | None],
                 wardrobe: Callable[[], dict[str, str]] | None = None,
                 petplay_busy: Callable[[], bool] | None = None, messages: Any = None,
                 base_url: str = "", probe: Any = None, sprites: Any = None,
                 spawn: Callable[..., Any] | None = None, clock: Callable[[], float] = time.monotonic,
                 rng: random.Random | None = None, log_file: str | None = None,
                 wall: Callable[[], float] = time.time) -> None:
        self.cfg = cfg
        self.bus = bus
        self._widget = widget
        self._watch = watch
        self._look = look
        self._wardrobe = wardrobe or (lambda: {})
        self._petplay_busy = petplay_busy or (lambda: False)
        self._messages = messages
        self._probe = probe or petplay.DesktopProbe()
        self._sprites = sprites or RobotSheets(base_url)
        self._spawn = spawn or _spawn_child
        self._clock = clock
        self._wall = wall
        self._rng = rng or random.Random()
        self._log_file = log_file
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._real: dict[str, Any] | None = None       # the queue's build (KoeWatch)
        self._demo: dict[str, Any] | None = None       # {key, since, until}
        self._build: dict[str, Any] | None = None      # the build shown now (+ key, seen)
        self._child: Any = None
        self._out = False
        self._side = "venstre"                      # the box side the robots went out through
        self._aiming = False
        self._published: dict[str, Any] | None = self._payload(None)
        self._tried: tuple[int, Any] | None = None     # (input tick, build) of the last outing
        self._sheet_path: str | None = None
        self._rendering = False
        self._render_failed_at: float | None = None
        self._render_thread: threading.Thread | None = None

    # -- lifecycle -------------------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self.cfg.on_change(lambda _snapshot: self._wake.set())
        self._thread = threading.Thread(target=self._run, name="crew", daemon=True)
        self._thread.start()

    def close(self) -> None:
        """App exit: the robots stop at once (the helper is told to quit; it ends on stdin EOF
        by itself too). Never waits for a sprite sheet being drawn."""
        self._stop.set()
        self._wake.set()
        with self._lock:
            child = self._child
        if child is not None:
            child.stop(CLOSE_WAIT_S)
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(0.2)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.step()
            except Exception:
                log.exception("crew step failed")
            self._wake.wait(POLL_S)
            self._wake.clear()

    # -- from the page, the messages and PetPlay -------------------------------------------
    def state(self) -> dict[str, Any]:
        """``{aktiv, navn, projekt, opgave, siden, demo, ude, faerdig, varighed_s}`` now."""
        with self._lock:
            return self._payload(self._build)

    def active(self) -> bool:
        """A build (or the demo) is on – or its robots are still on their way home: Klippe
        directs them and does not play meanwhile."""
        return self._build is not None or self._child is not None

    def demo(self, call: bool) -> dict[str, Any]:
        """"🤖 Vis robotterne" (a demo build now) or "📞 Prøv telefonen" (a demo call whose
        "Byg nu" starts the demo build)."""
        if not self.cfg.get("widget_enabled", False):
            raise ValueError(MESSAGES["off"])
        if not call:
            return self._start_demo()
        with self._lock:
            if self._real is not None:
                raise ValueError(MESSAGES["building"])
        if self._messages is None:
            raise ValueError(MESSAGES["no-board"])
        self._messages.remove(DEMO_TAG)              # a new demo call rings anew, even an unanswered one
        self._messages.post(demo_call(), internal=True)
        log.info("the demo call rings")
        return self.state()

    def handle_uri(self, uri: str) -> None:
        """A ``projektsog:`` button of a message Projektsøg posted itself was clicked."""
        if not isinstance(uri, str) or uri.strip().casefold() != DEMO_URI:
            raise ValueError(MESSAGES["uri"])
        self._start_demo()

    def _start_demo(self) -> dict[str, Any]:
        now = self._clock()
        with self._lock:
            if self._real is not None:
                raise ValueError(MESSAGES["building"])
            if self._demo is not None and now < self._demo["until"]:
                self._demo["until"] = now + DEMO_S         # once more: it simply lasts longer
            else:
                self._demo = {"key": ("demo", now), "since": self._wall(), "until": now + DEMO_S}
            ended = self._track(now)
            child = self._child
        if ended is not None and child is not None:
            child.send("done")
        log.info("the robots' demo build")
        self._wake.set()
        return self.state()

    # -- one step --------------------------------------------------------------------------
    def step(self) -> None:
        now = self._clock()
        real = self._read_watch()
        with self._lock:
            self._real = real
            ended = self._track(now)
            child, build = self._child, self._build
        if child is not None:
            if ended is not None:
                child.send("done")                  # the finale: a shine, a cheer, home
            if not self._may_stay_out():
                log.info("the robots were switched off while they were out")
                child.stop(STOP_WAIT_S)
            return
        if build is not None and not self._stop.is_set():
            self._maybe_go_out(build, now)

    def _read_watch(self) -> dict[str, Any] | None:
        if self._watch is None:
            return None
        try:
            return self._watch.current()
        except Exception:
            log.exception("reading the Resolve queue failed")
            return self._real

    def _may_stay_out(self) -> bool:
        return (not self._stop.is_set() and bool(self.cfg.get("widget_enabled", False))
                and bool(self.cfg.get("widget_crew", True)))

    # -- the build shown now (lock held) ---------------------------------------------------
    def _current(self, now: float) -> dict[str, Any] | None:
        real = self._real
        if real is not None:
            self._demo = None                       # a real session ends the demo
            return {"key": ("koe", real["navn"], real.get("siden")), "navn": real["navn"],
                    "projekt": real.get("projekt") or "", "opgave": real.get("opgave") or "",
                    "siden": real.get("siden"), "demo": False}
        demo = self._demo
        if demo is not None and now < demo["until"]:
            return {"key": demo["key"], "navn": DEMO_NAME, "projekt": DEMO_TEXT, "opgave": "",
                    "siden": demo["since"], "demo": True}
        self._demo = None
        return None

    def _track(self, now: float) -> dict[str, Any] | None:
        """Follow the build; publishes ``bygger`` on every change. Returns a build that has
        just ended."""
        current = self._current(now)
        old = self._build
        ended = None
        if old is not None and (current is None or current["key"] != old["key"]):
            ended, self._build = old, None
            self._end(old, now)
        if current is not None:
            current["seen"] = old["seen"] if old is not None and ended is None else now
            if old is None or ended is not None:
                log.info("the robots build: %s (%s)", current["navn"], current["projekt"] or "?")
            self._build = current
        self._publish_state()
        return ended

    def _end(self, build: dict[str, Any], now: float) -> None:
        since = _number(build.get("siden"))         # the queue's own start (also from before a restart)
        seconds = self._wall() - since if since is not None else now - build["seen"]
        duration = int(round(max(0.0, seconds)))
        self.bus.publish("bygger", self._payload(build, faerdig=True, duration=duration))
        self._published = self._payload(None)
        log.info("%s is done building (%d s)", build["navn"], duration)

    def _payload(self, build: dict[str, Any] | None, *, faerdig: bool = False,
                 duration: int | None = None) -> dict[str, Any]:
        return {"aktiv": build is not None and not faerdig,
                "navn": build["navn"] if build else None,
                "projekt": build["projekt"] if build else None,
                "opgave": build["opgave"] if build else None,
                "siden": build["siden"] if build else None,
                "demo": bool(build and build["demo"]),
                "ude": self._out, "retning": self._side if self._out else None,
                "faerdig": faerdig, "varighed_s": duration}

    def _publish_state(self) -> None:
        payload = self._payload(self._build)
        if payload != self._published:
            self._published = payload
            self.bus.publish("bygger", dict(payload))

    # -- out on the screen -----------------------------------------------------------------
    def _maybe_go_out(self, build: dict[str, Any], now: float) -> None:
        hwnd = getattr(self._widget, "hwnd", None)
        enabled = bool(self.cfg.get("widget_enabled", False)) and bool(self.cfg.get("widget_crew", True))
        shown = enabled and bool(hwnd) and bool(self._probe.window_shown(hwnd))
        look = self._get_look() if shown else None
        demo = bool(build["demo"])
        settled = demo or now - build["seen"] >= BUILD_SETTLE_S
        sheet = self._sheet(now) if look is not None and settled else None   # (drawn meanwhile)
        tick = self._probe.input_tick()            # before the idle time: a touch in between counts
        idle = self._probe.idle_s()
        still = idle >= CREW_IDLE_S
        locked = still and bool(self._probe.locked())
        strict = still and not locked and not demo and sheet is not None
        reason = crew_blocker(
            enabled=enabled, widget=shown, look=look is not None, demo=demo, settled=settled, idle_s=idle,
            locked=locked, fullscreen=strict and bool(self._probe.fullscreen(hwnd)),
            game=strict and self._game_on(), sprites=sheet is not None)
        if reason is not None:
            return
        with self._lock:
            if self._tried == (tick, build["key"]):
                return                              # one outing per pause (they were touched, or failed)
        assert look is not None and sheet is not None
        self._launch(build, look, int(hwnd), tick, sheet)

    def _get_look(self) -> dict[str, Any] | None:
        try:
            look = self._look()
        except Exception:
            log.debug("could not ask how Klippe looks", exc_info=True)
            return None
        if not look or look.get("stage") not in ROBOTS or not look.get("pet") or not look.get("view"):
            return None
        return look

    def _game_on(self) -> bool:
        try:
            return bool(self._petplay_busy())
        except Exception:
            return False

    def _sheet(self, now: float) -> str | None:
        """The robots' sprite sheet once it is drawn; starts drawing it (on its own thread, so
        neither the steps nor ``close()`` wait for Edge) when needed."""
        with self._lock:
            path = self._sheet_path
            if path is not None:
                if os.path.isfile(path):
                    return path
                self._sheet_path = None
            if self._rendering or self._stop.is_set():
                return None
            if self._render_failed_at is not None and now - self._render_failed_at < RENDER_RETRY_S:
                return None
            self._rendering = True
            self._render_thread = threading.Thread(target=self._render, name="crew-sprites", daemon=True)
            self._render_thread.start()
        return None

    def _render(self) -> None:
        try:
            path = self._sprites.ensure()
        except Exception:
            log.exception("drawing the robots failed")
            path = None
        with self._lock:
            self._rendering = False
            self._sheet_path = path
            self._render_failed_at = None if path is not None else self._clock()
        if path is None:
            log.warning("the robots could not be drawn – they stay in the box")
        self._wake.set()

    def _launch(self, build: dict[str, Any], look: dict[str, Any], hwnd: int, tick: int, sheet: str) -> None:
        stage = look["stage"]
        try:
            wearing = dict(self._wardrobe())
        except Exception:
            wearing = {}
        awp = wearing.get("haand") == "awp" and stage != "egg"
        argv = petplay.child_argv(CHILD_MODULE) + [
            "--sprites", sheet, "--poses", ",".join(ROBOT_POSES), "--cell", str(ROBOT_CELL_CSS),
            "--sheet-scale", str(petplay.SHEET_SCALE), "--widget", str(hwnd),
            "--pet", ",".join(str(v) for v in look["pet"]), "--view", ",".join(str(v) for v in look["view"]),
            "--stage", stage, "--robots", str(ROBOTS[stage]), "--awp", "1" if awp else "0",
            "--seed", str(self._rng.randrange(1 << 30)), "--input-tick", str(tick),
            "--log-file", self._log_file or os.path.join(config.log_dir(), "crew.log")]
        with self._lock:
            if self._stop.is_set():
                return
            self._tried = (tick, build["key"])
            try:
                self._child = self._spawn(argv, self._on_out, self._on_exit, self._on_event)
            except OSError as exc:
                log.error("could not let the robots out: %s", exc)
                return
            self._out = self._aiming = False
        log.info("the robots come out (%s, %d robots%s)", stage, ROBOTS[stage], ", AWP" if awp else "")

    # -- from the helper (its reader thread) -----------------------------------------------
    def _on_out(self) -> None:
        with self._lock:
            self._out = True
            self._publish_state()

    def _on_event(self, message: dict[str, Any]) -> None:
        kind = message.get("event")
        with self._lock:
            if kind == "side":                      # before "out": the hatch's side in the widget
                self._side = "hoejre" if message.get("side") == "right" else "venstre"
                return
            if kind == "aim":
                self._aiming = True
                data = {"haendelse": "sigter", "ram": False,
                        "retning": "hoejre" if message.get("side") == "right" else "venstre"}
            elif kind == "shot":
                data = {"haendelse": "skud", "ram": message.get("hit") is True}
            elif kind == "aim-end":
                self._aiming = False
                data = {"haendelse": "sigter-slut", "ram": False}
            else:
                return                              # unknown events are ignored
            self.bus.publish("robot", data)

    def _on_exit(self, reason: str) -> None:
        with self._lock:
            if self._aiming:                        # Klippe must not stay in its aiming pose
                self.bus.publish("robot", {"haendelse": "sigter-slut", "ram": False})
            self._child = None
            self._out = self._aiming = False
            self._publish_state()
        log.info("the robots are home (%s)", reason)
        self._wake.set()
