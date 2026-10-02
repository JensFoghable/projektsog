"""SQLite index database (SPEC §6): connections, schema v3, writes and read helpers.

Ownership: the scan worker writes ``entries`` (``entries_fts`` follows via triggers); the main
process writes ``sources`` and ``meta`` and runs :func:`ensure_schema` at startup before it starts
the worker.

Schema v3 (SPEC §15.2) adds ``entries.name_alt`` = ``textutil.alt(name_fold)`` when that differs
from ``name_fold`` (names with the ASCII spelling "oe" of "ø"), else NULL, as a second column of
``entries_fts``.  It is derived from ``name_fold`` inside :func:`apply_diff`, so it is not part of
:class:`Entry`.  A v2 index is migrated in place by :func:`ensure_schema` (no rescan).

Every connection made by :func:`connect` runs in autocommit mode (``isolation_level=None``).
Writes are grouped with :func:`transaction` (``BEGIN IMMEDIATE`` … ``COMMIT``); multi-statement
reads use :func:`read_snapshot`, so no transaction outlives a call and readers never keep a read
transaction open between requests.  A connection is used by one thread at a time; threads that
share one (the scan worker) serialise every call with their own lock.

Rel paths are relative to the source root, ``\\``-separated, without a leading ``\\``.  The
descendants of ``X`` are exactly the rel paths in ``[X + "\\", X + "]")`` because ``]`` is the
code point after ``\\``; such ranges are served by the ``UNIQUE(source_id, rel_path)`` index.
(An ``OR`` of an equality and that range is *not* index-served – use two statements.)
"""

from __future__ import annotations

import logging
import queue
import sqlite3
import threading
import time
from collections.abc import Collection, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass, field
from typing import Any, NamedTuple

from . import textutil

log = logging.getLogger(__name__)

SCHEMA_VERSION = 3
BUSY_TIMEOUT_MS = 10_000
JOURNAL_SIZE_LIMIT = 64 * 1024 * 1024

# entries.kind (SPEC §5.1)
KIND_FILE = 0
KIND_DIR = 1
KIND_PROJECT = 2
KIND_GROUP = 3
KIND_TEMPLATE = 4
KIND_TOPLEVEL = 5

# The entries table and its indexes (also recreated on their own by the v2 fallback below).
_ENTRIES_STATEMENTS = (
    """CREATE TABLE entries(
  id INTEGER PRIMARY KEY,
  source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  rel_path TEXT NOT NULL,
  parent_rel TEXT NOT NULL,
  name TEXT NOT NULL,
  name_fold TEXT NOT NULL,
  kind INTEGER NOT NULL,
  depth INTEGER NOT NULL,
  ext TEXT,
  size INTEGER, mtime REAL, file_count INTEGER,
  dir_mtime REAL,
  is_seq INTEGER NOT NULL DEFAULT 0, seq_count INTEGER,
  project_rel TEXT,
  name_alt TEXT,
  UNIQUE(source_id, rel_path)
)""",
    "CREATE INDEX ix_entries_parent ON entries(source_id, parent_rel)",
    "CREATE INDEX ix_entries_kind ON entries(kind, mtime)",
)
# (v3 appends name_alt as the last column: a migrated v2 table gets it there via ALTER TABLE.)

