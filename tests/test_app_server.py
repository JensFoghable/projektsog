"""Tests for projektsog.server (SPEC §11) with fake collaborators – loopback HTTP only."""

import json
import os
import re
import socket
import sys
import tempfile
import time
import unittest

from projektsog.config import Config
from projektsog.events import EventBus
from projektsog.server import INTERNAL_ERROR, Server
from tests import _app_fakes as fakes

_saved_env: dict[str, str | None] = {}
_tmp: tempfile.TemporaryDirectory | None = None


def setUpModule() -> None:
    global _tmp
    _tmp = tempfile.TemporaryDirectory()
    _saved_env["LOCALAPPDATA"] = os.environ.get("LOCALAPPDATA")
    os.environ["LOCALAPPDATA"] = _tmp.name


def tearDownModule() -> None:
    if _saved_env.get("LOCALAPPDATA") is None:
        os.environ.pop("LOCALAPPDATA", None)
    else:
        os.environ["LOCALAPPDATA"] = _saved_env["LOCALAPPDATA"]
    _tmp.cleanup()


def _write(path: str, data: bytes | str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data.encode("utf-8") if isinstance(data, str) else data)


class ServerTestBase(unittest.TestCase):
    heartbeat = 15.0

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        self.web_dir = os.path.join(self.root, "web")
        self.assets_dir = os.path.join(self.root, "assets")
        _write(os.path.join(self.web_dir, "index.html"),
               "<!doctype html><title>Projektsøg</title><script src=\"/app.js\"></script>")
        _write(os.path.join(self.web_dir, "app.js"), "console.log('Projektsøg');")
        _write(os.path.join(self.web_dir, "style.css"), "body { color: #eee; }")
        _write(os.path.join(self.web_dir, "notes.txt"), "not a web type")
        _write(os.path.join(self.assets_dir, "icon.png"), b"\x89PNG\r\n\x1a\n")
        _write(os.path.join(self.root, "secret.txt"), "outside the web dir")
        _write(os.path.join(self.root, "secret.js"), "outside the web dir")
        self.cfg = Config(path=os.path.join(self.root, "config.json"))
        self.bus = EventBus()
        self.indexer = fakes.FakeIndexer()
        self.bridge = fakes.FakeBridge()
        self.controller = fakes.FakeController()
        self.parsed: list[str] = []
        self.server = Server(self.cfg, self.bus, self.indexer, self.bridge, self.controller,
                             web_dir=self.web_dir, assets_dir=self.assets_dir,
                             parse_hotkey=self._parse_hotkey, sse_heartbeat_s=self.heartbeat)
        self.port = self.server.start(0)
        self.addCleanup(self.server.stop)

    def _parse_hotkey(self, spec: str) -> tuple:
        self.parsed.append(spec)
        if spec == "ctrl+nope":
            raise ValueError("Ugyldig genvejstast")
        return frozenset({"ctrl"}), 0x20

    def req(self, method: str, path: str, **kwargs) -> fakes.Response:
        return fakes.request(self.port, method, path, **kwargs)

    def raw(self, data: bytes, timeout: float = 5.0) -> bytes:
        """Send raw bytes, return everything until the server closes (or 1 s of silence)."""
        with socket.create_connection(("127.0.0.1", self.port), timeout=timeout) as sock:
            sock.sendall(data)
            sock.settimeout(1.0)
            chunks = []
            try:
                while chunk := sock.recv(65536):
                    chunks.append(chunk)
            except (TimeoutError, ConnectionError):
                pass
        return b"".join(chunks)


