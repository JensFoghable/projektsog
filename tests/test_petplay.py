"""Klippe plays (petplay.py, petplay_child.py): when a game may start, what it looks like, and
that the pointer always ends up where the user left it."""

import ctypes
import math
import os
import random
import struct
import tempfile
import threading
import time
import unittest
import zlib

from projektsog import petplay, petplay_child as pc
from projektsog.config import Config
from projektsog.petplay_child import Frame, Geometry, Rect, Segment

WORK = Rect(2560, 0, 5120, 1392)
PLAY = WORK.inset(36, 36)
WIDGET_HOME = (4974.0, 1100.0)


def geometry(unit: float = 1.0) -> Geometry:
    return Geometry(work=WORK, play=PLAY, home=WIDGET_HOME, home_scale=1.17, pet_w=93 * unit,
                    pet_h=78 * unit, unit=unit)


def frames(segments, step: float = 1 / 30):
    """Every frame of a show, sampled ``step`` seconds apart (and each segment's last one)."""
    out = []
    for seg in segments:
        n = max(1, math.ceil(seg.duration / step))
        for i in range(n + 1):
            out.append((seg, seg.at(i / n)))
    return out


# ============================================================================================
# The choreography
# ============================================================================================

class ShowTests(unittest.TestCase):
    POINTERS = {"inside": (3300.0, 500.0), "outside": (1200.0, 700.0), "corner": (2600.0, 10.0)}

    def shows(self):
        for stage in ("baby", "junior", "pro", "legend"):
            for name, pointer in self.POINTERS.items():
                for seed in range(6):
                    awp = seed % 2 == 1                      # every other one with the AWP
                    yield stage, name, pointer, seed, pc.build_show(geometry(), random.Random(seed), pointer, stage,
                                                                    awp=awp)

    def test_the_pointer_never_leaves_the_play_area_and_ends_where_it_was(self) -> None:
        for stage, name, pointer, seed, show in self.shows():
            with self.subTest(stage=stage, pointer=name, seed=seed):
                held = [(seg, f) for seg, f in frames(show) if seg.holds_pointer and f.cursor is not None]
                self.assertTrue(held, "Klippe never had the pointer")
                inside = name != "outside"
                for seg, f in held[:-1] if not inside else held:
                    if f.cursor == pointer:
                        continue           # (given back to its own screen)
                    self.assertTrue(PLAY.contains(*f.cursor, slack=1.0), (seg.name, f.cursor))
                self.assertEqual(held[-1][1].cursor, pointer)        # given back exactly

    def test_the_pet_stays_on_the_monitor_and_starts_and_ends_in_the_widget(self) -> None:
        for stage, name, pointer, seed, show in self.shows():
            with self.subTest(stage=stage, pointer=name, seed=seed):
                all_frames = frames(show)
                first, last = all_frames[0][1], all_frames[-1][1]
                self.assertEqual((round(first.x), round(first.y)), (round(WIDGET_HOME[0]), round(WIDGET_HOME[1])))
                self.assertAlmostEqual(last.x, WIDGET_HOME[0], places=3)
                self.assertAlmostEqual(last.y, WIDGET_HOME[1], places=3)
                self.assertAlmostEqual(first.scale, 1.17)
                self.assertAlmostEqual(last.scale, 1.17)
                for seg, f in all_frames:
                    self.assertTrue(WORK.contains(f.x, f.y, slack=4), (seg.name, f.x, f.y))
                    self.assertIn(f.pose, petplay.POSES)

    def test_segments_join_up(self) -> None:
        for stage, name, pointer, seed, show in self.shows():
            with self.subTest(stage=stage, pointer=name, seed=seed):
                for a, b in zip(show, show[1:]):
                    end, start = a.at(1.0), b.at(0.0)
                    self.assertLess(math.hypot(end.x - start.x, end.y - start.y), 12, (a.name, b.name))

    def test_stages_and_lengths(self) -> None:
        names = lambda show: [s.name for s in show]          # noqa: E731
        baby = pc.build_show(geometry(), random.Random(3), self.POINTERS["inside"], "baby")
        pro = pc.build_show(geometry(), random.Random(3), self.POINTERS["inside"], "pro")
        acts = set(pc.ACTS)
        self.assertEqual(len([n for n in names(baby) if n in acts]), 3)
        self.assertEqual(len([n for n in names(pro) if n in acts]), 2)
        self.assertEqual(names(baby)[:2], ["shake", "breakout"])
        self.assertEqual(names(baby)[-1], "home")
        for show in (baby, pro):
            self.assertTrue(8 < pc.show_length(show) < 45, pc.show_length(show))
        outside = pc.build_show(geometry(), random.Random(3), self.POINTERS["outside"], "baby", acts=["ride"])
        self.assertIn("reach", names(outside))                # fetched from the other screen …
        self.assertIn("toss", names(outside))                 # … and tossed back there

    def test_every_act(self) -> None:
        for act in pc.ACTS:
            with self.subTest(act=act):
                show = pc.build_show(geometry(1.5), random.Random(1), self.POINTERS["inside"], acts=[act])
                self.assertIn(act, [s.name for s in show])

    def test_with_the_awp_klippe_shoots_after_the_pointer(self) -> None:
        for seed in range(8):
            for name, pointer in self.POINTERS.items():
                with self.subTest(seed=seed, pointer=name):
                    show = pc.build_show(geometry(), random.Random(seed), pointer, "baby", awp=True)
                    names = [s.name for s in show]
                    self.assertEqual(len([n for n in names if n in (*pc.ACTS, "snipe")]), 3)
                    snipe = next(s for s in show if s.name == "snipe")
                    frames = [snipe.at(i / 1000) for i in range(1001)]
                    self.assertTrue(all(f.pose == "aim" and f.laser for f in frames))
                    flashes = sum(1 for a, b in zip(frames, frames[1:]) if b.flash and not a.flash)
                    self.assertTrue(3 <= flashes <= 9, flashes)         # a couple of tries
                    # The laser has a mind of its own: it is not glued to the pointer …
                    apart = [math.dist(f.laser_to, f.cursor) for f in frames]
                    self.assertGreater(max(apart), 40)
                    # … and the barrel follows the laser, not the pointer.
                    for f in frames:
                        dx = f.laser_to[0] - f.x
                        if abs(dx) > 60:
                            self.assertEqual(f.flip, dx < 0)
                    self.assertIn("drop", names)
                    self.assertIn("fetch-it", names)

    def test_the_hunt(self) -> None:
        play = PLAY
        for seed in range(12):
            with self.subTest(seed=seed):
                hunt = pc.hunt_plan(random.Random(seed), play, (2900, 450), (4400, 800))
                outcomes = [o for _t, o, _w in hunt.shots]
                self.assertEqual(outcomes[-1], "kill")
                self.assertEqual(outcomes.count("kill"), 1)
                self.assertTrue(2 <= len(outcomes) <= 9)
                self.assertTrue(all(play.contains(*a) and play.contains(*p) for a, p in zip(hunt.aim, hunt.pointer)))
                for t, outcome, where in hunt.shots:
                    i = hunt.index(t - hunt.step)
                    pointer = hunt.pointer[i]
                    if outcome == "miss":
                        self.assertGreater(math.dist(where, pointer), 10)    # the bullet went beside it
                    else:
                        self.assertEqual(where, pointer)                     # right on it
                # The pointer stands still – it only moves right after a hit.
                hits = [t for t, o, _w in hunt.shots if o == "hit"]
                for i, (a, b) in enumerate(zip(hunt.pointer, hunt.pointer[1:])):
                    if a != b:
                        t = (i + 1) * hunt.step
                        self.assertTrue(any(0 <= t - h <= 0.45 for h in hits), t)
                self.assertEqual(sum(1 for a, b in zip(hunt.pointer, hunt.pointer[1:]) if a != b) > 0, bool(hits))

    def test_aim(self) -> None:
        self.assertEqual(pc.aim((0, 0), (10, 0)), (0.0, False))
        self.assertEqual(pc.aim((0, 0), (10, 10)), (45.0, False))
        self.assertEqual(pc.aim((0, 0), (-10, 10)), (-45.0, True))
        self.assertEqual(pc.aim((0, 0), (1, 100)), (55.0, False))           # never steeper than 55°

    def test_caught_playing_it_flies_home_without_the_pointer(self) -> None:
        geo = geometry()
        mid = Frame(3400, 400, scale=1.0, pose="cheer", cursor=(3400, 440))
        show = pc.abort_show(geo, mid)
        all_frames = frames(show)
        self.assertEqual([s.name for s in show], ["oops", "home"])
        self.assertTrue(all(f.cursor is None for _seg, f in all_frames))
        self.assertTrue(all(not seg.holds_pointer for seg in show))
        self.assertAlmostEqual(all_frames[-1][1].x, WIDGET_HOME[0], places=3)
        self.assertLess(pc.show_length(show), 2.5)

    def test_frame_at(self) -> None:
        show = [Segment("a", 1.0, lambda u: Frame(u, 0)), Segment("b", 2.0, lambda u: Frame(10 + u, 0))]
        self.assertEqual(pc.frame_at(show, 0.5)[0], 0)
        self.assertAlmostEqual(pc.frame_at(show, 2.0)[1].x, 10.5)
        self.assertIsNone(pc.frame_at(show, 3.01))

    def test_a_thrown_pointer_stays_inside(self) -> None:
        path = pc.throw_path((3000, 300), (2500, -2000), PLAY, 2600, 6.0)   # a hard throw
        self.assertTrue(all(PLAY.contains(x, y) for x, y in path))
        self.assertAlmostEqual(path[-1][1], PLAY.bottom, delta=1)       # in the end it lies on the floor
        self.assertEqual(path[-1], path[-2])

    def test_rect(self) -> None:
        self.assertEqual(Rect(0, 0, 10, 10).inset(8, 2), Rect(5, 2, 5, 8))   # too narrow: the middle
        self.assertEqual(Rect(0, 0, 10, 10).clamp(-5, 20), (0, 10))
        self.assertEqual(Rect(0, 0, 10, 20).at(0.5, 0.25), (5, 5))

    def test_home_in_the_widget(self) -> None:
        # Widget 260×480 at 100 %, page 260×448 (a 32 px title bar), the pet SVG 210 px square.
        widget = Rect(4844, 896, 5104, 1376)
        x, y = pc.home_in_widget(widget, (25, 40, 210, 210), (260, 448), 1.0, (100, 100))
        self.assertEqual((x, y), (4844 + 25 + 105, 1376 - 448 + 40 + 105))
        # 150 %: everything ×1.5; a wide box centres the square drawing.
        x2, y2 = pc.home_in_widget(Rect(0, 0, 390, 720), (0, 0, 260, 210), (260, 448), 1.5, (0, 0))
        self.assertEqual((x2, y2), ((25) * 1.5, 720 - 448 * 1.5))

    def test_alpha_box(self) -> None:
        rows = [b"\x00\x00\x00\x00", b"\x00\x05\x00\x00", b"\x00\x00\x09\x00", b"\x00\x00\x00\x00"]
        self.assertEqual(pc.alpha_box(rows), (1, 1, 3, 3))
        self.assertIsNone(pc.alpha_box([b"\x00\x00"]))


