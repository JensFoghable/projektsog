"""Office Klippes (SPEC §22.3): the packet schema, hostile packets, rate limits, sending from bus
events and the broadcast address – with fake sockets (and one loopback socket for the thread);
nothing is ever sent to the LAN."""

import json
import os
import queue
import random
import socket
import tempfile
import threading
import time
import unittest

from projektsog import achievements, kontor
from projektsog.config import Config
from projektsog.events import EventBus

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


OTHER_ID = "0123456789abcdef"
LAN = ("192.168.1.50", kontor.PORT)
DEFAULT_PYNT = dict(achievements.DEFAULTS)


def packet(**changes) -> dict:
    msg = {"app": "projektsog-klippe", "v": 1, "type": "hej", "pc": "STUDIE-PC", "id": OTHER_ID, "seq": 1,
           "navn": "Klippe", "stage": "pro", "outfit": "color", "pynt": {"hat": "festhat", "haand": "durum"}}
    msg.update(changes)
    return msg


def raw(msg) -> bytes:
    return json.dumps(msg, ensure_ascii=False).encode("utf-8")


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class FakeSocket:
    """Records what Kontor does with its socket; never touches the network."""

    def __init__(self, bind_error: OSError | None = None) -> None:
        self.bind_error = bind_error
        self.options: list[tuple] = []
        self.bound = None
        self.blocking = True
        self.sent: list[tuple[bytes, tuple]] = []
        self.inbox: list = []
        self.closed = False

    def setsockopt(self, level, option, value) -> None:
        self.options.append((level, option, value))

    def bind(self, address) -> None:
        if self.bind_error is not None:
            raise self.bind_error
        self.bound = address

    def setblocking(self, flag: bool) -> None:
        self.blocking = flag

    def sendto(self, data: bytes, address) -> int:
        self.sent.append((data, address))
        return len(data)

    def recvfrom(self, size: int):
        if not self.inbox:
            raise BlockingIOError()
        item = self.inbox.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self) -> None:
        self.closed = True


class Harness:
    def __init__(self, test: unittest.TestCase, *, settings: dict | None = None, hosts=None,
                 bind_error: OSError | None = None, shown=None, stats_hours: float = 120.0) -> None:
        tmp = tempfile.TemporaryDirectory()
        test.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.cfg = Config(path=os.path.join(self.dir, "config.json"))
        self.cfg.update({"widget_enabled": True, **(settings or {})})
        self.bus = EventBus()
        self.events = self.bus.subscribe()
        self.clock = Clock()
        self.sockets: list[FakeSocket] = []
        self.bind_error = bind_error
        self.lookups: list[str] = []
        self.hosts_ips = {"STUDIE-PC": ["192.168.1.20", "fe80::1"], "GRAFIK-PC": ["192.168.1.21"],
                          "NETTET": ["8.8.8.8"], "TESTPC": ["192.168.1.10"]}
        self.equipped = {"farve": "skov", "striber": "klassisk", "hat": "festhat", "briller": "ingen-briller",
                         "mund": "ingen-mund", "haand": "awp", "aura": "ingen-aura"}
        self.stats_hours = stats_hours
        self.look = {"stage": "pro", "outfit": "fusion"}
        self.k = kontor.Kontor(
            self.cfg, self.bus, equipped=lambda: dict(self.equipped), stats=self._stats,
            hostname=lambda: "TESTPC", hosts=lambda: list(hosts if hosts is not None else ["STUDIE-PC", "testpc"]),
            sock_factory=self._socket, resolve_ips=self._resolve, broadcasts=lambda: ["192.168.1.255"],
            look=lambda: self.look, shown=shown, data_dir=self.dir, clock=self.clock, wall=lambda: 1_700_000_000.0,
            rng=random.Random(4))

    def _stats(self):
        return achievements.Stats(total_s=self.stats_hours * 3600)

    def _socket(self) -> FakeSocket:
        sock = FakeSocket(self.bind_error)
        self.sockets.append(sock)
        return sock

    def _resolve(self, host: str, timeout: float = 3.0) -> list[str]:
        self.lookups.append(host)
        return list(self.hosts_ips.get(host.upper(), []))

    @property
    def sock(self) -> FakeSocket:
        return self.sockets[-1]

    def published(self, kind: str) -> list:
        out = []
        while True:
            try:
                event, data, _ts = self.events.get_nowait()
            except queue.Empty:
                return out
            if event == kind:
                out.append(data)

    def sent(self) -> list[tuple[dict, tuple]]:
        return [(json.loads(data.decode("utf-8")), address) for data, address in self.sock.sent]


# --------------------------------------------------------------------------------------
# The schema
# --------------------------------------------------------------------------------------

