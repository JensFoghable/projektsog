"""Office Klippes and the delivery party over HTTP (/api/kontor, /api/kontor/demo,
/api/levering/demo, /api/festkat – SPEC §22.3, §22.4), with fake sockets and a fake fetcher."""

import os
import queue
import tempfile
import unittest

from projektsog import achievements, kontor, levering
from projektsog.config import Config
from tests import _app_fakes as fakes
from tests.test_app_server import ServerTestBase, setUpModule, tearDownModule  # noqa: F401
from tests.test_kontor import LAN, FakeSocket, packet, raw
from tests.test_levering import GIF, FakeBridge

STATE_OFF = {"enabled": False, "peers": [], "grund": None}


class OfficeServerTestBase(ServerTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.office_cfg = Config(path=os.path.join(self.dir.name, "config.json"))
        self.office_cfg.update({"widget_enabled": True})
        self.events = self.bus.subscribe()
        self.sockets: list[FakeSocket] = []
        self.kontor = kontor.Kontor(self.office_cfg, self.bus, equipped=dict, stats=achievements.Stats,
                                    hostname=lambda: "TESTPC", hosts=list, sock_factory=self._socket,
                                    resolve_ips=lambda host, timeout: [], broadcasts=lambda: ["192.168.1.255"],
                                    data_dir=self.dir.name, wall=lambda: 1_700_000_000.0)
        self.fetched: list[str] = []
        self.fetch_result: object = GIF
        self.rings: list[bool] = []
        self.levering = levering.Levering(self.office_cfg, self.bus, bridge=FakeBridge(), shown=lambda: False,
                                          ring=self.rings.append, fetch=self._fetch, data_dir=self.dir.name,
                                          lister=lambda path: [], call_with_timeout=lambda k, fn, t: ("ok", fn()))
        self.server.kontor = self.kontor
        self.server.levering = self.levering

    def _socket(self) -> FakeSocket:
        self.sockets.append(FakeSocket())
        return self.sockets[-1]

    def _fetch(self, url: str, timeout: float, max_bytes: int) -> bytes:
        self.fetched.append(url)
        if isinstance(self.fetch_result, BaseException):
            raise self.fetch_result
        return self.fetch_result

    def published(self, kind: str) -> list:
        out = []
        while True:
            try:
                event, data, _ts = self.events.get_nowait()
            except queue.Empty:
                return out
            if event == kind:
                out.append(data)


class KontorEndpointTests(OfficeServerTestBase):
    def test_the_office(self) -> None:
        self.assertEqual(self.req("GET", "/api/kontor").json(), STATE_OFF)
        self.kontor.step()                                      # the socket opens (a fake one)
        self.kontor.handle_packet(raw(packet(stage="junior")), LAN)
        self.assertEqual(self.req("GET", "/api/kontor").json(), {
            "enabled": True, "peers": [{"pc": "STUDIE-PC", "navn": "Klippe", "stage": "junior",
                                        "sidst": 1_700_000_000.0}], "grund": None})

    def test_a_demo_visit(self) -> None:
        answer = self.req("POST", "/api/kontor/demo", body={}).json()
        self.assertTrue(answer["ok"])
        self.assertEqual(self.published("besoeg"), [answer["besoeg"]])
        self.assertEqual(answer["besoeg"]["pc"], kontor.DEMO_PC)
        response = self.req("POST", "/api/kontor/demo", body={}, token=False)
        self.assertEqual(response.status, 403)                  # only Projektsøg's own pages
        self.office_cfg.update({"widget_enabled": False})
        response = self.req("POST", "/api/kontor/demo", body={})
        self.assertEqual((response.status, response.json()), (400, {"error": "Slå Klippe til først"}))
        self.assertEqual(self.sockets, [])                      # nothing went out

    def test_without_the_office(self) -> None:
        self.server.kontor = None                               # --no-window
        for method, path in (("GET", "/api/kontor"), ("POST", "/api/kontor/demo")):
            with self.subTest(path=path):
                response = self.req(method, path, body={} if method == "POST" else None)
                self.assertEqual((response.status, response.json()),
                                 (400, {"error": "Kontor-Klipperne er ikke startet"}))


class LeveringEndpointTests(OfficeServerTestBase):
    def test_the_demo_party(self) -> None:
        self.assertEqual(self.req("POST", "/api/levering/demo", body={}).json(), {"ok": True})
        self.assertEqual(self.published("levering"), [dict(levering.DEMO)])
        self.assertEqual(self.req("POST", "/api/levering/demo", body={}, token=False).status, 403)
        self.office_cfg.update({"widget_enabled": False})
        response = self.req("POST", "/api/levering/demo", body={})
        self.assertEqual((response.status, response.json()), (400, {"error": "Slå Klippe til først"}))

    def test_the_cat(self) -> None:
        response = self.req("GET", "/api/festkat")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Content-Type"], "image/gif")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.body, GIF)
        self.assertEqual(self.req("GET", "/api/festkat").body, GIF)
        self.assertEqual(self.fetched, [levering.FESTKAT_URL])  # fetched once, then kept

    def test_no_cat(self) -> None:
        self.fetch_result = OSError("no internet")
        with self.assertLogs("projektsog.levering", "WARNING"):
            response = self.req("GET", "/api/festkat")
        self.assertEqual((response.status, response.json()), (400, {"error": "Festkatten kunne ikke hentes"}))
        self.assertEqual(self.req("GET", "/api/festkat").status, 400)
        self.assertEqual(len(self.fetched), 1)                  # not asked again for 10 minutes

    def test_without_the_party(self) -> None:
        self.server.levering = None
        for method, path in (("POST", "/api/levering/demo"), ("GET", "/api/festkat")):
            with self.subTest(path=path):
                response = self.req(method, path, body={} if method == "POST" else None)
                self.assertEqual((response.status, response.json()),
                                 (400, {"error": "Leveringsfesten er ikke startet"}))

    def test_fakes_match(self) -> None:
        # The lifecycle fakes answer like the real ones.
        self.server.kontor, self.server.levering = fakes.FakeKontor(), fakes.FakeLevering()
        self.assertEqual(self.req("GET", "/api/kontor").json(), STATE_OFF)
        self.assertEqual(self.req("POST", "/api/levering/demo", body={}).json(), {"ok": True})
        self.assertEqual(self.req("GET", "/api/festkat").status, 400)


if __name__ == "__main__":
    unittest.main()
