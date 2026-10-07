"""The robot crew (crew.py, SPEC §21.2/§21.3): who builds in Resolve (the queue's state file),
when the robots may come out, the demo, what the widget hears and the helper's command line."""

import codecs
import json
import os
import random
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
import winreg

from projektsog import crew, petplay
from projektsog.config import Config

_tmp: tempfile.TemporaryDirectory | None = None
_saved: str | None = None


def setUpModule() -> None:
    global _tmp, _saved
    _tmp = tempfile.TemporaryDirectory()
    _saved = os.environ.get("LOCALAPPDATA")
    os.environ["LOCALAPPDATA"] = _tmp.name


def tearDownModule() -> None:
    if _saved is None:
        os.environ.pop("LOCALAPPDATA", None)
    else:
        os.environ["LOCALAPPDATA"] = _saved
    _tmp.cleanup()


HANDLER = '"C:\\Python\\pythonw.exe" "C:\\Github\\Davinci\\resolve-koe\\koe.py" klik "%1"'
LOOK = petplay.clean_look({"stage": "baby", "outfit": "none", "pet": {"x": 25, "y": 40, "w": 210, "h": 210},
                           "view": {"w": 260, "h": 448}})


# ============================================================================================
# Who builds: the queue's state file (KoeWatch)
# ============================================================================================

class HandlerTests(unittest.TestCase):
    def test_the_command_line_is_split_like_windows_does(self) -> None:
        self.assertEqual(crew.split_command_line(HANDLER),
                         ["C:\\Python\\pythonw.exe", "C:\\Github\\Davinci\\resolve-koe\\koe.py", "klik", "%1"])
        self.assertEqual(crew.split_command_line(""), [])
        self.assertEqual(crew.split_command_line('a "b c" d'), ["a", "b c", "d"])

    def test_the_state_file_sits_next_to_koe_py(self) -> None:
        self.assertEqual(crew.koe_state_path(HANDLER), "C:\\Github\\Davinci\\resolve-koe\\state\\koe.json")
        self.assertEqual(crew.koe_state_path('"C:\\Mine programmer\\Kø\\KOE.PY" "%1"'),
                         "C:\\Mine programmer\\Kø\\state\\koe.json")
        self.assertEqual(crew.koe_state_path("C:\\koe\\koe.py %1"), "C:\\koe\\state\\koe.json")
        for command in ('"C:\\Python\\pythonw.exe" "C:\\x\\other.py" "%1"', "koe.py %1", "", "   "):
            with self.subTest(command=command):
                self.assertIsNone(crew.koe_state_path(command))

    def test_the_users_own_registration_first(self) -> None:
        def registry(values):
            asked = []

            def query(root, subkey):
                asked.append((root, subkey))
                return values.get(root)
            return query, asked
        query, asked = registry({winreg.HKEY_CURRENT_USER: HANDLER, winreg.HKEY_CLASSES_ROOT: "C:\\y\\koe.py"})
        self.assertEqual(crew.find_koe_state(query), "C:\\Github\\Davinci\\resolve-koe\\state\\koe.json")
        self.assertEqual(asked, [(winreg.HKEY_CURRENT_USER, r"Software\Classes\resolvekoe\shell\open\command")])
        query, asked = registry({winreg.HKEY_CLASSES_ROOT: "C:\\y\\koe.py %1"})
        self.assertEqual(crew.find_koe_state(query), "C:\\y\\state\\koe.json")
        self.assertEqual(asked[1], (winreg.HKEY_CLASSES_ROOT, r"resolvekoe\shell\open\command"))
        query, _ = registry({winreg.HKEY_CURRENT_USER: "notepad.exe %1", winreg.HKEY_CLASSES_ROOT: "C:\\y\\koe.py"})
        self.assertEqual(crew.find_koe_state(query), "C:\\y\\state\\koe.json")
        self.assertIsNone(crew.find_koe_state(registry({})[0]))

    def test_a_missing_registry_key(self) -> None:
        missing = rf"Software\Classes\projektsog-test-{uuid.uuid4().hex}\shell\open\command"
        self.assertIsNone(crew.registry_default(winreg.HKEY_CURRENT_USER, missing))   # (read only)

    def test_processes(self) -> None:
        self.assertTrue(crew.pid_alive(os.getpid()))
        proc = subprocess.Popen([sys.executable, "-c", "pass"], creationflags=subprocess.CREATE_NO_WINDOW)
        proc.wait(30)
        self.assertFalse(crew.pid_alive(proc.pid))
        for pid in (0, -1, 2 ** 40):
            self.assertFalse(crew.pid_alive(pid))


class StateFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "state", "koe.json")
        os.makedirs(os.path.dirname(self.path))
        self.t = 1_700_000_000.0
        self.alive_asked: list[int] = []
        self.dead: set[int] = set()
        self.reads = 0
        self.lookups = 0
        self.found: str | None = self.path

    def write(self, holder, raw: str | None = None) -> None:
        temp = self.path + ".tmp"
        with open(temp, "w", encoding="utf-8") as fh:
            fh.write(raw if raw is not None else json.dumps({"aaben": False, "holder": holder, "koe": []}))
        os.replace(temp, self.path)

    def holder(self, **changes):
        return {"navn": "Mette", "projekt": "Rikke Lindholm", "opgave": "byg 3 klip", "pid": "4321",
                "siden": self.t - 600, "opdateret": self.t - 30, **changes}

    def watch(self) -> crew.KoeWatch:
        def find():
            self.lookups += 1
            return self.found

        def read(path):
            self.reads += 1
            return crew.read_koe_state(path)

        def alive(pid):
            self.alive_asked.append(pid)
            return pid not in self.dead
        return crew.KoeWatch(path_finder=find, reader=read, alive=alive, clock=lambda: self.t)

    def test_a_build(self) -> None:
        self.write(self.holder())
        self.assertEqual(self.watch().current(), {"navn": "Mette", "projekt": "Rikke Lindholm",
                                                  "opgave": "byg 3 klip", "siden": self.t - 600})
        self.assertEqual(self.alive_asked, [4321])               # the pid may come as a string

    def test_what_is_not_a_build(self) -> None:
        cases = {
            "nobody": None,
            "no name": self.holder(navn=None),
            "a name that is not text": self.holder(navn=7),
            "not heard from in an hour": self.holder(opdateret=self.t - 3601),
            "no time": self.holder(opdateret="i går"),
        }
        for name, holder in cases.items():
            with self.subTest(name):
                self.write(holder)
                self.assertIsNone(self.watch().current())
        self.dead.add(4321)
        self.write(self.holder())
        self.assertIsNone(self.watch().current())                # its session has ended
        self.write(self.holder(pid=None, opdateret=self.t - 3599))
        self.assertEqual(self.watch().current()["navn"], "Mette")   # no pid: alive

    def test_read_every_two_seconds(self) -> None:
        self.write(self.holder())
        watch = self.watch()
        watch.current()
        self.t += 1.5
        self.assertEqual(watch.current()["navn"], "Mette")
        self.assertEqual(self.reads, 1)
        self.write(self.holder(navn="Lene"))
        self.t += 0.5
        self.assertEqual(watch.current()["navn"], "Lene")
        self.assertEqual(self.reads, 2)

    def test_a_failed_read_keeps_the_last_answer(self) -> None:
        self.write(self.holder())
        watch = self.watch()
        watch.current()
        for raw in ("{bad json", "[]", ""):
            with self.subTest(raw=raw):
                self.write(None, raw=raw)
                self.t += 2
                self.assertEqual(watch.current()["navn"], "Mette")
        self.t += 3600                                       # … but not forever
        self.assertIsNone(watch.current())

    def test_no_queue_is_looked_up_again_every_minute(self) -> None:
        self.found = None
        watch = self.watch()
        self.assertIsNone(watch.current())
        self.t += 30
        self.assertIsNone(watch.current())
        self.assertEqual(self.lookups, 1)
        self.found = self.path
        self.write(self.holder())
        self.t += 31
        self.assertEqual(watch.current()["navn"], "Mette")
        self.assertEqual(self.lookups, 2)
        os.remove(self.path)                                 # the file is gone: look again later
        self.t += 2
        self.assertIsNone(watch.current())
        self.write(self.holder(opdateret=self.t))
        self.t += 2
        self.assertIsNone(watch.current())
        self.assertEqual(self.lookups, 2)
        self.t += 60
        self.assertEqual(watch.current()["navn"], "Mette")
        self.assertEqual(self.lookups, 3)

    def test_the_file_is_opened_only_when_it_changed(self) -> None:
        self.write(self.holder())
        watch = self.watch()
        for _ in range(5):
            self.assertEqual(watch.current()["navn"], "Mette")
            self.t += 2
        self.assertEqual(self.reads, 1)                      # (an open handle would block the queue's write)
        self.write(self.holder(navn="Lene"))                 # replaced: another file, even if it looks alike
        self.assertEqual(watch.current()["navn"], "Lene")
        self.assertEqual(self.reads, 2)

    def test_the_reader(self) -> None:
        self.write(self.holder())
        with open(self.path, "r+b") as fh:                   # a byte-order mark is fine
            data = fh.read()
            fh.seek(0)
            fh.write(codecs.BOM_UTF8 + data)
        self.assertEqual(crew.read_koe_state(self.path)["holder"]["navn"], "Mette")
        with self.assertRaises(FileNotFoundError):
            crew.read_koe_state(os.path.join(self.dir.name, "nope.json"))
        with open(self.path, "wb") as fh:
            fh.write(b" " * (crew.KOE_MAX_BYTES + 1))
        with self.assertRaises(ValueError):
            crew.read_koe_state(self.path)


