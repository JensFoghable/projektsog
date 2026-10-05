"""Application wiring and lifecycle (SPEC §13).

* ``harden_process()``: makes the process safe under pythonw.exe (stdio, error mode, crash
  log, exception hooks, logging) before anything else runs;
* single instance (named mutex) with hand-off to the running instance;
* ``Controller``: the one place that shows/hides the window and opens paths for the UI;
* ``App``: startup order, config listener, notification forwarding, bounded exit sequence;
* ``main()`` for ``python -m projektsog`` and ``Projektsøg.pyw``.

This is the only module that imports other agents' modules at module level.
"""

from __future__ import annotations

import argparse
import ctypes
import faulthandler
import http.client
import json
import logging
import logging.handlers
import ntpath
import os
import queue
import stat
import sys
import threading
import time
import urllib.error
import urllib.request
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any, Callable

from . import APP_NAME, __version__, config, hotkey, winfs, winui
from .config import Config
from .events import EventBus
from .hotkey import HotkeyManager
from .indexer import Indexer
from .resolve_bridge import ResolveBridge
from .server import HOST, Server
from .importer import Importer
from .achievements import PetProgress
from .messages import MessageBoard
from .petplay import PetPlay, window_shown
from .widget import PetWindow
from .timetrack import TimeTracker
from .tray import TrayIcon
from .window import AppWindow

log = logging.getLogger(__name__)

AUMID = "Projektsog.App"
PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(PACKAGE_DIR)
LAUNCHER = os.path.join(REPO_DIR, "Projektsøg.pyw")
ICON_PATH = os.path.join(PACKAGE_DIR, "assets", "icon.ico")

OPEN_ACTIONS = ("folder", "reveal", "file")
STAT_TIMEOUT_S = 3.0
EXIT_DEADLINE_S = 5.0
HOTKEY_STATUS_POLL_S = 2.0
HOTKEY_RECHECK_S = 10.0     # a hotkey not active at startup may still come up (SPEC §15.11)
HANDOFF_TIMEOUT_S = 20.0
# Keys typed after the hotkey stay captured while Edge is cold-started (§15.10): one extension
# reaches CAPTURE_EXTEND_S ahead (the hook child's cap per request) and is re-sent every
# CAPTURE_HEARTBEAT_S while show() waits (≤ 10 s), so a hung main process still loses the
# capture within seconds; the child also ends every capture 12 s after the fire (R2-WIN-1).
CAPTURE_EXTEND_S = 5.0
CAPTURE_HEARTBEAT_S = 2.0
CAPTURE_HEARTBEAT_MAX_S = 12.0
REPLAY_DELAY_S = 0.1        # let the shown page focus its search field before the replay
CRASH_LOG_MAX_BYTES = 1024 * 1024

MSG_NOT_RESPONDING = "Placeringen svarer ikke"
MSG_MISSING = "Findes ikke længere – indekset opdateres"
MSG_FOLDER_GONE = "Mappen findes ikke længere"      # offline, but its disk/computer is there
_OPEN_FAILED = {
    "folder": "Mappen kunne ikke åbnes",
    "reveal": "Placeringen kunne ikke vises i Stifinder",
    "file": "Filen kunne ikke åbnes – tryk Enter for at vise den i mappen",
}
# Share of the exit deadline each step may use, in the order of SPEC §13.
_EXIT_SHARES = {"hotkey": 0.10, "tray": 0.15, "import": 0.05, "time": 0.05, "resolve": 0.10, "indexer": 0.35,
                "server": 0.10, "window": 0.10}
# Config keys that concern the global hotkey -> HotkeyManager keyword.
_HOTKEY_OPTIONS = {
    "hotkey": "spec",
    "hotkey_passthrough_apps": "passthrough_apps",
    "hotkey_typing_guard_ms": "typing_guard_ms",
    "hotkey_double_tap_ms": "double_tap_ms",
}

ERROR_FILE_NOT_FOUND = 2
ERROR_PATH_NOT_FOUND = 3
ERROR_ACCESS_DENIED = 5
ERROR_ALREADY_EXISTS = 183
ASFW_ANY = 0xFFFFFFFF
SEM_FAILCRITICALERRORS = 0x0001
SEM_NOOPENFILEERRORBOX = 0x8000

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
_kernel32.CreateMutexW.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.GetErrorMode.argtypes = ()
_kernel32.GetErrorMode.restype = wintypes.UINT
_kernel32.SetErrorMode.argtypes = (wintypes.UINT,)
_kernel32.SetErrorMode.restype = wintypes.UINT
_user32.AllowSetForegroundWindow.argtypes = (wintypes.DWORD,)
_user32.AllowSetForegroundWindow.restype = wintypes.BOOL

_STOP = object()    # ends the notification forwarder


# ============================================================================================
# Autostart command
# ============================================================================================

def pythonw_path() -> str:
    """pythonw.exe next to the running interpreter (fallback: the interpreter itself)."""
    candidate = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return candidate if os.path.isfile(candidate) else sys.executable


def build_run_command(pythonw: str, launcher: str) -> str:
    return f'"{pythonw}" "{launcher}" --background'


RUN_COMMAND = build_run_command(pythonw_path(), LAUNCHER)


# ============================================================================================
# Single instance
# ============================================================================================

def mutex_name() -> str:
    user = os.environ.get("USERNAME") or "user"
    return "Local\\Projektsog-" + user.replace("\\", "_")