class SecurityTests(ServerTestBase):
    def test_host_header_must_name_this_server(self) -> None:
        for host in (f"127.0.0.1:{self.port}", f"localhost:{self.port}",
                     f"LOCALHOST:{self.port}"):
            with self.subTest(host=host):
                self.assertEqual(self.req("GET", "/api/status", host=host).status, 200)
        calls = len(self.indexer.called("status"))
        for host in ("evil.example", f"evil.example:{self.port}", f"127.0.0.1:{self.port + 1}",
                     "127.0.0.1", f"127.0.0.1.evil.example:{self.port}", ""):
            with self.subTest(host=host):
                response = self.req("GET", "/api/status", host=host)
                self.assertEqual(response.status, 403)
                self.assertIn("error", response.json())
        self.assertEqual(len(self.indexer.called("status")), calls)

    def test_non_get_requests_need_the_token(self) -> None:
        self.assertEqual(self.req("POST", "/api/scan", body={}, token=False).status, 403)
        self.assertEqual(self.req("POST", "/api/scan", body={}, token=False,
                                  headers={"X-Projektsog": "0"}).status, 403)
        self.assertEqual(self.req("DELETE", "/api/roots", body={"path": "C:\\x"},
                                  token=False).status, 403)
        self.assertEqual(self.indexer.called("scan_now"), [])
        self.assertEqual(self.indexer.called("remove_root"), [])
        self.assertEqual(self.req("POST", "/api/scan", body={}).status, 200)
        self.assertEqual(self.indexer.called("scan_now"), [((None,), {"full": False})])

    def test_no_cors_headers_and_preflight_refused(self) -> None:
        responses = [
            self.req("GET", "/api/status", headers={"Origin": "https://evil.example"}),
            self.req("OPTIONS", "/api/scan", token=False,
                     headers={"Origin": "https://evil.example",
                              "Access-Control-Request-Method": "POST",
                              "Access-Control-Request-Headers": "x-projektsog"}),
            self.req("GET", "/"),
        ]
        self.assertEqual(responses[1].status, 403)
        for response in responses:
            names = [name.lower() for name in response.headers.keys()]
            self.assertFalse([n for n in names if n.startswith("access-control-")], names)

    def test_absolute_and_scheme_relative_targets_are_not_followed(self) -> None:
        host = f"Host: 127.0.0.1:{self.port}\r\nConnection: close\r\n".encode()
        absolute = self.raw(b"GET http://evil.example/app.js HTTP/1.1\r\n" + host + b"\r\n")
        self.assertTrue(absolute.startswith(b"HTTP/1.1 400"), absolute[:40])
        scheme_relative = self.raw(b"GET //evil.example/app.js HTTP/1.1\r\n" + host + b"\r\n")
        self.assertTrue(scheme_relative.startswith(b"HTTP/1.1 404"), scheme_relative[:40])
        for reply in (absolute, scheme_relative):
            self.assertNotIn(b"\r\nlocation:", reply.lower())

    def test_static_paths_cannot_escape_the_web_dirs(self) -> None:
        for path in ("/../secret.txt", "/../secret.js", "/%2e%2e/secret.js",
                     "/assets/../../secret.js", "/assets/..%2f..%2fsecret.js",
                     "/assets/%2e%2e%5c%2e%2e%5csecret.js", "/app.js::$DATA", "/APP.JS.",
                     "/con.js", "/assets/", "/web/../secret.js", "/%00app.js"):
            with self.subTest(path=path):
                response = self.req("GET", path)
                self.assertEqual(response.status, 404, path)
                self.assertNotIn(b"outside the web dir", response.body)


