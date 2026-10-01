"""Filesystem scanning for the index (SPEC §5): deep unit-wise scans and shallow scans.

Runs in the scan worker process.  Every directory listing goes through a *lister*
(``path -> list[os.DirEntry]``, default :func:`list_dir`) which tests replace to simulate failing
or slow directories.  Each directory is read completely and its handle closed before its entries
are processed, so a scan never holds more than one directory handle.  Filesystem calls use the
``\\\\?\\`` / ``\\\\?\\UNC\\`` form; nothing is ever written to the scanned tree.

Database access goes through the caller's connection under the caller's ``lock`` (the worker
shares one writer connection between job threads); the filesystem walk runs without the lock.
A source must not be scanned by two jobs at once – each unit is diffed against the rows loaded
right before its walk (the worker serialises jobs per source).

An optional ``verify()`` callback (SPEC §15.5) is called before the root listing and before
every transaction; when it returns False (e.g. another disk now sits at the drive letter) the
scan stops at once without writing and reports :data:`DISK_CHANGED`.
"""

from __future__ import annotations

import fnmatch
import logging
import ntpath
import os
import re
import sqlite3
import stat
import threading
import time
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from typing import Any, NamedTuple

from . import config, db, textutil
from .db import (KIND_DIR, KIND_FILE, KIND_GROUP, KIND_PROJECT, KIND_TEMPLATE, KIND_TOPLEVEL,
                 Diff, Entry)

log = logging.getLogger(__name__)

Lister = Callable[[str], list[os.DirEntry]]
ProgressFn = Callable[[dict[str, int]], None]
CommitFn = Callable[[int], None]
VerifyFn = Callable[[], bool]            # True while the scanned volume is the expected one

DISK_CHANGED = "Disken er skiftet"

_FILE_ATTRIBUTE_HIDDEN = 0x2
_FILE_ATTRIBUTE_SYSTEM = 0x4
_FILE_ATTRIBUTE_DIRECTORY = 0x10
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_FILE_ATTRIBUTE_OFFLINE = 0x1000
_FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
_FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
_HIDDEN_SYSTEM = _FILE_ATTRIBUTE_HIDDEN | _FILE_ATTRIBUTE_SYSTEM
# Never descend into reparse points (junctions, symlinks, mount points, cloud placeholders) or
# into directories whose content would first be recalled from remote storage.
_NO_DESCEND = (_FILE_ATTRIBUTE_REPARSE_POINT | _FILE_ATTRIBUTE_OFFLINE
               | _FILE_ATTRIBUTE_RECALL_ON_OPEN | _FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS)

PROGRESS_INTERVAL_S = 0.25                                   # progress() at most 4x/s
_SEQ_RE = re.compile(r"(.*?)([0-9]+)\.([A-Za-z0-9]+)")       # used with fullmatch()
# A sequence is a dense run of frame numbers: consecutive numbers differ by at most this much
# (one dropped frame is tolerated).  Camera stills (DSC01616, DSC01702, …) stay single files.
SEQUENCE_MAX_GAP = 2

_WINERROR_DA = {
    2: "findes ikke",
    3: "findes ikke",
    5: "adgang nægtet",
    21: "drevet er ikke klar",
    53: "netværksstien blev ikke fundet",
    59: "uventet netværksfejl",
    64: "netværksforbindelsen blev afbrudt",
    67: "netværksnavnet blev ikke fundet",
    86: "forkert adgangskode",
    121: "svarer ikke (timeout)",
    1231: "netværket kan ikke nås",
    1326: "forkert brugernavn eller adgangskode",
}


