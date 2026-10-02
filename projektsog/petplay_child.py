"""Klippe out of its box: the helper process that plays with the mouse pointer (SPEC §18.4).

``projektsog.petplay`` (main process) starts it when the rules allow a game. It

* shows the pet in a small transparent window that never takes the focus and that every click
  goes through (a layered window drawn with GDI+ from a sprite sheet the widget page rendered),
* breaks out of the widget, fetches the pointer, rides it, flies with it, throws it, spins it
  round – then puts it back exactly where it was and flies home into the widget.

It lets go at once when the user touches the mouse or the keyboard (the session's last-input
time changes; moving the pointer from a program does not change it) and puts the pointer back
where the user left it. It never clicks, scrolls or types. It also stops when the screen is
locked, when the main process says ``quit`` or goes away (stdin EOF), and after ``MAX_S``.

Events on stdout, one JSON object per line: ``{"event": "out"}`` once the pet is outside,
``{"event": "home", "reason": "done" | "touched" | "locked" | "quit" | "error"}`` at the end.

The first half of the module is the choreography – pure arithmetic, tested without a screen.
The second half draws and moves the pointer (Win32/GDI+ through ctypes).
"""

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import logging.handlers
import math
import os
import random
import sys
import threading
import time
from collections.abc import Callable, Sequence
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any, BinaryIO, NamedTuple

log = logging.getLogger(__name__)

FPS = 60
MAX_S = 90.0                  # a game never lasts longer than this
LOCK_CHECK_S = 0.5            # how often the input desktop is checked (locked screen)
PLAY_SIZE = 0.9               # the pet outside: CSS pixels per SVG unit (in the widget ≈ 1.05)
POINTER_SLACK_PX = 2          # the pointer is "ours" while it is this close to where we put it
ACTS = ("ride", "fly", "throw", "spin")
ACTS_PER_STAGE = {"baby": 3, "junior": 2, "pro": 2, "legend": 2}
Point = tuple[float, float]


# ============================================================================================
# Choreography (pure)
# ============================================================================================

class Frame(NamedTuple):
    """What one moment of the game looks like."""
    x: float                          # the pet's centre (physical pixels)
    y: float
    angle: float = 0.0                # degrees, clockwise
    scale: float = 1.0                # relative to the play size
    squash: float = 1.0               # > 1 stretched up, < 1 squashed
    pose: str = "normal"
    cursor: Point | None = None       # where the pointer is put (None: leave it alone)


class Rect(NamedTuple):
    left: float
    top: float
    right: float
    bottom: float

    @property
    def width(self) -> float:
        return self.right - self.left

    @property
    def height(self) -> float:
        return self.bottom - self.top

    @property
    def centre(self) -> Point:
        return (self.left + self.right) / 2, (self.top + self.bottom) / 2

    def inset(self, dx: float, dy: float) -> Rect:
        """Smaller on every side (never inverted: a too small rectangle becomes its centre)."""
        cx, cy = self.centre
        left, right = (self.left + dx, self.right - dx) if self.width > 2 * dx else (cx, cx)
        top, bottom = (self.top + dy, self.bottom - dy) if self.height > 2 * dy else (cy, cy)
        return Rect(left, top, right, bottom)

    def clamp(self, x: float, y: float) -> Point:
        return min(max(x, self.left), self.right), min(max(y, self.top), self.bottom)

    def contains(self, x: float, y: float, slack: float = 0.5) -> bool:
        return (self.left - slack <= x <= self.right + slack
                and self.top - slack <= y <= self.bottom + slack)

    def at(self, fx: float, fy: float) -> Point:
        """The point at fractions ``fx``, ``fy`` of the width and height."""
        return self.left + fx * self.width, self.top + fy * self.height


class Geometry(NamedTuple):
    """Where the game takes place (physical pixels)."""
    work: Rect               # the monitor's work area
    play: Rect               # where the pointer may be put (the work area minus a margin)
    home: Point              # the pet's centre in the widget
    home_scale: float        # its size there, relative to the play size
    pet_w: float             # the pet's size outside
    pet_h: float
    unit: float              # physical pixels per CSS pixel (the monitor's scale)


@dataclass
class Segment:
    name: str
    duration: float
    at: Callable[[float], Frame]          # u in [0, 1] → the frame
    holds_pointer: bool = False           # the pointer is Klippe's during this segment


def clamp01(u: float) -> float:
    return 0.0 if u < 0 else 1.0 if u > 1 else u


def smooth(u: float) -> float:
    u = clamp01(u)
    return u * u * (3 - 2 * u)


def lerp(a: float, b: float, u: float) -> float:
    return a + (b - a) * u


def lerp2(p: Point, q: Point, u: float) -> Point:
    return lerp(p[0], q[0], u), lerp(p[1], q[1], u)


def bezier(p0: Point, p1: Point, p2: Point, u: float) -> Point:
    a, b, c = (1 - u) ** 2, 2 * (1 - u) * u, u * u
    return a * p0[0] + b * p1[0] + c * p2[0], a * p0[1] + b * p1[1] + c * p2[1]


def catmull_rom(points: Sequence[Point], u: float) -> Point:
    """A smooth curve through all ``points`` (u = 0 … 1 over the whole curve)."""
    n = len(points) - 1
    if n < 1:
        return points[0]
    pos = clamp01(u) * n
    i = min(int(pos), n - 1)
    t = pos - i
    p0, p1, p2 = points[max(i - 1, 0)], points[i], points[i + 1]
    p3 = points[min(i + 2, n)]
    t2, t3 = t * t, t * t * t

    def axis(k: int) -> float:
        return 0.5 * (2 * p1[k] + (-p0[k] + p2[k]) * t + (2 * p0[k] - 5 * p1[k] + 4 * p2[k] - p3[k]) * t2
                      + (-p0[k] + 3 * p1[k] - 3 * p2[k] + p3[k]) * t3)
    return axis(0), axis(1)


def distance(p: Point, q: Point) -> float:
    return math.hypot(q[0] - p[0], q[1] - p[1])


def throw_path(start: Point, velocity: Point, bounds: Rect, gravity: float, seconds: float,
               step: float = 1 / 240) -> list[Point]:
    """A thrown pointer: falls, bounces off the edges of ``bounds``, rolls to a stop."""
    x, y = start
    vx, vy = velocity
    points = [(x, y)]
    for _ in range(int(seconds / step)):
        vy += gravity * step
        x += vx * step
        y += vy * step
        if x < bounds.left or x > bounds.right:
            x = min(max(x, bounds.left), bounds.right)
            vx = -vx * 0.7
        if y < bounds.top:
            y, vy = bounds.top, -vy * 0.6
        if y > bounds.bottom:
            y, vy = bounds.bottom, -vy * 0.55
            vx *= 0.82
            if abs(vy) < gravity * 0.1:       # too tired to bounce: it lies on the floor
                vy = 0.0
        points.append((x, y))
    return points