# ============================================================================================
# The robots' sprite sheet
# ============================================================================================

def write_png_header(path: str, width: int, height: int) -> None:
    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", width, height) + bytes(9))


class RobotSheetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.web = os.path.join(self.dir.name, "web")
        self.folder = os.path.join(self.dir.name, "pet")
        os.makedirs(self.web)
        for name in ("widget.html", "widget.css", "widget.js"):
            with open(os.path.join(self.web, name), "w") as fh:
                fh.write(name)
        self.runs: list[list[str]] = []
        self.size = (1680, 240)

    def run_edge(self, args, **kwargs) -> None:
        self.runs.append(args)
        out = next(a.split("=", 1)[1] for a in args if a.startswith("--screenshot="))
        if self.size:
            write_png_header(out, *self.size)

    def sheets(self) -> crew.RobotSheets:
        return crew.RobotSheets("http://127.0.0.1:4711/", self.folder, web_dir=self.web, edge="msedge.exe",
                                profile_dir=os.path.join(self.dir.name, "prof"), run=self.run_edge)

    def test_rendered_once_and_reused(self) -> None:
        sheets = self.sheets()
        path = sheets.ensure()
        self.assertRegex(os.path.basename(path), r"^robot-[0-9a-f]{12}\.png$")
        self.assertTrue(os.path.isfile(path))
        args = self.runs[0]
        for flag in ("--headless=new", "--default-background-color=00000000", "--window-size=840,120",
                     "--force-device-scale-factor=2", f"--user-data-dir={os.path.join(self.dir.name, 'prof')}"):
            self.assertIn(flag, args)
        self.assertEqual(args[-1], "http://127.0.0.1:4711/widget.html?sprites=robot-a,robot-b,robot-baer,"
                                   "robot-klip,robot-hop,robot-fraek,robot-panik&cell=120")
        self.assertEqual(sheets.ensure(), path)
        self.assertEqual(len(self.runs), 1)
        self.assertEqual(sheets.expected_size(), (1680, 240))

    def test_a_new_drawing_renders_again_and_tidies_up(self) -> None:
        sheets = self.sheets()
        old = sheets.ensure()
        klippe = os.path.join(self.folder, "klippe-baby-none-x-0123456789ab.png")
        write_png_header(klippe, 2400, 480)
        with open(os.path.join(self.web, "widget.js"), "a") as fh:
            fh.write("// a new robot")
        new = sheets.ensure()
        self.assertNotEqual(old, new)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(klippe))              # Klippe's own sheets are not ours
        self.assertNotEqual(crew.RobotSheets("x", web_dir=self.web).path().rsplit("-", 1)[1],
                            os.path.basename(petplay.SpriteSheets("x", web_dir=self.web).path("baby", "none"))
                            .rsplit("-", 1)[1])

    def test_a_bad_screenshot_is_not_used(self) -> None:
        self.size = (840, 120)                               # 1×: wrong
        with self.assertLogs("projektsog.petplay", "WARNING"):
            self.assertIsNone(self.sheets().ensure())
        self.size = None
        self.assertIsNone(self.sheets().ensure())
        self.assertEqual(os.listdir(self.folder), [])


# ============================================================================================
# The helper process wrapper (a real child: python -c)
# ============================================================================================

CHILD_SCRIPT = r"""
import json, sys
def say(**event):
    print(json.dumps(event), flush=True)
say(event="out")
say(event="aim")
print("not json", flush=True)
say(event="shot", hit=True)
say(event="sparkle")
line = sys.stdin.readline().strip()
say(event="home", reason=line or "quit")
"""


