"""Index engine: regression tests for the review round 2 findings (SPEC §15.12).

R2-IDX-1 hidden top-level folders registered before §15.8, R2-IDX-3 removing a computer
(+ the shares a removed root leaves behind), R2-IDX-4 folders added to a whole-volume disk,
R2-IDX-6 roots named like template sub-folders, and the Source contract additions
(``last_shallow_scan``, ``volume_present``) behind RES2-1 and UI2-4.
"""

import os
import shutil
import threading

from tests._index_engine_fixtures import EngineTestCase, make_tree, module_env, project
from tests._index_store_fixtures import FILE_ATTRIBUTE_HIDDEN, set_attributes

_env = None


def setUpModule():
    global _env
    _env = module_env()


def tearDownModule():
    _env.cleanup()


class Review2TestCase(EngineTestCase):
    def probed(self, ix, *names):
        """Wait until the named sources exist and have a probe verdict; return them."""
        def ready():
            by_name = {s["display_name"]: s for s in ix.list_sources()}
            if all(n in by_name and (by_name[n]["auto_reason"] or by_name[n]["manual"])
                   for n in names):
                return [by_name[n] for n in names]
            return None
        return self.wait_until(ready, message=f"probes of {names}")

    def by_id(self, ix, sid):
        return next(s for s in ix.list_sources() if s["id"] == sid)

    def by_name(self, ix):
        return {s["display_name"]: s for s in ix.list_sources()}

    def updates(self, sid):
        return sum(1 for e in self.events_of("index_updated") if e["source_id"] == sid)


class HiddenFolderTest(Review2TestCase):
    """R2-IDX-1: a hidden top-level folder that already is a source stays online."""

    def test_a_hidden_folder_registered_before_stays_online(self):
        disk = os.path.join(self.tmp, "disk")
        self.world.volume("disk", "41DD0001", label="Arbejdsdisk", drive="X:",
                          tree=project("Skjult\\Kunde X", "Klip\\b.mov")
                          + project("Projekter\\Rikke Lindholm"))
        ix = self.start()
        self.settled(ix)
        skjult = self.source(ix, "Skjult")
        self.assertTrue(skjult["included"])
        ix.stop()
        # Hidden folders are no candidates any more (SPEC §15.8): an older version registered
        # this one (e.g. C:\$GetCurrent or a vendor folder on a USB disk).
        set_attributes(os.path.join(disk, "Skjult"), FILE_ATTRIBUTE_HIDDEN)

        ix = self.start()
        status = self.settled(ix)
        again = self.source(ix, "Skjult")
        self.assertEqual((again["id"], again["online"], again["included"]),
                         (skjult["id"], True, True))
        self.assertEqual(status["sources_offline"], 0)
        loc = ix.locate(os.path.join(disk, "Skjult", "Kunde X", "Klip", "b.mov"))
        self.assertEqual((loc["source"]["id"], loc["online"]), (skjult["id"], True))
        hit = ix.search("kunde x")["results"][0]
        self.assertEqual((hit["name"], hit["source"]["online"]), ("Kunde X", True))

        # A folder hidden before it was ever seen does not become a source.
        fresh = os.path.join(self.tmp, "fresh")
        make_tree(fresh, project("Gemt\\Hemmelig") + ["Synlig\\x.txt"])
        set_attributes(os.path.join(fresh, "Gemt"), FILE_ATTRIBUTE_HIDDEN)
        self.world.volume("fresh", "41DD0002", label="Ny", drive="Y:")
        self.rediscover(ix)
        self.probed(ix, "Synlig")
        self.assertEqual(sorted(s["key"] for s in ix.list_sources()
                                if s["volume_serial"] == "41DD0002"),
                         ["vol:41DD0002:\\Synlig"])


