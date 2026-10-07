"""The robot crew out of the box (crew_child.py): where the robots may be, the timeline they
build, Klippe's AWP, the loop that sends them home, and drawing them (GDI+, off screen)."""

import ctypes
import io
import math
import os
import random
import struct
import sys
import tempfile
import threading
import time
import unittest
import zlib
from unittest import mock

from projektsog import crew_child as cc, petplay_child as pc
from projektsog.crew_child import Item
from projektsog.petplay_child import Rect

WORK = Rect(0, 0, 2560, 1392)
WIDGET = Rect(2260, 900, 2540, 1392)          # bottom right: the robots work to its left
MUZZLE = (2300.0, 1150.0)


class FakeSprites:
    """A robot 36 × 56 px (a carrier's clip adds 20 px above its head)."""
    w, h = 36.0, 56.0

    def box(self, pose, flip, x, y):
        extra = 20 if pose == "robot-baer" else 0
        return round(x - 18), round(y - 56 - extra), round(x + 18), round(y)


BOXER = FakeSprites().box


def geometry(unit: float = 1.0, widget: Rect = WIDGET, work: Rect = WORK, awp: bool = True) -> cc.CrewGeometry:
    muzzle = (widget.left + 40 * unit, widget.top + 250 * unit)
    return cc.crew_geometry(work, widget, unit, 36 * unit, 56 * unit, muzzle, awp)


def simulate(scene: cc.Scene, seconds: float, dt: float = 1 / 30, check=None, every: int = 1):
    """Steps the scene; returns [(time, event)]. ``check(scene)`` runs every ``every`` frames."""
    events = []
    for i in range(round(seconds / dt)):
        for event in scene.step(dt):
            events.append((scene.t, event))
        if check is not None and i % every == 0:
            check(scene)
        if scene.over:
            break
    return events


# ============================================================================================
# Geometry
# ============================================================================================

class GeometryTests(unittest.TestCase):
    def test_the_door_faces_the_larger_free_side(self) -> None:
        geo = geometry()
        self.assertEqual((geo.side, geo.door), (-1, 2260))
        self.assertEqual(geo.floor, 1392 - cc.FLOOR_MARGIN)
        self.assertEqual(geo.length, 0.6 * 2560)                       # capped at 60 % of the width
        self.assertEqual(geo.x(100), 2160)
        self.assertEqual(geo.s(2160), 100)
        left = geometry(widget=Rect(20, 900, 300, 1392))
        self.assertEqual((left.side, left.door), (1, 300))
        middle = geometry(widget=Rect(1000, 900, 1280, 1392))
        self.assertEqual(middle.side, 1)                                # 1280 px free on the right
        self.assertAlmostEqual(middle.length, 1280 - 30 - max(24, 36 * 0.6 + 4))
        self.assertTrue(middle.near < middle.start < middle.start + middle.length <= middle.far)
        self.assertLessEqual(middle.x(middle.far) + 18, WORK.right)

    def test_the_band_reaches_above_the_muzzle(self) -> None:
        self.assertEqual(geometry(awp=False).band, Rect(0, 1392 - 260, 2560, 1392))
        self.assertEqual(geometry().band, Rect(0, 1150 - 40, 2560, 1392))
        high = geometry(widget=Rect(2260, 100, 2540, 600))
        self.assertEqual(high.band.top, 350 - 40)
        self.assertEqual(geometry(1.5, awp=False).band.top, 1392 - 390)  # sizes scale with the monitor

    def test_the_muzzle(self) -> None:
        for stage, scale in (("baby", 0.74), ("junior", 0.86), ("pro", 0.95), ("legend", 1.0)):
            self.assertEqual(cc.muzzle_units(stage), (100 - 92 * scale, 180 - 54.75 * scale))
            self.assertEqual(cc.muzzle_units(stage, 1), (100 + 92 * scale, 180 - 54.75 * scale))   # aiming right
        args = cc.parse_args(["--sprites", "r.png", "--widget", "5", "--pet", "25,40,210,210", "--view", "260,448",
                              "--stage", "pro", "--awp", "1"])
        widget = Rect(2264, 912, 2524, 1392)
        geo = cc.geometry_for(args, WORK, 1.0, widget, FakeSprites())
        self.assertEqual(geo.muzzle, pc.home_in_widget(widget, (25, 40, 210, 210), (260, 448), 1.0,
                                                       cc.muzzle_units("pro")))
        left = Rect(WORK.left + 16, 912, WORK.left + 276, 1392)                 # the widget at the left edge
        geo = cc.geometry_for(args, WORK, 1.0, left, FakeSprites())
        self.assertEqual(geo.side, 1)
        self.assertEqual(geo.muzzle, pc.home_in_widget(left, (25, 40, 210, 210), (260, 448), 1.0,
                                                       cc.muzzle_units("pro", 1)))
        self.assertEqual(geo.band.top, math.floor(geo.muzzle[1] - 40))
        egg = cc.geometry_for(cc.parse_args(["--sprites", "r.png", "--widget", "5", "--stage", "egg", "--awp", "1"]),
                              WORK, 1.0, widget, FakeSprites())
        self.assertEqual(egg.band.top, 1392 - 260)                      # no AWP for an egg

    def test_merge_boxes(self) -> None:
        self.assertEqual(cc.merge_boxes([(0, 0, 10, 10), (12, 0, 20, 10), (100, 0, 110, 10)]),
                         [(0, 0, 20, 10), (100, 0, 110, 10)])
        self.assertEqual(cc.merge_boxes([(0, 0, 0, 10)]), [])
        many = [(i * 100, 0, i * 100 + 10, 10) for i in range(30)]
        self.assertEqual(cc.merge_boxes(many), [(0, 0, 2910, 10)])


