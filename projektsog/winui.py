"""Windows shell and window helpers (SPEC §10.1).

* Explorer: ``open_folder`` / ``reveal`` / ``open_file``. Every shell call runs on ONE
  dedicated STA thread (COM initialised once, messages pumped); callers wait at most 3 s.
  With ``activate`` the resulting Explorer window – a new one, or an existing one that got a
  new tab or navigated – is detected and brought to the front.
* ``force_foreground`` never synthesises Alt or any other key (Left Alt+Shift switches the
  input language here); the only synthetic input ever sent is a zero-filled mouse event.
* Window/process queries, the per-user Run key, Edge discovery, DPI awareness, AppUserModelID.

All public functions are thread-safe and never send messages to other processes' windows.
"""

from __future__ import annotations

import collections
import ctypes
import logging
import ntpath
import os
import re
import threading
import time
import winreg
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

log = logging.getLogger(__name__)

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_shell32 = ctypes.WinDLL("shell32", use_last_error=True)
_ole32 = ctypes.WinDLL("ole32", use_last_error=True)

HANDLE = wintypes.HANDLE
LRESULT = ctypes.c_ssize_t
HRESULT = ctypes.c_long            # plain long: failures are checked explicitly, not raised


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


class _SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("fMask", wintypes.ULONG), ("hwnd", HANDLE),
                ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR),
                ("lpParameters", wintypes.LPCWSTR), ("lpDirectory", wintypes.LPCWSTR),
                ("nShow", ctypes.c_int), ("hInstApp", HANDLE), ("lpIDList", wintypes.LPVOID),
                ("lpClass", wintypes.LPCWSTR), ("hkeyClass", HANDLE), ("dwHotKey", wintypes.DWORD),
                ("hIconOrMonitor", HANDLE), ("hProcess", HANDLE)]


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]


def _declare(dll: ctypes.WinDLL, name: str, restype: Any, *argtypes: Any) -> Any:
    fn = getattr(dll, name)
    fn.restype = restype
    fn.argtypes = list(argtypes)
    return fn


def _declare_optional(dll: ctypes.WinDLL, name: str, restype: Any, *argtypes: Any) -> Any:
    try:
        return _declare(dll, name, restype, *argtypes)
    except AttributeError:
        return None


_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, HANDLE, wintypes.LPARAM)

_EnumWindows = _declare(_user32, "EnumWindows", wintypes.BOOL, _WNDENUMPROC, wintypes.LPARAM)
_GetClassNameW = _declare(_user32, "GetClassNameW", ctypes.c_int,
                          HANDLE, wintypes.LPWSTR, ctypes.c_int)
_GetWindowTextW = _declare(_user32, "GetWindowTextW", ctypes.c_int,
                           HANDLE, wintypes.LPWSTR, ctypes.c_int)
_GetWindowThreadProcessId = _declare(_user32, "GetWindowThreadProcessId", wintypes.DWORD,
                                     HANDLE, ctypes.POINTER(wintypes.DWORD))
_IsWindow = _declare(_user32, "IsWindow", wintypes.BOOL, HANDLE)
_IsWindowVisible = _declare(_user32, "IsWindowVisible", wintypes.BOOL, HANDLE)
_IsIconic = _declare(_user32, "IsIconic", wintypes.BOOL, HANDLE)
_IsHungAppWindow = _declare(_user32, "IsHungAppWindow", wintypes.BOOL, HANDLE)
_GetWindow = _declare(_user32, "GetWindow", HANDLE, HANDLE, wintypes.UINT)
_GetForegroundWindow = _declare(_user32, "GetForegroundWindow", HANDLE)
_SetForegroundWindow = _declare(_user32, "SetForegroundWindow", wintypes.BOOL, HANDLE)
_BringWindowToTop = _declare(_user32, "BringWindowToTop", wintypes.BOOL, HANDLE)
_ShowWindowAsync = _declare(_user32, "ShowWindowAsync", wintypes.BOOL, HANDLE, ctypes.c_int)
_AttachThreadInput = _declare(_user32, "AttachThreadInput", wintypes.BOOL,
                              wintypes.DWORD, wintypes.DWORD, wintypes.BOOL)
_SendInput = _declare(_user32, "SendInput", wintypes.UINT,
                      wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int)
_AllowSetForegroundWindow = _declare(_user32, "AllowSetForegroundWindow", wintypes.BOOL,
                                     wintypes.DWORD)
_MsgWaitForMultipleObjectsEx = _declare(_user32, "MsgWaitForMultipleObjectsEx", wintypes.DWORD,
                                        wintypes.DWORD, ctypes.POINTER(HANDLE), wintypes.DWORD,
                                        wintypes.DWORD, wintypes.DWORD)
