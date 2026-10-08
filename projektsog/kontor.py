"""Office Klippes (SPEC §22.3): the Klippes on the office's PCs say hello to each other.

Every Projektsøg with Klippe switched on listens on UDP port ``PORT`` and

* says ``hej`` every minute – to the IPs of the computers under ``hosts`` and to the LAN's
  directed broadcast – so the others know which Klippes are around (``GET /api/kontor``);
* visits the others (``besoeg``) when it earns a trophy or finds something rare, and throws a
  ``fest`` when a delivery is made (the delivery party, ``levering.py``).

A visit that comes in becomes SSE ``besoeg``: the guest Klippe walks into the widget. A packet is
small JSON with exactly the keys of the schema, every value checked; it only ever comes from a
private or link-local address, is rate limited per sender and in all, and nothing in it is ever
run, opened, fetched or put into HTML – names of trophies and items come from our own tables.
"""

from __future__ import annotations

import ctypes
import ipaddress
import json
import logging
import os
import queue
import random
import re
import secrets
import selectors
import socket
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from ctypes import wintypes
from typing import Any

from . import config

log = logging.getLogger(__name__)

PORT = 47850
APP_TAG = "projektsog-klippe"
VERSION = 1
MAX_PACKET = 1024                 # bytes of UTF-8 JSON
TYPES = ("hej", "besoeg", "fest")
STAGES = ("egg", "baby", "junior", "pro", "legend")
STAGE_HOURS = (("legend", 300.0), ("pro", 100.0), ("junior", 25.0), ("baby", 5.0))   # as the widget grows it
OUTFITS = ("none", "color", "fusion", "audio", "deliver")
KEYS = frozenset({"app", "v", "type", "pc", "id", "seq", "navn", "stage", "outfit", "pynt"})
OPTIONAL_KEYS = frozenset({"trofae"})
TROPHY_KINDS = ("trofae", "fund")
NAME_MAX = 20
PYNT_MAX = 16                     # entries in a packet's pynt (there are 7 slots)

HEJ_S = 60.0                      # hello this often (it also keeps the firewall's state open)
PEER_FRESH_S = 180.0              # a Klippe heard from within this long is "here"
HEJ_GAP_S = 15.0                  # per sender: at most one hej this often …
VISIT_GAP_S = 600.0               # … and one visit
VISITS_PER_HOUR = 6               # visits in all
MAX_PEERS = 64
MAX_SENDERS = 256
PACKETS_PER_S = 100               # more than this is left in the socket (the kernel drops it)
HOSTS_TTL_S = 600.0               # a host's IPs are looked up again this much later
HOSTS_FAIL_TTL_S = 120.0
RESOLVE_TIMEOUT_S = 2.0
BIND_RETRY_S = 300.0
STATS_TTL_S = 300.0               # the stage is worked out from all logged time this often
SAY_DELAY_S = 5.0                 # the first-time line waits until Klippe has been shown this long
WAIT_S = 0.5                      # the thread looks at settings and bus events at least this often
STATE_FILE = "kontor.json"

FIRST_SAY = ("Windows spørger måske, om Projektsøg må bruge netværket – sig ja, så kan jeg hilse på "
             "kollegernes Klipper 👋")
MSG_OFF = "Slå Klippe til først"
MSG_PORT_TAKEN = (f"Port {PORT} er optaget – måske kører Projektsøg for en anden bruger på denne pc, "
                  "så hilser Klippe herfra ikke på kollegerne")
DEMO_PC = "DEMO-PC"
DEMO_NAME = "Klippe"

_PC = re.compile(r"[A-Z0-9-]{1,15}")
_ID = re.compile(r"[0-9a-f]{16}")
_THING_ID = re.compile(r"[a-z0-9-]{1,40}")
_NOT_PC = re.compile(r"[^A-Z0-9-]")
_RARITY = {"legendarisk": 3, "sjælden": 2, "almindelig": 1}

WSAEACCES = 10013
WSAEADDRINUSE = 10048
WSAEMSGSIZE = 10040
SO_EXCLUSIVEADDRUSE = getattr(socket, "SO_EXCLUSIVEADDRUSE", ~socket.SO_REUSEADDR)


# --------------------------------------------------------------------------------------
# Klippe's own tables (achievements.py, imported when first needed)
# --------------------------------------------------------------------------------------

def _tables() -> Any:
    from . import achievements
    return achievements


