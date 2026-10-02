"""Fake Projektsøg backend for developing and testing the web UI (``projektsog/web``).

Serves the real UI files plus an in-memory imitation of the HTTP API (SPEC §11), the JSON
shapes (§7.1) and the SSE events (§3.1). It has no index, no Windows APIs and no Resolve.

    python -m tests._ui_mock_server [--port 0] [--scenario first,newdisk]

Then open ``http://127.0.0.1:<port>/?q=lindholm``. Scenarios are comma-separated flags. They come
from ``?mock=…`` in the page URL (API and SSE requests carry it in their ``Referer``), else
from ``--scenario`` / the ``PROJEKTSOG_UI_MOCK`` environment variable:

    first               first indexing ("10 af 11 placeringer klar")
    idle                nothing is scanning
    no-projects         the index is empty (no recent projects, no search results)
    host-offline        MEDIESERVER does not answer (its shares are offline)
    newdisk             SSE announces a new disk 'ARKIV' that is not included
    asked               resolve_hotkey_asked is true, so no Resolve question card
    nofocus             SSE sends no 'focus' event
    slow                every API call takes 400 ms longer
    resolve-suggestion  Resolve primary found by name only ("Muligt match")
    resolve-offline     Resolve clips on the offline disk and on a host that does not answer
    resolve-error       Resolve runs but external scripting is off
    resolve-off         Resolve is not running
    resolve-disabled    Resolve integration switched off
    resolve-empty       "Untitled Project" without clips
    time-idle           time tracking pauses: no input for longer than the idle limit
    time-paused         time tracking pauses: Resolve is not in front
    time-away           in another program for 3 min: still counting (if back within 10 min)
    card                an FX9 camera card is in E: (import helper, MockImporter)
    card2               … and an A7S card in G: whose clips are imported already
    import-fail         an import stops half-way ("Kortet blev taget ud …")

Time tracking (/api/time…) is a real ``TimeTracker`` over an in-memory store with the fixed
segments of ``TIME_SEGMENTS`` (today, yesterday and 40 days ago); it records "Color" on Rikke
Lindholm unless ``resolve-off`` or a ``time-*`` flag says otherwise.

Without flags: Resolve is connected to "Rikke Lindholm - Testimonial" (media match),
2025Arkiv is being deep-scanned, and the disk '2024 Disk Sølv' is offline. The portable
disk 'T7 Shield' holds one folder that is itself a project (SPEC §15.3: the source's root is
the project, so its Item is synthesised with rel_path "" and id -source_id, like search.py).

Offline sources carry ``volume_present`` (§15.12): a local one whose disk is still mounted (C:
always is) or a share whose computer still answers is a folder that is gone ("Mappen findes ikke
længere"), e.g. after ``set-online`` takes source 1 on C: offline. ``DELETE /api/hosts`` answers
``{"ok": true, "forgotten": n}`` and refuses while an added folder lies on the computer; a share
with ``Source.mapped`` set (reached via a mapped drive) is kept.

Tests use :class:`MockServer`: ``server.backend.requests`` logs every ``/api/`` request and
``server.backend.calls`` the POST/DELETE ones. Test-only endpoints steer the SSE stream:
``POST /api/_mock/publish {"type", "data"}`` pushes any event, ``POST /api/_mock/drop-events``
ends all open streams (to exercise reconnects), and ``POST /api/_mock/set-online {"source_id",
"online", "path"?, "events"?}`` connects/disconnects a location (optionally at another drive
letter) and publishes what the Indexer and the bridge do (§15.1, §15.9): ``events`` "all"
(default: sources + index_updated + status + resolve), "sources" (only the ``sources`` event,
like a backend without §15.1) or "none".
"""

from __future__ import annotations

import argparse
import json
import ntpath
import os
import queue
import random
import re
import sys
import threading
import time
import traceback
import urllib.parse
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from projektsog import __version__, config, importer, textutil, timetrack
from projektsog.events import EventBus
from projektsog.server import content_disposition, time_export, time_range

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(REPO_ROOT, "projektsog", "web")
ASSETS_DIR = os.path.join(REPO_ROOT, "projektsog", "assets")

OWN_HOST = "STUDIO-PC"
# The computers in settings["hosts"] (config.DEFAULTS has none: a fresh install finds its own).
HOSTS = ("STUDIO-PC", "KLIPPER-PC", "MEDIESERVER", "GRAFIK-PC")
SYSTEM_SERIAL = "5C1D2E3F"   # volume serial of C: (the Windows volume)
DAY = 86400.0
MAX_BODY = 1024 * 1024
KIND_NAMES = {0: "file", 1: "dir", 2: "project", 3: "group", 4: "template", 5: "toplevel"}
KIND_FILTERS = {"all": None, "project": {2, 3, 5}, "dir": {1, 2, 3, 5}, "file": {0}}
TEMPLATE_DIRS = ("Final", "Grafik", "Klip", "Logo", "Musik", "Project", "Speak", "Tekst")
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/widget.html": ("widget.html", "text/html; charset=utf-8"),
    "/widget.js": ("widget.js", "text/javascript; charset=utf-8"),
    "/widget.css": ("widget.css", "text/css; charset=utf-8"),
}
ASSET_TYPES = {".png": "image/png", ".ico": "image/x-icon", ".svg": "image/svg+xml"}
HOTKEY_RE = re.compile(r"^((ctrl|alt|shift|win)\+)+(space|[a-z0-9]|f([1-9]|1[0-9]|2[0-4]))$")
HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,62}$")
RESOLVE_SCRIPTING_ERROR = ("Slå ekstern scripting til i DaVinci Resolve: Preferences ▸ System ▸ "
                           "General ▸ External scripting using = Local")


# --------------------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------------------

@dataclass
class Source:
    id: int
    kind: str                      # "local" | "share"
    host: str
    display_name: str
    path: str
    unc_path: str | None
    volume_label: str | None
    volume_serial: str | None
    fs: str
    volume_size: int | None
    hotplug: bool = False
    manual: bool = False
    online: bool = True
    mode: str = "auto"
    auto_include: int = 1
    auto_reason: str = "Projektmapper fundet"
    counts: tuple[int, int, int, int, int] = (0, 0, 0, 0, 0)  # entries, dirs, files, projects, size
    scan_age_s: float | None = 300.0        # seconds since the last scan ended (None = never)
    last_seen_age_s: float = 0.0
    last_error: str | None = None
    root_is_project: bool = False           # §15.3: the folder itself is a project
    mapped: bool = False                    # a share also reached via a mapped drive: remove_host keeps it

    @property
    def included(self) -> bool:
        return self.manual or self.mode == "include" or (self.mode == "auto" and self.auto_include == 1)

    @property
    def is_system(self) -> bool:
        """§15.4: a local source on the Windows volume (serial of C: in this mock)."""
        return self.kind == "local" and self.volume_serial == SYSTEM_SERIAL

    @property
    def key(self) -> str:
        if self.kind == "share":
            rest = self.path[2:]
            return "unc:" + rest
        rel = self.path[2:] or "\\"
        return f"vol:{self.volume_serial}:{rel}"


@dataclass
class Entry:
    id: int
    source_id: int
    rel_path: str
    name: str
    kind: int
    ext: str | None = None
    size: int | None = None
    mtime: float | None = None
    file_count: int | None = None
    is_seq: bool = False
    seq_count: int | None = None
    seq_first: str | None = None
    project_rel: str | None = None
    subfolders: list[str] = field(default_factory=list)
    name_fold: str = ""
    path_fold: str = ""

    @property
    def depth(self) -> int:
        return self.rel_path.count("\\") + 1 if self.rel_path else 0

    @property
    def parent_rel(self) -> str:
        return self.rel_path.rpartition("\\")[0]


def _join(base: str, rel: str) -> str:
    """ntpath.join without the trailing backslash for the root itself (like search._join)."""
    return ntpath.join(base, rel) if rel else base


def _gb(value: float) -> int:
    return int(value * 1024 ** 3)


