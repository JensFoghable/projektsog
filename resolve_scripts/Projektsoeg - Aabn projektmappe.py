r"""Projektsoeg - open the folder of the project that is open in DaVinci Resolve.

DaVinci Resolve menu script (Workspace > Scripts > Utility), see SPEC.md sections 9 and 15.9:

1. Asks the running Projektsoeg app (instance.json -> HTTP API) to open it - but only when
   the app's cached state (GET /api/resolve, answered at once) describes the project open in
   Resolve and its media pool as it is now (same number of clips): the folder of an older
   walk can be wrong after clips were imported. Then POST /api/resolve/open names the
   project (name, database, unique id), so the app never opens the folder of another project
   it still has in mind (it answers without a folder then). After an import the app is only
   told to walk the media pool again (POST /api/resolve/refresh, never waited for).
2. When the app is not running or names no folder (other project, only a name guess, no
   match, an older walk), derives the folder from the media pool's clip paths itself: every
   project is a copy of the "1. KUNDENAVN" template, so the project folder is the one holding
   "Klip", "Musik", "Grafik", ... (template-folder heuristic), confirmed by listing it. The
   folder most clips come from wins.

Runs inside Resolve (``resolve`` global) and externally, e.g. with
C:\Program Files\Blackmagic Design\DaVinci Resolve\ResolvePython\ResolvePython.exe (it then
connects to Resolve first, so the app gets the same checks).
With PROJEKTSOG_DRY_RUN=1 it prints what it would open instead of opening anything.

Standalone on purpose (Resolve runs it without the app on sys.path) and pure ASCII (Danish
text uses \u escapes), so it runs whatever encoding the host reads it with.
"Projektsoeg - Aabn projektmappe.py" is a byte-identical copy under an ASCII file name.
"""

from __future__ import annotations

import ctypes
import http.client
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from collections.abc import Callable, Mapping
from ctypes import wintypes
from typing import Any

TITLE = "Projekts\u00f8g"
APP_DIR = "Projektsog"
DRY_RUN_ENV = "PROJEKTSOG_DRY_RUN"
HTTP_TIMEOUT_S = 15.0
REWALK_TIMEOUT_S = 0.5  # the app only has to receive "walk again"; its walk is never waited for
LIST_TIMEOUT_S = 5.0
WALK_MAX_SECONDS = 20.0
WALK_MAX_CLIPS = 50_000
CREATE_NO_WINDOW = 0x08000000
MESSAGE_BOX_FLAGS = 0x00050030  # MB_OK | MB_ICONWARNING | MB_SETFOREGROUND | MB_TOPMOST

# projektsog.config defaults for project_template_dirs / project_min_template_dirs; the app's
# config.json overrides them.
DEFAULT_TEMPLATE_DIRS = (
    "Final", "Grafik", "Klip", "Logo", "Musik", "Music", "Project", "Projekt", "Speak",
    "Tekst", "SFX", "Stills", "Raw", "R\u00e5materiale", "Lydmix", "Font", "Export",
)
DEFAULT_MIN_TEMPLATE_DIRS = 2

MSG_NOT_RUNNING = "DaVinci Resolve k\u00f8rer ikke."
MSG_NO_CONNECTION = ("Ingen forbindelse til DaVinci Resolve. Sl\u00e5 ekstern scripting til: "
                     "Preferences \u25b8 System \u25b8 General \u25b8 External scripting using = "
                     "Local.")
MSG_NO_PROJECT = "Der er ikke \u00e5bnet et projekt i DaVinci Resolve."
MSG_NO_FOLDER = "Ingen projektmappe fundet for \u2018{project}\u2019."
MSG_UNREACHABLE = ("Projektmappen \u2018{folder}\u2019 kan ikke n\u00e5s lige nu \u2013 er disken "
                   "tilsluttet, og er computeren t\u00e6ndt?")
MSG_OPEN_FAILED = "Mappen \u2018{folder}\u2019 kunne ikke \u00e5bnes."
MSG_OPENED = "\u00c5bner {folder}"
MSG_DRY_OPEN = "[TEST] Ville \u00e5bne: {folder}"


# --------------------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------------------

