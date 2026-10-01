"""Index engine: registry lifecycle, probes and modes, manual roots, hosts, new disks, commands,
JSON shapes and events (SPEC §4, §7.1, §8)."""

import json
import os
import time

from projektsog import config, indexer
from tests._index_engine_fixtures import (EngineTestCase, fake_worker_argv, make_tree,
                                          module_env, project)

_env = None

SOURCE_KEYS = {"id", "key", "kind", "display_name", "host", "path", "unc_path", "volume_label",
               "volume_serial", "fs", "drive", "last_drive", "disk_name", "hotplug",
               "volume_size", "online", "mode", "included", "auto_reason", "manual",
               "entry_count", "dir_count", "file_count", "project_count", "total_size",
               "last_scan_end", "last_scan_ok", "last_error", "last_seen", "scanning",
               "scan_kind", "queued", "is_system", "root_is_project",      # + SPEC §15.3/§15.4
               "last_shallow_scan", "volume_present"}                       # + SPEC §15.12
STATUS_KEYS = {"hostname", "version", "sources_total", "sources_online", "sources_offline",
               "sources_excluded", "sources_ready", "sources_included_online", "entries",
               "files", "dirs", "projects", "scanning", "queued", "last_scan_end",
               "initial_scan_done", "worker", "db_size"}
SCANNING_KEYS = {"source_id", "name", "kind", "entries", "dirs", "units_done", "units_total",
                 "started", "full"}
HOST_KEYS = {"name", "online", "shares", "last_seen", "self"}
SOURCE_REF_KEYS = {"id", "name", "host", "kind", "online", "drive", "disk_name", "volume_label",
                   "last_seen", "is_system", "volume_present"}


def setUpModule():
    global _env
    _env = module_env()


def tearDownModule():
    _env.cleanup()


