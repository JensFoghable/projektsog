"""Read-only smoke test of the DaVinci Resolve integration against the real, running Resolve.

    python tests/smoke_resolve.py

1. Runs ResolveBridge against the running Resolve - through its scripting helper process
   (``pythonw -m projektsog.resolve_child``) - with a stand-in indexer that groups clip paths
   by the template-folder heuristic (the real Indexer is not needed) and prints the project,
   database, clip count, walk time and the grouping. Follow mode is off; opening is disabled.
   Afterwards it checks that the helper has exited and that THIS process never loaded a module
   of the DaVinci Resolve installation (fusionscript.dll & co. live in the helper only).
2. Runs the menu script externally with ResolvePython.exe and PROJEKTSOG_DRY_RUN=1 (prints what
   it would open) and reports its output.

Only read-only Resolve getters are called; nothing is opened, written or changed (LOCALAPPDATA
points to a temporary folder).
"""

from __future__ import annotations

import ctypes
import importlib.util
import logging
import ntpath
import os
import subprocess
import sys
import tempfile
import threading
import time
from ctypes import wintypes
from typing import Any

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
SCRIPT = os.path.join(REPO, "resolve_scripts", "Projektsøg - Åbn projektmappe.py")
RESOLVE_PYTHON = os.path.join(os.environ.get("ProgramW6432") or r"C:\Program Files",
                              "Blackmagic Design", "DaVinci Resolve", "ResolvePython",
                              "ResolvePython.exe")

# -- process helpers (ctypes, read-only) --------------------------------------------------

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]


_kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
_kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
_kernel32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
_kernel32.Process32FirstW.restype = wintypes.BOOL
_kernel32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
_kernel32.Process32NextW.restype = wintypes.BOOL
_kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
_kernel32.GetProcessTimes.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.GetCurrentProcess.argtypes = ()
_kernel32.GetCurrentProcess.restype = wintypes.HANDLE
_kernel32.K32EnumProcessModules.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.HMODULE),
                                            wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
_kernel32.K32EnumProcessModules.restype = wintypes.BOOL
_kernel32.GetModuleFileNameW.argtypes = (wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD)
_kernel32.GetModuleFileNameW.restype = wintypes.DWORD

TH32CS_SNAPPROCESS = 0x2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


def pids_of(exe: str) -> list[int]:
    snapshot = _kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = PROCESSENTRY32W(dwSize=ctypes.sizeof(PROCESSENTRY32W))
        pids = []
        ok = _kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.casefold() == exe.casefold():
                pids.append(entry.th32ProcessID)
            ok = _kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        return pids
    finally:
        _kernel32.CloseHandle(snapshot)


def process_running(exe: str) -> bool:
    return bool(pids_of(exe))


def process_uptime(exe: str) -> float | None:
    starts = []
    for pid in pids_of(exe):
        handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            continue
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if _kernel32.GetProcessTimes(handle, *map(ctypes.byref, times)):
                created = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
                starts.append(created / 1e7 - 11644473600.0)
        finally:
            _kernel32.CloseHandle(handle)
    return time.time() - min(starts) if starts else None


