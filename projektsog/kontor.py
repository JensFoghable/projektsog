"""Office Klippes (SPEC §22.3, §23.7): the Klippes on the office's PCs say hello to each other.

Every Projektsøg with Klippe switched on listens on UDP port ``PORT`` and

* says ``hej`` every minute – to the IPs of the computers under ``hosts`` and to the LAN's
  directed broadcast – so the others know which Klippes are around (``GET /api/kontor``);
* visits the others (``besoeg``) when it earns a trophy or finds something rare, and throws a
  ``fest`` when a delivery is made (the delivery party, ``levering.py``);
* and has some fun with them (SPEC §23.7): it rings the lunch bell (``frokost``), carries a little
  post to one colleague (``post``), goes trick-or-treating in Halloween week (``slik`` – answered
  with a Booster, ``godbid``, or a prank, ``ballade``), lets the office bet on when a long render
  is done (``vaeddemaal``, ``gaet``, ``vinder``), tells a new record in the render game
  (``rekord``) and shares its year (``aar``).

A visit that comes in becomes SSE ``besoeg``: the guest Klippe walks into the widget. A packet is
small JSON with exactly the keys of the schema, every value checked; it only ever comes from a
private or link-local address, is rate limited per sender (and type) and in all, and nothing in it
is ever run, opened, fetched or put into HTML – names of trophies and items come from our own
tables. A packet meant for one PC (``x.til``) is sent to that PC's address only and ignored by any
other. What the API asks to send is built at once (so a mistake is the caller's 400) and goes out
from the office's own thread; no project name ever leaves the PC.
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
from datetime import date, datetime, timedelta
from typing import Any

from . import config

log = logging.getLogger(__name__)

PORT = 47850
APP_TAG = "projektsog-klippe"
VERSION = 1
MAX_PACKET = 1024                 # bytes of UTF-8 JSON
TYPES = ("hej", "besoeg", "fest", "slik", "godbid", "ballade", "frokost", "post", "vaeddemaal", "gaet",
         "vinder", "rekord", "aar")
STAGES = ("egg", "baby", "junior", "pro", "legend")
STAGE_HOURS = (("legend", 300.0), ("pro", 100.0), ("junior", 25.0), ("baby", 5.0))   # as the widget grows it
OUTFITS = ("none", "color", "fusion", "audio", "deliver")
KEYS = frozenset({"app", "v", "type", "pc", "id", "seq", "navn", "stage", "outfit", "pynt"})
OPTIONAL_KEYS = frozenset({"trofae", "x"})
TROPHY_KINDS = ("trofae", "fund")
NAME_MAX = 20
PYNT_MAX = 16                     # entries in a packet's pynt (there are 8 slots)

# The fun in the office (SPEC §23.7): each new type carries ``x`` with exactly these keys (the
# others carry no ``x``); the keys listed in X_NULLABLE may be null.
X_KEYS: dict[str, tuple[str, ...]] = {
    "slik": ("kostume",),
    "godbid": ("til",),
    "ballade": ("til", "hat"),
    "post": ("til", "tekst"),
    "vaeddemaal": ("vid", "fil", "pct", "start", "lukker"),
    "gaet": ("til", "vid", "tid"),
    "vinder": ("vid", "status", "vinder_pc", "vinder_navn", "tid", "slut"),
    "rekord": ("score",),
    "aar": ("aar", "timer", "side", "projekter", "durum", "kort", "leveringer", "trofaeer"),
}
X_NULLABLE: dict[str, frozenset[str]] = {
    "ballade": frozenset({"hat"}),
    "vinder": frozenset({"vinder_pc", "vinder_navn", "tid", "slut"}),
    "aar": frozenset({"side"}),
}
KOSTUMER = ("vampyr", "spoegelse", "skelet", "heks")    # achievements.KOSTUMER (SPEC §23.1)
POST_MAX = 120                    # characters of a post
FIL_MAX = 40                      # characters of a render's name in a bet
VID_MAX = 2 ** 31
SCORE_MAX = 10_000_000
YEAR_NUMBER_MAX = 1_000_000
YEAR_FIRST, YEAR_LAST = 2026, 2100
EPOCH_SLACK_S = 86400.0           # a time in a packet lies within a day of our clock
YEAR_NUMBERS = ("timer", "projekter", "durum", "kort", "leveringer", "trofaeer")
BUCKET_NAMES = {"edit": "Edit", "cut": "Cut", "color": "Color", "fusion": "Fusion", "fairlight": "Fairlight",
                "deliver": "Deliver", "media": "Media", "photo": "Photo", "musik": "musik og lyd",
                "ai": "AI-video og -billeder"}
# Per sender address and type: at most one packet this often (seconds).
TYPE_GAPS = {"slik": 600.0, "godbid": 5.0, "ballade": 5.0, "frokost": 300.0, "post": 15.0, "vaeddemaal": 30.0,
             "gaet": 2.0, "vinder": 10.0, "rekord": 30.0, "aar": 600.0}
VISIT_TYPES = frozenset({"besoeg", "fest", "slik", "aar"})    # these share the visits' budget
POSTS_PER_HOUR = 30               # posts in all
OUTBOX_MAX = 64                   # packets waiting for the office's thread

LUNCH_GAP_S = 300.0               # our own lunch bell, at most this often
POST_GAP_S = 15.0                 # our own posts
SHARE_GAP_S = 600.0               # sharing our year
POST_TTL_S = 8 * 3600             # a post card stays this long
YEAR_CARD_TTL_S = 2 * 86400       # a colleague's year card
SLIK_FROM_H, SLIK_TO_H = 10.0, 15.5   # trick or treat: a moment between these hours is drawn each day
SLIK_WAIT_S = 25                  # the guest waits this long for its Booster (the widget counts down)
SLIK_PENDING_S = 60.0             # an answer is taken this long after the guest came
SLIK_ANSWER_S = 300.0             # our own trick or treat: answers are taken this long
SLIK_PENDING_MAX = 16
TREAT_POINTS = 10.0               # a Booster from a colleague
BET_MIN_ETA_S = 180               # a render with at least this much left …
BET_AFTER_S = 20.0                # … that has run this long opens a bet
BET_OPEN_S = 180                  # guesses are taken this long (or until 50 %)
BET_CLOSE_PCT = 50
BET_GUESSES_MAX = 16
BET_LOST_S = 60.0                 # a render that ended without a word: the bet is off after this
BET_KEEP_S = 120.0                # an ended bet is still listed this long
BET_STALE_S = 3 * 3600.0          # a colleague's bet we never heard the end of
BETS_MAX = 8
BETS_PER_SENDER = 2               # colleague bets kept from one address
BET_UPDATE_GAP_S = 2.0            # what became of a known bet, from its owner: at most this often
GUESS_GAP_S = 2.5                 # our own guesses on one bet (the owner takes one per 2 s)
DEMO_BET_CLOSE_S = 60
DEMO_BET_END_S = 90
OFFICE_RECORD_MIN = 100           # a game's best below this is not told to the office

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
MSG_OFFICE_OFF = "Kontor-Klipperne er slået fra"
MSG_OFFICE_NOT_READY = "Kontor-Klipperne er ikke klar endnu – prøv igen om lidt"
MSG_LUNCH_OFF = "Frokostklokken er slået fra"
MSG_LUNCH_AGAIN = "Du har lige ringet – vent lidt 🔔"
MSG_POST_OFF = "Klippe-post er slået fra"
MSG_POST_EMPTY = "Skriv en besked"
MSG_POST_LONG = f"Beskeden er for lang (højst {POST_MAX} tegn)"
MSG_POST_WHO = "Vælg en kollega"
MSG_POST_AWAY = "{pc} er ikke på lige nu"
MSG_POST_AGAIN = "Klippe er stadig ude med posten – vent lidt ✉️"
MSG_GUEST_GONE = "Gæsten er gået igen"
MSG_BETS_OFF = "Render-væddemål er slået fra"
MSG_BET_CLOSED = "Væddemålet er lukket"
MSG_BET_TIME = "Gæt et tidspunkt efter nu"
MSG_GUESS_AGAIN = "Vent lidt, før du gætter igen"
MSG_YEAR_OFF = "Klippet er slået fra"
MSG_YEAR_AGAIN = "Du har lige delt den – vent lidt"
DEMO_PC = "DEMO-PC"
DEMO_NAME = "Klippe"
DEMO_POST = "Kan du lige kigge på det her? 👀"
DEMO_FIL = "Demo_render.mp4"

_PC = re.compile(r"[A-Z0-9-]{1,15}")
_ID = re.compile(r"[0-9a-f]{16}")
_THING_ID = re.compile(r"[a-z0-9-]{1,40}")
_BUCKET = re.compile(r"[a-z]{1,16}")
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


def costumes() -> tuple[str, ...]:
    """The Halloween costumes (achievements.KOSTUMER, SPEC §23.1)."""
    found = getattr(_tables(), "KOSTUMER", None)
    return tuple(found) if isinstance(found, (tuple, list)) and found else KOSTUMER


def halloween_week(day: date) -> bool:
    """Monday–Sunday of the ISO week that holds 31 October (achievements.halloween_week)."""
    found = getattr(_tables(), "halloween_week", None)
    if callable(found):
        return bool(found(day))
    halloween = date(day.year, 10, 31)
    monday = halloween - timedelta(days=halloween.weekday())
    return monday <= day <= monday + timedelta(days=6)


def known_hat(value: Any) -> str | None:
    """A hat of our own wardrobe (not "ingen"), else None – a newer PC's hat is no hat here."""
    if not isinstance(value, str) or not _THING_ID.fullmatch(value):
        return None
    item = _tables().ITEMS_BY_ID.get(value)
    if item is None or item.slot != "hat" or getattr(item, "default", False):
        return None
    return item.id


