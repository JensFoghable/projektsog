"""Klippe i bad (SPEC §23.6): loose files in the root of the current Resolve project's folder make
Klippe dirty – and when they are tidied away, Klippe gets a foam bath.

Every project is a copy of the template ("Final, Grafik, Klip, Logo, Musik, Project, Speak,
Tekst"); a clip, a voice-over or a logo lying loose in the project folder itself belongs in one of
them. ``Rod`` looks at the folder's root now and then (and soon after Resolve changes project),
counts the loose files and suggests a folder for each by its type and name. It is read-only
(SPEC §1): Projektsøg never moves a file – the user does, and Klippe is happy about it.

``GET /api/rod`` and SSE ``rod``: ``{aktiv, projekt, mappe, antal, niveau, filer: [{navn,
forslag, ny, slags}], mere, bad}``; ``bad`` is true only in the one event of the folder that was
messy at the last look and is tidy now (once per folder and day).
"""

from __future__ import annotations

import logging
import ntpath
import queue
import re
import threading
import time
from collections.abc import Callable, Collection
from datetime import date
from typing import Any

from . import levering

log = logging.getLogger(__name__)

POLL_S = 60.0
SOON_S = 2.0                      # after Resolve changed project (a ``resolve`` bus event)
FIRST_S = 10.0                    # the first look after the start
LIST_TIMEOUT_S = 5.0
MAX_ENTRIES = 400                 # entries read from the project folder's root
MAX_FILES_SHOWN = 50
DEMO_BATH_S = 8.0
MEMORY_MAX = 64                   # folders whose last count is remembered (for the bath)

MSG_OFF = "Slå Klippe til først"
IGNORED_SUFFIXES = (".lnk", ".url", ".projektsog-tmp")
IGNORED_NAMES = frozenset({"desktop.ini", "thumbs.db", ".ds_store", "icon\r"})

IDLE: dict[str, Any] = {"aktiv": False, "projekt": None, "mappe": None, "antal": 0, "niveau": "ren",
                        "filer": [], "mere": 0, "bad": False}

# By extension: the kind of file and the folders it belongs in (the first that exists wins).
VIDEO = frozenset("mov mp4 mxf braw r3d crm mts m2ts avi mkv m4v insv lrv mpg mpeg webm 3gp".split())
AUDIO = frozenset("wav mp3 aif aiff m4a flac bwf ogg aac".split())
IMAGE = frozenset("png jpg jpeg tif tiff gif webp heic bmp exr dpx tga".split())
GRAPHICS = frozenset("psd ai eps svg afdesign afphoto".split())
FONT = frozenset("ttf otf woff woff2".split())
TEXT = frozenset("txt doc docx rtf pdf pages odt srt vtt sbv xlsx xls csv md".split())
PROJECT = frozenset("drp drt dra drx prproj aep fcpxml xml edl aaf otio cube".split())

FINAL_HINTS = ("final", "export", "master", "levering")
SPEAK_HINTS = ("speak", "voice", "speaker")
SPEAK_WORDS = ("vo",)
SFX_HINTS = ("sfx", "effekt")
_WORDS = re.compile(r"[^0-9a-zæøå]+")

_STOP = object()


def _words(name: str) -> list[str]:
    stem = name.rsplit(".", 1)[0] if "." in name else name
    return [w for w in _WORDS.split(stem.casefold()) if w]


def _hinted(words: list[str], prefixes: tuple[str, ...], exact: tuple[str, ...] = ()) -> bool:
    return any(w.startswith(prefixes) or w in exact for w in words)


def classify(name: str) -> tuple[str, list[str]]:
    """``(slags, candidate folders)`` of a loose file by its extension and name ([]: no idea)."""
    ext = name.rsplit(".", 1)[1].casefold() if "." in name.strip(".") else ""
    words = _words(name)
    logo = "logo" in name.casefold()
    if ext in VIDEO:
        return "video", ["Final"] if _hinted(words, FINAL_HINTS) else ["Klip"]
    if ext in AUDIO:
        if _hinted(words, SPEAK_HINTS, SPEAK_WORDS):
            return "lyd", ["Speak"]
        if _hinted(words, SFX_HINTS):
            return "lyd", ["SFX", "Musik"]
        return "lyd", ["Musik", "Music"]
    if ext in IMAGE or ext in GRAPHICS:
        return ("billede" if ext in IMAGE else "grafik"), (["Logo"] if logo else ["Grafik", "Stills"])
    if ext in FONT:
        return "font", ["Font", "Grafik"]
    if ext in TEXT:
        return "tekst", ["Tekst"]
    if ext in PROJECT:
        return "projekt", ["Project", "Projekt"]
    return "andet", []


