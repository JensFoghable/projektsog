"""Unit tests for the Resolve menu script (resolve_scripts/Projektsøg - Åbn projektmappe.py)."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import types
import unittest
import urllib.error
import urllib.parse
from typing import Any
from unittest import mock

from projektsog import resolve_bridge as rb
from projektsog.config import DEFAULTS, Config
from projektsog.events import EventBus
from tests._resolve_fakes import (
    STUDIO_UNC, ChildFactory, FakeClip, FakeClock, FakeFolder, FakeIndexer, FakeProject,
    FakeResolve, FakeWinui, clips_in, folder_entry, source_ref, source_row)

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "resolve_scripts")
UNICODE_NAME = "Projektsøg - Åbn projektmappe.py"
ASCII_NAME = "Projektsoeg - Aabn projektmappe.py"
RIKKE = STUDIO_UNC + "\\Rikke Lindholm"
NAMES = frozenset(n.casefold() for n in DEFAULTS["project_template_dirs"])

_tmp: tempfile.TemporaryDirectory | None = None
menu: types.ModuleType


def setUpModule() -> None:
    global _tmp, menu
    _tmp = tempfile.TemporaryDirectory()
    os.environ["LOCALAPPDATA"] = _tmp.name
    spec = importlib.util.spec_from_file_location("projektsog_resolve_menu",
                                                  os.path.join(SCRIPTS, UNICODE_NAME))
    menu = importlib.util.module_from_spec(spec)
    keep = sys.dont_write_bytecode
    sys.dont_write_bytecode = True  # no __pycache__ next to the shipped script
    try:
        spec.loader.exec_module(menu)
    finally:
        sys.dont_write_bytecode = keep


def tearDownModule() -> None:
    if _tmp is not None:
        _tmp.cleanup()


def app_env(instance: dict[str, Any] | None = None, config: dict[str, Any] | None = None,
            **extra: str) -> dict[str, str]:
    """An environ whose LOCALAPPDATA holds the given instance.json / config.json."""
    base = tempfile.mkdtemp(dir=_tmp.name)
    folder = os.path.join(base, "Projektsog")
    os.makedirs(folder)
    for name, data in (("instance.json", instance), ("config.json", config)):
        if data is not None:
            with open(os.path.join(folder, name), "w", encoding="utf-8") as fh:
                json.dump(data, fh)
    return {"LOCALAPPDATA": base, **extra}


class FakeResponse(io.BytesIO):
    pass


class FakeOpener:
    def __init__(self, reply: Any) -> None:
        self.reply = reply
        self.requests: list[tuple[Any, float]] = []

    def open(self, request: Any, timeout: float | None = None) -> FakeResponse:
        self.requests.append((request, timeout))
        if isinstance(self.reply, Exception):
            raise self.reply
        body = self.reply if isinstance(self.reply, bytes) else json.dumps(self.reply).encode()
        return FakeResponse(body)


class RoutingOpener(FakeOpener):
    """Answers by (method, path); a route without a reply fails like a dead connection."""

    def __init__(self, routes: dict[tuple[str, str], Any]) -> None:
        super().__init__(None)
        self.routes = routes

    def open(self, request: Any, timeout: float | None = None) -> FakeResponse:
        self.reply = self.routes.get(self.route(request),
                                     urllib.error.URLError(ConnectionRefusedError(10061, "")))
        return super().open(request, timeout)

    @staticmethod
    def route(request: Any) -> tuple[str, str]:
        return request.get_method(), urllib.parse.urlsplit(request.full_url).path

    def calls(self) -> list[tuple[str, str]]:
        return [self.route(request) for request, _timeout in self.requests]


RIKKE_PROJECT = "Rikke Lindholm - Testimonial"
RIKKE_DB = "Kunder 2026 (Projektserver)"
RIKKE_IDENTITY = {"project": RIKKE_PROJECT, "database": RIKKE_DB, "uid": "uid-" + RIKKE_PROJECT}


def app_state(project: str | None = RIKKE_PROJECT, clip_count: int = 4,
              **changes: Any) -> dict[str, Any]:
    """The app's GET /api/resolve (SPEC §9) after a walk that found ``clip_count`` clips."""
    return {"enabled": True, "running": True, "connected": True, "error": None,
            "project": project, "database": RIKKE_DB, "clip_count": clip_count,
            "updated": 1_700_000_123.0, "folders": [], "other_dirs": [], "suggestions": [],
            "primary": {"name": "Rikke Lindholm", "path": RIKKE, "source": {"online": True},
                        "match": "media"},
            "offline_clips": 0, "offline_disks": [], **changes}