# ============================================================================================
# The game loop (fake overlay, fake pointer)
# ============================================================================================

class FakeOverlay:
    def __init__(self) -> None:
        self.shown: list[Frame] = []
        self.canvas = self

    def draw(self, sprites, frame, velocity, unit) -> None:
        pass

    def show(self, frame) -> bool:
        self.shown.append(frame)
        return True

    def pump(self) -> None:
        pass


class FakeMouse:
    def __init__(self, at=(1000.0, 700.0)) -> None:
        self.at = at
        self.moves: list[tuple[int, int]] = []

    def get(self):
        return self.at

    def put(self, p):
        self.at = (float(round(p[0])), float(round(p[1])))
        self.moves.append((round(p[0]), round(p[1])))
        return round(p[0]), round(p[1])


def quick_show(pointer):
    """Short: out of the box, take the pointer and play with it, give it back, go home."""
    return [
        Segment("breakout", 0.05, lambda u: Frame(100, 100)),
        Segment("ride", 0.25, lambda u: Frame(200 + 100 * u, 200, cursor=(200 + 100 * u, 240)), True),
        Segment("put-down", 0.05, lambda u: Frame(300, 200, cursor=pointer), True),
        Segment("home", 0.1, lambda u: Frame(300 - 200 * u, 200)),
    ]


class PlayerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tick = 500
        self.locked = False
        self.events: list[dict] = []
        self.mouse = FakeMouse((3000.0, 500.0))
        self.overlay = FakeOverlay()
        self.watch = pc.Watch(lambda: self.tick, lambda: self.locked)

    def player(self, show=None) -> pc.Player:
        return pc.Player(geometry(), show or quick_show(self.mouse.at), None, self.overlay, self.watch,
                         self.events.append, pointer=self.mouse.at, get_pointer=self.mouse.get,
                         set_pointer=self.mouse.put, fps=200)

    def test_a_whole_game(self) -> None:
        reason = self.player().run()
        self.assertEqual(reason, "done")
        self.assertEqual(self.events, [{"event": "out"}, {"event": "home", "reason": "done"}])
        self.assertGreater(len(self.mouse.moves), 5)
        self.assertEqual(self.mouse.at, (3000.0, 500.0))                 # back where it was
        self.assertAlmostEqual(self.overlay.shown[-1].x, 100, delta=15)   # home

    def touch_after(self, seconds: float, what: str) -> None:
        def later() -> None:
            time.sleep(seconds)
            if what == "key":
                self.tick += 1                       # any input: the last-input time changes
            elif what == "mouse":
                self.mouse.at = (self.mouse.at[0] + 40, self.mouse.at[1])
            elif what == "lock":
                self.locked = True
            else:
                self.watch.quit.set()
        threading.Thread(target=later, daemon=True).start()

    def test_touching_anything_ends_it_at_once(self) -> None:
        for what in ("key", "mouse"):
            with self.subTest(what=what):
                self.setUp()
                show = quick_show(self.mouse.at)
                show[1] = Segment("ride", 1.5, show[1].at, True)
                self.touch_after(0.3, what)
                player = self.player(show)
                reason = player.run()
                self.assertEqual(reason, "touched")
                self.assertEqual(self.mouse.moves[-1], (3000, 500))     # put back where the user left it
                self.assertEqual(self.events[-1], {"event": "home", "reason": "touched"})
                self.assertAlmostEqual(self.overlay.shown[-1].x, WIDGET_HOME[0], delta=25)   # flew home

    def test_a_locked_screen_or_quit_stops_it_dead(self) -> None:
        for what in ("lock", "quit"):
            with self.subTest(what=what):
                self.setUp()
                show = quick_show(self.mouse.at)
                show[1] = Segment("ride", 1.5, show[1].at, True)
                self.touch_after(0.3, what)
                started = time.monotonic()
                reason = self.player(show).run()
                self.assertEqual(reason, "locked" if what == "lock" else "quit")
                self.assertLess(time.monotonic() - started, 1.2)
                self.assertEqual(self.mouse.at, (3000.0, 500.0))

    def test_a_touch_before_it_started(self) -> None:
        self.watch = pc.Watch(lambda: self.tick, lambda: False, start_tick=self.tick - 1)
        reason = self.player().run()
        self.assertEqual(reason, "touched")
        self.assertEqual(self.mouse.moves, [])
        self.assertEqual(self.events, [{"event": "home", "reason": "touched"}])   # never out

    def test_a_hit_shows_on_the_pointer(self) -> None:
        target = FakeOverlay()
        target.hidden = 0
        target.draw_target = lambda frame: None
        target.hide = lambda: setattr(target, "hidden", target.hidden + 1)
        show = [Segment("snipe", 0.2, lambda u: Frame(100, 100, pose="aim", cursor=(400 + 100 * u, 300),
                                                      aim_at=(400 + 100 * u, 300), hit=1 - u), True),
                Segment("put-down", 0.05, lambda u: Frame(100, 100, cursor=self.mouse.at), True),
                Segment("home", 0.05, lambda u: Frame(100, 100))]
        player = pc.Player(geometry(), show, None, self.overlay, self.watch, self.events.append, pointer=self.mouse.at,
                           get_pointer=self.mouse.get, set_pointer=self.mouse.put, fps=200, target=target)
        self.assertEqual(player.run(), "done")
        self.assertTrue(target.shown and all(400 <= f.x <= 500 and f.y == 300 for f in target.shown))
        self.assertGreater(target.hidden, 0)                    # gone once the hit has faded

    def test_the_laser_is_drawn_once_while_everything_stands_still(self) -> None:
        class Laser(FakeOverlay):
            def __init__(self) -> None:
                super().__init__()
                self.lines, self.places, self.hidden = [], [], 0

            def draw_laser(self, start, end, clear=None) -> None:
                self.lines.append((start, end))

            def show_box(self, left, top, box) -> None:
                self.places.append((left, top, box))

            def hide(self) -> None:
                self.hidden += 1

        class Sprites:
            tip = (40.0, 0.0)
        laser = Laser()
        laser.size, laser.height = 2560, 1392
        laser.canvas = laser
        show = [Segment("aim", 0.2, lambda u: Frame(3000, 300, pose="aim", cursor=(3500, 400), laser=True,
                                                    laser_to=(3400, 420)), True),
                Segment("put-down", 0.05, lambda u: Frame(3000, 300, cursor=self.mouse.at), True),
                Segment("home", 0.05, lambda u: Frame(3000, 300))]
        player = pc.Player(geometry(), show, Sprites(), self.overlay, self.watch, self.events.append, pointer=self.mouse.at,
                           get_pointer=self.mouse.get, set_pointer=self.mouse.put, fps=200, laser=laser)
        self.assertEqual(player.run(), "done")
        self.assertEqual(laser.lines, [((3000 + 46 - 2560, 300), (3400 - 2560, 420))])   # barrel → where it aims
        self.assertEqual(laser.places, [(2560, 0, (486 - 8, 300 - 8, 840 - 486 + 16, 420 - 300 + 16))])  # just its box
        self.assertGreater(laser.hidden, 0)

    def test_without_moving_the_pointer(self) -> None:
        player = pc.Player(geometry(), quick_show(self.mouse.at), None, self.overlay, self.watch,
                           self.events.append, move_pointer=False, pointer=self.mouse.at,
                           get_pointer=self.mouse.get, set_pointer=self.mouse.put, fps=200)
        self.assertEqual(player.run(), "done")
        self.assertEqual(self.mouse.moves, [])


