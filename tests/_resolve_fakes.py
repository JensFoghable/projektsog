"""Fakes for the Resolve tests: a Resolve scripting object graph, the helper process run
in-process, an indexer, winui/winfs functions, a clock, builders for the SPEC §7.1 / §8 dict
shapes, and a stand-in DaVinciResolveScript module for tests of the real helper process."""

from __future__ import annotations

import json
import os
import time
from typing import Any, Callable

from projektsog import resolve_bridge as rb
from projektsog.resolve_child import Session

DB = {"DbType": "PostgreSQL", "DbName": "Kunder 2026 (Projektserver)", "IpAddress": "192.0.2.41"}
STUDIO_UNC = "\\\\studio-pc\\Kunder 2026 (STUDIO)"


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# -- Resolve object graph ----------------------------------------------------------------

class FakeClip:
    """A media pool clip; ``status`` is its "Online Status" ("Online" / "Offline")."""

    def __init__(self, path: str, name: str | None = None, *, uid: str | None = None,
                 status: str = "Online") -> None:
        self.path = path
        self.name = name or path.rsplit("\\", 1)[-1]
        self.uid = uid if uid is not None else f"clip:{path}"
        self.status = status
        self.property_calls = 0

    def GetName(self) -> str:
        return self.name

    def GetUniqueId(self) -> str:
        return self.uid

    def GetClipProperty(self, key: str | None = None) -> Any:
        self.property_calls += 1
        props = {"File Path": self.path, "Clip Name": self.name, "Online Status": self.status,
                 "File Name": self.path.rsplit("\\", 1)[-1], "Type": "Video + Audio",
                 "Frames": "250", "FPS": 25.0, "Resolution": "1920x1080"}
        return props if key is None else props.get(key, "")


def offline_clip(path: str, name: str | None = None, uid: str | None = None) -> FakeClip:
    return FakeClip(path, name, uid=uid, status="Offline")


class DictOnlyClip(FakeClip):
    """A future API where only the all-properties form exists."""

    def GetClipProperty(self, *args: Any) -> Any:
        if args:
            raise TypeError("GetClipProperty() takes no arguments")
        return {"File Path": self.path}


class FakeFolder:
    def __init__(self, name: str, clips: list[Any] | None = None,
                 subfolders: list["FakeFolder"] | None = None,
                 on_list: Callable[[], None] | None = None, as_dict: bool = False) -> None:
        self.name = name
        self.clips = clips or []
        self.subfolders = subfolders or []
        self.on_list = on_list
        self.as_dict = as_dict
        self.listed = 0

    def GetName(self) -> str:
        return self.name

    def GetClipList(self) -> Any:
        self.listed += 1
        if self.on_list:
            self.on_list()
        if self.as_dict:  # old API style {1.0: item, ...}
            return {float(i + 1): c for i, c in enumerate(self.clips)}
        return list(self.clips)

    def GetSubFolderList(self) -> Any:
        return list(self.subfolders)


class FakeMediaPool:
    """``RelinkClips`` moves each clip into the folder and brings it online - unless its file
    name is in ``missing`` (casefolded) - and returns ``result``; every call is recorded.
    ``on_relink(uids, folder)`` runs after a call has relinked (to change the world, or wait)."""

    def __init__(self, root: FakeFolder) -> None:
        self.root = root
        self.relinks: list[tuple[list[str], str]] = []    # (clip uids, folder)
        self.missing: set[str] = set()
        self.result = True
        self.raises: Exception | None = None
        self.on_relink: Callable[[list[str], str], None] | None = None

    def GetRootFolder(self) -> FakeFolder:
        return self.root

    def RelinkClips(self, clips: list[Any], folder: str) -> bool:
        self.relinks.append(([c.GetUniqueId() for c in clips], folder))
        if self.raises is not None:
            raise self.raises
        for clip in clips:
            name = clip.path.rsplit("\\", 1)[-1]
            if name.casefold() not in self.missing:
                clip.path = folder.rstrip("\\") + "\\" + name
                clip.status = "Online"
        if self.on_relink is not None:
            self.on_relink(self.relinks[-1][0], folder)
        return self.result


def render_job(job_id: str, file: str = "Portræt_v3.mp4", *,
               folder: str = "D:\\Rikke Lindholm\\Final", timeline: str = "Portræt v3",
               mode: str = "Single clip", name: str | None = None) -> dict[str, Any]:
    """A GetRenderJobList() entry (RenderJobInfo)."""
    return {"JobId": job_id, "RenderJobName": name or f"Job {job_id}", "TimelineName": timeline,
            "TargetDir": folder, "OutputFilename": file, "RenderMode": mode,
            "PresetName": "H.264 Master", "IsExportVideo": True, "FormatWidth": 1920}