def say(text: str) -> None:
    """Print to the Resolve console or the terminal; never fails (no stdout, narrow encoding)."""
    stream = sys.stdout
    if stream is None:
        return
    try:
        print(text, file=stream)
    except UnicodeEncodeError:
        print(text.encode("ascii", "backslashreplace").decode("ascii"), file=stream)


def show_message(text: str, dry_run: bool) -> None:
    """Tell the user: printed, and in a message box unless this is a dry run."""
    say(text)
    if dry_run:
        return
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.MessageBoxW.argtypes = (wintypes.HANDLE, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                   wintypes.UINT)
    user32.MessageBoxW.restype = ctypes.c_int
    user32.MessageBoxW(None, text, TITLE, MESSAGE_BOX_FLAGS)


# --------------------------------------------------------------------------------------
# The app (instance.json + HTTP API)
# --------------------------------------------------------------------------------------

def app_dir(environ: Mapping[str, str]) -> str:
    base = environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
    return os.path.join(base, APP_DIR)


def read_json(path: str) -> dict[str, Any] | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def request_json(url: str, body: bytes | None, opener: Any = None,
                 timeout: float = HTTP_TIMEOUT_S) -> Any:
    """GET (``body`` None) or POST ``url``; the decoded JSON answer, None without a usable one."""
    request = urllib.request.Request(
        url, data=body, method="GET" if body is None else "POST",
        headers={"X-Projektsog": "1", "Content-Type": "application/json"})
    # Never through a proxy: the app only listens on 127.0.0.1.
    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            return json.loads(response.read(1_000_000).decode("utf-8"))
    except (OSError, ValueError, http.client.HTTPException):
        return None