# --------------------------------------------------------------------------------------
# Rules, listing, sequences
# --------------------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ScanRules:
    """The scan-relevant settings of a config snapshot, precompiled."""

    exclude_dirs: frozenset[str]
    exclude_files: frozenset[str]
    exclude_glob: re.Pattern[str] | None
    sequence_exts: frozenset[str]
    sequence_min: int
    template_re: re.Pattern[str]
    project_dirs: frozenset[str]
    project_min: int

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any]) -> ScanRules:
        def get(key: str) -> Any:
            value = cfg.get(key)
            return config.DEFAULTS[key] if value is None else value

        try:
            template_re = re.compile(get("template_folder_regex"))
        except (re.error, TypeError):
            log.warning("invalid template_folder_regex %r – using the default",
                        cfg.get("template_folder_regex"))
            template_re = re.compile(config.DEFAULTS["template_folder_regex"])
        globs = [fnmatch.translate(g.casefold()) for g in get("exclude_file_globs") if g]
        return cls(
            exclude_dirs=frozenset(n.casefold() for n in get("exclude_dir_names")),
            exclude_files=frozenset(n.casefold() for n in get("exclude_file_names")),
            exclude_glob=re.compile("|".join(globs)) if globs else None,
            sequence_exts=frozenset(e.casefold().lstrip(".") for e in get("sequence_exts")),
            sequence_min=max(2, int(get("sequence_min_files"))),   # 1 frame is no sequence
            template_re=template_re,
            project_dirs=frozenset(n.casefold() for n in get("project_template_dirs")),
            project_min=int(get("project_min_template_dirs")),
        )

    def skip_dir(self, name: str) -> bool:
        return name.casefold() in self.exclude_dirs

    def skip_file(self, name: str) -> bool:
        low = name.casefold()
        return low in self.exclude_files or (
            self.exclude_glob is not None and self.exclude_glob.match(low) is not None)

    def is_template_fold(self, name_fold: str) -> bool:
        return self.template_re.search(name_fold) is not None

    def is_project(self, child_dir_names: Iterable[str]) -> bool:
        """SPEC §4.4: enough direct sub-folders carry template names (case-insensitive)."""
        hits = sum(1 for n in child_dir_names if n.casefold() in self.project_dirs)
        return hits >= self.project_min

    def is_project_part(self, name: str) -> bool:
        """A folder named like a template sub-folder (``Klip``, ``Musik`` …) is part of a
        project and never a project itself, whatever it contains (SPEC §15.7)."""
        return name.casefold() in self.project_dirs

    def is_project_dir(self, name: str, name_fold: str, child_dir_names: Iterable[str]) -> bool:
        """The project rule for a listed directory (templates and project parts excluded)."""
        return (not self.is_template_fold(name_fold) and not self.is_project_part(name)
                and self.is_project(child_dir_names))


class DirInfo(NamedTuple):
    name: str
    mtime: float
    walkable: bool          # False: junction/symlink/placeholder – stored, never descended


class FileInfo(NamedTuple):
    name: str
    size: int
    mtime: float


class SequenceInfo(NamedTuple):
    name: str               # "<prefix>[<first>-<last>].<ext of the first frame>"
    ext: str                # lower case
    size: int
    mtime: float
    count: int


def list_dir(path: str) -> list[os.DirEntry]:
    """Read a whole directory and close its handle before returning."""
    with os.scandir(path) as it:
        return list(it)


def long_path(path: str) -> str:
    """``\\\\?\\`` form of an absolute local or UNC path (for filesystem calls)."""
    p = path.replace("/", "\\")
    if p.startswith(("\\\\?\\", "\\\\.\\")):
        return p
    if len(p) == 2 and p[1] == ":":
        p += "\\"
    if not ntpath.isabs(p) or (not p.startswith("\\\\") and p[1:2] != ":"):
        raise ValueError(f"not an absolute path: {path!r}")
    p = ntpath.normpath(p)
    if p.startswith("\\\\"):
        return "\\\\?\\UNC\\" + p[2:]
    return "\\\\?\\" + p


def describe_os_error(exc: OSError) -> str:
    """Short Danish reason for a failed filesystem call."""
    code = getattr(exc, "winerror", None)
    if code in _WINERROR_DA:
        return _WINERROR_DA[code]
    if isinstance(exc, FileNotFoundError):
        return "findes ikke"
    if isinstance(exc, PermissionError):
        return "adgang nægtet"
    if isinstance(exc, TimeoutError):
        return "svarer ikke (timeout)"
    number = code if code is not None else exc.errno
    return f"fejl {number}" if number is not None else "ukendt fejl"


def _utf8_ok(name: str) -> bool:
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:       # lone surrogates cannot be stored in SQLite
        return False
    return True


def split_listing(items: Iterable[os.DirEntry],
                  rules: ScanRules) -> tuple[list[DirInfo], list[FileInfo]]:
    """Apply excludes and the hidden+system rule; split into sub-directories and files."""
    subs: list[DirInfo] = []
    files: list[FileInfo] = []
    for item in items:
        name = item.name
        if not name.isascii() and not _utf8_ok(name):
            log.debug("skipping name with unpaired surrogates: %r", name)
            continue
        try:
            st = item.stat(follow_symlinks=False)   # cached from the listing on Windows
        except OSError as exc:
            log.debug("stat failed for %r: %s", name, exc)
            continue
        attrs = getattr(st, "st_file_attributes", 0)
        if attrs & _HIDDEN_SYSTEM == _HIDDEN_SYSTEM:
            continue
        is_dir = stat.S_ISDIR(st.st_mode)
        if is_dir or attrs & _FILE_ATTRIBUTE_DIRECTORY:
            if not rules.skip_dir(name):
                walkable = is_dir and not attrs & _NO_DESCEND and not item.is_junction()
                subs.append(DirInfo(name, st.st_mtime, walkable))
        elif not rules.skip_file(name):
            files.append(FileInfo(name, st.st_size, st.st_mtime))
    return subs, files


_Frame = tuple[int, str, str, FileInfo]          # (number, digits, ext as spelled, file)