class FakeProject:
    """A project; its render queue: ``render_jobs`` (RenderJobInfo dicts in queue order),
    ``render_status`` (job id -> RenderJobStatus) and ``rendering`` (IsRenderingInProgress).
    ``on_status`` runs before every GetRenderJobStatus (to advance a clock)."""

    def __init__(self, name: str, root: FakeFolder | None = None, uid: str | None = None) -> None:
        self.name = name
        self.pool = FakeMediaPool(root or FakeFolder("Master"))
        self.uid = uid if uid is not None else f"uid-{name}"
        self.rendering = False
        self.render_jobs: list[dict[str, Any]] = []
        self.render_status: dict[str, dict[str, Any]] = {}
        self.status_calls: list[str] = []
        self.on_status: Callable[[], None] | None = None
        self.list_error: Exception | None = None

    def GetName(self) -> str:
        return self.name

    def GetUniqueId(self) -> str:
        return self.uid

    def GetMediaPool(self) -> FakeMediaPool:
        return self.pool

    def IsRenderingInProgress(self) -> bool:
        return self.rendering

    def GetRenderJobList(self) -> list[dict[str, Any]]:
        if self.list_error is not None:
            raise self.list_error
        return [dict(job) for job in self.render_jobs]

    def GetRenderJobStatus(self, job_id: str) -> dict[str, Any]:
        self.status_calls.append(job_id)
        if self.on_status is not None:
            self.on_status()
        return dict(self.render_status.get(job_id, {}))

    # -- test helpers ------------------------------------------------------------------------
    def add_job(self, job_id: str, status: str = "Ready", **info: Any) -> None:
        self.render_jobs.append(render_job(job_id, **info))
        self.set_status(job_id, status)

    def set_status(self, job_id: str, status: str, pct: int = 0, **extra: Any) -> None:
        self.render_status[job_id] = {"JobStatus": status, "CompletionPercentage": pct, **extra}


class UnstableIdProject(FakeProject):
    """GetUniqueId() returns a new value on every call."""

    def __init__(self, name: str, root: FakeFolder | None = None) -> None:
        super().__init__(name, root)
        self.calls = 0

    def GetUniqueId(self) -> str:
        self.calls += 1
        return f"volatile-{self.calls}"


class FakeProjectManager:
    def __init__(self, project: FakeProject | None = None, db: dict[str, str] | None = None) -> None:
        self.project = project
        self.db = dict(db or DB)

    def GetCurrentProject(self) -> FakeProject | None:
        return self.project

    def GetCurrentDatabase(self) -> dict[str, str]:
        return dict(self.db)


class FakeResolve:
    def __init__(self, project: FakeProject | None = None) -> None:
        self.pm = FakeProjectManager(project)
        self.alive = True
        self.raise_on_pm: Exception | None = None

    def GetProjectManager(self) -> FakeProjectManager | None:
        if self.raise_on_pm is not None:
            raise self.raise_on_pm
        return self.pm if self.alive else None


def clips_in(folder_path: str, *names: str) -> list[FakeClip]:
    return [FakeClip(folder_path + "\\" + n) for n in names]


# -- SPEC dict shapes --------------------------------------------------------------------

def source_ref(sid: int = 1, name: str = "Kunder 2026 (STUDIO)", *, host: str = "STUDIO-PC",
               kind: str = "local", online: bool = True, drive: str | None = "C:",
               disk_name: str | None = None, volume_label: str | None = None,
               is_system: bool = False, volume_present: bool | None = None) -> dict[str, Any]:
    """A SourceRef (SPEC §7.1, §15.4, §15.12) like search.source_ref() builds it
    (tests/test_resolve_index.py checks the keys against the real Indexer). ``volume_present``
    defaults to ``online``; True while offline: the folder is gone, its disk/computer is there."""
    return {"id": sid, "name": name, "host": host, "kind": kind, "online": online,
            "drive": drive, "disk_name": disk_name, "volume_label": volume_label,
            "last_seen": 1_700_000_000.0, "is_system": is_system,
            "volume_present": online if volume_present is None else volume_present}


def project_ref(name: str, path: str, rel_path: str | None = None,
                unc_path: str | None = None) -> dict[str, Any]:
    return {"name": name, "rel_path": rel_path if rel_path is not None else name, "path": path,
            "unc_path": unc_path}


