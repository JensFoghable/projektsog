"""Smoke test of the Indexer on the REAL environment (read-only).

    python tests/smoke_index_engine.py [--host NAME] [--include PATH] [--exclude PATH]
                                       [--skip NAME] [--query TEXT] [--expect QUERY=SUFFIX]
                                       [--resolve-path PATH] [--resolve-project NAME]
                                       [--suggest NAME] [--timeout MINUTES]

Runs the complete Indexer – discovery of the local volumes and the given hosts, probes, the real
scan worker under ``pythonw.exe`` (background priority, no window) – with a temporary
LOCALAPPDATA, config and index until ``initial_scan_done``, and reports:

* the discovery verdicts (included / excluded with the reason),
* a timeline: when each source was discovered, probed, searchable (after its first-time
  shallow scan) and deep-scanned,
* entry counts per source, and what image-sequence collapsing saved,
* the SPEC §7.2-style queries with their top 5 results,
* search latency (p50/p95/max) measured WHILE the worker was scanning (target < 250 ms),
* ``map_paths`` on DaVinci-Resolve-style paths, and the index size.

Nothing about a particular computer or network is built in.  The setup to test comes from the
command line or from environment variables; every option may be repeated, a variable holds
several values separated by ";", and an option given on the command line replaces its variable:

    --host            PROJEKTSOG_SMOKE_HOSTS          computers whose shares are discovered
    --include         PROJEKTSOG_SMOKE_INCLUDE        source paths that must be included
    --exclude         PROJEKTSOG_SMOKE_EXCLUDE        paths that must not be included sources
    --skip            PROJEKTSOG_SMOKE_SKIP           source names excluded as soon as they are
                                                      discovered (a quick run without a big crawl)
    --query           PROJEKTSOG_SMOKE_QUERIES        extra queries to report (and search while
                                                      the worker scans)
    --expect          PROJEKTSOG_SMOKE_EXPECT         "QUERY=SUFFIX": the first result of QUERY
                                                      must have a path ending in SUFFIX
    --resolve-path    PROJEKTSOG_SMOKE_RESOLVE_PATHS  media paths as DaVinci Resolve reports them
    --resolve-project PROJEKTSOG_SMOKE_RESOLVE_PROJECT  project folder those paths must map to
    --suggest         PROJEKTSOG_SMOKE_SUGGEST        a Resolve project name for
                                                      suggest_project_folders

Example (PowerShell):

    $env:PROJEKTSOG_SMOKE_HOSTS = "GRAFIK-PC"
    python tests/smoke_index_engine.py --include "C:\\Kunder 2026" --exclude C:\\Github `
        --expect "lindholm=Rikke Lindholm" --expect "lindholm klip=Rikke Lindholm\\Klip" `
        --resolve-path "\\\\studio-pc\\Kunder 2026\\Rikke Lindholm\\Klip\\A001.MXF" `
        --resolve-project "Rikke Lindholm" --suggest "Rikke Lindholm - Testimonial"

Nothing on the scanned drives/shares is written (directory listings and metadata only), and the
application's real data under %LOCALAPPDATA%\\Projektsog is untouched.
"""

from __future__ import annotations

import argparse
import os
import queue
import sqlite3
import statistics
import sys
import tempfile
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

GENERIC_QUERIES = ["klip", "final", "2026", "mxf", "FX9", "kundenavn"]


