"""The robot crew out of the box: the helper process that draws Klippe's robots building a
timeline while a Claude session builds in Resolve (SPEC §21.3).

``projektsog.crew`` (main process) starts it while a build is on and the rules allow it. It

* draws every robot, the timeline and the laser in ONE transparent window over a band at the
  bottom of the widget's monitor – a window that never takes the focus and that every click goes
  through (a layered window drawn with GDI+ from a robot sprite sheet the widget page rendered),
* lets the robots walk out of the widget's side, carry clips out of the box (``robot-baer``),
  snip them (``robot-klip``) and lay a two-track timeline on the floor; a full timeline gets a
  playhead sweep and starts over in new colours,
* with Klippe's AWP: now and then one robot turns naughty and Klippe shoots it from the widget.

It sends them home at once when the user touches the mouse or the keyboard (the session's
last-input time changes) – they rush back into the box within ``RUSH_S`` – and ends. ``done`` on
stdin is the finale: a shine across the timeline, the robots cheer, then march home. A locked
screen, ``quit``/stdin EOF and ``MAX_S`` end it too. It never moves the pointer, clicks or types.

Events on stdout, one JSON object per line: ``{"event": "out"}`` after the first frame,
``{"event": "aim"}``, ``{"event": "shot", "hit": bool}``, ``{"event": "aim-end"}`` (the AWP),
then exactly one ``{"event": "home", "reason": "done" | "touched" | "locked" | "quit" |
"timeout" | "error"}``.

The first half of the module is the scene – pure arithmetic, stepped by time and tested without a
screen. The second half draws it (Win32/GDI+ through ctypes, shared with ``petplay_child``).
"""

from __future__ import annotations

import argparse
import ctypes
import itertools
import logging
import logging.handlers
import math
import os
import random
import sys
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any, BinaryIO, NamedTuple

from . import petplay_child as pc
from .petplay_child import Hunt, Point, Rect, hunt_plan

log = logging.getLogger(__name__)

ROBOT_POSES = ("robot-a", "robot-b", "robot-baer", "robot-klip", "robot-hop", "robot-fraek", "robot-panik")
WALK_POSES = ("robot-a", "robot-b")
FPS = 30
MAX_S = 3600.0                # an outing never lasts longer than this ("timeout")
TOPMOST_S = 2.0               # how often the window is put on top again
# Klippe's AWP in the widget: the barrel end of the MIRRORED aim pose, in pet-SVG units, is
# (100 − 92·S, 180 − 54.75·S) with S the stage's body scale (an egg has no body and no AWP).
MUZZLE_SCALE = {"baby": 0.74, "junior": 0.86, "pro": 0.95, "legend": 1.0}

# Sizes in CSS pixels (× unit = physical pixels)
ROBOT_PX = 56                 # a robot's height on screen (the robot-a pose)
FLOOR_MARGIN = 4              # the floor: this far above the bottom of the work area
BAND_MIN = 260                # the window: at least this high …
MUZZLE_ROOM = 40              # … and up to this far above Klippe's muzzle
TIMELINE_START = 30           # the first frame of the timeline, from the door (the labels before it)
TRACK_H, TRACK_GAP, TRACK_LIFT = 11, 3, 2
MIN_CLIP = 24                 # no clip is narrower (a cut needs twice this)
CUT_GAP = 3                   # the gap a cut leaves
CLIP_RADIUS = 3
DEPTHS = (0, 2, 4)            # robots stand a little in front of / behind each other
# Speeds in CSS pixels per second
WALK, CARRY, RUN = 105.0, 125.0, 200.0
WALK_HZ = 6.0                 # robot-a / robot-b alternate this often
# Durations in seconds
SPAWN_GAP_S = 0.35            # the robots come out one after the other
RUSH_S = 0.45                 # touched: every robot is back in the box within this
CHEER_S = 1.2                 # done: the robots cheer …
MARCH_S = 1.9                 # … and march home within this
SHINE_S = 1.0
SWEEP_S, FADE_S = 2.4, 0.6    # a full timeline: the playhead sweep, then it fades
DROP_S = 0.25                 # a placed clip drops into its track
SNIP_S = 0.7
FIRST_AWP_S = (15.0, 40.0)    # the first naughty robot …
AWP_GAP_S = 60.0              # … and the next one at the earliest this long after
NAUGHTY_S = (1.2, 1.8)
AIM_WAIT_S = 0.4              # Klippe turns in the widget
RESPAWN_S = 1.5
FLASH_S = 0.07

# Colours (ARGB / RGB)
PALETTES = ((0x3D7BEA, 0x2FB463), (0x5A67E6, 0x22A58C), (0x2E9CDB, 0x5DB84A),
            (0x7158E2, 0x1EA97C), (0x3F8CD6, 0x48C78E))       # (V1 video, A1 audio)
LABEL_FILL = 0xC81E2430
PLAYHEAD = 0xFFE5484D
SPARK = 0xFFFFD56B
METAL = (0xFF8A9BA8, 0xFFB0BEC5, 0xFF5C6B73, 0xFFD0D8DC)
LASER = ((6.0, 0x40FF2A2A), (2.0, 0xE0FF3030))                  # (width, colour) as Klippe's game
FLASH = ((11, 0xC0FF8A3D), (7, 0xF0FFD56B), (3.5, 0xFFFFFFFF))


# ============================================================================================
# The scene (pure)
# ============================================================================================

class CrewGeometry(NamedTuple):
    """Where the robots work (physical pixels). Along the floor everything is measured as ``s``,
    the distance from the door – the widget's side facing the larger free part of the monitor."""
    work: Rect               # the monitor's work area
    band: Rect               # the window: the work area's width, from above the muzzle to the bottom
    door: float              # x of the widget's side the robots use
    side: int                # +1: the robots work to the right of the widget, −1: to the left
    floor: float             # y of the robots' feet
    unit: float              # physical pixels per CSS pixel
    robot_w: float
    robot_h: float
    muzzle: Point            # the end of Klippe's barrel in the widget
    start: float             # the timeline's first frame (s)
    length: float            # its full length
    near: float              # the yard, where robots work: from just outside the door …
    far: float               # … to a bit beyond the timeline's end

    def x(self, s: float) -> float:
        return self.door + self.side * s

    def s(self, x: float) -> float:
        return (x - self.door) * self.side

    @property
    def home(self) -> float:
        """Where a robot is fully back in the box."""
        return -(self.robot_w / 2 + 2 * self.unit)

    def track(self, index: int) -> tuple[float, float]:
        """``(top, bottom)`` of a track: 0 = V1 (video, the upper one), 1 = A1 (audio)."""
        u = self.unit
        bottom = self.floor - TRACK_LIFT * u - (1 - index) * (TRACK_H + TRACK_GAP) * u
        return bottom - TRACK_H * u, bottom


def muzzle_units(stage: str, side: int = -1) -> Point:
    """The end of the AWP's barrel in Klippe's aim pose (pet-SVG units): mirrored when the robots
    work to the left of the widget (``side`` −1), as drawn when they work to its right (+1)."""
    scale = MUZZLE_SCALE.get(stage, MUZZLE_SCALE["baby"])
    return 100 + side * 92 * scale, 180 - 54.75 * scale


def crew_side(work: Rect, widget: Rect) -> int:
    """+1: the robots work to the right of the widget, −1: to the left (the larger free part)."""
    return 1 if max(0.0, work.right - widget.right) > max(0.0, widget.left - work.left) else -1


def crew_geometry(work: Rect, widget: Rect, unit: float, robot_w: float, robot_h: float,
                  muzzle: Point | None = None, awp: bool = False) -> CrewGeometry:
    u = unit
    free_left = max(0.0, widget.left - work.left)
    free_right = max(0.0, work.right - widget.right)
    side = crew_side(work, widget)
    door = min(max(widget.right if side > 0 else widget.left, work.left), work.right)
    free = free_right if side > 0 else free_left
    floor = work.bottom - FLOOR_MARGIN * u
    start = TIMELINE_START * u
    end_room = max(24 * u, robot_w * 0.6 + 4 * u)
    length = max(0.0, min(free - start - end_room, 0.6 * work.width))
    near = robot_w * 0.6 + 2 * u
    far = max(near, min(free - robot_w * 0.6, start + length + 30 * u))
    if muzzle is None:
        muzzle = (door - side * 40 * u, floor - 120 * u)
    top = work.bottom - BAND_MIN * u
    if awp:
        top = min(top, muzzle[1] - MUZZLE_ROOM * u)
    band = Rect(work.left, max(work.top, math.floor(top)), work.right, work.bottom)
    return CrewGeometry(work=work, band=band, door=door, side=side, floor=floor, unit=u, robot_w=robot_w,
                        robot_h=robot_h, muzzle=muzzle, start=start, length=length, near=near, far=far)