class SingleInstance:
    """Named mutex: its existence means another instance of this user runs."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._handle: int | None = None

    def acquire(self) -> bool:
        """True when this process is the only instance (it then holds the mutex)."""
        handle = _kernel32.CreateMutexW(None, False, self.name)
        error = ctypes.get_last_error()
        if not handle:
            if error == ERROR_ACCESS_DENIED:     # exists, created in another security context
                return False
            log.warning("CreateMutexW(%s) failed: %s – running without the single-instance "
                        "guard", self.name, ctypes.WinError(error))
            return True
        if error == ERROR_ALREADY_EXISTS:
            _kernel32.CloseHandle(handle)
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle:
            _kernel32.CloseHandle(handle)


def allow_foreground_any() -> bool:
    """Let the running instance take the foreground (we are the process the user started)."""
    return bool(_user32.AllowSetForegroundWindow(ASFW_ANY))


def write_instance_file(path: str, pid: int, port: int) -> None:
    tmp = f"{path}.{pid}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"pid": pid, "port": port}, fh)
    _retry_sharing_violation(lambda: os.replace(tmp, path))


def read_instance_file(path: str) -> dict[str, int] | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    pid, port = data.get("pid"), data.get("port")
    if type(pid) is not int or type(port) is not int or not 0 < port < 65536:
        return None
    return {"pid": pid, "port": port}


def remove_instance_file(path: str, pid: int) -> None:
    """Delete instance.json if it still describes process ``pid``."""
    info = read_instance_file(path)
    if info is not None and info["pid"] != pid:
        return
    try:
        _retry_sharing_violation(lambda: os.remove(path))
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("Could not delete %s: %s", path, exc)


def _retry_sharing_violation(action: Callable[[], None], attempts: int = 5) -> None:
    # A reader (e.g. the Resolve script) may hold the file open for a moment.
    for attempt in range(attempts):
        try:
            action()
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.05)


_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def post_json(port: int, path: str, body: dict[str, Any], timeout: float) -> Any:
    """POST to the local API of the instance on ``port`` (never through a proxy)."""
    request = urllib.request.Request(
        f"http://{HOST}:{port}{path}", data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "X-Projektsog": "1"})
    with _NO_PROXY.open(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8") or "null")


def _handoff_retryable(exc: BaseException) -> bool:
    """An instance that is starting or exiting: nobody listening (yet / any more), a dropped
    connection, no answer in time, or HTTP 503 ("Projektsøg lukker ned")."""
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code == 503
    if isinstance(exc, urllib.error.URLError):
        exc = exc.reason
    return isinstance(exc, (ConnectionError, TimeoutError))


def handoff_to_running_instance(instance_file: str, *, background: bool, rescan: bool = False,
                                timeout: float = HANDOFF_TIMEOUT_S,
                                allow_foreground: Callable[[], bool] = allow_foreground_any,
                                reacquire: Callable[[], bool] | None = None) -> int | None:
    """Second instance (SPEC §13 step 2): ask the running one to show its window, then exit.

    Returns the exit code – or None when the running instance exited meanwhile and
    ``reacquire()`` (``SingleInstance.acquire``) got the mutex: this process must then run the
    app itself, so a launch made while the other instance is exiting is not lost.
    Retries while the running instance is starting or exiting (see ``_handoff_retryable``;
    an exiting instance deletes instance.json first). ``rescan`` is forwarded as
    POST /api/scan {"full": true}; ``--background`` starts (autostart, install.ps1) have
    nothing else to hand over.
    """
    if not background:
        allow_foreground()
    pending: list[tuple[str, dict[str, Any]]] = []
    if rescan:
        pending.append(("/api/scan", {"full": True}))
    if not background:
        pending.append(("/api/window/show", {}))
    deadline = time.monotonic() + timeout
    reason = "instance.json not found"
    while True:
        info = read_instance_file(instance_file)
        if info is None:
            reason = "instance.json not found"
        elif not pending:
            log.info("Already running (pid %d) – nothing to do for a --background start",
                     info["pid"])
            return 0
        else:
            try:
                while pending:
                    path, body = pending[0]
                    post_json(info["port"], path, body,
                              timeout=max(1.0, deadline - time.monotonic()))
                    pending.pop(0)
                log.info("Handed over to the running instance (pid %d)", info["pid"])
                return 0
            except urllib.error.HTTPError as exc:
                exc.close()     # it holds the reply; unclosed it is a ResourceWarning
                if not _handoff_retryable(exc):
                    log.warning("Hand-off refused by port %d: HTTP %d", info["port"], exc.code)
                    return 1
                reason = f"port {info['port']} is shutting down (HTTP {exc.code})"
            except (OSError, ValueError, http.client.HTTPException) as exc:
                if not _handoff_retryable(exc):
                    log.warning("Hand-off to port %d failed: %r", info["port"], exc)
                    return 1
                reason = f"port {info['port']}: {exc!r}"
        if reacquire is not None and reacquire():
            log.info("The running instance has exited (%s) – starting here instead", reason)
            return None
        if time.monotonic() >= deadline:
            if background and not pending:
                log.info("Another instance holds the mutex – nothing to do for a --background "
                         "start (%s)", reason)
                return 0
            log.warning("Could not reach the running instance: %s", reason)
            return 1
        time.sleep(0.25)


# ============================================================================================
# Controller
# ============================================================================================

def offline_hint(source: dict[str, Any]) -> str:
    """Danish hint for an offline location (``source`` is a SourceRef, SPEC §7.1). When its
    disk is mounted or its computer answers (``volume_present``, SPEC §15.12), the folder
    itself is gone – moved, renamed or deleted – and asking for the disk would mislead."""
    if source.get("volume_present"):
        return MSG_FOLDER_GONE
    if source.get("kind") == "local":
        disk = source.get("disk_name") or source.get("volume_label") or source.get("name") or "?"
        return f"Tilslut disken ‘{disk}’"
    return f"Computeren {source.get('host') or '?'} svarer ikke – er den tændt?"


def is_missing_error(exc: OSError) -> bool:
    """True when the path itself is gone. Python also maps unreachable hosts/shares
    (ERROR_BAD_NETPATH, ERROR_BAD_NET_NAME) and missing drives to FileNotFoundError."""
    if not isinstance(exc, FileNotFoundError):
        return False
    winerror = getattr(exc, "winerror", None)
    return winerror is None or winerror in (ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND)


def _is_absolute(path: str) -> bool:
    drive, rest = ntpath.splitdrive(path)
    return bool(drive) and (drive.startswith(("\\\\", "//")) or rest.startswith(("\\", "/")))


def _extended_path(path: str) -> str:
    """\\\\?\\ form for paths beyond MAX_PATH, which os.stat() could not reach otherwise."""
    if len(path) < 248 or path.startswith("\\\\?\\"):
        return path
    path = ntpath.normpath(path)
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path


def _probe_path(path: str) -> str:
    """Runs on a call_with_timeout thread: 'dir' | 'file' | 'missing' | 'error'."""
    try:
        st = os.stat(_extended_path(path))
    except OSError as exc:
        # "Missing" only when the drive/share itself answers; otherwise it is unreachable.
        anchor = ntpath.splitdrive(path)[0]
        if is_missing_error(exc) and anchor and os.path.isdir(anchor + "\\"):
            return "missing"
        log.debug("stat(%s) failed: %s", path, exc)
        return "error"
    return "dir" if stat.S_ISDIR(st.st_mode) else "file"


def _stat_key(path: str, location: dict[str, Any] | None) -> str:
    """call_with_timeout key: one in-flight stat per source (per drive/share for unknown
    paths) so a dead share never piles up threads. The 'open:' prefix keeps opens apart from
    the Indexer's probes of the same root, which may legitimately run for seconds."""
    source = (location or {}).get("source")
    if isinstance(source, dict) and source.get("id") is not None:
        return f"open:source:{source['id']}"
    return "open:" + (ntpath.splitdrive(path)[0] or path).casefold()


