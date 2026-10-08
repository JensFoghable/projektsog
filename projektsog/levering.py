"""The delivery party (SPEC §22.4): Klippe celebrates when a film is delivered.

A **delivery** is

* a render that Resolve finished into a ``Final`` folder (the bridge's SSE ``render`` with
  ``faerdig.levering``), or
* a new file in the current Resolve project's ``Final`` folder (and one level of subfolders):
  polled every few seconds through ``call_with_timeout`` and counted once its size and mtime
  stood still over two polls; temporary and hidden files, files that were there (or older than)
  when the watching began, and files a render just wrote do not count. A look reads at most
  ``MAX_ENTRIES`` entries in all (an image sequence of 60,000 frames is not read in full), and
  after a look that was big, slow, timed out or incomplete the next comes only after a minute.

The same file is never celebrated twice within ten minutes, and a batch of files (an image
sequence, a folder copied in) is one party. A delivery → SSE ``levering {kilde, fil, projekt, sti,
demo}`` and – while Klippe is shown – its short ring, once. A done render rings too, also when
it went somewhere else.

``festkat()`` serves the spinning cat of the party: ``%LOCALAPPDATA%\\Projektsog\\festkat.gif``
(the user's own when it is there), else fetched once from ``FESTKAT_URL`` – never at import time,
never into the repo.
"""

from __future__ import annotations

import logging
import ntpath
import os
import queue
import threading
import time
import urllib.request
from collections.abc import Callable, Iterable, Iterator
from typing import Any

from . import __version__, config

log = logging.getLogger(__name__)

FESTKAT_URL = "https://media.giphy.com/media/1OrIIOIcRTDaNidc5p/200.gif"
FESTKAT_FILE = "festkat.gif"
FESTKAT_MAX_BYTES = 3 * 1024 * 1024
FESTKAT_TIMEOUT_S = 15.0
FESTKAT_RETRY_S = 600.0           # a failed fetch is not tried again for this long

FINAL = "Final"
POLL_S = 5.0
LIST_TIMEOUT_S = 4.0
DEDUPE_S = 600.0                  # the same file never twice within this long
MTIME_SLACK_S = 5.0               # "newer than when the watching began", with a little clock slack
RENDER_GRACE_S = 30.0             # files a render wrote until this long after it ended are its own
RENDER_KEEP_S = 600.0
QUIET_S = 120.0                   # more files in a folder that just had its party join that party
PAUSE_RESET_S = 60.0              # Resolve gone this long: start watching afresh when it is back
MAX_ENTRIES = 3000                # entries read in all per look (Final's own and its subfolders')
BIG_LISTING = 1000                # a look that found this many files was a big one …
SLOW_S = 1.0                      # … and one that took this long a slow one:
BACKOFF_S = 60.0                  # the next look after a big, slow, timed-out or incomplete one
IGNORED_SUFFIXES = (".tmp", ".part", ".partial", ".crdownload", ".download")
IGNORED_NAMES = frozenset({"thumbs.db", "desktop.ini", ".ds_store", "ehthumbs.db"})
FILE_ATTRIBUTE_HIDDEN = 0x2
FILE_ATTRIBUTE_SYSTEM = 0x4
FILE_ATTRIBUTE_REPARSE_POINT = 0x400

MSG_OFF = "Slå Klippe til først"
DEMO = {"kilde": "render", "fil": "Demo_levering.mp4", "projekt": "Demo", "sti": None, "demo": True}

_STOP = object()

Entry = tuple[str, bool, int, float, int]               # (name, is_dir, size, mtime, attributes)
Listing = Iterable[Entry]


# --------------------------------------------------------------------------------------
# The Final folder
# --------------------------------------------------------------------------------------

def list_dir(path: str) -> Iterator[Entry]:
    """``(name, is_dir, size, mtime, attributes)`` of a folder's entries, read lazily – the
    reader stops when it has enough (reparse points are not followed; the stat comes with the
    listing on Windows). An unreadable folder raises on the first entry asked for."""
    with os.scandir(path) as entries:
        for entry in entries:
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            attributes = int(getattr(st, "st_file_attributes", 0) or 0)
            is_dir = entry.is_dir(follow_symlinks=False) and not attributes & FILE_ATTRIBUTE_REPARSE_POINT
            yield entry.name, is_dir, int(st.st_size), float(st.st_mtime), attributes


def _read(lister: Callable[[str], Listing], path: str, budget: int) -> tuple[list[Entry], bool]:
    """At most ``budget`` of ``path``'s entries: ``(entries, all of them were read)``. The listing
    is read lazily and closed as soon as the budget is spent."""
    listing = iter(lister(_long(path)))
    out: list[Entry] = []
    try:
        for entry in listing:
            if len(out) >= budget:
                return out, False
            out.append(entry)
        return out, True
    finally:
        close = getattr(listing, "close", None)
        if callable(close):
            close()