def stage_for(hours: float, hatched: bool = False) -> str:
    """Klippe's stage after ``hours`` of logged work (the widget's own thresholds)."""
    for stage, threshold in STAGE_HOURS:
        if hours >= threshold:
            return stage
    return "baby" if hatched else "egg"


def clean_pynt(value: Any) -> dict[str, str]:
    """``{slot: item}`` for every slot: a known item of that slot, else the slot's default."""
    tables = _tables()
    chosen = value if isinstance(value, dict) else {}
    out: dict[str, str] = {}
    for slot in tables.SLOTS:
        item = tables.ITEMS_BY_ID.get(chosen.get(slot)) if isinstance(chosen.get(slot), str) else None
        out[slot] = item.id if item is not None and item.slot == slot else tables.DEFAULTS[slot]
    return out


def trophy_info(trofae: dict[str, str] | None) -> dict[str, Any] | None:
    """``{kind, id, name, rarity}`` from our own tables; ``name`` None when we do not know it (the
    widget then says a generic line)."""
    if not trofae:
        return None
    tables = _tables()
    kind, thing = trofae["kind"], trofae["id"]
    name, rarity = None, "almindelig"
    if kind == "trofae":
        trophy = tables.TROPHIES_BY_ID.get(thing)
        if trophy is not None:
            reward = tables.ITEMS_BY_ID.get(trophy.reward) if trophy.reward else None
            name, rarity = trophy.name, reward.rarity if reward is not None else "almindelig"
    elif thing in dict(tables.FINDS):
        item = tables.ITEMS_BY_ID[thing]
        name, rarity = item.name, item.rarity
    return {"kind": kind, "id": thing, "name": name, "rarity": rarity}


# --------------------------------------------------------------------------------------
# Packets
# --------------------------------------------------------------------------------------

def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not JSON")