_PeekMessageW = _declare(_user32, "PeekMessageW", wintypes.BOOL, ctypes.POINTER(wintypes.MSG),
                         HANDLE, wintypes.UINT, wintypes.UINT, wintypes.UINT)
_TranslateMessage = _declare(_user32, "TranslateMessage", wintypes.BOOL,
                             ctypes.POINTER(wintypes.MSG))
_DispatchMessageW = _declare(_user32, "DispatchMessageW", LRESULT, ctypes.POINTER(wintypes.MSG))
_SetProcessDpiAwarenessContext = _declare_optional(_user32, "SetProcessDpiAwarenessContext",
                                                   wintypes.BOOL, HANDLE)
_SetProcessDPIAware = _declare(_user32, "SetProcessDPIAware", wintypes.BOOL)

_OpenProcess = _declare(_kernel32, "OpenProcess", HANDLE,
                        wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_CloseHandle = _declare(_kernel32, "CloseHandle", wintypes.BOOL, HANDLE)
_QueryFullProcessImageNameW = _declare(_kernel32, "QueryFullProcessImageNameW", wintypes.BOOL,
                                       HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                       ctypes.POINTER(wintypes.DWORD))
_CreateToolhelp32Snapshot = _declare(_kernel32, "CreateToolhelp32Snapshot", HANDLE,
                                     wintypes.DWORD, wintypes.DWORD)
_Process32FirstW = _declare(_kernel32, "Process32FirstW", wintypes.BOOL,
                            HANDLE, ctypes.POINTER(_PROCESSENTRY32W))
_Process32NextW = _declare(_kernel32, "Process32NextW", wintypes.BOOL,
                           HANDLE, ctypes.POINTER(_PROCESSENTRY32W))
_GetProcessTimes = _declare(_kernel32, "GetProcessTimes", wintypes.BOOL, HANDLE,
                            ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
                            ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME))
_GetSystemTimeAsFileTime = _declare(_kernel32, "GetSystemTimeAsFileTime", None,
                                    ctypes.POINTER(wintypes.FILETIME))
_GetCurrentThreadId = _declare(_kernel32, "GetCurrentThreadId", wintypes.DWORD)
_CreateEventW = _declare(_kernel32, "CreateEventW", HANDLE,
                         wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR)
_SetEvent = _declare(_kernel32, "SetEvent", wintypes.BOOL, HANDLE)

_ShellExecuteExW = _declare(_shell32, "ShellExecuteExW", wintypes.BOOL,
                            ctypes.POINTER(_SHELLEXECUTEINFOW))
_SHParseDisplayName = _declare(_shell32, "SHParseDisplayName", HRESULT, wintypes.LPCWSTR,
                               wintypes.LPVOID, ctypes.POINTER(ctypes.c_void_p), wintypes.ULONG,
                               ctypes.POINTER(wintypes.ULONG))
_SHOpenFolderAndSelectItems = _declare(_shell32, "SHOpenFolderAndSelectItems", HRESULT,
                                       wintypes.LPVOID, wintypes.UINT, wintypes.LPVOID,
                                       wintypes.DWORD)
_SetCurrentProcessExplicitAppUserModelID = _declare(
    _shell32, "SetCurrentProcessExplicitAppUserModelID", HRESULT, wintypes.LPCWSTR)

_CoInitializeEx = _declare(_ole32, "CoInitializeEx", HRESULT, wintypes.LPVOID, wintypes.DWORD)
_CoUninitialize = _declare(_ole32, "CoUninitialize", None)
_CoTaskMemFree = _declare(_ole32, "CoTaskMemFree", None, wintypes.LPVOID)

SW_SHOWNORMAL, SW_SHOWNOACTIVATE, SW_SHOW, SW_RESTORE = 1, 4, 5, 9
GW_OWNER = 4
INPUT_MOUSE = 0
ASFW_ANY = 0xFFFFFFFF
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TH32CS_SNAPPROCESS = 0x2
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
COINIT_APARTMENTTHREADED, COINIT_DISABLE_OLE1DDE = 0x2, 0x4
SEE_MASK_CLASSNAME, SEE_MASK_NOASYNC, SEE_MASK_FLAG_NO_UI = 0x1, 0x100, 0x400
QS_ALLINPUT, MWMO_INPUTAVAILABLE, INFINITE = 0x04FF, 0x0004, 0xFFFFFFFF
WAIT_OBJECT_0 = 0
PM_REMOVE = 0x0001
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
ERROR_ACCESS_DENIED = 5
E_ACCESSDENIED = -2147024891       # 0x80070005 as a signed HRESULT

CHROME_WINDOW_CLASS = "Chrome_WidgetWin_1"
EXPLORER_WINDOW_CLASSES = frozenset({"CabinetWClass", "ExploreWClass"})