_SCHEMA_SQL = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE sources(
  id INTEGER PRIMARY KEY,
  key TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL,
  host TEXT NOT NULL,
  share TEXT,
  display_name TEXT NOT NULL,
  current_path TEXT NOT NULL,
  unc_path TEXT,
  volume_serial TEXT, volume_label TEXT, fs TEXT, volume_size INTEGER,
  last_drive TEXT,
  hotplug INTEGER NOT NULL DEFAULT 0,
  manual INTEGER NOT NULL DEFAULT 0,
  online INTEGER NOT NULL DEFAULT 0,
  mode TEXT NOT NULL DEFAULT 'auto',
  auto_include INTEGER, auto_reason TEXT, probed_at REAL,
  first_seen REAL, last_seen REAL,
  last_scan_start REAL, last_scan_end REAL, last_scan_ok INTEGER, last_full_scan REAL,
  last_shallow_scan REAL, last_error TEXT, scan_seconds REAL,
  entry_count INTEGER NOT NULL DEFAULT 0, dir_count INTEGER NOT NULL DEFAULT 0,
  file_count INTEGER NOT NULL DEFAULT 0, project_count INTEGER NOT NULL DEFAULT 0,
  total_size INTEGER NOT NULL DEFAULT 0
);
""" + "".join(statement + ";\n" for statement in _ENTRIES_STATEMENTS)

# The FTS table and its sync triggers (created with the schema; recreated by the v2 migration).
_FTS_STATEMENTS = (
    "CREATE VIRTUAL TABLE entries_fts USING fts5(name_fold, name_alt, content='entries', "
    "content_rowid='id', tokenize='trigram')",
    "CREATE TRIGGER entries_ai AFTER INSERT ON entries BEGIN "
    "INSERT INTO entries_fts(rowid, name_fold, name_alt) "
    "VALUES (new.id, new.name_fold, new.name_alt); END",
    "CREATE TRIGGER entries_ad AFTER DELETE ON entries BEGIN "
    "INSERT INTO entries_fts(entries_fts, rowid, name_fold, name_alt) "
    "VALUES ('delete', old.id, old.name_fold, old.name_alt); END",
    "CREATE TRIGGER entries_au AFTER UPDATE OF name_fold, name_alt ON entries BEGIN "
    "INSERT INTO entries_fts(entries_fts, rowid, name_fold, name_alt) "
    "VALUES ('delete', old.id, old.name_fold, old.name_alt); "
    "INSERT INTO entries_fts(rowid, name_fold, name_alt) "
    "VALUES (new.id, new.name_fold, new.name_alt); END",
)

_SCHEMA_OBJECTS = ("meta", "sources", "entries", "ix_entries_parent", "ix_entries_kind",
                   "entries_fts", "entries_ai", "entries_ad", "entries_au")

# SQLite primary result codes of a failed v2 → v3 migration that mean the v2 content itself is
# unusable (damaged, not a database, objects or columns missing): the index is rebuilt.  Any
# other failure (disk full, I/O error …) left the v2 index intact – the migration is one
# transaction – so only its entries are given up (:func:`_migrate_v2_without_entries`).
_UNUSABLE_CODES = frozenset({sqlite3.SQLITE_ERROR, sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB})
# sources columns that describe the scanned index (reset when the entries are given up)
_SCAN_STATE_COLUMNS = ("last_scan_start", "last_scan_end", "last_scan_ok", "last_full_scan",
                       "last_shallow_scan", "last_error")
_COUNT_COLUMNS = ("entry_count", "dir_count", "file_count", "project_count", "total_size")


# --------------------------------------------------------------------------------------
# Entry rows
# --------------------------------------------------------------------------------------

class Entry(NamedTuple):
    """One ``entries`` row without ``id``/``source_id`` (column order of the table)."""

    rel_path: str
    parent_rel: str
    name: str
    name_fold: str
    kind: int
    depth: int
    ext: str | None
    size: int | None
    mtime: float | None
    file_count: int | None
    dir_mtime: float | None
    is_seq: int
    seq_count: int | None
    project_rel: str | None


ENTRY_FIELDS: tuple[str, ...] = Entry._fields
# Stored rows from load_subtree()/load_children() are plain tuples (id, *Entry):
S_ID = 0
S_REL = 1 + ENTRY_FIELDS.index("rel_path")
S_PARENT = 1 + ENTRY_FIELDS.index("parent_rel")
S_NAME = 1 + ENTRY_FIELDS.index("name")
S_FOLD = 1 + ENTRY_FIELDS.index("name_fold")
S_KIND = 1 + ENTRY_FIELDS.index("kind")
S_SIZE = 1 + ENTRY_FIELDS.index("size")
S_MTIME = 1 + ENTRY_FIELDS.index("mtime")
S_FILE_COUNT = 1 + ENTRY_FIELDS.index("file_count")
S_DIR_MTIME = 1 + ENTRY_FIELDS.index("dir_mtime")
_STORED_COLUMNS = "id, " + ", ".join(ENTRY_FIELDS)

# Rows for the main process: (id, source_id, *Entry).  The read helpers below return
# sqlite3.Row objects with these keys (mapping access, e.g. row["name"]).
ROW_FIELDS: tuple[str, ...] = ("id", "source_id") + ENTRY_FIELDS
ROW_COLUMNS = ", ".join(ROW_FIELDS)

_INSERT_SQL = ("INSERT INTO entries(source_id, " + ", ".join(ENTRY_FIELDS) + ", name_alt) "
               "VALUES (" + ", ".join("?" * (len(ENTRY_FIELDS) + 2)) + ")")
# name_fold (and with it name_alt) is never SET, so the FTS update trigger does not fire; a
# changed name_fold is applied as delete + insert (see scanner.compute_diff).
_UPDATE_FIELDS = ("parent_rel", "name", "kind", "depth", "ext", "size", "mtime", "file_count",
                  "dir_mtime", "is_seq", "seq_count", "project_rel")
_UPDATE_SQL = ("UPDATE entries SET " + ", ".join(f"{f} = ?" for f in _UPDATE_FIELDS)
               + " WHERE id = ?")
_UPDATE_IDX = tuple(ENTRY_FIELDS.index(f) for f in _UPDATE_FIELDS)


def descendant_bounds(rel_path: str) -> tuple[str, str]:
    """``(lo, hi)`` with ``lo <= r < hi`` exactly for the descendants of ``rel_path``."""
    return rel_path + "\\", rel_path + "]"


def name_alt(name_fold: str) -> str | None:
    """``entries.name_alt`` for a folded name: ``textutil.alt(name_fold)`` if it differs."""
    alt = textutil.alt(name_fold)
    return alt if alt != name_fold else None


def normalize_rel(rel_path: str) -> str:
    """``/`` → ``\\``, no leading, trailing or doubled separators (``''`` = the source root)."""
    return "\\".join(part for part in rel_path.replace("/", "\\").split("\\") if part)


@dataclass(slots=True)
class Diff:
    """Changes applied in one transaction by :func:`apply_diff`."""

    inserts: list[Entry] = field(default_factory=list)
    updates: list[tuple[int, Entry]] = field(default_factory=list)   # (row id, new values)
    deletes: list[int] = field(default_factory=list)                 # row ids
    delete_descendants: list[str] = field(default_factory=list)      # rel paths (rows below)

    def __bool__(self) -> bool:
        return bool(self.inserts or self.updates or self.deletes or self.delete_descendants)


# --------------------------------------------------------------------------------------
# Connections, transactions, schema
# --------------------------------------------------------------------------------------

def connect(path: str, *, writer: bool = False,
            check_same_thread: bool = True) -> sqlite3.Connection:
    """Open ``path`` with the SPEC §6 pragmas (autocommit mode).

    ``writer=False`` connections are ``query_only``.  Pass ``check_same_thread=False`` only if
    the caller serialises all use of the connection (scan worker lock, :class:`ReaderPool`).
    Open readers only after :func:`ensure_schema` ran.
    """
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None,
                           check_same_thread=check_same_thread)
    try:
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous = NORMAL")
        if writer:
            conn.execute(f"PRAGMA journal_size_limit = {JOURNAL_SIZE_LIMIT}")
        else:
            conn.execute("PRAGMA query_only = ON")
    except BaseException:
        conn.close()
        raise
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """``BEGIN IMMEDIATE`` … ``COMMIT``; rolls back when the block or the commit fails."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        _rollback(conn)
        raise


