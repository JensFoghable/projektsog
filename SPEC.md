# Projektsøg — technical specification (v2)

> This document is the **contract** between all modules. Implementers must follow the
> interfaces, JSON shapes and rules below exactly. If something is still ambiguous, pick the
> simplest behaviour that satisfies the user story and note it in a code comment + your report.
> User-facing text (UI, tray, notifications, error messages shown to the user) is **Danish**.
> Code, comments, logs and identifiers are **English**.
> v2 incorporates a 3-lens design review (UX, Windows robustness, contracts) with measurements
> taken on the development PC (§0).

## 0. User & environment (example; measured 2026-09-30)

> The computer, share, disk, client and project names in this document are **invented
> examples** (`STUDIO-PC`, `Kunder 2026 (STUDIO)`, `Rikke Lindholm` …); IP addresses are from
> the documentation range 192.0.2.0/24. The numbers, timings and observations were measured on
> a real installation of this kind and are kept because the design depends on them.

A small Danish film/TV production company. Projects live in folders spread over several PCs;
each PC shares folders over SMB. The user wants to press **Shift+Space**, type a few letters
and instantly find the project folder wherever it is, then open it in Explorer.
Also: DaVinci Resolve integration ("see where my footage comes from and open that folder").
Portable work disks are swapped during the day and must become searchable automatically.

* This PC: **STUDIO-PC** (Windows 11 Pro 25H2 build 26200, 32 logical CPUs).
  Python 3.14.3 (GIL build) at `C:\Users\<bruger>\AppData\Local\Python\pythoncore-3.14-64\`
  (`pythonw.exe` next to `python.exe`), SQLite 3.50.4 with FTS5 + trigram tokenizer, Edge at
  `C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`. The user's own browser is
  Chrome. No .NET SDK. Other PCs: unknown software → **stdlib only**.
* Input languages: da-DK and en-US (both Danish layout); the default **Left Alt+Shift** language
  switch is active → never synthesise Alt while Shift may be held.
* Explorer may open folders requested by other apps as a **new tab** in an existing window.
  Explorer window titles look like `<folder> – Stifinder`.
* Other PCs: `KLIPPER-PC` (192.0.2.18), `MEDIESERVER` (192.0.2.41 — also hosts the
  DaVinci Resolve PostgreSQL DBs "Kunder 2023–2026 (Projektserver)"), `GRAFIK-PC` (192.0.2.53).
  Admin shares (`D$`) are **not** accessible; only normal shares.
* Project roots the user named:

  | Path | Where | Files / dirs | Full walk |
  |---|---|---|---|
  | `C:\Kunder 2026 (STUDIO)` (share `\\STUDIO-PC\Kunder 2026 (STUDIO)`) | local | 10.9k / 1.5k | 0.4 s |
  | `D:\Forår 2026 RØD` (shared, volume label `Forår 2026 RØD`) | local, slow HDD | 14.9k / 1.7k | 13.8 s cold |
  | `H:\2024 Disk Sølv` (shared, portable USB disk labelled `2024 Disk Sølv`, reports *fixed*) | local | 39k / 0.6k | 6 s cold |
  | `Z:\(Z) Kunder 2026 (STUDIO)` (shared, label `Lokal disk 2`) | local | 66k / 0.5k | 0.6 s |
  | `F:\Kunder 2026 ARKIV` (shared, **exFAT** portable disk labelled `ARKIV`) | local | 83 / 54 | 0.2 s |
  | `\\KLIPPER-PC\Rejsefilm` | network | 16.8k / 0.35k | 1.9 s |
  | `\\KLIPPER-PC\Efterår 2023` | network | 16.9k / 0.6k | 4.3 s |
  | `\\MEDIESERVER\2026Arkiv` | network (StableBit DrivePool, reports NTFS) | 2.6k / 0.2k | 5.2 s |
  | `\\MEDIESERVER\2025Arkiv` | network (DrivePool) | **304k / 20.8k, 16 TB** | **442 s** |
  | `\\GRAFIK-PC\Forår 2026 (HDD)` | network | 15.2k / 0.26k | 5.1 s |
  | `\\GRAFIK-PC\Kunder 2026 (Grafik)` | network | 14.5k / 1.1k | 1.1 s |

  2025Arkiv holds 169k `.png` + 34k `.exr` frames (image sequences), an Unreal project
  (23k `.uasset`) and Python junk. Total ≈ 520k files. DrivePool leaf-dir mtimes are consistent;
  **exFAT directory mtimes are unreliable**.
* Non-project shares also exist: `\\GRAFIK-PC\Økonomi`, `\\GRAFIK-PC\Users`, local
  `C:\Users`, `C:\Github`, `C:\Effects`, `H:\Cache` (Resolve cache), `C:\Unreal kursus`.
  `\\MEDIESERVER\Efterår 2021` probably *is* a project root. Local shares can be **stale**:
  `E:\Gamle Opgaver (E) - Efterår 2021`, `E:\Hele 2022`, `E:\Rejsefilm` are shared but `E:` is
  now a Tascam DR-40 SD card. `I:` is an empty card-reader slot (not ready).
* `GetVolumeInformationW` works locally and over UNC (`\\STUDIO-PC\2024 Disk Sølv\` → serial
  `5E3A0B21` = `H:`). Both DrivePool shares report serial `8C5A3E61` → a serial is **not** a
  unique id for a share.
* `NetShareEnum` level 2 works locally without admin (share → path); level 1 works for remote
  hosts (~5 ms; unknown host fails after ~1.3 s; a powered-off host can block 20–60 s).
* **Folder convention**: every project is a copy of the template `1. KUNDENAVN` containing
  `Final, Grafik, Klip, Logo, Musik, Project, Speak, Tekst` (+ sometimes `SFX, Stills, Lydmix,
  Font, Music, Raw, Råmateriale`). Projects sit at depth 1 under a root or at depth 2 inside a
  client "group" folder (`<root>\Klar Tand 2026\Klar Tand - Silkeborg\…`). Some depth-1 folders
  are loose (`Sound Effects`, `Export Presets`).
* **DaVinci Resolve Studio 21.1** runs on this PC. External scripting works from our Python
  (`import DaVinciResolveScript` + `scriptapp("Resolve")` ≈ 20 ms). Resolve 21.1 bundles its own
  Python 3.14 (`C:\Program Files\Blackmagic Design\DaVinci Resolve\ResolvePython\ResolvePython.exe`),
  so **.py scripts in Workspace ▸ Scripts work out of the box**. User scripts folder:
  `%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility\` (already contains
  the user's `AutoSubs V2.lua` — never touch other files there). Media paths in Resolve are
  **UNC even for local files**, e.g. `\\studio-pc\Kunder 2026 (STUDIO)\Rikke Lindholm\Klip\FX9\FX9_7912.MXF`
  (project "Rikke Lindholm - Testimonial", 167 clips, 1 clip from `C:\Github\undertekster\…srt`).
  Resolve ≥ 20.1 uses **Shift+Space for its effects search** (Fusion: Select Tool dialog).
* Measured: with 1/2/4 CPU-busy Python threads in a process, a thread waking from a native wait
  (like a ctypes hook callback) waits 12/27/43 ms median, **up to 312 ms**; a 200-row SQLite
  query in another thread went from 0 ms to **6 s** with 4 busy threads. Hence the multi-process
  design below.
* Measured: under `pythonw.exe`, `sys.stdout`/`sys.stderr` are `None` → the stock
  `http.server` handler (logs to stderr) fails **every** request; `faulthandler.enable()` raises.
* Measured: FTS5 trigram at 500k rows: count ≤ 5 ms, 30k-row fetch 47 ms, LIKE full scan 64 ms,
  bulk insert 23k rows/s, DB ≈ 196 MB; WAL grows to transaction size unless checkpointed.

## 1. Ground rules for every implementer (NON-NEGOTIABLE)

1. **Read-only on real data.** Never create, modify, rename or delete anything under any project
   root, drive or network share. Only list directories / read metadata. Tests use
   `tempfile.TemporaryDirectory()`.
2. **Do not disturb the user's desktop.** The user is working in Resolve right now. In tests: no
   Explorer windows, no visible Edge windows, no message boxes, no sounds, no focus changes. A
   global keyboard hook may only be installed in an explicitly named smoke test that passes every
   event through and lasts < 2 s. Never call Resolve APIs that change state (`LoadProject`,
   `SetCurrentDatabase`, `CreateProject`, `ImportMedia`, `SetClipProperty`, `SaveProject`,
   `OpenPage`, …). Read-only getters only.
3. **Stdlib only** (Python 3.14 on Windows): `ctypes`, `sqlite3`, `http.server`, `json`,
   `threading`, `subprocess`, `winreg`, … No `pip install`.
4. Handle Unicode paths (æøå, parentheses, spaces), UNC paths, drive letters, paths > 260 chars
   (`\\?\` / `\\?\UNC\` prefix for filesystem calls in the scanner), and devices that disappear
   at any moment.
5. Never block HTTP, tray or hotkey threads on filesystem/network I/O that can hang. All
   potentially hanging calls go through `winfs.call_with_timeout` (§4.2).
6. Logging via `logging.getLogger(__name__)`; never `print` in library code (pythonw!).
7. Keep the modules and interfaces below; add private helpers freely.
8. **ctypes conventions**: each module creates its own `ctypes.WinDLL("<dll>", use_last_error=True)`
   objects — never `ctypes.windll.*` (function objects are shared process-wide, so argtypes set in
   one module would change another's). Declare `argtypes` **and** `restype` for every function
   called. Handles (`HANDLE/HWND/HMODULE/HHOOK/HICON/HMENU/LPVOID`) = `wintypes.HANDLE`
   (`c_void_p`); `WPARAM/LPARAM` = `wintypes.WPARAM/LPARAM`; `LRESULT` = `ctypes.c_ssize_t`.
   Callbacks (`WINFUNCTYPE(...)`) stay referenced for their whole lifetime. Errors via
   `ctypes.get_last_error()` / `ctypes.WinError(...)`. (Without restype, 64-bit handles are
   truncated — measured: `GetModuleHandleW(None)` returned `0xffffffffd69f0000`.)
9. **pythonw-safe**: code must work when `sys.stdout`/`sys.stderr` are `None`
   (the app hardens this at startup, §13, but library code must not depend on stdio).

### 1.1 Imports & tests (parallel development rules)
* Module-level imports: the given shared modules (`__init__`, `config`, `events`, `textutil`)
  plus modules **owned by your own agent** only. Collaborators owned by other agents arrive as
  constructor arguments (type hints under `if TYPE_CHECKING:`), and other agents' functions
  (e.g. `winui.open_folder`) are injected as optional constructor parameters whose default `None`
  means "import lazily inside the method". Only `app.py` imports across agents at module level;
  nothing imports `app` or `server`.
* Never create, stub or edit a file owned by another agent — not even a placeholder. Use fakes.
* Test files: `tests/test_<agent>_*.py` with `<agent>` ∈ `index`, `winui`, `resolve`, `app`, `ui`;
  helpers `tests/_<agent>_*.py`. While developing run only your own:
  `python -m unittest discover -s tests -t . -p "test_<agent>_*.py"`.
* Every test module's `setUpModule()` sets `os.environ["LOCALAPPDATA"]` to a
  `TemporaryDirectory` **before** any `config.*_path()` / `Config()` call; tests always pass
  explicit `Config(path=...)` and `db_path=...`. No network, Resolve, windows, Explorer or hooks in
  `test_*.py`. Real-environment checks go in `tests/smoke_<agent>_*.py` (not auto-discovered),
  are read-only, use a temporary `LOCALAPPDATA`, and print a short report.
* Run from the repo root `C:\Github\Search` with `python` (3.14).

## 2. Architecture

Three processes per user session + the Edge window:

```
 Shift+Space ─► [hotkey helper: pythonw -m projektsog.hotkey --child]   (LL hook only)
                   │ JSON lines (stdin/stdout)
                   ▼
 ┌──────────────── main process: pythonw -m projektsog  (CPU-light, never scans) ────────────────┐
 │ app.py (Controller) ─ window.py ─► Edge --app window (UI: web/)   tray.py (Shell_NotifyIcon)   │
 │ server.py 127.0.0.1:47811 (HTTP + SSE) ◄── web/app.js                                          │
 │ indexer.py: registry of sources, discovery (winfs/discovery/pathmap), scheduler, search       │
 │            (read-only DB connections), worker supervisor                                       │
 │ resolve_bridge.py (one Resolve thread)     winui.py (shell/COM thread, foreground helpers)     │
 └───────────────┬───────────────────────────────────────────────────────────────────────────────┘
                 │ JSON lines (stdin/stdout)
                 ▼
 [scan worker: pythonw -m projektsog.scanworker]  background priority; scanner.py + db writes
                 │
                 ▼
            index.db (SQLite WAL): worker writes `entries`/`entries_fts`; main writes `sources`/`meta`