def parse_packet(data: bytes) -> dict[str, Any]:
    """A datagram checked against the schema (ValueError otherwise): ``{type, pc, id, seq, navn,
    stage, outfit, pynt (every slot), trofae: {kind, id} | None}``."""
    if not isinstance(data, (bytes, bytearray)) or not 0 < len(data) <= MAX_PACKET:
        raise ValueError("size")
    try:
        msg = json.loads(bytes(data).decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ValueError("not JSON") from exc
    if not isinstance(msg, dict):
        raise ValueError("not an object")
    keys = set(msg)
    if not KEYS <= keys or not keys <= KEYS | OPTIONAL_KEYS:
        raise ValueError("keys")
    if msg["app"] != APP_TAG or type(msg["v"]) is not int or msg["v"] != VERSION:
        raise ValueError("not a Klippe")
    kind, pc, ident, seq = msg["type"], msg["pc"], msg["id"], msg["seq"]
    if not isinstance(kind, str) or kind not in TYPES:
        raise ValueError("type")
    if not isinstance(pc, str) or not _PC.fullmatch(pc):
        raise ValueError("pc")
    if not isinstance(ident, str) or not _ID.fullmatch(ident):
        raise ValueError("id")
    if type(seq) is not int or not 0 <= seq < 2 ** 53:
        raise ValueError("seq")
    name = msg["navn"]
    if not isinstance(name, str) or not 0 < len(name) <= NAME_MAX or not name.isprintable() or not name.strip():
        raise ValueError("navn")
    stage, outfit, pynt = msg["stage"], msg["outfit"], msg["pynt"]
    if not isinstance(stage, str) or stage not in STAGES:
        raise ValueError("stage")
    if not isinstance(outfit, str) or outfit not in OUTFITS:
        raise ValueError("outfit")
    if not isinstance(pynt, dict) or len(pynt) > PYNT_MAX:
        raise ValueError("pynt")
    trofae = msg.get("trofae")
    if trofae is not None:
        if not isinstance(trofae, dict) or set(trofae) != {"kind", "id"}:
            raise ValueError("trofae")
        if not isinstance(trofae["kind"], str) or trofae["kind"] not in TROPHY_KINDS \
                or not isinstance(trofae["id"], str) or not _THING_ID.fullmatch(trofae["id"]):
            raise ValueError("trofae")
        trofae = {"kind": trofae["kind"], "id": trofae["id"]}
    return {"type": kind, "pc": pc, "id": ident, "seq": seq, "navn": name.strip(), "stage": stage,
            "outfit": outfit, "pynt": clean_pynt(pynt), "trofae": trofae if kind == "besoeg" else None}


def encode_packet(msg: dict[str, Any]) -> bytes:
    data = json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_PACKET:
        raise ValueError(f"packet of {len(data)} bytes")
    return data


def pc_name(hostname: str) -> str:
    """This PC's name as a packet's ``pc`` (``^[A-Z0-9-]{1,15}$``)."""
    name = _NOT_PC.sub("-", (hostname or "").upper())[:15].strip("-")
    return name or "PC"


def pet_name(value: Any) -> str:
    name = "".join(c for c in value if c.isprintable()).strip()[:NAME_MAX].strip() if isinstance(value, str) else ""
    return name or "Klippe"


def best_news(news: Any) -> dict[str, str] | None:
    """The trophy or find of a ``pet_progress`` worth a visit: the rarest (the first of those)."""
    best, best_rank = None, 0
    for entry in news if isinstance(news, list) else ():
        if not isinstance(entry, dict) or entry.get("kind") not in TROPHY_KINDS:
            continue
        thing = entry.get("id")
        if not isinstance(thing, str) or not _THING_ID.fullmatch(thing):
            continue
        rank = _RARITY.get(entry.get("rarity"), 1)
        if rank > best_rank:
            best, best_rank = {"kind": entry["kind"], "id": thing}, rank
    return best


# --------------------------------------------------------------------------------------
# Addresses
# --------------------------------------------------------------------------------------

def lan_address(ip: Any) -> bool:
    """An IPv4 address on a private or link-local network (not loopback, multicast, …)."""
    if not isinstance(ip, str):
        return False
    try:
        address = ipaddress.IPv4Address(ip)
    except (ipaddress.AddressValueError, ValueError, TypeError):
        return False
    if address.is_loopback or address.is_multicast or address.is_unspecified or address.is_reserved \
            or address.packed[0] == 0:                    # "this network" (0.0.0.0/8) is not a sender
        return False
    return address.is_private or address.is_link_local


def broadcast_address(ip: str, prefix: int) -> str | None:
    """The directed broadcast of ``ip/prefix`` (None for host routes and nonsense)."""
    if type(prefix) is not int or not 8 <= prefix <= 30:
        return None
    try:
        network = ipaddress.IPv4Network(f"{ip}/{prefix}", strict=False)
    except ValueError:
        return None
    return str(network.broadcast_address)


IF_TYPE_SOFTWARE_LOOPBACK = 24
IF_TYPE_TUNNEL = 131
IF_OPER_STATUS_UP = 1
IP_DAD_STATE_PREFERRED = 4


def directed_broadcasts(adapters: Iterable[tuple[str, int, int, int]]) -> list[str]:
    """The broadcasts of the LANs this PC is on: ``adapters`` = ``(ip, prefix, if_type,
    oper_status)`` per IPv4 address; adapters that are down, loopback and tunnels are skipped."""
    out: list[str] = []
    for ip, prefix, if_type, status in adapters:
        if status != IF_OPER_STATUS_UP or if_type in (IF_TYPE_SOFTWARE_LOOPBACK, IF_TYPE_TUNNEL):
            continue
        if not lan_address(ip):
            continue
        broadcast = broadcast_address(ip, prefix)
        if broadcast is not None and broadcast not in out:
            out.append(broadcast)
    return out


# GetAdaptersAddresses (iphlpapi): only the fields up to OperStatus are declared – the list is only
# ever walked through pointers into the buffer it was written into.

class _SOCKET_ADDRESS(ctypes.Structure):
    _fields_ = [("lpSockaddr", ctypes.c_void_p), ("iSockaddrLength", ctypes.c_int)]


class _IP_ADAPTER_UNICAST_ADDRESS(ctypes.Structure):
    pass


_IP_ADAPTER_UNICAST_ADDRESS._fields_ = [
    ("Length", wintypes.ULONG), ("Flags", wintypes.DWORD),
    ("Next", ctypes.POINTER(_IP_ADAPTER_UNICAST_ADDRESS)),
    ("Address", _SOCKET_ADDRESS),
    ("PrefixOrigin", ctypes.c_int), ("SuffixOrigin", ctypes.c_int), ("DadState", ctypes.c_int),
    ("ValidLifetime", wintypes.ULONG), ("PreferredLifetime", wintypes.ULONG), ("LeaseLifetime", wintypes.ULONG),
    ("OnLinkPrefixLength", ctypes.c_uint8),
]


class _IP_ADAPTER_ADDRESSES(ctypes.Structure):
    pass


_IP_ADAPTER_ADDRESSES._fields_ = [
    ("Length", wintypes.ULONG), ("IfIndex", wintypes.DWORD),
    ("Next", ctypes.POINTER(_IP_ADAPTER_ADDRESSES)),
    ("AdapterName", ctypes.c_void_p),
    ("FirstUnicastAddress", ctypes.POINTER(_IP_ADAPTER_UNICAST_ADDRESS)),
    ("FirstAnycastAddress", ctypes.c_void_p), ("FirstMulticastAddress", ctypes.c_void_p),
    ("FirstDnsServerAddress", ctypes.c_void_p),
    ("DnsSuffix", ctypes.c_void_p), ("Description", ctypes.c_void_p), ("FriendlyName", ctypes.c_void_p),
    ("PhysicalAddress", ctypes.c_ubyte * 8), ("PhysicalAddressLength", wintypes.ULONG),
    ("Flags", wintypes.ULONG), ("Mtu", wintypes.ULONG), ("IfType", wintypes.ULONG), ("OperStatus", ctypes.c_int),
]

_iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)
_GetAdaptersAddresses = _iphlpapi.GetAdaptersAddresses
_GetAdaptersAddresses.argtypes = [wintypes.ULONG, wintypes.ULONG, ctypes.c_void_p, ctypes.c_void_p,
                                  ctypes.POINTER(wintypes.ULONG)]
