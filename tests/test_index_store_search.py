"""Search (SPEC §7): matching rules, ranking on realistic fixtures, filters, item shapes."""

import os
import tempfile
import time
import unittest
from unittest import mock

from projektsog import db, search
from tests._index_store_fixtures import (SETTLED_MTIME, TempIndex, make_tree, module_env,
                                         set_mtime, settle)

_env = None
TEMPLATE = ["Final", "Grafik", "Klip", "Logo", "Musik", "Project", "Speak", "Tekst"]


def setUpModule():
    global _env
    _env = module_env()


def tearDownModule():
    _env.cleanup()


def project(name, *extra):
    return [f"{name}\\Klip\\", f"{name}\\Final\\", *extra]


def hidden(kind, offline, source, any_):
    """Expected ``hidden``: per filter what only it hides; ``any`` (alias ``all``)."""
    return {"kind": kind, "offline": offline, "source": source, "any": any_, "all": any_}


# name -> (Source fields as the Indexer registry would hold them, tree, {project: mtime offset})
FIXTURE = {
    "studio": ({"kind": "local", "host": "STUDIO-PC", "display_name": "Kunder 2026 (STUDIO)",
                "volume_label": "Lokal disk", "path": "C:\\Kunder 2026 (STUDIO)",
                "unc_path": "\\\\STUDIO-PC\\Kunder 2026 (STUDIO)", "volume_size": 10**12},
               [f"1. KUNDENAVN\\{d}\\" for d in TEMPLATE]
               + [f"Rikke Lindholm\\{d}\\" for d in TEMPLATE]
               + ["Rikke Lindholm\\Klip\\FX9\\FX9_7912.MXF",
                  "Rikke Lindholm\\Klip\\FX9\\FX9_7913.MXF",
                  "Rikke Lindholm\\Final\\Rikke Lindholm - Testimonial.mp4"]
               + [f"Rikke Lindholm\\Grafik\\render_{i:04d}.exr" for i in range(1, 26)]
               + project("Klar Tand 2026\\Klar Tand - Silkeborg C")
               + project("Klar Tand 2026\\Klar Tand - Voxpop Silkeborg")
               + project("Bøgely Jul 2025")
               + ["Sound Effects\\boom.wav", "Export Presets\\", "PROMPT - Subtitles.txt"],
               {"Bøgely Jul 2025": 5000, "Rikke Lindholm": 4000}),
    "grafik": ({"kind": "share", "host": "GRAFIK-PC", "display_name": "Kunder 2026 (Grafik)",
                "volume_label": None, "path": "\\\\GRAFIK-PC\\Kunder 2026 (Grafik)",
                "unc_path": "\\\\GRAFIK-PC\\Kunder 2026 (Grafik)"},
               project("Klar Tand 2026\\Klar Tand - Silkeborg") + project("Bøgely Festival 2025")
               + project("Hotel Bøgelyhus") + [f"1. KUNDENAVN\\{d}\\" for d in TEMPLATE],
               {"Hotel Bøgelyhus": 3000}),
    "forar": ({"kind": "local", "host": "STUDIO-PC", "display_name": "Forår 2026 RØD",
               "volume_label": "Forår 2026 RØD", "path": "D:\\",
               "unc_path": "\\\\STUDIO-PC\\Forår 2026 RØD"},
              project("Pixelbro", "Pixelbro\\Final\\Pixelbro final.mp4"), {}),
    "solv": ({"kind": "local", "host": "STUDIO-PC", "display_name": "2024 Disk Sølv",
              "volume_label": "2024 Disk Sølv", "path": "H:\\2024 Disk Sølv",
              "unc_path": None, "hotplug": True},
             project("Pixelbro Radio") + project("Bøgely Jul 2024"),
             {"Bøgely Jul 2024": 6000}),
}


class SearchTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.index = TempIndex(cls._tmp.name)
        cls.ids = {}
        for name, (fields, tree, mtimes) in FIXTURE.items():
            root = os.path.join(cls._tmp.name, name)
            make_tree(root, tree, size=10)
            settle(root)
            for rel, offset in mtimes.items():
                set_mtime(os.path.join(root, rel), SETTLED_MTIME + offset)
            sid = cls.index.add_source(root, key=f"test:{name}", kind=fields["kind"],
                                       host=fields["host"], display_name=fields["display_name"])
            cls.index.deep(sid, root)
            cls.ids[name] = sid
        cls.reader = db.connect(cls.index.path)

    @classmethod
    def tearDownClass(cls):
        cls.reader.close()
        cls.index.close()
        cls._tmp.cleanup()

    def registry(self, offline=(), missing=(), excluded=()):
        out = {}
        for name, (fields, _tree, _mtimes) in FIXTURE.items():
            if name in missing:
                continue
            sid = self.ids[name]
            out[sid] = {"id": sid, "key": f"test:{name}", "mode": "auto", "manual": False,
                        "hotplug": False, "volume_size": None, "last_drive": None,
                        "last_seen": 1_790_000_000.0, **fields,
                        "online": name not in offline, "included": name not in excluded}
        return out

    def search(self, query, registry=None, **kwargs):
        result = search.search(self.reader, registry or self.registry(), query, **kwargs)
        self.assertFalse(self.reader.in_transaction)
        return result

    @staticmethod
    def names(result):
        return [item["name"] for item in result["results"]]


