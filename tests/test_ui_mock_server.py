"""The UI mock backend serves the real pages and speaks the SPEC §7.1/§8/§11 contract."""

from __future__ import annotations

import http.client
import json
import os
import tempfile
import time
import unittest
import urllib.parse
from typing import Any

from tests._ui_mock_server import MockServer

ITEM_KEYS = {"id", "kind", "name", "hl", "path", "open_path", "unc_path", "rel_path", "parent", "depth",
             "source", "project", "size", "mtime", "file_count", "ext", "is_seq", "seq_count",
             "subfolders", "score"}
SOURCE_REF_KEYS = {"id", "name", "host", "kind", "online", "drive", "disk_name", "volume_label", "last_seen",
                   "is_system", "volume_present"}  # SPEC §15.4, §15.12
SOURCE_KEYS = {"id", "key", "kind", "display_name", "host", "path", "unc_path", "volume_label",
               "volume_serial", "fs", "drive", "last_drive", "disk_name", "hotplug", "volume_size",
               "online", "mode", "included", "auto_reason", "manual", "entry_count", "dir_count",
               "file_count", "project_count", "total_size", "last_scan_end", "last_scan_ok",
               "last_error", "last_seen", "scanning", "scan_kind", "queued",
               "is_system", "root_is_project",                 # SPEC §15.3/§15.4
               "last_shallow_scan", "volume_present"}          # SPEC §15.12
STATUS_KEYS = {"hostname", "version", "sources_total", "sources_online", "sources_offline",
               "sources_excluded", "sources_ready", "sources_included_online", "entries", "files",
               "dirs", "projects", "scanning", "queued", "last_scan_end", "initial_scan_done",
               "worker", "db_size"}
RESOLVE_KEYS = {"enabled", "running", "connected", "error", "project", "database", "clip_count",
                "updated", "folders", "other_dirs", "suggestions", "primary", "offline_clips",
                "offline_disks"}

_tmp: tempfile.TemporaryDirectory | None = None


def setUpModule() -> None:
    global _tmp
    _tmp = tempfile.TemporaryDirectory()
    os.environ["LOCALAPPDATA"] = _tmp.name


def tearDownModule() -> None:
    if _tmp is not None:
        _tmp.cleanup()


class MockServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = MockServer(scenario="asked").start()

    def tearDown(self) -> None:
        self.server.stop()
        self.assertEqual(self.server.backend.errors, [])

    def request(self, method: str, path: str, body: Any = None, headers: dict[str, str] | None = None
                ) -> tuple[int, dict[str, str], bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=10)
        try:
            payload = json.dumps(body).encode() if body is not None else None
            all_headers = {"Host": f"127.0.0.1:{self.server.port}", **(headers or {})}
            if payload is not None:
                all_headers["Content-Type"] = "application/json"
            conn.request(method, path, body=payload, headers=all_headers)
            response = conn.getresponse()
            return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
        finally:
            conn.close()

    def get_json(self, path: str, **params: Any) -> Any:
        query = f"?{urllib.parse.urlencode(params)}" if params else ""
        status, _, data = self.request("GET", path + query)
        self.assertEqual(status, 200, data)
        return json.loads(data)

    def post_json(self, path: str, body: Any = None, method: str = "POST") -> tuple[int, Any]:
        status, _, data = self.request(method, path, body if body is not None else {}, {"X-Projektsog": "1"})
        return status, json.loads(data)

    def search(self, q: str, **params: Any) -> dict[str, Any]:
        return self.get_json("/api/search", q=q, **params)

    # -- static & security -------------------------------------------------------------
    def test_serves_the_ui_without_caching(self) -> None:
        for path, kind in (("/", "text/html"), ("/app.js", "text/javascript"), ("/style.css", "text/css")):
            status, headers, body = self.request("GET", path)
            self.assertEqual(status, 200, path)
            self.assertTrue(headers["content-type"].startswith(kind), headers)
            self.assertEqual(headers["cache-control"], "no-store")
            self.assertTrue(body)
        self.assertIn("<title>Projektsøg</title>", self.request("GET", "/")[2].decode("utf-8"))
        self.assertEqual(self.request("GET", "/assets/..%5Cconfig.py")[0], 404)
        self.assertEqual(self.request("GET", "/nope")[0], 404)

    def test_rejects_foreign_host_and_writes_without_header(self) -> None:
        self.assertEqual(self.request("GET", "/api/status", headers={"Host": "evil.example:80"})[0], 403)
        self.assertEqual(self.request("POST", "/api/scan", {})[0], 403)
        self.assertEqual(self.post_json("/api/scan", {"full": False}), (200, {"ok": True}))

    # -- search ------------------------------------------------------------------------
    def test_search_items_follow_the_shared_shapes(self) -> None:
        result = self.search("lindholm")
        self.assertLessEqual({"query", "tokens", "took_ms", "total", "truncated", "results"}, set(result))
        first = result["results"][0]
        self.assertEqual((first["name"], first["kind"]), ("Rikke Lindholm", "project"))
        self.assertEqual(first["hl"], [[6, 14]])
        self.assertEqual(set(first), ITEM_KEYS)
        self.assertEqual(set(first["source"]), SOURCE_REF_KEYS)
        self.assertEqual(first["project"], {"name": "Rikke Lindholm", "rel_path": "Rikke Lindholm",
                                            "path": "C:\\Kunder 2026 (STUDIO)\\Rikke Lindholm",
                                            "unc_path": "\\\\STUDIO-PC\\Kunder 2026 (STUDIO)\\Rikke Lindholm"})
        self.assertIn("Klip", first["subfolders"])
        self.assertNotIn("hidden", result)

    def test_acceptance_queries_of_the_spec(self) -> None:
        names = lambda q, n=3: [r["name"] for r in self.search(q)["results"][:n]]  # noqa: E731
        self.assertEqual(names("rikke lindholm", 1), ["Rikke Lindholm"])
        klip = self.search("lindholm klip")["results"][0]
        self.assertEqual((klip["kind"], klip["rel_path"]), ("dir", "Rikke Lindholm\\Klip"))
        self.assertEqual(names("klar tand silkeborg"),
                         ["Klar Tand - Silkeborg", "Klar Tand - Silkeborg C", "Klar Tand - Voxpop Silkeborg"])
        self.assertEqual(names("pixelbro", 1), ["Pixelbro"])
        self.assertIn("Pixelbro Radio", names("pixelbro", 5))
        self.assertEqual(set(names("bøgely", 6)), set(names("bogely", 6)))
        self.assertIn("Hotel Bøgelyhus", names("bogely", 10))
        self.assertEqual(names("forar pixelbro", 1), ["Pixelbro"])
        clip = self.search("FX9_7912")["results"][0]
        self.assertEqual(clip["rel_path"], "Rikke Lindholm\\Klip\\FX9\\FX9_7912.MXF")
        self.assertEqual(self.search("kundenavn")["total"], 0)
        self.assertEqual(self.search("kundenavn", templates=1)["results"][0]["kind"], "template")

    def test_a_folder_that_is_itself_a_project_is_found_by_its_name(self) -> None:
        """§15.3: the source root is the project – a synthesised Item (rel_path "", id -source_id)."""
        found = self.search("julefrokost 2026")["results"]
        root = next(r for r in found if r["kind"] == "project")
        self.assertEqual((root["name"], root["rel_path"], root["id"], root["depth"]),
                         ("Dækcentret Julefrokost 2026", "", -15, 0))
        self.assertEqual(root["path"], "E:\\Dækcentret Julefrokost 2026")  # no trailing backslash
        self.assertEqual(root["project"]["rel_path"], "")
        self.assertIn("Klip", root["subfolders"])
        self.assertGreater(root["file_count"], 0)
        clip = self.search("c0002")["results"][0]
        self.assertEqual(clip["project"], {"name": "Dækcentret Julefrokost 2026", "rel_path": "",
                                           "path": "E:\\Dækcentret Julefrokost 2026", "unc_path": None})
        recent = [r["name"] for r in self.get_json("/api/recent")["results"]]
        self.assertIn("Dækcentret Julefrokost 2026", recent)
        kids = self.get_json("/api/children", source=15, rel="")["results"]
        self.assertNotIn("", [k["rel_path"] for k in kids])
        sources = {s["id"]: s for s in self.get_json("/api/sources")["sources"]}
        self.assertTrue(sources[15]["root_is_project"])
        self.assertEqual([sid for sid, s in sources.items() if s["is_system"]], [1, 14])  # on C:

    def test_set_online_moves_a_disk_and_publishes_like_the_indexer(self) -> None:
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        conn.request("GET", "/api/events?mock=asked,nofocus,idle", headers={"Host": f"127.0.0.1:{self.server.port}"})
        response = conn.getresponse()
        self.assertEqual(self.post_json("/api/_mock/set-online", {"source_id": 3, "online": True,
                                                                  "path": "I:\\2024 Disk Sølv"}), (200, {"ok": True}))
        seen: dict[str, Any] = {}
        event = None
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not {"sources", "index_updated"} <= set(seen):
            line = response.fp.readline().decode("utf-8").rstrip("\n")
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: ") and event:
                seen[event] = json.loads(line[6:])
        conn.close()
        self.assertEqual(seen.get("sources"), {"changed": [3]})
        self.assertEqual(seen.get("index_updated"), {"source_id": 3})  # §15.1: online changes too
        radio = self.search("pixelbro radio")["results"][0]
        self.assertEqual((radio["source"]["online"], radio["source"]["drive"], radio["path"]),
                         (True, "I:", "I:\\2024 Disk Sølv\\Pixelbro Radio"))
        opened = self.post_json("/api/open", {"path": radio["path"], "action": "folder"})
        self.assertEqual(opened, (200, {"ok": True, "path": "I:\\2024 Disk Sølv\\Pixelbro Radio"}))
        self.assertEqual(self.post_json("/api/_mock/set-online", {"source_id": 3, "online": False})[0], 200)
        self.assertFalse(self.search("pixelbro radio")["results"][0]["source"]["online"])
        self.assertEqual(self.post_json("/api/_mock/set-online", {"source_id": 3, "events": "x"})[0], 400)

    def test_removing_a_host_forgets_its_shares_and_says_how_many(self) -> None:
        """§15.8/§15.12: remove_host() forgets the host's shares and answers {"ok", "forgotten"};
        a share also reached through a mapped drive stays."""
        self.server.backend.sources[10].mapped = True  # 'Forår 2026 (HDD)' is mapped as a drive too
        self.assertEqual(self.post_json("/api/hosts", {"name": "GRAFIK-PC"}, method="DELETE"),
                         (200, {"ok": True, "forgotten": 2}))
        left = [s["display_name"] for s in self.get_json("/api/sources")["sources"] if s["host"] == "GRAFIK-PC"]
        self.assertEqual(left, ["Forår 2026 (HDD)"])
        self.assertEqual(self.search("pixelbro podcast")["total"], 0)
        self.assertNotIn("GRAFIK-PC", self.get_json("/api/settings")["settings"]["hosts"])
        self.assertEqual(self.post_json("/api/hosts", {"name": "KLIPPER-PC"}, method="DELETE"),
                         (200, {"ok": True, "forgotten": 2}))

    def test_removing_a_host_with_an_added_folder_on_it_is_refused(self) -> None:
        """§15.12: an extra root on the computer → ValueError naming it (400), nothing changes."""
        self.assertTrue(self.post_json("/api/roots", {"path": "\\\\GRAFIK-PC\\Arkiv"})[1]["ok"])
        listed = lambda: [(s["id"], s["display_name"]) for s in self.get_json("/api/sources")["sources"]]  # noqa: E731
        before = listed()
        status, body = self.post_json("/api/hosts", {"name": "grafik-pc"}, method="DELETE")
        self.assertEqual((status, body), (400, {"error": "Mappen ‘\\\\GRAFIK-PC\\Arkiv’ ligger på GRAFIK-PC – fjern den først"}))
        self.assertEqual(listed(), before)
        self.assertGreater(self.search("pixelbro podcast")["total"], 0)
        self.assertIn("GRAFIK-PC", self.get_json("/api/settings")["settings"]["hosts"])
        self.assertEqual(self.post_json("/api/roots", {"path": "\\\\GRAFIK-PC\\Arkiv"}, method="DELETE"), (200, {"ok": True}))
        self.assertEqual(self.post_json("/api/hosts", {"name": "GRAFIK-PC"}, method="DELETE"),
                         (200, {"ok": True, "forgotten": 3}))

    def test_volume_present_tells_a_gone_folder_from_a_missing_disk_or_host(self) -> None:
        """§15.12: offline + volume_present → "Mappen findes ikke længere", else the disk/host hint."""
        def ref(q: str) -> dict[str, Any]:
            return self.search(q)["results"][0]["source"]
        disk = ref("pixelbro radio")  # H: '2024 Disk Sølv' is unplugged
        self.assertEqual((disk["online"], disk["volume_present"]), (False, False))
        self.assertEqual(ref("rikke lindholm")["volume_present"], True)  # online
        self.assertEqual(self.post_json("/api/_mock/set-online", {"source_id": 1, "online": False})[0], 200)
        gone = ref("rikke lindholm")  # C:\Kunder 2026 (STUDIO) moved away – C: is still there
        self.assertEqual((gone["online"], gone["volume_present"], gone["is_system"]), (False, True, True))
        opened = self.post_json("/api/open", {"path": "C:\\Kunder 2026 (STUDIO)\\Rikke Lindholm", "action": "folder"})
        self.assertEqual(opened, (200, {"ok": False, "error": "Mappen findes ikke længere"}))
        refused = self.post_json("/api/open", {"path": "H:\\2024 Disk Sølv\\Pixelbro Radio", "action": "folder"})
        self.assertEqual(refused[1]["error"], "Tilslut disken ‘2024 Disk Sølv’")
        self.assertEqual(self.post_json("/api/_mock/set-online", {"source_id": 10, "online": False})[0], 200)
        sources = {s["id"]: s for s in self.get_json("/api/sources")["sources"]}
        self.assertEqual((sources[10]["online"], sources[10]["volume_present"]), (False, True))  # GRAFIK-PC answers
        self.assertEqual((sources[1]["online"], sources[1]["volume_present"]), (False, True))
        self.assertEqual(sources[3]["volume_present"], False)
        resolve = self.get_json("/api/resolve")
        self.assertEqual(resolve["offline_disks"], ["2024 Disk Sølv"])  # the gone folder is no disk to connect
        self.assertEqual(resolve["offline_clips"], 166)
        referer = f"http://127.0.0.1:{self.server.port}/?mock=host-offline"
        status, _, data = self.request("GET", "/api/search?q=solkraft%20midt", headers={"Referer": referer})
        self.assertEqual(status, 200)
        host = json.loads(data)["results"][0]["source"]
        self.assertEqual((host["online"], host["volume_present"]), (False, False))  # the computer does not answer

    def test_sequences_open_their_first_frame(self) -> None:
        seq = self.search("render exr")["results"][0]
        self.assertTrue(seq["is_seq"])
        self.assertEqual(seq["seq_count"], 4500)
        self.assertTrue(seq["open_path"].endswith("\\Render\\render_0001.exr"))
        self.assertTrue(seq["path"].endswith("\\Render\\render_[0001-4500].exr"))

    def test_hidden_counts_explain_empty_filtered_results(self) -> None:
        by_kind = self.search("grafik", kind="file")
        self.assertEqual(by_kind["total"], 0)
        self.assertGreater(by_kind["hidden"]["kind"], 0)
        offline = self.search("fagmesse", online=1)
        self.assertEqual(offline["hidden"], {"kind": 0, "offline": 4, "source": 0, "any": 4})
        both = self.search("fagmesse", kind="file", online=1)  # the project fails two filters
        self.assertEqual(both["hidden"], {"kind": 0, "offline": 3, "source": 0, "any": 4})
        elsewhere = self.search("pixelbro podcast", source=1)
        self.assertGreater(elsewhere["hidden"]["source"], 0)
        self.assertGreater(self.search("fagmesse", online=0)["total"], 0)

    # -- status, sources, settings, resolve --------------------------------------------
    def test_status_sources_settings_and_resolve_shapes(self) -> None:
        status = self.get_json("/api/status")
        self.assertEqual(set(status) - {"resolve", "hotkey"}, STATUS_KEYS)
        self.assertEqual((status["sources_total"], status["sources_online"], status["sources_offline"],
                          status["sources_excluded"]), (15, 12, 1, 2))
        self.assertEqual(status["hotkey"], {"spec": "shift+space", "label": "Shift+Mellemrum",
                                            "enabled": True, "active": True, "mode": "ll"})
        self.assertEqual(set(status["resolve"]), RESOLVE_KEYS)
        self.assertEqual(status["resolve"]["primary"]["match"], "media")
        sources = self.get_json("/api/sources")
        self.assertEqual({frozenset(s) for s in sources["sources"]}, {frozenset(SOURCE_KEYS)})
        self.assertEqual(set(sources["hosts"][0]), {"name", "online", "shares", "last_seen", "self"})
        settings = self.get_json("/api/settings")["settings"]
        self.assertIs(settings["run_at_login"], True)
        self.assertIs(settings["resolve_hotkey_asked"], True)  # scenario "asked"

    def test_scenarios_follow_the_page_url_in_the_referer(self) -> None:
        def resolve(mock: str) -> dict[str, Any]:
            referer = f"http://127.0.0.1:{self.server.port}/?q=x&mock={mock}"
            status, _, data = self.request("GET", "/api/resolve", headers={"Referer": referer})
            self.assertEqual(status, 200)
            return json.loads(data)
        self.assertTrue(resolve("resolve-error")["error"].startswith("Slå ekstern scripting til"))
        self.assertFalse(resolve("resolve-off")["running"])
        self.assertEqual(resolve("resolve-suggestion")["primary"]["match"], "name")
        self.assertEqual(resolve("resolve-offline")["offline_clips"], 60)

    # -- commands ----------------------------------------------------------------------
    def test_commands_validate_like_the_real_api(self) -> None:
        self.assertEqual(self.post_json("/api/sources/13/mode", {"mode": "include"})[1]["included"], True)
        self.assertEqual(self.post_json("/api/sources/13/mode", {"mode": "sometimes"})[0], 400)
        status, body = self.post_json("/api/sources/1/forget")
        self.assertEqual((status, body), (400, {"error": "Kun offline placeringer kan glemmes"}))
        self.assertEqual(self.post_json("/api/sources/3/forget"), (200, {"ok": True}))
        self.assertEqual(self.post_json("/api/sources/999/scan", {"full": False}),
                         (400, {"error": "Placeringen findes ikke"}))
        self.assertEqual(self.post_json("/api/roots", {"path": "Arkiv"})[0], 400)
        added = self.post_json("/api/roots", {"path": "D:\\Arkiv"})[1]
        self.assertTrue(added["ok"])
        self.assertTrue(added["source"]["manual"])
        self.assertEqual(self.post_json("/api/roots", {"path": "D:\\Arkiv"}, method="DELETE"), (200, {"ok": True}))
        self.assertEqual(self.post_json("/api/hosts", {"name": "ny pc"})[0], 400)
        self.assertEqual(self.post_json("/api/hosts", {"name": "NYPC"}), (200, {"ok": True}))
        self.assertEqual(self.post_json("/api/settings", {"hotkey": "ctrl+"})[0], 400)
        self.assertEqual(self.post_json("/api/settings", {"nope": 1})[0], 400)
        saved = self.post_json("/api/settings", {"hotkey": "Ctrl+Alt+P", "run_at_login": False})[1]["settings"]
        self.assertEqual((saved["hotkey"], saved["run_at_login"]), ("ctrl+alt+p", False))

    def test_open_refuses_offline_locations(self) -> None:
        status, body = self.post_json("/api/open", {"path": "H:\\2024 Disk Sølv\\Pixelbro Radio", "action": "folder"})
        self.assertEqual((status, body), (200, {"ok": False, "error": "Tilslut disken ‘2024 Disk Sølv’"}))
        ok = self.post_json("/api/open", {"path": "C:\\Kunder 2026 (STUDIO)\\Rikke Lindholm", "action": "reveal"})
        self.assertEqual(ok, (200, {"ok": True, "path": "C:\\Kunder 2026 (STUDIO)\\Rikke Lindholm"}))
        self.assertEqual(self.post_json("/api/open", {"path": "C:\\x", "action": "delete"})[0], 400)
        calls = [c["body"]["action"] for c in self.server.backend.calls if c["path"] == "/api/open"]
        self.assertEqual(calls, ["folder", "reveal", "delete"])

    # -- SSE ---------------------------------------------------------------------------
    def test_event_stream_emits_progress_status_focus_and_published_events(self) -> None:
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        conn.request("GET", "/api/events", headers={"Host": f"127.0.0.1:{self.server.port}"})
        response = conn.getresponse()
        self.assertTrue(response.getheader("Content-Type").startswith("text/event-stream"))
        self.post_json("/api/_mock/publish", {"type": "new_volume", "data": {"disk_name": "X", "drive": "X:",
                                                                             "source_ids": [], "included": True,
                                                                             "reason": "Mediefiler fundet"}})
        seen: dict[str, Any] = {}
        deadline = time.monotonic() + 3
        event = None
        while time.monotonic() < deadline and not {"scan_progress", "status", "focus", "new_volume"} <= set(seen):
            line = response.fp.readline().decode("utf-8").rstrip("\n")
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: ") and event:
                seen[event] = json.loads(line[6:])
        conn.close()
        self.assertLessEqual({"scan_progress", "status", "focus", "new_volume"}, set(seen))
        self.assertEqual(seen["focus"], {"from_app": "Resolve.exe", "reason": "hotkey"})
        self.assertEqual(set(seen["scan_progress"]), {"source_id", "name", "entries", "dirs", "units_done",
                                                      "units_total", "started", "kind"})
        self.assertNotIn("resolve", seen["status"])


if __name__ == "__main__":
    unittest.main()