class Clip(NamedTuple):
    key: int
    track: int
    s0: float
    s1: float
    colour: int              # RGB
    born: float              # when it was placed (it drops into its track)


def shade(rgb: int, k: float) -> int:
    r, g, b = (rgb >> 16) & 255, (rgb >> 8) & 255, rgb & 255
    return (min(255, round(r * k)) << 16) | (min(255, round(g * k)) << 8) | min(255, round(b * k))


class Timeline:
    """Two tracks of clips on the floor, growing away from the door. Full: the playhead sweeps
    it, it fades, and it starts over in new colours."""

    def __init__(self, geo: CrewGeometry, rng: random.Random) -> None:
        self.geo = geo
        self.rng = rng
        self.tracks: tuple[list[Clip], list[Clip]] = ([], [])
        self.palette = rng.randrange(len(PALETTES))
        self.phase = "build"          # build | sweep | fade
        self.phase_t = 0.0
        self.playhead: float | None = None
        self.cycles = 0               # how often it started over
        self.min_clip = MIN_CLIP * geo.unit
        self.base = max(self.min_clip * 1.6, geo.length / 8)
        self._keys = itertools.count()
        self._shine_at: float | None = None
        self._leave: tuple[float, float] | None = None     # (from, seconds): the robots go home
        self.t = 0.0

    @property
    def limit(self) -> float:
        return self.geo.start + self.geo.length

    def end(self, track: int) -> float:
        clips = self.tracks[track]
        return clips[-1].s1 if clips else self.geo.start

    def room(self, track: int) -> float:
        return self.limit - self.end(track)

    def open(self, track: int) -> bool:
        return self.phase == "build" and self._leave is None and self.room(track) >= self.min_clip

    @property
    def length(self) -> float:
        return max(self.end(0), self.end(1)) - self.geo.start

    def clips(self) -> Iterable[Clip]:
        return itertools.chain(*self.tracks)

    def find(self, key: int) -> Clip | None:
        return next((c for c in self.clips() if c.key == key), None)

    def colour(self, track: int) -> int:
        return shade(PALETTES[self.palette][track], self.rng.uniform(0.84, 1.14))

    def shorter(self) -> int:
        a, b = self.open(0), self.open(1)
        if a != b:
            return 0 if a else 1
        ends = self.end(0), self.end(1)
        return self.rng.randrange(2) if ends[0] == ends[1] else (0 if ends[0] < ends[1] else 1)

    def place(self, track: int, width: float) -> Clip | None:
        if not self.open(track):
            return None
        room = self.room(track)
        width = max(self.min_clip, min(width, room))
        if room - width < self.min_clip:           # no sliver left at the end
            width = room
        end = self.end(track)
        clip = Clip(next(self._keys), track, end, end + width, self.colour(track), self.t)
        self.tracks[track].append(clip)
        return clip

    def cuttable(self, busy: set[int]) -> list[Clip]:
        if self.phase != "build" or self._leave is not None:
            return []
        wide = 2 * self.min_clip + CUT_GAP * self.geo.unit
        return [c for c in self.clips() if c.s1 - c.s0 >= wide and c.key not in busy
                and self.t - c.born > DROP_S]

    def split(self, key: int, at: float) -> bool:
        """A cut at ``at``: the clip becomes two with a small gap between them."""
        half = CUT_GAP * self.geo.unit / 2
        for clips in self.tracks:
            for i, c in enumerate(clips):
                if c.key == key:
                    if not (c.s0 + self.min_clip / 2 <= at - half and at + half <= c.s1 - self.min_clip / 2):
                        return False
                    clips[i:i + 1] = [c._replace(key=next(self._keys), s1=at - half),
                                      c._replace(key=next(self._keys), s0=at + half,
                                                 colour=shade(c.colour, self.rng.uniform(0.93, 1.07)))]
                    return True
        return False

    def steal(self) -> Clip | None:
        """A naughty robot runs off with the last clip of the longer track."""
        if self.phase != "build":
            return None
        track = 0 if self.end(0) >= self.end(1) else 1
        if not self.tracks[track]:
            return None
        return self.tracks[track].pop()

    def shine(self) -> None:
        self._shine_at = self.t

    def leave(self, seconds: float) -> None:
        if self._leave is None:
            self._leave = (self.t, max(1e-3, seconds))

    @property
    def shine_at(self) -> float | None:
        """Where the shine is now (s of its leading edge), None when there is none."""
        if self._shine_at is None:
            return None
        k = (self.t - self._shine_at) / SHINE_S
        if k > 1:
            return None
        width = 18 * self.geo.unit
        return self.geo.start - width + k * (self.length + 2 * width)

    @property
    def gone(self) -> float:
        """1 while the robots work, falling to 0 while they go home."""
        if self._leave is None:
            return 1.0
        since, seconds = self._leave
        return max(0.0, 1 - (self.t - since) / seconds)

    @property
    def alpha(self) -> float:
        fade = 1 - min(1.0, self.phase_t / FADE_S) if self.phase == "fade" else 1.0
        return fade * self.gone

    def step(self, dt: float) -> None:
        self.t += dt
        if self.phase == "build":
            if self._leave is None and self.length > 0 and not self.open(0) and not self.open(1):
                self.phase, self.phase_t = "sweep", 0.0
                self.playhead = self.geo.start
        elif self.phase == "sweep":
            self.phase_t += dt
            self.playhead = self.geo.start + self.length * min(1.0, self.phase_t / SWEEP_S)
            if self.phase_t >= SWEEP_S:
                self.phase, self.phase_t, self.playhead = "fade", 0.0, None
        elif self.phase == "fade":
            self.phase_t += dt
            if self.phase_t >= FADE_S:
                self.tracks[0].clear()
                self.tracks[1].clear()
                self.palette = (self.palette + 1) % len(PALETTES)
                self.phase, self.phase_t = "build", 0.0
                self.cycles += 1


@dataclass
class Robot:
    key: int
    depth: float                  # its feet are this far above the floor line (further back)
    spawn_at: float               # when it walks out of the box
    bob: float = 0.0              # its own phase for bobbing on the spot
    state: str = "inside"         # inside | enter | work | home | dead
    s: float = 0.0                # distance from the door (its centre)
    lift: float = 0.0             # how far above its standing line (hops, steps)
    facing: int = 1               # +1 away from the door, −1 towards it
    pose: str = "robot-a"
    job: str = ""                 # wander | carry | cut | naughty | cheer
    phase: str = ""
    target: float = 0.0
    until: float = 0.0
    speed: float = 0.0
    walked: float = 0.0           # its walking clock (the a/b poses)
    track: int = 0
    width: float = 0.0            # the clip it brings
    clip: int = -1                # the clip it cuts
    held: int | None = None       # the colour of a clip a naughty robot runs off with

    @property
    def out(self) -> bool:
        return self.state in ("enter", "work", "home")


@dataclass
class Chase:
    """A naughty robot and Klippe's hunt for it."""
    robot: Robot
    style: str                    # dance | run
    phase: str                    # naughty | wait | hunt
    until: float
    plan: Hunt | None = None
    started: float = 0.0
    shots: int = 0
    centre: float = 0.0           # y of the robot's middle
    panic: bool = False


@dataclass
class Spark:
    key: int
    x: float
    y: float
    born: float
    life: float
    size: float                   # the rays' reach
    rays: int
    colour: int


@dataclass
class Debris:
    key: int
    x: float
    y: float
    vx: float
    vy: float
    born: float
    life: float
    size: float
    colour: int
    floor: float


class Item(NamedTuple):
    """Something to draw: ``box`` (left, top, right, bottom; physical screen pixels, exclusive)
    and ``data`` – both compared with the last frame's to find what changed."""
    kind: str
    key: tuple
    box: tuple[int, int, int, int]
    data: tuple


