"""Index engine: regression tests for the review round 1 findings (SPEC §15).

XMC-1 live online state, IDX-1 sticky volume layout, IDX-2 roots that are projects, IDX-3 disk
swaps at the same letter, IDX-4 shared roots, IDX-8 slow manual roots, LOC-1 removing a host,
§15.4 system volume, and the add_root nesting rule (§15.8).
"""

import os
import shutil
import sqlite3
import time
from unittest import mock

from projektsog import indexer
from tests._index_engine_fixtures import (EngineTestCase, fake_worker_argv, make_tree,
                                          module_env, project)

_env = None

# A worker that refuses every scan like the real one does when another disk sits at the path.
_SWAPPED_WORKER = r"""
import json, sys
def emit(ev):
    sys.stdout.write(json.dumps(ev) + "\n")
    sys.stdout.flush()
emit({"ev": "ready", "pid": 0})
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("cmd") == "scan":
        emit({"ev": "failed", "job": msg["job"], "source_id": msg["source_id"],
              "error": "Disken er skiftet"})
    elif msg.get("cmd") == "quit":
        break
"""


def setUpModule():
    global _env
    _env = module_env()


def tearDownModule():
    _env.cleanup()


class ReviewTestCase(EngineTestCase):
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

    def updates(self, sid):
        return sum(1 for e in self.events_of("index_updated") if e["source_id"] == sid)

    def entry_names(self, sid):
        return sorted(n for (n,) in self.db_rows(
            "SELECT name FROM entries WHERE source_id = ?", sid))


class LiveOnlineStateTest(ReviewTestCase):
    """XMC-1 / SPEC §15.1: online, offline and path changes publish index_updated."""

    def test_disk_online_offline_and_moves_publish_index_updated(self):
        vol = self.world.volume("mnt1", "C0FFEE01", label="Sølv", drive="H:", hotplug=True,
                                tree=project("Kunder\\Pixelbro"))
        ix = self.start(start_worker=False)
        (src,) = self.probed(ix, "Kunder")
        sid = src["id"]
        time.sleep(0.7)
        self.assertEqual(self.updates(sid), 0)              # the first pass after startup

        self.world.volumes.remove(vol)                      # unplugged
        self.rediscover(ix)
        self.wait_until(lambda: self.updates(sid) == 1, message="index_updated on unplug")
        self.world.volumes.append(vol)                      # back within a minute: no rescan
        self.rediscover(ix)
        self.wait_until(lambda: self.updates(sid) == 2, message="index_updated on re-plug")
        self.assertTrue(self.by_id(ix, sid)["online"])

        self.world.volumes.remove(vol)
        self.rediscover(ix)
        self.wait_until(lambda: self.updates(sid) == 3, message="index_updated on unplug")
        self.world.clock.advance(120)
        self.world.volumes.append(vol)                      # back after two minutes
        self.rediscover(ix)
        self.wait_until(lambda: self.updates(sid) == 4, message="index_updated on re-plug")

        # The disk gets another drive letter between two passes: shown rows carry old paths.
        self.world.volumes.remove(vol)
        os.rename(os.path.join(self.tmp, "mnt1"), os.path.join(self.tmp, "mnt2"))
        self.world.volume("mnt2", "C0FFEE01", label="Sølv", drive="G:", hotplug=True)
        self.rediscover(ix)
        self.wait_until(lambda: self.updates(sid) == 5, message="index_updated on a move")
        moved = self.by_id(ix, sid)
        self.assertEqual((moved["online"], moved["last_drive"], moved["path"]),
                         (True, "G:", os.path.join(self.tmp, "mnt2", "Kunder")))

    def test_hosts_coming_and_going_publish_index_updated(self):
        self.world.share("NAS", "Projekter", project("Rikke Lindholm"))
        self.cfg.update({"hosts": ["NAS"]})
        ix = self.start(start_worker=False)
        (src,) = self.probed(ix, "Projekter")
        sid = src["id"]
        time.sleep(0.7)
        self.assertEqual(self.updates(sid), 0)              # the host's first pass
        self.world.remote["NAS"] = None                     # powered off
        self.rediscover(ix, "NAS")
        self.wait_until(lambda: self.updates(sid) == 1, message="index_updated when off")
        self.world.remote["NAS"] = ["Projekter"]            # powered on again
        self.rediscover(ix, "NAS")
        self.wait_until(lambda: self.updates(sid) == 2, message="index_updated when on")
        self.assertTrue(self.by_id(ix, sid)["online"])


