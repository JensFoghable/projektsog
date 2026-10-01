"""Scan worker process (SPEC §5.3): ``pythonw -m projektsog.scanworker [--db PATH]``.

Performs all scanning and all writes to ``entries``/``entries_fts`` at background priority, so
scans never compete with foreground work (Resolve playing footage).  JSON lines: commands on
stdin, events on stdout (ASCII JSON, one object per line)::

    → {"cmd": "scan", "job", "source_id", "root_path", "kind": "deep"|"shallow", "full",
       "first_time", "max_listings", "is_network", "fs", "expected_serial" (local sources)}
    → {"cmd": "cancel", "job"}   {"cmd": "cancel_source", "source_id"}
    → {"cmd": "forget", "job", "source_id"}   {"cmd": "config", "cfg": {...}}   {"cmd": "quit"}
    ← {"ev": "ready", "pid"}   {"ev": "progress", "job", "source_id", "entries", "dirs",
       "units_done", "units_total"}   {"ev": "committed", "job", "source_id", "changed"}
    ← {"ev": "done", "job", "source_id", "result"}   {"ev": "failed", "job", "source_id", "error"}

Every accepted job ends with exactly one ``done`` or ``failed``; a job cancelled before it
started ends with ``done`` whose result has ``aborted: true`` and ``counts: null``.
With ``expected_serial`` (SPEC §15.5) the serial of the volume holding ``root_path`` is checked
before the root listing and before every transaction; on a mismatch (another disk or card now
sits at that drive letter) or when it cannot be read, the job writes nothing more and ends with
``done`` whose result has ``ok: false, aborted: true, error: "Disken er skiftet",
volume_changed: true``.  Up to
``max_parallel_scans`` jobs run at once (one thread each, one shared writer connection under a
lock), never two for the same source.  The worker exits on ``quit`` or stdin EOF (parent gone),
cancelling running jobs, and logs to ``log_dir()\\scanworker.log``.
"""

from __future__ import annotations

import ctypes
import json
import logging
import logging.handlers
import os
import sqlite3
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, BinaryIO

from . import config, db, scanner

log = logging.getLogger(__name__)

Emit = Callable[[dict[str, Any]], None]

PROCESS_MODE_BACKGROUND_BEGIN = 0x00100000
THREAD_MODE_BACKGROUND_BEGIN = 0x00010000
# Thread already in background mode (400), or covered by the process mode (402).
_ALREADY_BACKGROUND = (400, 402)
_SEM_FAILCRITICALERRORS = 0x0001
_SEM_NOOPENFILEERRORBOX = 0x8000

CHECKPOINT_CHANGED_ROWS = 20_000     # checkpoint(TRUNCATE) after a job that changed more
VACUUM_DELETED_ROWS = 50_000         # incremental_vacuum after a job that deleted more
SHUTDOWN_TIMEOUT_S = 5.0

if sys.platform == "win32":
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.GetCurrentProcess.argtypes = []
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _kernel32.GetCurrentThread.argtypes = []
    _kernel32.GetCurrentThread.restype = wintypes.HANDLE
    _kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _kernel32.SetPriorityClass.restype = wintypes.BOOL
    _kernel32.SetThreadPriority.argtypes = [wintypes.HANDLE, ctypes.c_int]
    _kernel32.SetThreadPriority.restype = wintypes.BOOL
    _kernel32.SetErrorMode.argtypes = [wintypes.UINT]
    _kernel32.SetErrorMode.restype = wintypes.UINT
    _kernel32.GetVolumePathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    _kernel32.GetVolumePathNameW.restype = wintypes.BOOL
    _kernel32.GetVolumeInformationW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR,
        wintypes.DWORD]
    _kernel32.GetVolumeInformationW.restype = wintypes.BOOL
else:                                # keeps the module importable for tooling
    _kernel32 = None


def enter_background_mode() -> bool:
    """Lower CPU, I/O and memory priority of this process; no critical-error dialogs."""
    if _kernel32 is None:
        return False
    _kernel32.SetErrorMode(_SEM_FAILCRITICALERRORS | _SEM_NOOPENFILEERRORBOX)
    if _kernel32.SetPriorityClass(_kernel32.GetCurrentProcess(), PROCESS_MODE_BACKGROUND_BEGIN):
        return True
    log.warning("PROCESS_MODE_BACKGROUND_BEGIN failed: %s",
                ctypes.WinError(ctypes.get_last_error()))
    return False