def _make_sources() -> list[Source]:
    def local(i: int, path: str, label: str, serial: str, *, fs: str = "NTFS", size_tb: float,
              share: bool = True, **kw: Any) -> Source:
        name = ntpath.basename(path.rstrip("\\")) or f"{path[:2]} (uden navn)"
        unc = f"\\\\{OWN_HOST}\\{name}" if share else None
        return Source(i, "local", OWN_HOST, name, path, unc, label, serial, fs,
                      int(size_tb * 1024 ** 4), **kw)

    def remote(i: int, host: str, share: str, **kw: Any) -> Source:
        path = f"\\\\{host}\\{share}"
        return Source(i, "share", host, share, path, path, None, None, "NTFS", None, **kw)

    return [
        local(1, "C:\\Kunder 2026 (STUDIO)", "Windows", "5C1D2E3F", size_tb=1.8,
              auto_reason="24 projektmapper fundet", counts=(12_400, 1_500, 10_900, 24, _gb(412)),
              scan_age_s=95),
        local(2, "D:\\Forår 2026 RØD", "Forår 2026 RØD", "7A21C0DE", size_tb=8,
              auto_reason="31 projektmapper fundet", counts=(16_600, 1_700, 14_900, 31, _gb(2_870)),
              scan_age_s=140),
        local(3, "H:\\2024 Disk Sølv", "2024 Disk Sølv", "5E3A0B21", size_tb=4, hotplug=True,
              online=False, auto_reason="12 projektmapper fundet",
              counts=(39_600, 600, 39_000, 12, _gb(3_310)), scan_age_s=18 * DAY,
              last_seen_age_s=18 * DAY + 3_600),
        local(4, "Z:\\(Z) Kunder 2026 (STUDIO)", "Lokal disk 2", "3F00B1A2", size_tb=16,
              auto_reason="9 projektmapper fundet", counts=(66_500, 500, 66_000, 9, _gb(5_120)),
              scan_age_s=160),
        local(5, "F:\\Kunder 2026 ARKIV", "ARKIV", "1C2D3E4F", fs="exFAT", size_tb=2,
              hotplug=True, auto_reason="Mediefiler fundet", counts=(137, 54, 83, 2, _gb(96)),
              scan_age_s=70),
        remote(6, "KLIPPER-PC", "Rejsefilm", auto_reason="7 projektmapper fundet",
               counts=(17_150, 350, 16_800, 7, _gb(1_430)), scan_age_s=410),
        remote(7, "KLIPPER-PC", "Efterår 2023", auto_reason="14 projektmapper fundet",
               counts=(17_500, 600, 16_900, 14, _gb(1_980)), scan_age_s=520),
        remote(8, "MEDIESERVER", "2026Arkiv", auto_reason="5 projektmapper fundet",
               counts=(2_800, 200, 2_600, 5, _gb(640)), scan_age_s=600),
        remote(9, "MEDIESERVER", "2025Arkiv", auto_reason="38 projektmapper fundet",
               counts=(120_000, 8_000, 112_000, 38, _gb(16_000)), scan_age_s=None),
        remote(10, "GRAFIK-PC", "Forår 2026 (HDD)", auto_reason="6 projektmapper fundet",
               counts=(15_460, 260, 15_200, 6, _gb(1_210)), scan_age_s=300),
        remote(11, "GRAFIK-PC", "Kunder 2026 (Grafik)", auto_reason="17 projektmapper fundet",
               counts=(15_600, 1_100, 14_500, 17, _gb(1_650)), scan_age_s=240),
        remote(12, "MEDIESERVER", "Efterår 2021", auto_reason="11 projektmapper fundet",
               counts=(21_300, 900, 20_400, 11, _gb(2_200)), scan_age_s=900),
        remote(13, "GRAFIK-PC", "Økonomi", auto_include=0,
               auto_reason="Ingen projektmapper fundet", scan_age_s=None),
        local(14, "C:\\Github", "Windows", "5C1D2E3F", size_tb=1.8, share=False, auto_include=0,
              auto_reason="Ingen projektmapper fundet", scan_age_s=None),
        local(15, "E:\\Dækcentret Julefrokost 2026", "T7 Shield", "9B8A7C6D", size_tb=1, share=False,
              hotplug=True, root_is_project=True, auto_reason="Mappen er selv et projekt",
              counts=(22, 9, 13, 1, _gb(10.4)), scan_age_s=50),
    ]


class _TreeBuilder:
    """Builds the entries of one source; aggregates are computed by ``_finish_entries``."""

    def __init__(self, backend: "MockBackend", source_id: int, now: float) -> None:
        self.backend = backend
        self.source_id = source_id
        self.now = now
        self.rng = random.Random(source_id * 7919)

    def add(self, rel: str, kind: int, *, age_days: float = 30.0, size: int | None = None,
            name: str | None = None, **extra: Any) -> Entry:
        name = name or rel.rpartition("\\")[2]
        ext = None
        if kind == 0 and "." in name:
            ext = name.rpartition(".")[2].lower()
        mtime = self.now - age_days * DAY - self.rng.uniform(0, 0.6) * DAY
        entry = Entry(self.backend.next_id(), self.source_id, rel, name, kind, ext=ext,
                      size=size, mtime=mtime, **extra)
        self.backend.entries.append(entry)
        return entry

    def project(self, parent: str, name: str, age_days: float, *, camera: str = "A7S",
                first_clip: int = 1, clips: int = 6, root: bool = False) -> None:
        """A copy of the project template; ``root``: the source folder itself is the project."""
        rel = "" if root else f"{parent}\\{name}" if parent else name
        self.add(rel, 2, age_days=age_days, name=name)

        def at(*parts: str) -> str:
            return "\\".join(part for part in (rel, *parts) if part)

        for sub in TEMPLATE_DIRS:
            self.add(at(sub), 1, age_days=age_days + 3)
        r = self.rng
        self.add(at("Final", f"{name} v1.mp4"), 0, age_days=age_days + 1.5, size=_gb(r.uniform(0.4, 1.8)))
        self.add(at("Final", f"{name} v2.mp4"), 0, age_days=age_days, size=_gb(r.uniform(0.4, 1.8)))
        self.add(at("Grafik", "Titel.psd"), 0, age_days=age_days + 2, size=_gb(r.uniform(0.05, 0.3)))
        self.add(at("Grafik", "Lower third.png"), 0, age_days=age_days + 2, size=int(r.uniform(2e5, 2e6)))
        self.add(at("Klip", camera), 1, age_days=age_days + 4)
        for n in range(first_clip, first_clip + clips):
            clip = f"{camera}_{n:04d}.MXF" if camera == "FX9" else f"C{n:04d}.MP4"
            self.add(at("Klip", camera, clip), 0, age_days=age_days + 4, size=_gb(r.uniform(0.8, 4.2)))
        self.add(at("Logo", "Logo.png"), 0, age_days=age_days + 5, size=int(r.uniform(4e4, 4e5)))
        self.add(at("Musik", "Musik final.wav"), 0, age_days=age_days + 1, size=int(r.uniform(3e7, 9e7)))
        self.add(at("Speak", "Speak take 2.wav"), 0, age_days=age_days + 1, size=int(r.uniform(1e7, 4e7)))
        self.add(at("Project", f"{name}.drp"), 0, age_days=age_days, size=int(r.uniform(2e5, 3e6)))
        self.add(at("Tekst", "Manus.docx"), 0, age_days=age_days + 6, size=int(r.uniform(2e4, 9e4)))
        self.add(at("Tekst", "Undertekster.srt"), 0, age_days=age_days + 1, size=int(r.uniform(2e3, 9e3)))

    def group(self, name: str, age_days: float, projects: list[tuple[str, float]]) -> None:
        self.add(name, 3, age_days=age_days)
        for project_name, project_age in projects:
            self.project(name, project_name, project_age)


def _build_trees(backend: "MockBackend", now: float) -> None:
    def tree(source_id: int) -> _TreeBuilder:
        return _TreeBuilder(backend, source_id, now)

    c = tree(1)
    c.project("", "Rikke Lindholm", 2, camera="FX9", first_clip=7905, clips=12)
    c.add("Rikke Lindholm\\Project\\Rikke Lindholm - Testimonial.drp", 0, age_days=2, size=2_400_000)
    c.project("", "Dækcentret Årsmøde 2026", 5)
    c.project("", "Møbelhuset Kirkeby", 9)
    c.project("", "Vestervang Kommune - Sommer 2026", 12)
    c.project("", "Bøgely Jul 2025", 21)
    c.add("Sound Effects", 5, age_days=40)
    for sfx in ("Whoosh 01.wav", "Whoosh 02.wav", "Klik.wav", "Publikum jubel.wav"):
        c.add(f"Sound Effects\\{sfx}", 0, age_days=40, size=random.Random(sfx).randint(200_000, 9_000_000))
    c.add("Export Presets", 5, age_days=60)
    c.add("Export Presets\\YouTube 4K.xml", 0, age_days=60, size=4_200)
    c.add("1. KUNDENAVN", 4, age_days=200)
    for sub in TEMPLATE_DIRS:
        c.add(f"1. KUNDENAVN\\{sub}", 1, age_days=200)

    d = tree(2)
    d.project("", "Pixelbro", 3, camera="FX9", first_clip=6120, clips=8)
    d.project("", "Hotel Bøgelyhus", 14)
    d.project("", "Hjælpeforeningen Østerby - Kampagne", 30)
    d.group("Klar Tand 2026", 4, [("Klar Tand - Silkeborg C", 4), ("Klar Tand - Voxpop Silkeborg", 6),
                                   ("Klar Tand - Skive", 11)])

    h = tree(3)
    h.project("", "Pixelbro Radio", 150)
    h.project("", "Bøgely Jul 2024", 280)
    h.project("", "Fagmesse 2024", 330)

    z = tree(4)
    z.project("", "Filmdage Nørreby", 8)
    z.project("", "Pumpefabrikken - Onboarding", 16)

    f = tree(5)
    f.project("", "Spillekonsol - Unboxing", 1, clips=4)
    f.project("", "Gokart Event", 6, clips=4)

    rejsefilm = tree(6)
    rejsefilm.project("", "Rejsefilm - Sæson 2", 26)
    rejsefilm.project("", "Rejsefilm - Trailer", 45)

    efteraar = tree(7)
    efteraar.project("", "Bøgely Festival 2023", 370)
    efteraar.project("", "Julefrokost 2023", 290)

    tree(8).project("", "Solkraft Midt - Solceller", 7)

    pool = tree(9)
    pool.project("", "Bøgely Festival 2025", 60)
    pool.project("", "Tandlæge Lindholm 2025", 120)
    pool.add("Unreal Showreel 2025", 5, age_days=95)
    pool.add("Unreal Showreel 2025\\Render", 1, age_days=95)
    pool.add("Unreal Showreel 2025\\Render\\render_[0001-4500].exr", 0, age_days=95,
             size=4500 * 8_300_000, is_seq=True, seq_count=4500, seq_first="render_0001.exr")

    grafik_hdd = tree(10)
    grafik_hdd.project("", "Forårskoncert 2026", 13)

    grafik = tree(11)
    grafik.group("Klar Tand 2026", 3, [("Klar Tand - Silkeborg", 3), ("Klar Tand - Randers", 10)])
    grafik.project("", "Pixelbro Podcast", 19)

    tree(12).project("", "Bøgely Jul 2021", 1_010)

    # 'Mappen er selv et projekt': the real index has no entry for a source root, search
    # synthesises the project Item (§15.3) – here an entry with rel_path "" plays that part.
    tree(15).project("", "Dækcentret Julefrokost 2026", 4, clips=3, root=True)


