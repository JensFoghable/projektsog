"""Benchmark: apply throughput and search latency on a synthetic ~500k-entry index.

    python tests/bench_index_search.py [--entries 500000] [--reps 15] [--load-seconds 8]

Everything lives in a temporary directory (temporary LOCALAPPDATA); no real data is touched.
The tree mimics a typical set of roots (SPEC §0): projects from the "1. KUNDENAVN" template,
client groups, camera clips, collapsed image sequences, an Unreal project with ~23k assets, and
the project names used by the §7.2 acceptance queries (+ a few projects spelling "ø" as "oe",
SPEC §15.2); all names are invented examples.  Search latency is measured idle and while a
separate process keeps committing unit-sized transactions (like the scan worker); finally the
in-place schema v2 → v3 migration is replayed on the full index.
"""

from __future__ import annotations

import argparse
import os
import random
import statistics
import subprocess
import sys
import tempfile
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from projektsog import db, search, textutil  # noqa: E402
from projektsog.db import (KIND_DIR, KIND_FILE, KIND_GROUP, KIND_PROJECT,  # noqa: E402
                           KIND_TEMPLATE, KIND_TOPLEVEL, Diff, Entry)

TEMPLATE = ["Final", "Grafik", "Klip", "Logo", "Musik", "Project", "Speak", "Tekst"]
SOURCES = [  # display_name, kind, path, weight
    ("Kunder 2026 (STUDIO)", "local", "C:\\Kunder 2026 (STUDIO)", 3),
    ("Forår 2026 RØD", "local", "D:\\Forår 2026 RØD", 4),
    ("2024 Disk Sølv", "local", "H:\\2024 Disk Sølv", 5),
    ("(Z) Kunder 2026 (STUDIO)", "local", "Z:\\(Z) Kunder 2026 (STUDIO)", 8),
    ("Kunder 2026 ARKIV", "local", "F:\\Kunder 2026 ARKIV", 1),
    ("Rejsefilm", "share", "\\\\KLIPPER-PC\\Rejsefilm", 4),
    ("Efterår 2023", "share", "\\\\KLIPPER-PC\\Efterår 2023", 4),
    ("2026Arkiv", "share", "\\\\MEDIESERVER\\2026Arkiv", 2),
    ("2025Arkiv", "share", "\\\\MEDIESERVER\\2025Arkiv", 24),
    ("Forår 2026 (HDD)", "share", "\\\\GRAFIK-PC\\Forår 2026 (HDD)", 4),
    ("Kunder 2026 (Grafik)", "share", "\\\\GRAFIK-PC\\Kunder 2026 (Grafik)", 4),
]
NAMED_PROJECTS = {
    "Kunder 2026 (STUDIO)": ["Rikke Lindholm", "Bøgely Jul 2025",
                             "Klar Tand 2026\\Klar Tand - Silkeborg C",
                             "Klar Tand 2026\\Klar Tand - Voxpop Silkeborg"],
    "Kunder 2026 (Grafik)": ["Klar Tand 2026\\Klar Tand - Silkeborg", "Bøgely Festival 2025",
                             "Hotel Bøgelyhus"],
    "Forår 2026 RØD": ["Pixelbro"],
    "2024 Disk Sølv": ["Pixelbro Radio", "Bøgely Jul 2024"],
}
CLIENTS = ["Dækcentret", "Møbler Malte", "Kildedal", "Bolighuset Vejen", "Køkkenhuset",
           "Naturkost", "Plastfabrikken", "TrioGroup", "Skolen", "Kampagne 1", "Kodehuset",
           "Vindkraft Nord", "Sengehuset", "Varehuset", "Mejeriet", "Pumpefabrikken",
           "Ventilfabrikken", "Klodsen", "Tøjhuset", "Skofabrikken", "Superkøb", "Fødevarehallen",
           "Discount Nord", "Bankhuset", "Realkredit Vest", "Aabyhøj Dyrepark", "Aarslev Havn",
           "Nørreby Kommune", "Østerby Kommune", "Region Vest", "Bycenter Syd", "Lystparken"]
TOPICS = ["Årsmøde", "Julehilsen", "Testimonial", "Reklame", "Koncert", "Kampagne", "Event",
          "Produktfilm", "Portræt", "Messe", "Rekruttering", "Jubilæum", "SoMe", "Webinar"]