class PacketTests(unittest.TestCase):
    def test_a_good_packet(self) -> None:
        msg = kontor.parse_packet(raw(packet(type="besoeg", trofae={"kind": "trofae", "id": "durum10"})))
        self.assertEqual(msg, {"type": "besoeg", "pc": "STUDIE-PC", "id": OTHER_ID, "seq": 1, "navn": "Klippe",
                               "stage": "pro", "outfit": "color",
                               "pynt": {**DEFAULT_PYNT, "hat": "festhat", "haand": "durum"},
                               "trofae": {"kind": "trofae", "id": "durum10"}})
        self.assertIsNone(kontor.parse_packet(raw(packet()))["trofae"])
        # trofae belongs to a visit only
        self.assertIsNone(kontor.parse_packet(raw(packet(trofae={"kind": "fund", "id": "awp"})))["trofae"])

    def test_unknown_wardrobe_falls_back_to_the_defaults(self) -> None:
        msg = kontor.parse_packet(raw(packet(pynt={"hat": "krone", "farve": "awp", "vinger": "store",
                                                  "haand": 7, "aura": "lyn"})))
        self.assertEqual(msg["pynt"], {**DEFAULT_PYNT, "aura": "lyn"})

    def test_hostile_packets_are_refused(self) -> None:
        deep = b"[" * 500 + b"]" * 500
        bad = {
            "too large": raw(packet(navn="K")) + b" " * 1100,
            "empty": b"",
            "not utf-8": b"\xff\xfe{}",
            "not json": b"{app: 1}",
            "nan": raw(packet()).replace(b'"seq": 1', b'"seq": NaN'),
            "array": b"[1, 2]",
            "deep": deep,
            "missing key": raw({k: v for k, v in packet().items() if k != "outfit"}),
            "extra key": raw(packet(url="http://x")),
            "other app": raw(packet(app="noget-andet")),
            "v true": raw(packet(v=True)),
            "v 2": raw(packet(v=2)),
            "type": raw(packet(type="kom")),
            "type list": raw(packet(type=["hej"])),
            "pc lower": raw(packet(pc="studie-pc")),
            "pc long": raw(packet(pc="A" * 16)),
            "pc newline": raw(packet(pc="STUDIE\n")),
            "pc dot": raw(packet(pc="STUDIE.PC")),
            "id upper": raw(packet(id=OTHER_ID.upper())),
            "id short": raw(packet(id="abc")),
            "seq bool": raw(packet(seq=True)),
            "seq float": raw(packet(seq=1.5)),
            "seq negative": raw(packet(seq=-1)),
            "seq huge": raw(packet(seq=2 ** 60)),
            "navn empty": raw(packet(navn="")),
            "navn blank": raw(packet(navn="   ")),
            "navn long": raw(packet(navn="K" * 21)),
            "navn control": raw(packet(navn="Kli\x07ppe")),
            "navn newline": raw(packet(navn="Kli\nppe")),
            "navn bidi": raw(packet(navn="Klip‮pe")),
            "navn html is fine but a number is not": raw(packet(navn=5)),
            "stage": raw(packet(stage="god")),
            "outfit": raw(packet(outfit="<script>")),
            "pynt list": raw(packet(pynt=["hat"])),
            "pynt many": raw(packet(pynt={f"x{i}": "y" for i in range(17)})),
            "trofae keys": raw(packet(type="besoeg", trofae={"kind": "trofae", "id": "x", "url": "y"})),
            "trofae kind": raw(packet(type="besoeg", trofae={"kind": "fil", "id": "x"})),
            "trofae path": raw(packet(type="besoeg", trofae={"kind": "trofae", "id": "..\\..\\x"})),
            "trofae str": raw(packet(type="besoeg", trofae="durum10")),
        }
        for name, data in bad.items():
            with self.subTest(name):
                with self.assertRaises(ValueError):
                    kontor.parse_packet(data)
        # Markup in a name is only text – the widget puts it into textContent.
        self.assertEqual(kontor.parse_packet(raw(packet(navn="<b>Klippe</b>")))["navn"], "<b>Klippe</b>")

    def test_trophies_are_named_from_our_own_tables(self) -> None:
        self.assertEqual(kontor.trophy_info({"kind": "trofae", "id": "durum10"}),
                         {"kind": "trofae", "id": "durum10", "name": "Durumkongen", "rarity": "almindelig"})
        self.assertEqual(kontor.trophy_info({"kind": "trofae", "id": "kvartal"})["rarity"], "legendarisk")
        self.assertEqual(kontor.trophy_info({"kind": "fund", "id": "awp"}),
                         {"kind": "fund", "id": "awp", "name": "AWP", "rarity": "legendarisk"})
        for unknown in ({"kind": "trofae", "id": "verdensherre"}, {"kind": "fund", "id": "festhat"}):
            with self.subTest(unknown=unknown):
                self.assertEqual(kontor.trophy_info(unknown), {**unknown, "name": None, "rarity": "almindelig"})
        self.assertIsNone(kontor.trophy_info(None))

    def test_names(self) -> None:
        self.assertEqual(kontor.pc_name("Studio_pc"), "STUDIO-PC")
        self.assertEqual(kontor.pc_name("EN-MEGET-LANG-PC-NAVN"), "EN-MEGET-LANG-P")
        self.assertEqual(kontor.pc_name(""), "PC")
        self.assertEqual(kontor.pet_name("  Bølle\n\x00 "), "Bølle")
        self.assertEqual(kontor.pet_name("\x07"), "Klippe")
        self.assertEqual(kontor.pet_name(None), "Klippe")
        self.assertEqual(len(kontor.pet_name("K" * 40)), 20)

    def test_the_best_news_for_a_visit(self) -> None:
        news = [{"kind": "trofae", "id": "maal1", "rarity": "almindelig"},
                {"kind": "fund", "id": "kosmos", "rarity": "sjælden"},
                {"kind": "trofae", "id": "kvartal", "rarity": "legendarisk"},
                {"kind": "fund", "id": "awp", "rarity": "legendarisk"}]
        self.assertEqual(kontor.best_news(news), {"kind": "trofae", "id": "kvartal"})
        self.assertEqual(kontor.best_news(news[:1]), {"kind": "trofae", "id": "maal1"})
        self.assertIsNone(kontor.best_news([{"kind": "x", "id": "y"}, "z", {"kind": "fund", "id": "A B"}]))
        self.assertIsNone(kontor.best_news(None))

    def test_stage_like_the_widget(self) -> None:
        for hours, stage in ((0, "egg"), (4.9, "egg"), (5, "baby"), (25, "junior"), (99, "junior"),
                             (100, "pro"), (300, "legend"), (5000, "legend")):
            with self.subTest(hours=hours):
                self.assertEqual(kontor.stage_for(hours), stage)
        self.assertEqual(kontor.stage_for(1, hatched=True), "baby")
        self.assertEqual(kontor.stage_for(30, hatched=True), "junior")


