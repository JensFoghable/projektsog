"""Notification-area icon with menu and balloons (SPEC §10.4).

All Shell_NotifyIcon calls and all callbacks run on the tray's own thread, which owns a
hidden TOP-LEVEL window (message-only windows do not receive the ``TaskbarCreated``
broadcast that tells us to re-add the icon after Explorer restarts). ``notify()`` may be
called from any thread; it only queues the balloon and wakes the tray thread.
"""

from __future__ import annotations

import collections
import ctypes
import logging
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from . import APP_NAME

log = logging.getLogger(__name__)

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_shell32 = ctypes.WinDLL("shell32", use_last_error=True)

HANDLE = wintypes.HANDLE
LRESULT = ctypes.c_ssize_t
_WNDPROC = ctypes.WINFUNCTYPE(LRESULT, HANDLE, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", HANDLE), ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT),
                ("hIcon", HANDLE), ("szTip", wintypes.WCHAR * 128),
                ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
                ("szInfo", wintypes.WCHAR * 256), ("uVersion", wintypes.UINT),
                ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD),
                ("guidItem", _GUID), ("hBalloonIcon", HANDLE)]


class _WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT), ("lpfnWndProc", _WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", HANDLE), ("hIcon", HANDLE), ("hCursor", HANDLE),
                ("hbrBackground", HANDLE), ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", HANDLE)]


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


_Shell_NotifyIconW = _declare(_shell32, "Shell_NotifyIconW", wintypes.BOOL,
                              wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW))
_RegisterClassExW = _declare(_user32, "RegisterClassExW", wintypes.ATOM,
                             ctypes.POINTER(_WNDCLASSEXW))
_CreateWindowExW = _declare(_user32, "CreateWindowExW", HANDLE,
                            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                            HANDLE, HANDLE, HANDLE, wintypes.LPVOID)