class AcceptanceQueryTests(SearchTestCase):
    """SPEC §7.2 queries on a tree shaped like a production company's project folders."""

    def test_lindholm_finds_the_project_first(self):
        for query in ("lindholm", "rikke lindholm", "Rikke Lindholm", "LINDHOLM"):
            first = self.search(query)["results"][0]
            self.assertEqual((first["name"], first["kind"]), ("Rikke Lindholm", "project"), query)
            self.assertEqual(first["source"]["name"], "Kunder 2026 (STUDIO)")

    def test_lindholm_klip_finds_the_klip_folder_first(self):
        result = self.search("lindholm klip")
        first = result["results"][0]
        self.assertEqual((first["rel_path"], first["kind"]), ("Rikke Lindholm\\Klip", "dir"))
        self.assertNotIn("Rikke Lindholm", self.names(result))       # 'klip' not on its path
        self.assertEqual(first["hl"], [[0, 4]])

    def test_klar_tand_silkeborg(self):
        top = self.search("klar tand silkeborg")["results"][:3]
        self.assertEqual((top[0]["name"], top[0]["source"]["host"]),
                         ("Klar Tand - Silkeborg", "GRAFIK-PC"))
        self.assertEqual({t["name"] for t in top[1:]},
                         {"Klar Tand - Silkeborg C", "Klar Tand - Voxpop Silkeborg"})

    def test_pixelbro(self):
        results = self.search("pixelbro")["results"]
        self.assertEqual([r["name"] for r in results[:2]], ["Pixelbro", "Pixelbro Radio"])
        self.assertEqual(results[0]["source"]["name"], "Forår 2026 RØD")
        self.assertEqual(results[2]["name"], "Pixelbro final.mp4")

    def test_bogely_with_and_without_diacritics(self):
        for query in ("bøgely", "bogely", "BØGELY"):
            # equal scores are ordered by newest mtime, then name
            self.assertEqual(self.names(self.search(query))[:4],
                             ["Bøgely Jul 2024", "Bøgely Jul 2025", "Bøgely Festival 2025",
                              "Hotel Bøgelyhus"], query)

    def test_source_name_counts_as_path(self):
        result = self.search("forar pixelbro")
        self.assertEqual(self.names(result)[0], "Pixelbro")
        self.assertNotIn("Pixelbro Radio", self.names(result))
        self.assertEqual(self.search("forar")["total"], 0)       # never matched by source alone

    def test_file_by_camera_name(self):
        first = self.search("FX9_7912")["results"][0]
        self.assertEqual((first["name"], first["kind"], first["ext"]),
                         ("FX9_7912.MXF", "file", "mxf"))
        self.assertEqual(first["project"], {
            "name": "Rikke Lindholm", "rel_path": "Rikke Lindholm",
            "path": "C:\\Kunder 2026 (STUDIO)\\Rikke Lindholm",
            "unc_path": "\\\\STUDIO-PC\\Kunder 2026 (STUDIO)\\Rikke Lindholm"})

    def test_templates_are_hidden_unless_asked_for(self):
        result = self.search("kundenavn")
        self.assertEqual((result["total"], result["results"]), (0, []))
        self.assertNotIn("hidden", result)
        shown = self.search("kundenavn", include_templates=True)["results"]
        self.assertEqual({(r["name"], r["kind"]) for r in shown}, {("1. KUNDENAVN", "template")})
        klip = self.search("klip")["results"]
        self.assertFalse([r for r in klip if r["rel_path"].startswith("1. KUNDENAVN")])
        self.assertIn("Rikke Lindholm\\Klip", [r["rel_path"] for r in klip])
        klip_all = self.search("klip", include_templates=True)["results"]
        self.assertIn("1. KUNDENAVN\\Klip", [r["rel_path"] for r in klip_all])