QUERIES = ["lindholm", "rikke lindholm", "lindholm klip", "klar tand silkeborg", "pixelbro",
           "bogely", "bøgely", "forar pixelbro", "FX9_7912", "kundenavn", "silkeborg c",
           "dækcentret årsmøde", "klip", "final", "render", "dji", "2026", "mxf", "uasset",
           "klip mxf", "a", "li", "fx 79",
           # schema v3: the ASCII spelling "oe" of "ø" (SPEC §15.2)
           "boegely", "infomøde", "infomoede skolen", "købmand", "moebler", "moe", "koe final"]
# Names written with "oe" for "ø" (as in real exports); added as extra units drawn from their
# own random stream, so the tree above stays identical to earlier runs.
ALT_CLIENTS = ["Koebmand Hansen", "Moebelhuset Vejle", "Boegely Havn", "Groent Marked",
               "Boern og Unge", "Loebeklubben Aarhus"]
ALT_TOPICS = ["Infomøde", "Infomoede", "Møbler", "Købmand", "Bøgely Festival"]
NOW = time.time()
DAY = 86_400.0


class TreeBuilder:
    """Flattens a nested ``{name: subtree | (size, mtime) | ("seq", n, size, mtime)}`` unit
    into Entry rows with the scanner's aggregates, kinds and project_rel."""

    def __init__(self) -> None:
        self.entries: list[Entry] = []

    def unit(self, name: str, tree: dict) -> list[Entry]:
        self.entries = []
        self._dir(name, "", name, 1, tree, None)
        return self.entries

    def _dir(self, rel, parent, name, depth, tree, project):
        fold = textutil.fold(name)
        is_template = fold == "1 kundenavn"
        subdirs = [k for k, v in tree.items() if isinstance(v, dict)]
        is_project = not is_template and sum(1 for d in subdirs if d in TEMPLATE) >= 2
        own_project = rel if is_project else project
        size = count = 0
        newest = own_mtime = NOW - 400 * DAY
        has_project_child = False
        for child, value in tree.items():
            child_rel = f"{rel}\\{child}"
            if isinstance(value, dict):
                e = self._dir(child_rel, rel, child, depth + 1, value, own_project)
                has_project_child |= e.kind == KIND_PROJECT
            elif value[0] == "seq":
                _, n, fsize, mtime = value
                e = Entry(child_rel, rel, child, textutil.fold(child), KIND_FILE, depth + 1,
                          child.rpartition(".")[2].lower(), fsize * n, mtime, None, None, 1, n,
                          own_project)
                self.entries.append(e)
                count += n - 1
            else:
                fsize, mtime = value
                e = Entry(child_rel, rel, child, textutil.fold(child), KIND_FILE, depth + 1,
                          child.rpartition(".")[2].lower(), fsize, mtime, None, None, 0, None,
                          own_project)
                self.entries.append(e)
            size += e.size or 0
            count += e.file_count if e.kind != KIND_FILE else 1
            newest = max(newest, e.mtime or 0)
        kind = (KIND_TEMPLATE if is_template else KIND_PROJECT if is_project
                else KIND_GROUP if has_project_child else KIND_TOPLEVEL if depth == 1 else KIND_DIR)
        entry = Entry(rel, parent, name, fold, kind, depth, None, size, newest, count, own_mtime,
                      0, None, own_project)
        self.entries.append(entry)
        return entry


