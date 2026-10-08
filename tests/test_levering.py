"""The delivery party (SPEC §22.4): render events, the Final folder watched through a fake
lister (stable files, batches, dedupe, the render's own files), the demo, the ring and the cat
(cache, fetch, validation, failure memory) – nothing is ever downloaded and no sound is made."""

import ntpath
import os
import queue
import tempfile
import time
import unittest
from unittest import mock

from projektsog import levering
from projektsog.config import Config
from projektsog.events import EventBus

_saved_env: dict[str, str | None] = {}
_tmp: tempfile.TemporaryDirectory | None = None


def setUpModule() -> None:
    global _tmp
    _tmp = tempfile.TemporaryDirectory()
    _saved_env["LOCALAPPDATA"] = os.environ.get("LOCALAPPDATA")
    os.environ["LOCALAPPDATA"] = _tmp.name


def tearDownModule() -> None:
    if _saved_env.get("LOCALAPPDATA") is None:
        os.environ.pop("LOCALAPPDATA", None)
    else:
        os.environ["LOCALAPPDATA"] = _saved_env["LOCALAPPDATA"]
    _tmp.cleanup()


PROJECT = "D:\\Kunder\\Rikke Lindholm"
FINAL = PROJECT + "\\Final"
T0 = 1_700_000_000.0
GIF = b"GIF89a" + b"\x01\x00\x01\x00\x80\x00\x00" + b"\x00" * 30