def _box(left: float, top: float, right: float, bottom: float, pad: float = 1.0) -> tuple[int, int, int, int]:
    return (math.floor(left - pad), math.floor(top - pad), math.ceil(right + pad), math.ceil(bottom + pad))


class Scene:
    """The robots, the timeline and the AWP, stepped by time (``step``)."""

    def __init__(self, geo: CrewGeometry, rng: random.Random, count: int, *, awp: bool = False,
                 stage: str = "baby") -> None:
        self.geo = geo
        self.rng = rng
        self.t = 0.0
        self.mode = "work"                    # work | rush | cheer | march
        self.reason: str | None = None        # why they went home
        self.timeline = Timeline(geo, rng)
        self._keys = itertools.count()
        u = geo.unit
        self.robots = [Robot(next(self._keys), rng.choice(DEPTHS) * u, i * SPAWN_GAP_S + rng.uniform(0, 0.15),
                             bob=rng.uniform(0, 2 * math.pi)) for i in range(max(1, count))]
        self.awp = awp and stage in MUZZLE_SCALE
        self.next_hunt = rng.uniform(*FIRST_AWP_S) if self.awp else math.inf
        self.chase: Chase | None = None
        self.aiming = False                   # "aim" sent, "aim-end" not yet
        self.laser: tuple[Point, Point] | None = None
        self.flash_until = -1.0
        self.kills = 0
        self.respawns: list[float] = []
        self.sparks: list[Spark] = []
        self.debris: list[Debris] = []
        self._cheer_until = 0.0
        self._outbox: list[dict[str, Any]] = []
        xs = sorted((geo.x(geo.near), geo.x(geo.far)))
        # Where the laser and the hunted robot may be (hunt_plan's "play" rectangle).
        self.hunt_area = Rect(xs[0], geo.band.top + 8 * u, xs[1], max(geo.band.top + 8 * u, geo.floor - 6 * u))

    # -- the outside world ----------------------------------------------------------------
    @property
    def over(self) -> bool:
        """Every robot is back in the box after ``touch`` or ``finish``."""
        return self.mode in ("rush", "march") and not any(r.out for r in self.robots)

    def touch(self, reason: str = "touched") -> None:
        """Everybody home, now (within ``RUSH_S``)."""
        if self.mode == "rush":
            return
        self.mode, self.reason = "rush", reason
        self._stop_chase()
        self.sparks.clear()
        self.debris.clear()
        self.respawns.clear()
        self.timeline.leave(RUSH_S)
        for r in self.robots:
            if r.out:
                r.state = "home"
                r.speed = max(RUN * self.geo.unit, (r.s - self.geo.home) / (RUSH_S - 0.02))
            elif r.state == "inside":
                r.spawn_at = math.inf

    def finish(self) -> None:
        """The build is done: a shine across the timeline, the robots cheer, then march home."""
        if self.mode != "work":
            return
        self.mode, self.reason = "cheer", "done"
        self._cheer_until = self.t + CHEER_S
        self._stop_chase()
        self.respawns.clear()
        self.timeline.shine()
        for r in self.robots:
            if r.state == "inside":
                r.spawn_at = math.inf

    def _stop_chase(self) -> None:
        if self.aiming:
            self._outbox.append({"event": "aim-end"})
            self.aiming = False
        if self.chase is not None:
            self.chase.robot.job = ""
            self.chase = None
        self.laser = None
        self.flash_until = -1.0

    # -- time -------------------------------------------------------------------------------
    def step(self, dt: float) -> list[dict[str, Any]]:
        """Moves everything on by ``dt`` seconds; returns the events that happened."""
        self.t += dt
        if self.mode == "cheer" and self.t >= self._cheer_until:
            self.mode = "march"
            self.timeline.leave(MARCH_S)
            for r in self.robots:
                if r.out:
                    r.state = "home"
                    r.speed = max(WALK * 1.3 * self.geo.unit, (r.s - self.geo.home) / MARCH_S)
        if self.mode == "work":
            due = [at for at in self.respawns if at <= self.t]
            for _ in due:
                self.robots.append(Robot(next(self._keys), self.rng.choice(DEPTHS) * self.geo.unit, self.t,
                                         bob=self.rng.uniform(0, 2 * math.pi)))
            self.respawns = [at for at in self.respawns if at > self.t]
            if self.chase is None and self.t >= self.next_hunt:
                self._start_chase()
        if self.chase is not None:
            self._step_chase(dt)
        for r in self.robots:
            if self.chase is None or r is not self.chase.robot:
                self._step_robot(r, dt)
        self.timeline.step(dt)
        self._step_bits(dt)
        events, self._outbox = self._outbox, []
        return events

    # -- robots -----------------------------------------------------------------------------
    def _walk(self, r: Robot, target: float, speed: float, dt: float, poses: bool = True) -> bool:
        d = target - r.s
        reach = speed * dt
        r.walked += dt
        if abs(d) <= reach:
            r.s = target
            arrived = True
        else:
            r.s += reach if d > 0 else -reach
            r.facing = 1 if d > 0 else -1
            arrived = False
        if poses:
            r.pose = WALK_POSES[int(r.walked * WALK_HZ) % 2]
        r.lift = abs(math.sin(math.pi * WALK_HZ * r.walked)) * 1.5 * self.geo.unit
        return arrived

    def _idle(self, r: Robot, pose: str = "robot-a") -> None:
        r.pose = pose
        r.lift = (1 + math.sin(2 * math.pi * 1.4 * self.t + r.bob)) * self.geo.unit

    def _yard(self, s: float) -> float:
        return min(max(s, self.geo.near), self.geo.far)

    def _step_robot(self, r: Robot, dt: float) -> None:
        g = self.geo
        if r.state == "inside":
            if self.mode == "work" and self.t >= r.spawn_at:
                r.state, r.s, r.facing, r.job = "enter", g.home, 1, ""
                r.target = self._yard(g.near + self.rng.uniform(0, 40 * g.unit))
            return
        if r.state == "dead":
            return
        if r.state == "home":
            if self._walk(r, g.home, r.speed, dt):
                r.state, r.spawn_at, r.lift = "inside", math.inf, 0.0
            return
        if self.mode == "cheer":
            r.job = "cheer"
            r.pose = "robot-hop"
            r.lift = abs(math.sin(math.pi * 2.5 * self.t + r.bob)) * 12 * g.unit
            return
        if r.state == "enter":
            if self._walk(r, r.target, WALK * g.unit, dt):
                r.state = "work"
                self._new_job(r)
            return
        {"carry": self._carry, "cut": self._cut}.get(r.job, self._wander)(r, dt)

    def _new_job(self, r: Robot) -> None:
        tl, g = self.timeline, self.geo
        roll = self.rng.random()
        r.phase, r.held = "go", None
        if roll < 0.55 and (tl.open(0) or tl.open(1)):
            r.job, r.phase = "carry", "fetch"
            return
        if roll < 0.8:
            busy = {o.clip for o in self.robots if o.job == "cut" and o is not r}
            choices = tl.cuttable(busy)
            if choices:
                clip = self.rng.choice(choices)
                r.job, r.clip = "cut", clip.key
                r.target = self._yard(clip.s0 + (clip.s1 - clip.s0) * self.rng.uniform(0.35, 0.65))
                return
        r.job = "wander"
        r.target = self.rng.uniform(g.near, g.far)

    def _wander(self, r: Robot, dt: float) -> None:
        if r.phase == "go":
            if self._walk(r, r.target, WALK * self.geo.unit, dt):
                r.phase, r.until = "rest", self.t + self.rng.uniform(0.8, 2.6)
                self._idle(r)
            return
        self._idle(r)
        if self.t >= r.until:
            self._new_job(r)

    def _carry(self, r: Robot, dt: float) -> None:
        tl, g = self.timeline, self.geo
        if r.phase == "fetch":                     # to the door: a clip from the box
            if self._walk(r, g.near, CARRY * g.unit, dt):
                r.phase, r.until, r.facing = "pickup", self.t + 0.35, -1
                self._idle(r)
        elif r.phase == "pickup":
            self._idle(r)
            if self.t >= r.until:
                r.phase, r.track = "bring", tl.shorter()
                r.width = tl.base * self.rng.uniform(0.6, 1.4)
                r.pose = "robot-baer"
        elif r.phase == "bring":                   # to the end of a track
            if not tl.open(r.track) and tl.open(1 - r.track):
                r.track = 1 - r.track
            if not tl.open(r.track):               # full (the playhead sweeps): wait with it
                self._idle(r, "robot-baer")
                return
            end = tl.end(r.track)
            target = self._yard(end + min(r.width, tl.room(r.track)) / 2)
            arrived = self._walk(r, target, CARRY * g.unit, dt, poses=False)
            r.pose = "robot-baer"
            if arrived and tl.place(r.track, r.width) is not None:
                r.phase, r.until = "place", self.t + 0.3
                self._idle(r)
        else:                                      # placed: a breath, then the next job
            self._idle(r)
            if self.t >= r.until:
                self._new_job(r)

    def _cut(self, r: Robot, dt: float) -> None:
        tl, g = self.timeline, self.geo
        clip = tl.find(r.clip)
        if clip is None or tl.phase != "build" or not (clip.s0 < r.target < clip.s1):
            self._new_job(r)
            return
        if r.phase == "go":
            if self._walk(r, r.target, WALK * g.unit, dt):
                r.phase, r.until = "snip", self.t + SNIP_S
            return
        r.pose = "robot-klip"
        r.lift = abs(math.sin(math.pi * 5 * (r.until - self.t))) * 1.5 * g.unit
        if self.t >= r.until:
            if tl.split(r.clip, r.target):
                top, bottom = g.track(clip.track)
                self.sparks.append(Spark(next(self._keys), g.x(r.target), (top + bottom) / 2, self.t, 0.3,
                                         9 * g.unit, 6, SPARK))
            self._new_job(r)

    # -- the AWP ----------------------------------------------------------------------------
    def _start_chase(self) -> None:
        candidates = [r for r in self.robots if r.state == "work" and r.job in ("wander", "carry", "cut")]
        if not candidates:
            self.next_hunt = self.t + 2.0
            return
        r = self.rng.choice(candidates)
        g = self.geo
        held = None
        if r.job == "carry" and r.phase == "bring":
            held = self.timeline.colour(r.track)          # it runs off with the clip it brings
        elif self.rng.random() < 0.5:
            stolen = self.timeline.steal()
            held = stolen.colour if stolen is not None else None
        style = "run" if held is not None else "dance"
        r.job, r.phase, r.held = "naughty", style, held
        r.target = g.far if r.s < g.far - 120 * g.unit else (g.near + g.far) / 2
        self.chase = Chase(r, style, "naughty", self.t + self.rng.uniform(*NAUGHTY_S))
        log.info("a robot turns naughty (%s)", style)

    def _dance(self, r: Robot) -> None:
        r.pose = "robot-fraek"
        r.facing = 1 if int(self.t / 0.28) % 2 else -1
        r.lift = abs(math.sin(math.pi * self.t / 0.28)) * 7 * self.geo.unit

    def _step_chase(self, dt: float) -> None:
        c = self.chase
        assert c is not None
        r, g, u = c.robot, self.geo, self.geo.unit
        if c.phase == "naughty":
            if c.style == "run" and abs(r.s - r.target) > 0.5:
                self._walk(r, r.target, RUN * u, dt, poses=False)
                r.pose = "robot-fraek"
            else:
                self._dance(r)
            if self.t >= c.until:
                self._outbox.append({"event": "aim", "side": "right" if self.geo.side > 0 else "left"})
                self.aiming = True
                c.phase, c.until = "wait", self.t + AIM_WAIT_S
            return
        if c.phase == "wait":
            self._dance(r)
            if self.t >= c.until:
                c.centre = g.floor - r.depth - g.robot_h / 2
                start = self.hunt_area.clamp(g.x(r.s), c.centre)
                c.plan = hunt_plan(self.rng, self.hunt_area, g.muzzle, start, u)
                c.phase, c.started = "hunt", self.t
            return
        plan = c.plan
        assert plan is not None
        t = self.t - c.started
        i = plan.index(t)
        px, py = plan.pointer[i]
        shift = c.centre - py                      # the robot stays on the floor: the laser follows
        if r.state != "dead":
            r.s = g.s(px)
            if c.panic:
                r.pose, r.facing = "robot-panik", -1
                r.lift = (1 if int(self.t * 24) % 2 else 0) * 1.5 * u
            else:
                self._dance(r)
        ax, ay = plan.aim[i]
        self.laser = (g.muzzle, g.band.clamp(ax, ay + shift))
        while c.shots < len(plan.shots) and plan.shots[c.shots][0] <= t:
            _when, outcome, where = plan.shots[c.shots]
            c.shots += 1
            self._outbox.append({"event": "shot", "hit": outcome != "miss"})
            self.flash_until = self.t + FLASH_S
            wx, wy = g.band.clamp(where[0], where[1] + shift)
            if outcome == "kill" and r.state != "dead":
                self._burst(r, wx, c.centre)
            else:
                c.panic = True
                self.sparks.append(Spark(next(self._keys), wx, wy, self.t, 0.3, 26 * u if outcome == "hit" else 14 * u,
                                         8, SPARK if outcome == "hit" else 0xFFFFFFFF))
        if t >= plan.seconds:
            self._outbox.append({"event": "aim-end"})
            self.aiming = False
            self.laser = None
            self.chase = None
            self.respawns.append(self.t + RESPAWN_S)
            self.next_hunt = self.t + AWP_GAP_S + self.rng.uniform(0, 30)

    def _burst(self, r: Robot, x: float, y: float) -> None:
        """The kill: the robot bursts into sparks and a few bits that fall to the floor."""
        g, u = self.geo, self.geo.unit
        r.state, r.held, r.job = "dead", None, ""
        self.kills += 1
        self.sparks.append(Spark(next(self._keys), x, y, self.t, 0.45, 40 * u, 12, SPARK))
        floor = g.floor - r.depth
        for _ in range(10):
            self.debris.append(Debris(next(self._keys), x + self.rng.uniform(-8, 8) * u, y + self.rng.uniform(-10, 10) * u,
                                      self.rng.uniform(-260, 260) * u, self.rng.uniform(-520, -160) * u, self.t,
                                      self.rng.uniform(0.9, 1.4), self.rng.uniform(3, 6) * u, self.rng.choice(METAL),
                                      floor))
        log.info("Klippe got the naughty robot")

    def _step_bits(self, dt: float) -> None:
        self.sparks = [s for s in self.sparks if self.t - s.born < s.life]
        gravity = 1800 * self.geo.unit
        left, right = self.geo.band.left, self.geo.band.right
        for d in self.debris:
            d.vy += gravity * dt
            d.x = min(max(d.x + d.vx * dt, left), right)
            d.y += d.vy * dt
            if d.y > d.floor - d.size / 2:
                d.y = d.floor - d.size / 2
                d.vy = -d.vy * 0.35 if abs(d.vy) > 120 * self.geo.unit else 0.0
                d.vx *= 0.6
        self.debris = [d for d in self.debris if self.t - d.born < d.life]

    # -- what to draw -----------------------------------------------------------------------
    def items(self, boxer: Callable[[str, bool, float, float], tuple[int, int, int, int]]) -> list[Item]:
        """Everything in drawing order (back to front). ``boxer(pose, flip, x, y)`` is where a
        robot sprite lands with its feet at (x, y)."""
        g, u, tl = self.geo, self.geo.unit, self.timeline
        out: list[Item] = []
        alpha = tl.alpha
        gone = tl.gone
        label_w = (TIMELINE_START - 8) * u
        for index, text in enumerate(("V1", "A1")):
            if gone <= 0.01:
                break
            top, bottom = g.track(index)
            xs = sorted((g.x(4 * u), g.x(4 * u + label_w)))
            box = _box(xs[0], top, xs[1], bottom)
            out.append(Item("label", ("label", index), box,
                            (round(xs[0]), round(top), round(xs[1] - xs[0]), round(bottom - top), text, round(gone * 255))))
        if alpha > 0.01:
            a = round(alpha * 255)
            for clip in tl.clips():
                top, bottom = g.track(clip.track)
                k = (tl.t - clip.born) / DROP_S
                if k < 1:                               # dropping into its track from the robot's hands
                    drop = (1 - k * k) * g.robot_h * 0.8
                    top, bottom = top - drop, bottom - drop
                xs = sorted((g.x(clip.s0), g.x(clip.s1)))
                x0, x1 = round(xs[0]), round(xs[1])
                data = (x0, round(top), max(1, x1 - x0), round(bottom - top), (a << 24) | clip.colour)
                out.append(Item("clip", ("clip", clip.key), _box(x0, round(top), x1, round(bottom)), data))
            shine = tl.shine_at
            if shine is not None:
                width = 18 * u
                rects = []
                for clip in tl.clips():
                    s0, s1 = max(clip.s0, shine), min(clip.s1, shine + width)
                    if s1 > s0:
                        top, bottom = g.track(clip.track)
                        xs = sorted((g.x(s0), g.x(s1)))
                        rects.append((round(xs[0]), round(top), round(xs[1]), round(bottom)))
                if rects:
                    box = _box(min(r[0] for r in rects), min(r[1] for r in rects),
                               max(r[2] for r in rects), max(r[3] for r in rects))
                    out.append(Item("shine", ("shine",), box, (tuple(rects), a)))
            if tl.playhead is not None:
                x = round(g.x(tl.playhead))
                top = round(g.track(0)[0] - 7 * u)
                bottom = round(g.track(1)[1] + 1 * u)
                out.append(Item("playhead", ("playhead",), _box(x - 4 * u, top - 4 * u, x + 4 * u, bottom), (x, top, bottom, a)))
        door = round(g.door)
        for r in sorted((r for r in self.robots if r.out), key=lambda r: (-r.depth, r.key)):
            x = round(g.x(r.s))
            y = round(g.floor - r.depth - r.lift)
            flip = g.side * r.facing < 0
            left, top, right, bottom = boxer(r.pose, flip, x, y)
            cut = None
            if g.side < 0 and right > door:            # half in the box: only the outside shows
                right, cut = door, door
            elif g.side > 0 and left < door:
                left, cut = door, door
            if right <= left:
                continue
            out.append(Item("robot", ("robot", r.key), (left, top, right, bottom), (r.pose, flip, x, y, cut)))
            if r.held is not None and cut is None:      # a stolen clip above its head
                w, h = 30 * u, TRACK_H * u
                cy = y - g.robot_h - 4 * u - h / 2
                data = (round(x - w / 2), round(cy - h / 2), round(w), round(h), 0xFF000000 | r.held)
                out.append(Item("held", ("held", r.key), _box(x - w / 2, cy - h / 2, x + w / 2, cy + h / 2), data))
        for s in self.sparks:
            k = (self.t - s.born) / s.life
            reach = s.size * (0.45 + 0.55 * k)
            a = round(255 * (1 - k))
            out.append(Item("spark", ("spark", s.key), _box(s.x - reach, s.y - reach, s.x + reach, s.y + reach, 3 * u),
                            (round(s.x), round(s.y), round(reach * 0.35, 1), round(reach, 1), s.rays, (a << 24) | (s.colour & 0xFFFFFF))))
        for d in self.debris:
            k = (self.t - d.born) / d.life
            a = round(255 * (1 - max(0.0, k - 0.6) / 0.4))
            half = d.size / 2
            out.append(Item("debris", ("debris", d.key), _box(d.x - half, d.y - half, d.x + half, d.y + half),
                            (round(d.x - half), round(d.y - half), round(d.size), (a << 24) | (d.colour & 0xFFFFFF))))
        if self.laser is not None:
            (x0, y0), (x1, y1) = self.laser
            line = (round(x0), round(y0), round(x1), round(y1))
            out.append(Item("laser", ("laser",), _box(min(line[0], line[2]), min(line[1], line[3]),
                                                       max(line[0], line[2]), max(line[1], line[3]), 5 * u), line))
        if self.t < self.flash_until:
            mx, my = g.muzzle
            out.append(Item("flash", ("flash",), _box(mx - 12 * u, my - 12 * u, mx + 12 * u, my + 12 * u),
                            (round(mx), round(my))))
        return out


