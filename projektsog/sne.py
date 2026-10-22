"""Snow on monitor 2 (SPEC §23.5): a snowy door of the Christmas calendar (and its demo) lets it
snow over the widget's monitor for a while.

``Sne.start(seconds)`` launches the helper ``projektsog.sne_child`` – one click-through window
over the monitor's work area, drawn with GDI+, that never takes the focus – and tells the widget
(SSE ``sne {sekunder, demo}``), which lets it snow harder in its box meanwhile. Snow that falls
already is kept; a locked screen or a full-screen program means no snow this time.
"""

from __future__ import annotations

import logging
import os
import random
import threading
from collections.abc import Callable
from typing import Any

from . import config, petplay

log = logging.getLogger(__name__)

CHILD_MODULE = "projektsog.sne_child"
MAX_S = 120
CLOSE_WAIT_S = 2.0
MSG_OFF = "Slå Klippe til først"


def desktop_reason(hwnd: int) -> str | None:
    """Why it cannot snow now: Klippe's window is not shown, the screen is locked, or something
    is full screen (None: it can)."""
    if not hwnd or not petplay.window_shown(hwnd):
        return "Klippe er ikke på skærmen"
    if petplay.desktop_locked():
        return "Skærmen er låst"
    if petplay.windows_quiet() or petplay.fullscreen_beside(hwnd):
        return "Noget kører i fuld skærm"
    return None


def _spawn_child(argv: list[str], on_out: Callable[[], None], on_exit: Callable[[str], None],
                 on_event: Callable[[dict[str, Any]], None]) -> Any:
    return petplay._Child(argv, on_out, on_exit, on_event, name="sne")


class Sne:
    """Starts and stops the snow helper (one at a time)."""

    def __init__(self, cfg: Any, bus: Any, *, widget: Any, spawn: Callable[..., Any] | None = None,
                 quiet: Callable[[int], str | None] | None = None, rng: random.Random | None = None,
                 log_file: str | None = None) -> None:
        self.cfg = cfg
        self.bus = bus
        self._widget = widget
        self._spawn = spawn or _spawn_child
        self._quiet = quiet or desktop_reason
        self._rng = rng or random.Random()
        self._log_file = log_file
        self._lock = threading.Lock()
        self._child: Any = None
        self._closed = False

    def start(self, seconds: Any, demo: bool = False) -> dict[str, Any]:
        """Let it snow for ``seconds`` (1–120). Raises ValueError while Klippe is off."""
        if not self.cfg.get("widget_enabled", False):
            raise ValueError(MSG_OFF)
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not seconds > 0:
            raise ValueError("Ugyldig værdi: sekunder")
        seconds = int(min(MAX_S, max(1, round(seconds))))
        with self._lock:
            if self._closed:
                return {"ok": False, "grund": "Projektsøg lukker"}
            if self._child is not None:
                return {"ok": True, "sner": True}            # it snows already: that snow is kept
        hwnd = int(getattr(self._widget, "hwnd", 0) or 0)
        try:
            reason = self._quiet(hwnd)
        except Exception:
            log.debug("could not look at the desktop", exc_info=True)
            reason = "Skrivebordet kunne ikke ses"
        if reason is not None:
            log.info("no snow now: %s", reason)
            return {"ok": False, "grund": reason}
        argv = petplay.child_argv(CHILD_MODULE) + [
            "--widget", str(hwnd), "--seconds", str(seconds), "--seed", str(self._rng.randrange(1 << 30)),
            "--log-file", self._log_file or os.path.join(config.log_dir(), "sne.log")]
        with self._lock:
            if self._closed or self._child is not None:
                return {"ok": self._child is not None}
            try:
                self._child = self._spawn(argv, self._on_out, self._on_exit, self._on_event)
            except OSError as exc:
                log.error("could not let it snow: %s", exc)
                return {"ok": False, "grund": "Sneen kunne ikke startes"}
        log.info("it snows for %d s%s", seconds, " (demo)" if demo else "")
        self.bus.publish("sne", {"sekunder": seconds, "demo": bool(demo)})
        return {"ok": True}

    def snowing(self) -> bool:
        with self._lock:
            return self._child is not None

    def close(self) -> None:
        """App exit: the snow stops at once (the helper is told to quit; stdin EOF ends it too)."""
        with self._lock:
            self._closed = True
            child = self._child
        if child is not None:
            child.stop(CLOSE_WAIT_S)

    # -- from the helper (its reader thread) ---------------------------------------------------
    def _on_out(self) -> None:
        pass

    def _on_event(self, message: dict[str, Any]) -> None:
        pass                                              # nothing else is expected

    def _on_exit(self, reason: str) -> None:
        with self._lock:
            self._child = None
        log.info("the snow has stopped (%s)", reason)