class LifecycleTest(EngineTestCase):
    def probed(self, ix, *names):
        """Wait until the named sources exist and have a probe verdict; return them."""
        def ready():
            by_name = {s["display_name"]: s for s in ix.list_sources()}
            if all(n in by_name and by_name[n]["auto_reason"] for n in names):
                return [by_name[n] for n in names]
            return None
        return self.wait_until(ready, message=f"probes of {names}")

    def test_volume_moving_to_another_drive_letter_keeps_its_source(self):
        vol = self.world.volume("mnt1", "AAAA0001", label="Arbejde", drive="E:", hotplug=True,
                                tree=project("Kunder\\Rikke Lindholm"))
        ix = self.start(start_worker=False)
        (src,) = self.probed(ix, "Kunder")
        self.assertEqual(src["key"], "vol:AAAA0001:\\Kunder")
        self.assertEqual((src["online"], src["included"], src["last_drive"], src["disk_name"]),
                         (True, True, "E:", "Arbejde"))

        self.world.volumes.remove(vol)                                  # unplugged
        self.rediscover(ix)
        offline = self.source(ix, "Kunder")
        self.assertFalse(offline["online"])
        self.assertIsNotNone(offline["last_seen"])
        self.assertIn(src["id"], {i for e in self.events_of("sources") for i in e["changed"]})
        self.assertEqual(ix.status()["sources_offline"], 1)

        os.rename(os.path.join(self.tmp, "mnt1"), os.path.join(self.tmp, "mnt2"))
        self.world.volume("mnt2", "AAAA0001", label="Arbejde", drive="G:", hotplug=True)
        self.rediscover(ix)
        back = self.source(ix, "Kunder")
        self.assertEqual((back["id"], back["key"]), (src["id"], src["key"]))
        self.assertEqual((back["online"], back["last_drive"]), (True, "G:"))
        self.assertEqual(back["path"], os.path.join(self.tmp, "mnt2", "Kunder"))
        self.wait_until(lambda: self.db_rows(
            "SELECT current_path, last_drive FROM sources WHERE id = ?", src["id"])
            == [(back["path"], "G:")], message="the move to be saved")

    def test_registry_is_loaded_offline_until_rediscovered(self):
        self.world.volume("vol", "BBBB0002", label="Forår", tree=project("Projekter\\Pixelbro"))
        ix = self.start(start_worker=False)
        (first,) = self.probed(ix, "Projekter")
        ix.set_source_mode(first["id"], "include")
        ix.stop()
        self.world.volumes.clear()

        ix = self.start(start_worker=False)
        loaded = self.source(ix, "Projekter")
        self.assertEqual((loaded["id"], loaded["key"], loaded["mode"]),
                         (first["id"], first["key"], "include"))
        self.assertEqual(loaded["auto_reason"], "1 projektmappe fundet")
        self.assertFalse(loaded["online"])
        self.assertEqual(ix.status()["sources_online"], 0)

        self.world.volume("vol", "BBBB0002", label="Forår")
        self.rediscover(ix)
        self.assertTrue(self.source(ix, "Projekter")["online"])

    def test_probe_verdicts_and_modes(self):
        self.world.volume("vol", "CCCC0003",
                          tree=project("Kunder\\Rikke Lindholm") + ["Effects\\blur.fx"])
        ix = self.start(start_worker=False)
        kunder, effects = self.probed(ix, "Kunder", "Effects")
        self.assertEqual((kunder["included"], kunder["auto_reason"]),
                         (True, "1 projektmappe fundet"))
        self.assertEqual((effects["included"], effects["auto_reason"]),
                         (False, "Ingen projektmapper fundet"))
        self.assertEqual(ix.status()["sources_excluded"], 1)

        self.assertTrue(ix.set_source_mode(effects["id"], "include")["included"])
        self.assertFalse(ix.set_source_mode(kunder["id"], "exclude")["included"])
        self.assertEqual(ix.search("lindholm", online_only=False)["results"], [])
        restored = ix.set_source_mode(kunder["id"], "auto")
        self.assertEqual((restored["mode"], restored["included"]), ("auto", True))
        with self.assertRaisesRegex(ValueError, "^Ugyldig tilstand ‘sometimes’"):
            ix.set_source_mode(kunder["id"], "sometimes")
        with self.assertRaisesRegex(ValueError, "^Placeringen findes ikke$"):
            ix.set_source_mode(9999, "auto")
        self.wait_until(lambda: self.db_rows("SELECT mode FROM sources WHERE id = ?",
                                             effects["id"]) == [("include",)],
                        message="the mode to be saved")

    def test_reprobe_rules(self):
        root = os.path.join(self.tmp, "vol")
        self.world.volume("vol", "DDDD0004",
                          tree=project("Arkiv\\Gammelt") + ["Senere\\notes.txt", "Tomt\\"])
        ix = self.start(start_worker=False)
        arkiv, senere, _tomt = self.probed(ix, "Arkiv", "Senere", "Tomt")
        self.assertEqual((arkiv["included"], senere["included"]), (True, False))

        make_tree(os.path.join(root, "Senere"), project("Klar Tand"))   # now holds a project
        self.rediscover(ix)
        self.assertFalse(self.source(ix, "Senere")["included"])        # not before 30 min
        self.assertEqual(self.world.probes_of(os.path.join(root, "Senere")), 1)

        self.world.clock.advance(31 * 60)
        self.rediscover(ix)
        now = self.wait_until(lambda: self.source(ix, "Senere")["included"] and
                              self.source(ix, "Senere"), message="the re-probe")
        self.assertEqual(now["auto_reason"], "1 projektmappe fundet")
        # An included source is never probed again, so it can never flip back by itself.
        self.assertEqual(self.world.probes_of(os.path.join(root, "Arkiv")), 1)

        # An auto-excluded source is re-probed as soon as it comes online again.
        tomt_probes = self.world.probes_of(os.path.join(root, "Tomt"))
        vol = self.world.volumes.pop()
        self.rediscover(ix)
        self.world.volumes.append(vol)
        self.rediscover(ix)
        self.wait_until(lambda: self.world.probes_of(os.path.join(root, "Tomt")) > tomt_probes,
                        message="the re-probe on return")

    def test_a_known_volume_keeps_its_layout(self):
        vol = self.world.volume("vol", "DDDD1004", label="Kopi", drive="E:",
                                tree=["Arkiv\\Gammelt\\x.txt"])
        ix = self.start(start_worker=False)
        (arkiv,) = self.probed(ix, "Arkiv")

        # Unplugged, reorganised elsewhere (a project directly in the root), plugged in as G:
        self.world.volumes.remove(vol)
        self.rediscover(ix)
        root = os.path.join(self.tmp, "vol2")
        os.rename(os.path.join(self.tmp, "vol"), root)
        make_tree(root, project("Rikke Lindholm"))
        self.world.volume("vol2", "DDDD1004", label="Kopi", drive="G:")
        self.rediscover(ix)
        # The disk keeps its folder sources (SPEC §15.6): the new project folder is one more
        # source, and nothing is replaced or forgotten behind the user's back.
        again, rikke = self.probed(ix, "Arkiv", "Rikke Lindholm")
        self.assertEqual((again["id"], again["key"], again["online"], again["path"]),
                         (arkiv["id"], arkiv["key"], True, os.path.join(root, "Arkiv")))
        self.assertEqual((rikke["key"], rikke["included"], rikke["auto_reason"]),
                         ("vol:DDDD1004:\\Rikke Lindholm", True, "Mappen er selv et projekt"))
        self.assertEqual(sorted(s["display_name"] for s in ix.list_sources()),
                         ["Arkiv", "Rikke Lindholm"])
        self.assertEqual(ix._forget_ids, set())

        # A disk seen for the first time with a project in its root is one whole-volume source.
        self.world.volume("new", "DDDD2005", label="Ny", drive="H:", tree=project("Pixelbro"))
        self.rediscover(ix)
        (whole,) = self.probed(ix, "Ny")
        self.assertEqual((whole["key"], whole["included"]), ("vol:DDDD2005:\\", True))

    def test_manual_roots(self):
        root = os.path.join(self.tmp, "vol")
        self.world.volume("vol", "EEEE0005", tree=["Diverse\\Undermappe\\klip.mov",
                                                   "Diverse\\x.txt"])
        ix = self.start(start_worker=False)
        (diverse,) = self.probed(ix, "Diverse")
        self.assertFalse(diverse["included"])

        folder = os.path.join(root, "Diverse", "Undermappe")
        result = ix.add_root(folder + "\\")
        self.assertTrue(result["ok"])
        added = result["source"]
        self.assertEqual((added["key"], added["manual"], added["included"]),
                         ("vol:EEEE0005:\\Diverse\\Undermappe", True, True))
        self.assertEqual(self.cfg["extra_roots"], [folder])
        with open(self.cfg.path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["extra_roots"], [folder])
        self.rediscover(ix)
        self.assertTrue(self.source(ix, "Undermappe")["online"])
        ix.add_root(folder.lower())                                     # same root: no-op
        self.assertEqual(self.cfg["extra_roots"], [folder])

        with self.assertRaisesRegex(ValueError, "fuld sti"):
            ix.add_root("Projekter\\2026")
        with self.assertRaisesRegex(ValueError, "fuld sti"):
            ix.add_root("\\\\SERVER")
        with self.assertRaisesRegex(ValueError, "ikke tilføjet manuelt"):
            ix.remove_root("C:\\Findes\\Ikke")

        ix.remove_root(folder)                      # only existed because of the root: gone
        self.assertEqual(self.cfg["extra_roots"], [])
        self.assertNotIn("Undermappe", [s["display_name"] for s in ix.list_sources()])

        ix.add_root(os.path.join(root, "Diverse"))  # an auto candidate becomes manual ...
        self.assertTrue(self.source(ix, "Diverse")["manual"])
        self.assertTrue(self.source(ix, "Diverse")["included"])
        ix.remove_root(os.path.join(root, "Diverse"))       # ... and returns to its auto verdict
        after = self.source(ix, "Diverse")
        self.assertEqual((after["manual"], after["included"]), (False, False))

    def test_first_sighting_of_a_volume_is_announced_once(self):
        self.world.volume("sys", "11110000", label="System", tree=["Github\\x.py"])
        ix = self.start(start_worker=False)
        self.probed(ix, "Github")
        self.assertEqual(self.events_of("new_volume"), [])              # first run: baseline

        arkiv = self.world.volume("arkiv", "22220000", label="ARKIV", drive="F:", hotplug=True,
                                  tree=project("Kunder 2026 ARKIV\\Pixelbro"))
        self.rediscover(ix)
        (event,) = self.wait_until(lambda: self.events_of("new_volume"), message="new_volume")
        sid = self.source(ix, "Kunder 2026 ARKIV")["id"]
        self.assertEqual(event, {"disk_name": "ARKIV", "drive": "F:", "source_ids": [sid],
                                 "included": True, "reason": "1 projektmappe fundet"})
        self.assertEqual(self.events_of("notify"), [{
            "title": "Projektsøg", "level": "info",
            "text": "Ny disk ‘ARKIV’ tilsluttet – medtaget i søgningen"}])

        self.world.volume("sd", "33330000", drive="E:", hotplug=True, tree=["DCIM\\readme.txt"])
        self.rediscover(ix)
        self.wait_until(lambda: len(self.events_of("new_volume")) == 2, message="2nd new_volume")
        second = self.events_of("new_volume")[1]
        self.assertEqual((second["disk_name"], second["included"], second["reason"]),
                         ("disk uden navn (2 TB)", False, "Ingen projektmapper fundet"))
        self.assertEqual(self.events_of("notify")[1]["text"],
                         "Ny disk ‘disk uden navn (2 TB)’ tilsluttet – ikke medtaget: "
                         "Ingen projektmapper fundet")

        self.world.volumes.remove(arkiv)                     # re-plugging is not "new"
        self.rediscover(ix)
        self.world.volumes.append(arkiv)
        self.rediscover(ix)
        ix.stop()
        ix = self.start(start_worker=False)                     # the known set is persisted
        self.world.volume("usb", "44440000", label="Ny", drive="H:", tree=["Tom\\"])
        self.rediscover(ix)
        self.wait_until(lambda: len(self.events_of("new_volume")) == 3, message="3rd new_volume")
        time.sleep(0.2)
        self.assertEqual([e["disk_name"] for e in self.events_of("new_volume")],
                         ["ARKIV", "disk uden navn (2 TB)", "Ny"])