class RemoveHostTest(Review2TestCase):
    """R2-IDX-3 / SPEC §15.12: remove_host reports what it forgot (the refusal while an added
    folder lies on the computer: test_index_engine_review.RemoveHostTest)."""

    def test_a_pass_in_flight_cannot_bring_forgotten_shares_back(self):
        self.world.share("NAS", "Projekter", project("Rikke Lindholm"))
        self.world.share("NAS", "Media", project("Pixelbro"))
        self.world.mapped = {"M:": "\\\\NAS\\Media"}       # NAS stays polled for this share
        self.cfg.update({"hosts": ["NAS"]})
        ix = self.start(start_worker=False)
        self.probed(ix, "Projekter", "Media")
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        list_shares = self.world.env().remote_shares

        def slow(host):
            entered.set()
            release.wait(10)
            return list_shares(host)

        ix._env.remote_shares = slow
        ix._kick_discovery()
        self.assertTrue(entered.wait(10))                  # a pass listing every share ...
        self.assertEqual(ix.remove_host("NAS"), {"ok": True, "forgotten": 1})
        release.set()                                      # ... ends after the removal
        self.rediscover(ix, "NAS")
        self.assertFalse(ix._hosts["NAS"].enumerate)
        self.assertEqual([s["display_name"] for s in ix.list_sources()], ["Media"])

    def test_removing_the_only_root_on_a_computer_forgets_its_shares(self):
        # The computer was polled only because of the added folder: its other shares would
        # stay behind offline ("Computeren PC9 svarer ikke") with nothing to remove them.
        self.world.share("PC9", "Deling", ["Sub\\Noter\\x.txt"])
        self.world.share("PC9", "Projekter", project("Rikke Lindholm"))
        ix = self.start(start_worker=False)
        ix.add_root("\\\\pc9\\Deling\\Sub")
        self.probed(ix, "Sub", "Deling", "Projekter")
        self.assertTrue(self.source(ix, "Projekter")["included"])
        ix.remove_root("\\\\PC9\\Deling\\Sub")
        self.assertEqual(ix.list_sources(), [])
        self.assertNotIn("PC9", ix._hosts)
        self.assertEqual(ix.status()["sources_offline"], 0)

    def test_forgetting_the_only_root_on_a_computer_forgets_its_shares(self):
        folder = self.world.share("PC9", "Deling", ["Sub\\Noter\\x.txt"])
        self.world.share("PC9", "Projekter", project("Rikke Lindholm"))
        ix = self.start(start_worker=False)
        ix.add_root("\\\\pc9\\Deling\\Sub")
        sub = self.probed(ix, "Sub", "Deling", "Projekter")[0]
        shutil.rmtree(os.path.join(folder, "Sub"))           # the added folder is deleted
        self.rediscover(ix, "PC9")
        self.assertFalse(self.source(ix, "Sub")["online"])
        ix.forget_source(sub["id"])                          # "Glem": the root goes too
        self.assertEqual((self.cfg["extra_roots"], ix.list_sources()), ([], []))
        self.assertNotIn("PC9", ix._hosts)

    def test_forgetting_a_local_folder_added_by_hand_removes_the_root(self):
        vol = self.world.volume("vol", "F0F00001", tree=["Diverse\\Sub\\x.txt", "Andet\\y.txt"])
        ix = self.start(start_worker=False)
        self.probed(ix, "Diverse", "Andet")
        root = os.path.join(self.tmp, "vol", "Diverse", "Sub")
        sub = ix.add_root(root)["source"]
        self.world.volumes.remove(vol)                       # unplugged: now it can be forgotten
        self.rediscover(ix)
        ix.forget_source(sub["id"])
        self.assertEqual(self.cfg["extra_roots"], [])
        self.assertEqual(sorted(s["display_name"] for s in ix.list_sources()),
                         ["Andet", "Diverse"])