def item(name: str, path: str, source: dict[str, Any], iid: int = 1) -> dict[str, Any]:
    rel = name
    return {"id": iid, "kind": "project", "name": name, "hl": [], "path": path,
            "open_path": path, "unc_path": None, "rel_path": rel, "parent": "", "depth": 1,
            "source": source, "project": project_ref(name, path), "size": 10, "mtime": 1.0,
            "file_count": 3, "ext": None, "is_seq": False, "seq_count": None,
            "subfolders": ["Klip"], "score": None}


def folder_entry(name: str, path: str, count: int, source: dict[str, Any], *,
                 with_item: bool = True, iid: int = 1) -> dict[str, Any]:
    return {"project": project_ref(name, path), "source": source, "online": source["online"],
            "count": count, "item": item(name, path, source, iid) if with_item else None}


def suggestion(name: str, path: str, score: float, source: dict[str, Any] | None = None,
               iid: int = 1) -> dict[str, Any]:
    source = source or source_ref()
    return {"project": project_ref(name, path), "source": source, "online": source["online"],
            "score": score, "item": item(name, path, source, iid)}


def indexed_file(path: str, *, sid: int = 1, online: bool = True, root: str | None = None,
                 unc_root: str | None = None, project: str | None = None,
                 mtime: float = 1_700_000_000.0) -> dict[str, Any]:
    """An Indexer.find_files() row (SPEC §17, §22.2) for the file ``path`` of the location
    ``root`` (``unc_root``: the same location through its share)."""
    folder, name = path.rsplit("\\", 1)
    root = root or folder
    rel = path[len(root):].lstrip("\\")
    rel_folder = folder[len(root):].lstrip("\\")
    unc_folder = None
    if unc_root:
        unc_folder = unc_root + ("\\" + rel_folder if rel_folder else "")
    ref = None
    if project:
        project_path = root + "\\" + project
        ref = project_ref(project.rsplit("\\", 1)[-1], project_path, project,
                          unc_root + "\\" + project if unc_root else None)
    return {"name": name, "size": 1000, "path": path, "folder": folder, "online": online,
            "volume_serial": "5E3A0B21", "project": ref, "source_id": sid, "rel_path": rel,
            "unc_folder": unc_folder, "mtime": mtime, "is_seq": False}


def source_row(sid: int, name: str, path: str, *, online: bool = True, kind: str = "local",
               host: str = "STUDIO-PC", disk_name: str | None = None,
               unc_path: str | None = None, included: bool = True,
               last_scan_end: float | None = 1_700_000_000.0,
               last_shallow_scan: float | None = None, root_is_project: bool = False,
               volume_present: bool | None = None) -> dict[str, Any]:
    """A Source (SPEC §7.1, §15.3, §15.12) as Indexer.list_sources() returns it - the fields
    the bridge reads, under their real names (tests/test_resolve_index.py checks them against
    the real Indexer). ``volume_present`` defaults to ``online``."""
    return {"id": sid, "key": f"test:{sid}", "kind": kind, "display_name": name, "host": host,
            "path": path, "unc_path": unc_path, "volume_label": disk_name, "disk_name": disk_name,
            "online": online, "mode": "auto", "included": included, "manual": False,
            "last_scan_end": last_scan_end, "last_shallow_scan": last_shallow_scan,
            "last_seen": 1_700_000_000.0, "root_is_project": root_is_project,
            "volume_present": online if volume_present is None else volume_present}


# -- Collaborators -----------------------------------------------------------------------

class FakeIndexer:
    def __init__(self, mapping: dict[str, Any] | Callable[[list[str]], dict[str, Any]] | None = None,
                 suggestions: list[dict[str, Any]] | None = None,
                 sources: list[dict[str, Any]] | Callable[[], list[dict[str, Any]]] | None = None,
                 ) -> None:
        self.mapping = mapping if mapping is not None else {"folders": [], "other_dirs": [],
                                                            "total": 0}
        self.suggestions = suggestions or []
        self.sources = sources if sources is not None else []
        self.map_calls: list[list[str]] = []
        self.suggest_calls: list[str] = []
        self.missing: list[str] = []
        self.files: list[dict[str, Any]] = []         # find_files() rows (see indexed_file())
        self.find_calls: list[list[str]] = []
        self.refreshed: list[str] = []

    def find_files(self, names: list[str]) -> list[dict[str, Any]]:
        self.find_calls.append(list(names))
        wanted = {n.casefold() for n in names}
        return [dict(f) for f in self.files if f["name"].casefold() in wanted]

    def refresh_path(self, path: str) -> None:
        self.refreshed.append(path)

    def map_paths(self, paths: list[str]) -> dict[str, Any]:
        self.map_calls.append(list(paths))
        return self.mapping(paths) if callable(self.mapping) else self.mapping

    def suggest_project_folders(self, name: str, limit: int = 5) -> list[dict[str, Any]]:
        self.suggest_calls.append(name)
        return list(self.suggestions)

    def list_sources(self) -> list[dict[str, Any]]:
        sources = self.sources() if callable(self.sources) else self.sources
        return [dict(s) for s in sources]

    def path_missing(self, path: str) -> None:
        self.missing.append(path)