def project_tree(rng: random.Random, name: str, scale: int) -> dict:
    """A project folder in the template shape, ~scale*35 entries."""
    age = rng.choice([5, 20, 60, 150, 300, 700]) * DAY
    t = lambda: NOW - age - rng.random() * 30 * DAY  # noqa: E731
    klip: dict = {}
    for cam, pattern in (("FX9", "FX9_{:04d}.MXF"), ("A7S", "C{:04d}.MP4"),
                         ("DJI", "DJI_{:04d}.MP4"), ("GoPro", "GX01{:04d}.MP4")):
        if rng.random() < 0.7:
            start = rng.randint(1, 9000)
            klip[cam] = {pattern.format(start + i): (rng.randint(10**8, 4 * 10**9), t())
                         for i in range(rng.randint(3, 12) * scale)}
    grafik = {f"{name} grafik {i}.psd": (10**7, t()) for i in range(rng.randint(1, 5))}
    if rng.random() < 0.4:
        n = rng.randint(100, 4000)
        grafik[f"render_[0001-{n:04d}].exr"] = ("seq", n, 8 * 10**6, t())
    return {
        "Final": {f"{name} v{i}.mp4": (rng.randint(10**8, 2 * 10**9), t())
                  for i in range(1, rng.randint(2, 6))},
        "Grafik": grafik, "Klip": klip,
        "Logo": {"logo.png": (50_000, t()), "logo.ai": (900_000, t())},
        "Musik": {f"track {i}.wav": (5 * 10**7, t()) for i in range(rng.randint(1, 4))},
        "Project": {f"{name}.drp": (2 * 10**6, t())},
        "Speak": {f"speak {i}.wav": (10**7, t()) for i in range(rng.randint(0, 6))},
        "Tekst": {f"{name}.docx": (40_000, t()), f"{name}.srt": (5_000, t())},
    }


def source_units(rng: random.Random, display: str, target: int) -> list[tuple[str, dict]]:
    """Depth-1 units of one source (~target entries)."""
    units: list[tuple[str, dict]] = [("1. KUNDENAVN", {d: {} for d in TEMPLATE})]
    groups: dict[str, dict] = {}
    for rel in NAMED_PROJECTS.get(display, []):
        tree = project_tree(rng, rel.rpartition("\\")[2], 1)
        if rel.startswith("Rikke Lindholm"):
            tree["Klip"]["FX9"] = {f"FX9_{7900 + i}.MXF": (10**9, NOW - 20 * DAY)
                                   for i in range(30)}
            tree["Final"]["Rikke Lindholm - Testimonial.mp4"] = (10**9, NOW - 20 * DAY)
        group, _, leaf = rel.rpartition("\\")
        if group:
            groups.setdefault(group, {})[leaf] = tree
        else:
            units.append((leaf, tree))
    units.extend(groups.items())
    if display == "2025Arkiv":
        unreal: dict = {}
        for i in range(60):
            unreal[f"Maps{i:02d}"] = {f"SM_Asset_{i:02d}_{j:03d}.uasset": (400_000, NOW - 300 * DAY)
                                      for j in range(390)}
        config_dir = {"DefaultGame.ini": (900, NOW)}
        units.append(("Unreal Project", {"Content": unreal, "Config": config_dir}))
    produced = sum(len(TreeBuilder().unit(n, t)) for n, t in units)
    i = 0
    while produced < target:
        client = rng.choice(CLIENTS)
        name = f"{client} {rng.choice(TOPICS)} {rng.choice([2023, 2024, 2025, 2026])}"
        if rng.random() < 0.3:
            group = f"{client} {rng.choice([2024, 2025, 2026])}"
            tree = {f"{name} {i}": project_tree(rng, name, rng.randint(1, 4)) for i in range(3)}
            unit = (f"{group} ({i})", tree)
        else:
            unit = (f"{name} ({i})", project_tree(rng, name, rng.randint(1, 4)))
        produced += len(TreeBuilder().unit(*unit))
        units.append(unit)
        i += 1
    return units


def alt_spelling_units(rng: random.Random, count: int) -> list[tuple[str, dict]]:
    """Projects whose names use either spelling of "ø" (~35 entries per project)."""
    units = []
    for i in range(count):
        name = f"{rng.choice(ALT_CLIENTS)} {rng.choice(ALT_TOPICS)} {rng.choice([2024, 2025])}"
        tree = project_tree(rng, name, 1)
        tree["Final"][f"{name.replace('ø', 'oe')} 9x16.mp4"] = (10**8, NOW - 40 * DAY)
        units.append((f"{name} ({i})", tree))
    return units


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))]