def merge_boxes(boxes: Iterable[tuple[int, int, int, int]], slack: int = 4,
                limit: int = 24) -> list[tuple[int, int, int, int]]:
    """Overlapping (or nearly touching) boxes merged; too many become one."""
    out: list[tuple[int, int, int, int]] = []
    for box in boxes:
        if box[2] <= box[0] or box[3] <= box[1]:
            continue
        merged = True
        while merged:
            merged = False
            for i, o in enumerate(out):
                if box[0] <= o[2] + slack and o[0] <= box[2] + slack and box[1] <= o[3] + slack and o[1] <= box[3] + slack:
                    out.pop(i)
                    box = (min(box[0], o[0]), min(box[1], o[1]), max(box[2], o[2]), max(box[3], o[3]))
                    merged = True
                    break
        out.append(box)
    if len(out) > limit:
        return [(min(b[0] for b in out), min(b[1] for b in out), max(b[2] for b in out), max(b[3] for b in out))]
    return out


def _intersect(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> tuple[int, int, int, int] | None:
    box = (max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3]))
    return box if box[2] > box[0] and box[3] > box[1] else None


# ============================================================================================
# Windows: the band window, the robot sprites, drawing
# ============================================================================================

_gdiplus = ctypes.WinDLL("gdiplus")
_fn = pc._fn
_P, _VP, _F = ctypes.POINTER, ctypes.c_void_p, ctypes.c_float