@contextmanager
def read_snapshot(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run several SELECTs on one consistent snapshot; the read transaction always ends."""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN")
    try:
        yield conn
    finally:
        _rollback(conn)


def _rollback(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            log.debug("rollback failed", exc_info=True)


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create or migrate the schema (writer connection; main process before the worker runs).

    A v2 index is migrated in place (:func:`_migrate_v2`): sources, modes and entries are kept.
    When that fails for lack of resources (disk full, I/O error …) the v2 index is still intact
    and only its entries are given up (:func:`_migrate_v2_without_entries`): locations, their
    modes and the known disks survive, and if even that cannot be written the error is raised
    with the v2 index untouched (the next start tries again).  Otherwise the index is a
    rebuildable cache: a damaged v2 index, or a database with another (or no)
    ``schema_version``, is wiped and recreated – sources are rediscovered and entries rescanned.
    """
    version = _schema_version(conn)
    if version == str(SCHEMA_VERSION):
        missing = _missing_objects(conn)
        if not missing:
            return
        log.warning("index schema incomplete (missing %s): rebuilding", ", ".join(missing))
    elif version == "2" and not _missing_objects(conn, v2=True):
        try:
            _migrate_v2(conn)
            return
        except sqlite3.Error as exc:
            if _is_busy(exc):              # locked by another connection: try again later
                raise
            if _primary_code(exc) not in _UNUSABLE_CODES:
                log.error("migrating the index to schema v%d failed (%s): keeping the "
                          "locations, their index is rebuilt", SCHEMA_VERSION, exc)
                _migrate_v2_without_entries(conn)
                return
            log.exception("migrating the index to schema v%d failed: rebuilding it",
                          SCHEMA_VERSION)
    had_tables = _drop_all(conn)
    # auto_vacuum takes effect only before the first table exists, or after a VACUUM.
    conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
    if had_tables and conn.execute("PRAGMA auto_vacuum").fetchone()[0] != 2:
        conn.execute("VACUUM")
    mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
    if str(mode).lower() != "wal":
        log.warning("could not enable WAL (journal_mode=%s)", mode)
    try:
        conn.executescript(
            "BEGIN IMMEDIATE;" + _SCHEMA_SQL + "".join(s + ";" for s in _FTS_STATEMENTS)
            + f"INSERT INTO meta(key, value) VALUES ('schema_version', '{SCHEMA_VERSION}');"
            + "COMMIT;")
    except BaseException:
        _rollback(conn)
        raise
    log.info("index schema v%d created", SCHEMA_VERSION)


def _migrate_v2(conn: sqlite3.Connection) -> None:
    """Schema v2 → v3 in place (SPEC §15.2), in one transaction: add and fill
    ``entries.name_alt``, recreate ``entries_fts`` (+ triggers) with its second column and
    rebuild it from ``entries``.  Nothing is rescanned; sources, modes and meta are kept."""
    started = time.monotonic()
    with transaction(conn):
        for trigger in ("entries_ai", "entries_ad", "entries_au"):
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        conn.execute("DROP TABLE IF EXISTS entries_fts")
        if "name_alt" not in _entry_columns(conn):
            conn.execute("ALTER TABLE entries ADD COLUMN name_alt TEXT")
        rows = conn.execute(
            "SELECT id, name_fold FROM entries WHERE instr(name_fold, 'oe') > 0").fetchall()
        conn.executemany("UPDATE entries SET name_alt = ? WHERE id = ?",
                         [(name_alt(fold), row_id) for row_id, fold in rows])
        for statement in _FTS_STATEMENTS:
            conn.execute(statement)
        conn.execute("INSERT INTO entries_fts(entries_fts) VALUES ('rebuild')")
        # v3 also changed scan rules (dense sequence runs, template sub-folders are never
        # projects).  Incremental scans reuse the stored rows of unchanged leaf folders, so one
        # full rescan per source applies them everywhere (the indexer treats NULL as due).
        conn.execute("UPDATE sources SET last_full_scan = NULL")
        conn.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'",
                     (str(SCHEMA_VERSION),))
    log.info("index migrated to schema v%d in %.1f s (%d names with an alternative spelling)",
             SCHEMA_VERSION, time.monotonic() - started, len(rows))
    checkpoint(conn)                       # the rebuild went through the WAL


