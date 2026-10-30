"""The fun in the office (SPEC §23.7) – lunch, post, trick or treat, render bets, records and the
year – with fake sockets, clocks, a fake PetProgress and a fake message board; nothing is ever
sent to the LAN and no sound is made."""

import json
import os
import queue
import random
import tempfile
import unittest
from datetime import datetime

from projektsog import achievements, kontor
from projektsog.config import Config
from projektsog.events import EventBus
from tests.test_kontor import FakeSocket, OTHER_ID, raw

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


# Tuesday in Halloween week 2026 ("uge 44"), noon.
HALLOWEEN_NOON = datetime(2026, 10, 27, 12, 0).timestamp()
A_NORMAL_NOON = datetime(2026, 10, 13, 12, 0).timestamp()
STUDIE = ("192.168.1.50", kontor.PORT)        # STUDIE-PC's Klippe
GRAFIK = ("192.168.1.21", kontor.PORT)        # GRAFIK-PC's Klippe
GRAFIK_ID = "fedcba9876543210"


def packet(kind: str, x=None, **changes) -> dict:
    msg = {"app": "projektsog-klippe", "v": 1, "type": kind, "pc": "STUDIE-PC", "id": OTHER_ID, "seq": 7,
           "navn": "Bobby", "stage": "junior", "outfit": "none", "pynt": {"hat": "festhat"}}
    if x is not None:
        msg["x"] = x
    msg.update(changes)
    return msg


def grafik(kind: str, x=None, **changes) -> dict:
    return packet(kind, x, pc="GRAFIK-PC", id=GRAFIK_ID, navn="Gurli", **changes)


class Clock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class FakeProgress:
    """What the office asks of PetProgress (achievements.py, SPEC §23.1)."""

    def __init__(self) -> None:
        self.notes: list[str] = []
        self.treats: list[tuple] = []
        self.pranks: list[tuple] = []
        self.won: list[tuple] = []
        self.records: list[tuple] = []
        self.hat: str | None = "festhat"
        self.record_answer = True
        self.season = {"saeson": "halloween", "kostume": "vampyr", "kostume_aar": "vampyr", "ballade": None,
                       "bytte": None}

    def note(self, name: str, n: int = 1) -> None:
        self.notes.append(name)

    def treat(self, points: float, name: str) -> dict:
        self.treats.append((points, name))
        return {}

    def season_state(self) -> dict:
        return dict(self.season)

    def prank_received(self, af_pc: str, af_navn: str) -> dict:
        self.pranks.append((af_pc, af_navn))
        return {"hat": self.hat, "toiletpapir": self.hat is None}

    def prank_won(self, hat: str, fra_pc: str, fra_navn: str) -> None:
        self.won.append((hat, fra_pc, fra_navn))

    def office_record(self, pc: str, navn: str, score: int, game: str = "hop") -> bool:
        self.records.append((pc, navn, score, game))
        return self.record_answer


class FakeBoard:
    def __init__(self) -> None:
        self.posts: list[tuple[dict, bool]] = []

    def post(self, data: dict, *, internal: bool = False) -> dict:
        self.posts.append((data, internal))
        return {"ok": True, "vist": True}


class Harness:
    def __init__(self, test: unittest.TestCase, *, settings: dict | None = None, wall: float = A_NORMAL_NOON,
                 shown: bool = True, working: bool = True, progress: bool = True, board: bool = True) -> None:
        tmp = tempfile.TemporaryDirectory()
        test.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.cfg = Config(path=os.path.join(self.dir, "config.json"))
        self.cfg.update({"widget_enabled": True, "widget_pet_name": "Klippe", **(settings or {})})
        self.bus = EventBus()
        self.events = self.bus.subscribe()
        self.clock = Clock(1000.0)
        self.wall = Clock(wall)
        self.sockets: list[FakeSocket] = []
        self.shown = shown
        self.working = working
        self.bells: list[str] = []
        self.progress = FakeProgress() if progress else None
        self.board = FakeBoard() if board else None
        self.equipped = {"hat": "festhat", "haand": "awp"}
        self.k = kontor.Kontor(
            self.cfg, self.bus, equipped=lambda: dict(self.equipped), stats=lambda: achievements.Stats(total_s=40 * 3600),
            hostname=lambda: "TESTPC", hosts=lambda: ["STUDIE-PC"], sock_factory=self._socket,
            resolve_ips=lambda host, timeout=3.0: ["192.168.1.50"] if host.upper() == "STUDIE-PC" else [],
            broadcasts=lambda: ["192.168.1.255"], look=lambda: {"outfit": "color"}, shown=lambda: self.shown,
            progress=self.progress, board=self.board, working=lambda: self.working,
            bell=lambda: self.bells.append("ding"), data_dir=self.dir, clock=self.clock, wall=self.wall,
            rng=random.Random(5))
        self.k._vid = 0                 # (a real process starts its bets at a random number: they count from 1 here)

    def _socket(self) -> FakeSocket:
        self.sockets.append(FakeSocket())
        return self.sockets[-1]

    @property
    def sock(self) -> FakeSocket:
        return self.sockets[-1]

    def open(self) -> "Harness":
        """The socket opens and its first hellos go out (forgotten here)."""
        self.k.step()
        self.sock.sent.clear()
        self.published_all()
        return self

    def published_all(self) -> list[tuple[str, object]]:
        out = []
        while True:
            try:
                event, data, _ts = self.events.get_nowait()
            except queue.Empty:
                return out
            out.append((event, data))

    def published(self, kind: str) -> list:
        return [data for event, data in self.published_all() if event == kind]

    def sent(self) -> list[tuple[dict, tuple]]:
        return [(json.loads(data.decode("utf-8")), address) for data, address in self.sock.sent]

    def flush(self) -> list[tuple[dict, tuple]]:
        """One round of the thread (it sends what the API queued); what went out."""
        self.sock.sent.clear()
        self.k.step()
        out = [(msg, address) for msg, address in self.sent() if msg["type"] != "hej"]
        self.sock.sent.clear()
        return out

    def receive(self, msg: dict, address: tuple = STUDIE):
        return self.k.handle_packet(raw(msg), address)


# --------------------------------------------------------------------------------------
# The schema
# --------------------------------------------------------------------------------------