class ScoreTests(SearchTestCase):
    """SPEC §7 ranking arithmetic (fixture mtimes are years old: no recency bonus)."""

    def score(self, query, rel):
        return next(r["score"] for r in self.search(query)["results"] if r["rel_path"] == rel)

    def test_score_components(self):
        # project 1000 + all tokens 300 + name == query 250 + starts with first token 100
        # + 2 word starts 80 + online 200 - 4 x depth 1
        self.assertEqual(self.score("rikke lindholm", "Rikke Lindholm"), 1926.0)
        # project 1000 + all tokens 300 + 1 word start 40 + online 200 - 4
        self.assertEqual(self.score("lindholm", "Rikke Lindholm"), 1536.0)
        # dir 400 + word start 40 - lindholm only via the path 120 + online 200 - 4 x depth 2
        self.assertEqual(self.score("lindholm klip", "Rikke Lindholm\\Klip"), 512.0)
        offline = self.registry(offline={"studio"})
        result = search.search(self.reader, offline, "lindholm")
        self.assertEqual(result["results"][0]["score"], 1336.0)

    def test_recency_bonus_tiers(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        index = TempIndex(tmp.name)
        self.addCleanup(index.close)
        root = os.path.join(tmp.name, "root")
        ages = {"Recent A": 10, "Recent B": 100, "Recent C": 300, "Recent D": 500}
        make_tree(root, [f"{name}\\Klip\\" for name in ages])
        now = time.time()
        for name, days in ages.items():
            settle(os.path.join(root, name), now - days * 86_400)
        sid = index.add_source(root)
        index.deep(sid, root)
        reader = db.connect(index.path)
        self.addCleanup(reader.close)
        registry = {sid: {"id": sid, "display_name": "root", "kind": "local", "path": root,
                          "online": True, "included": True}}
        scores = {r["name"]: r["score"]
                  for r in search.search(reader, registry, "recent")["results"]}
        base = scores["Recent D"]
        self.assertEqual({n: s - base for n, s in scores.items()},
                         {"Recent A": 80, "Recent B": 40, "Recent C": 15, "Recent D": 0})


class FilterTests(SearchTestCase):
    def test_kind_filters(self):
        files = self.search("lindholm", kind="file")["results"]
        self.assertTrue(files)
        self.assertEqual({r["kind"] for r in files}, {"file"})
        self.assertIn("Rikke Lindholm - Testimonial.mp4", [r["name"] for r in files])
        projects = self.search("lindholm", kind="project")["results"]
        self.assertEqual([r["name"] for r in projects], ["Rikke Lindholm"])
        dirs = self.search("tand", kind="dir")["results"]
        self.assertEqual({r["kind"] for r in dirs}, {"group", "project"})

    def test_hidden_counts_when_filters_hide_everything(self):
        result = self.search("pixelbro radio", kind="file")
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["hidden"], hidden(1, 0, 0, 1))
        result = self.search("pixelbro", source_id=self.ids["studio"])
        self.assertEqual(result["hidden"], hidden(0, 0, 3, 3))
        self.assertEqual(self.search("zzzq", kind="file")["hidden"], hidden(0, 0, 0, 0))
        self.assertNotIn("hidden", self.search("zzzq"))

    def test_hidden_counts_are_what_each_one_click_fix_reveals(self):
        # IDX-7 / XMC-4: a match hidden by two filters is counted by neither of them.
        studio, forar = self.ids["studio"], self.ids["forar"]
        result = self.search("pixelbro radio", kind="file", source_id=studio)
        self.assertEqual(result["hidden"], hidden(0, 0, 0, 1))
        # "lindholm" matches the project and the Testimonial file, both on STUDIO
        result = self.search("lindholm", kind="file", source_id=forar)
        self.assertEqual(result["hidden"], hidden(0, 0, 1, 2))
        self.assertEqual(self.search("lindholm", kind="file")["total"], 1)   # the fix: 1 result
        result = self.search("lindholm", kind="project", source_id=forar)
        self.assertEqual(result["hidden"], hidden(0, 0, 1, 2))
        registry = self.registry(offline={"studio"})
        result = self.search("lindholm", registry, kind="file", online_only=True)
        self.assertEqual(result["hidden"], hidden(0, 1, 0, 2))
        self.assertEqual(self.search("lindholm", registry, kind="file")["total"], 1)
        self.assertEqual(self.search("lindholm", registry, online_only=True)["total"], 0)

    def test_offline_sources(self):
        registry = self.registry(offline={"solv"})
        results = self.search("pixelbro", registry)["results"]
        radio = next(r for r in results if r["name"] == "Pixelbro Radio")
        self.assertFalse(radio["source"]["online"])
        self.assertLess(radio["score"], results[0]["score"])
        self.assertNotIn("Pixelbro Radio",
                         self.names(self.search("pixelbro", registry, online_only=True)))
        result = self.search("pixelbro radio", registry, online_only=True)
        self.assertEqual((result["total"], result["hidden"]), (0, hidden(0, 1, 0, 1)))

    def test_source_filter(self):
        result = self.search("lindholm", source_id=self.ids["studio"])
        self.assertTrue(result["results"])
        self.assertEqual({r["source"]["id"] for r in result["results"]}, {self.ids["studio"]})

    def test_unknown_or_excluded_sources_are_never_returned(self):
        for registry in (self.registry(missing={"solv"}), self.registry(excluded={"solv"})):
            self.assertNotIn("Pixelbro Radio", self.names(self.search("pixelbro", registry)))

    def test_invalid_kind(self):
        with self.assertRaises(ValueError):
            self.search("lindholm", kind="alt")