def ignored(name: str, attributes: int = 0) -> bool:
    """Temporary, hidden and system files never count (nor do they lead into a folder)."""
    folded = name.casefold()
    return (attributes & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM) != 0 or folded.startswith((".", "~$"))
            or folded.endswith(IGNORED_SUFFIXES) or folded in IGNORED_NAMES)


def _long(path: str) -> str:
    """The \\\\?\\ form for paths beyond MAX_PATH."""
    if len(path) < 248 or path.startswith("\\\\?\\"):
        return path
    path = ntpath.normpath(path)
    return "\\\\?\\UNC\\" + path[2:] if path.startswith("\\\\") else "\\\\?\\" + path


def scan_final(project: str, lister: Callable[[str], Listing] = list_dir,
               budget: int = MAX_ENTRIES) -> tuple[dict[str, tuple[int, float]], bool] | None:
    """The files in ``project``\\Final and its subfolders: ``({path: (size, mtime)}, complete)``;
    ``{}`` while the project has no Final folder, None when the project cannot be reached.

    At most ``budget`` entries are read in all, while listing: Final's own first, then its
    subfolders (the newest first – the one a delivery just landed in) share what is left.
    ``complete`` is False when there was more than that, or a subfolder could not be read."""
    final = ntpath.join(project, FINAL)
    try:
        top, complete = _read(lister, final, budget)
    except FileNotFoundError:
        try:
            _read(lister, project, 0)                           # is the project itself there?
        except OSError:
            return None
        return {}, True
    except OSError:
        return None
    remaining = budget - len(top)
    files: dict[str, tuple[int, float]] = {}
    folders: list[tuple[float, str]] = []
    for name, is_dir, size, mtime, attributes in top:
        if ignored(name, attributes):
            continue
        path = ntpath.join(final, name)
        if is_dir:
            folders.append((mtime, path))
        else:
            files[path] = (size, mtime)
    for _mtime, path in sorted(folders, key=lambda folder: folder[0], reverse=True):
        if remaining <= 0:
            complete = False
            break
        try:
            inner, whole = _read(lister, path, remaining)
        except OSError:
            complete = False
            continue
        remaining -= len(inner)
        complete = complete and whole
        for name2, is_dir2, size2, mtime2, attributes2 in inner:
            if not is_dir2 and not ignored(name2, attributes2):
                files[ntpath.join(path, name2)] = (size2, mtime2)
    return files, complete


def _tail(path: str | None) -> str | None:
    """A folder by its last two parts (``rikke lindholm\\final``) – the same whether the render
    wrote it through a drive letter or a share."""
    if not isinstance(path, str) or not path.strip():
        return None
    parts = [p for p in ntpath.normpath(path).replace("/", "\\").split("\\") if p]
    return "\\".join(parts[-2:]).casefold() if parts else None


def is_gif(data: Any) -> bool:
    return isinstance(data, (bytes, bytearray)) and len(data) >= 13 and bytes(data[:4]) == b"GIF8"


def download(url: str, timeout: float, max_bytes: int) -> bytes:
    """``url``'s body (≤ ``max_bytes``, within ``timeout`` in all)."""
    request = urllib.request.Request(url, headers={"User-Agent": f"Projektsog/{__version__}"})
    deadline = time.monotonic() + timeout
    with urllib.request.urlopen(request, timeout=timeout) as response:     # noqa: S310 - a fixed https URL
        length = response.headers.get("Content-Length")
        if length and length.strip().isdigit() and int(length) > max_bytes:
            raise ValueError(f"{length} bytes")
        chunks: list[bytes] = []
        total = 0
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError("the download took too long")
            chunk = response.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"more than {max_bytes} bytes")
            chunks.append(chunk)
    return b"".join(chunks)


class _Watch:
    """What is known about one project's Final folder since the watching began."""

    def __init__(self, project: str, start: float) -> None:
        self.project = project
        self.key = project.casefold()
        self.start = start
        self.ready = False                                      # the first listing is the baseline
        self.settled: dict[str, tuple[int, float]] = {}         # there before, or counted already
        self.pending: dict[str, tuple[int, float]] = {}         # new or changed: seen once like this
        self.quiet: dict[str, float] = {}                       # folder → its party's batch ends


# --------------------------------------------------------------------------------------
# The party
# --------------------------------------------------------------------------------------