class FakeWinui:
    """winui functions, plus winfs.call_with_timeout for the bridge's timed stat (never runs
    it) and its folder listings before a relink (keys "relink:…": runs the function - tests
    patch ``resolve_bridge._list_names`` or list temp dirs - unless ``list_status`` is set)."""

    def __init__(self) -> None:
        self.running = True
        self.uptime: float | None = 120.0
        self.open_result = True
        self.opened: list[tuple[str, bool]] = []
        self.windows: dict[str, int] = {}
        self.exe_names: list[str] = []
        self.stat_result: tuple[str, Any] = ("ok", "dir")   # never touches the file system
        self.stat_calls: list[tuple[str, float]] = []
        self.list_status: str | None = None              # e.g. "timeout"
        self.list_calls: list[tuple[str, float]] = []

    def process_running(self, exe: str) -> bool:
        self.exe_names.append(exe)
        return self.running

    def process_uptime(self, exe: str) -> float | None:
        return self.uptime

    def open_folder(self, path: str, activate: bool = True) -> bool:
        self.opened.append((path, activate))
        return self.open_result

    def explorer_window_for(self, path: str) -> int | None:
        return self.windows.get(path)

    def call_with_timeout(self, key: str, fn: Callable[[], Any],
                          timeout: float) -> tuple[str, Any]:
        if key.startswith("relink:"):
            self.list_calls.append((key, timeout))
            if self.list_status is not None:
                return self.list_status, None
            try:
                return "ok", fn()
            except OSError:
                return "error", None
        self.stat_calls.append((key, timeout))
        return self.stat_result


# -- The helper process, in-process --------------------------------------------------------

class InProcessChild:
    """projektsog.resolve_child.Session behind the bridge's helper interface: no process, but
    every request and answer makes a JSON round trip as on the pipe. ``factory.fail`` injects
    failures: {"poll": "timeout" | "gone" | "late", ...} (one-shot per command, or "ready");
    "late": the helper handles the request, but its answer never comes (no answer in time)."""

    def __init__(self, session: Session, factory: "ChildFactory") -> None:
        self.session = session
        self.factory = factory
        self.pid = 40_000 + len(factory.children)
        self.closed = False
        self.crashed = False
        self.requests: list[dict[str, Any]] = []
        self._next_id = 0

    def wait_ready(self, timeout: float) -> None:
        self._maybe_fail("ready")

    def request(self, cmd: str, timeout: float, **params: Any) -> dict[str, Any]:
        if self.crashed:
            raise rb.ChildError("the helper exited (exit code 3)")
        if self.closed:
            raise rb.ChildError("the helper was stopped")
        self._next_id += 1
        message = json.loads(json.dumps({"id": self._next_id, "cmd": cmd, **params}))
        self.requests.append(message)
        self.factory.timeouts.append((cmd, timeout))
        late = self.factory.fail.get(cmd) == "late"
        if late:
            del self.factory.fail[cmd]
        self._maybe_fail(cmd)
        answer = json.loads(json.dumps(self.session.handle(message)))
        if late:
            raise rb.ChildError(f"no answer to {cmd!r} in time")
        return answer

    def _maybe_fail(self, what: str) -> None:
        mode = self.factory.fail.pop(what, None)
        if mode == "gone":
            self.crashed = True
            raise rb.ChildError(f"the helper exited during {what!r}")
        if mode == "timeout":
            raise rb.ChildError(f"no answer to {what!r} in time")

    def crash(self) -> None:
        self.crashed = True

    def alive(self) -> bool:
        return not (self.closed or self.crashed)

    def close(self, timeout: float = 0.0) -> None:
        self.closed = True