def suggest(name: str, folders: Collection[str]) -> dict[str, Any]:
    """``{navn, forslag, ny, slags}`` for one loose file: the first candidate folder the project
    has (with its own spelling), else the first candidate as a new one (``ny``)."""
    kind, candidates = classify(name)
    existing = {folder.casefold(): folder for folder in folders}
    for candidate in candidates:
        if candidate.casefold() in existing:
            return {"navn": name, "forslag": existing[candidate.casefold()], "ny": False, "slags": kind}
    return {"navn": name, "forslag": candidates[0] if candidates else None, "ny": bool(candidates), "slags": kind}


def level(count: int) -> str:
    if count <= 0:
        return "ren"
    if count <= 3:
        return "lidt"
    return "beskidt" if count <= 9 else "meget"


def ignored(name: str, attributes: int = 0) -> bool:
    folded = name.casefold()
    return levering.ignored(name, attributes) or folded.endswith(IGNORED_SUFFIXES) or folded in IGNORED_NAMES


def read_root(path: str, lister: Callable[[str], levering.Listing] = levering.list_dir,
              budget: int = MAX_ENTRIES) -> tuple[list[str], list[str]]:
    """``(loose files, folders)`` in the root of ``path`` (at most ``budget`` entries are read)."""
    entries, _complete = levering._read(lister, path, budget)
    files, folders = [], []
    for name, is_dir, _size, _mtime, attributes in entries:
        if ignored(name, attributes):
            continue
        (folders if is_dir else files).append(name)
    return sorted(files, key=str.casefold), folders


def mess(project: str, folder: str, files: list[str], folders: list[str]) -> dict[str, Any]:
    """The state for one look at a project folder."""
    shown = [suggest(name, folders) for name in files[:MAX_FILES_SHOWN]]
    return {"aktiv": True, "projekt": project, "mappe": folder, "antal": len(files), "niveau": level(len(files)),
            "filer": shown, "mere": max(0, len(files) - MAX_FILES_SHOWN), "bad": False}


def current_project(bridge: Any) -> tuple[str, str | None] | None:
    """(the current Resolve project's folder, the project's name) while Resolve is connected and
    the folder is online (as ``levering`` sees it)."""
    try:
        state = bridge.state()
    except Exception:
        return None
    if not isinstance(state, dict) or not state.get("connected"):
        return None
    primary = state.get("primary")
    if not isinstance(primary, dict):
        return None
    source = primary.get("source")
    online = source.get("online") if isinstance(source, dict) and "online" in source else primary.get("online")
    if online is False:
        return None
    path = primary.get("path")
    if not isinstance(path, str) or not path.strip():
        return None
    project = state.get("project")
    return path, project if isinstance(project, str) else None


def _timer(seconds: float, action: Callable[[], None]) -> Any:
    timer = threading.Timer(seconds, action)
    timer.name = "rod-demo"
    timer.daemon = True
    timer.start()
    return timer


DEMO_FILES = ([f"Klip_{n:02d}.mov" for n in range(1, 8)]
              + ["Interview_A.mxf", "Speak_final.wav", "Underlaegning.mp3", "Logo_hvid.png", "Kunde_brief.pdf"])