class ShowBuilder:
    """Puts a game together: break out, fetch the pointer, a few acts, give it back, go home."""

    def __init__(self, geo: Geometry, rng: random.Random, pointer: Point) -> None:
        self.geo = geo
        self.rng = rng
        self.pointer = pointer
        self.inside = geo.play.contains(*pointer)
        self.grab = geo.play.clamp(*pointer)          # where Klippe gets hold of the pointer
        self.feet = geo.pet_h * 0.5 - 4 * geo.unit    # the pointer, held: at Klippe's feet
        pets = geo.work.inset(geo.pet_w * 0.55, geo.pet_h * 0.55)
        # Where the pet's centre may be while it holds the pointer (both on screen):
        self.zone = Rect(max(pets.left, geo.play.left), max(pets.top, geo.play.top - self.feet),
                         min(pets.right, geo.play.right), min(pets.bottom, geo.play.bottom - self.feet))
        if self.zone.width < 0 or self.zone.height < 0:
            c = pets.centre
            self.zone = Rect(c[0], c[1], c[0], c[1])
        self.segments: list[Segment] = []
        self.pos: Point = geo.home
        self.scale = geo.home_scale

    # -- helpers ------------------------------------------------------------------------------
    def holding(self, x: float, y: float) -> Point:
        return self.geo.play.clamp(x, y + self.feet)

    def add(self, name: str, duration: float, at: Callable[[float], Frame],
            holds_pointer: bool = False) -> None:
        self.segments.append(Segment(name, duration, at, holds_pointer))
        end = at(1.0)
        self.pos = (end.x, end.y)
        self.scale = end.scale

    def flight_time(self, a: Point, b: Point, speed: float, low: float, high: float) -> float:
        return min(high, max(low, distance(a, b) / (speed * self.geo.unit)))

    def carry_to(self, target: Point, pose: str = "cheer", holds: bool = True) -> None:
        """Fly in an arc to ``target`` – with the pointer at the feet when ``holds``."""
        start, end = self.pos, self.zone.clamp(*target)
        if distance(start, end) < 4:
            return
        dx, dy = end[0] - start[0], end[1] - start[1]
        bend = self.rng.choice((-1, 1)) * 0.22
        control = self.zone.clamp((start[0] + end[0]) / 2 - dy * bend, (start[1] + end[1]) / 2 + dx * bend)
        tilt = 12.0 if dx > 0 else -12.0

        def at(u: float, start: Point = start, end: Point = end, control: Point = control) -> Frame:
            x, y = bezier(start, control, end, smooth(u))
            return Frame(x, y, angle=tilt * math.sin(math.pi * u), pose=pose,
                         cursor=self.holding(x, y) if holds else None)
        self.add("carry", self.flight_time(start, end, 1000, 0.5, 1.6), at, holds_pointer=holds)

    def glide_pointer(self, frm: Point, pose: str = "happy") -> None:
        """Klippe takes a firm grip: the pointer glides from ``frm`` to its feet."""
        x, y = self.pos
        to = self.holding(x, y)

        def at(u: float) -> Frame:
            return Frame(x, y - 10 * self.geo.unit * math.sin(math.pi * u), pose=pose,
                         cursor=lerp2(frm, to, smooth(u)))
        self.add("grip", 0.3, at, holds_pointer=True)

    # -- the parts of a game ------------------------------------------------------------------
    def breakout(self) -> None:
        """Shake in the box, then a somersault out of it, up and away from the widget."""
        g = self.geo
        hx, hy = g.home
        side = -1.0 if hx > g.work.centre[0] else 1.0      # away from the widget's edge
        target = self.zone.clamp(hx + side * 190 * g.unit, hy - 240 * g.unit)
        s0 = g.home_scale

        def shake(u: float) -> Frame:
            return Frame(hx + math.sin(u * 60) * 2.5 * g.unit, hy, angle=math.sin(u * 47) * 7,
                         scale=s0, squash=1 - 0.06 * math.sin(math.pi * u), pose="oops")
        self.add("shake", 0.55, shake)

        def hop(u: float) -> Frame:
            e = smooth(u)
            x = lerp(hx, target[0], e)
            y = lerp(hy, target[1], e) - math.sin(math.pi * u) * 110 * g.unit
            y = max(y, g.work.top + g.pet_h * 0.6)
            return Frame(x, y, angle=side * 360 * e, scale=lerp(s0, 1.0, e),
                         squash=1 + 0.18 * math.sin(math.pi * u), pose="cheer")
        self.add("breakout", 0.9, hop)

    def fetch(self) -> None:
        """Fly to the pointer – or, when it is on another screen, to the edge facing it and reach
        over: the pointer comes along to this screen."""
        g = self.geo
        gx, gy = self.grab
        self.carry_to((gx, gy - self.feet), pose="cheer", holds=False)
        if not self.inside:
            x, y = self.pos
            side = -1.0 if gx <= g.play.left + 1 else 1.0 if gx >= g.play.right - 1 else 0.0

            def reach(u: float) -> Frame:
                return Frame(x + side * 14 * g.unit * math.sin(math.pi * u), y,
                             angle=side * 24 * math.sin(math.pi * u), pose="oops" if u < 0.5 else "cheer",
                             cursor=(gx, gy) if u >= 0.55 else None)
            self.add("reach", 0.5, reach, holds_pointer=True)
        self.glide_pointer((gx, gy))

    def ride(self) -> None:
        """Klippe rides the pointer like a horse: gallops across the screen."""
        g, z = self.geo, self.zone
        right = self.pos[0] < z.centre[0]
        y = z.at(0, 0.8)[1]
        start = z.at(0.12 if right else 0.88, 0.8)
        end = z.at(0.9 if right else 0.1, 0.8)
        self.carry_to(start)
        sx = self.pos[0]
        span = abs(end[0] - sx)
        hops = max(2, round(span / (150 * g.unit)))
        lean = 8.0 if right else -8.0

        def gallop(u: float) -> Frame:
            x = lerp(sx, end[0], u)
            lift = abs(math.sin(math.pi * hops * u)) * 55 * g.unit
            py = max(y - lift, z.top)
            return Frame(x, py, angle=lean + 9 * math.sin(2 * math.pi * hops * u),
                         squash=1 + 0.1 * math.cos(2 * math.pi * hops * u), pose="happy",
                         cursor=self.holding(x, py))
        self.add("ride", min(6.0, max(3.0, span / (420 * g.unit))), gallop, holds_pointer=True)

    def fly(self) -> None:
        """Loops across the screen with the pointer dangling below."""
        g, z = self.geo, self.zone
        points: list[Point] = [self.pos]
        far = 0.3 * math.hypot(z.width, z.height)
        for _ in range(4):
            best = z.at(self.rng.random(), self.rng.random())
            for _try in range(8):
                if distance(best, points[-1]) >= far:
                    break
                best = z.at(self.rng.random(), self.rng.random())
            points.append(best)
        length = sum(distance(a, b) for a, b in zip(points, points[1:]))
        duration = min(6.0, max(3.5, length / (650 * g.unit)))
        step = 1 / 200

        def at(u: float, points: list[Point] = points) -> Frame:
            x, y = z.clamp(*catmull_rom(points, u))
            ax, _ay = catmull_rom(points, min(1.0, u + step))
            bx, _by = catmull_rom(points, max(0.0, u - step))
            vx = (ax - bx) / (2 * step * duration)
            tilt = max(-22.0, min(22.0, vx / (700 * g.unit) * 18))
            return Frame(x, y, angle=tilt, pose="cheer", cursor=self.holding(x, y))
        self.add("fly", duration, at, holds_pointer=True)

    def throw(self) -> None:
        """Wind up, throw the pointer across the screen, chase it and catch it."""
        g, z = self.geo, self.zone
        side = 1.0 if self.pos[0] < g.work.centre[0] else -1.0
        self.carry_to(z.at(0.25 if side > 0 else 0.75, 0.7))
        x, y = self.pos
        held = self.holding(x, y)
        overhead = g.play.clamp(x - side * 0.35 * g.pet_w, y - 0.55 * g.pet_h)

        def windup(u: float) -> Frame:
            return Frame(x, y, angle=-side * 18 * smooth(u), squash=1 - 0.08 * smooth(u), pose="cheer",
                         cursor=lerp2(held, overhead, smooth(u)))
        self.add("windup", 0.5, windup, holds_pointer=True)
        speed = (side * self.rng.uniform(1000, 1500) * g.unit, -self.rng.uniform(700, 1100) * g.unit)
        seconds, lag = 2.0, 0.35
        path = throw_path(overhead, speed, g.play, 2600 * g.unit, seconds)
        steps = len(path) - 1

        def point(t: float) -> Point:
            return path[min(steps, max(0, round(t / seconds * steps)))]

        def flight(u: float) -> Frame:
            t = u * seconds
            if t < lag:
                k = t / lag
                return Frame(x, y, angle=-side * 18 * (1 - smooth(k)) + side * 10 * math.sin(math.pi * k),
                             pose="happy", cursor=point(t))
            px, py = point(t - lag)
            cx, cy = z.clamp(px, py - self.feet)
            return Frame(cx, cy, angle=side * 10, pose="happy", cursor=point(t))
        self.add("throw", seconds, flight, holds_pointer=True)
        landed = path[-1]
        chase = flight(1.0)
        catch_at = z.clamp(landed[0], landed[1] - self.feet)

        def catch(u: float) -> Frame:
            cx, cy = lerp2((chase.x, chase.y), catch_at, smooth(u))
            return Frame(cx, cy, angle=side * 10 * (1 - u), pose="cheer", cursor=landed)
        self.add("catch", 0.45, catch, holds_pointer=True)
        self.glide_pointer(landed)

    def spin(self) -> None:
        """The pointer spins round Klippe – which gets a little dizzy."""
        g = self.geo
        radius = self.feet * 1.6
        room = self.zone.inset(radius * 0.7, radius * 0.7)
        self.carry_to(room.clamp(*self.pos))
        x, y = self.pos

        def orbit(u: float) -> Frame:
            theta = 2 * math.pi * 2 * smooth(u)
            r = self.feet * (1 + 0.6 * math.sin(math.pi * u))
            return Frame(x, y, angle=20 * math.sin(2 * math.pi * 2 * u), pose="happy",
                         cursor=g.play.clamp(x + r * math.sin(theta), y + r * math.cos(theta)))
        self.add("spin", 3.0, orbit, holds_pointer=True)

        def dizzy(u: float) -> Frame:
            return Frame(x, y, angle=10 * math.sin(u * 28) * (1 - u), pose="oops", cursor=self.holding(x, y))
        self.add("dizzy", 0.6, dizzy, holds_pointer=True)

    def give_back(self) -> None:
        """Put the pointer exactly where it was – or toss it back to its own screen."""
        g = self.geo
        px, py = self.pointer
        if self.inside:
            self.carry_to((px, py - self.feet))
            x, y = self.pos
            held = self.holding(x, y)

            def put_down(u: float) -> Frame:
                return Frame(x, y - 30 * g.unit * math.sin(math.pi * u), pose="happy",
                             cursor=lerp2(held, self.pointer, smooth(min(1.0, u * 1.6))))
            self.add("put-down", 0.45, put_down, holds_pointer=True)
            return
        gx, gy = self.grab
        self.carry_to((gx, gy - self.feet))
        x, y = self.pos
        side = -1.0 if gx <= g.play.left + 1 else 1.0 if gx >= g.play.right - 1 else 0.0

        def toss(u: float) -> Frame:
            return Frame(x, y, angle=side * 22 * math.sin(math.pi * u), pose="cheer",
                         cursor=self.holding(x, y) if u < 0.5 else self.pointer)
        self.add("toss", 0.45, toss, holds_pointer=True)

    def go_home(self, pose: str = "happy", fast: bool = False) -> None:
        """An arc back to the widget, the last bit a somersault into the box."""
        g = self.geo
        start, end, s0 = self.pos, g.home, self.scale
        top = max(g.work.top + g.pet_h * 0.6, min(start[1], end[1]) - 150 * g.unit)
        control = ((start[0] + end[0]) / 2, top)
        side = 1.0 if end[0] >= start[0] else -1.0

        def at(u: float) -> Frame:
            e = smooth(u)
            x, y = bezier(start, control, end, e)
            flip = side * 360 * smooth((u - 0.55) / 0.45)
            return Frame(x, y, angle=flip, scale=lerp(s0, g.home_scale, e), pose=pose)
        self.add("home", self.flight_time(start, end, 1300 if fast else 950, 0.8, 1.8), at)

    def oops(self, frame: Frame) -> None:
        """Caught playing: a startled hop where it is."""
        g = self.geo

        def at(u: float) -> Frame:
            return Frame(frame.x, frame.y - 26 * g.unit * math.sin(math.pi * u), scale=frame.scale,
                         squash=1 + 0.12 * math.sin(math.pi * u), pose="oops")
        self.add("oops", 0.35, at)