def allow_foreground(pid: Any) -> None:
    """Let the app bring Explorer to the front (this script runs in the foreground app)."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.AllowSetForegroundWindow.argtypes = (wintypes.DWORD,)
    user32.AllowSetForegroundWindow.restype = wintypes.BOOL
    user32.AllowSetForegroundWindow(pid)


def project_identity(resolve: Any) -> dict[str, str]:
    """{"project": name, "database": DbName, "uid": Project.GetUniqueId()} of the project open
    in Resolve, as far as known ({} without a scripting object or an open project). Read-only
    getters only."""
    if resolve is None:
        return {}
    try:
        manager = resolve.GetProjectManager()
        project = manager.GetCurrentProject() if manager is not None else None
        if project is None:
            return {}
        name = project.GetName()
        database = manager.GetCurrentDatabase()
    except Exception:  # a scripting call failed: the app decides without the identity
        return {}
    identity = {}
    if isinstance(name, str):
        identity["project"] = name
    db_name = database.get("DbName") if isinstance(database, dict) else None
    if isinstance(db_name, str) and db_name:
        identity["database"] = db_name
    try:
        uid = project.GetUniqueId()  # tells same-named projects apart
    except Exception:
        uid = None
    if isinstance(uid, str) and uid:
        identity["uid"] = uid
    return identity


def same_project(state: Mapping[str, Any], identity: Mapping[str, str]) -> bool:
    """Is the app's Resolve state about the project named by ``identity``? (The state names
    project and database; the unique id is checked by the app itself.)"""
    return all(state.get(key) == identity[key] for key in ("project", "database")
               if key in identity)


def app_mapping(state: Mapping[str, Any], identity: Mapping[str, str],
                clip_count: int | None) -> str:
    """How the app's Resolve state relates to the project open in Resolve: "current" (it maps
    this project's media pool as it is now), "other" (another project, or none yet),
    "pending" (the app is still mapping it) or "stale" (clips were imported or removed since
    the app walked the media pool: its folder may be the wrong one)."""
    if not same_project(state, identity):
        return "other"
    if state.get("updated") is None:
        return "pending"
    if clip_count is not None and state.get("clip_count") != clip_count:
        return "stale"
    return "current"


def open_via_app(environ: Mapping[str, str], dry_run: bool, opener: Any = None,
                 identity: Mapping[str, str] | None = None,
                 clip_count: int | None = None) -> bool:
    """Let the running app open the folder of the project named by ``identity``.

    ``clip_count`` is the number of clip paths in Resolve's media pool right now (None:
    unknown). Then the app is asked only when its cached state maps this project's media pool
    as it is now; after an import it is told to walk it again (not waited for) instead.

    True when the app handled it (opened the folder, or knows it but cannot open it and said
    why); False when the app is not running, names no folder for the project or has not
    mapped its current media pool (then the media pool fallback decides).
    """
    identity = dict(identity or {})
    instance = read_json(os.path.join(app_dir(environ), "instance.json")) or {}
    port = instance.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
        return False
    base = f"http://127.0.0.1:{port}"
    if dry_run or clip_count is not None:
        state = request_json(base + "/api/resolve", None, opener)
        if not isinstance(state, dict):
            return False
        status = app_mapping(state, identity, clip_count)
        if status != "current":
            if not dry_run:
                if status == "stale":  # so that the app knows the new clips next time
                    request_json(base + "/api/resolve/refresh", b"{}", opener, REWALK_TIMEOUT_S)
            elif status == "other":
                say(f"[TEST] {TITLE} (port {port}) kender endnu ikke projektet "
                    f"\u2018{identity.get('project', '?')}\u2019")
            elif status == "pending":
                say(f"[TEST] {TITLE} (port {port}) er ved at finde projektmappen")
            else:
                say(f"[TEST] {TITLE} (port {port}) har kortlagt {state.get('clip_count')} "
                    f"klip, men mediepuljen har {clip_count} nu")
            return False
    if dry_run:  # read the app's state instead of letting it open anything
        primary = state.get("primary")
        if not isinstance(primary, dict) or not primary.get("path"):
            say(f"[TEST] {TITLE} (port {port}) kender ingen projektmappe til projektet")
            return False
        if primary.get("match") == "name":
            say(f"[TEST] {TITLE} kender kun et muligt match: {primary['path']}")
            return False
        if (primary.get("source") or {}).get("online"):
            say(MSG_DRY_OPEN.format(folder=primary["path"]) + f" (via {TITLE}, port {port})")
        else:
            say(f"[TEST] {TITLE} kender projektmappen {primary['path']}, men den er offline")
        return True
    allow_foreground(instance.get("pid"))
    result = request_json(base + "/api/resolve/open", json.dumps(identity).encode("ascii"),
                          opener)
    if not isinstance(result, dict) or "ok" not in result:
        return False
    if result.get("ok"):
        say(MSG_OPENED.format(folder=result.get("path")))
        return True
    if result.get("path"):  # known folder that cannot be opened (offline disk, ...)
        show_message(result.get("error") or MSG_OPEN_FAILED.format(folder=result["path"]), dry_run)
        return True
    return False


# --------------------------------------------------------------------------------------
# Fallback: find the project folder from the media pool
# --------------------------------------------------------------------------------------

def template_settings(environ: Mapping[str, str]) -> tuple[frozenset[str], int]:
    """(case-folded template sub-folder names, how many make a project folder)."""
    config = read_json(os.path.join(app_dir(environ), "config.json")) or {}
    names = config.get("project_template_dirs")
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        names = DEFAULT_TEMPLATE_DIRS
    min_dirs = config.get("project_min_template_dirs")
    if isinstance(min_dirs, bool) or not isinstance(min_dirs, int) or min_dirs < 1:
        min_dirs = DEFAULT_MIN_TEMPLATE_DIRS
    return frozenset(n.strip().casefold() for n in names if n.strip()), min_dirs


def split_path(path: str) -> tuple[str, list[str]] | None:
    """Split an absolute Windows path into (drive or share root, [components])."""
    p = path.strip().replace("/", "\\")
    if p.startswith("\\\\?\\UNC\\"):
        p = "\\\\" + p[8:]
    elif p.startswith("\\\\?\\"):
        p = p[4:]
    if p.startswith("\\\\"):
        parts = [s for s in p[2:].split("\\") if s]
        if len(parts) < 3:
            return None
        return "\\\\" + parts[0] + "\\" + parts[1], parts[2:]
    if len(p) >= 3 and p[0].isalpha() and p[1:3] == ":\\":
        parts = [s for s in p[3:].split("\\") if s]
        return (p[0].upper() + ":", parts) if parts else None
    return None


def candidate_folders(path: str, template_names: frozenset[str]) -> list[str]:
    """Possible project folders of one clip path, outermost first: every folder (below the
    drive/share root) whose child on the path is named like a template sub-folder."""
    split = split_path(path)
    if split is None:
        return []
    root, parts = split
    dirs = parts[:-1]
    return ["\\".join([root, *dirs[:i]]) for i in range(1, len(dirs))
            if dirs[i].casefold() in template_names]


def list_is_project(folder: str, template_names: frozenset[str], min_dirs: int) -> bool | None:
    """Does ``folder`` hold >= ``min_dirs`` template sub-folders? None when it cannot be listed
    within LIST_TIMEOUT_S (disconnected disk, sleeping computer)."""
    found: list[int] = []

    def count() -> None:
        try:
            with os.scandir(folder) as entries:
                found.append(sum(1 for e in entries
                                 if e.name.casefold() in template_names and e.is_dir()))
        except OSError:
            pass

    worker = threading.Thread(target=count, name="projektsog-list", daemon=True)
    worker.start()
    worker.join(LIST_TIMEOUT_S)
    return found[0] >= min_dirs if found else None


def choose_project_folder(paths: list[str], template_names: frozenset[str], min_dirs: int,
                          is_project: Callable[[str], bool | None] | None = None,
                          ) -> tuple[str | None, int, str | None]:
    """Pick the project folder most clips come from.

    Returns (folder, clips, unreachable): the confirmed project folder with the most clips, or
    (None, 0, <folder>) when a candidate that could not be listed has more clips.
    """
    if is_project is None:
        def is_project(folder: str) -> bool | None:
            return list_is_project(folder, template_names, min_dirs)

    chains: dict[tuple[str, ...], int] = {}
    for path in paths:
        chain = tuple(candidate_folders(path, template_names))
        if chain:
            chains[chain] = chains.get(chain, 0) + 1
    verdicts: dict[str, bool | None] = {}
    confirmed: dict[str, list[Any]] = {}
    unknown: dict[str, list[Any]] = {}
    for chain, clips in sorted(chains.items(), key=lambda item: -item[1]):
        for folder in chain:
            key = folder.casefold()
            if key not in verdicts:
                verdicts[key] = is_project(folder)
            if verdicts[key] is False:
                continue  # not a project: try the next (deeper) candidate
            bucket = confirmed if verdicts[key] else unknown
            bucket.setdefault(key, [folder, 0])[1] += clips
            break
    best = max(confirmed.values(), key=lambda fc: fc[1], default=None)
    missing = max(unknown.values(), key=lambda fc: fc[1], default=None)
    if missing is not None and (best is None or missing[1] > best[1]):
        return None, 0, missing[0]
    return (best[0], best[1], None) if best is not None else (None, 0, None)


def as_list(value: Any) -> list[Any]:
    """Resolve returns lists; older versions returned {1.0: item, ...} dicts."""
    if value is None:
        return []
    if isinstance(value, dict):
        return [value[k] for k in sorted(value)]
    try:
        return list(value)
    except TypeError:
        return []


def clip_file_path(clip: Any) -> str:
    try:
        value = clip.GetClipProperty("File Path")
    except TypeError:  # an API without the (deprecated) single-key form
        value = clip.GetClipProperty()
    if isinstance(value, dict):
        value = value.get("File Path")
    return value.strip() if isinstance(value, str) else ""


def collect_clip_paths(project: Any, clock: Callable[[], float] = time.monotonic) -> list[str]:
    """File paths of the media pool's clips (bounded like the app: 20 s, 50k clips)."""
    pool = project.GetMediaPool()
    root = pool.GetRootFolder() if pool is not None else None
    deadline = clock() + WALK_MAX_SECONDS
    paths: list[str] = []
    seen = 0
    stack = [root] if root is not None else []
    while stack and clock() < deadline:
        folder = stack.pop()
        for clip in as_list(folder.GetClipList()):
            if seen >= WALK_MAX_CLIPS:
                return paths
            seen += 1
            path = clip_file_path(clip)
            if path:
                paths.append(path)
        stack.extend(reversed(as_list(folder.GetSubFolderList())))
    return paths