def enter_thread_background_mode() -> None:
    """THREAD_MODE_BACKGROUND_BEGIN for the calling scan thread (SPEC §5.1)."""
    if _kernel32 is None:
        return
    if not _kernel32.SetThreadPriority(_kernel32.GetCurrentThread(),
                                       THREAD_MODE_BACKGROUND_BEGIN):
        err = ctypes.get_last_error()
        if err not in _ALREADY_BACKGROUND:
            log.debug("THREAD_MODE_BACKGROUND_BEGIN failed: %s", ctypes.WinError(err))


def volume_root(path: str) -> str | None:
    """``X:\\`` for a drive-letter path (the volume a ``vol:`` source key names), else the
    mount point from ``GetVolumePathNameW``; None when unknown."""
    p = path.replace("/", "\\")
    if p.startswith("\\\\?\\"):              # GetVolumePathNameW misreads \\?\UNC\ paths
        rest = p[4:]
        if rest[:4].upper() == "UNC\\":
            p = "\\\\" + rest[4:]
        elif rest[1:2] == ":":
            p = rest
    if len(p) >= 2 and p[1] == ":" and p[0].isalpha():
        return p[0].upper() + ":\\"
    if _kernel32 is None:
        return None
    buf = ctypes.create_unicode_buffer(1024)
    if not _kernel32.GetVolumePathNameW(p, buf, len(buf)):
        return None
    return buf.value


def volume_serial(path: str) -> str | None:
    """Serial number (``"5E3A0B21"``) of the volume holding ``path``; None if unreadable
    (medium removed, drive not ready …).  Never shows a "no disk" dialog: the process runs
    with ``SEM_FAILCRITICALERRORS`` (:func:`enter_background_mode`)."""
    root = volume_root(path)
    if root is None or _kernel32 is None:
        return None
    serial = wintypes.DWORD()
    if not _kernel32.GetVolumeInformationW(root, None, 0, ctypes.byref(serial), None, None,
                                           None, 0):
        log.info("cannot read the volume serial of %s: %s", root,
                 ctypes.WinError(ctypes.get_last_error()))
        return None
    return f"{serial.value:08X}"


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _not_run_result() -> dict[str, Any]:
    """Result for a job that was cancelled before it started."""
    return {"ok": False, "aborted": True, "error": None, "changed": 0, "deleted": 0,
            "units_done": 0, "units_total": 0, "entries": 0, "dirs": 0, "files": 0,
            "failed_dirs": 0, "reused_dirs": 0, "seconds": 0.0, "counts": None}


@dataclass(eq=False)
class _Job:
    job: int
    source_id: int
    kind: str                                   # "deep" | "shallow" | "forget"
    params: dict[str, Any] = field(default_factory=dict)
    cancel: threading.Event = field(default_factory=threading.Event)


