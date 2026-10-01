"""Headless Microsoft Edge driven over the DevTools protocol – stdlib only, for UI checks.

Edge runs with ``--headless=new`` and a throwaway profile, so no window ever appears and the
user's own browser profile is untouched. ``navigator.clipboard`` and ``execCommand('copy')``
are replaced by recorders in every page (``window.__copied``), so checks never touch the real
clipboard either.

Every Edge process (browser, renderers, GPU/utility processes, crashpad handler) runs inside a
Windows job object that kills them all when it is closed: :meth:`Edge.close` ends the whole
tree, and so does Windows itself if the Python process dies without closing (a test run killed
by a timeout used to leave a headless Edge behind, since nothing tells it that its DevTools
client is gone). Edge is started suspended, so no child process can start before the job is in
place. Background mode, crash reporting and component updates are switched off; this Edge build
still starts its crashpad handler (msedge_elf does so before any switch is read – measured), but
the handler is in the job like every other process.

Why DevTools instead of ``msedge --screenshot``: with ``--virtual-time-budget`` this Edge build
(154) ignores the given URL and renders the online new-tab page, and without it the shot is
taken before the page's data has arrived.

    python -m tests._ui_browser --out <dir>     # screenshots of the main UI states (mock server)
"""

from __future__ import annotations

import argparse
import atexit
import base64
import ctypes
import json
import os
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from ctypes import wintypes
from typing import Any

EDGE_CANDIDATES = (
    os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), r"Microsoft\Edge\Application\msedge.exe"),
    os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), r"Microsoft\Edge\Application\msedge.exe"),
)
CREATE_NO_WINDOW = 0x08000000
CREATE_SUSPENDED = 0x00000004
# Headless, throwaway profile, and nothing that could outlive the browser or phone home.
EDGE_FLAGS = (
    "--headless=new", "--disable-gpu", "--remote-debugging-port=0", "--no-first-run",
    "--no-default-browser-check", "--disable-background-networking", "--disable-component-update",
    "--disable-sync", "--disable-extensions", "--disable-default-apps", "--mute-audio",
    "--hide-scrollbars", "--lang=da-DK",
    "--disable-background-mode", "--disable-breakpad",
    "--disable-crash-reporter", "--disable-component-extensions-with-background-pages",
    "--no-service-autorun", "--metrics-recording-only", "--disable-domain-reliability",
)

# Replaces the clipboard in every document before its scripts run.
CLIPBOARD_STUB = """
(() => {
  window.__copied = [];
  const record = (text) => { window.__copied.push(String(text)); return Promise.resolve(); };
  try {
    Object.defineProperty(navigator, 'clipboard', { configurable: true,
      value: { writeText: record, readText: () => Promise.resolve(window.__copied.at(-1) || '') } });
  } catch (e) { /* keep going: execCommand is stubbed as well */ }
  const exec = document.execCommand.bind(document);
  document.execCommand = (cmd, ...rest) => {
    if (String(cmd).toLowerCase() !== 'copy') return exec(cmd, ...rest);
    const field = document.activeElement;  // text selected in a field is not in getSelection()
    const inField = field && typeof field.selectionStart === 'number' && typeof field.value === 'string';
    record(inField ? field.value.slice(field.selectionStart, field.selectionEnd) : String(window.getSelection()));
    return true;
  };
})();
"""

# DevTools key definitions: key -> (code, windowsVirtualKeyCode)
SPECIAL_KEYS = {
    "Enter": ("Enter", 13), "Escape": ("Escape", 27), "Tab": ("Tab", 9), "Backspace": ("Backspace", 8),
    "ArrowDown": ("ArrowDown", 40), "ArrowUp": ("ArrowUp", 38), "ArrowLeft": ("ArrowLeft", 37),
    "ArrowRight": ("ArrowRight", 39), "PageDown": ("PageDown", 34), "PageUp": ("PageUp", 33),
    "Home": ("Home", 36), "End": ("End", 35), ",": ("Comma", 188),
}


def find_edge() -> str | None:
    for path in EDGE_CANDIDATES:
        if os.path.isfile(path):
            return path
    return None