class ChildTests(unittest.TestCase):
    def test_events_and_a_line_to_the_helper(self) -> None:
        seen: list = []
        ended = threading.Event()
        child = petplay._Child([sys.executable, "-c", CHILD_SCRIPT], lambda: seen.append("out"),
                               lambda reason: (seen.append(("home", reason)), ended.set()),
                               seen.append, name="crew-test")
        self.addCleanup(child.stop, 1.0)
        deadline = time.monotonic() + 20
        while {"event": "sparkle"} not in seen and time.monotonic() < deadline:
            time.sleep(0.02)
        child.send("done")
        self.assertTrue(ended.wait(20))
        self.assertEqual(seen, ["out", {"event": "aim"}, {"event": "shot", "hit": True}, {"event": "sparkle"},
                                ("home", "done")])
        child.send("too late")                               # gone: nothing happens

    def test_stop_does_not_wait_long(self) -> None:
        ended = threading.Event()
        child = petplay._Child([sys.executable, "-c", "import sys, time; sys.stdin.read(); time.sleep(30)"],
                               lambda: None, lambda reason: ended.set())
        started = time.monotonic()
        with self.assertLogs("projektsog.petplay", "WARNING"):
            child.stop(0.3)                                  # it does not end by itself: killed
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertTrue(ended.wait(10))
        self.assertFalse(child.alive())


# ============================================================================================
# The crew (main process): fakes
# ============================================================================================

class FakeBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def publish(self, kind: str, data=None) -> None:
        self.events.append((kind, data))

    def of(self, kind: str) -> list[dict]:
        return [data for k, data in self.events if k == kind]


class FakeProbe:
    def __init__(self) -> None:
        self.idle = 10.0
        self.tick = 1000
        self.is_locked = False
        self.shown = True
        self.full = False

    def idle_s(self) -> float: return self.idle
    def input_tick(self) -> int: return self.tick
    def locked(self) -> bool: return self.is_locked
    def window_shown(self, hwnd) -> bool: return self.shown
    def fullscreen(self, hwnd) -> bool: return self.full


class FakeWatch:
    def __init__(self) -> None:
        self.build = None
        self.fail = False

    def current(self):
        if self.fail:
            raise RuntimeError("the queue is broken")
        return dict(self.build) if self.build else None


class FakeChild:
    def __init__(self, argv, on_out, on_exit, on_event) -> None:
        self.argv = argv
        self.on_out = on_out
        self.on_exit = on_exit
        self.on_event = on_event
        self.sent: list[str] = []
        self.stopped: float | None = None

    def arg(self, name: str) -> str:
        return self.argv[self.argv.index(name) + 1]

    def send(self, line: str) -> None:
        self.sent.append(line)

    def stop(self, wait: float = 0) -> None:
        self.stopped = wait
        self.on_exit("quit")


class FakeSprites:
    def __init__(self, path: str) -> None:
        self.path = path
        self.ok = True
        self.calls = 0

    def ensure(self):
        self.calls += 1
        if not self.ok:
            return None
        if not os.path.exists(self.path):
            write_png_header(self.path, 1680, 240)
        return self.path


class FakeBoard:
    def __init__(self) -> None:
        self.posts: list[tuple[dict, bool]] = []
        self.removed: list[str] = []

    def post(self, data, *, internal: bool = False):
        self.posts.append((data, internal))
        return {"ok": True, "vist": True}

    def remove(self, tag):
        self.removed.append(tag)
        return {"ok": True}


class FakeWidget:
    hwnd = 4242


class CrewTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.cfg = Config(path=os.path.join(self.dir.name, "config.json"))
        self.cfg.update({"widget_enabled": True})
        self.bus = FakeBus()
        self.probe = FakeProbe()
        self.watch = FakeWatch()
        sheet = os.path.join(self.dir.name, "robot-abc.png")
        write_png_header(sheet, 1680, 240)
        self.sprites = FakeSprites(sheet)
        self.board = FakeBoard()
        self.children: list[FakeChild] = []
        self.look = dict(LOOK)
        self.wearing: dict[str, str] = {}
        self.game = False
        self.now = 1000.0
        self.wall = 1_700_000_000.0
        self.crew = self.make()

    def make(self, **kwargs) -> crew.Crew:
        def spawn(argv, on_out, on_exit, on_event):
            child = FakeChild(argv, on_out, on_exit, on_event)
            self.children.append(child)
            return child
        options = dict(widget=FakeWidget(), watch=self.watch, look=lambda: self.look, wardrobe=lambda: self.wearing,
                       petplay_busy=lambda: self.game, messages=self.board, probe=self.probe, sprites=self.sprites,
                       spawn=spawn, clock=lambda: self.now, wall=lambda: self.wall, rng=random.Random(1),
                       log_file="crew.log")
        options.update(kwargs)
        return crew.Crew(self.cfg, self.bus, **options)

    def step(self, seconds: float = 0.5) -> None:
        """Half a second later; a sprite sheet drawn meanwhile wakes the crew at once."""
        self.now += seconds
        self.wall += seconds
        before = self.crew._render_thread
        self.crew.step()
        thread = self.crew._render_thread
        if thread is not None and thread is not before:
            thread.join(5)
            self.crew.step()

    def build(self, name: str = "Mette") -> None:
        self.watch.build = {"navn": name, "projekt": "Rikke Lindholm", "opgave": "byg 3 klip",
                            "siden": self.wall - 60}

    def out(self) -> FakeChild:
        """A build, 2 s, the robots out."""
        self.build()
        self.step()
        self.step(2.0)
        self.assertEqual(len(self.children), 1, self.bus.events)
        child = self.children[-1]
        child.on_out()
        return child


# ============================================================================================
# The crew: when the robots come out
# ============================================================================================