class Worker:
    """Runs scan/forget jobs: at most ``max_parallel_scans`` at once, one per source."""

    def __init__(self, conn: sqlite3.Connection, emit: Emit, *,
                 cfg: Mapping[str, Any] | None = None,
                 lister: scanner.Lister | None = None,
                 serial_reader: Callable[[str], str | None] | None = None) -> None:
        self._conn = conn                  # writer, check_same_thread=False, used under _db_lock
        self._emit = emit
        self._lister = lister
        self._serial_reader = serial_reader or volume_serial
        self._db_lock = threading.Lock()
        self._lock = threading.Lock()      # guards the fields below
        self._cfg: dict[str, Any] = {**config.DEFAULTS, **(cfg or {})}
        self._pending: list[_Job] = []
        self._running: dict[_Job, threading.Thread] = {}
        self._closing = False

    # -- commands ---------------------------------------------------------------------
    def handle(self, msg: Mapping[str, Any]) -> bool:
        """Execute one command; returns False for ``quit``."""
        cmd = msg.get("cmd")
        if cmd == "scan":
            self._submit_scan(msg)
        elif cmd == "forget":
            self._submit_forget(msg)
        elif cmd == "cancel":
            job_id = msg.get("job")
            self._cancel(lambda j: j.job == job_id)
        elif cmd == "cancel_source":
            source_id = msg.get("source_id")
            self._cancel(lambda j: j.source_id == source_id)
        elif cmd == "config":
            self._configure(msg.get("cfg"))
        elif cmd == "quit":
            return False
        else:
            log.warning("unknown command %r", cmd)
        return True

    def shutdown(self, timeout: float = SHUTDOWN_TIMEOUT_S) -> bool:
        """Cancel everything and wait up to ``timeout``; True if no job thread is left."""
        with self._lock:
            self._closing = True
            dropped, self._pending = self._pending, []
            running = dict(self._running)
        for job in running:
            job.cancel.set()
        for job in dropped:
            self._emit_end(job, _not_run_result(), None)
        deadline = time.monotonic() + timeout
        for thread in running.values():
            thread.join(max(0.0, deadline - time.monotonic()))
        return not any(thread.is_alive() for thread in running.values())

    def _submit_scan(self, msg: Mapping[str, Any]) -> None:
        job_id, source_id = msg.get("job"), msg.get("source_id")
        kind, root = msg.get("kind", "deep"), msg.get("root_path")
        try:
            if not (_is_int(job_id) and _is_int(source_id)) or kind not in ("deep", "shallow"):
                raise ValueError("job, source_id and kind are required")
            if not isinstance(root, str):
                raise ValueError("root_path is required")
            scanner.long_path(root)                       # must be absolute
            params = {"root_path": root, "full": bool(msg.get("full", False)),
                      "first_time": bool(msg.get("first_time", False)),
                      "max_listings": int(msg.get("max_listings") or 2000),
                      "is_network": bool(msg.get("is_network", False)),
                      "fs": str(msg.get("fs") or ""),
                      "expected_serial": str(msg.get("expected_serial") or "").strip().upper()}
        except (TypeError, ValueError) as exc:
            log.warning("invalid scan command %r: %s", dict(msg), exc)
            self._emit({"ev": "failed", "job": job_id, "source_id": source_id,
                        "error": "Ugyldig scanningsopgave"})
            return
        self._enqueue(_Job(job_id, source_id, kind, params))

    def _submit_forget(self, msg: Mapping[str, Any]) -> None:
        job_id, source_id = msg.get("job"), msg.get("source_id")
        if not (_is_int(job_id) and _is_int(source_id)):
            log.warning("invalid forget command %r", dict(msg))
            self._emit({"ev": "failed", "job": job_id, "source_id": source_id,
                        "error": "Ugyldig opgave"})
            return
        self._cancel(lambda j: j.source_id == source_id)   # it runs after them (same source)
        self._enqueue(_Job(job_id, source_id, "forget"))

    def _configure(self, cfg: Any) -> None:
        if not isinstance(cfg, Mapping):
            log.warning("ignoring config command without a cfg object")
            return
        with self._lock:
            self._cfg = {**config.DEFAULTS, **cfg}
        self._dispatch()

    def _cancel(self, match: Callable[[_Job], bool]) -> None:
        with self._lock:
            dropped = [job for job in self._pending if match(job)]
            self._pending = [job for job in self._pending if not match(job)]
            for job in self._running:
                if match(job):
                    job.cancel.set()
        for job in dropped:
            self._emit_end(job, _not_run_result(), None)

    # -- scheduling -------------------------------------------------------------------
    def _enqueue(self, job: _Job) -> None:
        with self._lock:
            closing = self._closing
            if not closing:
                self._pending.append(job)
        if closing:
            self._emit_end(job, _not_run_result(), None)
        else:
            self._dispatch()

    def _dispatch(self) -> None:
        with self._lock:
            if self._closing:
                return
            try:
                limit = max(1, int(self._cfg.get("max_parallel_scans") or 1))
            except (TypeError, ValueError):
                limit = config.DEFAULTS["max_parallel_scans"]
            busy = {job.source_id for job in self._running}
            for job in list(self._pending):
                if len(self._running) >= limit:
                    break
                if job.source_id in busy:
                    continue
                self._pending.remove(job)
                busy.add(job.source_id)
                thread = threading.Thread(target=self._run, args=(job,), daemon=True,
                                          name=f"job-{job.job}-{job.kind}")
                self._running[job] = thread
                thread.start()

    def _run(self, job: _Job) -> None:
        enter_thread_background_mode()
        result: dict[str, Any] | None = None
        error: str | None = None
        try:
            result = self._forget(job) if job.kind == "forget" else self._scan(job)
        except Exception as exc:
            log.exception("job %s (%s, source %s) failed", job.job, job.kind, job.source_id)
            error = (f"Sletningen fejlede: {exc}" if job.kind == "forget"
                     else f"Scanningen fejlede: {exc}")
        if result is not None:
            try:
                self._maintain(job, result)
            except Exception:
                log.exception("database maintenance after job %s failed", job.job)
        with self._lock:
            self._running.pop(job, None)
        self._emit_end(job, result, error)
        self._dispatch()

    def _emit_end(self, job: _Job, result: dict[str, Any] | None, error: str | None) -> None:
        if result is not None:
            self._emit({"ev": "done", "job": job.job, "source_id": job.source_id,
                        "result": result})
        else:
            self._emit({"ev": "failed", "job": job.job, "source_id": job.source_id,
                        "error": error})

    # -- jobs -------------------------------------------------------------------------
    def _scan(self, job: _Job) -> dict[str, Any]:
        with self._lock:
            cfg = dict(self._cfg)
        p = job.params
        log.info("job %s: %s scan of source %s (%s)%s", job.job, job.kind, job.source_id,
                 p["root_path"], " full" if p["full"] else "")

        def progress(data: dict[str, int]) -> None:
            self._emit({"ev": "progress", "job": job.job, "source_id": job.source_id, **data})

        common: dict[str, Any] = {"cancel": job.cancel, "progress": progress,
                                  "on_commit": self._committed_callback(job),
                                  "lister": self._lister, "lock": self._db_lock,
                                  "verify": self._volume_check(job)}
        if job.kind == "deep":
            result = scanner.deep_scan(self._conn, job.source_id, p["root_path"], cfg,
                                       full=p["full"], is_network=p["is_network"], fs=p["fs"],
                                       **common)
        else:
            result = scanner.shallow_scan(self._conn, job.source_id, p["root_path"], cfg,
                                          first_time=p["first_time"],
                                          max_listings=p["max_listings"], fs=p["fs"], **common)
        log.info("job %s finished: ok=%s aborted=%s changed=%s entries=%s in %.1fs%s", job.job,
                 result["ok"], result["aborted"], result["changed"], result["entries"],
                 result["seconds"], f" ({result['error']})" if result["error"] else "")
        return result

    def _forget(self, job: _Job) -> dict[str, Any]:
        started = time.monotonic()
        deleted = db.delete_source_entries(self._conn, job.source_id, lock=self._db_lock,
                                           cancel=job.cancel)
        if deleted:
            self._committed_callback(job)(deleted)
        with self._db_lock:
            counts = db.source_counts(self._conn, job.source_id)
        aborted = counts["entry_count"] > 0
        log.info("job %s: forgot %d entries of source %s", job.job, deleted, job.source_id)
        return {"ok": not aborted, "aborted": aborted, "error": None, "changed": deleted,
                "deleted": deleted, "units_done": 0, "units_total": 0, "entries": 0,
                "dirs": 0, "files": 0, "failed_dirs": 0, "reused_dirs": 0,
                "seconds": round(time.monotonic() - started, 3), "counts": counts}

    def _volume_check(self, job: _Job) -> scanner.VerifyFn | None:
        """``verify()`` for the scanner: the root is still on the volume ``expected_serial``
        names (local sources; a drive letter can be taken by another disk or card)."""
        expected = job.params.get("expected_serial")
        if not expected:
            return None
        root, read = job.params["root_path"], self._serial_reader

        def verify() -> bool:
            serial = read(root)
            if serial is not None and serial.upper() == expected:
                return True
            log.warning("job %s: %s is on volume %s, expected %s", job.job, root,
                        serial or "(unreadable)", expected)
            return False

        return verify

    def _committed_callback(self, job: _Job) -> Callable[[int], None]:
        def committed(changed: int) -> None:
            self._emit({"ev": "committed", "job": job.job, "source_id": job.source_id,
                        "changed": changed})
        return committed

    def _maintain(self, job: _Job, result: Mapping[str, Any]) -> None:
        """SPEC §6: checkpoint after big jobs and forget; vacuum after big deletes."""
        if job.kind != "forget" and result.get("changed", 0) <= CHECKPOINT_CHANGED_ROWS:
            return
        if result.get("deleted", 0) > VACUUM_DELETED_ROWS:
            pages = db.incremental_vacuum(self._conn, lock=self._db_lock)
            log.info("incremental vacuum freed %d pages", pages)
        with self._db_lock:
            outcome = db.checkpoint(self._conn)
        log.info("wal checkpoint after job %s: %s", job.job, outcome)