class HostsTest(EngineTestCase):
    def test_remote_host_shares_come_and_go(self):
        folder = self.world.share("NAS", "Projekter", project("Rikke Lindholm"))
        self.world.share("NAS", "Privat", ["x.txt"])
        self.cfg.update({"hosts": ["TESTPC", "nas"]})
        ix = self.start(start_worker=False)

        def verdicts():
            by = {s["display_name"]: s for s in ix.list_sources()}
            ok = all(n in by and by[n]["auto_reason"] for n in ("Projekter", "Privat"))
            return (by["Projekter"], by["Privat"]) if ok else None
        projekter, privat = self.wait_until(verdicts, message="share probes")
        self.assertEqual((projekter["key"], projekter["kind"], projekter["host"], projekter["fs"]),
                         ("unc:NAS\\Projekter", "share", "NAS", "NTFS"))
        self.assertEqual((projekter["path"], projekter["unc_path"]), (folder, folder))
        self.assertEqual((projekter["included"], privat["included"]), (True, False))
        self.assertIsNone(projekter["disk_name"])
        hosts = ix.hosts()
        self.assertEqual([h["name"] for h in hosts], ["TESTPC", "NAS"])
        self.assertEqual({k: hosts[1][k] for k in ("online", "shares", "self")},
                         {"online": True, "shares": 2, "self": False})
        self.assertTrue(hosts[0]["self"])
        self.assertEqual(set(hosts[1]), {"name", "online", "shares", "last_seen", "self"})

        self.world.remote["NAS"] = None                                 # powered off
        self.rediscover(ix, "NAS")
        self.assertFalse(any(s["online"] for s in ix.list_sources()))
        self.assertFalse(ix.hosts()[1]["online"])
        self.world.remote["NAS"] = ["Projekter", "Privat"]
        self.rediscover(ix, "NAS")
        self.assertTrue(self.source(ix, "Projekter")["online"])

        ix.remove_host("nas")
        self.assertEqual(self.cfg["hosts"], ["TESTPC"])
        # Its polling stops and its shares are forgotten at once (SPEC §15.8, LOC-1).
        self.assertNotIn("NAS", ix._hosts)
        self.assertEqual(ix.list_sources(), [])
        self.assertEqual([h["name"] for h in ix.hosts()], ["TESTPC"])

        with self.assertRaisesRegex(ValueError, "^Ugyldigt computernavn"):
            ix.add_host("ugyldigt navn!")
        with self.assertRaisesRegex(ValueError, "er ikke på listen"):
            ix.remove_host("NOBODY")
        ix.add_host("\\\\grafik-pc")
        ix.add_host("Grafik-pc")
        self.assertEqual(self.cfg["hosts"], ["TESTPC", "GRAFIK-PC"])
        self.wait_until(lambda: "GRAFIK-PC" in ix._hosts, message="a thread for the new host")


    def test_unc_root_polls_its_host(self):
        folder = self.world.share("PC9", "Deling", ["Sub\\Noter\\x.txt", "Andet\\y.txt"])
        ix = self.start(start_worker=False)
        result = ix.add_root("\\\\pc9\\Deling\\Sub")
        self.assertEqual(self.cfg["extra_roots"], ["\\\\PC9\\Deling\\Sub"])
        added = result["source"]
        self.assertEqual((added["key"], added["kind"], added["manual"], added["path"]),
                         ("unc:PC9\\Deling\\Sub", "share", True, os.path.join(folder, "Sub")))
        self.wait_until(lambda: "PC9" in ix._hosts and ix._hosts["PC9"].passes,
                        message="the host of the root to be polled")
        self.assertTrue(ix._hosts["PC9"].enumerate)       # its other shares are candidates too
        self.wait_until(lambda: self.source(ix, "Sub")["online"], message="the root online")
        self.wait_until(lambda: self.source(ix, "Deling")["auto_reason"], message="share probe")
        self.assertTrue(self.source(ix, "Sub")["included"])

        ix.remove_root("\\\\PC9\\Deling\\Sub")
        self.assertNotIn("Sub", [s["display_name"] for s in ix.list_sources()])
        self.wait_until(lambda: "PC9" not in ix._hosts, message="the host to be dropped")


