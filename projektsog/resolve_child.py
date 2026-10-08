"""DaVinci Resolve scripting helper process (SPEC §15.9): ``pythonw -m projektsog.resolve_child``.

This is the only Projektsøg process that loads Resolve's scripting library
(``DaVinciResolveScript`` loads ``fusionscript.dll`` as an extension module, which CPython can
never unload). ResolveBridge starts it only while ``Resolve.exe`` runs (after its start-up grace)
and stops it as soon as Resolve exits, so no Projektsøg process keeps files of the Resolve
installation in use while Resolve is closed or being updated. A scripting call that hangs only
hangs this process, which the bridge can kill and restart.

JSON lines (ASCII), one answer per request, in order; the request's ``id`` is echoed::

    <- {"ev": "ready", "pid": 1234}                                           (once, at start)
    -> {"id": 1, "cmd": "connect"}
    <- {"id": 1, "ok": true}
    <- {"id": 1, "ok": false, "error": "module" | "refused" | "failed", "detail": "..."}
    -> {"id": 2, "cmd": "poll"}
    <- {"id": 2, "ok": true, "db": [DbType, DbName, IpAddress], "database": DbName | null,
        "project": str | null, "uid": str,
        "page": "edit" | "color" | ... | "", "timeline": str, "timecode": str, "rendering": bool}
    -> {"id": 2, "cmd": "poll", "render": {"scan": bool, "watch": [jobId, ...]}}   (SPEC §22.1)
    <- ... the same, plus "jobs": [{"id", "name", "timeline", "dir", "file", "mode", "preset",
        "status", "pct", "eta_ms", "took_ms", "error"}], "jobs_truncated": bool
    -> {"id": 3, "cmd": "uid"}        (the current project's unique id, through a fresh proxy)
    <- {"id": 3, "ok": true, "project": str | null, "uid": str}
    -> {"id": 4, "cmd": "walk", "max_clips": 50000, "max_seconds": 20.0}
    <- {"id": 4, "ok": true, "paths": [str, ...], "clip_count": int, "truncated": bool}
    -> {"id": 5, "cmd": "offline", "max_clips": 50000, "max_seconds": 20.0}       (SPEC §22.2)
    <- {"id": 5, "ok": true, "clips": [{"uid", "name", "path", "dir", "type", "frames", "fps",
        "resolution", "status"}], "scanned": int, "truncated": bool}
    -> {"id": 6, "cmd": "relink", "groups": [{"folder": str, "uids": [str], "expect": {uid: path}}],
        "project": {"name", "uid", "database"} (optional: refuse another project)}
    <- {"id": 6, "ok": true, "results": [{"uid", "ok", "path", "status", "why"}],
        "truncated": bool, "project_changed": bool}
    <- {"id": n, "ok": false, "error": "not_connected" | "unavailable" | "bad_request",
        "detail": "..."}
    -> {"cmd": "quit"}

``clip_count`` counts every clip visited (also clips without a file path). With ``render`` the
poll also reads the render queue (``scan``: the newest jobs; ``watch``: these jobs, if they still
exist): at most 25 jobs within 5 s, else ``jobs_truncated``. ``why`` of a relink result is null
(online now) or "not_found" | "online" | "changed" | "offline" | "relink_failed".

Only read-only scripting getters are called - with ONE exception (SPEC §1 rule 2, §22.2):
``relink`` calls ``MediaPool.RelinkClips(items, folder)``, once per folder, for clips that are
still offline at the path the user saw (``expect``). The bridge sends it only after the user
clicked "Genlink", for the project the plan was made for, never while a Claude session holds
Resolve or Resolve renders. Nothing here ever saves the project or writes anything else.

The helper exits on ``quit``, and at once when stdin reaches EOF (the app stopped it, quit or
died) - even in the middle of a scripting call. It logs to ``log_dir()\\resolve_child.log``.
"""

from __future__ import annotations

import ctypes
import json
import logging
import logging.handlers
import ntpath
import os
import queue
import sys
import threading
import time
from collections.abc import Callable, Mapping, MutableMapping
from typing import Any, BinaryIO

from .config import log_dir

log = logging.getLogger("projektsog.resolve_child")   # also when run as __main__

