"""Renders and offline media over HTTP (SPEC §22.1, §22.2): GET /api/render,
POST /api/resolve/offline and POST /api/resolve/relink, served by a real ResolveBridge on its
own thread - with the helper's request handling in-process against a fake Resolve, a fake
index and target folders in a temp dir."""

from __future__ import annotations

import os
import queue
from typing import Any

from projektsog import resolve_bridge as rb
from projektsog.config import Config
from tests._app_fakes import wait_until
from tests._resolve_fakes import (
    ChildFactory, FakeClock, FakeFolder, FakeIndexer, FakeProject, FakeResolve, FakeWinui,
    indexed_file, offline_clip)
from tests.test_app_server import ServerTestBase, setUpModule, tearDownModule  # noqa: F401

OLD = "H:\\2024 Disk Sølv\\Pixelbro Radio\\Klip"


class Resolve22Tests(ServerTestBase):
    def setUp(self) -> None:
        super().setUp()
        arkiv = os.path.join(self.root, "Arkiv")
        self.media = os.path.join(arkiv, "Pixelbro Radio", "Klip")
        os.makedirs(self.media)
        for name in ("a.mov", "b.mov"):
            with open(os.path.join(self.media, name), "wb"):
                pass
        self.project = FakeProject("Pixelbro Radio", FakeFolder("Master", [
            offline_clip(OLD + "\\a.mov", uid="u-a"), offline_clip(OLD + "\\b.mov", uid="u-b"),
            offline_clip(OLD + "\\c.mov", uid="u-c")]))
        self.project.add_job("old", "Complete", file="Gammel.mp4")
        self.final = os.path.join(self.root, "Pixelbro Radio", "Final")
        self.project.add_job("j1", "Ready", file="Pixelbro_v2.mp4", folder=self.final)
        self.resolve = FakeResolve(self.project)
        self.winui = FakeWinui()
        self.media_index = FakeIndexer()
        self.media_index.files = [indexed_file(os.path.join(self.media, n), sid=7, root=arkiv,
                                               project="Pixelbro Radio") for n in ("a.mov", "b.mov")]
        clock = FakeClock()
        cfg = Config(path=os.path.join(self.root, "resolve-config.json"))
        cfg.update({"resolve_follow": "off"})
        self.events = self.bus.subscribe()
        self.resolve_bridge = rb.ResolveBridge(
            cfg, self.bus, self.media_index, process_running=self.winui.process_running,
            process_uptime=self.winui.process_uptime, open_folder=self.winui.open_folder,
            explorer_window_for=self.winui.explorer_window_for,
            call_with_timeout=self.winui.call_with_timeout,
            spawn_child=ChildFactory(lambda: self.resolve, clock), clock=clock)
        self.server.bridge = self.resolve_bridge
        self.resolve_bridge.start()
        self.addCleanup(self.resolve_bridge.stop)
        self.assertTrue(wait_until(lambda: self.resolve_bridge.state()["updated"] is not None, 10.0),
                        self.resolve_bridge.state())

    def render_events(self) -> list[Any]:
        out = []
        while True:
            try:
                kind, data, _ts = self.events.get_nowait()
            except queue.Empty:
                return out
            if kind == "render":
                out.append(data)

    def test_render_state(self) -> None:
        self.assertEqual(self.req("GET", "/api/render").json(), rb._idle_render())
        # (the bridge's thread polls on its own too: the job changes before the render flag)
        self.project.set_status("j1", "Rendering", 40, EstimatedTimeRemainingInMs=90_000)
        self.project.rendering = True
        self.resolve_bridge.refresh(wait=True)            # the next poll sees the render
        running = self.req("GET", "/api/render").json()
        self.assertEqual({k: running[k] for k in ("aktiv", "pct", "eta_s", "navn", "projekt")},
                         {"aktiv": True, "pct": 40, "eta_s": 90, "navn": "Pixelbro_v2.mp4",
                          "projekt": "Pixelbro Radio"})
        self.project.set_status("j1", "Complete", 100)
        self.project.rendering = False
        self.resolve_bridge.refresh(wait=True)
        done = self.req("GET", "/api/render").json()
        self.assertEqual((done["aktiv"], done["faerdig"]), (False, {
            "udfald": "done", "fil": "Pixelbro_v2.mp4", "sti": os.path.join(self.final, "Pixelbro_v2.mp4"),
            "mappe": self.final, "levering": True, "fejl": None, "seq": 1}))
        events = self.render_events()                     # SSE "render": the same states
        self.assertEqual(events[0]["aktiv"], True)
        self.assertEqual(events[-1], done)
        self.assertEqual({(e["faerdig"] or {}).get("seq") for e in events}, {None, 1})
        self.assertEqual(self.media_index.refreshed, [self.final])

    def test_find_and_relink(self) -> None:
        plan = self.req("POST", "/api/resolve/offline", body={}).json()
        self.assertEqual((plan["project"], plan["scanned"], plan["blocked"]), ("Pixelbro Radio", 3, None))
        (group,) = plan["groups"]
        self.assertEqual((group["from"], group["to"], group["auto"], [c["uid"] for c in group["clips"]]),
                         (OLD, self.media, True, ["u-a", "u-b"]))
        self.assertEqual([c["uid"] for c in plan["not_found"]], ["u-c"])
        response = self.req("POST", "/api/resolve/relink", body={
            "uid": plan["uid"], "groups": [{"to": group["to"], "uids": ["u-a", "u-b"]}]})
        self.assertEqual((response.status, response.json()),
                         (200, {"relinked": 2, "still_offline": 1, "failed": [], "error": None}))
        self.assertEqual(self.project.pool.relinks, [(["u-a", "u-b"], self.media)])
        self.assertTrue(wait_until(lambda: self.resolve_bridge.state()["clip_count"] == 3, 5.0))

    def test_refusals(self) -> None:
        body = {"uid": "uid-Pixelbro Radio", "groups": [{"to": self.media, "uids": ["u-a"]}]}
        response = self.req("POST", "/api/resolve/relink", body=body)
        self.assertEqual((response.status, response.json()), (400, {"error": "Find de offline klip først"}))
        self.req("POST", "/api/resolve/offline", body={})
        for bad in ({"uid": "x"}, {"uid": "x", "groups": [{"to": "Klip", "uids": ["u-a"]}]},
                    {"uid": "x", "groups": [{"to": self.media, "uids": [3]}]}):
            with self.subTest(body=bad):
                response = self.req("POST", "/api/resolve/relink", body=bad)
                self.assertEqual((response.status, response.json()), (400, {"error": "Ugyldig forespørgsel"}))
        self.resolve_bridge.queue_holder = lambda: {"navn": "Mette"}
        response = self.req("POST", "/api/resolve/relink", body=body)
        self.assertEqual((response.status, response.json()["error"]),
                         (200, "Mette bygger i Resolve lige nu – genlink, når den er færdig"))
        self.assertEqual(self.req("POST", "/api/resolve/offline", body={}).json()["blocked"],
                         "Mette bygger i Resolve lige nu – genlink, når den er færdig")
        self.assertEqual(self.req("POST", "/api/resolve/relink", body=body, token=False).status, 403)
        self.assertEqual(self.project.pool.relinks, [])
        self.winui.running = False                         # Resolve quits
        self.resolve_bridge.refresh(wait=True)
        response = self.req("POST", "/api/resolve/offline", body={})
        self.assertEqual((response.status, response.json()), (400, {"error": "DaVinci Resolve kører ikke"}))


if __name__ == "__main__":
    import unittest

    unittest.main()