# --------------------------------------------------------------------------------------
# Windows and processes
# --------------------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class WindowInfo:
    hwnd: int
    cls: str
    title: str
    pid: int
    exe: str | None          # image name, e.g. "msedge.exe"
    visible: bool
    owner: int | None        # owner window (None for unowned top-level windows)


def _class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    return buf.value if _GetClassNameW(hwnd, buf, len(buf)) else ""


def _window_text(hwnd: int) -> str:
    # For other processes' windows GetWindowTextW reads the stored caption without sending a
    # message, so a hung window cannot block us (only this process's own windows are asked).
    buf = ctypes.create_unicode_buffer(1024)
    _GetWindowTextW(hwnd, buf, len(buf))
    return buf.value


def _window_pid(hwnd: int) -> int:
    pid = wintypes.DWORD()
    _GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _process_exe(pid: int) -> str | None:
    handle = _OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if not _QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return None
        return ntpath.basename(buf.value) or None
    finally:
        _CloseHandle(handle)


def window_info(hwnd: int | None) -> WindowInfo | None:
    """Snapshot of a top-level window, or None if ``hwnd`` is not a window (any more)."""
    if not hwnd or not _IsWindow(hwnd):
        return None
    pid = _window_pid(hwnd)
    return WindowInfo(hwnd=int(hwnd), cls=_class_name(hwnd), title=_window_text(hwnd), pid=pid,
                      exe=_process_exe(pid), visible=bool(_IsWindowVisible(hwnd)),
                      owner=_GetWindow(hwnd, GW_OWNER) or None)


def _enum_handles(classes: frozenset[str]) -> list[int]:
    """Top-level windows of the given classes, in Z order (topmost first)."""
    handles: list[int] = []

    def collect(hwnd: int, _lparam: int) -> bool:
        try:
            if hwnd and _class_name(hwnd) in classes:
                handles.append(hwnd)
        except Exception:
            log.exception("window enumeration callback failed")
        return True

    _EnumWindows(_WNDENUMPROC(collect), 0)
    return handles


def _enum_windows(classes: frozenset[str]) -> list[WindowInfo]:
    return [info for info in map(window_info, _enum_handles(classes)) if info is not None]


def is_app_window(info: WindowInfo, title: str, exe_name: str = "msedge.exe",
                  pid: int | None = None) -> bool:
    """Exact identification of our Edge app window (SPEC §10.2)."""
    return (info.cls == CHROME_WINDOW_CLASS and info.title == title and info.owner is None
            and info.exe is not None and info.exe.casefold() == exe_name.casefold()
            and (pid is None or info.pid == pid))


def find_app_window_info(title: str, exe_name: str = "msedge.exe", pid: int | None = None, *,
                         windows: Iterable[WindowInfo] | None = None) -> WindowInfo | None:
    """First (topmost) window passing ``is_app_window``; ``windows`` injects an enumeration."""
    if windows is None:     # cheap title check first; process details only for real candidates
        windows = (window_info(hwnd) for hwnd in _enum_handles(frozenset({CHROME_WINDOW_CLASS}))
                   if _window_text(hwnd) == title)
    return next((w for w in windows if w is not None and is_app_window(w, title, exe_name, pid)),
                None)


def find_app_window(title: str, exe_name: str = "msedge.exe", pid: int | None = None) -> int | None:
    info = find_app_window_info(title, exe_name, pid)
    return info.hwnd if info is not None else None


def foreground_window() -> int | None:
    return _GetForegroundWindow() or None


def foreground_process_name() -> str | None:
    """Image name of the foreground window's process, e.g. ``"Resolve.exe"``."""
    hwnd = _GetForegroundWindow()
    if not hwnd:
        return None
    pid = _window_pid(hwnd)
    name = _process_exe(pid)
    if name is None:
        name = next((exe for p, exe in _process_list() if p == pid), None)
    return name


def _process_list() -> list[tuple[int, str]]:
    snapshot = _CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == INVALID_HANDLE_VALUE:
        log.warning("CreateToolhelp32Snapshot failed: %s", ctypes.WinError(ctypes.get_last_error()))
        return []
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        result: list[tuple[int, str]] = []
        ok = _Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            result.append((entry.th32ProcessID, entry.szExeFile))
            ok = _Process32NextW(snapshot, ctypes.byref(entry))
        return result
    finally:
        _CloseHandle(snapshot)


def process_running(exe_name: str) -> bool:
    target = exe_name.casefold()
    return any(exe.casefold() == target for _pid, exe in _process_list())


def _filetime_value(ft: wintypes.FILETIME) -> int:
    return (ft.dwHighDateTime << 32) | ft.dwLowDateTime