DEFAULT_MAX_CLIPS = 50_000
DEFAULT_MAX_SECONDS = 20.0
MAX_RENDER_JOBS = 25           # render queue entries read per poll (SPEC §22.1)
RENDER_MAX_SECONDS = 5.0
MAX_RELINK_CLIPS = 50_000
LOG_FILE = "resolve_child.log"

_SEM_FAILCRITICALERRORS = 0x0001
_SEM_NOOPENFILEERRORBOX = 0x8000
_STD_INPUT_HANDLE = -10 & 0xFFFFFFFF
_STD_OUTPUT_HANDLE = -11 & 0xFFFFFFFF

if sys.platform == "win32":
    import msvcrt
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.SetErrorMode.argtypes = [wintypes.UINT]
    _kernel32.SetErrorMode.restype = wintypes.UINT
    _kernel32.SetStdHandle.argtypes = [wintypes.DWORD, wintypes.HANDLE]
    _kernel32.SetStdHandle.restype = wintypes.BOOL
else:                                # keeps the module importable for tooling
    msvcrt = None
    _kernel32 = None


# --------------------------------------------------------------------------------------
# Connecting
# --------------------------------------------------------------------------------------

def resolve_script_locations() -> tuple[str, str]:
    """Return (RESOLVE_SCRIPT_API dir, RESOLVE_SCRIPT_LIB path) of a default installation."""
    program_data = os.environ.get("PROGRAMDATA") or r"C:\ProgramData"
    program_files = (os.environ.get("ProgramW6432") or os.environ.get("PROGRAMFILES")
                     or r"C:\Program Files")
    api = ntpath.join(program_data, "Blackmagic Design", "DaVinci Resolve", "Support",
                      "Developer", "Scripting")
    lib = ntpath.join(program_files, "Blackmagic Design", "DaVinci Resolve", "fusionscript.dll")
    return api, lib


def prepare_script_environment(environ: MutableMapping[str, str] | None = None,
                               path: list[str] | None = None) -> str:
    """Set RESOLVE_SCRIPT_API/LIB (unless they already point to existing paths) and put the
    DaVinciResolveScript module folder on ``path``. Returns that folder."""
    environ = os.environ if environ is None else environ
    path = sys.path if path is None else path
    api, lib = resolve_script_locations()
    for name, value in (("RESOLVE_SCRIPT_API", api), ("RESOLVE_SCRIPT_LIB", lib)):
        current = environ.get(name)
        if not current or not os.path.exists(current):
            environ[name] = value
    modules = ntpath.join(environ["RESOLVE_SCRIPT_API"], "Modules")
    if modules not in path:
        path.append(modules)
    return modules


def connect_resolve() -> Any:
    """Import DaVinciResolveScript and return ``scriptapp("Resolve")`` (None when refused).

    Raises ImportError when the scripting module cannot be loaded.
    """
    prepare_script_environment()
    sys.dont_write_bytecode = True   # never write a __pycache__ next to Resolve's module
    import DaVinciResolveScript  # noqa: PLC0415 - ships with Resolve; this process only

    return DaVinciResolveScript.scriptapp("Resolve")


# --------------------------------------------------------------------------------------
# Answering requests
# --------------------------------------------------------------------------------------

class _Unavailable(Exception):
    """Resolve stopped answering scripting calls (quit, restarting or busy)."""


class _NotConnected(Exception):
    """A request that needs the scripting object arrived before a successful connect."""


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _call_text(getter: Callable[[], Any]) -> str:
    try:
        return _text(getter())
    except Exception:
        return ""


def _as_list(value: Any) -> list[Any]:
    """Resolve returns lists; older versions returned {1.0: item, ...} dicts."""
    if value is None:
        return []
    if isinstance(value, dict):
        return [value[k] for k in sorted(value)]
    try:
        return list(value)
    except TypeError:
        return []


def _clip_file_path(clip: Any) -> str:
    try:
        value = clip.GetClipProperty("File Path")
    except TypeError:  # an API without the (deprecated) single-key form
        value = clip.GetClipProperty()
    if isinstance(value, dict):
        value = value.get("File Path")
    return value.strip() if isinstance(value, str) else ""


