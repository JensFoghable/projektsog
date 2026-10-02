"""Indexer engine (SPEC §8): source registry, discovery, scan scheduling, scan-worker supervision
and the query/command API used by the HTTP server, the tray and the Resolve bridge.

Threads (daemon threads of the main process):

* ``indexer-scheduler`` – worker events, config changes, timers, job dispatch and the throttled
  events (``status``, ``sources``, ``index_updated``, ``scan_progress``, ``new_volume``).  It is
  the only thread that sends commands to the scan worker.
* ``indexer-db`` – the only writer of ``sources``/``meta``: registry changes are coalesced into
  short transactions.
* ``indexer-local`` – polls local volumes, shares and mapped drives every
  ``discovery_interval_local_s``; ``indexer-probe`` probes local candidates.
* ``indexer-host-<HOST>`` – one per remote host, every ``discovery_interval_network_s``: shares,
  reachability and the probes of that host's candidates.
* ``indexer-worker-out`` – reads the scan worker's JSON lines into the scheduler's inbox.

Queries (``search``, ``status``, ``locate`` …) run on the caller's thread: they copy registry
state under a short lock and read the index through pooled read-only connections; they never
touch the file system.  Commands validate, update the registry (and the config where the SPEC
says so) and wake the owning thread.  Everything that can hang runs on a discovery thread through
``winfs.call_with_timeout``.
"""

from __future__ import annotations

import collections
import difflib
import json
import logging
import ntpath
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from . import APP_NAME, __version__, config, db, discovery, search, textutil, winfs
from .config import Config
from .events import EventBus
from .pathmap import PathMap, clean_path, is_within, long_path, path_parts, split_unc

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------------------
# Tunables (seconds unless noted).  Module constants so tests can shorten them.
# --------------------------------------------------------------------------------------

WINDOW_SHOWN_DELAY_S = 1.0          # on_window_shown: calls within this delay are merged
WINDOW_SHOWN_MIN_AGE_S = 30.0       # ... then sources not refreshed for this long get a shallow
REPROBE_INTERVAL_S = 30 * 60.0      # auto-excluded candidates are re-probed at most this often
RETURN_RESCAN_MIN_OFFLINE_S = 60.0  # back after a shorter blip: the periodic schedule suffices
NEW_VOLUME_MAX_WAIT_S = 60.0        # announce a new disk even if its probes have not finished
HOST_RETRY_DELAY_S = 2.0            # one retry before a host counts as unreachable
HOST_ACTIVITY_GRACE_S = 30.0        # worker progress on a host this recent proves it is alive
OWN_IPS_REFRESH_S = 300.0
DB_SIZE_REFRESH_S = 10.0
LAST_SEEN_WRITE_S = 60.0            # last_seen of online sources is persisted at most this often
DB_RETRY_S = 2.0
STATUS_INTERVAL_S = 0.5             # status events ≤ 2/s
SOURCES_INTERVAL_S = 0.5
PROGRESS_INTERVAL_S = 0.25          # scan_progress events ≤ 4/s
INDEX_UPDATED_INTERVAL_S = 1.0      # index_updated ≤ 1/s per source
SCHEDULER_TICK_S = 0.5
MIN_NETWORK_POLL_S = 5.0
WORKER_MAX_RESTARTS = 5             # ... within WORKER_RESTART_WINDOW_S (SPEC §2)
WORKER_RESTART_WINDOW_S = 600.0
WORKER_BACKOFF_S = (1.0, 2.0, 5.0, 10.0, 30.0)
WORKER_STOP_SHARE = 0.6             # part of stop()'s budget the worker gets to exit by itself

PROBE_MAX_LISTINGS = 400
FIRST_SHALLOW_LISTINGS = 2000
LOCAL_SHALLOW_LISTINGS = 2000
NETWORK_SHALLOW_LISTINGS = 200
DIR_COST_S = 0.02                   # deep-queue cost estimate per directory (no scan_seconds yet)
SUGGEST_MIN_SCORE = 0.4
PASSING_CARD_MAX_BYTES = 512 * 1024 ** 3   # a hot-plug volume this small is a memory card …
PASSING_CARD_GRACE_S = 120.0       # … whose project-less sources are forgotten this long after
SWAP_HOLD_S = 10.0                  # a scan that met another disk waits for rediscovery ...
SWAP_HOLD_MAX_S = 30 * 60.0         # ... doubling each time it happens again, up to this

MODES = ("auto", "include", "exclude")

# User-facing texts (Danish).
ERR_UNKNOWN_SOURCE = "Placeringen findes ikke"
ERR_FORGET_ONLINE = "Kun offline placeringer kan glemmes"
ERR_BAD_MODE = "Ugyldig tilstand ‘{}’ – vælg auto, include eller exclude"
ERR_NOT_ABSOLUTE = "Angiv en fuld sti, f.eks. D:\\Projekter eller \\\\PC\\Delt mappe"
ERR_ROOT_UNKNOWN = "Mappen er ikke tilføjet manuelt: {}"
ERR_BAD_HOST = "Ugyldigt computernavn: ‘{}’"
ERR_HOST_UNKNOWN = "Computeren {} er ikke på listen"
ERR_HOST_HAS_ROOT = "Mappen ‘{}’ ligger på {} – fjern den først"     # SPEC §15.12
ERR_SOURCE_OFFLINE = "Placeringen er ikke tilgængelig lige nu"
ERR_SOURCE_EXCLUDED = "Placeringen er ikke medtaget i søgningen"
ERR_WORKER_LOST = "Scanningen blev afbrudt, fordi scanneren stoppede"
ERR_ALREADY_INCLUDED = "Mappen er allerede med i søgningen via ‘{}’"
# The scan worker's error when the volume at a local source's path has another serial than
# the command's ``expected_serial`` (SPEC §15.5): not a scan error, the disk was swapped.
ERR_DISK_SWAPPED = "Disken er skiftet"
NOTIFY_NEW_DISK = "Ny disk ‘{}’ tilsluttet"
NOTIFY_INCLUDED = " – medtaget i søgningen"
NOTIFY_EXCLUDED = " – ikke medtaget: {}"

_PERSISTED: tuple[str, ...] = db.SOURCE_FIELDS
_PERSISTED_SET = frozenset(_PERSISTED)
_COUNT_FIELDS = ("entry_count", "dir_count", "file_count", "project_count", "total_size")
_PROGRESS_FIELDS = ("entries", "dirs", "units_done", "units_total")

# Settings whose change needs a full rescan (reused leaf dirs keep the old rules otherwise).
_SCAN_RULE_KEYS = frozenset({
    "exclude_dir_names", "exclude_file_names", "exclude_file_globs", "sequence_exts",
    "sequence_min_files", "template_folder_regex", "project_template_dirs",
    "project_min_template_dirs"})
_PROBE_KEYS = frozenset({
    "project_template_dirs", "project_min_template_dirs", "template_folder_regex", "media_exts",
    "exclude_dir_names", "exclude_file_names", "exclude_file_globs"})
_LOCAL_KEYS = frozenset({"skip_volume_labels", "skip_top_level_dirs", "extra_roots",
                         "exclude_dir_names"})
_HOST_KEYS = frozenset({"hosts", "extra_roots"})

_HOST_LABEL = r"[A-Z0-9_][A-Z0-9_-]{0,62}"
_HOST_RE = re.compile(rf"(?=.{{1,253}}$){_HOST_LABEL}(?:\.{_HOST_LABEL})*")   # name, FQDN or IPv4
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CREATE_NO_WINDOW = 0x08000000
_INSERT_SOURCE_SQL = (f"INSERT INTO sources(id, {', '.join(_PERSISTED)}) "
                      f"VALUES ({', '.join('?' * (len(_PERSISTED) + 1))})")
_UPSERT_META_SQL = ("INSERT INTO meta(key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value")
_META_KEYS = ("known_volumes", "last_source_id", "pending_forget", "system_volumes")
_ROOT_DIR_LIMIT = 10_000                # root folder names read to decide root_is_project
_NEVER = float("-inf")


# --------------------------------------------------------------------------------------
# The outside world (injectable)
# --------------------------------------------------------------------------------------

def _unc_share_path(host: str, share: str) -> str:
    return f"\\\\{host}\\{share}"


@dataclass
class DiscoveryEnv:
    """Everything the Indexer asks the machine and the network.

    The defaults are the real Windows functions; tests replace members with fakes (volumes that
    are temporary folders, hosts whose shares map to temporary folders via ``share_path``, a
    controllable ``clock`` for all wall-clock decisions).
    """

    hostname: str = field(default_factory=config.hostname)
    list_volumes: Callable[[], list[dict]] = winfs.list_volumes
    local_shares: Callable[[], list[dict]] = winfs.local_shares
    mapped_drives: Callable[[], dict[str, str]] = winfs.mapped_drives
    remote_shares: Callable[[str], list[str] | None] = winfs.remote_shares
    resolve_host_ips: Callable[[str], list[str]] = winfs.resolve_host_ips
    volume_info: Callable[[str], dict | None] = winfs.volume_info
    probe: Callable[..., tuple[bool, str, int]] = discovery.probe
    share_path: Callable[[str, str], str] = _unc_share_path   # access path of \\HOST\share
    clock: Callable[[], float] = time.time


def default_worker_argv() -> list[str]:
    """``[pythonw.exe next to sys.executable (else sys.executable), -m, projektsog.scanworker]``."""
    exe = sys.executable
    pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return [pythonw if os.path.isfile(pythonw) else exe, "-m", "projektsog.scanworker"]


# --------------------------------------------------------------------------------------
# Registry records
# --------------------------------------------------------------------------------------

@dataclass(eq=False)
class _Pending:
    """A scan that is wanted but not yet sent to the worker."""

    kind: str                   # "shallow" | "deep"
    requested: float            # monotonic
    full: bool = False
    first_time: bool = False
    max_listings: int = FIRST_SHALLOW_LISTINGS
    window: bool = False        # part of an on_window_shown round
    retried: bool = False       # re-queued once after the worker died


@dataclass(eq=False)
class _Job:
    """A job sent to the worker (until its ``done``/``failed``)."""

    id: int
    source_id: int
    kind: str                   # "shallow" | "deep" | "forget"
    name: str = ""
    device: str = ""
    host: str | None = None     # network sources: the host (per-host shallow limit)
    full: bool = False
    first_time: bool = False
    max_listings: int = 0
    window: bool = False
    retried: bool = False
    started: float = 0.0        # wall clock
    progress: dict[str, int] = field(default_factory=dict)
    cancel_sent: bool = False


@dataclass(eq=False)
class _Source:
    """One ``sources`` row (the SOURCE_FIELDS, same order) plus runtime state."""

    id: int
    key: str
    kind: str
    host: str
    share: str | None
    display_name: str
    current_path: str
    unc_path: str | None = None
    volume_serial: str | None = None
    volume_label: str | None = None
    fs: str | None = None
    volume_size: int | None = None
    last_drive: str | None = None
    hotplug: int = 0
    manual: int = 0
    online: int = 0
    mode: str = "auto"
    auto_include: int | None = None
    auto_reason: str | None = None
    probed_at: float | None = None
    first_seen: float | None = None
    last_seen: float | None = None
    last_scan_start: float | None = None
    last_scan_end: float | None = None
    last_scan_ok: int | None = None
    last_full_scan: float | None = None
    last_shallow_scan: float | None = None
    last_error: str | None = None
    scan_seconds: float | None = None
    entry_count: int = 0
    dir_count: int = 0
    file_count: int = 0
    project_count: int = 0
    total_size: int = 0
    # -- runtime only -------------------------------------------------------------------
    persisted: bool = True
    auto_candidate: bool = False        # produced by auto discovery (not only by extra_roots)
    job: _Job | None = None
    pending: dict[str, _Pending] = field(default_factory=dict)
    needs_full: bool = False
    reprobe: bool = False
    probe_queued: bool = False
    offline_since: float | None = None  # wall clock, this session
    missing_at: float | None = None     # last path_missing that queued scans (wall clock)
    activity: float = _NEVER            # monotonic time of the last worker event
    seen_written: float | None = None   # last_seen value last persisted
    root_is_project: bool = False       # the root folder itself is a project (from its index)
    hold_until: float = _NEVER          # monotonic: no scans before (another disk was found)
    swap_strikes: int = 0               # scans in a row that found another disk

    @property
    def included(self) -> bool:
        return bool(self.manual or self.mode == "include"
                    or (self.mode == "auto" and self.auto_include == 1))

    @property
    def device(self) -> str:
        """At most one deep scan runs per device: a host (network) or a volume (local)."""
        if self.kind == "share":
            return "host:" + self.host.upper()
        return "vol:" + (self.volume_serial or self.key).upper()


@dataclass(eq=False)
class _HostState:
    name: str
    enumerate: bool                     # every share of the host is a candidate
    wake: threading.Event = field(default_factory=threading.Event)
    stop: threading.Event = field(default_factory=threading.Event)
    online: bool = False
    shares: list[str] | None = None
    last_seen: float | None = None
    passes: int = 0
    quiet_first: bool = False           # polled since startup: no index_updated on pass 1


@dataclass(eq=False)
class _Announcement:
    """A first-seen volume, published once its candidates are probed."""

    drive: str
    disk_name: str
    source_ids: list[int]
    deadline: float                     # monotonic


@dataclass(frozen=True, eq=False)
class _View:
    """Immutable snapshot for queries: Source dicts and the path index of their roots."""

    version: int
    sources: dict[int, dict[str, Any]]
    index: dict[str, list[int]]         # casefolded root path → source ids


# --------------------------------------------------------------------------------------
# Scan worker process
# --------------------------------------------------------------------------------------