def _finish_entries(entries: list[Entry]) -> None:
    """Fill folded names, subtree aggregates, project refs and subfolder lists."""
    by_key = {(e.source_id, e.rel_path): e for e in entries}
    for e in entries:
        e.name_fold = textutil.fold(e.name)
        e.path_fold = textutil.fold(e.parent_rel.replace("\\", " "))
        if e.kind:
            e.size, e.file_count = 0, 0
    for e in sorted(entries, key=lambda x: -x.depth):
        # depth-1 entries aggregate into a root project (rel_path ""), if the source has one
        parent = by_key.get((e.source_id, e.parent_rel)) if e.rel_path else None
        if parent is None:
            continue
        parent.size = (parent.size or 0) + (e.size or 0)
        parent.mtime = max(parent.mtime or 0.0, e.mtime or 0.0)
        parent.file_count = (parent.file_count or 0) + (
            (e.seq_count or 1) if e.kind == 0 else (e.file_count or 0))
        if e.kind:
            parent.subfolders.append(e.name)
    for e in entries:
        e.subfolders.sort(key=str.casefold)
        del e.subfolders[40:]
        rel = e.rel_path
        while rel:
            ancestor = by_key.get((e.source_id, rel))
            if ancestor is not None and ancestor.kind == 2:
                e.project_rel = rel
                break
            rel = rel.rpartition("\\")[0]
        else:
            root = by_key.get((e.source_id, ""))
            if root is not None and root.kind == 2:
                e.project_rel = ""


# --------------------------------------------------------------------------------------
# Backend (API logic)
# --------------------------------------------------------------------------------------