def _process_start_filetime(pid: int) -> int | None:
    handle = _OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not _GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                ctypes.byref(kernel), ctypes.byref(user)):
            return None
        return _filetime_value(created)
    finally:
        _CloseHandle(handle)


def process_uptime(exe_name: str) -> float | None:
    """Seconds since the oldest process named ``exe_name`` started; None if none is running
    (or no such process lets us read its start time)."""
    target = exe_name.casefold()
    starts = [start for pid, exe in _process_list() if exe.casefold() == target
              if (start := _process_start_filetime(pid)) is not None]
    if not starts:
        return None
    now = wintypes.FILETIME()
    _GetSystemTimeAsFileTime(ctypes.byref(now))
    return max(0.0, (_filetime_value(now) - min(starts)) / 10_000_000)


# --------------------------------------------------------------------------------------
# Foreground handling
# --------------------------------------------------------------------------------------

def _send_zero_mouse_input() -> None:
    """One zero-filled mouse event: makes this process 'the last input source', which lifts
    the foreground lock for it. Moves nothing, presses nothing."""
    event = _INPUT(type=INPUT_MOUSE)
    if _SendInput(1, ctypes.byref(event), ctypes.sizeof(_INPUT)) != 1:
        log.debug("SendInput(zero mouse) failed: %s", ctypes.WinError(ctypes.get_last_error()))


def _unlock_foreground() -> None:
    """Let the process we are about to ask (Explorer, a viewer) take the foreground."""
    _send_zero_mouse_input()
    if not _AllowSetForegroundWindow(ASFW_ANY):
        log.debug("AllowSetForegroundWindow failed: %s", ctypes.WinError(ctypes.get_last_error()))


def _wait_foreground(hwnd: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while _GetForegroundWindow() != hwnd:
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)
    return True


def _set_foreground_attached(hwnd: int) -> None:
    foreground = _GetForegroundWindow()
    if not foreground or _IsHungAppWindow(foreground):
        return
    fg_thread = _GetWindowThreadProcessId(foreground, None)
    me = _GetCurrentThreadId()
    attached = bool(fg_thread and fg_thread != me and _AttachThreadInput(me, fg_thread, True))
    try:
        _BringWindowToTop(hwnd)
        _SetForegroundWindow(hwnd)
    finally:
        if attached:
            _AttachThreadInput(me, fg_thread, False)


def force_foreground(hwnd: int) -> bool:
    """Bring ``hwnd`` to the front; True when it is the foreground window afterwards."""
    if not hwnd or not _IsWindow(hwnd):
        return False
    _ShowWindowAsync(hwnd, SW_RESTORE if _IsIconic(hwnd) else SW_SHOW)
    _SetForegroundWindow(hwnd)
    if _wait_foreground(hwnd, 0.1):
        return True
    _send_zero_mouse_input()
    _SetForegroundWindow(hwnd)
    if _wait_foreground(hwnd, 0.1):
        return True
    _BringWindowToTop(hwnd)
    if _wait_foreground(hwnd, 0.05):
        return True
    _set_foreground_attached(hwnd)
    return _wait_foreground(hwnd, 0.2)


# --------------------------------------------------------------------------------------
# Explorer window detection (pure parts are unit-tested)
# --------------------------------------------------------------------------------------

_DRIVE_ROOT_RE = re.compile(r"^([A-Za-z]):\\?$")
_SHARE_ROOT_RE = re.compile(r"^\\\\([^\\]+)\\([^\\]+)$")
# Localised names Explorer appends to its title ("<folder> – Stifinder"), casefolded. Windows 10
# shows the bare folder name. An unknown language only means a window is not recognised; a
# generic "anything after a dash" rule would take the sibling "Klar Tand - Silkeborg" for
# "Klar Tand" (WIN-2) – with the house naming "Kunde - Projekt" that is common.
EXPLORER_APP_NAMES: frozenset[str] = frozenset(name.casefold() for name in (
    "Stifinder", "File Explorer", "Datei-Explorer", "Utforskaren", "Filutforsker", "Verkenner",
    "Explorateur de fichiers", "Explorador de archivos", "Esplora file", "Resurssienhallinta",
    "Eksplorator plików", "Explorador de Arquivos", "Explorador de Ficheiros",
    "Průzkumník souborů", "Windows Explorer"))
# The tab counter between the folder name and the app name, e.g. "og 1 fane mere",
# "og 3 flere faner" (read from explorerframe.dll.mui), "and 2 more tabs": a conjunction,
# the number, one or two words.
_TAB_CONJUNCTIONS = ("og", "and", "und", "och", "en", "et", "y", "e", "ja", "i", "a")
_TITLE_TAIL_RE = re.compile(
    r"(?:(?:\s+(?:" + "|".join(_TAB_CONJUNCTIONS) + r")\s+\d+\s+\S+(?:\s+\S+)?)?"
    r"\s+[-\u2013\u2014]\s+(?:"
    + "|".join(re.escape(name) for name in sorted(EXPLORER_APP_NAMES, key=len, reverse=True))
    + r"))?\s*$")