def env_list(name: str) -> list[str]:
    """The ";"-separated values of the environment variable ``name`` (empty ones dropped)."""
    return [part.strip() for part in os.environ.get(name, "").split(";") if part.strip()]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    for option, metavar, text in (
            ("--host", "NAME", "computer whose shares are discovered (PROJEKTSOG_SMOKE_HOSTS)"),
            ("--include", "PATH", "source path that must be included (PROJEKTSOG_SMOKE_INCLUDE)"),
            ("--exclude", "PATH", "path that must not be an included source "
                                  "(PROJEKTSOG_SMOKE_EXCLUDE)"),
            ("--skip", "NAME", "source name to exclude once discovered (PROJEKTSOG_SMOKE_SKIP)"),
            ("--query", "TEXT", "extra query to report (PROJEKTSOG_SMOKE_QUERIES)"),
            ("--expect", "QUERY=SUFFIX", "the first result of QUERY must end in SUFFIX "
                                         "(PROJEKTSOG_SMOKE_EXPECT)"),
            ("--resolve-path", "PATH", "DaVinci-Resolve-style media path "
                                       "(PROJEKTSOG_SMOKE_RESOLVE_PATHS)")):
        parser.add_argument(option, action="append", metavar=metavar, help=text)
    parser.add_argument("--resolve-project", metavar="NAME",
                        help="project the Resolve paths must map to "
                             "(PROJEKTSOG_SMOKE_RESOLVE_PROJECT)")
    parser.add_argument("--suggest", metavar="NAME",
                        help="Resolve project name for suggest_project_folders "
                             "(PROJEKTSOG_SMOKE_SUGGEST)")
    parser.add_argument("--timeout", type=float, default=30.0, help="minutes (default 30)")
    args = parser.parse_args(argv)
    for attr, variable in (("host", "HOSTS"), ("include", "INCLUDE"), ("exclude", "EXCLUDE"),
                           ("skip", "SKIP"), ("query", "QUERIES"), ("expect", "EXPECT"),
                           ("resolve_path", "RESOLVE_PATHS")):
        setattr(args, attr, getattr(args, attr) or env_list(f"PROJEKTSOG_SMOKE_{variable}"))
    for attr, variable in (("resolve_project", "RESOLVE_PROJECT"), ("suggest", "SUGGEST")):
        value = getattr(args, attr) or os.environ.get(f"PROJEKTSOG_SMOKE_{variable}", "").strip()
        setattr(args, attr, value or None)
    expectations = []
    for item in args.expect:
        query, sep, suffix = item.partition("=")
        if not (sep and query.strip() and suffix.strip()):
            parser.error(f"--expect needs QUERY=SUFFIX, not {item!r}")
        expectations.append((query.strip(), suffix.strip()))
    args.expect = expectations
    args.queries = list(dict.fromkeys(args.query + [q for q, _ in expectations]
                                      + GENERIC_QUERIES))
    return args


def fmt_t(value: float | None) -> str:
    return "      –" if value is None else f"{value:6.1f}s"


class Timeline:
    """First time (seconds since start) each source reached a milestone."""

    def __init__(self, t0: float) -> None:
        self.t0 = t0
        self.rows: dict[int, dict] = {}

    def update(self, sources: list[dict]) -> None:
        now = time.monotonic() - self.t0
        for s in sources:
            row = self.rows.setdefault(s["id"], {})
            for milestone, reached in (("online", s["online"]),
                                       ("probed", bool(s["auto_reason"]) or s["manual"]),
                                       ("searchable", s["entry_count"] > 0),
                                       ("deep_done", s["last_scan_end"] is not None)):
                if reached and milestone not in row:
                    row[milestone] = now


def watch(ix, args: argparse.Namespace) -> tuple[bool, float, Timeline, list[tuple[float, str]]]:
    """Start the Indexer and run until initial_scan_done, searching while the worker scans."""
    t0 = time.monotonic()
    ix.start()
    timeline = Timeline(t0)
    latencies: list[tuple[float, str]] = []
    next_report = 0.0
    skip = {name.casefold() for name in args.skip}
    skipped: set[int] = set()
    deadline = t0 + args.timeout * 60
    while time.monotonic() < deadline:
        status = ix.status()
        sources = ix.list_sources()
        timeline.update(sources)
        for s in sources:
            if s["display_name"].casefold() in skip and s["id"] not in skipped:
                ix.set_source_mode(s["id"], "exclude")
                skipped.add(s["id"])
        if status["initial_scan_done"] and not status["queued"] and not status["scanning"]:
            return True, time.monotonic() - t0, timeline, latencies
        if status["scanning"]:
            query = args.queries[len(latencies) % len(args.queries)]
            started = time.perf_counter()
            ix.search(query)
            latencies.append(((time.perf_counter() - started) * 1000, query))
        elapsed = time.monotonic() - t0
        if elapsed >= next_report:
            next_report = elapsed + 15
            scans = ", ".join(f"{s['name']} {s['kind']} {s['entries']:,}"
                              for s in status["scanning"]) or "idle"
            print(f"  t={elapsed:6.1f}s ready {status['sources_ready']}/"
                  f"{status['sources_included_online']} queued {status['queued']} · {scans}",
                  flush=True)
        time.sleep(0.1)
    return False, time.monotonic() - t0, timeline, latencies