class _WorkerProcess:
    """One scan worker process plus the thread that reads its events."""

    def __init__(self, argv: list[str], generation: int,
                 on_event: Callable[[int, dict], None],
                 on_exit: Callable[[int, int | None], None]) -> None:
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(p for p in (_REPO_ROOT, env.get("PYTHONPATH")) if p)
        self.generation = generation
        self.ready = False
        self._on_event = on_event
        self._on_exit = on_exit
        self._write_lock = threading.Lock()
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, cwd=config.app_dir(), env=env,
                                     creationflags=_CREATE_NO_WINDOW, close_fds=True)
        self.pid = self.proc.pid

    def start_reader(self) -> None:
        """Start delivering events (after the owner has registered this worker)."""
        threading.Thread(target=self._read, name="indexer-worker-out", daemon=True).start()

    def _read(self) -> None:
        try:
            for raw in self.proc.stdout:
                try:
                    event = json.loads(raw)
                except ValueError:
                    log.warning("Scan worker sent a malformed line: %.200r", raw)
                    continue
                if isinstance(event, dict):
                    self._on_event(self.generation, event)
        except (OSError, ValueError):
            log.debug("Scan worker output closed", exc_info=True)
        finally:
            try:
                code = self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:      # output closed but still running
                self.proc.kill()
                code = None
            with self._write_lock:
                for pipe in (self.proc.stdin, self.proc.stdout):
                    try:
                        pipe.close()
                    except OSError:
                        pass
            self._on_exit(self.generation, code)

    def send(self, message: dict) -> bool:
        data = (json.dumps(message) + "\n").encode("ascii")
        with self._write_lock:
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except (OSError, ValueError):
                return False
        return True

    def close(self, timeout: float) -> None:
        """``quit`` (the worker cancels its jobs), then kill it if it does not exit in time."""
        self.send({"cmd": "quit"})
        with self._write_lock:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
        try:
            self.proc.wait(timeout=max(0.05, timeout))
        except subprocess.TimeoutExpired:
            log.warning("Scan worker %s did not exit in time - terminating it", self.pid)
            self.proc.kill()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                log.error("Scan worker %s could not be terminated", self.pid)


class _Throttle:
    """Latest value per key, released at most once per ``interval`` per key (trailing edge)."""

    def __init__(self, interval: float) -> None:
        self.interval = interval
        self._last: dict[Any, float] = {}
        self._pending: dict[Any, Any] = {}

    def put(self, key: Any, value: Any) -> None:
        self._pending[key] = value

    def pop_due(self, now: float) -> list[tuple[Any, Any]]:
        due = [(k, v) for k, v in self._pending.items()
               if now - self._last.get(k, _NEVER) >= self.interval]
        for k, _ in due:
            del self._pending[k]
            self._last[k] = now
        return due

    def __bool__(self) -> bool:
        return bool(self._pending)


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------

def _host_name(name: str) -> str:
    return name.strip().strip("\\").upper()


def _join(base: str | None, rel: str) -> str | None:
    if not base:
        return None
    return ntpath.join(base, rel) if rel else base


def _root_key(parts: list[str]) -> str:
    """Casefolded path of leading :func:`path_parts` components (``c:\\``, ``\\\\host\\share``)."""
    if parts[0].startswith("\\\\"):
        return "\\".join(parts).casefold()
    return (parts[0] + "\\" + "\\".join(parts[1:])).casefold()


def _index_keys(path: str | None) -> list[str]:
    if not path:
        return []
    parts = path_parts(path)
    if not parts:
        return []
    if parts[0].startswith("\\\\"):
        return [_root_key(parts)] if len(parts) >= 2 else []
    if len(parts[0]) == 2 and parts[0][1] == ":":
        return [_root_key(parts)]
    return []


def _match_index(path: str, view: _View) -> tuple[list[int], str]:
    """The sources whose root is the deepest ancestor-or-self of ``path``, and the rest.

    Several sources share a root when disks are used one after another under the same drive
    letter (whole volumes, or top-level folders with the same name).
    """
    parts = path_parts(path)
    if not parts:
        return [], ""
    if parts[0].startswith("\\\\"):
        lowest = 2
    elif len(parts[0]) == 2 and parts[0][1] == ":":
        lowest = 1
    else:
        return [], ""
    for i in range(len(parts), lowest - 1, -1):
        ids = view.index.get(_root_key(parts[:i]))
        if ids:
            return list(ids), "\\".join(parts[i:])
    return [], ""


def _pick_source(ids: list[int], rel: str, view: _View,
                 conn: sqlite3.Connection | None) -> int:
    """The source a path ``rel`` below a shared root belongs to (SPEC §15.5, IDX-4).

    A disk now mounted at the letter only owns the path when no other disk's index knows it
    better: the source whose index holds the path itself wins, then the deepest indexed
    ancestor; ties (identical folder names on both media) go to the mounted (online) source.
    Without an index connection: online, included, oldest.
    """
    src = view.sources

    def mounted(sid: int) -> tuple[bool, bool, int]:
        return not src[sid]["online"], not src[sid]["included"], sid

    if len(ids) == 1 or conn is None or not rel:
        return min(ids, key=mounted)
    known: dict[int, tuple[bool, int]] = {}
    for sid in ids:
        row = db.nearest_entry(conn, sid, rel)
        known[sid] = ((False, 0) if row is None else
                      (row["rel_path"].casefold() == rel.casefold(), int(row["depth"] or 0)))
    return min(ids, key=lambda s: (not known[s][0], -known[s][1], *mounted(s)))


def _root_folder(key: str) -> str | None:
    """Name of a source's root folder from its key (SPEC §4.1): the last folder of a local key,
    the share (or its last sub-folder) of a share key; None for a whole volume."""
    if key.startswith("vol:"):
        rel = key[4:].partition(":")[2]
    elif key.startswith("unc:"):
        rel = key[4:].partition("\\")[2]              # <share>[\<rest>]
    else:
        return None
    parts = [p for p in rel.split("\\") if p]
    return parts[-1] if parts else None


def _project_part_root(key: str, cfg: Any) -> bool:
    """The source's root folder is named like a project template sub-folder (``Klip`` …): it
    is never a project itself, whatever it contains (SPEC §15.7, §15.12)."""
    folder = _root_folder(key)
    return bool(folder) and discovery.is_project_part(folder, cfg)


def _root_project_flag(conn: sqlite3.Connection, source_id: int, cfg: Any, key: str) -> bool:
    """``root_is_project`` (SPEC §15.3): the indexed sub-folders of the source's root follow
    the project template, so the root folder itself is a project.  Search shows it as a
    project item - or as a template when the root is named like one (``1. KUNDENAVN``).  A
    root named like a template sub-folder (a shared ``Klip`` folder) never is (§15.12)."""
    if _project_part_root(key, cfg):
        return False
    names = db.child_dir_names(conn, source_id, "", _ROOT_DIR_LIMIT)
    return bool(names) and discovery.looks_like_project(names, cfg)


def _keys_nested(a: str, b: str) -> bool:
    """True when the root of source key ``a`` lies inside (or is) the root of ``b``, or the
    reverse (same volume serial or same share)."""
    va, vb = _volume_rel(a), _volume_rel(b)
    if va is not None and vb is not None:
        n = min(len(va[1]), len(vb[1]))
        return va[0] == vb[0] and va[1][:n] == vb[1][:n]
    if a.startswith("unc:") and b.startswith("unc:"):
        pa = [p for p in a[4:].casefold().split("\\") if p]
        pb = [p for p in b[4:].casefold().split("\\") if p]
        n = min(len(pa), len(pb))
        return n >= 2 and pa[:n] == pb[:n]
    return False


def _dir_exists(path: str, timeout: float = 5.0) -> bool | None:
    """True/False, or None when the answer did not come in time."""
    status, value = winfs.call_with_timeout(f"isdir:{path}",
                                            lambda: os.path.isdir(long_path(path)), timeout)
    return bool(value) if status == "ok" else None


def _project_ref(source: Mapping[str, Any], rel: str) -> dict[str, Any]:
    return {"name": rel.rpartition("\\")[2] if rel else source["display_name"],
            "rel_path": rel, "path": _join(source["path"], rel),
            "unc_path": _join(source.get("unc_path"), rel)}


def _entry_item(conn: sqlite3.Connection, row: Mapping[str, Any],
                source: Mapping[str, Any]) -> dict[str, Any]:
    subfolders = (db.child_dir_names(conn, row["source_id"], row["rel_path"],
                                     search.SUBFOLDER_LIMIT)
                  if row["kind"] != db.KIND_FILE else None)
    return search.make_item(row, source, subfolders=subfolders)


def _name_tokens(text: str) -> list[str]:
    return list(dict.fromkeys(textutil.fold(text).split()))


def _recency(mtime: float | None) -> float:
    """Tie-break key (newest first): a folder that is not deep-scanned yet (mtime NULL) was
    just created, so it counts as the newest (RES-1)."""
    return float("inf") if mtime is None else float(mtime)