# ============================================================================================
# Drawing (GDI+, off screen)
# ============================================================================================

def write_sheet(path: str, cells: int = 5, cell: int = 480) -> None:
    """A sprite sheet: in every cell an opaque square (the pet) on transparency; the last one
    ("aim") holds a barrel out to the right."""
    rows = []
    for y in range(cell):
        row = bytearray()
        for c in range(cells):
            for x in range(cell):
                inside = 180 <= x < 300 and 200 <= y < 360 + c * 4
                inside = inside or (c == cells - 1 and 300 <= x < 440 and 256 <= y < 264)
                row += bytes((40, 50, 60, 255)) if inside else bytes(4)
        rows.append(b"\x00" + bytes(row))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", cell * cells, cell, 8, 6, 0, 0, 0))
                 + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b""))


class DrawingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.sheet = os.path.join(self.dir.name, "sheet.png")
        write_sheet(self.sheet)

    def test_sprites_and_a_frame(self) -> None:
        with pc.GdiPlus():
            sprites = pc.Sprites(self.sheet, list(petplay.POSES), 240, 2, 0.9)
            try:
                # The "normal" pose (cell 0) is 120×160 sheet px → ×0.45 at 0.9 px per SVG unit.
                self.assertAlmostEqual(sprites.pet_w, 120 * 0.45)
                self.assertAlmostEqual(sprites.pet_h, 160 * 0.45)
                self.assertEqual(sprites.anchor_units, (240 / 2 - 20, 280 / 2 - 20))
                canvas = pc.Canvas(200)
                try:
                    canvas.draw(sprites, Frame(0, 0, angle=30, pose="cheer"), (0.0, 0.0), 1.0)
                    self.assertEqual(canvas.pixel_alpha(100, 100), 255)       # the pet in the middle
                    self.assertEqual(canvas.pixel_alpha(2, 2), 0)             # transparent around it
                    canvas.draw(sprites, Frame(0, 0, pose="normal"), (3000.0, 0.0), 1.0)   # speed lines
                    self.assertGreater(canvas.pixel_alpha(100 - 40, 100), 0)
                finally:
                    canvas.close()
            finally:
                sprites.close()
        os.remove(self.sheet)            # the file is not held open

    def test_the_awp_fires_from_its_muzzle(self) -> None:
        with pc.GdiPlus():
            sprites = pc.Sprites(self.sheet, list(petplay.POSES), 240, 2, 0.9)
            try:
                tx, ty = sprites.tip              # the barrel's end, from the middle of the pet
                self.assertAlmostEqual(tx, (439 - 240) * 0.45, delta=1)
                self.assertAlmostEqual(ty, (259.5 - 280) * 0.45, delta=1)
                canvas = pc.Canvas(260)
                try:
                    canvas.draw(sprites, Frame(0, 0, pose="aim", flash=1.0), (0.0, 0.0), 1.0)
                    self.assertGreater(canvas.pixel_alpha(int(130 + tx + 6), int(130 + ty)), 0)   # the flash
                    canvas.draw(sprites, Frame(0, 0, pose="aim", flip=True), (0.0, 0.0), 1.0)
                    self.assertGreater(canvas.pixel_alpha(int(130 - tx + 3), int(130 + ty + 3)), 0)  # mirrored
                    self.assertEqual(canvas.pixel_alpha(int(130 + tx - 3), int(130 + ty + 3)), 0)
                finally:
                    canvas.close()
                target = pc.Canvas(80)
                try:
                    target.draw_target(Frame(0, 0, hit=1.0))
                    self.assertGreater(target.pixel_alpha(40, 40), 0)             # the hit
                    self.assertEqual(target.pixel_alpha(2, 2), 0)
                    target.draw_target(Frame(0, 0, hit=0.0))
                    self.assertEqual(target.pixel_alpha(40, 40), 0)               # no sight on the pointer
                finally:
                    target.close()
                laser = pc.Canvas(200, 1.0, 60)                                    # any size
                try:
                    laser.draw_laser((10, 30), (190, 30))
                    self.assertGreater(laser.pixel_alpha(100, 30), 200)
                    self.assertEqual(laser.pixel_alpha(100, 5), 0)
                    self.assertEqual(laser.pixel_alpha(199, 59), 0)
                    laser.draw_laser((10, 50), (90, 50), clear=(0, 20, 120, 20))   # wipes only its box
                    self.assertEqual(laser.pixel_alpha(100, 30), 0)
                    self.assertGreater(laser.pixel_alpha(150, 30), 200)            # outside the box: kept
                    self.assertGreater(laser.pixel_alpha(50, 50), 200)
                finally:
                    laser.close()
            finally:
                sprites.close()

    def test_the_window_is_never_activated_and_lets_clicks_through(self) -> None:
        with pc.GdiPlus():
            overlay = pc.Overlay(64)
            try:
                user32 = ctypes.windll.user32
                user32.GetWindowLongW.restype = ctypes.c_long
                style = user32.GetWindowLongW(ctypes.c_void_p(overlay.hwnd), -20) & 0xFFFFFFFF   # GWL_EXSTYLE
                for flag in (pc.WS_EX_LAYERED, pc.WS_EX_TRANSPARENT, pc.WS_EX_TOPMOST, pc.WS_EX_TOOLWINDOW,
                             pc.WS_EX_NOACTIVATE):
                    self.assertTrue(style & flag, hex(flag))
                self.assertEqual(pc._wndproc(overlay.hwnd, pc.WM_NCHITTEST, 0, 0), pc.HTTRANSPARENT)
                self.assertTrue(overlay.show(Frame(-32000, -32000)))          # far off any screen
            finally:
                overlay.close()

    def test_the_command_line(self) -> None:
        args = pc.parse_args(["--sprites", "x.png", "--poses", "normal,happy", "--widget", "123",
                              "--pet", "1,2,3,4", "--view", "260,448", "--input-tick", "99", "--no-pointer"])
        self.assertEqual((args.widget, args.input_tick, args.no_pointer), (123, 99, True))
        self.assertEqual(pc._numbers(args.pet, 4), [1, 2, 3, 4])
        with self.assertRaises(ValueError):
            pc._numbers("1,2,nan,4", 4)