class ForgetTest(EngineTestCase):
    def test_forget_offline_source_deletes_its_index(self):
        self.world.volume("vol", "FFFF0006", tree=project("Kunder\\Rikke Lindholm", "Klip\\a.mov"))
        ix = self.start()
        self.settled(ix)
        src = self.source(ix, "Kunder")
        self.assertGreater(self.db_rows("SELECT count(*) FROM entries WHERE source_id = ?",
                                        src["id"])[0][0], 0)
        with self.assertRaisesRegex(ValueError, "^Kun offline placeringer kan glemmes$"):
            ix.forget_source(src["id"])

        vol = self.world.volumes.pop()
        self.rediscover(ix)
        ix.forget_source(src["id"])
        self.assertEqual(ix.list_sources(), [])
        self.assertEqual(ix.search("lindholm")["results"], [])
        self.wait_until(lambda: self.db_rows("SELECT count(*) FROM entries")[0][0] == 0
                        and not self.db_rows("SELECT id FROM sources"),
                        message="entries and row to be deleted")
        with self.assertRaisesRegex(ValueError, "^Placeringen findes ikke$"):
            ix.forget_source(src["id"])

        self.world.volumes.append(vol)                        # the disk returns: a new source
        self.rediscover(ix)
        again = self.wait_until(lambda: ix.list_sources(), message="rediscovery")[0]
        self.assertEqual(again["key"], src["key"])
        self.assertGreater(again["id"], src["id"])