class CDPError(RuntimeError):
    pass


# ------------------------------------------------------------------------------------------
# Job object: one per Edge instance, kills the whole process tree when closed
# ------------------------------------------------------------------------------------------

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOB_BASIC_ACCOUNTING = 1          # JobObjectBasicAccountingInformation
_JOB_PROCESS_ID_LIST = 3           # JobObjectBasicProcessIdList
_JOB_EXTENDED_LIMITS = 9           # JobObjectExtendedLimitInformation
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_THREAD_SUSPEND_RESUME = 0x0002
_TH32CS_SNAPPROCESS = 0x00000002
_TH32CS_SNAPTHREAD = 0x00000004
_STILL_ACTIVE = 259
_INVALID_HANDLE = ctypes.c_void_p(-1).value


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _BasicLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
                ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER), ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD), ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimits), ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


class _BasicAccounting(ctypes.Structure):
    _fields_ = [("TotalUserTime", wintypes.LARGE_INTEGER), ("TotalKernelTime", wintypes.LARGE_INTEGER),
                ("ThisPeriodTotalUserTime", wintypes.LARGE_INTEGER),
                ("ThisPeriodTotalKernelTime", wintypes.LARGE_INTEGER),
                ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD)]


class _ThreadEntry(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ThreadID", wintypes.DWORD), ("th32OwnerProcessID", wintypes.DWORD),
                ("tpBasePri", wintypes.LONG), ("tpDeltaPri", wintypes.LONG), ("dwFlags", wintypes.DWORD)]


class _ProcessEntry(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]


def _kernel32() -> Any:
    """kernel32 with argtypes/restype for every function used here (own WinDLL: SPEC §1 rule 8)."""
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    HANDLE, BOOL, DWORD = wintypes.HANDLE, wintypes.BOOL, wintypes.DWORD
    signatures = {
        "CreateJobObjectW": ([wintypes.LPVOID, wintypes.LPCWSTR], HANDLE),
        "SetInformationJobObject": ([HANDLE, ctypes.c_int, wintypes.LPVOID, DWORD], BOOL),
        "QueryInformationJobObject": ([HANDLE, ctypes.c_int, wintypes.LPVOID, DWORD,
                                       ctypes.POINTER(DWORD)], BOOL),
        "AssignProcessToJobObject": ([HANDLE, HANDLE], BOOL),
        "TerminateJobObject": ([HANDLE, wintypes.UINT], BOOL),
        "OpenProcess": ([DWORD, BOOL, DWORD], HANDLE),
        "GetExitCodeProcess": ([HANDLE, ctypes.POINTER(DWORD)], BOOL),
        "OpenThread": ([DWORD, BOOL, DWORD], HANDLE),
        "ResumeThread": ([HANDLE], DWORD),
        "CreateToolhelp32Snapshot": ([DWORD, DWORD], HANDLE),
        "Thread32First": ([HANDLE, ctypes.POINTER(_ThreadEntry)], BOOL),
        "Thread32Next": ([HANDLE, ctypes.POINTER(_ThreadEntry)], BOOL),
        "Process32FirstW": ([HANDLE, ctypes.POINTER(_ProcessEntry)], BOOL),
        "Process32NextW": ([HANDLE, ctypes.POINTER(_ProcessEntry)], BOOL),
        "CloseHandle": ([HANDLE], BOOL),
    }
    for name, (argtypes, restype) in signatures.items():
        fn = getattr(k, name)
        fn.argtypes, fn.restype = argtypes, restype
    return k


_k32: Any = None


def _kernel() -> Any:
    global _k32
    if _k32 is None:
        _k32 = _kernel32()
    return _k32