def build_show(geo: Geometry, rng: random.Random, pointer: Point, stage: str = "baby",
               acts: Sequence[str] | None = None) -> list[Segment]:
    """A whole game for a pet of ``stage``: the pointer starts (and ends) at ``pointer``."""
    show = ShowBuilder(geo, rng, pointer)
    show.breakout()
    show.fetch()
    chosen = list(acts) if acts is not None else rng.sample(ACTS, ACTS_PER_STAGE.get(stage, 2))
    for act in chosen:
        getattr(show, act)()
    show.give_back()
    show.go_home()
    return show.segments


def abort_show(geo: Geometry, frame: Frame) -> list[Segment]:
    """Caught playing: a startled hop, then straight home (the pointer is left alone)."""
    show = ShowBuilder(geo, random.Random(0), (frame.x, frame.y))
    show.pos, show.scale = (frame.x, frame.y), frame.scale
    show.oops(frame)
    show.go_home(pose="normal", fast=True)
    return show.segments


def frame_at(segments: Sequence[Segment], t: float) -> tuple[int, Frame] | None:
    """``(index, frame)`` at ``t`` seconds into the show, None once it is over."""
    for index, seg in enumerate(segments):
        if t < seg.duration:
            return index, seg.at(t / seg.duration if seg.duration > 0 else 1.0)
        t -= seg.duration
    return None


def show_length(segments: Sequence[Segment]) -> float:
    return sum(seg.duration for seg in segments)


