"""DaVinci Resolve integration (SPEC §9, §15.9): which project is open, and where its media lives.

* Every Resolve scripting call runs in a helper process (``pythonw -m projektsog.resolve_child``,
  JSON lines): the main process never loads Resolve's scripting library, which CPython could
  never unload again and which would keep Resolve's files in use during Resolve updates. ONE
  dedicated thread ("resolve-bridge") talks to the helper. It starts the helper only while
  ``Resolve.exe`` runs and has been running for at least 15 s (Resolve may fail scripts while
  it starts), stops it as soon as Resolve exits, the integration is switched off or the bridge
  stops, and restarts it with a back-off when it crashes or hangs. It polls the current project
  + database every ``resolve_poll_s`` seconds.
* On a project change, on ``refresh()`` and on ``on_window_shown()`` (last walk > 30 s old) the
  media pool is walked (bounded, inside the helper), the clip paths are mapped to indexed project
  folders with ``Indexer.map_paths()`` (without any match: ``suggest_project_folders()`` by
  project name) and the result is published as a ``resolve`` event.
* Every poll re-reads the Indexer's live registry. When a location goes online or offline,
  moves (another drive letter) or is added/forgotten, or a location the mapping depends on
  finishes a deep or shallow scan (``last_scan_end``/``last_shallow_scan``, SPEC §15.12) or
  gets a root project, the last walk's clip paths are mapped again - without a media pool
  walk, at most every 5 s. A mapping that falls back on name suggestions depends on every
  location (a new project folder can appear anywhere).
* ``open_primary()`` serves the Resolve menu script: it checks that the request is about the
  project the state describes and opens the primary folder at its live location after a
  timed stat. (The script itself first checks that the state maps its current media pool.)
* Follow mode (``resolve_follow``) notifies and optionally opens the folder once the user has
  settled on a project.

Published state dicts are never mutated after publication (a new dict is built for every change).
"""

from __future__ import annotations

import json
import logging
import ntpath
import os
import queue
import re
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from .config import DEFAULTS, VALID_RESOLVE_FOLLOW, Config, app_dir
from .events import EventBus

if TYPE_CHECKING:
    from .indexer import Indexer

log = logging.getLogger(__name__)

RESOLVE_EXE = "Resolve.exe"
CHILD_MODULE = "projektsog.resolve_child"

STARTUP_GRACE_S = 15.0        # Resolve may fail scripts while it is still starting
CONNECT_RETRY_S = 15.0        # back-off after a refused or failed connection attempt
MIN_POLL_S = 1.0
WALK_MAX_SECONDS = 20.0
WALK_MAX_CLIPS = 50_000
SLOW_WALK_S = 2.0             # walks slower than this are logged at INFO
REWALK_ON_SHOW_S = 30.0
REFRESH_WAIT_S = 10.0
REMAP_MIN_INTERVAL_S = 5.0    # re-maps of the last walk's paths after registry changes
FOLLOW_STABLE_S = 5.0
FOLLOW_REPEAT_S = 30 * 60.0
SUGGESTION_MIN_SCORE = 0.6
MAX_OTHER_DIRS = 200          # keeps the state (and every SSE event carrying it) small
STOP_JOIN_S = 2.0
STAT_TIMEOUT_S = 3.0          # like Controller.open_path (SPEC §11)

# The helper process.
CHILD_READY_TIMEOUT_S = 10.0
CHILD_CONNECT_TIMEOUT_S = 15.0
CHILD_CALL_TIMEOUT_S = 10.0   # poll / uid
CHILD_WALK_MARGIN_S = 10.0    # walk: WALK_MAX_SECONDS + this
CHILD_STOP_TIMEOUT_S = 0.5    # then it is killed
CHILD_BACKOFF_S = (1.0, 2.0, 5.0, 15.0, 30.0, 60.0)   # restart delays after failures in a row
CHILD_STABLE_S = 60.0         # a helper that ran this long before failing resets the back-off
_CREATE_NO_WINDOW = 0x08000000
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ERROR_FILE_NOT_FOUND = 2
_ERROR_PATH_NOT_FOUND = 3

_CFG_KEYS = ("resolve_enabled", "resolve_poll_s", "resolve_follow")
_UNTITLED_RE = re.compile(r"untitled project(?: \d+)?", re.IGNORECASE)

# User-facing texts (Danish).
ERR_EXTERNAL_SCRIPTING = ("Slå ekstern scripting til i DaVinci Resolve: Preferences ▸ System ▸ "
                          "General ▸ External scripting using = Local")
ERR_MODULE = ("DaVinci Resolves scripting-modul kunne ikke indlæses – er DaVinci Resolve "
              "installeret korrekt?")
ERR_NO_RESPONSE = "DaVinci Resolve svarer ikke på scripting-kald – prøver igen"
ERR_HELPER = "Hjælpeprocessen til DaVinci Resolve kunne ikke startes – prøver igen"
ERR_INTERNAL = "Intern fejl i DaVinci Resolve-integrationen – se loggen"
ERR_DISABLED = "DaVinci Resolve-integrationen er slået fra i indstillingerne"
ERR_NOT_RUNNING = "DaVinci Resolve kører ikke"
ERR_NOT_CONNECTED = "Projektsøg har endnu ikke forbindelse til DaVinci Resolve"
ERR_NO_PROJECT = "Der er ikke åbnet et projekt i DaVinci Resolve"
ERR_OPEN_FAILED = "Mappen kunne ikke åbnes"
ERR_NOT_RESPONDING = "Placeringen svarer ikke"
ERR_MISSING = "Findes ikke længere – indekset opdateres"
ERR_GONE = "Mappen findes ikke længere"   # offline, although its disk/computer is there (§15.12)


class ChildError(Exception):
    """The helper process died, hung, was stopped or could not be started."""


class _ResolveUnavailable(Exception):
    """Resolve stopped answering scripting calls (quit, restarting or busy)."""


@dataclass(frozen=True)
class _Snapshot:
    """What one poll saw: project identity (database + name + unique id) and display names."""

    key: tuple[str, ...]
    name: str | None
    database: str | None
    uid: str = ""                         # Project.GetUniqueId() ("" unknown or not trusted)


@dataclass
class _Walk:
    """The last media pool walk of the current project (re-mapped when the registry changes)."""

    name: str | None
    paths: list[str]
    updated: float                        # wall clock of the walk (state["updated"])
    rows: dict[Any, tuple] | None         # registry rows the current mapping was made from
    mapped_at: float                      # clock of the last walk or re-map
    depends_on: frozenset[Any] | None = None   # sources whose scans matter (None: all)


# --------------------------------------------------------------------------------------
# The helper process (main-process side)
# --------------------------------------------------------------------------------------