class VolumeLayoutTest(ReviewTestCase):
    """IDX-1 / SPEC §15.6: a root-level media file or project never replaces folder sources."""

    def test_root_level_media_or_project_keeps_the_folder_sources(self):
        hot = os.path.join(self.tmp, "hot")
        fixed = os.path.join(self.tmp, "fixed")
        self.world.volume("hot", "A1B2C3D4", label="2024 Disk Sølv", drive="H:", hotplug=True,
                          tree=project("Projekter\\Pixelbro Radio", "Klip\\a.mov")
                          + ["Cache\\noter.txt"])
        self.world.volume("fixed", "0A1B2C3D", label="Lokal disk 2", drive="Z:",
                          tree=project("Kunder\\Rikke Lindholm", "Klip\\b.mov")
                          + ["Arkiv\\Gammelt\\gammelt referat.txt"])
        ix = self.start()
        self.settled(ix)
        before = {s["display_name"]: s for s in ix.list_sources()}
        self.assertEqual(sorted(before), ["Arkiv", "Cache", "Kunder", "Projekter"])
        ix.set_source_mode(before["Cache"]["id"], "exclude")     # the user's choices
        ix.set_source_mode(before["Arkiv"]["id"], "include")
        self.settled(ix)
        self.assertTrue(ix.search("gammelt")["results"])

        make_tree(hot, ["Spot.mp4"])                             # an export saved to H:\
        make_tree(fixed, project("Ny Kunde"))                    # a project created in Z:\
        self.rediscover(ix)
        self.settled(ix)
        after = {s["display_name"]: s for s in ix.list_sources()}
        for name, old in before.items():
            self.assertEqual((after[name]["id"], after[name]["key"], after[name]["online"]),
                             (old["id"], old["key"], True), name)
        self.assertEqual((after["Cache"]["mode"], after["Arkiv"]["mode"]), ("exclude", "include"))
        self.assertEqual((after["Ny Kunde"]["key"], after["Ny Kunde"]["included"]),
                         ("vol:0A1B2C3D:\\Ny Kunde", True))
        self.assertNotIn("vol:A1B2C3D4:\\", {s["key"] for s in after.values()})
        self.assertEqual(self.commands("forget"), [])
        self.assertTrue(ix.search("gammelt")["results"])

        os.remove(os.path.join(hot, "Spot.mp4"))                 # moved away again
        shutil.rmtree(os.path.join(fixed, "Ny Kunde"))
        self.rediscover(ix)
        self.settled(ix)
        final = {s["display_name"]: s for s in ix.list_sources()}
        for name, old in before.items():
            self.assertEqual((final[name]["id"], final[name]["online"]), (old["id"], True))
        self.assertEqual((final["Cache"]["mode"], final["Arkiv"]["mode"]), ("exclude", "include"))
        self.assertFalse(final["Ny Kunde"]["online"])            # vanished: kept, not forgotten
        self.assertEqual(self.commands("forget"), [])