def descendants(pid: int, exe: str | None = None) -> list[int]:
    """Live processes started (directly or further down) by ``pid`` – Toolhelp snapshot.

    ``exe`` keeps only processes with that file name: parent ids are plain numbers, so a
    process whose long-gone parent's id was reused would otherwise count as a descendant."""
    k = _kernel()
    snapshot = k.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == _INVALID_HANDLE:
        raise ctypes.WinError(ctypes.get_last_error())
    parents: dict[int, int] = {}
    try:
        entry = _ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        more = k.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            if exe is None or entry.szExeFile.casefold() == exe.casefold():
                parents[entry.th32ProcessID] = entry.th32ParentProcessID
            more = k.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        k.CloseHandle(snapshot)
    found: set[int] = set()
    frontier = {pid}
    while frontier:
        children = {child for child, parent in parents.items()
                    if parent in frontier and child not in found and child != pid}
        found |= children
        frontier = children
    return sorted(found)


def process_alive(pid: int) -> bool:
    """True while process ``pid`` exists and has not exited (False if it cannot be opened)."""
    k = _kernel()
    handle = k.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        return bool(k.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == _STILL_ACTIVE
    finally:
        k.CloseHandle(handle)


class KillJob:
    """A job object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE (all its processes die with it)."""

    def __init__(self) -> None:
        k = _kernel()
        handle = k.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k.SetInformationJobObject(handle, _JOB_EXTENDED_LIMITS, ctypes.byref(limits),
                                         ctypes.sizeof(limits)):
            error = ctypes.get_last_error()
            k.CloseHandle(handle)
            raise ctypes.WinError(error)
        self.handle: int | None = handle

    def assign(self, pid: int) -> None:
        k = _kernel()
        process = k.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
        if not process:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not k.AssignProcessToJobObject(self.handle, process):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            k.CloseHandle(process)

    def pids(self) -> list[int]:
        """Processes currently in the job (the whole Edge tree)."""
        if not self.handle:
            return []
        count = 1024
        buffer = (ctypes.c_size_t * (2 + count))()   # 2 DWORDs pad to one ULONG_PTR on x64
        if not _kernel().QueryInformationJobObject(self.handle, _JOB_PROCESS_ID_LIST, buffer,
                                                   ctypes.sizeof(buffer), None):
            return []
        header = ctypes.cast(buffer, ctypes.POINTER(wintypes.DWORD))
        listed = min(header[1], count)
        offset = ctypes.sizeof(wintypes.DWORD) * 2
        ids = (ctypes.c_size_t * listed).from_buffer(buffer, offset) if listed else []
        return [int(pid) for pid in ids]

    def active(self) -> int:
        info = _BasicAccounting()
        if not self.handle or not _kernel().QueryInformationJobObject(
                self.handle, _JOB_BASIC_ACCOUNTING, ctypes.byref(info), ctypes.sizeof(info), None):
            return 0
        return int(info.ActiveProcesses)

    def kill(self, timeout: float = 5.0) -> bool:
        """Terminate every process in the job and wait until they are gone; True when they are."""
        if not self.handle:
            return True
        _kernel().TerminateJobObject(self.handle, 1)
        deadline = time.monotonic() + timeout
        while self.active():
            if time.monotonic() > deadline:
                return False
            time.sleep(0.02)
        return True

    def close(self) -> None:
        if self.handle:
            _kernel().CloseHandle(self.handle)  # KILL_ON_JOB_CLOSE: whatever is left dies now
            self.handle = None


def _resume_process(pid: int) -> None:
    """Resume the (only) thread of a process created with CREATE_SUSPENDED."""
    k = _kernel()
    snapshot = k.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
    if not snapshot or snapshot == _INVALID_HANDLE:
        raise ctypes.WinError(ctypes.get_last_error())
    resumed = 0
    try:
        entry = _ThreadEntry()
        entry.dwSize = ctypes.sizeof(entry)
        more = k.Thread32First(snapshot, ctypes.byref(entry))
        while more:
            if entry.th32OwnerProcessID == pid:
                thread = k.OpenThread(_THREAD_SUSPEND_RESUME, False, entry.th32ThreadID)
                if thread:
                    try:
                        if k.ResumeThread(thread) != 0xFFFFFFFF:
                            resumed += 1
                    finally:
                        k.CloseHandle(thread)
            more = k.Thread32Next(snapshot, ctypes.byref(entry))
    finally:
        k.CloseHandle(snapshot)
    if not resumed:
        raise OSError(f"could not resume process {pid}")


def _kill_tree_fallback(pid: int) -> None:
    """Without a job: end ``pid`` and its descendants (Edge normally ends its children itself)."""
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW,
                   timeout=15, check=False)