class _RectF(ctypes.Structure):
    _fields_ = [("X", _F), ("Y", _F), ("Width", _F), ("Height", _F)]


_GdipCreatePath = _fn(_gdiplus, "GdipCreatePath", ctypes.c_int, ctypes.c_int, _P(_VP))
_GdipDeletePath = _fn(_gdiplus, "GdipDeletePath", ctypes.c_int, _VP)
_GdipResetPath = _fn(_gdiplus, "GdipResetPath", ctypes.c_int, _VP)
_GdipAddPathArc = _fn(_gdiplus, "GdipAddPathArc", ctypes.c_int, _VP, _F, _F, _F, _F, _F, _F)
_GdipClosePathFigure = _fn(_gdiplus, "GdipClosePathFigure", ctypes.c_int, _VP)
_GdipFillPath = _fn(_gdiplus, "GdipFillPath", ctypes.c_int, _VP, _VP, _VP)
_GdipFillRectangle = _fn(_gdiplus, "GdipFillRectangle", ctypes.c_int, _VP, _VP, _F, _F, _F, _F)
_GdipDrawImageRectI = _fn(_gdiplus, "GdipDrawImageRectI", ctypes.c_int, _VP, _VP, ctypes.c_int, ctypes.c_int,
                          ctypes.c_int, ctypes.c_int)
_GdipCreateFontFamilyFromName = _fn(_gdiplus, "GdipCreateFontFamilyFromName", ctypes.c_int, wintypes.LPCWSTR, _VP,
                                    _P(_VP))
_GdipDeleteFontFamily = _fn(_gdiplus, "GdipDeleteFontFamily", ctypes.c_int, _VP)
_GdipCreateFont = _fn(_gdiplus, "GdipCreateFont", ctypes.c_int, _VP, _F, ctypes.c_int, ctypes.c_int, _P(_VP))
_GdipDeleteFont = _fn(_gdiplus, "GdipDeleteFont", ctypes.c_int, _VP)
_GdipCreateStringFormat = _fn(_gdiplus, "GdipCreateStringFormat", ctypes.c_int, ctypes.c_int, wintypes.WORD,
                              _P(_VP))
_GdipSetStringFormatAlign = _fn(_gdiplus, "GdipSetStringFormatAlign", ctypes.c_int, _VP, ctypes.c_int)
_GdipSetStringFormatLineAlign = _fn(_gdiplus, "GdipSetStringFormatLineAlign", ctypes.c_int, _VP, ctypes.c_int)
_GdipDeleteStringFormat = _fn(_gdiplus, "GdipDeleteStringFormat", ctypes.c_int, _VP)
_GdipDrawString = _fn(_gdiplus, "GdipDrawString", ctypes.c_int, _VP, wintypes.LPCWSTR, ctypes.c_int, _VP,
                      _P(_RectF), _VP, _VP)