# ============================================================================================
# When a game may start (main process)
# ============================================================================================

class FakeProbe:
    def __init__(self) -> None:
        self.idle = 0.0
        self.tick = 1000
        self.is_locked = False
        self.follows = False
        self.shown = True
        self.full = False

    def idle_s(self) -> float: return self.idle
    def input_tick(self) -> int: return self.tick
    def locked(self) -> bool: return self.is_locked
    def focus_follows_mouse(self) -> bool: return self.follows
    def window_shown(self, hwnd) -> bool: return self.shown
    def fullscreen(self, hwnd) -> bool: return self.full


class FakeBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def publish(self, kind: str, data=None) -> None:
        self.events.append((kind, data))


class FakeChild:
    def __init__(self, argv, on_out, on_exit) -> None:
        self.argv = argv
        self.on_out = on_out
        self.on_exit = on_exit
        self.stopped = False

    def stop(self, wait: float = 0) -> None:
        self.stopped = True
        self.on_exit("quit")


class FakeSprites:
    def __init__(self) -> None:
        self.ok = True
        self.asked: list[tuple[str, str]] = []

    def ensure(self, stage: str, outfit: str, wearing=None):
        self.asked.append((stage, outfit))
        self.wearing = wearing
        return "C:\\sheets\\klippe.png" if self.ok else None


