"""Automatic time tracking per DaVinci Resolve project, for invoicing.

Every few seconds the tracker looks at three cheap signals:

* which window is in front (``winui``): DaVinci Resolve, a browser showing a music/SFX site
  such as Artlist (``time_music_sites``) or an AI video/image site such as Higgsfield
  (``time_ai_sites``), or anything else;
* when the keyboard or mouse was last used (``GetLastInputInfo``);
* what Resolve is doing (``ResolveBridge.activity()``: the open project, the page – Edit, Color,
  Fusion … – the playhead and whether a render runs).

Time counts when Resolve is in front with a project open (bucket = the page), or when a music
site (bucket ``"musik"``) or an AI site (bucket ``"ai"``) is in front while a Resolve project is
open. A moving playhead counts as activity, so watching playback is not "idle" - for up to an
hour without any input, so a timeline left looping overnight does not bill the night. Rendering
alone is not activity.

One rule for every pause, ``time_idle_minutes`` long (the "Pause efter" setting): a pause
shorter than that counts fully, a longer one not at all. A pause is either no input at all (the
stretch is cut back to the last input) or time in another program while a Resolve project is
open (mail, Stifinder, Projektsøg …: the clock keeps running, and if you are not back in Resolve
in time, the stretch is cut back to when you left it).

Resolve answers scripts slowly while it is busy (editing, Fusion, playback). Its last known
project, page and timeline therefore stay valid for ``STALE_OK_S``.

Stretches of counted time are stored as segments in ``time.db`` (local SQLite, never shared):
``(project, database, uid, folder, bucket, start, end, host)`` with epoch seconds.
"""

from __future__ import annotations

import csv
import ctypes
import io
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Callable
from ctypes import wintypes
from datetime import date, datetime, timedelta
from typing import Any

from .config import Config, app_dir, hostname

log = logging.getLogger(__name__)

TICK_S = 5.0                 # how often the signals are read
SAVE_EVERY_S = 30.0          # how often a running segment's end is written to disk
GAP_S = 60.0                 # a longer gap between ticks (sleep, hibernation): close the segment
GRACE_S = TICK_S             # counted after the last input when a pause turns out too long
PLAYBACK_MAX_S = 60 * 60.0   # playback without any input counts for at most this long
STALE_OK_S = 120.0           # Resolve's last answer stays valid this long (it is slow when busy)
UNTITLED_PREFIX = "untitled project"   # Resolve's placeholder while the Project Manager is open

BROWSERS = frozenset({"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe",
                      "vivaldi.exe", "arc.exe"})
RESOLVE_EXES = frozenset({"resolve.exe"})

# Danish labels for the buckets, in report order.
BUCKET_LABELS = {
    "edit": "Edit", "cut": "Cut", "color": "Color", "fusion": "Fusion",
    "fairlight": "Fairlight", "deliver": "Deliver", "media": "Media", "photo": "Photo",
    "musik": "Musik/lyd", "ai": "AI-video/billeder",
}


def db_path() -> str:
    return os.path.join(app_dir(), "time.db")


# ------------------------------------------------------------------------------ signals

class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_GetLastInputInfo = _user32.GetLastInputInfo
_GetLastInputInfo.argtypes = [ctypes.POINTER(_LASTINPUTINFO)]
_GetLastInputInfo.restype = wintypes.BOOL
_GetTickCount = _kernel32.GetTickCount
_GetTickCount.argtypes = []
_GetTickCount.restype = wintypes.DWORD


