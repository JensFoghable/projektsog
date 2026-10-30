"""Klippe's trophies and wardrobe (SPEC §18.5).

Trophies are earned from what Projektsøg already knows – the time tracking (time.db), the
transferred cards and Klippe's own games – and many of them give an item for the wardrobe: a
body colour, clapper stripes, a hat, glasses, something in the mouth or in the hand, an aura.
The legendary ones (cool shades, a cigarette, an AWP that Klippe shoots at the pointer with
while you are away) are found, almost never earned. They reward steady work, focus with breaks,
variety and finishing on time – never late nights or overtime.

Some items can only be *found*: on every workday with at least an hour, Klippe may find a rare
one. Whether it does is decided by this PC's own secret (made once) and the date, so pets in
the same office end up different. Seasonal trophies can only be earned in their season, and the
secret ones show "???" until they are earned.

Klippe also gets hungry (SPEC §18.6): it burns energy while you work (and a little otherwise, but
never below "a bit peckish"), and you feed it from a small menu – durum, McDonald's, Faxe Kondi
Booster and Monster Mango Loco. The energy drinks give it a rush for a while. What it has eaten
counts for a few trophies of its own.

Fun in the office (SPEC §23) keeps its records here too: counters of what happened (lunch bells,
posts, Halloween treats, baths, deliveries …, also per year for "Klippet <år>"), the Halloween
week (this PC's costume, a hat stolen by a colleague's Klippe), the Christmas calendar (24 doors,
the gifts shuffled per PC) and the render game's records.

Everything lives in ``%LOCALAPPDATA%\\Projektsog\\pet.json``; it is recomputed every few minutes
(idempotent: a trophy, once earned, stays).
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import random
import re
import secrets
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

log = logging.getLogger(__name__)

REFRESH_S = 300.0
FIRST_REFRESH_S = 8.0
DAY_MIN_S = 3600.0             # a day "counts" (streaks, finds) from one hour
SLOTS = ("farve", "striber", "hat", "briller", "mund", "hals", "haand", "aura")
SLOT_NAMES = {"farve": "Farve", "striber": "Striber", "hat": "Hat", "briller": "Briller", "mund": "Mund",
              "hals": "Om halsen", "haand": "I hånden", "aura": "Aura"}


# --------------------------------------------------------------------------------------
# The wardrobe
# --------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Item:
    id: str
    slot: str
    name: str
    rarity: str = "almindelig"           # almindelig | sjælden | legendarisk
    default: bool = False                # everyone has it from the start


ITEMS = [
    Item("midnat", "farve", "Midnat", default=True),
    Item("skov", "farve", "Skov"),
    Item("bordeaux", "farve", "Bordeaux"),
    Item("havblaa", "farve", "Havblå"),
    Item("lavendel", "farve", "Lavendel"),
    Item("kobber", "farve", "Kobber"),
    Item("mint", "farve", "Mint"),
    Item("julerod", "farve", "Julerød"),
    Item("guld", "farve", "Guld", "sjælden"),
    Item("kosmos", "farve", "Kosmos", "sjælden"),
    Item("regnbue", "farve", "Regnbue", "legendarisk"),
    Item("klassisk", "striber", "Klassisk", default=True),
    Item("orange", "striber", "Orange"),
    Item("roed", "striber", "Rød"),
    Item("blaa", "striber", "Blå"),
    Item("polkagris", "striber", "Polkagris"),
    Item("zebra", "striber", "Lyserød zebra", "sjælden"),
    Item("guldstriber", "striber", "Guld", "sjælden"),
    Item("neon", "striber", "Neon", "sjælden"),
    Item("ingen", "hat", "Ingen", default=True),
    Item("sloejfe", "hat", "Sløjfe"),
    Item("festhat", "hat", "Festhat"),
    Item("baret", "hat", "Instruktørbaret"),
    Item("nathue", "hat", "Nathue"),
    Item("gevir", "hat", "Rensdyrgevir"),
    Item("glorie", "hat", "Engleglorie"),
    Item("nissehue", "hat", "Nissehue", "sjælden"),
    Item("blomst", "hat", "Forårsblomst", "sjælden"),
    Item("propelhat", "hat", "Propelhat", "sjælden"),
    Item("heksehat", "hat", "Heksehat", "sjælden"),
    Item("racerhjelm", "hat", "Racerhjelm", "sjælden"),
    Item("julestjerne", "hat", "Julestjerne", "legendarisk"),
    Item("ingen-briller", "briller", "Ingen", default=True),
    Item("hornbriller", "briller", "Hornbriller"),
    Item("racerbriller", "briller", "Racerbriller"),
    Item("julebriller", "briller", "Julebriller"),
    Item("solbriller", "briller", "Seje solbriller", "legendarisk"),
    Item("ingen-mund", "mund", "Ingen", default=True),
    Item("slikkepind", "mund", "Slikkepind"),
    Item("vampyrtaender", "mund", "Vampyrtænder", "sjælden"),
    Item("cigaret", "mund", "Cigaret", "legendarisk"),
    Item("ingen-hals", "hals", "Ingen", default=True),
    Item("halstoerklaede", "hals", "Halstørklæde"),
    Item("julesloejfe", "hals", "Julesløjfe"),
    Item("ingen-haand", "haand", "Ingen", default=True),
    Item("kaffe", "haand", "Kaffekop"),
    Item("durum", "haand", "Durum"),
    Item("pommes", "haand", "Pommes frites"),
    Item("booster", "haand", "Faxe Kondi Booster"),
    Item("mangoloco", "haand", "Monster Mango Loco"),
    Item("klokke", "haand", "Frokostklokke"),
    Item("brev", "haand", "Brev"),
    Item("badeand", "haand", "Badeand"),
    Item("slikstok", "haand", "Slikstok"),
    Item("julegave", "haand", "Julegave"),
    Item("graeskar", "haand", "Græskarlygte", "sjælden"),
    Item("krystalkugle", "haand", "Krystalkugle", "sjælden"),
    Item("awp", "haand", "AWP", "legendarisk"),
    Item("ingen-aura", "aura", "Ingen", default=True),
    Item("varm", "aura", "Varm glød"),
    Item("kold", "aura", "Kold glød"),
    Item("hjerter", "aura", "Hjerter"),
    Item("bobler", "aura", "Sæbebobler"),
    Item("snefnug", "aura", "Snefnug"),
    Item("julelys", "aura", "Julelys"),
    Item("lyn", "aura", "Lyn", "sjælden"),
    Item("stjernestoev", "aura", "Stjernestøv", "legendarisk"),
]
ITEMS_BY_ID = {item.id: item for item in ITEMS}
DEFAULTS = {item.slot: item.id for item in ITEMS if item.default}

# Found, never earned: (item, chance per workday with ≥ 1 hour).
FINDS = [("kosmos", 0.03), ("neon", 0.03), ("regnbue", 0.005), ("stjernestoev", 0.005),
         ("solbriller", 0.004), ("cigaret", 0.004), ("awp", 0.003)]


# --------------------------------------------------------------------------------------
# Seasons, costumes and the Christmas calendar (SPEC §23.1)
# --------------------------------------------------------------------------------------

KOSTUMER = ("vampyr", "spoegelse", "skelet", "heks")


def halloween_week(day: date) -> bool:
    """Monday–Sunday of the ISO week that holds 31 October of ``day``'s year ("uge 44" in 2026)."""
    halloween = date(day.year, 10, 31)
    monday = halloween - timedelta(days=halloween.weekday())
    return monday <= day <= monday + timedelta(days=6)


def season(day: date) -> str | None:
    """"halloween" in the Halloween week, "jul" all of December, else None."""
    if halloween_week(day):
        return "halloween"
    return "jul" if day.month == 12 else None


def costume(secret: str, year: int) -> str:
    """This PC's Halloween costume this year – colleagues' Klippes dress differently."""
    digest = hashlib.sha256(f"{secret}|{year}|kostume".encode()).hexdigest()
    return KOSTUMER[int(digest, 16) % len(KOSTUMER)]


@dataclass(frozen=True)
class Gift:
    """What one door of the Christmas calendar holds."""
    id: str
    kind: str                  # "ting" (a wardrobe item) | "sne" (snow) | "mad" (a treat) | "vits" (a joke)
    name: str
    tekst: str
    ikon: str = "🎁"
    item: str | None = None    # ting: the item it gives for good
    points: float = 0.0        # mad: how much fuller Klippe gets
    sne_s: int = 0             # seconds of snow on the monitor (door 24 snows too)

    def public(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "kind": self.kind, "name": self.name, "tekst": self.tekst,
                               "ikon": self.ikon}
        if self.item:
            out["item"] = self.item
            out["reward"] = _item_dict(ITEMS_BY_ID[self.item])
        if self.points:
            out["points"] = self.points
        if self.sne_s:
            out["sne_s"] = self.sne_s
        return out