def live_clip_paths(resolve: Any) -> list[str] | None:
    """The clip paths of the media pool of the project open in Resolve; None without a
    scripting object or an open project, or when a scripting call fails."""
    if resolve is None:
        return None
    try:
        manager = resolve.GetProjectManager()
        project = manager.GetCurrentProject() if manager is not None else None
        return None if project is None else collect_clip_paths(project)
    except Exception:  # a scripting call failed: the app decides without the live media pool
        return None


def resolve_running() -> bool:
    """Is Resolve.exe running? Checked before connecting from outside Resolve."""
    try:
        listing = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Resolve.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, errors="replace", timeout=10,
            creationflags=CREATE_NO_WINDOW).stdout
    except (OSError, subprocess.SubprocessError):
        return True  # unknown: let scriptapp() decide
    return '"resolve.exe"' in listing.casefold()


def get_resolve(script_globals: Mapping[str, Any]) -> tuple[Any, str | None]:
    """The Resolve scripting object, or (None, why not) as a user message."""
    app = script_globals.get("resolve")  # predefined when run from Resolve's Scripts menu
    if app is not None:
        return app, None
    if not resolve_running():
        return None, MSG_NOT_RUNNING
    try:
        import DaVinciResolveScript as dvr  # out of the box in ResolvePython.exe
    except ImportError:
        sys.path.append(os.path.join(os.environ.get("PROGRAMDATA") or r"C:\ProgramData",
                                     "Blackmagic Design", "DaVinci Resolve", "Support",
                                     "Developer", "Scripting", "Modules"))
        try:
            import DaVinciResolveScript as dvr
        except ImportError:
            return None, MSG_NO_CONNECTION
    app = dvr.scriptapp("Resolve")
    return (app, None) if app is not None else (None, MSG_NO_CONNECTION)