def group_sequences(files: Sequence[FileInfo],
                    rules: ScanRules) -> tuple[list[FileInfo], list[SequenceInfo]]:
    """Collapse image sequences (SPEC §5.1, §15.7); returns (remaining files, sequences).

    Files are grouped by (prefix, digit width, lower-case extension); each group is split into
    dense runs (:data:`SEQUENCE_MAX_GAP`) and only runs of at least ``sequence_min_files``
    frames become one entry ``<prefix>[<first>-<last>].<ext>``.
    """
    if len(files) < rules.sequence_min or not rules.sequence_exts:
        return list(files), []
    groups: dict[tuple[str, int, str], list[_Frame]] = {}
    for f in files:
        m = _SEQ_RE.fullmatch(f.name)
        if m is None:
            continue
        prefix, digits, ext = m.groups()
        ext_low = ext.lower()
        if ext_low in rules.sequence_exts:
            groups.setdefault((prefix, len(digits), ext_low), []).append(
                (int(digits), digits, ext, f))
    sequences: list[SequenceInfo] = []
    collapsed: set[str] = set()
    taken: set[str] | None = None
    for (prefix, _width, ext_low), members in groups.items():
        if len(members) < rules.sequence_min:
            continue
        members.sort(key=lambda m: m[0])
        for run in _dense_runs(members, rules.sequence_min):
            first, last = run[0], run[-1]
            name = f"{prefix}[{first[1]}-{last[1]}].{first[2]}"
            if taken is None:
                taken = {f.name.casefold() for f in files}
            if name.casefold() in taken:    # a real file already has that name: keep frames
                continue
            sequences.append(SequenceInfo(name, ext_low, sum(m[3].size for m in run),
                                          max(m[3].mtime for m in run), len(run)))
            collapsed.update(m[3].name for m in run)
    if not collapsed:
        return list(files), sequences
    return [f for f in files if f.name not in collapsed], sequences


def _dense_runs(frames: list[_Frame], minimum: int) -> Iterator[list[_Frame]]:
    """Runs of ``frames`` (sorted by number) without a gap > :data:`SEQUENCE_MAX_GAP` that
    have at least ``minimum`` members."""
    run = [frames[0]]
    for frame in frames[1:]:
        if frame[0] - run[-1][0] > SEQUENCE_MAX_GAP:
            if len(run) >= minimum:
                yield run
            run = []
        run.append(frame)
    if len(run) >= minimum:
        yield run


_SEQ_NAME_RE = re.compile(r"(.*)\[([0-9]+)-([0-9]+)\]\.([A-Za-z0-9]+)")


def sequence_first_frame(name: str) -> str | None:
    """File name of the first frame of a collapsed sequence entry name."""
    m = _SEQ_NAME_RE.fullmatch(name)
    return None if m is None else f"{m.group(1)}{m.group(2)}.{m.group(4)}"


def file_ext(name: str) -> str | None:
    """Lower-case extension without dot, or None (no dot, dot-files, odd suffixes)."""
    i = name.rfind(".")
    if i <= 0 or i == len(name) - 1:
        return None
    ext = name[i + 1:]
    if len(ext) > 16 or " " in ext:
        return None
    return ext.lower()


def build_file_entries(parent_rel: str, depth: int, files: Sequence[FileInfo],
                       rules: ScanRules, project_rel: str | None) -> list[Entry]:
    """Entries for the files of one directory, image sequences collapsed."""
    prefix = parent_rel + "\\" if parent_rel else ""
    fold = textutil.fold
    singles, sequences = group_sequences(files, rules)
    out = [Entry(prefix + f.name, parent_rel, f.name, fold(f.name), KIND_FILE, depth,
                 file_ext(f.name), f.size, f.mtime, None, None, 0, None, project_rel)
           for f in singles]
    out.extend(Entry(prefix + s.name, parent_rel, s.name, fold(s.name), KIND_FILE, depth,
                     s.ext, s.size, s.mtime, None, None, 1, s.count, project_rel)
               for s in sequences)
    return out


def _plain_dir_entry(parent_rel: str, depth: int, sub: DirInfo,
                     project_rel: str | None) -> Entry:
    """A directory that is never descended (junction, symlink, cloud placeholder)."""
    rel = f"{parent_rel}\\{sub.name}" if parent_rel else sub.name
    return Entry(rel, parent_rel, sub.name, textutil.fold(sub.name), KIND_DIR, depth, None,
                 None, sub.mtime, None, None, 0, None, project_rel)


# --------------------------------------------------------------------------------------
# Diff
# --------------------------------------------------------------------------------------