class Clock:
    def __init__(self, t: float = T0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def item(path: str, *, online: bool = True) -> dict:
    """The bridge's primary as the real Item has it (SPEC §7.1): online is the source's."""
    return {"id": 7, "kind": "project", "name": ntpath.basename(path), "path": path, "open_path": path,
            "unc_path": None, "rel_path": ntpath.basename(path),
            "source": {"id": 1, "name": "Kunder", "kind": "local", "online": online, "volume_present": online},
            "match": "name"}


class FakeBridge:
    def __init__(self) -> None:
        self.connected = True
        self.primary: dict | None = item(PROJECT)
        self.project = "Rikke Lindholm - Testimonial"
        self.render: dict = {"aktiv": False, "faerdig": None}

    def state(self) -> dict:
        return {"connected": self.connected, "project": self.project if self.connected else None,
                "primary": self.primary if self.connected else None}

    def render_state(self) -> dict:
        return self.render


class FakeDisk:
    """Folders → {name: (size, mtime) | "dir"}, listed lazily like list_dir (``read`` counts the
    entries handed out); a missing folder raises FileNotFoundError on the first entry asked for."""

    def __init__(self) -> None:
        self.folders: dict[str, dict] = {PROJECT: {"Final": "dir", "Klip": "dir"}, FINAL: {}}
        self.listed: list[str] = []
        self.read = 0
        self.error: OSError | None = None
        self.hidden: set[str] = set()
        self.dir_mtimes: dict[str, float] = {}                  # folder path → its mtime (else 0)

    def add(self, path: str, size: int, mtime: float) -> None:
        folder, name = ntpath.split(path)
        self.folders.setdefault(folder, {})[name] = (size, mtime)
        parent, child = ntpath.split(folder)
        if parent in self.folders and child not in self.folders[parent]:
            self.folders[parent][child] = "dir"

    def remove(self, path: str) -> None:
        folder, name = ntpath.split(path)
        del self.folders[folder][name]

    def __call__(self, path: str):
        self.listed.append(path)
        return self._entries(path)

    def _entries(self, path: str):
        if self.error is not None:
            raise self.error
        if path not in self.folders:
            raise FileNotFoundError(path)
        for name, value in self.folders[path].items():
            attributes = levering.FILE_ATTRIBUTE_HIDDEN if name in self.hidden else 0
            self.read += 1
            if value == "dir":
                yield name, True, 0, self.dir_mtimes.get(ntpath.join(path, name), 0.0), attributes
            else:
                yield name, False, value[0], value[1], attributes


class Harness:
    def __init__(self, test: unittest.TestCase, *, settings: dict | None = None, poll_s: float = 3600.0,
                 backoff_s: float = levering.BACKOFF_S) -> None:
        tmp = tempfile.TemporaryDirectory()
        test.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.cfg = Config(path=os.path.join(self.dir, "config.json"))
        self.cfg.update({"widget_enabled": True, **(settings or {})})
        self.bus = EventBus()
        self.events = self.bus.subscribe()
        self.bridge = FakeBridge()
        self.disk = FakeDisk()
        self.clock = Clock()
        self.rings: list[bool] = []
        self.is_shown = True
        self.fetched: list[tuple] = []
        self.fetch_result: object = GIF
        self.calls: list[str] = []
        self.call_status = "ok"
        self.call_takes = 0.0                                   # how long a listing "takes" (the timer)
        self.timer = Clock(0.0)
        self.lev = levering.Levering(self.cfg, self.bus, bridge=self.bridge, shown=lambda: self.is_shown,
                                     ring=self.rings.append, fetch=self._fetch, data_dir=self.dir, clock=self.clock,
                                     lister=self.disk, call_with_timeout=self._call, poll_s=poll_s,
                                     backoff_s=backoff_s, timer=self.timer)

    def _fetch(self, url: str, timeout: float, max_bytes: int) -> bytes:
        self.fetched.append((url, timeout, max_bytes))
        if isinstance(self.fetch_result, BaseException):
            raise self.fetch_result
        return self.fetch_result

    def _call(self, key: str, fn, timeout: float):
        self.calls.append(key)
        self.timer.t += self.call_takes
        if self.call_status != "ok":
            return self.call_status, None
        return "ok", fn()

    def published(self) -> list:
        out = []
        while True:
            try:
                kind, data, _ts = self.events.get_nowait()
            except queue.Empty:
                return out
            if kind == "levering":
                out.append(data)

    def poll(self, seconds: float = 5.0):
        self.clock.t += seconds
        return self.lev.poll()


def done(seq: int, *, udfald: str = "done", levering_: bool = True, fil: str | None = "Film_v3.mp4",
         sti: str | None = FINAL + "\\Film_v3.mp4", mappe: str | None = FINAL) -> dict:
    return {"aktiv": False, "pct": None, "eta_s": None, "navn": "Film_v3", "tidslinje": "Film", "projekt": "Rikke",
            "af_claude": None, "faerdig": {"udfald": udfald, "fil": fil, "sti": sti, "mappe": mappe,
                                            "levering": levering_, "fejl": None, "seq": seq}}


# --------------------------------------------------------------------------------------
# Renders
# --------------------------------------------------------------------------------------

class RenderTests(unittest.TestCase):
    def test_a_render_into_final_is_a_delivery(self) -> None:
        h = Harness(self)
        self.assertIsNone(h.lev.on_render({"aktiv": True, "pct": 10, "faerdig": None}))
        event = h.lev.on_render(done(1))
        expected = {"kilde": "render", "fil": "Film_v3.mp4", "projekt": "Rikke", "sti": FINAL + "\\Film_v3.mp4",
                    "demo": False}
        self.assertEqual(event, expected)
        self.assertEqual(h.published(), [expected])
        self.assertEqual(h.rings, [True])                      # Klippe's ring, once
        # The bridge keeps the last "faerdig" in its state: it is not a new render.
        self.assertIsNone(h.lev.on_render(done(1)))
        self.assertIsNone(h.lev.on_render({**done(1), "aktiv": True}))
        self.assertEqual((h.published(), h.rings), ([], [True]))

    def test_a_done_render_elsewhere_rings_only(self) -> None:
        h = Harness(self)
        self.assertIsNone(h.lev.on_render(done(1, levering_=False, sti="D:\\Eksport\\x.mp4", mappe="D:\\Eksport")))
        self.assertEqual((h.published(), h.rings), ([], [True]))
        self.assertIsNone(h.lev.on_render(done(2, udfald="failed")))
        self.assertIsNone(h.lev.on_render(done(3, udfald="cancelled")))
        self.assertEqual(h.rings, [True])

    def test_settings_and_a_hidden_klippe(self) -> None:
        h = Harness(self, settings={"widget_levering": False})
        self.assertIsNone(h.lev.on_render(done(1)))
        self.assertEqual((h.published(), h.rings), ([], [True]))   # still "the render is done"
        h.cfg.update({"widget_levering": True})
        h.is_shown = False
        self.assertIsNotNone(h.lev.on_render(done(2, sti=FINAL + "\\Andet.mp4")))
        self.assertEqual(h.rings, [True])                      # no sound while Klippe is not shown

    def test_the_same_file_never_twice(self) -> None:
        h = Harness(self)
        self.assertIsNotNone(h.lev.on_render(done(1)))
        h.clock.t += 120
        self.assertIsNone(h.lev.on_render(done(2)))             # rendered again over the same file
        h.clock.t += levering.DEDUPE_S
        self.assertIsNotNone(h.lev.on_render(done(3)))
        self.assertEqual(len(h.published()), 2)

    def test_a_render_that_ended_before_we_started_is_not_replayed(self) -> None:
        h = Harness(self)
        h.bridge.render = done(4)
        h.lev.start()
        self.addCleanup(h.lev.close)
        self.assertIsNone(h.lev.on_render(done(4)))
        self.assertIsNotNone(h.lev.on_render(done(5)))

    def test_render_events_arrive_through_the_bus(self) -> None:
        h = Harness(self)
        h.lev.start()
        self.addCleanup(h.lev.close)
        h.bus.publish("render", {"aktiv": True, "faerdig": None})
        h.bus.publish("render", done(1))
        self.assertTrue(_wait(lambda: h.rings == [True]))
        h.lev.close()
        self.assertTrue(_wait(lambda: not h.lev._thread.is_alive()))


# --------------------------------------------------------------------------------------
# The Final folder
# --------------------------------------------------------------------------------------

class FinalTests(unittest.TestCase):
    def test_a_new_file_counts_once_it_stands_still(self) -> None:
        h = Harness(self)
        h.disk.add(FINAL + "\\Gammel.mp4", 500, T0 - 86400)
        self.assertIsNone(h.poll())                             # the baseline
        h.disk.add(FINAL + "\\Film.mp4", 100, T0 + 6)
        self.assertIsNone(h.poll())
        h.disk.add(FINAL + "\\Film.mp4", 900, T0 + 11)          # still being written
        self.assertIsNone(h.poll())
        event = h.poll()                                        # the same size and time twice
        expected = {"kilde": "fil", "fil": "Film.mp4", "projekt": "Rikke Lindholm - Testimonial",
                    "sti": FINAL + "\\Film.mp4", "demo": False}
        self.assertEqual(event, expected)
        self.assertEqual(h.published(), [expected])
        self.assertEqual(h.rings, [True])
        for _ in range(3):
            self.assertIsNone(h.poll())
        self.assertEqual(h.published(), [])
        self.assertTrue(all(key == "levering:D:" for key in h.calls))

    def test_what_never_counts(self) -> None:
        h = Harness(self)
        h.poll()
        for name in ("Film.mp4.tmp", "Film.part", "~$Notat.docx", ".DS_Store", "Thumbs.db", "desktop.ini",
                     "Skjult.mp4", "Tom.mp4", "Kopi af gammel.mp4"):
            h.disk.add(FINAL + "\\" + name, 0 if name == "Tom.mp4" else 100, T0 - 3600 if "gammel" in name else T0 + 3)
        h.disk.hidden.add("Skjult.mp4")
        h.disk.add(FINAL + "\\Ny\\Dybere\\Film.mp4", 100, T0 + 3)
        for _ in range(3):
            self.assertIsNone(h.poll())
        self.assertEqual(h.published(), [])
        self.assertNotIn(FINAL + "\\Ny\\Dybere", h.disk.listed)   # Final and one level of subfolders

    def test_a_subfolder_counts_and_a_batch_is_one_party(self) -> None:
        h = Harness(self)
        h.poll()
        h.disk.add(FINAL + "\\Web\\Film_web.mp4", 300, T0 + 2)
        h.disk.add(FINAL + "\\Web\\Still_1.png", 20, T0 + 2)
        h.disk.add(FINAL + "\\Web\\Still_2.png", 20, T0 + 2)
        h.poll()
        event = h.poll()
        self.assertEqual(event["fil"], "Film_web.mp4")          # the film, not its stills
        self.assertEqual(len(h.published()), 1)
        h.disk.add(FINAL + "\\Web\\Still_3.png", 20, T0 + 12)   # more of the same batch
        h.poll()
        self.assertIsNone(h.poll())
        h.clock.t += levering.QUIET_S
        h.disk.add(FINAL + "\\Web\\Film_v2.mp4", 400, h.clock.t)
        h.poll()
        self.assertEqual(h.poll()["fil"], "Film_v2.mp4")
        self.assertEqual(h.rings, [True, True])

    def test_a_file_written_again_within_ten_minutes(self) -> None:
        h = Harness(self)
        h.poll()
        h.disk.add(FINAL + "\\Film.mp4", 100, T0 + 1)
        h.poll()
        self.assertIsNotNone(h.poll())
        h.disk.remove(FINAL + "\\Film.mp4")
        h.poll()
        h.disk.add(FINAL + "\\Film.mp4", 120, h.clock.t)
        h.poll()
        self.assertIsNone(h.poll())                             # the same path within 10 minutes
        h.clock.t += levering.DEDUPE_S
        h.disk.add(FINAL + "\\Film.mp4", 130, h.clock.t)        # exported again much later
        h.poll()
        self.assertIsNotNone(h.poll())

    def test_the_files_of_a_render_are_its_own(self) -> None:
        h = Harness(self)
        h.poll()
        h.lev.on_render({"aktiv": True, "faerdig": None})
        h.disk.add(FINAL + "\\Film_v3.mp4", 100, h.clock.t + 1)
        h.poll()
        h.disk.add(FINAL + "\\Film_v3.mp4", 200, h.clock.t + 1)
        h.poll()
        self.assertIsNone(h.poll())                             # it stands still while Resolve renders on
        h.clock.t += 2
        h.lev.on_render(done(1, sti="\\\\SERVER\\Kunder\\Rikke Lindholm\\Final\\Film_v3.mp4",
                             mappe="\\\\SERVER\\Kunder\\Rikke Lindholm\\Final"))   # through the share
        h.disk.add(FINAL + "\\Film_v3.mp4", 300, h.clock.t)
        h.poll()
        self.assertIsNone(h.poll())
        self.assertEqual([e["kilde"] for e in h.published()], ["render"])
        # A render that came and went between two of the bridge's looks: its file is still its own.
        h.clock.t += 600
        h.disk.add(FINAL + "\\Kort.mp4", 50, h.clock.t)
        h.lev.on_render(done(2, fil="Kort.mp4", sti=None, mappe=FINAL, levering_=False))
        h.poll()
        self.assertIsNone(h.poll())
        self.assertEqual(h.published(), [])

    def test_a_render_elsewhere_does_not_hide_a_file_in_final(self) -> None:
        h = Harness(self)
        h.poll()
        h.lev.on_render({"aktiv": True, "faerdig": None})
        h.clock.t += 10
        h.lev.on_render(done(1, levering_=False, sti="E:\\Eksport\\Proxy.mov", mappe="E:\\Eksport"))
        h.disk.add(FINAL + "\\Film.mp4", 100, h.clock.t)
        h.poll()
        self.assertEqual(h.poll()["kilde"], "fil")

    def test_only_while_resolve_has_the_project(self) -> None:
        h = Harness(self)
        h.bridge.connected = False
        self.assertIsNone(h.poll())
        h.bridge.connected = True
        h.bridge.primary = None
        self.assertIsNone(h.poll())
        h.bridge.primary = item(PROJECT, online=False)         # the real Item: source.online
        self.assertIsNone(h.poll())
        h.bridge.primary = {"path": PROJECT, "online": False}  # (a bare top-level online, too)
        self.assertIsNone(h.poll())
        h.bridge.primary = {**item(PROJECT), "source": {"id": 1}, "online": False}
        self.assertIsNone(h.poll())
        h.cfg.update({"widget_levering": False})
        h.bridge.primary = item(PROJECT)
        self.assertIsNone(h.poll())
        self.assertEqual(h.calls, [])
        h.cfg.update({"widget_levering": True, "widget_enabled": False})
        self.assertIsNone(h.poll())
        self.assertEqual(h.calls, [])
        h.cfg.update({"widget_enabled": True})
        h.bridge.primary = {**item(PROJECT), "online": False}  # source.online wins over a stray key
        self.assertIsNone(h.poll())
        self.assertEqual(h.calls, ["levering:D:"])

    def test_an_offline_project_folder_pauses_and_starts_afresh(self) -> None:
        h = Harness(self)
        h.poll()                                                # the baseline
        h.bridge.primary = item(PROJECT, online=False)         # the computer that holds it is off …
        h.disk.add(FINAL + "\\Imens.mp4", 100, h.clock.t)
        self.assertIsNone(h.poll())
        self.assertIsNone(h.poll(levering.PAUSE_RESET_S))
        self.assertEqual(h.calls, ["levering:D:"])              # … and is not listed meanwhile
        self.assertIsNone(h.lev._watch)                         # the pause became a reset
        h.bridge.primary = item(PROJECT)                        # back: what came meanwhile is the baseline
        self.assertIsNone(h.poll())
        self.assertIsNone(h.poll())
        self.assertIsNone(h.poll())
        self.assertEqual(h.published(), [])
        self.assertEqual(len(h.calls), 4)

    def test_a_new_project_or_a_long_pause_starts_afresh(self) -> None:
        h = Harness(self)
        h.poll()
        other = "E:\\Kunder\\Anden kunde"
        h.disk.folders[other + "\\Final"] = {"Gammel levering.mp4": (100, T0 + 7)}
        h.bridge.primary = item(other)
        h.poll()                                                # a baseline for the other project
        self.assertIsNone(h.poll())
        self.assertEqual(h.calls[-1], "levering:E:")
        h.bridge.primary = item(PROJECT)
        h.poll()
        h.bridge.connected = False                              # Resolve closed for a while …
        h.poll()
        h.disk.add(FINAL + "\\Imens.mp4", 100, h.clock.t)
        h.poll(levering.PAUSE_RESET_S)
        h.bridge.connected = True                               # … what came meanwhile is the baseline
        h.poll()
        self.assertIsNone(h.poll())
        self.assertEqual(h.published(), [])

    def test_unreachable_and_missing_folders(self) -> None:
        h = Harness(self)
        h.disk.error = OSError("the share is gone")
        self.assertIsNone(h.poll())
        self.assertIsNone(levering.scan_final(PROJECT, h.disk))
        h.disk.error = None
        del h.disk.folders[FINAL]
        self.assertEqual(levering.scan_final(PROJECT, h.disk), ({}, True))
        self.assertEqual(levering.scan_final("Q:\\Findes ikke", h.disk), None)
        h.poll()                                                # the baseline: no Final yet
        h.disk.folders[FINAL] = {}
        h.disk.add(FINAL + "\\Film.mp4", 100, h.clock.t)
        h.poll()
        self.assertIsNotNone(h.poll())
        h.call_status = "timeout"
        h.clock.t += levering.QUIET_S                           # (not part of the first one's batch)
        h.disk.add(FINAL + "\\Film2.mp4", 100, h.clock.t)
        self.assertIsNone(h.poll())
        self.assertIsNone(h.poll())
        h.call_status = "ok"
        self.assertIsNone(h.poll())                             # seen once …
        self.assertIsNotNone(h.poll())                          # … and still: counted

    def test_listing_helpers(self) -> None:
        for name, attributes, expected in (("Film.mp4", 0, False), ("film.MP4.TMP", 0, True), ("~$x.docx", 0, True),
                                           (".skjult", 0, True), ("Film.mp4", levering.FILE_ATTRIBUTE_HIDDEN, True),
                                           ("Film.mp4", levering.FILE_ATTRIBUTE_SYSTEM, True),
                                           ("Desktop.ini", 0, True), ("Film.crdownload", 0, True)):
            with self.subTest(name=name, attributes=attributes):
                self.assertEqual(levering.ignored(name, attributes), expected)
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "a.mp4"), "wb") as fh:
                fh.write(b"12345")
            os.mkdir(os.path.join(tmp, "Web"))
            listing = {name: (is_dir, size) for name, is_dir, size, _mtime, _attributes in levering.list_dir(tmp)}
            self.assertEqual(listing["a.mp4"], (False, 5))
            self.assertTrue(listing["Web"][0])
        long_path = "D:\\" + "x" * 300
        self.assertTrue(levering._long(long_path).startswith("\\\\?\\D:\\"))
        self.assertTrue(levering._long("\\\\SERVER\\" + "y" * 300).startswith("\\\\?\\UNC\\SERVER\\"))
        self.assertEqual(levering._long(FINAL), FINAL)