class ShapesAndCommandsTest(EngineTestCase):
    def test_json_shapes(self):
        self.world.volume("vol", "ABCD0007", label="Sølv",
                          tree=project("Kunder\\Rikke Lindholm", "Klip\\a.mov") + ["Tomt\\"])
        ix = self.start()
        status = self.settled(ix)
        self.assertEqual(set(status), STATUS_KEYS)
        self.assertEqual(set(status["worker"]), {"running", "restarts"})
        self.assertEqual((status["hostname"], status["version"]), ("TESTPC", "1.0.0"))
        self.assertEqual({k: status[k] for k in ("sources_total", "sources_online",
                                                   "sources_offline", "sources_excluded",
                                                   "sources_ready", "sources_included_online",
                                                   "projects", "initial_scan_done")},
                         {"sources_total": 2, "sources_online": 1, "sources_offline": 0,
                          "sources_excluded": 1, "sources_ready": 1,
                          "sources_included_online": 1, "projects": 1,
                          "initial_scan_done": True})
        self.assertGreater(status["db_size"] + 1, 0)
        for source in ix.list_sources():
            self.assertEqual(set(source), SOURCE_KEYS)
        kunder = self.source(ix, "Kunder")
        self.assertEqual((kunder["last_scan_ok"], kunder["scanning"], kunder["scan_kind"],
                          kunder["queued"], kunder["disk_name"], kunder["file_count"]),
                         (True, False, None, False, "Sølv", 1))
        item = ix.search("lindholm")["results"][0]
        self.assertEqual(set(item["source"]), SOURCE_REF_KEYS)
        for host in ix.hosts():
            self.assertEqual(set(host), HOST_KEYS)

    def test_command_errors(self):
        self.world.volume("vol", "ABCD0008", tree=project("Kunder\\A") + ["Tomt\\"])
        ix = self.start(start_worker=False)
        kunder = self.wait_until(lambda: [s for s in ix.list_sources() if s["included"]])[0]
        tomt = self.wait_until(lambda: [s for s in ix.list_sources()
                                        if s["auto_reason"] and not s["included"]])[0]
        with self.assertRaisesRegex(ValueError, "^Placeringen findes ikke$"):
            ix.scan_now(12345)
        with self.assertRaisesRegex(ValueError, "^Placeringen er ikke medtaget"):
            ix.scan_now(tomt["id"])
        with self.assertRaisesRegex(ValueError, "^Placeringen findes ikke$"):
            ix.children(12345, "")
        with self.assertRaisesRegex(ValueError, "^Ukendt filter"):
            ix.search("x", kind="alt")
        self.world.volumes.clear()
        self.rediscover(ix)
        with self.assertRaisesRegex(ValueError, "^Placeringen er ikke tilgængelig"):
            ix.scan_now(kunder["id"])
        ix.scan_now(None, full=True)            # offline sources get a full scan when back
        self.assertTrue(ix._sources[kunder["id"]].needs_full)

    def test_events(self):
        self.world.volume("vol", "ABCD0009", tree=project("Kunder\\Rikke Lindholm"))
        ix = self.start(worker_argv=fake_worker_argv())
        (progress, *_rest) = self.wait_until(lambda: self.events_of("scan_progress"),
                                             message="scan_progress")
        self.assertEqual(set(progress), SCANNING_KEYS)
        self.assertEqual((progress["kind"], progress["name"], progress["entries"],
                          progress["units_total"]), ("shallow", "Kunder", 5, 3))
        scanning = ix.status()["scanning"]
        self.assertEqual([(s["kind"], s["entries"]) for s in scanning], [("shallow", 5)])
        source = self.source(ix, "Kunder")
        self.assertEqual((source["scanning"], source["scan_kind"]), (True, "shallow"))
        self.assertIn(source["id"], {i for e in self.events_of("sources") for i in e["changed"]})

        self.drain()
        start = time.monotonic()
        count_before = len(self.events_of("status"))
        while time.monotonic() - start < 1.2:            # many changes, few status events
            ix.set_source_mode(source["id"], "include")
            ix.set_source_mode(source["id"], "auto")
            time.sleep(0.01)
        time.sleep(0.6)
        elapsed = time.monotonic() - start
        status_events = self.events_of("status")[count_before:]
        self.assertGreaterEqual(len(status_events), 2)                 # ... and the last state
        self.assertLessEqual(len(status_events), 1 + int(elapsed / indexer.STATUS_INTERVAL_S))
        self.assertEqual(set(status_events[-1]), STATUS_KEYS)

    def test_settings_change_reaches_worker_and_forces_full_rescans(self):
        self.world.volume("vol", "ABCD0010", tree=project("Kunder\\Rikke Lindholm"))
        ix = self.start()
        self.settled(ix)
        self.assertEqual(self.commands("config")[0]["cfg"]["hosts"], [])
        configs = len(self.commands("config"))
        deep_before = len(self.scans(kind="deep"))
        self.cfg.update({"exclude_file_names": ["Thumbs.db", "noter.txt"]})
        self.wait_until(lambda: len(self.commands("config")) > configs, message="config command")
        self.assertEqual(self.commands("config")[-1]["cfg"]["exclude_file_names"],
                         ["Thumbs.db", "noter.txt"])
        deep = self.wait_until(lambda: self.scans(kind="deep")[deep_before:],
                               message="a full rescan")
        self.assertTrue(deep[0]["full"])
        self.settled(ix)
        self.assertFalse(ix._sources[deep[0]["source_id"]].needs_full)
        self.assertEqual(config.Config(path=self.cfg.path)["exclude_file_names"],
                         ["Thumbs.db", "noter.txt"])
        self.assertIsInstance(ix, indexer.Indexer)