def _token_similarity(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if a.isdigit() or b.isdigit():          # 2024 vs 2025 is another project
        return 0.0
    if min(len(a), len(b)) >= 4 and (a.startswith(b) or b.startswith(a)):
        return 0.9
    if 2 * min(len(a), len(b)) < 0.8 * (len(a) + len(b)):   # ratio() cannot reach 0.8
        return 0.0
    matcher = difflib.SequenceMatcher(None, a, b)
    if matcher.quick_ratio() < 0.8:
        return 0.0
    ratio = matcher.ratio()
    return ratio if ratio >= 0.8 else 0.0


def name_similarity(query: str, name: str) -> float:
    """0..1 likeness of a project name (e.g. from DaVinci Resolve) and a folder name.

    Dice coefficient over folded words with fuzzy word matching (typos, plural endings);
    numbers must match exactly, and disagreeing numbers (years) lower the score.
    ``"Rikke Lindholm - Testimonial"`` vs ``"Rikke Lindholm"`` → 0.8.
    """
    a, b = _name_tokens(query), _name_tokens(name)
    if not a or not b:
        return 0.0
    used: set[int] = set()
    total = 0.0
    for word in b:
        best, pick = 0.0, -1
        for i, other in enumerate(a):
            if i not in used:
                score = _token_similarity(word, other)
                if score > best:
                    best, pick = score, i
        if pick >= 0:
            used.add(pick)
            total += best
    score = 2.0 * total / (len(a) + len(b))
    numbers_a = {t for t in a if t.isdigit()}
    numbers_b = {t for t in b if t.isdigit()}
    if numbers_a and numbers_b and not numbers_a & numbers_b:
        score *= 0.6
    return round(min(1.0, score), 3)


def _passing_card(src: "_Source") -> bool:
    """A source on a memory card that just passes through (see _forget_passing_cards)."""
    return (bool(src.hotplug) and src.mode == "auto" and not src.manual
            and 0 < (src.volume_size or 0) <= PASSING_CARD_MAX_BYTES
            and src.project_count == 0 and not src.root_is_project
            and src.auto_reason != discovery.REASON_TEMPLATE)


def _volume_rel(key: str) -> tuple[str, tuple[str, ...]] | None:
    """``vol:<SERIAL>:\\a\\b`` → ``("SERIAL", ("a", "b"))`` (casefolded); None for shares."""
    if not key.startswith("vol:"):
        return None
    serial, _, rel = key[4:].partition(":")
    return serial.upper(), tuple(p.casefold() for p in rel.split("\\") if p)


def _merge_local(auto: list[dict], manual: list[dict],
                 unsure: list[dict] = ()) -> tuple[list[dict], set[str], set[str]]:
    """Manual roots win; auto candidates inside a manual root of the same volume are dropped.

    ``unsure`` are manual roots whose existence check did not answer in time (e.g. a USB disk
    spinning up).  An auto candidate with the same key proves that the folder exists (the
    volume listing just saw it), so it is kept as the manual root, never as a plain auto
    candidate that would drop the root's ``manual`` flag; auto candidates inside an unsure
    root are dropped like those inside a confirmed one.

    Returns the candidates, the casefolded keys auto discovery produced and the casefolded
    keys of the manual roots whose presence stays unknown this time.
    """
    merged = {c["key"].casefold(): c for c in manual}
    pending = {c["key"].casefold(): c for c in unsure if c["key"].casefold() not in merged}
    roots = [*manual, *unsure]
    auto_keys: set[str] = set()
    for cand in auto:
        key = cand["key"].casefold()
        if key in pending:
            merged[key] = pending.pop(key)
        if key in merged:
            auto_keys.add(key)
            continue
        if any(m["volume_serial"] == cand["volume_serial"] and is_within(cand["path"], m["path"])
               for m in roots):
            continue
        merged[key] = cand
        auto_keys.add(key)
    return list(merged.values()), auto_keys, set(pending)


# --------------------------------------------------------------------------------------
# Indexer
# --------------------------------------------------------------------------------------

class Indexer:
    """Registry of sources, discovery, scheduling, worker supervision and the query API."""

    def __init__(self, cfg: Config, bus: EventBus, db_path: str | None = None, *,
                 worker_argv: list[str] | None = None, start_worker: bool = True,
                 env: DiscoveryEnv | None = None) -> None:
        self.cfg = cfg
        self.bus = bus
        self.db_path = db_path or config.db_path()
        self._env = env or DiscoveryEnv()
        self._clock = self._env.clock
        self._own = _host_name(self._env.hostname)
        argv = list(worker_argv or default_worker_argv())
        if not any(a == "--db" or a.startswith("--db=") for a in argv):
            argv += ["--db", self.db_path]
        self._worker_argv = argv
        self._start_worker = start_worker
        self._pathmap = PathMap(self._own)

        self._lock = threading.RLock()      # registry, jobs, discovery state
        self._sources: dict[int, _Source] = {}
        self._by_key: dict[str, int] = {}
        self._next_id = 1
        self._version = 0
        self._view: _View | None = None
        self._root_check: set[int] = set()  # sources whose root_is_project must be re-read
        self._system_serials: set[str] = set()  # serials seen as the system volume
        # persistence (indexer-db)
        self._dirty: dict[int, set[str]] = {}
        self._db_ops: list[tuple[str, int, str]] = []
        self._meta_dirty: set[str] = set()
        self._known_volumes: set[str] | None = None
        self._forget_ids: set[int] = set()
        # jobs
        self._jobs: dict[int, _Job] = {}
        self._next_job = 1
        self._cancels: list[int] = []
        self._forget_due: dict[int, float] = {}
        self._window_due: float | None = None
        self._announcements: list[_Announcement] = []
        # worker
        self._worker: _WorkerProcess | None = None
        self._worker_generation = 0
        self._worker_restarts = 0
        self._worker_failures = 0
        self._worker_next_start = 0.0
        self._restart_times: collections.deque[float] = collections.deque()
        self._budget_logged = False
        self._inbox: collections.deque[tuple[str, int, Any]] = collections.deque()
        # discovery
        self._volumes: list[dict] = []
        self._present_serials: set[str] = set()   # mounted, answering volumes (last pass)
        self._mapped: dict[str, str] = {}
        self._local_share_count = 0
        self._local_passes = 0
        self._last_local_pass: float | None = None
        self._own_ips_at = _NEVER
        self._hosts: dict[str, _HostState] = {}
        self._probe_queue: collections.deque[int] = collections.deque()
        self._path_lock = threading.Lock()
        self._path_inputs: dict[str, Any] = {"shares": [], "mapped": {}, "host_ips": {},
                                             "own_ips": []}
        # events
        self._changed: set[int] = set()
        self._status_dirty = True
        self._status_sent = _NEVER
        self._sources_sent = _NEVER
        self._index_updated = _Throttle(INDEX_UPDATED_INTERVAL_S)
        self._progress_dirty: set[int] = set()
        self._progress_sent: dict[int, float] = {}
        self._progress_at = _NEVER
        self._db_size = 0
        self._db_size_at = _NEVER
        # config
        self._cfg_lock = threading.Lock()
        self._cfg_pending: dict[str, Any] | None = None
        self._cfg_last: dict[str, Any] = {}
        # threads
        self._wake = threading.Event()
        self._db_wake = threading.Event()
        self._local_wake = threading.Event()
        self._probe_wake = threading.Event()
        self._stopping = threading.Event()
        self._started = False
        self._scheduler_thread: threading.Thread | None = None
        self._db_thread: threading.Thread | None = None
        self._readers: db.ReaderPool | None = None

    # ==================================================================================
    # Lifecycle
    # ==================================================================================

    def start(self) -> None:
        """Open/migrate the index, load the registry (all offline), start threads + worker."""
        with self._lock:
            if self._started:
                return
            self._started = True
        try:
            self._load()
        except BaseException:
            with self._lock:
                self._started = False
            raise
        self._readers = db.ReaderPool(self.db_path)
        self._cfg_last = self.cfg.snapshot()
        self.cfg.on_change(self._on_config)
        self._db_thread = self._spawn(self._db_loop, "indexer-db")
        if self._start_worker:
            self._spawn_worker()
        self._scheduler_thread = self._spawn(self._scheduler_loop, "indexer-scheduler")
        self._spawn(self._probe_loop, "indexer-probe")
        self._spawn(self._local_loop, "indexer-local")
        self._reconcile_hosts(self._cfg_last, initial=True)
        log.info("Indexer started: %d known locations, index %s", len(self._sources),
                 self.db_path)

    def stop(self, timeout: float = 5) -> None:
        """Cancel scans, stop the worker, persist the registry; returns within ``timeout``."""
        with self._lock:
            if not self._started or self._stopping.is_set():
                return
            self._stopping.set()
            hosts = list(self._hosts.values())
            worker, self._worker = self._worker, None
        deadline = time.monotonic() + max(0.1, float(timeout))
        for hs in hosts:
            hs.stop.set()
            hs.wake.set()
        for event in (self._wake, self._local_wake, self._probe_wake):
            event.set()
        if self._scheduler_thread is not None:
            self._scheduler_thread.join(max(0.0, min(0.5, deadline - time.monotonic())))
        if worker is not None:
            worker.close((deadline - time.monotonic()) * WORKER_STOP_SHARE)
        self._db_wake.set()
        if self._db_thread is not None:
            self._db_thread.join(max(0.0, deadline - time.monotonic()))
            if self._db_thread.is_alive():
                log.warning("Location changes could not be saved before exit")
        if self._readers is not None:
            self._readers.close()
        log.info("Indexer stopped")

    def _spawn(self, target: Callable[[], None], name: str) -> threading.Thread:
        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        return thread

    def _load(self) -> None:
        """Schema, registry and meta from the database (main thread, before any worker)."""
        conn = self._open_index()
        try:
            rows = db.load_sources(conn)
            meta = {key: db.get_meta(conn, key) for key in _META_KEYS}
            pending = {i for i in self._meta_list(meta["pending_forget"]) or []
                       if isinstance(i, int)}
            with db.transaction(conn):
                conn.execute("UPDATE sources SET online = 0 WHERE online <> 0")
                for sid in pending:    # a key rename may not have been committed before exit
                    conn.execute("UPDATE sources SET key = 'forgotten:' || id || ':' || key "
                                 "WHERE id = ? AND key NOT LIKE 'forgotten:%'", (sid,))
            root_projects = {int(row["id"]): _root_project_flag(conn, int(row["id"]), self.cfg,
                                                                str(row["key"]))
                             for row in rows if int(row["id"]) not in pending}
        finally:
            conn.close()
        known = self._meta_list(meta["known_volumes"])
        system = self._meta_list(meta["system_volumes"]) or []
        with self._lock:
            for row in rows:
                sid = int(row["id"])
                self._next_id = max(self._next_id, sid + 1)
                if sid in pending:
                    continue
                src = _Source(**row)
                src.online = 0
                src.seen_written = src.last_seen
                src.root_is_project = root_projects.get(sid, False)
                self._sources[sid] = src
                self._by_key[src.key.casefold()] = sid
            try:
                self._next_id = max(self._next_id, int(meta["last_source_id"] or 0) + 1)
            except ValueError:
                pass
            self._known_volumes = {str(s).upper() for s in known} if known is not None else None
            self._system_serials = {str(s).upper() for s in system if s}
            self._forget_ids = set(pending)
            self._forget_due = {sid: 0.0 for sid in pending}
            self._version += 1

    def _open_index(self) -> sqlite3.Connection:
        """Writer connection with the current schema.  The index is a rebuildable cache: a
        damaged file (not merely a locked one) is set aside and a new index is started."""
        try:
            return self._connect_with_schema()
        except sqlite3.OperationalError:
            raise
        except sqlite3.DatabaseError as exc:
            log.error("The index %s is damaged (%s) - starting a new one", self.db_path, exc)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        for suffix in ("", "-wal", "-shm"):
            try:
                os.replace(self.db_path + suffix, f"{self.db_path}.damaged-{stamp}{suffix}")
            except FileNotFoundError:
                pass
        return self._connect_with_schema()

    def _connect_with_schema(self) -> sqlite3.Connection:
        conn = db.connect(self.db_path, writer=True)
        try:
            db.ensure_schema(conn)
        except BaseException:
            conn.close()
            raise
        return conn

    @staticmethod
    def _meta_list(value: str | None) -> list | None:
        """A JSON list stored in ``meta``; None when absent or unreadable."""
        if value is None:
            return None
        try:
            parsed = json.loads(value)
        except ValueError:
            parsed = None
        if not isinstance(parsed, list):
            log.warning("Ignoring unreadable index meta value %.100r", value)
            return None
        return parsed

    # ==================================================================================
    # Queries (any thread; no file-system I/O)
    # ==================================================================================

    def status(self) -> dict[str, Any]:
        with self._lock:
            sources = list(self._sources.values())
            included = [s for s in sources if s.included]
            online = [s for s in included if s.online]
            scanning = sorted((self._scan_entry(job) for job in self._jobs.values()
                               if job.kind != "forget" and job.source_id in self._sources),
                              key=lambda e: e["started"])
            ends = [s.last_scan_end for s in sources if s.last_scan_end is not None]
            worker = self._worker
            return {
                "hostname": self._own, "version": __version__,
                "sources_total": len(sources), "sources_online": len(online),
                "sources_offline": len(included) - len(online),
                "sources_excluded": len(sources) - len(included),
                "sources_ready": sum(1 for s in online if s.last_scan_end is not None
                                     or s.last_shallow_scan is not None),
                "sources_included_online": len(online),
                "entries": sum(s.entry_count for s in included),
                "files": sum(s.file_count for s in included),
                "dirs": sum(s.dir_count for s in included),
                "projects": sum(s.project_count
                                + int(self._root_project(s.root_is_project, s.display_name,
                                                         s.key))
                                for s in included),
                "scanning": scanning,
                "queued": sum(len(s.pending) for s in sources),
                "last_scan_end": max(ends) if ends else None,
                "initial_scan_done": self._initial_scan_done(online),
                "worker": {"running": worker is not None and worker.ready,
                           "restarts": self._worker_restarts},
                "db_size": self._db_size,
            }

    def list_sources(self) -> list[dict[str, Any]]:
        view = self._snapshot()
        own = self._own
        return sorted((dict(s) for s in view.sources.values()),
                      key=lambda s: (s["host"].upper() != own, s["host"].upper(),
                                     s["display_name"].casefold(), s["id"]))

    def hosts(self) -> list[dict[str, Any]]:
        with self._lock:
            out = [{"name": self._own, "online": True, "shares": self._local_share_count,
                    "last_seen": self._last_local_pass, "self": True}]
            by_host: dict[str, list[_Source]] = {}
            for src in self._sources.values():
                if src.kind == "share":
                    by_host.setdefault(src.host.upper(), []).append(src)
            names = list(self._hosts) + sorted(set(by_host) - set(self._hosts) - {self._own})
            for name in names:
                hs = self._hosts.get(name)
                srcs = by_host.get(name, [])
                last_seen = hs.last_seen if hs is not None else None
                if last_seen is None:
                    last_seen = max((s.last_seen for s in srcs if s.last_seen is not None),
                                    default=None)
                shares = hs.shares if hs is not None else None
                out.append({"name": name, "online": bool(hs is not None and hs.online),
                            "shares": len(shares) if shares is not None else len(srcs),
                            "last_seen": last_seen, "self": False})
            return out

    def search(self, q: str, kind: str = "all", online_only: bool | None = None,
               source_id: int | None = None, limit: int | None = None,
               include_templates: bool = False) -> dict[str, Any]:
        if online_only is None:
            online_only = not bool(self.cfg.get("show_offline", True))
        if limit is None:
            limit = int(self.cfg.get("result_limit") or config.DEFAULTS["result_limit"])
        if kind not in search.KIND_FILTERS:
            raise ValueError(f"Ukendt filter: {kind}")
        view = self._snapshot()
        readers = self._readers
        if readers is None:
            return {"query": q, "tokens": textutil.tokenize(q or ""), "took_ms": 0.0,
                    "total": 0, "truncated": False, "results": []}
        with readers.connection() as conn:
            return search.search(conn, view.sources, q, kind=kind, online_only=online_only,
                                 source_id=source_id, limit=limit,
                                 include_templates=include_templates)

    def recent_projects(self, limit: int = 30) -> list[dict[str, Any]]:
        view = self._snapshot()
        readers = self._readers
        if readers is None:
            return []
        online_only = not bool(self.cfg.get("show_offline", True))
        with readers.connection() as conn:
            return search.recent_projects(conn, view.sources, max(0, int(limit)),
                                          online_only=online_only)

    def children(self, source_id: int, rel_path: str) -> list[dict[str, Any]]:
        view = self._snapshot()
        if source_id not in view.sources:
            raise ValueError(ERR_UNKNOWN_SOURCE)
        readers = self._readers
        if readers is None:
            return []
        with readers.connection() as conn:
            return search.children(conn, view.sources, source_id, rel_path or "")

    def locate(self, path: str) -> dict[str, Any] | None:
        """Which source ``path`` lies in (any spelling: UNC of this PC, IP, mapped drive …)."""
        if not isinstance(path, str) or not path.strip():
            return None
        view = self._snapshot()
        entry = project = None
        readers = self._readers
        if readers is None:
            sid, rel = self._match(path.strip(), view)
            if sid is None:
                return None
            src = view.sources[sid]
        else:
            with readers.connection() as conn, db.read_snapshot(conn):
                sid, rel = self._match(path.strip(), view, conn)
                if sid is None:
                    return None
                src = view.sources[sid]
                row = db.nearest_entry(conn, sid, rel) if rel else None
                project_rel = None
                if row is not None:
                    project_rel = row["project_rel"]
                    if row["rel_path"].casefold() == rel.casefold():
                        entry = _entry_item(conn, row, src)
                if project_rel is None and self._root_project(src["root_is_project"],
                                                              src["display_name"], src["key"]):
                    project_rel = ""
                if project_rel is not None:
                    project = _project_ref(src, project_rel)
        return {"source": search.source_ref(src), "rel_path": rel, "path": _join(src["path"], rel),
                "unc_path": _join(src["unc_path"], rel), "online": bool(src["online"]),
                "entry": entry, "project": project}

    def map_paths(self, paths: list[str]) -> dict[str, Any]:
        """Group media file paths by the project folder that holds them (SPEC §8).

        Paths outside every project go to ``other_dirs`` by folder; ``online`` is None there
        when the folder is in no known location.  Only the index is read (no file system).
        """
        total = 0
        by_folder: dict[str, list[Any]] = {}           # casefolded folder → [folder, count]
        for path in paths or ():
            if not isinstance(path, str) or not path.strip():
                continue
            total += 1
            raw = path.strip().replace("/", "\\")
            cut = raw.rfind("\\")
            folder = raw[:cut] if cut > 0 else raw
            # [folder, count, a file name]: the file decides between disks that share a root
            sample = raw[cut + 1:] if cut > 0 else ""
            group = by_folder.setdefault(folder.casefold(), [folder, 0, sample])
            group[1] += 1
        dirs: dict[str, list[Any]] = {}                # the same, after alias normalisation
        for folder, count, sample in by_folder.values():
            norm = self._pathmap.normalize(folder)
            dirs.setdefault(norm.casefold(), [norm, 0, sample])[1] += count
        view = self._snapshot()
        readers = self._readers
        folders: dict[tuple[int, str], dict[str, Any]] = {}
        other: list[dict[str, Any]] = []
        if readers is None:
            return {"folders": [], "total": total,
                    "other_dirs": [{"path": p, "count": n, "online": None}
                                   for p, n, _sample in dirs.values()]}
        with readers.connection() as conn, db.read_snapshot(conn):
            for norm, count, sample in dirs.values():
                sid, rel = self._match_folder(norm, sample, view, conn)
                if sid is None:
                    other.append({"path": norm, "count": count, "online": None})
                    continue
                src = view.sources[sid]
                project_rel = self._project_of(conn, src, rel) if src["included"] else None
                if project_rel is None:
                    other.append({"path": _join(src["path"], rel), "count": count,
                                  "online": bool(src["online"])})
                    continue
                slot = folders.setdefault((sid, project_rel.casefold()), {
                    "project": _project_ref(src, project_rel), "source": search.source_ref(src),
                    "online": bool(src["online"]), "count": 0, "item": None})
                slot["count"] += count
            for (sid, _), slot in folders.items():
                rel = slot["project"]["rel_path"]
                row = db.get_entry(conn, sid, rel) if rel else None
                if row is not None:
                    slot["item"] = _entry_item(conn, row, view.sources[sid])
        result = sorted(folders.values(), key=lambda f: (-f["count"], not f["online"],
                                                         f["project"]["name"].casefold()))
        other.sort(key=lambda d: (-d["count"], d["path"].casefold()))
        return {"folders": result, "other_dirs": other, "total": total}

    def suggest_project_folders(self, name: str, limit: int = 5) -> list[dict[str, Any]]:
        """Project folders whose name resembles ``name`` (score 0..1, best first)."""
        if not isinstance(name, str) or not _name_tokens(name) or limit <= 0:
            return []
        view = self._snapshot()
        readers = self._readers
        if readers is None:
            return []
        live = {sid: s for sid, s in view.sources.items() if s["included"]}
        if not live:
            return []
        with readers.connection() as conn, db.read_snapshot(conn):
            templates = db.template_prefixes(conn)
            # (score, online, recency, name, source id, rel path, row | None for a root project)
            scored: list[tuple[float, bool, float, str, int, str, Any]] = []
            for row in db.entries_of_kind(conn, (db.KIND_PROJECT,), list(live)):
                prefixes = templates.get(row["source_id"])
                if prefixes and row["rel_path"].startswith(prefixes):
                    continue
                score = name_similarity(name, row["name"])
                if score >= SUGGEST_MIN_SCORE:
                    src = live[row["source_id"]]
                    scored.append((score, bool(src["online"]), _recency(row["mtime"]),
                                   row["name"], row["source_id"], row["rel_path"], row))
            for sid, src in live.items():          # sources whose root is the project (§15.3)
                if self._root_project(src["root_is_project"], src["display_name"], src["key"]):
                    score = name_similarity(name, src["display_name"])
                    if score >= SUGGEST_MIN_SCORE:
                        newest = conn.execute("SELECT max(mtime) FROM entries WHERE source_id = ?"
                                              " AND parent_rel = ''", (sid,)).fetchone()[0]
                        scored.append((score, bool(src["online"]), _recency(newest),
                                       src["display_name"], sid, "", None))
            scored.sort(key=lambda s: (-s[0], not s[1], -s[2], s[3].casefold()))
            out = []
            for score, online, _recent, _name, sid, rel, row in scored[:limit]:
                src = live[sid]
                out.append({"project": _project_ref(src, rel), "source": search.source_ref(src),
                            "online": online, "score": score,
                            "item": _entry_item(conn, row, src) if row is not None else None})
            return out

    # -- query helpers -----------------------------------------------------------------
    def _snapshot(self) -> _View:
        with self._lock:
            view = self._view
            if view is not None and view.version == self._version:
                return view
            sources = {sid: self._source_dict(src) for sid, src in self._sources.items()}
            index: dict[str, list[int]] = {}
            for sid, src in self._sources.items():
                # A share is also reachable under its canonical \\HOST\share\rel (from its key).
                canonical = "\\\\" + src.key[4:] if src.kind == "share" and \
                    src.key.startswith("unc:") else None
                paths = (src.current_path, src.unc_path, canonical)
                for key in dict.fromkeys(k for p in paths for k in _index_keys(p)):
                    index.setdefault(key, []).append(sid)
            view = self._view = _View(self._version, sources, index)
            return view

    def _match(self, path: str, view: _View,
               conn: sqlite3.Connection | None = None) -> tuple[int | None, str]:
        """``(source id, rel path)`` of ``path``; with ``conn`` a root shared by several
        sources goes to the one whose index knows the path (:func:`_pick_source`)."""
        norm = self._pathmap.normalize(path)
        ids, rel = _match_index(norm, view)
        if not ids:
            cleaned = clean_path(path)
            if cleaned != norm:
                ids, rel = _match_index(cleaned, view)
        if not ids:
            return None, ""
        return _pick_source(ids, rel, view, conn), rel

    def _match_folder(self, folder: str, sample: str, view: _View,
                      conn: sqlite3.Connection) -> tuple[int | None, str]:
        """:meth:`_match` for a folder of media files: one of its files (``sample``) decides
        between disks that share a root (the folder can exist on both)."""
        if sample:
            sid, rel = self._match(_join(folder, sample) or folder, view, conn)
            if sid is not None and rel:
                return sid, rel.rpartition("\\")[0]
        return self._match(folder, view, conn)

    def _project_of(self, conn: sqlite3.Connection, src: Mapping[str, Any],
                    rel: str) -> str | None:
        """Rel path of the project holding folder ``rel`` ("" = the source root), or None."""
        row = db.nearest_entry(conn, src["id"], rel) if rel else None
        if row is not None and row["project_rel"] is not None:
            return row["project_rel"]
        return "" if self._root_project(src["root_is_project"], src["display_name"],
                                        src["key"]) else None

    def _root_project(self, root_is_project: Any, name: str | None, key: str) -> bool:
        """The source's root is a project folder: not a template root (search hides it like a
        template), and never a folder named like a template sub-folder - also while a flag
        from before a settings change is not re-read yet (SPEC §15.12)."""
        return (bool(root_is_project) and not discovery.is_template_name(name or "", self.cfg)
                and not _project_part_root(key, self.cfg))

    def _is_system(self, src: _Source) -> bool:
        """A local source on the system volume (SPEC §15.4); lock held."""
        return src.kind == "local" and (src.volume_serial or "").upper() in self._system_serials

    def _volume_present(self, src: _Source) -> bool:
        """Source.volume_present (SPEC §15.12; lock held): the source's disk is mounted (local
        source: its volume serial was seen by the last local pass) or its computer answers
        (share).  An offline source whose disk is present lost its folder, not its disk."""
        if src.online:
            return True
        if src.kind == "local":
            return (src.volume_serial or "").upper() in self._present_serials
        hs = self._hosts.get(src.host.upper())
        return hs is not None and hs.online

    def _presence_changed(self, src: _Source, *, initial: bool) -> None:
        """``volume_present`` of ``src`` changed (lock held): refresh the view and the
        ``sources`` event; shown rows of an offline source carry the old offline hint
        (``index_updated``, not on the first pass after startup, SPEC §15.1)."""
        self._touch(src)
        if not src.online and not initial:
            self._index_updated.put(src.id, True)

    def _source_dict(self, src: _Source) -> dict[str, Any]:
        """Source (SPEC §7.1, §15.3, §15.4, §15.12); lock held."""
        job = src.job
        scanning = job is not None and job.kind != "forget"
        is_system = self._is_system(src)
        return {
            "id": src.id, "key": src.key, "kind": src.kind, "display_name": src.display_name,
            "host": src.host, "path": src.current_path, "unc_path": src.unc_path,
            "volume_label": src.volume_label, "volume_serial": src.volume_serial, "fs": src.fs,
            "drive": search.drive_of(src.current_path), "last_drive": src.last_drive,
            "disk_name": search.disk_name({"kind": src.kind, "volume_label": src.volume_label,
                                           "volume_size": src.volume_size,
                                           "last_drive": src.last_drive,
                                           "path": src.current_path, "is_system": is_system}),
            "hotplug": bool(src.hotplug), "volume_size": src.volume_size,
            "online": bool(src.online), "mode": src.mode, "included": src.included,
            "auto_reason": src.auto_reason, "manual": bool(src.manual),
            "entry_count": src.entry_count, "dir_count": src.dir_count,
            "file_count": src.file_count,
            "project_count": src.project_count + int(self._root_project(
                src.root_is_project, src.display_name, src.key)),
            "total_size": src.total_size, "last_scan_end": src.last_scan_end,
            "last_shallow_scan": src.last_shallow_scan,
            "last_scan_ok": None if src.last_scan_ok is None else bool(src.last_scan_ok),
            "last_error": src.last_error, "last_seen": src.last_seen,
            "scanning": scanning, "scan_kind": job.kind if scanning else None,
            "queued": bool(src.pending) and job is None,
            "is_system": is_system,
            "root_is_project": bool(src.root_is_project)
            and not _project_part_root(src.key, self.cfg),
            "volume_present": self._volume_present(src),
        }

    def _scan_entry(self, job: _Job) -> dict[str, Any]:
        entry = {"source_id": job.source_id, "name": job.name, "kind": job.kind}
        entry.update({k: int(job.progress.get(k, 0)) for k in _PROGRESS_FIELDS})
        entry.update(started=job.started, full=job.full)
        return entry

    def _initial_scan_done(self, online_included: list[_Source]) -> bool:
        if self._local_passes == 0 or any(hs.passes == 0 for hs in self._hosts.values()):
            return False
        if any(s.online and s.mode == "auto" and not s.manual and s.auto_include is None
               for s in self._sources.values()):
            return False
        return all(s.last_scan_end is not None for s in online_included)

    # ==================================================================================
    # Commands (validate, update, wake; no file-system I/O)
    # ==================================================================================

    def scan_now(self, source_id: int | None = None, full: bool = False) -> None:
        with self._lock:
            if source_id is None:
                for src in self._sources.values():
                    if full and src.included:
                        src.needs_full = True       # also for locations that are offline now
                    if src.online and src.included:
                        self._request_deep(src, full=bool(full))
            else:
                src = self._get(source_id)
                if not src.online:
                    raise ValueError(ERR_SOURCE_OFFLINE)
                if not src.included:
                    raise ValueError(ERR_SOURCE_EXCLUDED)
                self._request_deep(src, full=bool(full))
        if source_id is None:
            self._kick_discovery()
        self._wake.set()

    def on_window_shown(self) -> None:
        with self._lock:
            if self._window_due is None:
                self._window_due = time.monotonic() + WINDOW_SHOWN_DELAY_S
        self._wake.set()

    def set_source_mode(self, source_id: int, mode: str) -> dict[str, Any]:
        if mode not in MODES:
            raise ValueError(ERR_BAD_MODE.format(mode))
        now = self._clock()
        with self._lock:
            src = self._get(source_id)
            was_included = src.included
            self._set(src, mode=mode)
            log.info("%s: mode %s", src.display_name, mode)
            if src.included and not was_included:
                self._queue_start(src, now, None)
            elif was_included and not src.included:
                self._drop_work(src)
            if self._probe_needed(src, now):
                self._queue_probe(src)
            return dict(self._source_dict(src))

    def forget_source(self, source_id: int) -> None:
        with self._lock:
            src = self._get(source_id)
            if src.online:
                raise ValueError(ERR_FORGET_ONLINE)
            manual = bool(src.manual)
            self._remove_source(src)
        if manual:
            polled = self._desired_hosts(self.cfg.snapshot())
            self._drop_extra_roots(src)
            self._forget_host_shares(polled)        # as remove_root()
        self._wake.set()

    def add_root(self, path: str) -> dict[str, Any]:
        clean = self._valid_root(path)
        roots = [str(r) for r in self.cfg.get("extra_roots") or []]
        want = self._pathmap.key(clean)
        if not any(self._pathmap.key(r) == want for r in roots):
            self._refuse_nested_root(clean)
            self.cfg.update({"extra_roots": roots + [clean]})
            log.info("Added root %s", clean)
        source = self._register_manual(clean)
        self._kick_discovery()
        return {"ok": True, "source": source}

    def remove_root(self, path: str) -> None:
        if not isinstance(path, str) or not path.strip():
            raise ValueError(ERR_ROOT_UNKNOWN.format(path))
        roots = [str(r) for r in self.cfg.get("extra_roots") or []]
        want = self._pathmap.key(path.strip())
        keep = [r for r in roots if r != path and self._pathmap.key(r) != want]
        if len(keep) == len(roots):
            raise ValueError(ERR_ROOT_UNKNOWN.format(path.strip()))
        polled = self._desired_hosts(self.cfg.snapshot())
        self.cfg.update({"extra_roots": keep})
        log.info("Removed root %s", path)
        key = self._share_key(path.strip())
        now = self._clock()
        with self._lock:
            for src in list(self._sources.values()):
                if not src.manual or not self._same_root(src, want, key):
                    continue
                self._set(src, manual=0)
                if not src.auto_candidate and src.probed_at is None:
                    self._remove_source(src)          # it only existed because of the root
                elif not src.included:
                    self._drop_work(src)
                if src.id in self._sources and self._probe_needed(src, now):
                    self._queue_probe(src)
        # A computer polled only for this root: its other shares would stay behind offline.
        self._forget_host_shares(polled)
        self._kick_discovery()

    def add_host(self, name: str) -> None:
        host = self._valid_host(name)
        hosts = [str(h) for h in self.cfg.get("hosts") or []]
        if any(_host_name(h) == host for h in hosts):
            return
        self.cfg.update({"hosts": hosts + [host]})
        log.info("Added host %s", host)

    def remove_host(self, name: str) -> dict[str, Any]:
        """Stop searching ``name``: its shares are forgotten (index deleted by the worker),
        except shares still reached through a mapped drive (SPEC §15.8).  Returns
        ``{"ok": True, "forgotten": <shares forgotten>}``.  Refused (nothing changes) while a
        folder added by hand lies on that computer: it keeps all its shares searched
        (SPEC §15.12, R2-IDX-3)."""
        host = self._valid_host(name)
        cfg = self.cfg.snapshot()
        hosts = [str(h) for h in cfg.get("hosts") or []]
        keep = [h for h in hosts if _host_name(h) != host]
        if len(keep) == len(hosts):
            raise ValueError(ERR_HOST_UNKNOWN.format(host))
        root = self._root_hosts(cfg).get(host)
        if root is not None:
            raise ValueError(ERR_HOST_HAS_ROOT.format(root, host))
        polled = self._desired_hosts(cfg)
        self.cfg.update({"hosts": keep})
        log.info("Removed host %s", host)
        return {"ok": True, "forgotten": self._forget_host_shares(polled)}

    def _forget_host_shares(self, polled: Mapping[str, bool]) -> int:
        """Forget the shares of computers whose shares are no longer all searched.

        ``polled`` is :meth:`_desired_hosts` from before a config change.  A computer that was
        polled with all its shares and now is not (removed, or only polled for a mapped drive)
        would leave its shares offline forever (LOC-1): they are forgotten, except manual
        roots and shares still reached through a mapped drive.  Its poll thread is stopped (or
        switched) first, so no pass in flight brings them back.  Returns the number forgotten.
        """
        cfg = self.cfg.snapshot()
        self._reconcile_hosts(cfg)
        now_polled = self._desired_hosts(cfg)
        lost = {h for h, every_share in polled.items() if every_share and not now_polled.get(h)}
        if not lost:
            return 0
        with self._lock:
            mapped = dict(self._mapped)
        still_mapped = {c["key"].casefold() for c in discovery.mapped_candidates(
            mapped, pathmap=self._pathmap, own_host=self._own)}
        with self._lock:
            gone = [s for s in self._sources.values()
                    if s.kind == "share" and s.host.upper() in lost and not s.manual
                    and s.key.casefold() not in still_mapped]
            for src in gone:
                self._remove_source(src)
        if gone:
            log.info("Forgot %d shared folder(s) of %s", len(gone), ", ".join(sorted(lost)))
        self._wake.set()
        return len(gone)

    def path_missing(self, path: str) -> None:
        """A path from the index was not found: refresh its source (rate limited)."""
        self._refresh_source_of(path, rate_limited=True)

    def refresh_path(self, path: str) -> None:
        """Something was created at ``path`` (a new project, imported clips): rescan its source."""
        self._refresh_source_of(path, rate_limited=False)

    def find_files(self, names: Collection[str]) -> list[dict[str, Any]]:
        """Indexed files named exactly one of ``names`` (case-insensitive), with their project:
        where the clips of a camera card already are."""
        wanted = {n.casefold() for n in names if isinstance(n, str) and n}
        view = self._snapshot()
        readers = self._readers
        if not wanted or readers is None:
            return []
        out: list[dict[str, Any]] = []
        with readers.connection() as conn, db.read_snapshot(conn):
            for row in db.files_named(conn, {textutil.fold(n) for n in wanted}):
                src = view.sources.get(row["source_id"])
                if src is None or row["name"].casefold() not in wanted:
                    continue
                project_rel = row["project_rel"]
                if project_rel is None and self._root_project(src["root_is_project"],
                                                              src["display_name"], src["key"]):
                    project_rel = ""
                out.append({"name": row["name"], "size": row["size"],
                            "path": _join(src["path"], row["rel_path"]),
                            "folder": _join(src["path"], row["parent_rel"]),
                            "online": bool(src["online"]), "volume_serial": src["volume_serial"],
                            "project": None if project_rel is None else _project_ref(src, project_rel)})
        return out

    def templates(self) -> list[dict[str, Any]]:
        """The project templates ("1. KUNDENAVN") of included sources; their parent folders are
        where new projects go."""
        view = self._snapshot()
        readers = self._readers
        live = {sid: s for sid, s in view.sources.items() if s["included"]}
        if readers is None or not live:
            return []
        with readers.connection() as conn, db.read_snapshot(conn):
            rows = db.entries_of_kind(conn, (db.KIND_TEMPLATE,), list(live))
        return [{"path": _join(live[r["source_id"]]["path"], r["rel_path"]),
                 "parent": _join(live[r["source_id"]]["path"], r["parent_rel"]),
                 "source": search.source_ref(live[r["source_id"]]),
                 "online": bool(live[r["source_id"]]["online"])} for r in rows]

    def projects_named(self, names: Collection[str]) -> list[dict[str, Any]]:
        """Project folders named exactly one of ``names`` (case-insensitive), newest first."""
        wanted = {n.casefold() for n in names if isinstance(n, str) and n}
        view = self._snapshot()
        readers = self._readers
        live = {sid: s for sid, s in view.sources.items() if s["included"]}
        if not wanted or readers is None or not live:
            return []
        with readers.connection() as conn, db.read_snapshot(conn):
            rows = [r for r in db.entries_of_kind(conn, (db.KIND_PROJECT,), list(live))
                    if r["name"].casefold() in wanted]
        rows.sort(key=lambda r: -(r["mtime"] or 0))
        return [{**_project_ref(live[r["source_id"]], r["rel_path"]),
                 "source": search.source_ref(live[r["source_id"]]),
                 "online": bool(live[r["source_id"]]["online"])} for r in rows]

    def _refresh_source_of(self, path: str, *, rate_limited: bool) -> None:
        if not isinstance(path, str) or not path.strip():
            return
        view = self._snapshot()
        readers = self._readers
        if readers is None:
            sid, _rel = self._match(path.strip(), view)
        else:       # a path of a disconnected disk must not rescan the disk now at its letter
            with readers.connection() as conn, db.read_snapshot(conn):
                sid, _rel = self._match(path.strip(), view, conn)
        if sid is None:
            return
        now = self._clock()
        with self._lock:
            src = self._sources.get(sid)
            if src is None or not src.online or not src.included:
                return
            if rate_limited:
                if src.missing_at is not None and now - src.missing_at < self._base_interval(src):
                    return
                src.missing_at = now
            self._request(src, "shallow", max_listings=self._shallow_listings(src))
            self._request_deep(src)
        self._wake.set()

    # -- command helpers ----------------------------------------------------------------
    def _get(self, source_id: Any) -> _Source:
        src = self._sources.get(source_id) if isinstance(source_id, int) else None
        if src is None:
            raise ValueError(ERR_UNKNOWN_SOURCE)
        return src

    @staticmethod
    def _valid_root(path: Any) -> str:
        if not isinstance(path, str) or not path.strip():
            raise ValueError(ERR_NOT_ABSOLUTE)
        clean = clean_path(path.strip())
        unc = split_unc(clean)
        drive = len(clean) >= 3 and clean[1:3] == ":\\" and clean[0].isascii() \
            and clean[0].isalpha()
        if not drive and not (unc is not None and unc[1]):
            raise ValueError(ERR_NOT_ABSOLUTE)
        return clean

    @staticmethod
    def _valid_host(name: Any) -> str:
        if not isinstance(name, str):
            raise ValueError(ERR_BAD_HOST.format(name))
        host = _host_name(name)
        if not _HOST_RE.fullmatch(host):
            raise ValueError(ERR_BAD_HOST.format(name.strip()))
        return host

    def _share_key(self, path: str) -> str | None:
        unc = split_unc(self._pathmap.normalize(path))
        if unc is None or not unc[1]:
            return None
        return discovery.key_share(unc[0], unc[1], unc[2]).casefold()

    def _same_root(self, src: _Source, path_key: str, share_key: str | None) -> bool:
        return (src.key.casefold() == share_key
                or any(p and self._pathmap.key(p) == path_key
                       for p in (src.current_path, src.unc_path)))

    def _drop_extra_roots(self, src: _Source) -> None:
        """Remove the ``extra_roots`` entries that are ``src``'s root (by path or, for a share,
        by its key - the same rule as :meth:`remove_root`)."""
        roots = [str(r) for r in self.cfg.get("extra_roots") or []]
        keep = [r for r in roots
                if not self._same_root(src, self._pathmap.key(r), self._share_key(r))]
        if len(keep) != len(roots):
            self.cfg.update({"extra_roots": keep})

    def _manual_candidate(self, path: str) -> dict | None:
        """Candidate of root ``path`` from the last known volumes (no I/O); None if its
        volume is not present."""
        with self._lock:
            volumes = list(self._volumes)
        cands = discovery.extra_root_candidates({"extra_roots": [path]}, volumes, self._own,
                                                pathmap=self._pathmap)
        return self._with_share_path(cands[0]) if cands else None

    def _refuse_nested_root(self, path: str) -> None:
        """ValueError when ``path`` lies inside (or contains) an included source (§15.8):
        its folders would be indexed twice."""
        cand = self._manual_candidate(path)
        norm = self._pathmap.normalize(path)
        with self._lock:
            for src in self._sources.values():
                if not src.included:
                    continue
                if cand is not None:
                    nested = _keys_nested(cand["key"], src.key)
                else:           # volume unknown: compare the paths of connected locations
                    nested = src.online and any(
                        p and (is_within(norm, p) or is_within(p, norm))
                        for p in (src.current_path, src.unc_path))
                if nested:
                    raise ValueError(ERR_ALREADY_INCLUDED.format(src.display_name))

    def _register_manual(self, path: str) -> dict[str, Any] | None:
        """Source for a just-added root, from the last known volumes (no I/O); None if its
        volume is unknown.  Discovery confirms it (online) within moments."""
        cand = self._manual_candidate(path)
        if cand is None:
            return None
        now = self._clock()
        with self._lock:
            src = self._sources.get(self._by_key.get(cand["key"].casefold(), -1))
            if src is None:
                src = self._create_source(cand, now)
                self._set(src, manual=1)
            elif not src.manual:
                was_included = src.included
                self._set(src, manual=1)
                if src.online and not was_included:
                    self._queue_start(src, now, None)
            return dict(self._source_dict(src))

    def _kick_discovery(self) -> None:
        self._local_wake.set()
        with self._lock:
            for hs in self._hosts.values():
                hs.wake.set()

    # ==================================================================================
    # Registry mutation (lock held)
    # ==================================================================================

    def _touch(self, src: _Source, *fields: str) -> None:
        """Record a change: persisted ``fields`` go to the DB thread; every change refreshes
        the query view and the ``sources``/``status`` events."""
        if fields:
            self._dirty.setdefault(src.id, set()).update(fields)
            self._db_wake.set()
        self._version += 1
        self._changed.add(src.id)
        self._status_dirty = True
        self._wake.set()

    def _set(self, src: _Source, **values: Any) -> bool:
        changed = [name for name, value in values.items() if getattr(src, name) != value]
        if not changed:
            return False
        for name in changed:
            setattr(src, name, values[name])
        self._touch(src, *(n for n in changed if n in _PERSISTED_SET))
        return True

    def _new_id(self) -> int:
        sid = self._next_id
        self._next_id += 1
        self._meta_dirty.add("last_source_id")
        return sid

    def _create_source(self, cand: dict, now: float) -> _Source:
        src = _Source(id=self._new_id(), key=cand["key"], kind=cand["kind"], host=cand["host"],
                      share=cand.get("share"), display_name=cand["display_name"],
                      current_path=cand["path"], unc_path=cand.get("unc_path"),
                      volume_serial=cand.get("volume_serial"),
                      volume_label=cand.get("volume_label"), fs=cand.get("fs") or None,
                      volume_size=cand.get("volume_size") or None, last_drive=cand.get("drive"),
                      hotplug=int(bool(cand.get("hotplug"))), manual=int(bool(cand.get("manual"))),
                      first_seen=now, persisted=False)
        self._sources[src.id] = src
        self._by_key[src.key.casefold()] = src.id
        self._touch(src, *_PERSISTED)
        log.info("New location %s (%s)", src.display_name, src.current_path)
        return src

    def _upsert(self, cand: dict, now: float, *, auto: bool, initial: bool = False) -> _Source:
        """Create or refresh the source of a present candidate; it is online afterwards.

        ``initial``: part of the first discovery pass after startup (no ``index_updated``).
        """
        src = self._sources.get(self._by_key.get(cand["key"].casefold(), -1))
        if src is None:
            src = self._create_source(cand, now)
        paths = (src.current_path, src.unc_path)
        values: dict[str, Any] = {
            "kind": cand["kind"], "host": cand["host"], "share": cand.get("share"),
            "display_name": cand["display_name"], "current_path": cand["path"],
            "unc_path": cand.get("unc_path"), "volume_serial": cand.get("volume_serial"),
            "volume_label": cand.get("volume_label"), "hotplug": int(bool(cand.get("hotplug"))),
            "manual": int(bool(cand.get("manual"))),
        }
        if cand.get("fs"):
            values["fs"] = cand["fs"]
        if cand.get("volume_size"):
            values["volume_size"] = int(cand["volume_size"])
        if cand.get("drive"):
            values["last_drive"] = cand["drive"]
        if src.current_path != cand["path"]:
            log.info("%s moved: %s -> %s", src.display_name, src.current_path, cand["path"])
        was_included = src.included
        self._set(src, **values)
        src.auto_candidate = auto
        src.last_seen = now
        if src.seen_written is None or now - src.seen_written >= LAST_SEEN_WRITE_S:
            src.seen_written = now
            self._dirty.setdefault(src.id, set()).add("last_seen")
            self._db_wake.set()
            self._version += 1
        if not src.online:
            self._go_online(src, now, initial=initial)
            return src
        if not initial and paths != (src.current_path, src.unc_path):
            self._index_updated.put(src.id, True)       # results carry the old paths
        if src.included and not was_included:           # became a manual root
            self._queue_start(src, now, None)
        elif was_included and not src.included:
            self._drop_work(src)
        return src

    def _go_online(self, src: _Source, now: float, *, initial: bool = False) -> None:
        offline_for = None if src.offline_since is None else now - src.offline_since
        src.offline_since = None
        src.hold_until, src.swap_strikes = _NEVER, 0
        self._set(src, online=1)
        if not initial:         # shown results of this source are dimmed as offline (§15.1)
            self._index_updated.put(src.id, True)
        log.info("%s is online (%s)", src.display_name, src.current_path)
        if src.mode == "auto" and not src.manual and src.auto_include == 0:
            src.reprobe = True                      # re-probe when it comes online again
        self._queue_start(src, now, offline_for)

    def _go_offline(self, src: _Source, now: float) -> None:
        if not src.online:
            return
        src.offline_since = now
        src.seen_written = src.last_seen
        src.hold_until, src.swap_strikes = _NEVER, 0
        self._set(src, online=0)
        self._touch(src, "last_seen")
        self._index_updated.put(src.id, True)
        self._drop_work(src)
        log.info("%s is offline", src.display_name)

    def _drop_work(self, src: _Source) -> None:
        """Forget queued scans and cancel the running job of ``src``."""
        if src.pending:
            src.pending.clear()
            self._touch(src)
        job = src.job
        if job is not None and not job.cancel_sent:
            job.cancel_sent = True
            self._cancels.append(job.id)
            self._wake.set()

    def _remove_source(self, src: _Source) -> None:
        """Take ``src`` out of the registry; its entries are deleted by a worker ``forget``."""
        self._drop_work(src)
        del self._sources[src.id]
        self._by_key.pop(src.key.casefold(), None)
        self._dirty.pop(src.id, None)
        self._root_check.discard(src.id)
        self._progress_dirty.discard(src.id)
        self._index_updated.put(src.id, True)       # its results disappear
        if src.persisted:
            self._forget_ids.add(src.id)
            self._meta_dirty.add("pending_forget")
            self._db_ops.append(("rename", src.id, src.key))   # frees the key for rediscovery
            self._forget_due[src.id] = 0.0
            self._db_wake.set()
        self._version += 1
        self._changed.add(src.id)
        self._status_dirty = True
        self._wake.set()
        log.info("Forgot location %s (%s)", src.display_name, src.current_path)

    def _apply_candidates(self, cands: Iterable[dict], auto_keys: set[str], unknown: set[str],
                          scope: Callable[[_Source], bool], now: float, *,
                          initial: bool = False) -> None:
        """Merge one discovery pass: candidates are online, other sources in ``scope`` offline
        (except those whose presence is ``unknown`` this time).  ``initial``: the first pass
        after startup, which brings every present source online without ``index_updated``."""
        seen: set[int] = set()
        for cand in cands:
            key = cand["key"].casefold()
            seen.add(self._upsert(cand, now, auto=key in auto_keys, initial=initial).id)
        for src in list(self._sources.values()):
            if (src.online and src.id not in seen and scope(src)
                    and src.key.casefold() not in unknown):
                self._go_offline(src, now)

    # ==================================================================================
    # Scan requests (lock held)
    # ==================================================================================

    def _request(self, src: _Source, kind: str, *, full: bool = False, first_time: bool = False,
                 max_listings: int = FIRST_SHALLOW_LISTINGS, window: bool = False,
                 retried: bool = False) -> None:
        pending = src.pending.get(kind)
        if pending is None:
            src.pending[kind] = _Pending(kind, time.monotonic(), full=full, first_time=first_time,
                                         max_listings=max_listings, window=window,
                                         retried=retried)
        else:
            pending.full |= full
            pending.first_time |= first_time
            pending.max_listings = max(pending.max_listings, max_listings)
            pending.window &= window
            pending.retried &= retried
        self._touch(src)

    def _request_deep(self, src: _Source, *, full: bool = False) -> None:
        """A deep scan; a never-scanned source gets its first-time shallow scan first."""
        if src.last_scan_end is None and src.last_shallow_scan is None:
            self._request(src, "shallow", first_time=True, max_listings=FIRST_SHALLOW_LISTINGS)
        self._request(src, "deep", full=full)

    def _queue_start(self, src: _Source, now: float, offline_for: float | None) -> None:
        """Scans for a source that just became online and/or included."""
        if not (src.online and src.included):
            return
        if (src.last_scan_end is not None and offline_for is not None
                and offline_for < RETURN_RESCAN_MIN_OFFLINE_S):
            return
        self._request_deep(src)

    def _shallow_listings(self, src: _Source) -> int:
        return NETWORK_SHALLOW_LISTINGS if src.kind == "share" else LOCAL_SHALLOW_LISTINGS

    def _cfg_int(self, key: str, minimum: int) -> int:
        try:
            value = int(self.cfg.get(key))
        except (TypeError, ValueError):
            value = int(config.DEFAULTS[key])
        return max(minimum, value)

    def _base_interval(self, src: _Source) -> float:
        key = "scan_interval_network_min" if src.kind == "share" else "scan_interval_local_min"
        return 60.0 * self._cfg_int(key, 1)

    def _deep_interval(self, src: _Source) -> float:
        """Seconds between periodic deep scans: local ``scan_interval_local_min``; network
        ``max(scan_interval_network_min, 4 × scan_seconds)``."""
        base = self._base_interval(src)
        if src.kind == "share" and src.scan_seconds:
            return max(base, 4.0 * src.scan_seconds)
        return base

    def _full_due(self, src: _Source, now: float) -> bool:
        hours = self._cfg_int("full_rescan_hours", 1)
        return (src.needs_full or src.last_full_scan is None
                or now - src.last_full_scan >= hours * 3600.0)

    def _deep_cost(self, src: _Source) -> float:
        """Queue order within a device: last scan_seconds, else the shallow scan's dir count."""
        if src.scan_seconds is not None:
            return src.scan_seconds
        return DIR_COST_S * (src.dir_count or 0)

    def _queue_periodic(self, now: float) -> None:
        for src in self._sources.values():
            if not (src.online and src.included) or src.pending or src.job is not None:
                continue
            if src.last_scan_end is None or now - src.last_scan_end >= self._deep_interval(src):
                self._request_deep(src)

    def _queue_window_round(self, now: float) -> None:
        for src in self._sources.values():
            if not (src.online and src.included and src.persisted) or src.job is not None \
                    or "shallow" in src.pending:
                continue
            last = max(src.last_shallow_scan or _NEVER, src.last_scan_end or _NEVER)
            if now - last >= WINDOW_SHOWN_MIN_AGE_S:
                self._request(src, "shallow", max_listings=self._shallow_listings(src),
                              window=True)

    # ==================================================================================
    # Scheduler thread
    # ==================================================================================

    def _scheduler_loop(self) -> None:
        while not self._stopping.is_set():
            self._wake.clear()
            try:
                timeout = self._scheduler_step()
            except Exception:
                log.exception("Scheduler step failed")
                timeout = SCHEDULER_TICK_S
            self._wake.wait(timeout)

    def _scheduler_step(self) -> float:
        self._drain_inbox()
        self._apply_config_change()
        self._refresh_root_projects()
        self._supervise_worker()
        now_m = time.monotonic()
        now = self._clock()
        with self._lock:
            self._queue_periodic(now)
            if self._window_due is not None and now_m >= self._window_due:
                self._window_due = None
                self._queue_window_round(now)
            events = self._due_announcements(now_m)
            commands: list[dict] = [{"cmd": "cancel", "job": job_id} for job_id in self._cancels]
            self._cancels.clear()
            commands += self._dispatch(now_m, now)
            events += self._due_events(now_m)
            busy = bool(self._changed or self._index_updated or self._progress_dirty
                        or self._status_dirty or self._window_due is not None)
            worker = self._worker
        for command in commands:
            if worker is None or not worker.send(command):
                break                       # the exit handler re-queues the jobs
        for event_type, data in events:
            self.bus.publish(event_type, data)
        if now_m >= self._db_size_at:
            self._db_size_at = now_m + DB_SIZE_REFRESH_S
            self._db_size = self._index_size()
        return PROGRESS_INTERVAL_S if busy else SCHEDULER_TICK_S

    def _index_size(self) -> int:
        size = 0
        for suffix in ("", "-wal"):
            try:
                size += os.path.getsize(self.db_path + suffix)
            except OSError:
                pass
        return size

    def _refresh_root_projects(self) -> None:
        """Re-read ``root_is_project`` of sources whose index or rules changed (SPEC §15.3).

        The flag decides whether search shows the source's root as a project, so a change
        publishes ``sources`` and ``index_updated``.
        """
        readers = self._readers
        with self._lock:
            if not self._root_check or readers is None:
                return
            todo = {sid: self._sources[sid].key for sid in self._root_check & set(self._sources)}
            self._root_check.clear()
        cfg = self.cfg.snapshot()
        try:
            with readers.connection() as conn, db.read_snapshot(conn):
                flags = {sid: _root_project_flag(conn, sid, cfg, key) for sid, key in todo.items()}
        except sqlite3.Error as exc:
            log.warning("Cannot read the root folders of %d location(s): %s", len(todo), exc)
            with self._lock:
                self._root_check.update(todo)
            return
        with self._lock:
            for sid, flag in flags.items():
                src = self._sources.get(sid)
                if src is not None and src.root_is_project != flag:
                    src.root_is_project = flag
                    self._touch(src)
                    self._index_updated.put(sid, True)
                    log.info("The root of %s %s a project folder", src.display_name,
                             "is" if flag else "is no longer")

    def _dispatch(self, now_m: float, now: float) -> list[dict]:
        """Jobs to send now (lock held).

        At most ``max_parallel_scans`` jobs in flight and one per source; shallow scans first
        (a first-time shallow always precedes its source's deep scan); one deep scan per
        device (host or volume), smallest first, and one slot kept free of deep scans for quick
        shallow ones; window-shown rounds visit one network share at a time.
        """
        worker = self._worker
        if worker is None or not worker.ready or self._stopping.is_set():
            return []
        limit = self._cfg_int("max_parallel_scans", 1)
        commands: list[dict] = []
        for sid, due in list(self._forget_due.items()):
            if len(self._jobs) >= limit:
                return commands
            if due <= now_m:
                del self._forget_due[sid]
                job = self._add_job(_Job(0, sid, "forget", name=f"#{sid}"))
                commands.append({"cmd": "forget", "job": job.id, "source_id": sid})
        running = list(self._jobs.values())
        capacity = limit - len(running)
        if capacity <= 0:
            return commands
        deep_cap = max(1, limit - 1)
        busy = {j.source_id for j in running}
        deep_devices = {j.device for j in running if j.kind == "deep"}
        deep_count = len([j for j in running if j.kind == "deep"])
        window_busy = any(j.window and j.host for j in running)
        queue: list[tuple[tuple, _Source, _Pending]] = []
        for src in self._sources.values():
            if not src.pending:
                continue
            if not (src.online and src.included):
                src.pending.clear()
                self._touch(src)
                continue
            if src.persisted and src.hold_until <= now_m:
                queue.extend((self._priority(src, p), src, p) for p in src.pending.values())
        queue.sort(key=lambda item: item[0])
        for _prio, src, p in queue:
            if capacity <= 0:
                break
            if src.id in busy:
                continue
            host = src.host.upper() if src.kind == "share" else None
            if p.kind == "deep":
                first = src.pending.get("shallow")
                if (src.device in deep_devices or deep_count >= deep_cap
                        or (first is not None and first.first_time)):
                    continue
            elif host is not None and p.window and window_busy:
                continue                    # window rounds: one network share at a time
            full = p.kind == "deep" and (p.full or self._full_due(src, now))
            job = self._add_job(_Job(0, src.id, p.kind, name=src.display_name,
                                     device=src.device, host=host, full=full,
                                     first_time=p.first_time, max_listings=p.max_listings,
                                     window=p.window, retried=p.retried, started=now))
            del src.pending[p.kind]
            src.job = job
            self._touch(src)
            busy.add(src.id)
            capacity -= 1
            if p.kind == "deep":
                deep_devices.add(src.device)
                deep_count += 1
            elif host is not None and p.window:
                window_busy = True
            command = {"cmd": "scan", "job": job.id, "source_id": src.id,
                       "root_path": src.current_path, "kind": p.kind, "full": full,
                       "first_time": p.first_time, "max_listings": p.max_listings,
                       "is_network": src.kind == "share", "fs": src.fs or ""}
            if src.kind == "local" and src.volume_serial:
                # The worker aborts without writing when another disk sits at the path now
                # (a card swapped in the same reader slot, SPEC §15.5).
                command["expected_serial"] = src.volume_serial
            commands.append(command)
            log.debug("Job %d: %s%s scan of %s", job.id, "full " if full else "", p.kind,
                      src.display_name)
        return commands

    def _add_job(self, job: _Job) -> _Job:
        job.id = self._next_job
        self._next_job += 1
        self._jobs[job.id] = job
        self._status_dirty = True
        return job

    def _priority(self, src: _Source, p: _Pending) -> tuple:
        if p.kind == "shallow":
            return (0, not p.first_time, p.requested)
        return (1, src.last_scan_end is not None, self._deep_cost(src), p.requested)

    def _due_announcements(self, now_m: float) -> list[tuple[str, dict]]:
        events: list[tuple[str, dict]] = []
        for ann in list(self._announcements):
            srcs = [self._sources[i] for i in ann.source_ids if i in self._sources]
            waiting = any(s.online and s.mode == "auto" and not s.manual and s.auto_include is None
                          for s in srcs)
            if waiting and now_m < ann.deadline:
                continue
            self._announcements.remove(ann)
            included = [s for s in srcs if s.included]
            if included:
                reasons = list(dict.fromkeys(s.auto_reason for s in included if s.auto_reason))
                reason = (reasons[0] if len(included) == 1 and reasons
                          else f"{len(included)} mapper medtaget" if len(included) > 1
                          else "Medtaget")
            else:
                reason = next((s.auto_reason for s in srcs if s.auto_reason),
                              discovery.REASON_NONE)
            events.append(("new_volume", {"disk_name": ann.disk_name, "drive": ann.drive,
                                          "source_ids": [s.id for s in srcs],
                                          "included": bool(included), "reason": reason}))
            text = NOTIFY_NEW_DISK.format(ann.disk_name) + (
                NOTIFY_INCLUDED if included else NOTIFY_EXCLUDED.format(reason))
            events.append(("notify", {"title": APP_NAME, "text": text, "level": "info"}))
            log.info("New disk %s (%s): %s", ann.disk_name, ann.drive, reason)
        return events

    def _due_events(self, now_m: float) -> list[tuple[str, dict]]:
        events: list[tuple[str, dict]] = []
        if self._changed and now_m - self._sources_sent >= SOURCES_INTERVAL_S:
            events.append(("sources", {"changed": sorted(self._changed)}))
            self._changed.clear()
            self._sources_sent = now_m
        for sid, _ in self._index_updated.pop_due(now_m):
            events.append(("index_updated", {"source_id": sid}))
        if self._progress_dirty and now_m - self._progress_at >= PROGRESS_INTERVAL_S:
            sid = min(self._progress_dirty, key=lambda s: self._progress_sent.get(s, _NEVER))
            self._progress_dirty.discard(sid)
            src = self._sources.get(sid)
            if src is not None and src.job is not None and src.job.kind != "forget":
                events.append(("scan_progress", self._scan_entry(src.job)))
                self._progress_sent[sid] = self._progress_at = now_m
        if self._status_dirty and now_m - self._status_sent >= STATUS_INTERVAL_S:
            events.append(("status", self.status()))
            self._status_dirty = False
            self._status_sent = now_m
        return events

    # -- worker events -------------------------------------------------------------------
    def _on_worker_event(self, generation: int, event: dict) -> None:
        self._inbox.append(("event", generation, event))
        self._wake.set()

    def _on_worker_exit(self, generation: int, code: int | None) -> None:
        self._inbox.append(("exit", generation, code))
        self._wake.set()

    def _drain_inbox(self) -> None:
        while self._inbox:
            kind, generation, payload = self._inbox.popleft()
            worker = self._worker
            if worker is None or worker.generation != generation:
                continue
            if kind == "exit":
                self._handle_worker_exit(payload)
            else:
                self._handle_worker_event(worker, payload)

    def _handle_worker_event(self, worker: _WorkerProcess, event: dict) -> None:
        kind = event.get("ev")
        with self._lock:
            if kind == "ready":
                worker.ready = True
                self._worker_failures = 0
                self._status_dirty = True
                log.info("Scan worker %s ready", event.get("pid"))
                return
            job = self._jobs.get(event.get("job"))
            if job is None:
                return
            src = self._sources.get(job.source_id)
            if src is not None and job.kind != "forget":
                src.activity = time.monotonic()
            if kind == "progress":
                job.progress = {k: event[k] for k in _PROGRESS_FIELDS
                                if isinstance(event.get(k), int)}
                if src is not None and src.job is job:
                    self._progress_dirty.add(src.id)
            elif kind == "committed":
                if src is not None and job.kind != "forget":
                    self._root_check.add(src.id)
                    self._index_updated.put(src.id, True)
            elif kind in ("done", "failed"):
                del self._jobs[job.id]
                self._status_dirty = True
                if job.kind == "forget":
                    self._finish_forget(job, event)
                elif src is not None and src.job is job:
                    self._finish_scan(src, job, event)

    def _finish_scan(self, src: _Source, job: _Job, event: dict) -> None:
        src.job = None
        self._progress_dirty.discard(src.id)
        self._root_check.add(src.id)
        now = self._clock()
        result = event.get("result") if event.get("ev") == "done" else None
        if not isinstance(result, dict):
            result = None
        error = str(event.get("error") or "Scanningen fejlede") if result is None \
            else result.get("error")
        if (result is not None and result.get("volume_changed")) or (
                error and ERR_DISK_SWAPPED in str(error)):
            self._disk_swapped(src, job)
            return
        src.swap_strikes = 0
        values: dict[str, Any] = {}
        counts = result.get("counts") if result is not None else None
        if isinstance(counts, dict):
            values.update({k: int(counts.get(k) or 0) for k in _COUNT_FIELDS})
        cancelled = result is not None and bool(result.get("aborted")) and not error
        if job.kind == "shallow":
            if result is not None and not cancelled:
                values["last_shallow_scan"] = now
            if error:
                log.info("Shallow scan of %s: %s", src.display_name, error)
        elif not cancelled:
            ok = bool(result is not None and result.get("ok"))
            values.update(last_scan_start=job.started, last_scan_end=now, last_scan_ok=int(ok),
                          last_error=error)
            if ok:
                values["scan_seconds"] = float(result.get("seconds") or 0.0)
                if not result.get("incremental"):
                    values["last_full_scan"] = now
                    src.needs_full = False
        self._set(src, **values)
        self._touch(src)
        changed = int(result.get("changed") or 0) if result is not None else 0
        if changed:
            self._index_updated.put(src.id, True)
        (log.info if changed or error else log.debug)(
            "Job %d (%s scan of %s) %s, %d changes in %.1f s", job.id, job.kind,
            src.display_name, "cancelled" if cancelled else (error or "ok"), changed,
            float(result.get("seconds") or 0.0) if result is not None else 0.0)

    def _disk_swapped(self, src: _Source, job: _Job) -> None:
        """The worker found another disk at ``src``'s path and wrote nothing (SPEC §15.5).

        Not a scan error: local discovery runs now and settles which disk is where; the scan
        is asked again but waits (``SWAP_HOLD_S``, doubling while it keeps happening), so it
        only runs if the source is still online and included after that pass.
        """
        src.swap_strikes += 1
        hold = min(SWAP_HOLD_MAX_S, SWAP_HOLD_S * 2 ** min(src.swap_strikes - 1, 16))
        src.hold_until = time.monotonic() + hold
        self._request(src, job.kind, full=job.full, first_time=job.first_time,
                      max_listings=job.max_listings or FIRST_SHALLOW_LISTINGS, window=job.window)
        self._local_wake.set()
        log.info("%s: another disk is mounted at %s - rediscovering (scan again in %.0f s)",
                 src.display_name, src.current_path, hold)

    def _finish_forget(self, job: _Job, event: dict) -> None:
        result = event.get("result")
        sid = job.source_id
        if isinstance(result, dict) and result.get("ok"):
            self._forget_ids.discard(sid)
            self._meta_dirty.add("pending_forget")
            self._db_ops.append(("delete", sid, ""))
            self._db_wake.set()
            log.info("Entries of forgotten location #%d deleted", sid)
        elif sid in self._forget_ids:
            self._forget_due[sid] = time.monotonic() + 30.0
            log.info("Forgetting location #%d did not finish - retrying later", sid)

    def _handle_worker_exit(self, code: int | None) -> None:
        now = self._clock()
        with self._lock:
            self._worker = None
            lost, self._jobs = list(self._jobs.values()), {}
            for job in lost:
                if job.kind == "forget":
                    if job.source_id in self._forget_ids:
                        self._forget_due[job.source_id] = 0.0
                    continue
                src = self._sources.get(job.source_id)
                if src is None or src.job is not job:
                    continue
                src.job = None
                self._touch(src)
                if job.cancel_sent:
                    continue
                if not job.retried:
                    self._request(src, job.kind, full=job.full, first_time=job.first_time,
                                  max_listings=job.max_listings or FIRST_SHALLOW_LISTINGS,
                                  retried=True)
                elif job.kind == "deep":
                    self._set(src, last_scan_start=job.started, last_scan_end=now,
                              last_scan_ok=0, last_error=ERR_WORKER_LOST)
            self._status_dirty = True
        delay = WORKER_BACKOFF_S[min(self._worker_failures, len(WORKER_BACKOFF_S) - 1)]
        self._worker_failures += 1
        self._worker_next_start = time.monotonic() + delay
        log.warning("Scan worker exited (code %s); %d job(s) interrupted - restart in %.0f s",
                    code, len(lost), delay)

    def _supervise_worker(self) -> None:
        if not self._start_worker or self._stopping.is_set() or self._worker is not None:
            return
        now_m = time.monotonic()
        if now_m < self._worker_next_start:
            return
        times = self._restart_times
        while times and now_m - times[0] > WORKER_RESTART_WINDOW_S:
            times.popleft()
        if len(times) >= WORKER_MAX_RESTARTS:
            self._worker_next_start = times[0] + WORKER_RESTART_WINDOW_S
            if not self._budget_logged:
                self._budget_logged = True
                log.error("Scan worker keeps exiting - not restarted before %.0f s",
                          self._worker_next_start - now_m)
            return
        self._budget_logged = False
        times.append(now_m)
        self._worker_restarts += 1
        self._spawn_worker()

    def _spawn_worker(self) -> None:
        self._worker_generation += 1
        try:
            worker = _WorkerProcess(self._worker_argv, self._worker_generation,
                                    self._on_worker_event, self._on_worker_exit)
        except OSError as exc:
            delay = WORKER_BACKOFF_S[min(self._worker_failures, len(WORKER_BACKOFF_S) - 1)]
            self._worker_failures += 1
            self._worker_next_start = time.monotonic() + delay
            log.error("Cannot start the scan worker (%s) - retrying in %.0f s", exc, delay)
            return
        with self._lock:
            stopping = self._stopping.is_set()
            if not stopping:
                self._worker = worker
                self._status_dirty = True
        if stopping:                        # stop() ran while the process was being created
            worker.close(1.0)
            return
        worker.start_reader()
        worker.send({"cmd": "config", "cfg": self.cfg.snapshot()})
        log.info("Scan worker started (pid %s)", worker.pid)

    # -- config ----------------------------------------------------------------------------
    def _on_config(self, snapshot: dict[str, Any]) -> None:
        """Config listener: record and wake (runs on the thread that called cfg.update)."""
        if self._stopping.is_set():
            return
        with self._cfg_lock:
            self._cfg_pending = snapshot
        self._wake.set()

    def _apply_config_change(self) -> None:
        with self._cfg_lock:
            snap, self._cfg_pending = self._cfg_pending, None
        if snap is None:
            return
        old, self._cfg_last = self._cfg_last, snap
        changed = {k for k in set(old) | set(snap) if old.get(k) != snap.get(k)}
        if not changed:
            return
        worker = self._worker
        if worker is not None:
            worker.send({"cmd": "config", "cfg": snap})
        if changed & _HOST_KEYS:
            self._reconcile_hosts(snap)
        with self._lock:
            if changed & _SCAN_RULE_KEYS:
                log.info("Scan settings changed (%s): full rescans queued",
                         ", ".join(sorted(changed & _SCAN_RULE_KEYS)))
                self._root_check.update(self._sources)      # the project rules may differ
                for src in self._sources.values():
                    src.needs_full = True
                    if src.online and src.included:
                        self._request_deep(src, full=True)
            if changed & _PROBE_KEYS:
                for src in self._sources.values():
                    if src.mode == "auto" and not src.manual and src.auto_include == 0:
                        src.reprobe = True
            self._status_dirty = True
        if changed & (_LOCAL_KEYS | _PROBE_KEYS | _HOST_KEYS):
            self._kick_discovery()

    # ==================================================================================
    # Discovery: local volumes
    # ==================================================================================

    def _local_loop(self) -> None:
        while not self._stopping.is_set():
            self._local_wake.clear()
            started = time.monotonic()
            try:
                self._local_pass()
            except Exception:
                log.exception("Local discovery failed")
            interval = self._cfg_int("discovery_interval_local_s", 1)
            self._local_wake.wait(max(0.0, interval - (time.monotonic() - started)))

    def _local_pass(self) -> None:
        env = self._env
        listed = [dict(v) for v in env.list_volumes() if v.get("serial")]
        # A volume that answered too slowly is reported with its last known facts ("stale"):
        # its medium may have been swapped since, so nothing is attributed to its serial.
        volumes = [v for v in listed if not v.get("stale")]
        stale = {str(v["serial"]).upper() for v in listed if v.get("stale")}
        shares = env.local_shares()
        mapped = dict(env.mapped_drives())
        self._update_path_inputs(shares=[dict(s) for s in shares], mapped=mapped)
        cfg = self.cfg.snapshot()
        with self._lock:
            layouts = self._volume_layouts()
            # Auto-discovered sources stay candidates even where a new one would not be (a
            # hidden top-level folder registered before SPEC §15.8, R2-IDX-1).
            known = [s.key for s in self._sources.values() if s.kind == "local" and not s.manual]
            initial = self._local_passes == 0
        auto = discovery.local_candidates(cfg, volumes, shares, self._own, layouts=layouts,
                                          known_keys=known)
        manual: list[dict] = []
        unsure: list[dict] = []
        for cand in discovery.extra_root_candidates(cfg, volumes, self._own,
                                                    pathmap=self._pathmap):
            if cand["kind"] != "local":
                continue
            exists = _dir_exists(cand["path"])
            if exists is None:
                unsure.append(cand)
            elif exists:
                manual.append(cand)
        cands, auto_keys, unknown = _merge_local(auto, manual, unsure)
        now = self._clock()
        with self._lock:
            if self._stopping.is_set():
                return
            unknown |= {s.key.casefold() for s in self._sources.values()
                        if s.kind == "local" and (s.volume_serial or "").upper() in stale}
            self._volumes = volumes
            self._local_share_count = len(shares)
            self._last_local_pass = now
            self._note_system_volumes(volumes)
            self._apply_candidates(cands, auto_keys, unknown, lambda s: s.kind == "local", now,
                                   initial=initial)
            self._forget_passing_cards(unknown, now)
            self._note_present_volumes({str(v["serial"]).upper() for v in volumes},
                                       initial=initial)
            self._note_volumes(volumes, cands)
            mapped_changed = mapped != self._mapped
            self._mapped = mapped
            self._local_passes += 1
            for src in self._sources.values():
                if src.kind == "local" and self._probe_needed(src, now):
                    self._queue_probe(src)
        if mapped_changed:
            self._reconcile_hosts(initial=initial)
        if time.monotonic() - self._own_ips_at >= OWN_IPS_REFRESH_S:
            self._own_ips_at = time.monotonic()
            self._update_path_inputs(own_ips=list(env.resolve_host_ips(self._own)))

    def _forget_passing_cards(self, unknown: set[str], now: float) -> None:
        """Memory cards pass through (SPEC §15.13); lock held.

        A source on a small hot-plug volume (≤ PASSING_CARD_MAX_BYTES: an SD card, a recorder's
        card, a USB stick) that holds no project, was never chosen by the user and has been
        offline for PASSING_CARD_GRACE_S (the card is out – or formatted: a new serial) is
        forgotten. Every card, and every format of one, would otherwise leave one more offline
        entry behind. Projects, templates, "Medtag altid"/"Medtag aldrig" and big disks stay."""
        for src in list(self._sources.values()):
            if (src.kind != "local" or src.online or src.key.casefold() in unknown
                    or not _passing_card(src)):
                continue
            offline_at = src.offline_since if src.offline_since is not None else src.last_seen
            if offline_at is not None and now - offline_at < PASSING_CARD_GRACE_S:
                continue
            log.info("%s was on a memory card that is gone – forgotten", src.display_name)
            self._remove_source(src)

    def _volume_layouts(self) -> dict[str, str]:
        """The kept layout of every volume that has sources (SPEC §15.6); lock held.

        A volume whose sources are folders stays split into folders when a project or a media
        file appears in its root, and a whole-volume source stays whole: nothing is replaced
        (and forgotten) behind the user's back.  Folders win when both exist.  Folders added by
        hand (extra_roots) are merged in separately and do not steer auto discovery: one folder
        added to a whole-volume disk the user excluded must not split that disk into new,
        auto-included folder sources (R2-IDX-4).  They only count on a volume that has no
        auto-discovered source.
        """
        auto: dict[str, str] = {}
        manual: dict[str, str] = {}
        for src in self._sources.values():
            mine = _volume_rel(src.key)
            if src.kind != "local" or mine is None:
                continue
            serial, rel = mine
            layouts = manual if src.manual else auto
            if rel:
                layouts[serial] = discovery.LAYOUT_FOLDERS
            else:
                layouts.setdefault(serial, discovery.LAYOUT_WHOLE)
        return {**manual, **auto}

    def _note_present_volumes(self, serials: set[str], *, initial: bool) -> None:
        """The serials of the mounted volumes that answered (``volume_present``, SPEC §15.12;
        stale ones are unverified and do not count); lock held."""
        changed = serials ^ self._present_serials
        if not changed:
            return
        self._present_serials = serials
        for src in self._sources.values():
            if (src.kind == "local" and not src.online
                    and (src.volume_serial or "").upper() in changed):
                self._presence_changed(src, initial=initial)

    def _note_system_volumes(self, volumes: list[dict]) -> None:
        """Remember the system volume's serial (persisted: ``is_system`` of offline and
        not yet rediscovered sources after a restart); lock held."""
        system = {str(v["serial"]).upper() for v in volumes if v.get("is_system")}
        if not system or system == self._system_serials:
            return
        changed = system ^ self._system_serials
        self._system_serials = system
        self._meta_dirty.add("system_volumes")
        self._db_wake.set()
        for src in self._sources.values():
            if src.kind == "local" and (src.volume_serial or "").upper() in changed:
                self._touch(src)

    def _note_volumes(self, volumes: list[dict], cands: list[dict]) -> None:
        """First sighting of a volume serial → announcement once its candidates are probed."""
        skip = {textutil.fold(s) for s in self.cfg.get("skip_volume_labels") or []}
        present = {str(v["serial"]).upper(): v for v in volumes
                   if textutil.fold(str(v.get("label") or "")) not in skip}
        if self._known_volumes is None:           # first run ever: today's disks are known
            self._known_volumes = set(present)
            self._meta_dirty.add("known_volumes")
            self._db_wake.set()
            return
        for serial, vol in present.items():
            if serial in self._known_volumes:
                continue
            self._known_volumes.add(serial)
            self._meta_dirty.add("known_volumes")
            self._db_wake.set()
            ids = [self._by_key[c["key"].casefold()] for c in cands
                   if str(c.get("volume_serial") or "").upper() == serial
                   and c["key"].casefold() in self._by_key]
            label = str(vol.get("label") or "").strip()
            size = vol.get("size")
            disk = label or ("disk uden navn" + (f" ({search.format_size(int(size))})"
                                                 if size else ""))
            self._announcements.append(_Announcement(
                str(vol.get("drive") or ""), disk, ids,
                time.monotonic() + NEW_VOLUME_MAX_WAIT_S))

    # ==================================================================================
    # Discovery: probes
    # ==================================================================================

    def _probe_needed(self, src: _Source, now: float) -> bool:
        if src.manual or src.mode != "auto" or not src.online or src.probe_queued:
            return False
        if src.auto_include is None:
            return True
        if src.auto_include == 1:                   # included never flips back by itself
            return False
        return (src.reprobe or src.probed_at is None
                or now - src.probed_at >= REPROBE_INTERVAL_S)

    def _queue_probe(self, src: _Source) -> None:
        """Local sources: the probe thread; share sources: their host thread (lock held)."""
        if src.kind == "local":
            src.probe_queued = True
            self._probe_queue.append(src.id)
            self._probe_wake.set()
        else:
            hs = self._hosts.get(src.host.upper())
            if hs is not None:
                hs.wake.set()

    def _probe_loop(self) -> None:
        while not self._stopping.is_set():
            self._probe_wake.clear()
            while not self._stopping.is_set():
                with self._lock:
                    sid = self._probe_queue.popleft() if self._probe_queue else None
                if sid is None:
                    break
                self._run_probe(sid)
            self._probe_wake.wait(SCHEDULER_TICK_S * 4)

    def _run_probe(self, sid: int) -> None:
        now = self._clock()
        with self._lock:
            src = self._sources.get(sid)
            if src is None:
                return
            src.probe_queued = False
            if not self._probe_needed(src, now):
                return
            src.probe_queued = True
            path, hotplug = src.current_path, bool(src.hotplug)
        try:
            include, reason, _count = self._env.probe(path, self.cfg.snapshot(), hotplug=hotplug,
                                                      max_listings=PROBE_MAX_LISTINGS)
        except Exception:
            log.exception("Probe of %s failed", path)
            include, reason = False, discovery.REASON_NO_ACCESS
        now = self._clock()
        with self._lock:
            src = self._sources.get(sid)
            if src is None:
                return
            src.probe_queued = False
            src.reprobe = False
            was_included = src.included
            values: dict[str, Any] = {"probed_at": now}
            if src.auto_include != 1:
                values.update(auto_include=int(bool(include)), auto_reason=reason)
            self._set(src, **values)
            log.info("Probe %s: %s (%s)", path, "included" if include else "excluded", reason)
            if src.included and not was_included:
                self._queue_start(src, now, None)

    # ==================================================================================
    # Discovery: remote hosts
    # ==================================================================================

    def _root_hosts(self, cfg: Mapping[str, Any]) -> dict[str, str]:
        """Remote computers holding an ``extra_roots`` folder → the first such root (as
        configured; mapped-drive and IP spellings are resolved)."""
        hosts: dict[str, str] = {}
        for raw in cfg.get("extra_roots") or []:
            unc = split_unc(self._pathmap.normalize(str(raw)))
            if unc is not None and unc[1] and unc[0] != self._own:
                hosts.setdefault(unc[0], str(raw))
        return hosts

    def _desired_hosts(self, cfg: Mapping[str, Any]) -> dict[str, bool]:
        """Hosts to poll → whether all their shares are candidates (SPEC §4.3.3)."""
        desired: dict[str, bool] = {}
        for raw in cfg.get("hosts") or []:
            name = _host_name(str(raw))
            if name and name != self._own:
                desired[name] = True
        for name in self._root_hosts(cfg):
            desired[name] = True
        for target in self._mapped.values():
            unc = split_unc(self._pathmap.normalize(target))
            if unc is not None and unc[1] and unc[0] != self._own:
                desired.setdefault(unc[0], False)
        return desired

    def _reconcile_hosts(self, cfg: Mapping[str, Any] | None = None, *,
                         initial: bool = False) -> None:
        """Start/stop the host threads; ``initial``: hosts polled since startup."""
        desired = self._desired_hosts(cfg if cfg is not None else self.cfg.snapshot())
        now = self._clock()
        with self._lock:
            if self._stopping.is_set() or not self._started:
                return
            for name in [n for n in self._hosts if n not in desired]:
                hs = self._hosts.pop(name)
                hs.stop.set()
                hs.wake.set()
                for src in list(self._sources.values()):
                    if src.kind == "share" and src.host.upper() == name:
                        if src.online:
                            self._go_offline(src, now)
                        elif hs.online:             # no longer known to answer
                            self._presence_changed(src, initial=False)
                log.info("Host %s is no longer polled", name)
            for name, enumerate_all in desired.items():
                hs = self._hosts.get(name)
                if hs is None:
                    hs = self._hosts[name] = _HostState(name, enumerate_all,
                                                        quiet_first=initial)
                    self._spawn(lambda hs=hs: self._host_loop(hs), f"indexer-host-{name}")
                elif hs.enumerate != enumerate_all:
                    hs.enumerate = enumerate_all
                    if not enumerate_all:
                        hs.shares = None        # hosts() counts its remaining sources
                    hs.wake.set()
            self._status_dirty = True

    def _host_loop(self, hs: _HostState) -> None:
        while not (hs.stop.is_set() or self._stopping.is_set()):
            hs.wake.clear()
            try:
                self._host_pass(hs)
            except Exception:
                log.exception("Discovery of %s failed", hs.name)
            hs.wake.wait(max(MIN_NETWORK_POLL_S,
                             float(self._cfg_int("discovery_interval_network_s", 1))))

    def _host_pass(self, hs: _HostState) -> None:
        env, host = self._env, hs.name
        cfg = self.cfg.snapshot()
        every_share = hs.enumerate
        shares: list[str] | None = None
        reachable: bool | None = None
        by_key: dict[str, dict] = {}
        auto_keys: set[str] = set()
        unknown: set[str] = set()
        if every_share:
            shares = env.remote_shares(host)
            if shares is None and not hs.stop.is_set():
                if self._host_recently_active(host):
                    log.debug("%s did not list its shares but is being scanned - keeping it",
                              host)
                    with self._lock:
                        hs.passes += 1
                    return
                hs.stop.wait(HOST_RETRY_DELAY_S)
                shares = None if hs.stop.is_set() else env.remote_shares(host)
            reachable = shares is not None
            for cand in discovery.remote_candidates(host, shares or []):
                by_key[cand["key"].casefold()] = self._with_share_path(cand)
                auto_keys.add(cand["key"].casefold())
        pm = self._pathmap
        extra = [c for c in discovery.extra_root_candidates(cfg, [], self._own, pathmap=pm)
                 if c["kind"] == "share" and c["host"].upper() == host]
        mapped = [c for c in discovery.mapped_candidates(self._mapped, pathmap=pm,
                                                         own_host=self._own)
                  if c["host"].upper() == host]
        for cand in mapped + extra:
            key = cand["key"].casefold()
            if key in by_key:
                by_key[key]["manual"] = by_key[key]["manual"] or cand["manual"]
                continue
            if reachable is False:
                continue
            cand = self._with_share_path(cand)
            exists = _dir_exists(cand["path"])
            if exists is None:
                unknown.add(key)
            elif exists:
                by_key[key] = cand
                if not cand["manual"]:
                    auto_keys.add(key)
        self._set_host_ips(host, list(env.resolve_host_ips(host)))
        with self._lock:
            known = {k: self._sources.get(self._by_key.get(k, -1)) for k in by_key}
        for key, cand in by_key.items():
            src = known[key]
            if src is not None and src.online and src.fs:
                cand["fs"] = src.fs
            else:
                info = env.volume_info(cand["path"])
                cand["fs"] = (info or {}).get("fs") or None
        now = self._clock()
        with self._lock:
            if hs.stop.is_set() or self._stopping.is_set():
                return
            if hs.enumerate != every_share:
                # Removed from (or added to) the hosts meanwhile: this pass saw the wrong set of
                # shares - it must neither bring back forgotten shares nor take others offline.
                # The switch woke the thread, so the next pass follows at once.
                return
            was_online = hs.online
            hs.online = bool(reachable) if every_share else bool(by_key)
            if hs.online:
                hs.last_seen = now
            if shares is not None:
                hs.shares = list(shares)
            initial = hs.quiet_first and hs.passes == 0
            self._apply_candidates(by_key.values(), auto_keys, unknown,
                                   lambda s: s.kind == "share" and s.host.upper() == host, now,
                                   initial=initial)
            if hs.online != was_online:         # offline shares: folder gone vs. host down
                for src in self._sources.values():
                    if src.kind == "share" and src.host.upper() == host and not src.online:
                        self._presence_changed(src, initial=initial)
            hs.passes += 1
            due = [s.id for s in self._sources.values()
                   if s.kind == "share" and s.host.upper() == host and self._probe_needed(s, now)]
        for sid in due:
            if hs.stop.is_set() or self._stopping.is_set():
                break
            self._run_probe(sid)

    def _host_recently_active(self, host: str) -> bool:
        limit = time.monotonic() - HOST_ACTIVITY_GRACE_S
        with self._lock:
            return any(s.kind == "share" and s.host.upper() == host and s.activity >= limit
                       for s in self._sources.values())

    def _with_share_path(self, cand: dict) -> dict:
        """Share candidates get their access path from ``env.share_path`` (tests)."""
        if cand["kind"] != "share":
            return cand
        unc = split_unc(cand["path"])
        if unc is None or not unc[1]:
            return cand
        base = self._env.share_path(unc[0], unc[1])
        path = _join(base, unc[2]) or base
        if path == cand["path"]:
            return cand
        return {**cand, "path": path, "unc_path": path}

    # -- path aliases --------------------------------------------------------------------
    def _update_path_inputs(self, **changes: Any) -> None:
        with self._path_lock:
            self._apply_path_inputs({**self._path_inputs, **changes})

    def _set_host_ips(self, host: str, ips: list[str]) -> None:
        with self._path_lock:
            host_ips = {**self._path_inputs["host_ips"], host: ips}
            self._apply_path_inputs({**self._path_inputs, "host_ips": host_ips})

    def _apply_path_inputs(self, inputs: dict[str, Any]) -> None:
        """Rebuild the PathMap when its inputs changed (path lock held)."""
        if inputs == self._path_inputs:
            return
        self._path_inputs = inputs
        self._pathmap.update(inputs["shares"], inputs["mapped"], inputs["host_ips"],
                             inputs["own_ips"])

    # ==================================================================================
    # Persistence thread (sources / meta)
    # ==================================================================================

    def _db_loop(self) -> None:
        conn: sqlite3.Connection | None = None
        try:
            while True:
                stopping = self._stopping.is_set()
                self._db_wake.clear()
                if conn is None:
                    try:
                        conn = db.connect(self.db_path, writer=True)
                    except sqlite3.Error:
                        log.exception("Cannot open the index for writing")
                        if stopping:
                            return
                        self._stopping.wait(DB_RETRY_S)
                        continue
                ok = self._db_flush(conn)
                if stopping:
                    return
                if not ok:
                    self._stopping.wait(DB_RETRY_S)
                    continue
                self._db_wake.wait(SCHEDULER_TICK_S * 2)
        finally:
            if conn is not None:
                conn.close()

    def _db_flush(self, conn: sqlite3.Connection) -> bool:
        with self._lock:
            ops, self._db_ops = self._db_ops, []
            dirty, self._dirty = self._dirty, {}
            meta_keys, self._meta_dirty = self._meta_dirty, set()
            inserts: list[tuple[int, list[Any]]] = []
            updates: list[tuple[int, dict[str, Any]]] = []
            for sid, names in dirty.items():
                src = self._sources.get(sid)
                if src is None:
                    continue
                if not src.persisted:
                    inserts.append((sid, [getattr(src, f) for f in _PERSISTED]))
                elif names:
                    updates.append((sid, {f: getattr(src, f) for f in names}))
            meta = {key: self._meta_value(key) for key in meta_keys}
        if not (ops or inserts or updates or meta):
            return True
        try:
            with db.transaction(conn):
                for op, sid, key in ops:
                    if op == "rename":
                        conn.execute("UPDATE sources SET key = ? WHERE id = ?",
                                     (f"forgotten:{sid}:{key}", sid))
                    else:
                        conn.execute("DELETE FROM sources WHERE id = ?", (sid,))
                for sid, values in inserts:
                    conn.execute(_INSERT_SOURCE_SQL, [sid, *values])
                for sid, values in updates:
                    names = list(values)
                    conn.execute(f"UPDATE sources SET {', '.join(f'{n} = ?' for n in names)} "
                                 "WHERE id = ?", [values[n] for n in names] + [sid])
                for key, value in meta.items():
                    conn.execute(_UPSERT_META_SQL, (key, value))
        except sqlite3.Error as exc:
            log.warning("Saving location changes failed (%s) - retrying", exc)
            with self._lock:
                self._db_ops[:0] = ops
                for sid, names in dirty.items():
                    self._dirty.setdefault(sid, set()).update(names)
                self._meta_dirty |= meta_keys
            return False
        with self._lock:
            for sid, _ in inserts:
                src = self._sources.get(sid)
                if src is not None:
                    src.persisted = True
                elif sid not in self._forget_ids:      # removed before its insert committed
                    self._db_ops.append(("delete", sid, ""))
                    self._db_wake.set()
        if inserts:
            self._wake.set()
        return True

    def _meta_value(self, key: str) -> str:
        if key == "known_volumes":
            return json.dumps(sorted(self._known_volumes or ()))
        if key == "pending_forget":
            return json.dumps(sorted(self._forget_ids))
        if key == "system_volumes":
            return json.dumps(sorted(self._system_serials))
        return str(self._next_id - 1)