_GdipSetTextRenderingHint = _fn(_gdiplus, "GdipSetTextRenderingHint", ctypes.c_int, _VP, ctypes.c_int)
_GdipSetInterpolationMode = _fn(_gdiplus, "GdipSetInterpolationMode", ctypes.c_int, _VP, ctypes.c_int)

INTERPOLATION_NEAREST = 5
COMBINE_REPLACE, COMBINE_INTERSECT = 0, 1
FONT_BOLD = 1
STRING_ALIGN_CENTER = 1
TEXT_ANTIALIAS = 4            # (ClearType would leave coloured fringes on a transparent window)


class RobotSprites:
    """The robot's poses, cut from the sprite sheet the widget page rendered and scaled once to
    ``ROBOT_PX`` CSS pixels tall – each pose twice: facing right and mirrored.

    Every cell holds the robot standing on its feet in the middle of the cell, facing right. Each
    pose is cropped to its own pixels; all share one scale (from ``robot-a``) and one anchor: the
    middle of the cell at the bottom of ``robot-a``'s pixels (its feet)."""

    def __init__(self, path: str, poses: Sequence[str], cell_css: int, sheet_scale: float, unit: float,
                 height_px: float = ROBOT_PX) -> None:
        self.poses = list(poses)
        self.images: dict[tuple[str, bool], tuple[int, float, float, int, int]] = {}
        sheet = _VP()
        pc._check(pc._GdipCreateBitmapFromFile(os.path.abspath(path), ctypes.byref(sheet)), "loading the robots")
        try:
            width, height = pc._image_size(sheet.value)
            cell = round(cell_css * sheet_scale)
            if not self.poses or cell * len(self.poses) > width:
                cell = width // len(self.poses) if self.poses else 0
            if cell <= 0:
                raise ValueError("the robot sheet has no cells")
            boxes, _tips = pc.Sprites._alpha_boxes(sheet.value, cell, height)
            self.boxes = boxes
            known = [b for b in boxes if b is not None]
            if not known:
                raise ValueError("the robot sheet is empty")
            ref = boxes[self.poses.index("robot-a")] if "robot-a" in self.poses else None
            ref = ref or known[0]
            self.k = height_px * unit / max(1, ref[3] - ref[1])       # screen pixels per sheet pixel
            self.w = (ref[2] - ref[0]) * self.k
            self.h = (ref[3] - ref[1]) * self.k
            cx, feet = cell / 2, ref[3]
            for index, (pose, box) in enumerate(zip(self.poses, boxes)):
                if box is None:
                    continue
                left, top = max(0, box[0] - 1), max(0, box[1] - 1)
                right, bottom = min(cell, box[2] + 1), min(height, box[3] + 1)
                dw, dh = (right - left) * self.k, (bottom - top) * self.k
                w, h = max(1, math.ceil(dw)), max(1, math.ceil(dh))
                for flip in (False, True):
                    image = self._scaled(sheet.value, index * cell + left, top, right - left, bottom - top,
                                         w, h, dw, dh, flip)
                    dx = (cx - right) * self.k if flip else (left - cx) * self.k
                    self.images[(pose, flip)] = (image, dx, (top - feet) * self.k, w, h)
        finally:
            pc._GdipDisposeImage(sheet.value)           # releases the file

    @staticmethod
    def _scaled(sheet: int, x: int, y: int, sw: int, sh: int, w: int, h: int, dw: float, dh: float,
                flip: bool) -> int:
        image = _VP()
        pc._check(pc._GdipCreateBitmapFromScan0(w, h, 0, pc.PIXEL_FORMAT_32BPP_PARGB, None, ctypes.byref(image)),
                  "GdipCreateBitmapFromScan0")
        graphics = _VP()
        pc._check(pc._GdipGetImageGraphicsContext(image.value, ctypes.byref(graphics)), "GdipGetImageGraphicsContext")
        try:
            g = graphics.value
            _GdipSetInterpolationMode(g, pc.INTERPOLATION_HQ_BICUBIC)
            pc._GdipSetPixelOffsetMode(g, pc.PIXEL_OFFSET_HQ)
            pc._GdipGraphicsClear(g, 0)
            if flip:
                pc._GdipScaleWorldTransform(g, -1.0, 1.0, pc.MATRIX_APPEND)
                pc._GdipTranslateWorldTransform(g, dw, 0.0, pc.MATRIX_APPEND)
            pc._check(pc._GdipDrawImageRectRect(g, sheet, 0, 0, dw, dh, x, y, sw, sh, pc.UNIT_PIXEL, None, None, None),
                      "drawing a robot")
        finally:
            pc._GdipDeleteGraphics(graphics.value)
        return image.value

    def image_for(self, pose: str, flip: bool) -> tuple[int, float, float, int, int]:
        """``(bitmap, dx, dy, w, h)`` of a pose (robot-a for an unknown one), from the feet."""
        found = self.images.get((pose, flip)) or self.images.get(("robot-a", flip))
        return found or next(v for (_p, f), v in self.images.items() if f == flip)

    def box(self, pose: str, flip: bool, x: float, y: float) -> tuple[int, int, int, int]:
        """Where the pose lands with the robot's feet at (x, y): (left, top, right, bottom)."""
        _image, dx, dy, w, h = self.image_for(pose, flip)
        left, top = round(x + dx), round(y + dy)
        return left, top, left + w, top + h

    def close(self) -> None:
        for image, *_rest in self.images.values():
            pc._GdipDisposeImage(image)
        self.images.clear()


class BandCanvas(pc.Canvas):
    """The band's bitmap, with what the scene needs: rounded clips, sprites at 1:1, text, lines."""

    def __init__(self, width: int, height: int, unit: float = 1.0) -> None:
        super().__init__(width, unit, height)
        g = self.graphics.value
        _GdipSetInterpolationMode(g, INTERPOLATION_NEAREST)          # the sprites are pre-scaled: 1:1
        _GdipSetTextRenderingHint(g, TEXT_ANTIALIAS)
        self.path = _VP()
        pc._check(_GdipCreatePath(0, ctypes.byref(self.path)), "GdipCreatePath")
        self.family, self.font, self.format = _VP(), _VP(), _VP()
        if _GdipCreateFontFamilyFromName("Segoe UI", None, ctypes.byref(self.family)) == 0:
            _GdipCreateFont(self.family.value, 8.0 * unit, FONT_BOLD, pc.UNIT_PIXEL, ctypes.byref(self.font))
        if _GdipCreateStringFormat(0, 0, ctypes.byref(self.format)) == 0:
            _GdipSetStringFormatAlign(self.format.value, STRING_ALIGN_CENTER)
            _GdipSetStringFormatLineAlign(self.format.value, STRING_ALIGN_CENTER)

    def begin(self, box: tuple[int, int, int, int]) -> None:
        """Wipe ``box`` (x, y, w, h) and draw only inside it until ``end``."""
        g = self.graphics.value
        pc._GdipResetWorldTransform(g)
        pc._GdipSetClipRectI(g, *box, COMBINE_REPLACE)
        pc._GdipGraphicsClear(g, 0)

    def end(self) -> None:
        pc._GdipResetClip(self.graphics.value)

    def narrow(self, box: tuple[int, int, int, int]) -> None:
        pc._GdipSetClipRectI(self.graphics.value, *box, COMBINE_INTERSECT)

    def restore(self, box: tuple[int, int, int, int]) -> None:
        pc._GdipSetClipRectI(self.graphics.value, *box, COMBINE_REPLACE)

    def fill(self, colour: int) -> None:
        pc._GdipSetSolidFillColor(self.brush.value, colour)

    def rect(self, x: float, y: float, w: float, h: float, colour: int) -> None:
        self.fill(colour)
        _GdipFillRectangle(self.graphics.value, self.brush.value, x, y, w, h)

    def rounded(self, x: float, y: float, w: float, h: float, radius: float, colour: int) -> None:
        d = max(0.5, min(2 * radius, w, h))
        path = self.path.value
        _GdipResetPath(path)
        _GdipAddPathArc(path, x, y, d, d, 180, 90)
        _GdipAddPathArc(path, x + w - d, y, d, d, 270, 90)
        _GdipAddPathArc(path, x + w - d, y + h - d, d, d, 0, 90)
        _GdipAddPathArc(path, x, y + h - d, d, d, 90, 90)
        _GdipClosePathFigure(path)
        self.fill(colour)
        _GdipFillPath(self.graphics.value, self.brush.value, path)

    def ellipse(self, cx: float, cy: float, r: float, colour: int) -> None:
        self.fill(colour)
        pc._GdipFillEllipse(self.graphics.value, self.brush.value, cx - r, cy - r, 2 * r, 2 * r)

    def line(self, x0: float, y0: float, x1: float, y1: float, width: float, colour: int) -> None:
        pen = self.pen.value
        pc._GdipSetPenWidth(pen, width)
        pc._GdipSetPenColor(pen, colour)
        pc._GdipDrawLine(self.graphics.value, pen, x0, y0, x1, y1)

    def blit(self, image: int, x: int, y: int, w: int, h: int) -> None:
        """A pre-scaled sprite at 1:1 (``image`` would clash with the canvas's own bitmap)."""
        _GdipDrawImageRectI(self.graphics.value, image, x, y, w, h)

    def text(self, text: str, x: float, y: float, w: float, h: float, colour: int) -> None:
        if not self.font.value:
            return
        self.fill(colour)
        _GdipDrawString(self.graphics.value, text, len(text), self.font.value, ctypes.byref(_RectF(x, y, w, h)),
                        self.format.value, self.brush.value)

    def flush(self) -> None:
        pc._GdipFlush(self.graphics.value, 1)

    def close(self) -> None:
        if self.format.value:
            _GdipDeleteStringFormat(self.format.value)
        if self.font.value:
            _GdipDeleteFont(self.font.value)
        if self.family.value:
            _GdipDeleteFontFamily(self.family.value)
        _GdipDeletePath(self.path.value)
        super().close()