class RetrievalTests(SearchTestCase):
    def test_short_tokens_use_a_substring_scan(self):
        self.assertIn("Rikke Lindholm", self.names(self.search("li")))
        self.assertEqual(self.names(self.search("fx 79"))[:2], ["FX9_7912.MXF", "FX9_7913.MXF"])

    def test_truncation_keeps_directories(self):
        with mock.patch.object(search, "CANDIDATE_CAP", 1):
            result = self.search("lindholm")
        self.assertTrue(result["truncated"])
        self.assertEqual(self.names(result), ["Rikke Lindholm"])
        self.assertFalse(self.search("lindholm")["truncated"])

    def test_descendants_via_fts_when_subtrees_are_huge(self):
        with mock.patch.object(search, "EXPANSION_ROW_BUDGET", 0):
            first = self.search("lindholm klip")["results"][0]
        self.assertEqual(first["rel_path"], "Rikke Lindholm\\Klip")

    def test_empty_query(self):
        result = self.search("  ")
        self.assertEqual((result["tokens"], result["total"], result["results"]), ([], 0, []))

    def test_response_shape(self):
        result = self.search("Rikke  LINDHOLM", limit=1)
        self.assertEqual(set(result), {"query", "tokens", "took_ms", "total", "truncated",
                                       "results"})
        self.assertEqual(result["tokens"], ["rikke", "lindholm"])
        self.assertEqual(self.names(result), ["Rikke Lindholm"])
        self.assertEqual(result["total"], 2)                 # + "Rikke Lindholm - Testimonial.mp4"
        self.assertIsInstance(result["took_ms"], float)


class ItemTests(SearchTestCase):
    def test_item_shape_and_paths(self):
        item = self.search("lindholm")["results"][0]
        self.assertEqual(set(item), {"id", "kind", "name", "hl", "path", "open_path", "unc_path",
                                     "rel_path", "parent", "depth", "source", "project", "size",
                                     "mtime", "file_count", "ext", "is_seq", "seq_count",
                                     "subfolders", "score"})
        self.assertEqual(item["hl"], [[6, 14]])
        self.assertEqual(item["path"], "C:\\Kunder 2026 (STUDIO)\\Rikke Lindholm")
        self.assertEqual(item["open_path"], item["path"])
        self.assertEqual(item["unc_path"], "\\\\STUDIO-PC\\Kunder 2026 (STUDIO)\\Rikke Lindholm")
        self.assertEqual((item["parent"], item["depth"]), ("", 1))
        self.assertEqual(item["project"]["rel_path"], "Rikke Lindholm")
        self.assertEqual(item["subfolders"], TEMPLATE)
        self.assertEqual(item["source"], {
            "id": self.ids["studio"], "name": "Kunder 2026 (STUDIO)", "host": "STUDIO-PC",
            "kind": "local", "online": True, "drive": "C:", "disk_name": "Lokal disk",
            "volume_label": "Lokal disk", "last_seen": 1_790_000_000.0, "is_system": False,
            "volume_present": True})
        self.assertIsInstance(item["score"], float)
        registry = self.registry()
        registry[self.ids["studio"]].update(is_system=True, volume_label="")
        item = self.search("lindholm", registry)["results"][0]
        self.assertEqual((item["source"]["is_system"], item["source"]["disk_name"]),
                         (True, "Systemdisk"))

    def test_share_items_and_files(self):
        item = self.search("klar tand silkeborg")["results"][0]
        self.assertEqual(item["path"], "\\\\GRAFIK-PC\\Kunder 2026 (Grafik)\\Klar Tand 2026"
                                       "\\Klar Tand - Silkeborg")
        self.assertEqual(item["unc_path"], item["path"])
        self.assertEqual((item["source"]["drive"], item["source"]["disk_name"]), (None, None))
        file_item = self.search("testimonial")["results"][0]
        self.assertIsNone(file_item["subfolders"])
        self.assertEqual(file_item["parent"], "Rikke Lindholm\\Final")
        root_item = self.search("pixelbro")["results"][0]
        self.assertEqual(root_item["path"], "D:\\Pixelbro")
        no_unc = self.search("pixelbro radio")["results"][0]
        self.assertIsNone(no_unc["unc_path"])
        self.assertIsNone(no_unc["project"]["unc_path"])

    def test_sequence_opens_its_first_frame(self):
        item = self.search("render")["results"][0]
        self.assertTrue(item["is_seq"])
        self.assertEqual(item["seq_count"], 25)
        self.assertTrue(item["path"].endswith("\\Grafik\\render_[0001-0025].exr"))
        self.assertEqual(item["open_path"],
                         "C:\\Kunder 2026 (STUDIO)\\Rikke Lindholm\\Grafik\\render_0001.exr")

    def test_make_item_from_a_plain_mapping(self):
        row = {"id": 1, "source_id": 9, "rel_path": "a\\b.txt", "parent_rel": "a", "name": "b.txt",
               "kind": 0, "depth": 2, "ext": "txt", "size": 3, "mtime": 1.0, "file_count": None,
               "is_seq": 0, "seq_count": None, "project_rel": None}
        item = search.make_item(row, {"id": 9, "display_name": "X", "kind": "share",
                                      "path": "\\\\HOST\\X", "online": False})
        self.assertEqual((item["path"], item["unc_path"], item["project"], item["hl"]),
                         ("\\\\HOST\\X\\a\\b.txt", None, None, []))
        self.assertIsNone(item["score"])

    def test_source_ref_helpers(self):
        unlabeled = {"kind": "local", "volume_label": " ", "volume_size": 2_000_398_934_016,
                     "last_drive": "H:", "path": "H:\\x"}
        self.assertEqual(search.disk_name(unlabeled), "disk uden navn (2 TB, sidst som H:)")
        self.assertEqual(search.disk_name({"kind": "local", "path": "\\\\?\\Volume{x}"}),
                         "disk uden navn")
        self.assertIsNone(search.disk_name({"kind": "share", "volume_label": "x"}))
        # SPEC §15.4: an unlabeled system volume is the "Systemdisk"; a label still wins.
        system = {"kind": "local", "volume_label": None, "is_system": True,
                  "volume_size": 2_000_398_934_016, "last_drive": "C:", "path": "C:\\VFX"}
        self.assertEqual(search.disk_name(system), "Systemdisk")
        self.assertEqual(search.disk_name({**system, "volume_label": "Windows"}), "Windows")
        self.assertEqual(search.source_ref(system)["is_system"], True)
        self.assertEqual(search.source_ref(unlabeled)["is_system"], False)
        # SPEC §15.12: the registry's volume_present; without it, present when online
        self.assertEqual(search.source_ref({**system, "online": False,
                                            "volume_present": True})["volume_present"], True)
        self.assertEqual(search.source_ref({**system, "online": True})["volume_present"], True)
        self.assertEqual(search.source_ref(system)["volume_present"], False)
        self.assertEqual(search.format_size(1_500_000_000_000), "1,5 TB")
        self.assertEqual(search.format_size(500_000_000_000), "500 GB")
        self.assertEqual(search.drive_of("h:\\x"), "H:")
        self.assertIsNone(search.drive_of("\\\\HOST\\share"))


