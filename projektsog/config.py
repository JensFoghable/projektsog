"""Settings (JSON on disk) and well-known file locations.

All per-machine state lives in ``%LOCALAPPDATA%\\Projektsog``:

    config.json      user settings (this module)
    index.db         SQLite index (projektsog.db)
    instance.json    {"pid": int, "port": int} of the running instance
    logs\\           rotating log files
    edge-profile\\   private Edge profile for the app window
"""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
from typing import Any, Callable

from . import APP_ID

log = logging.getLogger(__name__)

DEFAULTS: dict[str, Any] = {
    "config_version": 1,
    # --- HTTP / UI ---------------------------------------------------------------------
    "port": 47811,                      # first port tried; next free port is used if busy
    "hide_after_open": True,            # hide the search window after opening a folder
    "show_offline": True,               # include results from disconnected drives/shares
    "result_limit": 200,
    "theme": "dark",                    # "dark" | "light" | "system" (follow Windows)
    # --- Global hotkey -----------------------------------------------------------------
    "hotkey": "shift+space",
    "hotkey_enabled": True,
    # exe names (case-insensitive) where a SINGLE press is passed through untouched,
    # e.g. ["Resolve.exe", "Fusion.exe"] to let DaVinci Resolve keep its own Shift+Space.
    # In those apps a quick DOUBLE press (within hotkey_double_tap_ms) still opens Projektsøg.
    "hotkey_passthrough_apps": [],
    # Typing guard: never fire if any other (non-modifier) key went down less than this
    # many ms before the hotkey's main key (protects "Rikke Lindholm" typed fast).
    "hotkey_typing_guard_ms": 300,
    "hotkey_double_tap_ms": 400,
    # Set once the user answered the one-time "Resolve uses Shift+Space too" question.
    "resolve_hotkey_asked": False,
    # --- Locations -----------------------------------------------------------------------
    # Computers whose shares are discovered automatically (own computer is skipped).
    # Empty by default: add your own computers under Indstillinger → "Tilføj computer",
    # e.g. ["STUDIO-PC", "GRAFIK-PC", "MEDIESERVER"].
    "hosts": [],
    # Extra root folders added by hand (local or UNC paths). Always included.
    "extra_roots": [],
    # Volumes whose label matches (case-insensitive) are never indexed.
    "skip_volume_labels": ["Google Drive"],
    # Top-level folders on local volumes that are never root candidates.
    "skip_top_level_dirs": [
        "Windows", "Program Files", "Program Files (x86)", "ProgramData", "Users",
        "$Recycle.Bin", "$RECYCLE.BIN", "System Volume Information", "Recovery", "PerfLogs",
        "$WinREAgent", "$SysReset", "$Windows.~BT", "$Windows.~WS", "Windows.old",
        "Config.Msi", "MSOCache", "OneDriveTemp", "Intel", "AMD", "NVIDIA", "inetpub",
        "Documents and Settings", "xampp", "Temp", "tmp",
    ],
    # Top-level folders of camera cards: never root candidates on hot-plug volumes (cards pass
    # through – the import helper copies them; their clips are found in the projects).
    "skip_card_dirs": ["XDROOT", "PRIVATE", "DCIM", "MP_ROOT", "AVF_INFO", "CONTENTS"],
    # Folder names (case-insensitive, exact) that are skipped everywhere while scanning.
    "exclude_dir_names": [
        "$RECYCLE.BIN", "System Volume Information", ".Trashes", ".Trash", ".Spotlight-V100",
        ".fseventsd", ".TemporaryItems", ".DocumentRevisions-V100", ".git", ".svn", ".hg",
        "node_modules", "__pycache__", ".venv", "venv", "site-packages", ".cache",
        ".pytest_cache", ".mypy_cache", "CacheClip", "OptimizedMedia", "ProxyMedia",
        "DerivedDataCache", "Intermediate",
    ],
    # File names (case-insensitive, exact) and globs that are skipped.
    "exclude_file_names": ["Thumbs.db", "desktop.ini", ".DS_Store", "ehthumbs.db", ".localized"],
    "exclude_file_globs": ["._*", "~$*", "*.tmp", "*.crdownload", "*.partial"],
    # --- Project detection -------------------------------------------------------------
    # A folder is a PROJECT when at least ``project_min_template_dirs`` of its direct
    # sub-folders have one of these names (case-insensitive).
    "project_template_dirs": [
        "Final", "Grafik", "Klip", "Logo", "Musik", "Music", "Project", "Projekt", "Speak",
        "Tekst", "SFX", "Stills", "Raw", "Råmateriale", "Lydmix", "Font", "Export",
    ],
    "project_min_template_dirs": 2,
    # Folder names (regex on fold(name)) that are empty project TEMPLATES ("1. KUNDENAVN").
    "template_folder_regex": r"^\d+ kundenavn$",
    # On hot-plug (USB/SD) volumes a candidate is also auto-included when the probe sees
    # any file with one of these extensions (raw-footage disks have no template folders).
    "media_exts": [
        "mxf", "mov", "mp4", "mts", "m2ts", "braw", "r3d", "crm", "ari", "avi", "mkv",
        "insv", "lrv", "wav", "bwf", "mp3", "aif", "aiff", "drp", "prproj", "aep", "psd",
        "exr", "dpx", "arw", "cr3", "dng", "nef",
    ],
    # --- Image sequences ---------------------------------------------------------------
    # Files like render_0001.exr … render_4500.exr in one folder are stored as ONE entry.
    "sequence_exts": [
        "exr", "dpx", "png", "tif", "tiff", "jpg", "jpeg", "tga", "bmp", "gpr", "dng",
        "cin", "hdr", "webp", "heic", "sgi", "rgb", "iff", "jp2",
    ],
    "sequence_min_files": 20,
    # --- Scheduling ---------------------------------------------------------------------
    "scan_interval_local_min": 3,       # rescan local sources at least this often
    "scan_interval_network_min": 10,    # base interval for network sources (adaptive, see SPEC)
    "full_rescan_hours": 24,            # force a full (non-incremental) rescan this often
    "discovery_interval_local_s": 4,    # poll for added/removed drives
    "discovery_interval_network_s": 60, # poll hosts for shares
    "max_parallel_scans": 4,
    # --- DaVinci Resolve ---------------------------------------------------------------
    "resolve_enabled": True,
    "resolve_poll_s": 3,
    # What happens when the project open in Resolve changes:
    #   "off"    nothing
    #   "notify" show it in the app + a tray notification
    #   "open"   also open the project folder in Explorer automatically
    "resolve_follow": "notify",
    # --- Time tracking (projektsog/timetrack.py) -----------------------------------------
    # Counts time per Resolve project and page while Resolve is in front (and on music and AI
    # sites below while a project is open). A pause longer than time_idle_minutes is not counted;
    # a moving playhead counts as activity.
    "time_tracking_enabled": True,
    "time_idle_minutes": 10,
    # Browser tab titles (case-insensitive substrings) that count as "Musik/lyd" work.
    "time_music_sites": [
        "Artlist", "Epidemic Sound", "Musicbed", "Soundstripe", "PremiumBeat", "Envato",
        "Motion Array", "Audio Network", "Freesound", "Soundsnap",
    ],
    # Browser tab titles that count as "AI-video/billeder" work (generating AI video and images).
    "time_ai_sites": ["Higgsfield"],
    "time_round_minutes": 15,          # default rounding offered in the report (0 = none)
    # A project with less than this in the report's period is left out of the overview and the
    # export (a project opened by mistake, or for a moment by a Claude session) – 0 shows all.
    "time_min_minutes": 3,
    # Import helper (importer.py): camera cards (XDROOT, PRIVATE\M4ROOT, DJI, GoPro) are offered
    # for import into a project's Klip\<camera> folder.
    "import_enabled": True,
    "import_auto_open": True,          # show the window when a card with new clips goes in
    # "<model prefix>=<Klip subfolder>": the camera model from the clips' XML picks the folder.
    "import_camera_folders": ["PXW-FX9=FX9", "PXW-FS7=FS7", "ILCE-7SM3=A7S", "ILCE-7M3=A7III",
                              "ILME-FX3=FX3", "ILME-FX6=FX6", "ILME-FX30=FX30", "PXW-FX9V=FX9",
                              "DJI=Drone", "GOPRO=GoPro"],
    # New projects copy the "1. KUNDENAVN" template next to them; without one, these folders.
    "import_project_dirs": ["Final", "Grafik", "Klip\\A7S", "Klip\\Drone", "Klip\\FX9", "Logo",
                            "Musik", "Project", "Speak", "Tekst"],
    # --- Klippe, the pet widget (projektsog/widget.py, web/widget.*) ---------------------
    # A small always-on-top window at the side of the second monitor that follows the time
    # tracking: happy while you work, celebrates milestones, grows with all time logged.
    "widget_enabled": False,
    "widget_on_top": True,
    "widget_monitor": "auto",          # "auto" (the second monitor, else the main one) | "primary"
    "widget_position": "",             # "x,y" where the user dragged it ("" = bottom right)
    "widget_daily_goal_hours": 6,
    "widget_pet_name": "Klippe",
    # Hatched by hand: the pet is at least a baby whatever the hours (the time tracking itself
    # is never touched – it is what is billed).
    "widget_hatched": False,
    # Klippe plays (petplay.py): after this long without mouse and keyboard it may break out of
    # the widget and play with the mouse pointer on that monitor – never during playback in
    # Resolve, a transfer, in full screen or on a locked screen; any touch ends the game.
    "widget_play": True,
    "widget_play_idle_minutes": 5,
    # The phone and the robot crew (SPEC §21): a session that wants to build rings (Windows' call
    # sound), and while it builds a swarm of robots edits beside Klippe on its screen – any touch
    # of mouse or keyboard sends them back into the box.
    "widget_ring": True,
    "widget_crew": True,
    # Office Klippes (SPEC §22.3): Klippe says hello to the other PCs' Klippes on the LAN (UDP) and
    # visits them when it earns a trophy or a delivery is made.
    "widget_kontor": True,
    # The delivery party (SPEC §22.4): a render into Final, or a new file in the project's Final.
    "widget_levering": True,
}

