"""New versions from GitHub (updater.py, /api/update, SPEC §20): finding them, replacing the files
all or nothing, a git working copy, and the restart through install.ps1."""

import io
import json
import os
import tempfile
import time
import unittest
import urllib.error
import zipfile

from projektsog import updater as up
from tests.test_app_server import ServerTestBase, setUpModule, tearDownModule  # noqa: F401
from tests.test_petplay import FakeBus

SHA = "b" * 40
BASE = {"Projektsøg.pyw": b"from projektsog import app\n", "install.ps1": b"Write-Host 'Installerer'\n",
        "projektsog/__init__.py": b"__version__ = '1.0.0'\n", "projektsog/app.py": b"VERSION = 1\n"}
NEW = {**BASE, "projektsog/app.py": b"VERSION = 2\n", "projektsog/ny.py": b"NY = True\n"}


def archive(files: dict[str, bytes], top: str = f"projektsog-{SHA}") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{top}/", b"")
        for rel, content in files.items():
            zf.writestr(f"{top}/{rel}", content)
    return buf.getvalue()


class FakeGitHub:
    """The three GitHub requests the updater makes, for one version of the files."""

    def __init__(self, files: dict[str, bytes], *, zip_files: dict[str, bytes] | None = None) -> None:
        self.files = files
        self.zip_files = zip_files if zip_files is not None else files
        self.urls: list[str] = []
        self.fail: Exception | None = None

    def __call__(self, url: str, *, limit: int, accept: str = "") -> bytes:
        self.urls.append(url)
        if self.fail is not None:
            raise self.fail
        if url == f"{up.API_URL}/commits?sha=main&per_page=1":
            return json.dumps([{"sha": SHA, "commit": {"message": "Opdateringsknap\n\nDetaljer",
                                                        "committer": {"date": "2026-10-05T12:00:00Z"}}}]).encode()
        if url == f"{up.API_URL}/git/trees/{SHA}?recursive=1":
            tree = [{"path": "projektsog", "mode": "040000", "type": "tree", "sha": "t" * 40}]
            tree += [{"path": p, "mode": "100644", "type": "blob", "sha": up.blob_sha(c)} for p, c in self.files.items()]
            return json.dumps({"sha": SHA, "tree": tree, "truncated": False}).encode()
        if url == up.ZIP_URL.format(sha=SHA):
            return archive(self.zip_files)
        raise AssertionError(f"unexpected request {url}")


def write_files(root: str, files: dict[str, bytes]) -> None:
    for rel, content in files.items():
        path = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(content)


def read(root: str, rel: str) -> bytes:
    with open(os.path.join(root, *rel.split("/")), "rb") as fh:
        return fh.read()


class UpdaterBase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = os.path.join(tmp.name, "Projektsøg")
        self.data = os.path.join(tmp.name, "data")
        os.makedirs(self.repo)
        self.bus = FakeBus()
        self.spawned: list[tuple[list[str], str, str]] = []
        self.autostart = True
        self.hub = FakeGitHub(NEW)

    def make(self, **kwargs) -> up.Updater:
        kwargs.setdefault("fetch", self.hub)
        return up.Updater(None, self.bus, repo_dir=self.repo, data_dir=self.data,
                          autostart=lambda: self.autostart,
                          spawn=lambda cmd, cwd, log: self.spawned.append((cmd, cwd, log)), **kwargs)

    def saved(self) -> dict:
        with open(os.path.join(self.data, "update.json"), encoding="utf-8") as fh:
            return json.load(fh)