def idle_seconds() -> float:
    """Seconds since the last keyboard or mouse input in this session."""
    info = _LASTINPUTINFO(cbSize=ctypes.sizeof(_LASTINPUTINFO))
    if not _GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    return ((_GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0   # wraps after 49.7 days


def foreground() -> tuple[str, str]:
    """``(exe, title)`` of the window in front, exe in lower case ("" when unknown)."""
    from . import winui
    info = winui.window_info(winui.foreground_window())
    if info is None:
        return "", ""
    return (info.exe or "").lower(), info.title or ""


# ------------------------------------------------------------------------------ storage

class TimeStore:
    """The segments table. One connection, guarded by a lock (writes are tiny and rare)."""

    def __init__(self, path: str | None = None) -> None:
        self.path = path or db_path()
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS segments("
            " id INTEGER PRIMARY KEY, project TEXT NOT NULL, database TEXT, uid TEXT,"
            " folder TEXT, bucket TEXT NOT NULL, start REAL NOT NULL, end REAL NOT NULL,"
            " host TEXT, timeline TEXT)")
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(segments)")}
        if "timeline" not in columns:      # a time.db from before timelines were recorded
            self._conn.execute("ALTER TABLE segments ADD COLUMN timeline TEXT")
        self._conn.execute("CREATE INDEX IF NOT EXISTS ix_segments_start ON segments(start)")

    def insert(self, key: tuple, folder: str | None, start: float, end: float, host: str) -> int:
        """``key`` = (project, database, uid, bucket, timeline)."""
        project, database, uid, bucket, timeline = key
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO segments(project, database, uid, folder, bucket, start, end, host, timeline)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (project, database, uid, folder, bucket, start, end, host, timeline or None))
            return int(cur.lastrowid)

    def update(self, seg_id: int, end: float, folder: str | None = None) -> None:
        with self._lock:
            if folder:
                self._conn.execute("UPDATE segments SET end=?, folder=? WHERE id=?",
                                   (end, folder, seg_id))
            else:
                self._conn.execute("UPDATE segments SET end=? WHERE id=?", (end, seg_id))

    def delete_short(self, seg_id: int, min_s: float) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM segments WHERE id=? AND end - start < ?",
                               (seg_id, min_s))

    def between(self, start: float, end: float) -> list[tuple]:
        with self._lock:
            return self._conn.execute(
                "SELECT project, database, uid, folder, bucket, start, end, timeline FROM segments"
                " WHERE end > ? AND start < ? ORDER BY start", (start, end)).fetchall()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# ------------------------------------------------------------------------------ tracker