def _migrate_v2_without_entries(conn: sqlite3.Connection) -> None:
    """Schema v2 → v3 keeping ``sources`` and ``meta`` only (R2-IDX-2).

    The fallback when :func:`_migrate_v2` failed for lack of resources (typically a nearly full
    system disk: the FTS rebuild writes the whole index through the WAL).  ``entries`` and its
    FTS table are recreated empty and every source counts as never scanned (scan state and
    counters reset), so each location is scanned again – but the locations, their modes
    ("Medtag altid/aldrig"), the known disks and pending forgets are kept.  It needs almost no
    space: dropping the old tables frees far more pages than the empty new ones use.  Raises
    (with the v2 index intact) if even this cannot be written.
    """
    with transaction(conn):
        for trigger in ("entries_ai", "entries_ad", "entries_au"):
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        conn.execute("DROP TABLE IF EXISTS entries_fts")
        conn.execute("DROP TABLE IF EXISTS entries")
        for statement in (*_ENTRIES_STATEMENTS, *_FTS_STATEMENTS):
            conn.execute(statement)
        conn.execute("UPDATE sources SET "
                     + ", ".join([f"{c} = NULL" for c in _SCAN_STATE_COLUMNS]
                                 + [f"{c} = 0" for c in _COUNT_COLUMNS]))
        conn.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'",
                     (str(SCHEMA_VERSION),))
    log.warning("index schema v%d created without the old entries: the locations are kept "
                "and scanned again", SCHEMA_VERSION)
    checkpoint(conn)