def compute_diff(stored: Iterable[Sequence[Any]], new: Iterable[Entry], *,
                 descendants_loaded: bool, keep_under: Collection[str] = (),
                 into: Diff | None = None) -> Diff:
    """Diff stored rows ``(id, *Entry)`` of one scope against its freshly built entries.

    ``descendants_loaded``: ``stored`` contains whole subtrees (deep unit) so vanished rows are
    deleted one by one; otherwise (root unit, shallow children) a vanished directory – or one
    that turned into a file – also loses its descendants via ``delete_descendants``.
    ``keep_under``: directories that could not be listed; stored rows below them are neither
    updated nor deleted.  A changed ``name_fold`` becomes delete + insert (keeps FTS in sync).
    """
    diff = into if into is not None else Diff()
    by_rel = {row[db.S_REL]: row for row in stored}
    for e in new:
        row = by_rel.pop(e.rel_path, None)
        if row is None:
            diff.inserts.append(e)
            continue
        if row[1:] == e:
            continue
        if row[db.S_FOLD] != e.name_fold:
            diff.deletes.append(row[db.S_ID])
            diff.inserts.append(e)
        else:
            diff.updates.append((row[db.S_ID], e))
        if not descendants_loaded and row[db.S_KIND] != KIND_FILE and e.kind == KIND_FILE:
            diff.delete_descendants.append(e.rel_path)
    keep = tuple(rel + "\\" for rel in keep_under)
    for rel, row in by_rel.items():
        if keep and rel.startswith(keep):
            continue
        diff.deletes.append(row[db.S_ID])
        if not descendants_loaded and row[db.S_KIND] != KIND_FILE:
            diff.delete_descendants.append(rel)
    return diff


# --------------------------------------------------------------------------------------
# Scan state
# --------------------------------------------------------------------------------------

_LISTED, _REUSED, _FAILED = 0, 1, 2


class _Node:
    """A directory of the unit being walked; aggregated bottom-up after the walk."""

    __slots__ = ("rel", "name", "fold", "depth", "parent", "mtime", "inherited_prj",
                 "project_rel", "is_project", "state", "stored", "size", "file_count", "newest",
                 "has_project_child")

    def __init__(self, rel: str, name: str, depth: int, parent: _Node | None, mtime: float,
                 inherited_prj: str | None) -> None:
        self.rel = rel
        self.name = name
        self.fold = textutil.fold(name)
        self.depth = depth
        self.parent = parent
        self.mtime = mtime                    # own mtime from the parent listing
        self.inherited_prj = inherited_prj
        self.project_rel = inherited_prj
        self.is_project = False
        self.state = _LISTED
        self.stored: tuple | None = None
        self.size = 0
        self.file_count = 0
        self.newest = mtime
        self.has_project_child = False

    def add_file(self, e: Entry) -> None:
        self.size += e.size or 0
        self.file_count += e.seq_count if e.is_seq else 1
        if e.mtime is not None and e.mtime > self.newest:
            self.newest = e.mtime

    def classify(self, rules: ScanRules, child_dir_names: Iterable[str]) -> None:
        self.is_project = rules.is_project_dir(self.name, self.fold, child_dir_names)
        if self.is_project:
            self.project_rel = self.rel

    def finish(self, rules: ScanRules) -> Entry:
        """The directory's own entry; adds its aggregates to the parent."""
        if self.state == _FAILED:
            row = self.stored
            if row is not None and row[db.S_KIND] != KIND_FILE:
                # Keep what we knew, including the old dir_mtime, so it is listed next time.
                kind, size, mtime = row[db.S_KIND], row[db.S_SIZE], row[db.S_MTIME]
                file_count, dir_mtime = row[db.S_FILE_COUNT], row[db.S_DIR_MTIME]
            else:
                kind = _name_kind(rules, self.fold, self.depth)
                size = mtime = file_count = dir_mtime = None
            project_rel = self.rel if kind == KIND_PROJECT else self.inherited_prj
        else:
            if rules.is_template_fold(self.fold):
                kind = KIND_TEMPLATE
            elif self.is_project:
                kind = KIND_PROJECT
            elif self.has_project_child:
                kind = KIND_GROUP
            else:
                kind = KIND_TOPLEVEL if self.depth == 1 else KIND_DIR
            size, mtime, file_count = self.size, self.newest, self.file_count
            dir_mtime, project_rel = self.mtime, self.project_rel
        parent = self.parent
        if parent is not None:
            parent.size += size or 0
            parent.file_count += file_count or 0
            if mtime is not None and mtime > parent.newest:
                parent.newest = mtime
            if kind == KIND_PROJECT:
                parent.has_project_child = True
        return Entry(self.rel, parent.rel if parent is not None else "", self.name, self.fold,
                     kind, self.depth, None, size, mtime, file_count, dir_mtime, 0, None,
                     project_rel)


def _name_kind(rules: ScanRules, name_fold: str, depth: int) -> int:
    """Kind of a directory whose children are unknown."""
    if rules.is_template_fold(name_fold):
        return KIND_TEMPLATE
    return KIND_TOPLEVEL if depth == 1 else KIND_DIR


@dataclass(slots=True)
class _UnitWalk:
    entries: list[Entry] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    attempts: int = 0
    failures: int = 0


class _VolumeChanged(Exception):
    """The volume at the root is no longer the one the scan was started for."""


