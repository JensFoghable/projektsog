"""Helpers for the index-store tests: temporary trees and databases only."""

from __future__ import annotations

import ctypes
import logging
import os
import sqlite3
import tempfile
import threading
from collections.abc import Iterable
from ctypes import wintypes
from typing import Any

from projektsog import config, db, scanner

SETTLED_MTIME = 1_700_000_000.0          # 2023-11-14, far from "now" (no recency bonus)

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.SetFileAttributesW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
_kernel32.SetFileAttributesW.restype = wintypes.BOOL
FILE_ATTRIBUTE_HIDDEN = 0x2
FILE_ATTRIBUTE_SYSTEM = 0x4


class ModuleEnv:
    """setUpModule/tearDownModule state: a temp LOCALAPPDATA and quiet library logs."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="projektsog-test-")
        self.path = self._tmp.name
        os.environ["LOCALAPPDATA"] = self.path
        self._handler = logging.NullHandler()      # keeps expected warnings off stderr
        logging.getLogger("projektsog").addHandler(self._handler)

    def cleanup(self) -> None:
        logging.getLogger("projektsog").removeHandler(self._handler)
        self._tmp.cleanup()


def module_env() -> ModuleEnv:
    """Call from setUpModule() before any config path is used."""
    return ModuleEnv()


def cfg(**overrides: Any) -> dict[str, Any]:
    out = dict(config.DEFAULTS)
    out.update(overrides)
    return out


def make_tree(root: str, paths: Iterable[str], *, size: int = 1) -> None:
    """Create directories (paths ending in '/' or '\\') and files of ``size`` bytes."""
    for rel in paths:
        full = os.path.join(root, rel.rstrip("\\/"))
        if rel.endswith(("\\", "/")):
            os.makedirs(full, exist_ok=True)
        else:
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "wb") as fh:
                fh.write(b"x" * size)


def settle(root: str, mtime: float = SETTLED_MTIME) -> None:
    """Give every file and directory in ``root`` (inclusive) a fixed mtime, bottom-up.

    NTFS refreshes a directory's timestamp in its parent's index lazily, so a tree scanned right
    after it was built can report stale directory mtimes; explicit times make scans repeatable.
    """
    for dirpath, _dirs, files in os.walk(root, topdown=False):
        for name in files:
            os.utime(os.path.join(dirpath, name), (mtime, mtime))
        os.utime(dirpath, (mtime, mtime))


def set_mtime(path: str, mtime: float) -> None:
    os.utime(path, (mtime, mtime))


def set_attributes(path: str, attributes: int) -> None:
    if not _kernel32.SetFileAttributesW(path, attributes):
        raise ctypes.WinError(ctypes.get_last_error())


class CountingLister:
    """Lister that records listed paths and can fail or block chosen directories."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail: set[str] = set()          # path suffixes (relative, '\\'-separated)
        self._lock = threading.Lock()

    def __call__(self, path: str) -> list[os.DirEntry]:
        with self._lock:
            self.calls.append(path)
        if any(path.endswith("\\" + suffix) for suffix in self.fail):
            raise PermissionError(5, "Access is denied", path, 5)
        return scanner.list_dir(path)

    def listed(self, suffix: str) -> bool:
        return any(p.endswith("\\" + suffix) for p in self.calls)


class TempIndex:
    """A temporary index database (writer connection) plus scan helpers."""

    def __init__(self, directory: str, name: str = "index.db") -> None:
        self.path = os.path.join(directory, name)
        self.conn = db.connect(self.path, writer=True, check_same_thread=False)
        db.ensure_schema(self.conn)

    def close(self) -> None:
        self.conn.close()

    def add_source(self, root: str, *, key: str | None = None, **fields: Any) -> int:
        values = {"key": key or f"test:{root}", "kind": "local", "host": "TESTHOST",
                  "display_name": os.path.basename(root.rstrip("\\")) or root,
                  "current_path": root}
        values.update(fields)
        return db.upsert_source(self.conn, values)

    def deep(self, source_id: int, root: str, *, full: bool = False, is_network: bool = False,
             fs: str = "NTFS", config_: dict[str, Any] | None = None,
             **kwargs: Any) -> dict[str, Any]:
        kwargs.setdefault("cancel", threading.Event())
        kwargs.setdefault("progress", None)
        return scanner.deep_scan(self.conn, source_id, root, config_ or cfg(), full=full,
                                 is_network=is_network, fs=fs, **kwargs)

    def shallow(self, source_id: int, root: str, *, first_time: bool = False,
                max_listings: int = 2000, fs: str = "NTFS",
                config_: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        kwargs.setdefault("cancel", threading.Event())
        return scanner.shallow_scan(self.conn, source_id, root, config_ or cfg(),
                                    first_time=first_time, max_listings=max_listings, fs=fs,
                                    **kwargs)

    def rows(self, source_id: int) -> dict[str, sqlite3.Row]:
        cur = self.conn.cursor()
        cur.row_factory = sqlite3.Row
        return {r["rel_path"]: r for r in cur.execute(
            f"SELECT {db.ROW_COLUMNS} FROM entries WHERE source_id = ?", (source_id,))}

    def snapshot(self, source_id: int) -> dict[str, tuple]:
        """Rows without ids, for comparing two scans."""
        return {rel: tuple(row)[2:] for rel, row in self.rows(source_id).items()}

    def check_fts(self) -> None:
        """Raises sqlite3.DatabaseError if entries_fts is out of sync with entries."""
        self.conn.execute(
            "INSERT INTO entries_fts(entries_fts, rank) VALUES ('integrity-check', 1)")

    def fts_names(self, token: str) -> set[str]:
        return {name for (name,) in self.conn.execute(
            "SELECT e.name FROM entries_fts JOIN entries e ON e.id = entries_fts.rowid "
            "WHERE entries_fts MATCH ?", ('"' + token + '"',))}