class RecentAndChildrenTests(SearchTestCase):
    def test_recent_projects_newest_first(self):
        recent = search.recent_projects(self.reader, self.registry(), limit=4)
        self.assertEqual([r["name"] for r in recent],
                         ["Bøgely Jul 2024", "Bøgely Jul 2025", "Rikke Lindholm",
                          "Hotel Bøgelyhus"])
        self.assertEqual({(r["kind"], r["score"]) for r in recent}, {("project", None)})
        self.assertEqual(recent[2]["hl"], [])
        self.assertEqual(recent[2]["subfolders"], TEMPLATE)
        online = search.recent_projects(self.reader, self.registry(offline={"solv"}), limit=1,
                                        online_only=True)
        self.assertEqual([r["name"] for r in online], ["Bøgely Jul 2025"])
        self.assertFalse(self.reader.in_transaction)

    def test_children_lists_folders_first(self):
        items = search.children(self.reader, self.registry(), self.ids["studio"], "Rikke Lindholm")
        self.assertEqual([i["name"] for i in items], TEMPLATE)
        grafik = search.children(self.reader, self.registry(), self.ids["studio"],
                                 "Rikke Lindholm/Grafik/")
        self.assertEqual([i["name"] for i in grafik], ["render_[0001-0025].exr"])
        root = search.children(self.reader, self.registry(), self.ids["studio"], "")
        names = [i["name"] for i in root]
        self.assertEqual(names[-1], "PROMPT - Subtitles.txt")
        self.assertIn("1. KUNDENAVN", names)
        self.assertEqual(search.children(self.reader, self.registry(), 999, ""), [])


class OwnIndexTestCase(unittest.TestCase):
    """A small index of its own: ``TREES`` = {name: tree}, all settled."""

    TREES: dict[str, list[str]] = {}

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.index = TempIndex(cls._tmp.name)
        cls.roots, cls.ids = {}, {}
        for name, tree in cls.TREES.items():
            root = os.path.join(cls._tmp.name, name)
            make_tree(root, tree, size=10)
            settle(root)
            cls.roots[name] = root
            cls.ids[name] = cls.index.add_source(root, key=f"test:{name}")
        cls.prepare()
        for name in cls.TREES:
            cls.index.deep(cls.ids[name], cls.roots[name])
        cls.reader = db.connect(cls.index.path)

    @classmethod
    def prepare(cls):
        """Adjust the trees before they are scanned."""

    @classmethod
    def tearDownClass(cls):
        cls.reader.close()
        cls.index.close()
        cls._tmp.cleanup()

    def search(self, query, registry=None, **kwargs):
        result = search.search(self.reader, registry or self.registry(), query, **kwargs)
        self.assertFalse(self.reader.in_transaction)
        return result

    @staticmethod
    def names(result):
        return [item["name"] for item in result["results"]]


ARKIV = [f"Infomoede {year}.mp4" for year in range(2000, 2020)]   # FTS-heavy: range expansion