def clean_path(path: Any) -> str | None:
    """Absolute Windows path without ``\\\\?\\`` prefix or trailing separator (``C:\\`` keeps
    its one); None for anything that is not an absolute drive or UNC path."""
    if not isinstance(path, str):
        return None
    p = path.strip().replace("/", "\\")
    if p[:8].upper() == "\\\\?\\UNC\\":
        p = "\\\\" + p[8:]
    elif p.startswith("\\\\?\\"):
        p = p[4:]
    if re.fullmatch(r"[A-Za-z]:\\*", p):
        return p[0].upper() + ":\\"
    p = p.rstrip("\\")
    if re.match(r"[A-Za-z]:\\.", p) or re.match(r"\\\\[^\\]+\\[^\\]+", p):
        return p
    return None


def explorer_title_names(path: str) -> list[str]:
    """Names an Explorer window showing ``path`` may start its title with (casefolded)."""
    p = clean_path(path)
    if p is None or _DRIVE_ROOT_RE.match(p):
        return []
    share = _SHARE_ROOT_RE.match(p)
    if share:
        host, name = share.groups()
        return [f"{name} (\\\\{host})".casefold(), name.casefold()]
    return [p.casefold(), ntpath.basename(p).casefold()]


def explorer_title_matches(title: str, path: str) -> bool:
    """Does an Explorer title (``"<folder> – Stifinder"``, tabs, full-path mode) show ``path``?

    After the folder name only the end of the title, the Explorer app name and/or the tab
    counter may follow – never another name part ("Klar Tand - Silkeborg" is not "Klar Tand")."""
    text = title.strip().casefold()
    p = clean_path(path)
    if not text or p is None:
        return False
    drive = _DRIVE_ROOT_RE.match(p)
    if drive:       # "<label> (H:) – Stifinder"; the label is unknown without disk I/O
        return f"({drive.group(1).casefold()}:)" in text
    return any(text.startswith(name) and _TITLE_TAIL_RE.match(text, len(name))
               for name in explorer_title_names(p))


def pick_explorer_window(before: Mapping[int, str], now: Sequence[tuple[int, str]],
                         path: str) -> int | None:
    """After an open: a NEW window showing ``path``, else an existing one whose title CHANGED
    to it (reused window / new tab). ``now`` is in Z order, topmost first."""
    for hwnd, title in now:
        if hwnd not in before and explorer_title_matches(title, path):
            return hwnd
    for hwnd, title in now:
        if hwnd in before and before[hwnd] != title and explorer_title_matches(title, path):
            return hwnd
    return None


def _explorer_windows() -> list[tuple[int, str]]:
    return [(w.hwnd, w.title) for w in _enum_windows(EXPLORER_WINDOW_CLASSES)]


def explorer_window_for(path: str, *, windows: Sequence[tuple[int, str]] | None = None) -> int | None:
    """Topmost Explorer window currently showing ``path`` (``windows`` injects an enumeration)."""
    candidates = _explorer_windows() if windows is None else windows
    return next((hwnd for hwnd, title in candidates if explorer_title_matches(title, path)), None)


def _wait_for_explorer_window(before: Mapping[int, str], folder: str, timeout: float) -> int | None:
    deadline = time.monotonic() + timeout
    first_new_seen: float | None = None
    while True:
        now = _explorer_windows()
        hit = pick_explorer_window(before, now, folder)
        if hit is not None:
            return hit
        foreground = _GetForegroundWindow()
        if foreground and any(h == foreground and explorer_title_matches(t, folder) for h, t in now):
            return foreground               # Explorer re-activated a window already showing it
        new = [hwnd for hwnd, _title in now if hwnd not in before]
        t = time.monotonic()
        if new:
            first_new_seen = first_new_seen or t
            if t - first_new_seen >= 0.5:   # a new window whose title never matched (yet)
                return new[0]
        if t >= deadline:
            return new[0] if new else explorer_window_for(folder, windows=now)
        time.sleep(0.05)


# --------------------------------------------------------------------------------------
# Shell thread
# --------------------------------------------------------------------------------------

class _ShellRequest:
    __slots__ = ("fn", "done", "result", "error", "abandoned")

    def __init__(self, fn: Callable[[], Any]) -> None:
        self.fn = fn
        self.done = threading.Event()
        self.result: Any = None
        self.error: BaseException | None = None
        self.abandoned = False


