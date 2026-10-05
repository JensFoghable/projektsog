"""Updates from GitHub with one button (SPEC §20).

Projektsøg is published at github.com/JensFoghable/projektsog. A few times a day – and when the
user asks – the app looks at the newest version on ``main``, and the settings page shows it with
an "Opdater nu" button. Nothing is ever installed by itself, only when that button is pressed:

* A folder that came from a download (no ``.git``): its files are compared with the newest
  version through their git blob hashes, so it does not matter how the folder got there. The
  update downloads exactly that version as a zip, checks it (every file read back, the Python
  compiles, the main files are there) and replaces the files that differ. The old ones are kept
  in ``%LOCALAPPDATA%\\Projektsog\\update-backup`` and put back if anything goes wrong; files an earlier update (or a check
  that found the folder identical) put there and the new version no longer has are removed.
* A git working copy (``git clone``): ``git fetch`` + ``git merge --ff-only`` – only when no
  tracked file in the folder is changed and it has no commits of its own, so a developer's
  folder is never touched.

Then ``install.ps1`` runs, as after a manual update (README): it stops this Projektsøg, renews the
Start-menu shortcut, autostart (kept as it is) and the Resolve script and starts the new version,
which says so in a Windows notification.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from typing import Any

from . import __version__

log = logging.getLogger(__name__)

REPO = "JensFoghable/projektsog"
BRANCH = "main"
API_URL = f"https://api.github.com/repos/{REPO}"
ZIP_URL = f"https://codeload.github.com/{REPO}/zip/{{sha}}"
GIT_URL = f"https://github.com/{REPO}.git"

FIRST_CHECK_S = 20.0           # after the start (the tray exists by then, for the notification)
CHECK_INTERVAL_S = 6 * 3600.0
RESTART_WAIT_S = 120.0         # the installer stops this process well within this
HTTP_TIMEOUT_S = 30.0
GIT_TIMEOUT_S = 90.0
MAX_JSON_BYTES = 8 << 20
MAX_ZIP_BYTES = 64 << 20
# A download without these is not Projektsøg (or is broken): nothing is changed.
REQUIRED_FILES = ("Projektsøg.pyw", "install.ps1", "projektsog/__init__.py", "projektsog/app.py")

_CREATE_NO_WINDOW = 0x08000000
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000

NO_UPDATE = "Der er ingen ny version"
BUSY = "Opdateringen er allerede i gang"
CHECKING = "Søger efter en ny version – vent et øjeblik"
NO_GIT = "Mappen er hentet med git, men git findes ikke på pc'en – opdater den med git pull"
OWN_COMMITS = "Mappen har sine egne commits i git – opdater den med git pull"
DIRTY = "Der er ændrede filer i mappen, som ikke er gemt i git – opdater den med git pull"
NOT_RESTARTED = ("Projektsøg blev ikke genstartet – den nye version starter, næste gang Projektsøg "
                 "starter (se logs\\opdatering.log)")


# --------------------------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------------------------

def blob_sha(data: bytes) -> str:
    """git's id of a file's content (what GitHub's tree lists for every file)."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def same_content(old: bytes, new: bytes) -> bool:
    """Equal – also when the copy on disk got Windows line endings (git's autocrlf)."""
    return old == new or (b"\r\n" in old and old.replace(b"\r\n", b"\n") == new)


def file_matches(path: str, sha: str) -> bool:
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return False
    return blob_sha(data) == sha or (b"\r\n" in data and blob_sha(data.replace(b"\r\n", b"\n")) == sha)


def safe_relpath(name: str) -> str | None:
    """A zip member's path below the archive's top folder, or None when it may not be written."""
    if not name or "\\" in name or ":" in name or name.startswith("/"):
        return None
    parts = name.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None
    return "/".join(parts)


def read_archive(data: bytes) -> dict[str, bytes]:
    """The files of a GitHub zip (``<repo>-<sha>/…``) by path relative to that top folder.
    Raises ValueError when the download is damaged or is not Projektsøg."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ValueError("Den hentede fil er ikke en gyldig zip-fil") from exc
    files: dict[str, bytes] = {}
    tops: set[str] = set()
    with archive:
        try:
            damaged = archive.testzip()
        except (zipfile.BadZipFile, OSError, EOFError) as exc:
            raise ValueError("Den hentede fil er beskadiget") from exc
        if damaged is not None:
            raise ValueError(f"Den hentede fil er beskadiget ({damaged})")
        for info in archive.infolist():
            top, _, rest = info.filename.partition("/")
            tops.add(top)
            if info.is_dir() or not rest:
                continue
            if (info.external_attr >> 16) & 0o170000 == 0o120000:      # a symlink: never written
                continue
            rel = safe_relpath(rest)
            if rel is None:
                raise ValueError(f"Den hentede fil har en ugyldig sti: {info.filename}")
            files[rel] = archive.read(info)
    if len(tops) != 1:
        raise ValueError("Den hentede fil har ikke den forventede opbygning")
    check_files(files)
    return files


def check_files(files: dict[str, bytes]) -> None:
    """The main files are there and every Python file compiles (ValueError otherwise)."""
    missing = [name for name in REQUIRED_FILES if name not in files]
    if missing:
        raise ValueError(f"Den hentede version mangler {', '.join(missing)}")
    for rel, content in files.items():
        if rel.endswith((".py", ".pyw")):
            try:
                compile(content, rel, "exec", dont_inherit=True)
            except (SyntaxError, ValueError) as exc:
                raise ValueError(f"Den hentede version kan ikke køre ({rel}: {exc})") from exc


def http_get(url: str, *, limit: int, accept: str = "application/vnd.github+json") -> bytes:
    request = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": f"Projektsog/{__version__}"})
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_S) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise OSError("svaret fra GitHub er for stort")
    return data


def describe_error(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in (403, 429):
            return "GitHub har for travlt lige nu – prøv igen om en time"
        if exc.code == 404:
            return "Projektsøg blev ikke fundet på GitHub"
        return f"GitHub svarede med fejl {exc.code}"
    if isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError)):
        return "Ingen forbindelse til GitHub – er pc'en på internettet?"
    if isinstance(exc, subprocess.TimeoutExpired):
        return "git svarede ikke"
    if isinstance(exc, ValueError):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


def first_line(text: Any) -> str:
    lines = str(text or "").strip().splitlines()
    return lines[0].strip()[:200] if lines else ""


def run_git(git: str, args: list[str], cwd: str, timeout: float = GIT_TIMEOUT_S) -> tuple[int, str]:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
    proc = subprocess.run([git, *args], cwd=cwd, capture_output=True, timeout=timeout, env=env,
                          stdin=subprocess.DEVNULL, creationflags=_CREATE_NO_WINDOW, check=False)
    out = proc.stdout.decode("utf-8", "replace").strip()
    if proc.returncode != 0 and not out:
        out = proc.stderr.decode("utf-8", "replace").strip()
    return proc.returncode, out


def powershell_path() -> str:
    root = os.environ.get("SystemRoot") or r"C:\Windows"
    path = os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    return path if os.path.isfile(path) else "powershell.exe"


def spawn_detached(cmd: list[str], cwd: str, log_path: str) -> None:
    """Start the installer so that it outlives this process (it stops this process itself)."""
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, "ab") as out:
        out.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} {subprocess.list2cmdline(cmd)}\n".encode())
        out.flush()
        flags = _CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP
        try:
            subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                             creationflags=flags | _CREATE_BREAKAWAY_FROM_JOB, close_fds=True)
        except PermissionError:
            # In a job that does not allow breaking away: the installer still outlives us
            # unless the job kills its processes on close, which Explorer's does not.
            subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                             creationflags=flags, close_fds=True)


# --------------------------------------------------------------------------------------------
# The updater
# --------------------------------------------------------------------------------------------

class Updater:
    """Finds and installs a new version; one background thread does all the work.

    ``state()`` → ``{"mode": "zip"|"git", "installed": {"sha", "date"}|None,
    "latest": {"sha", "date", "title"}|None, "available", "blocked": str|None,
    "busy": None|"checking"|"downloading"|"installing"|"restarting", "checked", "error"}``;
    every change is published as SSE ``update``.
    """

    def __init__(self, cfg: Any, bus: Any, *, repo_dir: str, data_dir: str,
                 autostart: Callable[[], bool] = lambda: True,
                 fetch: Callable[..., bytes] = http_get,
                 git: str | None | Callable[[], str | None] = None,
                 git_runner: Callable[[str, list[str], str], tuple[int, str]] = run_git,
                 spawn: Callable[[list[str], str, str], None] = spawn_detached,
                 clock: Callable[[], float] = time.time,
                 first_check_s: float = FIRST_CHECK_S) -> None:
        self.cfg = cfg
        self.bus = bus
        self._repo = repo_dir
        self._data_dir = data_dir
        self._path = os.path.join(data_dir, "update.json")
        self._backup = os.path.join(data_dir, "update-backup")
        self._autostart = autostart
        self._fetch = fetch
        self._git_lookup = git if callable(git) else (lambda: git if git is not None else shutil.which("git"))
        self._git_runner = git_runner
        self._spawn = spawn
        self._clock = clock
        self._first_check_s = first_check_s
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._want: str | None = None
        self.mode = "git" if os.path.exists(os.path.join(repo_dir, ".git")) else "zip"
        self._data = self._load()
        self._latest: dict[str, str] | None = None
        self._available = False
        self._blocked: str | None = None
        self._busy: str | None = None
        self._checked: float | None = None
        self._error: str | None = None
        self._git_installed: dict[str, str] | None = None

    # -- lifecycle ----------------------------------------------------------------------------
    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="updater", daemon=True)
            self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()

    def _run(self) -> None:
        self._wake.wait(self._first_check_s)
        self._wake.clear()
        if self._stop.is_set():
            return
        self._announce()
        next_check = 0.0
        while not self._stop.is_set():
            with self._lock:
                want, self._want = self._want, None
            if want is None and self._clock() >= next_check:
                want = "check"
            try:
                if want == "update":
                    self.update()
                elif want == "check":
                    self.check()
            except Exception:
                log.exception("the update %s failed", want)
            if want == "check":
                next_check = self._clock() + CHECK_INTERVAL_S
            if self._busy == "restarting":
                # The installer stops this Projektsøg; still here after a while → it failed.
                if self._stop.wait(RESTART_WAIT_S):
                    return
                self._set(busy=None, error=NOT_RESTARTED)
            self._wake.wait(max(1.0, next_check - self._clock()))
            self._wake.clear()

    # -- API ------------------------------------------------------------------------------------
    def state(self) -> dict[str, Any]:
        with self._lock:
            return self._state_locked()

    def request_check(self) -> dict[str, Any]:
        with self._lock:
            if self._busy is None:
                self._want = "check"
                self._busy = "checking"
            state = self._state_locked()
        self._wake.set()
        self.bus.publish("update", state)
        return state

    def request_update(self) -> dict[str, Any]:
        with self._lock:
            if self._busy == "checking":
                raise ValueError(CHECKING)
            if self._busy is not None:
                raise ValueError(BUSY)
            if not self._available or self._latest is None:
                raise ValueError(NO_UPDATE)
            if self._blocked:
                raise ValueError(self._blocked)
            self._want = "update"
            self._busy = "downloading"
            self._error = None
            state = self._state_locked()
        self._wake.set()
        self.bus.publish("update", state)
        return state

    # -- the work (on the updater's thread; also called directly by the tests) -----------------
    def check(self) -> None:
        self._set(busy="checking")
        try:
            if self.mode == "git":
                result = self._check_git()
            else:
                result = self._check_zip()
        except Exception as exc:
            if not isinstance(exc, (OSError, ValueError, subprocess.SubprocessError)):
                log.exception("looking for a new version failed")
            else:
                log.info("looking for a new version failed: %s", exc)
            self._set(busy=None, error=describe_error(exc), checked=self._clock())
            return
        self._set(busy=None, error=None, checked=self._clock(), **result)

    def update(self) -> None:
        with self._lock:
            latest, available, blocked = self._latest, self._available, self._blocked
        if latest is None or not available or blocked:
            self._set(busy=None, error=blocked or NO_UPDATE)
            return
        try:
            self._set(busy="downloading", error=None)
            if self.mode == "git":
                self._update_git(latest)
            else:
                self._update_zip(latest)
            with self._lock:
                self._data["announce"] = {"date": latest["date"], "title": latest["title"]}
            self._save()
            self._run_installer()
        except Exception as exc:
            if not isinstance(exc, (OSError, ValueError, subprocess.SubprocessError)):
                log.exception("the update failed")
            else:
                log.warning("the update failed: %s", exc)
            self._set(busy=None, error=describe_error(exc))
            return
        log.info("updated to %s – the installer restarts Projektsøg", latest["sha"][:7])
        self._set(busy="restarting")

    # -- a downloaded folder --------------------------------------------------------------------
    def _latest_commit(self) -> dict[str, str]:
        data = json.loads(self._fetch(f"{API_URL}/commits?sha={BRANCH}&per_page=1", limit=MAX_JSON_BYTES))
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            raise ValueError("Uventet svar fra GitHub")
        commit = data[0]
        sha = str(commit.get("sha") or "")
        info = commit.get("commit") or {}
        date = ((info.get("committer") or {}).get("date") or (info.get("author") or {}).get("date") or "")
        if len(sha) != 40:
            raise ValueError("Uventet svar fra GitHub")
        return {"sha": sha, "date": str(date), "title": first_line(info.get("message"))}

    def _check_zip(self) -> dict[str, Any]:
        latest = self._latest_commit()
        tree = json.loads(self._fetch(f"{API_URL}/git/trees/{latest['sha']}?recursive=1", limit=MAX_JSON_BYTES))
        entries = tree.get("tree") if isinstance(tree, dict) else None
        if not isinstance(entries, list) or tree.get("truncated"):
            raise ValueError("Uventet svar fra GitHub")
        blobs = {str(e["path"]): str(e["sha"]) for e in entries
                 if isinstance(e, dict) and e.get("type") == "blob" and e.get("mode") != "120000"
                 and safe_relpath(str(e.get("path") or "")) is not None}
        if not blobs:
            raise ValueError("Uventet svar fra GitHub")
        differ = [p for p, sha in blobs.items() if not file_matches(os.path.join(self._repo, *p.split("/")), sha)]
        if not differ:
            # The folder is exactly that version: remember it, and that these files are ours.
            with self._lock:
                self._data.update(installed={"sha": latest["sha"], "date": latest["date"]},
                                  files=sorted(blobs), repo=os.path.normcase(self._repo))
            self._save()
        else:
            log.info("a new version %s: %d files differ (e.g. %s)", latest["sha"][:7], len(differ), differ[0])
        return {"latest": latest, "available": bool(differ), "blocked": None}

    def _update_zip(self, latest: dict[str, str]) -> None:
        files = read_archive(self._fetch(ZIP_URL.format(sha=latest["sha"]), limit=MAX_ZIP_BYTES,
                                         accept="application/zip"))
        self._set(busy="installing")
        previous = self._data.get("files") if self._data.get("repo") == os.path.normcase(self._repo) else None
        obsolete = sorted(set(previous or []) - set(files))
        self._replace(files, obsolete)
        with self._lock:
            self._data.update(installed={"sha": latest["sha"], "date": latest["date"]}, files=sorted(files),
                              repo=os.path.normcase(self._repo))
        self._save()

    def _replace(self, files: dict[str, bytes], obsolete: list[str]) -> None:
        """Write the files that differ and remove the obsolete ones – all or nothing."""
        backup_root = self._backup
        shutil.rmtree(backup_root, ignore_errors=True)
        done: list[tuple[str, str | None]] = []       # (file, its backup or None when it is new)
        temp: str | None = None

        def back_up(rel: str, target: str) -> str:
            backup = os.path.join(backup_root, *rel.split("/"))
            os.makedirs(os.path.dirname(backup), exist_ok=True)
            shutil.copy2(target, backup)
            return backup

        try:
            for rel in sorted(files):
                content = files[rel]
                target = os.path.join(self._repo, *rel.split("/"))
                try:
                    with open(target, "rb") as fh:
                        if same_content(fh.read(), content):
                            continue
                    exists = True
                except FileNotFoundError:
                    exists = False
                os.makedirs(os.path.dirname(target), exist_ok=True)
                temp = target + ".ny"
                with open(temp, "wb") as fh:
                    fh.write(content)
                backup = back_up(rel, target) if exists else None
                os.replace(temp, target)
                temp = None
                done.append((target, backup))
            for rel in obsolete:
                if safe_relpath(rel) is None:
                    continue
                target = os.path.join(self._repo, *rel.split("/"))
                if os.path.isfile(target):
                    backup = back_up(rel, target)
                    os.remove(target)
                    done.append((target, backup))
        except Exception:
            if temp is not None:
                try:
                    os.remove(temp)
                except OSError:
                    pass
            for target, backup in reversed(done):
                try:
                    if backup is None:
                        os.remove(target)
                    else:
                        shutil.copy2(backup, target)
                except OSError as exc:
                    log.error("could not put %s back: %s", target, exc)
            raise
        log.info("update: %d files written or removed", len(done))

    # -- a git working copy ---------------------------------------------------------------------
    def _git(self, *args: str) -> tuple[int, str]:
        git = self._git_lookup()
        if not git:
            raise ValueError("Git findes ikke på pc'en")
        return self._git_runner(git, list(args), self._repo)

    def _check_git(self) -> dict[str, Any]:
        if not self._git_lookup():
            return {"latest": None, "available": False, "blocked": NO_GIT}
        code, out = self._git("fetch", "--quiet", "--no-tags", GIT_URL, BRANCH)
        if code != 0:
            raise OSError(f"git fetch: {first_line(out)}")
        _, head = self._git("rev-parse", "HEAD")
        _, remote = self._git("rev-parse", "FETCH_HEAD")
        _, info = self._git("log", "-1", "--format=%cI%n%s", "FETCH_HEAD")
        date, _, title = info.partition("\n")
        latest = {"sha": remote, "date": date.strip(), "title": first_line(title)}
        _, head_date = self._git("log", "-1", "--format=%cI", "HEAD")
        with self._lock:
            self._git_installed = {"sha": head, "date": head_date.strip()}
        if head == remote or self._git("merge-base", "--is-ancestor", "FETCH_HEAD", "HEAD")[0] == 0:
            return {"latest": latest, "available": False, "blocked": None}
        if self._git("merge-base", "--is-ancestor", "HEAD", "FETCH_HEAD")[0] != 0:
            return {"latest": latest, "available": True, "blocked": OWN_COMMITS}
        _, changed = self._git("status", "--porcelain", "--untracked-files=no")
        if changed:
            return {"latest": latest, "available": True, "blocked": DIRTY}
        return {"latest": latest, "available": True, "blocked": None}

    def _update_git(self, latest: dict[str, str]) -> None:
        code, out = self._git("fetch", "--quiet", "--no-tags", GIT_URL, BRANCH)
        if code != 0:
            raise OSError(f"git fetch: {first_line(out)}")
        self._set(busy="installing")
        _, changed = self._git("status", "--porcelain", "--untracked-files=no")
        if changed:
            raise ValueError(DIRTY)
        code, out = self._git("merge", "--ff-only", "--quiet", "FETCH_HEAD")
        if code != 0:
            raise ValueError(f"git kunne ikke opdatere mappen ({first_line(out)}) – opdater den med git pull")
        _, head = self._git("rev-parse", "HEAD")
        _, date = self._git("log", "-1", "--format=%cI", "HEAD")
        with self._lock:
            self._git_installed = {"sha": head, "date": date.strip()}

    # -- after the files: the installer restarts the app ----------------------------------------
    def _run_installer(self) -> None:
        script = os.path.join(self._repo, "install.ps1")
        if not os.path.isfile(script):
            raise ValueError("install.ps1 mangler i mappen")
        cmd = [powershell_path(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", script]
        python = os.path.join(os.path.dirname(sys.executable), "python.exe")
        if os.path.isfile(python):
            cmd += ["-Python", python]
        try:
            autostart = bool(self._autostart())
        except Exception:
            autostart = True
        if not autostart:
            cmd.append("-NoAutostart")          # the user's own choice in the app is kept
        self._spawn(cmd, self._repo, os.path.join(self._data_dir, "logs", "opdatering.log"))

    def _announce(self) -> None:
        """The first start after an update says so in a Windows notification."""
        with self._lock:
            news = self._data.pop("announce", None)
        if not isinstance(news, dict):
            return
        self._save()
        title = str(news.get("title") or "")
        self.bus.publish("notify", {"title": "Projektsøg er opdateret",
                                    "text": title or "Den nye version kører nu", "level": "info"})

    # -- state & storage ------------------------------------------------------------------------
    def _set(self, **changes: Any) -> None:
        with self._lock:
            for key, value in changes.items():
                setattr(self, f"_{key}", value)
            state = self._state_locked()
        self.bus.publish("update", state)

    def _state_locked(self) -> dict[str, Any]:
        installed = self._git_installed if self.mode == "git" else None
        if self.mode == "zip" and self._data.get("repo") == os.path.normcase(self._repo):
            stored = self._data.get("installed")
            if isinstance(stored, dict) and stored.get("sha"):
                installed = {"sha": str(stored["sha"]), "date": str(stored.get("date") or "")}
        return {"mode": self.mode, "installed": dict(installed) if installed else None,
                "latest": dict(self._latest) if self._latest else None, "available": self._available,
                "blocked": self._blocked, "busy": self._busy, "checked": self._checked, "error": self._error}

    def _load(self) -> dict[str, Any]:
        try:
            with open(self._path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return data
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            log.warning("could not read %s: %s", self._path, exc)
        return {}

    def _save(self) -> None:
        temp = self._path + ".tmp"
        with self._lock:
            text = json.dumps(self._data, ensure_ascii=False, indent=1)
        try:
            os.makedirs(self._data_dir, exist_ok=True)
            with open(temp, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(temp, self._path)
        except OSError as exc:
            log.warning("could not save %s: %s", self._path, exc)