class RoutingTests(ServerTestBase):
    def test_unknown_api_path_is_404_json(self) -> None:
        response = self.req("GET", "/api/nope")
        self.assertEqual(response.status, 404)
        self.assertEqual(response.headers["Content-Type"], "application/json; charset=utf-8")
        self.assertEqual(response.json(), {"error": "Ikke fundet"})

    def test_wrong_method_is_405_with_allow(self) -> None:
        for method, path, allow in (("POST", "/api/status", "GET"),
                                    ("GET", "/api/roots", "DELETE, POST"),
                                    ("PUT", "/api/scan", "POST"),
                                    ("POST", "/api/events", "GET"),
                                    ("DELETE", "/api/open", "POST"),
                                    ("POST", "/", "GET")):
            with self.subTest(method=method, path=path):
                response = self.req(method, path, body={})
                self.assertEqual(response.status, 405)
                self.assertEqual(response.headers["Allow"], allow)

    def test_bad_json_bodies_are_400(self) -> None:
        for raw in (b"{bad", b"[1, 2]", b"\"text\"", b"\xff\xfe", b"[" * 5000):
            with self.subTest(raw=raw[:10]):
                response = self.req("POST", "/api/scan", raw_body=raw)
                self.assertEqual(response.status, 400)
                self.assertIn("error", response.json())
        self.assertEqual(self.indexer.called("scan_now"), [])

    def test_oversized_body_is_413_and_connection_closed(self) -> None:
        reply = self.raw(
            f"POST /api/scan HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nX-Projektsog: 1\r\n"
            f"Content-Type: application/json\r\nContent-Length: {2 * 1024 * 1024}\r\n\r\n"
            .encode())
        self.assertTrue(reply.startswith(b"HTTP/1.1 413"), reply[:40])
        self.assertIn(b"\r\nConnection: close\r\n", reply)

    def test_value_error_is_400_with_message(self) -> None:
        self.indexer.raises["set_source_mode"] = ValueError("Ugyldig tilstand")
        response = self.req("POST", "/api/sources/3/mode", body={"mode": "sometimes"})
        self.assertEqual(response.status, 400)
        self.assertEqual(response.json(), {"error": "Ugyldig tilstand"})

    def test_other_exception_is_500_and_logged(self) -> None:
        self.indexer.raises["list_sources"] = RuntimeError("database is locked")
        with self.assertLogs("projektsog.server", "ERROR") as logs:
            response = self.req("GET", "/api/sources")
        self.assertEqual(response.status, 500)
        self.assertEqual(response.json(), {"error": INTERNAL_ERROR})
        self.assertIn("database is locked", "\n".join(logs.output))

    def test_non_finite_numbers_are_sent_as_null(self) -> None:
        self.indexer.returns["status"] = {"db_size": float("nan"), "entries": 5}
        response = self.req("GET", "/api/status")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.json()["db_size"], None)
        self.assertEqual(response.json()["entries"], 5)

    def test_keep_alive_connection_survives_errors(self) -> None:
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(conn.close)
        headers = {"X-Projektsog": "1", "Content-Type": "application/json"}
        for method, path, body, status in (("GET", "/api/status", None, 200),
                                           ("POST", "/api/scan", "{bad", 400),
                                           ("GET", "/api/nope", None, 404),
                                           ("POST", "/api/scan", "{}", 200),
                                           ("GET", "/app.js", None, 200)):
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            response.read()
            self.assertEqual(response.status, status, (method, path))