# --------------------------------------------------------------------------------------
# Big Final folders: the budget while listing, and the slower looks after a heavy one
# --------------------------------------------------------------------------------------

EXR = FINAL + "\\Film_EXR"
WEB = FINAL + "\\Web"


def _frames(n: int, mtime: float = T0 - 3600) -> dict:
    return {f"Film.{i:06d}.exr": (12_000_000, mtime) for i in range(n)}


class BudgetTests(unittest.TestCase):
    def test_a_huge_subfolder_is_not_read_in_full(self) -> None:
        disk = FakeDisk()
        disk.folders[FINAL] = {"Film.mp4": (900, T0), "Film_EXR": "dir", "Web": "dir", "Arkiv": "dir"}
        disk.folders[EXR] = _frames(60_000)
        disk.folders[WEB] = {"Film_web.mp4": (300, T0)}
        disk.folders[FINAL + "\\Arkiv"] = {"Gammel.mp4": (100, T0 - 86400)}
        disk.dir_mtimes.update({WEB: T0, EXR: T0 - 3600, FINAL + "\\Arkiv": T0 - 86400})
        files, complete = levering.scan_final(PROJECT, disk)
        self.assertFalse(complete)
        self.assertLessEqual(disk.read, levering.MAX_ENTRIES + 1)        # not 60,000 entries
        self.assertLessEqual(len(files), levering.MAX_ENTRIES)
        self.assertIn(FINAL + "\\Film.mp4", files)                       # Final's own files first
        self.assertIn(WEB + "\\Film_web.mp4", files)                     # then the newest folder
        self.assertEqual([p for p in disk.listed if p.startswith(FINAL + "\\")], [WEB, EXR])
        self.assertNotIn(FINAL + "\\Arkiv", disk.listed)                 # the budget was spent

    def test_a_huge_final_folder_is_not_read_in_full(self) -> None:
        disk = FakeDisk()
        disk.folders[FINAL] = {**_frames(10_000), "Web": "dir"}
        disk.folders[WEB] = {"Film_web.mp4": (300, T0)}
        files, complete = levering.scan_final(PROJECT, disk)
        self.assertEqual((len(files), complete), (levering.MAX_ENTRIES, False))
        self.assertEqual(disk.read, levering.MAX_ENTRIES + 1)
        self.assertNotIn(WEB, disk.listed)

    def test_the_budget_is_shared_and_exact(self) -> None:
        disk = FakeDisk()
        disk.folders[FINAL] = {"Film.mp4": (900, T0), "Web": "dir"}
        disk.folders[WEB] = {"Film_web.mp4": (300, T0)}
        self.assertEqual(levering.scan_final(PROJECT, disk, budget=3),
                         ({FINAL + "\\Film.mp4": (900, T0), WEB + "\\Film_web.mp4": (300, T0)}, True))
        disk.folders[WEB]["Still.png"] = (20, T0)
        files, complete = levering.scan_final(PROJECT, disk, budget=3)
        self.assertEqual((len(files), complete), (2, False))
        disk.folders[FINAL]["Andet"] = "dir"
        disk.folders[FINAL + "\\Andet"] = {}
        self.assertEqual(levering.scan_final(PROJECT, disk, budget=2)[1], False)   # no budget left for them

    def test_a_listing_that_fails_midway(self) -> None:
        broken = {WEB}

        def lister(path: str):
            if path == FINAL:
                yield "Film.mp4", False, 900, T0, 0
                if FINAL in broken:
                    raise OSError("the share went away")
                yield "Web", True, 0, T0, 0
            elif path == WEB:
                yield "Film_web.mp4", False, 300, T0, 0
                raise OSError("the share went away")
            else:
                raise FileNotFoundError(path)

        self.assertEqual(levering.scan_final(PROJECT, lister), ({FINAL + "\\Film.mp4": (900, T0)}, False))
        broken.add(FINAL)
        self.assertIsNone(levering.scan_final(PROJECT, lister))         # Final itself: unreachable

    def test_a_listing_is_closed_when_the_budget_is_spent(self) -> None:
        closed: list[str] = []

        def lister(path: str):
            try:
                for i in range(100):
                    yield f"f{i:02d}.mp4", False, 1, T0, 0
            finally:
                closed.append(path)

        entries, whole = levering._read(lister, FINAL, 5)
        self.assertEqual((len(entries), whole, closed), (5, False, [FINAL]))
        self.assertEqual(levering._read(lister, FINAL, 100)[1], True)
        with tempfile.TemporaryDirectory() as tmp:                      # the real, lazy lister
            final = os.path.join(tmp, "Final")
            os.mkdir(final)
            for i in range(20):
                with open(os.path.join(final, f"f{i:02d}.mp4"), "wb") as fh:
                    fh.write(b"x")
            files, complete = levering.scan_final(tmp, budget=5)
            self.assertEqual((len(files), complete), (5, False))
            self.assertEqual((len(levering.scan_final(tmp)[0]), levering.scan_final(tmp)[1]), (20, True))

    def test_a_heavy_look_backs_off(self) -> None:
        h = Harness(self, poll_s=5.0)
        self.assertEqual(h.lev.next_poll_s(), 5.0)
        h.poll()
        self.assertEqual(h.lev.next_poll_s(), 5.0)                       # a light look
        stills = {f"Still_{i:05d}.png": (20, T0 - 3600) for i in range(levering.BIG_LISTING)}
        h.disk.folders[FINAL] = dict(stills)
        h.poll()
        self.assertEqual(h.lev.next_poll_s(), levering.BACKOFF_S)        # big
        h.disk.folders[FINAL] = {"Film_EXR": "dir"}
        h.disk.folders[EXR] = _frames(60_000)
        h.poll()
        self.assertEqual(h.lev.next_poll_s(), levering.BACKOFF_S)        # incomplete
        del h.disk.folders[EXR]
        h.poll()
        self.assertEqual(h.lev.next_poll_s(), levering.BACKOFF_S)        # a folder that could not be read
        h.disk.folders[FINAL] = {"Film.mp4": (100, T0 - 3600)}
        h.poll()
        self.assertEqual(h.lev.next_poll_s(), 5.0)                       # light again
        for status in ("timeout", "busy", "error"):
            with self.subTest(status=status):
                h.call_status = status
                h.poll()
                self.assertEqual(h.lev.next_poll_s(), levering.BACKOFF_S)
                h.call_status = "ok"
                h.poll()
                self.assertEqual(h.lev.next_poll_s(), 5.0)
        h.call_takes = levering.SLOW_S
        h.poll()
        self.assertEqual(h.lev.next_poll_s(), levering.BACKOFF_S)        # slow
        h.bridge.connected = False
        h.poll()
        self.assertEqual(h.lev.next_poll_s(), 5.0)                       # no look: nothing heavy
        rare = Harness(self, poll_s=3600.0)
        rare.lev._backoff = True
        self.assertEqual(rare.lev.next_poll_s(), 3600.0)                 # never sooner than poll_s

    def test_a_new_delivery_is_found_next_to_a_huge_sequence(self) -> None:
        h = Harness(self, poll_s=5.0)
        h.disk.folders[FINAL] = {"Film_EXR": "dir"}
        h.disk.folders[EXR] = _frames(60_000)
        h.disk.dir_mtimes[EXR] = T0 - 3600
        self.assertIsNone(h.poll())                                      # the baseline (incomplete)
        h.disk.folders[FINAL]["Web"] = "dir"
        h.disk.folders[WEB] = {"Film_web.mp4": (300, h.clock.t)}
        h.disk.dir_mtimes[WEB] = h.clock.t
        h.disk.read = 0
        self.assertIsNone(h.poll(levering.BACKOFF_S))
        self.assertLessEqual(h.disk.read, levering.MAX_ENTRIES + 1)
        event = h.poll(levering.BACKOFF_S)
        self.assertEqual((event["fil"], event["sti"]), ("Film_web.mp4", WEB + "\\Film_web.mp4"))
        self.assertEqual(h.lev.next_poll_s(), levering.BACKOFF_S)
        self.assertIsNone(h.poll(levering.BACKOFF_S))                    # and once only
        self.assertEqual(len(h.published()), 1)

    def test_the_watching_thread_waits_longer_after_a_heavy_look(self) -> None:
        h = Harness(self, poll_s=0.02, backoff_s=3600.0)
        h.lev.start()
        self.addCleanup(h.lev.close)
        self.assertTrue(_wait(lambda: len(h.calls) >= 3))                # light looks: every poll_s
        big = _frames(levering.MAX_ENTRIES + 10)
        h.disk.folders[FINAL] = big                                      # (one assignment: no race)
        self.assertTrue(_wait(lambda: h.lev.next_poll_s() == 3600.0))
        looks = len(h.calls)
        time.sleep(0.3)                                                  # ~15 looks at the light pace
        self.assertEqual(len(h.calls), looks)