def report_sources(sources: list[dict], args: argparse.Namespace, failures: list[str]) -> None:
    print("\n== Sources (discovery verdicts) ==")
    for s in sorted(sources, key=lambda s: (not s["included"], s["path"].casefold())):
        print(f"  {'INCL' if s['included'] else 'excl'} {'on ' if s['online'] else 'OFF'} "
              f"{s['path']:<42} {s['mode']:<7} {s['auto_reason'] or ''}")
    by_path = {s["path"].casefold(): s for s in sources}
    skip = {name.casefold() for name in args.skip}
    for path in args.include:
        src = by_path.get(path.casefold())
        if src is not None and src["display_name"].casefold() in skip:
            continue
        if src is None or not src["included"]:
            failures.append(f"{path} should be included ({src and src['auto_reason']})")
    for path in args.exclude:
        src = by_path.get(path.casefold())
        if src is not None and src["included"]:
            failures.append(f"{path} should be excluded")
    if "c:\\users" in by_path:
        failures.append("C:\\Users must not be a source")


def report_timeline(sources: list[dict], timeline: Timeline, db_path: str) -> None:
    print("\n== Timeline (s since start; 'searchable' = first-time shallow scan committed) ==")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        seconds = dict(conn.execute("SELECT id, scan_seconds FROM sources").fetchall())
    finally:
        conn.close()
    print(f"  {'source':<26} {'online':>7} {'probed':>7} {'search.':>7} {'deep':>7}  "
          f"latest deep scan took")
    included = [s for s in sources if s["included"]]
    for s in sorted(included, key=lambda s: timeline.rows.get(s["id"], {}).get("deep_done", 1e9)):
        row = timeline.rows.get(s["id"], {})
        took = seconds.get(s["id"])
        outcome = f"{took:.1f} s" if s["last_scan_ok"] and took is not None else s["last_error"]
        print(f"  {s['display_name']:<26} {fmt_t(row.get('online'))} {fmt_t(row.get('probed'))} "
              f"{fmt_t(row.get('searchable'))} {fmt_t(row.get('deep_done'))}  {outcome}")


def report_counts(sources: list[dict], db_path: str) -> None:
    print("\n== Entries per source ==")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        for s in sorted((s for s in sources if s["included"]), key=lambda s: -s["entry_count"]):
            seqs, frames = conn.execute(
                "SELECT count(*), coalesce(sum(seq_count), 0) FROM entries "
                "WHERE source_id = ? AND is_seq = 1", (s["id"],)).fetchone()
            print(f"  {s['display_name']:<26} {s['entry_count']:>8,} entries {s['file_count']:>8,}"
                  f" files {s['dir_count']:>6,} dirs {s['project_count']:>3} projects "
                  f"{s['total_size'] / 1e9:>7.1f} GB  {seqs:,} sequences = {frames:,} frames "
                  f"({frames - seqs:,} rows saved)")
    finally:
        conn.close()


def report_queries(ix, args: argparse.Namespace, failures: list[str]) -> None:
    print("\n== SPEC §7.2-style queries (top 5) ==")
    for query in args.queries:
        response = ix.search(query)
        print(f"  {query!r}: {response['total']} results in {response['took_ms']} ms")
        for item in response["results"][:5]:
            where = item["source"]["name"] + ("" if item["source"]["online"] else ", offline")
            print(f"      {item['kind']:<8} {item['rel_path']:<58} [{where}]")
    for query, suffix in args.expect:
        top = ix.search(query)["results"][:1]
        path = top[0]["rel_path"] or top[0]["name"] if top else ""
        if not (top and path.casefold().endswith(suffix.casefold())):
            failures.append(f"{query}: the first result must end in {suffix!r} "
                            f"(got {path!r})")
    if ix.search("kundenavn")["results"]:
        failures.append("kundenavn must find nothing without include_templates")


def report_latency(latencies: list[tuple[float, str]], failures: list[str]) -> None:
    print("\n== Search latency while the worker scanned ==")
    if not latencies:
        print("  (no scan was running)")
        return
    times = sorted(ms for ms, _ in latencies)
    p95 = times[min(len(times) - 1, int(0.95 * len(times)))]
    worst = max(latencies)
    print(f"  {len(times)} searches: p50 {statistics.median(times):.1f} ms, p95 {p95:.1f} ms, "
          f"max {worst[0]:.1f} ms ({worst[1]!r})")
    if p95 >= 250:
        failures.append(f"search p95 {p95:.0f} ms while scanning (target < 250 ms)")