VALID_RESOLVE_FOLLOW = ("off", "notify", "open")
VALID_THEMES = ("dark", "light", "system")
VALID_WIDGET_MONITORS = ("auto", "primary")


# --------------------------------------------------------------------------------------
# Locations
# --------------------------------------------------------------------------------------

def app_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
    path = os.path.join(base, APP_ID)
    os.makedirs(path, exist_ok=True)
    return path


def config_path() -> str:
    return os.path.join(app_dir(), "config.json")


def db_path() -> str:
    return os.path.join(app_dir(), "index.db")


def instance_path() -> str:
    return os.path.join(app_dir(), "instance.json")


def log_dir() -> str:
    path = os.path.join(app_dir(), "logs")
    os.makedirs(path, exist_ok=True)
    return path


def edge_profile_dir() -> str:
    path = os.path.join(app_dir(), "edge-profile")
    os.makedirs(path, exist_ok=True)
    return path


def widget_profile_dir() -> str:
    """Its own Edge profile: the pet widget runs independently of the search window."""
    path = os.path.join(app_dir(), "edge-widget")
    os.makedirs(path, exist_ok=True)
    return path


def hostname() -> str:
    """NetBIOS name of this computer, upper case (e.g. 'STUDIO-PC')."""
    name = os.environ.get("COMPUTERNAME")
    if not name:
        import socket
        name = socket.gethostname().split(".")[0]
    return name.upper()