class RootProjectTest(ReviewTestCase):
    """IDX-2 / SPEC §15.3: a source whose root is a project."""

    def test_a_share_that_is_a_project_is_found_by_its_name(self):
        folder = self.world.share("GRAFIK-PC", "Klar Tand - Silkeborg",
                                  ["Klip\\a.mov", "Grafik\\b.png", "Speak\\c.wav"])
        self.world.share("GRAFIK-PC", "1. KUNDENAVN", ["Klip\\", "Grafik\\", "Final\\"])
        self.cfg.update({"hosts": ["GRAFIK-PC"]})
        ix = self.start()
        self.settled(ix)
        src = self.wait_until(lambda: self.source(ix, "Klar Tand - Silkeborg")["root_is_project"]
                              and self.source(ix, "Klar Tand - Silkeborg"), message="the flag")
        self.assertEqual(src["project_count"], 1)
        template = self.source(ix, "1. KUNDENAVN")
        self.assertTrue(template["root_is_project"])     # search shows it as a template
        self.assertEqual((template["project_count"], ix.status()["projects"]), (0, 1))

        (suggestion,) = ix.suggest_project_folders("Klar Tand Silkeborg")
        self.assertEqual((suggestion["project"], suggestion["score"], suggestion["item"]),
                         ({"name": "Klar Tand - Silkeborg", "rel_path": "", "path": folder,
                           "unc_path": folder}, 1.0, None))
        self.assertEqual(ix.suggest_project_folders("Kundenavn"), [])
        self.assertEqual(ix.locate(os.path.join(folder, "Klip", "a.mov"))["project"]["rel_path"],
                         "")
        # Search (index-store side of the contract): the root is a project item.
        roots = [r for r in ix.search("silkeborg")["results"] if r["rel_path"] == ""]
        self.assertEqual([(r["kind"], r["name"], r["path"]) for r in roots],
                         [("project", "Klar Tand - Silkeborg", folder)])
        self.assertIn("Klar Tand - Silkeborg", [i["name"] for i in ix.recent_projects()])
        self.assertEqual(ix.search("kundenavn")["results"], [])

        ix.stop()                                        # the flag is known right at startup
        ix = self.start(start_worker=False)
        self.assertTrue(self.source(ix, "Klar Tand - Silkeborg")["root_is_project"])

    def test_the_flag_follows_the_index(self):
        folder = self.world.share("NAS", "Projekt X", ["Klip\\a.mov", "Andet\\b.txt"])
        self.cfg.update({"hosts": ["NAS"]})
        ix = self.start()
        sid = self.probed(ix, "Projekt X")[0]["id"]
        ix.set_source_mode(sid, "include")
        self.settled(ix)
        self.assertFalse(self.by_id(ix, sid)["root_is_project"])
        make_tree(folder, ["Grafik\\logo.png"])          # now Klip + Grafik: a project
        self.drain()
        ix.scan_now(sid)
        self.wait_until(lambda: self.by_id(ix, sid)["root_is_project"], message="the new flag")
        self.wait_until(lambda: self.updates(sid) >= 1, message="index_updated")
        self.assertIn(sid, {i for e in self.events_of("sources") for i in e["changed"]})

    def test_a_folder_that_is_not_deep_scanned_yet_wins_a_name_tie(self):
        self.world.share("NAS", "Kunder", project("Klar Tand - Voxpop Silkeborg")
                         + project("Klar Tand - Silkeborg Voxpop"))
        self.cfg.update({"hosts": ["NAS"]})
        ix = self.start()
        self.settled(ix)
        conn = sqlite3.connect(self.db_path)
        try:
            with conn:      # the new folder: found by a shallow scan only (RES-1)
                conn.execute("UPDATE entries SET mtime = NULL WHERE name = ?",
                             ("Klar Tand - Silkeborg Voxpop",))
                conn.execute("UPDATE entries SET mtime = 1000 WHERE name = ?",
                             ("Klar Tand - Voxpop Silkeborg",))
        finally:
            conn.close()
        best = ix.suggest_project_folders("Klar Tand - Silkeborg Voxpop")
        self.assertEqual([(s["project"]["name"], s["score"]) for s in best],
                         [("Klar Tand - Silkeborg Voxpop", 1.0),
                          ("Klar Tand - Voxpop Silkeborg", 1.0)])