class FakeBridge:
    def __init__(self) -> None:
        self.act = None

    def activity(self, max_age: float = 15.0):
        return self.act


class FakeImporter:
    def __init__(self) -> None:
        self.state = None

    def job(self):
        return {"state": self.state} if self.state else None


class FakeWidget:
    hwnd = 4242


class RNG(random.Random):
    def __init__(self, value: float) -> None:
        super().__init__(1)
        self.value = value

    def random(self) -> float:
        return self.value


LOOK = {"stage": "baby", "outfit": "color", "pet": {"x": 25, "y": 40, "w": 210, "h": 210}, "view": {"w": 260, "h": 448}}


class SchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.cfg = Config(path=os.path.join(self.dir.name, "config.json"))
        self.cfg.update({"widget_enabled": True})
        self.probe = FakeProbe()
        self.bus = FakeBus()
        self.sprites = FakeSprites()
        self.bridge = FakeBridge()
        self.importer = FakeImporter()
        self.children: list[FakeChild] = []
        self.now = 100.0
        self.play = self.make()

    def make(self, rng: float = 0.5) -> petplay.PetPlay:
        def spawn(argv, on_out, on_exit):
            child = FakeChild(argv, on_out, on_exit)
            self.children.append(child)
            return child
        play = petplay.PetPlay(self.cfg, self.bus, widget=FakeWidget(), bridge=self.bridge,
                               importer=self.importer, probe=self.probe, sprites=self.sprites, spawn=spawn,
                               rng=RNG(rng), clock=lambda: self.now, log_file="x.log")
        play.set_look(LOOK)
        return play

    def idle_for(self, minutes: float) -> None:
        self.probe.idle = minutes * 60
        self.now += 1
        self.play.step()

    def test_a_baby_plays_after_a_pause(self) -> None:
        self.idle_for(4.9)
        self.assertEqual(self.children, [])
        self.idle_for(5)
        self.assertEqual(len(self.children), 1)
        argv = self.children[0].argv
        self.assertEqual(argv[1:3], ["-m", "projektsog.petplay_child"])

        def arg(name):
            return argv[argv.index(name) + 1]
        self.assertEqual(arg("--widget"), "4242")
        self.assertEqual(arg("--stage"), "baby")
        self.assertEqual(arg("--input-tick"), "1000")
        self.assertEqual(arg("--pet"), "25.0,40.0,210.0,210.0")
        self.assertEqual(arg("--view"), "260.0,448.0")
        self.assertEqual(arg("--poses"), "normal,happy,cheer,oops,aim")
        self.assertEqual(self.sprites.asked, [("baby", "color")])
        self.assertEqual(arg("--awp"), "0")

    def test_klippe_wears_its_wardrobe_and_its_games_count(self) -> None:
        games = []
        self.play = petplay.PetPlay(self.cfg, self.bus, widget=FakeWidget(), probe=self.probe, sprites=self.sprites,
                                    spawn=lambda argv, out, end: self.children.append(FakeChild(argv, out, end))
                                    or self.children[-1], clock=lambda: self.now,
                                    wardrobe=lambda: {"hat": "baret", "haand": "awp"},
                                    on_game=lambda reason, out: games.append((reason, out)))
        self.play.set_look(LOOK)
        self.idle_for(6)
        argv = self.children[0].argv
        self.assertEqual(argv[argv.index("--awp") + 1], "1")              # it shoots at the pointer
        self.assertEqual(self.sprites.wearing, {"hat": "baret", "haand": "awp"})
        self.children[0].on_out()
        self.children[0].on_exit("touched")
        self.assertEqual(games, [("touched", True)])

    def test_one_game_per_pause(self) -> None:
        self.idle_for(6)
        child = self.children[0]
        child.on_out()
        self.assertEqual(self.bus.events[-1], ("pet", {"state": "out", "message": "", "reason": None}))
        child.on_exit("done")
        self.assertEqual(self.bus.events[-1], ("pet", {"state": "ready", "message": "", "reason": "done"}))
        self.idle_for(20)
        self.assertEqual(len(self.children), 1)            # still the same pause
        self.probe.tick += 5                               # the user came back …
        self.idle_for(0.1)
        self.idle_for(6)                                   # … and left again
        self.assertEqual(len(self.children), 2)

    def test_the_age_decides(self) -> None:
        self.play.set_look({**LOOK, "stage": "egg"})
        self.idle_for(30)
        self.assertEqual(self.children, [])                # an egg cannot play
        self.play = self.make(rng=0.6)
        self.play.set_look({**LOOK, "stage": "junior"})    # every other pause
        self.idle_for(6)
        self.idle_for(7)
        self.assertEqual(self.children, [])                # no – and no new roll in the same pause
        self.play = self.make(rng=0.4)
        self.play.set_look({**LOOK, "stage": "junior"})
        self.idle_for(6)
        self.assertEqual(len(self.children), 1)

    def test_what_stops_a_game_from_starting(self) -> None:
        cases = {
            "widget_play off": lambda: self.cfg.update({"widget_play": False}),
            "widget off": lambda: self.cfg.update({"widget_enabled": False}),
            "window hidden": lambda: setattr(self.probe, "shown", False),
            "locked": lambda: setattr(self.probe, "is_locked", True),
            "focus follows mouse": lambda: setattr(self.probe, "follows", True),
            "full screen": lambda: setattr(self.probe, "full", True),
            "a transfer": lambda: setattr(self.importer, "state", "verifying"),
            "busy Resolve": lambda: setattr(self.bridge, "act", {"project": "P", "timecode": "01:00:00:00",
                                                                 "age": 30.0}),
        }
        for name, block in cases.items():
            with self.subTest(name):
                self.setUp()
                block()
                self.idle_for(6)
                self.assertEqual(self.children, [], name)

    def test_never_during_playback_but_during_a_render(self) -> None:
        self.bridge.act = {"project": "P", "timecode": "01:00:00:00", "age": 1.0}
        self.probe.idle = 0
        self.play.step()
        self.bridge.act = {"project": "P", "timecode": "01:00:03:00", "age": 1.0}   # the playhead moves
        self.idle_for(6)
        self.assertEqual(self.children, [])
        self.now += petplay.PLAYBACK_RECENT_S + 1             # stopped
        self.idle_for(6)
        self.assertEqual(len(self.children), 1)
        self.setUp()
        for tc in ("01:00:00:00", "01:00:05:00", "01:00:09:00"):            # rendering: fine
            self.bridge.act = {"project": "P", "timecode": tc, "age": 1.0, "rendering": True}
            self.idle_for(6)
        self.assertEqual(len(self.children), 1)

    def test_no_look_from_the_page_no_game(self) -> None:
        self.play = petplay.PetPlay(self.cfg, self.bus, widget=FakeWidget(), probe=self.probe,
                                    sprites=self.sprites, spawn=lambda *a: self.fail("spawned"),
                                    clock=lambda: self.now)
        self.probe.idle = 600
        self.play.step()

    def test_sprites_that_cannot_be_drawn(self) -> None:
        self.sprites.ok = False
        self.idle_for(6)
        self.idle_for(7)
        self.assertEqual(self.children, [])
        self.assertEqual(len(self.sprites.asked), 1)       # no retry in the same pause

    def test_switched_off_during_a_game(self) -> None:
        self.idle_for(6)
        self.cfg.update({"widget_play": False})
        self.play.step()
        self.assertTrue(self.children[0].stopped)

    def test_close_ends_a_game(self) -> None:
        self.idle_for(6)
        self.play.close()
        self.assertTrue(self.children[0].stopped)

    # -- "Vis legen nu" ----------------------------------------------------------------------
    def test_play_now_waits_for_a_still_mouse(self) -> None:
        self.play.set_look({**LOOK, "stage": "egg"})
        self.importer.state = "copying"                    # none of the pause rules apply
        self.probe.full = True
        status = self.play.play_now()
        self.assertEqual(status["state"], "waiting")
        self.probe.idle = 0.4
        self.play.step()
        self.assertEqual(self.children, [])
        self.probe.idle = 1.6
        self.play.step()
        self.assertEqual(len(self.children), 1)
        argv = self.children[0].argv
        self.assertEqual(argv[argv.index("--stage") + 1], "baby")      # the egg shows the baby it will be
        with self.assertRaises(ValueError):
            self.play.play_now()                           # already playing

    def test_play_now_gives_up(self) -> None:
        self.play.play_now()
        self.probe.idle = 0.1
        self.now += petplay.MANUAL_WAIT_S + 1
        self.play.step()
        self.assertEqual(self.children, [])
        self.assertEqual(self.bus.events[-1][1]["state"], "ready")
        self.assertIn("Musen lå ikke stille", self.bus.events[-1][1]["message"])
        self.play.play_now()
        self.probe.is_locked = True
        self.play.step()
        self.assertEqual(self.bus.events[-1][1]["message"], "Skærmen er låst")

    def test_play_now_needs_klippe(self) -> None:
        self.cfg.update({"widget_enabled": False})
        with self.assertRaisesRegex(ValueError, "Slå Klippe til"):
            self.play.play_now()
        self.cfg.update({"widget_enabled": True, "widget_play": False})
        self.play.play_now()                               # the button works even when games are off
        self.probe.idle = 2
        self.play.step()
        self.assertEqual(len(self.children), 1)

    def test_touched_before_it_came_out(self) -> None:
        self.play.play_now()
        self.probe.idle = 2
        self.play.step()
        self.children[0].on_exit("touched")
        self.assertIn("Musen lå ikke stille", self.bus.events[-1][1]["message"])

    def test_the_page_report_is_checked(self) -> None:
        for bad in (None, [], {"stage": "dragon"}, {**LOOK, "outfit": "hat"},
                    {**LOOK, "pet": {"x": 1, "y": 2, "w": 0, "h": 3}}, {**LOOK, "view": {"w": "x", "h": 1}},
                    {**LOOK, "pet": {"x": float("inf"), "y": 2, "w": 3, "h": 3}}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.play.set_look(bad)
        self.assertEqual(petplay.clean_look({"stage": "pro"}),
                         {"stage": "pro", "outfit": "none", "pet": None, "view": None})

    def test_settings(self) -> None:
        from projektsog import config
        self.assertEqual(config.validate({"widget_play_idle_minutes": 2}), {"widget_play_idle_minutes": 2})
        for bad in (0, 61):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                config.validate({"widget_play_idle_minutes": bad})

    def test_blocker_order(self) -> None:
        base = dict(enabled=True, widget=True, look=True, locked=False, focus_follows=False, idle_s=400,
                    threshold_s=300, played=False, transfer=False, playback=False, fullscreen=False, manual=False)
        self.assertIsNone(petplay.play_blocker(**base))
        self.assertEqual(petplay.play_blocker(**{**base, "idle_s": 10}), "busy")
        self.assertEqual(petplay.play_blocker(**{**base, "played": True}), "played")
        self.assertEqual(petplay.play_blocker(**{**base, "enabled": False, "locked": True}), "off")
        self.assertIsNone(petplay.play_blocker(**{**base, "manual": True, "idle_s": 2, "fullscreen": True}))
        self.assertEqual(petplay.play_blocker(**{**base, "manual": True, "idle_s": 1}), "busy")


class SpriteSheetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.web = os.path.join(self.dir.name, "web")
        os.makedirs(self.web)
        for name in ("widget.html", "widget.css", "widget.js"):
            with open(os.path.join(self.web, name), "w") as fh:
                fh.write(name)
        self.runs: list[list[str]] = []
        self.size = (240 * 5 * 2, 240 * 2)

    def run_edge(self, args, **kwargs) -> None:
        self.runs.append(args)
        out = next(a.split("=", 1)[1] for a in args if a.startswith("--screenshot="))
        if self.size:
            write_png_header(out, *self.size)

    def sheets(self) -> petplay.SpriteSheets:
        return petplay.SpriteSheets("http://127.0.0.1:4711/", os.path.join(self.dir.name, "pet"), web_dir=self.web,
                                    edge="msedge.exe", profile_dir=os.path.join(self.dir.name, "prof"), run=self.run_edge)

    def test_rendered_once_and_reused(self) -> None:
        sheets = self.sheets()
        path = sheets.ensure("baby", "none")
        self.assertTrue(path.endswith(".png") and os.path.isfile(path))
        args = self.runs[0]
        self.assertIn("--headless=new", args)
        self.assertIn("--default-background-color=00000000", args)
        self.assertIn("--window-size=1200,240", args)
        self.assertIn("--force-device-scale-factor=2", args)
        self.assertEqual(args[-1], "http://127.0.0.1:4711/widget.html?sprites=normal,happy,cheer,oops,aim"
                                   "&stage=baby&outfit=none&cell=240")
        self.assertEqual(sheets.ensure("baby", "none"), path)
        self.assertEqual(len(self.runs), 1)

    def test_a_new_drawing_renders_again_and_tidies_up(self) -> None:
        sheets = self.sheets()
        old = sheets.ensure("baby", "none")
        with open(os.path.join(self.web, "widget.css"), "a") as fh:
            fh.write("/* a new hat */")
        new = sheets.ensure("baby", "none")
        self.assertNotEqual(old, new)
        self.assertFalse(os.path.exists(old))
        self.assertEqual(len(self.runs), 2)

    def test_a_bad_screenshot_is_not_used(self) -> None:
        self.size = (100, 100)
        self.assertIsNone(self.sheets().ensure("baby", "none"))
        self.size = None
        self.assertIsNone(self.sheets().ensure("pro", "none"))
        self.assertEqual(os.listdir(os.path.join(self.dir.name, "pet")), [])


def write_png_header(path: str, width: int, height: int) -> None:
    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", width, height) + bytes(9))


if __name__ == "__main__":
    unittest.main()