class ApiTests(ServerTestBase):
    def test_search_parameter_mapping(self) -> None:
        self.req("GET", "/api/search?q=rikke%20lindholm")
        self.req("GET", "/api/search?q=b%C3%B8gely&kind=file&online=1&source=3&limit=50"
                        "&templates=1")
        self.req("GET", "/api/search?q=x&online=0&templates=0")
        self.req("GET", "/api/search")
        self.assertEqual(self.indexer.called("search"), [
            (("rikke lindholm",), {"kind": "all", "online_only": None, "source_id": None,
                                "limit": None, "include_templates": False}),
            (("bøgely",), {"kind": "file", "online_only": True, "source_id": 3, "limit": 50,
                           "include_templates": True}),
            (("x",), {"kind": "all", "online_only": False, "source_id": None, "limit": None,
                      "include_templates": False}),
            (("",), {"kind": "all", "online_only": None, "source_id": None, "limit": None,
                     "include_templates": False}),
        ])
        response = self.req("GET", "/api/search?q=lindholm")
        self.assertEqual(response.json(), {"query": "lindholm", "results": []})

    def test_search_rejects_invalid_parameters(self) -> None:
        calls = len(self.indexer.called("search"))
        for query in ("kind=bogus", "online=maybe", "source=abc", "limit=0", "limit=-5",
                      "templates=2"):
            with self.subTest(query=query):
                response = self.req("GET", f"/api/search?q=x&{query}")
                self.assertEqual(response.status, 400)
                self.assertIn("error", response.json())
        self.assertEqual(len(self.indexer.called("search")), calls)

    def test_recent_and_children(self) -> None:
        self.assertEqual(self.req("GET", "/api/recent").json(),
                         {"results": [{"id": 7, "name": "Rikke Lindholm"}]})
        self.req("GET", "/api/recent?limit=5")
        self.assertEqual(self.indexer.called("recent_projects"),
                         [((), {"limit": 30}), ((), {"limit": 5})])
        response = self.req("GET", "/api/children?source=3&rel=Rikke%20Lindholm%5CKlip")
        self.assertEqual(response.json(), {"results": [{"id": 8, "rel_path": "Rikke Lindholm\\Klip"}]})
        self.assertEqual(self.indexer.called("children"), [((3, "Rikke Lindholm\\Klip"), {})])
        self.assertEqual(self.req("GET", "/api/children?rel=x").status, 400)

    def test_status_merges_resolve_and_hotkey(self) -> None:
        body = self.req("GET", "/api/status").json()
        expected = dict(self.indexer.returns["status"])
        expected["resolve"] = self.bridge.returns["state"]
        expected["hotkey"] = self.controller.returns["hotkey_status"]
        self.assertEqual(body, expected)

    def test_sources_and_hosts(self) -> None:
        self.assertEqual(self.req("GET", "/api/sources").json(),
                         {"sources": self.indexer.returns["list_sources"],
                          "hosts": self.indexer.returns["hosts"]})

    def test_source_commands(self) -> None:
        self.assertEqual(self.req("POST", "/api/sources/4/mode", body={"mode": "include"}).json(),
                         {"id": 4, "mode": "include"})
        self.assertEqual(self.req("POST", "/api/sources/4/scan", body={"full": True}).json(),
                         {"ok": True})
        self.assertEqual(self.req("POST", "/api/sources/4/forget").json(), {"ok": True})
        self.assertEqual(self.req("POST", "/api/scan").json(), {"ok": True})
        self.assertEqual(self.indexer.called("set_source_mode"), [((4, "include"), {})])
        self.assertEqual(self.indexer.called("scan_now"),
                         [((4,), {"full": True}), ((None,), {"full": False})])
        self.assertEqual(self.indexer.called("forget_source"), [((4,), {})])
        self.assertEqual(self.req("POST", "/api/scan", body={"full": "yes"}).status, 400)
        self.assertEqual(self.req("POST", "/api/sources/4/mode", body={}).status, 400)
        self.assertEqual(self.req("POST", "/api/sources/x/mode", body={"mode": "auto"}).status,
                         404)

    def test_roots_and_hosts(self) -> None:
        self.assertEqual(self.req("POST", "/api/roots", body={"path": "D:\\Forår 2026 RØD"}).json(),
                         {"ok": True, "source": None})
        self.assertEqual(self.req("DELETE", "/api/roots", body={"path": "D:\\Forår 2026 RØD"})
                         .json(), {"ok": True})
        self.assertEqual(self.req("POST", "/api/hosts", body={"name": "GRAFIK-PC"}).json(),
                         {"ok": True})
        self.assertEqual(self.req("DELETE", "/api/hosts", body={"name": "GRAFIK-PC"}).json(),
                         {"ok": True, "forgotten": 0})
        self.assertEqual(self.indexer.called("add_root"), [(("D:\\Forår 2026 RØD",), {})])
        self.assertEqual(self.indexer.called("remove_root"), [(("D:\\Forår 2026 RØD",), {})])
        self.assertEqual(self.indexer.called("add_host"), [(("GRAFIK-PC",), {})])
        self.assertEqual(self.indexer.called("remove_host"), [(("GRAFIK-PC",), {})])
        self.assertEqual(self.req("POST", "/api/roots", body={"path": "  "}).status, 400)
        self.assertEqual(self.req("POST", "/api/hosts", body={"name": 5}).status, 400)

    def test_removing_a_host_reports_what_the_indexer_forgot(self) -> None:
        # SPEC §15.12: the UI words its toast from "forgotten", not from its own count.
        self.indexer.returns["remove_host"] = lambda name: {"ok": True, "forgotten": 3}
        self.assertEqual(self.req("DELETE", "/api/hosts", body={"name": "NAS"}).json(),
                         {"ok": True, "forgotten": 3})
        # A folder the user added on that computer: refused, nothing changes (400, Danish).
        refusal = "Mappen ‘\\\\NAS\\Arkiv\\Gammelt’ ligger på NAS – fjern den først"
        self.indexer.raises["remove_host"] = ValueError(refusal)
        response = self.req("DELETE", "/api/hosts", body={"name": "NAS"})
        self.assertEqual((response.status, response.json()), (400, {"error": refusal}))
        self.assertEqual(self.req("DELETE", "/api/hosts", body={}).status, 400)
        self.assertEqual(self.indexer.called("remove_host"), [(("NAS",), {}), (("NAS",), {})])

    def test_open_is_forwarded_to_the_controller(self) -> None:
        path = "\\\\GRAFIK-PC\\Kunder 2026 (Grafik)\\Klar Tand - Silkeborg"
        response = self.req("POST", "/api/open", body={"path": path, "action": "reveal"})
        self.assertEqual(response.json(), {"ok": True, "path": path})
        self.assertEqual(self.controller.called("open_path"), [((path, "reveal"), {})])
        self.controller.returns["open_path"] = {"ok": False, "error": "Placeringen svarer ikke"}
        response = self.req("POST", "/api/open", body={"path": path})
        self.assertEqual(response.status, 200)       # user-level failure: HTTP 200, ok false
        self.assertEqual(response.json(), {"ok": False, "error": "Placeringen svarer ikke"})
        self.assertEqual(self.controller.called("open_path")[-1], ((path, "folder"), {}))
        self.assertEqual(self.req("POST", "/api/open", body={"action": "folder"}).status, 400)

    def test_resolve_endpoints(self) -> None:
        self.assertEqual(self.req("GET", "/api/resolve").json(), self.bridge.returns["state"])
        self.assertEqual(self.req("POST", "/api/resolve/refresh").json(),
                         {"enabled": True, "refreshed": True})
        self.assertEqual(self.req("POST", "/api/resolve/open").json(),
                         self.bridge.returns["open_primary"])
        self.assertEqual(self.bridge.called("open_primary"), [((), {})])   # no body: no args

    def test_resolve_open_passes_the_scripts_project_through(self) -> None:
        # SPEC §15.9: the menu script names its Resolve project and database.
        for body, expected in (
                ({"project": "Rikke Lindholm - Testimonial", "database": "Kunder 2026 (Projektserver)"},
                 {"project": "Rikke Lindholm - Testimonial", "database": "Kunder 2026 (Projektserver)"}),
                ({"project": "Klar Tand - Silkeborg"}, {"project": "Klar Tand - Silkeborg"}),
                ({"project": "Untitled Project", "database": None},
                 {"project": "Untitled Project"}),
                ({"project": "Rikke Lindholm", "database": "Kunder 2026 (Projektserver)",
                  "uid": "8f1c-42"},
                 {"project": "Rikke Lindholm", "database": "Kunder 2026 (Projektserver)",
                  "uid": "8f1c-42"}),
                ({}, {})):
            with self.subTest(body=body):
                response = self.req("POST", "/api/resolve/open", body=body)
                self.assertEqual(response.status, 200)
                self.assertEqual(self.bridge.called("open_primary")[-1], ((), expected))
        calls = len(self.bridge.called("open_primary"))
        for body in ({"project": 5}, {"database": ["x"]}, {"uid": 7}):
            with self.subTest(body=body):
                self.assertEqual(self.req("POST", "/api/resolve/open", body=body).status, 400)
        self.assertEqual(len(self.bridge.called("open_primary")), calls)

    def test_window_and_quit_endpoints(self) -> None:
        self.assertEqual(self.req("POST", "/api/window/hide", body={"restore_previous": True})
                         .json(), {"ok": True})
        self.assertEqual(self.req("POST", "/api/window/show").json(), {"ok": True})
        self.assertEqual(self.req("POST", "/api/quit").json(), {"ok": True})
        self.assertEqual(self.controller.called("hide_window"),
                         [((), {"restore_previous": True})])
        self.assertEqual(self.controller.called("show_window"),
                         [((None, "api"), {"panel": None})])
        self.assertEqual(len(self.controller.called("request_exit")), 1)

    def test_window_show_is_refused_while_exiting(self) -> None:
        # APP-2: a second launch must not get {"ok": true} from an instance that is closing.
        self.controller.exiting.set()
        response = self.req("POST", "/api/window/show")
        self.assertEqual(response.status, 503)
        self.assertEqual(response.json(), {"error": "Projektsøg lukker ned"})
        self.assertEqual(self.controller.called("show_window"), [])
        self.assertEqual(self.req("GET", "/api/status").status, 200)   # the rest still works