def _primary_code(exc: sqlite3.Error) -> int | None:
    """The SQLite primary result code of ``exc`` (None when it did not come from SQLite)."""
    code = getattr(exc, "sqlite_errorcode", None)
    return None if code is None else code & 0xFF


def _is_busy(exc: sqlite3.Error) -> bool:
    return _primary_code(exc) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)


def _entry_columns(conn: sqlite3.Connection) -> set[str]:
    return {row[1] for row in conn.execute("PRAGMA table_info(entries)")}


def _missing_objects(conn: sqlite3.Connection, *, v2: bool = False) -> list[str]:
    """Schema objects (and v3 columns) that are missing."""
    names = _object_names(conn)
    missing = [n for n in _SCHEMA_OBJECTS if n not in names]
    if not v2 and "entries" in names and "name_alt" not in _entry_columns(conn):
        missing.append("entries.name_alt")
    return missing


def _object_names(conn: sqlite3.Connection) -> set[str]:
    return {name for (name,) in conn.execute("SELECT name FROM sqlite_master")}


def _schema_version(conn: sqlite3.Connection) -> str | None:
    if "meta" not in _object_names(conn):
        return None
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    except sqlite3.Error:        # a foreign 'meta' table
        return None
    return None if row is None else str(row[0])


def _drop_all(conn: sqlite3.Connection) -> bool:
    """Drop every schema object (older/unknown schema).  Returns True if tables existed."""
    tables = [name for (name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
    if not tables:
        return False
    log.warning("rebuilding index database (schema version is not %d)", SCHEMA_VERSION)
    conn.execute("PRAGMA foreign_keys = OFF")        # no-op inside a transaction
    try:
        with transaction(conn):
            for kind in ("trigger", "view"):
                for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = ?",
                                            (kind,)).fetchall():
                    conn.execute(f'DROP {kind.upper()} IF EXISTS "{name}"')
            # Virtual tables first: dropping them also drops their shadow tables.
            virtual = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND sql LIKE 'CREATE VIRTUAL TABLE%'").fetchall()
            for (name,) in virtual:
                conn.execute(f'DROP TABLE IF EXISTS "{name}"')
            for (name,) in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name NOT LIKE 'sqlite_%'").fetchall():
                conn.execute(f'DROP TABLE IF EXISTS "{name}"')
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
    return True


# --------------------------------------------------------------------------------------
# meta (main process)
# --------------------------------------------------------------------------------------

def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else row[0]


def set_meta(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    with transaction(conn):
        conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


# --------------------------------------------------------------------------------------
# sources (main process)
# --------------------------------------------------------------------------------------

SOURCE_FIELDS: tuple[str, ...] = (
    "key", "kind", "host", "share", "display_name", "current_path", "unc_path",
    "volume_serial", "volume_label", "fs", "volume_size", "last_drive", "hotplug", "manual",
    "online", "mode", "auto_include", "auto_reason", "probed_at", "first_seen", "last_seen",
    "last_scan_start", "last_scan_end", "last_scan_ok", "last_full_scan", "last_shallow_scan",
    "last_error", "scan_seconds", "entry_count", "dir_count", "file_count", "project_count",
    "total_size",
)
_SOURCE_FIELD_SET = frozenset(SOURCE_FIELDS)
_SOURCE_COLUMNS = "id, " + ", ".join(SOURCE_FIELDS)


def _check_source_fields(fields: Mapping[str, Any]) -> None:
    unknown = set(fields) - _SOURCE_FIELD_SET
    if unknown:
        raise ValueError(f"unknown source field(s): {', '.join(sorted(unknown))}")


def load_sources(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """All sources as dicts (``id`` + :data:`SOURCE_FIELDS`), ordered by id."""
    cur = conn.execute(f"SELECT {_SOURCE_COLUMNS} FROM sources ORDER BY id")
    names = [d[0] for d in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


def get_source(conn: sqlite3.Connection, source_id: int) -> dict[str, Any] | None:
    cur = conn.execute(f"SELECT {_SOURCE_COLUMNS} FROM sources WHERE id = ?", (source_id,))
    row = cur.fetchone()
    return None if row is None else dict(zip([d[0] for d in cur.description], row))


def find_source_id(conn: sqlite3.Connection, key: str) -> int | None:
    """Id of the source whose key equals ``key`` under ``casefold()`` (SPEC §4.1)."""
    want = key.casefold()
    for source_id, stored in conn.execute("SELECT id, key FROM sources").fetchall():
        if stored.casefold() == want:
            return source_id
    return None


def upsert_source(conn: sqlite3.Connection, fields: Mapping[str, Any]) -> int:
    """Insert or update the source with ``fields["key"]`` (casefold match); returns its id.

    An existing row keeps its stored key spelling; only the given fields are written.  A new row
    needs at least key, kind, host, display_name and current_path.
    """
    _check_source_fields(fields)
    key = fields.get("key")
    if not isinstance(key, str) or not key:
        raise ValueError("upsert_source needs a non-empty 'key'")
    with transaction(conn):
        source_id = find_source_id(conn, key)
        if source_id is not None:
            _update_source_row(conn, source_id, {k: v for k, v in fields.items() if k != "key"})
            return source_id
        names = list(fields)
        cur = conn.execute(
            f"INSERT INTO sources({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
            [fields[n] for n in names])
        return int(cur.lastrowid)


def update_source(conn: sqlite3.Connection, source_id: int, fields: Mapping[str, Any]) -> None:
    """Write ``fields`` (subset of :data:`SOURCE_FIELDS`) to one source."""
    _check_source_fields(fields)
    if fields:
        with transaction(conn):
            _update_source_row(conn, source_id, fields)


def _update_source_row(conn: sqlite3.Connection, source_id: int,
                       fields: Mapping[str, Any]) -> None:
    if fields:
        names = list(fields)
        conn.execute(f"UPDATE sources SET {', '.join(f'{n} = ?' for n in names)} WHERE id = ?",
                     [fields[n] for n in names] + [source_id])


def delete_source(conn: sqlite3.Connection, source_id: int) -> None:
    """Delete a source row.  Its entries cascade, so call this after the worker's ``forget``
    job finished (which deletes the entries in short transactions)."""
    with transaction(conn):
        conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))


# --------------------------------------------------------------------------------------
# entries: worker side
# --------------------------------------------------------------------------------------

def load_subtree(conn: sqlite3.Connection, source_id: int, rel_path: str) -> list[tuple]:
    """Stored rows ``(id, *Entry)`` of ``rel_path`` itself and all its descendants."""
    lo, hi = descendant_bounds(rel_path)
    return conn.execute(
        f"SELECT {_STORED_COLUMNS} FROM entries WHERE source_id = ? AND rel_path = ? "
        f"UNION ALL SELECT {_STORED_COLUMNS} FROM entries "
        f"WHERE source_id = ? AND rel_path >= ? AND rel_path < ?",
        (source_id, rel_path, source_id, lo, hi)).fetchall()


def load_children(conn: sqlite3.Connection, source_id: int, parent_rel: str) -> list[tuple]:
    """Stored rows ``(id, *Entry)`` whose parent is ``parent_rel`` (``''`` = the root)."""
    return conn.execute(
        f"SELECT {_STORED_COLUMNS} FROM entries WHERE source_id = ? AND parent_rel = ?",
        (source_id, parent_rel)).fetchall()


def apply_diff(conn: sqlite3.Connection, source_id: int, diff: Diff) -> tuple[int, int]:
    """Apply ``diff`` in one ``BEGIN IMMEDIATE`` transaction.

    Order: descendant deletes, row deletes, updates, inserts (a re-inserted rel path never
    collides).  Returns ``(rows changed, rows deleted)``; trigger-made FTS changes not counted.
    """
    if not diff:
        return 0, 0
    deleted = 0
    changed = 0
    with transaction(conn):
        for rel in diff.delete_descendants:
            lo, hi = descendant_bounds(rel)
            deleted += conn.execute(
                "DELETE FROM entries WHERE source_id = ? AND rel_path >= ? AND rel_path < ?",
                (source_id, lo, hi)).rowcount
        if diff.deletes:
            deleted += conn.executemany("DELETE FROM entries WHERE id = ?",
                                        [(row_id,) for row_id in diff.deletes]).rowcount
        changed += deleted
        if diff.updates:
            changed += conn.executemany(
                _UPDATE_SQL,
                [tuple(e[i] for i in _UPDATE_IDX) + (row_id,) for row_id, e in diff.updates]
            ).rowcount
        if diff.inserts:
            changed += conn.executemany(
                _INSERT_SQL, [(source_id, *e, name_alt(e.name_fold)) for e in diff.inserts]
            ).rowcount
    return changed, deleted


def delete_source_entries(conn: sqlite3.Connection, source_id: int, *,
                          lock: AbstractContextManager[Any] | None = None,
                          chunk: int = 5000,
                          cancel: threading.Event | None = None) -> int:
    """Delete all entries of a source in short transactions (``forget``); returns the count."""
    guard = lock if lock is not None else nullcontext()
    total = 0
    while cancel is None or not cancel.is_set():
        with guard, transaction(conn):
            n = conn.execute(
                "DELETE FROM entries WHERE id IN "
                "(SELECT id FROM entries WHERE source_id = ? LIMIT ?)", (source_id, chunk)
            ).rowcount
        total += n
        if n < chunk:
            break
    return total


def source_counts(conn: sqlite3.Connection, source_id: int) -> dict[str, int]:
    """Counters for ``sources`` (a sequence counts ``seq_count`` files)."""
    row = conn.execute(
        "SELECT count(*), "
        "coalesce(sum(kind >= 1), 0), "
        "coalesce(sum(CASE WHEN kind = 0 THEN coalesce(seq_count, 1) END), 0), "
        "coalesce(sum(kind = 2), 0), "
        "coalesce(sum(CASE WHEN kind = 0 THEN size END), 0) "
        "FROM entries WHERE source_id = ?", (source_id,)).fetchone()
    return {"entry_count": row[0], "dir_count": row[1], "file_count": row[2],
            "project_count": row[3], "total_size": row[4]}


def checkpoint(conn: sqlite3.Connection, *, wait_ms: int = 2000) -> tuple[int, int, int] | None:
    """``wal_checkpoint(TRUNCATE)``, waiting at most ``wait_ms`` for readers; busy is ignored.

    Returns ``(busy, wal_pages, checkpointed_pages)`` or None if the database was locked.
    """
    conn.execute(f"PRAGMA busy_timeout = {int(wait_ms)}")
    try:
        row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        return tuple(row) if row is not None else None
    except sqlite3.OperationalError as exc:
        log.debug("checkpoint skipped: %s", exc)
        return None
    finally:
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")


def incremental_vacuum(conn: sqlite3.Connection, *,
                       lock: AbstractContextManager[Any] | None = None,
                       step_pages: int = 4096) -> int:
    """Return free pages to the file system in short steps; returns the pages freed."""
    guard = lock if lock is not None else nullcontext()
    freed = 0
    while True:
        with guard:
            free = conn.execute("PRAGMA freelist_count").fetchone()[0]
            if free <= 0:
                return freed
            step = min(free, step_pages)
            # Each sqlite3_step frees one page: fetchall() runs the pragma to completion.
            conn.execute(f"PRAGMA incremental_vacuum({step})").fetchall()
            after = conn.execute("PRAGMA freelist_count").fetchone()[0]
        if after >= free:          # nothing could be freed (e.g. locked): give up
            return freed
        freed += free - after


# --------------------------------------------------------------------------------------
# entries: read helpers (main process; sqlite3.Row results with ROW_FIELDS keys)
# --------------------------------------------------------------------------------------

def _row_cursor(conn: sqlite3.Connection) -> sqlite3.Cursor:
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    return cur


def get_entry(conn: sqlite3.Connection, source_id: int, rel_path: str) -> sqlite3.Row | None:
    """The entry at ``rel_path``: exact match first, then case-insensitively (Windows paths)."""
    rel = normalize_rel(rel_path)
    if not rel:
        return None
    row, complete = _resolve(conn, source_id, rel)
    return row if complete else None


def nearest_entry(conn: sqlite3.Connection, source_id: int, rel_path: str) -> sqlite3.Row | None:
    """The deepest indexed entry at or above ``rel_path`` (case-insensitive), or None."""
    rel = normalize_rel(rel_path)
    return _resolve(conn, source_id, rel)[0] if rel else None


def _resolve(conn: sqlite3.Connection, source_id: int,
             rel: str) -> tuple[sqlite3.Row | None, bool]:
    """``(deepest matching row, whether it is rel itself)``."""
    cur = _row_cursor(conn)
    with read_snapshot(conn):
        row = cur.execute(f"SELECT {ROW_COLUMNS} FROM entries WHERE source_id = ? AND rel_path = ?",
                          (source_id, rel)).fetchone()
        if row is not None:
            return row, True
        found: str | None = None
        parent = ""
        for part in rel.split("\\"):
            want = part.casefold()
            names = conn.execute(
                "SELECT rel_path, name FROM entries WHERE source_id = ? AND parent_rel = ?",
                (source_id, parent)).fetchall()
            match = next((r for r, n in names if n.casefold() == want), None)
            if match is None:
                break
            found = parent = match
        else:
            return cur.execute(
                f"SELECT {ROW_COLUMNS} FROM entries WHERE source_id = ? AND rel_path = ?",
                (source_id, found)).fetchone(), True
        if found is None:
            return None, False
        return cur.execute(
            f"SELECT {ROW_COLUMNS} FROM entries WHERE source_id = ? AND rel_path = ?",
            (source_id, found)).fetchone(), False


def entries_of_kind(conn: sqlite3.Connection, kinds: Collection[int],
                    source_ids: Collection[int] | None = None) -> list[sqlite3.Row]:
    """All entries of the given kinds (e.g. ``(KIND_PROJECT,)`` for name suggestions)."""
    kinds = [int(k) for k in kinds]
    if not kinds:
        return []
    sql = (f"SELECT {ROW_COLUMNS} FROM entries WHERE kind IN ({', '.join('?' * len(kinds))})")
    params: list[Any] = list(kinds)
    if source_ids is not None:
        ids = [int(s) for s in source_ids]
        if not ids:
            return []
        sql += f" AND source_id IN ({', '.join('?' * len(ids))})"
        params += ids
    return _row_cursor(conn).execute(sql, params).fetchall()


def child_dir_names(conn: sqlite3.Connection, source_id: int, rel_path: str,
                    limit: int = 40) -> list[str]:
    """Names of the direct sub-folders of ``rel_path`` (case-insensitive order)."""
    return [name for (name,) in conn.execute(
        "SELECT name FROM entries WHERE source_id = ? AND parent_rel = ? AND kind >= 1 "
        "ORDER BY name COLLATE NOCASE LIMIT ?", (source_id, rel_path, limit))]


def files_named(conn: sqlite3.Connection, folded_names: Collection[str]) -> list[sqlite3.Row]:
    """Files whose ``name_fold`` is one of ``folded_names`` (e.g. the clips of a camera card)."""
    names = list(dict.fromkeys(folded_names))
    rows: list[sqlite3.Row] = []
    cur = _row_cursor(conn)
    for i in range(0, len(names), 500):
        chunk = names[i:i + 500]
        rows += cur.execute(f"SELECT {ROW_COLUMNS} FROM entries WHERE kind = ? AND name_fold IN "
                            f"({', '.join('?' * len(chunk))})", [KIND_FILE, *chunk]).fetchall()
    return rows


def template_prefixes(conn: sqlite3.Connection) -> dict[int, tuple[str, ...]]:
    """``{source_id: (template_rel + "\\", …)}`` for hiding template subtrees."""
    out: dict[int, list[str]] = {}
    for source_id, rel in conn.execute(
            "SELECT source_id, rel_path FROM entries WHERE kind = ?", (KIND_TEMPLATE,)):
        out.setdefault(source_id, []).append(rel + "\\")
    return {sid: tuple(prefixes) for sid, prefixes in out.items()}


class ReaderPool:
    """Read-only connections for request threads.

    SPEC §6 asks for per-thread read connections; ``ThreadingHTTPServer`` starts a new thread
    per request, so connections are borrowed per call instead (each used by one thread at a
    time, warm page cache reused, never more idle connections than ``max_idle``).
    """

    def __init__(self, path: str, max_idle: int = 8) -> None:
        self._path = path
        self._idle: queue.LifoQueue[sqlite3.Connection] = queue.LifoQueue(maxsize=max_idle)
        self._closed = False

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        try:
            conn = self._idle.get_nowait()
        except queue.Empty:
            conn = connect(self._path, check_same_thread=False)
        try:
            yield conn
        finally:
            _rollback(conn)
            if self._closed:
                conn.close()
            else:
                try:
                    self._idle.put_nowait(conn)
                except queue.Full:
                    conn.close()

    def close(self) -> None:
        """Close idle connections; connections in use are closed when returned."""
        self._closed = True
        while True:
            try:
                self._idle.get_nowait().close()
            except queue.Empty:
                return