# --------------------------------------------------------------------------------------
# Config object
# --------------------------------------------------------------------------------------

class Config:
    """Thread-safe settings store backed by a JSON file.

    Unknown keys found in the file are preserved.  Missing keys fall back to DEFAULTS.
    """

    def __init__(self, path: str | None = None) -> None:
        self.path = path or config_path()
        self._lock = threading.RLock()
        self._data: dict[str, Any] = copy.deepcopy(DEFAULTS)
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        self.load()

    # -- persistence -----------------------------------------------------------------
    def load(self) -> None:
        with self._lock:
            data = copy.deepcopy(DEFAULTS)
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    stored = json.load(fh)
                if isinstance(stored, dict):
                    data.update(stored)
            except FileNotFoundError:
                pass
            except Exception:  # corrupt file: keep defaults, keep a backup
                log.exception("Could not read %s – using defaults", self.path)
                try:
                    os.replace(self.path, self.path + ".bad")
                except OSError:
                    pass
            self._data = data

    def save(self) -> None:
        with self._lock:
            tmp = self.path + ".tmp"
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)

    # -- access ------------------------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            if key in self._data:
                return copy.deepcopy(self._data[key])
            if key in DEFAULTS:
                return copy.deepcopy(DEFAULTS[key])
            return default

    def __getitem__(self, key: str) -> Any:
        return self.get(key)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data)

    def update(self, changes: dict[str, Any], save: bool = True) -> dict[str, Any]:
        """Apply ``changes`` (validated), persist, notify listeners. Returns new snapshot."""
        clean = validate(changes)
        with self._lock:
            self._data.update(clean)
            if save:
                self.save()
            snap = copy.deepcopy(self._data)
            listeners = list(self._listeners)
        for cb in listeners:
            try:
                cb(snap)
            except Exception:
                log.exception("config listener failed")
        return snap

    def on_change(self, callback: Callable[[dict[str, Any]], None]) -> None:
        """Register ``callback(snapshot)``.

        Callbacks run synchronously on the thread that called ``update()`` (often an HTTP
        or tray thread). They must only record the change and wake their owner's thread:
        no I/O, no joins, < 10 ms.
        """
        with self._lock:
            self._listeners.append(callback)