_DestroyWindow = _declare(_user32, "DestroyWindow", wintypes.BOOL, HANDLE)
_DefWindowProcW = _declare(_user32, "DefWindowProcW", LRESULT,
                           HANDLE, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
_GetMessageW = _declare(_user32, "GetMessageW", wintypes.BOOL,
                        ctypes.POINTER(wintypes.MSG), HANDLE, wintypes.UINT, wintypes.UINT)
_TranslateMessage = _declare(_user32, "TranslateMessage", wintypes.BOOL,
                             ctypes.POINTER(wintypes.MSG))
_DispatchMessageW = _declare(_user32, "DispatchMessageW", LRESULT, ctypes.POINTER(wintypes.MSG))
_PostMessageW = _declare(_user32, "PostMessageW", wintypes.BOOL,
                         HANDLE, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
_PostQuitMessage = _declare(_user32, "PostQuitMessage", None, ctypes.c_int)
_RegisterWindowMessageW = _declare(_user32, "RegisterWindowMessageW", wintypes.UINT,
                                   wintypes.LPCWSTR)
_SetTimer = _declare(_user32, "SetTimer", ctypes.c_size_t,
                     HANDLE, ctypes.c_size_t, wintypes.UINT, wintypes.LPVOID)
_KillTimer = _declare(_user32, "KillTimer", wintypes.BOOL, HANDLE, ctypes.c_size_t)
_LoadImageW = _declare(_user32, "LoadImageW", HANDLE, HANDLE, wintypes.LPCWSTR, wintypes.UINT,
                       ctypes.c_int, ctypes.c_int, wintypes.UINT)
_LoadIconW = _declare(_user32, "LoadIconW", HANDLE, HANDLE, wintypes.LPVOID)
_DestroyIcon = _declare(_user32, "DestroyIcon", wintypes.BOOL, HANDLE)
_CreatePopupMenu = _declare(_user32, "CreatePopupMenu", HANDLE)
_AppendMenuW = _declare(_user32, "AppendMenuW", wintypes.BOOL,
                        HANDLE, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR)
_CheckMenuRadioItem = _declare(_user32, "CheckMenuRadioItem", wintypes.BOOL, HANDLE,
                               wintypes.UINT, wintypes.UINT, wintypes.UINT, wintypes.UINT)
_SetMenuDefaultItem = _declare(_user32, "SetMenuDefaultItem", wintypes.BOOL,
                               HANDLE, wintypes.UINT, wintypes.UINT)
_TrackPopupMenuEx = _declare(_user32, "TrackPopupMenuEx", wintypes.BOOL, HANDLE, wintypes.UINT,
                             ctypes.c_int, ctypes.c_int, HANDLE, wintypes.LPVOID)
_DestroyMenu = _declare(_user32, "DestroyMenu", wintypes.BOOL, HANDLE)
_SetForegroundWindow = _declare(_user32, "SetForegroundWindow", wintypes.BOOL, HANDLE)
_GetCursorPos = _declare(_user32, "GetCursorPos", wintypes.BOOL, ctypes.POINTER(wintypes.POINT))
_GetSystemMetrics = _declare(_user32, "GetSystemMetrics", ctypes.c_int, ctypes.c_int)
_GetSystemMetricsForDpi = _declare_optional(_user32, "GetSystemMetricsForDpi", ctypes.c_int,
                                            ctypes.c_int, wintypes.UINT)
_GetDpiForWindow = _declare_optional(_user32, "GetDpiForWindow", wintypes.UINT, HANDLE)
_SetThreadDpiAwarenessContext = _declare_optional(_user32, "SetThreadDpiAwarenessContext",
                                                  HANDLE, HANDLE)
_GetModuleHandleW = _declare(_kernel32, "GetModuleHandleW", HANDLE, wintypes.LPCWSTR)

NIM_ADD, NIM_MODIFY, NIM_DELETE, NIM_SETVERSION = 0, 1, 2, 4
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO, NIF_SHOWTIP = 0x1, 0x2, 0x4, 0x10, 0x80
NIIF_INFO, NIIF_WARNING, NIIF_ERROR = 0x1, 0x2, 0x3
NIIF_NOSOUND, NIIF_RESPECT_QUIET_TIME = 0x10, 0x80
NOTIFYICON_VERSION_4 = 4
NIN_SELECT, NIN_KEYSELECT, NIN_BALLOONUSERCLICK = 0x400, 0x401, 0x405
WM_DESTROY, WM_CLOSE, WM_NULL = 0x0002, 0x0010, 0x0000
WM_CONTEXTMENU, WM_TIMER, WM_DISPLAYCHANGE, WM_DPICHANGED = 0x007B, 0x0113, 0x007E, 0x02E0
WM_LBUTTONUP, WM_RBUTTONUP = 0x0202, 0x0205
WM_APP = 0x8000
WM_TRAY_CALLBACK = WM_APP + 1
WM_TRAY_NOTIFY = WM_APP + 2
IMAGE_ICON, LR_LOADFROMFILE = 1, 0x10
IDI_APPLICATION = 32512
SM_CXSMICON, SM_MENUDROPALIGNMENT = 49, 40
MF_STRING, MF_CHECKED, MF_POPUP, MF_SEPARATOR, MF_BYCOMMAND = 0x0, 0x8, 0x10, 0x800, 0x0
TPM_RIGHTALIGN, TPM_BOTTOMALIGN, TPM_RIGHTBUTTON = 0x8, 0x20, 0x2
TPM_NONOTIFY, TPM_RETURNCMD = 0x80, 0x100
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
TIMER_ADD_RETRY = 1
ICON_ID = 1

ID_SHOW, ID_SETTINGS, ID_SCAN_ALL = 1001, 1002, 1003
ID_FOLLOW_OFF, ID_FOLLOW_NOTIFY, ID_FOLLOW_OPEN = 1011, 1012, 1013
ID_AUTOSTART, ID_EXIT = 1021, 1099
FOLLOW_ITEMS: tuple[tuple[int, str, str], ...] = (
    (ID_FOLLOW_OFF, "Fra", "off"),
    (ID_FOLLOW_NOTIFY, "Vis besked", "notify"),
    (ID_FOLLOW_OPEN, "Åbn mappe automatisk", "open"),
)
_LEVEL_FLAGS = {"info": NIIF_INFO, "warn": NIIF_WARNING, "warning": NIIF_WARNING,
                "error": NIIF_ERROR}
_DEFAULT_MENU_STATE = {"hotkey_label": "", "follow": "notify", "autostart": False}


# --------------------------------------------------------------------------------------
# Pure helpers (unit-tested)
# --------------------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class MenuItem:
    id: int = 0
    text: str = ""
    checked: bool = False
    radio: bool = False
    default: bool = False
    separator: bool = False
    children: tuple["MenuItem", ...] = ()


def build_menu(state: Mapping[str, Any]) -> list[MenuItem]:
    """The tray menu for ``menu_state()`` (SPEC §10.4)."""
    label = str(state.get("hotkey_label") or "")
    follow = state.get("follow")
    return [
        MenuItem(ID_SHOW, f"Åbn {APP_NAME}" + (f"\t{label}" if label else ""), default=True),
        MenuItem(ID_SETTINGS, "Indstillinger …"),
        MenuItem(ID_SCAN_ALL, "Scan alle nu"),
        MenuItem(text="DaVinci Resolve", children=tuple(
            MenuItem(item_id, text, checked=follow == value, radio=True)
            for item_id, text, value in FOLLOW_ITEMS)),
        MenuItem(ID_AUTOSTART, "Start med Windows", checked=bool(state.get("autostart"))),
        MenuItem(separator=True),
        MenuItem(ID_EXIT, "Afslut"),
    ]


def fit_utf16(text: str, capacity: int) -> str:
    """Trim ``text`` to fit a WCHAR[capacity] buffer (incl. NUL), ending in '…' if trimmed."""
    text = str(text).replace("\0", " ")
    if len(text.encode("utf-16-le")) // 2 < capacity:
        return text
    budget = capacity - 2                  # room for "…" and the terminating NUL
    out: list[str] = []
    for ch in text:
        units = 2 if ord(ch) > 0xFFFF else 1
        if budget < units:
            break
        out.append(ch)
        budget -= units
    return "".join(out) + "…"


# --------------------------------------------------------------------------------------
# Window class shared by all tray instances
# --------------------------------------------------------------------------------------

_CLASS_NAME = "Projektsog.TrayWindow"
_instances: dict[int, "TrayIcon"] = {}
_class_lock = threading.Lock()
_class_registered = False


def _dispatch_message(hwnd: int, msg: int, wparam: int, lparam: int) -> int:
    instance = _instances.get(hwnd)
    if instance is not None:
        try:
            result = instance._on_message(hwnd, msg, wparam, lparam)
        except Exception:
            log.exception("tray message handling failed")
            result = None
        if result is not None:
            return result
    return _DefWindowProcW(hwnd, msg, wparam, lparam)


_WNDPROC_REF = _WNDPROC(_dispatch_message)      # referenced for the life of the process


def _ensure_window_class() -> bool:
    global _class_registered
    with _class_lock:
        if _class_registered:
            return True
        wc = _WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(_WNDCLASSEXW)
        wc.lpfnWndProc = _WNDPROC_REF
        wc.hInstance = _GetModuleHandleW(None)
        wc.lpszClassName = _CLASS_NAME
        if not _RegisterClassExW(ctypes.byref(wc)):
            log.error("RegisterClassExW failed: %s", ctypes.WinError(ctypes.get_last_error()))
            return False
        _class_registered = True
        return True


# --------------------------------------------------------------------------------------
# TrayIcon
# --------------------------------------------------------------------------------------

class TrayIcon:
    """The app's notification-area icon.

    Callbacks run on the tray thread and must return quickly (< 50 ms); ``on_exit`` should
    only signal the app's shutdown – the main thread then calls ``stop()``.
    """

    RETRY_INTERVAL_MS = 2000
    RETRY_FOR_S = 120.0
    START_TIMEOUT_S = 5.0
    STOP_TIMEOUT_S = 3.0

    def __init__(self, icon_path: str, tooltip: str, *, on_show: Callable[[], None],
                 on_settings: Callable[[], None], on_scan_all: Callable[[], None],
                 on_set_follow: Callable[[str], None], on_set_autostart: Callable[[bool], None],
                 on_exit: Callable[[], None], menu_state: Callable[[], dict]) -> None:
        self.icon_path = icon_path
        self.tooltip = tooltip
        self._on_show = on_show
        self._on_settings = on_settings
        self._on_scan_all = on_scan_all
        self._on_set_follow = on_set_follow
        self._on_set_autostart = on_set_autostart
        self._on_exit = on_exit
        self._menu_state = menu_state
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._hwnd: int | None = None
        self._added = False
        self._v4 = False                     # NOTIFYICON_VERSION_4 events
        self._icon: int | None = None
        self._icon_shared = False
        self._icon_size = 0
        self._retry_until = 0.0
        self._taskbar_created = 0
        self._pending: collections.deque[tuple[str, str, str]] = collections.deque(maxlen=5)

    # -- public API ----------------------------------------------------------------------------
    def start(self) -> bool:
        """Start the tray thread; True when the icon is in the notification area. If the
        shell is not ready yet, adding is retried every 2 s for 2 min (and False returned)."""
        with self._lock:
            if self._thread is not None:
                return self._added
            self._ready.clear()
            self._thread = threading.Thread(target=self._run, name="TrayIcon", daemon=True)
            self._thread.start()
        self._ready.wait(self.START_TIMEOUT_S)
        return self._added

    def stop(self) -> None:
        """Remove the icon and end the tray thread. Idempotent; not from the tray thread."""
        with self._lock:
            thread, self._thread = self._thread, None
        if thread is None:
            return
        self._ready.wait(self.START_TIMEOUT_S)
        hwnd = self._hwnd
        if hwnd:
            _PostMessageW(hwnd, WM_CLOSE, 0, 0)
        if thread is threading.current_thread():
            log.warning("TrayIcon.stop() called on the tray thread – not waiting for it")
            return
        thread.join(self.STOP_TIMEOUT_S)
        if thread.is_alive():
            log.warning("the tray thread did not stop in time")

    def notify(self, title: str, text: str, level: str = "info") -> None:
        """Show a silent balloon/toast (respects quiet time). Any thread."""
        with self._lock:
            self._pending.append((title, text, level))
            hwnd = self._hwnd
        if hwnd:
            _PostMessageW(hwnd, WM_TRAY_NOTIFY, 0, 0)

    # -- tray thread ---------------------------------------------------------------------------
    def _run(self) -> None:
        hwnd = None
        try:
            if _SetThreadDpiAwarenessContext is not None:
                _SetThreadDpiAwarenessContext(
                    ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2))
            if not _ensure_window_class():
                return
            hwnd = _CreateWindowExW(0, _CLASS_NAME, APP_NAME, 0, 0, 0, 0, 0,
                                    None, None, _GetModuleHandleW(None), None)
            if not hwnd:
                log.error("could not create the tray window: %s",
                          ctypes.WinError(ctypes.get_last_error()))
                return
            _instances[hwnd] = self
            self._taskbar_created = _RegisterWindowMessageW("TaskbarCreated")
            with self._lock:
                self._hwnd = hwnd
            self._load_icon()
            self._added = self._add_icon()
            if self._added:
                self._flush_notifications()
            else:
                self._retry_until = time.monotonic() + self.RETRY_FOR_S
                _SetTimer(hwnd, TIMER_ADD_RETRY, self.RETRY_INTERVAL_MS, None)
        except Exception:
            log.exception("tray start-up failed")
        finally:
            self._ready.set()
        if not hwnd:
            return
        try:
            self._message_loop()
        finally:
            with self._lock:
                self._hwnd = None
            _instances.pop(hwnd, None)
            self._release_icon()

    @staticmethod
    def _message_loop() -> None:
        msg = wintypes.MSG()
        while True:
            result = _GetMessageW(ctypes.byref(msg), None, 0, 0)
            if result == 0:
                return
            if result == -1:
                log.error("GetMessageW failed: %s", ctypes.WinError(ctypes.get_last_error()))
                return
            _TranslateMessage(ctypes.byref(msg))
            _DispatchMessageW(ctypes.byref(msg))

    def _on_message(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int | None:
        if msg == WM_TRAY_CALLBACK:
            event = lparam & 0xFFFF
            # Version 4 reports clicks as NIN_SELECT/WM_CONTEXTMENU with the anchor point in
            # wparam; if the shell refused version 4, the legacy button messages arrive.
            select = (NIN_SELECT, NIN_KEYSELECT) if self._v4 else (WM_LBUTTONUP,)
            if event in select or event == NIN_BALLOONUSERCLICK:
                self._call(self._on_show)
            elif self._v4 and event == WM_CONTEXTMENU:
                self._show_menu(hwnd, _signed16(wparam & 0xFFFF), _signed16((wparam >> 16) & 0xFFFF))
            elif not self._v4 and event == WM_RBUTTONUP:
                self._show_menu(hwnd, 0, 0)
            return 0
        if msg == WM_TRAY_NOTIFY:
            self._flush_notifications()
            return 0
        if self._taskbar_created and msg == self._taskbar_created:
            self._load_icon()                 # the scale factor may have changed as well
            self._added = self._add_icon()
            if self._added:
                self._flush_notifications()
            return 0
        if msg == WM_TIMER and wparam == TIMER_ADD_RETRY:
            self._retry_add(hwnd)
            return 0
        if msg in (WM_DPICHANGED, WM_DISPLAYCHANGE):
            self._refresh_icon()
            return None
        if msg == WM_CLOSE:
            self._delete_icon()
            _DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            _PostQuitMessage(0)
            return 0
        return None

    # -- icon ----------------------------------------------------------------------------------
    def _wanted_icon_size(self) -> int:
        if _GetSystemMetricsForDpi is not None and _GetDpiForWindow is not None and self._hwnd:
            dpi = _GetDpiForWindow(self._hwnd) or 96
            return _GetSystemMetricsForDpi(SM_CXSMICON, dpi) or 16
        return _GetSystemMetrics(SM_CXSMICON) or 16

    def _load_icon(self) -> None:
        size = self._wanted_icon_size()
        icon = _LoadImageW(None, self.icon_path, IMAGE_ICON, size, size, LR_LOADFROMFILE)
        shared = False
        if not icon:
            log.warning("could not load the tray icon %s: %s", self.icon_path,
                        ctypes.WinError(ctypes.get_last_error()))
            icon = _LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION))
            shared = True
        self._release_icon()
        self._icon, self._icon_shared, self._icon_size = icon, shared, size

    def _release_icon(self) -> None:
        if self._icon and not self._icon_shared:
            _DestroyIcon(self._icon)
        self._icon = None

    def _refresh_icon(self) -> None:
        if self._wanted_icon_size() == self._icon_size:
            return
        self._load_icon()
        if self._added:
            data = self._icon_data(NIF_ICON)
            _Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(data))

    def _icon_data(self, flags: int) -> NOTIFYICONDATAW:
        data = NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        data.hWnd = self._hwnd
        data.uID = ICON_ID
        data.uFlags = flags
        data.uCallbackMessage = WM_TRAY_CALLBACK
        data.hIcon = self._icon
        data.szTip = fit_utf16(self.tooltip, 128)
        return data

    def _add_icon(self) -> bool:
        data = self._icon_data(NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_SHOWTIP)
        if not _Shell_NotifyIconW(NIM_ADD, ctypes.byref(data)):
            # After an Explorer restart the old icon may still be registered: replace it.
            _Shell_NotifyIconW(NIM_DELETE, ctypes.byref(data))
            if not _Shell_NotifyIconW(NIM_ADD, ctypes.byref(data)):
                log.info("Shell_NotifyIconW(NIM_ADD) failed (the taskbar may not be ready)")
                return False
        data.uVersion = NOTIFYICON_VERSION_4
        self._v4 = bool(_Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(data)))
        if not self._v4:
            log.warning("Shell_NotifyIconW(NIM_SETVERSION) failed – using legacy click messages")
        return True

    def _retry_add(self, hwnd: int) -> None:
        if not self._added:
            self._added = self._add_icon()
        if self._added or time.monotonic() >= self._retry_until:
            _KillTimer(hwnd, TIMER_ADD_RETRY)
            if self._added:
                self._flush_notifications()
            else:
                log.error("the tray icon could not be added within %.0f s", self.RETRY_FOR_S)

    def _delete_icon(self) -> None:
        if self._added:
            data = self._icon_data(0)
            _Shell_NotifyIconW(NIM_DELETE, ctypes.byref(data))
            self._added = False

    # -- balloons ------------------------------------------------------------------------------
    def _flush_notifications(self) -> None:
        if not self._added:
            return                          # shown once the icon has been added
        with self._lock:
            pending = list(self._pending)
            self._pending.clear()
        for title, text, level in pending:
            data = self._icon_data(NIF_INFO)
            data.szInfoTitle = fit_utf16(title, 64)
            data.szInfo = fit_utf16(text, 256)
            data.dwInfoFlags = (_LEVEL_FLAGS.get(level, NIIF_INFO) | NIIF_NOSOUND
                                | NIIF_RESPECT_QUIET_TIME)
            if not _Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(data)):
                log.warning("could not show a notification")

    # -- menu ----------------------------------------------------------------------------------
    def _show_menu(self, hwnd: int, x: int, y: int) -> None:
        try:
            state = {**_DEFAULT_MENU_STATE, **(self._menu_state() or {})}
        except Exception:
            log.exception("menu_state() failed")
            state = dict(_DEFAULT_MENU_STATE)
        if x == 0 and y == 0:                # legacy messages carry no anchor point
            point = wintypes.POINT()
            _GetCursorPos(ctypes.byref(point))
            x, y = point.x, point.y
        menu = _build_native_menu(build_menu(state))
        if not menu:
            return
        try:
            _SetForegroundWindow(hwnd)      # required, or the menu will not close on click-away
            align = TPM_RIGHTALIGN if _GetSystemMetrics(SM_MENUDROPALIGNMENT) else 0
            command = _TrackPopupMenuEx(menu, align | TPM_BOTTOMALIGN | TPM_RIGHTBUTTON
                                        | TPM_NONOTIFY | TPM_RETURNCMD, x, y, hwnd, None)
            _PostMessageW(hwnd, WM_NULL, 0, 0)
        finally:
            _DestroyMenu(menu)
        if command:
            self._dispatch(command, state)

    def _dispatch(self, command: int, state: Mapping[str, Any]) -> None:
        if command == ID_SHOW:
            self._call(self._on_show)
        elif command == ID_SETTINGS:
            self._call(self._on_settings)
        elif command == ID_SCAN_ALL:
            self._call(self._on_scan_all)
        elif command == ID_AUTOSTART:
            self._call(self._on_set_autostart, not state.get("autostart"))
        elif command == ID_EXIT:
            self._call(self._on_exit)
        else:
            follow = next((value for item_id, _text, value in FOLLOW_ITEMS if item_id == command),
                          None)
            if follow is not None:
                self._call(self._on_set_follow, follow)

    @staticmethod
    def _call(callback: Callable[..., Any], *args: Any) -> None:
        started = time.monotonic()
        try:
            callback(*args)
        except Exception:
            log.exception("tray callback %s failed", getattr(callback, "__name__", callback))
        elapsed = time.monotonic() - started
        if elapsed > 0.05:
            log.warning("tray callback %s took %.0f ms (limit 50 ms)",
                        getattr(callback, "__name__", callback), elapsed * 1000)


def _signed16(value: int) -> int:
    return value - 0x10000 if value & 0x8000 else value


def _build_native_menu(items: list[MenuItem]) -> int | None:
    menu = _CreatePopupMenu()
    if not menu:
        return None
    for item in items:
        if item.separator:
            _AppendMenuW(menu, MF_SEPARATOR, 0, None)
        elif item.children:
            submenu = _build_native_menu(list(item.children))
            if submenu:
                _AppendMenuW(menu, MF_POPUP, submenu, item.text)   # owned by the parent menu
        else:
            _AppendMenuW(menu, MF_STRING | (MF_CHECKED if item.checked and not item.radio else 0),
                         item.id, item.text)
        if item.default:
            _SetMenuDefaultItem(menu, item.id, False)
    radios = [i for i in items if i.radio]
    checked = next((i for i in radios if i.checked), None)
    if checked is not None:
        _CheckMenuRadioItem(menu, min(i.id for i in radios), max(i.id for i in radios),
                            checked.id, MF_BYCOMMAND)
    return menu