def build(path: str, total: int) -> tuple[dict[int, dict], dict]:
    rng = random.Random(2026)
    conn = db.connect(path, writer=True)
    try:
        db.ensure_schema(conn)
        weights = sum(w for *_x, w in SOURCES)
        registry: dict[int, dict] = {}
        stats = {"rows": 0, "seconds": 0.0, "transactions": 0}
        for display, kind, path_, weight in SOURCES:
            sid = db.upsert_source(conn, {"key": f"bench:{display}", "kind": kind,
                                          "host": "BENCH", "display_name": display,
                                          "current_path": path_})
            registry[sid] = {"id": sid, "display_name": display, "kind": kind, "host": "BENCH",
                             "path": path_, "unc_path": path_ if kind == "share" else None,
                             "volume_label": display if kind == "local" else None,
                             "online": True, "included": True, "last_seen": NOW}
            builder = TreeBuilder()
            units = source_units(rng, display, total * weight // weights)
            units += alt_spelling_units(random.Random(f"alt:{display}"), 8)
            for name, tree in units:
                entries = builder.unit(name, tree)
                started = time.perf_counter()
                db.apply_diff(conn, sid, Diff(inserts=entries))
                stats["seconds"] += time.perf_counter() - started
                stats["rows"] += len(entries)
                stats["transactions"] += 1
    finally:
        conn.close()
    return registry, stats


def update_and_delete(path: str, registry: dict[int, dict]) -> dict:
    conn = db.connect(path, writer=True)
    sid = next(s for s, src in registry.items()
               if src["display_name"] == "(Z) Kunder 2026 (STUDIO)")
    rows = db.load_children(conn, sid, "")
    started = time.perf_counter()
    updated = 0
    for row in rows:
        subtree = db.load_subtree(conn, sid, row[db.S_REL])
        diff = Diff(updates=[(r[0], Entry(*r[1:])._replace(mtime=(r[db.S_MTIME] or 0) + 1))
                             for r in subtree])
        updated += db.apply_diff(conn, sid, diff)[0]
    update_s = time.perf_counter() - started
    started = time.perf_counter()
    deleted = db.delete_source_entries(conn, sid)
    delete_s = time.perf_counter() - started
    conn.close()
    return {"updated": updated, "update_s": update_s, "deleted": deleted, "delete_s": delete_s,
            "deleted_source": sid}


def measure_migration(path: str) -> dict:
    """Replay the in-place schema v2 → v3 migration (name_alt + FTS rebuild) on the index."""
    conn = db.connect(path, writer=True)
    try:
        db.set_meta(conn, "schema_version", "2")
        started = time.perf_counter()
        db.ensure_schema(conn)
        seconds = time.perf_counter() - started
        entries = conn.execute("SELECT count(*) FROM entries").fetchone()[0]
        alt = conn.execute("SELECT count(*) FROM entries WHERE name_alt IS NOT NULL").fetchone()[0]
        conn.execute("INSERT INTO entries_fts(entries_fts, rank) VALUES ('integrity-check', 1)")
    finally:
        conn.close()
    return {"entries": entries, "alt": alt, "seconds": seconds}


def measure(conn, registry: dict[int, dict], reps: int) -> dict[str, dict]:
    out = {}
    for query in QUERIES:
        search.search(conn, registry, query)                    # warm-up
        times, result = [], None
        for _ in range(reps):
            started = time.perf_counter()
            result = search.search(conn, registry, query)
            times.append((time.perf_counter() - started) * 1000)
        out[query] = {"p50": statistics.median(times), "p95": percentile(times, 0.95),
                      "max": max(times), "total": result["total"],
                      "truncated": result["truncated"],
                      "top": result["results"][0]["name"] if result["results"] else "–"}
    return out


def print_table(title: str, rows: dict[str, dict]) -> None:
    print(f"\n{title}")
    print(f"  {'query':<20} {'p50 ms':>8} {'p95 ms':>8} {'max ms':>8} {'total':>7}  top result")
    for query, r in rows.items():
        flag = "+" if r["truncated"] else " "
        print(f"  {query:<20} {r['p50']:8.1f} {r['p95']:8.1f} {r['max']:8.1f} "
              f"{r['total']:>6}{flag}  {r['top']}")
    p50s = [r["p50"] for r in rows.values()]
    p95s = [r["p95"] for r in rows.values()]
    print(f"  median of p50: {statistics.median(p50s):.1f} ms, worst p95: {max(p95s):.1f} ms"
          f" (targets: typical < 50 ms, worst < 250 ms)")


def writer_main(path: str, seconds: float, source_id: int) -> None:
    """Load generator: unit-sized insert and delete transactions (like the scan worker)."""
    conn = db.connect(path, writer=True)
    rng = random.Random(7)
    builder = TreeBuilder()
    deadline = time.monotonic() + seconds
    i = 0
    while time.monotonic() < deadline:
        entries = builder.unit(f"Load {i}", project_tree(rng, f"Load project {i}", 3))
        db.apply_diff(conn, source_id, Diff(inserts=entries))
        ids = [r[0] for r in db.load_subtree(conn, source_id, f"Load {i}")]
        db.apply_diff(conn, source_id, Diff(deletes=ids))
        i += 1
    conn.close()
    print(f"writer: {i} insert+delete unit pairs", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--entries", type=int, default=500_000)
    parser.add_argument("--reps", type=int, default=15)
    parser.add_argument("--load-seconds", type=float, default=8.0)
    parser.add_argument("--writer", nargs=3, metavar=("DB", "SECONDS", "SOURCE_ID"),
                        help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.writer:
        writer_main(args.writer[0], float(args.writer[1]), int(args.writer[2]))
        return
    with tempfile.TemporaryDirectory(prefix="projektsog-bench-") as tmp:
        os.environ["LOCALAPPDATA"] = tmp
        path = os.path.join(tmp, "index.db")
        started = time.perf_counter()
        registry, stats = build(path, args.entries)
        print(f"built {stats['rows']:,} entries in {time.perf_counter() - started:.1f} s; "
              f"apply: {stats['rows'] / stats['seconds']:,.0f} rows/s in "
              f"{stats['transactions']} unit transactions ({stats['seconds']:.1f} s in SQLite)")
        size = os.path.getsize(path) + (os.path.getsize(path + "-wal")
                                        if os.path.exists(path + "-wal") else 0)
        print(f"database size: {size / 1e6:.0f} MB")

        conn = db.connect(path)
        counts_start = time.perf_counter()
        biggest = max(registry, key=lambda s: registry[s]["display_name"] == "2025Arkiv")
        counts = db.source_counts(conn, biggest)
        print(f"source_counts(2025Arkiv): {counts['entry_count']:,} entries in "
              f"{(time.perf_counter() - counts_start) * 1000:.0f} ms")
        print_table("search latency (idle)", measure(conn, registry, args.reps))

        started = time.perf_counter()
        for _ in range(args.reps):
            search.recent_projects(conn, registry, limit=30)
        print(f"\nrecent_projects(30): {(time.perf_counter() - started) * 1000 / args.reps:.1f} ms")
        started = time.perf_counter()
        for _ in range(args.reps):
            search.children(conn, registry, biggest, "Unreal Project\\Content\\Maps00")
        print(f"children(390 files): {(time.perf_counter() - started) * 1000 / args.reps:.1f} ms")

        load_sid = next(s for s, src in registry.items() if src["display_name"] == "Rejsefilm")
        writer = subprocess.Popen([sys.executable, os.path.abspath(__file__), "--writer", path,
                                   str(args.load_seconds), str(load_sid)],
                                  stdout=subprocess.PIPE, text=True)
        time.sleep(0.5)
        loaded = measure(conn, registry, max(3, args.reps // 3))
        out, _ = writer.communicate(timeout=args.load_seconds + 60)
        print_table(f"search latency while another process commits units ({out.strip()})",
                    loaded)
        conn.close()

        migration = measure_migration(path)
        print(f"\nschema v2 -> v3 migration of {migration['entries']:,} entries "
              f"({migration['alt']:,} with an 'oe' spelling): {migration['seconds']:.1f} s")

        maint = update_and_delete(path, registry)
        print(f"\nupdate pass: {maint['updated']:,} rows in {maint['update_s']:.1f} s "
              f"({maint['updated'] / maint['update_s']:,.0f} rows/s); forget: "
              f"{maint['deleted']:,} rows in {maint['delete_s']:.1f} s "
              f"({maint['deleted'] / maint['delete_s']:,.0f} rows/s)")
        conn = db.connect(path, writer=True)
        started = time.perf_counter()
        pages = db.incremental_vacuum(conn)
        checkpoint = db.checkpoint(conn)
        print(f"incremental_vacuum: {pages:,} pages in {time.perf_counter() - started:.1f} s; "
              f"checkpoint {checkpoint}")
        conn.close()


if __name__ == "__main__":
    main()