class Rod:
    """Looks at the current project folder's root and tells Klippe how messy it is."""

    def __init__(self, cfg: Any, bus: Any, *, bridge: Any, on_bath: Callable[[], None] | None = None,
                 lister: Callable[[str], levering.Listing] = levering.list_dir,
                 call_with_timeout: Callable[[str, Callable[[], Any], float], tuple[str, Any]] | None = None,
                 poll_s: float = POLL_S, today: Callable[[], date] = date.today,
                 timer: Callable[[float, Callable[[], None]], Any] = _timer) -> None:
        self.cfg = cfg
        self.bus = bus
        self.bridge = bridge
        self._on_bath = on_bath
        self._lister = lister
        self._call = call_with_timeout
        self._poll_s = poll_s
        self._today = today
        self._timer = timer
        self._lock = threading.Lock()
        self._state: dict[str, Any] = dict(IDLE)
        self._real: dict[str, Any] = dict(IDLE)            # the last real look (a demo hides it)
        self._counts: dict[str, int] = {}                  # casefolded folder → loose files last time
        self._baths: set[tuple[str, str]] = set()          # (folder, day) that had their bath
        self._demo: Any = None                             # the demo's timer while it runs
        self._queue: queue.Queue | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # -- lifecycle --------------------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._queue = self.bus.subscribe()
        if callable(getattr(self.cfg, "on_change", None)):
            self.cfg.on_change(lambda _snapshot: self._poke())
        self._thread = threading.Thread(target=self._run, name="rod", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            demo, self._demo = self._demo, None
        if demo is not None:
            demo.cancel()
        q = self._queue
        if q is not None:
            self.bus.unsubscribe(q)
            self._poke()

    def _poke(self) -> None:
        """Wake the thread (a setting changed, or the app exits): record and wake only."""
        q = self._queue
        if q is not None:
            try:
                q.put_nowait(("_wake", None, 0.0))
            except queue.Full:
                pass

    def _run(self) -> None:
        q = self._queue
        assert q is not None
        next_look = time.monotonic() + FIRST_S
        while not self._stop.is_set():
            try:
                kind, _data, _ts = q.get(timeout=max(0.0, next_look - time.monotonic()))
            except queue.Empty:
                kind = None
            if self._stop.is_set():
                break
            if kind in ("resolve", "_wake"):
                next_look = min(next_look, time.monotonic() + SOON_S)
            if time.monotonic() < next_look:
                continue
            try:
                self.look()
            except Exception:
                log.exception("looking at the project folder failed")
            next_look = time.monotonic() + self._poll_s

    # -- the API ----------------------------------------------------------------------------
    def state(self) -> dict[str, Any]:
        """``GET /api/rod``."""
        with self._lock:
            return _copy(self._state)

    def demo(self) -> dict[str, Any]:
        """``POST /api/rod/demo``: a made-up messy project (12 loose files), and 8 s later its bath."""
        if not self.cfg.get("widget_enabled", False):
            raise ValueError(MSG_OFF)
        state = {**mess("Demo", "", DEMO_FILES, ["Final", "Grafik", "Klip", "Logo", "Musik", "Project", "Speak",
                                                  "Tekst"]), "mappe": None, "demo": True}
        with self._lock:
            old, self._demo = self._demo, None
            self._state = _copy(state)
        if old is not None:
            old.cancel()
        self.bus.publish("rod", _copy(state))
        timer = self._timer(DEMO_BATH_S, self._demo_bath)
        with self._lock:
            self._demo = timer
        return {"ok": True, "rod": state}

    def _demo_bath(self) -> None:
        clean = {**mess("Demo", "", [], []), "mappe": None, "demo": True, "bad": True}
        with self._lock:
            self._demo = None
            real = _copy(self._real)
            self._state = _copy(real)
        self.bus.publish("rod", clean)
        if real != {**clean, "bad": False}:
            self.bus.publish("rod", _copy(real))

    # -- one look ---------------------------------------------------------------------------
    def _enabled(self) -> bool:
        return bool(self.cfg.get("widget_enabled", False) and self.cfg.get("widget_bad", True))

    def look(self) -> dict[str, Any] | None:
        """One look at the project folder's root; returns the event it published (or None)."""
        if not self._enabled():
            return self._show(dict(IDLE))
        found = current_project(self.bridge)
        if found is None:
            return self._show(dict(IDLE))
        path, _name = found
        call = self._call
        if call is None:
            from . import winfs
            call = winfs.call_with_timeout
        root = ntpath.splitdrive(path)[0] or path
        lister = self._lister
        status, result = call(f"rod:{root}", lambda: read_root(path, lister), LIST_TIMEOUT_S)
        if status != "ok" or result is None:
            log.debug("Klippe's bath: %s could not be read (%s)", path, status)
            return None                                    # a failed look keeps the last state
        files, folders = result
        project = ntpath.basename(path.rstrip("\\/")) or path
        state = mess(project, path, files, folders)
        key = path.casefold()
        with self._lock:
            before = self._counts.pop(key, None)
            self._counts[key] = len(files)
            while len(self._counts) > MEMORY_MAX:
                del self._counts[next(iter(self._counts))]
            day = self._today().isoformat()
            bath = bool(before) and not files and (key, day) not in self._baths
            if bath:
                self._baths = {b for b in self._baths if b[1] == day}
                self._baths.add((key, day))
        if bath:
            log.info("Klippe's bath: %s is tidy now", project)
            if self._on_bath is not None:
                try:
                    self._on_bath()
                except Exception:
                    log.exception("counting Klippe's bath failed")
        return self._show(state, bath=bath)

    def _show(self, state: dict[str, Any], *, bath: bool = False) -> dict[str, Any] | None:
        """Keep ``state`` as the real one and publish it when it is news (never during a demo)."""
        with self._lock:
            self._real = _copy(state)
            if self._demo is not None:
                return None
            if state == self._state and not bath:
                return None
            self._state = _copy(state)
        event = {**state, "bad": bath}
        self.bus.publish("rod", event)
        return event


def _copy(state: dict[str, Any]) -> dict[str, Any]:
    return {**state, "filer": [dict(f) for f in state.get("filer", [])]}