def home_in_widget(widget: Rect, pet_css: Sequence[float], view_css: Sequence[float], unit: float,
                   anchor_units: Point) -> Point:
    """The screen point of ``anchor_units`` (SVG units, 0…200) of the pet in the widget window.

    ``pet_css`` = the pet SVG's ``x, y, w, h`` in the page and ``view_css`` = the page's ``w, h``
    (CSS pixels, as the widget page reports them). The page fills the bottom of the window
    (the title bar is above it)."""
    x, y, w, h = pet_css
    vw, vh = view_css
    left = widget.left + (widget.width - vw * unit) / 2
    top = widget.bottom - vh * unit
    size = min(w, h)
    ox, oy = x + (w - size) / 2, y + (h - size) / 2
    per = size / 200.0
    return left + (ox + anchor_units[0] * per) * unit, top + (oy + anchor_units[1] * per) * unit


# ============================================================================================
# Windows: the transparent window, the sprites, the pointer
# ============================================================================================

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_gdiplus = ctypes.WinDLL("gdiplus")


def _fn(dll: Any, name: str, restype: Any, *argtypes: Any) -> Any:
    fn = getattr(dll, name)
    fn.restype = restype
    fn.argtypes = list(argtypes)
    return fn


LRESULT = ctypes.c_ssize_t
_WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class _WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT), ("lpfnWndProc", _WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
                ("hIconSm", wintypes.HICON)]


class _BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 1)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


class _GdiplusStartupInput(ctypes.Structure):
    _fields_ = [("GdiplusVersion", ctypes.c_uint32), ("DebugEventCallback", ctypes.c_void_p),
                ("SuppressBackgroundThread", wintypes.BOOL), ("SuppressExternalCodecs", wintypes.BOOL)]


class _GpRect(ctypes.Structure):
    _fields_ = [("X", ctypes.c_int), ("Y", ctypes.c_int), ("Width", ctypes.c_int), ("Height", ctypes.c_int)]


class _BitmapData(ctypes.Structure):
    _fields_ = [("Width", ctypes.c_uint), ("Height", ctypes.c_uint), ("Stride", ctypes.c_int),
                ("PixelFormat", ctypes.c_int), ("Scan0", ctypes.c_void_p), ("Reserved", ctypes.c_size_t)]


_P = ctypes.POINTER
_VP = ctypes.c_void_p
_F = ctypes.c_float

_DefWindowProcW = _fn(_user32, "DefWindowProcW", LRESULT, wintypes.HWND, wintypes.UINT,
                      wintypes.WPARAM, wintypes.LPARAM)
_RegisterClassExW = _fn(_user32, "RegisterClassExW", wintypes.ATOM, _P(_WNDCLASSEXW))
_UnregisterClassW = _fn(_user32, "UnregisterClassW", wintypes.BOOL, wintypes.LPCWSTR, wintypes.HINSTANCE)
_CreateWindowExW = _fn(_user32, "CreateWindowExW", wintypes.HWND, wintypes.DWORD, wintypes.LPCWSTR,
                       wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                       ctypes.c_int, wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, _VP)
_DestroyWindow = _fn(_user32, "DestroyWindow", wintypes.BOOL, wintypes.HWND)
_ShowWindow = _fn(_user32, "ShowWindow", wintypes.BOOL, wintypes.HWND, ctypes.c_int)
_SetWindowPos = _fn(_user32, "SetWindowPos", wintypes.BOOL, wintypes.HWND, wintypes.HWND, ctypes.c_int,
                    ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT)
_UpdateLayeredWindow = _fn(_user32, "UpdateLayeredWindow", wintypes.BOOL, wintypes.HWND, wintypes.HDC,
                           _P(wintypes.POINT), _P(wintypes.SIZE), wintypes.HDC, _P(wintypes.POINT),
                           wintypes.COLORREF, _P(_BLENDFUNCTION), wintypes.DWORD)
_PeekMessageW = _fn(_user32, "PeekMessageW", wintypes.BOOL, _P(wintypes.MSG), wintypes.HWND,
                    wintypes.UINT, wintypes.UINT, wintypes.UINT)
_TranslateMessage = _fn(_user32, "TranslateMessage", wintypes.BOOL, _P(wintypes.MSG))
_DispatchMessageW = _fn(_user32, "DispatchMessageW", LRESULT, _P(wintypes.MSG))
_GetDC = _fn(_user32, "GetDC", wintypes.HDC, wintypes.HWND)
_ReleaseDC = _fn(_user32, "ReleaseDC", ctypes.c_int, wintypes.HWND, wintypes.HDC)
_GetCursorPos = _fn(_user32, "GetCursorPos", wintypes.BOOL, _P(wintypes.POINT))
_SetCursorPos = _fn(_user32, "SetCursorPos", wintypes.BOOL, ctypes.c_int, ctypes.c_int)
_GetWindowRect = _fn(_user32, "GetWindowRect", wintypes.BOOL, wintypes.HWND, _P(wintypes.RECT))
_IsWindow = _fn(_user32, "IsWindow", wintypes.BOOL, wintypes.HWND)
_MonitorFromWindow = _fn(_user32, "MonitorFromWindow", wintypes.HANDLE, wintypes.HWND, wintypes.DWORD)
_GetMonitorInfoW = _fn(_user32, "GetMonitorInfoW", wintypes.BOOL, wintypes.HANDLE, _P(_MONITORINFO))
_GetModuleHandleW = _fn(_kernel32, "GetModuleHandleW", wintypes.HMODULE, wintypes.LPCWSTR)
try:
    _SetProcessDpiAwarenessContext = _fn(_user32, "SetProcessDpiAwarenessContext", wintypes.BOOL, _VP)
    _GetDpiForWindow = _fn(_user32, "GetDpiForWindow", wintypes.UINT, wintypes.HWND)
except AttributeError:          # Windows < 10 1703
    _SetProcessDpiAwarenessContext = _GetDpiForWindow = None

_CreateCompatibleDC = _fn(_gdi32, "CreateCompatibleDC", wintypes.HDC, wintypes.HDC)
_DeleteDC = _fn(_gdi32, "DeleteDC", wintypes.BOOL, wintypes.HDC)
_CreateDIBSection = _fn(_gdi32, "CreateDIBSection", wintypes.HBITMAP, wintypes.HDC, _P(_BITMAPINFO),
                        wintypes.UINT, _P(_VP), wintypes.HANDLE, wintypes.DWORD)
_SelectObject = _fn(_gdi32, "SelectObject", wintypes.HGDIOBJ, wintypes.HDC, wintypes.HGDIOBJ)
_DeleteObject = _fn(_gdi32, "DeleteObject", wintypes.BOOL, wintypes.HGDIOBJ)

_GdiplusStartup = _fn(_gdiplus, "GdiplusStartup", ctypes.c_int, _P(ctypes.c_size_t),
                      _P(_GdiplusStartupInput), _VP)
_GdiplusShutdown = _fn(_gdiplus, "GdiplusShutdown", None, ctypes.c_size_t)
_GdipCreateBitmapFromFile = _fn(_gdiplus, "GdipCreateBitmapFromFile", ctypes.c_int, wintypes.LPCWSTR, _P(_VP))
_GdipCreateBitmapFromScan0 = _fn(_gdiplus, "GdipCreateBitmapFromScan0", ctypes.c_int, ctypes.c_int,
                                 ctypes.c_int, ctypes.c_int, ctypes.c_int, _VP, _P(_VP))