_GetAdaptersAddresses.restype = wintypes.ULONG

AF_INET = 2
GAA_FLAG_SKIP_ANYCAST = 0x2
GAA_FLAG_SKIP_MULTICAST = 0x4
GAA_FLAG_SKIP_DNS_SERVER = 0x8
ERROR_SUCCESS = 0
ERROR_BUFFER_OVERFLOW = 111
ERROR_NO_DATA = 232


def adapter_addresses() -> list[tuple[str, int, int, int]]:
    """``(ip, prefix, if_type, oper_status)`` of every preferred IPv4 address on this PC."""
    size = wintypes.ULONG(16 * 1024)
    for _attempt in range(4):
        buffer = ctypes.create_string_buffer(size.value)
        result = _GetAdaptersAddresses(AF_INET, GAA_FLAG_SKIP_ANYCAST | GAA_FLAG_SKIP_MULTICAST
                                       | GAA_FLAG_SKIP_DNS_SERVER, None, buffer, ctypes.byref(size))
        if result == ERROR_BUFFER_OVERFLOW:
            continue
        if result == ERROR_NO_DATA:
            return []
        if result != ERROR_SUCCESS:
            raise ctypes.WinError(result)
        out: list[tuple[str, int, int, int]] = []
        adapter = ctypes.cast(buffer, ctypes.POINTER(_IP_ADAPTER_ADDRESSES))
        while adapter:
            entry = adapter.contents
            unicast = entry.FirstUnicastAddress
            while unicast:
                address = unicast.contents
                sockaddr = address.Address
                if address.DadState == IP_DAD_STATE_PREFERRED and sockaddr.lpSockaddr \
                        and sockaddr.iSockaddrLength >= 8:
                    raw = ctypes.string_at(sockaddr.lpSockaddr, 8)
                    if int.from_bytes(raw[:2], "little") == AF_INET:
                        out.append((socket.inet_ntoa(raw[4:8]), int(address.OnLinkPrefixLength),
                                    int(entry.IfType), int(entry.OperStatus)))
                unicast = address.Next
            adapter = entry.Next
        return out
    raise ctypes.WinError(ERROR_BUFFER_OVERFLOW)


def lan_broadcasts() -> list[str]:
    """The LANs' directed broadcasts, else the limited broadcast."""
    try:
        found = directed_broadcasts(adapter_addresses())
    except OSError as exc:
        log.debug("office Klippes: no adapter list: %s", exc)
        found = []
    return found or ["255.255.255.255"]


def udp_socket() -> socket.socket:
    return socket.socket(socket.AF_INET, socket.SOCK_DGRAM)


# --------------------------------------------------------------------------------------
# The office
# --------------------------------------------------------------------------------------

