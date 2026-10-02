"""Import helper: copy the clips of a camera card into a project folder (SPEC §17).

* A light watcher lists the drives every ``POLL_S`` (``winfs.list_volumes``, ~2 ms) and looks
  for camera cards on removable volumes: Sony XDCAM (``XDROOT\\Clip``, ``PRIVATE\\XDROOT\\Clip``:
  FX9, FS7), Sony Alpha/Cinema Line (``PRIVATE\\M4ROOT\\CLIP`` + stills in ``DCIM``), DJI and
  GoPro (``DCIM``). The camera model comes from the clips' XML (``modelName``) and picks the
  project's ``Klip\\<camera>`` folder (``import_camera_folders``).
* "Already imported" = a file with the same name AND size somewhere in the index (camera
  counters wrap, so names alone repeat) - or in the target folder.
* Targets: projects holding some of the card's clips, the project open in Resolve, projects
  worked on today (time tracking), projects created here; or a new project from the
  "1. KUNDENAVN" template next to it.
* Copying (``ImportJob``): the source is read and hashed (SHA-1) in one thread while another
  writes ``<name>.projektsog-tmp``; then the copy is read back without the Windows file cache
  while the card file is read a second time (also uncached), and all three hashes must match;
  only a verified copy is renamed to its real name. Nothing is ever overwritten. "Move" (like
  Ctrl+X, but checked) deletes the verified files from the card only after EVERY file is
  verified and flushed to the disk. "Prepare" only creates the folder and opens both folders.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import logging
import ntpath
import os
import queue
import re
import shutil
import threading
import time
from collections.abc import Callable
from ctypes import wintypes
from datetime import date, datetime
from typing import Any

from . import winfs, winui
from .config import Config, app_dir
from .events import EventBus
from .pathmap import long_path

log = logging.getLogger(__name__)

POLL_S = 2.0
CHUNK = 8 * 1024 * 1024
FREE_MARGIN = 512 * 1024 * 1024          # left free on the target disk
PART_SUFFIX = ".projektsog-tmp"
FS_TIMEOUT_S = 5.0
PUBLISH_EVERY_S = 0.25
HISTORY_KEEP = 300
TEMPLATE_FILE_MAX = 50 * 1024 * 1024     # template files larger than this are not copied
NAME_MAX = 120

MEDIA_EXTS = frozenset({".mxf", ".mp4", ".mov", ".mts", ".m2ts", ".braw", ".r3d", ".crm",
                        ".insv", ".avi", ".mkv", ".lrv"})
STILL_EXTS = frozenset({".arw", ".jpg", ".jpeg", ".dng", ".heic", ".hif", ".cr3", ".nef", ".raw"})
_MODEL_RE = re.compile(rb'modelName="([^"]+)"')
_CREATED_RE = re.compile(rb'<CreationDate\s+value="([^"]+)"')
_INVALID_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = frozenset({"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                       *(f"lpt{i}" for i in range(1, 10))})

MSG_BUSY = "En overførsel er allerede i gang"
MSG_NO_CARD = "Kortet er ikke sat i længere"
MSG_CARD_GONE = "Kortet blev taget ud under overførslen – sæt det i igen og tryk Fortsæt"
MSG_DISK_FULL = "Der er ikke plads nok på disken"


class ImportCancelled(Exception):
    pass


# ------------------------------------------------------------------------------ card layout

def detect_card(root: str) -> list[tuple[str, str]]:
    """``[(kind, clip folder), …]`` of a volume root that holds camera media, else ``[]``.

    Kinds: ``xdcam`` (FX9/FS7), ``m4root`` (Sony Alpha / Cinema Line), ``stills`` (``DCIM``
    folders next to M4ROOT), ``dji``, ``gopro``."""
    found: list[tuple[str, str]] = []
    for rel in ("XDROOT\\Clip", "PRIVATE\\XDROOT\\Clip"):
        if os.path.isdir(ntpath.join(root, rel)):
            found.append(("xdcam", ntpath.join(root, rel)))
    m4 = ntpath.join(root, "PRIVATE\\M4ROOT\\CLIP")
    if os.path.isdir(m4):
        found.append(("m4root", m4))
    dcim = ntpath.join(root, "DCIM")
    if os.path.isdir(dcim):
        try:
            with os.scandir(dcim) as entries:
                subdirs = sorted(e.name for e in entries if e.is_dir())
        except OSError:
            subdirs = []
        for name in subdirs:
            folder = ntpath.join(dcim, name)
            upper = name.upper()
            if upper.endswith("GOPRO"):
                found.append(("gopro", folder))
            elif upper.endswith("MEDIA") or upper.startswith("DJI"):
                found.append(("dji", folder))
            elif upper.endswith("MSDCF") and found and found[0][0] in ("m4root", "xdcam"):
                found.append(("stills", folder))
    return found


def card_files(folders: list[tuple[str, str]]) -> list[dict[str, Any]]:
    """The files to import: everything in the clip folders, flat (``name``, ``size``, …)."""
    files: list[dict[str, Any]] = []
    seen: set[str] = set()
    for kind, folder in folders:
        try:
            with os.scandir(folder) as listing:
                entries = sorted(listing, key=lambda e: e.name.casefold())
        except OSError:
            continue
        for entry in entries:
            if entry.name.startswith(".") or entry.name.endswith(PART_SUFFIX):
                continue
            try:
                if not entry.is_file():
                    continue
                st = entry.stat()
            except OSError:
                continue
            ext = ntpath.splitext(entry.name)[1].lower()
            files.append({"name": entry.name, "size": st.st_size, "mtime": st.st_mtime,
                          "path": entry.path, "kind": kind,
                          "media": ext in MEDIA_EXTS and not entry.name.upper().endswith("S03.MP4"),
                          "still": ext in STILL_EXTS,
                          "duplicate": entry.name.casefold() in seen})
            seen.add(entry.name.casefold())
    return files


def _read_head(path: str, limit: int = 64 * 1024) -> bytes:
    try:
        with open(path, "rb") as fh:
            return fh.read(limit)
    except OSError:
        return b""


def clip_xml(media_path: str) -> str | None:
    """The Sony sidecar of a clip: ``FX9_0001.MXF`` → ``FX9_0001M01.XML``."""
    stem, _ext = ntpath.splitext(media_path)
    candidate = stem + "M01.XML"
    return candidate if os.path.isfile(candidate) else None


def camera_model(files: list[dict[str, Any]], kinds: set[str]) -> str | None:
    """The camera model from the first clip's XML (``PXW-FX9V``, ``ILCE-7SM3``), else by kind."""
    for f in files:
        if f["media"]:
            xml = clip_xml(f["path"])
            if xml:
                m = _MODEL_RE.search(_read_head(xml))
                if m:
                    return m.group(1).decode("utf-8", "replace").strip()
            break
    if "dji" in kinds:
        return "DJI"
    if "gopro" in kinds:
        return "GoPro"
    return None


