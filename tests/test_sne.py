"""Snow on monitor 2 (sne.py, sne_child.py, SPEC §23.5): the launcher's rules and the snowfall's
arithmetic. Nothing here opens a window – the helper never runs in a test."""

import os
import random
import tempfile
import threading
import unittest

from projektsog import sne, sne_child
from projektsog.config import Config
from tests.test_petplay import FakeBus

_tmp: tempfile.TemporaryDirectory | None = None


def setUpModule() -> None:
    global _tmp
    _tmp = tempfile.TemporaryDirectory()
    os.environ["LOCALAPPDATA"] = _tmp.name


def tearDownModule() -> None:
    if _tmp is not None:
        _tmp.cleanup()


class FakeChild:
    def __init__(self, argv, on_out, on_exit, on_event) -> None:
        self.argv, self.on_out, self.on_exit, self.on_event = argv, on_out, on_exit, on_event
        self.stopped = False

    def stop(self, wait: float = 0) -> None:
        self.stopped = True


class FakeWidget:
    hwnd = 4242


class SneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.cfg = Config(path=os.path.join(self.dir.name, "config.json"))
        self.cfg.update({"widget_enabled": True})
        self.bus = FakeBus()
        self.children: list[FakeChild] = []
        self.reason: str | None = None
        self.snow = sne.Sne(self.cfg, self.bus, widget=FakeWidget(), spawn=self.spawn, quiet=lambda hwnd: self.reason,
                            rng=random.Random(1), log_file=os.path.join(self.dir.name, "sne.log"))

    def spawn(self, argv, on_out, on_exit, on_event) -> FakeChild:
        child = FakeChild(argv, on_out, on_exit, on_event)
        self.children.append(child)
        return child

    def test_it_snows_on_the_widgets_monitor(self) -> None:
        self.assertEqual(self.snow.start(45), {"ok": True})
        argv = self.children[0].argv
        self.assertEqual(argv[1:3], ["-m", "projektsog.sne_child"])
        self.assertEqual(argv[argv.index("--widget") + 1], "4242")
        self.assertEqual(argv[argv.index("--seconds") + 1], "45")
        self.assertEqual(self.bus.events, [("sne", {"sekunder": 45, "demo": False})])
        self.assertTrue(self.snow.snowing())

    def test_snow_that_falls_is_kept(self) -> None:
        self.snow.start(45)
        self.assertEqual(self.snow.start(20, demo=True), {"ok": True, "sner": True})
        self.assertEqual(len(self.children), 1)
        self.children[0].on_exit("done")
        self.assertFalse(self.snow.snowing())
        self.assertEqual(self.snow.start(20, demo=True), {"ok": True})
        self.assertEqual(self.bus.events[-1], ("sne", {"sekunder": 20, "demo": True}))

    def test_seconds_are_bounded_and_checked(self) -> None:
        self.snow.start(500)
        self.assertEqual(self.children[0].argv[self.children[0].argv.index("--seconds") + 1], "120")
        for bad in (0, -3, "45", None, True):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.snow.start(bad)

    def test_off_locked_or_full_screen(self) -> None:
        self.cfg.update({"widget_enabled": False})
        with self.assertRaisesRegex(ValueError, "Slå Klippe til først"):
            self.snow.start(45)
        self.cfg.update({"widget_enabled": True})
        self.reason = "Skærmen er låst"
        self.assertEqual(self.snow.start(45), {"ok": False, "grund": "Skærmen er låst"})
        self.assertEqual((self.children, self.bus.events), ([], []))

    def test_close_stops_the_snow(self) -> None:
        self.snow.start(45)
        self.snow.close()
        self.assertTrue(self.children[0].stopped)
        self.assertFalse(self.snow.start(45)["ok"])                         # nothing new while closing

    def test_a_failed_launch(self) -> None:
        def broken(*_args):
            raise OSError("no pythonw")
        self.snow._spawn = broken
        with self.assertLogs("projektsog.sne", "ERROR"):
            self.assertEqual(self.snow.start(45), {"ok": False, "grund": "Sneen kunne ikke startes"})
        self.assertEqual(self.bus.events, [])