def _clip_properties(clip: Any) -> dict[str, Any]:
    """All of a clip's properties in ONE call (``GetClipProperty()``); {} when that fails."""
    try:
        props = clip.GetClipProperty()
    except Exception:
        return {}
    return props if isinstance(props, dict) else {}


def _int_or_none(value: Any) -> int | None:
    """Resolve's numbers (ints, floats, digit strings) as an int; None otherwise."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value == value and abs(value) < 1e15 else None
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _scalar(value: Any) -> str | int | float:
    """A clip property as JSON-safe text or number ('' for anything else)."""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value == value \
            and abs(value) != float("inf"):
        return value
    return ""


def _positive_int(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return default
    return value


def _positive_float(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not value > 0:
        return default
    return float(value)


class _PoolWalk:
    """The clips of a media pool, folder by folder (depth first, folders in pool order),
    bounded by clip count and a deadline. ``count`` = clips yielded; ``truncated`` = a bound
    stopped the walk."""

    def __init__(self, root: Any, max_clips: int, deadline: float,
                 clock: Callable[[], float]) -> None:
        self._root = root
        self._max = max_clips
        self._deadline = deadline
        self._clock = clock
        self.count = 0
        self.truncated = False

    def __iter__(self) -> Any:
        stack = [self._root]
        while stack:
            if self._clock() > self._deadline:
                self.truncated = True
                return
            folder = stack.pop()
            for clip in _as_list(folder.GetClipList()):
                if self.count >= self._max or self._clock() > self._deadline:
                    self.truncated = True
                    return
                self.count += 1
                yield clip
            stack.extend(reversed(_as_list(folder.GetSubFolderList())))


class Session:
    """Answers protocol requests with read-only Resolve scripting calls.

    ``connect`` returns the scripting object (None when Resolve refuses the connection; raises
    ImportError when the scripting module cannot be loaded) and ``clock`` bounds the media pool
    walk. Both are seams for the tests, which drive a Session in-process against fakes.
    """

    def __init__(self, connect: Callable[[], Any] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._connect = connect or connect_resolve
        self._clock = clock
        self._resolve: Any = None

    def handle(self, message: Mapping[str, Any]) -> dict[str, Any]:
        """The answer to one request (never raises)."""
        command = message.get("cmd")
        handler = {"connect": self._connect_cmd, "poll": self._poll, "uid": self._uid,
                   "walk": self._walk, "offline": self._offline,
                   "relink": self._relink}.get(command) if isinstance(command, str) else None
        if handler is None:
            answer = {"ok": False, "error": "bad_request",
                      "detail": f"unknown command {command!r}"}
        else:
            try:
                answer = handler(message)
            except _NotConnected:
                answer = {"ok": False, "error": "not_connected", "detail": "connect first"}
            except _Unavailable as exc:
                log.info("DaVinci Resolve is not answering: %s", exc)
                self._resolve = None
                answer = {"ok": False, "error": "unavailable", "detail": str(exc)}
            except Exception as exc:  # a call on a Resolve proxy failed
                log.warning("Resolve scripting call failed (%s)", command, exc_info=True)
                self._resolve = None
                answer = {"ok": False, "error": "unavailable",
                          "detail": f"{type(exc).__name__}: {exc}"}
        return {"id": message.get("id"), **answer}

    def _connect_cmd(self, message: Mapping[str, Any]) -> dict[str, Any]:
        self._resolve = None
        try:
            resolve = self._connect()
        except ImportError as exc:
            log.warning("DaVinciResolveScript could not be loaded: %s", exc)
            return {"ok": False, "error": "module", "detail": str(exc)}
        except Exception as exc:
            log.exception("Connecting to DaVinci Resolve failed")
            return {"ok": False, "error": "failed", "detail": f"{type(exc).__name__}: {exc}"}
        if resolve is None:
            log.info("DaVinci Resolve refused the scripting connection (external scripting off?)")
            return {"ok": False, "error": "refused", "detail": "scriptapp() returned None"}
        self._resolve = resolve
        log.info("Connected to DaVinci Resolve")
        return {"ok": True}

    def _project_manager(self) -> Any:
        if self._resolve is None:
            raise _NotConnected
        manager = self._resolve.GetProjectManager()
        if manager is None:
            raise _Unavailable("GetProjectManager() returned None")
        return manager

    def _poll(self, message: Mapping[str, Any]) -> dict[str, Any]:
        manager = self._project_manager()
        db = manager.GetCurrentDatabase()
        db = db if isinstance(db, dict) else {}
        db_id = [_text(db.get("DbType")), _text(db.get("DbName")), _text(db.get("IpAddress"))]
        answer = {"ok": True, "db": db_id, "database": db_id[1] or None, "project": None,
                  "uid": ""}
        answer.update(self._activity(None))
        render = message.get("render")
        project = manager.GetCurrentProject()
        if project is None:
            if isinstance(render, Mapping):
                answer.update(jobs=[], jobs_truncated=False)
            return answer
        name = project.GetName()
        if not isinstance(name, str):
            raise _Unavailable(f"Project.GetName() returned {name!r}")
        answer = {**answer, "project": name, "uid": _call_text(project.GetUniqueId),
                  **self._activity(project)}
        if isinstance(render, Mapping):
            answer.update(self._render_jobs(project, render))
        return answer

    def _render_jobs(self, project: Any, render: Mapping[str, Any]) -> dict[str, Any]:
        """The render queue for the bridge's render watch (SPEC §22.1): the watched jobs that
        still exist, then (``scan``) the newest jobs - at most MAX_RENDER_JOBS, read within
        RENDER_MAX_SECONDS. Read-only getters, each in its own try."""
        deadline = self._clock() + RENDER_MAX_SECONDS
        watch = render.get("watch")
        watch = [w for w in watch if isinstance(w, str) and w] if isinstance(watch, list) else []
        try:
            listed = _as_list(project.GetRenderJobList())
        except Exception as exc:
            log.debug("GetRenderJobList() failed: %s", exc)
            return {"jobs": [], "jobs_truncated": True}
        infos: dict[str, Mapping[str, Any]] = {}
        for info in listed:
            if isinstance(info, Mapping) and _text(info.get("JobId")):
                infos[info["JobId"]] = info
        order = list(dict.fromkeys(w for w in watch if w in infos))
        if render.get("scan") is True:
            order += [job_id for job_id in reversed(infos) if job_id not in order]
        truncated = len(order) > MAX_RENDER_JOBS
        jobs = []
        for job_id in order[:MAX_RENDER_JOBS]:
            if self._clock() > deadline:
                truncated = True
                break
            jobs.append(self._render_job(project, infos[job_id]))
        return {"jobs": jobs, "jobs_truncated": truncated}

    @staticmethod
    def _render_job(project: Any, info: Mapping[str, Any]) -> dict[str, Any]:
        job_id = info["JobId"]
        try:
            status = project.GetRenderJobStatus(job_id)
        except Exception as exc:
            log.debug("GetRenderJobStatus(%s) failed: %s", job_id, exc)
            status = None
        status = status if isinstance(status, Mapping) else {}
        return {"id": job_id, "name": _text(info.get("RenderJobName")),
                "timeline": _text(info.get("TimelineName")), "dir": _text(info.get("TargetDir")),
                "file": _text(info.get("OutputFilename")), "mode": _text(info.get("RenderMode")),
                "preset": _text(info.get("PresetName")), "status": _text(status.get("JobStatus")),
                "pct": _int_or_none(status.get("CompletionPercentage")),
                "eta_ms": _int_or_none(status.get("EstimatedTimeRemainingInMs")),
                "took_ms": _int_or_none(status.get("TimeTakenToRenderInMs")),
                "error": _text(status.get("Error"))}

    def _activity(self, project: Any) -> dict[str, Any]:
        """What the editor is doing, for the time tracker: the open page, the playhead and
        whether a render runs. Each getter is optional - a failure leaves its field empty."""
        page = ""
        if self._resolve is not None:
            try:   # (older Resolve versions or test fakes may lack a getter)
                page = _text(self._resolve.GetCurrentPage())
            except Exception:
                page = ""
        timecode, timeline_name, rendering = "", "", False
        if project is not None:
            try:
                timeline = project.GetCurrentTimeline()
            except Exception:
                timeline = None
            if timeline is not None:
                try:
                    timecode = _call_text(timeline.GetCurrentTimecode)
                except Exception:
                    timecode = ""
                try:
                    timeline_name = _call_text(timeline.GetName)
                except Exception:
                    timeline_name = ""
            try:
                rendering = bool(project.IsRenderingInProgress())
            except Exception:
                rendering = False
        return {"page": page, "timeline": timeline_name, "timecode": timecode, "rendering": rendering}

    def _uid(self, message: Mapping[str, Any]) -> dict[str, Any]:
        project = self._project_manager().GetCurrentProject()
        if project is None:
            return {"ok": True, "project": None, "uid": ""}
        name = project.GetName()
        return {"ok": True, "project": name if isinstance(name, str) else None,
                "uid": _call_text(project.GetUniqueId)}

    def _walk(self, message: Mapping[str, Any]) -> dict[str, Any]:
        """The file paths of the media pool's clips, bounded by clip count and time."""
        project = self._project_manager().GetCurrentProject()
        if project is None:  # closed since the poll: the bridge's check after the walk sees it
            return {"ok": True, "paths": [], "clip_count": 0, "truncated": False}
        walk = self._pool_walk(self._media_pool(project)[1], message)
        paths = [path for clip in walk if (path := _clip_file_path(clip))]
        return {"ok": True, "paths": paths, "clip_count": walk.count, "truncated": walk.truncated}

    @staticmethod
    def _media_pool(project: Any) -> tuple[Any, Any]:
        """(media pool, its root folder) of ``project``."""
        pool = project.GetMediaPool()
        root = pool.GetRootFolder() if pool is not None else None
        if root is None:
            raise _Unavailable("the media pool is not available")
        return pool, root

    def _pool_walk(self, root: Any, message: Mapping[str, Any]) -> _PoolWalk:
        max_clips = _positive_int(message.get("max_clips"), DEFAULT_MAX_CLIPS)
        max_seconds = _positive_float(message.get("max_seconds"), DEFAULT_MAX_SECONDS)
        return _PoolWalk(root, max_clips, self._clock() + max_seconds, self._clock)

    def _offline(self, message: Mapping[str, Any]) -> dict[str, Any]:
        """The media pool's clips that are not online (SPEC §22.2): one ``GetClipProperty()``
        per clip, its unique id only for the offline ones."""
        project = self._project_manager().GetCurrentProject()
        if project is None:
            return {"ok": True, "clips": [], "scanned": 0, "truncated": False}
        walk = self._pool_walk(self._media_pool(project)[1], message)
        clips: list[dict[str, Any]] = []
        for clip in walk:
            props = _clip_properties(clip)
            path = _text(props.get("File Path")).strip()
            status = _text(props.get("Online Status")).strip()
            if not path or not status or status.casefold() == "online":
                continue          # no file (timelines, generators) or online / status unknown
            clips.append({"uid": _call_text(clip.GetUniqueId),
                          "name": (_text(props.get("Clip Name")) or _text(props.get("File Name"))
                                   or ntpath.basename(path)),
                          "path": path, "dir": ntpath.dirname(path), "type": _text(props.get("Type")),
                          "frames": _scalar(props.get("Frames")), "fps": _scalar(props.get("FPS")),
                          "resolution": _text(props.get("Resolution")), "status": status})
        return {"ok": True, "clips": clips, "scanned": walk.count, "truncated": walk.truncated}

    def _relink(self, message: Mapping[str, Any]) -> dict[str, Any]:
        """``MediaPool.RelinkClips`` - the one writing call (SPEC §1 rule 2, §22.2).

        The clips are found by unique id in a fresh walk; a clip that is online now or whose
        file path is no longer the one the user saw (``expect``) is left alone. One
        RelinkClips per folder, then each clip's File Path and Online Status are read again.
        """
        wanted = self._relink_request(message)
        if wanted is None:
            return {"ok": False, "error": "bad_request", "detail": "groups"}
        manager = self._project_manager()
        project = manager.GetCurrentProject()
        if project is None or not self._is_expected_project(manager, project, message.get("project")):
            log.info("relink refused: the project in Resolve is not the one the plan was made for")
            return {"ok": True, "results": [], "truncated": False, "project_changed": True}
        pool, root = self._media_pool(project)
        walk = self._pool_walk(root, message)
        items: dict[str, Any] = {}
        for clip in walk:
            uid = _call_text(clip.GetUniqueId)
            if uid in wanted and uid not in items:
                items[uid] = clip
                if len(items) == len(wanted):
                    break
        results: dict[str, dict[str, Any]] = {}
        batches: dict[str, tuple[str, list[tuple[str, Any]]]] = {}
        for uid, (folder, expect) in wanted.items():
            clip = items.get(uid)
            if clip is None:
                results[uid] = {"uid": uid, "ok": False, "path": None, "status": None,
                                "why": "not_found"}
                continue
            props = _clip_properties(clip)
            path = _text(props.get("File Path")).strip()
            status = _text(props.get("Online Status")).strip()
            why = ("online" if status.casefold() == "online"
                   else "changed" if expect is None or path.casefold() != expect.strip().casefold()
                   else None)
            if why is not None:
                results[uid] = {"uid": uid, "ok": False, "path": path or None,
                                "status": status or None, "why": why}
                continue
            batches.setdefault(folder.casefold(), (folder, []))[1].append((uid, clip))
        for folder, entries in batches.values():
            try:
                done = bool(pool.RelinkClips([clip for _uid, clip in entries], folder))
            except Exception as exc:
                log.warning("RelinkClips(%d clips, %s) failed: %s", len(entries), folder, exc)
                done = False
            log.info("RelinkClips(%d clips, %s) -> %s", len(entries), folder, done)
            for uid, clip in entries:
                props = _clip_properties(clip)
                path = _text(props.get("File Path")).strip()
                status = _text(props.get("Online Status")).strip()
                ok = status.casefold() == "online"
                results[uid] = {"uid": uid, "ok": ok, "path": path or None, "status": status or None,
                                "why": None if ok else "offline" if done else "relink_failed"}
        return {"ok": True, "results": [results[uid] for uid in wanted],
                "truncated": walk.truncated, "project_changed": False}

    @staticmethod
    def _relink_request(message: Mapping[str, Any]) -> dict[str, tuple[str, str | None]] | None:
        """``{uid: (folder, expected old path)}`` of a relink request; None when malformed."""
        groups = message.get("groups")
        if not isinstance(groups, list):
            return None
        wanted: dict[str, tuple[str, str | None]] = {}
        for group in groups:
            if not isinstance(group, Mapping):
                return None
            folder, uids, expect = group.get("folder"), group.get("uids"), group.get("expect")
            if not (isinstance(folder, str) and folder.strip() and isinstance(uids, list)):
                return None
            expect = expect if isinstance(expect, Mapping) else {}
            for uid in uids:
                if not isinstance(uid, str) or not uid:
                    return None
                old = expect.get(uid)
                wanted.setdefault(uid, (folder.strip(), old if isinstance(old, str) else None))
        return wanted if 0 < len(wanted) <= MAX_RELINK_CLIPS else None

    @staticmethod
    def _is_expected_project(manager: Any, project: Any, expected: Any) -> bool:
        """Is ``project`` the one named in a request's ``project`` ({name, uid, database}; an
        empty uid / database is not compared)?"""
        if not isinstance(expected, Mapping):
            return True
        if _call_text(project.GetName) != _text(expected.get("name")):
            return False
        uid = _text(expected.get("uid"))
        if uid and _call_text(project.GetUniqueId) != uid:
            return False
        database = _text(expected.get("database"))
        if database:
            db = manager.GetCurrentDatabase()
            if not isinstance(db, dict) or _text(db.get("DbName")) != database:
                return False
        return True