class HelperTests(unittest.TestCase):
    def test_blob_ids_are_gits_and_ignore_windows_line_endings(self) -> None:
        self.assertEqual(up.blob_sha(b"hello\n"), "ce013625030ba8dba906f756967f9e9ca394464a")   # git hash-object
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.py")
            with open(path, "wb") as fh:
                fh.write(b"a = 1\r\nb = 2\r\n")
            self.assertTrue(up.file_matches(path, up.blob_sha(b"a = 1\nb = 2\n")))
            self.assertFalse(up.file_matches(path, up.blob_sha(b"a = 1\n")))
            self.assertFalse(up.file_matches(os.path.join(tmp, "mangler.py"), up.blob_sha(b"")))

    def test_a_download_must_be_whole_and_stay_inside_the_folder(self) -> None:
        self.assertEqual(set(up.read_archive(archive(NEW))), set(NEW))
        for bad, message in (
                (archive({**BASE, "../uden/for.py": b""}), "ugyldig sti"),
                (archive({k: v for k, v in BASE.items() if k != "install.ps1"}), "mangler install.ps1"),
                (archive({**BASE, "projektsog/app.py": b"def (:\n"}), r"kan ikke køre \(projektsog/app.py"),
                (b"ikke en zip", "ikke en gyldig zip-fil")):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                up.read_archive(bad)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for rel, content in BASE.items():
                zf.writestr(f"a/{rel}", content)
            zf.writestr("b/x.py", b"")
        with self.assertRaisesRegex(ValueError, "opbygning"):
            up.read_archive(buf.getvalue())

    def test_errors_are_explained_in_danish(self) -> None:
        rate = urllib.error.HTTPError("u", 403, "rate limited", {}, None)
        self.addCleanup(rate.close)
        self.assertEqual(up.describe_error(rate), "GitHub har for travlt lige nu – prøv igen om en time")
        self.assertEqual(up.describe_error(urllib.error.URLError("dns")),
                         "Ingen forbindelse til GitHub – er pc'en på internettet?")
        self.assertEqual(up.describe_error(ValueError("Den hentede fil er beskadiget")), "Den hentede fil er beskadiget")