class SchemaTests(unittest.TestCase):
    NOW = 1_800_000_000.0

    def good(self) -> dict:
        now = int(self.NOW)
        return {
            "slik": {"kostume": "spoegelse"},
            "godbid": {"til": "TESTPC"},
            "ballade": {"til": "TESTPC", "hat": "festhat"},
            "frokost": None,
            "post": {"til": "TESTPC", "tekst": "Kaffe? ☕"},
            "vaeddemaal": {"vid": 3, "fil": "Portræt_v3.mp4", "pct": 12, "start": now - 60, "lukker": now + 120},
            "gaet": {"til": "TESTPC", "vid": 3, "tid": now + 600},
            "vinder": {"vid": 3, "status": "slut", "vinder_pc": "GRAFIK-PC", "vinder_navn": "Gurli",
                       "tid": now + 600, "slut": now + 590},
            "rekord": {"score": 2310},
            "spilrekord": {"spil": "kryds", "score": 2310},
            "aar": {"aar": 2026, "timer": 1234, "side": "color", "projekter": 23, "durum": 57, "kort": 38,
                    "leveringer": 19, "trofaeer": 21},
        }

    def test_every_new_type_with_its_x(self) -> None:
        for kind, x in self.good().items():
            with self.subTest(kind=kind):
                msg = kontor.parse_packet(raw(packet(kind, x)), now=self.NOW)
                self.assertEqual((msg["type"], msg["x"]), (kind, x))
                self.assertIsNone(msg["trofae"])
        # A text is trimmed, a null may stand where the schema allows it.
        post = kontor.parse_packet(raw(packet("post", {"til": "TESTPC", "tekst": "  Hej  "})), now=self.NOW)
        self.assertEqual(post["x"]["tekst"], "Hej")
        nobody = {"vid": 3, "status": "aflyst", "vinder_pc": None, "vinder_navn": None, "tid": None, "slut": None}
        self.assertEqual(kontor.parse_packet(raw(packet("vinder", nobody)), now=self.NOW)["x"], nobody)
        self.assertIsNone(kontor.parse_packet(raw(packet("ballade", {"til": "TESTPC", "hat": None})),
                                              now=self.NOW)["x"]["hat"])
        self.assertIsNone(kontor.parse_packet(raw(packet("aar", {**self.good()["aar"], "side": None})),
                                              now=self.NOW)["x"]["side"])

    def test_a_hat_we_do_not_know_is_no_hat(self) -> None:
        # A newer PC's hat (or the default "no hat") – the prank still counts, without a hat.
        for hat in ("verdenskrone", "ingen", "awp"):
            with self.subTest(hat=hat):
                msg = kontor.parse_packet(raw(packet("ballade", {"til": "TESTPC", "hat": hat})), now=self.NOW)
                self.assertIsNone(msg["x"]["hat"])

    def test_hostile_x_is_refused(self) -> None:
        now = int(self.NOW)
        good = self.good()
        bad = {
            "x missing": packet("post"),
            "x on a hello": packet("hej", {"til": "TESTPC"}),
            "x null on lunch": {**packet("frokost"), "x": None},
            "x a list": packet("post", ["TESTPC", "hej"]),
            "x extra key": packet("post", {**good["post"], "url": "http://x"}),
            "x key missing": packet("post", {"til": "TESTPC"}),
            "til lower": packet("post", {"til": "testpc", "tekst": "hej"}),
            "til long": packet("godbid", {"til": "A" * 16}),
            "tekst empty": packet("post", {"til": "TESTPC", "tekst": ""}),
            "tekst blank": packet("post", {"til": "TESTPC", "tekst": "   "}),
            "tekst long": packet("post", {"til": "TESTPC", "tekst": "x" * 121}),
            "tekst newline": packet("post", {"til": "TESTPC", "tekst": "hej\nmed dig"}),
            "tekst bidi": packet("post", {"til": "TESTPC", "tekst": "hej‮dig"}),
            "tekst number": packet("post", {"til": "TESTPC", "tekst": 5}),
            "kostume unknown": packet("slik", {"kostume": "astronaut"}),
            "hat path": packet("ballade", {"til": "TESTPC", "hat": "..\\x"}),
            "hat number": packet("ballade", {"til": "TESTPC", "hat": 3}),
            "vid bool": packet("vaeddemaal", {**good["vaeddemaal"], "vid": True}),
            "vid negative": packet("vaeddemaal", {**good["vaeddemaal"], "vid": -1}),
            "vid huge": packet("vaeddemaal", {**good["vaeddemaal"], "vid": 2 ** 31}),
            "pct 101": packet("vaeddemaal", {**good["vaeddemaal"], "pct": 101}),
            "pct float": packet("vaeddemaal", {**good["vaeddemaal"], "pct": 12.5}),
            "fil long": packet("vaeddemaal", {**good["vaeddemaal"], "fil": "x" * 41}),
            "fil control": packet("vaeddemaal", {**good["vaeddemaal"], "fil": "a\x07b"}),
            "start far away": packet("vaeddemaal", {**good["vaeddemaal"], "start": now - 90_000}),
            "start beyond a float": packet("vaeddemaal", {**good["vaeddemaal"], "start": 10 ** 400}),
            "tid beyond a float": packet("gaet", {**good["gaet"], "tid": -(10 ** 400)}),
            "slut beyond a float": packet("vinder", {**good["vinder"], "slut": 10 ** 300}),
            "lukker float": packet("vaeddemaal", {**good["vaeddemaal"], "lukker": now + 0.5}),
            "tid string": packet("gaet", {**good["gaet"], "tid": str(now)}),
            "tid next week": packet("gaet", {**good["gaet"], "tid": now + 7 * 86400}),
            "status": packet("vinder", {**good["vinder"], "status": "vundet"}),
            "vid null": packet("vinder", {**good["vinder"], "vid": None}),
            "vinder_navn long": packet("vinder", {**good["vinder"], "vinder_navn": "K" * 21}),
            "score negative": packet("rekord", {"score": -5}),
            "score huge": packet("rekord", {"score": 10 ** 9}),
            "spil unknown": packet("spilrekord", {"spil": "pong", "score": 500}),
            "spil hop": packet("spilrekord", {"spil": "hop", "score": 500}),     # Hop's go as rekord
            "spilrekord without spil": packet("spilrekord", {"score": 500}),
            "spil a list": packet("spilrekord", {"spil": ["skyd"], "score": 500}),
            "aar old": packet("aar", {**good["aar"], "aar": 2025}),
            "aar hours": packet("aar", {**good["aar"], "timer": 10 ** 7}),
            "side upper": packet("aar", {**good["aar"], "side": "Color"}),
            "side markup": packet("aar", {**good["aar"], "side": "<b>"}),
        }
        for name, msg in bad.items():
            with self.subTest(name):
                with self.assertRaises(ValueError):
                    kontor.parse_packet(raw(msg), now=self.NOW)

    def test_nothing_a_packet_holds_stops_the_office(self) -> None:
        # A number no float can hold (it used to raise OverflowError past the ValueError gate) …
        h = Harness(self).open()
        now = int(h.wall.t)
        self.assertIsNone(h.receive(packet("vaeddemaal", {"vid": 1, "fil": "x", "pct": 1, "start": 10 ** 400,
                                                          "lukker": now + 60})))
        # … or a handler that fails for a reason nobody thought of: dropped, the office goes on.
        def broken(msg, ip):
            raise RuntimeError("boom")
        h.k._got_frokost = broken
        self.assertIsNone(h.receive(packet("frokost")))
        h.sock.inbox.append((raw(packet("frokost", seq=8)), ("192.168.1.77", kontor.PORT)))
        h.clock.t += kontor.HEJ_S
        h.k.step()                                               # reads it, and still says hello
        self.assertIn("hej", [msg["type"] for msg, _address in h.sent()])

    def test_the_largest_post_fits_a_datagram(self) -> None:
        h = Harness(self)
        h.cfg.update({"widget_pet_name": "ø" * 20})
        h.equipped = {slot: max((i.id for i in achievements.ITEMS if i.slot == slot), key=len)
                      for slot in achievements.SLOTS}
        data = h.k._message("post", x={"til": "A" * 15, "tekst": "😀" * kontor.POST_MAX})
        self.assertLessEqual(len(data), kontor.MAX_PACKET)
        self.assertEqual(kontor.parse_packet(data, now=h.wall())["x"]["tekst"], "😀" * kontor.POST_MAX)

    def test_year_summary_and_line(self) -> None:
        review = {"aar": 2026, "timer": 1234.6, "projekter": 23, "sider": [{"side": "color", "navn": "Color",
                                                                            "timer": 400}],
                  "mad": {"spist": {"durum": 57, "booster": 3}}, "kort": {"antal": 38}, "leveringer": 19,
                  "klippe": {"trofaeer": 21}, "top": [{"navn": "Rikke Lindholm - Testimonial", "timer": 61}]}
        summary = kontor.year_summary(review)
        self.assertEqual(summary, {"aar": 2026, "timer": 1235, "side": "color", "projekter": 23, "durum": 57,
                                   "kort": 38, "leveringer": 19, "trofaeer": 21})
        self.assertNotIn("Rikke", json.dumps(summary))           # never a project's name
        self.assertEqual(kontor.clean_x("aar", summary), summary)
        self.assertEqual(kontor.year_line(summary),
                         "1.235 timer · mest i Color · 23 projekter · 57 durum · 38 kort · 19 leveringer · 21 trofæer")
        empty = kontor.year_summary({"aar": 2026})
        self.assertEqual(empty, {"aar": 2026, "timer": 0, "side": None, "projekter": 0, "durum": 0, "kort": 0,
                                 "leveringer": 0, "trofaeer": 0})
        self.assertEqual(kontor.year_line(empty), "0 timer")
        self.assertEqual(kontor.year_line({**empty, "projekter": 1, "leveringer": 1, "trofaeer": 1}),
                         "0 timer · 1 projekt · 1 levering · 1 trofæ")
        self.assertEqual(kontor.year_summary({"aar": 2026, "timer": float("nan"), "sider": [{"side": "X Y"}]})["side"],
                         None)

    def test_halloween_week(self) -> None:
        for day, expected in (("2026-10-25", False), ("2026-10-26", True), ("2026-10-31", True), ("2026-11-01", True),
                              ("2026-11-02", False), ("2027-10-25", True), ("2027-10-31", True),
                              ("2027-11-01", False)):
            with self.subTest(day=day):
                self.assertEqual(kontor.halloween_week(datetime.fromisoformat(day).date()), expected)

    def test_texts_as_typed(self) -> None:
        self.assertEqual(kontor.clean_text("  Kan du\nlige\tkigge? \x07 👀 "), "Kan du lige kigge? 👀")
        self.assertEqual(kontor.clean_text(None), "")