class _ShellWorker(threading.Thread):
    """STA thread (COM initialised once) that runs shell calls and pumps messages."""

    def __init__(self, com: bool = True) -> None:
        super().__init__(name="ShellThread", daemon=True)
        self._com = com
        self._queue: collections.deque[_ShellRequest] = collections.deque()
        self._lock = threading.Lock()
        self._event = _CreateEventW(None, False, False, None)
        if not self._event:
            raise ctypes.WinError(ctypes.get_last_error())
        self._closed = False
        self._retired = False
        self.busy_since: float | None = None

    def submit(self, request: _ShellRequest) -> bool:
        with self._lock:
            if self._closed:
                return False
            self._queue.append(request)
            _SetEvent(self._event)
        return True

    def retire(self) -> None:
        """Finish the current call, fail the queued ones, exit (a stuck call is left behind)."""
        with self._lock:
            self._retired = True
            if not self._closed:
                _SetEvent(self._event)

    def run(self) -> None:
        initialized = False
        if self._com:
            hr = _CoInitializeEx(None, COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE)
            initialized = hr >= 0
            if not initialized:
                log.error("CoInitializeEx failed on the shell thread: 0x%08X", hr & 0xFFFFFFFF)
        handles = (HANDLE * 1)(self._event)
        msg = wintypes.MSG()
        try:
            while not self._retired:
                rc = _MsgWaitForMultipleObjectsEx(1, handles, INFINITE, QS_ALLINPUT,
                                                  MWMO_INPUTAVAILABLE)
                if rc == WAIT_OBJECT_0:
                    self._run_queued()
                elif rc == WAIT_OBJECT_0 + 1:
                    while _PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                        _TranslateMessage(ctypes.byref(msg))
                        _DispatchMessageW(ctypes.byref(msg))
                else:
                    log.error("MsgWaitForMultipleObjectsEx failed: %s",
                              ctypes.WinError(ctypes.get_last_error()))
                    time.sleep(0.1)
        finally:
            with self._lock:
                self._closed = True
                pending = list(self._queue)
                self._queue.clear()
                _CloseHandle(self._event)
            for request in pending:
                request.error = RuntimeError("shell thread retired")
                request.done.set()
            if initialized:
                _CoUninitialize()

    def _run_queued(self) -> None:
        while not self._retired:
            with self._lock:
                if not self._queue:
                    return
                request = self._queue.popleft()
            if request.abandoned:
                continue
            self.busy_since = time.monotonic()
            try:
                request.result = request.fn()
            except Exception as exc:
                request.error = exc
            finally:
                self.busy_since = None
                request.done.set()


_SHELL_TIMEOUT_S = 3.0
_SHELL_STUCK_S = 10.0
_shell_lock = threading.Lock()
_shell_worker: _ShellWorker | None = None


def _get_shell_worker() -> _ShellWorker:
    global _shell_worker
    with _shell_lock:
        worker = _shell_worker
        busy = worker.busy_since if worker is not None else None
        stuck = busy is not None and time.monotonic() - busy > _SHELL_STUCK_S
        if worker is None or not worker.is_alive() or stuck:
            if worker is not None and stuck:
                log.warning("shell thread stuck for > %.0f s – starting a new one", _SHELL_STUCK_S)
                worker.retire()
            worker = _ShellWorker()
            worker.start()
            _shell_worker = worker
        return worker


def _run_shell(fn: Callable[[], Any], worker: _ShellWorker, timeout: float) -> Any:
    request = _ShellRequest(fn)
    if not worker.submit(request):
        return None
    if not request.done.wait(timeout):
        request.abandoned = True       # never run it late (no Explorer popping up later)
        log.warning("shell call did not finish within %.1f s", timeout)
        return None
    if request.error is not None:
        log.warning("shell call failed: %r", request.error)
        return None
    return request.result


def _shell_call(fn: Callable[[], Any]) -> Any:
    """Run ``fn`` on the shell thread; its result, or None on error/timeout (≤ 3 s)."""
    try:
        worker = _get_shell_worker()
    except OSError as exc:
        log.error("could not start the shell thread: %s", exc)
        return None
    return _run_shell(fn, worker, _SHELL_TIMEOUT_S)


def _shell_execute(target: str, *, verb: str | None, folder: bool, show: int) -> bool:
    """ShellExecuteExW on the shell thread. ``folder`` forces the Folder class, so a folder
    request can never execute a file."""
    info = _SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = SEE_MASK_NOASYNC | ((SEE_MASK_CLASSNAME | SEE_MASK_FLAG_NO_UI) if folder else 0)
    info.lpVerb = verb
    info.lpFile = target
    info.lpClass = "Folder" if folder else None
    info.nShow = show
    if _ShellExecuteExW(ctypes.byref(info)):
        return True
    log.info("ShellExecuteExW failed: %s", ctypes.WinError(ctypes.get_last_error()))
    return False