class AltSpellingTests(OwnIndexTestCase):
    """SRCH-1 / SPEC §15.2: the ASCII spelling "oe" of "ø" matches in both directions."""

    TREES = {"grafik": project("Infomøde Skolen Kolding")
             + ["Infomøde Skolen Kolding\\Final\\Infomoede Skolen Kolding - final.mp4",
                "Sommerfest Kildedal\\Final\\05b Koeb billet sommerfestkildedal.dk.webm",
                "Voxpop Aarhus\\Final\\Infomoede Aarhus.mp4", "Videoeksport\\Logoeffekt.mov"]
             + [f"Arkiv\\{name}" for name in ARKIV]
             + project("Boegely Havn 2023") + project("Bøgely Jul 2024")
             + project("Møbler Malte")}

    def registry(self, display_name="Kunder 2026 (Grafik)"):
        sid = self.ids["grafik"]
        return {sid: {"id": sid, "kind": "share", "host": "GRAFIK-PC",
                      "display_name": display_name, "volume_label": None,
                      "path": "\\\\GRAFIK-PC\\Kunder 2026 (Grafik)", "online": True,
                      "included": True}}

    def test_danish_query_finds_the_oe_spelling(self):
        result = self.search("infomøde")
        self.assertEqual(set(self.names(result)),
                         {"Infomøde Skolen Kolding", "Infomoede Skolen Kolding - final.mp4",
                          "Infomoede Aarhus.mp4", *ARKIV})
        hl = {r["name"]: r["hl"] for r in result["results"]}
        self.assertEqual(hl["Infomoede Aarhus.mp4"], [[0, 9]])
        self.assertEqual(self.names(self.search("køb billet")),
                         ["05b Koeb billet sommerfestkildedal.dk.webm"])
        self.assertEqual(self.names(self.search("møbler")), ["Møbler Malte"])

    def test_oe_query_finds_the_danish_spelling(self):
        self.assertEqual(set(self.names(self.search("infomoede"))),
                         set(self.names(self.search("infomøde"))))
        found = self.search("infomoede skolen")["results"]
        self.assertEqual(found[0]["name"], "Infomøde Skolen Kolding")
        self.assertEqual(found[0]["hl"], [[0, 8], [9, 15]])
        self.assertEqual(self.names(self.search("moebler")), ["Møbler Malte"])
        self.assertEqual(self.names(self.search("koeb")),
                         ["05b Koeb billet sommerfestkildedal.dk.webm"])

    def test_the_real_spelling_ranks_first(self):
        # SPEC §15.12 (R2-IDX-5): both spellings still match, but a name found only through
        # the other spelling earns no prefix/word-start bonus and loses 60.
        # project 1000 - 4 + all tokens 300 + starts with 100 + word start 40 + online 200
        literal = 1636.0
        other = literal - 100 - 40 - 60
        for query in ("bøgely", "bogely", "BØGELY"):
            top = [(r["name"], r["score"]) for r in self.search(query)["results"][:2]]
            self.assertEqual(top, [("Bøgely Jul 2024", literal), ("Boegely Havn 2023", other)],
                             query)
        for query in ("boegely", "BOEGELY"):
            top = [(r["name"], r["score"]) for r in self.search(query)["results"][:2]]
            self.assertEqual(top, [("Boegely Havn 2023", literal), ("Bøgely Jul 2024", other)],
                             query)
        # A token found through its "oe" spelling in the path costs the same.
        first = self.search("boegely klip")["results"][:2]
        self.assertEqual([(r["rel_path"], r["score"]) for r in first],
                         [("Boegely Havn 2023\\Klip", 512.0),
                          ("Bøgely Jul 2024\\Klip", 512.0 - 60)])

    def test_compounds_still_match_as_substrings(self):
        self.assertEqual(self.names(self.search("eksport")), ["Videoeksport"])
        self.assertEqual(self.names(self.search("effekt")), ["Logoeffekt.mov"])

    def test_descendants_and_source_names_use_both_spellings(self):
        for budget in (search.EXPANSION_ROW_BUDGET, 0):          # range scan, then FTS
            with mock.patch.object(search, "EXPANSION_ROW_BUDGET", budget):
                self.assertEqual(self.names(self.search("voxpop infomøde")),
                                 ["Infomoede Aarhus.mp4"], budget)
        registry = self.registry(display_name="Bøgely Arkiv")
        self.assertEqual(self.names(self.search("boegely videoeksport", registry)),
                         ["Videoeksport"])

    def test_a_too_short_oe_spelling_is_not_used(self):
        # SPEC §15.12 (R2-IDX-5): "koe" ~ "ko" would match almost everything ("Kolding"), so
        # "koe" matches literally only - and is found by FTS like any 3-letter token.
        self.assertIsNone(search.token_alt("koe"))
        self.assertEqual(search.token_alt("koeb"), "kob")
        self.assertEqual(search.token_alt("bogely"), "bogely")
        with mock.patch.object(search, "_fetch_substring",
                               side_effect=AssertionError("sequential scan")):
            self.assertEqual(self.names(self.search("koe")),
                             ["05b Koeb billet sommerfestkildedal.dk.webm"])
            self.assertEqual(self.names(self.search("koe billet")),
                             ["05b Koeb billet sommerfestkildedal.dk.webm"])
        self.assertIn("Infomøde Skolen Kolding", self.names(self.search("ko")))   # a plain "ko"


RECENT = ["Aftenshowet", "Showreel 2026", "Jobmessen", "Johan Show", "Bøgely Festival 2025"]