def open_via_media_pool(resolve: Any, environ: Mapping[str, str], dry_run: bool, *,
                        is_project: Callable[[str], bool | None] | None = None,
                        paths: list[str] | None = None) -> str | None:
    """Find the project folder from the clip paths (``paths``: already collected) and open it
    (dry run: print it). Returns that folder, or None after telling the user why there is
    none."""
    manager = resolve.GetProjectManager()
    project = manager.GetCurrentProject() if manager is not None else None
    if project is None:
        show_message(MSG_NO_PROJECT, dry_run)
        return None
    name = project.GetName() or ""
    template_names, min_dirs = template_settings(environ)
    if paths is None:
        paths = collect_clip_paths(project)
    folder, clips, unreachable = choose_project_folder(paths, template_names, min_dirs,
                                                       is_project)
    if folder is None:
        show_message(MSG_UNREACHABLE.format(folder=unreachable) if unreachable
                     else MSG_NO_FOLDER.format(project=name), dry_run)
        return None
    if dry_run:
        say(f"[TEST] {name}: {clips} af {len(paths)} klip ligger i {folder}")
        say(MSG_DRY_OPEN.format(folder=folder))
        return folder
    say(MSG_OPENED.format(folder=folder))
    try:
        os.startfile(folder)
    except OSError:
        show_message(MSG_OPEN_FAILED.format(folder=folder), dry_run)
        return None
    return folder


def main(script_globals: Mapping[str, Any], environ: Mapping[str, str] = os.environ) -> None:
    dry_run = environ.get(DRY_RUN_ENV) == "1"
    # The 'resolve' global - or, run externally, a connection made now: the app needs the
    # project's identity and its live media pool as much as the fallback does.
    try:
        resolve, problem = get_resolve(script_globals)
    except Exception:  # the scripting library failed: the app decides without them
        resolve, problem = None, MSG_NO_CONNECTION
    paths = live_clip_paths(resolve)
    if open_via_app(environ, dry_run, identity=project_identity(resolve),
                    clip_count=None if paths is None else len(paths)):
        return
    if resolve is None:
        show_message(problem or MSG_NO_CONNECTION, dry_run)
        return
    open_via_media_pool(resolve, environ, dry_run, paths=paths)


# Resolve runs menu scripts as __main__ (as Blackmagic's own example scripts rely on).
if __name__ == "__main__":
    main(globals())