class _Scan:
    """State shared by the phases of one deep or shallow scan."""

    def __init__(self, conn: sqlite3.Connection, source_id: int, root_path: str,
                 cfg: Mapping[str, Any], *,
                 cancel: threading.Event, progress: ProgressFn | None,
                 on_commit: CommitFn | None, lister: Lister | None,
                 lock: AbstractContextManager[Any] | None,
                 verify: VerifyFn | None = None) -> None:
        self.conn = conn
        self.source_id = source_id
        self.root_path = root_path
        self.rules = ScanRules.from_config(cfg)
        self.cancel = cancel
        self.lock = lock if lock is not None else nullcontext()
        self._lister = lister or list_dir
        self._progress = progress
        self._on_commit = on_commit
        self._verify = verify
        self.fs_root = long_path(root_path)
        self._fs_prefix = self.fs_root if self.fs_root.endswith("\\") else self.fs_root + "\\"
        self.started = time.monotonic()
        self._last_report = 0.0
        self.entries = self.dirs = self.files = 0
        self.units_done = self.units_total = 0
        self.changed = self.deleted = self.failed_dirs = self.reused_dirs = 0

    def list(self, rel: str) -> tuple[list[DirInfo], list[FileInfo]]:
        path = self._fs_prefix + rel if rel else self.fs_root
        return split_listing(self._lister(path), self.rules)

    def count(self, e: Entry) -> None:
        self.entries += 1
        if e.kind == KIND_FILE:
            self.files += e.seq_count if e.is_seq else 1
        else:
            self.dirs += 1

    def check_volume(self) -> None:
        """Raise :class:`_VolumeChanged` unless the root is still on the expected volume."""
        if self._verify is not None and not self._verify():
            log.info("%s is no longer on the expected volume: scan stopped", self.root_path)
            raise _VolumeChanged

    def apply(self, diff: Diff) -> None:
        if not diff:
            return
        self.check_volume()                 # outside the lock: it may touch the device
        with self.lock:
            changed, deleted = db.apply_diff(self.conn, self.source_id, diff)
        self.changed += changed
        self.deleted += deleted
        if changed and self._on_commit is not None:
            self._on_commit(changed)

    def report(self, force: bool = False) -> None:
        if self._progress is None:
            return
        now = time.monotonic()
        if force or now - self._last_report >= PROGRESS_INTERVAL_S:
            self._last_report = now
            self._progress({"entries": self.entries, "dirs": self.dirs,
                            "units_done": self.units_done, "units_total": self.units_total})

    def result(self, *, ok: bool, aborted: bool, error: str | None,
               **extra: Any) -> dict[str, Any]:
        with self.lock:
            counts = db.source_counts(self.conn, self.source_id)
        out: dict[str, Any] = {
            "ok": ok, "aborted": aborted, "error": error, "changed": self.changed,
            "deleted": self.deleted, "units_done": self.units_done,
            "units_total": self.units_total, "entries": self.entries, "dirs": self.dirs,
            "files": self.files, "failed_dirs": self.failed_dirs,
            "reused_dirs": self.reused_dirs,
            "seconds": round(time.monotonic() - self.started, 3), "counts": counts,
        }
        out.update(extra)
        return out

    def root_error(self, exc: OSError) -> str:
        log.warning("cannot list root %s: %s", self.root_path, exc)
        return f"Kunne ikke læse {self.root_path}: {describe_os_error(exc)}"

    # -- deep: one unit -----------------------------------------------------------------
    def walk_unit(self, unit: DirInfo, stored_rows: list[tuple],
                  incremental: bool) -> _UnitWalk | None:
        """Walk one depth-1 subtree (explicit stack).  None if cancelled."""
        rules = self.rules
        stored = {row[db.S_REL]: row for row in stored_rows}
        children_of: dict[str, list[tuple]] = {}
        has_subdirs: set[str] = set()
        if incremental:
            for row in stored_rows:
                children_of.setdefault(row[db.S_PARENT], []).append(row)
                if row[db.S_KIND] != KIND_FILE:
                    has_subdirs.add(row[db.S_PARENT])
        walk = _UnitWalk()
        order: list[_Node] = []
        stack = [_Node(unit.name, unit.name, 1, None, unit.mtime, None)]
        while stack:
            if self.cancel.is_set():
                return None
            node = stack.pop()
            order.append(node)
            self.entries += 1
            self.dirs += 1
            row = stored.get(node.rel)
            if (incremental and row is not None and row[db.S_KIND] != KIND_FILE
                    and node.rel not in has_subdirs and row[db.S_DIR_MTIME] is not None
                    and row[db.S_DIR_MTIME] == node.mtime):
                # Unchanged leaf: reuse its stored files instead of listing it again.
                node.state = _REUSED
                self.reused_dirs += 1
                node.classify(rules, ())
                for child in children_of.get(node.rel, ()):
                    if not rules.skip_file(child[db.S_NAME]):   # excludes may have changed
                        reused = Entry._make(child[1:-1] + (node.project_rel,))
                        self._add_file(walk, node, reused)
                continue
            walk.attempts += 1
            try:
                subs, files = self.list(node.rel)
            except OSError as exc:
                walk.failures += 1
                walk.failed.append(node.rel)
                node.state = _FAILED
                node.stored = row
                log.info("cannot list %s\\%s: %s", self.root_path, node.rel, exc)
                continue
            node.classify(rules, (s.name for s in subs))
            for e in build_file_entries(node.rel, node.depth + 1, files, rules, node.project_rel):
                self._add_file(walk, node, e)
            for sub in subs:
                if sub.walkable:
                    stack.append(_Node(f"{node.rel}\\{sub.name}", sub.name, node.depth + 1, node,
                                       sub.mtime, node.project_rel))
                else:
                    e = _plain_dir_entry(node.rel, node.depth + 1, sub, node.project_rel)
                    walk.entries.append(e)
                    self.count(e)
                    if sub.mtime > node.newest:
                        node.newest = sub.mtime
            self.report()
        for node in reversed(order):           # children before parents
            walk.entries.append(node.finish(rules))
        return walk

    def _add_file(self, walk: _UnitWalk, node: _Node, e: Entry) -> None:
        walk.entries.append(e)
        node.add_file(e)
        self.count(e)