_GdipGetImageWidth = _fn(_gdiplus, "GdipGetImageWidth", ctypes.c_int, _VP, _P(ctypes.c_uint))
_GdipGetImageHeight = _fn(_gdiplus, "GdipGetImageHeight", ctypes.c_int, _VP, _P(ctypes.c_uint))
_GdipBitmapLockBits = _fn(_gdiplus, "GdipBitmapLockBits", ctypes.c_int, _VP, _P(_GpRect), ctypes.c_uint,
                          ctypes.c_int, _P(_BitmapData))
_GdipBitmapUnlockBits = _fn(_gdiplus, "GdipBitmapUnlockBits", ctypes.c_int, _VP, _P(_BitmapData))
_GdipDisposeImage = _fn(_gdiplus, "GdipDisposeImage", ctypes.c_int, _VP)
_GdipGetImageGraphicsContext = _fn(_gdiplus, "GdipGetImageGraphicsContext", ctypes.c_int, _VP, _P(_VP))
_GdipDeleteGraphics = _fn(_gdiplus, "GdipDeleteGraphics", ctypes.c_int, _VP)
_GdipSetSmoothingMode = _fn(_gdiplus, "GdipSetSmoothingMode", ctypes.c_int, _VP, ctypes.c_int)
_GdipSetInterpolationMode = _fn(_gdiplus, "GdipSetInterpolationMode", ctypes.c_int, _VP, ctypes.c_int)
_GdipSetPixelOffsetMode = _fn(_gdiplus, "GdipSetPixelOffsetMode", ctypes.c_int, _VP, ctypes.c_int)
_GdipGraphicsClear = _fn(_gdiplus, "GdipGraphicsClear", ctypes.c_int, _VP, ctypes.c_uint32)
_GdipResetWorldTransform = _fn(_gdiplus, "GdipResetWorldTransform", ctypes.c_int, _VP)
_GdipTranslateWorldTransform = _fn(_gdiplus, "GdipTranslateWorldTransform", ctypes.c_int, _VP, _F, _F,
                                   ctypes.c_int)
_GdipRotateWorldTransform = _fn(_gdiplus, "GdipRotateWorldTransform", ctypes.c_int, _VP, _F, ctypes.c_int)
_GdipScaleWorldTransform = _fn(_gdiplus, "GdipScaleWorldTransform", ctypes.c_int, _VP, _F, _F, ctypes.c_int)
_GdipDrawImageRectRect = _fn(_gdiplus, "GdipDrawImageRectRect", ctypes.c_int, _VP, _VP, _F, _F, _F, _F,
                             _F, _F, _F, _F, ctypes.c_int, _VP, _VP, _VP)
_GdipCreatePen1 = _fn(_gdiplus, "GdipCreatePen1", ctypes.c_int, ctypes.c_uint32, _F, ctypes.c_int, _P(_VP))
_GdipSetPenColor = _fn(_gdiplus, "GdipSetPenColor", ctypes.c_int, _VP, ctypes.c_uint32)
_GdipSetPenLineCap197819 = _fn(_gdiplus, "GdipSetPenLineCap197819", ctypes.c_int, _VP, ctypes.c_int,
                               ctypes.c_int, ctypes.c_int)
_GdipDeletePen = _fn(_gdiplus, "GdipDeletePen", ctypes.c_int, _VP)
_GdipDrawLine = _fn(_gdiplus, "GdipDrawLine", ctypes.c_int, _VP, _VP, _F, _F, _F, _F)
_GdipFlush = _fn(_gdiplus, "GdipFlush", ctypes.c_int, _VP, ctypes.c_int)

try:
    _shcore = ctypes.WinDLL("shcore")
    _GetDpiForMonitor = _fn(_shcore, "GetDpiForMonitor", ctypes.c_long, wintypes.HANDLE, ctypes.c_int,
                            _P(wintypes.UINT), _P(wintypes.UINT))
except (OSError, AttributeError):
    _GetDpiForMonitor = None

PER_MONITOR_AWARE_V2 = -4
WS_POPUP = 0x80000000
WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_TOPMOST = 0x80000, 0x20, 0x8
WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x80, 0x08000000
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x1, 0x2, 0x10
HWND_TOPMOST = -1
WM_NCHITTEST, WM_MOUSEACTIVATE = 0x84, 0x21
HTTRANSPARENT, MA_NOACTIVATE = -1, 3
PM_REMOVE = 1
ULW_ALPHA, AC_SRC_OVER, AC_SRC_ALPHA = 2, 0, 1
MONITOR_DEFAULTTONEAREST = 2
PIXEL_FORMAT_32BPP_ARGB, PIXEL_FORMAT_32BPP_PARGB = 0x26200A, 0xE200B
IMAGE_LOCK_READ = 1
UNIT_PIXEL = 2
MATRIX_APPEND = 1
SMOOTHING_ANTIALIAS, INTERPOLATION_HQ_BICUBIC, INTERPOLATION_HQ_BILINEAR = 4, 7, 6
PIXEL_OFFSET_HQ = 2
LINE_CAP_ROUND = 2


class GdiPlus:
    """GDI+ started for the lifetime of the ``with`` block."""

    def __enter__(self) -> GdiPlus:
        self.token = ctypes.c_size_t()
        startup = _GdiplusStartupInput(GdiplusVersion=1)
        status = _GdiplusStartup(ctypes.byref(self.token), ctypes.byref(startup), None)
        if status != 0:
            raise OSError(f"GdiplusStartup failed ({status})")
        return self

    def __exit__(self, *exc: object) -> None:
        _GdiplusShutdown(self.token)


def _check(status: int, what: str) -> None:
    if status != 0:
        raise OSError(f"{what} failed (GDI+ status {status})")


def _image_size(image: int) -> tuple[int, int]:
    w, h = ctypes.c_uint(), ctypes.c_uint()
    _check(_GdipGetImageWidth(image, ctypes.byref(w)), "GdipGetImageWidth")
    _check(_GdipGetImageHeight(image, ctypes.byref(h)), "GdipGetImageHeight")
    return w.value, h.value


def alpha_box(alpha_rows: Sequence[bytes]) -> tuple[int, int, int, int] | None:
    """``(left, top, right, bottom)`` (exclusive) of the non-transparent pixels, None if none.
    ``alpha_rows``: one bytes object of alpha values per row."""
    top = bottom = None
    left, right = None, 0
    for y, row in enumerate(alpha_rows):
        stripped = row.lstrip(b"\x00")
        if not stripped:
            continue
        first = len(row) - len(stripped)
        last = len(row.rstrip(b"\x00"))
        top = y if top is None else top
        bottom = y + 1
        left = first if left is None else min(left, first)
        right = max(right, last)
    if top is None or left is None or bottom is None:
        return None
    return left, top, right, bottom