# ============================================================================================
# The timeline
# ============================================================================================

class TimelineTests(unittest.TestCase):
    def timeline(self) -> cc.Timeline:
        return cc.Timeline(geometry(work=Rect(0, 0, 1000, 800), widget=Rect(700, 500, 980, 800)), random.Random(1))

    def test_clips_are_placed_end_to_end_and_fill_the_track(self) -> None:
        tl = self.timeline()
        limit = tl.limit
        while tl.open(0):
            tl.place(0, 100)
        clips = tl.tracks[0]
        self.assertEqual(clips[0].s0, tl.geo.start)
        self.assertTrue(all(a.s1 == b.s0 for a, b in zip(clips, clips[1:])))
        self.assertAlmostEqual(clips[-1].s1, limit)                     # filled to the end, no sliver
        self.assertTrue(all(c.s1 - c.s0 >= tl.min_clip for c in clips))
        self.assertIsNone(tl.place(0, 100))
        self.assertEqual(tl.shorter(), 1)

    def test_a_cut_splits_a_clip_with_a_small_gap(self) -> None:
        tl = self.timeline()
        clip = tl.place(1, 120)
        tl.t += 1
        self.assertEqual(tl.cuttable(set()), [clip])
        self.assertEqual(tl.cuttable({clip.key}), [])
        self.assertTrue(tl.split(clip.key, clip.s0 + 60))
        a, b = tl.tracks[1]
        self.assertEqual((a.s0, b.s1), (clip.s0, clip.s1))              # the track is as long as before
        self.assertAlmostEqual(b.s0 - a.s1, cc.CUT_GAP)
        self.assertFalse(tl.split(a.key, a.s0 + 2))                     # too close to the edge
        self.assertFalse(tl.split(clip.key, clip.s0 + 60))              # gone

    def test_full_it_sweeps_fades_and_starts_over_in_new_colours(self) -> None:
        tl = self.timeline()
        palette = tl.palette
        while tl.open(0) or tl.open(1):
            tl.place(tl.shorter(), 90)
        tl.step(0.01)
        self.assertEqual(tl.phase, "sweep")
        heads = []
        while tl.phase == "sweep":
            tl.step(0.1)
            heads.append(tl.playhead)
        self.assertEqual(heads, sorted(h for h in heads if h is not None) + [None])
        self.assertEqual(tl.phase, "fade")
        alphas = []
        while tl.phase == "fade":
            alphas.append(tl.alpha)
            tl.step(0.1)
        self.assertEqual(alphas, sorted(alphas, reverse=True))
        self.assertEqual((tl.phase, tl.cycles, tl.length, list(tl.clips())), ("build", 1, 0, []))
        self.assertNotEqual(tl.palette, palette)


# ============================================================================================
# The scene
# ============================================================================================

