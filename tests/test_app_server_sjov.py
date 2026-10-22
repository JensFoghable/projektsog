"""The fun in the office over HTTP (SPEC §23.13) – the routes with fake collaborators, one real
office (fake sockets) through HTTP, and the app's tally of the bus (PetTally)."""

import os
import queue
import random
import tempfile
import threading
import unittest
from datetime import date

from projektsog import achievements, kontor
from projektsog.config import Config
from tests import _app_fakes as fakes
from tests.test_app_server import ServerTestBase, setUpModule, tearDownModule  # noqa: F401
from tests.test_kontor import FakeSocket, raw
from tests.test_kontor_sjov import FakeBoard, FakeProgress, packet

THIS_YEAR = date.today().year


class ProgressStandIn(FakeProgress):
    """PetProgress as the routes see it (SPEC §23.1); every call is recorded."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple] = []
        self.result = {"ok": True, "bedste": 2310, "ny_rekord": True, "kontor_rekord": False, "spil": {}}
        self.review = {"aar": THIS_YEAR, "timer": 311.5, "projekter": 23,
                       "sider": [{"side": "edit", "navn": "Edit", "timer": 180.2}], "mad": {"spist": {"durum": 31}},
                       "kort": {"antal": 38}, "leveringer": 19, "klippe": {"trofaeer": 21},
                       "top": [{"navn": "Rikke Lindholm - Testimonial", "timer": 61.2}]}

    def season_state(self) -> dict:
        self.calls.append(("season_state",))
        return {"saeson": None, "kostume": None, "kostume_aar": "vampyr", "ballade": None, "bytte": None}

    def calendar(self) -> dict:
        self.calls.append(("calendar",))
        return {"aar": THIS_YEAR, "aktiv": False, "dag": None, "uaabnede": 0, "laager": []}

    def open_door(self, nr: int) -> dict:
        self.calls.append(("open_door", nr))
        if nr == 13:
            raise ValueError("Låge 13 kan først åbnes den 13. december")
        return {"ok": True, "gave": {"id": "gevir"}, "jul": {}}

    def calendar_demo(self, nr: int) -> dict:
        self.calls.append(("calendar_demo", nr))
        return {"ok": True, "gave": {"id": "sne1"}, "demo": True}

    def game_state(self) -> dict:
        self.calls.append(("game_state",))
        return {"bedste": 2310, "spillet": 4, "kontor": []}

    def game_result(self, score: int, demo: bool) -> dict:
        self.calls.append(("game_result", score, demo))
        return dict(self.result)

    def year_review(self, year: int) -> dict:
        self.calls.append(("year_review", year))
        return dict(self.review, aar=year)


class FunRouteTests(ServerTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.progress = ProgressStandIn()
        self.server.progress = self.progress
        self.kontor = fakes.FakeKontor()
        self.server.kontor = self.kontor
        self.rod = fakes.FakeRod()
        self.server.rod = self.rod
        self.sne = fakes.FakeSne()
        self.server.sne = self.sne
        self.rendering = [False]
        self.server.render_recent = lambda: self.rendering[0]
        self.opened: list[str] = []
        self.server.open_url = self.opened.append
        self.events = self.bus.subscribe()

    def published(self, kind: str) -> list:
        out = []
        while True:
            try:
                event, data, _ts = self.events.get_nowait()
            except queue.Empty:
                return out
            if event == kind:
                out.append(data)

    def ok(self, method: str, path: str, body=None):
        response = self.req(method, path, body=body if body is not None or method == "GET" else {})
        self.assertEqual(response.status, 200, response.body)
        return response.json()

    def refused(self, method: str, path: str, body=None) -> str:
        response = self.req(method, path, body=body if body is not None or method == "GET" else {})
        self.assertEqual(response.status, 400, response.body)
        return response.json()["error"]

    # -- Klippe's season, calendar and game (§23.1, §23.10) ------------------------------------
    def test_season_and_calendar(self) -> None:
        self.assertEqual(self.ok("GET", "/api/pet/saeson")["kostume_aar"], "vampyr")
        self.assertEqual(self.ok("GET", "/api/pet/jul")["laager"], [])
        self.assertEqual(self.ok("POST", "/api/pet/jul", {"nr": 5})["gave"], {"id": "gevir"})
        self.assertEqual(self.refused("POST", "/api/pet/jul", {"nr": 13}), "Låge 13 kan først åbnes den 13. december")
        self.assertTrue(self.ok("POST", "/api/pet/jul/demo", {"nr": 1})["demo"])
        for bad in ({}, {"nr": "5"}, {"nr": True}, {"nr": 0}, {"nr": 25}, {"nr": 5.0}):
            with self.subTest(bad=bad):
                self.assertEqual(self.refused("POST", "/api/pet/jul", bad), "Ugyldig værdi: nr")
                self.assertEqual(self.refused("POST", "/api/pet/jul/demo", bad), "Ugyldig værdi: nr")
        self.assertEqual([c for c in self.progress.calls if c[0] in ("open_door", "calendar_demo")],
                         [("open_door", 5), ("open_door", 13), ("calendar_demo", 1)])

    def test_the_render_game_counts_only_while_resolve_renders(self) -> None:
        self.assertEqual(self.ok("GET", "/api/pet/spil")["bedste"], 2310)
        self.assertEqual(self.refused("POST", "/api/pet/spil", {"score": 1200}),
                         "Render-spillet kan kun spilles, mens Resolve renderer")
        self.assertEqual(self.ok("POST", "/api/pet/spil", {"score": 1200, "demo": True})["bedste"], 2310)
        self.assertEqual(self.kontor.called("record"), [])          # a demo round is never told
        self.rendering[0] = True
        self.assertTrue(self.ok("POST", "/api/pet/spil", {"score": 2400})["ny_rekord"])
        self.assertEqual(self.kontor.called("record"), [((2400,), {})])   # a new best: the office hears it
        self.progress.result["ny_rekord"] = False
        self.ok("POST", "/api/pet/spil", {"score": 300})
        self.assertEqual(len(self.kontor.called("record")), 1)
        self.assertEqual([c for c in self.progress.calls if c[0] == "game_result"],
                         [("game_result", 1200, True), ("game_result", 2400, False), ("game_result", 300, False)])
        for bad in ({}, {"score": -1}, {"score": 10 ** 8}, {"score": "100"}, {"score": 1.5}, {"score": True}):
            with self.subTest(bad=bad):
                self.assertEqual(self.refused("POST", "/api/pet/spil", bad), "Ugyldig værdi: score")
        self.assertEqual(self.refused("POST", "/api/pet/spil", {"score": 5, "demo": "ja"}), "demo skal være sand/falsk")

    def test_without_the_apps_tally_the_bridge_is_asked(self) -> None:
        self.server.render_recent = None
        self.assertEqual(self.refused("POST", "/api/pet/spil", {"score": 5}),
                         "Render-spillet kan kun spilles, mens Resolve renderer")   # FakeBridge: no renders
        self.bridge.render_state = lambda: {"aktiv": True}
        self.ok("POST", "/api/pet/spil", {"score": 5})

    def test_a_record_the_office_cannot_hear_is_still_kept(self) -> None:
        self.rendering[0] = True
        self.kontor.raises["record"] = OSError("no network")
        self.assertTrue(self.ok("POST", "/api/pet/spil", {"score": 2400})["ny_rekord"])
        self.server.kontor = None
        self.assertTrue(self.ok("POST", "/api/pet/spil", {"score": 2500})["ny_rekord"])

    # -- snow and Klippe's bath (§23.5, §23.6) ---------------------------------------------------
    def test_snow(self) -> None:
        self.assertEqual(self.ok("POST", "/api/sne/demo"), {"ok": True})
        self.assertEqual(self.sne.called("start"), [((20,), {"demo": True})])
        self.server.sne = None
        self.assertEqual(self.refused("POST", "/api/sne/demo"), "Sneen er ikke startet")

    def test_klippes_bath(self) -> None:
        self.assertEqual(self.ok("GET", "/api/rod")["niveau"], "ren")
        self.assertEqual(self.refused("POST", "/api/rod/aabn"), "Der er ingen projektmappe at åbne")
        self.rod.returns["state"] = {**fakes.FakeRod.ROD_IDLE, "aktiv": True, "antal": 7, "niveau": "beskidt",
                                     "mappe": "C:\\Kunder 2026 (STUDIO)\\Rikke Lindholm"}
        self.controller.returns["open_path"] = {"ok": True, "path": "C:\\Kunder 2026 (STUDIO)\\Rikke Lindholm"}
        self.assertEqual(self.ok("POST", "/api/rod/aabn")["ok"], True)
        self.assertEqual(self.controller.called("open_path"),
                         [(("C:\\Kunder 2026 (STUDIO)\\Rikke Lindholm", "folder"), {})])
        self.assertEqual(self.ok("POST", "/api/rod/demo"), {"ok": True})
        self.server.rod = None
        for method, path in (("GET", "/api/rod"), ("POST", "/api/rod/aabn"), ("POST", "/api/rod/demo")):
            with self.subTest(path=path):
                self.assertEqual(self.refused(method, path), "Klippes bad er ikke startet")

    # -- the office (§23.7) ------------------------------------------------------------------------
    def test_the_office_routes(self) -> None:
        self.ok("POST", "/api/kontor/frokost")
        self.ok("POST", "/api/kontor/frokost/demo")
        self.ok("POST", "/api/kontor/post", {"til": "STUDIE-PC", "tekst": "Kaffe? ☕"})
        self.ok("POST", "/api/kontor/post/demo")
        self.ok("POST", "/api/kontor/slik", {"id": "STUDIE-PC:7", "svar": "slik"})
        self.ok("POST", "/api/kontor/slik/demo")
        self.assertEqual(self.ok("GET", "/api/kontor/vaeddemaal"), {"vaeddemaal": []})
        self.ok("POST", "/api/kontor/gaet", {"pc": "STUDIE-PC", "vid": 4, "tid": 1_900_000_000})
        self.ok("POST", "/api/kontor/vaeddemaal/demo")
        self.assertEqual([name for name, _args, _kwargs in self.kontor.calls],
                         ["lunch", "lunch_demo", "send_post", "post_demo", "treat_or_trick", "slik_demo", "bets",
                          "guess", "bet_demo"])
        self.assertEqual(self.kontor.called("send_post"), [(("STUDIE-PC", "Kaffe? ☕"), {})])
        self.assertEqual(self.kontor.called("treat_or_trick"), [(("STUDIE-PC:7", "slik"), {})])
        self.assertEqual(self.kontor.called("guess"), [(("STUDIE-PC", 4, 1_900_000_000), {})])
        self.server.kontor = None
        for method, path in (("POST", "/api/kontor/frokost"), ("POST", "/api/kontor/post"),
                             ("POST", "/api/kontor/slik"), ("GET", "/api/kontor/vaeddemaal"),
                             ("POST", "/api/kontor/gaet"), ("POST", "/api/aar/del")):
            with self.subTest(path=path):
                self.assertEqual(self.refused(method, path), "Kontor-Klipperne er ikke startet")

    def test_only_projektsogs_own_pages(self) -> None:
        for path in ("/api/kontor/frokost", "/api/paaskeaeg", "/api/aar/aabn", "/api/pet/spil"):
            with self.subTest(path=path):
                self.assertEqual(self.req("POST", path, body={}, token=False).status, 403)
        self.assertEqual(self.kontor.calls, [])
        self.assertEqual(self.opened, [])

    # -- memories and easter eggs (§23.8, §23.9) ------------------------------------------------
    def test_memories(self) -> None:
        self.assertEqual(self.ok("GET", "/api/minder"), {"dato": date.today().isoformat(), "minder": []})
        memory = {"name": "Rikke Lindholm", "minde": {"aar_siden": 3, "antal": 112, "slags": "klip"}}
        self.indexer.memories = lambda: [memory]
        self.assertEqual(self.ok("GET", "/api/minder")["minder"], [memory])
        self.cfg.update({"search_minder": False})
        self.assertEqual(self.ok("GET", "/api/minder")["minder"], [])

    def test_easter_eggs(self) -> None:
        self.assertEqual(self.ok("POST", "/api/paaskeaeg", {"ord": "kaffe"}), {"ok": True})
        self.assertEqual(self.ok("POST", "/api/paaskeaeg", {"ord": "kaffe"}), {"ok": True, "ignoreret": True})
        self.assertEqual(self.ok("POST", "/api/paaskeaeg", {"ord": "fredag"}), {"ok": True})
        self.assertEqual(self.published("paaskeaeg"), [{"ord": "kaffe"}, {"ord": "fredag"}])
        for bad in ({}, {"ord": "KAFFE"}, {"ord": "<script>"}, {"ord": ["kaffe"]}):
            with self.subTest(bad=bad):
                self.assertEqual(self.refused("POST", "/api/paaskeaeg", bad), "Ukendt påskeæg")
        self.server._eggs["kaffe"] -= 3.0                        # three seconds later
        self.assertEqual(self.ok("POST", "/api/paaskeaeg", {"ord": "kaffe"}), {"ok": True})
        self.cfg.update({"search_paaskeaeg": False})
        self.assertEqual(self.refused("POST", "/api/paaskeaeg", {"ord": "sne"}), "Påskeæggene er slået fra")
        self.assertEqual(self.published("paaskeaeg"), [{"ord": "kaffe"}])

    # -- Klippet (§23.11) -----------------------------------------------------------------------
    def test_the_year(self) -> None:
        self.assertEqual(self.ok("GET", "/api/aar")["aar"], THIS_YEAR)
        self.assertEqual(self.ok("GET", f"/api/aar?aar={THIS_YEAR}")["aar"], THIS_YEAR)
        for bad in ("2025", str(THIS_YEAR + 1), "abc", "-1", "2026.5"):
            with self.subTest(bad=bad):
                self.assertEqual(self.refused("GET", f"/api/aar?aar={bad}"), "Det år findes ikke i Klippet")
        answer = self.ok("POST", "/api/aar/aabn", {})
        url = f"http://127.0.0.1:{self.port}/aar.html?aar={THIS_YEAR}"
        self.assertEqual((answer, self.opened), ({"ok": True, "url": url}, [url]))
        self.assertEqual(self.refused("POST", "/api/aar/aabn", {"aar": 1999}), "Det år findes ikke i Klippet")

        def broken(url: str) -> None:
            raise OSError("no browser")
        self.server.open_url = broken
        self.assertEqual(self.refused("POST", "/api/aar/aabn", {"aar": THIS_YEAR}), "Klippet kunne ikke åbnes")

    def test_sharing_the_year_sends_numbers_only(self) -> None:
        self.ok("POST", "/api/aar/del", {"aar": THIS_YEAR})
        (summary,), _kwargs = self.kontor.called("share_year")[0]
        self.assertEqual(summary, {"aar": THIS_YEAR, "timer": 312, "side": "edit", "projekter": 23, "durum": 31,
                                   "kort": 38, "leveringer": 19, "trofaeer": 21})
        self.assertNotIn("Rikke", repr(summary))

    def test_without_klippes_progress(self) -> None:
        self.server.progress = None
        for method, path, body in (("GET", "/api/pet/saeson", None), ("GET", "/api/pet/jul", None),
                                   ("POST", "/api/pet/jul", {"nr": 1}), ("GET", "/api/pet/spil", None),
                                   ("POST", "/api/pet/spil", {"score": 1, "demo": True}), ("GET", "/api/aar", None)):
            with self.subTest(path=path):
                self.assertEqual(self.refused(method, path, body), "Klippes trofæer er ikke startet")


class RealOfficeOverHttpTests(ServerTestBase):
    """One real Kontor (a fake socket) behind the routes: the Danish answers reach the page."""

    def setUp(self) -> None:
        super().setUp()
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.office_cfg = Config(path=os.path.join(self.dir.name, "config.json"))
        self.office_cfg.update({"widget_enabled": True})
        self.events = self.bus.subscribe()
        self.sockets: list[FakeSocket] = []
        self.board = FakeBoard()
        self.kontor = kontor.Kontor(self.office_cfg, self.bus, equipped=dict, stats=achievements.Stats,
                                    hostname=lambda: "TESTPC", hosts=list, sock_factory=self._socket,
                                    resolve_ips=lambda host, timeout: [], broadcasts=lambda: ["192.168.1.255"],
                                    progress=FakeProgress(), board=self.board, shown=lambda: False,
                                    data_dir=self.dir.name, rng=random.Random(1))
        self.server.kontor = self.kontor

    def _socket(self) -> FakeSocket:
        self.sockets.append(FakeSocket())
        return self.sockets[-1]

    def test_lunch_and_post_through_http(self) -> None:
        response = self.req("POST", "/api/kontor/frokost", body={})
        self.assertEqual((response.status, response.json()["error"]),
                         (400, "Kontor-Klipperne er ikke klar endnu – prøv igen om lidt"))
        self.kontor.step()                                         # the socket opens
        self.assertEqual(self.req("POST", "/api/kontor/frokost", body={}).json(), {"ok": True})
        self.assertEqual(self.req("POST", "/api/kontor/frokost", body={}).json()["error"], "Du har lige ringet – vent lidt 🔔")
        self.kontor.handle_packet(raw(packet("hej")), ("192.168.1.50", kontor.PORT))
        self.assertEqual(self.req("POST", "/api/kontor/post", body={"til": "STUDIE-PC", "tekst": "Hej! 👋"}).json(),
                         {"ok": True})
        response = self.req("POST", "/api/kontor/post", body={"til": "STUDIE-PC", "tekst": "Hej igen"})
        self.assertEqual((response.status, response.json()["error"]), (400, "Klippe er stadig ude med posten – vent lidt ✉️"))
        response = self.req("POST", "/api/kontor/post", body={"til": "GRAFIK-PC", "tekst": "Hej"})
        self.assertEqual((response.status, response.json()["error"]), (400, "GRAFIK-PC er ikke på lige nu"))
        self.sockets[-1].sent.clear()
        self.kontor.step()
        types = sorted(raw_data.decode("utf-8").split('"type":"')[1].split('"')[0] for raw_data, _a in self.sockets[-1].sent)
        self.assertEqual(types, ["frokost", "post"])    # lunch to the broadcast (no hosts here), the post to one PC

    def test_the_demo_bet_through_http(self) -> None:
        view = self.req("POST", "/api/kontor/vaeddemaal/demo", body={}).json()["vaeddemaal"]
        listed = self.req("GET", "/api/kontor/vaeddemaal").json()["vaeddemaal"]
        self.assertEqual([b["vid"] for b in listed], [view["vid"]])
        response = self.req("POST", "/api/kontor/gaet", body={"pc": "DEMO-PC", "vid": view["vid"], "tid": 5})
        self.assertEqual((response.status, response.json()["error"]), (400, "Gæt et tidspunkt efter nu"))
        answer = self.req("POST", "/api/kontor/gaet", body={"pc": "DEMO-PC", "vid": view["vid"],
                                                              "tid": view["lukker"] + 60}).json()
        self.assertEqual(answer["vaeddemaal"]["mit_gaet"], view["lukker"] + 60)


class TallyTests(unittest.TestCase):
    """PetTally (app.py): what Klippe's year counts from the bus, and the render game's gate."""

    def setUp(self) -> None:
        self.app = fakes.import_app()
        self.notes: list[str] = []
        self.progress = type("P", (), {"note": lambda _self, name: self.notes.append(name)})()
        self.now = [100.0]
        self.bus = fakes.FakeIndexer()                            # (only needs to exist for handle())

    def tally(self, state=None):
        return self.app.PetTally(self.bus, self.progress, render_state=(lambda: state) if state is not None else None,
                                 clock=lambda: self.now[0])

    def test_deliveries_and_done_renders(self) -> None:
        t = self.tally()
        t.handle("levering", {"kilde": "fil", "demo": False})
        t.handle("levering", {"kilde": "render", "demo": True})    # a demo party counts for nothing
        t.handle("render", {"aktiv": True, "faerdig": None})
        t.handle("render", {"aktiv": False, "faerdig": {"udfald": "done", "seq": 1}})
        t.handle("render", {"aktiv": False, "faerdig": {"udfald": "done", "seq": 1}})   # the same end again
        t.handle("render", {"aktiv": False, "faerdig": {"udfald": "failed", "seq": 2}})
        t.handle("render", {"aktiv": True, "faerdig": {"udfald": "done", "seq": 3}})
        t.handle("render", None)
        self.assertEqual(self.notes, ["leveringer", "renders", "renders"])

    def test_an_end_from_before_the_start_is_no_news(self) -> None:
        t = self.tally({"aktiv": False, "faerdig": {"udfald": "done", "seq": 7}})
        t.handle("render", {"aktiv": False, "faerdig": {"udfald": "done", "seq": 7}})
        self.assertEqual(self.notes, [])
        self.assertFalse(t.render_recent())

    def test_a_render_lately(self) -> None:
        t = self.tally()
        self.assertFalse(t.render_recent())
        t.handle("render", {"aktiv": True, "faerdig": None})
        self.now[0] += 3600
        self.assertTrue(t.render_recent())                        # still rendering (no news for an hour)
        t.handle("render", {"aktiv": False, "faerdig": None})
        self.now[0] += 299
        self.assertTrue(t.render_recent())                        # … and for five minutes after it
        self.now[0] += 2
        self.assertFalse(t.render_recent())
        t.handle("render", {"aktiv": False, "faerdig": {"udfald": "done", "seq": 1}})
        self.assertTrue(t.render_recent())                        # its end is "just now"
        self.assertTrue(self.tally({"aktiv": True, "faerdig": None}).render_recent())

    def test_the_thread(self) -> None:
        from projektsog.events import EventBus
        bus = EventBus()
        t = self.app.PetTally(bus, self.progress)
        t.start()
        self.addCleanup(t.close)
        bus.publish("levering", {"kilde": "fil"})
        self.assertTrue(fakes.wait_until(lambda: self.notes == ["leveringer"]))
        t.close()
        self.assertTrue(fakes.wait_until(lambda: not t._thread.is_alive()))
        bus.publish("levering", {"kilde": "fil"})
        self.assertEqual(self.notes, ["leveringer"])


if __name__ == "__main__":
    unittest.main()