class DownloadedFolderTests(UpdaterBase):
    def test_an_identical_folder_is_up_to_date_and_its_files_are_remembered(self) -> None:
        write_files(self.repo, {**NEW, "projektsog/app.py": b"VERSION = 2\r\n"})     # autocrlf copy
        u = self.make()
        u.check()
        state = u.state()
        self.assertEqual((state["mode"], state["available"], state["busy"], state["error"]), ("zip", False, None, None))
        self.assertEqual(state["latest"], {"sha": SHA, "date": "2026-10-05T12:00:00Z", "title": "Opdateringsknap"})
        self.assertEqual(state["installed"], {"sha": SHA, "date": "2026-10-05T12:00:00Z"})
        self.assertEqual(self.saved()["files"], sorted(NEW))
        self.assertEqual(self.bus.events[-1], ("update", state))
        with self.assertRaisesRegex(ValueError, up.NO_UPDATE):
            u.request_update()

    def test_update_replaces_what_changed_removes_what_went_and_restarts(self) -> None:
        write_files(self.repo, {**BASE, "projektsog/gammel.py": b"OLD = 1\n", "noter.txt": b"mine egne noter"})
        os.makedirs(self.data)
        with open(os.path.join(self.data, "update.json"), "w", encoding="utf-8") as fh:
            json.dump({"files": sorted(BASE) + ["projektsog/gammel.py"], "repo": os.path.normcase(self.repo)}, fh)
        self.autostart = False
        u = self.make()
        u.check()
        self.assertTrue(u.state()["available"])
        self.assertIsNone(u.state()["installed"])                 # not known before the update
        self.assertEqual(u.request_update()["busy"], "downloading")
        u.update()
        self.assertEqual(read(self.repo, "projektsog/app.py"), b"VERSION = 2\n")
        self.assertEqual(read(self.repo, "projektsog/ny.py"), b"NY = True\n")
        self.assertFalse(os.path.exists(os.path.join(self.repo, "projektsog", "gammel.py")))
        self.assertEqual(read(self.repo, "noter.txt"), b"mine egne noter")     # never ours: kept
        self.assertEqual(read(os.path.join(self.data, "update-backup"), "projektsog/app.py"), b"VERSION = 1\n")
        self.assertFalse([n for n in os.listdir(os.path.join(self.repo, "projektsog")) if n.endswith(".ny")])
        state = u.state()
        self.assertEqual((state["busy"], state["error"], state["installed"]["sha"]), ("restarting", None, SHA))
        saved = self.saved()
        self.assertEqual(saved["files"], sorted(NEW))
        self.assertEqual(saved["announce"], {"date": "2026-10-05T12:00:00Z", "title": "Opdateringsknap"})
        ((cmd, cwd, log),) = self.spawned
        self.assertEqual(cwd, self.repo)
        self.assertTrue(cmd[0].lower().endswith("powershell.exe"))
        self.assertEqual(cmd[cmd.index("-File") + 1], os.path.join(self.repo, "install.ps1"))
        self.assertIn("-NoAutostart", cmd)                           # the user's choice is kept
        self.assertEqual(log, os.path.join(self.data, "logs", "opdatering.log"))
        self.assertEqual([e[1]["busy"] for e in self.bus.events if e[0] == "update"][-3:],
                         ["downloading", "installing", "restarting"])

    def test_autostart_stays_on_when_it_is_on(self) -> None:
        write_files(self.repo, BASE)
        u = self.make()
        u.check()
        u.request_update()
        u.update()
        ((cmd, _, _),) = self.spawned
        self.assertNotIn("-NoAutostart", cmd)

    def test_a_failed_write_puts_every_file_back(self) -> None:
        write_files(self.repo, BASE)
        os.makedirs(os.path.join(self.repo, "projektsog", "ny.py"))      # cannot become a file
        u = self.make()
        u.check()
        u.request_update()
        u.update()
        self.assertEqual(read(self.repo, "projektsog/app.py"), b"VERSION = 1\n")    # put back
        self.assertFalse([n for n in os.listdir(os.path.join(self.repo, "projektsog")) if n.endswith(".ny")])
        state = u.state()
        self.assertEqual(state["busy"], None)
        self.assertTrue(state["error"])
        self.assertEqual(self.spawned, [])
        self.assertNotIn("announce", self.saved() if os.path.exists(os.path.join(self.data, "update.json")) else {})

    def test_a_broken_download_changes_nothing(self) -> None:
        write_files(self.repo, BASE)
        self.hub.zip_files = {**NEW, "projektsog/app.py": b"VERSION = (\n"}
        u = self.make()
        u.check()
        u.request_update()
        u.update()
        self.assertEqual(read(self.repo, "projektsog/app.py"), b"VERSION = 1\n")
        self.assertFalse(os.path.exists(os.path.join(self.repo, "projektsog", "ny.py")))
        self.assertRegex(u.state()["error"], r"^Den hentede version kan ikke køre \(projektsog/app.py")
        self.assertEqual(self.spawned, [])

    def test_no_connection_is_shown_and_the_next_check_clears_it(self) -> None:
        write_files(self.repo, NEW)
        u = self.make()
        self.hub.fail = urllib.error.URLError("getaddrinfo failed")
        u.check()
        state = u.state()
        self.assertEqual((state["error"], state["latest"], state["busy"]),
                         ("Ingen forbindelse til GitHub – er pc'en på internettet?", None, None))
        self.hub.fail = None
        u.check()
        self.assertEqual((u.state()["error"], u.state()["available"]), (None, False))

    def test_one_thing_at_a_time(self) -> None:
        write_files(self.repo, BASE)
        u = self.make()
        self.assertEqual(u.request_check()["busy"], "checking")
        with self.assertRaisesRegex(ValueError, up.CHECKING):
            u.request_update()
        u.check()
        u.request_update()
        with self.assertRaisesRegex(ValueError, up.BUSY):
            u.request_update()
        self.assertEqual(u.request_check()["busy"], "downloading")     # a check waits for the update

    def test_the_first_start_after_an_update_says_so(self) -> None:
        os.makedirs(self.data)
        with open(os.path.join(self.data, "update.json"), "w", encoding="utf-8") as fh:
            json.dump({"announce": {"date": "2026-10-05T12:00:00Z", "title": "Opdateringsknap"}}, fh)
        u = self.make()
        u._announce()
        u._announce()
        self.assertEqual([e for e in self.bus.events if e[0] == "notify"],
                         [("notify", {"title": "Projektsøg er opdateret", "text": "Opdateringsknap", "level": "info"})])
        self.assertNotIn("announce", self.saved())

    def test_the_thread_checks_after_the_start_and_when_asked(self) -> None:
        write_files(self.repo, NEW)
        u = self.make(first_check_s=0.05)
        u.start()
        self.addCleanup(u.close)
        commits = f"{up.API_URL}/commits?sha=main&per_page=1"
        deadline = time.monotonic() + 5
        while self.hub.urls.count(commits) < 1 or u.state()["busy"]:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        u.request_check()
        while self.hub.urls.count(commits) < 2 or u.state()["busy"]:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        self.assertEqual(u.state()["available"], False)