class LineWriter:
    """Thread-safe JSON-lines event writer; drops events once the pipe is gone."""

    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream
        self._lock = threading.Lock()
        self.broken = False

    def __call__(self, event: dict[str, Any]) -> None:
        try:
            data = json.dumps(event, separators=(",", ":")).encode("ascii") + b"\n"
        except (TypeError, ValueError):
            log.exception("cannot serialise event %r", event.get("ev"))
            return
        with self._lock:
            if self.broken:
                return
            try:
                self._stream.write(data)
                self._stream.flush()
            except (OSError, ValueError) as exc:
                self.broken = True
                log.warning("event pipe closed (%s); dropping further events", exc)


def serve(stdin: BinaryIO, worker: Worker) -> None:
    """Dispatch commands until ``quit`` or end of input."""
    while True:
        raw = stdin.readline()
        if not raw:
            log.info("stdin closed")
            return
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            log.warning("ignoring malformed command %.200r", line)
            continue
        if not isinstance(msg, dict):
            log.warning("ignoring non-object command %.200r", line)
            continue
        try:
            if not worker.handle(msg):
                log.info("quit requested")
                return
        except Exception:
            log.exception("command %r failed", msg.get("cmd"))


def _parse_args(args: list[str]) -> str | None:
    """``--db PATH`` / ``--db=PATH`` (other arguments are ignored)."""
    db_path = None
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--db" and i + 1 < len(args):
            db_path = args[i + 1]
            i += 1
        elif arg.startswith("--db="):
            db_path = arg[5:]
        else:
            log.warning("ignoring argument %r", arg)
        i += 1
    return db_path


