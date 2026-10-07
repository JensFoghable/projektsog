"""The robot crew over HTTP (/api/bygger, /api/bygger/demo, SPEC §21.2)."""

import os
import tempfile
import unittest

from projektsog import crew
from projektsog.config import Config
from tests.test_app_server import ServerTestBase, setUpModule, tearDownModule  # noqa: F401
from tests.test_crew import LOOK, FakeBoard, FakeBus, FakeProbe, FakeSprites, FakeWatch, FakeWidget

IDLE = {"aktiv": False, "navn": None, "projekt": None, "opgave": None, "siden": None, "demo": False,
        "ude": False, "retning": None, "faerdig": False, "varighed_s": None}


class BuildEndpointTests(ServerTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.crew_cfg = Config(path=os.path.join(self.dir.name, "config.json"))
        self.crew_cfg.update({"widget_enabled": True})
        self.bus = FakeBus()
        self.board = FakeBoard()
        self.watch = FakeWatch()
        self.crew = crew.Crew(self.crew_cfg, self.bus, widget=FakeWidget(), watch=self.watch, look=lambda: dict(LOOK),
                              messages=self.board, probe=FakeProbe(),
                              sprites=FakeSprites(os.path.join(self.dir.name, "robot.png")),
                              spawn=lambda *a: self.fail("no robots in these tests"), wall=lambda: 1_700_000_000.0)
        self.server.crew = self.crew

    def test_the_state(self) -> None:
        self.assertEqual(self.req("GET", "/api/bygger").json(), IDLE)
        self.watch.build = {"navn": "Mette", "projekt": "Rikke Lindholm", "opgave": "byg 3 klip",
                            "siden": 1_699_999_000.0}
        self.crew.step()
        self.assertEqual(self.req("GET", "/api/bygger").json(), {
            **IDLE, "aktiv": True, "navn": "Mette", "projekt": "Rikke Lindholm", "opgave": "byg 3 klip",
            "siden": 1_699_999_000.0})

    def test_a_demo_build(self) -> None:
        answer = self.req("POST", "/api/bygger/demo", body={"opkald": False}).json()
        self.assertEqual(answer, {**IDLE, "aktiv": True, "navn": "Demo", "projekt": "Robotterne øver sig",
                                  "opgave": "", "siden": 1_700_000_000.0, "demo": True})
        self.assertEqual(self.bus.events[-1], ("bygger", answer))
        self.assertEqual(self.req("POST", "/api/bygger/demo", body={}).json()["demo"], True)   # opkald: false

    def test_a_demo_call(self) -> None:
        answer = self.req("POST", "/api/bygger/demo", body={"opkald": True}).json()
        self.assertEqual(answer, IDLE)                       # it rings; "Byg nu" starts the demo
        (message, internal), = self.board.posts
        self.assertEqual((message["tag"], message["knapper"][0]["uri"], internal),
                         ("demo:opkald", "projektsog:demo", True))

    def test_bad_requests(self) -> None:
        response = self.req("POST", "/api/bygger/demo", body={"opkald": "ja"})
        self.assertEqual((response.status, response.json()), (400, {"error": "Ugyldig værdi: opkald"}))
        response = self.req("POST", "/api/bygger/demo", body={}, token=False)
        self.assertEqual(response.status, 403)               # only Projektsøg's own pages
        self.crew_cfg.update({"widget_enabled": False})
        for call in (False, True):
            with self.subTest(call=call):
                response = self.req("POST", "/api/bygger/demo", body={"opkald": call})
                self.assertEqual((response.status, response.json()), (400, {"error": "Slå Klippe til først"}))
        self.assertEqual(self.board.posts, [])
        self.crew_cfg.update({"widget_enabled": True})
        self.watch.build = {"navn": "Mette", "projekt": "P", "opgave": "", "siden": None}
        self.crew.step()
        response = self.req("POST", "/api/bygger/demo", body={"opkald": False})
        self.assertEqual((response.status, response.json()), (400, {"error": crew.MESSAGES["building"]}))

    def test_without_the_crew(self) -> None:
        self.server.crew = None                              # --no-window: no Klippe, no robots
        for method, path in (("GET", "/api/bygger"), ("POST", "/api/bygger/demo")):
            with self.subTest(path=path):
                response = self.req(method, path, body={} if method == "POST" else None)
                self.assertEqual((response.status, response.json()), (400, {"error": "Robotterne er ikke startet"}))


if __name__ == "__main__":
    unittest.main()