class Sprites:
    """The pet's poses, cut from the widget's sprite sheet and scaled to the play size.

    The sheet holds one cell per pose (``cell_css`` CSS pixels square, rendered at ``sheet_scale``)
    with the 200×200 pet SVG in its middle. All poses share one crop (the union of their
    pixels) and one reference point: the middle of the ``normal`` pose's pixels."""

    def __init__(self, path: str, poses: Sequence[str], cell_css: int, sheet_scale: float,
                 px_per_unit: float) -> None:
        self.poses = list(poses)
        self.images: dict[str, int] = {}
        sheet = _VP()
        _check(_GdipCreateBitmapFromFile(os.path.abspath(path), ctypes.byref(sheet)), "loading the sprites")
        try:
            width, height = _image_size(sheet.value)
            cell = width // len(self.poses)
            boxes = self._alpha_boxes(sheet.value, cell, height)
            self.boxes = boxes                              # (for tests: every pose has pixels)
            known = [b for b in boxes if b is not None]
            if not known:
                raise ValueError("the sprite sheet is empty")
            pad = 4
            left = max(0, min(b[0] for b in known) - pad)
            top = max(0, min(b[1] for b in known) - pad)
            right = min(cell, max(b[2] for b in known) + pad)
            bottom = min(height, max(b[3] for b in known) + pad)
            normal = boxes[self.poses.index("normal")] if "normal" in self.poses else None
            ref = normal or known[0]
            self.k = px_per_unit / sheet_scale                  # play pixels per sheet pixel
            self.w = max(1, round((right - left) * self.k))
            self.h = max(1, round((bottom - top) * self.k))
            self.ref = (((ref[0] + ref[2]) / 2 - left) * self.k, ((ref[1] + ref[3]) / 2 - top) * self.k)
            self.pet_w = (ref[2] - ref[0]) * self.k
            self.pet_h = (ref[3] - ref[1]) * self.k
            # the reference point in SVG units (where the pet is in the widget)
            pad_css = (cell_css - 200) / 2
            self.anchor_units = ((ref[0] + ref[2]) / 2 / sheet_scale - pad_css,
                                 (ref[1] + ref[3]) / 2 / sheet_scale - pad_css)
            for index, pose in enumerate(self.poses):
                self.images[pose] = self._scaled(sheet.value, index * cell + left, top,
                                                 right - left, bottom - top)
        finally:
            _GdipDisposeImage(sheet.value)        # releases the file

    @staticmethod
    def _alpha_boxes(sheet: int, cell: int, height: int) -> list[tuple[int, int, int, int] | None]:
        width = cell * (_image_size(sheet)[0] // cell)
        data = _BitmapData()
        rect = _GpRect(0, 0, width, height)
        _check(_GdipBitmapLockBits(sheet, ctypes.byref(rect), IMAGE_LOCK_READ, PIXEL_FORMAT_32BPP_ARGB,
                                   ctypes.byref(data)), "GdipBitmapLockBits")
        try:
            raw = ctypes.string_at(data.Scan0, data.Stride * height)
        finally:
            _GdipBitmapUnlockBits(sheet, ctypes.byref(data))
        boxes = []
        for c in range(width // cell):
            rows = [raw[y * data.Stride + c * cell * 4 + 3: y * data.Stride + (c + 1) * cell * 4: 4]
                    for y in range(height)]
            boxes.append(alpha_box(rows))
        return boxes

    def _scaled(self, sheet: int, x: int, y: int, w: int, h: int) -> int:
        image = _VP()
        _check(_GdipCreateBitmapFromScan0(self.w, self.h, 0, PIXEL_FORMAT_32BPP_PARGB, None,
                                          ctypes.byref(image)), "GdipCreateBitmapFromScan0")
        graphics = _VP()
        _check(_GdipGetImageGraphicsContext(image.value, ctypes.byref(graphics)), "GdipGetImageGraphicsContext")
        try:
            _GdipSetInterpolationMode(graphics.value, INTERPOLATION_HQ_BICUBIC)
            _GdipSetPixelOffsetMode(graphics.value, PIXEL_OFFSET_HQ)
            _GdipGraphicsClear(graphics.value, 0)
            _check(_GdipDrawImageRectRect(graphics.value, sheet, 0, 0, self.w, self.h, x, y, w, h,
                                          UNIT_PIXEL, None, None, None), "drawing a pose")
        finally:
            _GdipDeleteGraphics(graphics.value)
        return image.value

    def close(self) -> None:
        for image in self.images.values():
            _GdipDisposeImage(image)
        self.images.clear()


class Canvas:
    """A premultiplied 32-bit bitmap (a DIB section) that GDI+ draws into and that
    ``UpdateLayeredWindow`` shows."""

    def __init__(self, size: int, unit: float = 1.0) -> None:
        self.size = size
        self.dc = _CreateCompatibleDC(None)
        info = _BITMAPINFO()
        info.bmiHeader = _BITMAPINFOHEADER(biSize=ctypes.sizeof(_BITMAPINFOHEADER), biWidth=size,
                                           biHeight=-size, biPlanes=1, biBitCount=32, biCompression=0)
        self.bits = _VP()
        self.bitmap = _CreateDIBSection(self.dc, ctypes.byref(info), 0, ctypes.byref(self.bits), None, 0)
        if not self.bitmap or not self.bits.value:
            _DeleteDC(self.dc)
            raise OSError("CreateDIBSection failed")
        self._old = _SelectObject(self.dc, self.bitmap)
        self.image = _VP()
        _check(_GdipCreateBitmapFromScan0(size, size, size * 4, PIXEL_FORMAT_32BPP_PARGB, self.bits,
                                          ctypes.byref(self.image)), "wrapping the canvas")
        self.graphics = _VP()
        _check(_GdipGetImageGraphicsContext(self.image.value, ctypes.byref(self.graphics)),
               "GdipGetImageGraphicsContext")
        _GdipSetSmoothingMode(self.graphics.value, SMOOTHING_ANTIALIAS)
        _GdipSetInterpolationMode(self.graphics.value, INTERPOLATION_HQ_BILINEAR)
        _GdipSetPixelOffsetMode(self.graphics.value, PIXEL_OFFSET_HQ)
        self.pen = _VP()
        _check(_GdipCreatePen1(0x80FFFFFF, 3.0 * unit, UNIT_PIXEL, ctypes.byref(self.pen)), "GdipCreatePen1")
        _GdipSetPenLineCap197819(self.pen.value, LINE_CAP_ROUND, LINE_CAP_ROUND, 0)

    def draw(self, sprites: Sprites, frame: Frame, velocity: Point, unit: float) -> None:
        g = self.graphics.value
        half = self.size / 2
        _GdipGraphicsClear(g, 0)
        speed = math.hypot(*velocity)
        if speed > 700 * unit:                       # speed lines behind a fast pet
            dx, dy = -velocity[0] / speed, -velocity[1] / speed
            nx, ny = -dy, dx
            length = min(70.0, speed * 0.035) * unit
            r = 0.45 * max(sprites.pet_w, sprites.pet_h) * frame.scale
            pen = self.pen.value
            for i, alpha in ((-1, 0x55), (0, 0x80), (1, 0x55)):
                sx = half + dx * r + nx * i * r * 0.45
                sy = half + dy * r + ny * i * r * 0.45
                _GdipSetPenColor(pen, (alpha << 24) | 0xFFFFFF)
                _GdipDrawLine(g, pen, sx, sy, sx + dx * length, sy + dy * length)
        image = sprites.images.get(frame.pose) or sprites.images[sprites.poses[0]]
        squash = max(0.5, frame.squash)
        _GdipResetWorldTransform(g)
        _GdipScaleWorldTransform(g, frame.scale / squash ** 0.5, frame.scale * squash ** 0.5, MATRIX_APPEND)
        _GdipRotateWorldTransform(g, frame.angle, MATRIX_APPEND)
        _GdipTranslateWorldTransform(g, half, half, MATRIX_APPEND)
        _GdipDrawImageRectRect(g, image, -sprites.ref[0], -sprites.ref[1], sprites.w, sprites.h,
                               0, 0, sprites.w, sprites.h, UNIT_PIXEL, None, None, None)
        _GdipResetWorldTransform(g)
        _GdipFlush(g, 1)

    def pixel_alpha(self, x: int, y: int) -> int:
        """(For tests.) The alpha of one pixel of the canvas."""
        return ctypes.string_at(self.bits.value + (y * self.size + x) * 4, 4)[3]

    def close(self) -> None:
        _GdipDeletePen(self.pen.value)
        _GdipDeleteGraphics(self.graphics.value)
        _GdipDisposeImage(self.image.value)
        _SelectObject(self.dc, self._old)
        _DeleteObject(self.bitmap)
        _DeleteDC(self.dc)


@_WNDPROC
def _wndproc(hwnd: int, msg: int, wparam: int, lparam: int) -> int:
    if msg == WM_NCHITTEST:
        return HTTRANSPARENT          # every click goes through
    if msg == WM_MOUSEACTIVATE:
        return MA_NOACTIVATE
    return _DefWindowProcW(hwnd, msg, wparam, lparam)


class Overlay:
    """The pet's own window: transparent, click-through, topmost, never activated, not on the
    taskbar. It is moved with the pet (``UpdateLayeredWindow`` moves and repaints at once)."""

    CLASS = "ProjektsogKlippeLeger"

    def __init__(self, size: int, unit: float = 1.0) -> None:
        self.canvas = Canvas(size, unit)
        self.instance = _GetModuleHandleW(None)
        wc = _WNDCLASSEXW(cbSize=ctypes.sizeof(_WNDCLASSEXW), lpfnWndProc=_wndproc,
                          hInstance=self.instance, lpszClassName=self.CLASS)
        _RegisterClassExW(ctypes.byref(wc))
        self.hwnd = _CreateWindowExW(WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_TOOLWINDOW
                                     | WS_EX_NOACTIVATE, self.CLASS, "Klippe leger", WS_POPUP,
                                     0, 0, size, size, None, None, self.instance, None)
        if not self.hwnd:
            self.canvas.close()
            raise OSError(f"CreateWindowExW failed ({ctypes.get_last_error()})")
        self.shown = False
        self.screen = _GetDC(None)

    def show(self, frame: Frame, alpha: int = 255) -> bool:
        size = self.canvas.size
        dst = wintypes.POINT(round(frame.x - size / 2), round(frame.y - size / 2))
        blend = _BLENDFUNCTION(AC_SRC_OVER, 0, max(0, min(255, alpha)), AC_SRC_ALPHA)
        ok = bool(_UpdateLayeredWindow(self.hwnd, self.screen, ctypes.byref(dst),
                                       ctypes.byref(wintypes.SIZE(size, size)), self.canvas.dc,
                                       ctypes.byref(wintypes.POINT(0, 0)), 0, ctypes.byref(blend), ULW_ALPHA))
        if not self.shown:
            _ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
            _SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
            self.shown = True
        return ok

    @staticmethod
    def pump() -> None:
        msg = wintypes.MSG()
        while _PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
            _TranslateMessage(ctypes.byref(msg))
            _DispatchMessageW(ctypes.byref(msg))

    def close(self) -> None:
        _ShowWindow(self.hwnd, SW_HIDE)
        _DestroyWindow(self.hwnd)
        _UnregisterClassW(self.CLASS, self.instance)
        _ReleaseDC(None, self.screen)
        self.canvas.close()


def pointer_position() -> Point:
    p = wintypes.POINT()
    _GetCursorPos(ctypes.byref(p))
    return float(p.x), float(p.y)


def put_pointer(p: Point) -> tuple[int, int]:
    x, y = round(p[0]), round(p[1])
    _SetCursorPos(x, y)
    return x, y


def monitor_of(hwnd: int) -> tuple[Rect, Rect, float]:
    """``(work area, whole monitor, scale)`` of the monitor the window is on."""
    handle = _MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    info = _MONITORINFO(cbSize=ctypes.sizeof(_MONITORINFO))
    if not handle or not _GetMonitorInfoW(handle, ctypes.byref(info)):
        raise OSError("the widget's monitor is unknown")
    dpi = 0
    if _GetDpiForMonitor is not None:
        dx, dy = wintypes.UINT(), wintypes.UINT()
        if _GetDpiForMonitor(handle, 0, ctypes.byref(dx), ctypes.byref(dy)) == 0:
            dpi = dx.value
    if not dpi and _GetDpiForWindow is not None:
        dpi = _GetDpiForWindow(hwnd)
    w, m = info.rcWork, info.rcMonitor
    return (Rect(w.left, w.top, w.right, w.bottom), Rect(m.left, m.top, m.right, m.bottom),
            (dpi or 96) / 96)


def window_rect(hwnd: int) -> Rect | None:
    r = wintypes.RECT()
    if not _IsWindow(hwnd) or not _GetWindowRect(hwnd, ctypes.byref(r)):
        return None
    return Rect(r.left, r.top, r.right, r.bottom)


# ============================================================================================
# The game loop
# ============================================================================================

class Watch:
    """Why the game has to stop: the user touched something, the screen locked, quit."""

    def __init__(self, input_tick: Callable[[], int], locked: Callable[[], bool],
                 clock: Callable[[], float] = time.monotonic, start_tick: int | None = None) -> None:
        self._input_tick = input_tick
        self._locked = locked
        self._clock = clock
        # The main process passes the last-input time it saw when it decided to play: a touch
        # while this process was starting stops the game before it begins.
        self.start_tick = input_tick() if start_tick is None else start_tick
        self.quit = threading.Event()
        self._next_lock_check = 0.0

    def reason(self, pointer_moved: bool) -> str | None:
        if self.quit.is_set():
            return "quit"
        if pointer_moved or self._input_tick() != self.start_tick:
            return "touched"
        now = self._clock()
        if now >= self._next_lock_check:
            self._next_lock_check = now + LOCK_CHECK_S
            if self._locked():
                return "locked"
        return None


class Player:
    """Runs a show: draws every frame, moves the pointer, watches for the user."""

    def __init__(self, geo: Geometry, segments: list[Segment], sprites: Any, overlay: Any,
                 watch: Watch, emit: Callable[[dict[str, Any]], None], *, move_pointer: bool = True,
                 pointer: Point | None = None,
                 get_pointer: Callable[[], Point] = pointer_position,
                 set_pointer: Callable[[Point], tuple[int, int]] = put_pointer,
                 fps: int = FPS) -> None:
        self.geo = geo
        self.segments = segments
        self.sprites = sprites
        self.overlay = overlay
        self.watch = watch
        self.emit = emit
        self.move_pointer = move_pointer
        self._get_pointer = get_pointer
        self._set_pointer = set_pointer
        self._fps = fps
        self.original = pointer if pointer is not None else get_pointer()
        self.placed: tuple[int, int] | None = None     # where we last put the pointer
        self.moved = False
        self.home_sent = False

    def _pointer_taken_back(self) -> bool:
        if self.placed is None:
            return False
        x, y = self._get_pointer()
        return abs(x - self.placed[0]) > POINTER_SLACK_PX or abs(y - self.placed[1]) > POINTER_SLACK_PX

    def _give_pointer_back(self) -> None:
        if self.moved:
            self._set_pointer(self.original)
            self.moved = False
        self.placed = None

    def run(self) -> str:
        reason = "done"
        segments = self.segments
        started = time.perf_counter()
        last: Frame | None = None
        last_t = 0.0
        out_sent = False
        aborted = False
        frame_s = 1 / self._fps
        try:
            while True:
                t = time.perf_counter() - started
                if t > MAX_S:
                    reason = reason if aborted else "done"
                    break
                if not aborted:
                    why = self.watch.reason(self._pointer_taken_back())
                    if why is not None:
                        reason = why
                        self._give_pointer_back()
                        if why in ("locked", "quit") or last is None:
                            break
                        segments = abort_show(self.geo, last)
                        started, t, aborted = time.perf_counter(), 0.0, True
                        log.info("the game stopped (%s): flying home", why)
                current = frame_at(segments, t)
                if current is None:
                    break
                index, frame = current
                if not aborted and self.move_pointer:
                    if frame.cursor is not None and segments[index].holds_pointer:
                        self.placed = self._set_pointer(frame.cursor)
                        self.moved = True
                    elif self.moved and not segments[index].holds_pointer:
                        self._give_pointer_back()            # the pointer is back, exactly
                dt = max(1e-3, t - last_t)
                velocity = ((frame.x - last.x) / dt, (frame.y - last.y) / dt) if last is not None else (0.0, 0.0)
                self.overlay.canvas.draw(self.sprites, frame, velocity, self.geo.unit)
                self.overlay.show(frame)
                if not out_sent:
                    self.emit({"event": "out"})
                    out_sent = True
                self.overlay.pump()
                last, last_t = frame, t
                pause = frame_s - (time.perf_counter() - started - t)
                if pause > 0:
                    time.sleep(pause)
        except BaseException:
            reason = "error"
            raise
        finally:
            if not aborted:
                self._give_pointer_back()
            self.emit({"event": "home", "reason": reason})
            self.home_sent = True
        if last is not None and reason not in ("locked", "quit"):
            time.sleep(0.25)      # the widget shows its pet again while this last frame covers it
        return reason


# ============================================================================================
# Entry point
# ============================================================================================

class _Emitter:
    def __init__(self, stream: BinaryIO | None) -> None:
        self._stream = stream
        self._lock = threading.Lock()

    def __call__(self, message: dict[str, Any]) -> None:
        if self._stream is None:
            return
        with self._lock:
            try:
                self._stream.write(json.dumps(message).encode("ascii") + b"\n")
                self._stream.flush()
            except (OSError, ValueError):
                self._stream = None


def _watch_stdin(stream: BinaryIO | None, quit_event: threading.Event) -> None:
    """``quit`` or the end of stdin (the main process is gone) ends the game."""
    def read() -> None:
        try:
            for raw in stream:            # type: ignore[union-attr]
                if raw.strip() == b"quit":
                    break
        except (OSError, ValueError):
            pass
        quit_event.set()
    if stream is not None:
        threading.Thread(target=read, name="petplay-stdin", daemon=True).start()


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
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s petplay[%(process)d]: %(message)s"))
    root.addHandler(handler)


def _numbers(text: str, count: int) -> list[float]:
    values = [float(part) for part in text.split(",")]
    if len(values) != count or not all(math.isfinite(v) for v in values):
        raise ValueError(f"expected {count} numbers: {text!r}")
    return values


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="projektsog.petplay_child")
    parser.add_argument("--sprites", required=True, help="the sprite sheet (PNG)")
    parser.add_argument("--poses", required=True, help="the poses in the sheet, comma separated")
    parser.add_argument("--cell", type=int, default=240, help="a cell's size in CSS pixels")
    parser.add_argument("--sheet-scale", type=float, default=2.0)
    parser.add_argument("--widget", type=int, required=True, help="the widget window (HWND)")
    parser.add_argument("--pet", default="", help="the pet in the widget page: x,y,w,h (CSS px)")
    parser.add_argument("--view", default="", help="the widget page's size: w,h (CSS px)")
    parser.add_argument("--stage", default="baby")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--input-tick", type=int, default=None,
                        help="the last-input time (GetLastInputInfo) when the game was decided")
    parser.add_argument("--no-pointer", action="store_true", help="play without moving the pointer")
    parser.add_argument("--log-file")
    return parser.parse_args(list(argv))