class Kontor:
    """Listens for and greets the office's other Klippes on one daemon thread."""

    def __init__(self, cfg: Any, bus: Any, *, equipped: Callable[[], dict[str, str]], stats: Callable[[], Any],
                 hostname: Callable[[], str] = config.hostname,
                 hosts: Callable[[], Any] | None = None,
                 sock_factory: Callable[[], Any] | None = None,
                 resolve_ips: Callable[..., list[str]] | None = None,
                 broadcasts: Callable[[], list[str]] = lan_broadcasts,
                 look: Callable[[], dict[str, Any] | None] | None = None,
                 shown: Callable[[], bool] | None = None,
                 data_dir: str | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time,
                 rng: random.Random | None = None) -> None:
        self.cfg = cfg
        self.bus = bus
        self._equipped = equipped
        self._stats = stats
        self._hostname = hostname
        self._hosts = hosts if hosts is not None else (lambda: cfg.get("hosts"))
        self._sock_factory = sock_factory or udp_socket
        self._resolve_ips = resolve_ips
        self._broadcasts = broadcasts
        self._look = look
        self._shown = shown
        self._data_dir = data_dir
        self._clock = clock
        self._wall = wall
        self._rng = rng or random.Random()
        self.pc = pc_name(hostname())
        self.id = secrets.token_hex(8)              # this process (a restart is a new Klippe)
        self._seq = 0
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._queue: queue.Queue | None = None
        self._sock: Any = None
        self._reason: str | None = None              # why the office is off although it is wanted
        self._retry_at: float | None = None
        self._hej_at: float | None = None
        self._peers: dict[str, dict[str, Any]] = {}  # by sender IP: one Klippe per address
        self._senders: dict[str, dict[str, float]] = {}
        self._visits: deque[float] = deque()
        self._host_ips: dict[str, tuple[list[str], float]] = {}
        self._stage_value = "egg"
        self._stage_at: float | None = None
        self._budget_second: float | None = None
        self._budget_used = 0
        self._shown_since: float | None = None
        self._said = bool(self._load_state().get("netvaerk_sagt"))

    # -- lifecycle --------------------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._queue = self.bus.subscribe()
        if callable(getattr(self.cfg, "on_change", None)):
            self.cfg.on_change(self._on_config)
        self._thread = threading.Thread(target=self._run, name="kontor", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._queue is not None:
            self.bus.unsubscribe(self._queue)
        self._close_socket()

    def _on_config(self, settings: Any) -> None:
        self._wake.set()                              # (record and wake only, SPEC §3)

    # -- the API ----------------------------------------------------------------------------
    def state(self) -> dict[str, Any]:
        """``GET /api/kontor``: whether Klippe is in the office, and who else is (last 3 min)."""
        now = self._clock()
        with self._lock:
            latest: dict[str, dict[str, Any]] = {}     # a PC heard on two of its addresses is listed once
            for p in self._peers.values():
                if 0 <= now - p["seen"] <= PEER_FRESH_S and (p["pc"] not in latest
                                                             or p["seen"] >= latest[p["pc"]]["seen"]):
                    latest[p["pc"]] = p
            peers = [{"pc": p["pc"], "navn": p["navn"], "stage": p["stage"], "sidst": p["sidst"]}
                     for p in latest.values()]
            enabled = self._sock is not None
            reason = self._reason if self._wanted() else None
        return {"enabled": enabled, "peers": sorted(peers, key=lambda p: p["pc"]), "grund": reason}

    def demo(self) -> dict[str, Any]:
        """``POST /api/kontor/demo``: a made-up visit (for "👋 Prøv et besøg")."""
        if not self.cfg.get("widget_enabled", False):
            raise ValueError(MSG_OFF)
        tables, rng = _tables(), self._rng
        trophy = rng.choice([t for t in tables.TROPHIES if not t.secret])
        pynt = {slot: tables.DEFAULTS[slot] for slot in tables.SLOTS}
        for slot in rng.sample(list(tables.SLOTS), 3):
            pynt[slot] = rng.choice([item.id for item in tables.ITEMS if item.slot == slot])
        event = {"type": "trofae", "pc": DEMO_PC, "navn": DEMO_NAME, "stage": rng.choice(("junior", "pro", "legend")),
                 "outfit": rng.choice(OUTFITS), "pynt": pynt,
                 "trofae": trophy_info({"kind": "trofae", "id": trophy.id})}
        self.bus.publish("besoeg", event)
        return {"ok": True, "besoeg": event}

    # -- receiving --------------------------------------------------------------------------
    def handle_packet(self, data: bytes, addr: Any) -> dict[str, Any] | None:
        """One datagram from ``addr``; returns the ``besoeg`` event it became (or None)."""
        ip = addr[0] if isinstance(addr, tuple) and addr else None
        if not lan_address(ip):
            return None
        try:
            msg = parse_packet(data)
        except ValueError as exc:
            log.debug("office Klippes: a packet from %s refused (%s)", ip, exc)
            return None
        if msg["pc"] == self.pc or msg["id"] == self.id:
            return None                               # our own hello, back from the broadcast
        now = self._clock()
        with self._lock:
            # The per-sender gates come first: a refused packet is not even noted (it neither
            # keeps a Klippe "here" nor changes its name or stage).
            sender = self._sender(ip, now)
            if msg["type"] == "hej":
                if _within(sender.get("hej"), now, HEJ_GAP_S):
                    return None                       # the broadcast copy, or a flood
                sender["hej"] = now
                self._note_peer(msg, ip, now)
                return None
            if _within(sender.get("visit"), now, VISIT_GAP_S):
                log.debug("office Klippes: %s visits too often", msg["pc"])
                return None
            while self._visits and not _within(self._visits[0], now, 3600.0):
                self._visits.popleft()
            if len(self._visits) >= VISITS_PER_HOUR:
                log.debug("office Klippes: enough visits this hour")
                return None
            sender["visit"] = now
            self._visits.append(now)
            self._note_peer(msg, ip, now)
        event = {"type": "fest" if msg["type"] == "fest" else "trofae", "pc": msg["pc"], "navn": msg["navn"],
                 "stage": msg["stage"], "outfit": msg["outfit"], "pynt": msg["pynt"],
                 "trofae": trophy_info(msg["trofae"])}
        log.info("office Klippes: a visit from %s (%s)", msg["pc"], event["type"])
        self.bus.publish("besoeg", event)
        return event

    def _note_peer(self, msg: dict[str, Any], ip: str, now: float) -> None:
        """The Klippe at ``ip`` (an accepted hello or visit). Kept by address, so one PC on the LAN
        is one entry whatever names it makes up – it cannot push the colleagues out of the table."""
        if ip not in self._peers and len(self._peers) >= MAX_PEERS:
            del self._peers[min(self._peers, key=lambda key: self._peers[key]["seen"])]
        self._peers[ip] = {"pc": msg["pc"], "navn": msg["navn"], "stage": msg["stage"], "ip": ip,
                           "seen": now, "sidst": round(self._wall(), 1)}

    def _sender(self, ip: str, now: float) -> dict[str, float]:
        sender = self._senders.get(ip)
        if sender is None:
            if len(self._senders) >= MAX_SENDERS:
                for key in [k for k, v in self._senders.items()
                            if not _within(max(v.values(), default=None), now, VISIT_GAP_S)]:
                    del self._senders[key]
                if len(self._senders) >= MAX_SENDERS:     # all busy: forget the quietest
                    del self._senders[min(self._senders, key=lambda k: max(self._senders[k].values(), default=0.0))]
            sender = self._senders[ip] = {}
        return sender

    def _receive(self) -> None:
        sock = self._sock
        while sock is not None:
            now = self._clock()
            second = float(int(now))
            if self._budget_second != second:
                self._budget_second, self._budget_used = second, 0
            if self._budget_used >= PACKETS_PER_S:
                return                                  # the rest waits (or is dropped by the kernel)
            # Every datagram read counts against the budget, an error as much as a packet – else
            # a flood of resets or oversized datagrams would keep the thread here for good.
            try:
                data, addr = sock.recvfrom(MAX_PACKET + 1)
            except (BlockingIOError, InterruptedError, TimeoutError):
                return
            except ConnectionResetError:
                self._budget_used += 1                  # ICMP "port unreachable" after a hello to a PC without us
                continue
            except OSError as exc:
                if getattr(exc, "winerror", None) == WSAEMSGSIZE:
                    self._budget_used += 1              # too large: discarded
                    continue
                log.debug("office Klippes: receiving failed: %s", exc)
                return
            self._budget_used += 1
            self.handle_packet(data, addr)

    def _throttled(self) -> bool:
        return self._budget_used >= PACKETS_PER_S and self._budget_second == float(int(self._clock()))

    # -- sending ----------------------------------------------------------------------------
    def _message(self, kind: str, trofae: dict[str, str] | None = None) -> bytes:
        self._seq += 1
        msg: dict[str, Any] = {"app": APP_TAG, "v": VERSION, "type": kind, "pc": self.pc, "id": self.id,
                               "seq": self._seq, "navn": pet_name(self.cfg.get("widget_pet_name", "Klippe")),
                               "stage": self._stage(), "outfit": self._outfit(), "pynt": self._pynt()}
        if trofae is not None:
            msg["trofae"] = trofae
        return encode_packet(msg)

    def _send(self, data: bytes) -> int:
        """``data`` to every target; returns how many it went to."""
        sock = self._sock
        if sock is None:
            return 0
        sent = 0
        for ip in self._targets():
            try:
                sock.sendto(data, (ip, PORT))
                sent += 1
            except OSError as exc:
                log.debug("office Klippes: sending to %s failed: %s", ip, exc)
        return sent

    def send(self, kind: str, trofae: dict[str, str] | None = None) -> int:
        try:
            return self._send(self._message(kind, trofae))
        except ValueError as exc:
            log.warning("office Klippes: no %s: %s", kind, exc)
            return 0

    def _targets(self) -> list[str]:
        targets: list[str] = []
        for ip in self._host_targets() + list(self._broadcasts()):
            if ip not in targets:
                targets.append(ip)
        return targets

    def _host_targets(self) -> list[str]:
        """The IPs of the computers under ``hosts`` (looked up with a timeout, kept a while)."""
        hosts = self._hosts()
        own = self._hostname().casefold()
        now = self._clock()
        out: list[str] = []
        for host in hosts if isinstance(hosts, list) else ():
            if not isinstance(host, str):
                continue
            name = host.strip().lstrip("\\").casefold()
            if not name or name == own:
                continue
            cached = self._host_ips.get(name)
            if cached is None or not 0 <= now - cached[1] < (HOSTS_TTL_S if cached[0] else HOSTS_FAIL_TTL_S):
                cached = (self._lookup(host.strip()), now)
                self._host_ips[name] = cached
            out.extend(ip for ip in cached[0] if lan_address(ip))
        return out

    def _lookup(self, host: str) -> list[str]:
        resolve = self._resolve_ips
        if resolve is None:
            from . import winfs
            resolve = winfs.resolve_host_ips
        try:
            return [ip for ip in resolve(host, timeout=RESOLVE_TIMEOUT_S) if isinstance(ip, str)]
        except Exception:
            log.debug("office Klippes: looking up %s failed", host, exc_info=True)
            return []

    def _stage(self) -> str:
        now = self._clock()
        if self._stage_at is None or not 0 <= now - self._stage_at < STATS_TTL_S:
            self._stage_at = now
            try:
                st = self._stats()
                hours = float(getattr(st, "total_s", 0.0) or 0.0) / 3600.0
                hatched = bool(getattr(st, "hatched", False))
                self._stage_value = stage_for(hours, hatched)
            except Exception:
                log.debug("office Klippes: no stats for the stage", exc_info=True)
        stage = self._stage_value
        if stage == "egg" and self.cfg.get("widget_hatched", False):
            stage = "baby"
        return stage

    def _outfit(self) -> str:
        try:
            look = self._look() if self._look is not None else None
        except Exception:
            look = None
        outfit = look.get("outfit") if isinstance(look, dict) else None
        return outfit if isinstance(outfit, str) and outfit in OUTFITS else "none"

    def _pynt(self) -> dict[str, str]:
        try:
            return clean_pynt(self._equipped())
        except Exception:
            log.debug("office Klippes: no wardrobe", exc_info=True)
            return clean_pynt({})

    # -- bus events -------------------------------------------------------------------------
    def on_event(self, kind: str, data: Any) -> int:
        """A bus event: a new trophy or find → ``besoeg``; a real delivery → ``fest``."""
        if self._sock is None or not isinstance(data, dict):
            return 0
        if kind == "pet_progress" and not data.get("foerste"):
            news = best_news(data.get("nye"))
            if news is not None:
                return self.send("besoeg", news)
        elif kind == "levering" and not data.get("demo"):
            return self.send("fest")
        return 0

    def _drain_bus(self) -> None:
        q = self._queue
        while q is not None:
            try:
                kind, data, _ts = q.get_nowait()
            except queue.Empty:
                return
            if kind in ("pet_progress", "levering"):
                try:
                    self.on_event(kind, data)
                except Exception:
                    log.exception("office Klippes: %s failed", kind)

    # -- the thread -------------------------------------------------------------------------
    def _wanted(self) -> bool:
        return bool(self.cfg.get("widget_enabled", False) and self.cfg.get("widget_kontor", True))

    def step(self) -> None:
        """One round: follow the settings, act on bus events, read what came in, say hello."""
        now = self._clock()
        wanted = self._wanted() and not self._stop.is_set()
        if not wanted:
            self._retry_at = None                         # switched on again: try at once
            if self._sock is not None:
                self._close_socket()
                log.info("office Klippes: switched off")
        elif self._sock is None and (self._retry_at is None or now >= self._retry_at):
            self._open(now)
        self._drain_bus()
        if self._sock is None:
            return
        self._receive()
        if self._hej_at is None or not 0 <= now - self._hej_at < HEJ_S:
            self._hej_at = now
            self.send("hej")
        self._maybe_say(now)

    def _open(self, now: float) -> None:
        try:
            sock = self._sock_factory()
            try:
                sock.setsockopt(socket.SOL_SOCKET, SO_EXCLUSIVEADDRUSE, 1)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                sock.bind(("", PORT))
                sock.setblocking(False)
            except BaseException:
                sock.close()
                raise
        except OSError as exc:
            self._retry_at = now + BIND_RETRY_S
            taken = getattr(exc, "winerror", None) in (WSAEACCES, WSAEADDRINUSE)
            reason = MSG_PORT_TAKEN if taken else f"Netværket kunne ikke åbnes: {exc}"
            if reason != self._reason:
                log.warning("office Klippes: UDP %d could not be opened: %s", PORT, exc)
            self._reason = reason
            return
        with self._lock:
            self._sock = sock
        self._reason = None
        self._retry_at = None
        self._hej_at = None                               # hello at once
        log.info("office Klippes: listening on UDP %d as %s", PORT, self.pc)

    def _close_socket(self) -> None:
        with self._lock:
            sock, self._sock = self._sock, None
            self._peers.clear()
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def _run(self) -> None:
        selector = selectors.DefaultSelector()
        registered: Any = None
        try:
            while not self._stop.is_set():
                try:
                    self.step()
                except Exception:
                    log.exception("office Klippes: a round failed")
                sock = self._sock
                if sock is not registered:
                    if registered is not None:
                        try:
                            selector.unregister(registered)
                        except (KeyError, ValueError, OSError):
                            pass
                    registered = None
                    if sock is not None:
                        try:
                            selector.register(sock, selectors.EVENT_READ)
                            registered = sock
                        except (ValueError, OSError, TypeError, AttributeError):
                            pass
                if self._stop.is_set():
                    break
                if registered is not None and not self._throttled():
                    try:
                        selector.select(WAIT_S)
                    except (OSError, ValueError):
                        self._stop.wait(0.2)              # the socket was closed under us
                else:
                    self._wake.wait(WAIT_S if self._sock is not None else 1.0)
                    self._wake.clear()
        finally:
            selector.close()
            self._close_socket()

    # -- the first time ---------------------------------------------------------------------
    def _maybe_say(self, now: float) -> None:
        """Once ever: Klippe tells that Windows may ask about the network (the firewall)."""
        if self._said:
            return
        if self._shown is not None:
            try:
                shown = bool(self._shown())
            except Exception:
                shown = False
            if not shown:
                self._shown_since = None
                return
            if self._shown_since is None:
                self._shown_since = now
            if now - self._shown_since < SAY_DELAY_S:     # (its page has its event stream by then)
                return
        self._said = True
        self.bus.publish("say", {"tekst": FIRST_SAY})
        self._save_state({"netvaerk_sagt": True})

    def _state_path(self) -> str:
        return os.path.join(self._data_dir or config.app_dir(), STATE_FILE)

    def _load_state(self) -> dict[str, Any]:
        try:
            with open(self._state_path(), encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            log.debug("office Klippes: %s", exc)
            return {}
        return data if isinstance(data, dict) else {}

    def _save_state(self, data: dict[str, Any]) -> None:
        path = self._state_path()
        temp = f"{path}.{os.getpid()}.tmp"
        try:
            with open(temp, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(temp, path)
        except OSError as exc:
            log.warning("office Klippes: could not save %s: %s", path, exc)


def _within(at: float | None, now: float, seconds: float) -> bool:
    return at is not None and 0 <= now - at < seconds