class FakeGit:
    """Scripted answers for the git commands of a working copy."""

    def __init__(self, *, head: str = "1" * 40, remote: str = "2" * 40, behind: bool = True,
                 ahead: bool = False, dirty: str = "") -> None:
        self.head, self.remote, self.behind, self.ahead, self.dirty = head, remote, behind, ahead, dirty
        self.calls: list[list[str]] = []

    def __call__(self, git: str, args: list[str], cwd: str) -> tuple[int, str]:
        self.calls.append(args)
        match args:
            case ["fetch", *_]:
                return 0, ""
            case ["rev-parse", "HEAD"]:
                return 0, self.head
            case ["rev-parse", "FETCH_HEAD"]:
                return 0, self.remote
            case ["log", "-1", "--format=%cI%n%s", "FETCH_HEAD"]:
                return 0, "2026-10-05T14:00:00+02:00\nOpdateringsknap"
            case ["log", "-1", "--format=%cI", "HEAD"]:
                return 0, "2026-10-02T09:00:00+02:00"
            case ["merge-base", "--is-ancestor", "FETCH_HEAD", "HEAD"]:
                return (0 if not self.behind else 1), ""
            case ["merge-base", "--is-ancestor", "HEAD", "FETCH_HEAD"]:
                return (0 if not self.ahead else 1), ""
            case ["status", "--porcelain", "--untracked-files=no"]:
                return 0, self.dirty
            case ["merge", "--ff-only", "--quiet", "FETCH_HEAD"]:
                self.head = self.remote
                return 0, ""
        raise AssertionError(f"unexpected git {args}")


class GitWorkingCopyTests(UpdaterBase):
    def setUp(self) -> None:
        super().setUp()
        write_files(self.repo, BASE)
        os.makedirs(os.path.join(self.repo, ".git"))

    def make_git(self, git: FakeGit, lookup=lambda: "git.exe") -> up.Updater:
        return self.make(fetch=lambda *a, **k: self.fail("a working copy asks git, not the GitHub API"),
                         git=lookup, git_runner=git)

    def test_behind_and_clean_is_updated_with_a_fast_forward(self) -> None:
        git = FakeGit()
        u = self.make_git(git)
        u.check()
        state = u.state()
        self.assertEqual((state["mode"], state["available"], state["blocked"]), ("git", True, None))
        self.assertEqual(state["latest"], {"sha": "2" * 40, "date": "2026-10-05T14:00:00+02:00", "title": "Opdateringsknap"})
        self.assertEqual(state["installed"]["sha"], "1" * 40)
        self.assertIn(["fetch", "--quiet", "--no-tags", up.GIT_URL, "main"], git.calls)
        u.request_update()
        u.update()
        self.assertIn(["merge", "--ff-only", "--quiet", "FETCH_HEAD"], git.calls)
        self.assertEqual((u.state()["busy"], u.state()["installed"]["sha"]), ("restarting", "2" * 40))
        self.assertEqual(len(self.spawned), 1)

    def test_up_to_date_or_ahead_needs_nothing(self) -> None:
        for git in (FakeGit(remote="1" * 40), FakeGit(behind=False)):
            u = self.make_git(git)
            u.check()
            self.assertEqual((u.state()["available"], u.state()["blocked"]), (False, None))

    def test_a_developers_folder_is_never_touched(self) -> None:
        for git, why in ((FakeGit(dirty=" M projektsog/app.py"), up.DIRTY), (FakeGit(ahead=True), up.OWN_COMMITS)):
            u = self.make_git(git)
            u.check()
            self.assertEqual((u.state()["available"], u.state()["blocked"]), (True, why))
            with self.assertRaisesRegex(ValueError, why):
                u.request_update()
            self.assertNotIn(["merge", "--ff-only", "--quiet", "FETCH_HEAD"], git.calls)

    def test_without_git_the_folder_is_left_alone(self) -> None:
        u = self.make_git(FakeGit(), lookup=lambda: None)
        u.check()
        self.assertEqual((u.state()["available"], u.state()["blocked"]), (False, up.NO_GIT))


class UpdateEndpointTests(ServerTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        repo = os.path.join(self.dir.name, "app")
        write_files(repo, BASE)
        self.updater = up.Updater(None, FakeBus(), repo_dir=repo, data_dir=os.path.join(self.dir.name, "data"),
                                  fetch=FakeGitHub(NEW), spawn=lambda *a: self.fail("no installer in these tests"))
        self.server.updater = self.updater

    def test_state_check_and_install(self) -> None:
        self.assertEqual(self.req("GET", "/api/update").json()["available"], False)
        response = self.req("POST", "/api/update/install", body={})
        self.assertEqual((response.status, response.json()), (400, {"error": up.NO_UPDATE}))
        self.assertEqual(self.req("POST", "/api/update/check", body={}).json()["busy"], "checking")
        self.updater.check()
        self.assertEqual(self.req("GET", "/api/update").json()["available"], True)
        self.server.updater = None
        response = self.req("GET", "/api/update")
        self.assertEqual((response.status, response.json()), (400, {"error": "Opdateringer er ikke startet"}))


if __name__ == "__main__":
    unittest.main()