class AddressTests(unittest.TestCase):
    def test_lan_addresses(self) -> None:
        for ip in ("192.168.1.5", "10.0.0.1", "172.16.4.4", "169.254.10.10"):
            with self.subTest(ip=ip):
                self.assertTrue(kontor.lan_address(ip))
        for ip in ("8.8.8.8", "127.0.0.1", "0.0.0.0", "0.0.0.7", "255.255.255.255", "224.0.0.1", "100.89.44.61", "fe80::1",
                   "nonsense", None, 7):
            with self.subTest(ip=ip):
                self.assertFalse(kontor.lan_address(ip))

    def test_broadcast_address(self) -> None:
        self.assertEqual(kontor.broadcast_address("192.168.1.131", 24), "192.168.1.255")
        self.assertEqual(kontor.broadcast_address("10.20.30.40", 16), "10.20.255.255")
        self.assertEqual(kontor.broadcast_address("172.16.5.9", 22), "172.16.7.255")
        for ip, prefix in (("192.168.1.2", 31), ("192.168.1.2", 32), ("192.168.1.2", 0), ("x", 24),
                           ("192.168.1.2", True)):
            with self.subTest(ip=ip, prefix=prefix):
                self.assertIsNone(kontor.broadcast_address(ip, prefix))

    def test_directed_broadcasts_of_the_adapters(self) -> None:
        up, down = kontor.IF_OPER_STATUS_UP, 2
        adapters = [("192.168.1.131", 24, 6, up),        # Ethernet
                    ("192.168.1.140", 24, 71, up),       # Wi-Fi on the same LAN: the same broadcast
                    ("10.0.5.2", 16, 6, down),           # unplugged
                    ("127.0.0.1", 8, kontor.IF_TYPE_SOFTWARE_LOOPBACK, up),
                    ("10.8.0.2", 24, kontor.IF_TYPE_TUNNEL, up),
                    ("100.89.44.61", 32, 53, up),        # a VPN's CGNAT address
                    ("85.10.1.2", 24, 6, up),            # a public address
                    ("169.254.3.4", 16, 6, up)]
        self.assertEqual(kontor.directed_broadcasts(adapters), ["192.168.1.255", "169.254.255.255"])
        self.assertEqual(kontor.directed_broadcasts([]), [])

    def test_the_adapter_list_reads_without_errors(self) -> None:
        # Read-only (GetAdaptersAddresses); whatever this PC has, the result has the right shape.
        for ip, prefix, if_type, status in kontor.adapter_addresses():
            self.assertTrue(socket.inet_aton(ip))
            self.assertTrue(0 <= prefix <= 32)
            self.assertIsInstance(if_type, int)
            self.assertIsInstance(status, int)
        self.assertTrue(kontor.lan_broadcasts())