class Controller:
    """What the UI (HTTP), the tray, the hotkey and a second instance ask the app to do."""

    def __init__(self, cfg: Config, bus: EventBus, indexer: Indexer, bridge: ResolveBridge, *,
                 window: AppWindow | None = None,
                 hotkeys: HotkeyManager | None = None,
                 request_exit: Callable[[], None] | None = None,
                 call_with_timeout: Callable[[str, Callable[[], Any], float],
                                             tuple[str, Any]] | None = None,
                 open_folder: Callable[..., bool] | None = None,
                 reveal: Callable[..., bool] | None = None,
                 open_file: Callable[[str], bool] | None = None,
                 get_run_at_login: Callable[[], bool] | None = None,
                 set_run_at_login: Callable[[bool, str], None] | None = None,
                 format_hotkey: Callable[[str], str] | None = None,
                 run_command: str = RUN_COMMAND) -> None:
        self.cfg = cfg
        self.bus = bus
        self.indexer = indexer
        self.bridge = bridge
        self.window = window        # set by App once the server port is known
        self.hotkeys = hotkeys      # replaced by App when the hotkey is (re)configured
        self._request_exit = request_exit
        self._call_with_timeout = call_with_timeout or winfs.call_with_timeout
        self._open_folder = open_folder or winui.open_folder
        self._reveal = reveal or winui.reveal
        self._open_file = open_file or winui.open_file
        self._get_run_at_login = get_run_at_login or winui.get_run_at_login
        self._set_run_at_login = set_run_at_login or winui.set_run_at_login
        self._format_hotkey = format_hotkey or hotkey.format_hotkey
        self._run_command = run_command
        # Set once the app is shutting down: the window is not shown again, and the server
        # answers /api/window/show with 503 so a new launch waits and takes over (APP-2).
        self.exiting = threading.Event()
        # When the last hotkey-triggered show that succeeded had finished (monotonic); a fire
        # the HotkeyManager queued before then came in while that show was still running.
        self._hotkey_shown_at: float | None = None

    # -- window ---------------------------------------------------------------------------
    def show_window(self, from_app: str | None = None, reason: str = "api", *,
                    panel: str | None = None) -> bool:
        """Show + focus the window; ``panel="settings"`` asks the UI to open its settings."""
        if self.exiting.is_set():
            self._end_capture(False)
            return False
        window = self.window
        capturing = reason == "hotkey" and self.hotkeys is not None
        shown = False
        stop_heartbeat: Callable[[], None] | None = None
        try:
            if window is not None:
                if capturing and self._needs_launch(window):
                    # Edge has to start first (seconds): keep what the user types meanwhile.
                    stop_heartbeat = self._keep_capture_alive()
                shown = bool(window.show())
                if shown and capturing:
                    time.sleep(REPLAY_DELAY_S)  # the page focuses its search field on show
        finally:
            if stop_heartbeat is not None:
                stop_heartbeat()
            self._end_capture(shown)    # replay (or drop) keys typed after the hotkey
        for on_shown in (self.indexer.on_window_shown, self.bridge.on_window_shown):
            try:
                on_shown()
            except Exception:
                log.exception("on_window_shown failed")
        focus: dict[str, Any] = {"from_app": from_app, "reason": reason}
        if panel:
            focus["panel"] = panel
        self.bus.publish("focus", focus)
        return shown

    def hide_window(self, restore_previous: bool = False) -> None:
        window = self.window
        if window is not None:
            window.hide(restore_previous=restore_previous)

    def on_hotkey(self, info: dict[str, Any]) -> None:
        """HotkeyManager callback: toggle – hide when we are in front, show otherwise."""
        try:
            window = self.window
            if self._queued_during_show(info):
                # The hotkey went off again while its own show was still running (typed during
                # a cold start, or an impatient second press): keep the window that just came
                # up instead of hiding it at once (R2-APP-1). Keys captured since then belong
                # in its search field.
                log.info("Hotkey pressed while the window was being shown – ignored")
                self._end_capture(bool(window is not None and window.is_foreground()))
            elif window is not None and window.is_visible() and window.is_foreground():
                window.hide(restore_previous=True)
                self._end_capture(False)
            elif self.show_window(info.get("from_app"), "hotkey"):
                self._hotkey_shown_at = time.monotonic()
        except Exception:
            log.exception("Hotkey action failed")

    def _queued_during_show(self, info: dict[str, Any]) -> bool:
        fired_at, shown_at = info.get("fired_at"), self._hotkey_shown_at
        return (isinstance(fired_at, (int, float)) and shown_at is not None
                and fired_at < shown_at)

    def _end_capture(self, ok: bool) -> None:
        manager = self.hotkeys
        if manager is None:
            return
        try:
            manager.end_capture(ok)
        except Exception:
            log.exception("end_capture failed")

    def _extend_capture(self, seconds: float) -> None:
        manager = self.hotkeys
        if manager is None:
            return
        try:
            manager.extend_capture(seconds)
        except Exception:
            log.exception("extend_capture failed")

    def _keep_capture_alive(self) -> Callable[[], None]:
        """Extend the hotkey's key capture now and then every CAPTURE_HEARTBEAT_S until the
        returned function is called – a cold show() may take up to 10 s, one extension reaches
        only CAPTURE_EXTEND_S ahead (R2-WIN-1). Once that function has returned, no further
        extension is sent, so ``end_capture`` is the last word."""
        stop = threading.Event()
        lock = threading.Lock()
        deadline = time.monotonic() + CAPTURE_HEARTBEAT_MAX_S
        self._extend_capture(CAPTURE_EXTEND_S)

        def beat() -> None:
            while not stop.wait(CAPTURE_HEARTBEAT_S) and time.monotonic() < deadline:
                with lock:
                    if stop.is_set():
                        return
                    self._extend_capture(CAPTURE_EXTEND_S)

        threading.Thread(target=beat, name="capture-heartbeat", daemon=True).start()

        def cancel() -> None:
            with lock:
                stop.set()
        return cancel

    @staticmethod
    def _needs_launch(window: AppWindow) -> bool:
        try:
            return bool(window.needs_launch())
        except Exception:
            log.exception("needs_launch failed")
            return False

    # -- opening things -------------------------------------------------------------------
    def open_path(self, path: str, action: str) -> dict[str, Any]:
        """Open ``path`` in Explorer (SPEC §11). User-level failures -> {"ok": False, ...}."""
        if action not in OPEN_ACTIONS:
            raise ValueError(f"Ukendt handling: {action}")
        if not isinstance(path, str) or not _is_absolute(path):
            raise ValueError("Ugyldig sti")
        location = self.indexer.locate(path)
        if location and not location.get("online"):
            return {"ok": False, "error": offline_hint(location.get("source") or {})}
        status, kind = self._call_with_timeout(_stat_key(path, location),
                                               lambda: _probe_path(path), STAT_TIMEOUT_S)
        if status != "ok" or kind == "error":
            return {"ok": False, "error": MSG_NOT_RESPONDING}
        if kind == "missing":
            self.indexer.path_missing(path)
            return {"ok": False, "error": MSG_MISSING}
        # Never hand a file to "open folder" (the shell would run it) and vice versa.
        if action == "folder" and kind == "file":
            action = "reveal"
        elif action == "file" and kind == "dir":
            action = "folder"
        if action == "folder":
            opened = self._open_folder(path, activate=True)
        elif action == "reveal":
            opened = self._reveal(path, activate=True)
        else:
            opened = self._open_file(path)
        if not opened:
            return {"ok": False, "error": _OPEN_FAILED[action]}
        if self.cfg["hide_after_open"]:
            self.hide_window()
        return {"ok": True, "path": path}

    # -- hotkey / autostart / exit --------------------------------------------------------
    def hotkey_label(self, spec: str | None = None) -> str:
        spec = self.cfg["hotkey"] if spec is None else spec
        try:
            return self._format_hotkey(spec)
        except ValueError:
            return spec

    def hotkey_status(self) -> dict[str, Any]:
        spec = self.cfg["hotkey"]
        manager = self.hotkeys
        return {
            "spec": spec,
            "label": self.hotkey_label(spec),
            "enabled": bool(self.cfg["hotkey_enabled"]),
            "active": bool(manager.active) if manager is not None else False,
            "mode": manager.mode if manager is not None else None,
        }

    def get_run_at_login(self) -> bool:
        try:
            return bool(self._get_run_at_login())
        except OSError as exc:
            log.warning("Could not read the Run key: %s", exc)
            return False

    def set_run_at_login(self, enabled: bool) -> None:
        if not isinstance(enabled, bool):
            raise ValueError("run_at_login skal være sand/falsk")
        try:
            self._set_run_at_login(enabled, self._run_command)
        except OSError as exc:
            log.warning("Could not change the Run key: %s", exc)
            raise ValueError("‘Start med Windows’ kunne ikke ændres") from exc
        settings = self.cfg.snapshot()
        settings["run_at_login"] = self.get_run_at_login()
        self.bus.publish("settings", settings)

    def request_exit(self) -> None:
        if self._request_exit is None:
            raise ValueError("Projektsøg kan ikke afsluttes herfra")
        self._request_exit()