JUL_DOORS = 24
JUL_FIXED = {1: "sne1", 13: "lucia", 24: "julestjerne"}     # the same doors in every office
JULEKALENDER = [
    Gift("sne1", "sne", "Første sne", "Første sne! ❄️", "❄️", sne_s=45),
    Gift("lucia", "sne", "Luciadag", "Luciadag – sne og lys ✨", "🕯️", sne_s=45),
    Gift("julestjerne", "ting", "Julestjerne", "Glædelig jul! 🎄", "⭐", item="julestjerne", sne_s=60),
    Gift("halstoerklaede", "ting", "Halstørklæde", "Et lunt halstørklæde til de kolde dage 🧣", "🧣",
         item="halstoerklaede"),
    Gift("julesloejfe", "ting", "Julesløjfe", "En fin julesløjfe om halsen 🎀", "🎀", item="julesloejfe"),
    Gift("gevir", "ting", "Rensdyrgevir", "Et rensdyrgevir – nu kan Klippe trække kanen 🦌", "🦌", item="gevir"),
    Gift("glorie", "ting", "Engleglorie", "En engleglorie – Klippe har været sød i år 😇", "😇", item="glorie"),
    Gift("nissehue", "ting", "Nissehue", "En rigtig nissehue med kvast 🎅", "🎅", item="nissehue"),
    Gift("julebriller", "ting", "Julebriller", "Julebriller – alt ser lidt mere julet ud 🤓", "🤓",
         item="julebriller"),
    Gift("slikstok", "ting", "Slikstok", "En slikstok med røde striber 🍭", "🍭", item="slikstok"),
    Gift("julegave", "ting", "Julegave", "En julegave – hvad mon der er i? 🎁", "🎁", item="julegave"),
    Gift("snefnug", "ting", "Snefnug", "Snefnug, der danser rundt om Klippe ❄️", "❄️", item="snefnug"),
    Gift("julelys", "ting", "Julelys", "En kæde af julelys ✨", "✨", item="julelys"),
    Gift("julerod", "ting", "Julerød", "Klippe bliver julerød 🎄", "🔴", item="julerod"),
    Gift("polkagris", "ting", "Polkagris", "Striber som en polkagris 🍬", "🍬", item="polkagris"),
    Gift("aebleskiver", "mad", "Æbleskiver", "Æbleskiver med syltetøj og flormelis 😋", "🍩", points=25),
    Gift("risalamande", "mad", "Risalamande", "Risalamande med kirsebærsauce – hvem får mandlen? 🍒", "🍚",
         points=30),
    Gift("pebernoedder", "mad", "Pebernødder", "En håndfuld pebernødder 🍪", "🍪", points=10),
    Gift("brunkager", "mad", "Brunkager", "Sprøde brunkager 🍪", "🍪", points=10),
    Gift("klejner", "mad", "Klejner", "Klejner efter mormors opskrift 🥨", "🥨", points=10),
    Gift("julesodavand", "mad", "Julesodavand", "En kold julesodavand 🥤", "🥤", points=10),
    Gift("vits1", "vits", "Julevits", "Hvad får man, hvis man krydser en snemand med en vampyr? Frostbid! 🧛❄️",
         "😂"),
    Gift("vits2", "vits", "Julevits", "Hvad siger den ene snemand til den anden? „Kan du også lugte gulerødder?“ ☃️🥕",
         "😂"),
    Gift("vits3", "vits", "Julevits", "Hvorfor renderer julemanden altid i god tid? Han har kun én deadline om året! 🎅⏰",
         "😂"),
]
JULEKALENDER_BY_ID = {gift.id: gift for gift in JULEKALENDER}
JUL_ITEMS = frozenset(gift.item for gift in JULEKALENDER if gift.item)


def calendar_order(secret: str, year: int) -> list[Gift]:
    """The 24 doors' gifts on this PC this year: snow behind 1 and 13, the star behind 24, the
    rest shuffled by this PC's secret (colleagues open different things on the same day)."""
    fixed = {nr: JULEKALENDER_BY_ID[gift_id] for nr, gift_id in JUL_FIXED.items()}
    rest = [gift for gift in JULEKALENDER if gift.id not in JUL_FIXED.values()]
    random.Random(int(hashlib.sha256(f"{secret}|{year}|jul".encode()).hexdigest(), 16)).shuffle(rest)
    others = iter(rest)
    return [fixed.get(nr) or next(others) for nr in range(1, JUL_DOORS + 1)]


SCORE_MAX = 10_000_000         # the render game's scores (SPEC §23.10)
RECORD_MIN = 100               # a new best below this is not worth telling the office
OFFICE_RECORDS_MAX = 32        # colleagues' records kept
GAMES = ("hop", "skyd", "kryds")   # the render game's games: Hop, Fejljagt, Over tidslinjen
STAGE_HOURS = (("legend", 300.0), ("pro", 100.0), ("junior", 25.0), ("baby", 5.0))   # as the widget grows it
_PC_NAME = re.compile(r"[A-Z0-9-]{1,15}")
_NOT_PC = re.compile(r"[^A-Z0-9-]")


def stage_for(hours: float, hatched: bool = False) -> str:
    """Klippe's stage after ``hours`` of logged work (the widget's thresholds)."""
    for stage, threshold in STAGE_HOURS:
        if hours >= threshold:
            return stage
    return "baby" if hatched else "egg"


def _short_text(value: Any, limit: int) -> str:
    """Printable text, trimmed to ``limit`` characters ("" for anything that is not text)."""
    if not isinstance(value, str):
        return ""
    return "".join(c for c in value if c.isprintable()).strip()[:limit].strip()


def _clean_records(table: Any) -> dict[str, Any]:
    """The office's records of a game from pet.json – only rows with an int score."""
    if not isinstance(table, dict):
        return {}
    return {pc: row for pc, row in table.items() if isinstance(row, dict) and type(row.get("score")) is int}


def _own_pc() -> str:
    """This PC's name as the office knows it (like ``kontor.pc_name``)."""
    from . import config
    return _NOT_PC.sub("-", config.hostname().upper())[:15].strip("-") or "PC"


def next_midnight(now: float) -> float:
    """The next local midnight after ``now`` (a stolen hat comes back then)."""
    day = datetime.fromtimestamp(now).date() + timedelta(days=1)
    return datetime.combine(day, datetime.min.time()).timestamp()


def fyraften_hours(value: Any) -> float:
    """"16:00" → 16.0; "" or anything odd → 16.0 (SPEC §23.0: the office's fyraften)."""
    if isinstance(value, str):
        match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", value)
        if match and int(match.group(1)) < 24 and int(match.group(2)) < 60:
            return int(match.group(1)) + int(match.group(2)) / 60
    return 16.0


# --------------------------------------------------------------------------------------
# Food and hunger
# --------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Food:
    id: str
    name: str
    kind: str                  # "mad" (food) | "drik" (an energy drink) | "drop" (Booster on a drip)
    points: float              # how much fuller it makes Klippe (satiety 0–100)
    energy_min: float = 0.0    # minutes of energy rush
    mcd: bool = False          # from McDonald's

    @property
    def energy(self) -> bool:
        return self.energy_min > 0


MENU = [
    Food("durum", "Durum", "mad", 60),
    Food("bigmac", "Big Mac", "mad", 45, mcd=True),
    Food("nuggets", "Chicken McNuggets", "mad", 30, mcd=True),
    Food("pommes", "Pommes frites", "mad", 20, mcd=True),
    Food("booster", "Faxe Kondi Booster", "drik", 10, energy_min=20),
    Food("mangoloco", "Monster Mango Loco", "drik", 12, energy_min=25),
    Food("drop", "Booster-drop", "drop", 15, energy_min=45),
]
MENU_BY_ID = {food.id: food for food in MENU}

FULL = 100.0
START_SATIETY = 35.0       # a new Klippe is a little hungry: its first meal can come at once
WORK_BURN = 20.0           # satiety per hour of logged work: full to empty in 5 hours of work
REST_BURN = 4.0            # per hour otherwise (nights, weekends) …
REST_FLOOR = 30.0          # … but never below this: nobody comes back to a starving Klippe
TOO_FULL = 90.0            # no more food from here (a drink or a drip is fine)
DRINKS_MAX = 3             # energy drinks (and drips) …
DRINKS_WINDOW_S = 2 * 3600.0   # … within two hours, then its heart pounds
ENERGY_MAX_S = 3600.0      # a rush never lasts longer than an hour from now
MEAL_LOG_MAX = 3000        # a year of meals and more: "Klippet <år>" counts them


