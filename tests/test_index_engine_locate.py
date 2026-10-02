"""Index engine: locate, map_paths, suggest_project_folders and query delegation (SPEC §8, §9).

DaVinci Resolve reports local media as UNC paths of this computer
(``\\\\studio-pc\\<share>\\…``); they must resolve to the local sources.
"""

import os

from projektsog import indexer
from tests._index_engine_fixtures import EngineTestCase, forbid_fs_calls, module_env, project

_env = None
SHARE = "Kunder 2026 (STUDIO)"


def setUpModule():
    global _env
    _env = module_env()


def tearDownModule():
    _env.cleanup()


class LocateTest(EngineTestCase):
    def setUp(self):
        super().setUp()
        self.root = os.path.join(self.tmp, "c")
        self.kunder = os.path.join(self.root, SHARE)
        tree = (project(f"{SHARE}\\Rikke Lindholm", "Klip\\FX9\\FX9_7912.MXF",
                        "Klip\\FX9\\FX9_7913.MXF", "Klip\\A7\\C0001.MP4")
                + project(f"{SHARE}\\Klar Tand 2026\\Klar Tand - Silkeborg", "Klip\\kt.mov")
                + project(f"{SHARE}\\1. KUNDENAVN\\Rikke Lindholm kopi")
                + [f"{SHARE}\\Sound Effects\\boom.wav", "Github\\undertekster\\x.srt"])
        self.world.hostname = "STUDIO-PC"
        self.world.volume("c", "1C4F9D02", drive="C:", tree=tree)
        self.world.shares = [{"name": SHARE, "path": self.kunder}]
        self.world.ips["STUDIO-PC"] = ["192.0.2.99"]
        self.ix = self.start()
        self.settled(self.ix)
        self.unc = f"\\\\STUDIO-PC\\{SHARE}"

    def test_resolve_style_paths_group_by_project(self):
        paths = [
            f"\\\\studio-pc\\{SHARE}\\Rikke Lindholm\\Klip\\FX9\\FX9_7912.MXF",
            f"\\\\studio-pc\\{SHARE}\\Rikke Lindholm\\Klip\\FX9\\FX9_7913.MXF",
            f"\\\\192.0.2.99\\{SHARE}\\Rikke Lindholm\\Klip\\A7\\C0001.MP4",
            f"\\\\?\\UNC\\STUDIO-PC\\{SHARE}\\Klar Tand 2026\\Klar Tand - Silkeborg\\Klip\\kt.mov",
            os.path.join(self.kunder, "Sound Effects", "boom.wav"),
            os.path.join(self.root, "Github", "undertekster", "x.srt"),
            "Q:\\Ukendt\\klip.mov",
            "",
        ]
        with forbid_fs_calls():
            result = self.ix.map_paths(paths)
        self.assertEqual(result["total"], 7)
        rikke, silkeborg = result["folders"]
        self.assertEqual(rikke["project"], {
            "name": "Rikke Lindholm", "rel_path": "Rikke Lindholm",
            "path": os.path.join(self.kunder, "Rikke Lindholm"),
            "unc_path": f"{self.unc}\\Rikke Lindholm"})
        self.assertEqual((rikke["count"], rikke["online"], rikke["source"]["name"]),
                         (3, True, SHARE))
        self.assertEqual((rikke["item"]["kind"], rikke["item"]["path"]),
                         ("project", rikke["project"]["path"]))
        self.assertEqual(rikke["item"]["subfolders"], ["Grafik", "Klip", "Speak"])
        self.assertEqual((silkeborg["project"]["rel_path"], silkeborg["count"]),
                         ("Klar Tand 2026\\Klar Tand - Silkeborg", 1))
        self.assertEqual(result["other_dirs"], [                     # count desc, then path
            {"path": os.path.join(self.root, "Github", "undertekster"), "count": 1,
             "online": True},
            {"path": os.path.join(self.kunder, "Sound Effects"), "count": 1, "online": True},
            {"path": "Q:\\Ukendt", "count": 1, "online": None},
        ])

    def test_locate(self):
        spelled = f"\\\\studio-pc\\{SHARE}\\Rikke Lindholm\\Klip\\FX9\\FX9_7912.MXF"
        with forbid_fs_calls():
            loc = self.ix.locate(spelled)
            new_file = self.ix.locate(f"{self.unc.lower()}\\rikke lindholm\\klip\\ny.mov")
            root = self.ix.locate(self.kunder)
            self.assertIsNone(self.ix.locate("Q:\\Ukendt\\klip.mov"))
            self.assertIsNone(self.ix.locate(""))
        rel = "Rikke Lindholm\\Klip\\FX9\\FX9_7912.MXF"
        self.assertEqual((loc["rel_path"], loc["path"], loc["unc_path"], loc["online"]),
                         (rel, os.path.join(self.kunder, rel), f"{self.unc}\\{rel}", True))
        self.assertEqual((loc["entry"]["kind"], loc["entry"]["name"]), ("file", "FX9_7912.MXF"))
        self.assertEqual(loc["project"]["name"], "Rikke Lindholm")
        self.assertEqual(set(loc["source"]), {"id", "name", "host", "kind", "online", "drive",
                                              "disk_name", "volume_label", "last_seen",
                                              "is_system", "volume_present"})
        self.assertTrue(loc["source"]["volume_present"])
        self.assertIsNone(new_file["entry"])                    # not indexed (yet)
        self.assertEqual(new_file["project"]["rel_path"], "Rikke Lindholm")
        self.assertEqual((root["rel_path"], root["entry"], root["project"]), ("", None, None))

    def test_offline_locations_are_reported_offline(self):
        self.world.volumes.clear()
        self.rediscover(self.ix)
        result = self.ix.map_paths([f"{self.unc}\\Rikke Lindholm\\Klip\\FX9\\FX9_7912.MXF"])
        self.assertEqual([(f["project"]["name"], f["online"]) for f in result["folders"]],
                         [("Rikke Lindholm", False)])
        self.assertFalse(self.ix.locate(os.path.join(self.kunder, "Rikke Lindholm"))["online"])
        self.cfg.update({"show_offline": False})
        response = self.ix.search("lindholm")
        self.assertEqual((response["results"], response["hidden"]["offline"]), ([], 1))
        self.assertTrue(self.ix.search("lindholm", online_only=False)["results"])

    def test_suggest_project_folders(self):
        with forbid_fs_calls():
            rikke = self.ix.suggest_project_folders("Rikke Lindholm - Testimonial")
            silkeborg = self.ix.suggest_project_folders("Klar Tand Silkeborg", limit=1)
            none = self.ix.suggest_project_folders("Helt andet")
        self.assertEqual([(s["project"]["name"], s["score"]) for s in rikke],
                         [("Rikke Lindholm", 0.8)])   # the copy inside the template is hidden
        self.assertEqual((rikke[0]["online"], rikke[0]["item"]["kind"], rikke[0]["source"]["name"]),
                         (True, "project", SHARE))
        self.assertEqual([(s["project"]["name"], s["score"]) for s in silkeborg],
                         [("Klar Tand - Silkeborg", 1.0)])
        self.assertEqual(none, [])

    def test_import_helper_queries(self):
        """find_files / templates / projects_named answer from the index alone (SPEC §17)."""
        with forbid_fs_calls():
            found = self.ix.find_files(["fx9_7912.mxf", "FX9_7913.MXF", "FX9_9999.MXF"])
            templates = self.ix.templates()
            named = self.ix.projects_named(["rikke lindholm", "Ukendt"])
        rikke = os.path.join(self.kunder, "Rikke Lindholm")
        self.assertEqual(sorted(f["name"] for f in found), ["FX9_7912.MXF", "FX9_7913.MXF"])
        self.assertEqual({f["project"]["path"] for f in found}, {rikke})
        self.assertEqual(found[0]["folder"], os.path.join(rikke, "Klip", "FX9"))
        self.assertTrue(found[0]["online"])
        self.assertIsInstance(found[0]["size"], int)
        self.assertEqual([(t["path"], t["parent"]) for t in templates],
                         [(os.path.join(self.kunder, "1. KUNDENAVN"), self.kunder)])
        self.assertEqual([(p["name"], p["path"]) for p in named], [("Rikke Lindholm", rikke)])
        self.assertEqual(self.ix.find_files([]), [])

    def test_query_delegation(self):
        sid = self.source(self.ix, SHARE)["id"]
        recent = self.ix.recent_projects()
        self.assertEqual(sorted(i["name"] for i in recent),
                         ["Klar Tand - Silkeborg", "Rikke Lindholm"])
        kids = self.ix.children(sid, "Rikke Lindholm")
        self.assertEqual([k["name"] for k in kids], ["Grafik", "Klip", "Speak"])
        self.assertEqual(self.ix.search("lindholm klip")["results"][0]["rel_path"],
                         "Rikke Lindholm\\Klip")
        self.assertEqual(self.ix.search("kundenavn")["results"], [])
        self.assertTrue(self.ix.search("kundenavn", include_templates=True)["results"])
        self.cfg.update({"result_limit": 1})
        self.assertEqual(len(self.ix.search("klip")["results"]), 1)