def _shell_select(target: str) -> bool:
    """Explorer with ``target`` selected (SHOpenFolderAndSelectItems) on the shell thread."""
    pidl = ctypes.c_void_p()
    hr = _SHParseDisplayName(target, None, ctypes.byref(pidl), 0, None)
    if hr < 0 or not pidl.value:
        log.info("SHParseDisplayName failed: 0x%08X", hr & 0xFFFFFFFF)
        return False
    try:
        hr = _SHOpenFolderAndSelectItems(pidl, 0, None, 0)
        if hr < 0:
            log.info("SHOpenFolderAndSelectItems failed: 0x%08X", hr & 0xFFFFFFFF)
        return hr >= 0
    finally:
        _CoTaskMemFree(pidl)


def _open_in_explorer(action: Callable[[], bool], folder: str, activate: bool) -> bool:
    if activate:
        _unlock_foreground()
    before = dict(_explorer_windows())
    if not _shell_call(action):
        return False
    if activate:
        hwnd = _wait_for_explorer_window(before, folder, 2.0)
        if hwnd is None:
            log.info("could not identify the Explorer window for the opened folder")
        elif not force_foreground(hwnd):
            log.info("Explorer window %#x did not come to the front", hwnd)
    return True


# --------------------------------------------------------------------------------------
# Public open functions
# --------------------------------------------------------------------------------------

# Types opened with their default app (media, documents, project files). Everything else –
# exe, bat, cmd, ps1, vbs, js, lnk, msi, scr, com, hta, reg, html, macro documents … – is refused.
OPENABLE_EXTENSIONS: frozenset[str] = frozenset("""
    mxf mov mp4 m4v mts m2ts ts avi mkv webm wmv mpg mpeg m2v vob 3gp flv dv
    braw r3d crm ari insv insp lrv
    wav bwf mp3 aif aiff aifc m4a aac flac ogg oga opus wma ac3 caf
    jpg jpeg jpe png gif bmp tif tiff tga exr dpx cin hdr psd psb heic heif webp avif ico svg
    jxl jp2 arw cr2 cr3 crw dng nef raf orf rw2 srw pef gpr
    ai eps indd aep aepx prproj drp drt fcpxml edl ale otio xml blend c4d fbx obj abc usd usdz
    nk cube 3dl sesx ptx als rpp
    pdf txt rtf md doc docx odt xls xlsx ods csv ppt pptx odp srt vtt ass ssa stl scc
    ttf otf zip 7z rar
""".split())


def is_openable_file(path: str) -> bool:
    """Allow-list check used by ``open_file`` (also rejects alternate data streams)."""
    name = ntpath.basename(path)
    if not name or ":" in name:
        return False
    ext = ntpath.splitext(name)[1].lower().lstrip(".")
    return ext in OPENABLE_EXTENSIONS


def open_folder(path: str, activate: bool = True) -> bool:
    """Open ``path`` in Explorer; with ``activate`` its window comes to the front."""
    target = clean_path(path)
    if target is None:
        log.info("open_folder: not an absolute path: %r", path)
        return False
    show = SW_SHOWNORMAL if activate else SW_SHOWNOACTIVATE
    return _open_in_explorer(lambda: _shell_execute(target, verb="open", folder=True, show=show),
                             target, activate)


def reveal(path: str, activate: bool = True) -> bool:
    """Explorer on the parent folder with ``path`` selected (falls back to opening the parent)."""
    target = clean_path(path)
    if target is None:
        log.info("reveal: not an absolute path: %r", path)
        return False
    parent = ntpath.dirname(target) if not _DRIVE_ROOT_RE.match(target) else target
    show = SW_SHOWNORMAL if activate else SW_SHOWNOACTIVATE

    def action() -> bool:
        return _shell_select(target) or _shell_execute(parent, verb="open", folder=True, show=show)

    return _open_in_explorer(action, parent, activate)


def open_file(path: str) -> bool:
    """Open a file with its default app; refuses types outside ``OPENABLE_EXTENSIONS``."""
    target = clean_path(path)
    if target is None or not is_openable_file(target):
        log.info("open_file refused: %r", path)
        return False
    _unlock_foreground()
    return bool(_shell_call(lambda: _shell_execute(target, verb=None, folder=False,
                                                   show=SW_SHOWNORMAL)))


# --------------------------------------------------------------------------------------
# Run at login (HKCU Run key)
# --------------------------------------------------------------------------------------

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "Projektsøg"
_STARTUP_APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
_LEGACY_MARKERS = ("projektsøg.pyw", "projektsog.pyw", "-m projektsog")