class ManualRootLayoutTest(Review2TestCase):
    """R2-IDX-4: a folder added by hand never splits a whole-volume disk into folders."""

    CARD = ["A001.MOV", "DCIM\\100CANON\\IMG_0001.MP4", "PRIVATE\\M4ROOT\\CLIP\\C0001.MP4",
            "Keep\\notes.txt"]

    def settle_rounds(self, ix, rounds=2):
        for _ in range(rounds):
            self.rediscover(ix)
            self.settled(ix)

    def test_a_folder_added_to_an_excluded_card_does_not_split_it(self):
        card = os.path.join(self.tmp, "card")
        self.world.volume("card", "CAFE0001", label="SDCARD", drive="E:", hotplug=True,
                          tree=self.CARD)
        ix = self.start()
        self.settled(ix)
        whole = self.source(ix, "SDCARD")
        self.assertEqual((whole["key"], whole["included"]), ("vol:CAFE0001:\\", True))
        ix.set_source_mode(whole["id"], "exclude")            # "Medtag aldrig"
        ix.add_root(os.path.join(card, "Keep"))               # ... except this folder
        self.settle_rounds(ix)
        sources = self.by_name(ix)
        self.assertEqual(sorted(sources), ["Keep", "SDCARD"])  # no DCIM, no PRIVATE
        self.assertEqual({k: sources["SDCARD"][k] for k in ("id", "online", "included")},
                         {"id": whole["id"], "online": True, "included": False})
        self.assertEqual({k: sources["Keep"][k] for k in ("manual", "online", "included")},
                         {"manual": True, "online": True, "included": True})
        self.assertEqual(ix.search("c0001")["results"], [])
        self.assertEqual([r["source"]["name"] for r in ix.search("notes")["results"]], ["Keep"])

        ix.remove_root(os.path.join(card, "Keep"))
        self.settle_rounds(ix, 1)
        self.assertEqual([(s["display_name"], s["online"]) for s in ix.list_sources()],
                         [("SDCARD", True)])

    def test_a_manual_root_inside_an_included_whole_disk_keeps_it_whole(self):
        # An older version let a folder of an included whole disk be added (add_root refuses
        # that now), so an upgraded index can hold both.
        card = os.path.join(self.tmp, "card")
        self.world.volume("card", "CAFE0002", label="SDCARD", drive="E:", hotplug=True,
                          tree=self.CARD)
        ix = self.start()
        self.settled(ix)
        whole = self.source(ix, "SDCARD")
        ix.stop()
        self.cfg.update({"extra_roots": [os.path.join(card, "Keep")]})
        ix = self.start()
        self.settle_rounds(ix)
        sources = self.by_name(ix)
        self.assertEqual(sorted(sources), ["Keep", "SDCARD"])
        self.assertEqual({k: sources["SDCARD"][k] for k in ("id", "online", "included")},
                         {"id": whole["id"], "online": True, "included": True})
        self.assertTrue(sources["Keep"]["manual"] and sources["Keep"]["online"])
        self.assertEqual(ix.status()["sources_offline"], 0)


class ProjectPartRootTest(Review2TestCase):
    """R2-IDX-6 / SPEC §15.12: a root named like a template sub-folder is never a project."""

    def test_a_shared_klip_folder_is_no_project(self):
        klip = self.world.share("PC7", "Klip", ["Råmateriale\\A001.mov", "Stills\\still.jpg",
                                                "A7S\\C0001.MP4", "FX9\\X001.MXF"])
        kunde = self.world.share("PC7", "Kunde X", ["Råmateriale\\B001.mov",
                                                    "Stills\\s.jpg"])
        self.cfg.update({"hosts": ["PC7"]})
        ix = self.start()
        self.settled(ix)
        src = self.source(ix, "Klip")
        self.assertTrue(src["included"])                     # project material is searched ...
        self.assertEqual((src["root_is_project"], src["project_count"]), (False, 0))   # ...
        other = self.wait_until(lambda: self.source(ix, "Kunde X")["root_is_project"]
                                and self.source(ix, "Kunde X"), message="the flag")
        self.assertEqual(other["project_count"], 1)          # the same content, another name
        self.assertEqual(ix.status()["projects"], 1)
        self.assertEqual(ix.search("klip")["results"], [])
        clip = ix.search("c0001")["results"][0]
        self.assertEqual((clip["name"], clip["project"]), ("C0001.MP4", None))
        self.assertIsNone(ix.locate(os.path.join(klip, "A7S", "C0001.MP4"))["project"])
        self.assertEqual(ix.map_paths([os.path.join(klip, "A7S", "C0001.MP4")])["folders"], [])
        self.assertEqual(ix.suggest_project_folders("Klip"), [])
        self.assertEqual([i["name"] for i in ix.recent_projects()], ["Kunde X"])
        self.assertEqual(ix.locate(os.path.join(kunde, "Råmateriale", "B001.mov"))
                         ["project"]["name"], "Kunde X")

        ix.stop()                                            # also when loaded at startup
        ix = self.start(start_worker=False)
        self.assertEqual((self.source(ix, "Klip")["root_is_project"],
                          self.source(ix, "Kunde X")["root_is_project"]), (False, True))