class WebSocket:
    """Minimal RFC 6455 client: text frames, fragmentation, ping/pong, close."""

    def __init__(self, url: str, timeout: float = 10.0) -> None:
        parts = urllib.parse.urlsplit(url)
        self.sock = socket.create_connection((parts.hostname, parts.port or 80), timeout=timeout)
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        self.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {parts.netloc}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                           "Sec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise CDPError("websocket handshake failed")
            head += chunk
        header, _, self._buffer = head.partition(b"\r\n\r\n")
        if b" 101 " not in header.split(b"\r\n", 1)[0]:
            raise CDPError(f"websocket handshake refused: {header[:80]!r}")

    def _read(self, n: int) -> bytes:
        while len(self._buffer) < n:
            chunk = self.sock.recv(max(65536, n - len(self._buffer)))
            if not chunk:
                raise CDPError("websocket closed")
            self._buffer += chunk
        data, self._buffer = self._buffer[:n], self._buffer[n:]
        return data

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        mask = secrets.token_bytes(4)
        length = len(payload)
        if length < 126:
            header = struct.pack("!BB", 0x80 | opcode, 0x80 | length)
        elif length < 65536:
            header = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, length)
        else:
            header = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, length)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def send_text(self, text: str) -> None:
        self._send_frame(0x1, text.encode("utf-8"))

    def recv_text(self, timeout: float) -> str:
        self.sock.settimeout(timeout)
        message = b""
        while True:
            first, second = self._read(2)
            opcode, length = first & 0x0F, second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read(8))[0]
            payload = self._read(length)
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0x8:
                raise CDPError("websocket closed by peer")
            if opcode in (0x0, 0x1, 0x2):
                message += payload
                if first & 0x80:
                    return message.decode("utf-8")

    def close(self) -> None:
        try:
            self._send_frame(0x8, b"")
        except OSError:
            pass
        self.sock.close()


