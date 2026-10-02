"""The import helper (importer.py): cards, suggestions, new projects and the verified copy."""

import hashlib
import json
import os
import queue
import tempfile
import threading
import time
import unittest
from collections import namedtuple
from datetime import date

from projektsog import importer
from projektsog.config import Config
from projektsog.events import EventBus

Usage = namedtuple("Usage", "total used free")
SERIAL = "7E3A91C4"

_tmp: tempfile.TemporaryDirectory | None = None
_old_appdata: str | None = None


def setUpModule() -> None:
    global _tmp, _old_appdata
    _tmp = tempfile.TemporaryDirectory()
    _old_appdata = os.environ.get("LOCALAPPDATA")
    os.environ["LOCALAPPDATA"] = _tmp.name


def tearDownModule() -> None:
    if _old_appdata is not None:
        os.environ["LOCALAPPDATA"] = _old_appdata
    if _tmp is not None:
        _tmp.cleanup()


def write(path: str, data: bytes = b"", mtime: float | None = None) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def sony_xml(model: str, created: str) -> bytes:
    return (f'<?xml version="1.0" encoding="UTF-8"?><NonRealTimeMeta>'
            f'<CreationDate value="{created}"/><Device manufacturer="Sony" modelName="{model}" '
            f'serialNo="1234567"/></NonRealTimeMeta>').encode()


class FakeIndexer:
    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.template_list: list[dict] = []
        self.named: list[dict] = []
        self.refreshed: list[str] = []

    def find_files(self, names):
        wanted = {n.casefold() for n in names}
        return [r for r in self.rows if r["name"].casefold() in wanted]

    def templates(self):
        return self.template_list

    def projects_named(self, names):
        wanted = {n.casefold() for n in names}
        return [p for p in self.named if p["name"].casefold() in wanted]

    def refresh_path(self, path):
        self.refreshed.append(path)


class FakeController:
    def __init__(self) -> None:
        self.shown = queue.Queue()

    def show_window(self, **kwargs):
        self.shown.put(kwargs)
        return True


class ImporterCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        base = self.dir.name
        self.card_root = os.path.join(base, "card")
        clip = os.path.join(self.card_root, "XDROOT", "Clip")
        self.clips = {}
        for i, created in ((1, "2026-09-29T19:41:34Z"), (2, "2026-09-29T21:11:38Z")):
            data = bytes([i]) * (300_000 + i)
            self.clips[f"FX9_000{i}.MXF"] = data
            write(os.path.join(clip, f"FX9_000{i}.MXF"), data, mtime=1_790_000_000 + i)
            write(os.path.join(clip, f"FX9_000{i}M01.XML"), sony_xml("PXW-FX9V", created))
            write(os.path.join(clip, f"FX9_000{i}R01.BIM"), b"bim" * i)
        write(os.path.join(self.card_root, "XDROOT", "MEDIAPRO.XML"), b"<x/>")
        self.disk = os.path.join(base, "disk", "Kunder 2026 (TEST)")
        self.template = os.path.join(self.disk, "1. KUNDENAVN")
        for rel in ("Final", "Grafik", "Klip\\A7S", "Klip\\Drone", "Klip\\FX9", "Project"):
            os.makedirs(os.path.join(self.template, rel))
        write(os.path.join(self.template, "Project", "skabelon.drp"), b"drp")
        self.project = os.path.join(self.disk, "Rikke Lindholm")
        os.makedirs(os.path.join(self.project, "Klip", "FX9"))
        self.cfg = Config(path=os.path.join(base, "config.json"))
        self.bus = EventBus()
        self.events = self.bus.subscribe()
        self.indexer = FakeIndexer()
        self.indexer.template_list = [{"path": self.template, "parent": self.disk, "online": True,
                                       "source": {"kind": "local", "host": "STUDIO-PC", "disk_name": "Test"}}]
        self.controller = FakeController()
        self.volumes = [self.volume()]
        self.free = 10 ** 12
        self.opened: list[tuple[str, bool]] = []
        self.importer = self.make()

    def make(self, **kwargs) -> importer.Importer:
        options = dict(
            list_volumes=lambda timeout=5.0: [dict(v) for v in self.volumes],
            call_with_timeout=lambda key, fn, timeout: ("ok", fn()),
            disk_usage=lambda path: Usage(2 * 10 ** 12, 0, self.free),
            open_folder=lambda path, activate=True: self.opened.append((path, activate)) or True,
            history_path=os.path.join(self.dir.name, "imports.json"))
        options.update(kwargs)
        return importer.Importer(self.cfg, self.bus, self.indexer, None, None, self.controller, **options)

    def volume(self, **extra) -> dict:
        return {"drive": "X:", "root": self.card_root, "serial": SERIAL, "label": "", "drive_type": 2,
                "hotplug": True, "is_system": False, "size": 128 * 10 ** 9, **extra}

    def tearDown(self) -> None:
        self.importer.stop()
        self.bus.unsubscribe(self.events)
        self.dir.cleanup()

    def drain(self) -> list[tuple[str, object]]:
        out = []
        while True:
            try:
                event_type, data, _ts = self.events.get_nowait()
            except queue.Empty:
                return out
            out.append((event_type, data))

    def card_id(self) -> str:
        self.importer.poll(announce=False)
        return self.importer.cards()[0]["id"]

    def wait_job(self, timeout: float = 10.0) -> dict:
        deadline = time.monotonic() + timeout
        while self.importer.job()["state"] in ("copying", "verifying", "deleting"):
            if time.monotonic() > deadline:
                self.fail("import did not finish")
            time.sleep(0.02)
        self.importer._job.join(5)       # its done-callback (history, message) has run too
        return self.importer.job()

    def imported(self, names=None) -> str:
        """Put (some of) the card's files in Rikke Lindholm\\Klip\\FX9 and in the fake index."""
        folder = os.path.join(self.project, "Klip", "FX9")
        for name in names or self.card_names():
            with open(os.path.join(self.clip_dir(), name), "rb") as fh:
                write(os.path.join(folder, name), fh.read())
            self.indexer.rows.append({"name": name, "size": os.path.getsize(os.path.join(folder, name)),
                                      "folder": folder, "online": True,
                                      "project": {"name": "Rikke Lindholm", "path": self.project}})
        return folder

    def clip_dir(self) -> str:
        return os.path.join(self.card_root, "XDROOT", "Clip")

    def card_names(self) -> list[str]:
        return sorted(os.listdir(self.clip_dir()))