def satiety_after(satiety: float, work_s: float, rest_s: float) -> float:
    """How full Klippe is after ``work_s`` of logged work and ``rest_s`` of other time."""
    value = satiety - max(0.0, work_s) / 3600.0 * WORK_BURN
    if value > REST_FLOOR:
        value = max(REST_FLOOR, value - max(0.0, rest_s) / 3600.0 * REST_BURN)
    return max(0.0, min(FULL, value))


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _food_dict(food: Food) -> dict[str, Any]:
    return {"id": food.id, "name": food.name, "kind": food.kind, "points": food.points,
            "energy_min": food.energy_min, "mcd": food.mcd}


# --------------------------------------------------------------------------------------
# What happened (computed from the time tracking, the cards and Klippe's games)
# --------------------------------------------------------------------------------------

@dataclass
class Stats:
    total_s: float = 0.0
    days: dict[str, float] = field(default_factory=dict)            # ISO day → seconds
    day_buckets: dict[str, dict[str, float]] = field(default_factory=dict)
    day_first: dict[str, float] = field(default_factory=dict)       # ISO day → hour of day (8.5 = 08:30)
    day_last: dict[str, float] = field(default_factory=dict)
    buckets: dict[str, float] = field(default_factory=dict)
    projects: dict[str, float] = field(default_factory=dict)
    timelines: int = 0
    week_projects: int = 0           # most projects in one week
    focus_90: int = 0                # unbroken stretches of ≥ 90 minutes
    good_breaks: int = 0             # 5–30 min breaks after ≥ 85 min, work resumed
    weekday_streak: int = 0          # longest run of workdays (Mon–Fri) with ≥ 1 hour
    cards: int = 0
    card_bytes: int = 0
    moves: int = 0
    games: int = 0
    caught: int = 0
    eaten: dict[str, int] = field(default_factory=dict)            # menu item → times
    drinks_day: int = 0              # most energy drinks on one day
    hatched: bool = False
    goal_s: float = 6 * 3600.0
    today: str = ""
    # SPEC §23: what happened in the office (PetProgress.note), the game's best ("spil_bedste")
    # and the most calendar doors opened in one year ("jul_laager"); the office's fyraften (hours).
    counters: dict[str, int] = field(default_factory=dict)
    fyraften: float = 16.0

    @property
    def level(self) -> int:
        return 1 + int(math.floor(math.sqrt(self.total_s / 3600.0 * 2)))

    def worked(self, day: str) -> bool:
        return self.days.get(day, 0.0) >= DAY_MIN_S


def _hour_of_day(ts: float) -> float:
    moment = datetime.fromtimestamp(ts)
    return moment.hour + moment.minute / 60 + moment.second / 3600


def _split_days(start: float, end: float) -> list[tuple[str, float]]:
    parts = []
    cursor = start
    while cursor < end:
        day = datetime.fromtimestamp(cursor).date()
        midnight = datetime.combine(day + timedelta(days=1), datetime.min.time()).timestamp()
        stop = min(end, midnight)
        parts.append((day.isoformat(), stop - cursor))
        cursor = stop
    return parts


def _day_ends(start: float, end: float) -> list[tuple[str, float]]:
    """``(day, the hour this stretch ends that day)`` for every day it touches – 24.0 for a day it
    runs on past midnight, so a late evening is never "home on time" because of an earlier stop."""
    out = []
    cursor = start
    while cursor < end:
        day = datetime.fromtimestamp(cursor).date()
        midnight = datetime.combine(day + timedelta(days=1), datetime.min.time()).timestamp()
        stop = min(end, midnight)
        out.append((day.isoformat(), 24.0 if stop >= midnight else _hour_of_day(stop)))
        cursor = stop
    return out


def longest_weekday_run(worked_days: Iterable[str]) -> int:
    """The longest run of workdays (Mon–Fri) that all have work – weekends neither count nor
    break it, so nobody needs to work weekends for a streak."""
    days = sorted({date.fromisoformat(d) for d in worked_days if date.fromisoformat(d).weekday() < 5})
    best = run = 0
    previous: date | None = None
    for day in days:
        expected = previous + timedelta(days=1) if previous else None
        while expected is not None and expected.weekday() >= 5:
            expected += timedelta(days=1)
        run = run + 1 if expected == day else 1
        best = max(best, run)
        previous = day
    return best


def compute_stats(segments: Iterable[tuple], imports: Iterable[dict[str, Any]] = (), *,
                  counters: dict[str, Any] | None = None, eaten: dict[str, Any] | None = None,
                  meals: Iterable[tuple[float, str]] = (), hatched: bool = False,
                  goal_hours: float = 6.0, today: date | None = None, fyraften: float = 16.0) -> Stats:
    """``segments``: rows of TimeStore.between() (project, database, uid, folder, bucket, start,
    end, timeline); ``imports``: the import helper's history; ``eaten``: menu item → times;
    ``meals``: the latest meals as (time, item); ``counters``: PetProgress's counters (the games,
    and SPEC §23's); ``fyraften``: the hour the working day ends."""
    st = Stats(hatched=hatched, goal_s=goal_hours * 3600.0, today=(today or date.today()).isoformat(),
               fyraften=fyraften)
    rows = sorted((r for r in segments if r[6] > r[5]), key=lambda r: r[5])
    lines: set[tuple[str, str]] = set()
    weeks: dict[tuple[int, int], set[str]] = {}
    for project, _db, _uid, _folder, bucket, start, end, timeline in rows:
        length = end - start
        st.total_s += length
        st.buckets[bucket] = st.buckets.get(bucket, 0.0) + length
        st.projects[project] = st.projects.get(project, 0.0) + length
        if timeline:
            lines.add((project, timeline))
        for day, secs in _split_days(start, end):
            st.days[day] = st.days.get(day, 0.0) + secs
            per = st.day_buckets.setdefault(day, {})
            per[bucket] = per.get(bucket, 0.0) + secs
            iso = date.fromisoformat(day).isocalendar()
            weeks.setdefault((iso[0], iso[1]), set()).add(project)
        first = datetime.fromtimestamp(start).date().isoformat()
        st.day_first[first] = min(st.day_first.get(first, 99.0), _hour_of_day(start))
        for day, hour in _day_ends(start, end):
            st.day_last[day] = max(st.day_last.get(day, 0.0), hour)
    st.timelines = len(lines)
    st.week_projects = max((len(p) for p in weeks.values()), default=0)
    # Unbroken stretches: segments less than 2 minutes apart are one stretch.
    blocks: list[list[float]] = []
    for row in rows:
        if blocks and row[5] - blocks[-1][1] <= 120:
            blocks[-1][1] = max(blocks[-1][1], row[6])
        else:
            blocks.append([row[5], row[6]])
    st.focus_90 = sum(1 for s, e in blocks if e - s >= 90 * 60)
    st.good_breaks = sum(1 for (s1, e1), (s2, e2) in zip(blocks, blocks[1:])
                         if e1 - s1 >= 85 * 60 and 5 * 60 <= s2 - e1 <= 30 * 60 and e2 - s2 >= 15 * 60)
    st.weekday_streak = longest_weekday_run(d for d in st.days if st.worked(d))
    for entry in imports:
        if entry.get("mode") in ("copy", "move") and int(entry.get("files") or 0) > 0:
            st.cards += 1
            st.card_bytes += int(entry.get("bytes") or 0)
            st.moves += entry.get("mode") == "move"
    counters = counters or {}
    st.counters = {k: int(v) for k, v in counters.items() if isinstance(k, str) and _number(v)}
    st.games = st.counters.get("games", 0)
    st.caught = st.counters.get("caught", 0)
    st.eaten = {k: int(v) for k, v in (eaten or {}).items() if k in MENU_BY_ID and isinstance(v, int)}
    drinks: dict[str, int] = {}
    for at, item in meals:
        if item in MENU_BY_ID and MENU_BY_ID[item].energy:
            day = datetime.fromtimestamp(at).date().isoformat()
            drinks[day] = drinks.get(day, 0) + 1
    st.drinks_day = max(drinks.values(), default=0)
    return st


# --------------------------------------------------------------------------------------
# The trophies
# --------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Trophy:
    id: str
    group: str
    name: str
    text: str
    goal: float
    measure: Callable[[Stats], float]
    reward: str | None = None
    secret: bool = False
    unit: str = ""