class DiskSwapTest(ReviewTestCase):
    """IDX-3 (+ ux IDX-1) / SPEC §15.5: scans of local sources carry the expected serial."""

    def test_local_scans_carry_the_expected_serial(self):
        self.world.volume("vol", "5E1A0001", tree=project("Kunder\\Rikke Lindholm"))
        self.world.share("NAS", "Delt", project("Pixelbro"))
        self.cfg.update({"hosts": ["NAS"]})
        ix = self.start()
        self.settled(ix)
        local = self.scans(self.source(ix, "Kunder")["id"])
        share = self.scans(self.source(ix, "Delt")["id"])
        self.assertTrue(local and share)
        self.assertEqual({m.get("expected_serial") for m in local}, {"5E1A0001"})
        self.assertTrue(all("expected_serial" not in m for m in share))

    def test_a_card_swapped_before_discovery_notices_keeps_its_index(self):
        card = os.path.join(self.tmp, "card")
        card_a = self.world.volume("card", "AAAA0001", drive="E:", hotplug=True,
                                   tree=["MUSIC\\A_0001.WAV", "MUSIC\\A_0002.WAV"])
        with mock.patch.object(indexer, "SWAP_HOLD_S", 0.3):
            ix = self.start()
            self.settled(ix)
            sid = self.source(ix, "MUSIC")["id"]
            kept = self.entry_names(sid)
            self.assertEqual(kept, ["A_0001.WAV", "A_0002.WAV"])
            # Card B goes into the same slot: same letter and folder names, another serial.
            shutil.rmtree(os.path.join(card, "MUSIC"))
            make_tree(card, ["MUSIC\\B_0001.WAV"])
            self.world.set_serial(card, "BBBB0002")
            passes = ix._local_passes
            self.world.clock.advance(31)
            ix.on_window_shown()                         # Shift+Space before the next pass
            self.wait_until(lambda: ix._sources[sid].swap_strikes >= 1,
                            message="the refused window scan")
            self.assertEqual(self.entry_names(sid), kept)            # nothing of card B
            src = self.by_id(ix, sid)
            self.assertEqual((src["last_error"], src["last_scan_ok"]), (None, True))
            self.wait_until(lambda: ix._local_passes > passes, message="a rediscovery")

            self.world.volumes.remove(card_a)            # discovery sees card B now
            self.world.volume("card", "BBBB0002", drive="E:", hotplug=True)
            self.rediscover(ix)
            self.settled(ix)
        a, b = sorted((s for s in ix.list_sources() if s["display_name"] == "MUSIC"),
                      key=lambda s: s["id"])
        self.assertEqual((a["id"], a["online"], a["key"]), (sid, False, "vol:AAAA0001:\\MUSIC"))
        self.assertEqual((b["online"], b["key"]), (True, "vol:BBBB0002:\\MUSIC"))
        self.assertEqual(self.entry_names(a["id"]), kept)
        self.assertEqual(self.entry_names(b["id"]), ["B_0001.WAV"])

    def test_a_refused_scan_is_no_scan_error_and_waits_for_rediscovery(self):
        self.world.volume("vol", "5E1A0002", tree=project("Kunder\\Rikke Lindholm"))
        with mock.patch.object(indexer, "SWAP_HOLD_S", 0.2):
            ix = self.start(worker_argv=fake_worker_argv(_SWAPPED_WORKER))
            sid = self.probed(ix, "Kunder")[0]["id"]
            self.wait_until(lambda: len(self.scans(sid)) >= 3, message="three refused scans")
        times = [t for t, m in list(self.sent) if m.get("cmd") == "scan"][:3]
        self.assertGreaterEqual(times[1] - times[0], 0.2)            # held, doubling
        self.assertGreaterEqual(times[2] - times[1], 0.4)
        src = ix._sources[sid]
        self.assertEqual((src.last_error, src.last_scan_ok, src.last_scan_end,
                          src.last_shallow_scan), (None, None, None, None))
        self.assertTrue(all(m["kind"] == "shallow" and m["first_time"]
                            for m in self.scans(sid)))
        self.assertGreater(ix._local_passes, 1)                      # rediscovery was kicked
        self.assertIsNone(self.by_id(ix, sid)["last_error"])

    def test_a_slow_volume_is_neither_refreshed_nor_taken_offline(self):
        root = os.path.join(self.tmp, "vol")
        vol = self.world.volume("vol", "57A1E001", drive="E:", hotplug=True,
                                tree=project("Kunder\\A"))
        ix = self.start(start_worker=False)
        (src,) = self.probed(ix, "Kunder")
        seen = src["last_seen"]
        vol["stale"] = True             # list_volumes: E: answers slowly (old facts)
        make_tree(root, project("Ny mappe\\B"))
        self.world.clock.advance(120)
        self.rediscover(ix)
        now = self.source(ix, "Kunder")
        self.assertEqual((now["online"], now["last_seen"]), (True, seen))
        self.assertNotIn("Ny mappe", [s["display_name"] for s in ix.list_sources()])
        del vol["stale"]                # it answers again
        self.rediscover(ix)
        self.assertTrue(self.probed(ix, "Ny mappe"))
        self.assertGreater(self.source(ix, "Kunder")["last_seen"], seen)