def recorded_at(media: dict[str, Any]) -> float | None:
    """When a clip was recorded (its XML ``CreationDate``; the file time otherwise)."""
    xml = clip_xml(media["path"])
    if xml:
        m = _CREATED_RE.search(_read_head(xml))
        if m:
            try:
                return datetime.fromisoformat(m.group(1).decode("ascii")).timestamp()
            except ValueError:
                pass
    return media.get("mtime")


def camera_folder(model: str | None, first_name: str, mapping: list[str]) -> str:
    """The Klip subfolder for a camera: ``import_camera_folders`` by model prefix, else a
    folder named like the clips' prefix (``FX9_0001`` → ``FX9``), else ``Kamera``."""
    rules = []
    for rule in mapping:
        prefix, sep, folder = str(rule).partition("=")
        if sep and prefix.strip() and folder.strip():
            rules.append((prefix.strip().upper(), folder.strip()))
    if model:
        upper = model.upper()
        for prefix, folder in sorted(rules, key=lambda r: -len(r[0])):
            if upper.startswith(prefix):
                return folder
    stem = re.split(r"[_ ]", first_name, maxsplit=1)[0] if first_name else ""
    for _prefix, folder in rules:
        if stem and stem.casefold() == folder.casefold():
            return folder
    return model or "Kamera"


def validate_project_name(name: str) -> str:
    """A new project's folder name ("Kunde - Projekt", or "Gruppe\\Projekt"); ValueError."""
    text = str(name or "").strip().replace("/", "\\")
    parts = [p.strip() for p in text.split("\\")]
    if not text or not all(parts):
        raise ValueError("Skriv et navn til projektet")
    if len(parts) > 2:
        raise ValueError("Højst én gruppemappe, fx “Kunde 2026\\Kunde - Projekt”")
    for part in parts:
        if _INVALID_NAME.search(part):
            raise ValueError('Navnet må ikke indeholde < > : " / | ? *')
        if part.endswith(".") or part.casefold() in _RESERVED or part in (".", ".."):
            raise ValueError(f"“{part}” kan ikke bruges som mappenavn")
        if len(part) > NAME_MAX:
            raise ValueError(f"Navnet er for langt (højst {NAME_MAX} tegn)")
    return "\\".join(parts)


def _child_dir(parent: str, name: str) -> str | None:
    """``parent\\<name>`` with the casing on disk, when that folder exists."""
    try:
        with os.scandir(long_path(parent)) as entries:
            for entry in entries:
                if entry.name.casefold() == name.casefold() and entry.is_dir():
                    return ntpath.join(parent, entry.name)
    except OSError:
        return None
    return None


def _listing(folder: str) -> dict[str, int]:
    """``{name casefolded: size}`` of the files in ``folder`` ({} when it does not exist)."""
    out: dict[str, int] = {}
    try:
        with os.scandir(long_path(folder)) as entries:
            for entry in entries:
                if entry.is_file() and not entry.name.endswith(PART_SUFFIX):
                    out[entry.name.casefold()] = entry.stat().st_size
    except FileNotFoundError:
        return {}
    return out


# ------------------------------------------------------------------------------ uncached read

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_CreateFileW = _k32.CreateFileW
_CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                         wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
_CreateFileW.restype = wintypes.HANDLE
_ReadFile = _k32.ReadFile
_ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                      ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
_ReadFile.restype = wintypes.BOOL
_CloseHandle = _k32.CloseHandle
_CloseHandle.argtypes = [wintypes.HANDLE]
_CloseHandle.restype = wintypes.BOOL
_VirtualAlloc = _k32.VirtualAlloc
_VirtualAlloc.argtypes = [wintypes.LPVOID, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD]
_VirtualAlloc.restype = wintypes.LPVOID
_VirtualFree = _k32.VirtualFree
_VirtualFree.argtypes = [wintypes.LPVOID, ctypes.c_size_t, wintypes.DWORD]
_VirtualFree.restype = wintypes.BOOL
_INVALID_HANDLE = wintypes.HANDLE(-1).value
_GENERIC_READ = 0x80000000
_FILE_SHARE_READ = 0x1
_OPEN_EXISTING = 3
_FILE_FLAG_NO_BUFFERING = 0x20000000
_FILE_FLAG_SEQUENTIAL_SCAN = 0x08000000
_MEM_COMMIT_RESERVE = 0x3000
_MEM_RELEASE = 0x8000
_PAGE_READWRITE = 0x04


def hash_uncached(path: str, step: Callable[[int], None]) -> str | None:
    """SHA-1 of ``path`` read past the Windows file cache (so the disk itself is checked);
    None when the file cannot be opened that way (the caller then reads it normally)."""
    handle = _CreateFileW(long_path(path), _GENERIC_READ, _FILE_SHARE_READ, None, _OPEN_EXISTING,
                          _FILE_FLAG_NO_BUFFERING | _FILE_FLAG_SEQUENTIAL_SCAN, None)
    if handle in (None, _INVALID_HANDLE):
        return None
    buffer = _VirtualAlloc(None, CHUNK, _MEM_COMMIT_RESERVE, _PAGE_READWRITE)
    try:
        if not buffer:
            return None
        view = memoryview((ctypes.c_char * CHUNK).from_address(buffer)).cast("B")
        digest = hashlib.sha1()
        got = wintypes.DWORD()
        while True:
            if not _ReadFile(handle, buffer, CHUNK, ctypes.byref(got), None):
                raise ctypes.WinError(ctypes.get_last_error())
            if got.value == 0:
                break
            digest.update(view[:got.value])
            step(got.value)
        view.release()
        return digest.hexdigest()
    finally:
        if buffer:
            _VirtualFree(buffer, 0, _MEM_RELEASE)
        _CloseHandle(handle)


_FlushFileBuffers = _k32.FlushFileBuffers
_FlushFileBuffers.argtypes = [wintypes.HANDLE]
_FlushFileBuffers.restype = wintypes.BOOL
_GENERIC_WRITE = 0x40000000
_FILE_SHARE_WRITE = 0x2