class SceneTests(unittest.TestCase):
    def test_the_robots_stay_in_the_band_and_out_of_the_widget(self) -> None:
        cases = [(seed, count, unit, widget) for seed, (count, unit, widget) in enumerate(
            [(4, 1.0, WIDGET), (12, 1.0, WIDGET), (7, 1.5, Rect(30, 950, 450, 1392)), (12, 1.25, Rect(1100, 700, 1400, 1392))])]
        for seed, count, unit, widget in cases:
            with self.subTest(seed=seed, count=count, unit=unit):
                geo = geometry(unit, widget)
                scene = cc.Scene(geo, random.Random(seed), count, awp=True)
                seen_states = set()

                def check(scene: cc.Scene) -> None:
                    for r in scene.robots:
                        seen_states.add(r.state)
                        if r.state == "work":
                            self.assertGreaterEqual(r.s - geo.robot_w / 2, 0, (r.job, r.phase))   # never in the box
                            self.assertTrue(geo.near - 1e-6 <= r.s <= geo.far + 1e-6, (r.job, r.s))
                    for item in scene.items(lambda p, f, x, y: (round(x - 18 * unit), round(y - 76 * unit),
                                                                round(x + 18 * unit), round(y))):
                        left, top, right, bottom = item.box
                        if item.kind in ("spark", "debris"):          # (clipped to the band when drawn)
                            self.assertTrue(geo.band.contains((left + right) / 2, (top + bottom) / 2, 2), item)
                            continue
                        self.assertGreaterEqual(top, geo.band.top, item.kind)
                        self.assertLessEqual(bottom, geo.band.bottom + 1, item.kind)
                        self.assertGreaterEqual(left, geo.work.left - 1, item.kind)
                        self.assertLessEqual(right, geo.work.right + 1, item.kind)
                        if item.kind == "robot":
                            if geo.side < 0:
                                self.assertLessEqual(right, geo.door)           # what shows is outside the box
                            else:
                                self.assertGreaterEqual(left, geo.door)
                simulate(scene, 100, dt=1 / 20, check=check, every=4)
                self.assertTrue({"enter", "work"} <= seen_states)

    def test_they_come_out_of_the_door_and_go_back_in(self) -> None:
        geo = geometry(awp=False)
        scene = cc.Scene(geo, random.Random(3), 5)
        first: dict[int, float] = {}

        def check(scene: cc.Scene) -> None:
            for r in scene.robots:
                if r.out and r.key not in first:
                    first[r.key] = r.s
        simulate(scene, 8, check=check)
        self.assertEqual(len(first), 5)
        self.assertTrue(all(s <= geo.home + 4 for s in first.values()), first)    # out of the box, one by one
        scene.touch()
        simulate(scene, 1)
        self.assertTrue(scene.over)
        self.assertTrue(all(r.state == "inside" and r.s == geo.home for r in scene.robots))

    def test_the_timeline_grows_and_starts_over(self) -> None:
        geo = geometry(widget=Rect(700, 500, 980, 800), work=Rect(0, 0, 1000, 800), awp=False)
        scene = cc.Scene(geo, random.Random(5), 12)
        splits = []
        split = scene.timeline.split
        scene.timeline.split = lambda key, at: splits.append(key) or split(key, at)
        history = []

        def check(scene: cc.Scene) -> None:
            tl = scene.timeline
            for track in tl.tracks:
                self.assertTrue(all(a.s1 <= b.s0 + 1e-6 for a, b in zip(track, track[1:])))   # end to end
                self.assertTrue(all(geo.start - 1e-6 <= c.s0 < c.s1 <= tl.limit + 1e-6 for c in track))
            history.append((tl.cycles, tl.length, tl.phase, tl.palette))
        simulate(scene, 120, check=check)
        cycles = {c for c, *_ in history}
        self.assertGreaterEqual(max(cycles), 2, "the timeline never started over")
        for cycle in cycles:
            lengths = [length for c, length, phase, _p in history if c == cycle]
            self.assertEqual(lengths, sorted(lengths))                  # it only grows within a cycle
        self.assertIn("sweep", {phase for _c, _l, phase, _p in history})
        palettes = [next(p for c, _l, _ph, p in history if c == cycle) for cycle in sorted(cycles)]
        self.assertTrue(all(a != b for a, b in zip(palettes, palettes[1:])))
        self.assertTrue(splits, "nobody cut a clip")

    def test_the_finale(self) -> None:
        for seed in range(3):
            with self.subTest(seed=seed):
                scene = cc.Scene(geometry(awp=False), random.Random(seed), 9)
                simulate(scene, 20)
                while scene.timeline.length < 400 or scene.timeline.phase != "build":
                    scene.step(1 / 30)
                out = [r for r in scene.robots if r.out]
                self.assertTrue(out)
                scene.finish()
                started = scene.t
                poses, shines = set(), 0

                def check(scene: cc.Scene) -> None:
                    nonlocal shines
                    if scene.t - started < cc.CHEER_S - 0.05:
                        poses.update(r.pose for r in scene.robots if r.out)
                        shines += any(item.kind == "shine" for item in scene.items(BOXER))
                simulate(scene, 10, check=check)
                self.assertTrue(scene.over)
                self.assertEqual(scene.reason, "done")
                self.assertLess(scene.t - started, 3.5)
                self.assertEqual(poses, {"robot-hop"})                  # everybody cheers …
                self.assertGreater(shines, 5)                           # … while the timeline shines
                self.assertFalse(any(r.out for r in scene.robots))
                self.assertEqual([i.kind for i in scene.items(BOXER)], [])   # nothing left on the screen

    def test_a_touch_sends_everybody_home_at_once(self) -> None:
        for seed in range(4):
            with self.subTest(seed=seed):
                scene = cc.Scene(geometry(), random.Random(seed), 12, awp=True)
                simulate(scene, 25 + seed * 3)
                scene.touch()
                started = scene.t
                simulate(scene, 2)
                self.assertTrue(scene.over)
                self.assertEqual(scene.reason, "touched")
                self.assertLessEqual(scene.t - started, 0.5)
                scene.finish()                                          # too late for a finale
                self.assertEqual(scene.reason, "touched")

    def test_the_awp(self) -> None:
        for seed in range(6):
            with self.subTest(seed=seed):
                geo = geometry()
                scene = cc.Scene(geo, random.Random(seed), 7, awp=True, stage="junior")
                lasers = []

                def check(scene: cc.Scene) -> None:
                    if scene.laser is not None:
                        lasers.append(scene.laser)
                        self.assertEqual(scene.laser[0], geo.muzzle)        # from Klippe's barrel
                        self.assertTrue(geo.band.contains(*scene.laser[1]))
                events = simulate(scene, 60, check=check)
                kinds = [e["event"] for _t, e in events]
                self.assertEqual(kinds[0], "aim")
                end = kinds.index("aim-end")
                shots = [e for _t, e in events[1:end]]
                self.assertEqual({e["event"] for e in shots}, {"shot"})
                self.assertGreaterEqual(len(shots), 2)
                self.assertTrue(shots[-1]["hit"])                       # the kill
                self.assertEqual(scene.kills, 1)
                aim_at, first_shot, ended = events[0][0], events[1][0], events[end][0]
                self.assertTrue(15 + 1.2 - 0.05 <= aim_at <= 40 + 1.8 + 0.05, aim_at)
                self.assertGreaterEqual(first_shot - aim_at, 0.4)     # Klippe turned first
                self.assertTrue(lasers)
                self.assertEqual(kinds[end + 1:], [])                   # at most every 60 s
                # The naughty robot is gone, a new one walked out of the box 1.5 s later.
                self.assertEqual(sum(1 for r in scene.robots if r.state == "dead"), 1)
                newcomer = scene.robots[-1]
                self.assertEqual(len(scene.robots), 8)
                self.assertAlmostEqual(newcomer.spawn_at, ended + cc.RESPAWN_S, delta=0.05)
                self.assertTrue(newcomer.out)
                self.assertEqual(sum(1 for r in scene.robots if r.out), 7)

    def test_a_touch_while_aiming_ends_the_aim(self) -> None:
        scene = cc.Scene(geometry(), random.Random(2), 5, awp=True)
        events = []
        while not scene.aiming:
            events += scene.step(1 / 30)
        scene.touch()
        self.assertEqual(scene.step(1 / 30), [{"event": "aim-end"}])
        self.assertIsNone(scene.laser)
        self.assertFalse(scene.aiming)

    def test_no_awp_no_shooting(self) -> None:
        for kwargs in ({"awp": False}, {"awp": True, "stage": "egg"}):
            with self.subTest(**kwargs):
                scene = cc.Scene(geometry(), random.Random(1), 5, **kwargs)
                self.assertEqual(simulate(scene, 100, dt=1 / 15), [])