def rikke_resolve() -> FakeResolve:
    """Rikke Lindholm's project: 4 clip paths (and a clip without a file)."""
    fx9 = FakeFolder("FX9", clips_in(RIKKE + "\\Klip\\FX9", "FX9_7912.MXF", "FX9_7913.MXF"))
    root = FakeFolder("Master", [FakeClip(""), FakeClip("C:\\Github\\undertekster\\a.srt")],
                      [fx9, FakeFolder("Musik", clips_in(RIKKE + "\\Musik", "song.wav"))])
    return FakeResolve(FakeProject(RIKKE_PROJECT, root))


class ShippedFilesTests(unittest.TestCase):
    def test_ascii_named_copy_is_identical(self) -> None:
        with open(os.path.join(SCRIPTS, UNICODE_NAME), "rb") as a, \
                open(os.path.join(SCRIPTS, ASCII_NAME), "rb") as b:
            self.assertEqual(a.read(), b.read())

    def test_source_is_pure_ascii(self) -> None:
        with open(os.path.join(SCRIPTS, UNICODE_NAME), "rb") as fh:
            fh.read().decode("ascii")

    def test_defaults_and_bounds_match_the_app(self) -> None:
        self.assertEqual(list(menu.DEFAULT_TEMPLATE_DIRS), DEFAULTS["project_template_dirs"])
        self.assertEqual(menu.DEFAULT_MIN_TEMPLATE_DIRS, DEFAULTS["project_min_template_dirs"])
        self.assertEqual((menu.WALK_MAX_SECONDS, menu.WALK_MAX_CLIPS),
                         (rb.WALK_MAX_SECONDS, rb.WALK_MAX_CLIPS))
        self.assertEqual(menu.TITLE, "Projektsøg")


