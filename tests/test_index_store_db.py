"""Database layer (SPEC §6): schema, pragmas, FTS sync, sources API, read helpers."""

import os
import sqlite3
import tempfile
import threading
import unittest

from projektsog import db, scanner, search
from projektsog.db import KIND_DIR, KIND_FILE, KIND_PROJECT, KIND_TEMPLATE, Diff, Entry
from tests._index_store_fixtures import TempIndex, module_env

_env = None


def setUpModule():
    global _env
    _env = module_env()


def tearDownModule():
    _env.cleanup()


def entry(rel, kind=KIND_FILE, name_fold=None, **kw):
    parent, _, name = rel.rpartition("\\")
    values = dict(rel_path=rel, parent_rel=parent, name=name,
                  name_fold=name_fold if name_fold is not None else name.lower(), kind=kind,
                  depth=rel.count("\\") + 1, ext=None, size=1, mtime=1.0, file_count=None,
                  dir_mtime=None, is_seq=0, seq_count=None, project_rel=None)
    values.update(kw)
    return Entry(**values)


class DbTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.index = TempIndex(self.tmp)
        self.addCleanup(self.index.close)
        self.conn = self.index.conn
        self.sid = self.index.add_source("C:\\Root")

    def insert(self, *entries):
        return db.apply_diff(self.conn, self.sid, Diff(inserts=list(entries)))