# --------------------------------------------------------------------------------------
# The process
# --------------------------------------------------------------------------------------

class LineWriter:
    """Thread-safe writer of ASCII JSON lines; calls ``on_broken`` once the pipe is gone."""

    def __init__(self, stream: BinaryIO, on_broken: Callable[[], None] | None = None) -> None:
        self._stream = stream
        self._on_broken = on_broken
        self._lock = threading.Lock()
        self.broken = False

    def __call__(self, message: Mapping[str, Any]) -> None:
        try:
            data = json.dumps(message, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        except (TypeError, ValueError):
            log.exception("cannot serialise the answer to request %r", message.get("id"))
            data = json.dumps({"id": message.get("id"), "ok": False, "error": "unavailable",
                               "detail": "unserialisable answer"}).encode("ascii")
        with self._lock:
            if self.broken:
                return
            try:
                self._stream.write(data + b"\n")
                self._stream.flush()
                return
            except (OSError, ValueError) as exc:
                self.broken = True
                log.info("answer pipe closed (%s)", exc)
        if self._on_broken is not None:
            self._on_broken()


def serve(stdin: BinaryIO, emit: Callable[[Mapping[str, Any]], None], session: Session,
          on_eof: Callable[[], None]) -> None:
    """Answer requests until ``quit`` or the end of stdin.

    A reader thread reads stdin, so the end of input is noticed (``on_eof``, which exits the
    process in production) even while the main thread is stuck in a scripting call.
    """
    requests: queue.SimpleQueue[dict[str, Any] | None] = queue.SimpleQueue()

    def read() -> None:
        try:
            for raw in stdin:
                line = raw.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    log.warning("ignoring a malformed request %.200r", line)
                    continue
                if isinstance(message, dict):
                    requests.put(message)
                else:
                    log.warning("ignoring a non-object request %.200r", line)
        except (OSError, ValueError) as exc:
            log.info("reading requests failed: %s", exc)
        log.info("stdin closed")
        on_eof()
        requests.put(None)

    threading.Thread(target=read, name="resolve-child-stdin", daemon=True).start()
    while True:
        message = requests.get()
        if message is None or message.get("cmd") == "quit":
            return
        emit(session.handle(message))


def _protocol_streams() -> tuple[BinaryIO, BinaryIO] | None:
    """Private duplicates of the stdin/stdout pipes. File descriptors 0/1, the Win32 standard
    handles and sys.stdin/stdout then refer to NUL, so nothing the scripting library prints or
    reads can corrupt the protocol."""
    if sys.stdin is None or sys.stdout is None:
        return None
    try:
        sys.stdout.flush()
        in_fd, out_fd = os.dup(0), os.dup(1)
    except (OSError, ValueError):
        return None
    for fd, flags, std_handle in ((0, os.O_RDONLY, _STD_INPUT_HANDLE),
                                  (1, os.O_WRONLY, _STD_OUTPUT_HANDLE)):
        null = os.open(os.devnull, flags)
        try:
            os.dup2(null, fd)
        finally:
            os.close(null)
        if _kernel32 is not None and msvcrt is not None:
            _kernel32.SetStdHandle(std_handle, msvcrt.get_osfhandle(fd))
    return os.fdopen(in_fd, "rb"), os.fdopen(out_fd, "wb")


def _setup_logging() -> None:
    try:
        handler: logging.Handler = logging.handlers.RotatingFileHandler(
            os.path.join(log_dir(), LOG_FILE), maxBytes=512 * 1024, backupCount=1,
            encoding="utf-8", delay=True)
    except OSError:
        handler = logging.NullHandler()
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s resolve-child[%(process)d] %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)

    def thread_hook(args: threading.ExceptHookArgs) -> None:
        log.error("uncaught exception in thread %s", getattr(args.thread, "name", "?"),
                  exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    threading.excepthook = thread_hook
    sys.excepthook = lambda t, v, tb: log.critical("uncaught exception", exc_info=(t, v, tb))


def _exit_now(code: int = 0) -> None:
    """Leave at once - never wait for a scripting call that may hang."""
    try:
        logging.shutdown()
    finally:
        os._exit(code)


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    if _kernel32 is not None:
        _kernel32.SetErrorMode(_SEM_FAILCRITICALERRORS | _SEM_NOOPENFILEERRORBOX)
    streams = _protocol_streams()
    if streams is None:
        log.error("stdin/stdout pipes are missing - nothing to serve")
        return 2
    stdin, stdout = streams
    emit = LineWriter(stdout, on_broken=_exit_now)
    log.info("resolve helper %d ready", os.getpid())
    emit({"ev": "ready", "pid": os.getpid()})
    serve(stdin, emit, Session(), on_eof=_exit_now)
    log.info("resolve helper %d exiting", os.getpid())
    return 0


if __name__ == "__main__":
    _exit_now(main())
