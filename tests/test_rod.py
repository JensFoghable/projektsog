"""Klippe i bad (rod.py, SPEC §23.6): loose files in the project folder's root make Klippe dirty,
each gets a suggested folder, and a tidied folder gives Klippe a bath. Only ever reads."""

import os
import tempfile
import unittest
from datetime import date

from projektsog import rod
from projektsog.config import Config
from projektsog.events import EventBus
from tests._index_store_fixtures import FILE_ATTRIBUTE_HIDDEN, set_attributes
from tests.test_petplay import FakeBus

TEMPLATE = ["Final", "Grafik", "Klip", "Logo", "Musik", "Project", "Speak", "Tekst"]
_tmp: tempfile.TemporaryDirectory | None = None


def setUpModule() -> None:
    global _tmp
    _tmp = tempfile.TemporaryDirectory()
    os.environ["LOCALAPPDATA"] = _tmp.name


def tearDownModule() -> None:
    if _tmp is not None:
        _tmp.cleanup()


class SuggestionTests(unittest.TestCase):
    def test_where_things_belong(self) -> None:
        cases = {
            "FX9_7912.MXF": ("video", "Klip", False), "Portræt_final_v3.mp4": ("video", "Final", False),
            "export.mov": ("video", "Final", False), "Speak_take2.wav": ("lyd", "Speak", False),
            "VO_02.wav": ("lyd", "Speak", False), "voxpop.wav": ("lyd", "Musik", False),
            "whoosh_sfx.wav": ("lyd", "Musik", False), "Musik.mp3": ("lyd", "Musik", False),
            "Kundelogo.png": ("billede", "Logo", False), "still_01.jpg": ("billede", "Grafik", False),
            "Plakat.psd": ("grafik", "Grafik", False), "Montserrat.ttf": ("font", "Grafik", False),
            "Manus.docx": ("tekst", "Tekst", False), "Undertekster.srt": ("tekst", "Tekst", False),
            "Rikke - Testimonial.drp": ("projekt", "Project", False), "LUT.cube": ("projekt", "Project", False),
            "pakke.zip": ("andet", None, False), "README": ("andet", None, False)}
        for name, (kind, folder, new) in cases.items():
            with self.subTest(name=name):
                s = rod.suggest(name, TEMPLATE)
                self.assertEqual((s["navn"], s["slags"], s["forslag"], s["ny"]), (name, kind, folder, new))

    def test_existing_spelling_first_else_a_new_folder(self) -> None:
        self.assertEqual(rod.suggest("a.mov", ["KLIP", "Final"])["forslag"], "KLIP")
        self.assertEqual(rod.suggest("whoosh_sfx.wav", ["SFX", "Musik"])["forslag"], "SFX")
        self.assertEqual(rod.suggest("a.ttf", ["Font", "Grafik"])["forslag"], "Font")
        self.assertEqual(rod.suggest("a.mp3", ["Music"])["forslag"], "Music")
        self.assertEqual(rod.suggest("a.png", ["Stills"])["forslag"], "Stills")
        new = rod.suggest("a.mov", ["Grafik"])
        self.assertEqual((new["forslag"], new["ny"]), ("Klip", True))
        self.assertEqual(rod.suggest("a.drp", ["Projekt"])["forslag"], "Projekt")

    def test_levels(self) -> None:
        self.assertEqual([rod.level(n) for n in (0, 1, 3, 4, 9, 10, 99)],
                         ["ren", "lidt", "lidt", "beskidt", "beskidt", "meget", "meget"])


class FakeBridge:
    def __init__(self, path: str | None, *, connected: bool = True, online: bool = True) -> None:
        self.path = path
        self.connected = connected
        self.online = online

    def state(self) -> dict:
        if self.path is None:
            return {"connected": self.connected, "primary": None}
        return {"connected": self.connected, "project": "Rikke Lindholm - Testimonial",
                "primary": {"path": self.path, "source": {"online": self.online}}}


class FakeTimer:
    def __init__(self) -> None:
        self.pending: list = []

    def __call__(self, seconds, action):
        self.pending.append((seconds, action))
        return self

    def cancel(self) -> None:
        self.pending.clear()

    def fire(self) -> None:
        _seconds, action = self.pending.pop(0)
        action()


def direct(_key, fn, _timeout):
    return "ok", fn()


class RodTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.project = os.path.join(self.dir.name, "Rikke Lindholm")
        for folder in TEMPLATE:
            os.makedirs(os.path.join(self.project, folder))
        self.cfg = Config(path=os.path.join(self.dir.name, "config.json"))
        self.cfg.update({"widget_enabled": True})
        self.bus = FakeBus()
        self.bridge = FakeBridge(self.project)
        self.baths: list[int] = []
        self.timer = FakeTimer()
        self.day = date(2026, 10, 12)
        self.rod = rod.Rod(self.cfg, self.bus, bridge=self.bridge, on_bath=lambda: self.baths.append(1),
                           call_with_timeout=direct, today=lambda: self.day, timer=self.timer)

    def put(self, *names: str) -> None:
        for name in names:
            with open(os.path.join(self.project, name), "wb") as fh:
                fh.write(b"x")

    def drop(self, *names: str) -> None:
        for name in names:
            os.remove(os.path.join(self.project, name))

    def test_a_tidy_project_is_clean(self) -> None:
        event = self.rod.look()
        self.assertEqual(event, {"aktiv": True, "projekt": "Rikke Lindholm", "mappe": self.project, "antal": 0,
                                 "niveau": "ren", "filer": [], "mere": 0, "bad": False})
        self.assertIsNone(self.rod.look())                                  # nothing new: no event
        self.assertEqual(self.rod.state()["niveau"], "ren")

    def test_loose_files_and_what_is_ignored(self) -> None:
        self.put("Interview.mxf", "logo.png", "desktop.ini", "Thumbs.db", "~$Manus.docx", "genvej.lnk",
                 "klip.mov.projektsog-tmp", "eksport.tmp", ".skjult.wav", "skjult.wav")
        set_attributes(os.path.join(self.project, "skjult.wav"), FILE_ATTRIBUTE_HIDDEN)
        event = self.rod.look()
        self.assertEqual((event["antal"], event["niveau"]), (2, "lidt"))
        self.assertEqual([(f["navn"], f["forslag"]) for f in event["filer"]],
                         [("Interview.mxf", "Klip"), ("logo.png", "Logo")])
        self.assertEqual(self.bus.events[-1], ("rod", event))
        self.assertTrue(all(os.path.exists(os.path.join(self.project, n)) for n in ("Interview.mxf", "logo.png")))

    def test_many_files_are_counted_but_50_shown(self) -> None:
        self.put(*[f"klip_{n:03d}.mov" for n in range(60)])
        event = self.rod.look()
        self.assertEqual((event["antal"], event["niveau"], len(event["filer"]), event["mere"]), (60, "meget", 50, 10))

    def test_tidied_up_a_bath_once_a_day(self) -> None:
        self.put("a.mov", "b.wav", "c.png", "d.pdf")
        self.assertEqual(self.rod.look()["niveau"], "beskidt")
        self.drop("a.mov", "b.wav", "c.png", "d.pdf")
        bath = self.rod.look()
        self.assertEqual((bath["bad"], bath["niveau"], bath["antal"]), (True, "ren", 0))
        self.assertEqual(self.baths, [1])
        self.assertFalse(self.rod.state()["bad"])                           # only in that one event
        self.put("e.mov")
        self.rod.look()
        self.drop("e.mov")
        self.assertFalse(self.rod.look()["bad"])                            # once per folder and day
        self.day = date(2026, 10, 13)
        self.put("f.mov")
        self.rod.look()
        self.drop("f.mov")
        self.assertTrue(self.rod.look()["bad"])                             # a new day, a new bath
        self.assertEqual(self.baths, [1, 1])

    def test_a_folder_tidied_while_away_still_gets_its_bath(self) -> None:
        self.put("a.mov")
        self.rod.look()
        self.bridge.path = None                                             # Resolve shows no project
        self.assertEqual(self.rod.look(), {**rod.IDLE, "bad": False})
        self.drop("a.mov")
        self.bridge.path = self.project
        self.assertTrue(self.rod.look()["bad"])

    def test_a_failed_look_keeps_the_state(self) -> None:
        self.put("a.mov")
        self.rod.look()
        self.rod._call = lambda _key, _fn, _timeout: ("timeout", None)
        self.assertIsNone(self.rod.look())
        self.assertEqual(self.rod.state()["antal"], 1)

    def test_off_offline_or_disconnected_is_idle(self) -> None:
        self.put("a.mov")
        self.rod.look()
        for change in ({"online": False}, {"connected": False}):
            with self.subTest(change=change):
                self.bridge.__dict__.update({"online": True, "connected": True, **change})
                self.assertFalse(self.rod.look()["aktiv"])
                self.bridge.__dict__.update({"online": True, "connected": True})
                self.assertTrue(self.rod.look()["aktiv"])
        self.cfg.update({"widget_bad": False})
        self.assertEqual(self.rod.look(), {**rod.IDLE, "bad": False})
        self.cfg.update({"widget_bad": True, "widget_enabled": False})
        self.assertFalse(self.rod.state()["aktiv"])

    def test_the_demo(self) -> None:
        self.put("a.mov")
        self.rod.look()
        answer = self.rod.demo()
        demo = self.bus.events[-1][1]
        self.assertEqual((answer["ok"], demo["niveau"], demo["antal"], demo["mappe"], demo["demo"]),
                         (True, "meget", 12, None, True))
        self.assertEqual(self.rod.state()["projekt"], "Demo")
        self.assertIsNone(self.rod.look())                                  # a real look waits meanwhile
        self.assertEqual(self.timer.pending[0][0], 8.0)
        self.timer.fire()
        bath, real = self.bus.events[-2][1], self.bus.events[-1][1]
        self.assertEqual((bath["bad"], bath["niveau"], bath["projekt"]), (True, "ren", "Demo"))
        self.assertEqual((real["projekt"], real["antal"], real["bad"]), ("Rikke Lindholm", 1, False))
        self.assertEqual(self.rod.state()["projekt"], "Rikke Lindholm")
        self.assertEqual(self.baths, [])                                    # a demo bath counts for nothing
        self.cfg.update({"widget_enabled": False})
        with self.assertRaisesRegex(ValueError, "Slå Klippe til først"):
            self.rod.demo()

    def test_the_thread_looks_and_stops(self) -> None:
        bus = EventBus()
        q = bus.subscribe()
        looker = rod.Rod(self.cfg, bus, bridge=self.bridge, call_with_timeout=direct, poll_s=0.05)
        original = rod.FIRST_S
        rod.FIRST_S = 0.0
        try:
            self.put("a.mov")
            looker.start()
            kind, data, _ts = q.get(timeout=5)
            while kind != "rod":
                kind, data, _ts = q.get(timeout=5)
            self.assertEqual(data["antal"], 1)
        finally:
            rod.FIRST_S = original
            looker.close()
            looker._thread.join(2)
        self.assertFalse(looker._thread.is_alive())


if __name__ == "__main__":
    unittest.main()