class Levering:
    """Finds deliveries (renders into Final, new files in Final) and serves the party's cat."""

    def __init__(self, cfg: Any, bus: Any, *, bridge: Any, shown: Callable[[], bool],
                 ring: Callable[[bool], None] | None = None,
                 fetch: Callable[[str, float, int], bytes] = download,
                 data_dir: str | None = None,
                 clock: Callable[[], float] = time.time,
                 lister: Callable[[str], Listing] = list_dir,
                 call_with_timeout: Callable[[str, Callable[[], Any], float], tuple[str, Any]] | None = None,
                 poll_s: float = POLL_S, backoff_s: float = BACKOFF_S,
                 timer: Callable[[], float] = time.monotonic) -> None:
        self.cfg = cfg
        self.bus = bus
        self.bridge = bridge
        self._shown = shown
        self._ring = ring
        self._fetch = fetch
        self._data_dir = data_dir
        self._clock = clock
        self._lister = lister
        self._call = call_with_timeout
        self._poll_s = poll_s
        self._backoff_s = backoff_s
        self._timer = timer
        self._backoff = False                                   # the last look was a heavy one
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._queue: queue.Queue | None = None
        self._watch: _Watch | None = None
        self._paused_at: float | None = None
        self._seen: dict[str, float] = {}                       # casefolded path → when it was celebrated
        self._render_active = False
        self._render_seq: Any = None
        self._renders: list[list[Any]] = []                     # [start, end | None, folder tail | None]
        self._cat_lock = threading.Lock()
        self._cat: tuple[tuple[int, int], bytes] | None = None
        self._cat_failed_at: float | None = None
        self._cat_refused: tuple[int, int] | None = None

    # -- lifecycle --------------------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._queue = self.bus.subscribe()
        try:
            done = (self.bridge.render_state() or {}).get("faerdig")
        except Exception:
            done = None
        if isinstance(done, dict):
            self._render_seq = done.get("seq")                  # a render that ended before we started
        self._thread = threading.Thread(target=self._run, name="levering", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        q = self._queue
        if q is not None:
            self.bus.unsubscribe(q)
            while True:
                try:
                    q.put_nowait((_STOP, None, 0.0))
                    break
                except queue.Full:
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        pass

    def _run(self) -> None:
        q = self._queue
        next_poll = time.monotonic() + self._poll_s
        while not self._stop.is_set():
            try:
                kind, data, _ts = q.get(timeout=max(0.0, next_poll - time.monotonic()))
            except queue.Empty:
                kind = data = None
            if kind is _STOP or self._stop.is_set():
                break
            if kind == "render":
                try:
                    self.on_render(data)
                except Exception:
                    log.exception("the delivery party: a render event failed")
            if time.monotonic() >= next_poll:
                try:
                    self.poll()
                except Exception:
                    log.exception("the delivery party: watching Final failed")
                next_poll = time.monotonic() + self.next_poll_s()

    def next_poll_s(self) -> float:
        """Seconds until the next look at Final: ``poll_s``, or ``backoff_s`` after a look that
        was big, slow, timed out or incomplete."""
        return max(self._poll_s, self._backoff_s) if self._backoff else self._poll_s

    # -- the API ----------------------------------------------------------------------------
    def demo(self) -> dict[str, Any]:
        """``POST /api/levering/demo``: a party without a delivery ("🎉 Prøv leveringsfesten")."""
        if not self.cfg.get("widget_enabled", False):
            raise ValueError(MSG_OFF)
        self._deliver(dict(DEMO))
        return {"ok": True}

    def _enabled(self) -> bool:
        return bool(self.cfg.get("widget_enabled", False) and self.cfg.get("widget_levering", True))

    def _deliver(self, event: dict[str, Any]) -> None:
        log.info("the delivery party: %s (%s)", event.get("fil"), event.get("kilde"))
        self.bus.publish("levering", event)
        self._ring_once()

    def _ring_once(self) -> None:
        try:
            if not self._shown():
                return
        except Exception:
            return
        ring = self._ring
        if ring is None:
            from . import messages
            ring = messages.ring_sound
        try:
            ring(True)
        except Exception:
            log.debug("the delivery party: no ring", exc_info=True)

    def _seen_recently(self, path: str | None, now: float) -> bool:
        if not path:
            return False
        for key in [k for k, at in self._seen.items() if not 0 <= now - at < DEDUPE_S]:
            del self._seen[key]
        return path.casefold() in self._seen

    # -- renders (SSE "render" from the bridge, SPEC §22.1) --------------------------------------
    def on_render(self, data: Any) -> dict[str, Any] | None:
        """The bridge's render state; returns the ``levering`` event it became (or None)."""
        if not isinstance(data, dict):
            return None
        now = self._clock()
        active = bool(data.get("aktiv"))
        with self._lock:
            if active and not self._render_active:
                self._renders.append([now, None, None])
            elif not active and self._render_active and self._renders and self._renders[-1][1] is None:
                self._renders[-1][1] = now
            self._render_active = active
            self._renders = [r for r in self._renders if r[1] is None or 0 <= now - r[1] < RENDER_KEEP_S]
            done = data.get("faerdig")
            if not isinstance(done, dict) or done.get("seq") == self._render_seq:
                return None
            self._render_seq = done.get("seq")
            sti = done.get("sti") if isinstance(done.get("sti"), str) else None
            folder = _tail(done.get("mappe")) or (_tail(ntpath.dirname(sti)) if sti else None)
            if self._renders and (self._renders[-1][1] is None or 0 <= now - self._renders[-1][1] < RENDER_GRACE_S):
                self._renders[-1][2] = folder
            else:                                               # it began and ended between two of our looks
                self._renders.append([now - RENDER_GRACE_S, now, folder])
            if done.get("udfald") != "done":
                return None
            delivery = bool(done.get("levering")) and self._enabled() and not self._seen_recently(sti, now)
            if delivery and sti:
                self._seen[sti.casefold()] = now
        if not delivery:
            self._ring_once()                                   # "Renderen er færdig!" – once
            return None
        name = done.get("fil") if isinstance(done.get("fil"), str) and done.get("fil") else None
        event = {"kilde": "render", "fil": name or data.get("navn") or (ntpath.basename(sti) if sti else None),
                 "projekt": data.get("projekt"), "sti": sti, "demo": False}
        self._deliver(event)
        return event

    def _from_render(self, path: str, mtime: float) -> bool:
        """``path`` was written by a render (one running, or one that just ended)."""
        folder = _tail(ntpath.dirname(path))
        for start, end, render_folder in self._renders:
            if mtime < start - MTIME_SLACK_S:
                continue
            if end is not None and mtime > end + RENDER_GRACE_S:
                continue
            if render_folder is None or render_folder == folder:
                return True
        return False

    # -- the Final folder ---------------------------------------------------------------------
    def _project(self) -> tuple[str, str | None] | None:
        """(the current project's folder, the Resolve project's name) while Resolve is connected."""
        try:
            state = self.bridge.state()
        except Exception:
            return None
        if not isinstance(state, dict) or not state.get("connected"):
            return None
        primary = state.get("primary")
        if not isinstance(primary, dict):
            return None
        source = primary.get("source")                          # an Item: source.online (SPEC §7.1)
        online = source.get("online") if isinstance(source, dict) and "online" in source else primary.get("online")
        if online is False:
            return None                                         # its disk or computer is off
        path = primary.get("path")
        if not isinstance(path, str) or not path.strip():
            return None
        project = state.get("project")
        return path, project if isinstance(project, str) else None

    def poll(self) -> dict[str, Any] | None:
        """One look at the Final folder; returns the ``levering`` event it gave (or None)."""
        now = self._clock()
        self._backoff = False                                   # no look, or a light one: 5 s
        if not self._enabled():
            self._watch = self._paused_at = None
            return None
        found = self._project()
        if found is None:
            if self._watch is not None:
                if self._paused_at is None:
                    self._paused_at = now
                elif not 0 <= now - self._paused_at < PAUSE_RESET_S:
                    self._watch = self._paused_at = None
            return None
        project, name = found
        self._paused_at = None
        if self._watch is None or self._watch.key != project.casefold():
            self._watch = _Watch(project, now)
        watch = self._watch
        call = self._call
        if call is None:
            from . import winfs
            call = winfs.call_with_timeout
        root = ntpath.splitdrive(project)[0] or project
        lister = self._lister
        began = self._timer()
        status, result = call(f"levering:{root}", lambda: scan_final(project, lister), LIST_TIMEOUT_S)
        took = self._timer() - began
        # Big, slow, timed out ("busy": the one that timed out still runs) or incomplete: the
        # next look comes after backoff_s, not every 5 s.
        self._backoff = (status != "ok" or took >= SLOW_S
                         or (result is not None and (not result[1] or len(result[0]) >= BIG_LISTING)))
        if self._backoff:
            log.debug("the delivery party: %s (%s, %.1f s); next look in %.0f s", project, status, took,
                      self.next_poll_s())
        if status != "ok" or result is None:
            return None
        files, complete = result
        with self._lock:
            event = self._celebrate(watch, self._compare(watch, files, complete), name, now)
        if event is not None:
            self._deliver(event)
        return event

    @staticmethod
    def _compare(watch: _Watch, files: dict[str, tuple[int, float]], complete: bool) -> list[tuple[str, int, float]]:
        """The files that stood still over two looks (new or changed since the baseline)."""
        if not watch.ready:
            watch.settled = dict(files)
            watch.ready = True
            return []
        ready: list[tuple[str, int, float]] = []
        for path, sig in files.items():
            if watch.settled.get(path) == sig:
                continue
            if watch.pending.get(path) == sig:
                del watch.pending[path]
                watch.settled[path] = sig
                ready.append((path, sig[0], sig[1]))
            else:
                watch.pending[path] = sig
        if complete:                                            # gone files may come back as new ones
            watch.settled = {p: s for p, s in watch.settled.items() if p in files}
            watch.pending = {p: s for p, s in watch.pending.items() if p in files}
        return ready

    def _celebrate(self, watch: _Watch, ready: list[tuple[str, int, float]], name: str | None,
                   now: float) -> dict[str, Any] | None:
        batch: list[tuple[str, int]] = []
        for path, size, mtime in ready:
            if size <= 0 or mtime < watch.start - MTIME_SLACK_S:
                continue                                        # empty, or older than the watching
            if self._render_active or self._from_render(path, mtime):
                continue                                        # the render's own file
            if self._seen_recently(path, now):
                continue
            batch.append((path, size))
        if not batch:
            return None
        watch.quiet = {f: until for f, until in watch.quiet.items() if until > now}
        folders = {ntpath.dirname(path).casefold() for path, _size in batch}
        joins = any(folder in watch.quiet for folder in folders)
        for folder in folders:
            watch.quiet[folder] = now + QUIET_S
        for path, _size in batch:
            self._seen[path.casefold()] = now
        if joins:
            return None                                         # more of the batch that had its party
        path, _size = max(batch, key=lambda item: item[1])         # the film, not its stills
        return {"kilde": "fil", "fil": ntpath.basename(path), "projekt": name, "sti": path, "demo": False}

    # -- the cat ------------------------------------------------------------------------------
    def festkat(self) -> bytes | None:
        """``GET /api/festkat``: the GIF (the user's own festkat.gif, else fetched once), or None."""
        path = os.path.join(self._data_dir or config.app_dir(), FESTKAT_FILE)
        with self._cat_lock:
            try:
                st = os.stat(path)
            except FileNotFoundError:
                st = None
            except OSError as exc:
                log.debug("the delivery party: %s", exc)
                return None
            if st is not None:
                return self._read_cat(path, (st.st_size, st.st_mtime_ns))
            if self._cat is not None and self._cat[0] == (-1, -1):
                return self._cat[1]                             # fetched, but it could not be saved
            now = self._clock()
            if self._cat_failed_at is not None and 0 <= now - self._cat_failed_at < FESTKAT_RETRY_S:
                return None
            try:
                data = self._fetch(FESTKAT_URL, FESTKAT_TIMEOUT_S, FESTKAT_MAX_BYTES)
                if not is_gif(data) or len(data) > FESTKAT_MAX_BYTES:
                    raise ValueError("not a GIF of at most 3 MB")
            except Exception as exc:
                self._cat_failed_at = now
                log.warning("the delivery party: the cat could not be fetched: %s", exc)
                return None
            data = bytes(data)
            self._cat_failed_at = None
            temp = f"{path}.{os.getpid()}.tmp"
            try:
                with open(temp, "wb") as fh:
                    fh.write(data)
                os.replace(temp, path)
                st = os.stat(path)
                self._cat = ((st.st_size, st.st_mtime_ns), data)
            except OSError as exc:
                log.warning("the delivery party: could not save %s: %s", path, exc)
                self._cat = ((-1, -1), data)
            return data

    def _read_cat(self, path: str, signature: tuple[int, int]) -> bytes | None:
        if self._cat is not None and self._cat[0] == signature:
            return self._cat[1]
        if self._cat is not None and self._cat[0] == (-1, -1):
            self._cat = None                                    # a file is there now: it wins
        if signature[0] > FESTKAT_MAX_BYTES:
            data = None
        else:
            try:
                with open(path, "rb") as fh:
                    data = fh.read(FESTKAT_MAX_BYTES + 1)
            except OSError as exc:
                log.debug("the delivery party: %s", exc)
                return None
        if data is None or len(data) > FESTKAT_MAX_BYTES or not is_gif(data):
            if self._cat_refused != signature:
                self._cat_refused = signature
                log.warning("the delivery party: %s is not a GIF of at most 3 MB", path)
            return None
        self._cat = (signature, data)
        return data