class CardTests(ImporterCase):
    def test_an_fx9_card_is_recognised(self) -> None:
        self.importer.poll()
        cards = self.importer.cards()
        self.assertEqual(len(cards), 1)
        card = cards[0]
        self.assertEqual((card["drive"], card["model"], card["camera"]), ("X:", "PXW-FX9V", "FX9"))
        self.assertEqual((card["files"], card["clips"]), (6, 2))
        self.assertEqual(card["found"], {"clips": 0, "files": 0, "total": 6, "complete": False, "projects": []})
        self.assertLess(card["first"], card["last"])
        self.assertNotIn("_files", card)
        self.assertEqual(self.controller.shown.get(timeout=2), {"reason": "card", "panel": "import"})
        self.assertIn("cards", [t for t, _ in self.drain()])
        # Nothing new on the next look; taking the card out removes it.
        self.importer.poll()
        self.assertTrue(self.controller.shown.empty())
        self.volumes = []
        self.importer.poll()
        self.assertEqual(self.importer.cards(), [])

    def test_cards_present_at_startup_do_not_pop_up(self) -> None:
        self.importer.poll(announce=False)
        self.assertEqual(len(self.importer.cards()), 1)
        time.sleep(0.05)
        self.assertTrue(self.controller.shown.empty())

    def test_without_auto_open_a_message_is_shown_instead(self) -> None:
        self.cfg.update({"import_auto_open": False})
        self.importer.poll()
        notes = [d for t, d in self.drain() if t == "notify"]
        self.assertEqual(notes[0]["title"], "FX9-kort i X:")
        self.assertTrue(self.controller.shown.empty())

    def test_disks_and_volumes_without_media_are_ignored(self) -> None:
        empty = os.path.join(self.dir.name, "usb")
        os.makedirs(os.path.join(empty, "Kunder"))
        self.volumes = [self.volume(root=empty, serial="11112222", drive="Y:"),
                        self.volume(is_system=True, drive="C:", serial="33334444"),
                        self.volume(drive_type=3, hotplug=False, drive="D:", serial="55556666")]
        self.importer.poll()
        self.assertEqual(self.importer.cards(), [])

    def test_files_already_in_a_project_are_found_by_name_and_size_on_the_disk(self) -> None:
        folder = self.imported(["FX9_0001.MXF", "FX9_0001M01.XML"])
        self.indexer.rows += [
            {"name": "FX9_0002.MXF", "size": 5, "folder": "C:\\Andet\\Klip\\FX9",          # same name, other clip
             "project": {"name": "Andet", "path": "C:\\Andet"}},
            {"name": "FX9_0002.MXF", "size": len(self.clips["FX9_0002.MXF"]), "folder": "X:\\XDROOT\\Clip",
             "volume_serial": SERIAL, "project": None}]                                  # the card itself
        self.importer.poll()
        card = self.importer.cards()[0]
        self.assertEqual(card["found"], {"clips": 1, "files": 2, "total": 6, "complete": False, "projects": [
            {"name": "Rikke Lindholm", "path": self.project, "folder": folder, "clips": 1, "files": 2,
             "complete": False, "online": True}]})

    def test_a_card_that_is_fully_imported_pops_up_and_says_so(self) -> None:
        folder = self.imported()
        self.importer.poll()
        found = self.importer.cards()[0]["found"]
        self.assertEqual((found["clips"], found["files"], found["complete"]), (2, 6, True))
        self.assertEqual(found["projects"][0]["folder"], folder)
        self.assertEqual(self.controller.shown.get(timeout=2), {"reason": "card", "panel": "import"})
        reasons = [s["reason"] for s in self.importer.options(self.importer.cards()[0]["id"])["suggestions"]]
        self.assertEqual(reasons[0], "Alle kortets filer ligger her")
        # Without the window: a message that says it.
        self.cfg.update({"import_auto_open": False})
        self.volumes = []
        self.importer.poll()
        self.volumes = [self.volume()]
        self.importer.poll()
        note = [d for t, d in self.drain() if t == "notify"][-1]
        self.assertEqual((note["title"], note["text"]), ("Kortet er allerede overført", "Alle 2 klip ligger i Rikke Lindholm"))

    def test_the_disk_decides_not_an_old_index(self) -> None:
        folder = self.imported()
        os.remove(os.path.join(folder, "FX9_0002R01.BIM"))                 # deleted since the last scan
        with open(os.path.join(folder, "FX9_0002.MXF"), "ab") as fh:       # changed since the last scan
            fh.write(b"\xff")
        self.importer.poll(announce=False)
        found = self.importer.cards()[0]["found"]
        self.assertEqual((found["clips"], found["files"], found["complete"]), (1, 4, False))

    def test_a_folder_that_cannot_be_listed_now_is_judged_by_the_index(self) -> None:
        self.imported()
        busy = lambda key, fn, timeout: ("timeout", None) if key.startswith("found:") else ("ok", fn())  # noqa: E731
        self.importer = self.make(call_with_timeout=busy)
        self.importer.poll(announce=False)
        self.assertTrue(self.importer.cards()[0]["found"]["complete"])

    def test_camera_folders(self) -> None:
        rules = self.cfg["import_camera_folders"]
        self.assertEqual(importer.camera_folder("PXW-FX9V", "FX9_0001.MXF", rules), "FX9")
        self.assertEqual(importer.camera_folder("PXW-FS7", "207_0001.MXF", rules), "FS7")
        self.assertEqual(importer.camera_folder("ILCE-7SM3", "C0001.MP4", rules), "A7S")
        self.assertEqual(importer.camera_folder("ILCE-7M3", "C0001.MP4", rules), "A7III")
        self.assertEqual(importer.camera_folder(None, "FS7_0001.MXF", rules), "FS7")
        self.assertEqual(importer.camera_folder("DJI", "DJI_0001.MP4", rules), "Drone")
        self.assertEqual(importer.camera_folder("PXW-Z190", "Z19_0001.MXF", rules), "PXW-Z190")
        self.assertEqual(importer.camera_folder(None, "A001C002.MOV", rules), "Kamera")

    def test_alpha_dji_and_gopro_layouts(self) -> None:
        root = os.path.join(self.dir.name, "alpha")
        write(os.path.join(root, "PRIVATE", "M4ROOT", "CLIP", "C0001.MP4"), b"v")
        write(os.path.join(root, "PRIVATE", "M4ROOT", "CLIP", "C0001M01.XML"), sony_xml("ILCE-7SM3", "2026-09-29T10:00:00+02:00"))
        write(os.path.join(root, "DCIM", "100MSDCF", "DSC00001.ARW"), b"s")
        kinds = importer.detect_card(root)
        self.assertEqual([k for k, _ in kinds], ["m4root", "stills"])
        files = importer.card_files(kinds)
        self.assertEqual(importer.camera_model(files, {"m4root"}), "ILCE-7SM3")
        self.assertEqual([(f["name"], f["media"], f["still"]) for f in files],
                         [("C0001.MP4", True, False), ("C0001M01.XML", False, False), ("DSC00001.ARW", False, True)])
        dji = os.path.join(self.dir.name, "dji")
        write(os.path.join(dji, "DCIM", "100MEDIA", "DJI_0001.MP4"), b"d")
        self.assertEqual([k for k, _ in importer.detect_card(dji)], ["dji"])
        gopro = os.path.join(self.dir.name, "gopro")
        write(os.path.join(gopro, "DCIM", "100GOPRO", "GX010001.MP4"), b"g")
        self.assertEqual([k for k, _ in importer.detect_card(gopro)], ["gopro"])
        # A stills-only camera card (DCIM\100MSDCF without video) is not a card for Klip.
        stills = os.path.join(self.dir.name, "stills")
        write(os.path.join(stills, "DCIM", "100MSDCF", "DSC00001.JPG"), b"s")
        self.assertEqual(importer.detect_card(stills), [])

    def test_project_names(self) -> None:
        self.assertEqual(importer.validate_project_name("  Mette Juhl - Portræt "), "Mette Juhl - Portræt")
        self.assertEqual(importer.validate_project_name("Hansen Byg/Tilbygning"), "Hansen Byg\\Tilbygning")
        for bad in ("", "a\\b\\c", "Kunde: test", "Kunde?", "con", "Kunde.", "x" * 200, "\\Kunde"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                importer.validate_project_name(bad)


class WhereToTests(ImporterCase):
    def test_suggestions_and_disks(self) -> None:
        self.imported(["FX9_0001.MXF"])
        today_project = os.path.join(self.disk, "Klar Tand - Skive")
        self.indexer.named = [{"name": "Klar Tand - Skive", "path": today_project, "online": True}]
        tracker = type("Tracker", (), {"folders_on": lambda _self, day: ["Klar Tand - Skive"]})()
        bridge = type("Bridge", (), {"state": lambda _self: {"primary": {
            "name": "Vestervang", "path": os.path.join(self.disk, "Vestervang"), "online": True}}})()
        self.importer = self.make()
        self.importer.tracker, self.importer.bridge = tracker, bridge
        options = self.importer.options(self.card_id())
        self.assertEqual([(s["name"], s["reason"]) for s in options["suggestions"]], [
            ("Rikke Lindholm", "1 af kortets klip ligger her"),
            ("Vestervang", "Åben i DaVinci Resolve"),
            ("Klar Tand - Skive", "Arbejdet på i dag")])
        self.assertEqual(options["suggestions"][0]["free"], 10 ** 12)
        self.assertEqual([(d["path"], d["fits"]) for d in options["disks"]], [(self.disk, True)])
        self.free = 1000
        self.assertFalse(self.importer.disks(needed=10 ** 9)[0]["fits"])

    def test_a_share_root_is_named_by_its_share(self) -> None:
        self.indexer.template_list = [{"path": "\\\\GRAFIK-PC\\Kunder 2026\\1. KUNDENAVN",
                                       "parent": "\\\\GRAFIK-PC\\Kunder 2026", "online": True,
                                       "source": {"kind": "share", "host": "GRAFIK-PC"}}]
        disk, = self.importer.disks()
        self.assertEqual((disk["name"], disk["host"], disk["kind"]), ("Kunder 2026", "GRAFIK-PC", "share"))

    def test_plan_finds_the_camera_folder_and_what_is_new(self) -> None:
        card = self.card_id()
        write(os.path.join(self.project, "Klip", "FX9", "FX9_0001.MXF"), self.clips["FX9_0001.MXF"])
        plan = self.importer.plan(card, self.project)
        self.assertEqual(plan["target"], os.path.join(self.project, "Klip", "FX9"))
        self.assertEqual((plan["new_files"], plan["already"], plan["conflicts"]), (5, 1, 0))
        self.assertFalse(plan["suggest_separate"])
        self.assertTrue(plan["fits"])
        # Clips of another shoot in the folder: a "Dag 2" folder is offered, not forced.
        write(os.path.join(self.project, "Klip", "FX9", "FX9_7000.MXF"), b"other")
        plan = self.importer.plan(card, self.project)
        self.assertTrue(plan["suggest_separate"])
        self.assertEqual(plan["target"], os.path.join(self.project, "Klip", "FX9"))
        plan = self.importer.plan(card, self.project, separate=True)
        self.assertEqual((plan["target"], plan["new_files"]), (os.path.join(self.project, "Klip", "FX9 Dag 2"), 6))
        # Same name, other size (the camera's counter wrapped): never into the same folder.
        write(os.path.join(self.project, "Klip", "FX9", "FX9_0002.MXF"), b"another clip")
        plan = self.importer.plan(card, self.project)
        self.assertEqual((plan["conflicts"], plan["day_folder"]), (1, "FX9 Dag 2"))

    def test_plan_for_a_project_without_the_camera_folder_or_not_made_yet(self) -> None:
        card = self.card_id()
        bare = os.path.join(self.disk, "Uden Klip")
        os.makedirs(bare)
        self.assertEqual(self.importer.plan(card, bare)["target"], os.path.join(bare, "Klip", "FX9"))
        new = self.importer.plan(card, os.path.join(self.disk, "Nyt Projekt"))
        self.assertEqual((new["project_exists"], new["new_files"]), (False, 6))
        self.assertEqual(new["free"], 10 ** 12)   # asked the parent folder

    def test_create_project_from_the_template(self) -> None:
        created = self.importer.create_project(self.disk, "Mette Juhl - Portræt")
        path = os.path.join(self.disk, "Mette Juhl - Portræt")
        self.assertEqual(created["path"], path)
        for rel in ("Final", "Klip\\FX9", "Klip\\A7S", "Project\\skabelon.drp"):
            self.assertTrue(os.path.exists(os.path.join(path, rel)), rel)
        self.assertIn(path, self.indexer.refreshed)
        with self.assertRaisesRegex(ValueError, "findes allerede"):
            self.importer.create_project(self.disk, "Mette Juhl - Portræt")
        with self.assertRaisesRegex(ValueError, "Vælg en af diskene"):
            self.importer.create_project(os.path.join(self.dir.name, "andet"), "X")
        # In a group folder; and recently created projects are suggested.
        self.importer.create_project(self.disk, "Hansen Byg\\Tilbygning")
        self.assertTrue(os.path.isdir(os.path.join(self.disk, "Hansen Byg", "Tilbygning", "Klip", "FX9")))
        reasons = [s["reason"] for s in self.importer.options(self.card_id())["suggestions"]]
        self.assertEqual(reasons, ["Oprettet i dag", "Oprettet i dag"])

    def test_without_a_template_the_configured_folders_are_made(self) -> None:
        self.indexer.template_list[0]["path"] = os.path.join(self.disk, "findes ikke")
        self.importer.create_project(self.disk, "Uden Skabelon")
        self.assertTrue(os.path.isdir(os.path.join(self.disk, "Uden Skabelon", "Klip", "Drone")))


class CopyTests(ImporterCase):
    def test_copy_and_verify(self) -> None:
        card = self.card_id()
        write(os.path.join(self.project, "Klip", "FX9", "FX9_0001.MXF"), self.clips["FX9_0001.MXF"])
        job = self.importer.start_import(card, self.project)
        self.assertEqual(job["files_total"], 5)
        state = self.wait_job()
        self.assertEqual(state["state"], "done", state)
        target = os.path.join(self.project, "Klip", "FX9")
        self.assertEqual(sorted(os.listdir(target)), sorted(
            ["FX9_0001.MXF", "FX9_0001M01.XML", "FX9_0001R01.BIM",
             "FX9_0002.MXF", "FX9_0002M01.XML", "FX9_0002R01.BIM"]))
        with open(os.path.join(target, "FX9_0002.MXF"), "rb") as fh:
            self.assertEqual(fh.read(), self.clips["FX9_0002.MXF"])
        self.assertEqual(int(os.path.getmtime(os.path.join(target, "FX9_0002.MXF"))), 1_790_000_002)
        self.assertEqual(state["copied"], state["bytes_total"])
        self.assertEqual(state["verified"], state["bytes_total"])
        events = self.drain()
        self.assertIn("import", [t for t, _ in events])
        note = [d for t, d in events if t == "notify"][-1]
        self.assertEqual(note["title"], "Kortet er overført")
        self.assertIn(target, self.indexer.refreshed)
        # The card now says where its clips are, before the index has seen the new files.
        self.assertEqual(self.importer.cards()[0]["found"], {"clips": 2, "files": 6, "total": 6, "complete": True,
                                                             "projects": [{"name": "Rikke Lindholm", "path": self.project,
                                                                           "folder": target, "clips": 2, "files": 6,
                                                                           "complete": True, "online": True}]})
        history = self.importer.history()
        self.assertEqual((history[0]["camera"], history[0]["files"], history[0]["mode"]), ("FX9", 5, "copy"))
        # The project with the card's clips is suggested first (and only once).
        suggestions = self.importer.options(card)["suggestions"]
        self.assertEqual([(s["path"], s["reason"]) for s in suggestions], [(self.project, "Alle kortets filer ligger her")])
        with self.assertRaisesRegex(ValueError, "Alle klip ligger der allerede"):
            self.importer.start_import(card, self.project)

    def test_a_copy_that_does_not_verify_is_retried_then_removed(self) -> None:
        calls = []

        def wrong(path, step):           # the copy on the disk reads back wrong; the card is fine
            calls.append(path)
            return "0" * 40 if path.endswith(importer.PART_SUFFIX) else None
        self.importer = self.make(hash_check=wrong)
        self.importer.start_import(self.card_id(), self.project)
        state = self.wait_job()
        self.assertEqual(state["state"], "failed")
        self.assertIn("ikke identisk", state["error"])
        self.assertEqual(sum(1 for p in calls if p.endswith(importer.PART_SUFFIX)), 2)   # tried twice
        self.assertEqual(sum(1 for p in calls if p.startswith(self.card_root)), 2)          # card re-read too
        self.assertEqual(os.listdir(os.path.join(self.project, "Klip", "FX9")), [])   # no half files

    def test_cancel_removes_the_unfinished_file(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def slow(path, step):
            started.set()
            release.wait(5)
            step(1)            # the next step notices the cancel
            return None
        self.importer = self.make(hash_check=slow)
        self.importer.start_import(self.card_id(), self.project)
        self.assertTrue(started.wait(5))
        with self.assertRaisesRegex(ValueError, importer.MSG_BUSY):
            self.importer.start_import(self.importer.cards()[0]["id"], self.project)
        self.importer.cancel()
        release.set()
        self.assertEqual(self.wait_job()["state"], "cancelled")
        self.assertEqual(os.listdir(os.path.join(self.project, "Klip", "FX9")), [])

    def test_not_enough_space(self) -> None:
        self.free = 1000
        with self.assertRaisesRegex(ValueError, "Der er ikke plads nok"):
            self.importer.start_import(self.card_id(), self.project)

    def test_prepare_only_makes_the_folder_and_opens_both(self) -> None:
        result = self.importer.start_import(self.card_id(), os.path.join(self.disk, "Nyt"), mode="prepare")
        target = os.path.join(self.disk, "Nyt", "Klip", "FX9")
        self.assertEqual(result, {"ok": True, "target": target})
        self.assertTrue(os.path.isdir(target))
        self.assertEqual(self.opened, [(os.path.join(self.card_root, "XDROOT", "Clip"), False), (target, True)])
        self.assertEqual(os.listdir(target), [])

    def test_a_crashed_import_leaves_only_a_temp_file_which_is_redone(self) -> None:
        target = os.path.join(self.project, "Klip", "FX9")
        write(os.path.join(target, "FX9_0001.MXF" + importer.PART_SUFFIX), b"half a clip")   # power cut
        self.importer.start_import(self.card_id(), self.project)
        self.assertEqual(self.wait_job()["state"], "done")
        self.assertNotIn("FX9_0001.MXF" + importer.PART_SUFFIX, os.listdir(target))
        with open(os.path.join(target, "FX9_0001.MXF"), "rb") as fh:
            self.assertEqual(fh.read(), self.clips["FX9_0001.MXF"])

    def test_hash_uncached_matches_sha1(self) -> None:
        path = os.path.join(self.dir.name, "big.bin")
        data = os.urandom(importer.CHUNK + 12345)
        write(path, data)
        steps = []
        self.assertEqual(importer.hash_uncached(path, steps.append), hashlib.sha1(data).hexdigest())
        self.assertEqual(sum(steps), len(data))
        self.assertEqual(importer.hash_buffered(path, lambda n: None), hashlib.sha1(data).hexdigest())

    def test_today_uses_the_local_date(self) -> None:
        self.assertEqual(importer._day_text(time.time()), "i dag")
        self.assertIsInstance(date.today(), date)


class MoveTests(ImporterCase):
    """"Klip" (move): like Ctrl+X, but nothing leaves the card before every copy is verified."""

    def setUp(self) -> None:
        super().setUp()
        self.flushed: list[str] = []
        self.before_delete = lambda: None
        self.importer = self.make(flush_volume=self.flush)

    def flush(self, path: str) -> bool:
        self.flushed.append(path)
        self.before_delete()
        return True

    def move(self, **kwargs) -> dict:
        self.importer.start_import(self.card_id(), self.project, mode="move", **kwargs)
        return self.wait_job()

    def manifest(self) -> list[dict]:
        folder = os.path.join(self.dir.name, "imports")
        name, = os.listdir(folder)
        with open(os.path.join(folder, name), encoding="utf-8") as fh:
            return [json.loads(line) for line in fh]

    def test_move_copies_verifies_and_then_empties_the_card(self) -> None:
        names = self.card_names()
        self.imported(["FX9_0001.MXF"])            # already there (same content): compared, then deleted too
        state = self.move()
        self.assertEqual((state["state"], state["mode"], state["deleted"], state["kept"]), ("done", "move", 6, 0))
        target = os.path.join(self.project, "Klip", "FX9")
        self.assertEqual(sorted(os.listdir(target)), names)
        self.assertEqual(os.listdir(self.clip_dir()), [])                     # the clips left the card …
        self.assertTrue(os.path.exists(os.path.join(self.card_root, "XDROOT", "MEDIAPRO.XML")))   # … nothing else
        self.assertEqual(self.flushed, [target])                              # flushed before deleting
        records = self.manifest()
        self.assertEqual(records[0]["mode"], "move")
        verified = [r for r in records if r.get("verified")]
        deleted = [r["deleted"] for r in records if "deleted" in r and "end" not in r]
        self.assertEqual(sorted(r["file"] for r in verified), names)
        clip = next(r for r in verified if r["file"] == "FX9_0002.MXF")
        self.assertEqual((clip["sha1"], clip["copied"]), (hashlib.sha1(self.clips["FX9_0002.MXF"]).hexdigest(), True))
        compared = next(r for r in verified if r["file"] == "FX9_0001.MXF")
        self.assertEqual((compared["sha1"], compared["copied"]),
                         (hashlib.sha1(self.clips["FX9_0001.MXF"]).hexdigest(), False))
        self.assertEqual(sorted(deleted), names)
        self.assertLess(records.index(verified[-1]), min(i for i, r in enumerate(records) if "deleted" in r))
        self.assertEqual(records[-1]["end"], "done")
        card = self.importer.cards()[0]
        self.assertEqual((card["files"], card["clips"]), (0, 0))              # listed again: empty now
        self.assertEqual(self.importer.history()[0]["mode"], "move")
        note = [d for t, d in self.drain() if t == "notify"][-1]
        self.assertEqual(note["title"], "Kortet er flyttet")

    def test_nothing_is_deleted_when_a_copy_does_not_verify(self) -> None:
        self.importer = self.make(flush_volume=self.flush, hash_check=lambda path, step: "0" * 40)
        state = self.move()
        self.assertEqual(state["state"], "failed")
        self.assertEqual(self.card_names(), sorted(self.card_names()))
        self.assertEqual(len(self.card_names()), 6)                          # the card is untouched
        self.assertEqual(self.flushed, [])
        note = [d for t, d in self.drain() if t == "notify"][-1]
        self.assertIn("Intet er slettet fra kortet", note["text"])

    def test_nothing_is_deleted_when_a_file_already_there_differs(self) -> None:
        folder = self.imported(["FX9_0002R01.BIM"])
        with open(os.path.join(folder, "FX9_0002R01.BIM"), "r+b") as fh:   # same name and size, other bytes
            fh.write(b"X")
        state = self.move()
        self.assertEqual(state["state"], "failed")
        self.assertIn("ikke identisk med kortet", state["error"])
        self.assertEqual(len(self.card_names()), 6)

    def test_a_card_that_reads_differently_twice_stops_everything(self) -> None:
        # The first read (copy + hash) and the copy agree, but a second read of the card does
        # not: a flaky card or reader. The copy may hold bad data, so nothing leaves the card.
        flaky = lambda path, step: "f" * 40 if path.startswith(self.card_root) else None  # noqa: E731
        self.importer = self.make(flush_volume=self.flush, hash_check=flaky)
        state = self.move()
        self.assertEqual((state["state"], state["deleted"]), ("failed", 0))
        self.assertIn("læst forskelligt to gange fra kortet", state["error"])
        self.assertEqual(len(self.card_names()), 6)
        self.assertEqual(os.listdir(os.path.join(self.project, "Klip", "FX9")), [])

    def test_stopping_before_the_deletion_keeps_the_card(self) -> None:
        self.before_delete = self.importer.cancel
        state = self.move()
        self.assertEqual((state["state"], state["deleted"]), ("cancelled", 0))
        self.assertEqual(len(self.card_names()), 6)
        self.assertEqual(len(os.listdir(os.path.join(self.project, "Klip", "FX9"))), 6)   # copies stay

    def test_a_card_file_changed_after_copying_is_not_deleted(self) -> None:
        first = os.path.join(self.clip_dir(), "FX9_0001.MXF")
        self.before_delete = lambda: os.utime(first, (1_800_000_000, 1_800_000_000))
        state = self.move()
        self.assertEqual((state["state"], state["deleted"]), ("failed", 0))
        self.assertIn("er ændret på kortet", state["error"])
        self.assertEqual(len(self.card_names()), 6)

    def test_a_copy_that_vanished_stops_the_deletion(self) -> None:
        target = os.path.join(self.project, "Klip", "FX9")
        self.before_delete = lambda: os.remove(os.path.join(target, "FX9_0001.MXF"))
        state = self.move()
        self.assertEqual((state["state"], state["deleted"]), ("failed", 0))
        self.assertEqual(len(self.card_names()), 6)

    def test_flush_volume_cache_is_best_effort(self) -> None:
        self.assertFalse(importer.flush_volume_cache("\\\\STUDIO-PC\\Kunder"))
        self.assertIsInstance(importer.flush_volume_cache(self.dir.name), bool)


if __name__ == "__main__":
    unittest.main()