class SchemaTests(DbTestCase):
    def test_pragmas_and_version(self):
        c = self.conn
        self.assertEqual(c.execute("PRAGMA auto_vacuum").fetchone()[0], 2)
        self.assertEqual(c.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(c.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(c.execute("PRAGMA synchronous").fetchone()[0], 1)
        self.assertEqual(c.execute("PRAGMA busy_timeout").fetchone()[0], 10000)
        self.assertEqual(c.execute("PRAGMA journal_size_limit").fetchone()[0], 67108864)
        self.assertEqual(db.get_meta(c, "schema_version"), "3")
        names = {n for (n,) in c.execute("SELECT name FROM sqlite_master")}
        self.assertTrue({"meta", "sources", "entries", "entries_fts", "ix_entries_parent",
                         "ix_entries_kind", "entries_ai", "entries_ad", "entries_au"} <= names)
        self.assertIn("name_alt", [r[1] for r in c.execute("PRAGMA table_info(entries)")])
        self.assertEqual([r[1] for r in c.execute("PRAGMA table_info(entries_fts)")],
                         ["name_fold", "name_alt"])

    def test_ensure_schema_is_idempotent_and_keeps_data(self):
        self.insert(entry("a"))
        db.ensure_schema(self.conn)
        self.assertEqual(len(self.index.rows(self.sid)), 1)

    def test_readers_are_query_only(self):
        reader = db.connect(self.index.path)
        self.addCleanup(reader.close)
        self.assertEqual(reader.execute("PRAGMA busy_timeout").fetchone()[0], 10000)
        with self.assertRaises(sqlite3.OperationalError):
            reader.execute("DELETE FROM entries")

    def test_foreign_schema_is_rebuilt(self):
        path = os.path.join(self.tmp, "old.db")
        old = sqlite3.connect(path)
        old.executescript("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);"
                          "INSERT INTO meta VALUES ('schema_version', '1');"
                          "CREATE TABLE files(id INTEGER PRIMARY KEY, path TEXT);"
                          "CREATE VIRTUAL TABLE files_fts USING fts5(path);")
        old.close()
        conn = db.connect(path, writer=True)
        self.addCleanup(conn.close)
        db.ensure_schema(conn)
        names = {n for (n,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertNotIn("files", names)
        self.assertNotIn("files_fts", names)
        self.assertEqual(db.get_meta(conn, "schema_version"), "3")
        self.assertEqual(conn.execute("PRAGMA auto_vacuum").fetchone()[0], 2)


# Schema v2 as released (before SPEC §15.2), to test the in-place migration to v3.
V2_SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE sources(
  id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, host TEXT NOT NULL,
  share TEXT, display_name TEXT NOT NULL, current_path TEXT NOT NULL, unc_path TEXT,
  volume_serial TEXT, volume_label TEXT, fs TEXT, volume_size INTEGER, last_drive TEXT,
  hotplug INTEGER NOT NULL DEFAULT 0, manual INTEGER NOT NULL DEFAULT 0,
  online INTEGER NOT NULL DEFAULT 0, mode TEXT NOT NULL DEFAULT 'auto',
  auto_include INTEGER, auto_reason TEXT, probed_at REAL, first_seen REAL, last_seen REAL,
  last_scan_start REAL, last_scan_end REAL, last_scan_ok INTEGER, last_full_scan REAL,
  last_shallow_scan REAL, last_error TEXT, scan_seconds REAL,
  entry_count INTEGER NOT NULL DEFAULT 0, dir_count INTEGER NOT NULL DEFAULT 0,
  file_count INTEGER NOT NULL DEFAULT 0, project_count INTEGER NOT NULL DEFAULT 0,
  total_size INTEGER NOT NULL DEFAULT 0);
CREATE TABLE entries(
  id INTEGER PRIMARY KEY,
  source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  rel_path TEXT NOT NULL, parent_rel TEXT NOT NULL, name TEXT NOT NULL,
  name_fold TEXT NOT NULL, kind INTEGER NOT NULL, depth INTEGER NOT NULL, ext TEXT,
  size INTEGER, mtime REAL, file_count INTEGER, dir_mtime REAL,
  is_seq INTEGER NOT NULL DEFAULT 0, seq_count INTEGER, project_rel TEXT,
  UNIQUE(source_id, rel_path));
CREATE INDEX ix_entries_parent ON entries(source_id, parent_rel);
CREATE INDEX ix_entries_kind ON entries(kind, mtime);
CREATE VIRTUAL TABLE entries_fts USING fts5(name_fold, content='entries', content_rowid='id',
                                            tokenize='trigram');
CREATE TRIGGER entries_ai AFTER INSERT ON entries BEGIN
  INSERT INTO entries_fts(rowid, name_fold) VALUES (new.id, new.name_fold);
END;
CREATE TRIGGER entries_ad AFTER DELETE ON entries BEGIN
  INSERT INTO entries_fts(entries_fts, rowid, name_fold) VALUES ('delete', old.id, old.name_fold);
END;
CREATE TRIGGER entries_au AFTER UPDATE OF name_fold ON entries BEGIN
  INSERT INTO entries_fts(entries_fts, rowid, name_fold) VALUES ('delete', old.id, old.name_fold);
  INSERT INTO entries_fts(rowid, name_fold) VALUES (new.id, new.name_fold);
END;
INSERT INTO meta(key, value) VALUES ('schema_version', '2');
INSERT INTO meta(key, value) VALUES ('known_volumes', '["5E3A0B21"]');
INSERT INTO sources(id, key, kind, host, display_name, current_path, volume_serial, mode,
                    online, last_scan_end, last_full_scan, entry_count)
  VALUES (7, 'vol:5E3A0B21:\\2024 Disk Sølv', 'local', 'STUDIO-PC', '2024 Disk Sølv',
          'H:\\2024 Disk Sølv', '5E3A0B21', 'include', 1, 1700000000.0, 1700000000.0, 5);
"""
V2_ENTRIES = [  # rel_path, name_fold, kind, project_rel
    ("Bøgely Jul 2024", "bogely jul 2024", KIND_PROJECT, "Bøgely Jul 2024"),
    ("Bøgely Jul 2024\\Final", "final", KIND_DIR, "Bøgely Jul 2024"),
    ("Bøgely Jul 2024\\Final\\Infomoede Skolen Kolding.mp4", "infomoede skolen kolding mp4",
     KIND_FILE, "Bøgely Jul 2024"),
    ("Koeb billet.webm", "koeb billet webm", KIND_FILE, None),
    ("Videoeksport", "videoeksport", KIND_DIR, None),
]
V2_COLUMNS = ", ".join(("id", "source_id") + db.ENTRY_FIELDS)


class MigrationTests(unittest.TestCase):
    """SRCH-1: a v2 index becomes v3 in place – nothing is rescanned or forgotten."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "index.db")
        old = sqlite3.connect(self.path, isolation_level=None)
        old.execute("PRAGMA auto_vacuum = INCREMENTAL")
        old.execute("PRAGMA journal_mode = WAL")
        old.executescript(V2_SCHEMA)
        for rel, fold, kind, project_rel in V2_ENTRIES:
            parent, _, name = rel.rpartition("\\")
            old.execute("INSERT INTO entries(source_id, rel_path, parent_rel, name, name_fold, "
                        "kind, depth, size, mtime, project_rel) VALUES (7, ?, ?, ?, ?, ?, ?, 1, "
                        "1700000000.0, ?)",
                        (rel, parent, name, fold, kind, rel.count("\\") + 1, project_rel))
        old.close()

    def open(self):
        conn = db.connect(self.path, writer=True)
        self.addCleanup(conn.close)
        return conn

    @staticmethod
    def entry_rows(conn):
        return conn.execute(f"SELECT {V2_COLUMNS} FROM entries ORDER BY id").fetchall()

    @staticmethod
    def fts_names(conn, query):
        return {name for (name,) in conn.execute(
            "SELECT e.name FROM entries_fts JOIN entries e ON e.id = entries_fts.rowid "
            "WHERE entries_fts MATCH ?", (query,))}

    def test_v2_index_is_migrated_in_place(self):
        conn = self.open()
        before = self.entry_rows(conn)
        db.ensure_schema(conn)
        self.assertEqual(db.get_meta(conn, "schema_version"), "3")
        self.assertEqual(self.entry_rows(conn), before)                # same ids, same rows
        self.assertEqual(db.get_meta(conn, "known_volumes"), '["5E3A0B21"]')
        source = db.get_source(conn, 7)
        self.assertEqual((source["mode"], source["entry_count"], source["last_scan_end"]),
                         ("include", 5, 1700000000.0))
        self.assertIsNone(source["last_full_scan"])       # one full rescan for the v3 scan rules
        self.assertEqual(dict(conn.execute("SELECT name, name_alt FROM entries")), {
            "Bøgely Jul 2024": None, "Final": None, "Videoeksport": "videoksport",
            "Infomoede Skolen Kolding.mp4": "infomode skolen kolding mp4",
            "Koeb billet.webm": "kob billet webm"})
        conn.execute("INSERT INTO entries_fts(entries_fts, rank) VALUES ('integrity-check', 1)")
        self.assertEqual(self.fts_names(conn, '"infomode"'), {"Infomoede Skolen Kolding.mp4"})
        self.assertEqual(self.fts_names(conn, '"kob" OR "koeb"'), {"Koeb billet.webm"})
        self.assertEqual(self.fts_names(conn, '"eksport"'), {"Videoeksport"})
        self.assertEqual(self.fts_names(conn, '"bogely"'), {"Bøgely Jul 2024"})
        self.assertEqual(conn.execute("PRAGMA auto_vacuum").fetchone()[0], 2)
        self.assertEqual(os.path.getsize(self.path + "-wal"), 0)        # checkpointed

        # The migrated index keeps working: writes stay in sync, searches see both spellings.
        db.apply_diff(conn, 7, Diff(inserts=[entry("Moebler.mov", name_fold="moebler mov")]))
        self.assertEqual(self.fts_names(conn, '"mobler"'), {"Moebler.mov"})
        gone = db.load_subtree(conn, 7, "Koeb billet.webm")
        db.apply_diff(conn, 7, Diff(deletes=[r[db.S_ID] for r in gone]))
        conn.execute("INSERT INTO entries_fts(entries_fts, rank) VALUES ('integrity-check', 1)")
        reader = db.connect(self.path)
        self.addCleanup(reader.close)
        registry = {7: {"id": 7, "display_name": "2024 Disk Sølv", "kind": "local",
                        "path": "H:\\2024 Disk Sølv", "online": True, "included": True}}
        found = search.search(reader, registry, "infomøde")["results"]
        self.assertEqual([r["name"] for r in found], ["Infomoede Skolen Kolding.mp4"])
        self.assertEqual(found[0]["hl"], [[0, 9]])

        db.ensure_schema(conn)                                         # v3: nothing to do
        self.assertEqual(len(self.entry_rows(conn)), len(before))

    def test_a_locked_index_is_left_alone(self):
        blocker = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(blocker.close)
        blocker.execute("BEGIN IMMEDIATE")
        conn = self.open()
        conn.execute("PRAGMA busy_timeout = 50")
        with self.assertRaises(sqlite3.OperationalError):
            db.ensure_schema(conn)
        blocker.execute("ROLLBACK")
        self.assertEqual(db.get_meta(conn, "schema_version"), "2")     # not wiped
        self.assertEqual(len(self.entry_rows(conn)), len(V2_ENTRIES))
        db.ensure_schema(conn)
        self.assertEqual(db.get_meta(conn, "schema_version"), "3")
        self.assertEqual(len(self.entry_rows(conn)), len(V2_ENTRIES))

    def test_a_full_disk_during_the_migration_keeps_the_locations(self):
        # R2-IDX-2: the migration is one transaction, so a resource error leaves the v2 index
        # intact - the locations (their modes) and the known disks are kept; only the entries
        # are given up and every location is scanned again.
        conn = self.open()
        with db.transaction(conn):
            conn.executemany(
                "INSERT INTO entries(source_id, rel_path, parent_rel, name, name_fold, kind, "
                "depth) VALUES (7, ?, '', ?, ?, 0, 1)",
                [(f"Infomoede {i}.mp4", f"Infomoede {i}.mp4", f"infomoede {i} mp4")
                 for i in range(300)])
        conn.execute("INSERT INTO meta(key, value) VALUES ('pending_forget', '[3]')")
        db.checkpoint(conn)
        pages = conn.execute("PRAGMA page_count").fetchone()[0]
        conn.execute(f"PRAGMA max_page_count = {pages}")          # the disk is full
        with self.assertLogs("projektsog.db", "ERROR") as logs:
            db.ensure_schema(conn)
        self.assertIn("database or disk is full", "\n".join(logs.output))
        self.assertEqual(db.get_meta(conn, "schema_version"), "3")
        self.assertEqual(db.get_meta(conn, "known_volumes"), '["5E3A0B21"]')
        self.assertEqual(db.get_meta(conn, "pending_forget"), "[3]")
        source = db.get_source(conn, 7)
        self.assertEqual((source["key"], source["mode"], source["current_path"]),
                         ("vol:5E3A0B21:\\2024 Disk Sølv", "include", "H:\\2024 Disk Sølv"))
        self.assertEqual((source["last_scan_end"], source["last_full_scan"],
                          source["last_shallow_scan"], source["entry_count"]),
                         (None, None, None, 0))                  # scanned again from scratch
        self.assertEqual(self.entry_rows(conn), [])
        self.assertEqual([r[1] for r in conn.execute("PRAGMA table_info(entries)")],
                         ["id", "source_id", *db.ENTRY_FIELDS, "name_alt"])
        db.apply_diff(conn, 7, Diff(inserts=[entry("Moebler.mov", name_fold="moebler mov")]))
        self.assertEqual(self.fts_names(conn, '"mobler"'), {"Moebler.mov"})
        conn.execute("INSERT INTO entries_fts(entries_fts, rank) VALUES ('integrity-check', 1)")
        db.ensure_schema(conn)                                   # v3 now: nothing to do
        self.assertEqual(len(self.entry_rows(conn)), 1)

    def test_an_index_that_cannot_be_written_is_left_for_the_next_start(self):
        conn = self.open()
        before = self.entry_rows(conn)
        conn.execute("PRAGMA query_only = ON")          # not even the fallback can be written
        with self.assertLogs("projektsog.db", "ERROR"), \
                self.assertRaises(sqlite3.OperationalError):
            db.ensure_schema(conn)
        conn.execute("PRAGMA query_only = OFF")
        self.assertEqual(db.get_meta(conn, "schema_version"), "2")     # untouched
        self.assertEqual(self.entry_rows(conn), before)
        self.assertEqual(db.get_source(conn, 7)["mode"], "include")
        db.ensure_schema(conn)                                          # the next start
        self.assertEqual(db.get_meta(conn, "schema_version"), "3")
        self.assertEqual(self.entry_rows(conn), before)

    def test_incomplete_or_broken_v2_index_is_rebuilt(self):
        conn = self.open()
        conn.execute("DROP TABLE entries_fts")
        db.ensure_schema(conn)
        self.assertEqual(db.get_meta(conn, "schema_version"), "3")
        self.assertEqual(self.entry_rows(conn), [])
        broken = os.path.join(os.path.dirname(self.path), "broken.db")
        old = sqlite3.connect(broken, isolation_level=None)
        old.executescript(V2_SCHEMA.replace("name_fold TEXT NOT NULL,", "")
                          .replace("fts5(name_fold,", "fts5(name,")
                          .replace("new.name_fold", "new.name").replace("old.name_fold", "old.name")
                          .replace("rowid, name_fold)", "rowid, name)")
                          .replace("UPDATE OF name_fold", "UPDATE OF name"))
        old.close()
        conn = db.connect(broken, writer=True)
        self.addCleanup(conn.close)
        db.ensure_schema(conn)                              # the migration fails: rebuilt
        self.assertEqual(db.get_meta(conn, "schema_version"), "3")
        self.assertIn("name_alt", [r[1] for r in conn.execute("PRAGMA table_info(entries)")])


class FtsSyncTests(DbTestCase):
    def test_insert_update_delete_keep_fts_in_sync(self):
        self.insert(entry("Rikke Lindholm", KIND_PROJECT), entry("Rikke Lindholm\\klip.mxf"))
        self.assertEqual(self.index.fts_names("lindholm"), {"Rikke Lindholm"})
        rows = db.load_subtree(self.conn, self.sid, "Rikke Lindholm")
        by_rel = {r[db.S_REL]: r for r in rows}
        new = [entry("Rikke Lindholm", KIND_PROJECT, size=99),                  # data update
               entry("Rikke Lindholm\\klip.mxf", name_fold="klip mxf rev2")]    # refold
        diff = scanner.compute_diff(rows, new, descendants_loaded=True)
        self.assertEqual((len(diff.updates), len(diff.deletes), len(diff.inserts)), (1, 1, 1))
        self.assertEqual(db.apply_diff(self.conn, self.sid, diff), (3, 1))
        self.index.check_fts()
        self.assertEqual(self.index.fts_names("rev2"), {"klip.mxf"})
        self.assertEqual(self.index.fts_names("klip mxf"), {"klip.mxf"})
        self.assertEqual(self.index.rows(self.sid)["Rikke Lindholm"]["size"], 99)
        self.assertEqual(self.index.rows(self.sid)["Rikke Lindholm"]["id"],
                         by_rel["Rikke Lindholm"][db.S_ID])
        db.apply_diff(self.conn, self.sid, Diff(deletes=[by_rel["Rikke Lindholm"][db.S_ID]]))
        self.assertEqual(self.index.fts_names("lindholm"), set())
        self.index.check_fts()

    def test_oe_spellings_get_name_alt_and_both_columns_are_searchable(self):
        # SRCH-1: name_alt = alt(name_fold) only where it differs; FTS covers both columns.
        self.insert(entry("Infomoede Skolen.mp4", name_fold="infomoede skolen mp4"),
                    entry("Bøgely", KIND_DIR, name_fold="bogely"),
                    entry("Boegely", KIND_DIR, name_fold="boegely"))
        stored = dict(self.conn.execute("SELECT name, name_alt FROM entries"))
        self.assertEqual(stored, {"Infomoede Skolen.mp4": "infomode skolen mp4", "Bøgely": None,
                                  "Boegely": "bogely"})
        self.assertEqual(self.index.fts_names("infomode"), {"Infomoede Skolen.mp4"})
        self.assertEqual(self.index.fts_names("bogely"), {"Bøgely", "Boegely"})
        self.assertEqual(self.index.fts_names("boegely"), {"Boegely"})
        self.index.check_fts()
        rows = db.load_subtree(self.conn, self.sid, "Boegely")
        db.apply_diff(self.conn, self.sid, Diff(deletes=[r[db.S_ID] for r in rows]))
        self.assertEqual(self.index.fts_names("bogely"), {"Bøgely"})
        self.index.check_fts()
        self.assertEqual((db.name_alt("koeb billet"), db.name_alt("bogely")),
                         ("kob billet", None))

    def test_source_delete_cascades_to_entries_and_fts(self):
        self.insert(entry("Pixelbro", KIND_PROJECT), entry("Pixelbro\\a.mov"))
        db.delete_source(self.conn, self.sid)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM entries").fetchone()[0], 0)
        self.assertEqual(self.index.fts_names("pixelbro"), set())
        self.index.check_fts()

    def test_descendant_delete_spares_similar_names(self):
        self.insert(entry("Klip", KIND_DIR), entry("Klip\\a"), entry("Klip\\b\\c"),
                    entry("Klip 2", KIND_DIR), entry("Klip 2\\x"), entry("Klip]"), entry("Klipx"))
        changed, deleted = db.apply_diff(self.conn, self.sid,
                                         Diff(delete_descendants=["Klip"]))
        self.assertEqual((changed, deleted), (2, 2))
        self.assertEqual(set(self.index.rows(self.sid)),
                         {"Klip", "Klip 2", "Klip 2\\x", "Klip]", "Klipx"})
        self.index.check_fts()

    def test_load_subtree_and_children(self):
        self.insert(entry("A", KIND_DIR), entry("A\\x"), entry("A\\B", KIND_DIR),
                    entry("A\\B\\y"), entry("AB"), entry("A B"))
        self.assertEqual({r[db.S_REL] for r in db.load_subtree(self.conn, self.sid, "A")},
                         {"A", "A\\x", "A\\B", "A\\B\\y"})
        self.assertEqual({r[db.S_REL] for r in db.load_children(self.conn, self.sid, "")},
                         {"A", "AB", "A B"})

    def test_failed_transaction_rolls_back(self):
        self.insert(entry("a"))
        with self.assertRaises(sqlite3.IntegrityError):
            db.apply_diff(self.conn, self.sid, Diff(inserts=[entry("b"), entry("a")]))
        self.assertEqual(set(self.index.rows(self.sid)), {"a"})
        self.assertFalse(self.conn.in_transaction)


class SourceTests(DbTestCase):
    def test_upsert_matches_keys_case_insensitively(self):
        fields = {"key": "unc:GRAFIK-PC\\Forår 2026 (HDD)", "kind": "share",
                  "host": "GRAFIK-PC", "display_name": "Forår 2026 (HDD)",
                  "current_path": "\\\\GRAFIK-PC\\Forår 2026 (HDD)", "online": True}
        sid = db.upsert_source(self.conn, fields)
        again = db.upsert_source(self.conn, {**fields, "key": "unc:grafik-pc\\FORÅR 2026 (hdd)",
                                             "online": False, "entry_count": 12})
        self.assertEqual(sid, again)
        source = db.get_source(self.conn, sid)
        self.assertEqual(source["key"], fields["key"])            # stored spelling kept
        self.assertEqual((source["online"], source["entry_count"], source["mode"]),
                         (0, 12, "auto"))
        self.assertEqual(db.find_source_id(self.conn, "UNC:GRAFIK-PC\\forår 2026 (HDD)"), sid)
        self.assertIsNone(db.find_source_id(self.conn, "unc:OTHER\\x"))

    def test_update_load_delete(self):
        db.update_source(self.conn, self.sid, {"mode": "include", "last_error": "x",
                                               "last_scan_ok": True})
        loaded = {s["id"]: s for s in db.load_sources(self.conn)}
        self.assertEqual((loaded[self.sid]["mode"], loaded[self.sid]["last_scan_ok"]),
                         ("include", 1))
        self.assertEqual(set(loaded[self.sid]), {"id", *db.SOURCE_FIELDS})
        db.delete_source(self.conn, self.sid)
        self.assertIsNone(db.get_source(self.conn, self.sid))

    def test_unknown_fields_are_rejected(self):
        with self.assertRaises(ValueError):
            db.update_source(self.conn, self.sid, {"current_path; DROP TABLE x": 1})
        with self.assertRaises(ValueError):
            db.upsert_source(self.conn, {"kind": "local"})

    def test_meta(self):
        db.set_meta(self.conn, "hosts_seen", "a")
        db.set_meta(self.conn, "hosts_seen", "b")
        self.assertEqual(db.get_meta(self.conn, "hosts_seen"), "b")
        self.assertIsNone(db.get_meta(self.conn, "missing"))


class ReadHelperTests(DbTestCase):
    def setUp(self):
        super().setUp()
        self.insert(entry("Bøgely Jul 2024", KIND_PROJECT),
                    entry("Bøgely Jul 2024\\Klip", KIND_DIR, project_rel="Bøgely Jul 2024"),
                    entry("Bøgely Jul 2024\\Klip\\a.mxf", project_rel="Bøgely Jul 2024"),
                    entry("Bøgely Jul 2024\\Final", KIND_DIR),
                    entry("Bøgely Jul 2024\\grafik", KIND_DIR),
                    entry("1. KUNDENAVN", KIND_TEMPLATE))

    def test_get_entry_exact_and_case_insensitive(self):
        row = db.get_entry(self.conn, self.sid, "Bøgely Jul 2024\\Klip\\a.mxf")
        self.assertEqual((row["name"], row["source_id"]), ("a.mxf", self.sid))
        row = db.get_entry(self.conn, self.sid, "BØGELY JUL 2024/klip/A.MXF")
        self.assertEqual(row["rel_path"], "Bøgely Jul 2024\\Klip\\a.mxf")
        self.assertIsNone(db.get_entry(self.conn, self.sid, "Bøgely Jul 2024\\Klip\\b.mxf"))
        self.assertIsNone(db.get_entry(self.conn, self.sid, ""))

    def test_nearest_entry(self):
        row = db.nearest_entry(self.conn, self.sid, "bøgely jul 2024\\KLIP\\FX9\\x.mxf")
        self.assertEqual(row["rel_path"], "Bøgely Jul 2024\\Klip")
        self.assertEqual(row["project_rel"], "Bøgely Jul 2024")
        self.assertIsNone(db.nearest_entry(self.conn, self.sid, "Andet\\x"))

    def test_lists(self):
        self.assertEqual(db.child_dir_names(self.conn, self.sid, "Bøgely Jul 2024"),
                         ["Final", "grafik", "Klip"])
        self.assertEqual([r["name"] for r in db.entries_of_kind(self.conn, [KIND_PROJECT])],
                         ["Bøgely Jul 2024"])
        self.assertEqual(db.entries_of_kind(self.conn, [KIND_PROJECT], source_ids=[]), [])
        self.assertEqual(db.template_prefixes(self.conn), {self.sid: ("1. KUNDENAVN\\",)})
        self.assertEqual(db.source_counts(self.conn, self.sid),
                         {"entry_count": 6, "dir_count": 5, "file_count": 1,
                          "project_count": 1, "total_size": 1})

    def test_reader_pool_reuses_connections(self):
        pool = db.ReaderPool(self.index.path, max_idle=2)
        self.addCleanup(pool.close)
        with pool.connection() as first:
            first.execute("BEGIN")
            first.execute("SELECT count(*) FROM entries").fetchone()
        self.assertFalse(first.in_transaction)                 # rolled back when returned
        with pool.connection() as again:
            self.assertIs(again, first)
            with pool.connection() as second:
                self.assertIsNot(second, first)

        errors = []

        def use():
            try:
                with pool.connection() as conn:
                    conn.execute("SELECT count(*) FROM entries").fetchone()
            except Exception as exc:                            # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=use) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])


class MaintenanceTests(DbTestCase):
    def test_chunked_forget_checkpoint_and_vacuum(self):
        entries = [entry(f"d\\f{i:05d}", name_fold=f"file number {i}") for i in range(6000)]
        self.insert(entry("d", KIND_DIR), *entries)
        deleted = db.delete_source_entries(self.conn, self.sid, chunk=2500)
        self.assertEqual(deleted, 6001)
        self.assertEqual(db.source_counts(self.conn, self.sid)["entry_count"], 0)
        self.index.check_fts()
        self.assertGreater(db.incremental_vacuum(self.conn, step_pages=16), 0)
        self.assertEqual(self.conn.execute("PRAGMA freelist_count").fetchone()[0], 0)
        busy, _log, _done = db.checkpoint(self.conn)
        self.assertEqual(busy, 0)
        self.assertEqual(self.conn.execute("PRAGMA busy_timeout").fetchone()[0], 10000)
        self.assertEqual(os.path.getsize(self.index.path + "-wal"), 0)


if __name__ == "__main__":
    unittest.main()