# ============================================================================================
# App
# ============================================================================================

@dataclass(frozen=True)
class Components:
    """Factories App.start() uses for its collaborators (tests substitute fakes)."""
    config: Callable[[], Config] = Config
    indexer: Callable[[Config, EventBus], Indexer] = Indexer
    bridge: Callable[[Config, EventBus, Indexer], ResolveBridge] = ResolveBridge
    tracker: Callable[..., TimeTracker] = TimeTracker
    importer: Callable[..., Importer] = Importer
    controller: Callable[..., Controller] = Controller
    server: Callable[..., Server] = Server
    window: Callable[[str, str], AppWindow] = AppWindow
    widget: Callable[..., PetWindow] = PetWindow
    petplay: Callable[..., PetPlay] = PetPlay
    messages: Callable[..., MessageBoard] = MessageBoard
    progress: Callable[..., PetProgress] = PetProgress
    tray: Callable[..., TrayIcon] = TrayIcon
    hotkeys: Callable[..., HotkeyManager] = HotkeyManager


def _hotkey_settings(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {key: snapshot.get(key, config.DEFAULTS[key])
            for key in (*_HOTKEY_OPTIONS, "hotkey_enabled")}


class _ActionRunner:
    """One daemon thread running submitted calls in order – keeps slow work (showing the
    window, registry, config writes) off the tray thread, whose callbacks must be instant."""

    def __init__(self) -> None:
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._thread = threading.Thread(target=self._run, name="app-actions", daemon=True)
        self._thread.start()

    def submit(self, fn: Callable[..., object], *args: Any, **kwargs: Any) -> None:
        self._queue.put((fn, args, kwargs))

    def stop(self) -> None:
        self._queue.put(None)

    def _run(self) -> None:
        while (item := self._queue.get()) is not None:
            fn, args, kwargs = item
            try:
                fn(*args, **kwargs)
            except Exception:
                log.exception("Background action %s failed", getattr(fn, "__qualname__", fn))


def _run_bounded(name: str, action: Callable[[], object], timeout: float) -> bool:
    """Run ``action`` on a daemon thread and wait at most ``timeout`` seconds for it."""
    if timeout <= 0:
        log.warning("Exit: no time left to stop %s", name)
        return False
    done = threading.Event()

    def target() -> None:
        try:
            action()
        except Exception:
            log.exception("Exit: stopping %s failed", name)
        finally:
            done.set()

    threading.Thread(target=target, name=f"exit-{name}", daemon=True).start()
    if not done.wait(timeout):
        log.warning("Exit: %s did not stop within %.2f s", name, timeout)
        return False
    return True


class App:
    """Owns the collaborators and the process lifecycle of one Projektsøg instance."""

    def __init__(self, args: argparse.Namespace, instance: SingleInstance, *,
                 components: Components | None = None,
                 exit_deadline_s: float = EXIT_DEADLINE_S,
                 hotkey_recheck_s: float = HOTKEY_RECHECK_S) -> None:
        self.args = args
        self.instance = instance
        self._components = components or Components()
        self._exit_deadline_s = exit_deadline_s
        self._hotkey_recheck_s = hotkey_recheck_s
        self.cfg: Config | None = None
        self.bus: EventBus | None = None
        self.indexer: Indexer | None = None
        self.bridge: ResolveBridge | None = None
        self.tracker: TimeTracker | None = None
        self.importer: Importer | None = None
        self.controller: Controller | None = None
        self.server: Server | None = None
        self.window: AppWindow | None = None
        self.widget: PetWindow | None = None
        self.petplay: PetPlay | None = None
        self.messages: MessageBoard | None = None
        self.progress: PetProgress | None = None
        self.tray: TrayIcon | None = None
        self._actions: _ActionRunner | None = None
        self._notify_queue: queue.Queue | None = None
        self._instance_file: str | None = None
        self._shutdown = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._pending_config: dict[str, Any] | None = None
        self._hotkey_settings: dict[str, Any] = {}
        self._hotkey_failure_notified = False
        self._hotkey_check_at: float | None = None     # monotonic time of a pending re-check
        self._last_hotkey_status: dict[str, Any] | None = None

    # -- lifecycle ------------------------------------------------------------------------
    def run(self) -> int:
        """Start, wait for "Afslut", run the exit sequence. Returns the exit code."""
        code = 0
        try:
            self.start()
            self.wait_for_exit()
        except KeyboardInterrupt:
            log.info("Interrupted")
        except Exception:
            log.exception("Startup failed")
            code = 1
        self.exit_sequence()
        return code

    def start(self) -> None:
        """Create and start everything in the order of SPEC §13 (step 3)."""
        c, args = self._components, self.args
        with_ui = not args.no_window
        self._actions = _ActionRunner()
        self.cfg = cfg = c.config()
        self.bus = bus = EventBus()
        cfg.on_change(self._on_config_change)
        if with_ui:
            self._notify_queue = bus.subscribe()    # before anything can publish "notify"
        self.indexer = c.indexer(cfg, bus)
        self.indexer.start()
        if args.rescan:
            self.indexer.scan_now(None, full=True)
        self.bridge = c.bridge(cfg, bus, self.indexer)
        self.bridge.start()
        self.tracker = c.tracker(cfg, self.bridge)
        self.tracker.start()
        self.controller = c.controller(cfg, bus, self.indexer, self.bridge,
                                       request_exit=self.request_exit)
        self.importer = c.importer(cfg, bus, self.indexer, self.bridge, self.tracker, self.controller)
        self.server = c.server(cfg, bus, self.indexer, self.bridge, self.controller,
                               parse_hotkey=hotkey.parse_hotkey, tracker=self.tracker,
                               importer=self.importer)
        # Messages from other programs (the Claude sessions' Resolve queue), shown by Klippe.
        self.messages = c.messages(cfg, bus, shown=self._klippe_shown,
                                   path=os.path.join(config.app_dir(), "messages.json"))
        self.server.messages = self.messages
        # Klippe's trophies and wardrobe (achievements.py): from the time and the cards.
        self.progress = c.progress(cfg, bus, tracker=self.tracker, importer=self.importer,
                                   path=os.path.join(config.app_dir(), "pet.json"))
        self.server.progress = self.progress
        port = self.server.start(args.port)
        self._instance_file = config.instance_path()
        write_instance_file(self._instance_file, os.getpid(), port)
        if with_ui:
            self.window = c.window(f"http://{HOST}:{port}/", config.edge_profile_dir())
            self.controller.window = self.window
            # Klippe, the pet widget: its own small window, shown while widget_enabled is on –
            # and its games with the mouse pointer when nobody is at the PC (petplay.py).
            self.widget = c.widget(cfg, f"http://{HOST}:{port}/widget.html")
            self.petplay = c.petplay(cfg, bus, widget=self.widget, bridge=self.bridge,
                                     importer=self.importer, base_url=f"http://{HOST}:{port}",
                                     wardrobe=self.progress.equipped, on_game=self.progress.note_game)
            self.server.petplay = self.petplay
            self.widget.start()
            self.petplay.start()
            self._start_tray()
            self._spawn(self._forward_notifications, "notify-forwarder")
            self._hotkey_settings = _hotkey_settings(cfg.snapshot())
            if self._hotkey_settings["hotkey_enabled"]:
                self._start_hotkeys(self._hotkey_settings)
        self.importer.start()      # camera cards: a card going in may show the window
        self.progress.start()
        self._spawn(self._maintenance_loop, "app-maintenance")
        if with_ui:
            if args.background:
                self._actions.submit(self.window.preload)
            else:
                self._actions.submit(self.controller.show_window, reason="launch")
        log.info("%s %s running on port %d", APP_NAME, __version__, port)

    def _klippe_shown(self) -> bool:
        """Klippe is on the screen (it shows messages with their buttons)."""
        widget = self.widget
        hwnd = getattr(widget, "hwnd", None) if widget is not None else None
        return bool(self.cfg is not None and self.cfg.get("widget_enabled", False)
                    and hwnd and window_shown(hwnd))

    def request_exit(self) -> None:
        """"Afslut" / /api/quit: only signals; the main thread runs the exit sequence."""
        self._mark_exiting()
        self._shutdown.set()
        self._wake.set()

    def _mark_exiting(self) -> None:
        controller = self.controller
        if controller is not None:
            controller.exiting.set()

    def wait_for_exit(self) -> None:
        # A timed wait keeps Ctrl+C working when started from a console.
        while not self._shutdown.wait(1.0):
            pass

    def exit_sequence(self) -> None:
        """Stop everything within the exit deadline (SPEC §13); the caller then os._exit()s,
        so a thread stuck on a dead share cannot keep the process alive."""
        self._mark_exiting()
        self._shutdown.set()
        self._wake.set()
        # First of all: nobody may hand work to an instance that is going away. A launch
        # made now finds no instance.json, waits for the mutex and takes over (APP-2); the
        # Resolve script uses its own fallback.
        self._remove_instance_file()
        deadline = time.monotonic() + self._exit_deadline_s
        budgets = {name: share * self._exit_deadline_s for name, share in _EXIT_SHARES.items()}
        steps: dict[str, Callable[[], object]] = {}
        if self.controller is not None:
            steps["hotkey"] = self._stop_hotkeys
        if self.tray is not None:
            steps["tray"] = self.tray.stop
        if self.importer is not None:
            steps["import"] = self.importer.stop   # cancels a running copy (removes its temp file)
        if self.tracker is not None:
            steps["time"] = self.tracker.stop   # closes the running stretch before Resolve stops
        if self.bridge is not None:
            steps["resolve"] = self.bridge.stop
        if self.indexer is not None:
            indexer, worker_timeout = self.indexer, max(0.1, budgets["indexer"] - 0.25)
            steps["indexer"] = lambda: indexer.stop(timeout=worker_timeout)
        if self.server is not None:
            steps["server"] = self.server.stop
        # A game ends first: the helper puts the pointer back where it was.
        windows = [w.close for w in (self.petplay, self.progress, self.widget, self.window) if w is not None]
        if windows:
            steps["window"] = lambda: [close() for close in windows]
        for name, budget in budgets.items():
            if name in steps:
                _run_bounded(name, steps[name], min(budget, deadline - time.monotonic()))
        if self._actions is not None:
            self._actions.stop()
        if self._notify_queue is not None:
            self.bus.unsubscribe(self._notify_queue)
            try:
                self._notify_queue.put_nowait(_STOP)
            except queue.Full:
                pass    # the forwarder is stuck in tray.notify(); it ends with the process
        self._remove_instance_file()    # (again, in case a reader held it open) – while the
        self.instance.release()         # mutex is ours, so a successor's file is never hit
        log.info("%s stopped", APP_NAME)

    def _remove_instance_file(self) -> None:
        if self._instance_file is not None:
            remove_instance_file(self._instance_file, os.getpid())

    # -- startup helpers ------------------------------------------------------------------
    def _spawn(self, target: Callable[[], None], name: str) -> None:
        threading.Thread(target=target, name=name, daemon=True).start()

    def _start_tray(self) -> None:
        try:
            self.tray = self._components.tray(
                ICON_PATH, APP_NAME, on_show=self._tray_show, on_settings=self._tray_settings,
                on_scan_all=self._tray_scan_all, on_set_follow=self._tray_set_follow,
                on_set_autostart=self._tray_set_autostart, on_exit=self.request_exit,
                menu_state=self._tray_menu_state)
            if not self.tray.start():
                log.warning("The tray icon could not be added")
        except Exception:
            log.exception("The tray icon could not be created")

    def _start_hotkeys(self, settings: dict[str, Any]) -> None:
        options = {kw: settings[key] for key, kw in _HOTKEY_OPTIONS.items() if key != "hotkey"}
        try:
            manager = self._components.hotkeys(settings["hotkey"], self.controller.on_hotkey,
                                               enabled=True, **options)
        except Exception:
            log.exception("Could not create the hotkey manager")
            self._notify_hotkey_failure(settings["hotkey"])
            return
        self.controller.hotkeys = manager
        try:
            started = manager.start()
        except Exception:
            log.exception("The hotkey manager failed to start")
            started = False
        if not started:
            self._recheck_hotkey_later()

    def _recheck_hotkey_later(self) -> None:
        """The hotkey is not active (yet): its helper may still come up – e.g. right after
        login – so notify only when it is still inactive after HOTKEY_RECHECK_S."""
        log.info("The hotkey is not active (yet) – checking again in %g s",
                 self._hotkey_recheck_s)
        with self._lock:
            self._hotkey_check_at = time.monotonic() + self._hotkey_recheck_s
        self._wake.set()

    def _check_hotkey(self) -> None:
        with self._lock:
            due = self._hotkey_check_at
            if due is None or time.monotonic() < due:
                return
            self._hotkey_check_at = None
        manager = self.controller.hotkeys
        if manager is None:                 # switched off meanwhile
            return
        if manager.active:
            log.info("The hotkey became active after all")
            return
        log.warning("The hotkey is still not active")
        self._notify_hotkey_failure(self._hotkey_settings.get("hotkey", self.cfg["hotkey"]))

    def _stop_hotkeys(self) -> None:
        manager, self.controller.hotkeys = self.controller.hotkeys, None
        if manager is not None:
            manager.stop()

    def _notify_hotkey_failure(self, spec: str) -> None:
        if self._hotkey_failure_notified:       # once per session (SPEC §10.4)
            return
        self._hotkey_failure_notified = True
        label = self.controller.hotkey_label(spec)
        self.bus.publish("notify", {
            "title": APP_NAME,
            "text": f"Genvejstasten {label} kunne ikke aktiveres. "
                    "Åbn Projektsøg fra ikonet i meddelelsesområdet i stedet.",
            "level": "warn",
        })

    # -- tray callbacks (tray thread: return at once) -------------------------------------
    def _tray_show(self) -> None:
        self._actions.submit(self.controller.show_window, reason="tray")

    def _tray_settings(self) -> None:
        self._actions.submit(self.controller.show_window, reason="tray", panel="settings")

    def _tray_scan_all(self) -> None:
        self._actions.submit(self.indexer.scan_now, None)

    def _tray_set_follow(self, mode: str) -> None:
        self._actions.submit(self.cfg.update, {"resolve_follow": mode})

    def _tray_set_autostart(self, enabled: bool) -> None:
        self._actions.submit(self.controller.set_run_at_login, bool(enabled))

    def _tray_menu_state(self) -> dict[str, Any]:
        return {"hotkey_label": self.controller.hotkey_label(),
                "follow": self.cfg["resolve_follow"],
                "autostart": self.controller.get_run_at_login()}

    # -- background threads ---------------------------------------------------------------
    def _forward_notifications(self) -> None:
        """The one bus subscriber that shows ``notify`` events as tray notifications."""
        q = self._notify_queue
        while (item := q.get()) is not _STOP:
            event_type, data, _ts = item
            tray = self.tray
            if event_type != "notify" or tray is None or not isinstance(data, dict):
                continue
            try:
                tray.notify(str(data.get("title") or APP_NAME), str(data.get("text") or ""),
                            str(data.get("level") or "info"))
            except Exception:
                log.exception("Tray notification failed")

    def _on_config_change(self, snapshot: dict[str, Any]) -> None:
        # Runs on the thread that called cfg.update(): record and wake only (SPEC §3).
        with self._lock:
            self._pending_config = snapshot
        self._wake.set()

    def _maintenance_loop(self) -> None:
        while not self._shutdown.is_set():
            try:
                self._apply_pending_config()
                self._check_hotkey()
                self._publish_hotkey_status()
            except Exception:
                log.exception("App maintenance failed")
            self._wake.wait(self._maintenance_wait())
            self._wake.clear()

    def _maintenance_wait(self) -> float:
        with self._lock:
            due = self._hotkey_check_at
        if due is None:
            return HOTKEY_STATUS_POLL_S
        return min(HOTKEY_STATUS_POLL_S, max(0.0, due - time.monotonic()))

    def _apply_pending_config(self) -> None:
        with self._lock:
            snapshot, self._pending_config = self._pending_config, None
        if snapshot is None:
            return
        settings = dict(snapshot)
        settings["run_at_login"] = self.controller.get_run_at_login()
        self.bus.publish("settings", settings)
        if self.args.no_window:
            return
        wanted = _hotkey_settings(snapshot)
        if wanted != self._hotkey_settings:
            previous, self._hotkey_settings = self._hotkey_settings, wanted
            self._reconfigure_hotkeys(previous, wanted)

    def _reconfigure_hotkeys(self, previous: dict[str, Any], wanted: dict[str, Any]) -> None:
        manager = self.controller.hotkeys
        if not wanted["hotkey_enabled"]:
            if manager is not None:
                self._stop_hotkeys()
            return
        if manager is None:
            self._start_hotkeys(wanted)
            return
        changes = {kw: wanted[key] for key, kw in _HOTKEY_OPTIONS.items()
                   if wanted[key] != previous.get(key)}
        if not changes:
            return
        try:
            applied = manager.update(**changes)
        except ValueError as exc:       # a value the hotkey helper refuses (hand-edited config)
            log.warning("Hotkey settings refused: %s", exc)
            self._notify_hotkey_failure(wanted["hotkey"])
            return
        if not applied:
            self._recheck_hotkey_later()

    def _publish_hotkey_status(self) -> None:
        status = self.controller.hotkey_status()
        if status != self._last_hotkey_status:
            self._last_hotkey_status = status
            self.bus.publish("hotkey", status)


# ============================================================================================
# Process setup and entry point
# ============================================================================================

_crash_file = None      # keeps faulthandler's file open for the lifetime of the process


def harden_process(debug: bool) -> None:
    """Startup step 1 (SPEC §13): make the process safe to run under pythonw.exe."""
    has_stderr = sys.stderr is not None
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:      # pythonw.exe: writes would raise
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
    _kernel32.SetErrorMode(_kernel32.GetErrorMode() | SEM_FAILCRITICALERRORS
                           | SEM_NOOPENFILEERRORBOX)
    crash_log_error = _enable_faulthandler()
    _install_exception_hooks()
    _configure_logging(debug, console=debug and has_stderr)
    if crash_log_error is not None:
        log.warning("No crash log: %s", crash_log_error)
    os.chdir(config.app_dir())
    for what, step in (("DPI awareness", winui.set_dpi_awareness),
                       ("AppUserModelID", lambda: winui.set_app_user_model_id(AUMID))):
        try:
            step()
        except Exception:
            log.exception("Could not set %s", what)


def _enable_faulthandler() -> OSError | None:
    global _crash_file
    path = os.path.join(config.log_dir(), "crash.log")
    try:
        mode = "w" if os.path.getsize(path) > CRASH_LOG_MAX_BYTES else "a"
    except OSError:
        mode = "a"
    try:
        _crash_file = open(path, mode, encoding="utf-8")
        _crash_file.write(f"--- {APP_NAME} {__version__} started "
                          f"{time.strftime('%Y-%m-%d %H:%M:%S')} (pid {os.getpid()})\n")
        _crash_file.flush()
    except OSError as exc:
        return exc
    faulthandler.enable(file=_crash_file, all_threads=True)
    return None


def _install_exception_hooks() -> None:
    def excepthook(exc_type: type[BaseException], exc: BaseException, tb: Any) -> None:
        log.critical("Unhandled exception", exc_info=(exc_type, exc, tb))

    def thread_excepthook(args: threading.ExceptHookArgs) -> None:
        if args.exc_type is SystemExit:
            return
        name = args.thread.name if args.thread is not None else "?"
        log.error("Unhandled exception in thread %s", name,
                  exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    def unraisablehook(unraisable: Any) -> None:
        exc_info = ((unraisable.exc_type, unraisable.exc_value, unraisable.exc_traceback)
                    if unraisable.exc_type is not None else None)
        log.warning("%s: %r", unraisable.err_msg or "Exception ignored in", unraisable.object,
                    exc_info=exc_info)

    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook
    sys.unraisablehook = unraisablehook


def _configure_logging(debug: bool, console: bool) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s")
    handlers: list[logging.Handler] = [logging.handlers.RotatingFileHandler(
        os.path.join(config.log_dir(), "projektsog.log"), maxBytes=2 * 1024 * 1024,
        backupCount=3, encoding="utf-8")]
    if console:
        handlers.append(logging.StreamHandler(sys.stderr))
    for handler in handlers:
        handler.setFormatter(formatter)
        root.addHandler(handler)


def _port_arg(value: str) -> int:
    port = int(value) if value.isdigit() else -1
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"ugyldig port {value!r} – brug et tal fra 0 til 65535")
    return port


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="projektsog",
        description="Projektsøg – find projektmapper overalt med Shift+Mellemrum.")
    parser.add_argument("--background", action="store_true",
                        help="start skjult; vinduet forberedes i baggrunden (bruges ved autostart)")
    parser.add_argument("--no-window", action="store_true",
                        help="kør uden vindue, ikon og genvejstast – kun indeks og HTTP-server "
                             "(til test); stop med Ctrl+C")
    parser.add_argument("--port", type=_port_arg,
                        help="første port der prøves (standard: indstillingen 'port')")
    parser.add_argument("--debug", action="store_true",
                        help="detaljeret log, også i konsollen hvis der er en")
    parser.add_argument("--rescan", action="store_true",
                        help="scan alle placeringer helt forfra (kører Projektsøg allerede, "
                             "sendes det videre til den kørende)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    harden_process(args.debug)
    log.info("%s %s starting (pid %d, %s)", APP_NAME, __version__, os.getpid(), sys.executable)
    instance = SingleInstance(mutex_name())
    if not instance.acquire():
        code = handoff_to_running_instance(config.instance_path(), background=args.background,
                                           rescan=args.rescan, reacquire=instance.acquire)
        if code is not None:
            return code
        # The other instance exited while we waited: this process holds the mutex now.
    code = App(args, instance).run()
    logging.shutdown()
    os._exit(code)