class Page:
    """One DevTools page session."""

    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self._next = 0
        self.events: list[dict[str, Any]] = []

    def call(self, method: str, params: dict[str, Any] | None = None, timeout: float = 15.0) -> dict[str, Any]:
        self._next += 1
        call_id = self._next
        self.ws.send_text(json.dumps({"id": call_id, "method": method, "params": params or {}}))
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CDPError(f"timeout waiting for {method}")
            message = json.loads(self.ws.recv_text(remaining))
            if message.get("id") == call_id:
                if "error" in message:
                    raise CDPError(f"{method}: {message['error']}")
                return message.get("result", {})
            if "method" in message:
                self.events.append(message)
                del self.events[:-200]

    def evaluate(self, expression: str, timeout: float = 15.0) -> Any:
        result = self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True,
                                                "awaitPromise": True}, timeout)
        if "exceptionDetails" in result:
            details = result["exceptionDetails"]
            text = details.get("exception", {}).get("description") or details.get("text")
            raise CDPError(f"JS error: {text}")
        return result.get("result", {}).get("value")

    def wait_for(self, expression: str, timeout: float = 8.0, interval: float = 0.05) -> Any:
        """Poll until ``expression`` is truthy (a DOM node counts as true); return its value."""
        probe = f"(() => {{ const v = ({expression}); return v instanceof Node ? true : v; }})()"
        deadline = time.monotonic() + timeout
        while True:
            value = self.evaluate(probe)
            if value:
                return value
            if time.monotonic() > deadline:
                raise CDPError(f"condition not met within {timeout}s: {expression}")
            time.sleep(interval)

    def navigate(self, url: str) -> None:
        self.evaluate("window.__stale = true")  # gone once the new document has replaced this one
        self.call("Page.navigate", {"url": url})
        self.wait_for("!window.__stale && document.readyState === 'complete'", timeout=15)

    def set_viewport(self, width: int, height: int) -> None:
        self.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": height,
                                                          "deviceScaleFactor": 1, "mobile": False})

    def color_scheme(self, scheme: str) -> None:
        self.call("Emulation.setEmulatedMedia", {"features": [{"name": "prefers-color-scheme", "value": scheme}]})

    def key(self, key: str, *, ctrl: bool = False, shift: bool = False, alt: bool = False) -> None:
        modifiers = (1 if alt else 0) | (2 if ctrl else 0) | (8 if shift else 0)
        if key in SPECIAL_KEYS:
            code, vk = SPECIAL_KEYS[key]
            text = "\r" if key == "Enter" and not ctrl else ""
        else:
            code = f"Key{key.upper()}" if key.isalpha() else f"Digit{key}" if key.isdigit() else ""
            vk = ord(key.upper())
            text = "" if ctrl or alt else key
        down = {"type": "keyDown" if text else "rawKeyDown", "modifiers": modifiers, "key": key,
                "code": code, "windowsVirtualKeyCode": vk}
        if text:
            down["text"] = down["unmodifiedText"] = text
        self.call("Input.dispatchKeyEvent", down)
        self.call("Input.dispatchKeyEvent", {"type": "keyUp", "modifiers": modifiers, "key": key,
                                             "code": code, "windowsVirtualKeyCode": vk})

    def type(self, text: str) -> None:
        self.call("Input.insertText", {"text": text})

    def click(self, selector: str, *, button: str = "left", count: int = 1) -> None:
        box = self.evaluate(f"""(() => {{ const el = document.querySelector({json.dumps(selector)});
            if (!el) return null; el.scrollIntoView({{block: 'nearest'}}); const r = el.getBoundingClientRect();
            return [r.left + r.width / 2, r.top + r.height / 2]; }})()""")
        if not box:
            raise CDPError(f"no element for {selector}")
        x, y = box
        self.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
        for n in range(1, count + 1):
            for kind in ("mousePressed", "mouseReleased"):
                self.call("Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y, "button": button,
                                                       "clickCount": n})

    def screenshot(self, path: str) -> None:
        data = self.call("Page.captureScreenshot", {"format": "png"})["data"]
        with open(path, "wb") as fh:
            fh.write(base64.b64decode(data))


class Edge:
    """A headless Edge instance with one page (``self.page``), its whole process tree in a job."""

    def __init__(self, width: int = 1180, height: int = 780) -> None:
        self.width, self.height = width, height
        self.proc: subprocess.Popen[bytes] | None = None
        self.job: KillJob | None = None
        self.profile = ""
        self.page: Page | None = None

    def start(self, timeout: float = 20.0) -> Page:
        edge = find_edge()
        if edge is None:
            raise CDPError("Microsoft Edge not found")
        self.profile = tempfile.mkdtemp(prefix="projektsog-ui-edge-")
        args = [edge, *EDGE_FLAGS, f"--user-data-dir={self.profile}",
                f"--window-size={self.width},{self.height}", "about:blank"]
        atexit.register(self.close)  # normal interpreter exit; the job covers a hard kill
        try:
            self.job = KillJob()
        except OSError:
            self.job = None
        flags = CREATE_NO_WINDOW | (CREATE_SUSPENDED if self.job else 0)
        self.proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, creationflags=flags)
        if self.job is not None:
            try:
                self.job.assign(self.proc.pid)  # before it runs: every child lands in the job too
            except OSError:
                self.job.close()
                self.job = None
            finally:
                _resume_process(self.proc.pid)
        port_file = os.path.join(self.profile, "DevToolsActivePort")
        deadline = time.monotonic() + timeout
        port = None
        while port is None:
            if self.proc.poll() is not None:
                raise CDPError(f"Edge exited early with code {self.proc.returncode}")
            try:
                with open(port_file, encoding="ascii") as fh:
                    port = int(fh.readline().strip())
            except (OSError, ValueError):
                if time.monotonic() > deadline:
                    raise CDPError("Edge did not open a DevTools port") from None
                time.sleep(0.05)
        targets: list[dict[str, Any]] = []
        while not targets:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5) as resp:
                targets = [t for t in json.load(resp) if t.get("type") == "page"]
            if not targets:
                if time.monotonic() > deadline:
                    raise CDPError("no page target")
                time.sleep(0.05)
        self.page = Page(WebSocket(targets[0]["webSocketDebuggerUrl"]))
        self.page.call("Page.enable")
        self.page.call("Runtime.enable")
        self.page.call("Page.addScriptToEvaluateOnNewDocument", {"source": CLIPBOARD_STUB})
        self.page.set_viewport(self.width, self.height)
        return self.page

    def pids(self) -> list[int]:
        """Every process of this Edge instance (browser, renderers, helpers, crash handler)."""
        return self.job.pids() if self.job is not None else ([self.proc.pid] if self.proc else [])

    def close(self) -> None:
        """Close the browser and make sure no process of its tree survives; idempotent."""
        atexit.unregister(self.close)
        if self.page is not None:
            try:
                self.page.call("Browser.close", timeout=5)
            except (CDPError, OSError):
                pass
            try:
                self.page.ws.close()
            except OSError:
                pass
            self.page = None
        if self.proc is not None:
            try:
                self.proc.wait(timeout=5)  # a graceful exit ends the children as well …
            except subprocess.TimeoutExpired:
                pass
            if self.job is not None:
                self.job.kill()            # … and whatever did not end is ended here
            elif self.proc.poll() is None:
                _kill_tree_fallback(self.proc.pid)
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
            self.proc = None
        if self.job is not None:
            self.job.close()
            self.job = None
        if self.profile:
            for _ in range(20):  # the file system may release the profile a moment later
                shutil.rmtree(self.profile, ignore_errors=True)
                if not os.path.exists(self.profile):
                    break
                time.sleep(0.25)
            self.profile = ""

    def __enter__(self) -> Page:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()