class SettingsTests(ServerTestBase):
    def test_get_settings(self) -> None:
        settings = self.req("GET", "/api/settings").json()["settings"]
        expected = self.cfg.snapshot()
        expected["run_at_login"] = False
        self.assertEqual(settings, expected)

    def test_post_applies_run_at_login_hotkey_and_config(self) -> None:
        changes: list[dict] = []
        self.cfg.on_change(changes.append)
        response = self.req("POST", "/api/settings", body={
            "run_at_login": True, "hotkey": "ctrl+space", "hide_after_open": False})
        self.assertEqual(response.status, 200)
        self.assertEqual(self.controller.called("set_run_at_login"), [((True,), {})])
        self.assertEqual(self.parsed, ["ctrl+space"])
        self.assertEqual(self.cfg["hotkey"], "ctrl+space")
        self.assertFalse(self.cfg["hide_after_open"])
        self.assertEqual(len(changes), 1)
        self.assertNotIn("run_at_login", self.cfg.snapshot())
        settings = response.json()["settings"]
        self.assertEqual(settings["hotkey"], "ctrl+space")
        self.assertIn("run_at_login", settings)

    def test_invalid_hotkey_changes_nothing(self) -> None:
        before = self.cfg.snapshot()
        response = self.req("POST", "/api/settings", body={
            "run_at_login": True, "hotkey": "ctrl+nope", "hide_after_open": False})
        self.assertEqual(response.status, 400)
        self.assertEqual(response.json(), {"error": "Ugyldig genvejstast"})
        self.assertEqual(self.controller.called("set_run_at_login"), [])
        self.assertEqual(self.cfg.snapshot(), before)

    def test_invalid_settings_change_nothing(self) -> None:
        before = self.cfg.snapshot()
        for body in ({"run_at_login": True, "no_such_setting": 1},
                     {"run_at_login": "yes"},
                     {"hotkey": 5},
                     {"result_limit": "many"},
                     {"resolve_follow": "sometimes"}):
            with self.subTest(body=body):
                response = self.req("POST", "/api/settings", body=body)
                self.assertEqual(response.status, 400)
                self.assertIn("error", response.json())
        self.assertEqual(self.controller.called("set_run_at_login"), [])
        self.assertEqual(self.parsed, [])
        self.assertEqual(self.cfg.snapshot(), before)


