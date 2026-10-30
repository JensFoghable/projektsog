"""The fun in the office (SPEC §23) for the fake backend (``tests/_ui_mock_server.py``).

Canned state for the new routes, kept in memory and steered by tests:

* ``POST /api/_mock/set {"key", "data"}`` replaces one piece of state – ``saeson`` (GET
  /api/pet/saeson, SSE ``pet_saeson``), ``jul`` (GET /api/pet/jul, SSE ``pet_jul``), ``spil`` (per game: GET
  /api/pet/spil?spil=…), ``rod`` (GET /api/rod, SSE ``rod``), ``vaeddemaal`` (the list of GET
  /api/kontor/vaeddemaal), ``minder`` (GET /api/minder) or ``aar`` (GET /api/aar) – and publishes
  the matching event where there is one.
* ``POST /api/_mock/publish`` of ``pet_saeson``, ``pet_jul``, ``rod`` or ``vaeddemaal`` updates the
  state too (like the real components would), so a reload sees what the event said.

The POST routes answer like the real ones (Danish 400s for the usual mistakes) and publish the
events the real components publish; demos run on short timers. Nothing touches the disk.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import date
from typing import Any

EGGS = ("kaffe", "fredag", "deadline", "klippe", "durum", "booster", "festkat", "hjaelp", "fyraften", "sne")
KOSTUMER = ("vampyr", "spoegelse", "skelet", "heks")
DEMO_PC = "DEMO-PC"

GAMES = ("hop", "skyd", "kryds")   # the render game's games
GET_ROUTES = ("/api/pet/saeson", "/api/pet/jul", "/api/pet/spil", "/api/rod", "/api/kontor/vaeddemaal",
              "/api/minder", "/api/aar")
POST_ROUTES = ("/api/pet/jul", "/api/pet/jul/demo", "/api/pet/spil", "/api/sne/demo", "/api/rod/aabn",
               "/api/rod/demo", "/api/kontor/frokost", "/api/kontor/frokost/demo", "/api/kontor/post",
               "/api/kontor/post/demo", "/api/kontor/slik", "/api/kontor/slik/demo", "/api/kontor/gaet",
               "/api/kontor/vaeddemaal/demo", "/api/paaskeaeg", "/api/aar/aabn", "/api/aar/del")
EVENT_OF = {"saeson": "pet_saeson", "jul": "pet_jul", "rod": "rod"}

# A few gifts like achievements.JULEKALENDER (the real ones are shuffled per PC and year).
GIFTS = [
    {"id": "sne1", "kind": "sne", "name": "Første sne", "tekst": "Første sne! ❄️", "sne_s": 45},
    {"id": "gevir", "kind": "ting", "name": "Rensdyrgevir", "tekst": "Et rensdyrgevir til Klippe!", "item": "gevir"},
    {"id": "aebleskiver", "kind": "mad", "name": "Æbleskiver", "tekst": "Æbleskiver med syltetøj og flormelis",
     "points": 25},
    {"id": "vits1", "kind": "vits", "name": "Julevits",
     "tekst": "Hvorfor kan nissen ikke klippe film? Han er altid oppe på loftet! 🎅"},
]

ROD_IDLE: dict[str, Any] = {"aktiv": False, "projekt": None, "mappe": None, "antal": 0, "niveau": "ren",
                            "filer": [], "mere": 0, "bad": False}
ROD_DEMO: dict[str, Any] = {
    "aktiv": True, "projekt": "Demo", "mappe": "C:\\Kunder 2026 (STUDIO)\\Demo", "antal": 12, "niveau": "meget",
    "filer": [{"navn": f"Klip_{n:02d}.mov", "forslag": "Klip", "ny": False, "slags": "video"} for n in range(1, 11)]
    + [{"navn": "Speak_final.wav", "forslag": "Speak", "ny": False, "slags": "lyd"},
       {"navn": "Logo.png", "forslag": "Logo", "ny": True, "slags": "billede"}],
    "mere": 0, "bad": False}


def _jul_idle(today: date) -> dict[str, Any]:
    return {"aar": today.year, "aktiv": today.month == 12, "dag": today.day if today.month == 12 else None,
            "uaabnede": 0, "laager": [{"nr": n, "aaben": False, "kan": False, "gave": None} for n in range(1, 25)]}


def _review(year: int) -> dict[str, Any]:
    return {
        "aar": year, "fra": f"{year}-01-01", "til": f"{year}-12-31", "siden": f"{year}-09-30", "dage": 52,
        "timer": 311.5, "projekter": 23,
        "top": [{"navn": "Rikke Lindholm - Testimonial", "timer": 61.2}, {"navn": "Klar Tand - Silkeborg", "timer": 40.5},
                {"navn": "Forår 2026", "timer": 22.0}],
        "sider": [{"side": "edit", "navn": "Edit", "timer": 180.2}, {"side": "color", "navn": "Color", "timer": 60.1},
                  {"side": "fusion", "navn": "Fusion", "timer": 21.0}, {"side": "musik", "navn": "Musik og lyd",
                                                                         "timer": 12.5}],
        "bedste_dag": {"dato": f"{year}-10-08", "timer": 8.1}, "ugedag": "tirsdag", "tidligst": "07:42",
        "fokus_90": 41, "pauser": 17, "kort": {"antal": 38, "tb": 4.2, "filer": 5120, "kamera": "FX9"},
        "leveringer": 19, "renders": 64,
        "mad": {"i_alt": 88, "spist": {"durum": 31, "booster": 20}, "yndlings": {"id": "durum", "navn": "Durum",
                                                                                "antal": 31}},
        "klippe": {"navn": "Klippe", "stage": "junior", "level": 25, "trofaeer": 21, "fund": 1},
        "kontor": {"frokost_ringet": 9, "post_sendt": 14, "post_faaet": 11, "slik_givet": 3, "vaeddemaal_vundet": 2,
                   "spil_bedste": 1480, "sne": 3}}


class MockSjov:
    """The state behind the §23 routes of one MockBackend."""

    def __init__(self, backend: Any) -> None:
        self.backend = backend
        today = date.today()
        self.state: dict[str, Any] = {
            "saeson": {"saeson": None, "kostume": None, "kostume_aar": "vampyr", "ballade": None, "bytte": None},
            "jul": _jul_idle(today),
            "spil": {game: {"spil": game, "bedste": 0, "spillet": 0, "kontor": []} for game in GAMES},
            "rod": dict(ROD_IDLE),
            "vaeddemaal": [],
            "minder": {"dato": today.isoformat(), "minder": []},
            "aar": None,
        }
        self._eggs: dict[str, float] = {}
        self._seq = 0

    # -- helpers -----------------------------------------------------------------------------
    @property
    def bus(self) -> Any:
        return self.backend.bus

    def _settings(self, key: str) -> Any:
        with self.backend.lock:
            return self.backend.settings.get(key)

    def _klippe_on(self) -> None:
        if not self._settings("widget_enabled"):
            raise ValueError("Slå Klippe til først")

    def _later(self, seconds: float, fn: Any) -> None:
        timer = threading.Timer(seconds, fn)
        timer.daemon = True
        timer.start()

    def _copy(self, value: Any) -> Any:
        return json.loads(json.dumps(value))

    # -- test control ------------------------------------------------------------------------
    def set(self, key: str, data: Any) -> None:
        if key not in self.state:
            raise ValueError(f"Ukendt nøgle: {key}")
        with self.backend.lock:
            self.state[key] = self._copy(data)
        if key in EVENT_OF and data is not None:
            self.bus.publish(EVENT_OF[key], self._copy(data))

    def on_publish(self, kind: str, data: Any) -> None:
        """A test published ``kind``: keep what it says (the real component would hold it)."""
        if not isinstance(data, dict):
            return
        with self.backend.lock:
            if kind == "pet_saeson":
                self.state["saeson"] = self._copy(data)
            elif kind == "pet_jul":
                self.state["jul"] = self._copy(data)
            elif kind == "rod":
                self.state["rod"] = {**self._copy(data), "bad": False}
            elif kind == "vaeddemaal":
                bets = [b for b in self.state["vaeddemaal"] if (b.get("pc"), b.get("vid")) != (data.get("pc"), data.get("vid"))]
                self.state["vaeddemaal"] = [*bets, self._copy(data)]

    # -- GET -----------------------------------------------------------------------------------
    def get(self, route: str, query: dict[str, str]) -> Any:
        with self.backend.lock:
            if route == "/api/pet/saeson":
                return self._copy(self.state["saeson"])
            if route == "/api/pet/jul":
                return self._copy(self.state["jul"])
            if route == "/api/pet/spil":
                game = query.get("spil", "hop")
                if game not in GAMES:
                    raise ValueError("Ugyldig værdi: spil")
                return self._copy(self.state["spil"][game])
            if route == "/api/rod":
                return self._copy(self.state["rod"])
            if route == "/api/kontor/vaeddemaal":
                return {"vaeddemaal": self._copy(self.state["vaeddemaal"])}
            if route == "/api/minder":
                if not self.backend.settings.get("search_minder", True):
                    return {"dato": date.today().isoformat(), "minder": []}
                return self._copy(self.state["minder"])
            if route == "/api/aar":
                year = int(query.get("aar") or date.today().year)
                if not 2026 <= year <= date.today().year:
                    raise ValueError("Det år findes ikke i Klippet")
                if self.state["aar"] == "fejl":       # test control: the review cannot be made
                    raise ValueError("Klippet kunne ikke regnes ud lige nu")
                return self._copy(self.state["aar"]) if self.state["aar"] else _review(year)
        raise ValueError("Findes ikke")

    # -- POST ----------------------------------------------------------------------------------
    def post(self, route: str, body: dict[str, Any]) -> Any:
        handler = getattr(self, "_post_" + route[len("/api/"):].replace("/", "_"))
        return handler(body)

    def _post_pet_jul(self, body: dict[str, Any]) -> Any:
        nr = body.get("nr")
        with self.backend.lock:
            jul = self.state["jul"]
            if not jul.get("aktiv"):
                raise ValueError("Julekalenderen er kun åben i december")
            door = next((d for d in jul["laager"] if d["nr"] == nr), None)
            if door is None:
                raise ValueError("Ugyldig værdi: nr")
            if door["aaben"]:
                raise ValueError("Lågen er allerede åbnet")
            if not door["kan"]:
                raise ValueError(f"Låge {nr} kan først åbnes den {nr}. december")
            gift = self._copy(GIFTS[(nr - 1) % len(GIFTS)])
            door.update(aaben=True, kan=False, gave=gift)
            jul["uaabnede"] = sum(1 for d in jul["laager"] if d["kan"])
            snapshot = self._copy(jul)
        self.bus.publish("pet_jul", snapshot)
        if gift["kind"] == "sne":
            self.bus.publish("sne", {"sekunder": gift.get("sne_s", 45), "demo": False})
        return {"ok": True, "gave": gift, "jul": snapshot}

    def _post_pet_jul_demo(self, body: dict[str, Any]) -> Any:
        nr = body.get("nr")
        if type(nr) is not int or not 1 <= nr <= 24:
            raise ValueError("Ugyldig værdi: nr")
        gift = self._copy(GIFTS[(nr - 1) % len(GIFTS)])
        if gift["kind"] == "sne":
            self.bus.publish("sne", {"sekunder": 20, "demo": True})
        return {"ok": True, "gave": gift, "demo": True}

    def _post_pet_spil(self, body: dict[str, Any]) -> Any:
        score, demo, game = body.get("score"), bool(body.get("demo")), body.get("spil", "hop")
        if type(score) is not int or not 0 <= score <= 10_000_000:
            raise ValueError("Ugyldig værdi: score")
        if game not in GAMES:
            raise ValueError("Ugyldig værdi: spil")
        with self.backend.lock:
            spil = self.state["spil"][game]
            record = not demo and score > spil["bedste"] and score >= 100
            if not demo:
                spil["spillet"] += 1
                spil["bedste"] = max(spil["bedste"], score)
            snapshot = self._copy(spil)
        return {"ok": True, "bedste": snapshot["bedste"], "ny_rekord": record, "kontor_rekord": False,
                "spil": snapshot}

    def _post_sne_demo(self, body: dict[str, Any]) -> Any:
        self._klippe_on()
        self.bus.publish("sne", {"sekunder": 20, "demo": True})
        return {"ok": True}

    def _post_rod_aabn(self, body: dict[str, Any]) -> Any:
        return {"ok": True}

    def _post_rod_demo(self, body: dict[str, Any]) -> Any:
        self._klippe_on()
        self.on_publish("rod", ROD_DEMO)
        self.bus.publish("rod", self._copy(ROD_DEMO))

        def bath() -> None:
            clean = {**ROD_DEMO, "antal": 0, "niveau": "ren", "filer": [], "bad": True}
            with self.backend.lock:
                self.state["rod"] = {**clean, "bad": False}
            self.bus.publish("rod", clean)
        self._later(8.0, bath)
        return {"ok": True}

    def _post_kontor_frokost(self, body: dict[str, Any]) -> Any:
        self._klippe_on()
        if not self._settings("widget_frokost"):
            raise ValueError("Frokostklokken er slået fra")
        self.bus.publish("frokost", {"pc": "STUDIO-PC", "navn": self._settings("widget_pet_name") or "Klippe",
                                     "selv": True})
        return {"ok": True}

    def _post_kontor_frokost_demo(self, body: dict[str, Any]) -> Any:
        self._klippe_on()
        self.bus.publish("frokost", {"pc": DEMO_PC, "navn": "Klippe", "selv": False, "demo": True})
        return {"ok": True}

    def _post_kontor_post(self, body: dict[str, Any]) -> Any:
        self._klippe_on()
        if not self._settings("widget_post"):
            raise ValueError("Klippe-post er slået fra")
        til, tekst = body.get("til"), body.get("tekst")
        if not isinstance(tekst, str) or not tekst.strip():
            raise ValueError("Skriv en besked")
        if len(tekst.strip()) > 120:
            raise ValueError("Beskeden er for lang (højst 120 tegn)")
        peers = {p["pc"]: p for p in self.backend.kontor_state().get("peers", [])}
        if til not in peers:
            raise ValueError(f"{til} er ikke på lige nu")
        self.bus.publish("post_sendt", {"til": til, "navn_til": peers[til]["navn"]})
        return {"ok": True}

    def _post_kontor_post_demo(self, body: dict[str, Any]) -> Any:
        self._klippe_on()
        self._seq += 1
        self.bus.publish("besoeg", {"type": "post", "pc": DEMO_PC, "navn": "Klippe", "stage": "pro",
                                    "outfit": "none", "pynt": {}})
        message = {"tag": f"post:{DEMO_PC}:{self._seq}", "titel": f"✉️ Post fra Klippe ({DEMO_PC})",
                   "tekst": "Kan du lige kigge på det her? 👀",
                   "knapper": [{"tekst": "Svar", "uri": f"projektsog:post/{DEMO_PC}"}], "session": "",
                   "lyd": True, "visning": "kort", "prioritet": "normal", "opkald": False, "besvaret": True,
                   "ringer": False, "tid": time.time(), "udloeber_ved": time.time() + 8 * 3600}
        with self.backend.lock:
            messages = [message, *self.backend.messages]
        self.backend.set_messages({"messages": messages})
        self.bus.publish("messages", {"messages": messages})
        return {"ok": True}

    def _post_kontor_slik(self, body: dict[str, Any]) -> Any:
        ident, answer = body.get("id"), body.get("svar")
        if answer not in ("slik", "ballade") or not isinstance(ident, str) or not ident:
            raise ValueError("Gæsten er gået igen")
        with self.backend.lock:
            hat = self.backend.pet_equipped.get("hat", "ingen")
        prank = {"hat": None if hat == "ingen" else hat, "toiletpapir": hat == "ingen"}
        if ident.startswith("demo:"):
            return {"ok": True, "demo": True, "ballade": prank if answer == "ballade" else None}
        if answer == "ballade":
            return {"ok": True, "ballade": prank}
        return {"ok": True}

    def _post_kontor_slik_demo(self, body: dict[str, Any]) -> Any:
        self._klippe_on()
        self._seq += 1
        event = {"type": "slik", "id": f"demo:{self._seq}", "pc": DEMO_PC, "navn": "Klippe", "stage": "junior",
                 "outfit": "none", "pynt": {"hat": "ingen"}, "kostume": KOSTUMER[self._seq % len(KOSTUMER)],
                 "til_s": 25, "demo": True}
        self.bus.publish("besoeg", event)
        return {"ok": True, "besoeg": event}

    def _post_kontor_gaet(self, body: dict[str, Any]) -> Any:
        pc, vid, tid = body.get("pc"), body.get("vid"), body.get("tid")
        now = time.time()
        if type(tid) is not int or not now < tid <= now + 86400:
            raise ValueError("Gæt et tidspunkt efter nu")
        with self.backend.lock:
            bet = next((b for b in self.state["vaeddemaal"] if b.get("pc") == pc and b.get("vid") == vid), None)
            if bet is None or bet.get("status") != "aaben" or now >= bet.get("lukker", 0):
                raise ValueError("Væddemålet er lukket")
            if bet.get("mit_gaet") is None:
                bet["antal_gaet"] = int(bet.get("antal_gaet") or 0) + 1
            bet["mit_gaet"] = tid
            snapshot = self._copy(bet)
        self.bus.publish("vaeddemaal", snapshot)
        return {"ok": True}

    def _post_kontor_vaeddemaal_demo(self, body: dict[str, Any]) -> Any:
        self._klippe_on()
        now = int(time.time())
        self._seq += 1
        bet = {"pc": DEMO_PC, "vid": self._seq, "navn": "Klippe", "maskine": DEMO_PC, "fil": "Demo_render.mp4",
               "pct": 12, "start": now - 60, "lukker": now + 60, "status": "aaben", "egen": False, "mit_gaet": None,
               "antal_gaet": 1, "resultat": None}
        self.on_publish("vaeddemaal", bet)
        self.bus.publish("vaeddemaal", self._copy(bet))
        return {"ok": True, "vaeddemaal": bet}

    def _post_paaskeaeg(self, body: dict[str, Any]) -> Any:
        if not self._settings("search_paaskeaeg"):
            raise ValueError("Påskeæggene er slået fra")
        word = body.get("ord")
        if word not in EGGS:
            raise ValueError("Ukendt påskeæg")
        now = time.monotonic()
        if now - self._eggs.get(word, -1e9) < 3.0:
            return {"ok": True, "ignoreret": True}
        self._eggs[word] = now
        self.bus.publish("paaskeaeg", {"ord": word})
        return {"ok": True}

    def _post_aar_aabn(self, body: dict[str, Any]) -> Any:
        year = body.get("aar") or date.today().year
        return {"ok": True, "url": f"/aar.html?aar={year}"}

    def _post_aar_del(self, body: dict[str, Any]) -> Any:
        self._klippe_on()
        return {"ok": True}