class Band(pc.Overlay):
    """The robots' window over the band: transparent, click-through, topmost, never activated,
    not on the taskbar. Only the part with something in it is shown (``show_box``)."""

    CLASS = "ProjektsogRobotter"

    def __init__(self, width: int, height: int, unit: float = 1.0) -> None:
        # (Overlay's own __init__ is not called: the canvas is a BandCanvas, the rest is the same.)
        self.canvas = BandCanvas(width, height, unit)
        self.instance = pc._GetModuleHandleW(None)
        wc = pc._WNDCLASSEXW(cbSize=ctypes.sizeof(pc._WNDCLASSEXW), lpfnWndProc=pc._wndproc,
                             hInstance=self.instance, lpszClassName=self.CLASS)
        pc._RegisterClassExW(ctypes.byref(wc))
        self.hwnd = pc._CreateWindowExW(pc.WS_EX_LAYERED | pc.WS_EX_TRANSPARENT | pc.WS_EX_TOPMOST
                                        | pc.WS_EX_TOOLWINDOW | pc.WS_EX_NOACTIVATE, self.CLASS, "Robotterne bygger",
                                        pc.WS_POPUP, 0, 0, width, height, None, None, self.instance, None)
        if not self.hwnd:
            self.canvas.close()
            raise OSError(f"CreateWindowExW failed ({ctypes.get_last_error()})")
        self.shown = False
        self.screen = pc._GetDC(None)

    def keep_on_top(self) -> None:
        if self.shown:
            pc._SetWindowPos(self.hwnd, pc.HWND_TOPMOST, 0, 0, 0, 0, pc.SWP_NOMOVE | pc.SWP_NOSIZE | pc.SWP_NOACTIVATE)