# --------------------------------------------------------------------------------------
# Deep scan
# --------------------------------------------------------------------------------------

def deep_scan(conn: sqlite3.Connection, source_id: int, root_path: str,
              cfg: Mapping[str, Any], *,
              full: bool, is_network: bool, fs: str | None, cancel: threading.Event,
              progress: ProgressFn | None, on_commit: CommitFn | None = None,
              lister: Lister | None = None,
              lock: AbstractContextManager[Any] | None = None,
              verify: VerifyFn | None = None) -> dict[str, Any]:
    """Scan ``root_path`` unit by unit (SPEC §5.1); every unit is its own transaction.

    Returns the SPEC shape plus ``deleted``, ``failed_dirs``, ``reused_dirs`` and
    ``incremental`` (True when the leaf-skip rule was active, i.e. not a full walk).
    ``ok`` is False when the scan was cancelled, the root could not be listed or a unit was
    not applied (> 50 % of its directories failed); ``error`` is a Danish summary.
    ``on_commit(changed)`` is called after each committed transaction with changes.
    ``verify()`` runs before the root listing and before every transaction; if it fails the
    scan stops with ``aborted``, ``error`` = :data:`DISK_CHANGED` and ``volume_changed``.
    """
    scan = _Scan(conn, source_id, root_path, cfg, cancel=cancel, progress=progress,
                 on_commit=on_commit, lister=lister, lock=lock, verify=verify)
    incremental = not full and is_network and (fs or "").upper() == "NTFS"
    try:
        return _deep(scan, incremental)
    except _VolumeChanged:
        return scan.result(ok=False, aborted=True, error=DISK_CHANGED, incremental=incremental,
                           volume_changed=True)


def _deep(scan: _Scan, incremental: bool) -> dict[str, Any]:
    conn, source_id, root_path, cancel = scan.conn, scan.source_id, scan.root_path, scan.cancel
    scan.check_volume()
    try:
        subs, files = scan.list("")
    except OSError as exc:
        return scan.result(ok=False, aborted=True, error=scan.root_error(exc),
                           incremental=incremental)
    if cancel.is_set():
        return scan.result(ok=False, aborted=True, error=None, incremental=incremental)
    units = [s for s in subs if s.walkable]
    unit_names = {s.name for s in units}
    scan.units_total = 1 + len(units)

    # Unit 0: the root's own files and non-walkable entries.  Its diff also removes depth-1
    # entries (with subtrees) that vanished from this successful root listing.
    root_entries = build_file_entries("", 1, files, scan.rules, None)
    root_entries += [_plain_dir_entry("", 1, s, None) for s in subs if not s.walkable]
    for e in root_entries:
        scan.count(e)
    with scan.lock:
        stored = [row for row in db.load_children(conn, source_id, "")
                  if row[db.S_REL] not in unit_names]
    scan.apply(compute_diff(stored, root_entries, descendants_loaded=False))
    scan.units_done = 1
    scan.report(force=True)

    units_failed = 0
    for unit in units:
        if cancel.is_set():
            return scan.result(ok=False, aborted=True, error=None, incremental=incremental)
        with scan.lock:
            stored_rows = db.load_subtree(conn, source_id, unit.name)
        walk = scan.walk_unit(unit, stored_rows, incremental and bool(stored_rows))
        if walk is None:
            return scan.result(ok=False, aborted=True, error=None, incremental=incremental)
        scan.failed_dirs += walk.failures
        if walk.failures * 2 > walk.attempts:
            units_failed += 1
            log.warning("%s\\%s not updated: %d of %d directories could not be listed",
                        root_path, unit.name, walk.failures, walk.attempts)
        else:
            scan.apply(compute_diff(stored_rows, walk.entries, keep_under=walk.failed,
                                    descendants_loaded=True))
        scan.units_done += 1
        scan.report()
    scan.report(force=True)
    return scan.result(ok=units_failed == 0, aborted=False,
                       error=_failure_text(scan.failed_dirs, units_failed),
                       incremental=incremental)