class StaticTests(ServerTestBase):
    def test_static_files_are_served_with_no_store(self) -> None:
        for path, content_type in (("/", "text/html; charset=utf-8"),
                                   ("/app.js", "text/javascript; charset=utf-8"),
                                   ("/style.css", "text/css; charset=utf-8"),
                                   ("/assets/icon.png", "image/png")):
            with self.subTest(path=path):
                response = self.req("GET", path)
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers["Content-Type"], content_type)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("Projektsøg".encode(), self.req("GET", "/").body)

    def test_favicon_is_the_app_icon(self) -> None:
        self.assertEqual(self.req("GET", "/favicon.ico").status, 404)
        _write(os.path.join(self.assets_dir, "icon.ico"), b"\x00\x00\x01\x00")
        response = self.req("GET", "/favicon.ico")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Content-Type"], "image/x-icon")

    def test_only_web_file_types_are_served(self) -> None:
        self.assertEqual(self.req("GET", "/notes.txt").status, 404)
        self.assertEqual(self.req("GET", "/missing.js").status, 404)

    def test_csp_allows_no_external_origin(self) -> None:
        csp = self.req("GET", "/").headers["Content-Security-Policy"]
        allowed = {"'self'", "'unsafe-inline'", "'none'", "data:", "blob:"}
        for directive in filter(None, (part.strip() for part in csp.split(";"))):
            name, *sources = directive.split()
            with self.subTest(directive=name):
                self.assertTrue(set(sources) <= allowed, directive)
        self.assertIn("default-src 'self'", csp)
        self.assertIn("connect-src 'self'", csp)

    def test_shipped_ui_loads_nothing_external(self) -> None:
        web_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "projektsog", "web")
        names = [n for n in ("index.html", "app.js", "style.css")
                 if os.path.isfile(os.path.join(web_dir, n))]
        if not names:
            self.skipTest("projektsog/web is not written yet")
        external = re.compile(
            r"""(?:\b(?:src|href|action)\s*=\s*["']?|url\(\s*["']?|@import\s+["']?|"""
            r"""\b(?:fetch|EventSource|import)\s*\(\s*["'`])\s*(?:https?:)?//""", re.I)
        for name in names:
            with open(os.path.join(web_dir, name), encoding="utf-8") as fh:
                text = fh.read()
            self.assertIsNone(external.search(text), f"{name} loads an external resource")


