"""Smoke test of the winui modules on the real desktop – short, careful, read-only.

Run from the repo root:  python tests/smoke_winui.py [--steps hook,tray,edge,helpers,shell]
                                                     [--real-desktop]

Steps (each records the foreground window before/after and requires it to be unchanged):
  hook     the real hook child with --dry-run (passes every event through, never fires):
           'ready' arrives, extend_capture/end_capture are accepted, the child is stopped
           again well within 2 s;
  tray     the tray icon is shown for about a second and removed – no balloon;
  edge     AppWindow with a real Edge on a separate desktop that is never shown (CreateDesktopW,
           no SwitchDesktop – nothing can appear on the user's screen or take the foreground):
           preload() → hidden window; the "user" closes it (WM_CLOSE) → no msedge process of
           the temporary profile survives (background mode off – crashpad handler included);
           the watcher preloads a hidden window again (WIN-1); close() → again nothing
           survives, and Local State has background_mode.enabled == false;
  desktop  (only with --real-desktop) AppWindow.preload() on the user's real desktop: it must end
           up hidden and off-screen, no Edge window may be visible 1.5 s later, and the
           foreground must not stay inside our Edge (sampled every 2 ms; Chromium may activate
           its first window itself for a few ms after a long idle period – reported as WARN);
  helpers  read-only helpers: foreground_process_name, process_running/process_uptime,
           edge_path, get_run_at_login;
  shell    the shell (STA) thread runs a harmless COM call – nothing is opened.
Every step that starts Edge kills all processes of its temporary profile at the end (browser,
renderers, GPU/utility processes and the crashpad handler), whatever happened. The test never
calls open_folder/reveal/open_file/force_foreground (preload() may hand the foreground back to
the previously active window if Edge kept it). LOCALAPPDATA is a temporary folder.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
import urllib.parse
import uuid
from ctypes import wintypes
from unittest import mock

_TMP = tempfile.TemporaryDirectory(prefix="projektsog-smoke-")
os.environ["LOCALAPPDATA"] = _TMP.name
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from projektsog import config, hotkey, tray, window, winui  # noqa: E402

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
ole32 = ctypes.WinDLL("ole32", use_last_error=True)
ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
HANDLE = wintypes.HANDLE


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
                ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
                ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD), ("dwXSize", wintypes.DWORD),
                ("dwYSize", wintypes.DWORD), ("dwXCountChars", wintypes.DWORD),
                ("dwYCountChars", wintypes.DWORD), ("dwFillAttribute", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("wShowWindow", wintypes.WORD),
                ("cbReserved2", wintypes.WORD), ("lpReserved2", ctypes.c_void_p),
                ("hStdInput", HANDLE), ("hStdOutput", HANDLE), ("hStdError", HANDLE)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", HANDLE), ("hThread", HANDLE), ("dwProcessId", wintypes.DWORD),
                ("dwThreadId", wintypes.DWORD)]


class UNICODE_STRING(ctypes.Structure):
    _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT),
                ("Buffer", ctypes.c_void_p)]


def _declare(dll, name, restype, *argtypes):
    fn = getattr(dll, name)
    fn.restype = restype
    fn.argtypes = list(argtypes)
    return fn


_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, HANDLE, wintypes.LPARAM)
_CreateDesktopW = _declare(user32, "CreateDesktopW", HANDLE, wintypes.LPCWSTR, wintypes.LPCWSTR,
                           ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p)
_CloseDesktop = _declare(user32, "CloseDesktop", wintypes.BOOL, HANDLE)
_EnumDesktopWindows = _declare(user32, "EnumDesktopWindows", wintypes.BOOL, HANDLE, _WNDENUMPROC,
                               wintypes.LPARAM)
_PostMessageW = _declare(user32, "PostMessageW", wintypes.BOOL, HANDLE, wintypes.UINT,
                         wintypes.WPARAM, wintypes.LPARAM)
_CreateProcessW = _declare(kernel32, "CreateProcessW", wintypes.BOOL, wintypes.LPCWSTR,
                           wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, wintypes.BOOL,
                           wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
                           ctypes.POINTER(STARTUPINFOW), ctypes.POINTER(PROCESS_INFORMATION))
_OpenProcess = _declare(kernel32, "OpenProcess", HANDLE, wintypes.DWORD, wintypes.BOOL,
                        wintypes.DWORD)
_CloseHandle = _declare(kernel32, "CloseHandle", wintypes.BOOL, HANDLE)
_TerminateProcess = _declare(kernel32, "TerminateProcess", wintypes.BOOL, HANDLE, wintypes.UINT)
_WaitForSingleObject = _declare(kernel32, "WaitForSingleObject", wintypes.DWORD, HANDLE,
                                wintypes.DWORD)
_GetExitCodeProcess = _declare(kernel32, "GetExitCodeProcess", wintypes.BOOL, HANDLE,
                               ctypes.POINTER(wintypes.DWORD))
_GetProcessTimes = _declare(kernel32, "GetProcessTimes", wintypes.BOOL, HANDLE,
                            ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
                            ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME))
_NtQueryInformationProcess = _declare(ntdll, "NtQueryInformationProcess", ctypes.c_long, HANDLE,
                                      wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG,
                                      ctypes.POINTER(wintypes.ULONG))
PROCESS_TERMINATE, PROCESS_QUERY_LIMITED_INFORMATION, SYNCHRONIZE = 0x0001, 0x1000, 0x00100000
PROCESS_COMMAND_LINE_INFORMATION = 60
STATUS_INFO_LENGTH_MISMATCH = -1073741820
WAIT_OBJECT_0, WAIT_TIMEOUT = 0, 0x102
GENERIC_ALL = 0x10000000
WM_CLOSE = 0x0010


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


class NOTIFYICONIDENTIFIER(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HANDLE), ("uID", wintypes.UINT),
                ("guidItem", ctypes.c_byte * 16)]


user32.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]
user32.GetLastInputInfo.restype = wintypes.BOOL
user32.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT, wintypes.LPVOID, wintypes.UINT]
user32.SystemParametersInfoW.restype = wintypes.BOOL
user32.GetWindowRect.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HANDLE]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.IsIconic.argtypes = [wintypes.HANDLE]
user32.IsIconic.restype = wintypes.BOOL
user32.IsWindow.argtypes = [wintypes.HANDLE]
user32.IsWindow.restype = wintypes.BOOL
kernel32.GetTickCount.argtypes = []
kernel32.GetTickCount.restype = wintypes.DWORD
shell32.Shell_NotifyIconGetRect.argtypes = [ctypes.POINTER(NOTIFYICONIDENTIFIER),
                                            ctypes.POINTER(wintypes.RECT)]
shell32.Shell_NotifyIconGetRect.restype = ctypes.c_long
ole32.CoGetApartmentType.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)]
ole32.CoGetApartmentType.restype = ctypes.c_long
ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
ole32.CoTaskMemFree.restype = None
shell32.GetCurrentProcessExplicitAppUserModelID.argtypes = [ctypes.POINTER(ctypes.c_wchar_p)]
shell32.GetCurrentProcessExplicitAppUserModelID.restype = ctypes.c_long

REPORT: list[str] = []
FAILURES: list[str] = []


def say(line: str) -> None:
    REPORT.append(line)
    print(line, flush=True)


def foreground() -> tuple[int | None, str | None, str]:
    hwnd = winui.foreground_window()
    info = winui.window_info(hwnd)
    return hwnd, (info.exe if info else None), (info.title[:50] if info else "")


def check(condition: bool, message: str) -> None:
    say(("  OK   " if condition else "  FAIL ") + message)
    if not condition:
        FAILURES.append(message)


class Step:
    """Prints a header and verifies the foreground window did not change during the step."""

    def __init__(self, title: str, ours: set[int] | None = None) -> None:
        self.title = title
        self.ours = ours if ours is not None else set()

    def __enter__(self) -> "Step":
        say(f"\n== {self.title}")
        self.before = foreground()
        self.started = time.monotonic()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        after = foreground()
        say(f"  foreground before: {self.before[0]} {self.before[1]} '{self.before[2]}'")
        say(f"  foreground after:  {after[0]} {after[1]} '{after[2]}'")
        if after[0] in self.ours:
            check(False, "one of OUR windows became the foreground window")
        elif after[0] != self.before[0]:
            say("  WARN foreground changed to a window that is not ours (user switched?)")
            FAILURES.append(f"{self.title}: foreground changed")
        else:
            check(True, "foreground unchanged")
        if exc is not None:
            check(False, f"exception: {exc_type.__name__}: {exc}")
        say(f"  step took {time.monotonic() - self.started:.2f} s")
        return True


# --------------------------------------------------------------------------------------
# Edge process bookkeeping: find and (at the end, always) kill every process of a profile
# --------------------------------------------------------------------------------------

def _process_table() -> list[tuple[int, int, str]]:
    """(pid, parent pid, image name) of every process (Toolhelp snapshot)."""
    snapshot = winui._CreateToolhelp32Snapshot(winui.TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == winui.INVALID_HANDLE_VALUE:
        return []
    table: list[tuple[int, int, str]] = []
    try:
        entry = winui._PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = winui._Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            table.append((entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile))
            ok = winui._Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        winui._CloseHandle(snapshot)
    return table


def _command_line(pid: int) -> str | None:
    handle = _OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        size = wintypes.ULONG(0)
        buf = ctypes.create_string_buffer(4096)
        status = _NtQueryInformationProcess(handle, PROCESS_COMMAND_LINE_INFORMATION, buf,
                                            len(buf), ctypes.byref(size))
        if status == STATUS_INFO_LENGTH_MISMATCH and size.value:
            buf = ctypes.create_string_buffer(size.value)
            status = _NtQueryInformationProcess(handle, PROCESS_COMMAND_LINE_INFORMATION, buf,
                                                len(buf), ctypes.byref(size))
        if status != 0:
            return None
        text = UNICODE_STRING.from_buffer(buf)
        return ctypes.wstring_at(text.Buffer, text.Length // 2) if text.Buffer else ""
    finally:
        _CloseHandle(handle)


def _running_since(pid: int) -> int | None:
    """Creation time of ``pid`` if it is still running (an exited process can linger as an
    object while another process holds a handle to it – that does not count)."""
    handle = _OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid)
    if not handle:
        return None
    try:
        if _WaitForSingleObject(handle, 0) != WAIT_TIMEOUT:
            return None
        times = [wintypes.FILETIME() for _ in range(4)]
        if not _GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            return None
        return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
    finally:
        _CloseHandle(handle)


class EdgeProcesses:
    """All msedge processes started because of one test profile: those whose command line
    names the profile or one of ``extra`` folders (the temporary LOCALAPPDATA – Edge started
    for its default profile there is ours too), the browser that owns such a process, and all
    msedge descendants of anything ever seen (renderers, GPU, utility, crashpad handler – and a
    browser Edge spawned while shutting down). Every process is remembered with its creation
    time, so ``kill_all()`` ends whatever is left – never a process that merely reused a PID."""

    def __init__(self, profile: str, extra: tuple[str, ...] = ()) -> None:
        self.keys = tuple(os.path.normcase(os.path.abspath(p)) for p in (profile, *extra))
        self.seen: dict[int, tuple[int, str]] = {}          # pid -> (created, kind)

    def scan(self) -> dict[int, str]:
        """pid -> kind ('browser', 'renderer', 'crashpad-handler', …) of the running ones."""
        table = {pid: ppid for pid, ppid, exe in _process_table() if exe.casefold() == "msedge.exe"}
        born = {pid: t for pid in table if (t := _running_since(pid)) is not None}
        lines = {pid: _command_line(pid) or "" for pid in born}
        found = {pid for pid in born if any(k in os.path.normcase(lines[pid]) for k in self.keys)}
        for pid in list(found):                  # the browser that owns a matching child
            parent = table.get(pid)
            if (parent in born and "--type=" not in lines[parent]
                    and born[parent] <= born[pid] and "--type=" in lines[pid]):
                found.add(parent)
        grew = True
        while grew:                              # descendants, also of processes that exited
            grew = False
            for pid, parent in table.items():
                if pid in born and pid not in found:
                    parent_born = born.get(parent) if parent in found else (
                        self.seen[parent][0] if parent in self.seen else None)
                    if parent_born is not None and born[pid] >= parent_born:
                        found.add(pid)
                        grew = True
        for pid in found:
            if pid not in self.seen:
                self.seen[pid] = (born[pid], self._kind(lines[pid]))
        return {pid: self.seen[pid][1] for pid in found}

    @staticmethod
    def _kind(line: str) -> str:
        if "--type=" in line:
            return line.split("--type=", 1)[1].split()[0].strip('"')
        return "browser --no-startup-window" if "--no-startup-window" in line else "browser"

    def alive(self, pids) -> dict[int, str]:
        """Which of ``pids`` (seen before) still run – same PID *and* creation time."""
        return {pid: self.seen[pid][1] for pid in pids
                if pid in self.seen and _running_since(pid) == self.seen[pid][0]}

    def wait_gone(self, pids, timeout: float) -> dict[int, str]:
        """Wait until none of ``pids`` runs; also records processes started meanwhile."""
        deadline = time.monotonic() + timeout
        while True:
            self.scan()
            left = self.alive(pids)
            if not left or time.monotonic() >= deadline:
                return left
            time.sleep(0.1)

    def spawned_boost(self) -> dict[int, str]:
        """Background browsers Edge started by itself (startup boost) – must never happen."""
        return {pid: kind for pid, (_born, kind) in self.seen.items()
                if kind == "browser --no-startup-window"}

    def kill_all(self) -> dict[int, str]:
        """Terminate every remembered or currently found process that still runs."""
        self.scan()
        killed: dict[int, str] = {}
        for pid, kind in self.alive(list(self.seen)).items():
            handle = _OpenProcess(PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION
                                  | SYNCHRONIZE, False, pid)
            if handle:
                try:
                    if _running_since(pid) == self.seen[pid][0] and _TerminateProcess(handle, 1):
                        killed[pid] = kind
                finally:
                    _CloseHandle(handle)
        return killed


def summary(processes: dict[int, str]) -> str:
    kinds: dict[str, int] = {}
    for kind in processes.values():
        kinds[kind] = kinds.get(kind, 0) + 1
    return ", ".join(f"{n}× {kind}" for kind, n in sorted(kinds.items())) or "none"


# --------------------------------------------------------------------------------------
# A desktop that is never shown: real windows, but nothing on the user's screen
# --------------------------------------------------------------------------------------

class DesktopProcess:
    """The part of ``subprocess.Popen`` that AppWindow uses, started on another desktop
    (``STARTUPINFO.lpDesktop``, which ``subprocess`` cannot set)."""

    desktop = ""                                    # "WinSta0\\<name>", set by HiddenDesktop

    def __init__(self, args, *, cwd=None, startupinfo=None, **_ignored) -> None:
        info = STARTUPINFOW()
        info.cb = ctypes.sizeof(info)
        info.lpDesktop = self.desktop
        if startupinfo is not None:
            info.dwFlags = startupinfo.dwFlags
            info.wShowWindow = startupinfo.wShowWindow
        process = PROCESS_INFORMATION()
        command = ctypes.create_unicode_buffer(subprocess.list2cmdline(args))
        if not _CreateProcessW(None, command, None, None, False, 0, None, cwd,
                               ctypes.byref(info), ctypes.byref(process)):
            raise ctypes.WinError(ctypes.get_last_error())
        _CloseHandle(process.hThread)
        self._handle = process.hProcess
        self.pid = process.dwProcessId
        self.returncode: int | None = None

    def poll(self) -> int | None:
        if self._handle is not None and _WaitForSingleObject(self._handle, 0) == WAIT_OBJECT_0:
            code = wintypes.DWORD()
            _GetExitCodeProcess(self._handle, ctypes.byref(code))
            self.returncode = code.value
            _CloseHandle(self._handle)          # do not keep the exited process object around
            self._handle = None
        return self.returncode

    def terminate(self) -> None:
        if self.poll() is None:
            _TerminateProcess(self._handle, 1)


class HiddenDesktop:
    """``with HiddenDesktop() as desk, desk.patched():`` – AppWindow launches Edge on a
    desktop of its own that is never switched to, and finds windows there."""

    def __init__(self) -> None:
        self.name = "projektsog-smoke-" + uuid.uuid4().hex[:8]
        self.handle = None

    def __enter__(self) -> "HiddenDesktop":
        self.handle = _CreateDesktopW(self.name, None, None, 0, GENERIC_ALL, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        return self

    def __exit__(self, *exc) -> None:
        if self.handle:
            _CloseDesktop(self.handle)
            self.handle = None

    def enum_handles(self, classes: frozenset[str]) -> list[int]:
        """winui._enum_handles for this desktop (EnumWindows only sees the thread's own)."""
        handles: list[int] = []

        def collect(hwnd, _lparam):
            if hwnd and winui._class_name(hwnd) in classes:
                handles.append(hwnd)
            return True

        _EnumDesktopWindows(self.handle, _WNDENUMPROC(collect), 0)
        return handles

    @contextlib.contextmanager
    def patched(self):
        shim = types.SimpleNamespace(STARTUPINFO=subprocess.STARTUPINFO, DEVNULL=subprocess.DEVNULL,
                                     STARTF_USESHOWWINDOW=subprocess.STARTF_USESHOWWINDOW,
                                     Popen=type("Popen", (DesktopProcess,),
                                                {"desktop": f"WinSta0\\{self.name}"}))
        with mock.patch.object(window, "subprocess", shim), \
                mock.patch.object(winui, "_enum_handles", self.enum_handles):
            yield


def local_state(profile: str) -> dict | None:
    try:
        with open(os.path.join(profile, window.LOCAL_STATE), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def step_hook_child() -> None:
    with Step("1. hook child --dry-run (real WH_KEYBOARD_LL hook, passes everything)"):
        log_file = os.path.join(config.log_dir(), "hotkey-smoke.log")
        fires: list[dict] = []
        pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        argv = [pythonw if os.path.isfile(pythonw) else sys.executable, "-m",
                "projektsog.hotkey", "--child", "--dry-run", "--log-file", log_file]
        manager = hotkey.HotkeyManager("shift+space", fires.append, child_argv=argv)
        t0 = time.monotonic()
        try:
            ok = manager.start()
            t_ready = time.monotonic()
            proc = manager._child.proc if manager._child is not None else None
            mode = manager.mode
            manager.extend_capture(2.0)     # SPEC §15.10 command (no capture running: no-op)
            manager.end_capture(False)
            time.sleep(0.1)
            alive_after_commands = proc is not None and proc.poll() is None
        finally:
            manager.stop()                  # a hook must never outlive this step
        t_stopped = time.monotonic()
        say(f"  child argv: {os.path.basename(argv[0])} {' '.join(argv[1:5])}")
        check(ok, f"start() returned True (ready after {1000 * (t_ready - t0):.0f} ms), mode={mode!r}")
        check(mode == "ll", "mode is 'll'")
        check(alive_after_commands, "child still running after extend_capture/end_capture")
        check(proc is not None and proc.poll() is not None,
              f"child exited (exit code {proc.poll() if proc else None})")
        check(t_stopped - t_ready < 2.0,
              f"hook lifetime ready→stopped {1000 * (t_stopped - t_ready):.0f} ms (< 2 s)")
        check(not fires, "no fire events")
        try:
            with open(log_file, encoding="utf-8") as fh:
                lines = [line.strip() for line in fh if line.strip()]
            say("  child log: " + " | ".join(line.split(": ", 1)[-1] for line in lines))
            check(not any("unknown command" in line or "ignoring" in line for line in lines),
                  "the child accepted every command")
        except OSError as exc:
            say(f"  child log unreadable: {exc}")


def step_tray() -> None:
    icon_path = os.path.join(os.path.dirname(os.path.abspath(tray.__file__)), "assets", "icon.ico")
    icon = tray.TrayIcon(
        icon_path, "Projektsøg (røgtest)", on_show=lambda: None, on_settings=lambda: None,
        on_scan_all=lambda: None, on_set_follow=lambda v: None, on_set_autostart=lambda v: None,
        on_exit=lambda: None,
        menu_state=lambda: {"hotkey_label": "Shift+Mellemrum", "follow": "notify",
                            "autostart": False})
    ours: set[int] = set()
    with Step("2. tray icon shown briefly, no balloon", ours):
        t0 = time.monotonic()
        try:
            added = icon.start()
            hwnd = icon._hwnd
            if hwnd:
                ours.add(hwnd)
            check(added, f"icon added after {1000 * (time.monotonic() - t0):.0f} ms "
                         f"(icon size {icon._icon_size}px, hidden window {hwnd})")
            rect_hr, rect = icon_rect(hwnd)
            say(f"  Shell_NotifyIconGetRect: hr=0x{rect_hr & 0xFFFFFFFF:08X} rect={rect}")
            time.sleep(1.0)
        finally:
            icon.stop()
        shown = time.monotonic() - t0
        check(shown <= 3.0, f"icon removed after {shown:.2f} s")
        gone_hr, _ = icon_rect(hwnd)
        check(gone_hr != 0, f"icon no longer registered (hr=0x{gone_hr & 0xFFFFFFFF:08X})")
        check(icon._thread is None, "tray thread finished")


def icon_rect(hwnd: int | None) -> tuple[int, tuple | None]:
    ident = NOTIFYICONIDENTIFIER(cbSize=ctypes.sizeof(NOTIFYICONIDENTIFIER), hWnd=hwnd, uID=tray.ICON_ID)
    rect = wintypes.RECT()
    hr = shell32.Shell_NotifyIconGetRect(ctypes.byref(ident), ctypes.byref(rect))
    return hr, ((rect.left, rect.top, rect.right, rect.bottom) if hr == 0 else None)


def idle_ms() -> int:
    info = LASTINPUTINFO(cbSize=ctypes.sizeof(LASTINPUTINFO))
    user32.GetLastInputInfo(ctypes.byref(info))
    return (kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF


def lock_timeout_ms() -> int:
    value = wintypes.DWORD()
    user32.SystemParametersInfoW(0x2000, 0, ctypes.byref(value), 0)   # SPI_GETFOREGROUNDLOCKTIMEOUT
    return value.value


def edge_windows() -> dict[int, winui.WindowInfo]:
    return {w.hwnd: w for w in winui._enum_windows(frozenset({winui.CHROME_WINDOW_CLASS}))
            if (w.exe or "").casefold() == "msedge.exe"}


class FocusTracer(threading.Thread):
    """Samples the foreground window every 2 ms, so even a brief activation is seen."""

    def __init__(self) -> None:
        super().__init__(name="focus-tracer", daemon=True)
        self.events: list[tuple[float, int | None, int | None, str]] = []
        self._stop_event = threading.Event()
        self._t0 = time.perf_counter()
        self._end = 0.0

    def run(self) -> None:
        last = -1
        while not self._stop_event.is_set():
            hwnd = winui.foreground_window()
            if hwnd != last:
                info = winui.window_info(hwnd)
                self.events.append((time.perf_counter() - self._t0, hwnd,
                                    info.pid if info else None,
                                    f"{info.exe} '{info.title[:30]}'" if info else "-"))
                last = hwnd
            time.sleep(0.002)

    def stop(self) -> None:
        self._end = time.perf_counter() - self._t0
        self._stop_event.set()
        self.join(1.0)

    def foreground_ms(self, pids: set[int]) -> float:
        total = 0.0
        for (t, _hwnd, pid, _what), following in zip(self.events, self.events[1:] + [None]):
            if pid in pids:
                total += (following[0] if following else self._end) - t
        return total * 1000


def data_url(title: str) -> str:
    html = f"<!doctype html><meta charset='utf-8'><title>{title}</title><p>røgtest"
    return "data:text/html;charset=utf-8," + urllib.parse.quote(html)


def step_edge_hidden_desktop() -> None:
    title = f"Projektsøg røgtest {uuid.uuid4().hex[:8]}"
    profile = os.path.join(_TMP.name, "edge-profile-hidden")
    processes = EdgeProcesses(profile, extra=(_TMP.name,))
    app = window.AppWindow(data_url(title), profile, title=title)
    app.WATCH_INTERVAL_S, app.REPRELOAD_DELAY_S = 0.25, 1.0     # production: 2 s and 3 s
    with Step("3. AppWindow + real Edge on a hidden desktop: re-preload after close, "
              "no lingering processes"):
        try:
            with HiddenDesktop() as desk, desk.patched():
                say(f"  desktop WinSta0\\{desk.name} (never shown), profile {profile}")
                check(app.needs_launch(), "needs_launch() is True before the first launch")
                t0 = time.monotonic()
                app.preload()
                first = winui.find_app_window_info(title)
                check(first is not None, f"preload() found the window after "
                                         f"{time.monotonic() - t0:.2f} s")
                if first is None:
                    return
                check(wait_until(lambda: not app.is_visible(), 2.0),
                      "the preloaded window is hidden (ShowWindowAsync applied)")
                check(not app.needs_launch(), "needs_launch() is False once the window is ready")
                generation1 = processes.scan()
                say(f"  first Edge: {summary(generation1)}")
                # The user closes the window (X / Alt+F4 both end in WM_CLOSE).
                t1 = time.monotonic()
                _PostMessageW(first.hwnd, WM_CLOSE, 0, 0)
                left = processes.wait_gone(generation1, 10.0)
                check(not left, f"no process of the closed Edge survived (all gone after "
                                f"{time.monotonic() - t1:.2f} s)"
                      + (f" – still running: {summary(left)}" if left else ""))

                def relaunched() -> bool:
                    info = winui.find_app_window_info(title)
                    return info is not None and info.hwnd != first.hwnd and not app.needs_launch()

                again = wait_until(relaunched, 20.0)
                second = winui.find_app_window_info(title)
                check(again and second is not None,
                      f"the watcher preloaded a new window {time.monotonic() - t1:.2f} s after "
                      "the close")
                if second is not None:
                    check(wait_until(lambda: not app.is_visible(), 2.0), "… and it is hidden")
                    check(second.pid != first.pid,
                          f"… in a new Edge (pid {first.pid} → {second.pid})")
                generation2 = processes.scan()
                fresh = {pid: kind for pid, kind in generation2.items() if pid not in generation1}
                say(f"  second Edge: {summary(fresh)}")
                t2 = time.monotonic()
                app.close()
                left = processes.wait_gone(generation2, 10.0)
                check(not left, f"close(): every process of the profile exited "
                                f"({time.monotonic() - t2:.2f} s)"
                      + (f" – still running: {summary(left)}" if left else ""))
                time.sleep(1.0)
                check(not processes.scan(), "no new Edge after close() (watcher stopped)")
                boost = processes.spawned_boost()
                check(not boost, "Edge started no startup-boost browser in the background"
                      + (f" – it did: {sorted(boost)}" if boost else ""))
                state = local_state(profile) or {}
                prefs = {key: (state.get(key) or {}).get("enabled")
                         for key in ("background_mode", "startup_boost")}
                check(prefs == {"background_mode": False, "startup_boost": False},
                      f"Local State keeps both off: {prefs}")
        finally:
            app.close()
            killed = processes.kill_all()
            say(f"  cleanup: killed {summary(killed)}; {len(processes.seen)} processes seen "
                "in total")
            if killed:
                FAILURES.append("edge: processes had to be killed")
    remove_tree(profile)


def step_real_desktop_preload() -> None:
    idle, lock = idle_ms(), lock_timeout_ms()
    title = f"Projektsøg røgtest {uuid.uuid4().hex[:8]}"
    profile = os.path.join(_TMP.name, "edge-profile-smoke")
    processes = EdgeProcesses(profile, extra=(_TMP.name,))
    app = window.AppWindow(data_url(title), profile, title=title)
    app.WATCH = False                       # never a re-preload on the user's screen
    ours: set[int] = set()
    before = edge_windows()
    tracer = FocusTracer()
    with Step("3b. AppWindow.preload() on the real desktop – hidden, off-screen, focus handed "
              "back", ours):
        say(f"  user idle {idle / 1000:.1f} s, foreground lock timeout {lock / 1000:.0f} s")
        tracer.start()
        t0 = time.monotonic()
        try:
            try:
                app.preload()
            finally:
                took = time.monotonic() - t0
                info = winui.find_app_window_info(title)
                proc = app._proc
                processes.scan()
                if info is None:
                    app.close()             # never leave our Edge behind
            edge_pids = {p for p in (proc.pid if proc else None, info.pid if info else None) if p}
            check(info is not None, f"identification found the window; preload() took {took:.2f} s")
            if info is not None:
                ours.add(info.hwnd)
                say(f"  window: hwnd={info.hwnd} class={info.cls} exe={info.exe} pid={info.pid} "
                    f"(launched pid {proc.pid if proc else None}) owner={info.owner}")
                say(f"  exact title used by Edge: {info.title!r}")
                rect = wintypes.RECT()
                user32.GetWindowRect(info.hwnd, ctypes.byref(rect))
                say(f"  after preload: visible={bool(user32.IsWindowVisible(info.hwnd))} "
                    f"iconic={bool(user32.IsIconic(info.hwnd))} "
                    f"rect=({rect.left},{rect.top},{rect.right},{rect.bottom}) "
                    f"normal size={window._normal_size(info.hwnd)}")
                check(wait_until(lambda: not user32.IsWindowVisible(info.hwnd), 2.0),
                      "window is hidden")
                check(not app.is_visible() and not app.is_foreground(),
                      "AppWindow.is_visible()/is_foreground() are False")
            time.sleep(1.5)                 # delayed popups (sign-in notice etc.) show up here
            new_visible = [w for h, w in edge_windows().items() if h not in before and w.visible]
            ours.update(w.hwnd for w in new_visible)
            check(not new_visible, "no visible Edge window 1.5 s after preload"
                  + (f": {[(w.hwnd, w.title, w.owner) for w in new_visible]}" if new_visible else ""))
            final = winui.window_info(winui.foreground_window())
            check(final is None or final.pid not in edge_pids, "foreground is not inside our Edge")
            running = processes.scan()
            say(f"  Edge processes: {summary(running)}")
            t1 = time.monotonic()
            app.close()
            gone = wait_until(lambda: winui.find_app_window_info(title) is None, 5.0)
            left = processes.wait_gone(running, 10.0)
            check(gone, f"window closed after {time.monotonic() - t1:.2f} s")
            check(not left, "every process of the profile exited (crashpad handler included)"
                  + (f" – still running: {summary(left)}" if left else ""))
            boost = processes.spawned_boost()
            check(not boost, "Edge started no startup-boost browser in the background"
                  + (f" – it did: {sorted(boost)}" if boost else ""))
            tracer.stop()
            say("  foreground trace: " + " → ".join(f"+{t * 1000:.0f}ms {what}"
                                                     for t, _h, _p, what in tracer.events))
            activated = tracer.foreground_ms(edge_pids)
            if activated:
                say(f"  WARN Edge activated its new window itself for {activated:.0f} ms before it "
                    "was hidden (Chromium calls SetForegroundWindow on first show; Windows allowed "
                    f"it because nobody has typed for {idle / 1000:.0f} s)")
            else:
                check(True, "Edge never became the foreground window")
        finally:
            tracer.stop()
            app.close()
            killed = processes.kill_all()
            say(f"  cleanup: killed {summary(killed)}")
            if killed:
                FAILURES.append("desktop: processes had to be killed")
    remove_tree(profile)


def wait_until(predicate, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def remove_tree(path: str) -> None:
    for _ in range(20):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(0.25)
    say(f"  (could not remove {path} yet – left in the temp folder)")


def step_read_only_helpers() -> None:
    with Step("4. read-only helpers"):
        t0 = time.perf_counter()
        name = winui.foreground_process_name()
        running = winui.process_running("Resolve.exe")
        uptime = winui.process_uptime("Resolve.exe")
        edge = winui.edge_path()
        autostart = winui.get_run_at_login()
        took = 1000 * (time.perf_counter() - t0)
        say(f"  foreground_process_name() = {name!r}")
        say(f"  process_running('Resolve.exe') = {running}")
        say(f"  process_uptime('Resolve.exe') = "
            f"{f'{uptime:.0f} s ({uptime / 3600:.1f} h)' if uptime is not None else None}")
        say(f"  edge_path() = {edge!r}")
        say(f"  get_run_at_login() = {autostart}")
        check(name is not None, "foreground process name known")
        check(running == (uptime is not None), "process_running and process_uptime agree")
        check(edge is not None and os.path.isfile(edge), "Edge found")
        say(f"  all five took {took:.1f} ms")
        winui.set_app_user_model_id("Projektsog.App.Smoke")      # this process only
        value = ctypes.c_wchar_p()
        hr = shell32.GetCurrentProcessExplicitAppUserModelID(ctypes.byref(value))
        aumid = value.value if hr == 0 else None
        if hr == 0:
            ole32.CoTaskMemFree(value)
        check(aumid == "Projektsog.App.Smoke", f"AppUserModelID set: {aumid!r}")


def step_shell_thread() -> None:
    with Step("5. shell thread: STA apartment + SHParseDisplayName (nothing opened)"):
        def probe() -> tuple[int, str, bool]:
            kind, qualifier = ctypes.c_int(), ctypes.c_int()
            hr = ole32.CoGetApartmentType(ctypes.byref(kind), ctypes.byref(qualifier))
            pidl = ctypes.c_void_p()
            parsed = winui._SHParseDisplayName(_TMP.name, None, ctypes.byref(pidl), 0, None)
            if pidl.value:
                winui._CoTaskMemFree(pidl)
            return (kind.value if hr == 0 else -1), threading.current_thread().name, parsed == 0

        t0 = time.monotonic()
        result = winui._shell_call(probe)
        check(result is not None, f"shell call returned in {1000 * (time.monotonic() - t0):.0f} ms")
        if result is not None:
            apartment, thread_name, parsed = result
            check(apartment in (0, 3), f"apartment type {apartment} (0=STA, 3=main STA) on "
                                       f"{thread_name}")
            check(parsed, "SHParseDisplayName worked on the shell thread")


STEPS = {"hook": step_hook_child, "tray": step_tray, "edge": step_edge_hidden_desktop,
         "desktop": step_real_desktop_preload, "helpers": step_read_only_helpers,
         "shell": step_shell_thread}
DEFAULT_STEPS = ("hook", "tray", "edge", "helpers", "shell")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="winui smoke test (see the module docstring)")
    parser.add_argument("--steps", default=",".join(DEFAULT_STEPS),
                        help="comma-separated subset of: " + ", ".join(STEPS))
    parser.add_argument("--real-desktop", action="store_true",
                        help="also preload an Edge window on the user's real desktop")
    args = parser.parse_args(argv)
    steps = [name.strip() for name in args.steps.split(",") if name.strip()]
    if args.real_desktop and "desktop" not in steps:
        steps.insert(steps.index("edge") + 1 if "edge" in steps else len(steps), "desktop")
    unknown = [name for name in steps if name not in STEPS]
    if unknown:
        parser.error(f"unknown step(s): {', '.join(unknown)}")
    logging.basicConfig(level=logging.INFO, format="  log %(name)s %(levelname)s: %(message)s")
    winui.set_dpi_awareness()
    say(f"Projektsøg winui smoke test – {time.strftime('%Y-%m-%d %H:%M:%S')}, "
        f"LOCALAPPDATA={_TMP.name}, steps: {', '.join(steps)}")
    for name in steps:
        STEPS[name]()
    say("\nRESULT: " + ("all checks passed" if not FAILURES else f"{len(FAILURES)} problem(s): "
                        + "; ".join(FAILURES)))
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    try:
        code = main(sys.argv[1:])
    finally:
        logging.shutdown()
        try:
            _TMP.cleanup()
        except OSError:
            pass
    sys.exit(code)