class SharedRootTest(ReviewTestCase):
    """IDX-4 / SPEC §15.5: a disconnected disk's paths stay with that disk."""

    def test_paths_known_only_to_a_disconnected_disk(self):
        card = os.path.join(self.tmp, "card")
        disk_a = self.world.volume("card", "DA000001", label="Disk A", drive="F:", hotplug=True,
                                   tree=project("Rikke Lindholm", "Klip\\A001.MOV"))
        ix = self.start()
        self.settled(ix)
        a_id = self.source(ix, "Disk A")["id"]
        self.world.volumes.remove(disk_a)
        self.rediscover(ix)
        os.remove(os.path.join(card, "Rikke Lindholm", "Klip", "A001.MOV"))
        make_tree(card, ["Rikke Lindholm\\Klip\\B001.MOV"])
        self.world.volume("card", "DB000002", label="Disk B", drive="F:", hotplug=True)
        self.rediscover(ix)
        self.settled(ix)
        b_id = self.source(ix, "Disk B")["id"]
        clip_a = os.path.join(card, "Rikke Lindholm", "Klip", "A001.MOV")
        clip_b = os.path.join(card, "Rikke Lindholm", "Klip", "B001.MOV")

        loc = ix.locate(clip_a)
        self.assertEqual((loc["source"]["id"], loc["online"], loc["project"]["name"],
                          loc["entry"]["name"]), (a_id, False, "Rikke Lindholm", "A001.MOV"))
        result = ix.map_paths([clip_a] * 5)
        self.assertEqual([(f["source"]["id"], f["online"], f["count"])
                          for f in result["folders"]], [(a_id, False, 5)])
        self.assertEqual(result["other_dirs"], [])
        self.assertEqual((ix.locate(clip_b)["source"]["id"], ix.locate(clip_b)["online"]),
                         (b_id, True))
        # Both disks know the folder: a new file in it belongs to the connected one.
        new = ix.locate(os.path.join(card, "Rikke Lindholm", "Klip", "C001.MOV"))
        self.assertEqual((new["source"]["id"], new["online"]), (b_id, True))

        scans = len(self.scans(b_id))
        ix.path_missing(clip_a)                     # not the connected disk's path
        time.sleep(0.4)
        self.assertEqual(len(self.scans(b_id)), scans)