class MockBackend:
    def __init__(self, scenario: str = "") -> None:
        self.lock = threading.RLock()
        self.bus = EventBus()
        self.stopping = threading.Event()
        self.default_flags = _parse_flags(scenario)
        self.started = time.time()
        self._next_id = 100
        self.calls: list[dict[str, Any]] = []      # POST/DELETE requests
        self.requests: list[dict[str, Any]] = []   # every /api/ request (newest 1000)
        self.pet_state = "ready"
        self.errors: list[str] = []                # tracebacks of 500 responses
        self.sse_generation = 0                    # bumped by /api/_mock/drop-events
        self.settings: dict[str, Any] = config.validate({**config.DEFAULTS, "hosts": list(HOSTS)})
        self.run_at_login = True
        self.sources: dict[int, Source] = {s.id: s for s in _make_sources()}
        self.scan_requests: set[int] = set()
        self.entries: list[Entry] = []
        _build_trees(self, self.started)
        _finish_entries(self.entries)
        self.time = _make_time_tracker(self.settings)
        self.importer = MockImporter(self.bus)

    def next_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def record(self, method: str, path: str, query: dict[str, str], body: Any) -> None:
        entry = {"method": method, "path": path, "query": query, "body": body, "t": time.time()}
        with self.lock:
            self.requests.append(entry)
            del self.requests[:-1000]
            if method != "GET":
                self.calls.append(entry)

    # -- scenario-dependent views ------------------------------------------------------
    def online(self, src: Source, flags: frozenset[str]) -> bool:
        if "host-offline" in flags and src.host == "MEDIESERVER":
            return False
        return src.online

    def host_online(self, name: str, flags: frozenset[str]) -> bool:
        """The computer answers: one of its shares is online (a new one without shares does)."""
        if name == OWN_HOST:
            return True
        shares = [s for s in self.sources.values() if s.host == name and s.kind == "share"]
        return any(self.online(s, flags) for s in shares) or (not shares and name != "MEDIESERVER")

    def volume_present(self, src: Source, flags: frozenset[str]) -> bool:
        """§15.12: a local source's volume is mounted (C: always is; others while one of their
        folders is online), a share's computer answers. Offline but present = the folder is gone."""
        if self.online(src, flags):
            return True
        if src.kind == "share":
            return self.host_online(src.host, flags)
        mounted = {SYSTEM_SERIAL} | {s.volume_serial for s in self.sources.values()
                                     if s.kind == "local" and self.online(s, flags)}
        return src.volume_serial in mounted

    def scanning(self, flags: frozenset[str]) -> list[dict[str, Any]]:
        if "idle" in flags or 9 not in self.sources or not self.online(self.sources[9], flags):
            return []
        elapsed = time.time() - self.started
        entries = min(120_000 + int(elapsed * 850), 303_900)
        units_total = 53
        units_done = min(units_total - 1, 12 + int(elapsed / 9))
        return [{"source_id": 9, "name": "2025Arkiv", "kind": "deep", "entries": entries,
                 "dirs": entries // 15, "units_done": units_done, "units_total": units_total,
                 "started": self.started - 95.0, "full": True}]

    def source_ref(self, src: Source, flags: frozenset[str]) -> dict[str, Any]:
        return {"id": src.id, "name": src.display_name, "host": src.host, "kind": src.kind,
                "online": self.online(src, flags), "drive": _drive_of(src.path),
                "disk_name": _disk_name(src), "volume_label": src.volume_label,
                "last_seen": self.started - src.last_seen_age_s, "is_system": src.is_system,
                "volume_present": self.volume_present(src, flags)}

    def source_full(self, src: Source, flags: frozenset[str]) -> dict[str, Any]:
        scans = {s["source_id"]: s for s in self.scanning(flags)}
        entry_count, dir_count, file_count, project_count, total_size = src.counts
        if src.id in scans:
            entry_count = scans[src.id]["entries"]
            dir_count = scans[src.id]["dirs"]
            file_count = entry_count - dir_count
        online = self.online(src, flags)
        last_end = None if src.scan_age_s is None else self.started - src.scan_age_s
        return {
            "id": src.id, "key": src.key, "kind": src.kind, "display_name": src.display_name,
            "host": src.host, "path": src.path, "unc_path": src.unc_path,
            "volume_label": src.volume_label, "volume_serial": src.volume_serial, "fs": src.fs,
            "drive": _drive_of(src.path), "last_drive": _drive_of(src.path),
            "disk_name": _disk_name(src), "hotplug": src.hotplug, "volume_size": src.volume_size,
            "online": online, "mode": src.mode, "included": src.included,
            "auto_reason": src.auto_reason, "manual": src.manual, "entry_count": entry_count,
            "dir_count": dir_count, "file_count": file_count, "project_count": project_count,
            "total_size": total_size, "last_scan_end": last_end,
            "last_scan_ok": None if last_end is None else src.last_error is None,
            "last_error": src.last_error, "last_seen": self.started - src.last_seen_age_s,
            "scanning": src.id in scans, "scan_kind": scans[src.id]["kind"] if src.id in scans else None,
            "queued": src.id in self.scan_requests and src.id not in scans and online,
            "is_system": src.is_system, "root_is_project": src.root_is_project,
            # §15.12: shallow scans run more often than deep ones
            "last_shallow_scan": None if last_end is None else max(last_end, self.started - 60.0),
            "volume_present": self.volume_present(src, flags),
        }

    def item(self, e: Entry, flags: frozenset[str], tokens: list[str] | None = None,
             score: float | None = None) -> dict[str, Any]:
        src = self.sources[e.source_id]
        path = _join(src.path, e.rel_path)
        open_path = ntpath.join(ntpath.dirname(path), e.seq_first) if e.is_seq and e.seq_first else path
        project = None
        if e.project_rel is not None:  # "" = the source root is the project (§15.3)
            project_name = e.project_rel.rpartition("\\")[2] if e.project_rel else src.display_name
            project = {"name": project_name, "rel_path": e.project_rel,
                       "path": _join(src.path, e.project_rel),
                       "unc_path": _join(src.unc_path, e.project_rel) if src.unc_path else None}
        own_tokens = [t for t in tokens or [] if t in e.name_fold]
        return {
            "id": e.id if e.rel_path else -src.id, "kind": KIND_NAMES[e.kind], "name": e.name,
            "hl": textutil.highlight_ranges(e.name, own_tokens),
            "path": path, "open_path": open_path,
            "unc_path": _join(src.unc_path, e.rel_path) if src.unc_path else None,
            "rel_path": e.rel_path, "parent": e.parent_rel, "depth": e.depth,
            "source": self.source_ref(src, flags), "project": project,
            "size": e.size, "mtime": e.mtime, "file_count": e.file_count if e.kind else None,
            "ext": e.ext, "is_seq": e.is_seq, "seq_count": e.seq_count,
            "subfolders": list(e.subfolders) if e.kind else None, "score": score,
        }

    def entry_by_path(self, source_id: int, rel: str) -> Entry | None:
        rel_key = rel.casefold()
        return next((e for e in self.entries
                     if e.source_id == source_id and e.rel_path.casefold() == rel_key), None)

    # -- queries -----------------------------------------------------------------------
    def search(self, q: str, kind: str, online_param: str | None, source_param: str | None,
               limit: int, templates: bool, flags: frozenset[str]) -> dict[str, Any]:
        started = time.perf_counter()
        tokens = textutil.tokenize(q)
        if kind not in KIND_FILTERS:
            raise ValueError("Ukendt type – brug all, project, dir eller file")
        online_only = (not self.settings["show_offline"]) if online_param is None else online_param == "1"
        source_id = int(source_param) if source_param else None
        matches: list[tuple[Entry, list[str], Source]] = []
        with self.lock:
            for e in self.entries if tokens and "no-projects" not in flags else ():
                src = self.sources.get(e.source_id)
                if src is None or not src.included or (e.kind == 4 and not templates):
                    continue
                own = [t for t in tokens if t in e.name_fold]
                if not own:
                    continue
                context = f"{e.path_fold} {textutil.fold(src.display_name)} {textutil.fold(src.volume_label or '')}"
                if all(t in e.name_fold or t in context for t in tokens):
                    matches.append((e, own, src))

            def ok_kind(e: Entry) -> bool:
                allowed = KIND_FILTERS[kind]
                return allowed is None or e.kind in allowed

            def ok_online(src: Source) -> bool:
                return not online_only or self.online(src, flags)

            def ok_source(src: Source) -> bool:
                return source_id is None or src.id == source_id

            visible = [(e, own, src) for e, own, src in matches
                       if ok_kind(e) and ok_online(src) and ok_source(src)]
            scored = sorted(((self._score(e, own, src, tokens, q, flags), e) for e, own, src in visible),
                            key=lambda pair: (-pair[0], -(pair[1].mtime or 0)))
            results = [self.item(e, flags, tokens, score) for score, e in scored[:limit]]
            response: dict[str, Any] = {
                "query": q, "tokens": tokens, "took_ms": 0, "total": len(visible),
                "truncated": False, "results": results,
            }
            if not visible and (kind != "all" or online_only or source_id is not None):
                # §15.7: per filter what switching off only that filter shows; "any" = hidden at all
                response["hidden"] = {
                    "kind": sum(1 for e, _, src in matches if not ok_kind(e) and ok_online(src) and ok_source(src)),
                    "offline": sum(1 for e, _, src in matches if ok_kind(e) and not ok_online(src) and ok_source(src)),
                    "source": sum(1 for e, _, src in matches if ok_kind(e) and ok_online(src) and not ok_source(src)),
                    "any": len(matches),
                }
        response["took_ms"] = round((time.perf_counter() - started) * 1000, 1)
        return response

    def _score(self, e: Entry, own: list[str], src: Source, tokens: list[str], q: str,
               flags: frozenset[str]) -> float:
        score = {2: 1000, 3: 800, 5: 700, 1: 400, 4: 300, 0: 100}[e.kind]
        words = e.name_fold.split(" ")
        if len(own) == len(tokens):
            score += 300
        if e.name_fold == textutil.fold(q):
            score += 250
        if tokens and e.name_fold.startswith(tokens[0]):
            score += 100
        score += 40 * sum(1 for t in tokens if any(w.startswith(t) for w in words))
        score -= 120 * (len(tokens) - len(own))
        if self.online(src, flags):
            score += 200
        age = time.time() - (e.mtime or 0)
        score += 80 if age < 30 * DAY else 40 if age < 180 * DAY else 15 if age < 365 * DAY else 0
        return float(score - 4 * e.depth)

    def recent(self, limit: int, flags: frozenset[str]) -> list[dict[str, Any]]:
        if "no-projects" in flags:
            return []
        with self.lock:
            projects = [e for e in self.entries if e.kind == 2
                        and (src := self.sources.get(e.source_id)) is not None and src.included
                        and (self.settings["show_offline"] or self.online(src, flags))]
            projects.sort(key=lambda e: -(e.mtime or 0))
            return [self.item(e, flags) for e in projects[:limit]]

    def children(self, source_id: int, rel: str, flags: frozenset[str]) -> list[dict[str, Any]]:
        with self.lock:
            if source_id not in self.sources:
                raise ValueError("Placeringen findes ikke")
            kids = [e for e in self.entries if e.source_id == source_id and e.rel_path and e.parent_rel == rel]
            kids.sort(key=lambda e: (e.kind == 0, e.name.casefold()))
            return [self.item(e, flags) for e in kids]

    def status(self, flags: frozenset[str], *, full: bool = True) -> dict[str, Any]:
        with self.lock:
            sources = list(self.sources.values())
            included = [s for s in sources if s.included]
            online = [s for s in included if self.online(s, flags)]
            scanning = self.scanning(flags)
            ready = len(online) - (1 if "first" in flags else 0)
            status: dict[str, Any] = {
                "hostname": OWN_HOST, "version": __version__,
                "sources_total": len(sources), "sources_online": len(online),
                "sources_offline": len(included) - len(online),
                "sources_excluded": len(sources) - len(included),
                "sources_ready": max(0, ready), "sources_included_online": len(online),
                "entries": 523_401, "files": 510_000, "dirs": 13_401, "projects": 412,
                "scanning": scanning, "queued": 0 if "idle" in flags else 2,
                "last_scan_end": self.started - 40, "initial_scan_done": "first" not in flags,
                "worker": {"running": True, "restarts": 0}, "db_size": 196_000_000,
            }
            if full:
                status["resolve"] = self.resolve_state(flags)
                status["hotkey"] = self.hotkey_status()
            return status

    def hotkey_status(self) -> dict[str, Any]:
        spec = self.settings["hotkey"]
        enabled = self.settings["hotkey_enabled"]
        return {"spec": spec, "label": _hotkey_label(spec), "enabled": enabled,
                "active": enabled, "mode": "ll" if enabled else None}

    def settings_payload(self, flags: frozenset[str]) -> dict[str, Any]:
        with self.lock:
            snap = json.loads(json.dumps(self.settings))
            if "asked" in flags:
                snap["resolve_hotkey_asked"] = True
            snap["run_at_login"] = self.run_at_login
            return snap

    def sources_payload(self, flags: frozenset[str]) -> dict[str, Any]:
        with self.lock:
            sources = [self.source_full(s, flags) for s in self.sources.values()]
            hosts = []
            for name in dict.fromkeys([OWN_HOST, *self.settings["hosts"]]):
                shares = [s for s in self.sources.values() if s.host == name and s.kind == "share"]
                host_online = self.host_online(name, flags)
                hosts.append({"name": name, "online": host_online, "shares": len(shares),
                              "last_seen": self.started - (0 if host_online else 3 * DAY),
                              "self": name == OWN_HOST})
            return {"sources": sources, "hosts": hosts}

    # -- DaVinci Resolve ---------------------------------------------------------------
    def resolve_state(self, flags: frozenset[str]) -> dict[str, Any]:
        base = {"enabled": True, "running": True, "connected": False, "error": None,
                "project": None, "database": None, "clip_count": 0, "updated": None,
                "folders": [], "other_dirs": [], "suggestions": [], "primary": None,
                "offline_clips": 0, "offline_disks": []}
        if "resolve-disabled" in flags or not self.settings["resolve_enabled"]:
            return {**base, "enabled": False, "running": False}
        if "resolve-off" in flags:
            return {**base, "running": False}
        if "resolve-error" in flags:
            return {**base, "error": RESOLVE_SCRIPTING_ERROR}
        state = {**base, "connected": True, "database": "Kunder 2026 (Projektserver)",
                 "updated": time.time() - 40}
        if "resolve-empty" in flags:
            return {**state, "project": "Untitled Project"}
        if "resolve-suggestion" in flags:
            suggestions = _present(self._suggestion(2, "Pixelbro", 0.82, flags),
                                   self._suggestion(3, "Pixelbro Radio", 0.64, flags))
            primary = {**suggestions[0]["item"], "match": "name"} if suggestions else None
            return {**state, "project": "Pixelbro - Sommerkampagne", "clip_count": 0,
                    "suggestions": suggestions, "primary": primary}
        if "resolve-offline" in flags:
            folders = _present(self._folder(3, "Pixelbro Radio", 48, flags),
                               self._folder(8, "Solkraft Midt - Solceller", 12, {*flags, "host-offline"}))
            return _media_state(state, "Pixelbro Radio - Spot", 60, folders, [])
        folders = _present(self._folder(1, "Rikke Lindholm", 160, flags),
                           self._folder(3, "Pixelbro Radio", 6, flags))
        return _media_state(state, "Rikke Lindholm - Testimonial", 167, folders,
                            [{"path": "C:\\Github\\undertekster", "count": 1, "online": True}])

    def _folder(self, source_id: int, rel: str, count: int, flags: Any) -> dict[str, Any] | None:
        """map_paths()-style folder entry; None once the source has been forgotten."""
        flags = frozenset(flags)
        src = self.sources.get(source_id)
        entry = self.entry_by_path(source_id, rel) if src else None
        if src is None or entry is None:
            return None
        item = self.item(entry, flags)
        return {"project": item["project"], "source": self.source_ref(src, flags),
                "online": self.online(src, flags), "count": count, "item": item}

    def _suggestion(self, source_id: int, rel: str, score: float, flags: frozenset[str]) -> dict[str, Any] | None:
        folder = self._folder(source_id, rel, 0, flags)
        if folder is None:
            return None
        return {"project": folder["project"], "source": folder["source"],
                "online": folder["online"], "score": score, "item": folder["item"]}

    def open_path(self, path: str, action: str, flags: frozenset[str]) -> dict[str, Any]:
        if action not in ("folder", "reveal", "file"):
            raise ValueError("Ukendt handling")
        with self.lock:
            src = self._source_for_path(path)
            if src is not None and not self.online(src, flags):
                if self.volume_present(src, flags):  # §15.12: the disk/computer is there, the folder not
                    return {"ok": False, "error": "Mappen findes ikke længere"}
                if src.kind == "local":
                    return {"ok": False, "error": f"Tilslut disken ‘{_disk_name(src)}’"}
                return {"ok": False, "error": f"Computeren {src.host} svarer ikke – er den tændt?"}
        if "findes-ikke" in path.casefold():
            return {"ok": False, "error": "Findes ikke længere – indekset opdateres"}
        return {"ok": True, "path": path}

    def _source_for_path(self, path: str) -> Source | None:
        folded = path.casefold()
        best = None
        for src in self.sources.values():
            for root in filter(None, (src.path, src.unc_path)):
                r = root.casefold()
                if (folded == r or folded.startswith(r + "\\")) and (best is None or len(r) > best[0]):
                    best = (len(r), src)
        return best[1] if best else None

    def time_status(self, flags: frozenset[str]) -> dict[str, Any]:
        """TimeTracker.status(): recording Color on Rikke Lindholm (or what the flags say)."""
        today = date.today()
        status: dict[str, Any] = {
            "state": "recording", "enabled": bool(self.settings["time_tracking_enabled"]),
            "project": None, "bucket": None, "since": None,
            "today_s": round(self.time.report(today, today)["total_s"])}
        if not status["enabled"]:
            status["state"] = "off"
        elif "resolve-off" in flags:
            status["state"] = "no-resolve"
        elif "time-idle" in flags:
            status["state"] = "idle"
        elif "time-paused" in flags:
            status["state"] = "paused"
        elif "time-away" in flags:
            project, database, _uid, folder = _TIME_RIKKE
            since = time.time() - 3 * 60
            status.update(state="away", project=project, database=database, bucket="edit", bucket_label="Edit",
                          timeline="Testimonial v3", folder=folder, since=since - 1800, away_since=since,
                          away_until=since + 600)
        else:
            project, database, _uid, folder = _TIME_RIKKE
            status.update(project=project, database=database, bucket="color", bucket_label="Color",
                          timeline="Testimonial v3", folder=folder, since=time.time() - 25 * 60)
        return status

    # -- commands ----------------------------------------------------------------------
    def update_settings(self, changes: dict[str, Any]) -> None:
        if not isinstance(changes, dict):
            raise ValueError("Forventede et JSON-objekt")
        changes = dict(changes)
        run_at_login = changes.pop("run_at_login", None)
        if run_at_login is not None and not isinstance(run_at_login, bool):
            raise ValueError("run_at_login skal være sand/falsk")
        if "hotkey" in changes:
            spec = str(changes["hotkey"]).strip().lower().replace(" ", "")
            if not HOTKEY_RE.match(spec):
                raise ValueError("Ugyldig genvejstast – skriv fx shift+space eller ctrl+alt+p")
            changes["hotkey"] = spec
        clean = config.validate(changes)
        with self.lock:
            self.settings.update(clean)
            if run_at_login is not None:
                self.run_at_login = run_at_login
        self.publish_settings()
        if {"hotkey", "hotkey_enabled"} & clean.keys():
            self.bus.publish("hotkey", self.hotkey_status())
        if "resolve_enabled" in clean:
            self.bus.publish("resolve", self.resolve_state(self.default_flags))

    def publish_settings(self) -> None:
        self.bus.publish("settings", self.settings_payload(self.default_flags))

    # -- Klippe plays (petplay.py) ---------------------------------------------------------
    def pet_status(self) -> dict[str, Any]:
        with self.lock:
            return {"state": self.pet_state, "message": "", "enabled": self.settings["widget_play"]}

    def pet_play_now(self) -> dict[str, Any]:
        with self.lock:
            if not self.settings["widget_enabled"]:
                raise ValueError("Slå Klippe til først")
            self.pet_state = "waiting"
        status = self.pet_status()
        self.bus.publish("pet", status)
        return status

    def set_mode(self, source_id: int, mode: str, flags: frozenset[str]) -> dict[str, Any]:
        if mode not in ("auto", "include", "exclude"):
            raise ValueError("Ugyldig tilstand – brug auto, include eller exclude")
        with self.lock:
            src = self._source(source_id)
            src.mode = mode
            payload = self.source_full(src, flags)
        self.bus.publish("sources", {"changed": [source_id]})
        return payload

    def scan(self, source_id: int | None) -> None:
        with self.lock:
            ids = [self._source(source_id).id] if source_id is not None else list(self.sources)
            self.scan_requests.update(ids)
        self.bus.publish("sources", {"changed": ids})

    def forget(self, source_id: int, flags: frozenset[str]) -> None:
        with self.lock:
            src = self._source(source_id)
            if self.online(src, flags):
                raise ValueError("Kun offline placeringer kan glemmes")
            del self.sources[source_id]
            self.entries = [e for e in self.entries if e.source_id != source_id]
        self.bus.publish("sources", {"changed": [source_id]})

    def add_root(self, path: str, flags: frozenset[str]) -> dict[str, Any]:
        path = str(path or "").strip()
        if len(path) > 3:
            path = path.rstrip("\\")  # keep "D:\" for a whole drive
        if not re.match(r"^([A-Za-z]:\\.*|\\\\[^\\]+\\[^\\]+.*)$", path):
            raise ValueError("Skriv en fuld sti, fx D:\\Projekter eller \\\\GRAFIK-PC\\Arkiv")
        with self.lock:
            roots = list(self.settings["extra_roots"])
            if any(r.casefold() == path.casefold() for r in roots):
                raise ValueError("Mappen er allerede tilføjet")
            self.settings["extra_roots"] = roots + [path]
            is_unc = path.startswith("\\\\")
            host = path[2:].split("\\")[0].upper() if is_unc else OWN_HOST
            name = [part for part in path.split("\\") if part][-1]  # ntpath.basename('\\\\H\\share') is ''
            src = Source(max(self.sources) + 1, "share" if is_unc else "local", host,
                         name, path, path if is_unc else None, None if is_unc else "Windows",
                         None if is_unc else "5C1D2E3F", "NTFS", None, manual=True,
                         auto_reason="Tilføjet manuelt", scan_age_s=None)
            self.sources[src.id] = src
            self.scan_requests.add(src.id)
            payload = self.source_full(src, flags)
        self.publish_settings()
        self.bus.publish("sources", {"changed": [src.id]})
        return {"ok": True, "source": payload}

    def remove_root(self, path: str) -> None:
        path = str(path or "").strip()
        with self.lock:
            roots = self.settings["extra_roots"]
            match = next((r for r in roots if r.casefold() == path.casefold()), None)
            if match is None:
                raise ValueError("Mappen er ikke tilføjet manuelt")
            self.settings["extra_roots"] = [r for r in roots if r is not match]
            gone = [s.id for s in self.sources.values() if s.manual and s.path.casefold() == path.casefold()]
            for source_id in gone:
                del self.sources[source_id]
        self.publish_settings()
        self.bus.publish("sources", {"changed": gone})

    def add_host(self, name: str) -> None:
        name = str(name or "").strip().lstrip("\\").upper()
        if not HOST_RE.match(name):
            raise ValueError("Ugyldigt computernavn")
        with self.lock:
            if name in self.settings["hosts"]:
                raise ValueError("Computeren er allerede tilføjet")
            self.settings["hosts"] = self.settings["hosts"] + [name]
        self.publish_settings()
        self.bus.publish("sources", {"changed": []})

    def remove_host(self, name: str) -> dict[str, Any]:
        """§15.8/§15.12: the host's shares are forgotten with it, except manual ones and shares
        also reached via a mapped drive; refused (nothing changes) while an added folder lies on it."""
        name = str(name or "").strip().upper()
        with self.lock:
            if name not in self.settings["hosts"]:
                raise ValueError("Computeren findes ikke på listen")
            prefix = f"\\\\{name}\\".casefold()
            root = next((r for r in self.settings["extra_roots"] if r.casefold().startswith(prefix)), None)
            if root is not None:
                raise ValueError(f"Mappen ‘{root}’ ligger på {name} – fjern den først")
            self.settings["hosts"] = [h for h in self.settings["hosts"] if h != name]
            gone = [s.id for s in self.sources.values()
                    if s.kind == "share" and s.host == name and not s.manual and not s.mapped]
            for source_id in gone:
                del self.sources[source_id]
            self.entries = [e for e in self.entries if e.source_id not in gone]
        self.publish_settings()
        self.bus.publish("sources", {"changed": gone})
        return {"ok": True, "forgotten": len(gone)}

    def set_online(self, source_id: int, online: bool, path: str | None = None, events: str = "all") -> None:
        """Test control: a disk is plugged in/out (``path``: at another drive letter) or a host
        answers again. Publishes what the Indexer publishes for that (§15.1)."""
        if events not in ("all", "sources", "none"):
            raise ValueError("events: all, sources eller none")
        with self.lock:
            src = self._source(source_id)
            src.online = bool(online)
            if online:
                src.last_seen_age_s = self.started - time.time()  # last seen: now
            if path:
                src.path = str(path)
        if events != "none":
            self.bus.publish("sources", {"changed": [source_id]})
        if events == "all":
            self.bus.publish("index_updated", {"source_id": source_id})
            self.bus.publish("status", self.status(self.default_flags, full=False))
            # §15.9: the bridge re-derives online state from the registry and republishes.
            self.bus.publish("resolve", self.resolve_state(self.default_flags))

    def _source(self, source_id: int | None) -> Source:
        src = self.sources.get(source_id) if source_id is not None else None
        if src is None:
            raise ValueError("Placeringen findes ikke")
        return src