def validate(changes: dict[str, Any]) -> dict[str, Any]:
    """Light validation/coercion of user supplied settings. Raises ValueError."""
    out: dict[str, Any] = {}
    for key, value in changes.items():
        if key not in DEFAULTS:
            raise ValueError(f"Ukendt indstilling: {key}")
        default = DEFAULTS[key]
        if isinstance(default, bool):
            if not isinstance(value, bool):
                raise ValueError(f"{key} skal være sand/falsk")
        elif isinstance(default, int):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{key} skal være et tal")
            value = int(value)
            if value < 0:
                raise ValueError(f"{key} må ikke være negativ")
        elif isinstance(default, str):
            if not isinstance(value, str):
                raise ValueError(f"{key} skal være tekst")
            value = value.strip()
        elif isinstance(default, list):
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ValueError(f"{key} skal være en liste af tekster")
            value = [v.strip() for v in value if v.strip()]
        if key == "resolve_follow" and value not in VALID_RESOLVE_FOLLOW:
            raise ValueError("resolve_follow skal være off, notify eller open")
        if key == "theme" and value not in VALID_THEMES:
            raise ValueError("theme skal være dark, light eller system")
        if key == "widget_monitor" and value not in VALID_WIDGET_MONITORS:
            raise ValueError("widget_monitor skal være auto eller primary")
        if key == "widget_daily_goal_hours" and not (1 <= value <= 16):
            raise ValueError("Dagens mål skal være mellem 1 og 16 timer")
        if key == "widget_pet_name":
            value = value[:20] or "Klippe"
        if key == "widget_play_idle_minutes" and not (1 <= value <= 60):
            raise ValueError("Pausen før legen skal være mellem 1 og 60 minutter")
        if key == "time_idle_minutes" and not (1 <= value <= 120):
            raise ValueError("Pausegrænsen skal være mellem 1 og 120 minutter")
        if key == "time_round_minutes" and value > 240:
            raise ValueError("Afrunding må højst være 240 minutter")
        if key == "time_min_minutes" and value > 60:
            raise ValueError("Korte besøg må højst være 60 minutter")
        if key == "port" and not (1024 <= value <= 65535):
            raise ValueError("port skal være mellem 1024 og 65535")
        out[key] = value
    return out