class ManualRootTest(ReviewTestCase):
    """IDX-8: a slow existence check never turns a manual root into an auto candidate."""

    def test_a_slow_existence_check_keeps_the_root_manual_and_scanning(self):
        root = os.path.join(self.tmp, "vol")
        self.world.volume("vol", "E1E10008", tree=["Arkiv\\notes.txt"])
        ix = self.start(worker_argv=fake_worker_argv())
        (arkiv,) = self.probed(ix, "Arkiv")
        self.assertFalse(arkiv["included"])
        ix.add_root(os.path.join(root, "Arkiv"))
        self.wait_until(lambda: self.scans(arkiv["id"]), message="its first scan")
        with mock.patch.object(indexer, "_dir_exists", return_value=None):
            self.rediscover(ix)
        src = self.source(ix, "Arkiv")
        self.assertEqual((src["manual"], src["included"], src["online"], src["scanning"]),
                         (True, True, True, True))
        self.assertEqual(self.commands("cancel"), [])

    def test_folders_inside_an_unanswered_manual_root_do_not_become_sources(self):
        root = os.path.join(self.tmp, "vol")
        self.world.volume("vol", "E1E10009", label="Arkivdisk",
                          tree=["A\\x.txt", "Users\\Mette\\Videos\\Projekter\\z.txt"])
        projekter = os.path.join(root, "Users", "Mette", "Videos", "Projekter")
        self.world.shares = [{"name": "Projekter", "path": projekter}]    # a share candidate
        self.cfg.update({"extra_roots": [os.path.join(root, "Users", "Mette")]})
        ix = self.start(start_worker=False)
        self.probed(ix, "A", "Mette")
        keys = sorted(s["key"] for s in ix.list_sources())
        self.assertEqual(keys, ["vol:E1E10009:\\A", "vol:E1E10009:\\Users\\Mette"])
        with mock.patch.object(indexer, "_dir_exists", return_value=None):
            self.rediscover(ix)
        self.assertEqual(sorted(s["key"] for s in ix.list_sources()), keys)   # no "Projekter"
        mette = self.source(ix, "Mette")
        self.assertEqual((mette["manual"], mette["online"]), (True, True))


class RemoveHostTest(ReviewTestCase):
    """LOC-1 / SPEC §15.8: removing a computer forgets its shares."""

    def test_removed_host_shares_are_forgotten_and_come_back_when_added(self):
        self.world.share("NAS", "Projekter", project("Rikke Lindholm", "Klip\\a.mov"))
        media = self.world.share("NAS", "Media", project("Pixelbro", "Klip\\b.mov"))
        self.world.mapped = {"M:": "\\\\NAS\\Media"}
        self.cfg.update({"hosts": ["NAS"]})
        ix = self.start()
        self.settled(ix)
        old = self.source(ix, "Projekter")
        self.assertTrue(ix.search("lindholm")["results"])

        self.drain()
        self.assertEqual(ix.remove_host("NAS"), {"ok": True, "forgotten": 1})   # §15.12
        self.assertEqual([s["display_name"] for s in ix.list_sources()], ["Media"])  # mapped
        self.wait_until(lambda: self.updates(old["id"]) >= 1, message="index_updated")
        self.assertEqual(ix.search("lindholm")["results"], [])
        self.assertNotIn("Rikke Lindholm", [i["name"] for i in ix.recent_projects()])
        self.assertEqual((ix.status()["sources_total"], ix.status()["sources_offline"]), (1, 0))
        self.assertEqual([(h["name"], h["shares"]) for h in ix.hosts()][1:], [("NAS", 1)])
        self.wait_until(lambda: [m for m in self.commands("forget")
                                 if m["source_id"] == old["id"]], message="the forget")
        self.wait_until(lambda: not self.db_rows(
            "SELECT 1 FROM entries WHERE source_id = ?", old["id"]), message="entries deleted")
        self.assertTrue(self.source(ix, "Media")["online"])
        self.assertTrue(media)

        ix.add_host("NAS")                               # added again: rediscovered
        self.wait_until(lambda: any(s["display_name"] == "Projekter"
                                    for s in ix.list_sources()), message="rediscovery")
        self.settled(ix)
        again = self.source(ix, "Projekter")
        self.assertEqual(again["key"], old["key"])
        self.assertGreater(again["id"], old["id"])
        self.assertTrue(ix.search("lindholm")["results"])

    def test_a_host_holding_an_added_folder_is_not_removed(self):
        # R2-IDX-3 / SPEC §15.12: an added folder keeps every share of its computer searched,
        # so removing the computer is refused (nothing changes) until the folder is removed.
        self.world.share("NAS", "Projekter", project("Rikke Lindholm"))
        self.world.share("NAS", "Arkiv", ["Gammelt\\x.txt"])
        self.cfg.update({"hosts": ["NAS"], "extra_roots": ["\\\\NAS\\Arkiv\\Gammelt"]})
        ix = self.start(start_worker=False)
        self.probed(ix, "Projekter", "Gammelt")
        before = ix.list_sources()
        with self.assertRaises(ValueError) as caught:
            ix.remove_host("nas")
        self.assertEqual(str(caught.exception),
                         "Mappen ‘\\\\NAS\\Arkiv\\Gammelt’ ligger på NAS – fjern den først")
        self.assertEqual(self.cfg["hosts"], ["NAS"])
        self.assertEqual([s["id"] for s in ix.list_sources()], [s["id"] for s in before])
        self.assertEqual((ix._hosts["NAS"].enumerate, ix._forget_ids), (True, set()))

        ix.remove_root("\\\\NAS\\Arkiv\\Gammelt")          # the folder first ...
        self.assertEqual(sorted(s["display_name"] for s in ix.list_sources()),
                         ["Arkiv", "Projekter"])          # (NAS is still on the list)
        self.assertEqual(ix.remove_host("NAS"), {"ok": True, "forgotten": 2})   # ... then it
        self.assertEqual((ix.list_sources(), self.cfg["hosts"]), ([], []))
        self.assertNotIn("NAS", ix._hosts)


