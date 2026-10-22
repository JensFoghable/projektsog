"""Snow on monitor 2 (SPEC §23.5): the helper process that lets it snow over the widget's monitor.

``projektsog.sne`` (main process) starts it when a snowy door of the Christmas calendar is opened.
One layered window covers the work area of the monitor Klippe's widget is on – transparent, every
click goes through, topmost, never activated, not on the taskbar – and white flakes fall in it,
swaying and drifting with a slow wind. In the last seconds no new flakes come and the snow fades
out. It never takes the focus, never moves the pointer and never clicks. It ends after
``--seconds`` (at most ``MAX_S``), on ``quit`` or the end of stdin (the main process is gone) and on
a locked screen.

Events on stdout, one JSON object per line (like the other helpers): ``{"event": "out"}`` after the
first frame, ``{"event": "home", "reason": "done" | "quit" | "locked" | "error"}`` at the end.

The first half is the snowfall – pure arithmetic, tested without a screen. The second half draws
it with ``petplay_child``'s GDI+ canvas and window.
"""

from __future__ import annotations

import argparse
import ctypes
import logging
import logging.handlers
import math
import os
import random
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

FPS = 20
MAX_S = 120.0
FADE_IN_S = 1.0               # the first flakes fade in …
FADE_OUT_S = 3.0              # … and in the last seconds no new ones come and the snow fades out
MAX_FLAKES = 260
LOCK_CHECK_S = 0.5
TOPMOST_S = 2.0               # topmost is asserted again this often (the widget is topmost too)
RIM = 1.8                     # the soft rim's radius, in flake radii
RIM_ALPHA = 0.28


# ============================================================================================
# The snowfall (pure)
# ============================================================================================

def flake_count(width: float, height: float) -> int:
    """More flakes on a bigger monitor – at most ``MAX_FLAKES``."""
    return int(min(MAX_FLAKES, 90 + max(0.0, width) * max(0.0, height) / 25_000))


@dataclass
class Flake:
    base: float               # x without the sway
    y: float                  # its centre
    r: float                  # radius (px)
    alpha: float              # 0.55–0.95
    speed: float              # px/s downwards
    sway: float               # px to each side
    freq: float               # sways per second
    phase: float

    def x(self, t: float) -> float:
        return self.base + self.sway * math.sin(2 * math.pi * self.freq * t + self.phase)


def new_flake(rng: random.Random, width: float, height: float, scale: float = 1.0, *, top: bool = False) -> Flake:
    """A flake somewhere on the screen (at the start) or just above it (``top``)."""
    r = rng.uniform(1.5, 4.5) * scale
    y = -r * RIM - rng.uniform(0.0, 0.08 * height) if top else rng.uniform(-0.3 * height, height)
    # Big flakes fall a little faster than small ones.
    speed = (30 + 60 * (0.6 * rng.random() + 0.4 * (r / scale - 1.5) / 3.0)) * scale
    return Flake(base=rng.uniform(0.0, width), y=y, r=r, alpha=rng.uniform(0.55, 0.95), speed=speed,
                 sway=rng.uniform(6.0, 22.0) * scale, freq=rng.uniform(0.2, 0.6), phase=rng.uniform(0, 2 * math.pi))


class Snowfall:
    """One snowfall over a ``width`` × ``height`` area for ``seconds``: ``advance(dt)`` moves it on,
    ``dots()`` says what to draw – ``(x, y, r, alpha)`` per flake, the fading included."""

    def __init__(self, width: float, height: float, scale: float, seconds: float, rng: random.Random) -> None:
        self.width = max(1.0, float(width))
        self.height = max(1.0, float(height))
        self.scale = max(0.5, float(scale))
        self.seconds = max(1.0, min(MAX_S, float(seconds)))
        self.rng = rng
        self.t = 0.0
        self.wind_phase = rng.uniform(0, 2 * math.pi)
        self.flakes = [new_flake(rng, self.width, self.height, self.scale)
                       for _ in range(flake_count(self.width, self.height))]

    def wind(self) -> float:
        """Sideways drift (px/s): a slow breeze that turns now and then."""
        t = self.t
        return self.scale * (14 * math.sin(2 * math.pi * t / 17.0 + self.wind_phase)
                             + 5 * math.sin(2 * math.pi * t / 5.3 + 2 * self.wind_phase))

    def respawning(self) -> bool:
        return self.t < self.seconds - FADE_OUT_S

    def opacity(self) -> float:
        fade_in = min(1.0, self.t / FADE_IN_S)
        fade_out = min(1.0, max(0.0, (self.seconds - self.t) / FADE_OUT_S))
        return max(0.0, min(fade_in, fade_out))

    def done(self) -> bool:
        return self.t >= self.seconds

    def advance(self, dt: float) -> None:
        dt = max(0.0, min(0.25, dt))                 # a stall never throws the flakes far
        self.t += dt
        wind = self.wind()
        margin = 24 * self.scale
        span = self.width + 2 * margin
        respawn = self.respawning()
        alive: list[Flake] = []
        for flake in self.flakes:
            flake.y += flake.speed * dt
            flake.base += wind * dt
            if flake.base < -margin:                 # out at one side, in at the other
                flake.base += span
            elif flake.base > self.width + margin:
                flake.base -= span
            if flake.y - flake.r * RIM > self.height:
                if respawn:
                    alive.append(new_flake(self.rng, self.width, self.height, self.scale, top=True))
                continue
            alive.append(flake)
        self.flakes = alive

    def dots(self) -> list[tuple[float, float, float, float]]:
        k = self.opacity()
        if k <= 0:
            return []
        return [(flake.x(self.t), flake.y, flake.r, flake.alpha * k) for flake in self.flakes
                if flake.y + flake.r * RIM >= 0]