def report_resolve(ix, args: argparse.Namespace, failures: list[str]) -> None:
    print("\n== map_paths on DaVinci-Resolve-style paths ==")
    if not args.resolve_path:
        print("  (no paths given: see --resolve-path)")
    else:
        started = time.perf_counter()
        mapped = ix.map_paths(args.resolve_path)
        print(f"  {mapped['total']} paths in {(time.perf_counter() - started) * 1000:.1f} ms")
        for f in mapped["folders"]:
            print(f"  folder {f['project']['name']!r} [{f['source']['name']}] count {f['count']} "
                  f"online {f['online']} → {f['project']['path']}")
        for d in mapped["other_dirs"]:
            print(f"  other  {d['path']} count {d['count']} online {d['online']}")
        if args.resolve_project:
            wanted = args.resolve_project.casefold()
            if not any(f["project"]["name"].casefold() == wanted and f["online"]
                       for f in mapped["folders"]):
                failures.append(f"map_paths: the Resolve paths must map to "
                                f"{args.resolve_project!r} (online)")
        loc = ix.locate(args.resolve_path[0]) or {}
        print(f"  locate → {loc.get('path')} (entry {(loc.get('entry') or {}).get('kind')}, "
              f"project {(loc.get('project') or {}).get('name')}, online {loc.get('online')})")
    if args.suggest:
        suggestions = ix.suggest_project_folders(args.suggest)
        print(f"  suggest {args.suggest!r} → "
              + ", ".join(f"{s['project']['name']} [{s['source']['name']}] {s['score']}"
                          for s in suggestions))


def report_index(ix, feed: queue.Queue, db_path: str) -> None:
    final = ix.status()
    wal = db_path + "-wal"
    print("\n== Index ==")
    print(f"  {final['entries']:,} entries, {final['files']:,} files, {final['dirs']:,} dirs, "
          f"{final['projects']} projects; DB {os.path.getsize(db_path) / 1e6:.1f} MB + WAL "
          f"{(os.path.getsize(wal) if os.path.exists(wal) else 0) / 1e6:.1f} MB")
    print(f"  sources: {final['sources_total']} total, {final['sources_included_online']} "
          f"included online, {final['sources_excluded']} excluded, {final['sources_offline']} "
          f"offline; worker {final['worker']}")
    seen: dict[str, int] = {}
    while True:
        try:
            kind = feed.get_nowait()[0]
        except queue.Empty:
            break
        seen[kind] = seen.get(kind, 0) + 1
    print(f"  events: {dict(sorted(seen.items()))}")


def main() -> int:
    args = parse_args()
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")

    with tempfile.TemporaryDirectory(prefix="projektsog-engine-smoke-",
                                     ignore_cleanup_errors=True) as tmp:
        os.environ["LOCALAPPDATA"] = tmp
        from projektsog import config, events, indexer

        cfg = config.Config(path=os.path.join(tmp, "config.json"))
        cfg.update({"hosts": list(args.host)})
        bus = events.EventBus(maxsize=100_000)
        feed = bus.subscribe()
        db_path = os.path.join(tmp, "index.db")
        ix = indexer.Indexer(cfg, bus, db_path=db_path)
        print(f"Smoke run on {config.hostname()} – hosts {cfg['hosts']}, index {db_path}")
        failures: list[str] = []
        try:
            done, seconds, timeline, latencies = watch(ix, args)
            print(f"\ninitial_scan_done={done} after {seconds:.1f} s")
            if not done:
                failures.append("initial_scan_done was not reached")
            sources = ix.list_sources()
            report_sources(sources, args, failures)
            report_timeline(sources, timeline, db_path)
            report_counts(sources, db_path)
            report_queries(ix, args, failures)
            report_latency(latencies, failures)
            report_resolve(ix, args, failures)
            report_index(ix, feed, db_path)
        finally:
            worker = ix._worker
            started = time.perf_counter()
            ix.stop()
            print(f"  stop() took {(time.perf_counter() - started) * 1000:.0f} ms; worker exit "
                  f"code {worker.proc.poll() if worker else None}")
    print("\n" + ("PASS" if not failures else "FAIL:\n  " + "\n  ".join(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