def default_child_argv() -> list[str]:
    """``[pythonw.exe next to sys.executable (else sys.executable), -m, CHILD_MODULE]``."""
    exe = sys.executable
    pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return [pythonw if os.path.isfile(pythonw) else exe, "-m", CHILD_MODULE]


class _ChildProcess:
    """One running helper: JSON-line requests and answers (see projektsog.resolve_child).

    ``request()`` is called by the Resolve thread only; ``close()`` may be called from any
    thread (``ResolveBridge.stop()``) and makes a pending ``request()`` fail promptly.
    """

    def __init__(self, argv: list[str]) -> None:
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(p for p in (_REPO_ROOT, env.get("PYTHONPATH")) if p)
        self._proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, cwd=app_dir(), env=env,
                                      creationflags=_CREATE_NO_WINDOW, close_fds=True)
        self.pid = self._proc.pid
        self._answers: queue.SimpleQueue[dict[str, Any] | None] = queue.SimpleQueue()
        self._write_lock = threading.Lock()
        self._closed = False
        self._next_id = 0
        threading.Thread(target=self._read, name=f"resolve-child-{self.pid}-out",
                         daemon=True).start()

    def _read(self) -> None:
        stdout = self._proc.stdout
        try:
            for raw in stdout:
                try:
                    message = json.loads(raw)
                except ValueError:
                    log.warning("The Resolve helper sent a malformed line: %.200r", raw)
                    continue
                if isinstance(message, dict):
                    self._answers.put(message)
        except (OSError, ValueError):
            log.debug("Resolve helper output closed", exc_info=True)
        finally:
            try:
                stdout.close()
            except OSError:
                pass
            try:                           # its pipe closes just before the process ends
                self._proc.wait(2.0)
            except subprocess.TimeoutExpired:
                pass
            self._answers.put(None)        # end of output: the helper is gone

    def alive(self) -> bool:
        return self._proc.poll() is None

    def wait_ready(self, timeout: float) -> None:
        message = self._next(time.monotonic() + timeout, "ready")
        if message.get("ev") != "ready":
            raise ChildError(f"unexpected first message {message!r:.200}")

    def request(self, cmd: str, timeout: float, **params: Any) -> dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        data = (json.dumps({"id": request_id, "cmd": cmd, **params}) + "\n").encode("ascii")
        with self._write_lock:
            if self._closed:
                raise ChildError("the helper was stopped")
            try:
                self._proc.stdin.write(data)
                self._proc.stdin.flush()
            except (OSError, ValueError) as exc:
                raise ChildError(f"writing to the helper failed: {exc}") from None
        deadline = time.monotonic() + timeout
        while True:
            message = self._next(deadline, cmd)
            if message.get("id") == request_id:
                return message
            # an answer to an earlier, abandoned request: skip it

    def _next(self, deadline: float, what: str) -> dict[str, Any]:
        try:
            message = self._answers.get(timeout=max(0.001, deadline - time.monotonic()))
        except queue.Empty:
            raise ChildError(f"no answer to {what!r} in time") from None
        if message is None:
            self._answers.put(None)        # stays gone for every later call
            raise ChildError(f"the helper exited (exit code {self._proc.poll()})")
        return message

    def close(self, timeout: float = CHILD_STOP_TIMEOUT_S) -> None:
        """``quit`` + end of stdin (the helper exits at once on EOF); kill it if it lingers."""
        with self._write_lock:
            if not self._closed:
                self._closed = True
                try:
                    self._proc.stdin.write(b'{"cmd": "quit"}\n')
                    self._proc.stdin.flush()
                except (OSError, ValueError):
                    pass
                try:
                    self._proc.stdin.close()
                except (OSError, ValueError):
                    pass
        try:
            self._proc.wait(timeout)
        except subprocess.TimeoutExpired:
            log.warning("The Resolve helper %s did not exit - killing it", self.pid)
            self._proc.kill()
            try:
                self._proc.wait(1.0)
            except subprocess.TimeoutExpired:
                log.error("The Resolve helper %s could not be killed", self.pid)


class _InProcessHelper:
    """The helper's request handling run in this process - the ``connect`` test seam, whose
    object stands in for Resolve (so nothing of Resolve is loaded here either). Requests and
    answers make the same JSON round trip as on the pipe."""

    def __init__(self, connect: Callable[[], Any], clock: Callable[[], float]) -> None:
        from .resolve_child import Session  # noqa: PLC0415 - own module; loads nothing of Resolve

        self._session = Session(connect=connect, clock=clock)
        self._closed = False
        self._next_id = 0
        self.pid = os.getpid()

    def wait_ready(self, timeout: float) -> None:
        return None

    def request(self, cmd: str, timeout: float, **params: Any) -> dict[str, Any]:
        if self._closed:
            raise ChildError("the helper was stopped")
        self._next_id += 1
        message = json.loads(json.dumps({"id": self._next_id, "cmd": cmd, **params}))
        return json.loads(json.dumps(self._session.handle(message)))

    def alive(self) -> bool:
        return not self._closed

    def close(self, timeout: float = CHILD_STOP_TIMEOUT_S) -> None:
        self._closed = True


# --------------------------------------------------------------------------------------
# Pure helpers (state shapes, mapping, texts)
# --------------------------------------------------------------------------------------

def _idle_state(enabled: bool, running: bool, error: str | None = None, *,
                connected: bool = False) -> dict[str, Any]:
    return {"enabled": enabled, "running": running, "connected": connected, "error": error,
            "project": None, "database": None, "clip_count": 0, "updated": None,
            "folders": [], "other_dirs": [], "suggestions": [], "primary": None,
            "offline_clips": 0, "offline_disks": []}


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _is_untitled(name: str) -> bool:
    return bool(_UNTITLED_RE.fullmatch(name.strip()))


def _count(entry: dict[str, Any]) -> int:
    try:
        return int(entry.get("count") or 0)
    except (TypeError, ValueError):
        return 0