class AddRootTest(ReviewTestCase):
    """SPEC §15.8: a root inside (or around) an included location is refused."""

    def test_nested_roots_are_refused(self):
        root = os.path.join(self.tmp, "vol")
        self.world.volume("vol", "ADD00008", tree=project("Kunder\\Rikke Lindholm")
                          + ["Diverse\\Sub\\x.txt"])
        self.world.share("NAS", "Video", project("Pixelbro"))
        self.cfg.update({"hosts": ["NAS"]})
        ix = self.start(start_worker=False)
        self.probed(ix, "Kunder", "Diverse", "Video")
        for path, name in ((os.path.join(root, "Kunder", "Rikke Lindholm"), "Kunder"),
                           (os.path.join(root, "Kunder"), "Kunder"),
                           (root + "\\", "Kunder"),
                           ("\\\\nas\\Video\\Pixelbro", "Video")):
            with self.assertRaises(ValueError) as caught:
                ix.add_root(path)
            self.assertEqual(str(caught.exception),
                             f"Mappen er allerede med i søgningen via ‘{name}’")
        self.assertEqual(self.cfg["extra_roots"], [])

        sub = os.path.join(root, "Diverse", "Sub")      # inside an excluded location: fine
        self.assertTrue(ix.add_root(sub)["source"]["manual"])
        ix.add_root(sub.upper())                        # the same root again: no-op
        self.assertEqual(self.cfg["extra_roots"], [sub])


class SystemVolumeTest(ReviewTestCase):
    """SPEC §15.4: Source.is_system (and "Systemdisk" for an unlabeled system volume)."""

    def test_sources_on_the_system_volume(self):
        system = self.world.volume("sys", "5Y5E0001", drive="C:", tree=project("Nyt Projekt"))
        system["is_system"] = True
        self.world.volume("usb", "0B0B0002", label="Sølv", drive="H:", hotplug=True,
                          tree=project("Kunder\\A"))
        ix = self.start(start_worker=False)
        nyt, kunder = self.probed(ix, "Nyt Projekt", "Kunder")
        self.assertEqual((nyt["is_system"], nyt["disk_name"]), (True, "Systemdisk"))
        self.assertEqual((kunder["is_system"], kunder["disk_name"]), (False, "Sølv"))
        self.wait_until(lambda: self.db_rows("SELECT value FROM meta WHERE key = ?",
                                             "system_volumes") == [('["5Y5E0001"]',)],
                        message="the system serial to be saved")
        ix.stop()
        self.world.volumes.clear()
        ix = self.start(start_worker=False)             # known before any rediscovery
        loaded = self.source(ix, "Nyt Projekt")
        self.assertEqual((loaded["online"], loaded["is_system"]), (False, True))