# --------------------------------------------------------------------------------------
# Receiving
# --------------------------------------------------------------------------------------

class ReceiveTests(unittest.TestCase):
    def test_lunch(self) -> None:
        h = Harness(self)
        self.assertEqual(h.receive(packet("frokost")), {"pc": "STUDIE-PC", "navn": "Bobby", "selv": False})
        self.assertEqual(h.published("frokost"), [{"pc": "STUDIE-PC", "navn": "Bobby", "selv": False}])
        self.assertEqual(h.bells, ["ding"])
        self.assertIsNone(h.receive(packet("frokost", seq=8)))   # the same sender: once in 5 minutes
        h.clock.t += kontor.TYPE_GAPS["frokost"]
        h.shown = False                                          # Klippe is not on the screen: no sound
        self.assertIsNotNone(h.receive(packet("frokost", seq=9)))
        self.assertEqual(h.bells, ["ding"])
        h.cfg.update({"widget_frokost": False})
        h.clock.t += kontor.TYPE_GAPS["frokost"]
        self.assertIsNone(h.receive(packet("frokost", seq=10)))
        self.assertEqual(h.published("frokost"), [{"pc": "STUDIE-PC", "navn": "Bobby", "selv": False}])

    def test_a_post_for_us(self) -> None:
        h = Harness(self)
        event = h.receive(packet("post", {"til": "TESTPC", "tekst": "Kan du lige kigge på det her? 👀"}))
        self.assertEqual(event, {"type": "post", "pc": "STUDIE-PC", "navn": "Bobby", "stage": "junior", "outfit": "none",
                                 "pynt": kontor.clean_pynt({"hat": "festhat"})})
        self.assertEqual(h.published("besoeg"), [event])
        (card, internal), = h.board.posts
        self.assertTrue(internal)
        self.assertEqual(card, {"tag": "post:STUDIE-PC:7", "titel": "✉️ Post fra Bobby (STUDIE-PC)",
                                "tekst": "Kan du lige kigge på det her? 👀",
                                "knapper": [{"tekst": "Svar", "uri": "projektsog:post/STUDIE-PC"}],
                                "udloeber": 8 * 3600, "lyd": True, "opkald": False})
        self.assertEqual(h.progress.notes, ["post_faaet"])

    def test_a_post_for_someone_else_is_ignored(self) -> None:
        h = Harness(self)
        self.assertIsNone(h.receive(packet("post", {"til": "GRAFIK-PC", "tekst": "hemmeligt"})))
        self.assertEqual((h.board.posts, h.published_all()), ([], []))
        self.assertEqual(h.k.state()["peers"], [])                # not even noted
        self.assertNotIn(STUDIE[0], h.k._senders)                 # … nor its rate limit spent

    def test_post_rate_limits(self) -> None:
        h = Harness(self)
        self.assertIsNotNone(h.receive(packet("post", {"til": "TESTPC", "tekst": "1"})))
        self.assertIsNone(h.receive(packet("post", {"til": "TESTPC", "tekst": "2"}, seq=8)))   # 15 s
        h.clock.t += kontor.TYPE_GAPS["post"]
        self.assertIsNotNone(h.receive(packet("post", {"til": "TESTPC", "tekst": "3"}, seq=9)))
        # ≤ 30 posts an hour from the whole office
        for n in range(kontor.POSTS_PER_HOUR - 2):
            self.assertIsNotNone(h.receive(packet("post", {"til": "TESTPC", "tekst": "x"}, pc=f"PC{n}",
                                                  id=f"{n:016x}"), (f"192.168.1.{100 + n}", kontor.PORT)))
        self.assertIsNone(h.receive(packet("post", {"til": "TESTPC", "tekst": "x"}, pc="PC99", id="9" * 16),
                                    ("192.168.1.199", kontor.PORT)))
        h.clock.t += 3600
        self.assertIsNotNone(h.receive(packet("post", {"til": "TESTPC", "tekst": "x"}, pc="PC99", id="9" * 16),
                                       ("192.168.1.199", kontor.PORT)))
        h.cfg.update({"widget_post": False})
        h.clock.t += 60
        self.assertIsNone(h.receive(packet("post", {"til": "TESTPC", "tekst": "x"}, seq=20)))

    def test_trick_or_treat_at_our_door(self) -> None:
        h = Harness(self, wall=HALLOWEEN_NOON)
        event = h.receive(packet("slik", {"kostume": "skelet"}))
        self.assertEqual(event, {"type": "slik", "pc": "STUDIE-PC", "navn": "Bobby", "stage": "junior",
                                 "outfit": "none", "pynt": kontor.clean_pynt({"hat": "festhat"}),
                                 "id": "STUDIE-PC:7", "kostume": "skelet", "til_s": 25})
        self.assertEqual(h.published("besoeg"), [event])
        h.clock.t += kontor.TYPE_GAPS["slik"]
        self.assertIsNone(h.receive(packet("slik", {"kostume": "skelet"}, seq=8)))   # once a day per sender
        h.wall.t += 86400
        h.clock.t += kontor.TYPE_GAPS["slik"]
        self.assertIsNotNone(h.receive(packet("slik", {"kostume": "skelet"}, seq=9)))
        h.cfg.update({"widget_saeson": False})
        self.assertIsNone(h.receive(grafik("slik", {"kostume": "heks"}), GRAFIK))

    def test_trick_or_treat_uses_the_visits_budget(self) -> None:
        h = Harness(self, wall=HALLOWEEN_NOON)
        for n in range(kontor.VISITS_PER_HOUR):
            self.assertIsNotNone(h.receive(packet("besoeg", pc=f"PC{n}", id=f"{n:016x}"),
                                           (f"192.168.1.{100 + n}", kontor.PORT)))
        self.assertIsNone(h.receive(packet("slik", {"kostume": "heks"})))

    def test_a_booster_for_the_guest(self) -> None:
        h = Harness(self, wall=HALLOWEEN_NOON).open()
        guest = h.receive(packet("slik", {"kostume": "skelet"}))
        self.assertEqual(h.k.treat_or_trick(guest["id"], "slik"), {"ok": True, "ballade": None})
        (msg, address), = h.flush()
        self.assertEqual(address, STUDIE)                         # to the guest's PC only
        self.assertEqual((msg["type"], msg["x"]), ("godbid", {"til": "STUDIE-PC"}))
        self.assertEqual(h.progress.notes, ["slik_givet"])
        with self.assertRaisesRegex(ValueError, "Gæsten er gået igen"):
            h.k.treat_or_trick(guest["id"], "ballade")            # answered already
        with self.assertRaisesRegex(ValueError, "Gæsten er gået igen"):
            h.k.treat_or_trick("ANDEN-PC:1", "slik")
        with self.assertRaisesRegex(ValueError, "svar"):
            h.k.treat_or_trick(guest["id"], "chokolade")

    def test_mischief_takes_our_hat(self) -> None:
        h = Harness(self, wall=HALLOWEEN_NOON).open()
        guest = h.receive(packet("slik", {"kostume": "vampyr"}))
        self.assertEqual(h.k.treat_or_trick(guest["id"], "ballade"),
                         {"ok": True, "ballade": {"hat": "festhat", "toiletpapir": False}})
        self.assertEqual(h.progress.pranks, [("STUDIE-PC", "Bobby")])
        (msg, address), = h.flush()
        self.assertEqual((msg["type"], msg["x"], address), ("ballade", {"til": "STUDIE-PC", "hat": "festhat"}, STUDIE))
        # No hat: toilet paper.
        h.progress.hat = None
        h.clock.t += kontor.TYPE_GAPS["slik"]
        other = h.receive(grafik("slik", {"kostume": "heks"}), GRAFIK)
        self.assertEqual(h.k.treat_or_trick(other["id"], "ballade"),
                         {"ok": True, "ballade": {"hat": None, "toiletpapir": True}})
        (msg, address), = h.flush()
        self.assertEqual((msg["x"], address), ({"til": "GRAFIK-PC", "hat": None}, GRAFIK))

    def test_a_guest_does_not_wait_forever(self) -> None:
        h = Harness(self, wall=HALLOWEEN_NOON).open()
        guest = h.receive(packet("slik", {"kostume": "skelet"}))
        h.clock.t += kontor.SLIK_PENDING_S
        with self.assertRaisesRegex(ValueError, "Gæsten er gået igen"):
            h.k.treat_or_trick(guest["id"], "slik")
        self.assertEqual(h.flush(), [])

    def test_answers_to_our_own_trick_or_treat(self) -> None:
        h = Harness(self, wall=HALLOWEEN_NOON).open()
        # Nobody asked: a Booster from nowhere is not taken (nobody can stuff Klippe).
        self.assertIsNone(h.receive(packet("godbid", {"til": "TESTPC"})))
        h.clock.t += kontor.TYPE_GAPS["godbid"]                 # (that one counted against its sender too)
        h.k._slik_sent_at = h.clock.t                           # (we went trick-or-treating just now)
        self.assertEqual(h.receive(packet("godbid", {"til": "TESTPC"}, seq=8)),
                         {"type": "godbid", "pc": "STUDIE-PC", "navn": "Bobby"})
        self.assertEqual(h.progress.treats, [(10.0, "Booster")])
        self.assertEqual(h.progress.notes, ["slik_faaet"])
        h.clock.t += 10
        self.assertIsNone(h.receive(packet("godbid", {"til": "TESTPC"}, seq=9)))   # once per PC
        self.assertEqual(h.receive(grafik("ballade", {"til": "TESTPC", "hat": "nissehue"}), GRAFIK),
                         {"type": "ballade", "pc": "GRAFIK-PC", "navn": "Gurli", "hat": "nissehue",
                          "toiletpapir": False})
        self.assertEqual(h.progress.won, [("nissehue", "GRAFIK-PC", "Gurli")])
        self.assertEqual([e["type"] for e in h.published("slik_svar")], ["godbid", "ballade"])
        # Toilet paper: no hat to wear.
        h.k._slik_answers.clear()
        h.clock.t += 10
        self.assertEqual(h.receive(grafik("ballade", {"til": "TESTPC", "hat": None}, seq=8), GRAFIK)["toiletpapir"],
                         True)
        self.assertEqual(len(h.progress.won), 1)
        h.clock.t += kontor.SLIK_ANSWER_S                       # too late
        h.k._slik_answers.clear()
        self.assertIsNone(h.receive(packet("godbid", {"til": "TESTPC"}, seq=10)))

    def test_a_colleagues_record(self) -> None:
        h = Harness(self)
        self.assertEqual(h.receive(packet("rekord", {"score": 2310})),
                         {"tekst": "🎮 Bobby (STUDIE-PC) har ny kontorrekord i render-spillet: 2310!"})
        self.assertEqual(h.progress.records, [("STUDIE-PC", "Bobby", 2310, "hop")])
        self.assertEqual(len(h.published("say")), 1)
        # the render game's other games come as spilrekord
        self.assertEqual(h.receive(packet("spilrekord", {"spil": "skyd", "score": 1400}, seq=7)),
                         {"tekst": "🎮 Bobby (STUDIE-PC) har ny kontorrekord i Fejljagt: 1400!"})
        self.assertEqual(h.progress.records[-1], ("STUDIE-PC", "Bobby", 1400, "skyd"))
        self.assertEqual(len(h.published("say")), 1)
        h.progress.record_answer = False                         # not the office's best
        self.assertIsNone(h.receive(grafik("rekord", {"score": 900}), GRAFIK))
        self.assertEqual(h.published("say"), [])
        h.cfg.update({"widget_renderspil": False})
        h.clock.t += 60
        self.assertIsNone(h.receive(packet("rekord", {"score": 5000}, seq=8)))
        self.assertIsNone(h.receive(packet("spilrekord", {"spil": "kryds", "score": 5000}, seq=9)))
        self.assertEqual(len(h.progress.records), 3)

    def test_a_colleagues_year(self) -> None:
        h = Harness(self)
        x = {"aar": 2026, "timer": 1234, "side": "color", "projekter": 23, "durum": 57, "kort": 38,
             "leveringer": 19, "trofaeer": 21}
        event = h.receive(packet("aar", x))
        self.assertEqual(event["type"], "aar")
        self.assertEqual(event["aar"], x)
        (card, internal), = h.board.posts
        self.assertEqual(card, {"tag": "aar:STUDIE-PC:2026", "titel": "📊 Klippet 2026 fra Bobby (STUDIE-PC)",
                                "tekst": kontor.year_line(x), "udloeber": 2 * 86400, "lyd": False})
        self.assertTrue(internal)
        h.cfg.update({"widget_aarsrapport": False})
        self.assertIsNone(h.receive(grafik("aar", x), GRAFIK))

    def test_without_progress_or_board(self) -> None:
        h = Harness(self, progress=False, board=False)
        self.assertIsNotNone(h.receive(packet("post", {"til": "TESTPC", "tekst": "hej"})))
        self.assertIsNone(h.receive(packet("rekord", {"score": 2310})))