class SnowfallTests(unittest.TestCase):
    def test_more_flakes_on_a_bigger_monitor(self) -> None:
        self.assertEqual(sne_child.flake_count(1920, 1040), 169)
        self.assertEqual(sne_child.flake_count(3840, 2100), sne_child.MAX_FLAKES)
        self.assertEqual(sne_child.flake_count(0, 0), 90)

    def test_flakes(self) -> None:
        rng = random.Random(3)
        for _ in range(200):
            f = sne_child.new_flake(rng, 1000, 800, 1.5)
            self.assertTrue(1.5 * 1.5 <= f.r <= 4.5 * 1.5)
            self.assertTrue(0.55 <= f.alpha <= 0.95)
            self.assertTrue(30 * 1.5 <= f.speed <= 90 * 1.5)
            self.assertTrue(0 <= f.base <= 1000 and -0.3 * 800 <= f.y <= 800)
            top = sne_child.new_flake(rng, 1000, 800, 1.5, top=True)
            self.assertLess(top.y, 0)

    def test_it_falls_sways_and_ends(self) -> None:
        fall = sne_child.Snowfall(1200, 700, 1.0, 10, random.Random(5))
        count = len(fall.flakes)
        self.assertEqual(fall.opacity(), 0.0)                               # fades in …
        originals = list(fall.flakes)                                       # (kept alive: ids stay unique)
        ys = {id(f): f.y for f in originals}
        fall.advance(0.25)
        fall.advance(0.25)
        kept = [f for f in fall.flakes if id(f) in ys]
        self.assertGreater(len(kept), count * 0.9)
        self.assertTrue(all(f.y > ys[id(f)] for f in kept))                 # everything falls
        self.assertAlmostEqual(fall.opacity(), 0.5)
        for _ in range(24):
            fall.advance(0.25)                                              # 6.5 s: still snowing
        self.assertEqual(len(fall.flakes), count)                           # fallen flakes come back at the top
        self.assertEqual(fall.opacity(), 1.0)
        self.assertTrue(all(-30 <= x <= 1230 and r > 0 and 0 < a <= 0.95 for x, _y, r, a in fall.dots()))
        fall.advance(0.25)
        fall.advance(0.25)                                                  # 7 s: no new flakes from here
        self.assertFalse(fall.respawning())
        for _ in range(8):
            fall.advance(0.25)
        self.assertLessEqual(len(fall.flakes), count)
        self.assertAlmostEqual(fall.opacity(), 1 / 3, places=5)
        self.assertFalse(fall.done())
        for _ in range(4):
            fall.advance(0.25)
        self.assertTrue(fall.done())
        self.assertEqual(fall.dots(), [])

    def test_a_stall_never_throws_the_flakes(self) -> None:
        fall = sne_child.Snowfall(800, 600, 1.0, 30, random.Random(1))
        ys = [f.y for f in fall.flakes]
        fall.advance(10.0)                                                  # the PC hung for 10 s
        self.assertEqual(fall.t, 0.25)
        self.assertTrue(all(f.y - y <= 90 * 0.25 + 1e-9 for f, y in zip(fall.flakes, ys)))

    def test_the_wind_wraps_round(self) -> None:
        fall = sne_child.Snowfall(400, 300, 1.0, 60, random.Random(2))
        for _ in range(400):
            fall.advance(0.1)
        margin = 24
        self.assertTrue(all(-margin <= f.base <= 400 + margin for f in fall.flakes))
        self.assertLessEqual(max(abs(fall.wind()) for _ in range(1)), 19.0)

    def test_seconds_are_bounded(self) -> None:
        self.assertEqual(sne_child.Snowfall(10, 10, 1, 999, random.Random()).seconds, sne_child.MAX_S)
        self.assertEqual(sne_child.Snowfall(10, 10, 1, 0, random.Random()).seconds, 1.0)

    def test_play_ends_on_quit_lock_and_time(self) -> None:
        class Canvas:
            graphics = brush = type("V", (), {"value": None})()

        class Overlay:
            def __init__(self) -> None:
                self.canvas = Canvas()
                self.shown = 0

            def show_at(self, x, y) -> bool:
                self.shown += 1
                return True

            @staticmethod
            def pump() -> None:
                pass

        drawn: list[int] = []
        original = sne_child.draw
        sne_child.draw = lambda canvas, fall: drawn.append(len(fall.flakes))
        try:
            t = [0.0]
            clock = lambda: t[0]                                            # noqa: E731

            def sleep(seconds: float) -> None:
                t[0] += max(seconds, 0.05)
            events: list[dict] = []
            fall = sne_child.Snowfall(300, 200, 1.0, 2, random.Random(1))
            reason = sne_child.play(fall, Overlay(), (0, 0), threading.Event(), events.append, locked=lambda: False,
                                    clock=clock, sleep=sleep)
            self.assertEqual((reason, events), ("done", [{"event": "out"}]))
            self.assertGreater(len(drawn), 10)
            quit_event = threading.Event()
            quit_event.set()
            self.assertEqual(sne_child.play(sne_child.Snowfall(300, 200, 1.0, 2, random.Random(1)), Overlay(), (0, 0),
                                            quit_event, events.append, locked=lambda: False, clock=clock, sleep=sleep),
                             "quit")
            self.assertEqual(sne_child.play(sne_child.Snowfall(300, 200, 1.0, 2, random.Random(1)), Overlay(), (0, 0),
                                            threading.Event(), events.append, locked=lambda: True, clock=clock,
                                            sleep=sleep), "locked")
        finally:
            sne_child.draw = original

    def test_arguments(self) -> None:
        args = sne_child.parse_args(["--widget", "12", "--seconds", "20", "--seed", "3"])
        self.assertEqual((args.widget, args.seconds, args.seed, args.log_file), (12, 20.0, 3, None))


if __name__ == "__main__":
    unittest.main()