class Painter:
    """Draws the scene's items into the band canvas: only what changed since the last frame is
    wiped and drawn again, and the window shows just the part that has anything in it."""

    def __init__(self, canvas: BandCanvas, sprites: RobotSprites, origin: Point, overlay: Band | None = None) -> None:
        self.canvas = canvas
        self.sprites = sprites
        self.origin = (round(origin[0]), round(origin[1]))
        self.overlay = overlay
        self.bounds = (self.origin[0], self.origin[1], self.origin[0] + canvas.size, self.origin[1] + canvas.height)
        self._last: dict[tuple, Item] = {}
        self.dirty: list[tuple[int, int, int, int]] = []      # (for tests) what the last paint redrew
        self.content: tuple[int, int, int, int] | None = None

    def boxer(self, pose: str, flip: bool, x: float, y: float) -> tuple[int, int, int, int]:
        return self.sprites.box(pose, flip, x, y)

    def paint(self, items: Sequence[Item], full: bool = False) -> None:
        new = {item.key: item for item in items}
        if full:
            boxes = [self.bounds]
        else:
            boxes = []
            for key, item in new.items():
                old = self._last.get(key)
                if old is None:
                    boxes.append(item.box)
                elif old.box != item.box or old.data != item.data:
                    boxes += (old.box, item.box)
            boxes += (old.box for key, old in self._last.items() if key not in new)
        clipped = (_intersect(b, self.bounds) for b in boxes)
        self.dirty = merge_boxes(b for b in clipped if b is not None)
        ox, oy = self.origin
        canvas = self.canvas
        for rect in self.dirty:
            local = (rect[0] - ox, rect[1] - oy, rect[2] - rect[0], rect[3] - rect[1])
            canvas.begin(local)
            for item in items:
                if _intersect(item.box, rect) is not None:
                    self._draw(item, local)
            canvas.end()
        if self.dirty:
            canvas.flush()
        self._last = new
        content = None
        for item in items:
            box = _intersect(item.box, self.bounds)
            if box is not None:
                content = box if content is None else (min(content[0], box[0]), min(content[1], box[1]),
                                                       max(content[2], box[2]), max(content[3], box[3]))
        self.content = content
        if self.overlay is not None:
            if content is None:
                self.overlay.hide()
            elif self.dirty or not self.overlay.shown:
                self.overlay.show_box(ox, oy, (content[0] - ox, content[1] - oy,
                                               content[2] - content[0], content[3] - content[1]))

    def _draw(self, item: Item, local: tuple[int, int, int, int]) -> None:
        c, (ox, oy), u = self.canvas, self.origin, self.canvas.unit
        kind, data = item.kind, item.data
        if kind == "robot":
            pose, flip, x, y, cut = data
            image, dx, dy, w, h = self.sprites.image_for(pose, flip)
            if cut is not None:                      # walking through the door: only the outside
                side_box = (0, 0, cut - ox, c.height) if item.box[2] <= cut else (cut - ox, 0, c.size - (cut - ox), c.height)
                c.narrow(side_box)
            c.blit(image, round(x + dx) - ox, round(y + dy) - oy, w, h)
            if cut is not None:
                c.restore(local)
        elif kind in ("clip", "held"):
            x, y, w, h, colour = data
            c.rounded(x - ox, y - oy, w, h, CLIP_RADIUS * u, colour)
            if w > 6 * u:                            # a little light along the top edge
                c.rect(x - ox + 2 * u, y - oy + 1.5 * u, w - 4 * u, 1.2 * u, ((colour >> 24) // 4) << 24 | 0xFFFFFF)
        elif kind == "label":
            x, y, w, h, text, a = data
            c.rounded(x - ox, y - oy, w, h, h / 2, (LABEL_FILL >> 24) * a // 255 << 24 | (LABEL_FILL & 0xFFFFFF))
            c.text(text, x - ox, y - oy, w, h, (a << 24) | 0xFFFFFF)
        elif kind == "shine":
            rects, a = data
            for left, top, right, bottom in rects:
                c.rect(left - ox, top - oy, right - left, bottom - top, (0x9C * a // 255) << 24 | 0xFFFFFF)
        elif kind == "playhead":
            x, top, bottom, a = data
            colour = (a << 24) | (PLAYHEAD & 0xFFFFFF)
            c.line(x - ox, top - oy, x - ox, bottom - oy, 2 * u, colour)
            c.ellipse(x - ox, top - oy, 3.5 * u, colour)
        elif kind == "spark":
            x, y, inner, outer, rays, colour = data
            if (colour >> 24) > 160:
                c.ellipse(x - ox, y - oy, inner * 0.8, (colour & 0xFF000000) | 0xFFFFFF)
            for i in range(rays):
                angle = i * 2 * math.pi / rays + 0.3
                cos, sin = math.cos(angle), math.sin(angle)
                c.line(x - ox + cos * inner, y - oy + sin * inner, x - ox + cos * outer, y - oy + sin * outer,
                       2.2 * u, colour)
        elif kind == "debris":
            x, y, size, colour = data
            c.rect(x - ox, y - oy, size, size, colour)
        elif kind == "laser":
            x0, y0, x1, y1 = data
            for width, colour in LASER:
                c.line(x0 - ox, y0 - oy, x1 - ox, y1 - oy, width * u, colour)
        elif kind == "flash":
            x, y = data
            for radius, colour in FLASH:
                c.ellipse(x - ox, y - oy, radius * u, colour)

    def keep_on_top(self) -> None:
        if self.overlay is not None:
            self.overlay.keep_on_top()

    def pump(self) -> None:
        if self.overlay is not None:
            self.overlay.pump()

    def hide(self) -> None:
        if self.overlay is not None:
            self.overlay.hide()


# ============================================================================================
# The loop
# ============================================================================================

class Player:
    """Runs the scene: steps it, draws every frame, watches for the user, the lock and stdin."""

    def __init__(self, scene: Scene, painter: Any, watch: pc.Watch, emit: Callable[[dict[str, Any]], None], *,
                 done: threading.Event | None = None, fps: int = FPS, max_s: float = MAX_S,
                 time_scale: float = 1.0) -> None:
        self.scene = scene
        self.painter = painter
        self.watch = watch
        self.emit = emit
        self.done = done or threading.Event()
        self._fps = fps
        self._max_s = max_s
        self._time_scale = time_scale              # (tests run the scene faster than real time)
        self.home_sent = False
        self.frames = 0

    def run(self) -> str:
        reason = "error"
        scene = self.scene
        started = time.perf_counter()
        last_t = 0.0
        out_sent = False
        next_top = TOPMOST_S
        frame_s = 1 / self._fps
        try:
            while True:
                tick = time.perf_counter()
                t = (tick - started) * self._time_scale
                why = self.watch.reason(False)
                if why in ("quit", "locked"):
                    reason = why
                    break
                if why == "touched" and scene.mode != "rush":
                    if not out_sent:                   # touched before anything was shown
                        reason = why
                        break
                    scene.touch("touched")
                    log.info("touched: the robots rush home")
                if self.done.is_set() and scene.mode == "work":
                    scene.finish()
                    log.info("the build is done: the finale")
                if t > self._max_s and scene.mode != "rush":
                    scene.touch("timeout")
                events = scene.step(min(0.1, t - last_t))
                last_t = t
                self.painter.paint(scene.items(self.painter.boxer))
                self.frames += 1
                if not out_sent:                    # which side of the box they use, then out
                    self.emit({"event": "side", "side": "right" if scene.geo.side > 0 else "left"})
                    self.emit({"event": "out"})
                    out_sent = True
                for event in events:
                    self.emit(event)
                if scene.over:
                    reason = scene.reason or "done"
                    break
                if t >= next_top:
                    self.painter.keep_on_top()
                    next_top = t + TOPMOST_S * self._time_scale
                self.painter.pump()
                pause = frame_s - (time.perf_counter() - tick)
                if pause > 0:
                    time.sleep(pause)
        except BaseException:
            reason = "error"
            raise
        finally:
            if scene.aiming:
                self.emit({"event": "aim-end"})
                scene.aiming = False
            self.emit({"event": "home", "reason": reason})
            self.home_sent = True
            try:
                self.painter.hide()
            except Exception:              # (the window may be gone already)
                log.debug("hiding the band failed", exc_info=True)
        return reason


# ============================================================================================
# Entry point
# ============================================================================================

def _watch_stdin(stream: BinaryIO | None, quit_event: threading.Event, done_event: threading.Event) -> None:
    """``done`` starts the finale; ``quit`` or the end of stdin (the main process is gone) ends it now."""
    def read() -> None:
        try:
            for raw in stream:            # type: ignore[union-attr]
                word = raw.strip()
                if word == b"done":
                    done_event.set()
                elif word == b"quit":
                    break
        except (OSError, ValueError):
            pass
        quit_event.set()
    if stream is not None:
        threading.Thread(target=read, name="crew-stdin", daemon=True).start()


def _configure_logging(log_file: str | None) -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not log_file:
        root.addHandler(logging.NullHandler())
        return
    try:
        os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(log_file, maxBytes=256 * 1024, backupCount=1,
                                                       encoding="utf-8", delay=True)
    except OSError:
        root.addHandler(logging.NullHandler())
        return
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s crew[%(process)d]: %(message)s"))
    root.addHandler(handler)


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="projektsog.crew_child")
    parser.add_argument("--sprites", required=True, help="the robot sprite sheet (PNG)")
    parser.add_argument("--poses", default=",".join(ROBOT_POSES), help="the poses in the sheet, comma separated")
    parser.add_argument("--cell", type=int, default=120, help="a cell's size in CSS pixels")
    parser.add_argument("--sheet-scale", type=float, default=2.0)
    parser.add_argument("--widget", type=int, required=True, help="the widget window (HWND)")
    parser.add_argument("--pet", default="", help="the pet in the widget page: x,y,w,h (CSS px)")
    parser.add_argument("--view", default="", help="the widget page's size: w,h (CSS px)")
    parser.add_argument("--stage", default="baby")
    parser.add_argument("--robots", type=int, default=5)
    parser.add_argument("--awp", type=int, default=0, help="1: Klippe has its AWP – it shoots naughty robots")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--input-tick", type=int, default=None,
                        help="the last-input time (GetLastInputInfo) when the robots were sent out")
    parser.add_argument("--log-file")
    return parser.parse_args(list(argv))


def geometry_for(args: argparse.Namespace, work: Rect, unit: float, widget: Rect,
                 sprites: RobotSprites) -> CrewGeometry:
    awp = bool(args.awp) and args.stage in MUZZLE_SCALE
    muzzle = None
    try:
        muzzle = pc.home_in_widget(widget, pc._numbers(args.pet, 4), pc._numbers(args.view, 2), unit,
                                   muzzle_units(args.stage, crew_side(work, widget)))
    except ValueError:
        pass
    return crew_geometry(work, widget, unit, sprites.w, sprites.h, muzzle, awp)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    _configure_logging(args.log_file)
    if pc._SetProcessDpiAwarenessContext is not None:
        pc._SetProcessDpiAwarenessContext(ctypes.c_void_p(pc.PER_MONITOR_AWARE_V2))
    emit = pc._Emitter(getattr(sys.stdout, "buffer", None))
    from .petplay import desktop_locked, last_input_tick
    watch = pc.Watch(last_input_tick, desktop_locked, start_tick=args.input_tick)
    done = threading.Event()
    _watch_stdin(getattr(sys.stdin, "buffer", None), watch.quit, done)
    player: Player | None = None
    try:
        with pc.GdiPlus():
            widget = pc.window_rect(args.widget)
            if widget is None:
                raise OSError("the widget window is gone")
            work, _monitor, unit = pc.monitor_of(args.widget)
            sprites = RobotSprites(args.sprites, args.poses.split(","), args.cell, args.sheet_scale, unit)
            try:
                geo = geometry_for(args, work, unit, widget, sprites)
                awp = bool(args.awp) and args.stage in MUZZLE_SCALE
                scene = Scene(geo, random.Random(args.seed), max(1, min(args.robots, 24)), awp=awp, stage=args.stage)
                band = Band(max(1, int(geo.band.width)), max(1, int(geo.band.height)), unit)
                try:
                    log.info("out: %d robots, %s of the widget, timeline %.0f px%s", len(scene.robots),
                             "right" if geo.side > 0 else "left", geo.length, ", AWP" if awp else "")
                    painter = Painter(band.canvas, sprites, (geo.band.left, geo.band.top), band)
                    player = Player(scene, painter, watch, emit, done=done)
                    log.info("home (%s)", player.run())
                finally:
                    band.close()
            finally:
                sprites.close()
    except Exception:
        log.exception("the robots failed")
    if player is None or not player.home_sent:
        emit({"event": "home", "reason": "error"})
    return 0


if __name__ == "__main__":
    # End without the interpreter's shutdown: the stdin reader thread may still be blocked in a read
    # on the pipe, and finalising sys.stdin under it is a fatal error (0xC0000005) – "home" and the
    # log are written already.
    status = main()
    logging.shutdown()
    os._exit(status)