def _present(*items: Any) -> list[Any]:
    return [item for item in items if item is not None]


def _media_state(state: dict[str, Any], project: str, clip_count: int, folders: list[dict[str, Any]],
                 other_dirs: list[dict[str, Any]]) -> dict[str, Any]:
    """ResolveBridge.state() for a project whose media was mapped to folders (count desc)."""
    offline = [f for f in folders if not f["online"]]
    # a folder gone from a disk that is there (§15.12) is no disk to connect
    disks = sorted({f["source"]["disk_name"] or f["source"]["host"] for f in offline
                    if not f["source"]["volume_present"]})
    return {**state, "project": project, "clip_count": clip_count, "folders": folders,
            "other_dirs": other_dirs,
            "primary": {**folders[0]["item"], "match": "media"} if folders else None,
            "offline_clips": sum(f["count"] for f in offline), "offline_disks": disks}


def _drive_of(path: str) -> str | None:
    return path[:2].upper() if re.match(r"^[A-Za-z]:", path) else None


def _disk_name(src: Source) -> str | None:
    if src.kind != "local":
        return None
    if src.volume_label:
        return src.volume_label
    size = f"{(src.volume_size or 0) / 1024 ** 4:.1f} TB".replace(".", ",")
    return f"disk uden navn ({size}, sidst som {_drive_of(src.path)})"