# ------------------------------------------------------------------------------------------
# Screenshot tour (manual visual review)
# ------------------------------------------------------------------------------------------

ROWS_READY = "document.querySelectorAll('#results .row').length > 0"


def _settle(page: Page, expression: str = "true", pause: float = 0.35) -> None:
    page.wait_for(expression)
    time.sleep(pause)  # let entry animations finish


def tour(out_dir: str) -> list[str]:
    from tests._ui_mock_server import MockServer

    os.makedirs(out_dir, exist_ok=True)
    shots: list[str] = []
    with MockServer() as server, Edge() as page:
        base = server.url

        def shot(name: str) -> None:
            path = os.path.join(out_dir, f"{name}.png")
            page.screenshot(path)
            shots.append(path)

        def load(query: str, ready: str = "true", width: int = 1180, height: int = 780,
                 scheme: str = "dark") -> None:
            page.set_viewport(width, height)
            page.color_scheme(scheme)
            page.navigate(base + query)
            _settle(page, ready)

        load("?mock=asked", ROWS_READY + " && !document.querySelector('#resolve').hidden")
        shot("01-empty-resolve-recent")
        load("?q=lindholm&mock=asked", ROWS_READY)
        shot("02-results-lindholm")
        load("?q=klar%20tand&mock=asked", ROWS_READY)
        page.key("ArrowDown")
        page.key("ArrowDown")
        time.sleep(0.2)
        shot("03-results-klar-tand-selection")
        load("?q=grafik&mock=asked", ROWS_READY)
        page.key("4", ctrl=True)
        _settle(page, "!document.querySelector('#empty').hidden")
        shot("04-zero-results-filter")
        load("?q=fagmesse&mock=asked", ROWS_READY)
        page.click("#online-only")
        _settle(page, "!document.querySelector('#empty').hidden")
        shot("05-zero-results-offline")
        load("?q=pixelbro%20radio&mock=asked", ROWS_READY)
        page.key("Enter")
        _settle(page, "document.querySelector('.row__notice.is-active')")
        shot("06-offline-row-enter")
        load("?panel=settings&mock=asked", "document.querySelectorAll('.src').length > 5")
        shot("07-settings-placeringer")
        load("?panel=settings&tab=generelt&mock=asked", "!document.querySelector('#panel-generelt').hidden")
        shot("08-settings-generelt")
        load("?panel=settings&tab=resolve&mock=asked", "!document.querySelector('#panel-resolve').hidden")
        shot("09-settings-resolve")
        load("?q=bøgely&mock=asked", ROWS_READY, width=720)
        shot("10-narrow-720-results")
        load("?mock=asked", ROWS_READY, width=720)
        shot("11-narrow-720-empty")
        load("?panel=settings&mock=asked", "document.querySelectorAll('.src').length > 5", width=720)
        shot("12-narrow-720-settings")
        load("", "document.querySelector('.card')")
        shot("13-question-card")
        load("?mock=first,newdisk,asked", "document.querySelector('.card')")
        shot("14-first-indexing-new-disk")
        load("?mock=resolve-offline,asked", ROWS_READY + " && document.querySelector('.resolve__warn')")
        page.click("[data-resolve=toggle]")
        _settle(page, "document.querySelector('.resolve__details')")
        shot("15-resolve-offline-details")
        load("?mock=resolve-suggestion,asked", ROWS_READY + " && !document.querySelector('#resolve').hidden")
        shot("16-resolve-suggestion")
        load("?mock=resolve-error,asked", "document.querySelector('.resolve__error')")
        shot("17-resolve-error")
        load("?q=lindholm&mock=asked", ROWS_READY, scheme="light")
        shot("18-light-results")
        load("?panel=settings&mock=asked", "document.querySelectorAll('.src').length > 5", scheme="light")
        shot("19-light-settings")
        load("?q=rikke%20lindholm&mock=asked", ROWS_READY)
        page.click("#results .row:nth-child(2)", button="right")
        _settle(page, "document.querySelector('.menu')")
        shot("20-context-menu")
        load("?q=fx9&mock=asked", ROWS_READY, width=1600, height=900)
        shot("21-wide-1600-files")
        load("?mock=first,no-projects,asked,resolve-off", "!document.querySelector('#empty').hidden")
        shot("22-first-indexing-empty")
        load("?mock=idle,no-projects,asked,resolve-empty", "!document.querySelector('#empty').hidden")
        shot("23-no-projects")
        load("?q=grafik&mock=asked", ROWS_READY, width=720, scheme="light")
        page.key("4", ctrl=True)
        _settle(page, "!document.querySelector('#empty').hidden")
        shot("24-narrow-light-zero-results")

        # Settings: "Ikke medtaget (N)" unfolded, and the question before a computer is removed.
        load("?panel=settings&mock=asked", "document.querySelectorAll('.src').length > 5")
        page.click('.sg[aria-label="GRAFIK-PC"] .sg__more-head')
        page.click('.sg[aria-label="GRAFIK-PC"] [data-remove-host]')
        _settle(page, "!document.querySelector('.sg[aria-label=\"GRAFIK-PC\"] .sg__confirm').hidden")
        page.evaluate("document.querySelector('.sg[aria-label=\"GRAFIK-PC\"]').scrollIntoView({block: 'center'})")
        shot("25-settings-unfolded-remove-host")

        # New-disk cards: nothing to include / partly included / included by the indexer.
        load("?mock=asked,nofocus,idle", ROWS_READY)
        disk = {"included": False, "reason": "Ingen projektmapper fundet"}
        server.backend.bus.publish("new_volume", {**disk, "disk_name": "Ny SSD", "drive": "G:", "source_ids": []})
        server.backend.bus.publish("new_volume", {**disk, "disk_name": "Kamerakort", "drive": "K:",
                                                  "source_ids": [14, 999]})
        _settle(page, "document.querySelectorAll('.card').length === 2")
        page.click(".card:nth-child(2) [data-card-focus$=include]")
        _settle(page, "document.querySelector('.card:nth-child(2) .card__error')")
        server.backend.bus.publish("new_volume", {"disk_name": "ARKIV", "drive": "F:", "source_ids": [5],
                                                  "included": True, "reason": "Mediefiler fundet"})
        _settle(page, "document.querySelectorAll('.card').length === 3")
        shot("26-new-disk-cards")

        # A folder that is itself a project (§15.3) and the files inside it.
        load("?q=d%C3%A6kcentret&mock=asked,nofocus,idle", ROWS_READY)
        shot("29-root-project")

        # A disk comes back (at another drive letter): the dimmed row and its hint go away.
        load("?q=pixelbro%20radio&mock=asked,nofocus,idle", ROWS_READY)
        page.key("Enter")
        _settle(page, "document.querySelector('.row__notice.is-active')")
        shot("27-offline-row-notice")
        server.backend.set_online(3, True, "I:\\2024 Disk Sølv")
        _settle(page, "!document.querySelector('#results .row--offline') && !document.querySelector('.row__notice')")
        shot("28-row-back-online")

        # §15.12: 'Kunder 2026 (STUDIO)' is gone from C: (moved away) – next to an unplugged disk.
        server.backend.set_online(3, False, "H:\\2024 Disk Sølv")
        server.backend.set_online(1, False)
        load("?q=rikke%20lindholm&mock=asked,nofocus,idle", ROWS_READY)
        shot("30-folder-gone-rows")
        page.key("Enter")
        _settle(page, "document.querySelector('.row__notice.is-active')")
        shot("31-folder-gone-enter")
        load("?mock=asked,nofocus,idle", "document.querySelectorAll('.resolve__warn').length === 2")
        page.click("[data-resolve=toggle]")
        _settle(page, "document.querySelector('.resolve__details')")
        shot("32-folder-gone-resolve")
        load("?panel=settings&mock=asked,nofocus,idle", "document.querySelectorAll('.src').length > 5")
        shot("33-settings-folder-gone")
        server.backend.set_online(1, True)

        # Removing a computer (§15.12): refused while an added folder lies on it; afterwards the
        # toast counts what the server really forgot (a share also mapped as a drive stays).
        grafik = '.sg[aria-label="GRAFIK-PC"]'
        server.backend.add_root("\\\\GRAFIK-PC\\Arkiv", frozenset())
        load("?panel=settings&mock=asked,nofocus,idle", "document.querySelectorAll('.src').length > 5")
        page.click(f"{grafik} [data-remove-host]")
        _settle(page, f"!document.querySelector('{grafik} .sg__confirm').hidden", pause=0.1)
        page.click(f"{grafik} [data-confirm-remove-host]")
        _settle(page, f"document.querySelector('{grafik} .sg__confirm-text').textContent.startsWith('Mappen')")
        page.evaluate(f"document.querySelector('{grafik}').scrollIntoView({{block: 'center'}})")
        shot("34-remove-host-refused")
        server.backend.remove_root("\\\\GRAFIK-PC\\Arkiv")
        server.backend.sources[10].mapped = True
        _settle(page, f"document.querySelectorAll('{grafik} .src').length === 3", pause=0.1)  # 'Arkiv' is gone
        page.click(f"{grafik} [data-cancel-remove-host]")  # OK
        page.click(f"{grafik} [data-remove-host]")
        _settle(page, f"!document.querySelector('{grafik} .sg__confirm').hidden", pause=0.1)
        page.click(f"{grafik} [data-confirm-remove-host]")
        _settle(page, f"document.querySelectorAll('{grafik} .src').length === 1"
                      " && document.querySelector('#toast .toast__body div')?.textContent.startsWith('GRAFIK-PC')")
        page.evaluate(f"document.querySelector('{grafik}').scrollIntoView({{block: 'center'}})")
        shot("35-remove-host-forgotten")
        page.click('.sg[aria-label="KLIPPER-PC"] [data-remove-host]')  # confirmed with the keyboard
        _settle(page, "document.activeElement.dataset.confirmRemoveHost === 'KLIPPER-PC'", pause=0.1)
        page.key("Enter")
        _settle(page, "!document.querySelector('.sg[aria-label=\"KLIPPER-PC\"]')")
        page.key("Tab")  # goes on from where the removed group was: the next computer's [Fjern]
        _settle(page, "document.activeElement.dataset.removeHost === 'MEDIESERVER'")
        shot("36-settings-focus-after-keyboard-remove")
    return shots


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Screenshot tour of the Projektsøg UI (headless Edge + mock)")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    for path in tour(args.out):
        if sys.stdout is not None:
            print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