def flush_volume_cache(path: str) -> bool:
    """Ask Windows to write the cache of the whole disk holding ``path`` (best effort: some
    disks need administrator rights; network shares flush per file on their server)."""
    m = re.match(r"^([A-Za-z]):", path)
    if not m:
        return False
    handle = _CreateFileW(f"\\\\.\\{m.group(1)}:", _GENERIC_READ | _GENERIC_WRITE,
                          _FILE_SHARE_READ | _FILE_SHARE_WRITE, None, _OPEN_EXISTING, 0, None)
    if handle in (None, _INVALID_HANDLE):
        return False
    try:
        return bool(_FlushFileBuffers(handle))
    finally:
        _CloseHandle(handle)


def hash_buffered(path: str, step: Callable[[int], None]) -> str:
    digest = hashlib.sha1()
    with open(long_path(path), "rb", buffering=0) as fh:
        while chunk := fh.read(CHUNK):
            digest.update(chunk)
            step(len(chunk))
    return digest.hexdigest()


# ------------------------------------------------------------------------------ the copy job

class ImportJob:
    """Copies ``files`` into ``target`` (one thread; reading+hashing in a helper thread)."""

    def __init__(self, card: dict[str, Any], files: list[dict[str, Any]], target: str,
                 project: str, publish: Callable[[dict[str, Any]], None],
                 done: Callable[["ImportJob"], None], *,
                 present: list[dict[str, Any]] | None = None, move: bool = False,
                 manifest_dir: str | None = None,
                 hash_check: Callable[[str, Callable[[int], None]], str | None] = hash_uncached,
                 flush_volume: Callable[[str], bool] | None = None,
                 clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time) -> None:
        self.card = card
        self.files = files
        # Move only: files already in the target (same name and size). They are compared by
        # content and, when identical, deleted from the card like the copied ones.
        self.present = list(present or []) if move else []
        self.move = move
        self.target = target
        self.project = project
        self._publish = publish
        self._done = done
        self._hash_check = hash_check
        self._flush_volume = flush_volume or flush_volume_cache
        self._clock = clock
        self._wall = wall
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.verified: list[dict[str, Any]] = []   # card files whose copy is verified identical
        self._written: list[str] = []               # final paths this job created
        self._manifest = self._open_manifest(manifest_dir)
        total = sum(f["size"] for f in files) + sum(f["size"] for f in self.present)
        self.state: dict[str, Any] = {
            "id": f"{card['id']}-{int(wall())}", "card": card["id"], "drive": card["drive"],
            "camera": card["camera"], "target": target, "project": project,
            "project_name": ntpath.basename(project.rstrip("\\")) or project,
            "mode": "move" if move else "copy",
            "state": "copying", "phase": "copy", "current": None,
            "files_total": len(files) + len(self.present), "files_done": 0, "bytes_total": total,
            "copied": 0, "verified": 0, "deleted": 0, "kept": 0, "speed": 0.0, "eta_s": None,
            "started": wall(), "finished": None, "error": None}
        self._samples: list[tuple[float, int]] = []
        self._last_publish = 0.0

    # -- control ---------------------------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="import-job", daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._cancel.set()

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    @property
    def running(self) -> bool:
        return self.state["state"] in ("copying", "verifying", "deleting")

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self.state)

    # -- work ------------------------------------------------------------------------------
    def _run(self) -> None:
        try:
            os.makedirs(long_path(self.target), exist_ok=True)
            self._log({"start": self.state["id"], "mode": self.state["mode"], "card": self.card["id"],
                       "camera": self.card["camera"], "target": self.target, "at": self._wall()})
            for f in self.files:
                digest = self._one(f)
                self._verified_file(f, digest, copied=True)
            for f in self.present:        # (move only)
                digest = self._compare(f)
                self._verified_file(f, digest, copied=False)
            if self.move:
                # Nothing is deleted unless EVERY file above is copied and verified (an error
                # or a stop before this line leaves the card exactly as it was).
                self._delete_from_card()
            self._finish("done")
        except ImportCancelled:
            self._finish("cancelled")
        except Exception as exc:   # noqa: BLE001 - reported to the user, logged in full
            log.warning("Import into %s failed", self.target, exc_info=True)
            self._finish("failed", _error_text(exc, self.card["drive"]))
        finally:
            self._log({"end": self.state["state"], "deleted": self.state["deleted"], "at": self._wall()})
            if self._manifest is not None:
                self._manifest.close()
            try:
                self._done(self)
            except Exception:
                log.exception("import done callback failed")

    def _verified_file(self, f: dict[str, Any], digest: str, *, copied: bool) -> None:
        self.verified.append(f)
        if copied:
            self._written.append(ntpath.join(self.target, f["name"]))
        self._log({"file": f["name"], "size": f["size"], "sha1": digest, "source": f["path"],
                   "copied": copied, "verified": True})
        with self._lock:
            self.state["files_done"] += 1

    def _compare(self, f: dict[str, Any]) -> str:
        """A file the target already had (same name and size): identical content as well?"""
        final = ntpath.join(self.target, f["name"])
        with self._lock:
            self.state.update(phase="compare", state="verifying", current=f["name"])
        source = self._hash_check(f["path"], self._read) or hash_buffered(f["path"], self._read)
        check = self._hash_check(final, self._verified)
        if check is None:
            check = hash_buffered(final, self._verified)
        if check != source:
            raise OSError(f"{f['name']} ligger allerede i mappen, men er ikke identisk med kortet")
        return source

    def _delete_from_card(self) -> None:
        """The "Ctrl+X" part: delete the verified files from the card, one by one, each only
        after checking that its copy is still there with the same size and that the card file
        is unchanged. Stopping or a failure here leaves the rest on the card."""
        with self._lock:
            self.state.update(state="deleting", phase="delete", current=None)
        self._progress(force=True)
        self._make_durable()
        for f in self.verified:
            if self._cancel.is_set():
                raise ImportCancelled()
            copy = os.stat(long_path(ntpath.join(self.target, f["name"])))
            if copy.st_size != f["size"]:
                raise OSError(f"Kopien af {f['name']} har ikke længere samme størrelse – "
                              "resten er ikke slettet fra kortet")
            source = os.stat(long_path(f["path"]))
            if source.st_size != f["size"] or abs(source.st_mtime - (f.get("mtime") or source.st_mtime)) > 2:
                raise OSError(f"{f['name']} er ændret på kortet – resten er ikke slettet fra kortet")
            with self._lock:
                self.state["current"] = f["name"]
            try:
                os.remove(long_path(f["path"]))
            except PermissionError:
                log.warning("%s is in use and stays on the card", f["path"])
                with self._lock:
                    self.state["kept"] += 1
                continue
            self._log({"deleted": f["name"], "at": self._wall()})
            with self._lock:
                self.state["deleted"] += 1
            self._progress()

    def _make_durable(self) -> None:
        """Before anything leaves the card: every copy made here is flushed to the disk (data,
        size, directory entry), and the disk's own write cache as far as Windows allows, so a
        crash or power cut right after cannot take a copy whose original is already gone."""
        for path in self._written:
            with open(long_path(path), "r+b") as fh:
                os.fsync(fh.fileno())
        try:
            self._flush_volume(self.target)
        except Exception:
            log.debug("volume flush failed", exc_info=True)

    # -- the manifest: a crash-proof record of what was verified and deleted -----------------
    def _open_manifest(self, folder: str | None) -> Any:
        if not folder:
            return None
        try:
            os.makedirs(folder, exist_ok=True)
            stamp = datetime.fromtimestamp(self._wall()).strftime("%Y-%m-%d %H%M%S")
            name = f"{stamp} {self.card['camera']} {self.card['drive'].rstrip(':')}.jsonl"
            return open(os.path.join(folder, name), "a", encoding="utf-8")
        except OSError:
            log.warning("Could not create the import log", exc_info=True)
            return None

    def _log(self, record: dict[str, Any]) -> None:
        fh = self._manifest
        if fh is None:
            return
        try:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        except (OSError, ValueError):
            log.warning("Could not write the import log", exc_info=True)

    def _one(self, f: dict[str, Any]) -> str:
        final = ntpath.join(self.target, f["name"])
        part = final + PART_SUFFIX
        if os.path.exists(long_path(final)):
            raise FileExistsError(f"{f['name']} findes allerede i målmappen")
        try:
            for attempt in (1, 2):
                self._remove(part)  # left over from an interrupted import, or the failed attempt
                with self._lock:
                    self.state.update(phase="copy", state="copying", current=f["name"])
                copied_before = self.state["copied"]
                source_hash = self._copy(f["path"], part)
                with self._lock:
                    self.state.update(phase="verify", state="verifying")
                verified_before = self.state["verified"]
                check, again = self._verify(part, f["path"])
                if check == source_hash == again:
                    break
                card_flaky = again != source_hash
                log.warning("Verification of %s failed (attempt %d, %s)", final, attempt,
                            "the card read differently twice" if card_flaky else "the copy differs")
                with self._lock:   # count this file again
                    self.state["copied"] = copied_before
                    self.state["verified"] = verified_before
                if attempt == 2:
                    if card_flaky:
                        raise OSError(f"{f['name']} blev læst forskelligt to gange fra kortet – kortet "
                                      "eller kortlæseren kan være defekt")
                    raise OSError(f"Kopien af {f['name']} er ikke identisk med kortet")
            st_time = f.get("mtime")
            if st_time:
                os.utime(long_path(part), (st_time, st_time))
            os.rename(long_path(part), long_path(final))   # never replaces an existing file
        except BaseException:
            self._remove(part)
            raise
        return source_hash

    def _verify(self, copy: str, source: str) -> tuple[str, str]:
        """Read the copy back from the disk AND the card file once more, both past the Windows
        file cache and at the same time (different devices: the card read costs no extra
        time). A card or card reader that returns different data on a second read is caught
        too - the copy would otherwise faithfully hold the bad data and still "match"."""
        result: dict[str, Any] = {}

        def stop_on_cancel(_n: int) -> None:
            if self._cancel.is_set():
                raise ImportCancelled()

        def reread() -> None:
            try:
                result["hash"] = (self._hash_check(source, stop_on_cancel)
                                  or hash_buffered(source, stop_on_cancel))
            except BaseException as exc:   # noqa: BLE001 - re-raised below
                result["error"] = exc

        thread = threading.Thread(target=reread, name="import-reread", daemon=True)
        thread.start()
        try:
            check = self._hash_check(copy, self._verified)
            if check is None:
                check = hash_buffered(copy, self._verified)
        finally:
            thread.join()
        if "error" in result:
            raise result["error"]
        return check, result["hash"]

    def _copy(self, source: str, part: str) -> str:
        """Copy ``source`` to ``part``: a reader thread reads + hashes, this thread writes."""
        chunks: queue.Queue[Any] = queue.Queue(maxsize=4)
        stop = threading.Event()
        digest = hashlib.sha1()

        def put(item: Any) -> None:
            while not stop.is_set():
                try:
                    chunks.put(item, timeout=0.2)
                    return
                except queue.Full:
                    continue

        def reader() -> None:
            try:
                with open(long_path(source), "rb", buffering=0) as fh:
                    while not stop.is_set():
                        chunk = fh.read(CHUNK)
                        if chunk:
                            digest.update(chunk)
                        put(chunk)
                        if not chunk:
                            return
            except BaseException as exc:   # noqa: BLE001 - handed to the writer
                put(exc)

        thread = threading.Thread(target=reader, name="import-read", daemon=True)
        thread.start()
        try:
            with open(long_path(part), "xb", buffering=0) as out:
                while True:
                    if self._cancel.is_set():
                        raise ImportCancelled()
                    item = chunks.get()
                    if isinstance(item, BaseException):
                        raise item
                    if not item:
                        break
                    view = memoryview(item)
                    while view:
                        written = out.write(view)
                        view = view[written:]
                    self._copied(len(item))
                os.fsync(out.fileno())
        finally:
            stop.set()
            thread.join(5)
        return digest.hexdigest()

    def _copied(self, n: int) -> None:
        with self._lock:
            self.state["copied"] += n
        self._progress()

    def _read(self, n: int) -> None:
        """A chunk of a card file read for comparing (counts like copying)."""
        if self._cancel.is_set():
            raise ImportCancelled()
        self._copied(n)

    def _verified(self, n: int) -> None:
        if self._cancel.is_set():
            raise ImportCancelled()
        with self._lock:
            self.state["verified"] += n
        self._progress()

    def _progress(self, force: bool = False) -> None:
        now = self._clock()
        with self._lock:
            done = self.state["copied"] + self.state["verified"]
            self._samples.append((now, done))
            while len(self._samples) > 2 and now - self._samples[0][0] > 5.0:
                self._samples.pop(0)
            first_t, first_done = self._samples[0]
            if now - first_t >= 0.5:
                self.state["speed"] = (done - first_done) / (now - first_t) / 2  # bytes of card/s
            speed = self.state["speed"]
            left = 2 * self.state["bytes_total"] - done
            self.state["eta_s"] = round(left / (2 * speed)) if speed > 0 else None
            due = force or now - self._last_publish >= PUBLISH_EVERY_S
            if due:
                self._last_publish = now
                snapshot = dict(self.state)
        if due:
            self._publish(snapshot)

    def _finish(self, state: str, error: str | None = None) -> None:
        with self._lock:
            self.state.update(state=state, error=error, current=None, finished=self._wall(), eta_s=None)
        self._progress(force=True)

    @staticmethod
    def _remove(path: str) -> None:
        try:
            os.remove(long_path(path))
        except FileNotFoundError:
            pass
        except OSError:
            log.warning("Could not remove %s", path, exc_info=True)