def own_modules_under(folder: str) -> list[str]:
    """Modules (DLLs) of THIS process whose file lies in ``folder`` (read-only)."""
    process = _kernel32.GetCurrentProcess()
    needed = wintypes.DWORD()
    modules = (wintypes.HMODULE * 2048)()
    if not _kernel32.K32EnumProcessModules(process, modules, ctypes.sizeof(modules),
                                           ctypes.byref(needed)):
        raise ctypes.WinError(ctypes.get_last_error())
    prefix = os.path.normcase(os.path.abspath(folder)) + os.sep
    found = []
    for handle in modules[:needed.value // ctypes.sizeof(wintypes.HMODULE)]:
        name = ctypes.create_unicode_buffer(32768)
        if _kernel32.GetModuleFileNameW(handle, name, len(name)):
            if os.path.normcase(name.value).startswith(prefix):
                found.append(name.value)
    return found


# -- stand-in indexer -----------------------------------------------------------------------

def load_menu_script() -> Any:
    spec = importlib.util.spec_from_file_location("projektsog_resolve_menu", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.dont_write_bytecode = True
    spec.loader.exec_module(module)
    return module


def reachable(path: str, timeout: float = 3.0) -> bool:
    result: list[bool] = []
    worker = threading.Thread(target=lambda: result.append(os.path.isdir(path)), daemon=True)
    worker.start()
    worker.join(timeout)
    return bool(result and result[0])


class TemplateIndexer:
    """Stand-in for Indexer.map_paths(): groups clip paths by the template-folder heuristic
    (outermost candidate, unconfirmed) and fills the SPEC §8 shapes."""

    def __init__(self, menu: Any, hostname: str) -> None:
        self.menu = menu
        self.hostname = hostname
        self.names = frozenset(n.casefold() for n in menu.DEFAULT_TEMPLATE_DIRS)

    def source_ref(self, path: str, online: bool) -> dict[str, Any]:
        root, _ = self.menu.split_path(path) or ("", [])
        if root.startswith("\\\\"):
            host, share = root[2:].split("\\", 1)
            return {"id": None, "name": share, "host": host.upper(), "kind": "share",
                    "online": online, "drive": None, "disk_name": None, "volume_label": None,
                    "last_seen": None}
        return {"id": None, "name": root + "\\", "host": self.hostname, "kind": "local",
                "online": online, "drive": root or None, "disk_name": None, "volume_label": None,
                "last_seen": None}

    def map_paths(self, paths: list[str]) -> dict[str, Any]:
        projects: dict[str, list[Any]] = {}
        others: dict[str, list[Any]] = {}
        for path in paths:
            candidates = self.menu.candidate_folders(path, self.names)
            bucket, folder = (projects, candidates[0]) if candidates else (others,
                                                                          ntpath.dirname(path))
            bucket.setdefault(folder.casefold(), [folder, 0])[1] += 1
        folders = []
        for folder, count in projects.values():
            online = reachable(folder)
            _, parts = self.menu.split_path(folder + "\\x") or ("", [])
            rel = "\\".join(parts[:-1])
            folders.append({"project": {"name": ntpath.basename(folder), "rel_path": rel,
                                        "path": folder, "unc_path": None},
                            "source": self.source_ref(folder, online), "online": online,
                            "count": count, "item": None})
        folders.sort(key=lambda f: (-f["count"], not f["online"]))
        other_dirs = sorted(({"path": d, "count": n, "online": reachable(d)}
                             for d, n in others.values()), key=lambda d: -d["count"])
        return {"folders": folders, "other_dirs": other_dirs, "total": len(paths)}

    def suggest_project_folders(self, name: str, limit: int = 5) -> list[dict[str, Any]]:
        return []

    def list_sources(self) -> list[dict[str, Any]]:
        return []                      # no registry: the bridge keeps the mapped flags

    def path_missing(self, path: str) -> None:
        pass


# -- the smoke test ------------------------------------------------------------------------

def run_bridge(tmp: str) -> None:
    from projektsog import resolve_bridge
    from projektsog.config import Config, hostname
    from projektsog.events import EventBus

    cfg = Config(path=os.path.join(tmp, "config.json"))
    cfg.update({"resolve_follow": "off", "resolve_poll_s": 1})
    opened: list[tuple[Any, ...]] = []

    def refuse_open(*args: Any, **kwargs: Any) -> bool:
        opened.append(args)
        return False

    helpers: list[Any] = []

    def spawn_helper() -> Any:
        helper = resolve_bridge._ChildProcess(resolve_bridge.default_child_argv())
        helpers.append(helper)
        return helper

    def no_stat(key: str, fn: Any, timeout: float) -> tuple[str, Any]:
        opened.append(("stat", key))
        return "error", None

    bridge = resolve_bridge.ResolveBridge(
        cfg, EventBus(), TemplateIndexer(load_menu_script(), hostname()),
        process_running=process_running, process_uptime=process_uptime,
        open_folder=refuse_open, explorer_window_for=lambda path: None,
        call_with_timeout=no_stat, spawn_child=spawn_helper)
    print(f"Resolve.exe running: {process_running('Resolve.exe')}, "
          f"uptime: {process_uptime('Resolve.exe')}")
    started = time.monotonic()
    bridge.start()
    try:
        deadline = started + 40
        while time.monotonic() < deadline:
            st = bridge.state()
            if (not st["running"] or st["error"] or st["updated"] is not None
                    or (st["connected"] and st["project"] is None)):
                break
            time.sleep(0.2)
        first = time.monotonic() - started
        t0 = time.monotonic()
        st = bridge.refresh(wait=True)
        walk = time.monotonic() - t0
    finally:
        bridge.stop()
    print(f"first state after {first:.1f} s; refresh() re-walk took {walk:.2f} s")
    print(f"helper processes: {[h.pid for h in helpers]}, still running after stop(): "
          f"{[h.pid for h in helpers if h.alive()]}")
    resolve_dir = os.path.dirname(os.path.dirname(RESOLVE_PYTHON))
    own = own_modules_under(resolve_dir)
    print(f"modules of {resolve_dir} loaded in this process: {own or 'none'}; "
          f"DaVinciResolveScript imported here: {'DaVinciResolveScript' in sys.modules}")
    assert not own and "DaVinciResolveScript" not in sys.modules, \
        "the main process must never load Resolve's scripting library"
    assert not any(h.alive() for h in helpers), "the helper must exit with the bridge"
    for key in ("enabled", "running", "connected", "error", "project", "database",
                "clip_count", "offline_clips", "offline_disks"):
        print(f"  {key}: {st[key]!r}")
    print(f"  folders ({len(st['folders'])}):")
    for f in st["folders"]:
        print(f"    {f['count']:5d}  {'online ' if f['online'] else 'OFFLINE'}  {f['project']['path']}")
    print(f"  other_dirs ({len(st['other_dirs'])}, top 10):")
    for d in st["other_dirs"][:10]:
        print(f"    {d['count']:5d}  {d['path']}")
    p = st["primary"]
    print(f"  primary: {p['name']!r} ({p['match']}) {p['path']}" if p else "  primary: None")
    assert not opened, "the smoke test must never open anything"


def run_menu_script(tmp: str) -> None:
    env = dict(os.environ, LOCALAPPDATA=tmp, PROJEKTSOG_DRY_RUN="1", PYTHONIOENCODING="utf-8")
    started = time.monotonic()
    proc = subprocess.run([RESOLVE_PYTHON, SCRIPT], capture_output=True, env=env, timeout=120,
                          creationflags=subprocess.CREATE_NO_WINDOW)
    print(f"menu script via ResolvePython.exe (dry run): exit {proc.returncode} in "
          f"{time.monotonic() - started:.1f} s")
    for stream in (proc.stdout, proc.stderr):
        text = stream.decode("utf-8", "replace").strip()
        if text:
            print("    " + text.replace("\n", "\n    "))


def main() -> None:
    if not sys.stdout.isatty():
        sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(stream=sys.stdout, level=logging.WARNING, format="    log: %(message)s")
    logging.getLogger("projektsog.resolve_bridge").setLevel(logging.DEBUG)
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["LOCALAPPDATA"] = tmp
        print("== ResolveBridge (read-only) ==")
        run_bridge(tmp)
        print("== Resolve menu script ==")
        run_menu_script(tmp)


if __name__ == "__main__":
    main()