class SourceContractTest(Review2TestCase):
    """SPEC §15.12: Source.last_shallow_scan (RES2-1), Source/SourceRef.volume_present
    (UI2-4)."""

    def test_last_shallow_scan_shows_a_finished_shallow_scan(self):
        self.world.volume("vol", "5A11A001", tree=project("Kunder\\Rikke Lindholm"))
        ix = self.start()
        self.settled(ix)
        first = self.source(ix, "Kunder")["last_shallow_scan"]
        self.assertIsNotNone(first)                         # the first-time shallow scan
        self.world.clock.advance(31)
        make_tree(os.path.join(self.tmp, "vol", "Kunder"), project("Nyt projekt"))
        ix.on_window_shown()                                # a window round: shallow only
        later = self.wait_until(lambda: (self.source(ix, "Kunder")["last_shallow_scan"] or 0)
                                > first and self.source(ix, "Kunder"),
                                message="the window round")
        self.assertGreater(later["last_shallow_scan"], later["last_scan_end"])
        self.assertIn("Nyt projekt", [r["name"] for r in ix.search("nyt projekt")["results"]])

    def test_volume_present_tells_a_vanished_folder_from_a_missing_disk(self):
        root = os.path.join(self.tmp, "sys")
        system = self.world.volume("sys", "5Y5E0002", drive="C:",
                                   tree=project("Kunder 2026 (STUDIO)\\Rikke Lindholm",
                                                "Klip\\a.mov"))
        system["is_system"] = True
        usb = self.world.volume("usb", "0B0B0003", label="Sølv", drive="H:", hotplug=True,
                                tree=project("Projekter\\Pixelbro", "Klip\\m.mov"))
        ix = self.start()
        self.settled(ix)
        kunder, projekter = self.source(ix, "Kunder 2026 (STUDIO)"), self.source(ix, "Projekter")
        self.assertEqual((kunder["online"], kunder["volume_present"]), (True, True))
        self.assertTrue(ix.search("lindholm")["results"][0]["source"]["volume_present"])

        # The folder is moved away at year end: the (system) disk is still there.
        os.rename(os.path.join(root, "Kunder 2026 (STUDIO)"), os.path.join(root, "Arkiv 2026"))
        self.rediscover(ix)
        gone = self.by_id(ix, kunder["id"])
        self.assertEqual((gone["online"], gone["volume_present"], gone["disk_name"]),
                         (False, True, "Systemdisk"))
        hit = next(r for r in ix.search("lindholm", online_only=False)["results"]
                   if r["source"]["id"] == kunder["id"])
        self.assertEqual((hit["source"]["online"], hit["source"]["volume_present"]),
                         (False, True))
        loc = ix.locate(os.path.join(root, "Kunder 2026 (STUDIO)", "Rikke Lindholm"))
        self.assertEqual((loc["online"], loc["source"]["volume_present"]), (False, True))

        # A disk that is unplugged: its folders are not gone, the disk is missing.
        self.world.volumes.remove(usb)
        self.rediscover(ix)
        unplugged = self.by_id(ix, projekter["id"])
        self.assertEqual((unplugged["online"], unplugged["volume_present"]), (False, False))
        # The disk of an offline source leaves: shown rows get the other hint.
        updates = self.updates(kunder["id"])
        self.world.volumes.remove(system)
        self.rediscover(ix)
        self.assertFalse(self.by_id(ix, kunder["id"])["volume_present"])
        self.wait_until(lambda: self.updates(kunder["id"]) > updates,
                        message="index_updated for the new hint")

    def test_volume_present_of_a_share_follows_its_computer(self):
        self.world.share("NAS", "Projekter", project("Rikke Lindholm"))
        self.world.share("NAS", "Gammel", project("Pixelbro"))
        self.cfg.update({"hosts": ["NAS"]})
        ix = self.start(start_worker=False)
        self.probed(ix, "Projekter", "Gammel")
        self.world.remote["NAS"] = ["Projekter"]            # "Gammel" is no longer shared
        self.rediscover(ix, "NAS")
        gammel = self.source(ix, "Gammel")
        self.assertEqual((gammel["online"], gammel["volume_present"]), (False, True))
        self.world.remote["NAS"] = None                     # NAS is switched off
        self.rediscover(ix, "NAS")
        self.assertEqual([(s["display_name"], s["online"], s["volume_present"])
                          for s in ix.list_sources()],
                         [("Gammel", False, False), ("Projekter", False, False)])