def _hours(bucket: str) -> Callable[[Stats], float]:
    return lambda st: st.buckets.get(bucket, 0.0) / 3600.0


def _days(test: Callable[[Stats, str], bool]) -> Callable[[Stats], float]:
    return lambda st: sum(1 for d in st.days if test(st, d))


def _any_day(test: Callable[[Stats, str, date], bool]) -> Callable[[Stats], float]:
    return lambda st: float(any(test(st, d, date.fromisoformat(d)) for d in st.days))


def _count(name: str) -> Callable[[Stats], float]:
    return lambda st: st.counters.get(name, 0)


def _home_on_time(st: Stats, d: str) -> bool:
    """A weekday with work that ended at most a quarter of an hour after fyraften."""
    return (st.worked(d) and date.fromisoformat(d).weekday() < 5
            and st.day_last.get(d, 24.0) <= st.fyraften + 0.25)


def _goal_reached(st: Stats, d: str) -> bool:
    return st.days.get(d, 0.0) >= st.goal_s


def _allround(st: Stats, d: str) -> bool:
    b = st.day_buckets.get(d, {})
    edit = b.get("edit", 0.0) + b.get("cut", 0.0)
    return min(edit, *(b.get(k, 0.0) for k in ("color", "fusion", "fairlight", "deliver"))) >= 300


def _on_the_minute(st: Stats, d: str) -> bool:
    return d != st.today and abs(st.days.get(d, 0.0) - st.goal_s) <= 60


TB = 1000 ** 4

TROPHIES = [
    # Growth
    Trophy("klaekket", "Vækst", "Klækket!", "Ægget er klækket", 1, lambda st: float(st.hatched or st.total_s >= 5 * 3600), "sloejfe"),
    Trophy("lv5", "Vækst", "Level 5", "Nå level 5", 5, lambda st: st.level, "orange", unit="level"),
    Trophy("lv10", "Vækst", "Level 10", "Nå level 10", 10, lambda st: st.level, "skov", unit="level"),
    Trophy("lv15", "Vækst", "Level 15", "Nå level 15", 15, lambda st: st.level, "baret", unit="level"),
    Trophy("lv20", "Vækst", "Level 20", "Nå level 20", 20, lambda st: st.level, "havblaa", unit="level"),
    Trophy("lv25", "Vækst", "Level 25", "Nå level 25", 25, lambda st: st.level, "guldstriber", unit="level"),
    Trophy("legende", "Vækst", "Legende", "300 timer i alt", 300, lambda st: st.total_s / 3600, "guld", unit="t"),
    # Rhythm – workdays only: weekends never count, never break a streak
    Trophy("uge", "Rytme", "En hel uge", "5 hverdage i træk med mindst 1 time", 5, lambda st: st.weekday_streak, "varm", unit="dage"),
    Trophy("maaned", "Rytme", "Stabil som et stativ", "20 hverdage i træk med mindst 1 time", 20, lambda st: st.weekday_streak, "blaa", unit="dage"),
    Trophy("kvartal", "Rytme", "Maraton – med pauser", "60 hverdage i træk med mindst 1 time", 60,
           lambda st: st.weekday_streak, "solbriller", unit="dage"),
    Trophy("morgen", "Rytme", "Morgenfrisk", "I gang før kl. 8 på 5 dage", 5,
           _days(lambda st, d: st.worked(d) and st.day_first.get(d, 99) < 8), "kaffe", unit="dage"),
    # The day's goal – and going home
    Trophy("maal1", "Mål", "Mål!", "Nå dagens mål", 1, _days(_goal_reached), "festhat", unit="dage"),
    Trophy("maal10", "Mål", "Målmaskine", "Nå dagens mål på 10 dage", 10, _days(_goal_reached), "roed", unit="dage"),
    Trophy("maal50", "Mål", "Dagens helt", "Nå dagens mål på 50 dage", 50, _days(_goal_reached), "bordeaux", unit="dage"),
    Trophy("fyraften", "Mål", "Fyraften til tiden", "Nå dagens mål og stop før kl. 17.30 – 5 gange", 5,
           _days(lambda st, d: _goal_reached(st, d) and st.day_last.get(d, 24) <= 17.5), "lavendel", unit="dage"),
    Trophy("hjemtid", "Mål", "Hjem til tiden", "Stop senest et kvarter efter fyraften på 10 hverdage", 10,
           _days(_home_on_time), "nathue", unit="dage"),
    # Focus – and breaks
    Trophy("flow", "Fokus", "I flow", "90 minutter uden afbrydelse", 1, lambda st: st.focus_90),
    Trophy("flow10", "Fokus", "Flow-tilstand", "90 minutter uden afbrydelse – 10 gange", 10, lambda st: st.focus_90, "mint", unit="gange"),
    Trophy("pause", "Fokus", "Pausemester", "Hold 5–30 minutters pause efter halvanden times arbejde – 10 gange", 10,
           lambda st: st.good_breaks, "hjerter", unit="pauser"),
    # Resolve's pages
    Trophy("color10", "Sider", "Farvelægger", "10 timer på Color", 10, _hours("color"), "kobber", unit="t"),
    Trophy("fusion10", "Sider", "Troldmand", "10 timer i Fusion", 10, _hours("fusion"), unit="t"),
    Trophy("fairlight10", "Sider", "Lydnørd", "10 timer i Fairlight", 10, _hours("fairlight"), unit="t"),
    Trophy("deliver", "Sider", "Afsender", "3 timer på Deliver", 3, _hours("deliver"), unit="t"),
    Trophy("musik", "Sider", "Musikjæger", "5 timer på musiksider", 5, _hours("musik"), unit="t"),
    Trophy("ai", "Sider", "Prompt-instruktør", "5 timer på AI-sider", 5, _hours("ai"), unit="t"),
    Trophy("allround", "Sider", "Allround", "Edit, Color, Fusion, Fairlight og Deliver på samme dag", 1,
           _days(_allround), "slikkepind"),
    # Projects
    Trophy("bolde", "Projekter", "Mange bolde i luften", "5 projekter i samme uge", 5, lambda st: st.week_projects,
           "kold", unit="projekter"),
    Trophy("trofast", "Projekter", "Trofast", "40 timer på ét projekt", 40,
           lambda st: max(st.projects.values(), default=0.0) / 3600, unit="t"),
    Trophy("tidslinjer", "Projekter", "Tidslinje-tæmmer", "Arbejd i 25 forskellige tidslinjer", 25,
           lambda st: st.timelines, "hornbriller", unit="tidslinjer"),
    # Cards
    Trophy("kort1", "Kort", "Første kort", "Overfør et kort med Projektsøg", 1, lambda st: st.cards),
    Trophy("kort25", "Kort", "Kortbærer", "25 kort overført", 25, lambda st: st.cards, unit="kort"),
    Trophy("kort100", "Kort", "Kortmester", "100 kort overført", 100, lambda st: st.cards, unit="kort"),
    Trophy("tb1", "Kort", "Terabyte", "1 TB overført og kontrolleret", 1, lambda st: st.card_bytes / TB, unit="TB"),
    Trophy("klip10", "Kort", "Klip-klap", "10 kort flyttet med Klip", 10, lambda st: st.moves, unit="kort"),
    # Klippe
    Trophy("leg10", "Klippe", "Legekammerat", "Klippe har leget med musen 10 gange", 10, lambda st: st.games, unit="lege"),
    Trophy("fanget", "Klippe", "Fanget!", "Fang Klippe i at lege 5 gange", 5, lambda st: st.caught, unit="gange"),
    Trophy("racer", "Klippe", "Render-racer", "Få 1000 point i render-spillet", 1000, _count("spil_bedste"),
           "racerbriller", unit="point"),
    Trophy("bad1", "Klippe", "Skrubbet ren", "Ryd op i et projekt, så Klippe kan komme i bad", 1, _count("bade"),
           "badeand"),
    Trophy("bad10", "Klippe", "Skumbad", "Giv Klippe 10 skumbade", 10, _count("bade"), "bobler", unit="bade"),
    # Food
    Trophy("velbekomme", "Mad", "Velbekomme", "Giv Klippe noget at spise eller drikke", 1,
           lambda st: sum(st.eaten.values())),
    Trophy("durum10", "Mad", "Durumkongen", "Giv Klippe 10 durum", 10, lambda st: st.eaten.get("durum", 0),
           "durum", unit="durum"),
    Trophy("mcd10", "Mad", "Stamkunde", "Giv Klippe 10 ting fra McDonald's", 10,
           lambda st: sum(n for k, n in st.eaten.items() if MENU_BY_ID[k].mcd), "pommes", unit="ting"),
    Trophy("booster10", "Mad", "Booster-holdet", "Klippe har drukket 10 Faxe Kondi Booster", 10,
           lambda st: st.eaten.get("booster", 0), "booster", unit="dåser"),
    Trophy("mango10", "Mad", "Loco for mango", "Klippe har drukket 10 Monster Mango Loco", 10,
           lambda st: st.eaten.get("mangoloco", 0), "mangoloco", unit="dåser"),
    # The office (SPEC §23.7)
    Trophy("frokost10", "Kontoret", "Frokostklokken", "Ring til frokost 10 gange", 10, _count("frokost_ringet"),
           "klokke", unit="gange"),
    Trophy("postbud", "Kontoret", "Postbud", "Send Klippe med post 10 gange", 10, _count("post_sendt"), "brev",
           unit="breve"),
    Trophy("spaamand", "Kontoret", "Spåmand", "Vind 3 render-væddemål", 3, _count("vaeddemaal_vundet"),
           "krystalkugle", unit="sejre"),
    # Seasons: only in their season
    Trophy("jul", "Sæson", "Juleklipper", "Arbejd mindst en time en dag i december", 1,
           _any_day(lambda st, d, day: day.month == 12 and st.worked(d)), "nissehue"),
    Trophy("foraar", "Sæson", "Forårsklip", "Arbejd mindst en time en dag i april", 1,
           _any_day(lambda st, d, day: day.month == 4 and st.worked(d)), "blomst"),
    Trophy("nytaar", "Sæson", "Godt nytår", "Arbejd mindst en time i årets første uge", 1,
           _any_day(lambda st, d, day: day.month == 1 and day.day <= 7 and st.worked(d))),
    Trophy("halloween", "Sæson", "Uhyggelig klipper", "Arbejd mindst en time i Halloween-ugen", 1,
           _any_day(lambda st, d, day: halloween_week(day) and st.worked(d)), "heksehat"),
    Trophy("slikmutter", "Sæson", "Slikmutter", "Giv 5 Boostere til kollegernes Klipper til Halloween", 5,
           _count("slik_givet"), "graeskar", unit="Boostere"),
    Trophy("julekalender", "Sæson", "Alle låger", "Åbn alle 24 låger i julekalenderen", 24, _count("jul_laager"),
           unit="låger"),
    # Secret
    Trophy("fredag13", "Hemmelig", "Fredag den 13.", "Arbejdede på en fredag den 13.", 1,
           _any_day(lambda st, d, day: day.weekday() == 4 and day.day == 13 and st.worked(d)), "zebra", secret=True),
    Trophy("skuddag", "Hemmelig", "Skuddag", "Arbejdede den 29. februar", 1,
           _any_day(lambda st, d, day: day.month == 2 and day.day == 29 and st.days[d] >= 1800), "propelhat", secret=True),
    Trophy("praecis", "Hemmelig", "På minuttet", "En dag, der endte præcis på dagens mål", 1, _days(_on_the_minute), secret=True),
    Trophy("sukkerchok", "Hemmelig", "Sukkerchok", "3 energidrikke på én dag", 3, lambda st: st.drinks_day, "lyn",
           secret=True),
    Trophy("ballademager", "Hemmelig", "Ballademager", "Stjal en hat til Halloween", 1, _count("hatte_stjaalet"),
           "vampyrtaender", secret=True),
    Trophy("fejlfri", "Hemmelig", "Fejlfri", "3000 point i render-spillet", 3000, _count("spil_bedste"),
           "racerhjelm", secret=True),
]
TROPHIES_BY_ID = {t.id: t for t in TROPHIES}