class AltRankingTests(OwnIndexTestCase):
    """R2-IDX-5 / SPEC §15.12: a real "oe" in a query is not drowned by its "o" spelling."""

    TREES = {"kunder": project("Joe Nygaard", "Joe Nygaard\\Klip\\Johan.mov")
             + project("Production Dummy Shoemaker") + project("Hotel Bøgelyhus")
             + [f"{name}\\Klip\\" for name in RECENT] + [f"{name}\\Final\\" for name in RECENT]}

    @classmethod
    def prepare(cls):
        week_ago = time.time() - 7 * 86_400                 # newer: +80 recency bonus
        for name in RECENT:
            settle(os.path.join(cls.roots["kunder"], name), week_ago)
        settle(os.path.join(cls.roots["kunder"], "Bøgely Festival 2025"), week_ago + 60)

    def registry(self):
        sid = self.ids["kunder"]
        return {sid: {"id": sid, "kind": "local", "host": "PC", "display_name": "Kunder",
                      "volume_label": None, "path": "D:\\Kunder", "online": True,
                      "included": True}}

    def scores(self, query):
        return [(r["name"], r["score"]) for r in self.search(query)["results"]]

    def test_short_oe_tokens_match_literally(self):
        # "joe" ~ "jo" matched every "jo…" name with the same bonuses (Jobmessen outranked Joe)
        self.assertEqual(self.names(self.search("joe")), ["Joe Nygaard"])
        self.assertEqual(self.names(self.search("zoe")), [])
        found = self.search("joe mov")["results"]           # "joe" via the path only
        self.assertEqual([(r["name"], r["hl"]) for r in found], [("Johan.mov", [[6, 9]])])

    def test_the_real_oe_spelling_ranks_first(self):
        # project 1000 - 4 + all tokens 300 + word start 40 + online 200 (old: no recency)
        shoemaker = 1536.0
        # the same, found only through "sho": no word-start bonus, -60, recent: +80
        self.assertEqual(self.scores("shoe"), [
            ("Production Dummy Shoemaker", shoemaker), ("Aftenshowet", 1516.0),
            ("Johan Show", 1516.0), ("Showreel 2026", 1516.0)])

    def test_the_other_spelling_is_still_found_when_it_is_the_only_one(self):
        # No "boegely" is written with "oe" here: both Bøgely names match, without bonuses.
        self.assertEqual(self.scores("boegely"), [("Bøgely Festival 2025", 1516.0),
                                                  ("Hotel Bøgelyhus", 1436.0)])
        self.assertEqual(self.search("boegely")["results"][0]["hl"], [[0, 6]])


SHARE = "\\\\GRAFIK-PC\\Klar Tand - Silkeborg"


