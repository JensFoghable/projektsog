""""I dag for N år siden" (search.memories, Indexer.memories, SPEC §23.9): the project with the most
files from this calendar day, year by year – projects only, never a template."""

import contextlib
import os
import tempfile
import threading
import unittest
from datetime import date, datetime
from unittest import mock

from projektsog import config, db, indexer, search
from tests._index_store_fixtures import TempIndex, make_tree, module_env, set_mtime, settle

_env = None
TODAY = date(2026, 10, 12)


def setUpModule():
    global _env
    _env = module_env()


def tearDownModule():
    _env.cleanup()


def noon(day: date) -> float:
    return datetime(day.year, day.month, day.day, 12).timestamp()


def project(name: str, *files: str) -> list[str]:
    return [f"{name}\\Klip\\", f"{name}\\Final\\", f"{name}\\Musik\\", *files]


class MemoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.index = TempIndex(cls._tmp.name)
        y1, y2, y3, y4 = (search.same_day(TODAY, n) for n in (1, 2, 3, 4))
        kunder = os.path.join(cls._tmp.name, "Kunder")
        dated: dict[str, date] = {}
        tree = (project("Rikke Lindholm", *[f"Rikke Lindholm\\Musik\\speak_{n}.wav" for n in range(5)],
                        *[f"Rikke Lindholm\\Klip\\FX9_{n}.MXF" for n in range(6)])
                + project("Mette Juhl", *[f"Mette Juhl\\Final\\still_{n}.png" for n in range(8)])
                + project("Klar Tand", *[f"Klar Tand\\Klip\\c{n}.mov" for n in range(4)])
                + [f"1. KUNDENAVN\\{d}\\" for d in ("Final", "Grafik", "Klip", "Logo", "Musik")]
                + [f"1. KUNDENAVN\\Klip\\skabelon_{n}.mov" for n in range(10)]
                + [f"Diverse\\noter_{n}.txt" for n in range(10)])
        make_tree(kunder, tree)
        settle(kunder)
        for n in range(5):
            dated[f"Rikke Lindholm\\Musik\\speak_{n}.wav"] = y1         # 1 year ago: 5 sound files
        for n in range(6):
            dated[f"Rikke Lindholm\\Klip\\FX9_{n}.MXF"] = y3            # 3 years ago: 6 clips …
        for n in range(8):
            dated[f"Mette Juhl\\Final\\still_{n}.png"] = y3             # … and 8 stills (more)
        for n in range(4):
            dated[f"Klar Tand\\Klip\\c{n}.mov"] = y2                    # 2 years ago: too few
        for n in range(10):
            dated[f"1. KUNDENAVN\\Klip\\skabelon_{n}.mov"] = y2         # … and a template
            dated[f"Diverse\\noter_{n}.txt"] = y4                       # 4 years ago: no project
        for rel, day in dated.items():
            set_mtime(os.path.join(kunder, rel), noon(day))
        # A share whose root is itself a project: 6 mixed files 4 years ago
        root = os.path.join(cls._tmp.name, "Efterår 2022")
        make_tree(root, ["Klip\\a.mov", "Klip\\b.mov", "Grafik\\c.png", "Grafik\\d.png", "Tekst\\e.txt",
                         "Tekst\\f.txt", "Final\\"])
        settle(root)
        for rel in ("Klip\\a.mov", "Klip\\b.mov", "Grafik\\c.png", "Grafik\\d.png", "Tekst\\e.txt", "Tekst\\f.txt"):
            set_mtime(os.path.join(root, rel), noon(y4))
        cls.roots = {"kunder": kunder, "root": root}
        cls.ids = {"kunder": cls.index.add_source(kunder, key="test:kunder"),
                   "root": cls.index.add_source(root, key="test:root")}
        for name, path in cls.roots.items():
            cls.index.deep(cls.ids[name], path)
        cls.reader = db.connect(cls.index.path)

    @classmethod
    def tearDownClass(cls):
        cls.reader.close()
        cls.index.close()
        cls._tmp.cleanup()

    def registry(self, *, root_online=True, root_project=True):
        k, r = self.ids["kunder"], self.ids["root"]
        return {k: {"id": k, "kind": "local", "host": "STUDIO-PC", "display_name": "Kunder",
                    "path": self.roots["kunder"], "online": True, "included": True},
                r: {"id": r, "kind": "share", "host": "NAS", "display_name": "Efterår 2022",
                    "path": self.roots["root"], "online": root_online, "included": True,
                    "root_is_project": root_project}}

    def test_the_nearest_years_with_a_project(self):
        found = search.memories(self.reader, self.registry(), TODAY)
        self.assertEqual([(m["name"], m["minde"]["aar_siden"], m["minde"]["antal"], m["minde"]["slags"])
                          for m in found],
                         [("Rikke Lindholm", 1, 5, "lydfiler"), ("Mette Juhl", 3, 8, "billeder"),
                          ("Efterår 2022", 4, 6, "filer")])
        self.assertEqual(found[0]["minde"]["dato"], "2025-10-12")
        self.assertEqual((found[0]["kind"], found[0]["rel_path"]), ("project", "Rikke Lindholm"))
        self.assertEqual((found[2]["id"], found[2]["rel_path"]), (-self.ids["root"], ""))
        self.assertFalse(self.reader.in_transaction)

    def test_limits_and_filters(self):
        self.assertEqual([m["name"] for m in search.memories(self.reader, self.registry(), TODAY, limit=1)],
                         ["Rikke Lindholm"])
        self.assertEqual([m["name"] for m in search.memories(self.reader, self.registry(), TODAY, years=2)],
                         ["Rikke Lindholm"])
        offline = search.memories(self.reader, self.registry(root_online=False), TODAY, online_only=True)
        self.assertEqual([m["name"] for m in offline], ["Rikke Lindholm", "Mette Juhl"])
        flagless = search.memories(self.reader, self.registry(root_project=False), TODAY)
        self.assertNotIn("Efterår 2022", [m["name"] for m in flagless])
        self.assertEqual(search.memories(self.reader, self.registry(), TODAY, minimum=50), [])
        self.assertEqual(search.memories(self.reader, {}, TODAY), [])

    def test_the_same_day_back_then(self):
        self.assertEqual(search.same_day(date(2026, 10, 12), 3), date(2023, 10, 12))
        self.assertEqual(search.same_day(date(2028, 2, 29), 1), date(2027, 2, 28))
        self.assertEqual(search.same_day(date(2028, 2, 29), 4), date(2024, 2, 29))

    def test_the_kind_of_files(self):
        self.assertEqual(search._memory_kind(10, 6, 2, 2), ("klip", 6))
        self.assertEqual(search._memory_kind(10, 5, 5, 0), ("filer", 10))    # no majority
        self.assertEqual(search._memory_kind(9, 0, 0, 9), ("lydfiler", 9))
        # FX9: every clip has an XML sidecar – it is still 180 clips, not 360 files
        self.assertEqual(search._memory_kind(360, 180, 0, 0), ("klip", 180))
        self.assertEqual(search._memory_kind(300, 2, 0, 0), ("filer", 300))  # a few clips among papers
        self.assertEqual(search._memory_kind(7, 0, 0, 0), ("filer", 7))