# --------------------------------------------------------------------------------------
# Sending
# --------------------------------------------------------------------------------------

class SendTests(unittest.TestCase):
    def test_ringing_for_lunch(self) -> None:
        h = Harness(self)
        with self.assertRaisesRegex(ValueError, "ikke klar endnu"):
            h.k.lunch()                                          # the socket is not open yet
        h.open()
        self.assertEqual(h.k.lunch(), {"ok": True})
        self.assertEqual(h.published("frokost"), [{"pc": "TESTPC", "navn": "Klippe", "selv": True}])
        self.assertEqual(h.bells, ["ding"])
        self.assertEqual(h.progress.notes, ["frokost_ringet"])
        sent = h.flush()
        self.assertEqual([address for _msg, address in sent], [STUDIE, ("192.168.1.255", kontor.PORT)])
        self.assertEqual({msg["type"] for msg, _address in sent}, {"frokost"})
        self.assertNotIn("x", sent[0][0])
        with self.assertRaisesRegex(ValueError, "Du har lige ringet"):
            h.k.lunch()
        h.clock.t += kontor.LUNCH_GAP_S
        self.assertEqual(h.k.lunch(), {"ok": True})

    def test_lunch_needs_klippe_the_bell_and_the_office(self) -> None:
        for settings, message in (({"widget_enabled": False}, "Slå Klippe til først"),
                                  ({"widget_frokost": False}, "Frokostklokken er slået fra"),
                                  ({"widget_kontor": False}, "Kontor-Klipperne er slået fra")):
            with self.subTest(settings=settings):
                h = Harness(self)
                h.open()
                h.cfg.update(settings)
                with self.assertRaisesRegex(ValueError, message):
                    h.k.lunch()
                self.assertEqual(h.published("frokost"), [])

    def test_a_lunch_demo_stays_here(self) -> None:
        h = Harness(self)
        self.assertEqual(h.k.lunch_demo(), {"ok": True})
        self.assertEqual(h.published("frokost"), [{"pc": "DEMO-PC", "navn": "Klippe", "selv": False, "demo": True}])
        self.assertEqual(h.bells, ["ding"])
        self.assertEqual(h.sockets, [])
        h.cfg.update({"widget_enabled": False})
        with self.assertRaisesRegex(ValueError, "Slå Klippe til først"):
            h.k.lunch_demo()

    def test_a_post_to_a_colleague(self) -> None:
        h = Harness(self).open()
        h.receive(packet("hej"))                                 # STUDIE-PC's Klippe is here
        h.receive(grafik("hej"), GRAFIK)
        self.assertEqual(h.k.send_post(" studie-pc ", "  Kan du lige\nkigge? 👀 "), {"ok": True})
        (msg, address), = h.flush()
        self.assertEqual(address, STUDIE)                         # that PC only – never the broadcast
        self.assertEqual((msg["type"], msg["x"]), ("post", {"til": "STUDIE-PC", "tekst": "Kan du lige kigge? 👀"}))
        self.assertEqual(h.published("post_sendt"), [{"til": "STUDIE-PC", "navn_til": "Bobby"}])
        self.assertEqual(h.progress.notes, ["post_sendt"])
        with self.assertRaisesRegex(ValueError, "stadig ude med posten"):
            h.k.send_post("GRAFIK-PC", "Frokost? 🍔")
        h.clock.t += kontor.POST_GAP_S
        self.assertEqual(h.k.send_post("GRAFIK-PC", "Frokost? 🍔"), {"ok": True})
        (msg, address), = h.flush()
        self.assertEqual(address, GRAFIK)

    def test_post_mistakes(self) -> None:
        h = Harness(self).open()
        h.receive(packet("hej"))
        for til, tekst, message in ((None, "hej", "Vælg en kollega"), ("  ", "hej", "Vælg en kollega"),
                                    ("STUDIE-PC", " \n ", "Skriv en besked"),
                                    ("STUDIE-PC", "x" * 121, "for lang"),
                                    ("GRAFIK-PC", "hej", "GRAFIK-PC er ikke på lige nu"),
                                    ("<script>", "hej", "Kollegaen er ikke på lige nu"),
                                    ("TESTPC", "hej", "TESTPC er ikke på lige nu")):
            with self.subTest(til=til, tekst=tekst):
                with self.assertRaisesRegex(ValueError, message):
                    h.k.send_post(til, tekst)
        h.clock.t += kontor.PEER_FRESH_S + 1                     # STUDIE-PC has not said hello for a while
        with self.assertRaisesRegex(ValueError, "STUDIE-PC er ikke på lige nu"):
            h.k.send_post("STUDIE-PC", "hej")
        self.assertEqual(h.flush(), [])
        h.cfg.update({"widget_post": False})
        with self.assertRaisesRegex(ValueError, "Klippe-post er slået fra"):
            h.k.send_post("STUDIE-PC", "hej")

    def test_the_demo_post_and_its_answer(self) -> None:
        h = Harness(self)
        answer = h.k.post_demo()
        self.assertEqual(answer["besoeg"]["type"], "post")
        self.assertEqual(answer["besoeg"]["pc"], "DEMO-PC")
        (card, internal), = h.board.posts
        self.assertEqual((card["tag"], card["knapper"][0]["uri"], card["opkald"], internal),
                         ("post:DEMO-PC:1", "projektsog:post/DEMO-PC", False, True))
        self.assertEqual(h.progress.notes, [])                   # a demo counts for nothing
        # "Svar" to DEMO-PC: Klippe walks out with the letter, nothing is sent.
        self.assertEqual(h.k.send_post("DEMO-PC", "Tak! 👍"), {"ok": True, "demo": True})
        self.assertEqual(h.published("post_sendt"), [{"til": "DEMO-PC", "navn_til": "Klippe"}])
        self.assertEqual(h.sockets, [])

    def test_trick_or_treat_once_a_day_in_halloween_week(self) -> None:
        h = Harness(self, wall=datetime(2026, 10, 27, 9, 0).timestamp()).open()
        h.k.step()
        self.assertEqual(h.flush(), [])                          # not before 10:00
        plan_day, plan_at = h.k._slik_plan
        self.assertEqual(plan_day, "2026-10-27")
        self.assertTrue(datetime(2026, 10, 27, 10).timestamp() <= plan_at <= datetime(2026, 10, 27, 15, 30).timestamp())
        h.wall.t = plan_at + 1
        h.working = False                                         # it waits until you work …
        self.assertEqual(h.flush(), [])
        h.working, h.shown = True, False                          # … and Klippe is on the screen
        self.assertEqual(h.flush(), [])
        h.shown = True
        sent = h.flush()
        self.assertEqual({msg["type"] for msg, _address in sent}, {"slik"})
        self.assertEqual(sent[0][0]["x"], {"kostume": "vampyr"})
        self.assertEqual([address for _msg, address in sent], [STUDIE, ("192.168.1.255", kontor.PORT)])
        h.wall.t += 600
        self.assertEqual(h.flush(), [])                          # once a day
        with open(os.path.join(h.dir, "kontor.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["slik_dag"], "2026-10-27")
        # The next weekday it goes again; the weekend and other weeks it stays home.
        h.wall.t = datetime(2026, 10, 28, 15, 31).timestamp()
        self.assertEqual({msg["type"] for msg, _address in h.flush()}, {"slik"})
        h.wall.t = datetime(2026, 10, 31, 15, 31).timestamp()   # a Saturday
        self.assertEqual(h.flush(), [])
        h.wall.t = datetime(2026, 11, 2, 15, 31).timestamp()    # week 45
        self.assertEqual(h.flush(), [])

    def test_no_trick_or_treat_without_a_costume_or_the_season(self) -> None:
        h = Harness(self, wall=datetime(2026, 10, 27, 15, 31).timestamp())
        h.progress.season = {"saeson": None, "kostume": None, "kostume_aar": None}
        h.open()
        self.assertEqual(h.flush(), [])
        h.progress.season = {"kostume": None, "kostume_aar": "heks"}   # the year's costume will do
        self.assertEqual(h.flush()[0][0]["x"], {"kostume": "heks"})
        h2 = Harness(self, wall=datetime(2026, 10, 27, 15, 31).timestamp(), settings={"widget_saeson": False}).open()
        self.assertEqual(h2.flush(), [])

    def test_a_demo_guest(self) -> None:
        h = Harness(self)
        answer = h.k.slik_demo()
        event = answer["besoeg"]
        self.assertEqual((event["type"], event["pc"], event["id"], event["til_s"], event["demo"]),
                         ("slik", "DEMO-PC", "demo:1", 25, True))
        self.assertIn(event["kostume"], kontor.KOSTUMER)
        # Answering the demo changes nothing; mischief shows our own hat.
        self.assertEqual(h.k.treat_or_trick("demo:1", "slik"), {"ok": True, "demo": True, "ballade": None})
        self.assertEqual(h.k.treat_or_trick("demo:1", "ballade"),
                         {"ok": True, "demo": True, "ballade": {"hat": "festhat", "toiletpapir": False}})
        h.equipped = {"hat": "ingen"}
        self.assertEqual(h.k.treat_or_trick("demo:1", "ballade")["ballade"], {"hat": None, "toiletpapir": True})
        self.assertEqual((h.progress.pranks, h.progress.notes, h.sockets), ([], [], []))

    def test_a_record_and_the_year_go_to_everyone(self) -> None:
        h = Harness(self)
        self.assertEqual(h.k.record(5000), 0)                    # the office is not open
        h.open()
        self.assertEqual(h.k.record(99), 0)                      # too small to tell
        self.assertEqual(h.k.record(2310), 1)
        (msg, _address), _broadcast = h.flush()
        self.assertEqual((msg["type"], msg["x"]), ("rekord", {"score": 2310}))
        self.assertEqual(h.k.record(1500, "skyd"), 1)
        (msg, _address), _broadcast = h.flush()
        self.assertEqual((msg["type"], msg["x"]), ("spilrekord", {"spil": "skyd", "score": 1500}))
        self.assertEqual(h.k.record(1500, "pong"), 0)
        summary = {"aar": 2026, "timer": 1234, "side": "color", "projekter": 23, "durum": 57, "kort": 38,
                   "leveringer": 19, "trofaeer": 21}
        self.assertEqual(h.k.share_year(summary), {"ok": True})
        (msg, _address), _broadcast = h.flush()
        self.assertEqual((msg["type"], msg["x"]), ("aar", summary))
        with self.assertRaisesRegex(ValueError, "Du har lige delt den"):
            h.k.share_year(summary)
        h.clock.t += kontor.SHARE_GAP_S
        with self.assertRaisesRegex(ValueError, "kunne ikke deles"):
            h.k.share_year({**summary, "timer": -1})
        h.cfg.update({"widget_aarsrapport": False})
        with self.assertRaisesRegex(ValueError, "Klippet er slået fra"):
            h.k.share_year(summary)

    def test_nothing_waits_when_the_office_closes(self) -> None:
        h = Harness(self).open()
        h.k.lunch()
        h.cfg.update({"widget_kontor": False})
        h.k.step()
        self.assertEqual(list(h.k._outbox), [])
        self.assertTrue(h.sockets[0].closed)


# --------------------------------------------------------------------------------------
# Render bets
# --------------------------------------------------------------------------------------

def rendering(pct: int = 10, eta_s: int | None = 600, navn: str = "Portræt_v3.mp4", faerdig=None) -> dict:
    return {"aktiv": True, "pct": pct, "eta_s": eta_s, "navn": navn, "tidslinje": "Portræt", "projekt": "Rikke",
            "af_claude": None, "faerdig": faerdig}


def ended(udfald: str = "done", seq: int = 1, fil: str = "Portræt_v3.mp4") -> dict:
    return {"aktiv": False, "pct": None, "eta_s": None, "navn": None, "tidslinje": None, "projekt": None,
            "af_claude": None, "faerdig": {"udfald": udfald, "fil": fil, "sti": None, "mappe": None,
                                           "levering": False, "fejl": None, "seq": seq}}


def start_own_bet(h: Harness) -> dict:
    """Our render runs long enough for a bet: the bet it opened."""
    h.k.on_event("render", rendering(pct=3))
    h.clock.t += kontor.BET_AFTER_S
    h.wall.t += kontor.BET_AFTER_S
    h.k.on_event("render", rendering(pct=6))
    bet, = h.published("vaeddemaal")
    return bet


class OwnBetTests(unittest.TestCase):
    def start_bet(self, h: Harness) -> dict:
        return start_own_bet(h)

    def test_a_long_render_opens_a_bet(self) -> None:
        h = Harness(self).open()
        start = int(h.wall.t)
        bet = self.start_bet(h)
        self.assertEqual(bet, {"pc": "TESTPC", "vid": 1, "navn": "Klippe", "maskine": "TESTPC", "fil": "Portræt_v3.mp4",
                               "pct": 6, "start": start, "lukker": int(h.wall.t) + 180, "status": "aaben", "egen": True,
                               "mit_gaet": None, "antal_gaet": 0, "resultat": None})
        sent = h.flush()
        self.assertEqual([address for _msg, address in sent], [STUDIE, ("192.168.1.255", kontor.PORT)])
        self.assertEqual(sent[0][0]["x"], {"vid": 1, "fil": "Portræt_v3.mp4", "pct": 6, "start": start,
                                           "lukker": int(h.wall.t) + 180})
        h.k.on_event("render", rendering(pct=8))
        self.assertEqual(h.flush(), [])                          # one bet per render
        self.assertEqual(h.k.bets()["vaeddemaal"][0]["vid"], 1)

    def test_short_renders_and_switched_off_bets(self) -> None:
        for settings, eta in (({}, 179), ({}, None), ({"widget_vaeddemaal": False}, 900)):
            with self.subTest(settings=settings, eta=eta):
                h = Harness(self, settings=settings).open()
                h.k.on_event("render", rendering(eta_s=eta))
                h.clock.t += 60
                h.k.on_event("render", rendering(eta_s=eta))
                self.assertEqual(h.published("vaeddemaal"), [])
                self.assertEqual(h.flush(), [])
        h = Harness(self)                                        # the office is closed: no bet
        h.k.on_event("render", rendering())
        h.clock.t += 60
        h.k.on_event("render", rendering())
        self.assertEqual(h.published("vaeddemaal"), [])

    def test_guesses_and_the_winner(self) -> None:
        h = Harness(self).open()
        self.start_bet(h)
        h.flush()
        now = int(h.wall.t)
        # Two colleagues guess (to us only); we guess ourselves.
        h.receive(packet("gaet", {"til": "TESTPC", "vid": 1, "tid": now + 900}))
        h.receive(grafik("gaet", {"til": "TESTPC", "vid": 1, "tid": now + 620}), GRAFIK)
        self.assertEqual(h.k.guess("TESTPC", 1, now + 400)["vaeddemaal"]["antal_gaet"], 3)
        self.assertEqual(h.flush(), [])                          # our own guess stays here
        self.assertEqual(h.progress.notes, ["vaeddemaal_gaet"])
        h.wall.t += 600                                          # done after ten minutes
        h.published_all()
        h.k.on_event("render", ended("done"))
        bet, = h.published("vaeddemaal")
        self.assertEqual((bet["status"], bet["resultat"]),
                         ("slut", {"vinder_pc": "GRAFIK-PC", "vinder_navn": "Gurli", "tid": now + 620,
                                   "slut": int(h.wall.t), "mig": False}))
        (msg, _address), _broadcast = h.flush()
        self.assertEqual((msg["type"], msg["x"]), ("vinder", {"vid": 1, "status": "slut", "vinder_pc": "GRAFIK-PC",
                                                            "vinder_navn": "Gurli", "tid": now + 620,
                                                            "slut": int(h.wall.t)}))
        self.assertNotIn("vaeddemaal_vundet", h.progress.notes)
        h.clock.t += kontor.BET_KEEP_S
        self.assertEqual(h.k.bets(), {"vaeddemaal": []})         # forgotten after two minutes

    def test_we_can_win_our_own_bet(self) -> None:
        h = Harness(self).open()
        self.start_bet(h)
        now = int(h.wall.t)
        h.k.guess("TESTPC", 1, now + 300)
        h.receive(packet("gaet", {"til": "TESTPC", "vid": 1, "tid": now + 900}))
        h.wall.t += 290
        h.k.on_event("render", ended("done"))
        self.assertTrue(h.published("vaeddemaal")[-1]["resultat"]["mig"])
        self.assertIn("vaeddemaal_vundet", h.progress.notes)

    def test_a_tie_goes_to_the_earlier_guess(self) -> None:
        self.assertEqual(kontor.winner_of({"A": ("a", 1000, 50.0), "B": ("b", 1020, 40.0)}, 1010), ("B", "b", 1020))
        self.assertEqual(kontor.winner_of({"A": ("a", 1000, 30.0), "B": ("b", 1020, 40.0)}, 1010), ("A", "a", 1000))
        self.assertIsNone(kontor.winner_of({}, 1010))

    def test_guesses_close_at_half_way_or_on_time(self) -> None:
        h = Harness(self).open()
        self.start_bet(h)
        h.flush()
        h.k.on_event("render", rendering(pct=50))
        bet = h.published("vaeddemaal")[-1]
        self.assertEqual((bet["status"], bet["lukker"]), ("lukket", int(h.wall.t)))
        (msg, _address), _broadcast = h.flush()                  # the colleagues close it too
        self.assertEqual((msg["type"], msg["x"]["lukker"]), ("vaeddemaal", int(h.wall.t)))
        with self.assertRaisesRegex(ValueError, "Væddemålet er lukket"):
            h.k.guess("TESTPC", 1, int(h.wall.t) + 60)
        self.assertIsNone(h.receive(packet("gaet", {"til": "TESTPC", "vid": 1, "tid": int(h.wall.t) + 60})))
        # … or after three minutes, by the clock.
        h2 = Harness(self).open()
        self.start_bet(h2)
        h2.wall.t += kontor.BET_OPEN_S
        h2.k.step()
        self.assertEqual(h2.published("vaeddemaal")[-1]["status"], "lukket")

    def test_bad_guesses(self) -> None:
        h = Harness(self).open()
        self.start_bet(h)
        now = int(h.wall.t)
        for args, message in ((("TESTPC", 1, now), "efter nu"), (("TESTPC", 1, now + 2 * 86400), "efter nu"),
                              (("TESTPC", 1, "14:32"), "efter nu"), (("TESTPC", 2, now + 60), "lukket"),
                              (("GRAFIK-PC", 1, now + 60), "lukket"), ((None, 1, now + 60), "lukket")):
            with self.subTest(args=args):
                with self.assertRaisesRegex(ValueError, message):
                    h.k.guess(*args)
        h.cfg.update({"widget_vaeddemaal": False})
        with self.assertRaisesRegex(ValueError, "slået fra"):
            h.k.guess("TESTPC", 1, now + 60)

    def test_a_failed_render_calls_the_bet_off(self) -> None:
        h = Harness(self).open()
        self.start_bet(h)
        h.flush()
        h.k.on_event("render", ended("failed"))
        bet = h.published("vaeddemaal")[-1]
        self.assertEqual((bet["status"], bet["resultat"]), ("aflyst", None))
        (msg, _address), _broadcast = h.flush()
        self.assertEqual(msg["x"], {"vid": 1, "status": "aflyst", "vinder_pc": None, "vinder_navn": None, "tid": None,
                                    "slut": None})

    def test_another_job_in_the_queue_does_not_end_it(self) -> None:
        h = Harness(self).open()
        h.k.on_event("render", rendering(navn="Teaser.mp4", faerdig={"udfald": "done", "seq": 4, "fil": "Gammel.mp4"}))
        h.clock.t += kontor.BET_AFTER_S
        h.k.on_event("render", rendering(navn="Teaser.mp4", faerdig={"udfald": "done", "seq": 4, "fil": "Gammel.mp4"}))
        h.flush()
        h.published_all()
        # The queue's first job ends while the rest still renders: the bet is about Teaser.mp4.
        h.k.on_event("render", rendering(navn="Hovedfilm.mov", faerdig={"udfald": "done", "seq": 5, "fil": "Forspil.mp4"}))
        self.assertEqual([b["status"] for b in h.published("vaeddemaal")], [])
        h.k.on_event("render", rendering(navn="Hovedfilm.mov", faerdig={"udfald": "done", "seq": 6, "fil": "Teaser.mp4"}))
        self.assertEqual(h.published("vaeddemaal")[-1]["status"], "slut")

    def test_a_render_that_goes_quiet(self) -> None:
        h = Harness(self).open()
        self.start_bet(h)
        h.flush()
        h.k.on_event("render", {"aktiv": False, "faerdig": None})   # Resolve closed: no end comes
        h.clock.t += kontor.BET_LOST_S - 1
        h.k.step()
        self.assertEqual(h.k.bets()["vaeddemaal"][0]["status"], "aaben")
        h.clock.t += 1
        sent = h.flush()
        self.assertEqual(h.k.bets()["vaeddemaal"][0]["status"], "aflyst")
        self.assertEqual(sent[0][0]["x"]["status"], "aflyst")


class ColleagueBetTests(unittest.TestCase):
    def open_bet(self, h: Harness, **x) -> dict:
        now = int(h.wall.t)
        bet = {"vid": 4, "fil": "Reklame.mp4", "pct": 9, "start": now - 30, "lukker": now + 180, **x}
        return h.receive(packet("vaeddemaal", bet))

    def test_a_colleagues_bet(self) -> None:
        h = Harness(self).open()
        now = int(h.wall.t)
        view = self.open_bet(h)
        self.assertEqual(view, {"pc": "STUDIE-PC", "vid": 4, "navn": "Bobby", "maskine": "STUDIE-PC", "fil": "Reklame.mp4",
                                "pct": 9, "start": now - 30, "lukker": now + 180, "status": "aaben", "egen": False,
                                "mit_gaet": None, "antal_gaet": 0, "resultat": None})
        self.assertEqual(h.published("vaeddemaal"), [view])
        answer = h.k.guess("STUDIE-PC", 4, now + 700)
        self.assertEqual((answer["vaeddemaal"]["mit_gaet"], answer["vaeddemaal"]["antal_gaet"]), (now + 700, 1))
        (msg, address), = h.flush()
        self.assertEqual((msg["type"], msg["x"], address), ("gaet", {"til": "STUDIE-PC", "vid": 4, "tid": now + 700},
                                                           STUDIE))
        # A new guess replaces it (sent again) – not within a moment: its owner takes one per 2 s.
        with self.assertRaisesRegex(ValueError, "Vent lidt, før du gætter igen"):
            h.k.guess("STUDIE-PC", 4, now + 650)
        h.clock.t += kontor.GUESS_GAP_S
        h.k.guess("STUDIE-PC", 4, now + 650)
        self.assertEqual(h.flush()[0][0]["x"]["tid"], now + 650)
        # The winner is us.
        h.clock.t += 60
        result = h.receive(packet("vinder", {"vid": 4, "status": "slut", "vinder_pc": "TESTPC", "vinder_navn": "Klippe",
                                             "tid": now + 650, "slut": now + 640}))
        self.assertEqual((result["status"], result["resultat"]["mig"]), ("slut", True))
        self.assertIn("vaeddemaal_vundet", h.progress.notes)
        self.assertIsNone(h.receive(packet("vinder", {"vid": 4, "status": "slut", "vinder_pc": "TESTPC",
                                                      "vinder_navn": "Klippe", "tid": now + 650, "slut": now + 640},
                                           seq=9)))    # once

    def test_it_closes_on_time_and_early(self) -> None:
        h = Harness(self).open()
        now = int(h.wall.t)
        self.open_bet(h)
        h.clock.t += 30
        closed = h.receive(packet("vaeddemaal", {"vid": 4, "fil": "Reklame.mp4", "pct": 50, "start": now - 30,
                                                 "lukker": now}, seq=8))
        self.assertEqual(closed["status"], "lukket")
        with self.assertRaisesRegex(ValueError, "lukket"):
            h.k.guess("STUDIE-PC", 4, now + 600)

    def test_bets_we_do_not_want(self) -> None:
        h = Harness(self, settings={"widget_vaeddemaal": False}).open()
        self.assertIsNone(self.open_bet(h))
        self.assertEqual(h.k.bets(), {"vaeddemaal": []})
        h2 = Harness(self).open()
        # A winner for a bet we never saw, a guess for a bet that is not ours: ignored.
        now = int(h2.wall.t)
        self.assertIsNone(h2.receive(packet("vinder", {"vid": 4, "status": "aflyst", "vinder_pc": None,
                                                       "vinder_navn": None, "tid": None, "slut": None})))
        self.assertIsNone(h2.receive(packet("gaet", {"til": "TESTPC", "vid": 4, "tid": now + 60})))

    def test_never_more_than_a_few_bets(self) -> None:
        h = Harness(self).open()
        now = int(h.wall.t)
        for n in range(kontor.BETS_MAX + 3):
            h.receive(packet("vaeddemaal", {"vid": n, "fil": f"F{n}", "pct": 1, "start": now, "lukker": now + 180},
                             pc=f"PC{n}", id=f"{n:016x}"), (f"192.168.1.{100 + n}", kontor.PORT))
        self.assertEqual(len(h.k.bets()["vaeddemaal"]), kontor.BETS_MAX)

    def test_stale_bets_are_forgotten(self) -> None:
        h = Harness(self).open()
        self.open_bet(h)
        h.wall.t += kontor.BET_STALE_S + 180
        self.assertEqual(h.k.bets(), {"vaeddemaal": []})

    def test_our_own_bet_is_never_pushed_out(self) -> None:
        h = Harness(self).open()
        start_own_bet(h)
        now = int(h.wall.t)
        # Colleague bets from many addresses, all claiming a start far in the future (a packet may say so).
        for n in range(kontor.BETS_MAX + 3):
            h.clock.t += 1
            h.receive(packet("vaeddemaal", {"vid": n, "fil": f"F{n}", "pct": 1, "start": now + 80000,
                                            "lukker": now + 180}, pc=f"PC{n}", id=f"{n:016x}"),
                      (f"192.168.1.{100 + n}", kontor.PORT))
        bets = h.k.bets()["vaeddemaal"]
        self.assertEqual(len(bets), kontor.BETS_MAX)
        self.assertIn(("TESTPC", 1, True), [(b["pc"], b["vid"], b["egen"]) for b in bets])
        # The ones that came first went: PC0…PC3 (by our clock, not by their "start").
        self.assertEqual(sorted(b["vid"] for b in bets if not b["egen"]), list(range(4, 11)))

    def test_at_most_two_bets_from_one_address(self) -> None:
        h = Harness(self).open()
        now = int(h.wall.t)
        for n in range(4):
            h.clock.t += kontor.TYPE_GAPS["vaeddemaal"]
            h.receive(packet("vaeddemaal", {"vid": n, "fil": f"F{n}", "pct": 1, "start": now, "lukker": now + 180},
                             pc=f"PC{n}", id=f"{n:016x}"))
        self.assertEqual([(b["pc"], b["vid"]) for b in h.k.bets()["vaeddemaal"]], [("PC2", 2), ("PC3", 3)])

    def test_only_its_own_pc_may_change_a_bet(self) -> None:
        h = Harness(self).open()
        now = int(h.wall.t)
        self.open_bet(h)
        h.published_all()
        # Another address closes it, or names a winner: ignored.
        other = ("192.168.1.66", kontor.PORT)
        self.assertIsNone(h.receive(packet("vaeddemaal", {"vid": 4, "fil": "Reklame.mp4", "pct": 50, "start": now - 30,
                                                          "lukker": now}, seq=8), other))
        self.assertIsNone(h.receive(packet("vinder", {"vid": 4, "status": "aflyst", "vinder_pc": None,
                                                      "vinder_navn": None, "tid": None, "slut": None}, seq=9), other))
        self.assertEqual(h.k.bets()["vaeddemaal"][0]["status"], "aaben")
        # Its owner closes it at 50 % – within the 30 s a new bet would have to wait: that is no new bet.
        h.clock.t += 5
        closed = h.receive(packet("vaeddemaal", {"vid": 4, "fil": "Reklame.mp4", "pct": 50, "start": now - 30,
                                                 "lukker": now}, seq=10))
        self.assertEqual(closed["status"], "lukket")
        # … and a winner from it counts.
        h.clock.t += 5
        self.assertEqual(h.receive(packet("vinder", {"vid": 4, "status": "aflyst", "vinder_pc": None,
                                                     "vinder_navn": None, "tid": None, "slut": None}, seq=11))["status"],
                         "aflyst")

    def test_a_restart_never_reuses_a_bets_number(self) -> None:
        # Colleagues know a bet by (pc, vid): a new process starts its numbers somewhere new.
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        numbers = set()
        for n in range(3):
            fresh = kontor.Kontor(Config(path=os.path.join(folder.name, f"c{n}.json")), EventBus(),
                                  equipped=dict, stats=achievements.Stats, data_dir=folder.name)
            self.assertTrue(0 <= fresh._vid < 2 ** 30)
            numbers.add(fresh._vid)
        self.assertEqual(len(numbers), 3)


class TwoOfficesTests(unittest.TestCase):
    """Two real offices with real PetProgress and MessageBoard: what one PC sends, the other
    understands (the packets are handed over by hand – nothing touches the LAN)."""

    def office(self, name: str, ip: str, wall: Clock):
        from projektsog import messages
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        cfg = Config(path=os.path.join(folder.name, "config.json"))
        cfg.update({"widget_enabled": True, "widget_pet_name": name})
        bus = EventBus()
        progress = achievements.PetProgress(cfg, bus, path=os.path.join(folder.name, "pet.json"), clock=wall,
                                            today=lambda: datetime.fromtimestamp(wall()).date())
        board = messages.MessageBoard(cfg, bus, shown=lambda: True, sound=lambda: None, ring=lambda on: None,
                                      clock=wall, path=os.path.join(folder.name, "messages.json"))
        sockets: list[FakeSocket] = []
        office = kontor.Kontor(cfg, bus, equipped=progress.equipped, stats=progress.stats, hostname=lambda: name.upper(),
                               hosts=list, sock_factory=lambda: sockets.append(FakeSocket()) or sockets[-1],
                               resolve_ips=lambda host, timeout: [], broadcasts=lambda: ["192.168.1.255"],
                               shown=lambda: True, progress=progress, board=board, working=lambda: True,
                               bell=lambda: None, data_dir=folder.name, clock=Clock(1000.0), wall=wall,
                               rng=random.Random(2))
        office.step()
        sockets[-1].sent.clear()
        return {"k": office, "progress": progress, "board": board, "sockets": sockets, "ip": ip, "bus": bus}

    @staticmethod
    def hand_over(sender: dict, receiver: dict, kind: str) -> list:
        """Every ``kind`` packet ``sender`` sent, delivered to ``receiver``; their events."""
        sender["k"].step()
        out = []
        for data, _address in sender["sockets"][-1].sent:
            if json.loads(data)["type"] == kind:
                out.append(receiver["k"].handle_packet(data, (sender["ip"], kontor.PORT)))
        sender["sockets"][-1].sent.clear()
        return out

    def test_a_trick_or_treat_goes_round(self) -> None:
        wall = Clock(datetime(2026, 10, 27, 9, 0).timestamp())
        studio = self.office("Bobby", "192.168.1.10", wall)
        grafik_pc = self.office("Gurli", "192.168.1.20", wall)
        wall.t = datetime(2026, 10, 27, 15, 31).timestamp()      # (after the moment drawn for today)
        before = studio["progress"].food()["maet"]
        (guest,) = self.hand_over(studio, grafik_pc, "slik")       # Bobby goes trick-or-treating …
        self.assertEqual((guest["type"], guest["pc"], guest["kostume"]),
                         ("slik", "BOBBY", studio["progress"].season_state()["kostume"]))
        self.assertEqual(grafik_pc["k"].treat_or_trick(guest["id"], "slik"), {"ok": True, "ballade": None})
        (answer,) = self.hand_over(grafik_pc, studio, "godbid")     # … and gets a Booster from Gurli
        self.assertEqual(answer, {"type": "godbid", "pc": "GURLI", "navn": "Gurli"})
        self.assertAlmostEqual(studio["progress"].food()["maet"], min(100.0, before + 10), places=0)
        self.assertEqual(grafik_pc["progress"].year_review(2026)["kontor"]["slik_givet"], 1)

    def test_mischief_without_a_hat_is_toilet_paper(self) -> None:
        wall = Clock(datetime(2026, 10, 27, 9, 0).timestamp())
        studio = self.office("Bobby", "192.168.1.10", wall)
        grafik_pc = self.office("Gurli", "192.168.1.20", wall)
        wall.t = datetime(2026, 10, 27, 15, 31).timestamp()
        (guest,) = self.hand_over(studio, grafik_pc, "slik")
        self.assertEqual(grafik_pc["k"].treat_or_trick(guest["id"], "ballade"),
                         {"ok": True, "ballade": {"hat": None, "toiletpapir": True}})
        self.assertTrue(grafik_pc["progress"].season_state()["ballade"]["toiletpapir"])
        (answer,) = self.hand_over(grafik_pc, studio, "ballade")
        self.assertEqual((answer["hat"], answer["toiletpapir"]), (None, True))

    def test_a_post_reaches_the_colleagues_board(self) -> None:
        wall = Clock(A_NORMAL_NOON)
        studio = self.office("Bobby", "192.168.1.10", wall)
        grafik_pc = self.office("Gurli", "192.168.1.20", wall)
        grafik_pc["k"]._hej_at = None
        self.hand_over(grafik_pc, studio, "hej")                    # Gurli's Klippe says hello
        self.assertEqual(studio["k"].send_post("GURLI", "Frokost? 🍔"), {"ok": True})
        (visit,) = self.hand_over(studio, grafik_pc, "post")
        self.assertEqual((visit["type"], visit["pc"]), ("post", "BOBBY"))
        (card,) = grafik_pc["board"].list()["messages"]
        self.assertEqual((card["titel"], card["tekst"], card["opkald"], card["ringer"]),
                         ("✉️ Post fra Bobby (BOBBY)", "Frokost? 🍔", False, False))
        self.assertEqual(card["knapper"], [{"tekst": "Svar", "uri": "projektsog:post/BOBBY"}])
        self.assertEqual(studio["progress"].year_review(2026)["kontor"]["post_sendt"], 1)
        self.assertEqual(grafik_pc["progress"].year_review(2026)["kontor"]["post_faaet"], 1)


class DemoBetTests(unittest.TestCase):
    def test_the_demo_bet_plays_out(self) -> None:
        h = Harness(self)
        view = h.k.bet_demo()["vaeddemaal"]
        now = int(h.wall.t)
        self.assertEqual((view["pc"], view["status"], view["antal_gaet"], view["lukker"], view["demo"]),
                         ("DEMO-PC", "aaben", 1, now + 60, True))
        h.k.guess("DEMO-PC", view["vid"], now + 85)
        self.assertEqual(h.k.bets()["vaeddemaal"][0]["antal_gaet"], 2)
        h.wall.t += 60
        h.k.step()
        self.assertEqual(h.k.bets()["vaeddemaal"][0]["status"], "lukket")
        h.wall.t += 30
        h.k.step()
        bet = h.published("vaeddemaal")[-1]
        self.assertEqual((bet["status"], bet["resultat"]["vinder_pc"], bet["resultat"]["mig"]), ("slut", "TESTPC", True))
        self.assertEqual(h.progress.notes, [])                  # nothing counts, nothing is sent
        self.assertEqual([msg["type"] for msg, _address in h.sent()], ["hej", "hej"])

    def test_without_a_guess_demo_pc_wins(self) -> None:
        h = Harness(self)
        h.k.bet_demo()
        h.wall.t += kontor.DEMO_BET_END_S
        h.k.step()
        bet = h.published("vaeddemaal")[-1]
        self.assertEqual((bet["resultat"]["vinder_pc"], bet["resultat"]["mig"]), ("DEMO-PC", False))

    def test_klippe_must_be_on(self) -> None:
        h = Harness(self, settings={"widget_enabled": False})
        with self.assertRaisesRegex(ValueError, "Slå Klippe til først"):
            h.k.bet_demo()


if __name__ == "__main__":
    unittest.main()
