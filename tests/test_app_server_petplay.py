"""Klippe's games over HTTP (/api/widget/…, SPEC §18.4)."""

import os
import tempfile
import unittest

from projektsog import petplay
from projektsog.config import Config
from tests.test_app_server import ServerTestBase, setUpModule, tearDownModule  # noqa: F401
from tests.test_petplay import FakeBus, FakeProbe, FakeSprites, FakeWidget

LOOK = {"stage": "baby", "outfit": "none", "pet": {"x": 25, "y": 40, "w": 210, "h": 210},
        "view": {"w": 260, "h": 448}}


class TrophyEndpointTests(ServerTestBase):
    def setUp(self) -> None:
        super().setUp()
        from projektsog import achievements
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        cfg = Config(path=os.path.join(self.dir.name, "config.json"))
        self.progress = achievements.PetProgress(cfg, FakeBus(), path=os.path.join(self.dir.name, "pet.json"))
        self.server.progress = self.progress

    def test_state_and_equip(self) -> None:
        state = self.req("GET", "/api/pet").json()
        self.assertEqual((state["unlocked"], state["total"]), (0, len(state["trophies"])))
        self.assertEqual(state["equipped"]["hat"], "ingen")
        answer = self.req("POST", "/api/pet/equip", body={"slot": "hat", "item": "ingen"}).json()
        self.assertEqual(answer["ok"], True)
        response = self.req("POST", "/api/pet/equip", body={"slot": "haand", "item": "awp"})
        self.assertEqual((response.status, response.json()), (400, {"error": "AWP er ikke låst op endnu"}))
        self.server.progress = None
        response = self.req("GET", "/api/pet")
        self.assertEqual((response.status, response.json()), (400, {"error": "Klippes trofæer er ikke startet"}))


class PetEndpointTests(ServerTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.pet_cfg = Config(path=os.path.join(self.dir.name, "config.json"))
        self.pet_cfg.update({"widget_enabled": True})
        self.bus = FakeBus()
        self.play = petplay.PetPlay(self.pet_cfg, self.bus, widget=FakeWidget(), probe=FakeProbe(),
                                    sprites=FakeSprites(), spawn=lambda *a: self.fail("no game in these tests"))
        self.server.petplay = self.play

    def test_status_look_and_play_now(self) -> None:
        self.assertEqual(self.req("GET", "/api/widget/play").json(),
                         {"state": "ready", "message": "", "enabled": True})
        self.assertEqual(self.req("POST", "/api/widget/look", body=LOOK).json(), {"ok": True})
        self.assertEqual(self.play._look["pet"], (25.0, 40.0, 210.0, 210.0))
        answer = self.req("POST", "/api/widget/play", body={}).json()
        self.assertEqual(answer["state"], "waiting")
        self.assertEqual(self.bus.events[-1][0], "pet")

    def test_bad_requests(self) -> None:
        for path, body, error in (
                ("/api/widget/look", {"stage": "dragon"}, "Ugyldig værdi: stage"),
                ("/api/widget/look", {**LOOK, "pet": {"x": 1}}, "Ugyldig værdi: y")):
            with self.subTest(body=body):
                response = self.req("POST", path, body=body)
                self.assertEqual((response.status, response.json()), (400, {"error": error}))
        self.pet_cfg.update({"widget_enabled": False})
        response = self.req("POST", "/api/widget/play", body={})
        self.assertEqual((response.status, response.json()), (400, {"error": "Slå Klippe til først"}))
        response = self.req("POST", "/api/widget/look", body=LOOK, token=False)
        self.assertEqual(response.status, 403)                    # only Projektsøg's own pages

    def test_without_klippe(self) -> None:
        self.server.petplay = None
        response = self.req("GET", "/api/widget/play")
        self.assertEqual((response.status, response.json()), (400, {"error": "Klippe er ikke startet"}))


if __name__ == "__main__":
    unittest.main()