# ============================================================================================
# Drawing (Win32/GDI+ through petplay_child)
# ============================================================================================

def draw(canvas: Any, fall: Snowfall) -> None:
    """The flakes into the canvas (premultiplied 32-bit): a soft rim, then the white core."""
    from . import petplay_child as pc
    g = canvas.graphics.value
    brush = canvas.brush.value
    pc._GdipResetWorldTransform(g)
    pc._GdipGraphicsClear(g, 0)
    for x, y, r, alpha in fall.dots():
        rim = int(255 * alpha * RIM_ALPHA)
        if rim > 0:
            pc._GdipSetSolidFillColor(brush, (rim << 24) | 0xFFFFFF)
            pc._GdipFillEllipse(g, brush, x - r * RIM, y - r * RIM, 2 * r * RIM, 2 * r * RIM)
        pc._GdipSetSolidFillColor(brush, (int(255 * alpha) << 24) | 0xFFFFFF)
        pc._GdipFillEllipse(g, brush, x - r, y - r, 2 * r, 2 * r)
    pc._GdipFlush(g, 1)


def play(fall: Snowfall, overlay: Any, origin: tuple[float, float], quit_event: threading.Event,
         emit: Callable[[dict[str, Any]], None], *, locked: Callable[[], bool],
         keep_on_top: Callable[[], None] = lambda: None, fps: int = FPS,
         clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> str:
    """Draws the snowfall until it is over; returns why it ended (done | quit | locked)."""
    frame = 1.0 / fps
    last = clock()
    next_lock = next_top = 0.0
    shown = False
    while True:
        if quit_event.is_set():
            return "quit"
        now = clock()
        if now >= next_lock:
            next_lock = now + LOCK_CHECK_S
            if locked():
                return "locked"
        fall.advance(now - last)
        last = now
        if fall.done():
            return "done"
        draw(overlay.canvas, fall)
        overlay.show_at(origin[0], origin[1])
        if not shown:
            shown = True
            emit({"event": "out"})
        if now >= next_top:
            next_top = now + TOPMOST_S
            keep_on_top()
        overlay.pump()
        sleep(max(0.0, frame - (clock() - now)))


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
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s sne[%(process)d]: %(message)s"))
    root.addHandler(handler)


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="projektsog.sne_child")
    parser.add_argument("--widget", type=int, required=True, help="the widget window (HWND): its monitor")
    parser.add_argument("--seconds", type=float, default=45.0)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--log-file")
    return parser.parse_args(list(argv))


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    _configure_logging(args.log_file)
    from . import petplay_child as pc
    from .petplay import desktop_locked
    if pc._SetProcessDpiAwarenessContext is not None:
        pc._SetProcessDpiAwarenessContext(ctypes.c_void_p(pc.PER_MONITOR_AWARE_V2))
    emit = pc._Emitter(getattr(sys.stdout, "buffer", None))
    quit_event = threading.Event()
    pc._watch_stdin(getattr(sys.stdin, "buffer", None), quit_event)

    class SnowWindow(pc.Overlay):
        CLASS = "ProjektsogSne"

    reason = "error"
    try:
        with pc.GdiPlus():
            work, _monitor, scale = pc.monitor_of(args.widget)
            width, height = max(1, int(work.width)), max(1, int(work.height))
            fall = Snowfall(width, height, scale, min(MAX_S, max(1.0, args.seconds)), random.Random(args.seed))
            overlay = SnowWindow(width, scale, height)

            def keep_on_top() -> None:
                pc._SetWindowPos(overlay.hwnd, pc.HWND_TOPMOST, 0, 0, 0, 0,
                                 pc.SWP_NOMOVE | pc.SWP_NOSIZE | pc.SWP_NOACTIVATE)
            try:
                log.info("snow: %d flakes over %dx%d for %.0f s", len(fall.flakes), width, height, fall.seconds)
                reason = play(fall, overlay, (work.left, work.top), quit_event, emit, locked=desktop_locked,
                              keep_on_top=keep_on_top)
                log.info("the snow has stopped (%s)", reason)
            finally:
                overlay.close()
    except Exception:
        log.exception("the snow failed")
        reason = "error"
    emit({"event": "home", "reason": reason})
    return 0


if __name__ == "__main__":
    # End without the interpreter's shutdown (like petplay_child): the stdin reader thread may still
    # be blocked in a read on the pipe – "home" and the log are written already.
    status = main()
    logging.shutdown()
    os._exit(status)