def _failure_text(failed_dirs: int, units_failed: int) -> str | None:
    if not failed_dirs:
        return None
    text = ("1 mappe kunne ikke læses" if failed_dirs == 1
            else f"{failed_dirs} mapper kunne ikke læses")
    if units_failed:
        text += (" – 1 mappe på øverste niveau blev ikke opdateret" if units_failed == 1
                 else f" – {units_failed} mapper på øverste niveau blev ikke opdateret")
    return text


# --------------------------------------------------------------------------------------
# Shallow scan
# --------------------------------------------------------------------------------------

def shallow_scan(conn: sqlite3.Connection, source_id: int, root_path: str,
                 cfg: Mapping[str, Any], *,
                 first_time: bool, max_listings: int, cancel: threading.Event,
                 progress: ProgressFn | None = None, fs: str | None = None,
                 on_commit: CommitFn | None = None, lister: Lister | None = None,
                 lock: AbstractContextManager[Any] | None = None,
                 verify: VerifyFn | None = None) -> dict[str, Any]:
    """Refresh depth 1–2 quickly (SPEC §5.2); one transaction.  Result: deep_scan shape +
    ``listings``.

    Lists the root, every depth-1 dir that is new or whose own mtime differs from the stored
    ``dir_mtime`` (all of them unless ``fs`` is NTFS), and every new depth-2 dir once to
    classify it; ``first_time`` lists all depth-1 and depth-2 dirs.  At most ``max_listings``
    listings (root included).  Rows below depth 2 are never touched, except the subtrees of
    depth ≤ 2 entries that vanished from a successfully listed parent.  New directories get
    NULL size/file_count and, provisionally, their own mtime as ``mtime`` (SPEC §15.7: new
    projects show up in "Seneste projekter" at once; the deep scan stores the subtree
    aggregate); depth-2 dirs keep their stored ``dir_mtime`` (NULL when new) because their
    children were not refreshed.  ``verify`` as for :func:`deep_scan`.
    """
    scan = _Scan(conn, source_id, root_path, cfg, cancel=cancel, progress=progress,
                 on_commit=on_commit, lister=lister, lock=lock, verify=verify)
    try:
        return _shallow(scan, first_time=first_time, max_listings=max_listings, fs=fs)
    except _VolumeChanged:              # before the root listing
        return scan.result(ok=False, aborted=True, error=DISK_CHANGED, listings=0,
                           volume_changed=True)