class OutingTests(CrewTestBase):
    def test_no_build_no_robots(self) -> None:
        for _ in range(10):
            self.step()
        self.assertEqual(self.children, [])
        self.assertEqual(self.bus.events, [])
        self.assertEqual(self.crew.state(), {"aktiv": False, "navn": None, "projekt": None, "opgave": None,
                                             "siden": None, "demo": False, "ude": False, "retning": None, "faerdig": False,
                                             "varighed_s": None})
        self.assertFalse(self.crew.active())
        self.assertEqual(self.sprites.calls, 0)              # nothing drawn for nothing

    def test_a_build_brings_them_out_when_nobody_is_at_the_pc(self) -> None:
        self.build()
        self.step()
        self.assertEqual(self.bus.of("bygger"), [{
            "aktiv": True, "navn": "Mette", "projekt": "Rikke Lindholm", "opgave": "byg 3 klip",
            "siden": self.wall - 60.5, "demo": False, "ude": False, "retning": None, "faerdig": False, "varighed_s": None}])
        self.assertTrue(self.crew.active())
        self.step(1.0)
        self.assertEqual(self.children, [])                  # the build has only just begun
        self.step(1.0)
        self.assertEqual(len(self.children), 1)
        child = self.children[0]
        self.assertEqual(child.argv[1:3], ["-m", "projektsog.crew_child"])
        self.assertEqual(child.arg("--sprites"), self.sprites.path)
        self.assertEqual(child.arg("--poses"), ",".join(crew.ROBOT_POSES))
        self.assertEqual((child.arg("--cell"), child.arg("--sheet-scale")), ("120", "2"))
        self.assertEqual(child.arg("--widget"), "4242")
        self.assertEqual(child.arg("--pet"), "25.0,40.0,210.0,210.0")
        self.assertEqual(child.arg("--view"), "260.0,448.0")
        self.assertEqual((child.arg("--stage"), child.arg("--robots"), child.arg("--awp")), ("baby", "5", "0"))
        self.assertEqual(child.arg("--input-tick"), "1000")
        self.assertEqual(child.arg("--log-file"), "crew.log")
        self.assertTrue(child.arg("--seed").isdigit())
        self.assertEqual(self.bus.of("bygger")[-1]["ude"], False)
        child.on_out()
        self.assertEqual(self.bus.of("bygger")[-1]["ude"], True)
        self.assertEqual(self.crew.state()["ude"], True)
        self.step()
        self.assertEqual(len(self.children), 1)

    def test_what_keeps_them_in_the_box(self) -> None:
        cases = {
            "Klippe off": lambda: self.cfg.update({"widget_enabled": False}),
            "robots off": lambda: self.cfg.update({"widget_crew": False}),
            "widget hidden": lambda: setattr(self.probe, "shown", False),
            "no look yet": lambda: setattr(self, "look", None),
            "a look without its box": lambda: setattr(self, "look", {**LOOK, "pet": None}),
            "someone at the PC": lambda: setattr(self.probe, "idle", 2.9),
            "locked": lambda: setattr(self.probe, "is_locked", True),
            "full screen": lambda: setattr(self.probe, "full", True),
            "Klippe plays": lambda: setattr(self, "game", True),
            "no sprites": lambda: setattr(self.sprites, "ok", False),
        }
        for name, block in cases.items():
            with self.subTest(name):
                self.setUp()
                block()
                self.build()
                with self.assertLogs("projektsog.crew") as logs:
                    for _ in range(3):
                        self.step(2.0)
                self.assertEqual([r for r in logs.records if r.levelname == "ERROR"], [])
                self.assertEqual(self.children, [], name)
                self.assertTrue(self.crew.active())          # the widget shows the build anyway

    def test_how_many_robots_and_the_awp(self) -> None:
        for stage, count in crew.ROBOTS.items():
            with self.subTest(stage=stage):
                self.setUp()
                self.look = {**LOOK, "stage": stage}
                self.wearing = {"hat": "baret", "haand": "awp"}
                child = self.out()
                self.assertEqual(child.arg("--stage"), stage)
                self.assertEqual(child.arg("--robots"), str(count))
                self.assertEqual(child.arg("--awp"), "0" if stage == "egg" else "1")   # an egg cannot aim
        self.assertEqual(crew.ROBOTS, {"egg": 4, "baby": 5, "junior": 7, "pro": 9, "legend": 12})

    def test_touched_they_wait_for_the_next_stillness(self) -> None:
        child = self.out()
        self.probe.tick += 7                                 # the user is back: the helper sees it …
        self.probe.idle = 0.1
        child.on_exit("touched")                             # … and they run home
        self.assertEqual(self.bus.of("bygger")[-1]["ude"], False)
        self.step()
        self.probe.idle = 2.5
        self.step()
        self.assertEqual(len(self.children), 1)
        self.probe.idle = 3.0
        self.step()
        self.assertEqual(len(self.children), 2)               # out again while the build lasts
        self.assertEqual(self.children[1].arg("--input-tick"), "1007")

    def test_one_outing_per_pause(self) -> None:
        child = self.out()
        child.on_exit("error")
        for _ in range(4):
            self.step()
        self.assertEqual(len(self.children), 1)               # no retrying in a loop
        self.watch.build = {**self.watch.build, "navn": "Lene", "siden": self.wall}   # another build
        self.step()
        self.step(2.0)
        self.assertEqual(len(self.children), 2)

    def test_the_build_ends_while_they_are_out(self) -> None:
        child = self.out()
        events = len(self.bus.events)
        self.watch.build = None
        self.step()
        self.assertEqual(child.sent, ["done"])               # the finale, then home
        self.assertEqual(self.bus.events[events:], [("bygger", {
            "aktiv": False, "navn": "Mette", "projekt": "Rikke Lindholm", "opgave": "byg 3 klip",
            "siden": self.wall - 63.0, "demo": False, "ude": True, "retning": "venstre", "faerdig": True, "varighed_s": 63})])
        self.assertEqual(self.crew.state(), {"aktiv": False, "navn": None, "projekt": None, "opgave": None,
                                             "siden": None, "demo": False, "ude": True, "retning": "venstre", "faerdig": False,
                                             "varighed_s": None})
        self.assertTrue(self.crew.active())                  # (still marching home: no game yet)
        child.on_exit("done")
        self.assertFalse(self.crew.active())
        self.assertEqual(self.bus.events[-1], ("bygger", self.crew.state()))
        self.assertFalse(self.crew.state()["ude"])
        count = len(self.bus.events)
        self.step()
        self.assertEqual(len(self.bus.events), count)       # nothing more to say
        self.assertEqual(sum(1 for e in self.bus.of("bygger") if e["faerdig"]), 1)
        self.assertEqual(child.sent, ["done"])

    def test_a_build_that_ends_in_the_box(self) -> None:
        self.build()
        self.step()
        self.watch.build = None
        self.step()
        ends = [e for e in self.bus.of("bygger") if e["faerdig"]]
        self.assertEqual(len(ends), 1)
        self.assertEqual((ends[0]["navn"], ends[0]["ude"], ends[0]["varighed_s"]), ("Mette", False, 61))
        self.assertEqual(self.children, [])

    def test_switched_off_while_they_are_out(self) -> None:
        child = self.out()
        self.cfg.update({"widget_crew": False})
        self.step()
        self.assertEqual(child.stopped, crew.STOP_WAIT_S)
        self.assertFalse(self.crew.state()["ude"])

    def test_robot_events_reach_the_widget(self) -> None:
        child = self.out()
        self.assertEqual(self.crew.state()["retning"], "venstre")
        child.on_event({"event": "side", "side": "right"})   # the widget at the left: they went out right
        self.assertEqual(self.crew.state()["retning"], "hoejre")
        child.on_event({"event": "aim"})
        child.on_event({"event": "shot", "hit": False})
        child.on_event({"event": "sparkle"})                 # unknown: ignored
        child.on_event({"event": "shot", "hit": True})
        child.on_event({"event": "aim-end"})
        child.on_event({"event": "aim", "side": "right"})   # the robots work to the right of the widget
        child.on_exit("touched")                             # home while aiming: Klippe lowers the AWP
        self.assertEqual(self.bus.of("robot"), [
            {"haendelse": "sigter", "ram": False, "retning": "venstre"}, {"haendelse": "skud", "ram": False},
            {"haendelse": "skud", "ram": True}, {"haendelse": "sigter-slut", "ram": False},
            {"haendelse": "sigter", "ram": False, "retning": "hoejre"}, {"haendelse": "sigter-slut", "ram": False}])

    def test_close_quits_the_helper_without_waiting(self) -> None:
        child = self.out()
        self.crew.close()
        self.assertLessEqual(child.stopped, 0.3)
        self.step()
        self.assertEqual(len(self.children), 1)
        crew_ = self.make()                                  # a running thread stops at once
        crew_.start()
        started = time.monotonic()
        crew_.close()
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertFalse(crew_._thread.is_alive())

    def test_close_does_not_wait_for_the_drawing(self) -> None:
        release = threading.Event()

        class SlowSprites:
            def ensure(self_inner):
                release.wait(10)
                return None
        self.crew = self.make(sprites=SlowSprites())
        self.build()
        self.now += 0.5
        self.crew.step()
        self.now += 2.0
        self.crew.step()                                     # starts drawing, does not wait
        self.assertTrue(self.crew._render_thread.is_alive())
        started = time.monotonic()
        self.crew.close()
        self.assertLess(time.monotonic() - started, 1.0)
        with self.assertLogs("projektsog.crew", "WARNING"):
            release.set()                                    # (Edge gave up meanwhile)
            self.crew._render_thread.join(5)

    def test_sprites_are_drawn_once_and_retried_much_later(self) -> None:
        self.sprites.ok = False
        self.build()
        with self.assertLogs("projektsog.crew", "WARNING"):
            for _ in range(5):
                self.step(2.0)
        self.assertEqual(self.sprites.calls, 1)
        self.sprites.ok = True
        self.step(crew.RENDER_RETRY_S)
        self.assertEqual(self.sprites.calls, 2)
        self.assertEqual(len(self.children), 1)
        os.remove(self.sprites.path)                         # someone cleaned up the folder
        self.children[0].on_exit("touched")
        self.probe.tick += 1
        self.step()
        self.assertEqual(self.sprites.calls, 3)

    def test_a_broken_queue_keeps_the_last_build(self) -> None:
        self.build()
        self.step()
        self.watch.fail = True
        with self.assertLogs("projektsog.crew", "ERROR"):
            self.step()
        self.assertTrue(self.crew.active())

    def test_the_rules_in_order(self) -> None:
        base = dict(enabled=True, widget=True, look=True, demo=False, settled=True, idle_s=5.0, locked=False,
                    fullscreen=False, game=False, sprites=True)
        self.assertIsNone(crew.crew_blocker(**base))
        self.assertEqual(crew.crew_blocker(**{**base, "enabled": False, "widget": False}), "off")
        self.assertEqual(crew.crew_blocker(**{**base, "settled": False}), "settling")
        self.assertEqual(crew.crew_blocker(**{**base, "idle_s": 2.99}), "busy")
        self.assertEqual(crew.crew_blocker(**{**base, "locked": True, "fullscreen": True}), "locked")
        self.assertEqual(crew.crew_blocker(**{**base, "fullscreen": True}), "fullscreen")
        self.assertEqual(crew.crew_blocker(**{**base, "game": True}), "game")
        self.assertEqual(crew.crew_blocker(**{**base, "sprites": False}), "sprites")
        demo = {**base, "demo": True, "settled": False, "fullscreen": True, "game": True}
        self.assertIsNone(crew.crew_blocker(**demo))
        self.assertEqual(crew.crew_blocker(**{**demo, "idle_s": 1.0}), "busy")
        self.assertEqual(crew.crew_blocker(**{**demo, "locked": True}), "locked")
        self.assertEqual(crew.crew_blocker(**{**demo, "widget": False}), "no-widget")