def geometry_for(args: argparse.Namespace, sprites: Sprites, work: Rect, unit: float,
                 widget: Rect) -> Geometry:
    margin = 36 * unit
    play = work.inset(margin, margin)
    pet_px = 1.05 * 200                       # the widget's pet when the page did not say
    try:
        pet_css = _numbers(args.pet, 4)
        view_css = _numbers(args.view, 2)
        home = home_in_widget(widget, pet_css, view_css, unit, sprites.anchor_units)
        pet_px = min(pet_css[2], pet_css[3])
    except ValueError:
        home = (widget.left + widget.width / 2, widget.top + widget.height * 0.45)
    home_scale = (pet_px / 200) / PLAY_SIZE
    return Geometry(work=work, play=play, home=home, home_scale=home_scale,
                    pet_w=sprites.pet_w, pet_h=sprites.pet_h, unit=unit)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    _configure_logging(args.log_file)
    if _SetProcessDpiAwarenessContext is not None:
        _SetProcessDpiAwarenessContext(ctypes.c_void_p(PER_MONITOR_AWARE_V2))
    emit = _Emitter(getattr(sys.stdout, "buffer", None))
    from .petplay import desktop_locked, last_input_tick
    watch = Watch(last_input_tick, desktop_locked, start_tick=args.input_tick)
    _watch_stdin(getattr(sys.stdin, "buffer", None), watch.quit)
    player: Player | None = None
    try:
        with GdiPlus():
            widget = window_rect(args.widget)
            if widget is None:
                raise OSError("the widget window is gone")
            work, _monitor, unit = monitor_of(args.widget)
            sprites = Sprites(args.sprites, args.poses.split(","), args.cell, args.sheet_scale,
                              PLAY_SIZE * unit)
            try:
                geo = geometry_for(args, sprites, work, unit, widget)
                pointer = pointer_position()
                segments = build_show(geo, random.Random(args.seed), pointer, args.stage)
                side = 2 * math.ceil(0.5 * math.hypot(sprites.w, sprites.h) * max(1.0, geo.home_scale) * 1.25
                                     + 80 * unit)
                overlay = Overlay(side, unit)
                try:
                    log.info("playing: %s (%.1f s), pointer %s", " → ".join(s.name for s in segments),
                             show_length(segments), "inside" if geo.play.contains(*pointer) else "on another screen")
                    player = Player(geo, segments, sprites, overlay, watch, emit,
                                    move_pointer=not args.no_pointer, pointer=pointer)
                    log.info("home (%s)", player.run())
                finally:
                    overlay.close()
            finally:
                sprites.close()
    except Exception:
        log.exception("the game failed")
    if player is None or not player.home_sent:
        emit({"event": "home", "reason": "error"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