# --------------------------------------------------------------------------------------
# The demo and the cat
# --------------------------------------------------------------------------------------

class DemoTests(unittest.TestCase):
    def test_the_demo_party(self) -> None:
        h = Harness(self, settings={"widget_levering": False})
        self.assertEqual(h.lev.demo(), {"ok": True})
        self.assertEqual(h.published(), [{"kilde": "render", "fil": "Demo_levering.mp4", "projekt": "Demo",
                                          "sti": None, "demo": True}])
        self.assertEqual(h.rings, [True])
        h.cfg.update({"widget_enabled": False})
        with self.assertRaisesRegex(ValueError, "Slå Klippe til først"):
            h.lev.demo()


class CatTests(unittest.TestCase):
    def path(self, h: Harness) -> str:
        return os.path.join(h.dir, levering.FESTKAT_FILE)

    def test_fetched_once_and_kept(self) -> None:
        h = Harness(self)
        self.assertEqual(h.lev.festkat(), GIF)
        self.assertEqual(h.fetched, [(levering.FESTKAT_URL, 15.0, 3 * 1024 * 1024)])
        with open(self.path(h), "rb") as fh:
            self.assertEqual(fh.read(), GIF)
        self.assertEqual(sorted(os.listdir(h.dir)), ["config.json", "festkat.gif"])    # no temp file left
        self.assertEqual(h.lev.festkat(), GIF)
        self.assertEqual(len(h.fetched), 1)

    def test_the_users_own_cat(self) -> None:
        h = Harness(self)
        own = b"GIF87a" + b"\x02" * 40
        with open(self.path(h), "wb") as fh:
            fh.write(own)
        self.assertEqual(h.lev.festkat(), own)
        self.assertEqual(h.fetched, [])
        with open(self.path(h), "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 40)          # not a GIF: not served, not replaced
        os.utime(self.path(h), (T0, T0))
        with self.assertLogs("projektsog.levering", "WARNING"):
            self.assertIsNone(h.lev.festkat())
        self.assertIsNone(h.lev.festkat())
        self.assertEqual(h.fetched, [])
        with open(self.path(h), "rb") as fh:
            self.assertTrue(fh.read().startswith(b"\x89PNG"))

    def test_a_too_large_file_is_not_served(self) -> None:
        h = Harness(self)
        with open(self.path(h), "wb") as fh:
            fh.write(b"GIF89a" + b"\x00" * levering.FESTKAT_MAX_BYTES)
        with self.assertLogs("projektsog.levering", "WARNING"):
            self.assertIsNone(h.lev.festkat())

    def test_a_failure_is_remembered_for_ten_minutes(self) -> None:
        h = Harness(self)
        h.fetch_result = OSError("no internet")
        with self.assertLogs("projektsog.levering", "WARNING"):
            self.assertIsNone(h.lev.festkat())
        self.assertIsNone(h.lev.festkat())
        self.assertEqual(len(h.fetched), 1)
        h.clock.t += levering.FESTKAT_RETRY_S
        h.fetch_result = b"<html>not a gif</html>"
        with self.assertLogs("projektsog.levering", "WARNING"):
            self.assertIsNone(h.lev.festkat())
        self.assertEqual(len(h.fetched), 2)
        self.assertFalse(os.path.exists(self.path(h)))
        h.clock.t += levering.FESTKAT_RETRY_S
        h.fetch_result = GIF
        self.assertEqual(h.lev.festkat(), GIF)

    def test_download_limits(self) -> None:
        # urllib is replaced: nothing goes out.
        class Response:
            def __init__(self, body: bytes, length: str | None) -> None:
                self.body, self.headers = body, {"Content-Length": length} if length else {}

            def __enter__(self):
                return self

            def __exit__(self, *exc) -> None:
                pass

            def read(self, n: int) -> bytes:
                chunk, self.body = self.body[:n], self.body[n:]
                return chunk

        seen: list = []

        def urlopen(request, timeout):
            seen.append((request.full_url, request.get_header("User-agent"), timeout))
            return responses.pop(0)

        responses = [Response(GIF, str(len(GIF))), Response(b"x" * 10, "999999"), Response(b"x" * 200, None)]
        with mock.patch.object(levering.urllib.request, "urlopen", urlopen):
            self.assertEqual(levering.download(levering.FESTKAT_URL, 15.0, 100), GIF)
            with self.assertRaises(ValueError):
                levering.download(levering.FESTKAT_URL, 15.0, 100)
            with self.assertRaises(ValueError):
                levering.download(levering.FESTKAT_URL, 15.0, 100)
        self.assertEqual(seen[0][0], levering.FESTKAT_URL)
        self.assertTrue(seen[0][1].startswith("Projektsog/"))
        self.assertTrue(levering.is_gif(GIF))
        self.assertFalse(levering.is_gif(b"GIF8"))
        self.assertFalse(levering.is_gif(None))


def _wait(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


if __name__ == "__main__":
    unittest.main()