def _error_text(exc: BaseException, drive: str) -> str:
    winerror = getattr(exc, "winerror", None)
    if winerror in (112, 39) or getattr(exc, "errno", None) == 28:
        return MSG_DISK_FULL
    filename = str(getattr(exc, "filename", "") or "")
    if winerror in (2, 3, 21, 1167, 433, 1006, 55) and filename.replace("\\\\?\\", "").upper().startswith(drive.upper()):
        return MSG_CARD_GONE
    if isinstance(exc, FileExistsError) and exc.args:
        return str(exc.args[0])
    if isinstance(exc, OSError) and exc.args and isinstance(exc.args[0], str) and not winerror:
        return exc.args[0]
    return f"Overførslen stoppede: {exc.strerror if isinstance(exc, OSError) and exc.strerror else exc}"


# ------------------------------------------------------------------------------ the helper

class Importer:
    """Watches for camera cards and runs imports (one at a time)."""

    def __init__(self, cfg: Config, bus: EventBus, indexer: Any, bridge: Any = None,
                 tracker: Any = None, controller: Any = None, *,
                 list_volumes: Callable[..., list[dict]] = winfs.list_volumes,
                 call_with_timeout: Callable[..., tuple[str, Any]] = winfs.call_with_timeout,
                 disk_usage: Callable[[str], Any] = shutil.disk_usage,
                 open_folder: Callable[..., bool] = winui.open_folder,
                 history_path: str | None = None,
                 hash_check: Callable[[str, Callable[[int], None]], str | None] = hash_uncached,
                 flush_volume: Callable[[str], bool] = flush_volume_cache,
                 wall: Callable[[], float] = time.time) -> None:
        self.cfg = cfg
        self.bus = bus
        self.indexer = indexer
        self.bridge = bridge
        self.tracker = tracker
        self.controller = controller
        self._list_volumes = list_volumes
        self._call = call_with_timeout
        self._disk_usage = disk_usage
        self._open_folder = open_folder
        self._history_path = history_path or os.path.join(app_dir(), "imports.json")
        self._hash_check = hash_check
        self._flush_volume = flush_volume
        self._wall = wall
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._cards: dict[str, dict[str, Any]] = {}      # id → card (with private "_files")
        self._not_cards: dict[str, str] = {}             # serial → drive of volumes without media
        self._job: ImportJob | None = None
        self._last_job: dict[str, Any] | None = None
        self._history = self._load_history()

    # -- lifecycle -------------------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._watch, name="card-watcher", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        job = self._job
        if job is not None:
            job.cancel()
            job.join(timeout)
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout)

    def _watch(self) -> None:
        first = True
        while not self._stop.is_set():
            try:
                self.poll(announce=not first)
            except Exception:
                log.exception("card watcher failed")
            first = False
            self._stop.wait(POLL_S)

    # -- cards -----------------------------------------------------------------------------
    def poll(self, announce: bool = True) -> None:
        """One look at the drives: new cards are analysed (and announced), gone ones dropped."""
        if not self.cfg.get("import_enabled", True):
            if self._cards:
                with self._lock:
                    self._cards.clear()
                self._publish_cards()
            return
        volumes = [v for v in self._list_volumes(timeout=3.0)
                   if v.get("serial") and not v.get("stale") and not v.get("is_system")
                   and (v.get("drive_type") == 2 or v.get("hotplug"))]
        present = {f"{str(v['serial']).upper()}@{v['drive']}": v for v in volumes}
        changed = False
        with self._lock:
            for card_id in [c for c in self._cards if c not in present]:
                del self._cards[card_id]
                changed = True
            for key in [k for k in self._not_cards if k not in present]:
                del self._not_cards[key]
            new = [(k, v) for k, v in present.items() if k not in self._cards and k not in self._not_cards]
        for key, vol in new:
            card = self._analyse(key, vol)
            with self._lock:
                if card is None:
                    self._not_cards[key] = vol["drive"]
                    continue
                self._cards[key] = card
            changed = True
            log.info("Card in %s: %s, %d files", vol["drive"], card["camera"], card["files"])
            if announce:
                self._announce(card)
        if changed:
            self._publish_cards()

    def _analyse(self, key: str, vol: dict[str, Any]) -> dict[str, Any] | None:
        root = str(vol.get("root") or (vol["drive"] + "\\"))
        status, folders = self._call(f"card:{vol['drive']}", lambda: detect_card(root), FS_TIMEOUT_S)
        if status != "ok" or not folders:
            return None
        status, files = self._call(f"card:{vol['drive']}", lambda: card_files(folders), 30.0)
        if status != "ok" or not files:
            return None
        kinds = {kind for kind, _ in folders}
        model = camera_model(files, kinds)
        media = [f for f in files if f["media"]]
        first_name = media[0]["name"] if media else files[0]["name"]
        camera = camera_folder(model, first_name, self.cfg.get("import_camera_folders") or [])
        by_time = sorted(media, key=lambda f: f["mtime"] or 0)
        card = {
            "id": key, "drive": vol["drive"], "serial": str(vol["serial"]).upper(),
            "label": vol.get("label") or "", "volume_size": vol.get("size") or 0,
            "kinds": sorted(kinds), "model": model, "camera": camera,
            "folder": folders[0][1],
            "files": len(files), "clips": len(media), "stills": sum(1 for f in files if f["still"]),
            "bytes": sum(f["size"] for f in files),
            "first": recorded_at(by_time[0]) if by_time else None,
            "last": recorded_at(by_time[-1]) if by_time else None,
            "found": self._already_imported(files, str(vol["serial"]).upper()),
            "inserted": self._wall(), "dismissed": False,
            "_files": files, "_folders": folders,
        }
        return card

    def _already_imported(self, files: list[dict[str, Any]], serial: str,
                          copied: tuple[dict[str, Any], str, dict[str, int]] | None = None) -> dict[str, Any]:
        """Where this card's files already are: same name AND size, checked on the disk itself.

        All files count (clips and their XML/BIM files). The index only tells which folders to
        look in - it may be minutes old - and each of those folders is listed now, so a file
        deleted or changed since the last scan never counts. Only a folder that cannot be
        listed now (offline, not answering) is judged by the index. ``copied`` = (project,
        folder, listing) of a folder just imported into, which the index has not seen yet."""
        sizes = {f["name"].casefold(): f["size"] for f in files if not f["duplicate"]}
        media = {f["name"].casefold() for f in files if f["media"] and not f["duplicate"]}
        try:
            rows = self.indexer.find_files(list(sizes)) if sizes else []
        except Exception:
            log.exception("find_files failed")
            rows = []
        folders: dict[str, dict[str, Any]] = {}
        for row in rows:
            name = row["name"].casefold()
            if (row.get("volume_serial") or "").upper() == serial or name not in sizes:
                continue
            entry = folders.setdefault(row["folder"].casefold(), {
                "folder": row["folder"], "online": row.get("online", True), "indexed": {},
                "project": row.get("project") or {"name": ntpath.basename(row["folder"]), "path": row["folder"]}})
            entry["indexed"][name] = row.get("size")
        candidates = sorted(folders.values(),
                            key=lambda e: -sum(1 for n, s in e["indexed"].items() if sizes[n] == s))
        checked: list[tuple[dict[str, Any], str, bool, dict[str, Any]]] = []
        if copied is not None:
            checked.append((copied[0], copied[1], True, copied[2]))
        for entry in candidates[:8]:
            listing = None
            if entry["online"]:
                status, listing = self._call(f"found:{entry['folder'].casefold()}",
                                             lambda f=entry["folder"]: _listing(f), FS_TIMEOUT_S)
                if status != "ok":
                    listing = None
            checked.append((entry["project"], entry["folder"], bool(entry["online"]),
                            entry["indexed"] if listing is None else listing))
        everywhere: set[str] = set()
        projects: dict[str, dict[str, Any]] = {}
        for project, folder, online, listing in checked:
            matched = {n for n, s in sizes.items() if listing.get(n) == s}
            if not matched:
                continue
            everywhere |= matched
            entry = projects.setdefault(project["path"].casefold(), {
                "name": project["name"], "path": project["path"], "online": online,
                "files": set(), "folder": folder, "best": 0})
            entry["files"] |= matched
            if len(matched) > entry["best"]:
                entry["best"], entry["folder"] = len(matched), folder
        ranked = sorted(projects.values(), key=lambda p: -len(p["files"]))
        return {"clips": len(everywhere & media), "files": len(everywhere), "total": len(sizes),
                "complete": bool(sizes) and len(everywhere) == len(sizes),
                "projects": [{"name": p["name"], "path": p["path"], "folder": p["folder"],
                              "clips": len(p["files"] & media), "files": len(p["files"]),
                              "complete": len(p["files"]) == len(sizes), "online": p["online"]}
                             for p in ranked[:5]]}

    def _announce(self, card: dict[str, Any]) -> None:
        """A card went in: show the Import tab (or a message) - also when it is imported already."""
        controller = self.controller
        if controller is not None and self.cfg.get("import_auto_open", True):
            threading.Thread(target=lambda: controller.show_window(reason="card", panel="import"),
                             name="card-show", daemon=True).start()
            return
        found = card["found"]
        clips = card["clips"] or card["files"]
        where = found["projects"][0]["name"] if found["projects"] else ""
        if found["complete"]:
            title, text = "Kortet er allerede overført", f"Alle {clips} klip ligger i {where}"
        else:
            title, text = f"{card['camera']}-kort i {card['drive']}", f"{clips} klip – åbn Projektsøg for at importere dem"
        self.bus.publish("notify", {"title": title, "text": text, "level": "info"})

    def cards(self) -> list[dict[str, Any]]:
        with self._lock:
            return [_public(c) for c in sorted(self._cards.values(), key=lambda c: c["drive"])]

    def _card(self, card_id: str) -> dict[str, Any]:
        with self._lock:
            card = self._cards.get(card_id)
        if card is None:
            raise ValueError(MSG_NO_CARD)
        return card

    def dismiss(self, card_id: str) -> None:
        with self._lock:
            if card_id in self._cards:
                self._cards[card_id]["dismissed"] = True
        self._publish_cards()

    def _publish_cards(self) -> None:
        self.bus.publish("cards", {"cards": self.cards()})

    # -- where to ----------------------------------------------------------------------------
    def options(self, card_id: str) -> dict[str, Any]:
        """Suggested projects and the disks a new project can go on, for one card."""
        card = self._card(card_id)
        suggestions: list[dict[str, Any]] = []
        seen: set[str] = set()

        def add(path: str | None, name: str | None, reason: str, online: bool = True) -> None:
            if not path or path.casefold() in seen:
                return
            seen.add(path.casefold())
            suggestions.append({"path": path, "name": name or ntpath.basename(path), "reason": reason,
                                "online": bool(online)})

        for project in card["found"]["projects"]:
            reason = ("Alle kortets filer ligger her" if project.get("complete")
                      else f"{project['clips']} af kortets klip ligger her")
            add(project["path"], project["name"], reason, project["online"])
        primary = None
        if self.bridge is not None:
            try:
                primary = (self.bridge.state() or {}).get("primary")
            except Exception:
                log.exception("bridge.state failed")
        if isinstance(primary, dict) and primary.get("path"):
            add(primary["path"], primary.get("name"), "Åben i DaVinci Resolve", primary.get("online", True))
        today = date.today().isoformat()
        for item in reversed(self._history.get("imports", [])):
            if datetime.fromtimestamp(item.get("at", 0)).date().isoformat() == today:
                add(item.get("project"), None, "Importeret til i dag")
        folders: list[str] = []
        if self.tracker is not None:
            try:
                folders = self.tracker.folders_on(date.today())
            except Exception:
                log.exception("folders_on failed")
        if folders:
            try:
                named = self.indexer.projects_named(folders)
            except Exception:
                log.exception("projects_named failed")
                named = []
            for project in named:
                add(project["path"], project["name"], "Arbejdet på i dag", project.get("online", True))
        week = self._wall() - 7 * 86400
        for item in reversed(self._history.get("created", [])):
            if item.get("at", 0) >= week:
                add(item.get("path"), None, "Oprettet " + _day_text(item["at"]))
        for s in suggestions:
            s["free"] = self._free(s["path"]) if s["online"] else None
        return {"card": _public(card), "suggestions": suggestions[:8], "disks": self.disks(card["bytes"])}

    def disks(self, needed: int = 0) -> list[dict[str, Any]]:
        """Folders with a project template, where new projects go (with free space)."""
        try:
            templates = self.indexer.templates()
        except Exception:
            log.exception("templates failed")
            templates = []
        out: dict[str, dict[str, Any]] = {}
        for t in templates:
            parent = t.get("parent")
            if not parent or parent.casefold() in out:
                continue
            source = t.get("source") or {}
            free = self._free(parent) if t.get("online") else None
            out[parent.casefold()] = {
                "path": parent, "template": t["path"],
                "name": parent.rstrip("\\").rpartition("\\")[2] or parent,   # also for \\HOST\share
                "host": source.get("host"), "kind": source.get("kind"),
                "disk": source.get("disk_name") or source.get("drive") or source.get("host"),
                "online": bool(t.get("online")), "free": free,
                "fits": free is not None and free >= needed + FREE_MARGIN}
        return sorted(out.values(), key=lambda d: (not d["online"], d["kind"] != "local", not d["fits"],
                                                   -(d["free"] or 0)))

    def _free(self, path: str) -> int | None:
        """Free bytes on the disk of ``path`` (or of its parent, for a folder not made yet)."""
        candidate = path
        for _ in range(3):
            status, usage = self._call(f"free:{candidate.casefold()}",
                                       lambda c=candidate: self._disk_usage(c), FS_TIMEOUT_S)
            if status == "ok" and usage is not None:
                return int(usage.free)
            parent = ntpath.dirname(candidate.rstrip("\\"))
            if not parent or parent == candidate:
                break
            candidate = parent
        return None

    def _fs(self, key: str, fn: Callable[[], Any], timeout: float, message: str) -> Any:
        """Run file system work with a timeout; failures become a ValueError for the user."""
        def run() -> tuple[Any, BaseException | None]:
            try:
                return fn(), None
            except (OSError, ValueError) as exc:
                return None, exc
        status, result = self._call(key, run, timeout)
        if status != "ok" or result is None:
            raise ValueError(message)
        value, exc = result
        if isinstance(exc, ValueError):
            raise exc
        if exc is not None:
            raise ValueError(f"{message}: {getattr(exc, 'strerror', None) or exc}")
        return value

    def plan(self, card_id: str, project: str, *, separate: bool = False) -> dict[str, Any]:
        """Where exactly the clips go in ``project`` and what is new there."""
        card = self._card(card_id)
        project = _clean_dir(project)
        result = self._fs(f"plan:{project.casefold()}", lambda: self._plan(card, project, separate),
                          15.0, "Projektmappen svarer ikke")
        result["free"] = self._free(project)
        result["fits"] = result["free"] is not None and result["free"] >= result["new_bytes"] + FREE_MARGIN
        return result

    def _plan(self, card: dict[str, Any], project: str, separate: bool) -> dict[str, Any]:
        camera = card["camera"]
        exists = os.path.isdir(long_path(project))
        klip = (_child_dir(project, "Klip") if exists else None) or ntpath.join(project, "Klip")
        camera_dir = (_child_dir(klip, camera) if os.path.isdir(klip) else None) or ntpath.join(klip, camera)
        existing = _listing(camera_dir)
        files = card["_files"]
        mine = {f["name"].casefold() for f in files}
        conflicts = sum(1 for f in files if existing.get(f["name"].casefold(), f["size"]) != f["size"])
        others = sum(1 for name in existing if name not in mine
                     and ntpath.splitext(name)[1] in MEDIA_EXTS)
        day_folder = None
        target = camera_dir
        if separate or conflicts:
            n = 2
            while True:
                candidate = ntpath.join(klip, f"{ntpath.basename(camera_dir)} Dag {n}")
                listing = _listing(candidate)
                if not listing or all(listing.get(f["name"].casefold(), f["size"]) == f["size"]
                                      for f in files):
                    break
                n += 1
            target, day_folder = candidate, ntpath.basename(candidate)
            existing = _listing(target)
        new = [f for f in files if f["name"].casefold() not in existing and not f["duplicate"]]
        return {"card": card["id"], "project": project, "project_exists": exists,
                "target": target, "target_exists": os.path.isdir(long_path(target)),
                "camera": camera, "day_folder": day_folder, "separate": bool(day_folder),
                "files": len(files), "new_files": len(new), "new_bytes": sum(f["size"] for f in new),
                "already": len(files) - len(new) - sum(1 for f in files if f["duplicate"]),
                "conflicts": conflicts, "other_media": others,
                "suggest_separate": others > 0 or conflicts > 0}

    # -- new project ------------------------------------------------------------------------
    def create_project(self, root: str, name: str) -> dict[str, Any]:
        """A new project folder under ``root`` (a folder with a template) from the template."""
        name = validate_project_name(name)
        root = _clean_dir(root)
        disk = next((d for d in self.disks() if d["path"].casefold() == root.casefold()), None)
        if disk is None:
            raise ValueError("Vælg en af diskene på listen")
        target = ntpath.join(root, name)
        self._fs(f"create:{target.casefold()}", lambda: self._create(disk["template"], target), 30.0,
                 "Projektmappen kunne ikke oprettes")
        with self._lock:
            self._history.setdefault("created", []).append({"at": self._wall(), "path": target})
            self._save_history()
        self._refresh(target)
        return {"path": target, "name": ntpath.basename(target)}

    def _create(self, template: str, target: str) -> None:
        if os.path.exists(long_path(target)):
            raise ValueError("Der findes allerede en mappe med det navn")
        dirs = list(self.cfg.get("import_project_dirs") or [])
        os.makedirs(long_path(target))
        if os.path.isdir(long_path(template)):
            for current, subdirs, files in os.walk(template):
                rel = os.path.relpath(current, template)
                dest = target if rel == "." else ntpath.join(target, rel)
                os.makedirs(long_path(dest), exist_ok=True)
                for name in files:
                    source = ntpath.join(current, name)
                    try:
                        if os.path.getsize(source) <= TEMPLATE_FILE_MAX:
                            shutil.copy2(long_path(source), long_path(ntpath.join(dest, name)))
                    except OSError:
                        log.warning("Template file %s not copied", source, exc_info=True)
        else:
            for rel in dirs:
                os.makedirs(long_path(ntpath.join(target, rel)), exist_ok=True)

    # -- import -----------------------------------------------------------------------------
    def start_import(self, card_id: str, project: str, *, separate: bool = False,
                     mode: str = "copy") -> dict[str, Any]:
        """Start copying (``copy``), moving (``move``: copy, verify everything, then delete the
        verified files from the card - like Ctrl+X, but checked) or only create the folder and
        open both (``prepare``)."""
        if mode not in ("copy", "prepare", "move"):
            raise ValueError("Ukendt handling")
        card = self._card(card_id)
        plan = self.plan(card_id, project, separate=separate)
        if mode == "prepare":
            target = plan["target"]
            self._fs(f"plan:{target.casefold()}", lambda: os.makedirs(long_path(target), exist_ok=True),
                     15.0, "Mappen kunne ikke oprettes")
            self._open_folder(card["folder"], activate=False)
            self._open_folder(target, activate=True)
            self._record(card, plan, "prepare", 0, 0)
            self._refresh(target)
            return {"ok": True, "target": target}
        there = self._fs(f"plan:{plan['target'].casefold()}", lambda: _listing(plan["target"]), 15.0,
                         "Projektmappen svarer ikke")
        files = [f for f in card["_files"] if f["name"].casefold() not in there and not f["duplicate"]]
        present = [f for f in card["_files"]
                   if not f["duplicate"] and there.get(f["name"].casefold()) == f["size"]] if mode == "move" else []
        if not files and not present:
            raise ValueError("Alle klip ligger der allerede")
        if files and not plan["fits"]:
            free = plan["free"]
            raise ValueError(f"{MSG_DISK_FULL}: kortet fylder {_gb(plan['new_bytes'])}, "
                             f"der er {_gb(free) if free is not None else 'ukendt'} fri")
        with self._lock:
            if self._job is not None and self._job.running:
                raise ValueError(MSG_BUSY)
            job = ImportJob(_public(card), files, plan["target"], plan["project"],
                            self._publish_job, self._job_done, present=present, move=mode == "move",
                            manifest_dir=os.path.join(os.path.dirname(self._history_path), "imports"),
                            hash_check=self._hash_check, flush_volume=self._flush_volume, wall=self._wall)
            self._job = job
        job.start()
        return job.snapshot()

    def cancel(self) -> None:
        job = self._job
        if job is not None:
            job.cancel()

    def job(self) -> dict[str, Any] | None:
        job = self._job
        return job.snapshot() if job is not None else self._last_job

    def _publish_job(self, state: dict[str, Any]) -> None:
        self.bus.publish("import", state)

    def _job_done(self, job: ImportJob) -> None:
        state = job.snapshot()
        with self._lock:
            self._last_job = state
            card = self._cards.get(job.card["id"])
        self._refresh(job.target)
        if card is not None and (state["state"] == "done" or state["deleted"]):
            if state["deleted"]:
                self._rescan_card(card)          # the moved files are gone from the card now
            try:
                listing = _listing(job.target)
            except OSError:
                listing = {}
            project = {"name": state["project_name"], "path": job.project}
            card["found"] = self._already_imported(card["_files"], card["serial"],
                                                   (project, job.target, listing))
            self._publish_cards()
        if state["state"] == "done":
            self._record(job.card, {"project": job.project, "target": job.target}, state["mode"],
                         state["files_done"], state["bytes_total"])
            if state["mode"] == "move":
                kept = f" {state['kept']} filer var i brug og ligger stadig på kortet." if state["kept"] else ""
                title = "Kortet er flyttet"
                text = (f"{state['deleted']} filer ({_gb(state['bytes_total'])}) er kopieret, kontrolleret "
                        f"og slettet fra kortet – de ligger i {state['project_name']}.{kept}")
            else:
                title = "Kortet er overført"
                text = (f"{state['files_done']} filer ({_gb(state['bytes_total'])}) er kopieret og "
                        f"kontrolleret i {state['project_name']}. Kortet kan tages ud.")
            self.bus.publish("notify", {"title": title, "text": text, "level": "info"})
        elif state["state"] == "failed":
            nothing = " Intet er slettet fra kortet." if state["mode"] == "move" and not state["deleted"] else ""
            self.bus.publish("notify", {"title": "Overførslen stoppede",
                                        "text": (state["error"] or "") + nothing, "level": "warn"})

    def _rescan_card(self, card: dict[str, Any]) -> None:
        """List the card again (after a move): what is left on it."""
        folders = card.get("_folders") or []
        status, files = self._call(f"card:{card['drive']}", lambda: card_files(folders), 30.0)
        if status != "ok" or files is None:
            return
        card.update(_files=files, files=len(files), clips=sum(1 for f in files if f["media"]),
                    stills=sum(1 for f in files if f["still"]), bytes=sum(f["size"] for f in files))

    def history(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            return list(reversed(self._history.get("imports", [])))[:limit]

    # -- helpers ----------------------------------------------------------------------------
    def _record(self, card: dict[str, Any], plan: dict[str, Any], mode: str, files: int, size: int) -> None:
        with self._lock:
            self._history.setdefault("imports", []).append({
                "at": self._wall(), "mode": mode, "camera": card["camera"], "model": card.get("model"),
                "serial": card.get("serial"), "project": plan["project"], "target": plan["target"],
                "files": files, "bytes": size})
            self._save_history()

    def _refresh(self, path: str) -> None:
        try:
            self.indexer.refresh_path(path)
        except Exception:
            log.exception("refresh_path failed")

    def _load_history(self) -> dict[str, Any]:
        try:
            with open(self._history_path, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_history(self) -> None:
        for key in ("imports", "created"):
            del self._history.setdefault(key, [])[:-HISTORY_KEEP]
        tmp = self._history_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._history, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, self._history_path)
        except OSError:
            log.warning("Could not save the import history", exc_info=True)


def _public(card: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in card.items() if not k.startswith("_")}


def _clean_dir(path: str) -> str:
    if not isinstance(path, str) or not path.strip():
        raise ValueError("Vælg en projektmappe")
    text = path.strip().replace("/", "\\")
    if not (re.match(r"^[A-Za-z]:\\", text) or text.startswith("\\\\")):
        raise ValueError("Ugyldig sti")
    return text.rstrip("\\") if len(text) > 3 else text


def _gb(size: int | None) -> str:
    """Like Explorer and the UI: 1024-based, Danish decimal comma ("72,4 GB")."""
    if size is None:
        return "–"
    return f"{size / 1024 ** 3:.1f} GB".replace(".", ",")


def _day_text(at: float) -> str:
    day = datetime.fromtimestamp(at).date()
    delta = (date.today() - day).days
    return "i dag" if delta == 0 else "i går" if delta == 1 else f"for {delta} dage siden"