def is_legacy_run_value(data: object) -> bool:
    """Does a Run value start this app (any name, any older launcher)?"""
    return isinstance(data, str) and any(m in data.casefold() for m in _LEGACY_MARKERS)


class RunValues(Protocol):
    def items(self) -> list[tuple[str, object]]: ...
    def set(self, name: str, data: str) -> None: ...
    def delete(self, name: str) -> None: ...


class _RegistryRunValues:
    def __init__(self, key: winreg.HKEYType) -> None:
        self._key = key

    def items(self) -> list[tuple[str, object]]:
        result: list[tuple[str, object]] = []
        index = 0
        while True:
            try:
                name, data, _kind = winreg.EnumValue(self._key, index)
            except OSError:
                return result
            result.append((name, data))
            index += 1

    def set(self, name: str, data: str) -> None:
        winreg.SetValueEx(self._key, name, 0, winreg.REG_SZ, data)

    def delete(self, name: str) -> None:
        try:
            winreg.DeleteValue(self._key, name)
        except FileNotFoundError:
            pass


def apply_run_at_login(values: RunValues, enabled: bool, command: str) -> None:
    """Write/remove our Run value; always drop other values that start this app too."""
    for name, data in values.items():
        if name.casefold() != RUN_VALUE.casefold() and is_legacy_run_value(data):
            values.delete(name)
    if enabled:
        values.set(RUN_VALUE, command)
    else:
        values.delete(RUN_VALUE)


def set_run_at_login(enabled: bool, command: str) -> None:
    """Enable/disable autostart. Raises ``ValueError`` (Danish) if the registry refuses."""
    if enabled and not (isinstance(command, str) and command.strip()):
        raise ValueError("Mangler kommandoen til ‘Start med Windows’")
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE) as key:
            apply_run_at_login(_RegistryRunValues(key), enabled, command)
    except OSError as exc:
        log.warning("could not update the Run key: %s", exc)
        raise ValueError("Kunne ikke ændre ‘Start med Windows’ i registreringsdatabasen") from exc
    _forget_startup_approval()


def _forget_startup_approval() -> None:
    """Drop Task Manager's enabled/disabled flag for our entry, so a fresh 'on' really runs."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _STARTUP_APPROVED_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, RUN_VALUE)
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.info("could not reset the StartupApproved flag: %s", exc)


def get_run_at_login() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_QUERY_VALUE) as key:
            winreg.QueryValueEx(key, RUN_VALUE)
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        log.warning("could not read the Run key: %s", exc)
        return False


# --------------------------------------------------------------------------------------
# Edge, DPI, AppUserModelID
# --------------------------------------------------------------------------------------

_EDGE_APP_PATHS = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe"
_EDGE_RELATIVE = os.path.join("Microsoft", "Edge", "Application", "msedge.exe")


def _edge_candidates() -> list[str]:
    candidates: list[str] = []
    for root, view in ((winreg.HKEY_CURRENT_USER, 0),
                       (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
                       (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY)):
        try:
            with winreg.OpenKey(root, _EDGE_APP_PATHS, 0, winreg.KEY_QUERY_VALUE | view) as key:
                value, _kind = winreg.QueryValueEx(key, "")
        except OSError:
            continue
        if isinstance(value, str) and value.strip():
            candidates.append(os.path.expandvars(value.strip().strip('"')))
    for variable in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        base = os.environ.get(variable)
        if base:
            candidates.append(os.path.join(base, _EDGE_RELATIVE))
    return candidates


def edge_path() -> str | None:
    """Full path of msedge.exe (App Paths registration first, then the usual folders)."""
    return next((c for c in _edge_candidates() if os.path.isfile(c)), None)


def set_dpi_awareness() -> None:
    """Per-monitor v2 DPI awareness, with fallbacks for older Windows. Idempotent."""
    if _SetProcessDpiAwarenessContext is not None:
        if _SetProcessDpiAwarenessContext(ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)):
            return
        if ctypes.get_last_error() == ERROR_ACCESS_DENIED:     # already set (manifest/earlier)
            return
    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        set_awareness = _declare(shcore, "SetProcessDpiAwareness", HRESULT, ctypes.c_int)
        hr = set_awareness(2)                                  # PROCESS_PER_MONITOR_DPI_AWARE
        if hr >= 0 or hr == E_ACCESSDENIED:
            return
    except (OSError, AttributeError):
        pass
    if not _SetProcessDPIAware():
        log.warning("could not make the process DPI aware")


def set_app_user_model_id(aumid: str) -> None:
    hr = _SetCurrentProcessExplicitAppUserModelID(aumid)
    if hr < 0:
        log.warning("SetCurrentProcessExplicitAppUserModelID failed: 0x%08X", hr & 0xFFFFFFFF)