def _hotkey_label(spec: str) -> str:
    names = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "win": "Win", "space": "Mellemrum"}
    return "+".join(names.get(part, part.upper()) for part in spec.split("+"))


def _parse_flags(text: str | None) -> frozenset[str]:
    return frozenset(f.strip().lower() for f in (text or "").split(",") if f.strip())


# (project, database, uid, folder) of the fixture time segments
_TIME_RIKKE = ("Rikke Lindholm - Testimonial", "Kunder 2026 (Projektserver)", "u-rikke", "Rikke Lindholm")
_TIME_KLAR = ("Klar Tand - Skive", "Kunder 2026 (Projektserver)", "u-klar", "Klar Tand - Skive")
_TIME_VEST = ("Vestervang Kommune - Sommer 2026", "Kunder 2026 (Projektserver)", "u-vest",
              "Vestervang Kommune - Sommer 2026")
_TIME_SOL = ("Solkraft Midt - Solceller", "Kunder 2025", "u-sol", None)
# (project, page, timeline, days before today, from, to) in local time. Today: Rikke 2:47
# (Testimonial v3 2:22, Teaser 0:25), Klar Tand 0:45.
TIME_SEGMENTS = (
    (_TIME_RIKKE, "edit", "Testimonial v3", 0, "08:30", "10:00"),
    (_TIME_RIKKE, "color", "Testimonial v3", 0, "10:00", "10:40"),
    (_TIME_RIKKE, "musik", "Testimonial v3", 0, "10:40", "10:52"),
    (_TIME_KLAR, "edit", "Skive 30 sek", 0, "11:00", "11:45"),
    (_TIME_RIKKE, "fusion", "Teaser", 0, "13:00", "13:25"),
    (_TIME_VEST, "edit", "Sommer 60 sek", 1, "09:00", "12:00"),
    (_TIME_VEST, "color", "Sommer 60 sek", 1, "12:30", "14:00"),
    (_TIME_RIKKE, "edit", "Testimonial v2", 1, "14:00", "15:00"),
    (_TIME_SOL, "edit", "", 40, "09:00", "11:00"),
    (_TIME_SOL, "deliver", "", 40, "11:00", "11:30"),
)


GIB = 1024 ** 3
_IMPORT_ROOT = "C:\\Kunder 2026 (STUDIO)"
_IMPORT_RIKKE = _IMPORT_ROOT + "\\Rikke Lindholm"
_IMPORT_VEST = _IMPORT_ROOT + "\\Vestervang Kommune - Sommer 2026"
# Disks with a "1. KUNDENAVN" template: C: is too full for the FX9 card (72 GiB), like on the real PC.
_IMPORT_DISKS = (
    {"path": _IMPORT_ROOT, "template": _IMPORT_ROOT + "\\1. KUNDENAVN", "name": "Kunder 2026 (STUDIO)",
     "host": "STUDIO-PC", "kind": "local", "disk": "C:", "online": True, "free": 70 * GIB},
    {"path": "F:\\Kunder 2026 ARKIV", "template": "F:\\Kunder 2026 ARKIV\\1. KUNDENAVN",
     "name": "Kunder 2026 ARKIV", "host": "STUDIO-PC", "kind": "local", "disk": "ARKIV", "online": True,
     "free": 7200 * GIB},
    {"path": "\\\\GRAFIK-PC\\Kunder 2026 (Grafik)", "template": "\\\\GRAFIK-PC\\Kunder 2026 (Grafik)\\1. KUNDENAVN",
     "name": "Kunder 2026 (Grafik)", "host": "GRAFIK-PC", "kind": "share", "disk": "GRAFIK-PC", "online": True,
     "free": 1100 * GIB},
)