class EventStreamTests(ServerTestBase):
    heartbeat = 0.1

    def open_stream(self) -> tuple[socket.socket, bytes]:
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        self.addCleanup(sock.close)
        sock.sendall(f"GET /api/events HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
                     "Accept: text/event-stream\r\n\r\n".encode())
        data = self.read_until(sock, b"", b"\r\n\r\n")
        head, _, rest = data.partition(b"\r\n\r\n")
        self.assertTrue(head.startswith(b"HTTP/1.1 200"), head)
        self.assertIn(b"Content-Type: text/event-stream; charset=utf-8", head)
        self.assertIn(b"Cache-Control: no-store", head)
        return sock, rest

    def read_until(self, sock: socket.socket, data: bytes, marker: bytes,
                   timeout: float = 3.0) -> bytes:
        deadline = time.monotonic() + timeout
        while marker not in data:
            self.assertLess(time.monotonic(), deadline, f"{marker!r} not received: {data!r}")
            chunk = sock.recv(65536)
            self.assertTrue(chunk, f"stream closed before {marker!r}")
            data += chunk
        return data

    def test_published_events_are_streamed(self) -> None:
        sock, data = self.open_stream()
        self.assertTrue(fakes.wait_until(lambda: self.bus.subscriber_count == 1))
        self.bus.publish("scan_progress", {"source_id": 3, "name": "Forår 2026 RØD",
                                           "entries": 120000})
        data = self.read_until(sock, data, b"event: scan_progress\n")
        frame = data[data.index(b"event: scan_progress\n"):]
        frame = self.read_until(sock, frame, b"\n\n")
        line = frame.split(b"\n")[1]
        self.assertTrue(line.startswith(b"data: "))
        self.assertEqual(json.loads(line[len(b"data: "):].decode("utf-8")),
                         {"source_id": 3, "name": "Forår 2026 RØD", "entries": 120000})

    def test_heartbeat_comment(self) -> None:
        sock, data = self.open_stream()
        self.read_until(sock, data, b": ping\n\n")

    def test_client_disconnect_unsubscribes(self) -> None:
        sock, _ = self.open_stream()
        self.assertTrue(fakes.wait_until(lambda: self.bus.subscriber_count == 1))
        sock.close()
        self.assertTrue(fakes.wait_until(lambda: self.bus.subscriber_count == 0, timeout=5))

    def test_stop_ends_streams_promptly(self) -> None:
        self.server.sse_heartbeat_s = 15.0      # only the stop may end this stream
        sock, data = self.open_stream()
        self.assertTrue(fakes.wait_until(lambda: self.bus.subscriber_count == 1))
        started = time.monotonic()
        self.server.stop()
        self.assertLess(time.monotonic() - started, 2.0)
        sock.settimeout(2.0)
        try:
            while sock.recv(65536):
                pass
        except ConnectionError:
            pass
        self.assertEqual(self.bus.subscriber_count, 0)