# ============================================================================================
# The loop (fake painter)
# ============================================================================================

class FakePainter:
    def __init__(self, fail_after: int | None = None) -> None:
        self.frames = 0
        self.tops = 0
        self.hidden = 0
        self.fail_after = fail_after

    def boxer(self, pose, flip, x, y):
        return BOXER(pose, flip, x, y)

    def paint(self, items) -> None:
        self.frames += 1
        if self.fail_after is not None and self.frames > self.fail_after:
            raise OSError("drawing failed")

    def keep_on_top(self) -> None:
        self.tops += 1

    def pump(self) -> None:
        pass

    def hide(self) -> None:
        self.hidden += 1


class PlayerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tick = 500
        self.locked = False
        self.events: list[dict] = []
        self.painter = FakePainter()
        self.watch = pc.Watch(lambda: self.tick, lambda: self.locked)
        self.done = threading.Event()

    def player(self, scene=None, **kwargs) -> cc.Player:
        scene = scene or cc.Scene(geometry(awp=False), random.Random(1), 5)
        return cc.Player(scene, self.painter, self.watch, self.events.append, done=self.done, fps=200,
                         time_scale=kwargs.pop("time_scale", 8.0), **kwargs)

    def later(self, seconds: float, what: str) -> None:
        def run() -> None:
            time.sleep(seconds)
            if what == "key":
                self.tick += 1
            elif what == "lock":
                self.locked = True
            elif what == "quit":
                self.watch.quit.set()
            else:
                self.done.set()
        threading.Thread(target=run, daemon=True).start()

    def test_the_build_is_done(self) -> None:
        self.later(0.3, "done")
        started = time.monotonic()
        self.assertEqual(self.player().run(), "done")
        self.assertLess(time.monotonic() - started, 0.3 + 3.5 / 8 + 0.4)
        self.assertEqual(self.events[:2], [{"event": "side", "side": "left"}, {"event": "out"}])
        self.assertEqual(self.events[-1], {"event": "home", "reason": "done"})
        self.assertEqual(len(self.events), 3)
        self.assertGreater(self.painter.frames, 20)
        self.assertGreater(self.painter.tops, 0)                        # back on top now and then
        self.assertEqual(self.painter.hidden, 1)

    def test_a_touch_sends_them_home(self) -> None:
        self.later(0.2, "key")
        player = self.player()
        self.assertEqual(player.run(), "touched")
        self.assertEqual(self.events, [{"event": "side", "side": "left"}, {"event": "out"},
                                       {"event": "home", "reason": "touched"}])
        self.assertTrue(player.scene.over)

    def test_a_locked_screen_or_quit_stops_it_dead(self) -> None:
        for what in ("lock", "quit"):
            with self.subTest(what=what):
                self.setUp()
                self.later(0.15, what)
                started = time.monotonic()
                player = self.player()
                self.assertEqual(player.run(), "locked" if what == "lock" else "quit")
                self.assertLess(time.monotonic() - started, 0.15 + 0.5 + 0.3)
                self.assertFalse(player.scene.over)                     # no walk home: gone at once
                self.assertEqual(self.events[-1], {"event": "home", "reason": "locked" if what == "lock" else "quit"})

    def test_quit_while_aiming_ends_the_aim_first(self) -> None:
        scene = cc.Scene(geometry(), random.Random(1), 5, awp=True)
        while not scene.aiming:
            scene.step(1 / 30)
        self.later(0.05, "quit")
        self.assertEqual(self.player(scene).run(), "quit")
        self.assertEqual(self.events[-2:], [{"event": "aim-end"}, {"event": "home", "reason": "quit"}])

    def test_a_touch_before_it_started(self) -> None:
        self.watch = pc.Watch(lambda: self.tick, lambda: False, start_tick=self.tick - 1)
        self.assertEqual(self.player().run(), "touched")
        self.assertEqual(self.events, [{"event": "home", "reason": "touched"}])     # never out
        self.assertEqual(self.painter.frames, 0)

    def test_an_hour_is_enough(self) -> None:
        self.assertEqual(self.player(max_s=1.0).run(), "timeout")
        self.assertEqual(self.events[-1], {"event": "home", "reason": "timeout"})

    def test_an_error_still_sends_exactly_one_home(self) -> None:
        self.painter = FakePainter(fail_after=3)
        player = self.player()
        with self.assertRaises(OSError):
            player.run()
        self.assertTrue(player.home_sent)
        self.assertEqual([e for e in self.events if e["event"] == "home"], [{"event": "home", "reason": "error"}])

    def test_stdin(self) -> None:
        for data, done, quit in ((b"done\n", True, True), (b"done\r\nquit\n", True, True), (b"quit\n", False, True),
                                 (b"hello\n", False, True)):
            with self.subTest(data=data):
                done_event, quit_event = threading.Event(), threading.Event()
                cc._watch_stdin(io.BytesIO(data), quit_event, done_event)
                self.assertTrue(quit_event.wait(2))                     # quit, or the end of stdin
                self.assertEqual(done_event.is_set(), done)

    def test_main_without_a_widget_ends_with_one_error(self) -> None:
        class Stream:
            def __init__(self, data: bytes = b"") -> None:
                self.buffer = io.BytesIO(data)
        stdout = Stream()
        with mock.patch.object(pc, "_SetProcessDpiAwarenessContext", None), \
                mock.patch.object(cc, "_configure_logging"), \
                mock.patch.object(sys, "stdout", stdout), mock.patch.object(sys, "stdin", Stream()),                 self.assertLogs("projektsog.crew_child", "ERROR"):
            self.assertEqual(cc.main(["--sprites", "missing.png", "--widget", "0"]), 0)
        self.assertEqual(stdout.buffer.getvalue(), b'{"event": "home", "reason": "error"}\n')

    def test_the_command_line(self) -> None:
        args = cc.parse_args(["--sprites", "r.png", "--poses", ",".join(cc.ROBOT_POSES), "--cell", "120",
                              "--sheet-scale", "2", "--widget", "123", "--pet", "1,2,3,4", "--view", "260,448",
                              "--stage", "legend", "--robots", "12", "--awp", "1", "--seed", "7", "--input-tick", "99",
                              "--log-file", "x.log"])
        self.assertEqual((args.widget, args.robots, args.awp, args.seed, args.input_tick, args.cell, args.sheet_scale),
                         (123, 12, 1, 7, 99, 120, 2.0))
        self.assertEqual(args.poses.split(","), list(cc.ROBOT_POSES))