class _Pool:
    def __init__(self, conn) -> None:
        self.conn = conn

    @contextlib.contextmanager
    def connection(self):
        yield self.conn


class IndexerMemoryTests(unittest.TestCase):
    """Indexer.memories: asks search.memories once per day and keeps it 10 minutes."""

    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.cfg = config.Config(path=os.path.join(self.dir.name, "config.json"))
        self.ix = indexer.Indexer.__new__(indexer.Indexer)          # only what memories() needs
        self.ix.cfg = self.cfg
        self.ix._memories_lock = threading.Lock()
        self.ix._memories = None
        self.ix._readers = _Pool(object())
        self.ix._snapshot = lambda: type("View", (), {"sources": {1: {"id": 1}}})()
        self.now = 1000.0
        patcher = mock.patch.object(indexer.time, "monotonic", lambda: self.now)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_cached_per_day(self) -> None:
        calls = []

        def fake(conn, sources, day, *, online_only=False):
            calls.append((day, online_only))
            return [{"name": "Rikke Lindholm", "minde": {"aar_siden": 1}}]
        with mock.patch.object(indexer.search, "memories", fake):
            first = self.ix.memories(TODAY)
            first[0]["name"] = "changed"                                    # a copy: the cache stays
            self.assertEqual(self.ix.memories(TODAY)[0]["name"], "Rikke Lindholm")
            self.assertEqual(len(calls), 1)
            self.ix.memories(date(2026, 10, 13))                            # a new day
            self.assertEqual(len(calls), 2)
            self.now += indexer.MEMORIES_TTL_S + 1
            self.ix.memories(date(2026, 10, 13))                            # 10 minutes later
            self.assertEqual(len(calls), 3)
            self.cfg.update({"show_offline": False})
            self.ix.memories(date(2026, 10, 13))
            self.assertEqual(calls[-1], (date(2026, 10, 13), True))
            self.cfg.update({"search_minder": False})
            self.assertEqual(self.ix.memories(TODAY), [])
            self.assertEqual(len(calls), 4)

    def test_without_readers(self) -> None:
        self.ix._readers = None
        self.assertEqual(self.ix.memories(TODAY), [])


if __name__ == "__main__":
    unittest.main()