class MockImporter:
    """In-memory imitation of importer.Importer (SPEC §17): it never touches a real disk.

    Flags: ``card`` puts an FX9 card in E:, ``card2`` also an A7S card in G: (its clips are
    in Rikke Lindholm already); ``import-fail`` stops a copy half-way ("Kortet blev taget ud …")."""

    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self.lock = threading.Lock()
        self.job: dict[str, Any] | None = None
        self.history: list[dict[str, Any]] = []
        self.created: list[str] = []
        self.cancelled = threading.Event()

    @staticmethod
    def _cards(flags: frozenset[str]) -> list[dict[str, Any]]:
        yesterday = datetime.combine(date.today() - timedelta(days=1), datetime.min.time()).timestamp()
        cards = []
        if "card" in flags or "card2" in flags:
            cards.append({"id": "7E3A91C4@E:", "drive": "E:", "serial": "7E3A91C4", "label": "",
                          "volume_size": 128 * 10 ** 9, "kinds": ["xdcam"], "model": "PXW-FX9V",
                          "camera": "FX9", "folder": "E:\\XDROOT\\Clip", "files": 297, "clips": 99,
                          "stills": 0, "bytes": 77_700_000_000, "first": yesterday + 21 * 3600 + 41 * 60,
                          "last": yesterday + 23 * 3600 + 11 * 60,
                          "found": {"clips": 0, "files": 0, "total": 297, "complete": False, "projects": []},
                          "inserted": time.time(), "dismissed": False})
        if "card2" in flags:
            cards.append({"id": "1A2B3C4D@G:", "drive": "G:", "serial": "1A2B3C4D", "label": "",
                          "volume_size": 64 * 10 ** 9, "kinds": ["m4root", "stills"], "model": "ILCE-7SM3",
                          "camera": "A7S", "folder": "G:\\PRIVATE\\M4ROOT\\CLIP", "files": 60, "clips": 24,
                          "stills": 12, "bytes": 21_000_000_000, "first": yesterday + 10 * 3600,
                          "last": yesterday + 15 * 3600,
                          "found": {"clips": 24, "files": 60, "total": 60, "complete": True, "projects": [
                              {"name": "Rikke Lindholm", "path": _IMPORT_RIKKE, "folder": _IMPORT_RIKKE + "\\Klip\\A7S",
                               "clips": 24, "files": 60, "complete": True, "online": True}]},
                          "inserted": time.time(), "dismissed": False})
        return cards

    def card(self, card_id: str, flags: frozenset[str]) -> dict[str, Any]:
        for card in self._cards(flags):
            if card["id"] == card_id:
                return card
        raise ValueError("Kortet er ikke sat i længere")

    def state(self, flags: frozenset[str]) -> dict[str, Any]:
        with self.lock:
            return {"cards": self._cards(flags), "job": dict(self.job) if self.job else None,
                    "history": list(reversed(self.history))[:8]}

    def options(self, card_id: str, flags: frozenset[str]) -> dict[str, Any]:
        card = self.card(card_id, flags)
        suggestions = [{"path": p["path"], "name": p["name"],
                        "reason": "Alle kortets filer ligger her" if p["complete"] else f"{p['clips']} af kortets klip ligger her",
                        "online": True, "free": 70 * GIB} for p in card["found"]["projects"]]
        if not suggestions:
            suggestions.append({"path": _IMPORT_RIKKE, "name": "Rikke Lindholm", "reason": "Åben i DaVinci Resolve",
                                "online": True, "free": 70 * GIB})
        suggestions.append({"path": _IMPORT_VEST, "name": "Vestervang Kommune - Sommer 2026",
                            "reason": "Arbejdet på i dag", "online": True, "free": 70 * GIB})
        for path in self.created:
            suggestions.append({"path": path, "name": path.rpartition("\\")[2], "reason": "Oprettet i dag",
                                "online": True, "free": 7200 * GIB})
        disks = [{**d, "fits": d["free"] >= card["bytes"] + 512 * 1024 ** 2} for d in _IMPORT_DISKS]
        return {"card": card, "suggestions": suggestions, "disks": disks}

    def plan(self, card_id: str, project: str, separate: bool, flags: frozenset[str]) -> dict[str, Any]:
        card = self.card(card_id, flags)
        project = importer._clean_dir(project)
        disk = next((d for d in _IMPORT_DISKS if project.casefold().startswith(d["path"].casefold() + "\\")), None)
        exists = project in (_IMPORT_RIKKE, _IMPORT_VEST) or project in self.created
        others = 12 if project == _IMPORT_RIKKE and card["camera"] == "FX9" else 0
        already = card["files"] if card["found"]["clips"] and project == _IMPORT_RIKKE else 0
        target = f"{project}\\Klip\\{card['camera']}"
        day_folder = None
        if separate:
            target, day_folder = f"{target} Dag 2", f"{card['camera']} Dag 2"
            already = 0
        new_bytes = 0 if already else card["bytes"]
        free = disk["free"] if disk else 70 * GIB
        return {"card": card_id, "project": project, "project_exists": exists, "target": target,
                "target_exists": exists and not day_folder, "camera": card["camera"], "day_folder": day_folder,
                "separate": bool(day_folder), "files": card["files"], "new_files": card["files"] - already,
                "new_bytes": new_bytes, "already": already, "conflicts": 0, "other_media": others,
                "suggest_separate": others > 0, "free": free, "fits": free >= new_bytes + 512 * 1024 ** 2}

    def create_project(self, root: str, name: str) -> dict[str, Any]:
        name = importer.validate_project_name(name)
        if not any(d["path"] == root for d in _IMPORT_DISKS):
            raise ValueError("Vælg en af diskene på listen")
        path = f"{root}\\{name}"
        if path in self.created or path in (_IMPORT_RIKKE, _IMPORT_VEST):
            raise ValueError("Der findes allerede en mappe med det navn")
        self.created.append(path)
        return {"path": path, "name": path.rpartition("\\")[2]}

    def start(self, card_id: str, project: str, separate: bool, mode: str,
              flags: frozenset[str]) -> dict[str, Any]:
        card = self.card(card_id, flags)
        plan = self.plan(card_id, project, separate, flags)
        if mode == "prepare":
            with self.lock:
                self.history.append({"at": time.time(), "mode": "prepare", "camera": card["camera"],
                                     "project": plan["project"], "target": plan["target"], "files": 0, "bytes": 0})
            return {"ok": True, "target": plan["target"]}
        move = mode == "move"
        files = plan["new_files"] + (plan["already"] if move else 0)
        if not files:
            raise ValueError("Alle klip ligger der allerede")
        if plan["new_files"] and not plan["fits"]:
            raise ValueError(f"Der er ikke plads nok på disken: kortet fylder {importer._gb(plan['new_bytes'])}, "
                             f"der er {importer._gb(plan['free'])} fri")
        with self.lock:
            if self.job and self.job["state"] in ("copying", "verifying", "deleting"):
                raise ValueError(importer.MSG_BUSY)
            self.cancelled.clear()
            self.job = {"id": f"{card_id}-1", "card": card_id, "drive": card["drive"], "camera": card["camera"],
                        "target": plan["target"], "project": plan["project"],
                        "project_name": plan["project"].rpartition("\\")[2], "mode": "move" if move else "copy",
                        "state": "copying", "phase": "copy", "current": "FX9_9066.MXF", "files_total": files,
                        "files_done": 0, "bytes_total": plan["new_bytes"] if not move else card["bytes"],
                        "copied": 0, "verified": 0, "deleted": 0, "kept": 0, "speed": 0.0,
                        "eta_s": None, "started": time.time(), "finished": None, "error": None}
            job = dict(self.job)
        threading.Thread(target=self._run, args=("import-fail" in flags, move), daemon=True).start()
        return job

    def _run(self, fail: bool, move: bool = False) -> None:
        steps = 12
        for step in range(1, steps + 1):
            if self.cancelled.wait(0.12):
                self._finish("cancelled")
                return
            with self.lock:
                job = self.job
                assert job is not None
                part = step / steps
                job.update(state="verifying" if step % 2 else "copying", copied=int(job["bytes_total"] * part),
                           verified=int(job["bytes_total"] * max(0.0, part - 1 / steps)),
                           files_done=int(job["files_total"] * max(0.0, part - 1 / steps)),
                           current=f"FX9_{9066 + step:04d}.MXF", speed=420 * 1024 ** 2, eta_s=(steps - step) * 9)
                snapshot = dict(job)
            self.bus.publish("import", snapshot)
            if fail and step == steps // 2:
                self._finish("failed", importer.MSG_CARD_GONE)
                return
        with self.lock:
            assert self.job is not None
            self.job.update(copied=self.job["bytes_total"], verified=self.job["bytes_total"],
                            files_done=self.job["files_total"])
        if move:     # like the real job: the card is emptied only after everything is verified
            for done in (self.job["files_total"] // 2, self.job["files_total"]):
                if self.cancelled.wait(0.15):
                    self._finish("cancelled")
                    return
                with self.lock:
                    self.job.update(state="deleting", phase="delete", deleted=done, current="FX9_9100.MXF")
                    snapshot = dict(self.job)
                self.bus.publish("import", snapshot)
        self._finish("done")

    def _finish(self, state: str, error: str | None = None) -> None:
        with self.lock:
            assert self.job is not None
            self.job.update(state=state, error=error, current=None, finished=time.time(), eta_s=None)
            snapshot = dict(self.job)
            if state == "done":
                self.history.append({"at": time.time(), "mode": snapshot["mode"], "camera": snapshot["camera"],
                                     "project": snapshot["project"], "target": snapshot["target"],
                                     "files": snapshot["files_done"], "bytes": snapshot["bytes_total"]})
        self.bus.publish("import", snapshot)


def _make_time_tracker(settings: dict[str, Any]) -> timetrack.TimeTracker:
    """A real TimeTracker (never started) over an in-memory store holding TIME_SEGMENTS."""
    store = timetrack.TimeStore(":memory:")
    today = date.today()

    def at(days_ago: int, hhmm: str) -> float:
        day = today - timedelta(days=days_ago)
        hours, minutes = (int(part) for part in hhmm.split(":"))
        return datetime(day.year, day.month, day.day, hours, minutes).timestamp()

    for (project, database, uid, folder), page, timeline, days_ago, start, end in TIME_SEGMENTS:
        store.insert((project, database, uid, page, timeline), folder, at(days_ago, start), at(days_ago, end),
                     OWN_HOST)
    return timetrack.TimeTracker(settings, None, store=store)


# --------------------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------------------

class MockHandler(BaseHTTPRequestHandler):
    server: "_HTTPServer"
    protocol_version = "HTTP/1.1"
    timeout = 60

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        """Silence the stock stderr logging (pythonw has no stderr)."""

    # -- plumbing ----------------------------------------------------------------------
    @property
    def backend(self) -> MockBackend:
        return self.server.backend

    def _flags(self) -> frozenset[str]:
        own = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("mock")
        referer = self.headers.get("Referer") or ""
        ref = urllib.parse.parse_qs(urllib.parse.urlsplit(referer).query).get("mock")
        chosen = own or ref
        return _parse_flags(chosen[0]) if chosen else self.backend.default_flags

    def _host_ok(self) -> bool:
        port = self.server.server_address[1]
        return (self.headers.get("Host") or "") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _send(self, status: int, body: bytes, content_type: str,
              headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: Any, status: int = 200) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _body(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ValueError("For stor forespørgsel")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Ugyldig JSON") from exc

    def _guarded(self, fn: Any) -> None:
        if not self._host_ok():
            self._json({"error": "Forkert værtsnavn"}, 403)
            return
        if self.command != "GET" and self.headers.get("X-Projektsog") != "1":
            self._json({"error": "Mangler X-Projektsog-header"}, 403)
            return
        try:
            fn()
        except ValueError as exc:
            self._json({"error": str(exc)}, 400)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception:  # like the real server (§11): 500 + log, never a traceback on stderr
            with self.backend.lock:
                self.backend.errors.append(traceback.format_exc())
            self._json({"error": "Intern fejl – se loggen"}, 500)

    def _delay(self, flags: frozenset[str], seconds: float = 0.0) -> None:
        extra = 0.4 if "slow" in flags else 0.0
        if seconds + extra:
            time.sleep(seconds + extra)

    # -- verbs -------------------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        self._guarded(self._get)

    def do_POST(self) -> None:  # noqa: N802
        self._guarded(lambda: self._modify("POST"))

    def do_DELETE(self) -> None:  # noqa: N802
        self._guarded(lambda: self._modify("DELETE"))

    def _get(self) -> None:
        parts = urllib.parse.urlsplit(self.path)
        route = parts.path
        query = {k: v[-1] for k, v in urllib.parse.parse_qs(parts.query).items()}
        flags = self._flags()
        backend = self.backend
        if route.startswith("/api/"):
            backend.record("GET", route, query, None)
        if route in STATIC_FILES:
            name, content_type = STATIC_FILES[route]
            self._static(os.path.join(WEB_DIR, name), content_type)
        elif route.startswith("/assets/"):
            name = route[len("/assets/"):]
            ext = os.path.splitext(name)[1].lower()
            if "/" in name or "\\" in name or ext not in ASSET_TYPES:
                self._json({"error": "Findes ikke"}, 404)
            else:
                self._static(os.path.join(ASSETS_DIR, name), ASSET_TYPES[ext])
        elif route == "/api/search":
            self._delay(flags)
            limit = int(query.get("limit") or backend.settings["result_limit"])
            self._json(backend.search(query.get("q", ""), query.get("kind", "all"), query.get("online"),
                                      query.get("source"), min(limit, 500),
                                      query.get("templates") == "1", flags))
        elif route == "/api/recent":
            self._delay(flags)
            self._json({"results": backend.recent(int(query.get("limit") or 30), flags)})
        elif route == "/api/children":
            self._json({"results": backend.children(int(query.get("source") or 0),
                                                    query.get("rel", ""), flags)})
        elif route == "/api/status":
            self._json(backend.status(flags))
        elif route == "/api/sources":
            self._json(backend.sources_payload(flags))
        elif route == "/api/resolve":
            self._json(backend.resolve_state(flags))
        elif route == "/api/settings":
            self._json({"settings": backend.settings_payload(flags)})
        elif route == "/api/time":
            self._delay(flags)
            first, last = time_range(query)
            self._json({"report": backend.time.report(first, last), "status": backend.time_status(flags)})
        elif route == "/api/time/status":
            self._json(backend.time_status(flags))
        elif route == "/api/time/export":
            result = time_export(backend.time, query)
            self._send(200, result.data, result.content_type,
                       {"Content-Disposition": content_disposition(result.filename)})
        elif route == "/api/widget/play":
            self._json(backend.pet_status())
        elif route == "/api/messages":
            self._json({"messages": []})
        elif route == "/api/import":
            self._json(backend.importer.state(flags))
        elif route == "/api/import/options":
            self._json(backend.importer.options(query.get("card", ""), flags))
        elif route == "/api/import/plan":
            self._json(backend.importer.plan(query.get("card", ""), query.get("project", ""),
                                             query.get("separate") in ("1", "true"), flags))
        elif route == "/api/events":
            self._events(flags)
        else:
            self._json({"error": "Findes ikke"}, 404)

    def _modify(self, method: str) -> None:
        route = urllib.parse.urlsplit(self.path).path
        body = self._body()
        flags = self._flags()
        backend = self.backend
        backend.record(method, route, {}, body)
        m = re.fullmatch(r"/api/sources/(\d+)/(mode|scan|forget)", route)
        if method == "POST" and m:
            source_id, action = int(m.group(1)), m.group(2)
            if action == "mode":
                self._json(backend.set_mode(source_id, str(body.get("mode", "")), flags))
            elif action == "scan":
                backend.scan(source_id)
                self._json({"ok": True})
            else:
                backend.forget(source_id, flags)
                self._json({"ok": True})
        elif (method, route) == ("POST", "/api/scan"):
            backend.scan(None)
            self._json({"ok": True})
        elif route == "/api/roots":
            if method == "POST":
                self._json(backend.add_root(body.get("path", ""), flags))
            else:
                backend.remove_root(body.get("path", ""))
                self._json({"ok": True})
        elif route == "/api/hosts":
            if method == "POST":
                backend.add_host(body.get("name", ""))
                self._json({"ok": True})
            else:
                self._json(backend.remove_host(body.get("name", "")))
        elif (method, route) == ("POST", "/api/open"):
            self._delay(flags, 0.12)
            self._json(backend.open_path(str(body.get("path", "")), str(body.get("action", "")), flags))
        elif (method, route) == ("POST", "/api/resolve/refresh"):
            self._delay(flags, 0.6)
            state = backend.resolve_state(flags)
            backend.bus.publish("resolve", state)
            self._json(state)
        elif (method, route) == ("POST", "/api/resolve/open"):
            primary = backend.resolve_state(flags)["primary"]
            if primary is None:
                self._json({"ok": False, "path": None, "error": "Ingen projektmappe fundet"})
            else:
                result = backend.open_path(primary["path"], "folder", flags)
                self._json({"ok": result["ok"], "path": primary["path"], "error": result.get("error")})
        elif (method, route) == ("POST", "/api/settings"):
            backend.update_settings(body)
            self._json({"settings": backend.settings_payload(flags)})
        elif (method, route) == ("POST", "/api/import/project"):
            self._json(backend.importer.create_project(str(body.get("root", "")), str(body.get("name", ""))))
        elif (method, route) == ("POST", "/api/import/start"):
            self._json(backend.importer.start(str(body.get("card", "")), str(body.get("project", "")),
                                              bool(body.get("separate")), str(body.get("mode", "copy")), flags))
        elif (method, route) == ("POST", "/api/import/cancel"):
            backend.importer.cancelled.set()
            self._json({"ok": True})
        elif (method, route) == ("POST", "/api/import/dismiss"):
            self._json({"ok": True})
        elif (method, route) == ("POST", "/api/widget/play"):
            self._json(backend.pet_play_now())
        elif (method, route) == ("POST", "/api/widget/look"):
            self._json({"ok": True})
        elif (method, route) in (("POST", "/api/messages/click"), ("DELETE", "/api/messages")):
            self._json({"ok": True})
        elif (method, route) in (("POST", "/api/window/hide"), ("POST", "/api/window/show")):
            self._json({"ok": True})
        elif (method, route) == ("POST", "/api/_mock/publish"):  # test control: push any SSE event
            backend.bus.publish(str(body.get("type", "")), body.get("data"))
            self._json({"ok": True})
        elif (method, route) == ("POST", "/api/_mock/drop-events"):  # test control: end all SSE streams
            with backend.lock:
                backend.sse_generation += 1
            self._json({"ok": True})
        elif (method, route) == ("POST", "/api/_mock/set-online"):  # test control: location on/offline
            backend.set_online(int(body.get("source_id") or 0), bool(body.get("online")), body.get("path"),
                               str(body.get("events", "all")))
            self._json({"ok": True})
        else:
            self._json({"error": "Findes ikke"}, 404)

    def _static(self, path: str, content_type: str) -> None:
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            self._json({"error": "Findes ikke"}, 404)
            return
        self._send(200, data, content_type)

    # -- SSE ---------------------------------------------------------------------------
    def _events(self, flags: frozenset[str]) -> None:
        backend = self.backend
        sub = backend.bus.subscribe()  # before the headers: a client may publish right after them
        generation = backend.sse_generation
        self.close_connection = True
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
        except OSError:
            backend.bus.unsubscribe(sub)
            return
        self.connection.settimeout(None)
        start = time.monotonic()
        timers = {"scan_progress": start, "status": start + 1.0, "index_updated": start + 4.0,
                  "heartbeat": start + 15.0}
        once = {}
        if "nofocus" not in flags:
            once["focus"] = (start + 0.7, {"from_app": "Resolve.exe", "reason": "hotkey"})
        if "newdisk" in flags:
            once["new_volume"] = (start + 1.2, {"disk_name": "ARKIV", "drive": "F:", "source_ids": [5],
                                                "included": False, "reason": "Ingen projektmapper fundet"})
        try:
            while not backend.stopping.is_set() and backend.sse_generation == generation:
                now = time.monotonic()
                out: list[tuple[str, Any]] = []
                scans = backend.scanning(flags)
                if now >= timers["scan_progress"]:
                    timers["scan_progress"] = now + 0.25
                    out.extend(("scan_progress", {k: s[k] for k in (
                        "source_id", "name", "entries", "dirs", "units_done", "units_total",
                        "started", "kind")}) for s in scans)
                if now >= timers["status"]:
                    timers["status"] = now + 1.0
                    out.append(("status", backend.status(flags, full=False)))
                if now >= timers["index_updated"]:
                    timers["index_updated"] = now + 4.0
                    if scans:
                        out.append(("index_updated", {"source_id": scans[0]["source_id"]}))
                for name, (due, data) in list(once.items()):
                    if now >= due:
                        out.append((name, data))
                        del once[name]
                try:
                    while True:
                        event_type, data, _ts = sub.get(timeout=0.05 if not out else 0)
                        out.append((event_type, data))
                except queue.Empty:
                    pass
                chunks = [f"event: {t}\ndata: {json.dumps(d, ensure_ascii=False)}\n\n" for t, d in out]
                if now >= timers["heartbeat"]:
                    timers["heartbeat"] = now + 15.0
                    chunks.append(": ping\n\n")
                if chunks:
                    self.wfile.write("".join(chunks).encode("utf-8"))
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            backend.bus.unsubscribe(sub)


class _HTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    request_queue_size = 64
    backend: MockBackend

    def handle_error(self, request: Any, client_address: Any) -> None:
        """Browsers drop idle keep-alive connections at will; handlers catch everything else."""


class MockServer:
    """Mock backend on ``127.0.0.1:<port>`` (0 = any free port), served from a daemon thread."""

    def __init__(self, port: int = 0, scenario: str | None = None) -> None:
        if scenario is None:
            scenario = os.environ.get("PROJEKTSOG_UI_MOCK", "")
        self.backend = MockBackend(scenario)
        self._httpd = _HTTPServer(("127.0.0.1", port), MockHandler)
        self._httpd.backend = self.backend
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def start(self) -> "MockServer":
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="ui-mock-http",
                                        kwargs={"poll_interval": 0.1}, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self.backend.stopping.set()
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        if self.backend.time.store is not None:
            self.backend.time.store.close()

    def __enter__(self) -> "MockServer":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Projektsøg UI mock server")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--scenario", default=None, help="comma-separated scenario flags")
    args = parser.parse_args(argv)
    server = MockServer(args.port, args.scenario).start()
    if sys.stdout is not None:
        print(f"Projektsøg UI mock: {server.url}", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