# ============================================================================================
# The crew: the demo
# ============================================================================================

class DemoTests(CrewTestBase):
    def test_a_demo_build(self) -> None:
        self.probe.full = True                               # a demo does not mind full screen …
        self.game = True                                     # … or a game
        state = self.crew.demo(False)
        self.assertEqual(state, {"aktiv": True, "navn": "Demo", "projekt": "Robotterne øver sig", "opgave": "",
                                 "siden": self.wall, "demo": True, "ude": False, "retning": None, "faerdig": False,
                                 "varighed_s": None})
        self.assertEqual(self.bus.of("bygger"), [state])
        self.assertTrue(self.crew.active())
        self.probe.idle = 2.0
        self.step()
        self.assertEqual(self.children, [])                  # but it waits for stillness too
        self.probe.idle = 3.0
        self.step()
        self.assertEqual(len(self.children), 1)
        self.children[0].on_out()
        self.step(crew.DEMO_S - 2.0)                       # 44 s after the click
        self.assertTrue(self.crew.active())
        self.step(1.0)
        self.assertEqual(self.children[0].sent, ["done"])
        end = self.bus.of("bygger")[-1]
        self.assertEqual((end["faerdig"], end["demo"], end["navn"], end["varighed_s"]), (True, True, "Demo", 45))
        self.children[0].on_exit("done")
        self.assertFalse(self.crew.active())

    def test_once_more_lasts_longer(self) -> None:
        self.probe.idle = 0.0                                # (the robots stay in the box)
        self.crew.demo(False)
        self.step(30)
        self.crew.demo(False)
        self.step(30)
        self.assertTrue(self.crew.active())
        self.assertEqual([e["faerdig"] for e in self.bus.of("bygger")], [False])
        self.step(16)
        self.assertFalse(self.crew.active())

    def test_a_locked_screen_stops_a_demo_too(self) -> None:
        self.probe.is_locked = True
        self.crew.demo(False)
        self.step()
        self.step()
        self.assertEqual(self.children, [])

    def test_the_demo_call_and_its_button(self) -> None:
        state = self.crew.demo(True)
        self.assertFalse(state["aktiv"])
        (message, internal), = self.board.posts
        self.assertTrue(internal)                            # Projektsøg's own: projektsog: is allowed
        self.assertEqual((message["tag"], message["titel"]), ("demo:opkald", "🎬 Demo vil bruge Resolve"))
        self.assertEqual(message["knapper"], [{"tekst": "Byg nu", "uri": "projektsog:demo"}])
        self.assertEqual((message["prioritet"], message["lyd"], message["visning"]), ("normal", True, "kort"))
        self.assertTrue(message["tekst"])
        self.assertEqual(self.board.removed, ["demo:opkald"])  # a new demo call rings anew
        self.assertIsNone(self.crew.handle_uri("projektsog:demo"))   # "Byg nu"
        self.assertTrue(self.crew.state()["demo"])
        for uri in ("projektsog:noget-andet", "resolvekoe:byg", None):
            with self.subTest(uri=uri), self.assertRaises(ValueError):
                self.crew.handle_uri(uri)

    def test_the_demo_needs_klippe(self) -> None:
        self.cfg.update({"widget_enabled": False})
        for call in (False, True):
            with self.subTest(call=call), self.assertRaisesRegex(ValueError, "Slå Klippe til først"):
                self.crew.demo(call)
        self.assertEqual(self.board.posts, [])
        self.cfg.update({"widget_enabled": True})
        self.crew = self.make(messages=None)
        with self.assertRaisesRegex(ValueError, "Beskederne er ikke startet"):
            self.crew.demo(True)

    def test_no_demo_while_a_session_builds(self) -> None:
        self.build()
        self.step()
        for action in (lambda: self.crew.demo(False), lambda: self.crew.demo(True),
                       lambda: self.crew.handle_uri("projektsog:demo")):
            with self.assertRaisesRegex(ValueError, "bygger allerede"):
                action()
        self.assertEqual(self.board.posts, [])

    def test_a_real_build_takes_over_from_the_demo(self) -> None:
        self.crew.demo(False)
        self.step()
        self.build("Lene")
        self.step()
        states = self.bus.of("bygger")
        self.assertEqual([(s["navn"], s["aktiv"], s["faerdig"]) for s in states],
                         [("Demo", True, False), ("Demo", False, True), ("Lene", True, False)])
        self.assertFalse(self.crew.state()["demo"])


if __name__ == "__main__":
    unittest.main()