def _shallow(scan: _Scan, *, first_time: bool, max_listings: int,
             fs: str | None) -> dict[str, Any]:
    conn, source_id, root_path, cancel = scan.conn, scan.source_id, scan.root_path, scan.cancel
    scan.units_total = 1
    rules = scan.rules
    mtime_reliable = (fs or "").upper() == "NTFS"
    budget = max(1, int(max_listings))
    scan.check_volume()
    try:
        subs1, files1 = scan.list("")
    except OSError as exc:
        return scan.result(ok=False, aborted=True, error=scan.root_error(exc), listings=1)
    listings = 1
    seen = len(subs1) + len(files1)
    with scan.lock:
        stored1 = {row[db.S_REL]: row for row in db.load_children(conn, source_id, "")}

    listed: dict[str, tuple[list[DirInfo], list[FileInfo]]] = {}
    for sub in subs1:
        if not sub.walkable:
            continue
        row = stored1.get(sub.name)
        changed = (row is None or row[db.S_KIND] == KIND_FILE
                   or row[db.S_DIR_MTIME] != sub.mtime)
        if not (first_time or not mtime_reliable or changed):
            continue
        if listings >= budget or cancel.is_set():
            break
        listings += 1
        try:
            listed[sub.name] = scan.list(sub.name)
        except OSError as exc:
            scan.failed_dirs += 1
            log.info("cannot list %s\\%s: %s", root_path, sub.name, exc)
            continue
        seen += len(listed[sub.name][0]) + len(listed[sub.name][1])
        scan.entries, scan.dirs = seen, listings
        scan.report()

    with scan.lock:
        stored2 = {d1: {row[db.S_REL]: row for row in db.load_children(conn, source_id, d1)}
                   for d1 in listed}
    classified: dict[str, list[str]] = {}          # depth-2 rel -> its sub-folder names
    for d1, (subs2, _files2) in listed.items():
        for sub in subs2:
            rel = f"{d1}\\{sub.name}"
            row = stored2[d1].get(rel)
            is_new = row is None or row[db.S_KIND] == KIND_FILE
            if not sub.walkable or not (first_time or is_new):
                continue
            if listings >= budget or cancel.is_set():
                break
            listings += 1
            try:
                classified[rel] = [s.name for s in scan.list(rel)[0]]
            except OSError as exc:
                scan.failed_dirs += 1
                log.info("cannot list %s\\%s: %s", root_path, rel, exc)
            scan.dirs = listings
            scan.report()
    if cancel.is_set():
        return scan.result(ok=False, aborted=True, error=None, listings=listings)

    diff = Diff()
    built: list[Entry] = []
    root_entries = build_file_entries("", 1, files1, rules, None)
    for sub in subs1:
        if not sub.walkable:
            root_entries.append(_plain_dir_entry("", 1, sub, None))
            continue
        row = stored1.get(sub.name)
        stored_dir = row is not None and row[db.S_KIND] != KIND_FILE
        name_fold = textutil.fold(sub.name)
        if sub.name in listed:
            subs2, files2 = listed[sub.name]
            is_template = rules.is_template_fold(name_fold)
            is_project = rules.is_project_dir(sub.name, name_fold, (s.name for s in subs2))
            prj = sub.name if is_project else None
            children, has_project_child = _shallow_children(
                sub.name, subs2, files2, prj, stored2[sub.name], classified, rules)
            built.extend(children)
            compute_diff(stored2[sub.name].values(), children, descendants_loaded=False,
                         into=diff)
            if is_template:
                kind = KIND_TEMPLATE
            elif is_project:
                kind = KIND_PROJECT
            else:
                kind = KIND_GROUP if has_project_child else KIND_TOPLEVEL
            size, mtime, file_count = ((row[db.S_SIZE], row[db.S_MTIME], row[db.S_FILE_COUNT])
                                       if stored_dir else (None, None, None))
            root_entries.append(Entry(sub.name, "", sub.name, name_fold, kind, 1, None, size,
                                      sub.mtime if mtime is None else mtime, file_count,
                                      sub.mtime, 0, None, prj))
        elif stored_dir:
            root_entries.append(Entry._make(row[1:]))          # unchanged, not listed
        else:                                                   # new, listing budget spent
            root_entries.append(Entry(sub.name, "", sub.name, name_fold,
                                      _name_kind(rules, name_fold, 1), 1, None, None, sub.mtime,
                                      None, None, 0, None, None))
    compute_diff(stored1.values(), root_entries, descendants_loaded=False, into=diff)
    built.extend(root_entries)

    scan.entries = scan.dirs = scan.files = 0         # were listing counters for progress
    for e in built:
        scan.count(e)
    try:
        scan.apply(diff)
    except _VolumeChanged:
        return scan.result(ok=False, aborted=True, error=DISK_CHANGED, listings=listings,
                           volume_changed=True)
    scan.units_done = 1
    scan.report(force=True)
    return scan.result(ok=True, aborted=False, error=_failure_text(scan.failed_dirs, 0),
                       listings=listings)


def _shallow_children(d1: str, subs: list[DirInfo], files: list[FileInfo], prj: str | None,
                      stored: Mapping[str, tuple], classified: Mapping[str, list[str]],
                      rules: ScanRules) -> tuple[list[Entry], bool]:
    """Depth-2 entries of a listed depth-1 dir, and whether one of them is a project."""
    out = build_file_entries(d1, 2, files, rules, prj)
    has_project = False
    for sub in subs:
        if not sub.walkable:
            out.append(_plain_dir_entry(d1, 2, sub, prj))
            continue
        rel = f"{d1}\\{sub.name}"
        row = stored.get(rel)
        stored_dir = row is not None and row[db.S_KIND] != KIND_FILE
        name_fold = textutil.fold(sub.name)
        if rel in classified:
            if rules.is_template_fold(name_fold):
                kind = KIND_TEMPLATE
            elif rules.is_project_dir(sub.name, name_fold, classified[rel]):
                kind = KIND_PROJECT
            else:   # group status needs depth 3: keep what the deep scan found
                kind = KIND_GROUP if stored_dir and row[db.S_KIND] == KIND_GROUP else KIND_DIR
        elif stored_dir:
            kind = row[db.S_KIND]
        else:
            kind = _name_kind(rules, name_fold, 2)
        if stored_dir:
            size, mtime = row[db.S_SIZE], row[db.S_MTIME]
            file_count, dir_mtime = row[db.S_FILE_COUNT], row[db.S_DIR_MTIME]
        else:
            size = mtime = file_count = dir_mtime = None
        if mtime is None:        # provisional: its own mtime (dir_mtime stays NULL, see above)
            mtime = sub.mtime
        has_project = has_project or kind == KIND_PROJECT
        out.append(Entry(rel, d1, sub.name, name_fold, kind, 2, None, size, mtime, file_count,
                         dir_mtime, 0, None, rel if kind == KIND_PROJECT else prj))
    return out, has_project