# --------------------------------------------------------------------------------------
# Receiving
# --------------------------------------------------------------------------------------

class ReceiveTests(unittest.TestCase):
    def test_a_visit_becomes_an_event(self) -> None:
        h = Harness(self)
        event = h.k.handle_packet(raw(packet(type="besoeg", trofae={"kind": "trofae", "id": "durum10"})), LAN)
        expected = {"type": "trofae", "pc": "STUDIE-PC", "navn": "Klippe", "stage": "pro", "outfit": "color",
                    "pynt": {**DEFAULT_PYNT, "hat": "festhat", "haand": "durum"},
                    "trofae": {"kind": "trofae", "id": "durum10", "name": "Durumkongen", "rarity": "almindelig"}}
        self.assertEqual(event, expected)
        self.assertEqual(h.published("besoeg"), [expected])
        fest = h.k.handle_packet(raw(packet(type="fest", pc="GRAFIK-PC", id="fedcba9876543210")),
                                 ("192.168.1.21", 47850))
        self.assertEqual((fest["type"], fest["trofae"]), ("fest", None))

    def test_hello_only_tells_who_is_there(self) -> None:
        h = Harness(self)
        self.assertIsNone(h.k.handle_packet(raw(packet(stage="legend")), LAN))
        self.assertEqual(h.published("besoeg"), [])
        self.assertEqual(h.k.state()["peers"],
                         [{"pc": "STUDIE-PC", "navn": "Klippe", "stage": "legend", "sidst": 1_700_000_000.0}])
        h.clock.t += kontor.PEER_FRESH_S + 1                  # not heard from for 3 minutes
        self.assertEqual(h.k.state()["peers"], [])

    def test_our_own_echo_and_strangers_are_ignored(self) -> None:
        h = Harness(self)
        self.assertIsNone(h.k.handle_packet(raw(packet(type="fest", pc="TESTPC")), LAN))
        self.assertIsNone(h.k.handle_packet(raw(packet(type="fest", id=h.k.id)), LAN))
        for address in (("8.8.8.8", 47850), ("127.0.0.1", 47850), ("100.89.44.61", 47850), None, ("x",)):
            with self.subTest(address=address):
                self.assertIsNone(h.k.handle_packet(raw(packet(type="fest")), address))
        self.assertEqual(h.published("besoeg"), [])
        self.assertEqual(h.k.state()["peers"], [])
        self.assertTrue(h.k.handle_packet(raw(packet(type="fest")), ("169.254.7.7", 47850)))   # link-local

    def test_hello_rate_limit_per_sender(self) -> None:
        h = Harness(self)
        h.k.handle_packet(raw(packet()), LAN)
        first = h.k._senders[LAN[0]]["hej"]
        h.clock.t += 5
        h.k.handle_packet(raw(packet(seq=2)), LAN)             # the broadcast copy, or a flood
        self.assertEqual(h.k._senders[LAN[0]]["hej"], first)
        h.clock.t += kontor.HEJ_GAP_S
        h.k.handle_packet(raw(packet(seq=3)), LAN)
        self.assertEqual(h.k._senders[LAN[0]]["hej"], h.clock.t)

    def test_visit_rate_limits(self) -> None:
        h = Harness(self)
        visit = raw(packet(type="besoeg", trofae={"kind": "trofae", "id": "maal1"}))
        self.assertIsNotNone(h.k.handle_packet(visit, LAN))
        h.clock.t += 60
        self.assertIsNone(h.k.handle_packet(visit, LAN))        # one visit per sender per 10 minutes
        h.clock.t += kontor.VISIT_GAP_S
        self.assertIsNotNone(h.k.handle_packet(visit, LAN))
        # … and 6 an hour in all (two from STUDIE-PC so far)
        for n in range(4):
            self.assertIsNotNone(h.k.handle_packet(raw(packet(type="fest", pc=f"PC-{n}")), (f"10.0.0.{n + 1}", 47850)))
        self.assertIsNone(h.k.handle_packet(raw(packet(type="fest", pc="PC-4")), ("10.0.0.5", 47850)))
        self.assertEqual(len(h.published("besoeg")), 6)
        self.assertNotIn("PC-4", [p["pc"] for p in h.k.state()["peers"]])    # a refused visit is not noted
        h.clock.t += 3600
        self.assertIsNotNone(h.k.handle_packet(raw(packet(type="fest", pc="PC-4")), ("10.0.0.5", 47850)))

    def test_the_tables_stay_small(self) -> None:
        h = Harness(self)
        for n in range(kontor.MAX_PEERS + 30):
            h.clock.t += 1
            h.k.handle_packet(raw(packet(pc=f"PC-{n}")), (f"10.1.{n // 250}.{n % 250 + 1}", 47850))
        self.assertEqual(len(h.k._peers), kontor.MAX_PEERS)
        names = [p["pc"] for p in h.k.state()["peers"]]
        self.assertEqual(len(names), kontor.MAX_PEERS)
        self.assertNotIn("PC-0", names)                          # the quietest went first
        self.assertIn(f"PC-{kontor.MAX_PEERS + 29}", names)
        self.assertLessEqual(len(h.k._senders), kontor.MAX_SENDERS)

    def test_one_address_is_one_klippe(self) -> None:
        h = Harness(self)
        colleagues = {f"KOLLEGA-{n}": f"10.3.0.{n + 1}" for n in range(kontor.MAX_PEERS - 1)}
        for pc, ip in colleagues.items():
            h.k.handle_packet(raw(packet(pc=pc)), (ip, kontor.PORT))
        flood = ("192.168.1.66", kontor.PORT)
        for n in range(kontor.MAX_PEERS * 3):                   # made-up PCs as fast as it likes …
            h.k.handle_packet(raw(packet(pc=f"FALSK-{n}", seq=n)), flood)
        self.assertEqual({p["pc"] for p in h.k.state()["peers"]}, set(colleagues) | {"FALSK-0"})
        for n in range(1, 10):                                   # … or slowly enough for the hello gate
            h.clock.t += kontor.HEJ_GAP_S
            h.k.handle_packet(raw(packet(pc=f"LANGSOM-{n}", seq=1000 + n)), flood)
        peers = h.k.state()["peers"]
        self.assertEqual({p["pc"] for p in peers}, set(colleagues) | {"LANGSOM-9"})
        self.assertEqual(len(peers), kontor.MAX_PEERS)           # no colleague was pushed out
        self.assertEqual(len(h.k._peers), kontor.MAX_PEERS)
        # Visits from the same address are the same Klippe too.
        h.k.handle_packet(raw(packet(type="fest", pc="FEST-PC")), flood)
        self.assertEqual({p["pc"] for p in h.k.state()["peers"]}, set(colleagues) | {"FEST-PC"})

    def test_a_pc_on_two_addresses_is_listed_once(self) -> None:
        h = Harness(self)
        h.k.handle_packet(raw(packet(navn="Kabel")), ("192.168.1.20", kontor.PORT))
        h.clock.t += 1
        h.k.handle_packet(raw(packet(navn="Wifi")), ("192.168.1.40", kontor.PORT))
        self.assertEqual(h.k.state()["peers"],
                         [{"pc": "STUDIE-PC", "navn": "Wifi", "stage": "pro", "sidst": 1_700_000_000.0}])

    def test_refused_packets_change_nothing(self) -> None:
        h = Harness(self)
        h.k.handle_packet(raw(packet()), LAN)
        before = h.k.state()["peers"]
        self.assertEqual([(p["navn"], p["stage"]) for p in before], [("Klippe", "pro")])
        h.clock.t += 5                                           # within the hello gate
        h.k.handle_packet(raw(packet(navn="Ondsindet", stage="egg", seq=2)), LAN)
        self.assertEqual(h.k.state()["peers"], before)
        self.assertEqual(h.k._peers[LAN[0]]["seen"], h.clock.t - 5)   # nor keeps it "here" longer
        visit = packet(type="besoeg", navn="Gæst", stage="legend", trofae={"kind": "trofae", "id": "maal1"})
        self.assertIsNotNone(h.k.handle_packet(raw(visit), LAN))     # an accepted visit is noted
        self.assertEqual([(p["navn"], p["stage"]) for p in h.k.state()["peers"]], [("Gæst", "legend")])
        h.clock.t += 60                                          # within the visit gate
        for kind in ("besoeg", "fest"):
            with self.subTest(kind=kind):
                self.assertIsNone(h.k.handle_packet(raw(packet(type=kind, navn="Ondsindet", stage="egg")), LAN))
                self.assertEqual([(p["navn"], p["stage"]) for p in h.k.state()["peers"]], [("Gæst", "legend")])
        h.clock.t += kontor.PEER_FRESH_S - 60 + 1                # refused packets kept nobody "here"
        self.assertEqual(h.k.state()["peers"], [])

    def test_the_socket_is_read_until_empty_within_a_budget(self) -> None:
        h = Harness(self)
        h.k.step()
        h.sock.inbox = [ConnectionResetError(), OSError(None, "too large", None, kontor.WSAEMSGSIZE),
                        (raw(packet(type="fest")), LAN)]
        h.k.step()
        self.assertEqual(len(h.published("besoeg")), 1)
        h.clock.t = 5000.25
        h.sock.inbox = [(raw(packet(seq=n)), (f"10.2.{n // 250}.{n % 250 + 1}", 47850)) for n in range(150)]
        h.k._receive()
        self.assertEqual(len(h.sock.inbox), 150 - kontor.PACKETS_PER_S)     # the rest waits
        self.assertTrue(h.k._throttled())
        h.clock.t = 5001.0
        h.k._receive()
        self.assertEqual(h.sock.inbox, [])

    def test_a_flood_of_errors_counts_against_the_budget(self) -> None:
        errors = {"too large": lambda: OSError(None, "too large", None, kontor.WSAEMSGSIZE),
                  "reset": ConnectionResetError}
        for name, error in errors.items():
            with self.subTest(name):
                h = Harness(self)
                h.k.step()
                h.published("besoeg")
                h.clock.t = 5000.5
                h.sock.inbox = [error() for _n in range(kontor.PACKETS_PER_S + 20)] + [(raw(packet(type="fest")), LAN)]
                h.k._receive()                                    # returns: it does not loop for good
                self.assertEqual(len(h.sock.inbox), 21)           # the rest stays queued
                self.assertTrue(h.k._throttled())                 # (the thread waits instead of select)
                self.assertEqual(h.published("besoeg"), [])
                h.k._receive()
                self.assertEqual(len(h.sock.inbox), 21)           # still this second
                h.clock.t = 5001.0
                h.k._receive()
                self.assertEqual(h.sock.inbox, [])
                self.assertFalse(h.k._throttled())
                self.assertEqual(len(h.published("besoeg")), 1)