class ServerLifecycleTests(unittest.TestCase):
    def make_server(self, **kwargs) -> Server:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = Config(path=os.path.join(tmp.name, "config.json"))
        server = Server(cfg, EventBus(), fakes.FakeIndexer(), fakes.FakeBridge(),
                        fakes.FakeController(), web_dir=tmp.name, assets_dir=tmp.name, **kwargs)
        self.addCleanup(server.stop)
        return server

    def test_next_port_is_used_when_taken(self) -> None:
        blocker = socket.socket()
        self.addCleanup(blocker.close)
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        taken = blocker.getsockname()[1]
        server = self.make_server()
        port = server.start(taken)
        self.assertGreater(port, taken)
        self.assertLessEqual(port, taken + 20)
        self.assertEqual(fakes.request(port, "GET", "/api/status").status, 200)

    def test_port_is_held_exclusively(self) -> None:
        server = self.make_server()
        port = server.start(0)
        intruder = socket.socket()
        self.addCleanup(intruder.close)
        intruder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        with self.assertRaises(OSError):
            intruder.bind(("127.0.0.1", port))

    def test_start_twice_is_an_error_and_stop_is_idempotent(self) -> None:
        server = self.make_server()
        server.start(0)
        with self.assertRaises(RuntimeError):
            server.start(0)
        server.stop()
        server.stop()

    def test_stop_closes_idle_keep_alive_connections(self) -> None:
        import http.client
        server = self.make_server()
        port = server.start(0)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        self.addCleanup(conn.close)
        conn.request("GET", "/api/status")
        response = conn.getresponse()
        response.read()
        self.assertEqual(response.status, 200)
        self.assertFalse(response.will_close)       # an idle keep-alive connection remains
        server.stop()
        conn.sock.settimeout(2.0)
        try:
            self.assertEqual(conn.sock.recv(65536), b"")
        except ConnectionError:
            pass

    def test_works_without_stderr_like_pythonw(self) -> None:
        server = self.make_server()
        port = server.start(0)
        saved = sys.stderr
        sys.stderr = None
        try:
            self.assertEqual(fakes.request(port, "GET", "/api/status").status, 200)
            self.assertEqual(fakes.request(port, "GET", "/api/nope").status, 404)
            with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
                # Unknown method: the stdlib answers via send_error() -> log_error().
                sock.sendall(f"BREW /api/status HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n"
                             .encode())
                self.assertTrue(sock.recv(65536).startswith(b"HTTP/1.1 501"))
            self.assertEqual(fakes.request(port, "GET", "/api/status").status, 200)
        finally:
            sys.stderr = saved

    def test_requests_are_logged_at_debug_level(self) -> None:
        server = self.make_server()
        port = server.start(0)
        with self.assertLogs("projektsog.server", "DEBUG") as logs:
            fakes.request(port, "GET", "/api/status")
        self.assertTrue(any("/api/status" in line and line.startswith("DEBUG")
                            for line in logs.output), logs.output)

    def test_log_lines_escape_control_characters(self) -> None:
        server = self.make_server()
        port = server.start(0)
        with self.assertLogs("projektsog.server", "DEBUG") as logs:
            with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
                sock.sendall(f"GET /api/x\x1b[2J\x08 HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                             "Connection: close\r\n\r\n".encode("latin-1"))
                self.assertTrue(sock.recv(65536).startswith(b"HTTP/1.1 404"))
        text = "\n".join(logs.output)
        self.assertIn("/api/x\\x1b[2J\\x08", text)
        self.assertNotIn("\x1b", text)


if __name__ == "__main__":
    unittest.main()
