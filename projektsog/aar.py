"""Klippet <år> (SPEC §23.11): one year in review, the data behind the story page ``aar.html``.

Pure arithmetic: ``PetProgress.year_review`` hands over the year's time segments (as
``TimeStore.between`` returns them), the import helper's history, this year's counters (lunch
bells, posts, deliveries …), the meals, the trophies and finds with their times, and Klippe as it
is now. Only that year counts – a segment across New Year is cut at midnight – and nothing is
said about late nights or overtime.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from typing import Any

from .achievements import DAY_MIN_S, MENU, MENU_BY_ID, compute_stats

PROJECT_MIN_S = 180.0            # a project opened for a moment is no project of the year
PAGE_MIN_S = 360.0               # a page used for less than this is left out of "sider"
TOP_PROJECTS = 5

# Resolve's pages and the browser buckets, as the story names them.
SIDE_NAVNE = {
    "edit": "Edit", "cut": "Cut", "color": "Color", "fusion": "Fusion", "fairlight": "Fairlight",
    "deliver": "Deliver", "media": "Media", "photo": "Photo", "musik": "Musik og lyd",
    "ai": "AI-video og -billeder",
}
UGEDAGE = ("mandag", "tirsdag", "onsdag", "torsdag", "fredag", "lørdag", "søndag")
KONTOR_TAELLERE = ("frokost_ringet", "post_sendt", "post_faaet", "slik_givet", "vaeddemaal_vundet",
                   "spil_bedste", "sne")


def _hours(seconds: float) -> float:
    return round(max(0.0, seconds) / 3600.0, 1)


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _clip(segments: Iterable[tuple], start: float, end: float) -> list[tuple]:
    """The segments' parts inside [start, end)."""
    out = []
    for row in segments:
        if len(row) < 8 or not (_number(row[5]) and _number(row[6])):
            continue
        a, b = max(float(row[5]), start), min(float(row[6]), end)
        if b > a:
            out.append((*row[:5], a, b, row[7]))
    return out


def _clock(hour: float) -> str:
    minutes = int(round(hour * 60))
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _in_year(at: Any, year: int) -> bool:
    if _number(at):
        try:
            return datetime.fromtimestamp(float(at)).year == year
        except (OverflowError, OSError, ValueError):
            return False
    if isinstance(at, str):
        try:
            return date.fromisoformat(at[:10]).year == year
        except ValueError:
            return False
    return False


def _cards(imports: Iterable[Mapping[str, Any]], year: int) -> dict[str, Any]:
    count = files = size = 0
    cameras: Counter[str] = Counter()
    for entry in imports:
        if not isinstance(entry, Mapping) or entry.get("mode") not in ("copy", "move"):
            continue
        n = entry.get("files")
        if type(n) is not int or n <= 0 or not _in_year(entry.get("at"), year):
            continue
        count += 1
        files += n
        size += int(entry.get("bytes") or 0) if _number(entry.get("bytes")) else 0
        camera = entry.get("camera")
        if isinstance(camera, str) and camera.strip():
            cameras[camera.strip()] += 1
    camera = cameras.most_common(1)[0][0] if cameras else None
    return {"antal": count, "tb": round(size / 1000 ** 4, 2), "filer": files, "kamera": camera}


def _meals(meals: Iterable[tuple[Any, Any]], year: int) -> dict[str, Any]:
    eaten: Counter[str] = Counter()
    for at, item in meals:
        if item in MENU_BY_ID and _in_year(at, year):
            eaten[item] += 1
    favourite = None
    if eaten:
        best = max(eaten.values())
        food = next(f for f in MENU if eaten.get(f.id) == best)          # ties: the menu's order
        favourite = {"id": food.id, "navn": food.name, "antal": best}
    return {"i_alt": sum(eaten.values()), "spist": {f.id: eaten[f.id] for f in MENU if eaten.get(f.id)},
            "yndlings": favourite}


def year_review(year: int, *, segments: Iterable[tuple] = (), imports: Iterable[Mapping[str, Any]] = (),
                counters: Mapping[str, Any] | None = None, meals: Iterable[tuple[Any, Any]] = (),
                unlocked: Mapping[str, Any] | None = None, found: Mapping[str, Any] | None = None,
                name: str = "Klippe", stage: str = "egg", level: int = 1,
                today: date | None = None) -> dict[str, Any]:
    """``GET /api/aar``: what happened in ``year`` (see SPEC §23.11 for the shape)."""
    start = datetime(year, 1, 1).timestamp()
    end = datetime(year + 1, 1, 1).timestamp()
    rows = _clip(segments, start, end)
    st = compute_stats(rows, today=today)
    counters = {k: int(v) for k, v in (counters or {}).items() if isinstance(k, str) and _number(v)}

    projects = sorted(((p, s) for p, s in st.projects.items() if s >= PROJECT_MIN_S), key=lambda ps: -ps[1])
    pages = sorted(((b, s) for b, s in st.buckets.items() if s >= PAGE_MIN_S), key=lambda bs: -bs[1])
    best_day = max(st.days.items(), key=lambda ds: (ds[1], ds[0]), default=None)
    weekdays = [0.0] * 7
    for day, seconds in st.days.items():
        weekdays[date.fromisoformat(day).weekday()] += seconds
    worked = [d for d, s in st.days.items() if s >= DAY_MIN_S]
    earliest = min((st.day_first[d] for d in worked if d in st.day_first), default=None)

    trophies = sum(1 for at in (unlocked or {}).values() if _in_year(at, year))
    finds = sum(1 for value in (found or {}).values()
                if isinstance(value, Mapping) and _in_year(value.get("day") or value.get("at"), year))
    return {
        "aar": year,
        "fra": f"{year}-01-01",
        "til": f"{year}-12-31",
        "siden": min(st.days) if st.days else None,
        "dage": len(worked),
        "timer": _hours(st.total_s),
        "projekter": len(projects),
        "top": [{"navn": p, "timer": _hours(s)} for p, s in projects[:TOP_PROJECTS]],
        "sider": [{"side": b, "navn": SIDE_NAVNE.get(b, b.title()), "timer": _hours(s)} for b, s in pages],
        "bedste_dag": {"dato": best_day[0], "timer": _hours(best_day[1])} if best_day else None,
        "ugedag": UGEDAGE[max(range(7), key=lambda i: weekdays[i])] if any(weekdays) else None,
        "tidligst": _clock(earliest) if earliest is not None else None,
        "fokus_90": st.focus_90,
        "pauser": st.good_breaks,
        "kort": _cards(imports, year),
        "leveringer": counters.get("leveringer", 0),
        "renders": counters.get("renders", 0),
        "mad": _meals(meals, year),
        "klippe": {"navn": name, "stage": stage, "level": level, "trofaeer": trophies, "fund": finds},
        "kontor": {key: counters.get(key, 0) for key in KONTOR_TAELLERE},
    }