# --------------------------------------------------------------------------------------
# Sending
# --------------------------------------------------------------------------------------

class SendTests(unittest.TestCase):
    def test_the_socket_and_the_first_hello(self) -> None:
        h = Harness(self)
        h.k.step()
        sock = h.sock
        self.assertEqual(sock.bound, ("", kontor.PORT))
        self.assertIn((socket.SOL_SOCKET, kontor.SO_EXCLUSIVEADDRUSE, 1), sock.options)
        self.assertIn((socket.SOL_SOCKET, socket.SO_BROADCAST, 1), sock.options)
        self.assertLess(sock.options.index((socket.SOL_SOCKET, kontor.SO_EXCLUSIVEADDRUSE, 1)), len(sock.options))
        self.assertFalse(sock.blocking)
        sent = h.sent()
        # The other PC's LAN address (not its IPv6, not our own PC) and the LAN's broadcast.
        self.assertEqual([address for _msg, address in sent], [("192.168.1.20", kontor.PORT),
                                                               ("192.168.1.255", kontor.PORT)])
        self.assertEqual(h.lookups, ["STUDIE-PC"])
        msg = sent[0][0]
        self.assertEqual(msg, {"app": "projektsog-klippe", "v": 1, "type": "hej", "pc": "TESTPC", "id": h.k.id,
                               "seq": 1, "navn": "Klippe", "stage": "pro", "outfit": "fusion",
                               "pynt": h.equipped})
        self.assertEqual(sent[1][0], msg)                          # one message, the same seq
        self.assertRegex(h.k.id, r"^[0-9a-f]{16}$")
        self.assertEqual(kontor.parse_packet(sock.sent[0][0])["pc"], "TESTPC")   # we pass our own check
        self.assertEqual(h.k.state(), {"enabled": True, "peers": [], "grund": None})

    def test_hello_every_minute_and_lookups_are_kept(self) -> None:
        h = Harness(self, hosts=["STUDIE-PC", "GRAFIK-PC", "NETTET", "UKENDT", 7, "\\\\GRAFIK-PC"])
        h.k.step()
        self.assertEqual(len(h.sock.sent), 3)                    # 2 PCs + the broadcast (not the public IP)
        h.clock.t += 30
        h.k.step()
        self.assertEqual(len(h.sock.sent), 3)
        h.clock.t += 31
        h.k.step()
        self.assertEqual(len(h.sock.sent), 6)
        self.assertEqual(json.loads(h.sock.sent[-1][0])["seq"], 2)
        self.assertEqual(sorted(h.lookups), ["GRAFIK-PC", "NETTET", "STUDIE-PC", "UKENDT"])
        h.clock.t += kontor.HOSTS_FAIL_TTL_S                      # an unknown name is asked again sooner
        h.k.step()
        self.assertEqual(h.lookups.count("UKENDT"), 2)
        self.assertEqual(h.lookups.count("STUDIE-PC"), 1)

    def test_a_trophy_sends_a_visit_and_a_delivery_a_party(self) -> None:
        h = Harness(self)
        self.assertEqual(h.k.on_event("pet_progress", {"nye": [{"kind": "trofae", "id": "maal1"}]}), 0)   # closed
        h.k.step()
        h.sock.sent.clear()
        news = [{"kind": "trofae", "id": "maal1", "rarity": "almindelig"},
                {"kind": "fund", "id": "solbriller", "rarity": "legendarisk"}]
        self.assertEqual(h.k.on_event("pet_progress", {"nye": news, "foerste": False}), 2)
        msg = h.sent()[0][0]
        self.assertEqual((msg["type"], msg["trofae"]), ("besoeg", {"kind": "fund", "id": "solbriller"}))
        h.sock.sent.clear()
        self.assertEqual(h.k.on_event("pet_progress", {"nye": news, "foerste": True}), 0)   # the first summary
        self.assertEqual(h.k.on_event("pet_progress", {"nye": [], "foerste": False}), 0)
        self.assertEqual(h.k.on_event("levering", {"kilde": "render", "demo": True}), 0)   # a demo party stays home
        self.assertEqual(h.sock.sent, [])
        self.assertEqual(h.k.on_event("levering", {"kilde": "fil", "fil": "Film.mp4", "demo": False}), 2)
        msg = h.sent()[0][0]
        self.assertEqual(msg["type"], "fest")
        self.assertNotIn("trofae", msg)

    def test_bus_events_reach_the_office(self) -> None:
        h = Harness(self)
        h.k._queue = h.bus.subscribe()
        h.k.step()
        h.sock.sent.clear()
        h.bus.publish("status", {"sources_online": 3})
        h.bus.publish("pet_progress", {"nye": [{"kind": "trofae", "id": "flow"}], "foerste": False})
        h.bus.publish("levering", {"kilde": "fil", "demo": False})
        h.k.step()
        self.assertEqual([msg["type"] for msg, _address in h.sent()], ["besoeg", "besoeg", "fest", "fest"])

    def test_the_stage_follows_the_hours(self) -> None:
        h = Harness(self, stats_hours=2)
        self.assertEqual(h.k._stage(), "egg")
        h.cfg.update({"widget_hatched": True})
        self.assertEqual(h.k._stage(), "baby")
        h.stats_hours = 30
        self.assertEqual(h.k._stage(), "baby")                   # worked out every 5 minutes
        h.clock.t += kontor.STATS_TTL_S
        self.assertEqual(h.k._stage(), "junior")
        h.look = None                                            # the widget has not told its look
        h.equipped = {"hat": "krone"}
        h.k.step()
        msg = h.sent()[0][0]
        self.assertEqual((msg["outfit"], msg["pynt"]), ("none", DEFAULT_PYNT))