class TimeTracker:
    """Background thread that turns the signals into segments (see module docstring)."""

    def __init__(self, cfg: Config, bridge: Any, *, store: TimeStore | None = None,
                 foreground_fn: Callable[[], tuple[str, str]] | None = None,
                 idle_fn: Callable[[], float] | None = None,
                 wall: Callable[[], float] = time.time,
                 mono: Callable[[], float] = time.monotonic) -> None:
        self._cfg = cfg
        self._bridge = bridge
        self._store_arg = store
        self.store: TimeStore | None = store
        self._foreground = foreground_fn or foreground
        self._idle = idle_fn or idle_seconds
        self._wall = wall
        self._mono = mono
        self._host = hostname()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Tracker-thread state (the lock guards what status() reads).
        self._seg: dict[str, Any] | None = None
        self._last_tick_mono: float | None = None
        self._last_tc: tuple[str | None, str] | None = None
        self._away_since: float | None = None   # left Resolve (the segment's end then)
        self._state = "off"

    # -- lifecycle -------------------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        if self.store is None:
            self.store = TimeStore()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="time-tracker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        with self._lock:
            self._close_segment(None)
        if self.store is not None and self._store_arg is None:
            self.store.close()

    def _run(self) -> None:
        while not self._stop.wait(TICK_S):
            try:
                self.tick()
            except Exception:
                log.exception("time tracking tick failed")

    # -- one step --------------------------------------------------------------------------
    def tick(self) -> None:
        """Read the signals once and update the running segment."""
        now, mono = self._wall(), self._mono()
        with self._lock:
            if not self._cfg.get("time_tracking_enabled", True):
                self._close_segment(None)
                self._away_since = None
                self._state = "off"
                self._last_tick_mono = mono
                return
            gap = self._last_tick_mono is not None and mono - self._last_tick_mono > GAP_S
            self._last_tick_mono = mono
            if gap:   # the PC slept: the segment ended at its last saved tick
                self._close_segment(None)
                self._away_since = None

            threshold = max(1.0, float(self._cfg.get("time_idle_minutes", 10))) * 60.0
            act = self._activity()
            exe, title = self._foreground()
            idle = max(0.0, self._idle())
            last_active = now - idle
            if self._playhead_moved(act) and idle <= PLAYBACK_MAX_S:
                last_active = now   # watching playback counts - but a forgotten loop doesn't bill all night

            ctx = self._context(act, exe, title)
            if self._seg is not None:
                self._seg["last_active"] = max(self._seg["last_active"], last_active)
            if ctx is None:
                open_project = bool(act and act.get("project"))
                seg = self._seg
                if seg is not None and open_project:
                    # In another program: no hard pause. The clock keeps running, and coming
                    # back within the pause limit counts the whole excursion.
                    if self._away_since is None:
                        self._away_since = seg["end"]
                    if now - self._away_since <= threshold:
                        self._state = "away"
                        return
                if self._away_since is not None:
                    self._close_segment(None)      # away too long: ends when you left Resolve
                else:
                    self._close_segment(min(now, last_active + threshold))   # Resolve closed
                self._away_since = None
                self._state = "paused" if open_project else "no-resolve"
                return
            self._away_since = None
            if now - last_active > threshold:
                # Away for longer than the idle limit: the pause is not counted at all.
                if self._seg is not None:
                    self._close_segment(self._seg["last_active"] + GRACE_S)
                self._state = "idle"
                return

            folder = act.get("folder") if act else None
            key = ctx
            if self._seg is not None and self._seg["key"] == key:
                self._seg["end"] = now
                if folder and not self._seg["folder"]:
                    self._seg["folder"] = folder
                    self._save(force=True)
                else:
                    self._save()
            else:
                switched = self._seg is not None
                self._close_segment(now)
                # A switch (page, project, Resolve <-> music/AI site) continues seamlessly at `now`;
                # resuming after a pause starts at the input that ended it (at most one tick ago).
                start = now if switched else max(now - TICK_S, min(now, last_active))
                self._open_segment(key, folder, start, now)
            self._state = "recording"

    # -- helpers ---------------------------------------------------------------------------
    def _activity(self) -> dict[str, Any] | None:
        if self._bridge is None:
            return None
        try:
            return self._bridge.activity(max_age=STALE_OK_S)
        except Exception:
            log.debug("bridge.activity() failed", exc_info=True)
            return None

    def _context(self, act: dict[str, Any] | None, exe: str,
                 title: str) -> tuple[str, str | None, str, str, str] | None:
        """``(project, database, uid, bucket, timeline)`` when this moment counts, else None.
        Time on a music or AI site counts on the timeline that is open in Resolve."""
        if not act:
            return None
        project = act.get("project")
        if not isinstance(project, str) or not project.strip() \
                or project.strip().casefold().startswith(UNTITLED_PREFIX):
            return None
        base = (project, act.get("database"), act.get("uid") or "")
        timeline = str(act.get("timeline") or "")
        if exe in RESOLVE_EXES:
            return base + ((act.get("page") or "edit").lower(), timeline)
        if exe in BROWSERS and title != "Projektsøg":
            folded = title.casefold()
            for bucket, key in (("musik", "time_music_sites"), ("ai", "time_ai_sites")):
                sites = [s.casefold() for s in self._cfg.get(key, []) if s.strip()]
                if any(site in folded for site in sites):
                    return base + (bucket, timeline)
        return None

    def _playhead_moved(self, act: dict[str, Any] | None) -> bool:
        if not act or act.get("rendering"):
            self._last_tc = None
            return False
        current = (act.get("project"), act.get("timecode") or "")
        moved = (self._last_tc is not None and current[1] != ""
                 and current[0] == self._last_tc[0] and current[1] != self._last_tc[1])
        self._last_tc = current
        return moved

    def _open_segment(self, key: tuple, folder: str | None, start: float, end: float) -> None:
        assert self.store is not None
        seg_id = self.store.insert(key, folder, start, end, self._host)
        self._seg = {"id": seg_id, "key": key, "folder": folder, "start": start, "end": end,
                     "last_active": end, "saved": self._mono()}

    def _save(self, force: bool = False) -> None:
        seg = self._seg
        if seg is None or self.store is None:
            return
        if force or self._mono() - seg["saved"] >= SAVE_EVERY_S:
            self.store.update(seg["id"], seg["end"], seg["folder"])
            seg["saved"] = self._mono()

    def _close_segment(self, end: float | None) -> None:
        """Finish the running segment at ``end`` (None: at its last known end)."""
        seg, self._seg = self._seg, None
        if seg is None or self.store is None:
            return
        # ``end`` may lie before the last tick: an over-long pause is cut back to the last input.
        final = seg["end"] if end is None else max(seg["start"], end)
        self.store.update(seg["id"], final, seg["folder"])
        self.store.delete_short(seg["id"], 1.0)

    # -- queries (any thread) --------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        """Live state for the UI: recording / away (in another program, still counting if you
        come back within ``away_until``) / idle / paused / no-resolve / off."""
        with self._lock:
            seg = dict(self._seg) if self._seg else None
            state = self._state
            away_since = self._away_since if state == "away" else None
        threshold = max(1.0, float(self._cfg.get("time_idle_minutes", 10))) * 60.0
        result: dict[str, Any] = {"state": state, "enabled": bool(self._cfg.get("time_tracking_enabled", True)),
                                  "project": None, "bucket": None, "since": None,
                                  "away_since": away_since,
                                  "away_until": away_since + threshold if away_since is not None else None}
        if seg:
            result.update(project=seg["key"][0], database=seg["key"][1], bucket=seg["key"][3],
                          bucket_label=BUCKET_LABELS.get(seg["key"][3], seg["key"][3].title()),
                          timeline=seg["key"][4] or None, folder=seg["folder"], since=seg["start"])
        today = datetime.fromtimestamp(self._wall()).date()   # the tracker's own clock
        result["today_s"] = round(sum(r["total_s"] for r in self.report(today, today)["projects"]))
        return result

    def report(self, first: date, last: date) -> dict[str, Any]:
        """Totals per project between two local dates (inclusive), split by bucket, by day and
        by timeline (``timelines``: largest first; "" = time recorded before timelines were)."""
        start = datetime.combine(first, datetime.min.time()).timestamp()
        end = datetime.combine(last + timedelta(days=1), datetime.min.time()).timestamp()
        rows = self.store.between(start, end) if self.store else []
        with self._lock:   # include the running segment's unsaved end
            live = dict(self._seg) if self._seg else None
        projects: dict[tuple, dict[str, Any]] = {}
        for project, database, uid, folder, bucket, s, e, timeline in rows:
            if live and live["key"] == (project, database, uid or "", bucket, timeline or "")                     and abs(s - live["start"]) < 1e-6:
                e = max(e, live["end"])
                folder = folder or live["folder"]
            s, e = max(s, start), min(e, end)
            if e <= s:
                continue
            entry = projects.setdefault((project, database), {
                "project": project, "database": database, "folder": None,
                "buckets": {}, "days": {}, "day_buckets": {}, "timelines": {}, "total_s": 0.0, "last": 0.0})
            if folder:
                entry["folder"] = folder
            entry["buckets"][bucket] = entry["buckets"].get(bucket, 0.0) + (e - s)
            for day, secs in _split_days(s, e):
                entry["days"][day] = entry["days"].get(day, 0.0) + secs
                per_day = entry["day_buckets"].setdefault(day, {})
                per_day[bucket] = per_day.get(bucket, 0.0) + secs
            line = entry["timelines"].setdefault(timeline or "", {"name": timeline or "", "total_s": 0.0,
                                                                   "buckets": {}})
            line["total_s"] += e - s
            line["buckets"][bucket] = line["buckets"].get(bucket, 0.0) + (e - s)
            entry["total_s"] += e - s
            entry["last"] = max(entry["last"], e)
        ordered = sorted(projects.values(), key=lambda p: (-p["total_s"], p["project"].casefold()))
        for p in ordered:
            p["buckets"] = {k: round(v, 1) for k, v in p["buckets"].items()}
            p["days"] = {k: round(v, 1) for k, v in sorted(p["days"].items())}
            p["day_buckets"] = {day: {k: round(v, 1) for k, v in b.items()}
                                for day, b in sorted(p["day_buckets"].items())}
            p["timelines"] = [{"name": t["name"], "total_s": round(t["total_s"], 1),
                               "buckets": {k: round(v, 1) for k, v in t["buckets"].items()}}
                              for t in sorted(p["timelines"].values(),
                                              key=lambda t: (-t["total_s"], t["name"].casefold()))]
            p["total_s"] = round(p["total_s"], 1)
        return {"from": first.isoformat(), "to": last.isoformat(), "projects": ordered,
                "total_s": round(sum(p["total_s"] for p in ordered), 1),
                "buckets": BUCKET_LABELS}

    def folders_on(self, day: date) -> list[str]:
        """The project folder names worked on that day, most recent first (for the import helper)."""
        rep = self.report(day, day)
        ordered = sorted(rep["projects"], key=lambda p: -p["last"])
        return list(dict.fromkeys(p["folder"] for p in ordered if p["folder"]))

    def export_csv(self, first: date, last: date, round_minutes: int = 0,
                   per_day: bool = False, per_timeline: bool = False) -> str:
        """Excel-friendly CSV (semicolon, Danish decimal comma, UTF-8 BOM): one row per project,
        or per project and day / per project and timeline (rounded per row)."""
        rep = self.report(first, last)
        buckets = [b for b in BUCKET_LABELS if any(b in p["buckets"] for p in rep["projects"])]
        out = io.StringIO()
        out.write("﻿")
        writer = csv.writer(out, delimiter=";", lineterminator="\r\n")
        head = (["Dato"] if per_day else []) + ["Projekt"] + (["Tidslinje"] if per_timeline else [])
        head += ["Projektmappe", "Database"]
        head += [BUCKET_LABELS[b] + " (t)" for b in buckets]
        head += ["Total (t)", "Total (t:mm)"]
        if round_minutes:
            head += [f"Afrundet til {round_minutes} min (t)"]
        writer.writerow(head)
        for p in rep["projects"]:
            if per_day:
                for day, secs in p["days"].items():
                    writer.writerow([day] + _project_cells(p, buckets, p["day_buckets"][day], secs,
                                                           round_minutes))
            elif per_timeline:
                for t in p["timelines"]:
                    cells = _project_cells(p, buckets, t["buckets"], t["total_s"], round_minutes)
                    writer.writerow(cells[:1] + [t["name"] or "(ukendt)"] + cells[1:])
            else:
                writer.writerow(_project_cells(p, buckets, p["buckets"], p["total_s"], round_minutes))
        return out.getvalue()


def _project_cells(p: dict[str, Any], buckets: list[str], times: dict[str, float], total: float,
                   round_minutes: int) -> list[str]:
    cells = [p["project"], p["folder"] or "", p["database"] or ""]
    cells += [_hours(times.get(b, 0.0)) for b in buckets]
    cells += [_hours(total), _hmm(total)]
    if round_minutes:
        cells.append(_hours(_round_up(total, round_minutes)))
    return cells


def _round_up(seconds: float, minutes: int) -> float:
    step = minutes * 60.0
    return 0.0 if seconds <= 0 else -(-seconds // step) * step


def _hours(seconds: float) -> str:
    return f"{seconds / 3600.0:.2f}".replace(".", ",")


def _hmm(seconds: float) -> str:
    minutes = int(round(seconds / 60.0))
    return f"{minutes // 60}:{minutes % 60:02d}"


def _split_days(start: float, end: float) -> list[tuple[str, float]]:
    """Split ``[start, end)`` at local midnights: ``[("2026-10-01", seconds), ...]``."""
    parts = []
    cursor = start
    while cursor < end:
        day = datetime.fromtimestamp(cursor).date()
        midnight = datetime.combine(day + timedelta(days=1), datetime.min.time()).timestamp()
        stop = min(end, midnight)
        parts.append((day.isoformat(), stop - cursor))
        cursor = stop
    return parts
