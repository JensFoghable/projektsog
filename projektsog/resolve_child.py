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
    -> {"id": 3, "cmd": "uid"}        (the current project's unique id, through a fresh proxy)
    <- {"id": 3, "ok": true, "project": str | null, "uid": str}
    -> {"id": 4, "cmd": "walk", "max_clips": 50000, "max_seconds": 20.0}
    <- {"id": 4, "ok": true, "paths": [str, ...], "clip_count": int, "truncated": bool}
    <- {"id": n, "ok": false, "error": "not_connected" | "unavailable" | "bad_request",
        "detail": "..."}
    -> {"cmd": "quit"}

``clip_count`` counts every clip visited (also clips without a file path). Only read-only
scripting getters are called. The helper exits on ``quit``, and at once when stdin reaches EOF
(the app stopped it, quit or died) - even in the middle of a scripting call. It logs to
``log_dir()\\resolve_child.log``.
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


def _positive_int(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return default
    return value


def _positive_float(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not value > 0:
        return default
    return float(value)


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
                   "walk": self._walk}.get(command) if isinstance(command, str) else None
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
        project = manager.GetCurrentProject()
        if project is None:
            return answer
        name = project.GetName()
        if not isinstance(name, str):
            raise _Unavailable(f"Project.GetName() returned {name!r}")
        return {**answer, "project": name, "uid": _call_text(project.GetUniqueId),
                **self._activity(project)}

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
        max_clips = _positive_int(message.get("max_clips"), DEFAULT_MAX_CLIPS)
        max_seconds = _positive_float(message.get("max_seconds"), DEFAULT_MAX_SECONDS)
        deadline = self._clock() + max_seconds
        project = self._project_manager().GetCurrentProject()
        if project is None:  # closed since the poll: the bridge's check after the walk sees it
            return {"ok": True, "paths": [], "clip_count": 0, "truncated": False}
        pool = project.GetMediaPool()
        root = pool.GetRootFolder() if pool is not None else None
        if root is None:
            raise _Unavailable("the media pool is not available")
        paths: list[str] = []
        clips = 0
        stack = [root]
        while stack:
            if self._clock() > deadline:
                return {"ok": True, "paths": paths, "clip_count": clips, "truncated": True}
            folder = stack.pop()
            for clip in _as_list(folder.GetClipList()):
                if clips >= max_clips or self._clock() > deadline:
                    return {"ok": True, "paths": paths, "clip_count": clips, "truncated": True}
                clips += 1
                path = _clip_file_path(clip)
                if path:
                    paths.append(path)
            stack.extend(reversed(_as_list(folder.GetSubFolderList())))
        return {"ok": True, "paths": paths, "clip_count": clips, "truncated": False}


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