# ============================================================================================
# Drawing (GDI+, off screen)
# ============================================================================================

def write_robot_sheet(path: str, cells: int = 7, cell: int = 240) -> None:
    """A robot sheet: in every cell an opaque body (x 96–144, y 60–228: feet at 228) with a nose
    on its right (it faces right); the carrier (cell 2) holds a clip above its head."""
    rows = []
    for y in range(cell):
        row = bytearray()
        for c in range(cells):
            for x in range(cell):
                inside = 96 <= x < 144 and 60 <= y < 228
                inside = inside or (144 <= x < 156 and 100 <= y < 110)
                inside = inside or (c == 2 and 80 <= x < 160 and 20 <= y < 50)
                row += bytes((40, 50, 60, 255)) if inside else bytes(4)
        rows.append(b"\x00" + bytes(row))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", cell * cells, cell, 8, 6, 0, 0, 0))
                 + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b""))


class DrawingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dir = tempfile.TemporaryDirectory()
        cls.sheet = os.path.join(cls.dir.name, "robot.png")
        write_robot_sheet(cls.sheet)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.dir.cleanup()

    def setUp(self) -> None:
        self.gdiplus = pc.GdiPlus().__enter__()
        self.addCleanup(self.gdiplus.__exit__)
        self.sprites = cc.RobotSprites(self.sheet, cc.ROBOT_POSES, 120, 2, 1.0)
        self.addCleanup(self.sprites.close)

    def test_the_sprites(self) -> None:
        s = self.sprites
        self.assertAlmostEqual(s.h, 56)                                 # robot-a: 168 sheet px → 56 px
        self.assertAlmostEqual(s.w, 60 / 3)
        self.assertEqual(len(s.images), 14)                             # every pose, both ways
        left, top, right, bottom = s.box("robot-a", False, 100, 200)
        self.assertEqual((left, top), (round(100 + (95 - 120) / 3), round(200 + (59 - 228) / 3)))
        self.assertAlmostEqual(bottom, 200, delta=2)                    # standing on its feet
        mleft, _t, mright, _b = s.box("robot-a", True, 100, 200)
        self.assertAlmostEqual(100 - mright, left - 100, delta=1)       # mirrored round its middle
        self.assertLess(s.box("robot-baer", False, 100, 200)[1], top - 10)   # the clip above its head
        self.assertEqual(s.box("unknown", False, 100, 200), (left, top, right, bottom))
        os.remove(self.sheet)                                           # the file is not held open
        write_robot_sheet(self.sheet)

    def painter(self, width: int = 400, height: int = 200, origin=(1000, 1200)) -> cc.Painter:
        canvas = cc.BandCanvas(width, height, 1.0)
        self.addCleanup(canvas.close)
        return cc.Painter(canvas, self.sprites, origin)

    def test_robots_and_clips_land_where_they_should(self) -> None:
        painter = self.painter()
        alpha = lambda x, y: painter.canvas.pixel_alpha(x - 1000, y - 1200)     # noqa: E731
        clip = Item("clip", ("clip", 1), (1049, 1369, 1151, 1381), (1050, 1370, 100, 10, 0xFF3D7BEA))
        robot = Item("robot", ("robot", 1), self.sprites.box("robot-a", False, 1300, 1390),
                     ("robot-a", False, 1300, 1390, None))
        painter.paint([clip, robot])
        self.assertEqual(alpha(1100, 1375), 255)                        # the clip
        # The sheet's robot: body x 96–144 / y 60–228, nose x 144–156 / y 100–110, feet at 228,
        # its middle at x 120; a third of that on screen.
        self.assertEqual(alpha(1300, 1370), 255)                        # the robot's body
        self.assertEqual(alpha(1310, 1349), 255)                        # its nose, on the right
        self.assertEqual(alpha(1290, 1349), 0)
        self.assertEqual(alpha(1300, 1390 - 59), 0)                     # above its head
        self.assertEqual(alpha(1200, 1300), 0)                          # nothing between them
        self.assertEqual(painter.content, (1049, robot.box[1], robot.box[2], max(1381, robot.box[3])))
        # The robot walks on: only its old and new place are drawn again; the clip is left alone.
        moved = Item("robot", ("robot", 1), self.sprites.box("robot-b", True, 1330, 1390),
                     ("robot-b", True, 1330, 1390, None))
        painter.paint([clip, moved])
        self.assertEqual(alpha(1310, 1349), 0)
        self.assertEqual(alpha(1330, 1370), 255)
        self.assertEqual(alpha(1320, 1349), 255)                        # mirrored: the nose on the left
        self.assertEqual(alpha(1340, 1349), 0)
        self.assertEqual(alpha(1100, 1375), 255)
        self.assertTrue(painter.dirty)
        self.assertTrue(all(left > 1151 for left, *_ in painter.dirty), painter.dirty)   # the clip was not touched
        painter.paint([clip])
        self.assertEqual(alpha(1330, 1370), 0)                          # gone
        painter.paint([])
        self.assertEqual(alpha(1100, 1375), 0)
        self.assertIsNone(painter.content)

    def test_half_through_the_door_only_the_outside_shows(self) -> None:
        painter = self.painter()
        box = self.sprites.box("robot-a", False, 1200, 1390)
        robot = Item("robot", ("robot", 1), (box[0], box[1], 1200, box[3]), ("robot-a", False, 1200, 1390, 1200))
        painter.paint([robot])
        self.assertEqual(painter.canvas.pixel_alpha(195, 170), 255)
        self.assertEqual(painter.canvas.pixel_alpha(203, 170), 0)       # inside the widget: not drawn

    def test_a_whole_scene(self) -> None:
        geo = cc.crew_geometry(Rect(0, 0, 1600, 900), Rect(1300, 500, 1580, 900), 1.0, self.sprites.w,
                               self.sprites.h, (1340, 700), True)
        scene = cc.Scene(geo, random.Random(4), 9, awp=True)
        canvas = cc.BandCanvas(int(geo.band.width), int(geo.band.height), 1.0)
        self.addCleanup(canvas.close)
        painter = cc.Painter(canvas, self.sprites, (geo.band.left, geo.band.top))
        for _ in range(30 * 30):
            scene.step(1 / 30)
            painter.paint(scene.items(painter.boxer))
        items = scene.items(painter.boxer)
        clips = [i for i in items if i.kind == "clip"]
        robots = [i for i in items if i.kind == "robot"]
        self.assertTrue(clips and robots)
        for item in clips:
            x, y, w, h, _c = item.data
            self.assertGreater(canvas.pixel_alpha(x + w // 2 - geo.band.left, y + 2 - geo.band.top), 0)
        for item in robots:
            _pose, _flip, x, y, _cut = item.data
            self.assertEqual(canvas.pixel_alpha(x - geo.band.left, y - 20 - geo.band.top), 255)
        self.assertEqual(canvas.pixel_alpha(5, 5), 0)
        # Painting it all again from scratch gives the same picture.
        before = ctypes.string_at(canvas.bits.value, canvas.size * canvas.height * 4)
        painter.paint(items, full=True)
        self.assertEqual(ctypes.string_at(canvas.bits.value, canvas.size * canvas.height * 4), before)

    def test_the_laser_and_the_flash(self) -> None:
        painter = self.painter()
        painter.paint([Item("laser", ("laser",), (1015, 1295, 1205, 1325), (1020, 1300, 1200, 1320)),
                       Item("flash", ("flash",), (1007, 1287, 1033, 1313), (1020, 1300)),
                       Item("spark", ("spark", 1), (1260, 1260, 1300, 1300), (1280, 1280, 5.0, 16.0, 8, 0xFFFFD56B))])
        self.assertGreater(painter.canvas.pixel_alpha(110, 110), 150)  # on the line
        self.assertEqual(painter.canvas.pixel_alpha(110, 150), 0)
        self.assertEqual(painter.canvas.pixel_alpha(20, 100), 255)     # the flash's white core
        self.assertGreater(painter.canvas.pixel_alpha(280, 80), 0)      # the spark

    def test_the_window_is_never_activated_and_lets_clicks_through(self) -> None:
        band = cc.Band(64, 32, 1.0)
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.GetWindowLongW.restype = ctypes.c_long
            user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
            style = user32.GetWindowLongW(band.hwnd, -20) & 0xFFFFFFFF          # GWL_EXSTYLE
            for flag in (pc.WS_EX_LAYERED, pc.WS_EX_TRANSPARENT, pc.WS_EX_TOPMOST, pc.WS_EX_TOOLWINDOW,
                         pc.WS_EX_NOACTIVATE):
                self.assertTrue(style & flag, hex(flag))
            self.assertEqual(pc._wndproc(band.hwnd, pc.WM_NCHITTEST, 0, 0), pc.HTTRANSPARENT)
            band.canvas.rect(0, 0, 64, 32, 0xFF00FF00)
            self.assertTrue(band.show_box(-32000, -32000, (8, 4, 40, 20)))     # far off any screen
            band.keep_on_top()
        finally:
            band.close()

    def test_a_full_frame_is_quick(self) -> None:
        unit = 1.0
        geo = cc.crew_geometry(WORK, WIDGET, unit, self.sprites.w, self.sprites.h, MUZZLE, True)
        scene = cc.Scene(geo, random.Random(1), 12, awp=True)
        tl = scene.timeline
        while tl.open(0) or tl.open(1):
            tl.place(tl.shorter(), tl.base * 0.5)
        tl.t += 1
        tl.playhead = geo.start + 400
        for i, r in enumerate(scene.robots):
            r.state, r.s, r.pose = "work", geo.near + i * 120, cc.ROBOT_POSES[i % 7]
        canvas = cc.BandCanvas(int(geo.band.width), int(geo.band.height), unit)
        self.addCleanup(canvas.close)
        painter = cc.Painter(canvas, self.sprites, (geo.band.left, geo.band.top))
        items = scene.items(painter.boxer)
        self.assertEqual(sum(1 for i in items if i.kind == "robot"), 12)
        self.assertGreater(sum(1 for i in items if i.kind == "clip"), 20)
        painter.paint(items, full=True)                                 # (warm up)
        runs = 10
        started = time.perf_counter()
        for _ in range(runs):
            painter.paint(items, full=True)
        per_frame = (time.perf_counter() - started) / runs
        self.assertLess(per_frame, 0.015, f"{per_frame * 1000:.1f} ms")


if __name__ == "__main__":
    unittest.main()