def item_name(item_id: Any) -> str | None:
    item = _tables().ITEMS_BY_ID.get(item_id) if isinstance(item_id, str) else None
    return item.name if item is not None else None


# --------------------------------------------------------------------------------------
# Packets
# --------------------------------------------------------------------------------------

def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not JSON")


def _printable(value: Any, limit: int, *, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit or not value.isprintable():
        raise ValueError("text")
    if not empty and not value.strip():
        raise ValueError("text")
    return value.strip()


def _int_in(value: Any, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError("number")
    return value


def _epoch(value: Any, now: float) -> int:
    # (int against int: a huge number from a packet must not overflow a float)
    if type(value) is not int or abs(value - int(now)) > EPOCH_SLACK_S:
        raise ValueError("time")
    return value


def _x_value(key: str, value: Any, now: float) -> Any:
    """One value of an ``x``: checked (ValueError) and cleaned."""
    if key in ("til", "vinder_pc"):
        if not isinstance(value, str) or not _PC.fullmatch(value):
            raise ValueError(key)
        return value
    if key == "kostume":
        if not isinstance(value, str) or value not in costumes():
            raise ValueError(key)
        return value
    if key == "hat":
        if not isinstance(value, str) or not _THING_ID.fullmatch(value):
            raise ValueError(key)
        return known_hat(value)
    if key == "tekst":
        return _printable(value, POST_MAX)
    if key == "fil":
        return _printable(value, FIL_MAX, empty=True)
    if key == "vinder_navn":
        return _printable(value, NAME_MAX)
    if key == "vid":
        return _int_in(value, 0, VID_MAX - 1)
    if key == "pct":
        return _int_in(value, 0, 100)
    if key in ("start", "lukker", "tid", "slut"):
        return _epoch(value, now)
    if key == "status":
        if value not in ("slut", "aflyst"):
            raise ValueError(key)
        return value
    if key == "score":
        return _int_in(value, 0, SCORE_MAX)
    if key == "aar":
        return _int_in(value, YEAR_FIRST, YEAR_LAST)
    if key in YEAR_NUMBERS:
        return _int_in(value, 0, YEAR_NUMBER_MAX)
    if key == "side":
        if not isinstance(value, str) or not _BUCKET.fullmatch(value):
            raise ValueError(key)
        return value
    raise ValueError(key)


def clean_x(kind: str, x: Any, now: float | None = None) -> dict[str, Any] | None:
    """The ``x`` of a ``kind`` packet, every value checked (ValueError otherwise); None for the
    types without one (which must not carry it)."""
    keys = X_KEYS.get(kind)
    if keys is None:
        if x is not None:
            raise ValueError("x")
        return None
    if not isinstance(x, dict) or set(x) != set(keys):
        raise ValueError("x")
    now = time.time() if now is None else now
    nullable = X_NULLABLE.get(kind, frozenset())
    out: dict[str, Any] = {}
    for key in keys:
        value = x[key]
        if value is None and key in nullable:
            out[key] = None
        else:
            try:
                out[key] = _x_value(key, value, now)
            except ValueError:
                raise ValueError(f"x.{key}") from None
    return out


def parse_packet(data: bytes, now: float | None = None) -> dict[str, Any]:
    """A datagram checked against the schema (ValueError otherwise): ``{type, pc, id, seq, navn,
    stage, outfit, pynt (every slot), trofae: {kind, id} | None, x: {…} | None}`` – ``now`` (our
    wall clock) for the times in ``x``."""
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
    if ("x" in msg) != (kind in X_KEYS):
        raise ValueError("x")
    x = clean_x(kind, msg.get("x"), now)
    return {"type": kind, "pc": pc, "id": ident, "seq": seq, "navn": name.strip(), "stage": stage,
            "outfit": outfit, "pynt": clean_pynt(pynt), "trofae": trofae if kind == "besoeg" else None, "x": x}


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


def clean_text(value: Any) -> str:
    """A post as typed: whitespace (newlines too) folded to single spaces, unprintable characters
    dropped."""
    if not isinstance(value, str):
        return ""
    return " ".join("".join(c if c.isprintable() or c.isspace() else "" for c in value).split())


def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        return 0
    return max(0, min(YEAR_NUMBER_MAX, int(round(value))))


def year_summary(review: Any) -> dict[str, Any]:
    """The ``x`` of an ``aar`` packet from ``PetProgress.year_review()`` (SPEC §23.11): numbers
    and the top page only – never a project's name."""
    r = review if isinstance(review, dict) else {}
    pages = r.get("sider") if isinstance(r.get("sider"), list) else []
    top = pages[0].get("side") if pages and isinstance(pages[0], dict) else None
    food = r.get("mad") if isinstance(r.get("mad"), dict) else {}
    eaten = food.get("spist") if isinstance(food.get("spist"), dict) else {}
    cards = r.get("kort") if isinstance(r.get("kort"), dict) else {}
    pet = r.get("klippe") if isinstance(r.get("klippe"), dict) else {}
    year = r.get("aar")
    return {"aar": year if type(year) is int else date.today().year, "timer": _count(r.get("timer")),
            "side": top if isinstance(top, str) and _BUCKET.fullmatch(top) else None,
            "projekter": _count(r.get("projekter")), "durum": _count(eaten.get("durum")),
            "kort": _count(cards.get("antal")), "leveringer": _count(r.get("leveringer")),
            "trofaeer": _count(pet.get("trofaeer"))}


def _number_da(value: int) -> str:
    return f"{value:,}".replace(",", ".")


def year_line(x: dict[str, Any]) -> str:
    """One line of a colleague's year: "1.234 timer · mest i Color · 23 projekter · 57 durum …"."""
    parts = [f"{_number_da(x['timer'])} timer"]
    if x.get("side"):
        parts.append(f"mest i {BUCKET_NAMES.get(x['side'], x['side'].title())}")
    for key, one, many in (("projekter", "projekt", "projekter"), ("durum", "durum", "durum"),
                           ("kort", "kort", "kort"), ("leveringer", "levering", "leveringer"),
                           ("trofaeer", "trofæ", "trofæer")):
        if x.get(key):
            parts.append(f"{_number_da(x[key])} {one if x[key] == 1 else many}")
    return " · ".join(parts)


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
# Render bets (SPEC §23.7)
# --------------------------------------------------------------------------------------

class _Bet:
    """One render bet – ours (``egen``: our render, we keep the guesses and name the winner), a
    colleague's (we only know our own guess), or the demo's."""

    def __init__(self, pc: str, vid: int, navn: str, fil: str, pct: int, start: int, lukker: int, *,
                 egen: bool = False, demo: bool = False, ip: str | None = None) -> None:
        self.pc, self.vid, self.navn, self.fil = pc, vid, navn, fil
        self.pct, self.start, self.lukker = pct, start, lukker
        self.egen, self.demo, self.ip = egen, demo, ip
        self.status = "aaben"
        self.mit_gaet: int | None = None
        self.guesses: dict[str, tuple[str, int, float]] = {}    # pc → (navn, tid, when it came)
        self.resultat: dict[str, Any] | None = None
        self.ended_at: float | None = None                        # (monotonic) slut or aflyst
        self.render_navn: str | None = None                       # ours: the job it is about …
        self.seq_base: Any = None                                 # … and the last end seen before it
        self.lost_at: float | None = None                         # ours: the render stopped without a word
        self.demo_end: float | None = None                        # the demo's end (wall)
        self.arrived = 0.0                                        # a colleague's: when it came (monotonic)
        self.guessed_at: float | None = None                      # our last guess (monotonic)

    @property
    def key(self) -> tuple[str, int]:
        return self.pc, self.vid

    def ended(self) -> bool:
        return self.status in ("slut", "aflyst")

    def view(self, own_pc: str) -> dict[str, Any]:
        """The bet as SSE ``vaeddemaal`` and ``GET /api/kontor/vaeddemaal`` show it."""
        result = None
        if self.resultat is not None:
            result = {**self.resultat, "mig": self.resultat.get("vinder_pc") == own_pc}
        count = len(self.guesses) if self.egen or self.demo else int(self.mit_gaet is not None)
        out = {"pc": self.pc, "vid": self.vid, "navn": self.navn, "maskine": self.pc, "fil": self.fil,
               "pct": self.pct, "start": self.start, "lukker": self.lukker, "status": self.status,
               "egen": self.egen, "mit_gaet": self.mit_gaet, "antal_gaet": count, "resultat": result}
        if self.demo:
            out["demo"] = True
        return out


def winner_of(guesses: dict[str, tuple[str, int, float]], end: float) -> tuple[str, str, int] | None:
    """``(pc, navn, tid)`` of the guess nearest the end – the earlier guess on a tie."""
    best = None
    for pc, (navn, tid, at) in guesses.items():
        rank = (abs(tid - end), at)
        if best is None or rank < best[0]:
            best = (rank, (pc, navn, tid))
    return best[1] if best is not None else None


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
                 progress: Any = None,
                 board: Any = None,
                 working: Callable[[], bool] | None = None,
                 bell: Callable[[], None] | None = None,
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
        self._progress = progress                   # PetProgress (SPEC §23.1): treats, pranks, counters
        self._board = board                         # MessageBoard: posts and a colleague's year as cards
        self._working = working                     # the time tracking records now (trick or treat)
        self._bell = bell                           # the lunch bell (messages.bell_sound)
        self._data_dir = data_dir
        self._clock = clock
        self._wall = wall
        self._rng = rng or random.Random()
        self.pc = pc_name(hostname())
        self.id = secrets.token_hex(8)              # this process (a restart is a new Klippe)
        self._seq = 0
        self._lock = threading.RLock()
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
        self._state = self._load_state()           # kontor.json
        self._said = bool(self._state.get("netvaerk_sagt"))
        # The fun in the office (SPEC §23.7).
        self._outbox: deque[tuple[bytes, list[str] | None]] = deque(maxlen=OUTBOX_MAX)
        self._posts: deque[float] = deque()          # posts that came in (≤ POSTS_PER_HOUR)
        self._lunch_at: float | None = None          # our own lunch bell …
        self._post_at: float | None = None           # … post …
        self._share_at: float | None = None          # … and year, last sent
        self._slik_shown: set[tuple[str, str]] = set()   # (pc, day): one trick or treat per sender and day
        self._slik_pending: dict[str, dict[str, Any]] = {}    # a guest at our door, by its id
        self._slik_plan: tuple[str, float] | None = None      # (day, when) our own trick or treat
        self._slik_sent_at: float | None = None
        self._slik_answers: set[str] = set()         # the PCs that answered our trick or treat
        self._bets: dict[tuple[str, int], _Bet] = {}
        # A new process starts its bets somewhere else: a restart in the middle of a bet must not
        # reuse its number – colleagues know bets by (pc, vid).
        self._vid = secrets.randbelow(2 ** 30)
        self._demo_seq = 0
        self._render_since: tuple[float, float] | None = None    # (monotonic, wall) of the rising edge
        self._render_bet_done = False                # this render had its bet

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

    # -- the fun in the office (SPEC §23.7) ----------------------------------------------------
    def lunch(self) -> dict[str, Any]:
        """``POST /api/kontor/frokost``: ring every Klippe in the office to lunch."""
        self._ready("widget_frokost", MSG_LUNCH_OFF)
        now = self._clock()
        with self._lock:
            if _within(self._lunch_at, now, LUNCH_GAP_S):
                raise ValueError(MSG_LUNCH_AGAIN)
            self._lunch_at = now
        self._queue_packet("frokost")
        self._note("frokost_ringet")
        log.info("office Klippes: we ring for lunch")
        self.bus.publish("frokost", {"pc": self.pc, "navn": self._name(), "selv": True})
        self._ring_bell()
        return {"ok": True}

    def lunch_demo(self) -> dict[str, Any]:
        """``POST /api/kontor/frokost/demo``: the bell rings here only (nothing is sent)."""
        if not self.cfg.get("widget_enabled", False):
            raise ValueError(MSG_OFF)
        self.bus.publish("frokost", {"pc": DEMO_PC, "navn": DEMO_NAME, "selv": False, "demo": True})
        self._ring_bell()
        return {"ok": True}

    def send_post(self, til: Any, tekst: Any) -> dict[str, Any]:
        """``POST /api/kontor/post {til, tekst}``: Klippe carries a little note to one colleague
        (to that PC's address only)."""
        text = clean_text(tekst)
        if not isinstance(til, str) or not til.strip():
            raise ValueError(MSG_POST_WHO)
        til = til.strip().upper()
        if til == DEMO_PC:                       # an answer to the demo post: it stays here
            if not self.cfg.get("widget_enabled", False):
                raise ValueError(MSG_OFF)
            if not self.cfg.get("widget_post", True):
                raise ValueError(MSG_POST_OFF)
            self._check_post_text(text)
            self.bus.publish("post_sendt", {"til": DEMO_PC, "navn_til": DEMO_NAME})
            return {"ok": True, "demo": True}
        self._ready("widget_post", MSG_POST_OFF)
        self._check_post_text(text)
        peer = self._peer(til)
        if peer is None:
            raise ValueError(MSG_POST_AWAY.format(pc=til if _PC.fullmatch(til) else "Kollegaen"))
        now = self._clock()
        with self._lock:
            if _within(self._post_at, now, POST_GAP_S):
                raise ValueError(MSG_POST_AGAIN)
            before, self._post_at = self._post_at, now
        try:
            self._queue_packet("post", {"til": til, "tekst": text}, ip=peer["ip"])
        except ValueError:
            with self._lock:
                self._post_at = before
            raise ValueError(MSG_POST_LONG) from None
        self._note("post_sendt")
        self.bus.publish("post_sendt", {"til": til, "navn_til": peer["navn"]})
        return {"ok": True}

    @staticmethod
    def _check_post_text(text: str) -> None:
        if not text:
            raise ValueError(MSG_POST_EMPTY)
        if len(text) > POST_MAX:
            raise ValueError(MSG_POST_LONG)

    def post_demo(self) -> dict[str, Any]:
        """``POST /api/kontor/post/demo``: a post from DEMO-PC to ourselves (nothing is sent)."""
        if not self.cfg.get("widget_enabled", False):
            raise ValueError(MSG_OFF)
        with self._lock:
            self._demo_seq += 1
            seq = self._demo_seq
        self._post_card(DEMO_PC, DEMO_NAME, seq, DEMO_POST)
        event = {"type": "post", "pc": DEMO_PC, "navn": DEMO_NAME, **self._demo_look(), "demo": True}
        self.bus.publish("besoeg", event)
        return {"ok": True, "besoeg": event}

    def treat_or_trick(self, ident: Any, svar: Any) -> dict[str, Any]:
        """``POST /api/kontor/slik {id, svar}``: the guest at the door gets a Booster ("slik") – or,
        when nobody gave it one, makes mischief ("ballade"): our hat goes with it until midnight
        (no hat: Klippe is wrapped in toilet paper)."""
        if svar not in ("slik", "ballade"):
            raise ValueError("Ugyldig værdi: svar")
        if not isinstance(ident, str) or not ident or len(ident) > 64:
            raise ValueError(MSG_GUEST_GONE)
        if ident.startswith("demo:"):            # "Prøv Halloween": shown, nothing kept
            return {"ok": True, "demo": True, "ballade": self._own_prank() if svar == "ballade" else None}
        now = self._clock()
        with self._lock:
            self._prune_slik(now)
            guest = self._slik_pending.get(ident)
            if guest is None or guest["svar"] is not None:
                raise ValueError(MSG_GUEST_GONE)
            guest["svar"] = svar
        if svar == "slik":
            self._queue_packet("godbid", {"til": guest["pc"]}, ip=guest["ip"])
            self._note("slik_givet")
            log.info("office Klippes: a Booster for %s", guest["pc"])
            return {"ok": True, "ballade": None}
        prank = _clean_prank(self._progress_call("prank_received", guest["pc"], guest["navn"]))
        self._queue_packet("ballade", {"til": guest["pc"], "hat": prank["hat"]}, ip=guest["ip"])
        log.info("office Klippes: %s made mischief (%s)", guest["pc"], prank["hat"] or "toilet paper")
        return {"ok": True, "ballade": prank}

    def _own_prank(self) -> dict[str, Any]:
        try:
            hat = known_hat((self._equipped() or {}).get("hat"))
        except Exception:
            hat = None
        return {"hat": hat, "toiletpapir": hat is None}

    def slik_demo(self) -> dict[str, Any]:
        """``POST /api/kontor/slik/demo``: a made-up trick-or-treater ("Prøv Halloween")."""
        if not self.cfg.get("widget_enabled", False):
            raise ValueError(MSG_OFF)
        with self._lock:
            self._demo_seq += 1
            seq = self._demo_seq
        event = {"type": "slik", "id": f"demo:{seq}", "pc": DEMO_PC, "navn": DEMO_NAME, **self._demo_look(),
                 "kostume": self._rng.choice(costumes()), "til_s": SLIK_WAIT_S, "demo": True}
        self.bus.publish("besoeg", event)
        return {"ok": True, "besoeg": event}

    def guess(self, pc: Any, vid: Any, tid: Any) -> dict[str, Any]:
        """``POST /api/kontor/gaet {pc, vid, tid}``: when will that render be done? Our own render's
        guess stays here, a colleague's goes to that PC."""
        if not self.cfg.get("widget_vaeddemaal", True):
            raise ValueError(MSG_BETS_OFF)
        if not isinstance(pc, str) or type(vid) is not int:
            raise ValueError(MSG_BET_CLOSED)
        if type(tid) is not int:
            raise ValueError(MSG_BET_TIME)
        wall, now = self._wall(), self._clock()
        to: str | None = None
        with self._lock:
            bet = self._bets.get((pc, vid))
            if bet is None or bet.status != "aaben" or wall >= bet.lukker:
                raise ValueError(MSG_BET_CLOSED)
            if not wall < tid <= wall + EPOCH_SLACK_S:
                raise ValueError(MSG_BET_TIME)
            if _within(bet.guessed_at, now, GUESS_GAP_S):
                raise ValueError(MSG_GUESS_AGAIN)      # (the owner would not take it so soon)
            if bet.egen or bet.demo:
                if self.pc not in bet.guesses and len(bet.guesses) >= BET_GUESSES_MAX:
                    raise ValueError(MSG_BET_CLOSED)
                bet.guesses[self.pc] = (self._name(), tid, wall)
            elif self._sock is None or bet.ip is None:
                raise ValueError(self._reason or MSG_OFFICE_NOT_READY)
            else:
                to = bet.ip
            bet.mit_gaet, bet.guessed_at = tid, now
            view = bet.view(self.pc)
        if to is not None:
            self._queue_packet("gaet", {"til": bet.pc, "vid": bet.vid, "tid": tid}, ip=to)
        if not bet.demo:
            self._note("vaeddemaal_gaet")
        self.bus.publish("vaeddemaal", view)
        return {"ok": True, "vaeddemaal": view}

    def bets(self) -> dict[str, Any]:
        """``GET /api/kontor/vaeddemaal``: the open bets and those that ended a moment ago."""
        self._bet_tick(self._clock(), self._wall())
        with self._lock:
            return {"vaeddemaal": [bet.view(self.pc) for bet in sorted(self._bets.values(), key=lambda b: b.start)]}

    def bet_demo(self) -> dict[str, Any]:
        """``POST /api/kontor/vaeddemaal/demo``: DEMO-PC renders – guesses close after a minute, the
        end comes after one and a half (your guess wins when there is one). Nothing is sent."""
        if not self.cfg.get("widget_enabled", False):
            raise ValueError(MSG_OFF)
        wall = int(self._wall())
        with self._lock:
            self._demo_seq += 1
            bet = _Bet(DEMO_PC, self._demo_seq, DEMO_NAME, DEMO_FIL, 12, wall - 60, wall + DEMO_BET_CLOSE_S, demo=True)
            bet.demo_end = wall + DEMO_BET_END_S
            bet.guesses[DEMO_PC] = (DEMO_NAME, wall + DEMO_BET_END_S + self._rng.randint(-120, 300), float(wall))
            self._add_bet(bet)
            view = bet.view(self.pc)
        self.bus.publish("vaeddemaal", view)
        return {"ok": True, "vaeddemaal": view}

    def record(self, score: Any) -> int:
        """A new best in the render game (``POST /api/pet/spil``): the office hears it."""
        if type(score) is not int or score < OFFICE_RECORD_MIN or self._sock is None \
                or not self.cfg.get("widget_renderspil", True):
            return 0
        try:
            self._queue_packet("rekord", {"score": min(score, SCORE_MAX)})
        except ValueError:
            return 0
        return 1

    def share_year(self, summary: Any) -> dict[str, Any]:
        """``POST /api/aar/del``: our year (numbers only, ``year_summary``) to the office."""
        self._ready("widget_aarsrapport", MSG_YEAR_OFF)
        try:
            x = clean_x("aar", summary, self._wall())
        except ValueError:
            raise ValueError("Klippet kunne ikke deles") from None
        now = self._clock()
        with self._lock:
            if _within(self._share_at, now, SHARE_GAP_S):
                raise ValueError(MSG_YEAR_AGAIN)
            self._share_at = now
        self._queue_packet("aar", x)
        log.info("office Klippes: we share our year %s", x["aar"])
        return {"ok": True}

    # -- helpers of the fun ---------------------------------------------------------------------
    def _ready(self, setting: str | None, off: str | None) -> None:
        """The office can send now (else the Danish reason)."""
        if not self.cfg.get("widget_enabled", False):
            raise ValueError(MSG_OFF)
        if setting is not None and not self.cfg.get(setting, True):
            raise ValueError(off)
        if not self.cfg.get("widget_kontor", True):
            raise ValueError(MSG_OFFICE_OFF)
        if self._sock is None:
            raise ValueError(self._reason or MSG_OFFICE_NOT_READY)

    def _name(self) -> str:
        return pet_name(self.cfg.get("widget_pet_name", "Klippe"))

    def _today(self) -> date:
        return datetime.fromtimestamp(self._wall()).date()

    def _demo_look(self) -> dict[str, Any]:
        tables, rng = _tables(), self._rng
        pynt = {slot: tables.DEFAULTS[slot] for slot in tables.SLOTS}
        for slot in rng.sample(list(tables.SLOTS), 3):
            pynt[slot] = rng.choice([item.id for item in tables.ITEMS if item.slot == slot])
        return {"stage": rng.choice(("junior", "pro", "legend")), "outfit": rng.choice(OUTFITS), "pynt": pynt}

    def _peer(self, pc: str) -> dict[str, Any] | None:
        """The colleague's Klippe on ``pc`` heard from lately (its newest address)."""
        now = self._clock()
        with self._lock:
            found = [p for p in self._peers.values() if p["pc"] == pc and 0 <= now - p["seen"] <= PEER_FRESH_S]
            return dict(max(found, key=lambda p: p["seen"])) if found else None

    def _note(self, name: str) -> None:
        self._progress_call("note", name)

    def _progress_call(self, method: str, *args: Any) -> Any:
        fn = getattr(self._progress, method, None) if self._progress is not None else None
        if not callable(fn):
            return None
        try:
            return fn(*args)
        except Exception:
            log.exception("office Klippes: Klippe's %s failed", method)
            return None

    def _call_bool(self, fn: Callable[[], Any] | None) -> bool:
        if fn is None:
            return False
        try:
            return bool(fn())
        except Exception:
            return False

    def _ring_bell(self) -> None:
        """The lunch bell, once – only while Klippe is on the screen."""
        if not self._call_bool(self._shown):
            return
        bell = self._bell
        if bell is None:
            from . import messages
            bell = messages.bell_sound
        try:
            bell()
        except Exception:
            log.debug("office Klippes: no lunch bell", exc_info=True)

    def _kostume(self) -> str | None:
        state = self._progress_call("season_state")
        if not isinstance(state, dict):
            return None
        for key in ("kostume", "kostume_aar"):
            if state.get(key) in costumes():
                return state[key]
        return None

    def _prune_slik(self, now: float) -> None:
        for ident in [k for k, guest in self._slik_pending.items() if not _within(guest["at"], now, SLIK_PENDING_S)]:
            del self._slik_pending[ident]

    def _add_bet(self, bet: _Bet) -> bool:
        """Keep ``bet`` (lock held): at most BETS_PER_SENDER from one address and BETS_MAX in all –
        the ended ones go first, then the colleague bet that came first (by our own clock: a
        packet's ``start`` may say anything). Our own bet and the demo's are never pushed out.
        False: a colleague's bet found no room (the table holds only ours)."""
        mine = bet.egen or bet.demo
        if not mine:
            theirs = sorted((b for b in self._bets.values() if b.ip == bet.ip and not (b.egen or b.demo)),
                            key=lambda b: b.arrived)
            while len(theirs) >= BETS_PER_SENDER:
                del self._bets[theirs.pop(0).key]
        while len(self._bets) >= BETS_MAX:
            ended = [b for b in self._bets.values() if b.ended()]
            colleagues = [b for b in self._bets.values() if not (b.egen or b.demo)]
            if ended:
                victim = min(ended, key=lambda b: b.ended_at or 0.0)
            elif colleagues:
                victim = min(colleagues, key=lambda b: b.arrived)
            elif mine:
                break                                 # only ours are left: ours may still come in
            else:
                return False
            del self._bets[victim.key]
        self._bets[bet.key] = bet
        return True

    def _bet_from(self, pc: str, vid: int, ip: str) -> _Bet | None:
        """A colleague's bet we know, when ``ip`` is the address it came from (lock held)."""
        bet = self._bets.get((pc, vid))
        return bet if bet is not None and not (bet.egen or bet.demo) and bet.ip == ip else None

    def _own_bet(self) -> _Bet | None:
        """Our render's bet while it is not over (lock held)."""
        for bet in self._bets.values():
            if bet.egen and not bet.ended():
                return bet
        return None

    # -- receiving --------------------------------------------------------------------------
    def handle_packet(self, data: bytes, addr: Any) -> dict[str, Any] | None:
        """One datagram from ``addr``; returns the event it became (``besoeg`` for a visit, or
        the event of SPEC §23.7 it gave), or None. Whatever a packet holds, it never stops the
        office: a packet that cannot be handled is dropped (logged at debug)."""
        try:
            return self._handle_packet(data, addr)
        except Exception:
            log.debug("office Klippes: a packet from %s could not be handled", addr, exc_info=True)
            return None

    def _handle_packet(self, data: bytes, addr: Any) -> dict[str, Any] | None:
        ip = addr[0] if isinstance(addr, tuple) and addr else None
        if not lan_address(ip):
            return None
        try:
            msg = parse_packet(data, now=self._wall())
        except ValueError as exc:
            log.debug("office Klippes: a packet from %s refused (%s)", ip, exc)
            return None
        if msg["pc"] == self.pc or msg["id"] == self.id:
            return None                               # our own hello, back from the broadcast
        kind, x = msg["type"], msg["x"]
        if x is not None and "til" in x and x["til"] != self.pc:
            return None                               # meant for another PC
        if not self._accepts(kind):
            return None                               # that fun is switched off here
        now = self._clock()
        with self._lock:
            # The per-sender gates come first: a refused packet is not even noted (it neither
            # keeps a Klippe "here" nor changes its name or stage).
            sender = self._sender(ip, now)
            if kind == "hej":
                if _within(sender.get("hej"), now, HEJ_GAP_S):
                    return None                       # the broadcast copy, or a flood
                sender["hej"] = now
                self._note_peer(msg, ip, now)
                return None
            gate = "visit" if kind in ("besoeg", "fest") else kind
            gap = VISIT_GAP_S if gate == "visit" else TYPE_GAPS[kind]
            if kind == "vaeddemaal" and self._bet_from(msg["pc"], x["vid"], ip) is not None:
                # Its owner tells what became of a bet we know (it closed at 50 %): not a new bet.
                gate, gap = "vaeddemaal_opd", BET_UPDATE_GAP_S
            if _within(sender.get(gate), now, gap):
                log.debug("office Klippes: %s sends %s too often", msg["pc"], kind)
                return None
            if kind in VISIT_TYPES and not _room(self._visits, now, VISITS_PER_HOUR):
                log.debug("office Klippes: enough visits this hour")
                return None
            if kind == "post" and not _room(self._posts, now, POSTS_PER_HOUR):
                log.debug("office Klippes: enough posts this hour")
                return None
            sender[gate] = now
            if kind in VISIT_TYPES:
                self._visits.append(now)
            if kind == "post":
                self._posts.append(now)
            self._note_peer(msg, ip, now)
        if kind not in ("besoeg", "fest"):
            return getattr(self, f"_got_{kind}")(msg, ip)
        event = {"type": "fest" if kind == "fest" else "trofae", "pc": msg["pc"], "navn": msg["navn"],
                 "stage": msg["stage"], "outfit": msg["outfit"], "pynt": msg["pynt"],
                 "trofae": trophy_info(msg["trofae"])}
        log.info("office Klippes: a visit from %s (%s)", msg["pc"], event["type"])
        self.bus.publish("besoeg", event)
        return event

    def _accepts(self, kind: str) -> bool:
        """Whether this PC wants a ``kind`` packet now (its fun switched on, SPEC §23.0); the
        answers to our own trick or treat are always taken."""
        setting = {"slik": "widget_saeson", "frokost": "widget_frokost", "post": "widget_post",
                   "vaeddemaal": "widget_vaeddemaal", "gaet": "widget_vaeddemaal", "vinder": "widget_vaeddemaal",
                   "rekord": "widget_renderspil", "aar": "widget_aarsrapport"}.get(kind)
        return setting is None or bool(self.cfg.get(setting, True))

    @staticmethod
    def _guest(kind: str, msg: dict[str, Any]) -> dict[str, Any]:
        """A ``besoeg`` event of a colleague's Klippe (its look only from the checked packet)."""
        return {"type": kind, "pc": msg["pc"], "navn": msg["navn"], "stage": msg["stage"], "outfit": msg["outfit"],
                "pynt": msg["pynt"]}

    def _got_frokost(self, msg: dict[str, Any], ip: str) -> dict[str, Any]:
        event = {"pc": msg["pc"], "navn": msg["navn"], "selv": False}
        log.info("office Klippes: %s rings for lunch", msg["pc"])
        self.bus.publish("frokost", event)
        self._ring_bell()
        return event

    def _got_post(self, msg: dict[str, Any], ip: str) -> dict[str, Any]:
        self._post_card(msg["pc"], msg["navn"], msg["seq"], msg["x"]["tekst"])
        self._note("post_faaet")
        event = self._guest("post", msg)
        self.bus.publish("besoeg", event)
        return event

    def _post_card(self, pc: str, navn: str, seq: int, tekst: str) -> None:
        """A post as a card in Klippe (no call: it never rings) with "Svar" (SPEC §23.7)."""
        board = self._board
        if board is None:
            return
        try:
            board.post({"tag": f"post:{pc}:{seq}", "titel": f"✉️ Post fra {navn} ({pc})", "tekst": tekst,
                        "knapper": [{"tekst": "Svar", "uri": f"projektsog:post/{pc}"}], "udloeber": POST_TTL_S,
                        "lyd": True, "opkald": False}, internal=True)
        except Exception:
            log.error("office Klippes: a post could not be shown")

    def _got_slik(self, msg: dict[str, Any], ip: str) -> dict[str, Any] | None:
        day = self._today().isoformat()
        now = self._clock()
        ident = f"{msg['pc']}:{msg['seq']}"
        with self._lock:
            if (msg["pc"], day) in self._slik_shown:
                return None                           # one trick or treat per sender and day
            self._slik_shown = {seen for seen in self._slik_shown if seen[1] == day}
            self._slik_shown.add((msg["pc"], day))
            self._prune_slik(now)
            while len(self._slik_pending) >= SLIK_PENDING_MAX:
                del self._slik_pending[min(self._slik_pending, key=lambda k: self._slik_pending[k]["at"])]
            self._slik_pending[ident] = {"ip": ip, "pc": msg["pc"], "navn": msg["navn"], "at": now, "svar": None}
        event = {**self._guest("slik", msg), "id": ident, "kostume": msg["x"]["kostume"], "til_s": SLIK_WAIT_S}
        log.info("office Klippes: %s is trick-or-treating (%s)", msg["pc"], event["kostume"])
        self.bus.publish("besoeg", event)
        return event

    def _answer_expected(self, pc: str) -> bool:
        """An answer to our own trick or treat: only within a while of it, one per PC."""
        with self._lock:
            if not _within(self._slik_sent_at, self._clock(), SLIK_ANSWER_S) or pc in self._slik_answers:
                return False
            self._slik_answers.add(pc)
            return True

    def _got_godbid(self, msg: dict[str, Any], ip: str) -> dict[str, Any] | None:
        if not self._answer_expected(msg["pc"]):
            return None
        self._progress_call("treat", TREAT_POINTS, "Booster")
        self._note("slik_faaet")
        event = {"type": "godbid", "pc": msg["pc"], "navn": msg["navn"]}
        log.info("office Klippes: %s gave us a Booster", msg["pc"])
        self.bus.publish("slik_svar", event)
        return event

    def _got_ballade(self, msg: dict[str, Any], ip: str) -> dict[str, Any] | None:
        if not self._answer_expected(msg["pc"]):
            return None
        hat = msg["x"]["hat"]
        if hat is not None:
            self._progress_call("prank_won", hat, msg["pc"], msg["navn"])
        event = {"type": "ballade", "pc": msg["pc"], "navn": msg["navn"], "hat": hat, "toiletpapir": hat is None}
        log.info("office Klippes: a prank on %s (%s)", msg["pc"], hat or "toilet paper")
        self.bus.publish("slik_svar", event)
        return event

    def _got_vaeddemaal(self, msg: dict[str, Any], ip: str) -> dict[str, Any] | None:
        x, wall = msg["x"], self._wall()
        with self._lock:
            bet = self._bets.get((msg["pc"], x["vid"]))
            if bet is None:
                bet = _Bet(msg["pc"], x["vid"], msg["navn"], x["fil"], x["pct"], x["start"], x["lukker"], ip=ip)
                bet.arrived = self._clock()
                if not self._add_bet(bet):
                    return None
            elif bet.egen or bet.demo or bet.ended() or bet.ip != ip:
                return None                           # (only the PC that opened it may change it)
            else:                                     # it closed early (50 %)
                bet.pct = x["pct"]
                bet.lukker = min(bet.lukker, x["lukker"])
            if bet.status == "aaben" and wall >= bet.lukker:
                bet.status = "lukket"
            view = bet.view(self.pc)
        log.info("office Klippes: %s opened a render bet (%s)", msg["pc"], x["fil"])
        self.bus.publish("vaeddemaal", view)
        return view

    def _got_gaet(self, msg: dict[str, Any], ip: str) -> dict[str, Any] | None:
        x, wall = msg["x"], self._wall()
        with self._lock:
            bet = self._bets.get((self.pc, x["vid"]))
            if bet is None or not bet.egen or bet.status != "aaben" or wall >= bet.lukker or x["tid"] <= wall:
                return None
            if msg["pc"] not in bet.guesses and len(bet.guesses) >= BET_GUESSES_MAX:
                return None
            bet.guesses[msg["pc"]] = (msg["navn"], x["tid"], wall)
            view = bet.view(self.pc)
        self.bus.publish("vaeddemaal", view)
        return view

    def _got_vinder(self, msg: dict[str, Any], ip: str) -> dict[str, Any] | None:
        x = msg["x"]
        with self._lock:
            bet = self._bet_from(msg["pc"], x["vid"], ip)
            if bet is None or bet.ended():
                return None                           # (only the PC that opened it may end it)
            bet.status = x["status"]
            bet.resultat = None if x["status"] == "aflyst" else {
                "vinder_pc": x["vinder_pc"], "vinder_navn": x["vinder_navn"], "tid": x["tid"], "slut": x["slut"]}
            bet.ended_at = self._clock()
            won = x["status"] == "slut" and x["vinder_pc"] == self.pc and bet.mit_gaet is not None
            view = bet.view(self.pc)
        if won:
            self._note("vaeddemaal_vundet")
        self.bus.publish("vaeddemaal", view)
        return view

    def _got_rekord(self, msg: dict[str, Any], ip: str) -> dict[str, Any] | None:
        score = msg["x"]["score"]
        if self._progress_call("office_record", msg["pc"], msg["navn"], score) is not True:
            return None
        event = {"tekst": f"🎮 {msg['navn']} ({msg['pc']}) har ny kontorrekord i render-spillet: {score}!"}
        self.bus.publish("say", event)
        return event

    def _got_aar(self, msg: dict[str, Any], ip: str) -> dict[str, Any]:
        x = msg["x"]
        board = self._board
        if board is not None:
            try:
                board.post({"tag": f"aar:{msg['pc']}:{x['aar']}", "titel": f"📊 Klippet {x['aar']} fra {msg['navn']} "
                            f"({msg['pc']})", "tekst": year_line(x), "udloeber": YEAR_CARD_TTL_S, "lyd": False},
                           internal=True)
            except Exception:
                log.exception("office Klippes: the year of %s could not be shown", msg["pc"])
        event = {**self._guest("aar", msg), "aar": x}
        log.info("office Klippes: %s shares its year %s", msg["pc"], x["aar"])
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
    def _message(self, kind: str, trofae: dict[str, str] | None = None, x: dict[str, Any] | None = None) -> bytes:
        stage, outfit, pynt = self._stage(), self._outfit(), self._pynt()
        with self._lock:
            self._seq += 1
            seq = self._seq
        msg: dict[str, Any] = {"app": APP_TAG, "v": VERSION, "type": kind, "pc": self.pc, "id": self.id,
                               "seq": seq, "navn": self._name(), "stage": stage, "outfit": outfit, "pynt": pynt}
        if trofae is not None:
            msg["trofae"] = trofae
        if x is not None:
            msg["x"] = x
        return encode_packet(msg)

    def _queue_packet(self, kind: str, x: dict[str, Any] | None = None, *, ip: str | None = None) -> None:
        """Build a packet now (ValueError: too large) and let the office's thread send it – to
        ``ip`` only, or to everyone (the hosts and the broadcast). The thread is never kept
        waiting by an API call, and nothing waits here for a name lookup."""
        data = self._message(kind, x=x)
        self._outbox.append((data, [ip] if ip else None))
        self._wake.set()

    def _flush_outbox(self) -> int:
        """Send what waits (the office's thread); returns how many datagrams went out."""
        sent = 0
        while True:
            try:
                data, targets = self._outbox.popleft()
            except IndexError:
                return sent
            sock = self._sock
            if sock is None:
                continue
            for ip in targets if targets is not None else self._targets():
                try:
                    sock.sendto(data, (ip, PORT))
                    sent += 1
                except OSError as exc:
                    log.debug("office Klippes: sending to %s failed: %s", ip, exc)

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
        """A bus event: a new trophy or find → ``besoeg``; a real delivery → ``fest``; the
        bridge's render state → a render bet (SPEC §23.7, sent by the next round)."""
        if kind == "render":
            self._on_render(data)
            return 0
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
            if kind in ("pet_progress", "levering", "render"):
                try:
                    self.on_event(kind, data)
                except Exception:
                    log.exception("office Klippes: %s failed", kind)

    # -- render bets (SPEC §23.7) ---------------------------------------------------------------
    def _on_render(self, data: Any) -> None:
        """The bridge's render state: a long render opens our bet; its end names the winner (or
        calls the bet off). Several jobs in a queue: the bet is about the job it was opened on."""
        if not isinstance(data, dict):
            return
        now, wall = self._clock(), self._wall()
        active = bool(data.get("aktiv"))
        done = data.get("faerdig") if isinstance(data.get("faerdig"), dict) else None
        seq = done.get("seq") if done is not None else None
        news: list[tuple[str, _Bet]] = []
        with self._lock:
            bet = self._own_bet()
            if active:
                if self._render_since is None:
                    self._render_since = (now, wall)
                    self._render_bet_done = False
                if bet is not None:
                    bet.lost_at = None
                    pct = data.get("pct")
                    if type(pct) is int:
                        bet.pct = max(0, min(100, pct))
                    if bet.status == "aaben" and bet.pct >= BET_CLOSE_PCT:
                        bet.status, bet.lukker = "lukket", min(bet.lukker, int(wall))
                        news.append(("lukket", bet))
                elif self._may_bet(data, now):
                    bet = self._open_bet(data, wall, seq)
                    news.append(("aaben", bet))
            if bet is not None and done is not None and seq != bet.seq_base:
                job = done.get("fil")
                same = not job or not bet.render_navn or str(job).casefold() == bet.render_navn.casefold()
                if same or not active:
                    self._end_bet(bet, wall if done.get("udfald") == "done" else None)
                    news.append(("slut", bet))
                else:
                    bet.seq_base = seq                  # another job of the queue ended
            if not active:
                self._render_since = None
                if bet is not None and not bet.ended() and bet.lost_at is None:
                    bet.lost_at = now                   # its end may still come – else it is off later
        for what, item in news:
            self._announce(what, item)

    def _may_bet(self, data: dict[str, Any], now: float) -> bool:
        eta = data.get("eta_s")
        since = self._render_since
        return (not self._render_bet_done and self._sock is not None and since is not None
                and now - since[0] >= BET_AFTER_S
                and isinstance(eta, (int, float)) and not isinstance(eta, bool) and eta >= BET_MIN_ETA_S
                and bool(self.cfg.get("widget_enabled", False)) and bool(self.cfg.get("widget_kontor", True))
                and bool(self.cfg.get("widget_vaeddemaal", True)))

    def _open_bet(self, data: dict[str, Any], wall: float, seq: Any) -> _Bet:
        """Our render's bet (lock held)."""
        name = data.get("navn") or data.get("tidslinje")
        name = name if isinstance(name, str) else ""
        fil = clean_text(name)[:FIL_MAX].strip()
        pct = data.get("pct")
        started = self._render_since[1] if self._render_since is not None else wall
        self._vid = (self._vid + 1) % VID_MAX
        bet = _Bet(self.pc, self._vid, self._name(), fil, max(0, min(100, pct)) if type(pct) is int else 0,
                   int(max(started, wall - EPOCH_SLACK_S + 60)), int(wall) + BET_OPEN_S, egen=True)
        bet.render_navn = name or None
        bet.seq_base = seq
        self._render_bet_done = True
        self._add_bet(bet)
        return bet

    def _end_bet(self, bet: _Bet, end: float | None) -> None:
        """Over (lock held): ``end`` the time it was done – or None: the bet is off."""
        if end is None:
            bet.status, bet.resultat = "aflyst", None
        else:
            win = winner_of(bet.guesses, end)
            bet.status = "slut"
            bet.resultat = {"vinder_pc": win[0] if win else None, "vinder_navn": win[1] if win else None,
                            "tid": win[2] if win else None, "slut": int(end)}
        bet.ended_at = self._clock()

    def _announce(self, what: str, bet: _Bet) -> None:
        """Tell the office and the widget what happened to ``bet`` (lock not held)."""
        with self._lock:
            view = bet.view(self.pc)
            if bet.demo:
                packet = None
            elif what in ("aaben", "lukket") and bet.egen:
                packet = ("vaeddemaal", {"vid": bet.vid, "fil": bet.fil, "pct": bet.pct, "start": bet.start,
                                         "lukker": bet.lukker})
            elif what == "slut" and bet.egen:
                result = bet.resultat or {}
                packet = ("vinder", {"vid": bet.vid, "status": bet.status, "vinder_pc": result.get("vinder_pc"),
                                     "vinder_navn": result.get("vinder_navn"), "tid": result.get("tid"),
                                     "slut": result.get("slut")})
            else:
                packet = None
        if packet is not None and self._sock is not None:
            try:
                self._queue_packet(*packet)
            except ValueError as exc:
                log.warning("office Klippes: no %s: %s", packet[0], exc)
        if what == "slut" and bet.egen and view["resultat"] and view["resultat"]["mig"]:
            self._note("vaeddemaal_vundet")
        if what == "aaben":
            log.info("office Klippes: a render bet on %s", bet.fil or "a render")
        elif what == "slut":
            log.info("office Klippes: the render bet ended (%s)", bet.status)
        self.bus.publish("vaeddemaal", view)

    def _bet_tick(self, now: float, wall: float) -> None:
        """Bets close by the clock, a render that went quiet calls ours off, the demo plays out,
        old ones are forgotten."""
        news: list[tuple[str, _Bet]] = []
        with self._lock:
            for bet in list(self._bets.values()):
                if bet.status == "aaben" and wall >= bet.lukker:
                    bet.status = "lukket"
                    news.append(("vis", bet))
                if bet.demo and not bet.ended() and bet.demo_end is not None and wall >= bet.demo_end:
                    if bet.mit_gaet is not None:
                        bet.guesses = {self.pc: (self._name(), bet.mit_gaet, 0.0)}   # yours wins the demo
                    self._end_bet(bet, bet.demo_end)
                    news.append(("slut", bet))
                elif bet.egen and not bet.ended() and bet.lost_at is not None and now - bet.lost_at >= BET_LOST_S:
                    self._end_bet(bet, None)
                    news.append(("slut", bet))
                if bet.ended() and bet.ended_at is not None and now - bet.ended_at >= BET_KEEP_S \
                        or not bet.egen and not bet.demo and not bet.ended() and wall - bet.lukker >= BET_STALE_S:
                    del self._bets[bet.key]
        for what, bet in news:
            self._announce(what, bet)

    # -- trick or treat (SPEC §23.7) -------------------------------------------------------------
    def _slik_tick(self, wall: float) -> None:
        """Halloween week, a weekday: at a moment drawn between 10:00 and 15:30, as soon as you
        work and Klippe is shown, Klippe goes trick-or-treating – once a day."""
        if self._sock is None or not self.cfg.get("widget_saeson", True):
            return
        day = datetime.fromtimestamp(wall).date()
        if day.weekday() >= 5 or not halloween_week(day):
            return
        iso = day.isoformat()
        if self._state.get("slik_dag") == iso:
            return
        if self._slik_plan is None or self._slik_plan[0] != iso:
            first = datetime.combine(day, datetime.min.time()).timestamp() + SLIK_FROM_H * 3600
            self._slik_plan = (iso, first + self._rng.uniform(0.0, (SLIK_TO_H - SLIK_FROM_H) * 3600))
        if wall < self._slik_plan[1] or not self._call_bool(self._working) or not self._call_bool(self._shown):
            return
        costume = self._kostume()
        if costume is None:
            return
        try:
            self._queue_packet("slik", {"kostume": costume})
        except ValueError as exc:
            log.warning("office Klippes: no trick or treat: %s", exc)
            return
        with self._lock:
            self._slik_sent_at = self._clock()
            self._slik_answers = set()
        self._state["slik_dag"] = iso
        self._save_state()
        log.info("office Klippes: trick or treat (%s)", costume)

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
        self._bet_tick(now, self._wall())
        if self._sock is None:
            self._outbox.clear()
            return
        self._receive()
        self._slik_tick(self._wall())
        if self._hej_at is None or not 0 <= now - self._hej_at < HEJ_S:
            self._hej_at = now
            self.send("hej")
        self._flush_outbox()
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
        self._state["netvaerk_sagt"] = True
        self._save_state()

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

    def _save_state(self) -> None:
        """kontor.json: the firewall line was said, the day of our trick or treat."""
        path = self._state_path()
        temp = f"{path}.{os.getpid()}.tmp"
        try:
            with open(temp, "w", encoding="utf-8") as fh:
                json.dump(self._state, fh)
            os.replace(temp, path)
        except OSError as exc:
            log.warning("office Klippes: could not save %s: %s", path, exc)


def _within(at: float | None, now: float, seconds: float) -> bool:
    return at is not None and 0 <= now - at < seconds


def _room(times: deque[float], now: float, per_hour: int) -> bool:
    """Room for one more in the last hour (the old ones are dropped first)."""
    while times and not _within(times[0], now, 3600.0):
        times.popleft()
    return len(times) < per_hour


def _clean_prank(value: Any) -> dict[str, Any]:
    """``PetProgress.prank_received()``'s answer: ``{hat: a hat id | None, toiletpapir}``."""
    hat = known_hat(value.get("hat")) if isinstance(value, dict) else None
    return {"hat": hat, "toiletpapir": hat is None}