class ChildFactory:
    """``spawn_child`` for the bridge: every helper runs a fresh Session in-process."""

    def __init__(self, connect: Callable[[], Any], clock: Callable[[], float]) -> None:
        self.connect = connect
        self.clock = clock
        self.children: list[InProcessChild] = []
        self.fail: dict[str, str] = {}
        self.spawn_error: Exception | None = None
        self.timeouts: list[tuple[str, float]] = []

    def __call__(self) -> InProcessChild:
        if self.spawn_error is not None:
            raise self.spawn_error
        child = InProcessChild(Session(connect=self.connect, clock=self.clock), self)
        self.children.append(child)
        return child

    @property
    def running(self) -> list[InProcessChild]:
        return [c for c in self.children if c.alive()]


# -- A stand-in DaVinciResolveScript for the real helper process -----------------------------

FAKE_MODULE_SOURCE = r'''"""Test stand-in for DaVinciResolveScript (imported by projektsog.resolve_child in tests).

Reads its world from the JSON file named by PROJEKTSOG_FAKE_RESOLVE on every call, so a test can
change it while the helper runs. "on_connect"/"on_poll"/"on_walk": "hang" | "crash";
"noise": true prints to stdout (Python, C runtime fd 1 and the Win32 handle) during connect.
"""
import ctypes
import json
import os
import sys
import time
from ctypes import wintypes


def _spec():
    with open(os.environ["PROJEKTSOG_FAKE_RESOLVE"], encoding="utf-8") as fh:
        return json.load(fh)


def _act(where):
    action = _spec().get("on_" + where)
    if action == "hang":
        time.sleep(3600)
    elif action == "crash":
        os._exit(3)


def _noise():
    print("noise from the scripting library")
    sys.stdout.flush()
    os.write(1, b"noise on fd 1\n")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel32.GetStdHandle.restype = wintypes.HANDLE
    kernel32.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                   ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    kernel32.WriteFile.restype = wintypes.BOOL
    data = b"noise on the Win32 handle\n"
    written = wintypes.DWORD()
    kernel32.WriteFile(kernel32.GetStdHandle(-11 & 0xFFFFFFFF), data, len(data),
                       ctypes.byref(written), None)


class _Clip:
    def __init__(self, path):
        self._path = path

    def GetClipProperty(self, key=None):
        props = {"File Path": self._path}
        return props if key is None else props.get(key, "")


class _Folder:
    def __init__(self, clips, subfolders=()):
        self._clips = [_Clip(p) for p in clips]
        self._subfolders = list(subfolders)

    def GetClipList(self):
        return list(self._clips)

    def GetSubFolderList(self):
        return list(self._subfolders)


class _Pool:
    def __init__(self, clips):
        self._root = _Folder(clips[:1], [_Folder(clips[1:])])

    def GetRootFolder(self):
        return self._root


class _Project:
    def __init__(self, spec):
        self._spec = spec

    def GetName(self):
        return self._spec["name"]

    def GetUniqueId(self):
        return self._spec.get("uid", "")

    def GetMediaPool(self):
        _act("walk")
        return _Pool(self._spec.get("clips", []))


class _ProjectManager:
    def GetCurrentProject(self):
        spec = _spec().get("project")
        return _Project(spec) if spec else None

    def GetCurrentDatabase(self):
        return _spec().get("db", {"DbType": "Disk", "DbName": "Local Database"})


class _Resolve:
    def GetProjectManager(self):
        _act("poll")
        return _ProjectManager()


def scriptapp(name):
    _act("connect")
    if _spec().get("noise"):
        _noise()
    return None if _spec().get("refuse") else _Resolve()
'''


def write_fake_resolve(folder: str, spec: dict[str, Any]) -> dict[str, str]:
    """Put the stand-in DaVinciResolveScript into ``folder`` (``Modules`` like the real API
    folder) with ``spec`` as its world; returns the environment variables that make
    projektsog.resolve_child import it instead of Resolve's own module."""
    modules = os.path.join(folder, "Modules")
    os.makedirs(modules, exist_ok=True)
    module = os.path.join(modules, "DaVinciResolveScript.py")
    with open(module, "w", encoding="utf-8") as fh:
        fh.write(FAKE_MODULE_SOURCE)
    spec_path = os.path.join(folder, "world.json")
    update_fake_resolve(spec_path, spec)
    return {"RESOLVE_SCRIPT_API": folder, "RESOLVE_SCRIPT_LIB": module,
            "PROJEKTSOG_FAKE_RESOLVE": spec_path, "PYTHONDONTWRITEBYTECODE": "1"}


def update_fake_resolve(spec_path: str, spec: dict[str, Any]) -> None:
    tmp = spec_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(spec, fh)
    for attempt in range(50):          # the helper may be reading the file right now
        try:
            os.replace(tmp, spec_path)
            return
        except PermissionError:
            if attempt == 49:
                raise
            time.sleep(0.02)