class RootProjectTests(OwnIndexTestCase):
    """IDX-2 / SPEC §15.3: a source whose root folder is itself a project."""

    TREES = {"silkeborg": ["Klip\\x.mxf", "Final\\Klar Tand final.mp4", "Grafik\\",
                           "Arkiv\\Sub Projekt\\Klip\\s.mxf", "Arkiv\\Sub Projekt\\Final\\"],
             "studio": project("Rikke Lindholm") + project("Pixelbro Silkeborg"),
             "template": [f"{d}\\" for d in TEMPLATE]}

    @classmethod
    def prepare(cls):
        set_mtime(os.path.join(cls.roots["silkeborg"], "Final", "Klar Tand final.mp4"),
                  SETTLED_MTIME + 7000)
        settle(os.path.join(cls.roots["studio"], "Rikke Lindholm"), SETTLED_MTIME + 5000)

    def registry(self, offline=(), flag=True):
        h, p, t = self.ids["silkeborg"], self.ids["studio"], self.ids["template"]
        counts = db.source_counts(self.reader, h)
        common = {"volume_label": None, "included": True, "last_seen": 1_790_000_000.0,
                  "last_scan_end": 1_790_000_000.0}
        return {
            h: {**common, "id": h, "kind": "share", "host": "GRAFIK-PC",
                "display_name": "Klar Tand - Silkeborg", "path": SHARE, "unc_path": SHARE,
                "online": "silkeborg" not in offline, "root_is_project": flag,
                "total_size": counts["total_size"], "file_count": counts["file_count"]},
            p: {**common, "id": p, "kind": "local", "host": "STUDIO-PC",
                "display_name": "Kunder 2026 (STUDIO)", "path": "C:\\Kunder 2026 (STUDIO)",
                "unc_path": None, "online": "studio" not in offline, "root_is_project": False},
            t: {**common, "id": t, "kind": "local", "host": "STUDIO-PC",
                "display_name": "1. KUNDENAVN", "path": "C:\\Skabeloner\\1. KUNDENAVN",
                "unc_path": None, "online": True, "root_is_project": True},
        }

    def root_ref(self):
        return {"name": "Klar Tand - Silkeborg", "rel_path": "", "path": SHARE, "unc_path": SHARE}

    def test_the_root_project_is_found_by_its_name(self):
        result = self.search("silkeborg")
        root = result["results"][0]
        registry = self.registry()[self.ids["silkeborg"]]
        self.assertEqual(root, {
            "id": -self.ids["silkeborg"], "kind": "project", "name": "Klar Tand - Silkeborg",
            "hl": [[12, 21]], "path": SHARE, "open_path": SHARE, "unc_path": SHARE,
            "rel_path": "", "parent": "", "depth": 0,
            "source": search.source_ref(registry), "project": self.root_ref(),
            "size": registry["total_size"], "mtime": SETTLED_MTIME + 7000,
            "file_count": registry["file_count"], "ext": None, "is_seq": False,
            "seq_count": None, "subfolders": ["Arkiv", "Final", "Grafik", "Klip"],
            "score": 1540.0})
        self.assertEqual(self.names(result)[1], "Pixelbro Silkeborg")      # depth 1: -4
        first = self.search("klar tand silkeborg")["results"][0]
        # 1000 + 300 all tokens + 250 name == query + 100 first token + 3 x 40 + 200 online
        self.assertEqual((first["id"], first["score"]), (-self.ids["silkeborg"], 1970.0))

    def test_entries_get_the_root_as_their_project(self):
        first = self.search("silkeborg klip")["results"][0]
        self.assertEqual((first["rel_path"], first["kind"]), ("Klip", "dir"))   # not toplevel
        self.assertEqual(first["project"], self.root_ref())
        inner = next(r for r in self.search("klip")["results"]
                     if r["rel_path"] == "Arkiv\\Sub Projekt\\Klip")
        self.assertEqual(inner["project"]["rel_path"], "Arkiv\\Sub Projekt")
        movie = self.search("klar tand final")["results"][0]
        self.assertEqual((movie["name"], movie["project"]),
                         ("Klar Tand final.mp4", self.root_ref()))
        kids = search.children(self.reader, self.registry(), self.ids["silkeborg"], "")
        self.assertEqual([(k["name"], k["kind"]) for k in kids],
                         [("Arkiv", "group"), ("Final", "dir"), ("Grafik", "dir"),
                          ("Klip", "dir")])
        self.assertEqual({str(k["project"]) for k in kids}, {str(self.root_ref())})

    def test_filters_and_hidden_counts_cover_the_root(self):
        self.assertEqual(self.search("silkeborg", kind="file")["hidden"],
                         hidden(2, 0, 0, 2))
        self.assertIn(-self.ids["silkeborg"],
                      [r["id"] for r in self.search("silkeborg", kind="project")["results"]])
        self.assertEqual(self.search("klar tand", source_id=self.ids["studio"])["hidden"],
                         hidden(0, 0, 2, 2))
        registry = self.registry(offline={"silkeborg"})
        result = self.search("klar tand silkeborg", registry, online_only=True)
        self.assertEqual(result["hidden"], hidden(0, 2, 0, 2))
        self.assertFalse(self.search("klar tand silkeborg", registry)["results"][0]["source"]
                         ["online"])

    def test_recent_projects_include_the_root(self):
        recent = search.recent_projects(self.reader, self.registry(), limit=10)
        self.assertEqual([r["name"] for r in recent[:2]],
                         ["Klar Tand - Silkeborg", "Rikke Lindholm"])
        self.assertEqual((recent[0]["id"], recent[0]["kind"], recent[0]["hl"], recent[0]["score"]),
                         (-self.ids["silkeborg"], "project", [], None))
        self.assertEqual(sorted(r["name"] for r in recent[2:]),
                         ["Pixelbro Silkeborg", "Sub Projekt"])
        online = search.recent_projects(self.reader, self.registry(offline={"silkeborg"}),
                                        online_only=True)
        self.assertEqual([r["name"] for r in online], ["Rikke Lindholm", "Pixelbro Silkeborg"])
        self.assertEqual([r["name"] for r in search.recent_projects(
            self.reader, self.registry(), limit=1)], ["Klar Tand - Silkeborg"])

    def test_without_the_flag_there_is_no_root_item(self):
        registry = self.registry(flag=False)
        self.assertNotIn("", [r["rel_path"] for r in self.search("silkeborg", registry)["results"]])
        kids = search.children(self.reader, registry, self.ids["silkeborg"], "")
        self.assertEqual([k["project"] for k in kids], [None] * 4)
        self.assertEqual(search.recent_projects(self.reader, registry, limit=1)[0]["name"],
                         "Rikke Lindholm")

    def test_a_template_root_is_hidden_unless_asked_for(self):
        self.assertEqual(self.search("kundenavn")["total"], 0)
        shown = self.search("kundenavn", include_templates=True)["results"]
        self.assertEqual([(r["name"], r["kind"], r["rel_path"], r["project"]) for r in shown],
                         [("1. KUNDENAVN", "template", "", None)])
        klip = [r["source"]["id"] for r in self.search("klip")["results"]]
        self.assertNotIn(self.ids["template"], klip)
        klip_all = [r["source"]["id"] for r in self.search("klip", include_templates=True)
                    ["results"]]
        self.assertIn(self.ids["template"], klip_all)
        recent = search.recent_projects(self.reader, self.registry(), limit=10)
        self.assertNotIn("1. KUNDENAVN", [r["name"] for r in recent])


if __name__ == "__main__":
    unittest.main()