def find_roll(secret: str, day: str, item: str) -> float:
    """This PC's dice for ``item`` on ``day``: 0 ≤ x < 1, the same every time it is asked."""
    digest = hashlib.sha256(f"{secret}|{day}|{item}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2 ** 64


def finds(secret: str, st: Stats) -> dict[str, str]:
    """``{item: day}``: the rare items this PC found – on workdays with at least an hour."""
    found: dict[str, str] = {}
    for day in sorted(st.days):
        if not st.worked(day) or date.fromisoformat(day).weekday() >= 5:
            continue
        for item, chance in FINDS:
            if item not in found and find_roll(secret, day, item) < chance:
                found[item] = day
    return found


# --------------------------------------------------------------------------------------
# Keeping track (main process)
# --------------------------------------------------------------------------------------

class PetProgress:
    """Recomputes the trophies now and then, keeps them and the chosen wardrobe in pet.json."""

    def __init__(self, cfg: Any, bus: Any, *, tracker: Any = None, importer: Any = None,
                 path: str | None = None, clock: Callable[[], float] = time.time,
                 today: Callable[[], date] = date.today) -> None:
        self.cfg = cfg
        self.bus = bus
        self._tracker = tracker
        self._importer = importer
        self._path = path
        self._clock = clock
        self._today = today
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._data = self._load()
        self._stats: Stats | None = None
        # Snow on the monitor (SPEC §23.5): the app sets it to Sne.start(seconds, demo=False).
        self.on_snow: Callable[..., Any] | None = None

    # -- lifecycle --------------------------------------------------------------------------
    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="pet-progress", daemon=True)
            self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()

    def _run(self) -> None:
        if self._stop.wait(FIRST_REFRESH_S):
            return
        while not self._stop.is_set():
            try:
                self.refresh()
            except Exception:
                log.exception("updating Klippe's trophies failed")
            self._wake.wait(REFRESH_S)
            self._wake.clear()

    # -- storage ------------------------------------------------------------------------------
    def _load(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        if self._path:
            try:
                with open(self._path, encoding="utf-8") as fh:
                    loaded = json.load(fh)
                if isinstance(loaded, dict):
                    data = loaded
            except FileNotFoundError:
                pass
            except (OSError, ValueError) as exc:
                log.warning("could not read %s: %s", self._path, exc)
        if not isinstance(data.get("secret"), str) or len(data["secret"]) < 16:
            data["secret"] = secrets.token_hex(16)          # this PC's own luck
        for key in ("unlocked", "found", "equipped", "counters", "mad", "halloween", "jul", "spil", "aar"):
            if not isinstance(data.get(key), dict):
                data[key] = {}
        # SPEC §23: per-year counters, the calendar's doors per year, its gifts and the game.
        data["aar"] = {year: per for year, per in data["aar"].items() if isinstance(per, dict)}
        data["jul"] = {year: doors for year, doors in data["jul"].items() if isinstance(doors, dict)}
        gifts = data.get("julegaver")
        data["julegaver"] = list(dict.fromkeys(i for i in gifts if i in JUL_ITEMS)) if isinstance(gifts, list) else []
        spil = data["spil"]
        for key in ("bedste", "spillet"):
            if type(spil.get(key)) is not int or spil[key] < 0:
                spil[key] = 0
        if not isinstance(spil.get("kontor"), dict):
            spil["kontor"] = {}
        spil["kontor"] = _clean_records(spil["kontor"])
        # The other games (not Hop) keep their own best and the office's (SPEC §23.10).
        andre = spil.get("andre") if isinstance(spil.get("andre"), dict) else {}
        spil["andre"] = {}
        for game in GAMES[1:]:
            own = andre.get(game) if isinstance(andre.get(game), dict) else {}
            best = own.get("bedste")
            spil["andre"][game] = {"bedste": best if type(best) is int and best >= 0 else 0,
                                   "bedste_ved": own.get("bedste_ved"), "kontor": _clean_records(own.get("kontor"))}
        mad = data["mad"]
        if not isinstance(mad.get("spist"), dict):
            mad["spist"] = {}
        if not isinstance(mad.get("log"), list):
            mad["log"] = []
        mad["log"] = [entry for entry in mad["log"] if isinstance(entry, list) and len(entry) == 2
                      and isinstance(entry[0], (int, float)) and entry[1] in MENU_BY_ID]
        return data

    def _save(self) -> None:
        if not self._path:
            return
        temp = self._path + ".tmp"
        try:
            with open(temp, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, ensure_ascii=False, indent=1)
            os.replace(temp, self._path)
        except OSError as exc:
            log.warning("could not save Klippe's trophies: %s", exc)

    # -- computing ----------------------------------------------------------------------------
    def stats(self) -> Stats:
        rows: list[tuple] = []
        store = getattr(self._tracker, "store", None)
        if store is not None:
            rows = store.between(0.0, self._clock() + 86400)
        imports: list[dict[str, Any]] = []
        if self._importer is not None:
            try:
                imports = self._importer.history(10_000)
            except Exception:
                log.debug("no import history", exc_info=True)
        with self._lock:
            counters = dict(self._data["counters"])
            counters["spil_bedste"] = self._data["spil"]["bedste"]
            counters["jul_laager"] = max((len(doors) for doors in self._data["jul"].values()), default=0)
            eaten = dict(self._data["mad"]["spist"])
            meals = [(entry[0], entry[1]) for entry in self._data["mad"]["log"]]
        return compute_stats(rows, imports, counters=counters, eaten=eaten, meals=meals,
                             hatched=bool(self.cfg.get("widget_hatched", False)),
                             goal_hours=float(self.cfg.get("widget_daily_goal_hours", 6) or 6),
                             today=self._today(), fyraften=fyraften_hours(self.cfg.get("widget_fyraften", "16:00")))

    def refresh(self) -> list[dict[str, Any]]:
        """Recompute; new trophies and finds are kept and announced (``pet_progress``)."""
        st = self.stats()
        now = self._clock()
        work = self._work_s()
        fresh: list[dict[str, Any]] = []
        with self._lock:
            self._hunger(now, work)                 # kept up to date: a restart goes on from here
            first = not self._data["unlocked"] and not self._data.get("computed")
            for trophy in TROPHIES:
                if trophy.id not in self._data["unlocked"] and trophy.measure(st) >= trophy.goal:
                    self._data["unlocked"][trophy.id] = now
                    fresh.append(self._news(trophy=trophy))
            for item, day in finds(self._data["secret"], st).items():
                if item not in self._data["found"]:
                    self._data["found"][item] = {"at": now, "day": day}
                    fresh.append(self._news(item=ITEMS_BY_ID[item], day=day))
            self._stats = st
            self._data["computed"] = now
            self._save()
        if fresh:
            log.info("Klippe: %s", ", ".join(n["name"] for n in fresh))
            self.bus.publish("pet_progress", {"nye": fresh, "foerste": first, **self.counts()})
        return fresh

    @staticmethod
    def _news(*, trophy: Trophy | None = None, item: Item | None = None, day: str | None = None) -> dict[str, Any]:
        if trophy is not None:
            reward = ITEMS_BY_ID.get(trophy.reward) if trophy.reward else None
            return {"kind": "trofae", "id": trophy.id, "name": trophy.name, "text": trophy.text,
                    "reward": _item_dict(reward) if reward else None,
                    "rarity": reward.rarity if reward else "almindelig"}
        assert item is not None
        return {"kind": "fund", "id": item.id, "name": item.name, "text": f"Fundet {day}",
                "reward": _item_dict(item), "rarity": item.rarity}

    # -- wardrobe -----------------------------------------------------------------------------
    def owned(self) -> set[str]:
        with self._lock:
            have = {i.id for i in ITEMS if i.default}
            have |= {TROPHIES_BY_ID[t].reward for t in self._data["unlocked"]
                     if t in TROPHIES_BY_ID and TROPHIES_BY_ID[t].reward}
            have |= {i for i in self._data["found"] if i in ITEMS_BY_ID}
            have |= set(self._data["julegaver"])
            return have

    def equipped(self) -> dict[str, str]:
        owned = self.owned()
        with self._lock:
            chosen = self._data["equipped"]
            return {slot: chosen[slot] if chosen.get(slot) in owned and ITEMS_BY_ID[chosen[slot]].slot == slot
                    else DEFAULTS[slot] for slot in SLOTS}

    def equip(self, slot: Any, item: Any) -> dict[str, Any]:
        if slot not in SLOTS:
            raise ValueError("Ugyldig værdi: slot")
        thing = ITEMS_BY_ID.get(item) if isinstance(item, str) else None
        if thing is None or thing.slot != slot:
            raise ValueError("Ugyldig værdi: item")
        if thing.id not in self.owned():
            raise ValueError(f"{thing.name} er ikke låst op endnu")
        with self._lock:
            self._data["equipped"][slot] = thing.id
            self._save()
        equipped = self.equipped()
        self.bus.publish("pet_look", {"equipped": equipped})
        return {"ok": True, "equipped": equipped}

    def note_game(self, reason: str, was_out: bool) -> None:
        """A game ended (petplay): it counts when Klippe was out; "touched" = caught playing."""
        if not was_out:
            return
        with self._lock:
            self._bump("games")
            if reason == "touched":
                self._bump("caught")
            self._save()
        self._wake.set()

    # -- what happened in the office (SPEC §23.1) ----------------------------------------------
    def note(self, name: str, n: int = 1) -> None:
        """Counts ``name`` (``frokost_ringet``, ``post_sendt``, ``bade`` …) – in all and for this
        year ("Klippet <år>"). The trophies are recomputed soon."""
        if not isinstance(name, str) or not name or type(n) is not int or n < 1:
            raise ValueError("Ugyldig tæller")
        with self._lock:
            self._bump(name, n)
            self._save()
        self._wake.set()

    def _bump(self, name: str, n: int = 1) -> None:
        """(Under the lock.) ``counters[name]`` and this year's ``aar[year][name]`` go up by ``n``."""
        counters = self._data["counters"]
        counters[name] = int(counters.get(name) or 0) + n
        per = self._data["aar"].setdefault(str(self._today().year), {})
        per[name] = int(per.get(name) or 0) + n

    def treat(self, points: Any, name: Any = "") -> dict[str, Any]:
        """A treat from outside the menu (a Booster from a colleague, æbleskiver from the calendar):
        satiety + ``points`` (≤ 100), no meal is logged. Publishes ``pet_mad``."""
        if not _number(points) or not 0 < points <= FULL:
            raise ValueError("Ugyldig værdi: points")
        now = self._clock()
        work = self._work_s()
        with self._lock:
            mad = self._hunger(now, work)
            mad["maet"] = min(FULL, float(mad["maet"]) + float(points))
            self._save()
            state = self._food_state(mad, now)
        log.info("Klippe got a treat: %s (+%s)", name, points)
        self.bus.publish("pet_mad", state)
        return state

    # -- Halloween (SPEC §23.1, §23.3) ----------------------------------------------------------
    def _expire_pranks(self, now: float) -> bool:
        """(Under the lock.) Drops a stolen or borrowed hat whose day is over; True when changed."""
        record = self._data["halloween"]
        changed = False
        for key in ("ballade", "bytte"):
            value = record.get(key)
            if value is not None and not (isinstance(value, dict) and _number(value.get("til")) and value["til"] > now):
                record[key] = None
                changed = True
        return changed

    def season_state(self, today: date | None = None) -> dict[str, Any]:
        """``GET /api/pet/saeson``: the season, this PC's costume, a hat stolen from Klippe today
        (``ballade``) and a hat it stole itself (``bytte``)."""
        day = today or self._today()
        now = self._clock()
        with self._lock:
            if self._expire_pranks(now):
                self._save()
            record = self._data["halloween"]
            ballade = dict(record["ballade"]) if isinstance(record.get("ballade"), dict) else None
            bytte = dict(record["bytte"]) if isinstance(record.get("bytte"), dict) else None
            secret = self._data["secret"]
        current = season(day)
        mine = costume(secret, day.year)
        return {"saeson": current, "kostume": mine if current == "halloween" else None, "kostume_aar": mine,
                "ballade": ballade, "bytte": bytte}

    def prank_received(self, af_pc: Any, af_navn: Any) -> dict[str, Any]:
        """Trick or treat without a treat: the hat Klippe wears is "stolen" until midnight (the
        widget hides it); without a hat (or with it stolen already) Klippe is wrapped in toilet
        paper. Returns what this prank did: ``{hat, toiletpapir}``."""
        now = self._clock()
        hat = self.equipped()["hat"]
        with self._lock:
            self._expire_pranks(now)
            record = self._data["halloween"]
            current = record.get("ballade") if isinstance(record.get("ballade"), dict) else {}
            stolen = current.get("hat")
            if hat != DEFAULTS["hat"] and not stolen:
                result = {"hat": hat, "toiletpapir": False}
            else:
                result = {"hat": None, "toiletpapir": True}
            record["ballade"] = {"hat": stolen or result["hat"],
                                 "toiletpapir": bool(current.get("toiletpapir")) or result["toiletpapir"],
                                 "af_pc": _short_text(af_pc, 15), "af_navn": _short_text(af_navn, 20) or "Klippe",
                                 "til": next_midnight(now)}
            self._save()
        self.bus.publish("pet_saeson", self.season_state())
        return result

    def prank_won(self, hat: Any, fra_pc: Any, fra_navn: Any) -> dict[str, Any]:
        """Klippe stole ``hat`` from a colleague's Klippe: it wears it until midnight (shown only,
        never owned) and it counts for "Ballademager". A toilet-paper prank brings nothing home."""
        item = ITEMS_BY_ID.get(hat) if isinstance(hat, str) else None
        if item is None or item.slot != "hat" or item.default:
            return self.season_state()
        now = self._clock()
        with self._lock:
            self._data["halloween"]["bytte"] = {"hat": item.id, "fra_pc": _short_text(fra_pc, 15),
                                                "fra_navn": _short_text(fra_navn, 20) or "Klippe",
                                                "til": next_midnight(now)}
            self._bump("hatte_stjaalet")
            self._save()
        self._wake.set()
        state = self.season_state()
        self.bus.publish("pet_saeson", state)
        return state

    # -- the Christmas calendar (SPEC §23.1, §23.4) ----------------------------------------------
    def _calendar_state(self, day: date) -> dict[str, Any]:
        """(Under the lock.)"""
        opened = self._data["jul"].get(str(day.year), {})
        active = day.month == 12
        today = day.day if active else None
        doors = []
        for nr, gift in enumerate(calendar_order(self._data["secret"], day.year), 1):
            is_open = str(nr) in opened
            doors.append({"nr": nr, "aaben": is_open, "kan": active and nr <= day.day and not is_open,
                          "gave": gift.public() if is_open else None})
        return {"aar": day.year, "aktiv": active, "dag": today, "uaabnede": sum(1 for d in doors if d["kan"]),
                "laager": doors}

    def calendar(self, today: date | None = None) -> dict[str, Any]:
        """``GET /api/pet/jul``: the 24 doors this year – what the opened ones held, which can be
        opened now (December, up to today's date; missed ones until 31 December)."""
        day = today or self._today()
        with self._lock:
            return self._calendar_state(day)

    def open_door(self, nr: Any) -> dict[str, Any]:
        """``POST /api/pet/jul {nr}``: opens a door – a wardrobe item for good (announced like a
        find), a treat, snow on the monitor, or a joke. Publishes ``pet_jul``."""
        if type(nr) is not int or not 1 <= nr <= JUL_DOORS:
            raise ValueError("Ugyldig værdi: nr")
        day = self._today()
        now = self._clock()
        news = None
        with self._lock:
            if day.month != 12:
                raise ValueError("Julekalenderen er kun åben i december")
            if nr > day.day:
                raise ValueError(f"Låge {nr} kan først åbnes den {nr}. december")
            opened = self._data["jul"].setdefault(str(day.year), {})
            if str(nr) in opened:
                raise ValueError("Lågen er allerede åbnet")
            gift = calendar_order(self._data["secret"], day.year)[nr - 1]
            opened[str(nr)] = now
            if gift.item:
                if gift.item not in self._data["julegaver"]:
                    self._data["julegaver"].append(gift.item)
                item = ITEMS_BY_ID[gift.item]
                news = {"kind": "jul", "id": item.id, "name": item.name, "text": f"Fra julekalenderen, låge {nr}",
                        "reward": _item_dict(item), "rarity": item.rarity}
            if gift.sne_s:
                self._bump("sne")
            self._save()
            state = self._calendar_state(day)
        log.info("Klippe opened door %d: %s", nr, gift.name)
        if news is not None:
            self.bus.publish("pet_progress", {"nye": [news], "foerste": False, **self.counts()})
        if gift.kind == "mad" and gift.points:
            self.treat(gift.points, gift.name)
        if gift.sne_s:
            self._snow(gift.sne_s, False)
        self.bus.publish("pet_jul", state)
        self._wake.set()                            # "Alle låger"
        return {"ok": True, "gave": gift.public(), "jul": state}

    def calendar_demo(self, nr: Any) -> dict[str, Any]:
        """``POST /api/pet/jul/demo {nr}``: what that door holds this year – nothing is kept (a
        snowy door snows 20 s, as a demo)."""
        if type(nr) is not int or not 1 <= nr <= JUL_DOORS:
            raise ValueError("Ugyldig værdi: nr")
        day = self._today()
        with self._lock:
            gift = calendar_order(self._data["secret"], day.year)[nr - 1]
        if gift.sne_s:
            self._snow(20, True)
        return {"ok": True, "gave": gift.public(), "demo": True}

    def _snow(self, seconds: float, demo: bool) -> None:
        snow = self.on_snow
        if snow is None:
            return
        try:
            snow(seconds, demo=demo)
        except Exception:
            log.debug("no snow on the monitor", exc_info=True)

    # -- the render game (SPEC §23.10) ------------------------------------------------------------
    def _game_records(self, game: str) -> dict[str, Any]:
        """The records of one game (under the lock): Hop's live at the top of ``spil``."""
        spil = self._data["spil"]
        return spil if game == "hop" else spil["andre"][game]

    def game_result(self, score: Any, demo: Any = False, game: Any = "hop") -> dict[str, Any]:
        """``POST /api/pet/spil {score, demo, spil}``: a round of one of the render game's games.
        A demo round keeps nothing. ``ny_rekord``: beats this PC's best in that game (≥ 100);
        ``kontor_rekord``: and every colleague's record too. Only Hop counts for the trophies
        and the year."""
        if type(score) is not int or not 0 <= score <= SCORE_MAX:
            raise ValueError("Ugyldig værdi: score")
        if game not in GAMES:
            raise ValueError("Ugyldig værdi: spil")
        if demo:
            state = self.game_state(game)
            return {"ok": True, "bedste": state["bedste"], "ny_rekord": False, "kontor_rekord": False, "spil": state}
        now = self._clock()
        with self._lock:
            spil = self._data["spil"]
            records = self._game_records(game)
            best = records["bedste"]
            spil["spillet"] += 1
            self._bump("spil")
            if game == "hop":
                per = self._data["aar"].setdefault(str(self._today().year), {})
                per["spil_bedste"] = max(int(per.get("spil_bedste") or 0), score)
            if score > best:
                records["bedste"], records["bedste_ved"] = score, now
            record = score > best and score >= RECORD_MIN
            others = max((row["score"] for row in records["kontor"].values()), default=0)
            self._save()
        self._wake.set()                            # "Render-racer", "Fejlfri"
        state = self.game_state(game)
        return {"ok": True, "bedste": state["bedste"], "ny_rekord": record,
                "kontor_rekord": record and score > others, "spil": state}

    def office_record(self, pc: Any, navn: Any, score: Any, game: Any = "hop") -> bool:
        """A colleague's Klippe has a new record in a game (``rekord``/``spilrekord`` from the
        office): kept as that PC's best. True when it beats everyone – this PC included."""
        if not isinstance(pc, str) or not _PC_NAME.fullmatch(pc) or type(score) is not int \
                or not 0 < score <= SCORE_MAX or game not in GAMES:
            return False
        now = self._clock()
        with self._lock:
            records = self._game_records(game)
            table = records["kontor"]
            old = table.get(pc)
            if old is not None and old["score"] >= score:
                return False
            best_other = max([row["score"] for key, row in table.items() if key != pc] + [records["bedste"]])
            table[pc] = {"navn": _short_text(navn, 20) or "Klippe", "score": score, "ved": now}
            while len(table) > OFFICE_RECORDS_MAX:
                del table[min(table, key=lambda key: (table[key]["score"], table[key].get("ved") or 0))]
            self._save()
        return score > best_other

    def game_state(self, game: Any = "hop") -> dict[str, Any]:
        """``GET /api/pet/spil?spil=…``: this PC's best in a game, rounds played (all games) and
        the office's best (≤ 10)."""
        if game not in GAMES:
            raise ValueError("Ugyldig værdi: spil")
        with self._lock:
            records = self._game_records(game)
            rows = [{"pc": pc, "navn": row.get("navn") or "Klippe", "score": row["score"], "ved": row.get("ved"),
                     "egen": False} for pc, row in records["kontor"].items()]
            best, when = records["bedste"], records.get("bedste_ved")
            played = self._data["spil"]["spillet"]
        if best > 0:
            rows.append({"pc": _own_pc(), "navn": _short_text(self.cfg.get("widget_pet_name", "Klippe"), 20) or "Klippe",
                         "score": best, "ved": when, "egen": True})
        rows.sort(key=lambda row: (-row["score"], row["ved"] or 0))
        return {"spil": game, "bedste": best, "spillet": played, "kontor": rows[:10]}

    # -- Klippet <år> (SPEC §23.11) ------------------------------------------------------------------
    def year_review(self, year: int) -> dict[str, Any]:
        """The data of one year for ``aar.year_review``: its time, cards, meals, counters and
        trophies – and Klippe as it is now."""
        from . import aar
        if type(year) is not int or not 2000 <= year <= 9000:
            raise ValueError("Det år findes ikke i Klippet")
        start = datetime(year, 1, 1).timestamp()
        end = datetime(year + 1, 1, 1).timestamp()
        rows: list[tuple] = []
        store = getattr(self._tracker, "store", None)
        if store is not None:
            try:
                rows = store.between(start, end)
            except Exception:
                log.debug("no time for the year review", exc_info=True)
        imports: list[dict[str, Any]] = []
        if self._importer is not None:
            try:
                imports = self._importer.history(10_000)
            except Exception:
                log.debug("no import history", exc_info=True)
        st = self.stats()
        with self._lock:
            counters = dict(self._data["aar"].get(str(year), {}))
            meals = [(entry[0], entry[1]) for entry in self._data["mad"]["log"]]
            unlocked = dict(self._data["unlocked"])
            found = {item: dict(value) for item, value in self._data["found"].items() if isinstance(value, dict)}
        return aar.year_review(year, segments=rows, imports=imports, counters=counters, meals=meals,
                               unlocked=unlocked, found=found,
                               name=_short_text(self.cfg.get("widget_pet_name", "Klippe"), 20) or "Klippe",
                               stage=stage_for(st.total_s / 3600.0, st.hatched), level=st.level, today=self._today())

    # -- food and hunger (SPEC §18.6) ----------------------------------------------------------
    def _work_s(self) -> float | None:
        """All time ever logged, in seconds (what Klippe burns its food on); None when the time
        store cannot be read right now (closed on exit, busy) – never a made-up 0."""
        store = getattr(self._tracker, "store", None)
        if store is None:
            return 0.0
        try:
            total = getattr(store, "total_s", None)
            if callable(total):
                return float(total())
            return sum(r[6] - r[5] for r in store.between(0.0, self._clock() + 86400) if r[6] > r[5])
        except Exception:
            log.debug("no time for Klippe's hunger", exc_info=True)
            return None

    def _hunger(self, now: float, work_s: float | None) -> dict[str, Any]:
        """Brings the satiety up to ``now`` (under the lock) and returns the food record."""
        mad = self._data["mad"]
        if not (_number(mad.get("maet")) and _number(mad.get("ved"))):
            mad.update(maet=START_SATIETY, ved=now, arbejde_s=work_s)
            return mad
        if work_s is None:                  # the time is not known now: the anchor stays as it is
            return mad
        if not _number(mad.get("arbejde_s")):
            mad["arbejde_s"] = work_s       # first time known: nothing worked since
        satiety = float(mad["maet"])
        if work_s < mad["arbejde_s"]:
            # Time taken back (the tracker trims a pause it had counted as work): give back what
            # Klippe burnt on it.
            satiety = min(FULL, satiety + (mad["arbejde_s"] - work_s) / 3600.0 * WORK_BURN)
        worked = max(0.0, work_s - mad["arbejde_s"])
        rest = max(0.0, now - mad["ved"] - worked)
        mad.update(maet=satiety_after(satiety, worked, rest), ved=now, arbejde_s=work_s)
        return mad

    def _food_state(self, mad: dict[str, Any], now: float) -> dict[str, Any]:
        energy = mad.get("energi")
        rush = None
        if isinstance(energy, dict) and energy.get("item") in MENU_BY_ID \
                and isinstance(energy.get("til"), (int, float)) and energy["til"] > now:
            start = energy.get("fra")
            rush = {"item": energy["item"], "name": MENU_BY_ID[energy["item"]].name, "til": energy["til"],
                    "fra": start if isinstance(start, (int, float)) else None, "left_s": round(energy["til"] - now)}
        drip = mad.get("drop")
        bag = None                          # the drip's own bag: it stays until empty, whatever is drunk
        if isinstance(drip, dict) and _number(drip.get("fra")) and _number(drip.get("til")) and drip["til"] > now:
            bag = {"fra": drip["fra"], "til": drip["til"]}
        return {"maet": round(float(mad["maet"]), 1), "energi": rush, "drop": bag, "spist": dict(mad["spist"]),
                "menu": [_food_dict(food) for food in MENU]}

    def food(self) -> dict[str, Any]:
        """``GET /api/pet/mad``: how full Klippe is, an energy rush, what it has eaten, the menu."""
        now = self._clock()
        work = self._work_s()
        with self._lock:
            return self._food_state(self._hunger(now, work), now)

    def feed(self, item: Any) -> dict[str, Any]:
        """``POST /api/pet/mad {item}``. Klippe says no to food when it is full (``grund``
        "maet") and to a fourth energy drink or drip within two hours ("hjerte")."""
        food = MENU_BY_ID.get(item) if isinstance(item, str) else None
        if food is None:
            raise ValueError("Ugyldig værdi: item")
        now = self._clock()
        work = self._work_s()
        with self._lock:
            mad = self._hunger(now, work)
            reason = None
            if food.kind == "mad" and mad["maet"] >= TOO_FULL:
                reason = "maet"
            elif food.energy and sum(1 for at, i in mad["log"] if MENU_BY_ID[i].energy
                                     and 0 <= now - at < DRINKS_WINDOW_S) >= DRINKS_MAX:
                reason = "hjerte"
            if reason is None:
                mad["maet"] = min(FULL, mad["maet"] + food.points)
                if food.energy:
                    energy = mad.get("energi")
                    until = energy.get("til") if isinstance(energy, dict) else None
                    start = max(now, until) if isinstance(until, (int, float)) else now
                    mad["energi"] = {"item": food.id, "fra": now,
                                     "til": min(now + ENERGY_MAX_S, start + food.energy_min * 60)}
                if food.kind == "drop":
                    mad["drop"] = {"fra": now, "til": now + food.energy_min * 60}
                mad["log"].append([now, food.id])
                del mad["log"][:-MEAL_LOG_MAX]
                mad["spist"][food.id] = int(mad["spist"].get(food.id) or 0) + 1
                self._save()
            state = self._food_state(mad, now)
        if reason is None:
            log.info("Klippe: %s", food.name)
            self.bus.publish("pet_mad", state)
            self._wake.set()                        # its food trophies
        return {"ok": True, "spiste": reason is None, "grund": reason, "item": _food_dict(food), "mad": state}

    # -- the API ------------------------------------------------------------------------------
    def counts(self) -> dict[str, int]:
        with self._lock:
            return {"unlocked": sum(1 for t in TROPHIES if t.id in self._data["unlocked"]),
                    "total": len(TROPHIES)}

    def state(self) -> dict[str, Any]:
        with self._lock:
            st = self._stats
            unlocked = dict(self._data["unlocked"])
            found = dict(self._data["found"])
        if st is None:
            st = self.stats()
        trophies = []
        for t in TROPHIES:
            at = unlocked.get(t.id)
            hidden = t.secret and at is None
            current = t.measure(st)
            reward = ITEMS_BY_ID.get(t.reward) if t.reward else None
            trophies.append({
                "id": t.id, "group": t.group, "secret": t.secret, "unlocked": at,
                "name": "???" if hidden else t.name, "text": "Hemmelig" if hidden else t.text,
                "goal": t.goal, "current": round(min(current, t.goal), 2), "unit": t.unit,
                "progress": 1.0 if at else round(max(0.0, min(1.0, current / t.goal)), 3),
                "reward": None if hidden or reward is None else _item_dict(reward)})
        owned = self.owned()
        sources = {t.reward: t for t in TROPHIES if t.reward}
        items = []
        for item in ITEMS:
            source = sources.get(item.id)
            findable = item.id in dict(FINDS)
            if item.default:
                how = "Fra start"
            elif item.id in found:
                how = f"Fundet {found[item.id].get('day', '')}"
            elif source is not None:
                how = "???" if source.secret and source.id not in unlocked else f"Trofæ: {source.name}"
                if findable:
                    how += " – eller et sjældent fund"
                if item.id in JUL_ITEMS:
                    how += " – eller julekalenderen"
            elif item.id in JUL_ITEMS:
                how = "Fra julekalenderen"
            else:
                how = "Sjældent fund – Klippe finder det måske en dag"
            items.append({**_item_dict(item), "owned": item.id in owned, "how": how})
        return {"trophies": trophies, "items": items, "equipped": self.equipped(),
                "slots": [{"id": s, "name": SLOT_NAMES[s]} for s in SLOTS], **self.counts()}


def _item_dict(item: Item) -> dict[str, Any]:
    return {"id": item.id, "slot": item.slot, "name": item.name, "rarity": item.rarity}