# --------------------------------------------------------------------------------------
# Switching on and off, the first time, the demo
# --------------------------------------------------------------------------------------

class LifecycleTests(unittest.TestCase):
    def test_the_settings_open_and_close_the_socket(self) -> None:
        h = Harness(self, settings={"widget_kontor": False})
        h.k.step()
        self.assertEqual(h.sockets, [])
        self.assertEqual(h.k.state(), {"enabled": False, "peers": [], "grund": None})
        h.cfg.update({"widget_kontor": True})
        h.k.step()
        self.assertEqual(len(h.sockets), 1)
        h.k.handle_packet(raw(packet()), LAN)
        h.cfg.update({"widget_enabled": False})
        h.k.step()
        self.assertTrue(h.sockets[0].closed)
        self.assertEqual(h.k.state(), {"enabled": False, "peers": [], "grund": None})
        h.cfg.update({"widget_enabled": True})
        h.k.step()
        self.assertEqual(len(h.sockets), 2)
        self.assertTrue(h.k.state()["enabled"])

    def test_a_taken_port_switches_the_office_off(self) -> None:
        h = Harness(self, bind_error=OSError(None, "taken", None, kontor.WSAEACCES))
        with self.assertLogs("projektsog.kontor", "WARNING"):
            h.k.step()
        self.assertTrue(h.sock.closed)
        self.assertEqual(h.k.state(), {"enabled": False, "peers": [], "grund": kontor.MSG_PORT_TAKEN})
        h.k.step()
        self.assertEqual(len(h.sockets), 1)                      # not tried again at once …
        h.clock.t += kontor.BIND_RETRY_S
        h.bind_error = None
        h.k.step()
        self.assertEqual(len(h.sockets), 2)                      # … but later
        self.assertEqual(h.k.state()["grund"], None)
        self.assertTrue(h.k.state()["enabled"])

    def test_klippe_tells_about_the_firewall_once(self) -> None:
        shown = [False]
        h = Harness(self, shown=lambda: shown[0])
        h.k.step()
        self.assertEqual(h.published("say"), [])
        shown[0] = True
        h.k.step()
        h.clock.t += kontor.SAY_DELAY_S - 1
        h.k.step()
        self.assertEqual(h.published("say"), [])                 # its page needs a moment
        h.clock.t += 1
        h.k.step()
        self.assertEqual(h.published("say"), [{"tekst": kontor.FIRST_SAY}])
        h.clock.t += 100
        h.k.step()
        self.assertEqual(h.published("say"), [])
        with open(os.path.join(h.dir, "kontor.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), {"netvaerk_sagt": True})
        again = kontor.Kontor(h.cfg, h.bus, equipped=dict, stats=achievements.Stats, hostname=lambda: "TESTPC",
                              hosts=list, sock_factory=FakeSocket, resolve_ips=lambda host, timeout: [],
                              broadcasts=list, data_dir=h.dir, clock=h.clock)
        again.step()
        self.assertEqual(h.published("say"), [])

    def test_a_demo_visit(self) -> None:
        h = Harness(self)
        answer = h.k.demo()
        (event,) = h.published("besoeg")
        self.assertEqual(answer, {"ok": True, "besoeg": event})
        self.assertEqual((event["type"], event["pc"], event["navn"]), ("trofae", kontor.DEMO_PC, kontor.DEMO_NAME))
        self.assertIn(event["stage"], kontor.STAGES)
        self.assertEqual(sorted(event["pynt"]), sorted(achievements.SLOTS))
        self.assertIsInstance(event["trofae"]["name"], str)
        self.assertEqual(h.sockets, [])                          # nothing is sent for a demo
        h.cfg.update({"widget_enabled": False})
        with self.assertRaisesRegex(ValueError, "Slå Klippe til først"):
            h.k.demo()


class LoopbackSocket:
    """A real UDP socket on 127.0.0.1 in place of Kontor's: it binds to a free loopback port
    whatever it is asked, sends nothing, and reports what comes in as coming from a LAN PC."""

    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sent: list = []
        self.bound = threading.Event()

    def setsockopt(self, *args) -> None:
        pass

    def bind(self, address) -> None:
        self.sock.bind(("127.0.0.1", 0))
        self.bound.set()

    def setblocking(self, flag: bool) -> None:
        self.sock.setblocking(flag)

    def fileno(self) -> int:
        return self.sock.fileno()

    def sendto(self, data: bytes, address) -> int:
        self.sent.append((data, address))
        return len(data)

    def recvfrom(self, size: int):
        data, _address = self.sock.recvfrom(size)
        return data, LAN

    def close(self) -> None:
        self.sock.close()


class ThreadTests(unittest.TestCase):
    def test_the_thread_receives_and_follows_the_settings(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = Config(path=os.path.join(tmp.name, "config.json"))
        cfg.update({"widget_enabled": True})
        bus = EventBus()
        events = bus.subscribe()
        sockets: list[LoopbackSocket] = []

        def factory() -> LoopbackSocket:
            sockets.append(LoopbackSocket())
            return sockets[-1]

        k = kontor.Kontor(cfg, bus, equipped=dict, stats=achievements.Stats, hostname=lambda: "TESTPC",
                          hosts=list, sock_factory=factory, resolve_ips=lambda host, timeout: [],
                          broadcasts=lambda: ["192.168.1.255"], data_dir=tmp.name)
        k.start()
        self.addCleanup(k.close)
        self.assertTrue(_wait(lambda: sockets and sockets[0].bound.is_set()))
        port = sockets[0].sock.getsockname()[1]
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            sender.sendto(raw(packet(type="besoeg", trofae={"kind": "trofae", "id": "flow"})), ("127.0.0.1", port))
        seen: list = []

        def visit() -> bool:
            try:
                while True:
                    kind, data, _ts = events.get_nowait()
                    if kind == "besoeg":
                        seen.append(data)
            except queue.Empty:
                return bool(seen)
        self.assertTrue(_wait(visit))
        self.assertEqual(seen[0]["trofae"]["name"], "I flow")
        self.assertTrue(_wait(lambda: sockets[0].sent))            # its hello (kept, never sent)
        started = time.monotonic()
        cfg.update({"widget_kontor": False})
        self.assertTrue(_wait(lambda: sockets[0].sock.fileno() == -1))
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertFalse(k.state()["enabled"])
        k.close()
        self.assertTrue(_wait(lambda: not k._thread.is_alive()))


def _wait(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


if __name__ == "__main__":
    unittest.main()