def _setup_logging() -> None:
    try:
        handler: logging.Handler = logging.handlers.RotatingFileHandler(
            os.path.join(config.log_dir(), "scanworker.log"), maxBytes=2 * 1024 * 1024,
            backupCount=2, encoding="utf-8")
    except OSError:
        handler = logging.NullHandler()
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)

    def thread_hook(args: threading.ExceptHookArgs) -> None:
        log.error("uncaught exception in thread %s", getattr(args.thread, "name", "?"),
                  exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    threading.excepthook = thread_hook
    sys.excepthook = lambda t, v, tb: log.critical("uncaught exception", exc_info=(t, v, tb))


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    db_path = _parse_args(list(sys.argv[1:] if argv is None else argv)) or config.db_path()
    stdin = getattr(sys.stdin, "buffer", None)
    stdout = getattr(sys.stdout, "buffer", None)
    if stdin is None or stdout is None:
        log.error("stdin/stdout are not connected – nothing to serve")
        return 2
    enter_background_mode()
    try:
        conn = db.connect(db_path, writer=True, check_same_thread=False)
        db.ensure_schema(conn)
    except (sqlite3.Error, OSError):
        log.exception("cannot open the index %s", db_path)
        return 1
    emit = LineWriter(stdout)
    worker = Worker(conn, emit)
    log.info("scan worker %d ready (index %s)", os.getpid(), db_path)
    emit({"ev": "ready", "pid": os.getpid()})
    try:
        serve(stdin, worker)
    finally:
        if worker.shutdown():
            conn.close()
        else:
            log.warning("jobs still blocked at exit (unresponsive drive or share?)")
        log.info("scan worker %d exiting", os.getpid())
    return 0


if __name__ == "__main__":
    exit_code = main()
    logging.shutdown()
    os._exit(exit_code)      # never wait for a job thread stuck on a dead share