def _format_count(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def _join_da(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " og " + items[-1]


def _disk_label(source: dict[str, Any]) -> str:
    """Disk name of a SourceRef or a Source (which has display_name instead of name)."""
    return (source.get("disk_name") or source.get("volume_label") or source.get("name")
            or source.get("display_name") or "ukendt disk")


def _item_path(item: dict[str, Any]) -> str | None:
    return item.get("open_path") or item.get("path")


def _is_online(item: dict[str, Any]) -> bool:
    return bool((item.get("source") or {}).get("online"))


def _folder_item(entry: dict[str, Any]) -> dict[str, Any]:
    """The Item of a map_paths/suggestion entry; built from its ProjectRef if it has none."""
    item = entry.get("item")
    if isinstance(item, dict):
        return dict(item)
    project = entry.get("project") or {}
    path = project.get("path")
    rel = project.get("rel_path") or ""
    return {"id": None, "kind": "project", "name": project.get("name") or ntpath.basename(path or ""),
            "hl": [], "path": path, "open_path": path, "unc_path": project.get("unc_path"),
            "rel_path": rel, "parent": ntpath.dirname(rel), "depth": len(rel.split("\\")) if rel else 0,
            "source": entry.get("source") or {}, "project": project, "size": None, "mtime": None,
            "file_count": None, "ext": None, "is_seq": False, "seq_count": None,
            "subfolders": None, "score": None}


def _choose_primary(folders: list[dict[str, Any]],
                    suggestions: list[dict[str, Any]]) -> dict[str, Any] | None:
    """folders[0] (media match), else the best suggestion scoring ≥ 0.6 (name match)."""
    if folders:
        return {**_folder_item(folders[0]), "match": "media"}
    if suggestions and float(suggestions[0].get("score") or 0.0) >= SUGGESTION_MIN_SCORE:
        return {**_folder_item(suggestions[0]), "match": "name"}
    return None


def _folder_gone(entry: dict[str, Any]) -> bool:
    """An offline folder whose disk is mounted or whose computer answers (``volume_present``,
    SPEC §15.12): the folder itself was moved, renamed or deleted - no disk to connect, no
    computer to switch on."""
    return not entry.get("online") and bool((entry.get("source") or {}).get("volume_present"))


def _offline_summary(folders: list[dict[str, Any]],
                     other_dirs: list[dict[str, Any]]) -> tuple[int, list[str]]:
    """(clips on offline locations, names of the offline local disks by clip count); a folder
    gone from a disk that is mounted counts its clips but names no disk (SPEC §15.12)."""
    clips = 0
    disks: dict[str, int] = {}
    for entry in folders:
        if not entry.get("online"):
            n = _count(entry)
            clips += n
            source = entry.get("source") or {}
            if source.get("kind") == "local" and not _folder_gone(entry):
                label = _disk_label(source)
                disks[label] = disks.get(label, 0) + n
    clips += sum(_count(d) for d in other_dirs if d.get("online") is False)
    return clips, sorted(disks, key=lambda k: -disks[k])


def _offline_hosts(folders: list[dict[str, Any]]) -> list[str]:
    """Computers that do not answer, holding offline shares, by clip count."""
    hosts: dict[str, int] = {}
    for entry in folders:
        source = entry.get("source") or {}
        if (not entry.get("online") and source.get("kind") == "share" and source.get("host")
                and not _folder_gone(entry)):
            hosts[source["host"]] = hosts.get(source["host"], 0) + _count(entry)
    return sorted(hosts, key=lambda k: -hosts[k])


def _gone_folders(folders: list[dict[str, Any]]) -> list[tuple[str, int]]:
    """(location name, clips) of the folders gone from a disk/computer that is there (SPEC
    §15.12), per location, most clips first - grouped and named like the UI's Resolve bar."""
    groups: dict[Any, list[Any]] = {}
    for entry in folders:
        n = _count(entry)
        if n <= 0 or not _folder_gone(entry):
            continue
        source = entry.get("source") or {}
        host = source.get("host") if source.get("kind") == "share" else None
        name = source.get("name") or host or _disk_label(source)
        sid = source.get("id")
        group = groups.setdefault(name if sid is None else sid, [name, 0])
        group[1] += n
    return sorted(((name, n) for name, n in groups.values()), key=lambda g: -g[1])


def _offline_hint(state: dict[str, Any]) -> str | None:
    """Why clips cannot be reached, worded like the UI's Resolve bar (SPEC §12, §15.12): folders
    gone from a disk or computer that is there ("… i mappen ‘X’, som ikke findes længere") and
    disks that are not connected / computers that do not answer - at most two lines, most clips
    first (the tray fits the text into 256 characters)."""
    n = state["offline_clips"]
    if n <= 0:
        return None
    gone = _gone_folders(state["folders"])
    gone_clips = sum(c for _name, c in gone)
    lines: list[tuple[int, str]] = []
    if gone:
        where = (f"mappen ‘{gone[0][0]}’" if len(gone) == 1
                 else "mapperne " + _join_da([f"‘{name}’" for name, _c in gone]))
        lines.append((gone_clips, f"{_format_count(gone_clips)} klip ligger i {where}, "
                                  "som ikke findes længere"))
    if n > gone_clips:
        lines.append((n - gone_clips, _unreachable_hint(n - gone_clips, state)))
    lines.sort(key=lambda line: -line[0])
    return "\n".join(text for _n, text in lines)


def _unreachable_hint(n: int, state: dict[str, Any]) -> str:
    """The line for ``n`` clips on disks that are not connected, computers that do not answer
    or other locations that cannot be reached."""
    disks = state["offline_disks"]
    hosts = _offline_hosts(state["folders"])
    lead = f"{_format_count(n)} klip ligger på"
    if disks and not hosts:
        where = (f"disken ‘{disks[0]}’" if len(disks) == 1
                 else "diskene " + _join_da([f"‘{d}’" for d in disks]))
        return f"{lead} {where}, som ikke er tilsluttet"
    if hosts and not disks:
        return f"{lead} {_join_da(hosts)}, som ikke svarer"
    places = [f"‘{d}’" for d in disks] + hosts
    detail = f" ({', '.join(places)})" if places else ""
    return f"{lead} placeringer, som ikke er tilgængelige{detail}"


def _follow_message(state: dict[str, Any]) -> dict[str, Any] | None:
    lines = []
    primary = state["primary"]
    if primary:
        label = "Projektmappe" if primary.get("match") == "media" else "Muligt match"
        lines.append(f"{label}: {primary.get('name')}")
    hint = _offline_hint(state)
    if hint:
        lines.append(hint)
    if not lines:
        return None
    return {"title": f"DaVinci Resolve: {state['project']}", "text": "\n".join(lines),
            "level": "warn" if hint else "info"}


def _offline_refusal(source: dict[str, Any]) -> str:
    """Same hints as Controller.open_path (SPEC §11, §15.12); ``source`` is a SourceRef or a
    Source of an offline location."""
    if source.get("volume_present"):      # the disk/computer is there, the folder is not
        return ERR_GONE
    if source.get("kind") == "share":
        return f"Computeren {source.get('host') or '?'} svarer ikke – er den tændt?"
    return f"Tilslut disken ‘{_disk_label(source)}’"


def _finding_reason(project: str | None) -> str:
    return (f"Projektmappen for ‘{project or '?'}’ er ved at blive fundet – "
            "prøv igen om et øjeblik")


def _no_primary_reason(state: dict[str, Any]) -> str:
    if not state["enabled"]:
        return ERR_DISABLED
    if not state["running"]:
        return ERR_NOT_RUNNING
    if not state["connected"]:
        return state["error"] or ERR_NOT_CONNECTED
    if not state["project"]:
        return ERR_NO_PROJECT
    if state["updated"] is None:
        return _finding_reason(state["project"])
    return f"Ingen projektmappe fundet for ‘{state['project']}’"


def _name_match_reason(state: dict[str, Any], primary: dict[str, Any]) -> str:
    return (f"Ingen af klippene i ‘{state['project']}’ ligger i en kendt projektmappe "
            f"(muligt match: ‘{primary.get('name')}’)")


def _within(path: str, root: str) -> bool:
    """Is casefolded ``path`` the casefolded folder ``root`` or inside it?"""
    root = root.rstrip("\\")
    return bool(root) and (path == root or path.startswith(root + "\\"))


def _source_row(source: dict[str, Any]) -> tuple:
    """A Source (SPEC §7.1, §15.3, §15.12) as a comparable registry row: ``(online, path,
    unc_path, included, content)``; ``content`` changes when a scan of the source finishes -
    deep (``last_scan_end``) or shallow (``last_shallow_scan``, e.g. the round after the window
    is shown, or the first scan of a new disk) - or its root becomes/stops being a project or its
    disk/computer appears or disappears (``volume_present``, part of its SourceRefs)."""
    return (bool(source.get("online")), source.get("path"), source.get("unc_path"),
            bool(source.get("included")),
            (source.get("last_scan_end"), source.get("last_shallow_scan"),
             bool(source.get("root_is_project")), bool(source.get("volume_present"))))


def _mapping_sources(folders: list[dict[str, Any]], other_dirs: list[dict[str, Any]],
                     rows: dict[Any, tuple]) -> frozenset[Any]:
    """Sources a media mapping depends on: those of its folders (the primary is one of them),
    and those holding one of its other (not project) folders - all of them, also those the
    published state leaves out (MAX_OTHER_DIRS). A folder in no known location (``online``
    None) depends on no source: a new location changes the registry's keys anyway."""
    ids = {(entry.get("source") or {}).get("id") for entry in folders if isinstance(entry, dict)}
    dirs = {d["path"].casefold() for d in other_dirs
            if isinstance(d, dict) and isinstance(d.get("path"), str)
            and d.get("online") is not None}
    if dirs:
        for sid, row in rows.items():
            roots = [r.casefold() for r in (row[1], row[2]) if isinstance(r, str) and r]
            if any(_within(d, r) for r in roots for d in dirs):
                ids.add(sid)
    ids.discard(None)
    return frozenset(ids)


def _registry_changed(before: dict[Any, tuple], after: dict[Any, tuple],
                      depends_on: frozenset[Any] | None) -> bool:
    """Did the registry change in a way that can change the mapping made from ``before``?

    Any source added or forgotten, going online/offline, moving or changing its inclusion
    counts; a change of a source's content (see :func:`_source_row`) only for the sources the
    mapping depends on (``depends_on``; None: every source - name suggestions may come from
    any location).
    """
    if before.keys() != after.keys():
        return True
    if any(before[sid][:4] != row[:4] for sid, row in after.items()):
        return True
    ids = after.keys() if depends_on is None else [sid for sid in depends_on if sid in after]
    return any(before[sid][4] != after[sid][4] for sid in ids)


def _is_missing_error(exc: OSError) -> bool:
    """True when the path itself is gone (Python also maps unreachable hosts/shares and
    missing drives to FileNotFoundError)."""
    if not isinstance(exc, FileNotFoundError):
        return False
    winerror = getattr(exc, "winerror", None)
    return winerror is None or winerror in (_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND)


def _extended_path(path: str) -> str:
    """\\\\?\\ form for paths beyond MAX_PATH, which os.stat() could not reach otherwise."""
    if len(path) < 248 or path.startswith("\\\\?\\"):
        return path
    path = ntpath.normpath(path)
    return "\\\\?\\UNC\\" + path[2:] if path.startswith("\\\\") else "\\\\?\\" + path


def _probe_folder(path: str) -> str:
    """Runs on a call_with_timeout thread: 'dir' | 'file' | 'missing' | 'error'."""
    try:
        st = os.stat(_extended_path(path))
    except OSError as exc:
        # "Missing" only when the drive/share itself answers; otherwise it is unreachable.
        anchor = ntpath.splitdrive(path)[0]
        if _is_missing_error(exc) and anchor and os.path.isdir(anchor + "\\"):
            return "missing"
        log.debug("stat(%s) failed: %s", path, exc)
        return "error"
    return "dir" if stat.S_ISDIR(st.st_mode) else "file"


# --------------------------------------------------------------------------------------
# The bridge
# --------------------------------------------------------------------------------------

class ResolveBridge:
    """Tracks the project open in DaVinci Resolve and maps its media to project folders.

    Beyond SPEC §9 the constructor accepts ``call_with_timeout`` (default
    ``winfs.call_with_timeout``, imported lazily) and test seams: ``spawn_child`` (returns a
    started helper with ``pid``, ``wait_ready(timeout)``, ``request(cmd, timeout, **params)``,
    ``alive()`` and ``close(timeout)``; failures raise :class:`ChildError`; default: the real
    ``projektsog.resolve_child`` process), ``connect`` (instead of ``spawn_child``: run the
    helper's request handling in this process with this factory of a stand-in scripting
    object), ``clock`` (monotonic seconds) and ``wall_clock`` (epoch seconds for ``updated``).
    """

    def __init__(self, cfg: Config, bus: EventBus, indexer: "Indexer", *,
                 process_running: Callable[[str], bool] | None = None,
                 process_uptime: Callable[[str], float | None] | None = None,
                 open_folder: Callable[..., bool] | None = None,
                 explorer_window_for: Callable[[str], int | None] | None = None,
                 call_with_timeout: Callable[[str, Callable[[], Any], float],
                                             tuple[str, Any]] | None = None,
                 spawn_child: Callable[[], Any] | None = None,
                 connect: Callable[[], Any] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], float] = time.time) -> None:
        self._cfg = cfg
        self._bus = bus
        self._indexer = indexer
        # winui functions: injected, or imported lazily from projektsog.winui (SPEC §1.1).
        self._funcs: dict[str, Callable[..., Any]] = {
            name: fn for name, fn in (("process_running", process_running),
                                      ("process_uptime", process_uptime),
                                      ("open_folder", open_folder),
                                      ("explorer_window_for", explorer_window_for))
            if fn is not None}
        self._call_with_timeout = call_with_timeout
        if spawn_child is None:
            spawn_child = ((lambda: _InProcessHelper(connect, clock)) if connect is not None
                           else (lambda: _ChildProcess(default_child_argv())))
        self._spawn_child = spawn_child
        self._clock = clock
        self._wall = wall_clock

        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state: dict[str, Any] = _idle_state(bool(cfg.get("resolve_enabled")), False)
        self._state_uid = ""                  # unique id of the state's project (with _state)
        self._refresh_requested = 0
        self._refresh_completed = 0
        self._window_shown = False
        self._failed_funcs: set[str] = set()
        self._child: Any = None               # guarded by _lock: stop() may close it

        # Resolve-thread-only state.
        self._connected = False
        self._child_started: float | None = None
        self._child_failures = 0
        self._child_retry_at = 0.0
        self._first_seen_running: float | None = None
        self._retry_at = 0.0
        self._connect_error: str | None = None
        self._project_key: tuple[str, ...] | None = None
        self._project_since = 0.0
        self._last_walk: float | None = None
        self._walk: _Walk | None = None
        self._follow_pending = False
        self._followed: dict[tuple[str, ...], float] = {}
        self._uid_unreliable = False
        self._registry_failed = False

        self._cfg_seen = tuple(cfg.get(k) for k in _CFG_KEYS)
        cfg.on_change(self._on_config_change)

    # -- public API ------------------------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="resolve-bridge", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        with self._cond:
            self._refresh_completed = self._refresh_requested
            self._cond.notify_all()
        child = self._detach_child()   # also ends a request the Resolve thread is waiting on
        if child is not None:
            child.close(CHILD_STOP_TIMEOUT_S)
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(STOP_JOIN_S)
            if thread.is_alive():
                log.warning("Resolve thread did not stop within %.0f s", STOP_JOIN_S)

    def state(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def refresh(self, wait: bool = True) -> dict[str, Any]:
        """Queue a media pool re-walk; with ``wait`` block ≤ 10 s for it. Returns state()."""
        with self._cond:
            self._refresh_requested += 1
            target = self._refresh_requested
        self._wake.set()
        if wait:
            deadline = time.monotonic() + REFRESH_WAIT_S
            with self._cond:
                while self._refresh_completed < target and self._thread_alive():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._cond.wait(remaining)
        return self.state()

    def on_window_shown(self) -> None:
        self._request_stale_rewalk()

    def open_primary(self, project: str | None = None, database: str | None = None,
                     uid: str | None = None) -> dict[str, Any]:
        """Open the primary folder in Explorer (activated) - the Resolve menu script's action.

        ``project``/``database`` are what the script sees in Resolve (SPEC §15.9); ``uid``
        (Project.GetUniqueId(), optional) tells same-named projects apart and is compared when
        the bridge trusts the id of the state's project too. When they differ from the state,
        nothing is opened, ``path`` is None (the script then finds the folder from the live
        media pool itself) and a re-walk is queued. A name match ("Muligt match") is never
        opened here either. The folder is opened at its source's live location, refused with
        the disk/host hint while that source is offline, and stat'ed with a timeout first (like
        Controller.open_path).
        """
        for value, what in ((project, "projektnavn"), (database, "databasenavn"),
                            (uid, "projekt-id")):
            if value is not None and not isinstance(value, str):
                raise ValueError(f"Ugyldigt {what}")
        with self._lock:
            state, state_uid = dict(self._state), self._state_uid
        if ((project is not None and project != state["project"])
                or (database is not None and database != state["database"])
                or (uid and state_uid and uid != state_uid)):
            log.info("Menu script asks for %r (database %r, id %r) but the state describes "
                     "%r (%r, id %r)", project, database, uid, state["project"],
                     state["database"], state_uid)
            self.refresh(wait=False)
            return {"ok": False, "path": None,
                    "error": _finding_reason(project if project is not None
                                             else state["project"])}
        self._request_stale_rewalk()   # clips may have been imported since the last walk
        primary = state["primary"]
        if primary is None:
            return {"ok": False, "path": None, "error": _no_primary_reason(state)}
        if primary.get("match") != "media":
            return {"ok": False, "path": None, "error": _name_match_reason(state, primary)}
        source, path, online = self._live_location(primary)
        if not path:
            return {"ok": False, "path": None, "error": ERR_OPEN_FAILED}
        if not online:
            return {"ok": False, "path": path, "error": _offline_refusal(source)}
        status = self._probe(path, source)
        if status == "missing":
            try:
                self._indexer.path_missing(path)
            except Exception:
                log.exception("Indexer.path_missing(%s) failed", path)
            return {"ok": False, "path": path, "error": ERR_MISSING}
        if status != "dir":
            return {"ok": False, "path": path,
                    "error": ERR_OPEN_FAILED if status == "file" else ERR_NOT_RESPONDING}
        try:
            opened = bool(self._fn("open_folder")(path, activate=True))
        except Exception:
            log.exception("Opening %s failed", path)
            opened = False
        return {"ok": opened, "path": path, "error": None if opened else ERR_OPEN_FAILED}

    # -- cross-thread plumbing ---------------------------------------------------------------
    def _thread_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and not self._stop.is_set()

    def _on_config_change(self, snapshot: dict[str, Any]) -> None:
        seen = tuple(snapshot.get(k) for k in _CFG_KEYS)
        if seen != self._cfg_seen:
            self._cfg_seen = seen
            self._wake.set()

    def _request_stale_rewalk(self) -> None:
        """Re-walk the media pool soon if the last walk is > 30 s old."""
        with self._lock:
            self._window_shown = True
        self._wake.set()

    def _fn(self, name: str) -> Callable[..., Any]:
        fn = self._funcs.get(name)
        if fn is None:
            from . import winui  # noqa: PLC0415 - owned by the winui agent (SPEC §1.1)

            fn = self._funcs[name] = getattr(winui, name)
        return fn

    def _timed_call(self, key: str, fn: Callable[[], Any], timeout: float) -> tuple[str, Any]:
        call = self._call_with_timeout
        if call is None:
            from . import winfs  # noqa: PLC0415 - owned by the index-core agent (SPEC §1.1)

            call = self._call_with_timeout = winfs.call_with_timeout
        return call(key, fn, timeout)

    def _call_quietly(self, name: str, *args: Any) -> Any:
        """Call a winui function; failures count as 'no' and are logged in full only once."""
        try:
            result = self._fn(name)(*args)
        except Exception:
            level = logging.DEBUG if name in self._failed_funcs else logging.WARNING
            log.log(level, "%s(%s) failed", name, ", ".join(map(repr, args)), exc_info=True)
            self._failed_funcs.add(name)
            return None
        self._failed_funcs.discard(name)
        return result

    def _take_requests(self) -> tuple[int, bool, bool]:
        with self._cond:
            shown, self._window_shown = self._window_shown, False
            return (self._refresh_requested,
                    self._refresh_requested > self._refresh_completed, shown)

    def _complete_refresh(self, target: int) -> None:
        with self._cond:
            if target > self._refresh_completed:
                self._refresh_completed = target
                self._cond.notify_all()

    def _set_state(self, state: dict[str, Any], uid: str = "") -> None:
        """Publish ``state``; ``uid`` is the unique id of its project ("" unknown / none)."""
        with self._lock:
            self._state_uid = uid
            if state == self._state:
                return
            self._state = state
        self._bus.publish("resolve", state)

    def _settings(self) -> tuple[bool, float, str]:
        enabled = bool(self._cfg.get("resolve_enabled"))
        try:
            poll = float(self._cfg.get("resolve_poll_s"))
        except (TypeError, ValueError):
            poll = float(DEFAULTS["resolve_poll_s"])
        follow = self._cfg.get("resolve_follow")
        return enabled, max(MIN_POLL_S, poll), follow if follow in VALID_RESOLVE_FOLLOW else "off"

    # -- live locations (any thread) -------------------------------------------------------
    def _live_location(self, item: dict[str, Any]) -> tuple[dict[str, Any], str | None, bool]:
        """(source, path, online) of a mapped folder from the Indexer's live registry: the
        source's current online state and path (another drive letter after a re-plug). Falls
        back to the item's own values when the registry does not know the source."""
        cached = item.get("source") or {}
        sid = cached.get("id")
        list_sources = getattr(self._indexer, "list_sources", None)
        if sid is not None and list_sources is not None:
            try:
                live = next((s for s in list_sources() or ()
                             if isinstance(s, dict) and s.get("id") == sid), None)
            except Exception:
                log.warning("Indexer.list_sources failed", exc_info=True)
                live = None
            base = live.get("path") if live is not None else None
            if isinstance(base, str) and base:
                rel = item.get("rel_path") or ""
                return live, ntpath.join(base, rel) if rel else base, bool(live.get("online"))
        return cached, _item_path(item), _is_online(item)

    def _probe(self, path: str, source: dict[str, Any]) -> str:
        """'dir' | 'file' | 'missing' | 'error' | 'timeout': a stat bounded by STAT_TIMEOUT_S,
        at most one in flight per source (the key Controller.open_path uses too)."""
        sid = source.get("id")
        key = (f"open:source:{sid}" if sid is not None
               else "open:" + (ntpath.splitdrive(path)[0] or path).casefold())
        try:
            status, kind = self._timed_call(key, lambda: _probe_folder(path), STAT_TIMEOUT_S)
        except Exception:
            log.exception("Checking %s failed", path)
            return "error"
        if status != "ok":
            log.info("Checking %s: %s", path, status)
            return "timeout" if status in ("timeout", "busy") else "error"
        return kind if kind in ("dir", "file", "missing") else "error"

    # -- the helper process (Resolve thread; stop() may detach it) --------------------------
    def _detach_child(self) -> Any:
        with self._lock:
            child, self._child = self._child, None
        return child

    def _ensure_child(self, now: float) -> Any:
        """The running helper, started if needed; None while a start is not allowed/failed."""
        with self._lock:
            child = self._child
        if child is not None:
            if child.alive():
                return child
            log.warning("The DaVinci Resolve helper (pid %s) exited unexpectedly", child.pid)
            self._stop_child(failed=True)
            self._connect_error = ERR_NO_RESPONSE
        if now < self._child_retry_at or self._stop.is_set():
            return None
        try:
            child = self._spawn_child()
        except (OSError, ValueError, ChildError) as exc:
            log.error("Could not start the DaVinci Resolve helper: %s", exc)
            self._child_started = None
            self._note_child_failure(now)
            self._connect_error = ERR_HELPER
            return None
        with self._lock:
            stopping = self._stop.is_set()
            if not stopping:
                self._child = child
        if stopping:
            child.close(CHILD_STOP_TIMEOUT_S)
            return None
        self._child_started = now
        try:
            child.wait_ready(CHILD_READY_TIMEOUT_S)
        except ChildError as exc:
            if not self._stop.is_set():
                log.error("The DaVinci Resolve helper did not start: %s", exc)
            self._stop_child(failed=True)
            self._connect_error = ERR_HELPER
            return None
        log.info("DaVinci Resolve helper started (pid %s)", child.pid)
        return child

    def _stop_child(self, failed: bool = False) -> None:
        """Stop the helper (Resolve gone, integration off, bridge stopping - or it failed)."""
        child = self._detach_child()
        if child is not None:
            child.close(CHILD_STOP_TIMEOUT_S)
            if not failed:
                log.info("DaVinci Resolve helper stopped (pid %s)", child.pid)
        if failed:
            self._note_child_failure(self._clock())
        self._child_started = None

    def _note_child_failure(self, now: float) -> None:
        started = self._child_started
        if started is not None and now - started >= CHILD_STABLE_S:
            self._child_failures = 0
        self._child_failures += 1
        delay = CHILD_BACKOFF_S[min(self._child_failures, len(CHILD_BACKOFF_S)) - 1]
        self._child_retry_at = now + delay
        log.info("Next DaVinci Resolve helper start in %.0f s", delay)

    def _child_call(self, cmd: str, timeout: float, **params: Any) -> dict[str, Any]:
        """A request the helper must answer with ok; raises ChildError / _ResolveUnavailable."""
        with self._lock:
            child = self._child
        if child is None:
            raise ChildError("the helper is not running")
        answer = child.request(cmd, timeout, **params)
        if not isinstance(answer, dict) or answer.get("ok") is not True:
            error = answer.get("error") if isinstance(answer, dict) else None
            detail = answer.get("detail") if isinstance(answer, dict) else answer
            raise _ResolveUnavailable(f"{cmd}: {error or 'no answer'} ({detail})")
        return answer

    # -- the Resolve thread ----------------------------------------------------------------
    def _run(self) -> None:
        log.debug("Resolve thread started")
        try:
            while not self._stop.is_set():
                self._wake.clear()
                try:
                    delay = self._tick()
                except Exception:
                    log.exception("Resolve thread iteration failed")
                    self._disconnect()
                    self._stop_child(failed=True)
                    enabled, delay, _ = self._settings()
                    self._set_state(_idle_state(enabled, self._state["running"], ERR_INTERNAL))
                if not self._stop.is_set():
                    self._wake.wait(max(0.0, delay))
        finally:
            self._disconnect()
            self._stop_child()
            log.debug("Resolve thread stopped")

    def _tick(self) -> float:
        """One iteration of the Resolve thread; returns the seconds until the next one."""
        enabled, poll_s, follow = self._settings()
        refresh_target, refresh_wanted, shown = self._take_requests()
        now = self._clock()
        if not enabled:
            self._disconnect()
            self._stop_child()
            self._reset_gate()
            running = bool(self._call_quietly("process_running", RESOLVE_EXE))
            self._set_state(_idle_state(False, running))
            self._complete_refresh(refresh_target)
            return poll_s
        wait = self._ensure_connected(now)
        if wait is not None:
            self._complete_refresh(refresh_target)
            return min(poll_s, wait)
        try:
            return self._connected_tick(now, poll_s, follow, refresh_target, refresh_wanted, shown)
        except ChildError as exc:
            self._disconnect()
            self._complete_refresh(refresh_target)
            if self._stop.is_set():
                return 0.0
            log.warning("The DaVinci Resolve helper failed: %s", exc)
            self._stop_child(failed=True)
            self._connect_error = ERR_NO_RESPONSE
            self._set_state(_idle_state(True, True, ERR_NO_RESPONSE))
            return min(poll_s, max(0.0, self._child_retry_at - self._clock()))
        except _ResolveUnavailable as exc:
            log.info("DaVinci Resolve stopped answering: %s", exc)
            self._disconnect()
            self._set_state(_idle_state(True, True, ERR_NO_RESPONSE))
            self._complete_refresh(refresh_target)
            return poll_s

    def _ensure_connected(self, now: float) -> float | None:
        """Gate, start the helper and connect. None when connected, else seconds until it is
        worth retrying."""
        if not self._call_quietly("process_running", RESOLVE_EXE):
            self._disconnect()
            self._stop_child()          # nothing of Resolve stays loaded while it is closed
            self._reset_gate()
            self._set_state(_idle_state(True, False))
            return float("inf")
        if self._first_seen_running is None:
            self._first_seen_running = now
        uptime = self._call_quietly("process_uptime", RESOLVE_EXE)
        if not isinstance(uptime, (int, float)):
            uptime = now - self._first_seen_running
        if uptime < STARTUP_GRACE_S:
            self._disconnect()
            self._stop_child()
            self._set_state(_idle_state(True, True))
            return STARTUP_GRACE_S - uptime
        if self._connected:
            return None
        if now < self._retry_at:
            self._set_state(_idle_state(True, True, self._connect_error))
            return self._retry_at - now
        child = self._ensure_child(now)
        if child is None:
            self._set_state(_idle_state(True, True, self._connect_error))
            return max(0.0, self._child_retry_at - now)
        try:
            answer = child.request("connect", CHILD_CONNECT_TIMEOUT_S)
        except ChildError as exc:
            if self._stop.is_set():
                return 0.0
            log.warning("The DaVinci Resolve helper did not answer the connect request: %s", exc)
            self._stop_child(failed=True)
            self._connect_error = ERR_NO_RESPONSE
            self._set_state(_idle_state(True, True, ERR_NO_RESPONSE))
            return max(0.0, self._child_retry_at - now)
        if isinstance(answer, dict) and answer.get("ok") is True:
            log.info("Connected to DaVinci Resolve (helper pid %s)", child.pid)
            self._connected = True
            self._connect_error = None
            return None
        code = answer.get("error") if isinstance(answer, dict) else None
        detail = answer.get("detail") if isinstance(answer, dict) else answer
        if code == "module":
            log.warning("DaVinciResolveScript could not be loaded: %s", detail)
            error = ERR_MODULE
        elif code == "refused":
            log.info("DaVinci Resolve refused the scripting connection (external scripting off?)")
            error = ERR_EXTERNAL_SCRIPTING
        else:
            log.warning("Connecting to DaVinci Resolve failed: %s", detail or code)
            error = ERR_NO_RESPONSE
        self._connect_error = error
        self._retry_at = now + CONNECT_RETRY_S
        self._set_state(_idle_state(True, True, error))
        return CONNECT_RETRY_S

    def _disconnect(self) -> None:
        self._connected = False
        self._project_key = None
        self._last_walk = None
        self._walk = None
        self._follow_pending = False

    def _reset_gate(self) -> None:
        """Forget start-up observation and back-offs (Resolve gone or integration off)."""
        self._first_seen_running = None
        self._retry_at = 0.0
        self._connect_error = None
        self._child_failures = 0
        self._child_retry_at = 0.0

    def _connected_tick(self, now: float, poll_s: float, follow: str, refresh_target: int,
                        refresh_wanted: bool, shown: bool) -> float:
        snap = self._poll()
        remap_due: float | None = None
        if snap.key != self._project_key:
            log.info("Resolve project: %r (database %r)", snap.name, snap.database)
            self._project_key = snap.key
            self._project_since = now
            self._last_walk = None
            self._walk = None
            self._follow_pending = snap.name is not None
            self._set_state(self._connected_state(snap), snap.uid)
            walk = snap.name is not None
        else:
            stale = self._last_walk is None or now - self._last_walk > REWALK_ON_SHOW_S
            walk = snap.name is not None and (refresh_wanted or (shown and stale))
        if walk:
            if not self._walk_and_map(snap):
                return 0.0  # the project changed during the walk: handle the new one right away
        else:
            remap_due = self._maybe_remap(snap, now)
        self._complete_refresh(refresh_target)
        due = self._maybe_follow(self._clock(), follow)
        return min(d for d in (poll_s, due, remap_due) if d is not None)

    def _poll(self) -> _Snapshot:
        answer = self._child_call("poll", CHILD_CALL_TIMEOUT_S)
        db = answer.get("db")
        if not isinstance(db, list) or len(db) != 3:
            db = ["", "", ""]
        db_id = tuple(_text(v) for v in db)        # DbType, DbName, IpAddress
        database = db_id[1] or None
        name = answer.get("project")
        if name is None:
            return _Snapshot(db_id + ("", ""), None, database)
        if not isinstance(name, str):
            raise _ResolveUnavailable(f"poll: project {name!r}")
        uid = self._project_uid(_text(answer.get("uid")), db_id + (name,))
        return _Snapshot(db_id + (name, uid), name, database, uid)

    def _project_uid(self, uid: str, db_and_name: tuple[str, ...]) -> str:
        """Project.GetUniqueId() tells same-named projects apart; dropped if it proves unstable."""
        if self._uid_unreliable:
            return ""
        last = self._project_key
        if uid and last is not None and last[:-1] == db_and_name and last[-1] != uid:
            # Same database and name but another id: a different project - unless the id is
            # not a stable one, which a second read through a fresh proxy reveals.
            again = self._child_call("uid", CHILD_CALL_TIMEOUT_S)
            if again.get("project") is None or _text(again.get("uid")) != uid:
                log.warning("Project.GetUniqueId() is not stable; using name + database only")
                self._uid_unreliable = True
                return ""
        return uid

    def _connected_state(self, snap: _Snapshot, clip_count: int = 0,
                         mapping: dict[str, Any] | None = None,
                         updated: float | None = None) -> dict[str, Any]:
        state = _idle_state(True, True, connected=True)
        state["project"] = snap.name
        state["database"] = snap.database
        if mapping is not None:
            state.update(mapping)
            state["clip_count"] = clip_count
            state["updated"] = self._wall() if updated is None else updated
        return state

    def _walk_and_map(self, snap: _Snapshot) -> bool:
        """Walk the media pool of ``snap`` and publish the mapping. False if the project
        changed meanwhile (nothing is published then)."""
        started = self._clock()
        answer = self._child_call("walk", WALK_MAX_SECONDS + CHILD_WALK_MARGIN_S,
                                  max_clips=WALK_MAX_CLIPS, max_seconds=WALK_MAX_SECONDS)
        raw = answer.get("paths")
        paths = [p for p in raw if isinstance(p, str) and p] if isinstance(raw, list) else []
        truncated = bool(answer.get("truncated"))
        if self._stop.is_set():
            return True
        if self._poll().key != snap.key:
            log.info("Resolve project changed during the media pool walk")
            return False
        rows = self._registry_rows()      # before mapping: later changes are seen as changes
        mapping, depends_on = self._map(snap.name, paths, rows)
        self._last_walk = self._clock()
        self._walk = _Walk(snap.name, paths, self._wall(), rows, self._last_walk, depends_on)
        elapsed = self._last_walk - started
        log.log(logging.INFO if truncated or elapsed > SLOW_WALK_S else logging.DEBUG,
                "Mapped %d clip paths of %r in %.1f s%s: %d folder(s), %d other dir(s)",
                len(paths), snap.name, elapsed, " (walk truncated)" if truncated else "",
                len(mapping["folders"]), len(mapping["other_dirs"]))
        self._set_state(self._connected_state(snap, len(paths), mapping, self._walk.updated),
                        snap.uid)
        return True

    def _registry_rows(self) -> dict[Any, tuple] | None:
        """The Indexer's live registry as comparable rows (None when it cannot be read)."""
        list_sources = getattr(self._indexer, "list_sources", None)
        if list_sources is None:
            return None
        try:
            sources = list_sources()
        except Exception:
            log.log(logging.DEBUG if self._registry_failed else logging.WARNING,
                    "Indexer.list_sources failed", exc_info=True)
            self._registry_failed = True
            return None
        self._registry_failed = False
        return {s.get("id"): _source_row(s) for s in sources or () if isinstance(s, dict)}

    def _maybe_remap(self, snap: _Snapshot, now: float) -> float | None:
        """Map the last walk's clip paths again when the registry changed in a way the mapping
        depends on (a disk plugged in or out, a host back, another drive letter, a finished
        deep or shallow scan): no media pool walk, at most every REMAP_MIN_INTERVAL_S. Returns
        the seconds until a pending re-map is allowed, else None. The follow bookkeeping is left
        alone."""
        walk = self._walk
        if walk is None:
            return None
        rows = self._registry_rows()
        if rows is None:
            return None
        if walk.rows is not None and not _registry_changed(walk.rows, rows, walk.depends_on):
            return None
        wait = walk.mapped_at + REMAP_MIN_INTERVAL_S - now
        if wait > 0:
            return wait
        mapping, walk.depends_on = self._map(walk.name, walk.paths, rows)
        walk.rows = rows
        walk.mapped_at = self._clock()
        log.debug("Re-mapped %d clip paths of %r after a location change", len(walk.paths),
                  walk.name)
        self._set_state(self._connected_state(snap, len(walk.paths), mapping, walk.updated),
                        snap.uid)
        return None

    def _map(self, name: str | None, paths: list[str], rows: dict[Any, tuple] | None,
             ) -> tuple[dict[str, Any], frozenset[Any] | None]:
        """The mapping of the clip ``paths`` (name suggestions without a media match) and the
        sources it depends on (None: every source, when it asked for name suggestions)."""
        folders: list[dict[str, Any]] = []
        other_dirs: list[dict[str, Any]] = []
        if paths:
            try:
                mapped = self._indexer.map_paths(paths)
                folders = sorted(mapped.get("folders") or [],
                                 key=lambda f: (-_count(f), not f.get("online")))
                other_dirs = sorted(mapped.get("other_dirs") or [], key=lambda d: -_count(d))
            except Exception:
                log.exception("Indexer.map_paths failed")
                folders, other_dirs = [], []
        suggestions: list[dict[str, Any]] = []
        by_name = not folders and bool(name) and not _is_untitled(name)
        if by_name:
            try:
                suggestions = sorted(self._indexer.suggest_project_folders(name) or [],
                                     key=lambda s: (-float(s.get("score") or 0.0),
                                                    not s.get("online")))
            except Exception:
                log.exception("Indexer.suggest_project_folders failed")
        offline_clips, offline_disks = _offline_summary(folders, other_dirs)
        mapping = {"folders": folders, "other_dirs": other_dirs[:MAX_OTHER_DIRS],
                   "suggestions": suggestions, "primary": _choose_primary(folders, suggestions),
                   "offline_clips": offline_clips, "offline_disks": offline_disks}
        return mapping, None if by_name else _mapping_sources(folders, other_dirs, rows or {})

    # -- follow mode -------------------------------------------------------------------------
    def _maybe_follow(self, now: float, mode: str) -> float | None:
        """Act once the project has been stable ≥ 5 s. Returns seconds until due, if pending."""
        if not self._follow_pending:
            return None
        state = self._state
        if state["updated"] is None:
            return None
        remaining = FOLLOW_STABLE_S - (now - self._project_since)
        if remaining > 0:
            return remaining
        self._follow_pending = False
        key = self._project_key
        if (mode == "off" or key is None or not state["project"] or _is_untitled(state["project"])
                or state["clip_count"] < 1):
            return None
        last = self._followed.get(key)
        if last is not None and now - last < FOLLOW_REPEAT_S:
            return None
        message = _follow_message(state)
        if message is None:
            return None
        self._followed = {k: t for k, t in self._followed.items() if now - t < FOLLOW_REPEAT_S}
        self._followed[key] = now
        self._bus.publish("notify", message)
        if mode == "open":
            self._follow_open(state["primary"])
        return None

    def _follow_open(self, primary: dict[str, Any] | None) -> None:
        """Open a media-matched, online primary without stealing focus - unless already open."""
        if not primary or primary.get("match") != "media":
            return
        source, path, online = self._live_location(primary)
        if not path or not online:
            return
        try:
            if self._fn("explorer_window_for")(path):
                log.debug("Follow: %s is already open in Explorer", path)
                return
            if self._probe(path, source) != "dir":
                log.info("Follow: %s cannot be reached", path)
                return
            if not self._fn("open_folder")(path, activate=False):
                log.info("Follow: could not open %s", path)
        except Exception:
            log.exception("Follow: opening %s failed", path)