class RootProjectTest(EngineTestCase):
    def test_a_share_that_is_itself_a_project(self):
        folder = self.world.share("NAS", "Efterår 2021", ["Klip\\a.mov", "Grafik\\b.png",
                                                            "Final\\c.mp4"])
        self.cfg.update({"hosts": ["NAS"]})
        ix = self.start()
        self.settled(ix)
        src = self.source(ix, "Efterår 2021")
        self.assertEqual((src["included"], src["auto_reason"]),
                         (True, "Mappen er selv et projekt"))
        result = ix.map_paths([os.path.join(folder, "Klip", "a.mov")])
        (entry,) = result["folders"]
        self.assertEqual(entry["project"], {"name": "Efterår 2021", "rel_path": "",
                                            "path": folder, "unc_path": folder})
        self.assertIsNone(entry["item"])
        self.assertEqual(ix.locate(os.path.join(folder, "Klip"))["project"]["rel_path"], "")


class RemoteAliasTest(EngineTestCase):
    def test_mapped_drive_and_ip_spellings_reach_the_share(self):
        folder = self.world.share("NAS", "Media", project("Rikke Lindholm", "Klip\\a.mov"))
        self.world.mapped = {"M:": "\\\\nas\\Media"}          # NAS is not in cfg["hosts"]
        self.world.ips["NAS"] = ["10.0.0.9"]
        ix = self.start()
        self.settled(ix)
        media = self.source(ix, "Media")
        self.assertEqual((media["key"], media["kind"], media["included"], media["path"]),
                         ("unc:NAS\\Media", "share", True, folder))
        self.assertFalse(ix._hosts["NAS"].enumerate)          # only the mapped share
        self.assertEqual([(h["name"], h["online"], h["shares"]) for h in ix.hosts()][1:],
                         [("NAS", True, 1)])
        result = ix.map_paths(["M:\\Rikke Lindholm\\Klip\\a.mov",
                               "\\\\10.0.0.9\\Media\\Rikke Lindholm\\Klip\\b.mov",
                               "\\\\NAS\\media\\Rikke Lindholm\\Grafik\\c.png"])
        (rikke,) = result["folders"]
        self.assertEqual((rikke["project"]["name"], rikke["count"], rikke["online"]),
                         ("Rikke Lindholm", 3, True))
        loc = ix.locate("M:\\Rikke Lindholm\\Klip\\a.mov")
        self.assertEqual((loc["path"], loc["entry"]["kind"]),
                         (os.path.join(folder, "Rikke Lindholm", "Klip", "a.mov"), "file"))


class NameSimilarityTest(EngineTestCase):
    def test_scores(self):
        score = indexer.name_similarity
        self.assertEqual(score("Rikke Lindholm - Testimonial", "Rikke Lindholm"), 0.8)
        self.assertEqual(score("Klar Tand Silkeborg", "Klar Tand - Silkeborg"), 1.0)
        self.assertEqual(score("Bøgely Jul", "Bogely jul"), 1.0)          # folded
        self.assertEqual(score("Pixelbro Radio", "Pixelbro"), 0.667)
        self.assertLess(score("Bøgely Jul 2025", "Bøgely Jul 2024"), 0.6)  # another year
        self.assertGreaterEqual(score("Lindholms testimonial", "Lindholm testimonial"), 0.9)
        self.assertEqual(score("Helt andet", "Rikke Lindholm"), 0.0)
        self.assertEqual(score("", "Rikke"), 0.0)