```

* **Main process** (`pythonw -m projektsog`): no thread may run CPU-bound Python for more than a
  few ms at a time (search scoring is bounded). It owns discovery (light I/O through
  `call_with_timeout`), the in-memory source registry, search, HTTP/SSE, window, tray, Resolve,
  and supervises the two helper processes.
* **Scan worker** (`pythonw -m projektsog.scanworker`): started and supervised by
  `Indexer.start()` (restarted if it exits, max 5×/10 min; killed on app exit; exits by itself on
  stdin EOF). Calls `SetPriorityClass(GetCurrentProcess(), PROCESS_MODE_BACKGROUND_BEGIN)` so scans
  never compete with Resolve playing footage. Performs all scanning and all writes to
  `entries`/`entries_fts`.
* **Hotkey helper** (`pythonw -m projektsog.hotkey --child`): owns the `WH_KEYBOARD_LL` hook and
  nothing else (imports only stdlib + `projektsog.hotkey`), so keyboard latency never depends on
  the main process. Started/supervised by `HotkeyManager`; exits on stdin EOF (parent death) →
  a hook can never outlive the app.
* Helper processes are started with the `pythonw.exe` next to `sys.executable`
  (fallback `sys.executable`), `cwd=config.app_dir()`, `creationflags=CREATE_NO_WINDOW`,
  `stdin=PIPE, stdout=PIPE, stderr=DEVNULL`, `close_fds=True`, and the repo root on `PYTHONPATH`.
* UI = local web page (`projektsog/web/`) in an **Edge app-mode window** with a private profile.
  It is pre-launched hidden at startup so Shift+Space is instant.
* Every PC runs its own instance with its own index; remote shares are crawled over SMB.

### Files & ownership

| File | Agent | Purpose |
|---|---|---|
| `projektsog/__init__.py`, `config.py`, `events.py`, `textutil.py`, `tests/test_textutil.py` | **given** | shared base — do not change interfaces |
| `projektsog/winfs.py` | index-core | ctypes: volumes (+hotplug), volume info, shares, mapped drives, `call_with_timeout` |
| `projektsog/pathmap.py` | index-core | path normalisation + UNC↔local aliases |
| `projektsog/discovery.py` | index-core | candidate roots, probe, source keys |
| `projektsog/scanner.py` | index-store | deep (unit-wise) and shallow scans, sequences, kinds, aggregates, diffs |
| `projektsog/db.py` | index-store | schema/migrations, pragmas, apply functions, read queries |
| `projektsog/search.py` | index-store | query planning, filtering, ranking, result shaping |
| `projektsog/scanworker.py` | index-store | scan worker process main + IPC |
| `projektsog/indexer.py` | index-engine | `Indexer`: registry, discovery loops, scheduler, worker supervision, public API |
| `projektsog/winui.py` | winui | shell (COM) thread, Explorer open/reveal + foreground, window/process helpers, run key, DPI, Edge path |
| `projektsog/window.py` | winui | `AppWindow` |
| `projektsog/hotkey.py` | winui | `parse_hotkey`, `format_hotkey`, `HotkeyStateMachine`, child main, `HotkeyManager` |
| `projektsog/tray.py` | winui | `TrayIcon` |
| `projektsog/assets/icon.ico`, `icon.png` | winui | app icon (ico: 16/20/24/32/40/48/64/256) |
| `projektsog/resolve_bridge.py` | resolve | `ResolveBridge` |
| `resolve_scripts/*` | resolve | Workspace ▸ Scripts ▸ Utility script(s) |
| `projektsog/web/index.html`, `app.js`, `style.css` | ui | the UI (no build step, no CDN) |
| `projektsog/server.py` | app | `Server` (HTTP API + SSE + static) |
| `projektsog/app.py`, `projektsog/__main__.py` | app | `Controller`, wiring, lifecycle, CLI |
| `Projektsøg.pyw`, `install.ps1`, `uninstall.ps1`, `README.md` | app | launcher, install, Danish user guide |

## 3. Shared base (already written)

* `events.EventBus`: `publish(type, data)`, `subscribe() -> queue.Queue`, `unsubscribe(q)`; items
  are `(type, data, ts)`.
* `config.Config`: `get(key)`, `cfg[key]`, `snapshot()`, `update(changes) -> snapshot`
  (validates via `config.validate`, saves atomically, calls listeners), `on_change(cb)`.
  `config.DEFAULTS` documents every key. **Listeners run synchronously on the caller's thread
  and must only record the change and wake their owner (no I/O, < 10 ms).** Paths:
  `app_dir()`, `db_path()`, `config_path()`, `instance_path()`, `log_dir()`,
  `edge_profile_dir()`, `hostname()`.
* `textutil.fold(s)`, `tokenize(query)`, `highlight_ranges(name, tokens)` (ranges are
  `[start, end)` in Unicode **code points** of `name`), `fold_with_map(s)`.

### 3.1 Events (publisher → consumer)

| type | data | publisher | when | consumer |
|---|---|---|---|---|
| `status` | `Indexer.status()` (no resolve/hotkey keys) | Indexer | on change, ≤ 2/s | UI merges into its `/api/status` model |
| `sources` | `{"changed": [id…]}` | Indexer | registry change (online/offline, mode, counters, added/forgotten) | UI re-GETs `/api/sources` when settings open |
| `index_updated` | `{"source_id": int}` | Indexer | worker committed changes (unit/shallow), ≤ 1/s per source | UI re-runs the query (rules §12) |
| `scan_progress` | `{"source_id","name","entries","dirs","units_done","units_total","started","kind":"deep"|"shallow"}` | Indexer | ≤ 4/s | UI status pill |
| `new_volume` | `{"disk_name","drive","source_ids","included","reason"}` | Indexer | first sighting of a volume serial | UI card "Ny disk …" with [Medtag] |
| `resolve` | `ResolveBridge.state()` | ResolveBridge | whenever state changes (connect, disconnect, project change, mapping done, error) — independent of follow mode | UI Resolve bar |
| `focus` | `{"from_app": "Resolve.exe"|null, "reason": "hotkey"|"tray"|"launch"|"api"}` | Controller.show_window | after every show | UI (§12 focus rules, Resolve question card) |
| `notify` | `{"title","text","level"}` | ResolveBridge, Indexer, app — only per §10.4 rules | rare | app forwards to `tray.notify()`; UI ignores |
| `settings` | `Config.snapshot()` + `{"run_at_login": bool}` | app (config listener) | after every `cfg.update()` | UI settings panel |
| `hotkey` | `{"spec","label","enabled","active","mode"}` | app | hotkey status change | UI settings/footer |

## 4. Sources, keys and discovery (`winfs.py`, `discovery.py`, `pathmap.py`)

A **source** is one root folder that is indexed recursively.

### 4.1 Source keys (stable identity)
* Local: `vol:<SERIAL>:<rel>` — `<SERIAL>` = 8 upper-case hex digits (`GetVolumeInformationW`),
  `<rel>` = root path relative to the volume root with a leading backslash (`\2024 Disk Sølv`, or
  `\` for a whole volume). Same disk under another drive letter → same key.
* Network share: `unc:<HOST>\<share><rel>` (`<HOST>` upper case, share name as reported, `<rel>`
  empty for the share root), e.g. `unc:GRAFIK-PC\Forår 2026 (HDD)`.
* Keys are compared with `casefold()`.

### 4.2 `winfs.py`
```python
def call_with_timeout(key: str, fn: Callable[[], T], timeout: float) -> tuple[str, T | None]
    # -> ("ok", value) | ("timeout", None) | ("busy", None) | ("error", None)
    # At most ONE in-flight daemon thread per key (drive letter, host, root path…). While the
    # previous call for a key still runs, returns ("busy", None) immediately — never piles up
    # stuck threads. Exceptions inside fn → ("error", None) and are logged at debug level
    # (the exception object is available via a second helper or an out-param; implementer's choice).
def list_volumes() -> list[dict]
    # Ready local volumes with a drive letter, skipping DRIVE_NO_ROOT_DIR(1), DRIVE_REMOTE(4),
    # DRIVE_CDROM(5), DRIVE_RAMDISK(6) and not-ready drives. Each:
    # {"drive": "H:", "root": "H:\\", "label": str, "serial": "5E3A0B21", "fs": "NTFS",
    #  "drive_type": 2|3, "is_system": bool, "hotplug": bool, "size": int (total bytes)}
    # hotplug = BusType in (USB 7, SD 0xC, MMC 0xD, 1394 4) via IOCTL_STORAGE_QUERY_PROPERTY
    # (StorageDeviceProperty) on "\\\\.\\H:" opened with desired access 0 (no admin) and CLOSED
    # immediately; or DeviceHotplug via IOCTL_STORAGE_GET_HOTPLUG_INFO. Per-drive calls go
    # through call_with_timeout(key=drive).
def volume_info(root: str, timeout: float = 5.0) -> dict | None     # {"label","serial","fs"}
def volume_size(root: str) -> int | None                            # GetDiskFreeSpaceExW total
def local_shares() -> list[dict]      # NetShareEnum level 2, disk shares, no special/$: {"name","path"}
def remote_shares(host: str, timeout: float = 8.0) -> list[str] | None   # level 1; None = unreachable/busy
def mapped_drives() -> dict[str, str] # {"Z:": "\\\\HOST\\share"} via WNetGetConnectionW (no network I/O)
def resolve_host_ips(host: str, timeout: float = 3.0) -> list[str]
```
`SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOOPENFILEERRORBOX)` is set by the process at startup
(§13); winfs must still never trigger a "no disk in drive" dialog (check readiness first).

### 4.3 Candidate roots (`discovery.py`)
```python
def local_candidates(cfg, volumes: list[dict], shares: list[dict], own_host: str) -> list[dict]
def remote_candidates(host: str, share_names: list[str]) -> list[dict]
def mapped_candidates(mapped: dict[str, str]) -> list[dict]
def extra_root_candidates(cfg, volumes: list[dict], own_host: str) -> list[dict]
def probe(path: str, cfg, *, hotplug: bool = False, max_listings: int = 400) -> tuple[bool, str, int]
def looks_like_project(child_dir_names: Iterable[str], cfg) -> bool
def is_template_name(name: str, cfg) -> bool
def key_local(serial: str, rel: str) -> str; def key_share(host: str, share: str, rel: str = "") -> str
```
Candidate dict:
`{"key","kind":"local"|"share","path","unc_path"|None,"host","share"|None,"volume_serial",
"volume_label","fs","display_name","drive"|None,"hotplug":bool,"volume_size"|None,"manual":bool}`.

1. **Local volumes** (skip labels in `skip_volume_labels`):
   * each local share whose path is on that volume and exists;
   * each top-level directory (not hidden+system, not in `skip_top_level_dirs`);
   * if the **volume root directly contains a project folder** (or, on a hotplug volume, the root
     directly contains media files), the candidate is the whole volume (`<rel> = \`) instead;
   * drop candidates nested inside another candidate of the same volume (keep outermost).
   * These listings are few and local but still go through `call_with_timeout`.
2. **Mapped network drives** → the UNC share they point to (dedupe with host shares).
3. **Remote hosts** = `cfg["hosts"]` minus own `hostname()` (+ hosts of UNC `extra_roots`):
   each share from `remote_shares(host)` is a candidate; one thread per host.
4. `extra_roots` are candidates with `manual=True` (forced include).

`display_name`: share name for shares; folder name for local folders; for whole volumes the
volume label or `"<drive> (uden navn)"`. `unc_path`: for local candidates inside a local share:
`\\<HOSTNAME>\<share>\<rest>`; for share candidates: the share path itself.

### 4.4 Auto include (probe)
A folder **looks like a project** when ≥ `project_min_template_dirs` of its direct sub-folder
names (case-insensitive) are in `project_template_dirs`, or `is_template_name()`.
`probe()` lists the folder, its children and (budget permitting) grandchildren; it returns
`(include, reason_da, project_count)`:
* the folder itself, a child or a grandchild looks like a project → include
  (`"3 projektmapper fundet"`, `"Mappen er selv et projekt"`);
* else, **if `hotplug`**, any file with an extension in `media_exts` seen within the budget →
  include (`"Mediefiler fundet"`);
* else exclude (`"Ingen projektmapper fundet"`, `"Ingen adgang"`, `"Svarer ikke"`).
Sources have a user **mode**: `auto` (default, use probe), `include`, `exclude`.
Re-probe auto-excluded candidates at most every 30 min and when they come online again.
An included source never flips to excluded by itself.

### 4.5 `pathmap.py`
```python
class PathMap:
    def __init__(self, hostname: str): ...
    def update(self, local_shares: list[dict], mapped_drives: dict[str, str],
               host_ips: dict[str, list[str]], own_ips: list[str]) -> None
    def normalize(self, path: str) -> str
        # strip \\?\ and \\?\UNC\, '/'→'\', collapse duplicate '\' (not the leading '\\' of UNC),
        # remove trailing '\' (keep 'C:\'), upper-case drive letter and UNC host,
        # \\<OWN HOST|localhost|127.0.0.1|own IPs>\<share>\rest → local share path + rest,
        # \\<IP>\share → \\<HOST>\share when the IP belongs to a known host,
        # mapped drive letter paths (Z:\x) → UNC.
    def key(self, path: str) -> str                    # casefold(normalize(path))
    def unc_for(self, local_path: str) -> str | None   # local path inside a local share → UNC
```

## 5. Scanning (`scanner.py`, `scanworker.py`)

### 5.1 Deep scan (unit-wise, chunked commits)
```python
def deep_scan(conn, source_id: int, root_path: str, cfg: dict, *, full: bool, is_network: bool,
              fs: str, cancel: threading.Event, progress: Callable[[dict], None]) -> dict
    # returns {"ok": bool, "aborted": bool, "error": str|None, "changed": int, "units_done": int,
    #          "units_total": int, "entries": int, "dirs": int, "files": int, "seconds": float,
    #          "counts": {"entry_count","dir_count","file_count","project_count","total_size"}}
```
* List the root (`with os.scandir(p) as it: items = list(it)` — **read each directory completely
  and close it before processing**; never keep more than one directory handle open). If the root
  cannot be listed → abort, no writes.
* **Units** = each depth-1 directory subtree, plus one unit for the root's own files. For each
  unit: walk it iteratively (explicit stack), build its entries, diff them against the stored rows
  of that subtree (range query on `rel_path`), and apply the diff in **its own transaction**
  (short write locks; progress becomes searchable unit by unit; memory bounded by the largest
  unit). Units present in the DB but missing from the (successful) root listing are deleted with
  their subtrees. Cancel/abort stops after the current unit; committed units stay.
* Use the `\\?\` / `\\?\UNC\` form for filesystem calls. `DirEntry.is_dir(follow_symlinks=False)`;
  never descend into junctions/symlinks (`is_junction()`/`is_symlink()`), store them as plain
  entries.
* Skip `exclude_dir_names`, `exclude_file_names`, `exclude_file_globs` (case-insensitive) and
  entries that are both HIDDEN and SYSTEM (`st_file_attributes`).
* **Image sequences**: in each directory, files matching `^(.*?)(\d+)\.([A-Za-z0-9]+)$` with an
  extension in `sequence_exts`, grouped by (prefix, digit width, ext lower); groups with
  ≥ `sequence_min_files` members become ONE entry named `"<prefix>[<first>-<last>].<ext>"`
  (e.g. `render_[0001-4500].exr`), `is_seq=1`, `seq_count`, `size`=sum, `mtime`=max.
* **Kinds**: `0 file`, `1 dir`, `2 project` (§4.4 rule), `3 group` (dir, not project, with ≥ 1
  project child), `4 template` (`template_folder_regex`), `5 toplevel` (depth-1 dir that is none
  of 2/3/4).
* Aggregates for dirs: `size` = total bytes in subtree, `mtime` = newest mtime in subtree
  (incl. own), `file_count` = files in subtree (a sequence counts `seq_count`); `dir_mtime` = the
  directory's **own** mtime as reported by its parent listing. `project_rel` = nearest
  ancestor-or-self project `rel_path`.
* **Incremental** (`full=False`, only when `is_network` and `fs == "NTFS"` and the unit has stored
  rows): a directory that was a **leaf** (no sub-dirs) in the DB and whose own mtime from the
  parent listing equals the stored `dir_mtime` is not listed again; its stored children and
  aggregates are reused. Everything else is listed. Local sources and non-NTFS sources always
  walk fully. A full walk is forced every `full_rescan_hours`.
* **Errors**: a directory that cannot be listed is recorded; the diff keeps the stored rows under
  it. If > 50 % of a unit's directories fail, that unit is not applied. Errors never delete data.
* `progress()` at most 4×/s with `{"entries","dirs","units_done","units_total"}`.
* Rel paths: relative to the root, `\`-separated, no leading `\`; the root is not an entry.
* Scan threads call `SetThreadPriority(GetCurrentThread(), THREAD_MODE_BACKGROUND_BEGIN)` (in
  addition to the process mode).

### 5.2 Shallow scan
```python
def shallow_scan(conn, source_id: int, root_path: str, cfg: dict, *, first_time: bool,
                 max_listings: int, cancel: threading.Event, progress=None) -> dict
    # same return shape as deep_scan (+ "listings": int)
```
Purpose: make new project folders findable within seconds, and make a never-scanned source
searchable at project level quickly. List the root; list each depth-1 dir that is new or whose
own mtime ≠ stored `dir_mtime` (non-NTFS: every depth-1 dir); list each new dir at depth 2 once
to classify it (`first_time`: list all depth-1 and depth-2 dirs, ≤ `max_listings`, default 2000).
Apply with `db.apply_shallow()`: insert/update dirs (and files) at depth ≤ 2, delete depth ≤ 2
entries (with subtrees) that vanished from a successfully listed parent; never touch deeper rows.
New dirs get `size/mtime/file_count` = NULL until the deep scan (UI shows "–").

### 5.3 Worker IPC (`scanworker.py`)
JSON lines. Main → worker (stdin):
```json
{"cmd": "scan", "job": 17, "source_id": 3, "root_path": "\\\\GRAFIK-PC\\Forår 2026 (HDD)",
 "kind": "deep"|"shallow", "full": false, "first_time": false, "max_listings": 2000,
 "is_network": true, "fs": "NTFS"}
{"cmd": "cancel", "job": 17}            {"cmd": "cancel_source", "source_id": 3}
{"cmd": "forget", "job": 18, "source_id": 3}      // delete all entries of the source
{"cmd": "config", "cfg": {…Config.snapshot()…}}   // sent first and after every settings change
{"cmd": "quit"}
```
Worker → main (stdout):
```json
{"ev": "ready", "pid": 1234}
{"ev": "progress", "job": 17, "source_id": 3, "entries": 120000, "dirs": 8000, "units_done": 12, "units_total": 53}
{"ev": "committed", "job": 17, "source_id": 3, "changed": 250}        // after each unit/shallow apply with changes
{"ev": "done", "job": 17, "source_id": 3, "result": {…deep_scan()/shallow_scan() return…}}
{"ev": "failed", "job": 17, "source_id": 3, "error": "…"}
```
The worker runs up to `max_parallel_scans` jobs concurrently (threads, one DB writer connection
shared under a lock; each transaction short). It logs to `log_dir()\scanworker.log`. It exits on
`quit` or stdin EOF, cancelling running jobs.

## 6. Database (`db.py`, SQLite at `config.db_path()`)

Ownership: the **worker** writes `entries` (+ FTS); the **main process** writes `sources` and
`meta` (on a background thread, never on HTTP threads) and creates/migrates the schema at startup
before starting the worker. Pragmas: `auto_vacuum=INCREMENTAL` (before the first table), WAL,
`synchronous=NORMAL`, `foreign_keys=ON`, `busy_timeout=10000` on every connection,
`journal_size_limit=67108864` on writers. After a job that changed > 20k rows and after
`forget`: `PRAGMA wal_checkpoint(TRUNCATE)` (ignore busy) and `incremental_vacuum` if > 50k rows
were deleted. Readers never keep a read transaction open between requests (always `fetchall()`
or close cursors). Read connections are thread-local.

```sql
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);          -- schema_version = '2'
CREATE TABLE sources(
  id INTEGER PRIMARY KEY,
  key TEXT NOT NULL UNIQUE,                 -- §4.1
  kind TEXT NOT NULL,                       -- 'local' | 'share'
  host TEXT NOT NULL,                       -- physical owner computer (own hostname for local)
  share TEXT,
  display_name TEXT NOT NULL,
  current_path TEXT NOT NULL,               -- current (or last known) access path
  unc_path TEXT,
  volume_serial TEXT, volume_label TEXT, fs TEXT, volume_size INTEGER,
  last_drive TEXT,                          -- last drive letter seen ("H:") for local sources
  hotplug INTEGER NOT NULL DEFAULT 0,
  manual INTEGER NOT NULL DEFAULT 0,        -- from extra_roots
  online INTEGER NOT NULL DEFAULT 0,
  mode TEXT NOT NULL DEFAULT 'auto',        -- auto | include | exclude
  auto_include INTEGER, auto_reason TEXT, probed_at REAL,
  first_seen REAL, last_seen REAL,
  last_scan_start REAL, last_scan_end REAL, last_scan_ok INTEGER, last_full_scan REAL,
  last_shallow_scan REAL, last_error TEXT, scan_seconds REAL,
  entry_count INTEGER NOT NULL DEFAULT 0, dir_count INTEGER NOT NULL DEFAULT 0,
  file_count INTEGER NOT NULL DEFAULT 0, project_count INTEGER NOT NULL DEFAULT 0,
  total_size INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE entries(
  id INTEGER PRIMARY KEY,
  source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  rel_path TEXT NOT NULL,
  parent_rel TEXT NOT NULL,                 -- '' for depth-1 entries
  name TEXT NOT NULL,
  name_fold TEXT NOT NULL,                  -- textutil.fold(name)
  kind INTEGER NOT NULL,                    -- §5.1
  depth INTEGER NOT NULL,                   -- 1 = directly under the root
  ext TEXT,                                 -- files: lower-case extension without dot
  size INTEGER, mtime REAL, file_count INTEGER,
  dir_mtime REAL,                           -- dirs: own mtime (for incremental/shallow)
  is_seq INTEGER NOT NULL DEFAULT 0, seq_count INTEGER,
  project_rel TEXT,
  UNIQUE(source_id, rel_path)
);
CREATE INDEX ix_entries_parent ON entries(source_id, parent_rel);
CREATE INDEX ix_entries_kind ON entries(kind, mtime);
CREATE VIRTUAL TABLE entries_fts USING fts5(name_fold, content='entries', content_rowid='id',
                                            tokenize='trigram');
-- triggers keep entries_fts in sync: AFTER INSERT, AFTER DELETE, AFTER UPDATE OF name_fold
```
Source counters (`entry_count` …) are returned by the worker in `done` and written by main.

## 7. Search (`search.py`)

```python
def search(conn, sources: dict[int, dict], query: str, *, kind: str = "all",
           online_only: bool = False, source_id: int | None = None, limit: int = 200,
           include_templates: bool = False) -> dict
def recent_projects(conn, sources: dict[int, dict], limit: int = 30, online_only: bool = False) -> list[dict]
def children(conn, sources: dict[int, dict], source_id: int, rel_path: str) -> list[dict]
def make_item(row, source: dict, tokens: list[str] | None = None) -> dict
```
`sources` = the Indexer's in-memory registry (id → Source object §7.1); online status and paths
come from there, never from stale DB rows.
* `tokens = textutil.tokenize(query)`. **Every** token must occur (substring) in the entry's
  `name_fold`, or in the folded ancestor path within the source, or in the folded source
  `display_name`/`volume_label`. At least one token must match the entry's own name.
* Retrieval: the most selective token with ≥ 3 chars (FTS count) drives an FTS trigram `MATCH`
  on `name_fold` (quote the token); descendants of matching **directories** whose own name
  matches the remaining tokens are added (range query on `rel_path` within the subtree), so
  `lindholm klip` finds `…\Rikke Lindholm\Klip`. All tokens < 3 chars → `LIKE`. Cap candidates
  (~30k) and set `truncated`.
* `kind`: `all` | `project` (kinds 2,3,5) | `dir` (1,2,3,5) | `file` (0). Templates (4) only with
  `include_templates`.
* Ranking (desc), then `mtime` desc: base project 1000, group 800, toplevel 700, dir 400,
  file 100; all tokens in the name +300; name == query +250; name starts with first token +100;
  each token starting a word in the name +40; each token matched only via path −120;
  source online +200; newest mtime < 30 d +80, < 180 d +40, < 1 y +15; `−4 × depth`.
* Target: typical < 50 ms, worst < 250 ms at ~500k entries (in the CPU-light main process).
* Response: `{"query","tokens","took_ms","total","truncated","results":[Item…]}`; when
  `total == 0` and a filter (kind/online_only/source_id) is active, also
  `"hidden": {"kind": int, "offline": int, "source": int}` = how many matches each filter hid.

### 7.1 Shared JSON shapes (all producers emit exactly these)
```text
SourceRef  = {"id","name" (=display_name),"host","kind","online": bool,"drive": "H:"|null,
              "disk_name": str|null,"volume_label": str|null,"last_seen": float|null}
             drive = first two chars of current_path upper-cased if it starts with "<letter>:", else null
             disk_name (local only) = volume_label, else "disk uden navn (<size>, sidst som <last_drive>)"
ProjectRef = {"name","rel_path","path","unc_path": str|null}
Item       = {"id","kind": "project"|"group"|"toplevel"|"dir"|"file"|"template","name",
              "hl": [[s,e],…],                 # [] for recent/children/locate items
              "path",                          # ntpath.join(current_path, rel_path) (no doubled '\')
              "open_path",                     # real path to open; == path except sequences: first frame
              "unc_path": str|null,            # ntpath.join(sources.unc_path, rel_path)
              "rel_path","parent","depth",
              "source": SourceRef,"project": ProjectRef|null (self for projects),
              "size": int|null,"mtime": float|null,"file_count": int|null,"ext": str|null,
              "is_seq": bool,"seq_count": int|null,
              "subfolders": [str]|null,        # dirs only, direct child dir names (≤ 40)
              "score": float|null}
Source     = {"id","key","kind","display_name","host","path" (=current_path),"unc_path",
              "volume_label","volume_serial","fs","drive","last_drive","disk_name","hotplug",
              "volume_size","online","mode","included","auto_reason","manual",
              "entry_count","dir_count","file_count","project_count","total_size",
              "last_scan_end","last_scan_ok","last_error","last_seen","scanning": bool,
              "scan_kind": "deep"|"shallow"|null,"queued": bool}
             included = manual or mode == "include" or (mode == "auto" and auto_include == 1)
Host       = {"name","online": bool,"shares": int,"last_seen": float|null,"self": bool}
```
For `kind == "share"` sources `unc_path == current_path`.

### 7.2 Acceptance queries (real data, after indexing)
| Query | Expected near the top |
|---|---|
| `lindholm` / `rikke lindholm` | project `Rikke Lindholm` in `C:\Kunder 2026 (STUDIO)` first |
| `lindholm klip` | dir `…\Rikke Lindholm\Klip` first |
| `klar tand silkeborg` | `Klar Tand - Silkeborg` (GRAFIK-PC), `Klar Tand - Silkeborg C`, `Klar Tand - Voxpop Silkeborg` |
| `pixelbro` | `Pixelbro` (D:), `Pixelbro Radio` (H:) |
| `bøgely` and `bogely` | `Bøgely Jul 2024`, `Bøgely Jul 2025`, `Bøgely Festival…`, `Hotel Bøgelyhus` |
| `forar pixelbro` | `Pixelbro` (via source name "Forår 2026 RØD") |
| `FX9_7912` | file `FX9_7912.MXF` in `Rikke Lindholm\Klip\FX9` |
| `kundenavn` | nothing unless `include_templates` |

## 8. Indexer engine (`indexer.py`, main process)

```python
class Indexer:
    def __init__(self, cfg: Config, bus: EventBus, db_path: str | None = None, *,
                 worker_argv: list[str] | None = None,   # default: [pythonw, "-m", "projektsog.scanworker"]
                 start_worker: bool = True): ...
    def start(self) -> None; def stop(self, timeout: float = 5) -> None
    # queries — thread-safe, never block on I/O, return in < 250 ms
    def status(self) -> dict
    def list_sources(self) -> list[dict]            # [Source]
    def hosts(self) -> list[dict]                   # [Host]
    def search(self, q: str, kind="all", online_only: bool | None = None,
               source_id: int | None = None, limit: int | None = None,
               include_templates: bool = False) -> dict
        # online_only None → not cfg["show_offline"]; limit None → cfg["result_limit"]
    def recent_projects(self, limit: int = 30) -> list[dict]           # [Item], show_offline rule applies
    def children(self, source_id: int, rel_path: str) -> list[dict]     # [Item]
    def locate(self, path: str) -> dict | None
        # {"source": SourceRef, "rel_path": str, "path": str, "unc_path": str|None, "online": bool,
        #  "entry": Item|None, "project": ProjectRef|None}
    def map_paths(self, paths: list[str]) -> dict
        # {"folders": [{"project": ProjectRef, "source": SourceRef, "online": bool, "count": int,
        #               "item": Item|None}],                 # count desc, ties → online first
        #  "other_dirs": [{"path": str, "count": int, "online": bool|None}],   # None = unknown location
        #  "total": int}
    def suggest_project_folders(self, name: str, limit: int = 5) -> list[dict]
        # [{"project": ProjectRef, "source": SourceRef, "online": bool, "score": float (0..1), "item": Item}]
    # commands — validate, update registry/config, queue work, return in < 50 ms (no I/O here)
    def scan_now(self, source_id: int | None = None, full: bool = False) -> None
    def on_window_shown(self) -> None
    def set_source_mode(self, source_id: int, mode: str) -> dict       # -> Source
    def forget_source(self, source_id: int) -> None                    # offline sources only
    def add_root(self, path: str) -> dict            # {"ok": True, "source": Source|None}
    def remove_root(self, path: str) -> None
    def add_host(self, name: str) -> None; def remove_host(self, name: str) -> None
    def path_missing(self, path: str) -> None
```
**Errors**: invalid input or refused commands raise `ValueError("<Danish message>")`
(unknown source id → "Placeringen findes ikke"; forget of an online source → "Kun offline
placeringer kan glemmes"; bad mode; non-absolute path; invalid host name; removing a root not in
`extra_roots`). `add_root/remove_root/add_host/remove_host` persist via `cfg.update()`. The
Indexer registers its own `cfg.on_change` (record + wake) so edits made through `/api/settings`
take effect too, and forwards `{"cmd":"config"}` to the worker.

**Status dict**:
```json
{"hostname": "STUDIO-PC", "version": "1.0.0",
 "sources_total": 14, "sources_online": 11, "sources_offline": 1, "sources_excluded": 2,
 "sources_ready": 10, "sources_included_online": 11,
 "entries": 523401, "files": 510000, "dirs": 13401, "projects": 412,
 "scanning": [{"source_id": 3, "name": "2025Arkiv", "kind": "deep", "entries": 120000,
               "dirs": 8000, "units_done": 12, "units_total": 53, "started": 1727700000.0, "full": true}],
 "queued": 2, "last_scan_end": 1727700000.0, "initial_scan_done": false,
 "worker": {"running": true, "restarts": 0}, "db_size": 123456789}
```
`sources_ready` = included online sources with `last_scan_end` or `last_shallow_scan` set;
`initial_scan_done` = every included online source has `last_scan_end` set.

**Scheduling**:
* Local volume poll every `discovery_interval_local_s`; host poll every
  `discovery_interval_network_s` (one thread per host). New/returned candidate → registry
  upsert (DB write on a background thread) → probe if needed → if included: first time ever →
  shallow scan (`first_time=True`) then deep; otherwise deep (incremental rules). Vanished volume
  / unreachable host → its sources offline at once (publish `sources`, cancel their jobs).
* First sighting of a volume serial → publish `new_volume` and a `notify` ("Ny disk ‘<disk_name>’
  tilsluttet" + " – medtaget i søgningen" or " – ikke medtaget: <reason>").
* Periodic deep scans: local every `scan_interval_local_min`; network every
  `max(scan_interval_network_min, 4 × scan_seconds)`. Max `max_parallel_scans` concurrent jobs;
  **at most one deep job per host** (network) and per volume (local). Per-host deep queue:
  ascending by last `scan_seconds` (first run: by the shallow scan's dir count), so small shares
  never wait behind 2025Arkiv.
* `on_window_shown()`: after a 1 s delay (merged if called repeatedly), queue a **shallow**
  (non-first-time) scan for every online included source not shallow-scanned in the last 30 s
  (network: one host at a time, `max_listings` 200). Never triggers deep scans.
* `path_missing(path)`: ignored for offline sources; queues at most one shallow + one deep scan
  per source per `scan_interval_local_min`/`scan_interval_network_min`.
* Publish `status` (≤ 2/s), `scan_progress`, `index_updated` (from worker `committed`),
  `sources`, `new_volume`.

## 9. DaVinci Resolve (`resolve_bridge.py`, main process)

```python
class ResolveBridge:
    def __init__(self, cfg: Config, bus: EventBus, indexer: "Indexer", *,
                 process_running: Callable[[str], bool] | None = None,       # default winui.process_running
                 process_uptime: Callable[[str], float | None] | None = None, # default winui.process_uptime
                 open_folder: Callable[..., bool] | None = None,             # default winui.open_folder
                 explorer_window_for: Callable[[str], int | None] | None = None): ...
    def start(self) -> None; def stop(self) -> None
    def state(self) -> dict                   # cached, instant
    def refresh(self, wait: bool = True) -> dict   # any thread; enqueue a re-walk on the Resolve
                                                   # thread; if wait, wait ≤ 10 s; returns state()
    def on_window_shown(self) -> None         # async re-walk if the last walk is > 30 s old
    def open_primary(self) -> dict            # {"ok": bool, "path": str|None, "error": str|None}
```
* All Resolve calls happen on ONE dedicated thread. Connect only while `Resolve.exe` runs and has
  been running ≥ 15 s (Resolve may fail scripts while starting). Import `DaVinciResolveScript`
  from `%PROGRAMDATA%\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting\Modules` with
  `RESOLVE_SCRIPT_API`/`RESOLVE_SCRIPT_LIB` set (`…\DaVinci Resolve\fusionscript.dll`).
  `scriptapp("Resolve")` returning None while Resolve runs → error "Slå ekstern scripting til i
  DaVinci Resolve: Preferences ▸ System ▸ General ▸ External scripting using = Local".
* Poll every `resolve_poll_s` (re-read `resolve_enabled`, `resolve_poll_s`, `resolve_follow`
  each loop): current project name + `GetCurrentDatabase()["DbName"]`. On change, `refresh()` or
  `on_window_shown()` (> 30 s): walk the media pool recursively (bounded: ≤ 20 s, ≤ 50k clips),
  collect `GetClipProperty("File Path")` (skip empty), `indexer.map_paths(paths)`; if no folder,
  `indexer.suggest_project_folders(project_name)`.
* **Primary** = `folders[0]` (count desc, ties → online first); else `suggestions[0]` only if
  `score ≥ 0.6` — but suggestions are shown as "Muligt match" and are **never pre-selected**.
* `state()`:
```json
{"enabled": true, "running": true, "connected": true, "error": null,
 "project": "Rikke Lindholm - Testimonial", "database": "Kunder 2026 (Projektserver)",
 "clip_count": 167, "updated": 1727700000.0,
 "folders": [ …map_paths()["folders"]… ], "other_dirs": [ … ],
 "suggestions": [ …suggest_project_folders()… ],
 "primary": {…Item of the chosen folder…, "match": "media"|"name"} | null,
 "offline_clips": 0, "offline_disks": ["2024 Disk Sølv"]}
```
  Disabled / not running / not connected: `{"enabled", "running", "connected": false,
  "error", "project": null, "database": null, "clip_count": 0, "updated": null, "folders": [],
  "other_dirs": [], "suggestions": [], "primary": null, "offline_clips": 0, "offline_disks": []}`.
* Follow mode on a project change that is **stable ≥ 5 s**, not `"Untitled Project"`, with
  ≥ 1 clip, at most once per project per 30 min: `notify` → publish a `notify` event
  ("DaVinci Resolve: <project>", "Projektmappe: <name>" or the offline-disk hint); `open` → also
  open the primary folder unless `explorer_window_for(path)` finds one already, with
  `open_folder(path, activate=False)` (never steal focus); only if online.
* `open_primary()` uses cached state; refuses offline folders (Danish error); opens with
  `open_folder(path)` (activate).
* `resolve_scripts/Projektsøg - Åbn projektmappe.py` (+ ASCII-named fallback if the Unicode name
  is not listed by Resolve; research and document): run from Workspace ▸ Scripts. Reads
  `%LOCALAPPDATA%\Projektsog\instance.json`, `POST /api/resolve/open` with header
  `X-Projektsog: 1`; if the app is not running, falls back to opening the deepest common folder of
  the media pool's clip paths that is a project (template-folder heuristic). Works both inside
  Resolve (`resolve` global) and via `ResolvePython.exe` externally. Supports a dry-run env var
  `PROJEKTSOG_DRY_RUN=1` (prints instead of opening) for testing.

## 10. Windows plumbing (`winui.py`, `window.py`, `hotkey.py`, `tray.py`)

All public functions/methods here are **thread-safe** and callable from any thread.

### 10.1 `winui.py`
```python
def open_folder(path: str, activate: bool = True) -> bool
def reveal(path: str, activate: bool = True) -> bool    # Explorer with the item selected
def open_file(path: str) -> bool     # default app; refuses non-allow-listed types (no exe/bat/cmd/ps1/vbs/js/lnk/msi/scr/com/hta/reg/…)
def explorer_window_for(path: str) -> int | None        # CabinetWClass window whose title starts with the folder name
def force_foreground(hwnd: int) -> bool
def find_app_window(title: str, exe_name: str = "msedge.exe", pid: int | None = None) -> int | None
def foreground_window() -> int | None
def foreground_process_name() -> str | None
def process_running(exe_name: str) -> bool
def process_uptime(exe_name: str) -> float | None        # seconds since the oldest such process started
def set_run_at_login(enabled: bool, command: str) -> None
def get_run_at_login() -> bool
def edge_path() -> str | None
def set_dpi_awareness() -> None                           # SetProcessDpiAwarenessContext(-4), fallback older APIs
def set_app_user_model_id(aumid: str) -> None
```
* **Shell thread**: all `ShellExecuteW`/`SHParseDisplayName`/`SHOpenFolderAndSelectItems` calls run on
  ONE dedicated thread that called `CoInitializeEx(None, COINIT_APARTMENTTHREADED |
  COINIT_DISABLE_OLE1DDE)` once and pumps messages; callers wait ≤ 3 s for the result.
* `open_folder`/`reveal` with `activate`: before the shell call, `SendInput` one zero-filled
  `INPUT_MOUSE` (lifts the foreground lock for our process) and `AllowSetForegroundWindow(ASFW_ANY)`;
  snapshot CabinetWClass windows + titles; after the call poll ≤ 2 s for a **new** CabinetWClass
  window or an existing one whose title changed to start with the folder name (reused window /
  new tab), and `force_foreground` it.
* `force_foreground(hwnd)`: `ShowWindowAsync(SW_RESTORE if IsIconic else SW_SHOW)`,
  `SetForegroundWindow`; if not foreground: `SendInput` zero mouse input, retry, `BringWindowToTop`;
  last resort `AttachThreadInput` (never if `IsHungAppWindow(foreground)`; always detach in
  `finally`). **Never synthesise Alt or any real key.**
* Run key: `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`, value name exactly `Projektsøg`;
  `get_run_at_login()` is True iff it exists; `set_run_at_login(False)` also removes any HKCU Run
  value whose data contains `Projektsøg.pyw` or `-m projektsog`.

### 10.2 `window.py`
```python
class AppWindow:
    def __init__(self, url: str, profile_dir: str, title: str = "Projektsøg",
                 edge: str | None = None): ...
    def preload(self) -> None       # launch hidden/off-screen if no window exists (no activation)
    def show(self) -> bool          # True when our window ended up foreground
    def hide(self, restore_previous: bool = False) -> None
    def is_visible(self) -> bool; def is_foreground(self) -> bool
    def close(self) -> None         # WM_CLOSE (app exit)
```
* Our window = top-level, class `Chrome_WidgetWin_1`, process image `msedge.exe`, title **exactly**
  `Projektsøg` (app windows carry the bare page title; the UI never changes `document.title`).
  Cache hwnd+pid; re-validate (`IsWindow`, same pid, same title) before every use; never touch a
  window that fails validation (Chrome/VS Code/Electron windows share the class).
* Launch: `Popen([edge, "--app=<url>", "--user-data-dir=<profile>", "--window-size=1180,780",
  "--no-first-run", "--no-default-browser-check", "--disable-features=Translate",
  "--disable-renderer-backgrounding", "--disable-background-timer-throttling",
  "--disable-backgrounding-occluded-windows", "--hide-crash-restore-bubble"], cwd=app_dir())`.
  `preload()` adds `--window-position=-32000,-32000` and starts minimised without activation
  (`STARTUPINFO` `SW_SHOWMINNOACTIVE`), waits ≤ 15 s for the window, then hides it. While a launch
  is pending (≤ 15 s) `show()` waits for that window instead of launching a second one.
* `show()`: remember the current foreground window (if not ours); move/centre the window on the
  work area of that window's monitor (keep size; also fixes off-screen preload position); show
  with `ShowWindowAsync`; `force_foreground`. Returns within 10 s.
* `hide(restore_previous=True)` (Esc, hotkey toggle) re-activates the remembered window if it
  still exists; `hide()` after an open does not.

### 10.3 `hotkey.py`
```python
def parse_hotkey(spec: str) -> tuple[frozenset[str], int]
    # grammar mod(+mod)*+key, mod ∈ ctrl|alt|shift|win, key ∈ space|a–z|0–9|f1–f24, case-insensitive
    # raises ValueError("<Danish message>") for invalid specs
def format_hotkey(spec: str) -> str      # "shift+space" → "Shift+Mellemrum"
class HotkeyStateMachine:                # pure logic, unit-tested without Windows
    def __init__(self, mods: frozenset[str], vk: int, *, typing_guard_ms: int = 300,
                 double_tap_ms: int = 400): ...
    def on_event(self, vk: int, down: bool, injected: bool, t_ms: float, *,
                 held_mods: frozenset[str] | None = None, key_was_down: bool | None = None,
                 passthrough: bool = False) -> tuple[bool, bool]     # (swallow, fire)
class HotkeyManager:                      # main process side
    def __init__(self, spec: str, callback: Callable[[dict], None], *,
                 passthrough_apps: list[str] = (), typing_guard_ms: int = 300,
                 double_tap_ms: int = 400, enabled: bool = True,
                 child_argv: list[str] | None = None): ...
        # callback(info) on a manager thread (never in the hook); info = {"from_app": "Resolve.exe"|None}
    def start(self) -> bool                # True once the child reports ready (≤ 3 s)
    def stop(self) -> None
    def update(self, **changes) -> bool    # spec/passthrough_apps/typing_guard_ms/double_tap_ms/enabled
    def end_capture(self, ok: bool) -> None
    @property
    def active(self) -> bool
    @property
    def mode(self) -> str | None          # "ll" | "registerhotkey" | None
```
**State machine rules** (for the configured mods + main key; Shift+Space by default):
* Fire only on a main-key **down** while exactly the required modifiers are held (`held_mods`
  from `GetAsyncKeyState`, when given, overrides internal tracking; if it shows a required
  modifier up, reset "modifier down" and "other key since modifier" state first).
* Typing guard: do **not** fire (and do not swallow) if any non-modifier key went down less than
  `typing_guard_ms` before, or — for Shift-only hotkeys — any other key went down since Shift
  went down.
* Swallow the triggering down, its auto-repeats (`key_was_down=True` only if the press that
  started the repeat was swallowed) and its up. Ignore injected events.
* Passthrough app in foreground (evaluated only for main-key downs): pass a single press through;
  a **second** press within `double_tap_ms` of a passed-through one (no other non-modifier key in
  between) is swallowed and fires.
* Required unit tests include: idle 1 s, Shift↓(+80 ms) Space↓ → fire+swallow down/repeat/up;
  `e↓ e↑ (+90) Shift↓ (+60) Space↓` → no fire; `Shift↓ H E J Space↓` → no fire;
  Ctrl+Shift+Space → no fire; lost Shift-up then Space with `held_mods=∅` → no fire, no swallow;
  lost Space-up then Space with `key_was_down=False` → fires; passthrough single → pass,
  double within 400 ms → fire.

**Child process** (`python -m projektsog.hotkey --child`): installs the `WH_KEYBOARD_LL` hook on
its main thread and runs `GetMessageW` there; the hook proc runs the state machine only
(`GetAsyncKeyState` for `held_mods`/`key_was_down`; foreground exe only for main-key downs).
**Capture mode**: after firing, every non-injected, non-modifier key event is swallowed and
buffered until the parent sends `end_capture` or 1.5 s pass; on `ok` they are replayed in order
with `SendInput` (injected → ignored by our hook) into the now-focused search field; otherwise
dropped — never delivered to the previous app. The hook is re-installed on the hook thread every
5 min while no key is held and after session unlock/resume. If the hook cannot be installed, fall
back to `RegisterHotKey` (report `mode: "registerhotkey"`). Stdin EOF → unhook and exit.
Protocol (JSON lines) parent→child: `{"cmd":"config","spec","passthrough":[…],"typing_guard_ms",
"double_tap_ms","enabled"}`, `{"cmd":"end_capture","ok":true}`, `{"cmd":"quit"}`;
child→parent: `{"ev":"ready","mode":"ll"}`, `{"ev":"fire","from_app":"Resolve.exe"}`,
`{"ev":"error","msg":"…"}`. The manager restarts a crashed child (max 3×/min).

### 10.4 `tray.py` and notification rules
```python
class TrayIcon:
    def __init__(self, icon_path: str, tooltip: str, *, on_show: Callable[[], None],
                 on_settings: Callable[[], None], on_scan_all: Callable[[], None],
                 on_set_follow: Callable[[str], None],       # "off" | "notify" | "open"
                 on_set_autostart: Callable[[bool], None], on_exit: Callable[[], None],
                 menu_state: Callable[[], dict]): ...
        # menu_state() -> {"hotkey_label": "Shift+Mellemrum", "follow": "notify", "autostart": True}
    def start(self) -> bool      # own thread: hidden TOP-LEVEL window + message loop
    def stop(self) -> None       # idempotent; from any thread except the tray thread
    def notify(self, title: str, text: str, level: str = "info") -> None
```
* Menu: "Åbn Projektsøg\t<hotkey_label>", "Indstillinger …", "Scan alle nu", "DaVinci Resolve ▸"
  (radio: "Fra" / "Vis besked" / "Åbn mappe automatisk"), "Start med Windows" (check),
  separator, "Afslut". Left click → `on_show`.
* Callbacks run on the tray thread and must return in < 50 ms; `on_exit` only sets the app's
  shutdown event (the main thread runs the exit sequence, incl. `tray.stop()`).
* Re-add the icon on `TaskbarCreated`; retry `NIM_ADD` every 2 s for 2 min at startup; load the
  icon at `GetSystemMetricsForDpi(SM_CXSMICON)` size; `notify()` uses `NIIF_NOSOUND |
  NIIF_RESPECT_QUIET_TIME`.
* **Who may notify**: (1) Resolve follow (§9 rules); (2) first sighting of a volume serial
  (§8); (3) the hotkey could not be installed (once per session). Nothing else.

## 11. HTTP API (`server.py`)

```python
class Server:
    def __init__(self, cfg: Config, bus: EventBus, indexer: "Indexer", bridge: "ResolveBridge",
                 controller: "Controller", *, web_dir: str | None = None): ...
    def start(self, port: int | None = None) -> int     # binds, serves on a thread, returns the port
    def stop(self) -> None
```
* `ThreadingHTTPServer` subclass: `daemon_threads=True`, `allow_reuse_address=False`,
  `request_queue_size=64`, `server_bind()` sets `SO_EXCLUSIVEADDRUSE`; bind `127.0.0.1`; on
  `WinError 10048` try port+1 … port+20. Handler: `timeout=60`, `protocol_version="HTTP/1.1"`
  for normal requests, **override `log_message()` → `log.debug`** (stock handler writes to
  stderr = crash under pythonw). SSE: `connection.settimeout(None)`, heartbeat comment every 15 s,
  exits on stop or client disconnect (unsubscribe in `finally`).
* Security: reject requests whose `Host` is not `127.0.0.1:<port>`/`localhost:<port>` (403);
  every non-GET request must carry `X-Projektsog: 1` (403); never send CORS headers; JSON bodies
  ≤ 1 MB.
* Errors: `ValueError` from Indexer/Bridge/Controller → 400 `{"error": str(e)}`; other
  exceptions → 500 `{"error": "Intern fejl – se loggen"}` + `log.exception`. User-level failures of
  open actions are HTTP 200 with `{"ok": false, "error": …}`.

| Method & path | Body / query | Response |
|---|---|---|
| `GET /`, `/app.js`, `/style.css`, `/assets/*` | | static UI (`Cache-Control: no-store`) |
| `GET /api/search` | `q, kind=all, online=0/1 (absent → None), source=<id>, limit, templates=0/1` | §7 response |
| `GET /api/recent` | `limit` | `{"results": [Item]}` |
| `GET /api/children` | `source, rel` | `{"results": [Item]}` |
| `GET /api/status` | | `Indexer.status()` + `"resolve": bridge.state()` + `"hotkey": controller.hotkey_status()` |
| `GET /api/sources` | | `{"sources": [Source], "hosts": [Host]}` |
| `POST /api/sources/<id>/mode` | `{"mode"}` | `Source` |
| `POST /api/sources/<id>/scan` | `{"full": false}` | `{"ok": true}` |
| `POST /api/sources/<id>/forget` | | `{"ok": true}` |
| `POST /api/scan` | `{"full": false}` | `{"ok": true}` |
| `POST /api/roots` | `{"path"}` | `indexer.add_root()` |
| `DELETE /api/roots` | `{"path"}` | `{"ok": true}` |
| `POST /api/hosts` / `DELETE /api/hosts` | `{"name"}` | `{"ok": true}` |
| `POST /api/open` | `{"path", "action": "folder"|"reveal"|"file"}` | `controller.open_path()` |
| `GET /api/resolve` | | `bridge.state()` |
| `POST /api/resolve/refresh` | | `bridge.refresh()` |
| `POST /api/resolve/open` | | `bridge.open_primary()` (used by the Resolve menu script) |
| `GET /api/settings` | | `{"settings": {…cfg.snapshot(), "run_at_login": bool}}` |
| `POST /api/settings` | partial dict | same shape; 400 `{"error"}` on invalid |
| `GET /api/time` | `?from&to` (local dates, default today) | `{"report": tracker.report(), "status": tracker.status()}` (§16) |
| `GET /api/time/status` | | `tracker.status()` |
| `GET /api/time/export` | `?from&to&round=<min ≤ 240>&detail=day` | CSV download (`Content-Disposition: attachment`) |
| `POST /api/window/hide` | `{"restore_previous": true}` | `{"ok": true}` |
| `POST /api/window/show` | | `{"ok": true}` |
| `GET /api/events` | | SSE: `event: <type>\ndata: <json>\n\n` |

`/api/settings` POST: pop `run_at_login` → `controller.set_run_at_login(bool)`; if `hotkey`
present validate with `hotkey.parse_hotkey` (ValueError → 400); then `cfg.update(rest)`.

**`Controller.open_path(path, action)`** (app.py) — the only place that opens things for the UI:
1. action ∉ {folder, reveal, file} → `ValueError`.
2. `loc = indexer.locate(path)`; if `loc` and not `loc["online"]` → `{"ok": false, "error":
   <hint>}` without any filesystem access (hint: local → "Tilslut disken ‘<disk_name>’";
   share → "Computeren <HOST> svarer ikke – er den tændt?").
3. `os.stat(path)` via `winfs.call_with_timeout(key=<source root or path>, 3 s)`:
   not found → `indexer.path_missing(path)` + `{"ok": false, "error": "Findes ikke længere –
   indekset opdateres"}`; timeout/other error → `{"ok": false, "error": "Placeringen svarer
   ikke"}` (no path_missing).
4. `winui.open_folder/reveal/open_file(path)` (activate) → Explorer comes to the front.
5. On success and `cfg["hide_after_open"]`: `window.hide()` (after step 4).
Returns `{"ok": true, "path": path}`.

## 12. UI (`web/`)

Single page, vanilla JS/CSS, no external resources, Danish, keyboard-first, dark theme by default
with `prefers-color-scheme: light` support. `document.title` is always exactly `Projektsøg`.
Supports `?q=<query>` and `?panel=settings` URL parameters (dev/testing).
* **Header**: big search field (placeholder "Søg efter projekt, mappe eller fil …"), status pill,
  visible ⚙ "Indstillinger" button. Debounce ~80 ms; cancel stale requests (AbortController).
* **Status pill**: "11 placeringer online · 1 offline"; while scanning "Scanner 2025Arkiv –
  120.000 filer"; during first indexing "10 af 11 placeringer klar · Scanner 2025Arkiv – 120.000 filer".
* **Resolve bar** (when running): "DaVinci Resolve: <project> → 📁 <primary name> (<source name>)"
  + [Åbn mappe] + [Opdater]; expandable list of all folders/other dirs with clip counts;
  suggestions labelled "Muligt match"; offline warning "⚠ <n> klip ligger på disken ‘<disk>’, som
  ikke er tilsluttet" (share: "… på <HOST>, som ikke svarer"); error text when not connected.
* **Resolve hotkey question card** (once): on `focus` with `from_app` ∈ {Resolve.exe, Fusion.exe}
  and `resolve_hotkey_asked == false`: "DaVinci Resolve bruger selv Shift+Mellemrum til
  effektsøgning. Hvad skal genvejen gøre, når Resolve er aktiv?" [Åbn Projektsøg] [Lad Resolve
  beholde den (tryk to gange hurtigt for Projektsøg)] → POST settings
  (`hotkey_passthrough_apps` ± `Resolve.exe`,`Fusion.exe`; `resolve_hotkey_asked: true`).
* **New disk card** on `new_volume`: "Ny disk ‘<disk_name>’ (<drive>) tilsluttet – <reason>" with
  [Medtag] when not included (sets mode include) and [Luk].
* **Empty query** → the Resolve primary (if any, pre-selected) then "Seneste projekter"
  (`/api/recent`).
* **Filters**: chips Alle · Projekter · Mapper · Filer; toggle "Kun online" (initial =
  !show_offline; the UI sends `online` only after the user toggled it); location dropdown.
* **Rows**: project/group/toplevel rows (name with highlights, badge
  `STUDIO-PC · Kunder 2026 (STUDIO)` or `Disk: <disk_name> (<drive>) · <display_name>` for local
  sources on non-system volumes, parent breadcrumb, "ændret for 2 dage siden", size, file count,
  subfolder chips → open `item.path + "\\" + chip` with action folder); dir rows; file rows with
  type icon (video/audio/image/sequence/project-file/document/other), size, date, project badge.
  Offline rows dimmed with "Offline – sidst set 12. sep." and the disk/host hint; Enter on an
  offline row shows the hint inline and keeps the window open (no `/api/open`).
* **Keys**: ↑/↓ select, Enter/double-click → dirs `folder`, files `reveal`; Ctrl+Enter → files
  `file`, dirs `reveal`; always send `item.open_path`. Ctrl+C copy path, Ctrl+Shift+C copy
  `unc_path`; Esc clears the query, Esc on empty → `POST /api/window/hide {"restore_previous":true}`;
  Ctrl+1..4 filters; Ctrl+, settings. The UI never calls `/api/window/hide` after an open (the
  server hides on success).
* **Focus**: on window `focus` and `visibilitychange→visible`, focus the input synchronously;
  never `select()`/clear at show time. SSE `focus` only refreshes the Resolve bar and triggers the
  question card.
* **Query lifetime**: record when the page became hidden; when shown again after ≥ 30 s hidden, or
  if the Resolve project changed meanwhile, clear the query and reset filters to defaults
  (on becoming visible, before the user types); otherwise keep the query and select it.
* On re-render (typing, `index_updated`), keep the selected row by item id (fallback: first row);
  don't apply an `index_updated` re-run while a key was pressed or the mouse moved over the list
  in the last 1.5 s.
* **Zero results**: "Ingen resultater for ‘<q>’" plus applicable reasons with one-click fixes from
  `hidden`: "<n> resultater skjules af filteret ‘Filer’ – Vis alle", "<n> på offline placeringer
  – Vis", "2025Arkiv indekseres stadig (120.000 filer indtil nu)", "<n> tilsluttede
  placeringer er ikke medtaget – Vis".
* **Settings panel** ("Indstillinger"): tabs **Placeringer** (sources grouped by computer: status
  dot, name, path, disk name, files, size, last scan, mode select "Automatisk / Medtag altid /
  Medtag aldrig" with the auto reason below, [Scan nu], [Glem] for offline; add root path; hosts
  add/remove), **Generelt** (hotkey text + "Global genvejstast" toggle (`hotkey_enabled`),
  "Lad DaVinci Resolve beholde Shift+Mellemrum (tryk to gange hurtigt for Projektsøg)" toggle,
  hide after open, show offline, "Start med Windows" (`run_at_login`)), **DaVinci Resolve**
  (enabled; follow Fra / Vis besked / Åbn mappe automatisk).
* Footer hint: "Shift+Mellemrum åbner Projektsøg overalt · Esc skjuler" (label from status.hotkey).
* Danish number/date formats (`toLocaleString('da-DK')`), sizes kB/MB/GB/TB, ≤ 200 rows.

## 13. App lifecycle (`app.py`, `__main__.py`, launcher, install)

`python -m projektsog [--background] [--no-window] [--port N] [--debug] [--rescan]`.

**Startup order**:
1. pythonw hardening: replace `None` `sys.stdout/stderr` with `open(os.devnull, "w")`;
   `SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOOPENFILEERRORBOX)`; `faulthandler.enable(file=<open
   log_dir()\crash.log>)`; `sys.excepthook`, `threading.excepthook`, `sys.unraisablehook` log;
   logging → `RotatingFileHandler(log_dir()\projektsog.log, 2 MB × 3)` only (+ console with
   `--debug` when a console exists); `os.chdir(app_dir())`; `winui.set_dpi_awareness()`;
   `winui.set_app_user_model_id("Projektsog.App")` (`app.AUMID`).
2. Single instance: `CreateMutexW("Local\Projektsog-<USERNAME>")` + `get_last_error() == 183` →
   `AllowSetForegroundWindow(ASFW_ANY)`, read `instance.json`, `POST /api/window/show`, exit 0.
3. Config → EventBus → Indexer.start() → ResolveBridge.start() → Server.start() → write
   `instance.json` `{"pid","port"}` → AppWindow → TrayIcon.start() → HotkeyManager.start() →
   (default) `Controller.show_window(reason="launch")`, (`--background`) `window.preload()` on a
   worker thread.
* **Controller** (app.py): `show_window(from_app=None, reason="api")` = `window.show()`;
  `hotkey.end_capture(ok)`; `indexer.on_window_shown()`; `bridge.on_window_shown()`;
  `bus.publish("focus", {...})`. Used by hotkey, tray left-click/"Åbn Projektsøg", `/api/window/show`,
  startup. Hotkey callback: if the window is visible and foreground → `window.hide(restore_previous=True)`
  + `end_capture(False)`, else `show_window(from_app, "hotkey")`. `hide_window(restore_previous)`,
  `open_path()` (§11), `hotkey_status()`, `get_run_at_login()/set_run_at_login()`.
* One bus-subscriber thread forwards `notify` events to `tray.notify()`.
* Config listener (record + wake an app thread): hotkey keys → `HotkeyManager.update()`
  (`hotkey_enabled` false → stop); publish `settings` and `hotkey` events.
* Hotkey start failure → one `notify` ("Genvejstasten <label> kunne ikke aktiveres …").
* **Exit** ("Afslut" / shutdown event, overall deadline 5 s): hotkey stop, tray stop, bridge stop,
  indexer stop (cancels scans, stops worker), server stop, window close, release mutex, delete
  `instance.json`, then `os._exit(0)` (a thread stuck on a dead share must not keep the process alive).
* `RUN_COMMAND` = `"<dir of sys.executable>\pythonw.exe" "<repo>\Projektsøg.pyw" --background`
  (both quoted). `Projektsøg.pyw` puts the repo root on `sys.path` and calls `projektsog.app.main()`.
* `install.ps1` (saved as **UTF-8 with BOM**): Start-menu shortcut "Projektsøg" → pythonw.exe
  `"<repo>\Projektsøg.pyw"` with the app icon and AppUserModelID `Projektsog.App` (= `app.AUMID`);
  autostart **on by default** (writes exactly `RUN_COMMAND`; `-NoAutostart` skips); copies the
  Resolve script(s) into the Utility folder; registers the Claude sessions' Resolve queue
  (`<python> koe.py installer`, SPEC §21.2) when `-Koe <path>` or `Davinci\resolve-koe\koe.py`
  beside the repo folder, `C:\Github\Davinci\…`, `%USERPROFILE%\Github\Davinci\…` or
  `%USERPROFILE%\Documents\GitHub\Davinci\…` is found (else a note, the install goes on);
  starts the app with `--background`; prints
  "Projektsøg kører – tryk Shift+Mellemrum hvor som helst" and "Genstart DaVinci Resolve for at se
  ‘Projektsøg’ under Workspace ▸ Scripts ▸ Utility". `config.DEFAULTS["hosts"]` is empty; the
  optional `-Hosts A,B` adds computers to `cfg["hosts"]` (names checked like
  `Indexer.add_host`, already listed ones kept) via `Config.update()` in Python — PowerShell
  never writes `config.json` — after the running app was stopped; an invalid name stops the
  script before anything changes. `uninstall.ps1` reverses it (stops the app via
  `instance.json` + `taskkill /PID`, removes shortcut/Run value/Resolve script; keeps the index
  unless `-RemoveData`).
* `README.md` (Danish): what it is, install, daily use, keys, settings, Resolve, troubleshooting,
  other PCs.

## 14. Tests
* `python -m unittest discover -s tests -t .` passes in < 60 s, without network, Resolve, windows
  or hooks.
* Smoke tests (`tests/smoke_*.py`, read-only, temp LOCALAPPDATA): index the real roots and run the
  §7.2 acceptance queries; Resolve read-only mapping of the current project; server under
  `pythonw.exe` answers `/api/status`; while the worker scans, a 200-row search stays < 250 ms.

## 15. Amendments v2.1 (after the adversarial code review, 2026-09-30)

These override earlier sections where they conflict. Finding ids refer to
`scratchpad/review_full.txt` (review round 1).

1. **Live online state** (XMC-1): the Indexer also publishes `index_updated {"source_id"}` when a
   source goes online/offline or its `current_path`/`unc_path` changes (skip the very first
   discovery pass after startup). The UI treats it as "re-run the view"; it clears stale row
   notices and never refuses to open a row whose source the live state (`state.sources`/status)
   says is online.
2. **"oe" spelling of ø** (SRCH-1): `textutil.alt()/fold_alt()/token_matches()` (given). A token
   `t` matches name `n` iff `t in fold(n)` or `alt(t) in alt(fold(n))`. Schema **v3** adds
   `entries.name_alt TEXT` (= `fold_alt(name)` when it differs from `name_fold`, else NULL) and
   indexes it as a second FTS5 column (`fts5(name_fold, name_alt, …)`). v2 → v3 is an **in-place
   migration** in `db.ensure_schema()` (add column, fill `name_alt` for rows whose `name_fold`
   contains "oe", recreate FTS table + triggers, `rebuild`) — no rescan. FTS queries per token:
   `"t" OR "alt(t)"`; the Python filter uses `token_matches`.
3. **Root is a project** (IDX-2): Source objects carry `root_is_project: bool` (Indexer knows it);
   `search/recent_projects` synthesise a project Item for such a source (rel_path `""`, name =
   display_name, kind project, mtime/size/file_count from the source) and use it as the
   ProjectRef of its entries.
4. **SourceRef/Source** gain `is_system: bool` (local source on the system volume). `disk_name` of
   an unlabeled system volume is `"Systemdisk"`. The UI uses `is_system` instead of assuming `C:`.
5. **Disk swaps at the same letter** (IDX-3): every scan command for a local source carries
   `"expected_serial"`; the worker verifies the volume serial of the root before the first listing
   and again before each unit commit / the shallow apply, and aborts without writing on mismatch
   ("Disken er skiftet"). winfs never reports a stale serial for a letter whose medium changed.
   locate()/map_paths(): when several sources share a matched root (a disconnected disk and the
   disk now mounted at the same letter), the source whose index actually holds the path wins,
   then the deepest indexed ancestor, then the online one — so an offline disk's paths keep their
   "Tilslut disken" hint instead of being attributed to the new disk (IDX-4, confirmed).
6. **Whole-volume decision is sticky** (IDX-1): once a volume serial has folder sources, a later
   root-level media file/project never flips it to a whole-volume source (and vice versa) unless
   the user forces it; superseded sources are never forgotten automatically.
7. **Sub-folders named like template folders** (`Klip`, `Musik`, …) are never classified as
   projects (ux IDX-3). **Sequences** collapse only dense runs (split into runs where the gap
   between consecutive frame numbers is ≤ 2; collapse runs with ≥ `sequence_min_files` members)
   (IDX-6). **Shallow-scan** entries get `mtime = dir_mtime` so new projects appear in "Seneste
   projekter" at once (IDX-5). `hidden` counts are computed so that each one-click fix really
   yields results (IDX-7/XMC-4).
8. **Hosts**: `remove_host()` forgets the host's non-manual share sources (UI asks "Fjern <HOST>
   og glem dens N delte mapper?") (LOC-1). `add_root()` refuses a path inside (or containing) an
   included source with "Mappen er allerede med i søgningen via ‘<name>’". Hidden top-level
   folders are not candidates.
9. **DaVinci Resolve**: all fusionscript access moves to a helper process
   `pythonw -m projektsog.resolve_child` (JSON lines), spawned by ResolveBridge only while
   Resolve.exe runs and stopped when it exits — the main process never loads fusionscript.dll
   (RES-3). The bridge re-derives online state from the live registry on every poll and re-maps
   the cached paths (no media-pool walk) when relevant sources change, at most every 5 s
   (XMC-2/RES-1). `open_primary(project: str | None = None, database: str | None = None)`: when
   given and different from the cached project → `{"ok": false, "path": null, "error": …}` + async
   refresh, so the menu script uses its own fallback; opens via live source path + timed stat
   (RES-2). The menu script POSTs `{"project": …, "database": …}`; the server passes them through.
10. **Hotkey**: the hook child treats our own app window (msedge.exe + class
    `Chrome_WidgetWin_1` + exact title `Projektsøg`) as a passthrough typing context (APP-1):
    a single Shift+Space types a space; a quick double press (within `hotkey_double_tap_ms`)
    still fires and the Controller then hides the window.
    `HotkeyManager.extend_capture(seconds)` lets the Controller keep captured keys while Edge is
    cold-launched; `AppWindow.needs_launch() -> bool`; AppWindow re-preloads (hidden) after the
    user closes the window (WIN-1). Hotkeys with Win/Alt as the only modifier send a mask key on
    release (HK-1). Edge background mode is disabled in the private profile so no msedge process
    lingers after the window closes.
11. **Install**: copy only `Projektsøg - Åbn projektmappe.py` (uninstall removes both names);
    never kill a PID from `instance.json` unless it is verifiably our pythonw/python process
    running `projektsog` (INST-1). Controller waits ~100 ms after a successful show before
    `end_capture(True)`; a hotkey that is not active at startup is re-checked after 10 s before
    the one-time notification. Exit deletes `instance.json` at the START of the exit sequence
    (and again before releasing the mutex) so scripts never talk to a dying instance; a second
    launch during exit waits for the mutex and then starts normally (APP-2).
12. **Round-2 contract additions** (second review, `scratchpad/rereview_full.txt`):
    * `Indexer.remove_host(name) -> {"ok": true, "forgotten": int}`; if a manual extra root lies
      on that host it raises `ValueError("Mappen ‘<root>’ ligger på <HOST> – fjern den først")`
      and changes nothing. `DELETE /api/hosts` returns that dict; the UI toast uses `forgotten`.
    * Source objects gain `last_shallow_scan` (float|null) and `volume_present` (bool: for local
      sources, the source's volume serial is currently mounted; for shares, the host answers).
      SourceRef gains `volume_present` too. Offline hints (UI and `Controller.open_path`): not
      online but `volume_present` → "Mappen findes ikke længere"; otherwise the disk/host hint.
    * Search: a token that matches only via `alt()` gets no exact/prefix/word-start bonus and a
      −60 penalty; `alt()` matching is only used for tokens whose `alt()` form has ≥ 3 chars.
    * A root folder whose own name is a project template dir (`Klip`, `Musik`, …) is never a
      root project.
    * The Resolve follow notification uses the same `volume_present` rule; `offline_disks` lists
      only disks that are really disconnected (round 3, R3-RES-1).
    * UI focus rules: settings tab panels are not focusable; while the settings panel is open no
      result key (↑/↓/Enter/PgUp/PgDn …) acts on the hidden result list and the covered content
      is `inert` (round 3, R3-UI-1/2).

13. **Memory cards pass through** (2026-10-02): on hot-plug volumes the top-level folders in
    `skip_card_dirs` (XDROOT, PRIVATE, DCIM, MP_ROOT, AVF_INFO, CONTENTS) are no candidates –
    camera cards are the import helper's (§17). A local source with `hotplug`, mode `auto`, not
    manual, `0 < volume_size ≤ 512 GiB`, no projects (and not a root project or template) that
    has been offline for `PASSING_CARD_GRACE_S` (120 s; after a restart: since `last_seen`) is
    forgotten like "Glem" (its entries deleted). Every card, and every format of one (a new
    serial), would otherwise leave one more offline location. Stale volumes (presence unknown)
    are left alone; included-by-choice, excluded-by-choice, project and big disks stay.

## 16. Time tracking (`timetrack.py`, main process)

`TimeTracker(cfg, bridge)` starts after the ResolveBridge and stops before it. Every `TICK_S`
(5 s) it reads: the foreground window (`winui`), seconds since the last input
(`GetLastInputInfo`) and `bridge.activity()` — `{project, database, uid, page, timecode,
rendering, folder}` from the Resolve helper's poll (`GetCurrentPage`, the current timeline's
`GetCurrentTimecode`, `IsRenderingInProgress`; `None` when older than 15 s).

* **Counts** when `Resolve.exe` is in front with a project open (bucket = page), or a browser
  whose title contains one of `time_music_sites` (bucket `musik`) or `time_ai_sites` (bucket
  `ai`, AI video/images such as Higgsfield) while a project is open.
  Never for `Untitled Project…` or without a project.
* **Activity** = input, or a playhead that moved since the last tick (not while rendering) — the
  latter only while the last input is ≤ `PLAYBACK_MAX_S` (1 h) old.
* A pause ≤ `time_idle_minutes` counts fully; a longer one is cut back to the last activity
  + `GRACE_S`. Another program in front while a project is open is the same kind of pause
  (state `away`, `away_since` = the segment's end when Resolve was left, `away_until`): back in
  Resolve in time, the segment simply continues (the excursion counts); not back in time (or
  Resolve closed meanwhile), it ends where Resolve was left. A gap > `GAP_S` between ticks
  (sleep) ends it at its last saved end. Segments < 1 s are dropped.
* Resolve answers scripts slowly while busy, so the tracker accepts its last answer for
  `STALE_OK_S` (120 s; `activity(max_age)`, `age` in the answer). The bridge waits up to
  `CHILD_CALL_TIMEOUT_S` (60 s) for a poll before it restarts the helper, and forgets the
  activity when Resolve's process is gone.
* Storage: `time.db` (local SQLite, WAL) table `segments(project, database, uid, folder, bucket,
  start, end, host)`, epoch seconds; the running segment's end is saved every 30 s.
* `report(first, last, minimum_s=None)`: per (project, database), sorted by total desc:
  `buckets`, `days`, `day_buckets` (per local day, split at midnight), `total_s`, `folder`,
  `last`; plus `total_s` and `buckets` (labels in display order). Includes the running segment.
  A project with less than `minimum_s` in the period (default `time_min_minutes` × 60, config
  default 3, 0–60, 0 = all) is left out of `projects`, `total_s` and the export; `skjult:
  {projekter, total_s, minimum_s}` tells the Tid tab ("6 korte besøg under 3 min er ikke med").
  `status().today_s` counts everything (`minimum_s=0`).
* `export_csv(first, last, round_minutes, per_day)`: UTF-8 BOM, `;`, decimal comma, hours with
  two decimals; rounding is UP to whole steps, per row (per project, or per day with `per_day`).
* `status()`: `{state: recording|idle|paused|no-resolve|off, enabled, project, bucket,
  bucket_label, folder, since, today_s}`.
* UI: header button (today's total; dot = recording) opens the settings panel's **Tid** tab
  (live status, periods Monday–Sunday / whole months, per-day rows, rounding, export, the three
  settings). Polls `/api/time/status` every 60 s while visible, the report every 10 s while the
  tab is open; nothing while hidden.

## 17. Import helper (`importer.py`, main process)

`Importer(cfg, bus, indexer, bridge, tracker, controller)` is created after the Controller,
started at the end of `App.start()` and stopped right after the tray (a running copy is
cancelled and its temp file removed).

* **Cards.** A watcher thread calls `winfs.list_volumes()` every `POLL_S` (2 s; ~2 ms). A
  removable (`drive_type` 2) or hotplug volume that is not the system volume is a card when its
  root holds `XDROOT\Clip` / `PRIVATE\XDROOT\Clip` (Sony XDCAM: FX9, FS7),
  `PRIVATE\M4ROOT\CLIP` (Sony Alpha/Cinema Line, plus `DCIM\*MSDCF` stills), `DCIM\*MEDIA` /
  `DCIM\DJI*` (DJI) or `DCIM\*GOPRO`. Card id = `<serial>@<drive>`. Files = everything in those
  folders, flat. The model comes from the first clip's `…M01.XML` `modelName`
  (`PXW-FX9V`, `PXW-FS7`, `ILCE-7SM3`); `import_camera_folders` (`"<model prefix>=<folder>"`,
  longest prefix wins; else a folder named like the clip prefix; else the model) gives the
  `Klip` subfolder. Recording span = `CreationDate` of the first/last clip (display only: camera
  clocks can be wrong). Cards present at startup are listed but never announced.
* **Empty cards** are cards too, so one going in never looks unnoticed: those folders without a
  file (formatted in the camera; `camera` from the layout: XDCAM/M4ROOT → "Sony", DJI, GoPro),
  or a removable (`drive_type` 2, never a hard disk) volume with nothing but
  `System Volume Information`, `$RECYCLE.BIN` and dot-entries at its root (`camera` null,
  `kinds` [], `folder` = the root). `blank: true` = empty when it went in. `plan`/`start_import`
  → 400 "Kortet er tomt – der er ingen klip at overføre"; without auto-open the notification is
  "<camera>-kortet i E: er tomt". The UI: "Sony-kort i F:" ("Kort i F:" without a camera),
  "Ingen filer · 119 GB-kort · Kortet er tomt – der er ingen klip at overføre"; the Import tab
  hides "Hvor skal klippene hen?" and the transfer box and (for `blank`) says the card was read
  without errors, it just holds no clips.
* **Already imported** = the same name AND size, for every file of the card (clips and their
  XML/BIM sidecars). `Indexer.find_files` only nominates folders (never the card's own volume);
  up to 8 of them are listed NOW (`call_with_timeout`), and only a folder that cannot be listed
  is judged by the index. `found = {clips, files, total, complete, projects: [{name, path,
  folder, clips, files, complete, online}]}`. Clip names repeat (camera counters wrap), so
  never the name alone.
* **Announcing** every card that goes in (also a fully imported one, which the UI reports as
  "Alle klip er overført til …"): `controller.show_window(reason="card", panel="import")` when
  `import_auto_open`, else a `notify` event. Bus events: `cards` (`{"cards": [...]}` on every
  change) and `import` (job state, ≤ 4/s).
* **Targets** (`options`): projects holding some of the card's clips, Resolve's `primary`, today's
  imports, projects worked on today (`TimeTracker.folders_on` → `Indexer.projects_named`),
  projects created here in the last 7 days; each with free space. **Disks** = parents of the
  `KIND_TEMPLATE` folders (`Indexer.templates`) with free space and `fits` (card + 512 MiB).
* **Plan**: `<project>\Klip` (existing casing, else created) `\<camera>`; files there with the
  same name and size are skipped; a same-name/other-size file (a conflict) or `separate` moves the
  target to the first free `<camera> Dag N` sibling. All file system work runs through
  `call_with_timeout`.
* **New project**: `validate_project_name` (≤ 2 parts, no `<>:"/\|?*`, no reserved names or
  trailing dots), only under a listed disk, never over an existing folder; the template tree is
  copied (files ≤ 50 MB), else `import_project_dirs` are made. `Indexer.refresh_path` rescans.
* **Copy** (`ImportJob`, one at a time): per file a reader thread reads 8 MiB chunks and hashes
  them (SHA-1) while the job thread writes `<name>.projektsog-tmp` (`xb`, fsync); then the temp
  file is read back without the file cache (`FILE_FLAG_NO_BUFFERING`, aligned `VirtualAlloc`
  buffer; buffered read as fallback) while, in parallel, the card file is read a second time
  (also uncached). All three hashes must match; a mismatch is retried once, then the job fails
  ("ikke identisk" for the copy, "læst forskelligt to gange" for the card). Only a verified file
  gets its mtime and is renamed (`os.rename` never replaces). Cancel/errors remove the temp
  file; finished files stay, so starting again resumes. Free space is checked first.
  "prepare" only makes the target and opens the card's clip folder and the target in Explorer.
* **Move** ("Klip", like Ctrl+X but checked): as copy, plus every card file that is already in
  the target with the same name and size is compared by content (uncached hashes). Only when
  EVERY file is verified: each copy made is fsynced (data, size, directory entry), the target
  volume cache is flushed (best effort), and then per file - after checking that the copy still
  has the same size and the card file the same size and mtime - the card file is deleted (an
  in-use file stays and is counted as `kept`). A stop or failure before the deletion leaves the
  card untouched; during it, the rest stays on the card. Only the listed files are deleted
  (never folders, MEDIAPRO.XML or thumbnails). Afterwards the card is listed again.
* **Manifest**: `imports\<date time> <camera> <drive>.jsonl` in the app dir, one JSON record per
  line, flushed and fsynced per line: start, every verified file (`name, size, sha1, source,
  copied`), every deletion, end - so after a crash it shows exactly what was verified and deleted.
* History (`imports.json` in the app dir, last 300): imports `{at, mode, camera, model, serial,
  project, target, files, bytes}` and created projects `{at, path}`.
* API: `GET /api/import` → `{cards, job, history}`; `GET /api/import/options?card`;
  `GET /api/import/plan?card&project&separate`; `POST /api/import/project {root, name}`;
  `POST /api/import/start {card, project, separate, mode: copy|move|prepare}`;
  `POST /api/import/cancel`; `POST /api/import/dismiss {card}`. Errors are 400 `{"error"}`.
* UI: the settings panel's **Import** tab (card, suggestions + search + new project with disks,
  target and what is new, Kopiér og kontrollér / Kun opret mappen) and a card above the results
  per inserted card (progress while copying). "Klip" needs a second, confirming click.

## 18. Klippe, the pet widget (`widget.py`, `web/widget.*`)

* `PetWindow(cfg, url)` is created with the UI (`App.start`), closed with the main window on
  exit. A thread follows `widget_enabled` (default off): on → Edge `--app=/widget.html` with its
  own profile (`config.widget_profile_dir()`, independent of the search window), started
  minimised without activation, found by its exact title `Klippe – Projektsøg`, placed at the
  bottom right of `choose_monitor(monitors(), widget_monitor)` ("auto" = the first secondary
  monitor left to right, else the primary) or at `widget_position` ("x,y", only while that
  point is on a monitor), shown without activation, `HWND_TOPMOST` while `widget_on_top`; the
  focus is handed back if Chromium took it. A position the user dragged it to is saved once it
  stood still 3 s. Closed with X (not by us) → `widget_enabled` is switched off.
* The page polls `/api/time/status` every 5 s and `/api/time` (last 400 days) every 10 min, and
  listens to SSE `import`, `cards` and `settings`. Mood: recording → working, away → waiting,
  idle → sleepy, paused → chill, off/no-resolve → sleeping. Outfit by bucket (color, fusion,
  fairlight/musik → audio, deliver). Celebrations (once per day and key, kept in localStorage):
  every whole hour today, `widget_daily_goal_hours`, 25/50/90 min of unbroken focus
  (recording/away), a finished import (copy or move); break nudges at 90/150/210 min. What is
  already reached when the page loads is not celebrated. Growth by all hours logged: egg (0),
  baby (5), junior (25), pro (100), legend (300); level = 1 + ⌊√(2h)⌋; streak = days in a row
  with ≥ 1 h (ending today, or yesterday while today is under an hour). Effects on a canvas;
  `prefers-reduced-motion` shows only words and an emoji.

### 18.5 Trophies and wardrobe (`achievements.py`)

* `PetProgress(cfg, bus, tracker=, importer=, path=pet.json)` recomputes every 5 min (first
  after 8 s; at once after a game): `compute_stats()` from all time segments, the import history
  the game counters and the meals → `TROPHIES` (47: growth/levels, rhythm, goal, focus, pages,
  projects, cards, Klippe, food, seasons, secrets). A trophy, once earned, is kept with its time. They reward
  steady work, breaks, variety and going home on time – streaks count workdays only (weekends
  neither count nor break them); nothing rewards overtime or nights.
* Wardrobe slots: farve, striber, hat, briller, mund, haand, aura (`ITEMS`; one default per
  slot). A trophy may give one item. `FINDS`: rare and legendary items (incl. the AWP, the cool
  shades, the cigarette) are found on workdays with ≥ 1 h, decided by
  `sha256(<this PC's secret>|<day>|<item>)` < chance – the same every time, different per PC.
* `GET /api/pet` → `{trophies: [...], items: [...], equipped, slots, unlocked, total}` (secret
  locked trophies: name "???", text "Hemmelig", no reward). `POST /api/pet/equip {slot, item}`
  (only owned items) → SSE `pet_look {equipped}`. New trophies/finds → SSE `pet_progress {nye,
  foerste, unlocked, total}`: the widget celebrates (fireworks for legendary; the very first
  computation gives one summary line).
* The widget's 🏆 button opens a panel (trophies with progress, wardrobe with locked items and
  how to get them). Worn items are `data-<slot>` attributes on `#app` and on every sprite cell
  (`widget.html?…&pynt=slot:item,…`), so Klippe wears them outside the box too. With the AWP the
  game always includes "snipe": Klippe tosses the pointer away and hunts it (`hunt_plan`). The
  pointer stands still; a red laser sight runs from the end of the barrel (the right-most pixel
  of the aiming pose `aim`, mirrored to the left, ±55°) to where Klippe aims – independent of the
  pointer: a baby's aim, a spring (ω 7, ζ 0.5) with trembling that searches its way there – and
  Klippe shoots once it is steady (impatient after 1.7 s). Planned: 1–2 misses (aimed 28–55 px
  off) and 1–2 hits in random order, then the kill; what the laser really points at decides (a
  shot far off is a miss after all). A hit knocks the pointer 110–220 px away (it stays still
  there) while Klippe reloads (0.4 s, the laser kicks up) and has to find it again; the bullet's
  sparks land where the laser pointed. The kill drops the pointer to the floor, and Klippe fetches
  it. The laser is drawn into a work-area-sized canvas but only its own box is wiped and shown
  (`UpdateLayeredWindow` with a source offset). The panel also has "🎮 Lad Klippe lege nu" (= "Vis legen nu").

### 18.6 Hunger and food (`achievements.py`, `web/widget.*`)

* `PetProgress` keeps `mad` in pet.json: `maet` (satiety 0–100) anchored at `ved` (time) and
  `arbejde_s` (`TimeStore.total_s()`, all time ever logged), `energi {item, fra, til}` (the rush),
  `drop {fra, til}` (the drip's own bag), `spist` (item → times, kept forever) and `log` (the last
  300 meals as [time, item]). On every read and every refresh it is brought up to now by
  `satiety_after()`: −20 per hour of logged work, −4 per hour of other time but never below 30 by
  that (no one comes back to a starving Klippe after a weekend). A new Klippe starts at 35. When
  the time store cannot be read (closed on exit, busy) the anchor is left as it is; when its total
  goes down (the tracker trims a pause it had counted), the burnt satiety is given back.
* `MENU`: Durum (+60), Big Mac (+45), Chicken McNuggets (+30), Pommes frites (+20) – food;
  Faxe Kondi Booster (+10, ⚡ 20 min) and Monster Mango Loco (+12, ⚡ 25 min) – energy drinks;
  Booster-drop (+15, ⚡ 45 min) – Booster on a drip. A rush stacks on what is left of the last one,
  never beyond an hour from now.
* `GET /api/pet/mad` → `{maet, energi: {item, name, fra, til, left_s} | null, drop: {fra, til} |
  null, spist, menu: [{id, name, kind (mad | drik | drop), points, energy_min, mcd}]}`. `POST /api/pet/mad {item}` →
  `{ok, spiste, grund, item, mad}`: food is refused at ≥ 90 (`grund` "maet"); a fourth energy
  drink or drip within two hours too ("hjerte"). A meal → SSE `pet_mad` (the same as GET) and the
  trophies are recomputed at once.
* Trophies "Mad": Velbekomme (anything), Durumkongen (10 durum → hand item Durum), Stamkunde
  (10 from McDonald's → Pommes frites), Booster-holdet (10 Booster → the can), Loco for mango (10
  Mango Loco → the can); secret Sukkerchok (3 energy drinks/drips on one day → aura Lyn).
* The widget (not an egg) shows a "Mæthed" row with a 🍔 Mad button → a tray with the menu
  (icons are the same drawings, `#mad-<id>`). Levels: mæt ≥ 70, fin ≥ 35, sulten ≥ 12,
  skrubsulten below (`data-sult`). Hungry: it dreams of one dish (picked per hunger, kept in
  localStorage; feeding exactly that one gives an extra happy line), wavy mouth, a growling stomach;
  starving: a sad mouth and drool. It says so when it gets hungrier, never on the first load
  (except one hello per day). Eating (`data-spiser`, ~5 s): the dish flies from the hand to the
  mouth, four bites (an SVG mask), the rest is thrown away; a drink is tilted up, crushed and
  thrown, then "BØVS!"; the drip's stand rolls in and its tube goes to the left hand. While a rush
  lasts (`data-energi`): lightning, a glow, it cannot sit still, ⚡ before the work line. The drip
  (`data-drop`) stays until its own 45-minute bag is empty – also when a drink is taken meanwhile –
  with the bag emptying (`--drop`) and that hand kept still. A trophy that arrives while it eats is celebrated after
  the meal. The food state is polled every minute.

### 18.4 Klippe plays (`petplay.py` main process, `petplay_child.py` helper)

* `PetPlay(cfg, bus, widget=, bridge=, importer=, base_url=)` is created with the widget and
  closed first on exit. A thread checks once a second (5×/s while "Vis legen nu" waits). A game
  starts only when: `widget_enabled` and `widget_play` (default on) are on; the widget window is
  shown; the widget page has reported its look (`POST /api/widget/look` `{stage, outfit, pet:
  {x,y,w,h}, view: {w,h}}` in CSS px, on every change and every minute); nobody touched mouse or
  keyboard for `widget_play_idle_minutes` (1–60, default 5; `GetLastInputInfo`, which
  `SetCursorPos` does not change); this pause has not had its game yet (keyed by the last-input
  tick); the screen is not locked (input desktop ≠ "Default"); "activate a window by hovering"
  is off; no import job is copying/verifying/deleting; Resolve is not playing back (playhead
  moved < 8 s ago, or its last answer is > 12 s old while not rendering — a render is fine);
  nothing is full screen (`SHQueryUserNotificationState` busy/D3D/presentation, or the front
  window covers the widget's monitor). Then a die decides once per pause: egg 0, baby 1,
  junior ½, pro ⅕, legend ¼.
* `POST /api/widget/play` ("Vis legen nu"): 400 unless `widget_enabled`; waits ≤ 20 s for the
  mouse to be still 1.5 s and then plays regardless of pause, age (an egg plays as baby),
  transfer, Resolve or full screen (locked screen and hover-activation still stop it).
  `GET /api/widget/play` → `{state: ready|waiting|out, message, enabled}`. SSE `pet` carries the
  same plus `reason` (done | touched | locked | quit | error).
* Sprites: headless Edge renders `widget.html?sprites=normal,happy,cheer,oops&stage=&outfit=&cell=240`
  (every pose in a 240-px cell, nothing animated, transparent background, 2×) to
  `%LOCALAPPDATA%\Projektsog\pet\klippe-<stage>-<outfit>-<sig>.png`; `sig` hashes
  widget.html/css/js, so a new drawing renders anew and old sheets are removed.
* The helper (`pythonw -m projektsog.petplay_child`, per-monitor DPI aware) draws the pet with
  GDI+ in a layered window (`WS_EX_LAYERED|TRANSPARENT|TOPMOST|TOOLWINDOW|NOACTIVATE`,
  `HTTRANSPARENT`) that moves with it, and plays: shake + somersault out of the widget, fetch the
  pointer (or reach over to the screen it is on and pull it here), 3 acts for a baby / 2 later
  (ride, fly, throw, spin), give the pointer back exactly (or toss it back to its own screen),
  fly home. The pointer stays inside the work area minus 36 px. It stops at once when the
  last-input tick differs from the one the main process decided on, or the pointer is not
  where it was put (± 2 px): the pointer goes back where the user left it and the pet flies home
  without it. A locked screen, `quit`/stdin EOF or 90 s end it immediately. Never clicks,
  scrolls or types. stdout: `{"event":"out"}`, then `{"event":"home","reason":…}`.

## 19. Messages from other programs (`messages.py`)

* `MessageBoard(cfg, bus, shown=)` is created in `App.start` (before the server starts). The
  Claude sessions' Resolve queue (`koe.py`, outside this repo) is the sender: it finds the port
  in `instance.json` and calls the API with `X-Projektsog: 1`.
* `POST /api/messages` `{tag (≤ 80, required), titel (≤ 120), tekst (≤ 400, may hold "\n"),
  knapper: [{tekst (≤ 40), uri}] (≤ 3), session (≤ 40), udloeber (s, 1–86400, default 3600),
  lyd (bool, default true), visning ("kort" | "boble"), prioritet ("normal" | "stille")}` →
  `{"ok": true, "vist": bool}`. `stille` (a session that is done and needs no answer) never
  sounds and never opens a card by itself.
  `vist` = Klippe is on (`widget_enabled`) and its window is shown – only then may the sender
  skip its own notification. Same tag replaces; ≤ 20 messages (oldest dropped); expired ones
  are pruned. A card: SSE `messages {"messages": [...]}` (newest first; `tid`, `udloeber_ved`,
  `lyd` added) and, when shown and the title/text is new and `lyd`, Windows'
  "SystemNotification" sound – except a call (§21.1), which rings instead. `visning: "boble"` is a passing note: SSE `say {"tekst"}`,
  nothing kept (a card with that tag is removed).
* `DELETE /api/messages {tag}` → `{"ok": true}` (also for an unknown tag). `GET /api/messages`.
* `POST /api/messages/click {tag, knap}` → the message is removed and its button's uri is
  opened with `os.startfile` (ShellExecute → the scheme's registered handler; Projektsøg runs
  no command itself). Only `URI_SCHEMES` (`resolvekoe`) are accepted at POST and click; no
  spaces or control characters. An unknown tag/button → 400 "Beskeden er der ikke længere".
* The widget shows ONE card at a time right under the pet, above a transfer, with its buttons,
  × and – when there are more – "‹ 1 / 3 ›". Order: normal before quiet, newest first; a new
  normal message comes to the front and makes Klippe jump and clap (not for `lyd: false`; an
  unanswered call is shown as a ringing phone instead of its card, §21.1).
  Quiet messages alone are folded into one line "📬 2 beskeder · vis" (click: the cards, ▾ folds
  them again). Text is clamped to 4 lines. Messages are never shown in the search window.

## 20. New versions from GitHub (`updater.py`)

* `Updater(cfg, bus, repo_dir=REPO_DIR, data_dir=app_dir(), autostart=controller.get_run_at_login)`
  is created in `App.start` and closed with the windows. The source is the public repo
  `JensFoghable/projektsog`, branch `main`. Its thread checks 20 s after the start (it first
  shows a pending "Projektsøg er opdateret" notification), then every 6 h and on request.
  Nothing is installed without the button.
* A downloaded folder (no `.git`): `GET api.github.com/…/commits?sha=main&per_page=1` and
  `…/git/trees/<sha>?recursive=1`; every file of that tree is compared with the folder by its git
  blob hash (also after CRLF → LF, for autocrlf copies). Identical → the folder *is* that version:
  `update.json` remembers `installed {sha, date}` and its `files`. Install: the zip of exactly that
  sha from codeload.github.com (≤ 64 MB) must be whole (`testzip`), have one top folder, no path
  outside it, `Projektsøg.pyw`, `install.ps1`, `projektsog/__init__.py`, `projektsog/app.py`, and
  every `.py`/`.pyw` must compile – otherwise nothing is touched. Then each differing file is
  written as `<file>.ny` and `os.replace`d, the old one copied to `update-backup\`; files in the
  previous `files` list that the new version lacks are removed (backed up too). Any error puts
  every file back. Files the updater never listed are never removed.
* A git working copy: `git fetch --no-tags <repo url> main` (no prompt, no window), then
  `HEAD == FETCH_HEAD` or FETCH_HEAD an ancestor → up to date; HEAD not an ancestor → blocked
  "Mappen har sine egne commits …"; a changed tracked file → blocked "Der er ændrede filer …";
  no git → blocked. Install = `git merge --ff-only FETCH_HEAD`.
* Then `powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File install.ps1
  [-Python <python.exe next to sys.executable>] [-NoAutostart when "Start med Windows" is off]`,
  detached (breaks away from a job when allowed), output in `logs\opdatering.log`. It stops this
  app (`POST /api/quit`) and starts the new version; still running after 120 s → the error
  "Projektsøg blev ikke genstartet …" (the files are new; the next start runs them).
* `GET /api/update` → `{mode: "zip"|"git", installed: {sha, date}|null, latest: {sha, date,
  title}|null, available, blocked: str|null, busy: null|"checking"|"downloading"|"installing"|
  "restarting", checked, error}`; every change → SSE `update` (same dict).
  `POST /api/update/check` → the state (busy "checking"). `POST /api/update/install` → the state
  (busy "downloading"); 400 "Der er ingen ny version", the `blocked` text, "Søger efter en ny
  version – vent et øjeblik" or "Opdateringen er allerede i gang".
* UI: Indstillinger ▸ Generelt ▸ **Opdatering** (above the version line): "Du har den nyeste
  version · Version fra 5. okt. · tjekket for 5 minutter siden" + [Søg efter opdatering]; "Ny
  version klar · Fra 5. okt.: <commit title>" + [Opdater nu] (primary) and an accent dot on ⚙;
  busy texts with the button disabled; `blocked`/`error` as a warning hint.

## 21. The phone and the robot crew (`messages.py`, `crew.py`, `crew_child.py`, `web/widget.*`)

### 21.1 A call rings (`messages.py`, widget)

* A **call** is a card (`visning` "kort") with `prioritet` "normal" and at least one button –
  today only the queue's "🎬 Mette vil bruge Resolve · Byg nu" (`koe:venter`). Messages that
  just wait for the user (no buttons: "🔐 … skal have lov", "Claude · …", "💬 …") never ring.
* Every stored message gets `opkald` (bool: it is a call) and `besvaret` (bool). A call posted
  with `lyd` true and a new tag or a new title/text starts unanswered (`besvaret` false); a call
  posted with `lyd` false (the queue re-posting it silently) keeps `besvaret` of the message it
  replaces, or is answered (`besvaret` true = a plain card) when there was none. A non-call is
  always `besvaret` true. Both fields are kept in messages.json.
* **Ringing**: while Klippe is shown (`shown()`), a new unanswered call rings (`ringer`, the phone
  shakes in the widget) until it is answered, clicked, removed, expired, or for `RING_S` = 30 s
  (the call stays unanswered: a
  missed call), or as soon as `widget_enabled` is switched off (a config listener; the call stays
  missed). Its sound, when the phone starts ringing: Klippe's own short, quiet ring (< 1 s, peak
  0.2 of full scale: two little bell trills and a clapper "klap", made by `ringtone_samples()` into
  `%LOCALAPPDATA%\Projektsog\klippe-ring-1.wav`, played once with `SND_FILENAME|SND_ASYNC`) instead
  of "SystemNotification"; with `widget_ring` off "SystemNotification" once (a call that starts
  ringing while another already rings gets the notification too). Sound calls are injected
  (`ring(on: bool)`, `sound()`); tests never make a sound.
  Snapshot field `ringer` (bool) = this call is ringing now; every change publishes `messages`.
* `POST /api/messages/svar {tag}` → `{"ok": true}`: the call is answered (`besvaret` true,
  `ringer` false, the ring stops if no other call rings); unknown tag → 400 "Beskeden er der
  ikke længere". A click (`/click`) or × also ends its ringing.
* Internal scheme `projektsog:` – only for messages Projektsøg posts itself (`post(data,
  internal=True)`); `click` hands such a uri to the `on_internal(uri)` callback instead of
  `os.startfile`. Posting it from outside → 400 like any other scheme.
* Widget: an unanswered call is not a card. Klippe holds a ringing red phone (it shakes, "RING!"
  marks; still and with a red "1" when missed), the messages area shows one line-card "📞
  Mette ringer" (caller: `session`, else the name in "🎬 <navn> vil bruge Resolve", else
  "Claude"; missed: "📞 Ubesvaret opkald fra Mette") with the button "Tag telefonen" and ×.
  Clicking the phone or the button answers (`/svar`): Klippe holds the receiver to its ear
  ("Hallo? 📞", 1.6 s), then the real card appears (glowing) with "Byg nu". No jump/clap for a
  call (the phone is the alarm). Reduced motion: no shaking.

### 21.2 Building (`crew.py` – `KoeWatch`)

* The queue's state file is found through its registered link handler, never a fixed path:
  (default) of `HKCU\Software\Classes\resolvekoe\shell\open\command` (else
  `HKEY_CLASSES_ROOT\resolvekoe\shell\open\command`), split like a Windows command line, the
  argument ending in `koe.py` → `<its folder>\state\koe.json`. Not found → no builds (looked up
  again every 60 s). The file is only read (no mutex, never written); a failed read (being
  replaced, bad JSON) keeps the last answer.
* A **build** is `holder` = `{navn, projekt, opgave, pid, siden, opdateret}` with a str `navn`,
  `now − opdateret ≤ 3600` s and its pid alive (`OpenProcess`; no pid = alive). Read every 2 s.
* **Demo**: `POST /api/bygger/demo {opkald: bool}`: `opkald` false → a demo build ("Demo" ·
  "Robotterne øver sig") for `DEMO_S` = 45 s; `opkald` true → Projektsøg posts a demo call
  (tag `demo:opkald`, "🎬 Demo vil bruge Resolve", button "Byg nu" → `projektsog:demo`) whose
  click starts that demo build. 400 when Klippe is off ("Slå Klippe til først").
* State and SSE `bygger`: `{aktiv, navn, projekt, opgave, siden, demo, ude, retning, faerdig,
  varighed_s}` – `ude`: the robots are out on the screen now; `retning` ("venstre"|"hoejre", null
  when home): the side of the box they went out through; `faerdig` true only in the one event
  that ends a build (with `varighed_s`). Published on every change. `GET /api/bygger` → the
  current state (`faerdig` false); the widget asks again whenever its event stream (re)opens and
  every 30 s while a build is shown, so a lost event never leaves it building.

### 21.3 The robots (`crew.py` – `Crew`, helper `crew_child.py`)

* `Crew(cfg, bus, *, widget, watch, look, wardrobe, petplay_busy, base_url, probe, sprites,
  spawn, clock)` – a thread (every 0.5 s). While a build is on, the robots come out of the
  widget onto its monitor when ALL hold: `widget_enabled` and `widget_crew` (default on), the
  widget window shown, its look reported (`PetPlay.look()`), the build running ≥ 2 s, no mouse
  or keyboard input for `CREW_IDLE_S` = 3 s (`GetLastInputInfo`; the "Byg nu" click itself is
  input), not locked, nothing full screen (`windows_quiet` / `fullscreen_beside`), no Klippe
  game (`petplay_busy()`), the robot sprites rendered. A demo build only needs the first three,
  the idle rule and the lock rule.
* Any input sends them home at once (the helper sees the input tick change): they run back into
  the box within 0.5 s and the helper ends ("touched"); they come out again after the next
  3 s of stillness while the build lasts. When the build ends while they are out, the helper
  gets `done`: the timeline gets a shine, the robots cheer (1.2 s) and march home (≤ 2 s).
* PetPlay starts no idle game while a build is on, and "Vis legen nu" is refused then ("Klippe
  dirigerer robotterne lige nu 🤖"); it asks again after drawing its sprites, just before the
  game starts. `PetPlay.busy()` = a game child, one being launched, or a manual wait.
* Number of robots by Klippe's stage: egg 4, baby 5, junior 7, pro 9, legend 12.
* **Sprites**: headless Edge renders `widget.html?sprites=<ROBOT_POSES>&cell=120` (the robot
  branch of the sprite page, every pose in a 120-px cell, 2×, transparent) to
  `%LOCALAPPDATA%\Projektsog\pet\robot-<sig>.png`, size (120·7·2, 240); `sig` like Klippe's
  sheets (widget files + poses); old `robot-*.png` are removed. `ROBOT_POSES` = `robot-a`
  (standing/walk 1, the reference), `robot-b` (walk 2), `robot-baer` (a clip held above the
  head), `robot-klip` (cutting with scissors), `robot-hop` (cheering, arms up), `robot-fraek`
  (naughty: red eyes, little horns, grin), `robot-panik` (panic: wide eyes, sweat). They face
  right; the helper mirrors them.
* Helper command line (`pythonw -m projektsog.crew_child`): `--sprites PNG --poses <7 poses>
  --cell 120 --sheet-scale 2 --widget <hwnd> --pet x,y,w,h --view w,h --stage <stage>
  --robots N --awp 0|1 --seed S --input-tick T --log-file F`. stdin: `done` (finale, then
  home), `quit`/EOF (stop now). stdout JSON lines: `{"event":"side","side":"left"|"right"}`
  and `{"event":"out"}` after the first frame, `{"event":"aim","side":…}`,
  `{"event":"shot","hit":bool}`, `{"event":"aim-end"}`, then exactly one
  `{"event":"home","reason":"done"|"touched"|"locked"|"quit"|"timeout"|"error"}` (`timeout` =
  60 min). Unknown events are ignored by the main process, which closes the helper's stdin on
  `home`; the helper (like `petplay_child`) ends with `os._exit` after `home` and its log are
  written – a stdin reader still blocked in a read must not meet the interpreter's shutdown.
* The scene: one layered window (`WS_EX_LAYERED|TRANSPARENT|TOPMOST|TOOLWINDOW|NOACTIVATE`,
  `HTTRANSPARENT` – clicks go through, it never takes focus) over a band of the widget's monitor
  work area from the floor up to just above Klippe's AWP muzzle; ≤ 30 fps, only the dirty box is
  updated, topmost re-asserted every 2 s. The robots walk out through the widget's side facing
  the larger free part of the monitor and build a "timeline" on the floor beside it: two tracks
  of rounded clip blocks (blue video V1, green audio A1) that grow away from the widget; carriers
  bring clips from the box (`robot-baer`), cutters snip clips (`robot-klip`, a spark, the clip
  splits), the rest walk between jobs (`robot-a`/`robot-b`). A full timeline gets a playhead
  sweep and starts over with new colours. Sizes scale with the monitor's DPI (`unit`).
* **AWP**: when Klippe wears the AWP (`--awp 1`), once per outing after 15–40 s (and again at
  most every 60 s) one robot turns naughty (`robot-fraek`: leaves its job, dances or runs off
  with a clip). The helper emits `aim`; Klippe in the widget turns towards the robots and takes
  the aiming pose; a red laser runs from its muzzle (the aim pose's barrel end, mirrored, from
  the widget's pet rect; as drawn when the robots work to the right of the widget) to the robot
  (`hunt_plan`), each shot emits `shot`, the robot panics
  (`robot-panik`) after a miss and bursts into sparks on the kill; `aim-end`; a new robot walks
  out of the box 1.5 s later. Main → SSE `robot {haendelse: "sigter"|"skud"|"sigter-slut",
  ram, retning}` (`retning` with "sigter": "venstre" = mirrored aim pose, "hoejre" = as drawn).
* **Widget**: while a build is on (`data-bygger`), Klippe directs with a megaphone and says a
  line now and then ("Action! 🎬", "Klip! ✂️", "Mere tempo! 📣" …); the mood line names the
  session and what it builds. Robots home (`ude` false): three mini robots work in the box
  (carry, cut, tap) beside a little timeline that grows; with the AWP one of them is naughty now
  and then (every 45–90 s) and Klippe shoots it in the box. Robots out (`ude` true): the mini
  robots are gone and an open hatch glows at the box's side. `faerdig` → fireworks and "✅ <navn>
  er færdig – klar til at klippe!". `robot` events drive Klippe's aim (`data-sigter`: mirrored
  aim pose) and the recoil. The 🏆 panel's footer gets "🤖 Vis robotterne" and "📞 Prøv
  telefonen" (`/api/bygger/demo`).
* Settings (Indstillinger ▸ Klippe): `widget_crew` (bool, default true) "Robotterne må komme ud
  på skærmen, mens en Claude-session bygger"; `widget_ring` (bool, default true) "Klippes
  egen ringelyd, når en session vil bygge".
* Exit: `crew.close()` first (the helper is told `quit` and ends on stdin EOF by itself).