class PathHeuristicTests(unittest.TestCase):
    def test_split_path(self) -> None:
        cases = {
            RIKKE + "\\Klip\\a.mxf": (STUDIO_UNC, ["Rikke Lindholm", "Klip", "a.mxf"]),
            "\\\\?\\UNC\\host\\share\\a\\b.mov": ("\\\\host\\share", ["a", "b.mov"]),
            "\\\\?\\d:\\x\\y.mov": ("D:", ["x", "y.mov"]),
            "d:/x//y.mov": ("D:", ["x", "y.mov"]),
            "\\\\host\\share": None,
            "relative\\x.mov": None,
            "C:\\": None,
            "": None,
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                self.assertEqual(menu.split_path(path), expected)

    def test_candidate_folders(self) -> None:
        silkeborg = "\\\\GRAFIK-PC\\Kunder 2026 (Grafik)\\Klar Tand 2026\\Klar Tand - Silkeborg"
        pixelbro = "D:\\Forår 2026 RØD\\Pixelbro"
        cases = {
            RIKKE + "\\Klip\\FX9\\FX9_7912.MXF": [RIKKE],
            silkeborg + "\\Klip\\A7S\\C1859.MP4": [silkeborg],
            pixelbro + "\\Klip\\Raw\\Musik\\x.wav": [pixelbro, pixelbro + "\\Klip",
                                                     pixelbro + "\\Klip\\Raw"],
            "C:\\Kunder\\P\\KLIP\\a.mov": ["C:\\Kunder\\P"],
            "C:\\Klip\\x.mov": [],                    # a project needs a folder below the root
            "\\\\host\\Export\\x.mov": [],
            "C:\\Github\\undertekster\\a.srt": [],
            RIKKE + "\\Klip": [],                       # the last component is the clip itself
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                self.assertEqual(menu.candidate_folders(path, NAMES), expected)

    def test_choose_the_confirmed_folder_with_most_clips(self) -> None:
        other = "C:\\Kunder\\Andet"
        paths = [RIKKE + "\\Klip\\FX9\\a.mxf"] * 3 + [RIKKE + "\\Musik\\s.wav",
                                                    "C:\\Github\\undertekster\\a.srt",
                                                    other + "\\Klip\\b.mov"]
        asked: list[str] = []

        def is_project(folder: str) -> bool | None:
            asked.append(folder)
            return True

        self.assertEqual(menu.choose_project_folder(paths, NAMES, 2, is_project), (RIKKE, 4, None))
        self.assertEqual(sorted(asked), sorted([RIKKE, other]))

    def test_deeper_candidate_when_the_outer_one_is_no_project(self) -> None:
        path = "D:\\Arkiv\\Musik\\Projekt X\\Klip\\a.mov"
        verdicts = {"D:\\Arkiv": False, "D:\\Arkiv\\Musik\\Projekt X": True}
        self.assertEqual(menu.choose_project_folder([path], NAMES, 2, verdicts.get),
                         ("D:\\Arkiv\\Musik\\Projekt X", 1, None))

    def test_unreachable_folder_with_most_clips(self) -> None:
        offline = ["H:\\Arkiv\\P\\Klip\\a.mov"] * 5
        online = ["C:\\K\\Q\\Klip\\b.mov"] * 2
        verdicts = {"H:\\Arkiv\\P": None, "C:\\K\\Q": True}
        self.assertEqual(menu.choose_project_folder(offline + online, NAMES, 2, verdicts.get),
                         (None, 0, "H:\\Arkiv\\P"))
        self.assertEqual(menu.choose_project_folder(offline[:1] + online, NAMES, 2,
                                                    verdicts.get), ("C:\\K\\Q", 2, None))
        self.assertEqual(menu.choose_project_folder(["C:\\x\\y.mov"], NAMES, 2, verdicts.get),
                         (None, 0, None))

    def test_list_is_project_on_disk(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            project = os.path.join(root, "Rikke Lindholm")
            for sub in ("Klip", "musik", "Andet"):
                os.makedirs(os.path.join(project, sub))
            open(os.path.join(project, "Final"), "w").close()  # a file, not a folder
            self.assertTrue(menu.list_is_project(project, NAMES, 2))
            self.assertFalse(menu.list_is_project(project, NAMES, 3))
            self.assertFalse(menu.list_is_project(os.path.join(project, "Klip"), NAMES, 2))
            self.assertIsNone(menu.list_is_project(os.path.join(root, "mangler"), NAMES, 2))

    def test_list_is_project_times_out(self) -> None:
        release = threading.Event()

        def slow_scandir(path: str) -> Any:
            release.wait(5)
            raise OSError("too late")

        with mock.patch.object(menu, "LIST_TIMEOUT_S", 0.05), \
                mock.patch("os.scandir", slow_scandir):
            self.assertIsNone(menu.list_is_project("X:\\sover", NAMES, 2))
        release.set()

    def test_template_settings(self) -> None:
        env = app_env(config={"project_template_dirs": ["Klip", " Musik ", ""],
                              "project_min_template_dirs": 1})
        self.assertEqual(menu.template_settings(env), (frozenset({"klip", "musik"}), 1))
        env = app_env(config={"project_template_dirs": "Klip", "project_min_template_dirs": 0})
        self.assertEqual(menu.template_settings(env), (NAMES, 2))
        self.assertEqual(menu.template_settings(app_env()), (NAMES, 2))


class MediaPoolTests(unittest.TestCase):
    def test_collect_clip_paths(self) -> None:
        project = rikke_resolve().pm.project
        self.assertEqual(menu.collect_clip_paths(project), [
            "C:\\Github\\undertekster\\a.srt", RIKKE + "\\Klip\\FX9\\FX9_7912.MXF",
            RIKKE + "\\Klip\\FX9\\FX9_7913.MXF", RIKKE + "\\Musik\\song.wav"])

    def test_collect_clip_paths_is_bounded(self) -> None:
        root = FakeFolder("Master", clips_in("D:\\x", *[f"{i}.mov" for i in range(9)]))
        with mock.patch.object(menu, "WALK_MAX_CLIPS", 4):
            self.assertEqual(len(menu.collect_clip_paths(FakeProject("P", root))), 4)
        clock = FakeClock()
        folders = [FakeFolder(f"F{i}", clips_in(f"D:\\f{i}", "a.mov"),
                              on_list=lambda: clock.advance(5.0)) for i in range(10)]
        project = FakeProject("P", FakeFolder("Master", [], folders))
        self.assertEqual(len(menu.collect_clip_paths(project, clock)), 4)

    def test_open_via_media_pool_dry_run(self) -> None:
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            folder = menu.open_via_media_pool(rikke_resolve(), app_env(), True,
                                              is_project=lambda f: f == RIKKE)
        self.assertEqual(folder, RIKKE)
        self.assertIn("[TEST] Rikke Lindholm - Testimonial: 3 af 4 klip ligger i " + RIKKE,
                      out.getvalue())
        self.assertIn("[TEST] Ville åbne: " + RIKKE, out.getvalue())

    def test_open_via_media_pool_opens_the_folder(self) -> None:
        with mock.patch("os.startfile") as startfile, \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            folder = menu.open_via_media_pool(rikke_resolve(), app_env(), False,
                                              is_project=lambda f: True)
        self.assertEqual(folder, RIKKE)
        startfile.assert_called_once_with(RIKKE)

    def test_open_via_media_pool_explains_failures(self) -> None:
        empty = FakeResolve(None)
        cases = [(empty, None, "Der er ikke åbnet et projekt i DaVinci Resolve."),
                 (rikke_resolve(), lambda f: False,
                  "Ingen projektmappe fundet for ‘Rikke Lindholm - Testimonial’."),
                 (rikke_resolve(), lambda f: None,
                  f"Projektmappen ‘{RIKKE}’ kan ikke nås lige nu – er disken tilsluttet, "
                  "og er computeren tændt?")]
        for resolve, is_project, message in cases:
            with self.subTest(message=message), \
                    mock.patch.object(menu, "show_message") as show:
                self.assertIsNone(menu.open_via_media_pool(resolve, app_env(), False,
                                                           is_project=is_project))
                show.assert_called_once_with(message, False)


class AppTests(unittest.TestCase):
    INSTANCE = {"pid": 4242, "port": 47811}

    def test_posts_to_the_app(self) -> None:
        opener = FakeOpener({"ok": True, "path": RIKKE, "error": None})
        with mock.patch.object(menu, "allow_foreground") as allow, \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertTrue(menu.open_via_app(app_env(self.INSTANCE), False, opener))
        (request, timeout), = opener.requests
        self.assertEqual((request.get_method(), request.full_url),
                         ("POST", "http://127.0.0.1:47811/api/resolve/open"))
        self.assertEqual(request.get_header("X-projektsog"), "1")
        self.assertEqual((request.data, timeout), (b"{}", menu.HTTP_TIMEOUT_S))
        allow.assert_called_once_with(4242)

    def test_app_refusal_is_shown(self) -> None:
        opener = FakeOpener({"ok": False, "path": RIKKE, "error": "Tilslut disken ‘X’"})
        with mock.patch.object(menu, "allow_foreground"), \
                mock.patch.object(menu, "show_message") as show:
            self.assertTrue(menu.open_via_app(app_env(self.INSTANCE), False, opener))
        show.assert_called_once_with("Tilslut disken ‘X’", False)

    def test_falls_back_when_the_app_cannot_help(self) -> None:
        forbidden = urllib.error.HTTPError("http://x", 403, "Forbidden", {}, None)
        self.addCleanup(forbidden.close)
        replies = [{"ok": False, "path": None, "error": "Ingen projektmappe fundet"},
                   urllib.error.URLError(ConnectionRefusedError(10061, "refused")),
                   forbidden, b"<html>not the app</html>", {"unexpected": True}]
        for reply in replies:
            with self.subTest(reply=reply), mock.patch.object(menu, "allow_foreground"):
                self.assertFalse(menu.open_via_app(app_env(self.INSTANCE), False,
                                                   FakeOpener(reply)))

    def test_no_or_bad_instance_file(self) -> None:
        for instance in (None, {"pid": 1, "port": "47811"}, {"pid": 1, "port": 0},
                         {"pid": 1, "port": True}):
            with self.subTest(instance=instance):
                opener = FakeOpener({"ok": True})
                self.assertFalse(menu.open_via_app(app_env(instance), False, opener))
                self.assertEqual(opener.requests, [])

    def test_posts_the_project_identity(self) -> None:
        identity = {"project": "Bøgely Jul 2025", "database": "Kunder 2026 (Projektserver)"}
        opener = FakeOpener({"ok": False, "path": None,
                             "error": "Projektmappen for ‘Bøgely Jul 2025’ er ved at blive fundet"})
        with mock.patch.object(menu, "allow_foreground"), \
                mock.patch.object(menu, "show_message") as show:
            self.assertFalse(menu.open_via_app(app_env(self.INSTANCE), False, opener, identity))
        (request, _), = opener.requests
        self.assertEqual(json.loads(request.data), identity)
        request.data.decode("ascii")                  # ASCII JSON on the wire
        show.assert_not_called()                      # no folder: the media pool decides

    def test_project_identity(self) -> None:
        self.assertEqual(menu.project_identity(rikke_resolve()), RIKKE_IDENTITY)
        self.assertEqual(menu.project_identity(None), {})
        self.assertEqual(menu.project_identity(FakeResolve(None)), {})
        broken = rikke_resolve()
        broken.raise_on_pm = RuntimeError("scripting gone")
        self.assertEqual(menu.project_identity(broken), {})
        no_db = rikke_resolve()
        no_db.pm.db = {"DbType": "Disk", "DbName": ""}
        self.assertEqual(menu.project_identity(no_db),
                         {"project": RIKKE_PROJECT, "uid": RIKKE_IDENTITY["uid"]})
        no_id = rikke_resolve()
        no_id.pm.project.GetUniqueId = mock.Mock(side_effect=AttributeError("GetUniqueId"))
        self.assertEqual(menu.project_identity(no_id), {"project": RIKKE_PROJECT,
                                                         "database": RIKKE_DB})

    def test_asks_the_app_when_it_maps_the_current_media_pool(self) -> None:
        opener = RoutingOpener({("GET", "/api/resolve"): app_state(clip_count=4),
                                ("POST", "/api/resolve/open"): {"ok": True, "path": RIKKE,
                                                                "error": None}})
        with mock.patch.object(menu, "allow_foreground") as allow, \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertTrue(menu.open_via_app(app_env(self.INSTANCE), False, opener,
                                              RIKKE_IDENTITY, clip_count=4))
        self.assertEqual(opener.calls(), [("GET", "/api/resolve"), ("POST", "/api/resolve/open")])
        self.assertEqual(json.loads(opener.requests[1][0].data), RIKKE_IDENTITY)
        allow.assert_called_once_with(4242)

    def test_stale_mapping_is_not_opened_by_the_app(self) -> None:
        """RES2-2: clips were imported since the app's walk (1 clip then, 41 now), so the
        folder it knows may be the wrong one: the app is only told to walk again - without
        waiting for it - and the media pool decides."""
        opener = RoutingOpener({("GET", "/api/resolve"): app_state(clip_count=1),
                                ("POST", "/api/resolve/refresh"): app_state(clip_count=1),
                                ("POST", "/api/resolve/open"): {"ok": True, "path": RIKKE}})
        with mock.patch.object(menu, "allow_foreground") as allow:
            self.assertFalse(menu.open_via_app(app_env(self.INSTANCE), False, opener,
                                               RIKKE_IDENTITY, clip_count=41))
        self.assertEqual(opener.calls(), [("GET", "/api/resolve"),
                                          ("POST", "/api/resolve/refresh")])
        self.assertEqual(opener.requests[1][1], menu.REWALK_TIMEOUT_S)
        self.assertLessEqual(menu.REWALK_TIMEOUT_S, 1.0)
        allow.assert_not_called()

    def test_state_of_another_or_an_unmapped_project_is_not_used(self) -> None:
        cases = {"another project": app_state(project="Andet projekt"),
                 "another database": app_state(database="Local Database"),
                 "not connected": app_state(project=None, connected=False),
                 "still mapping": app_state(updated=None, clip_count=0, primary=None),
                 "a timeout": urllib.error.URLError(TimeoutError("timed out"))}
        for case, reply in cases.items():
            with self.subTest(case), mock.patch.object(menu, "allow_foreground"):
                opener = RoutingOpener({("GET", "/api/resolve"): reply,
                                        ("POST", "/api/resolve/open"): {"ok": True,
                                                                        "path": RIKKE}})
                self.assertFalse(menu.open_via_app(app_env(self.INSTANCE), False, opener,
                                                   RIKKE_IDENTITY, clip_count=4))
                self.assertEqual(opener.calls(), [("GET", "/api/resolve")])

    def test_main_falls_back_when_the_app_names_no_folder(self) -> None:
        env = app_env(self.INSTANCE)
        posted: list[tuple[str, Any]] = []

        def fake_request(url: str, body: Any, opener: Any = None, timeout: float = 0) -> Any:
            posted.append((url, body))
            if body is None:
                return app_state()
            return {"ok": False, "path": None, "error": "andet projekt"}

        with mock.patch.object(menu, "request_json", fake_request), \
                mock.patch.object(menu, "allow_foreground"), \
                mock.patch.object(menu, "open_via_media_pool") as fallback:
            menu.main({"resolve": rikke_resolve()}, env)
        (state_url, _), (url, body) = posted
        self.assertEqual((state_url, url), ("http://127.0.0.1:47811/api/resolve",
                                            "http://127.0.0.1:47811/api/resolve/open"))
        self.assertEqual(json.loads(body), RIKKE_IDENTITY)
        fallback.assert_called_once()
        self.assertEqual(len(fallback.call_args.kwargs["paths"]), 4, "the paths it collected")

    def test_main_stops_when_the_app_opened_the_folder(self) -> None:
        def fake_request(url: str, body: Any, opener: Any = None, timeout: float = 0) -> Any:
            return app_state() if body is None else {"ok": True, "path": RIKKE, "error": None}

        with mock.patch.object(menu, "request_json", fake_request), \
                mock.patch.object(menu, "allow_foreground"), \
                mock.patch.object(menu, "open_via_media_pool") as fallback, \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            menu.main({"resolve": rikke_resolve()}, app_env(self.INSTANCE))
        fallback.assert_not_called()

    def test_main_uses_its_live_media_pool_after_an_import(self) -> None:
        """RES2-2: 40 clips of the new project were imported after the app's walk (4 clips):
        the script opens the folder the live media pool points to, not the app's."""
        new = STUDIO_UNC + "\\Ny Kunde - Reklame"
        resolve = rikke_resolve()
        resolve.pm.project.pool.root.clips.extend(
            clips_in(new + "\\Klip", *[f"C{i:04d}.MXF" for i in range(40)]))
        opener = RoutingOpener({("GET", "/api/resolve"): app_state(clip_count=4),
                                ("POST", "/api/resolve/refresh"): app_state(clip_count=4),
                                ("POST", "/api/resolve/open"): {"ok": True, "path": RIKKE}})
        real_request = menu.request_json

        def request(url: str, body: Any, _opener: Any = None, timeout: float = 15.0) -> Any:
            return real_request(url, body, opener, timeout)

        with mock.patch.object(menu, "request_json", request), \
                mock.patch.object(menu, "allow_foreground"), \
                mock.patch.object(menu, "list_is_project", lambda f, n, m: f in (RIKKE, new)), \
                mock.patch.object(menu, "collect_clip_paths",
                                  wraps=menu.collect_clip_paths) as collect, \
                mock.patch("os.startfile") as startfile, \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            menu.main({"resolve": resolve}, app_env(self.INSTANCE))
        self.assertEqual(opener.calls(), [("GET", "/api/resolve"),
                                          ("POST", "/api/resolve/refresh")])
        startfile.assert_called_once_with(new)
        self.assertEqual(collect.call_count, 1, "the fallback reuses the paths it collected")

    def test_external_run_connects_first_and_names_the_project(self) -> None:
        """Run with ResolvePython.exe there is no 'resolve' global: the script connects before
        asking the app, so the app gets the same checks as from the Scripts menu."""
        posted: list[tuple[str, Any]] = []

        def fake_request(url: str, body: Any, opener: Any = None, timeout: float = 0) -> Any:
            posted.append((url, body))
            return app_state() if body is None else {"ok": True, "path": RIKKE, "error": None}

        with mock.patch.object(menu, "get_resolve", return_value=(rikke_resolve(), None)), \
                mock.patch.object(menu, "request_json", fake_request), \
                mock.patch.object(menu, "allow_foreground"), \
                mock.patch.object(menu, "open_via_media_pool") as fallback, \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            menu.main({}, app_env(self.INSTANCE))
        self.assertEqual(json.loads(posted[-1][1]), RIKKE_IDENTITY)
        fallback.assert_not_called()

    def test_external_run_without_a_connection_leaves_it_to_the_app(self) -> None:
        posted: list[tuple[str, Any]] = []

        def fake_request(url: str, body: Any, opener: Any = None, timeout: float = 0) -> Any:
            posted.append((url, body))
            return {"ok": False, "path": None, "error": "Ingen projektmappe fundet"}

        with mock.patch.object(menu, "get_resolve", side_effect=OSError("fusionscript.dll")), \
                mock.patch.object(menu, "request_json", fake_request), \
                mock.patch.object(menu, "allow_foreground"), \
                mock.patch.object(menu, "show_message") as show:
            menu.main({}, app_env(self.INSTANCE))
        self.assertEqual(posted, [("http://127.0.0.1:47811/api/resolve/open", b"{}")])
        show.assert_called_once_with(menu.MSG_NO_CONNECTION, False)

    def test_dry_run_checks_the_project_and_the_match(self) -> None:
        identity = {"project": RIKKE_PROJECT}
        other = FakeOpener(app_state(project="Andet projekt"))
        guess = FakeOpener(app_state(primary=dict(app_state()["primary"], match="name")))
        stale = FakeOpener(app_state(clip_count=1))
        same = FakeOpener(app_state())
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertFalse(menu.open_via_app(app_env(self.INSTANCE), True, other, identity))
            self.assertFalse(menu.open_via_app(app_env(self.INSTANCE), True, guess, identity))
            self.assertFalse(menu.open_via_app(app_env(self.INSTANCE), True, stale, identity,
                                               clip_count=41))
            self.assertTrue(menu.open_via_app(app_env(self.INSTANCE), True, same, identity,
                                              clip_count=4))
        text = out.getvalue()
        self.assertIn("kender endnu ikke projektet ‘Rikke Lindholm - Testimonial’", text)
        self.assertIn(f"kender kun et muligt match: {RIKKE}", text)
        self.assertIn("har kortlagt 1 klip, men mediepuljen har 41 nu", text)
        self.assertIn(f"[TEST] Ville åbne: {RIKKE}", text)
        self.assertEqual([r.get_method() for o in (other, guess, stale, same)
                          for r, _ in o.requests], ["GET"] * 4, "a dry run only reads")

    def test_dry_run_only_reads_the_state(self) -> None:
        primary = {"name": "Rikke Lindholm", "path": RIKKE, "source": {"online": True}}
        opener = FakeOpener(app_state(project="P", primary=primary))
        with mock.patch.object(menu, "allow_foreground") as allow, \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertTrue(menu.open_via_app(app_env(self.INSTANCE), True, opener))
        (request, _), = opener.requests
        self.assertEqual((request.get_method(), request.full_url),
                         ("GET", "http://127.0.0.1:47811/api/resolve"))
        self.assertIn(f"[TEST] Ville åbne: {RIKKE} (via Projektsøg, port 47811)", out.getvalue())
        allow.assert_not_called()
        offline = dict(primary, source={"online": False})
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertTrue(menu.open_via_app(app_env(self.INSTANCE), True,
                                              FakeOpener(app_state(primary=offline))))
            self.assertFalse(menu.open_via_app(app_env(self.INSTANCE), True,
                                               FakeOpener(app_state(primary=None))))
            self.assertFalse(menu.open_via_app(app_env(self.INSTANCE), True,
                                               FakeOpener(app_state(updated=None))))
        self.assertIn(f"kender projektmappen {RIKKE}, men den er offline", out.getvalue())
        self.assertIn("er ved at finde projektmappen", out.getvalue())


class MainTests(unittest.TestCase):
    def test_say_survives_narrow_or_missing_stdout(self) -> None:
        narrow = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
        with mock.patch("sys.stdout", narrow):
            menu.say(menu.MSG_NO_CONNECTION)
        narrow.flush()
        self.assertIn(b"Preferences \\u25b8 System", narrow.buffer.getvalue())
        with mock.patch("sys.stdout", None):
            menu.say("ingen konsol")

    def test_inside_resolve_without_the_app(self) -> None:
        env = app_env(PROJEKTSOG_DRY_RUN="1")
        with mock.patch.object(menu, "list_is_project", lambda f, n, m: f == RIKKE), \
                mock.patch.object(menu, "resolve_running", side_effect=AssertionError), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            menu.main({"resolve": rikke_resolve()}, env)
        self.assertIn("[TEST] Ville åbne: " + RIKKE, out.getvalue())

    def test_outside_resolve_when_it_is_not_running(self) -> None:
        env = app_env(PROJEKTSOG_DRY_RUN="1")
        with mock.patch.object(menu, "resolve_running", return_value=False), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            menu.main({}, env)
        self.assertEqual(out.getvalue().strip(), "DaVinci Resolve kører ikke.")

    def test_resolve_running(self) -> None:
        def listing(stdout: str) -> Any:
            return mock.patch("subprocess.run", return_value=subprocess.CompletedProcess(
                [], 0, stdout=stdout))

        with listing('"Resolve.exe","11604","Console","1","2.345.678 K"\n'):
            self.assertTrue(menu.resolve_running())
        with listing("INFO: Der kører ingen opgaver, som svarer til de angivne kriterier.\n"):
            self.assertFalse(menu.resolve_running())
        with mock.patch("subprocess.run", side_effect=OSError("no tasklist")):
            self.assertTrue(menu.resolve_running())


class ServerEndToEndTests(unittest.TestCase):
    """RES2-2 end to end: the menu script's main() against the real Server (127.0.0.1) and
    ResolveBridge (in-process helper; fake Resolve, indexer and Explorer). Nothing is opened:
    the bridge's open_folder and the script's os.startfile only record."""

    STUDIO = "C:\\Kunder 2026 (STUDIO)"
    OLD = STUDIO + "\\Gammel Kunde - Film"
    NEW = STUDIO + "\\Ny Kunde - Reklame"

    def setUp(self) -> None:
        from projektsog.server import Server  # noqa: PLC0415 - the app agent's module

        self.pool = FakeFolder("Master", [FakeClip(self.OLD + "\\Musik\\signatur.wav")])
        self.resolve = FakeResolve(FakeProject("Ny Kunde - Reklame", self.pool))
        self.winui = FakeWinui()
        self.started: list[str] = []
        source = source_ref(1, "Kunder 2026 (STUDIO)")

        def mapping(paths: list[str]) -> dict[str, Any]:
            counts: dict[str, int] = {}
            for path in paths:
                for folder in (self.OLD, self.NEW):
                    if path.startswith(folder + "\\"):
                        counts[folder] = counts.get(folder, 0) + 1
            return {"folders": [folder_entry(folder.rsplit("\\", 1)[1], folder, n, source)
                                for folder, n in counts.items()],
                    "other_dirs": [], "total": len(paths)}

        cfg = Config(path=os.path.join(tempfile.mkdtemp(dir=_tmp.name), "config.json"))
        cfg.update({"resolve_follow": "off"})
        bus = EventBus()
        indexer = FakeIndexer(mapping, sources=[source_row(1, "Kunder 2026 (STUDIO)", self.STUDIO)])
        clock = FakeClock()
        self.bridge = rb.ResolveBridge(
            cfg, bus, indexer, process_running=self.winui.process_running,
            process_uptime=self.winui.process_uptime, open_folder=self.winui.open_folder,
            explorer_window_for=self.winui.explorer_window_for,
            call_with_timeout=self.winui.call_with_timeout,
            spawn_child=ChildFactory(lambda: self.resolve, clock), clock=clock)
        self.addCleanup(self.bridge.stop)
        server = Server(cfg, bus, indexer, self.bridge, types.SimpleNamespace())
        port = server.start(0)
        self.addCleanup(server.stop)
        self.env = app_env({"pid": os.getpid(), "port": port})

    def run_script(self, script_globals: dict[str, Any]) -> None:
        with mock.patch.object(menu, "allow_foreground"), \
                mock.patch.object(menu, "show_message") as show, \
                mock.patch.object(menu, "list_is_project",
                                  lambda folder, names, n: folder in (self.OLD, self.NEW)), \
                mock.patch("os.startfile", self.started.append), \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            menu.main(script_globals, self.env)
        show.assert_not_called()

    def test_clips_imported_after_the_walk(self) -> None:
        self.bridge._tick()                    # the walk: one music track of an old project
        self.assertEqual(self.bridge.state()["primary"]["name"], "Gammel Kunde - Film")
        self.pool.clips.extend(clips_in(self.NEW + "\\Klip",
                                        *[f"C{i:04d}.MXF" for i in range(40)]))
        self.run_script({"resolve": self.resolve})
        self.assertEqual(self.winui.opened, [], "the old walk's folder is not opened")
        self.assertEqual(self.started, [self.NEW])
        self.bridge._tick()                    # the walk the script asked for
        state = self.bridge.state()
        self.assertEqual((state["clip_count"], state["primary"]["name"]),
                         (41, "Ny Kunde - Reklame"))
        self.run_script({"resolve": self.resolve})
        self.assertEqual(self.winui.opened, [(self.NEW, True)], "now the app opens it")
        self.assertEqual(self.started, [self.NEW])

    def test_external_run_after_a_project_switch(self) -> None:
        self.bridge._tick()
        self.resolve.pm.project = FakeProject(
            "Andet projekt", FakeFolder("Master", clips_in(self.NEW + "\\Klip", "a.mxf")))
        with mock.patch.object(menu, "get_resolve", return_value=(self.resolve, None)):
            self.run_script({})                # ResolvePython.exe: no 'resolve' global
        self.assertEqual(self.winui.opened, [], "not the previous project's folder")
        self.assertEqual(self.started, [self.NEW])


if __name__ == "__main__":
    unittest.main()
