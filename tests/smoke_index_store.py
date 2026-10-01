"""Smoke test on real data (read-only): scan the given roots through the scan worker process
and run SPEC §7.2-style queries on them.

    python tests/smoke_index_store.py --root PATH [--root PATH ...] [--query TEXT]
                                      [--expect QUERY=SUFFIX]

Nothing about a particular computer or network is built in: the roots, the queries and their
expected first results come from the command line or from environment variables.  Every option
may be repeated; a variable holds several values separated by ";", and an option given on the
command line replaces its variable:

    --root    PROJEKTSOG_SMOKE_ROOTS    local folders or UNC shares to scan (at least one)
    --query   PROJEKTSOG_SMOKE_QUERIES  extra queries to report (informational)
    --expect  PROJEKTSOG_SMOKE_EXPECT   "QUERY=SUFFIX": the first result of QUERY must have a
                                        path ending in SUFFIX

Example (PowerShell):

    $env:PROJEKTSOG_SMOKE_ROOTS = "C:\\Kunder 2026;\\\\GRAFIK-PC\\Arkiv"
    python tests/smoke_index_store.py --expect "lindholm=Rikke Lindholm" `
        --expect "lindholm klip=Rikke Lindholm\\Klip" --query "klar tand"

Uses a temporary LOCALAPPDATA and index database.  The roots are only listed (directory
entries and their metadata); nothing on them is opened for writing.  The worker runs under
``pythonw.exe`` with CREATE_NO_WINDOW and background priority, exactly as the app starts it.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import queue
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from ctypes import wintypes

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from projektsog import config, db, search, winfs  # noqa: E402
from projektsog.pathmap import PathMap, clean_path, split_unc  # noqa: E402

CREATE_NO_WINDOW = 0x08000000
# Informational queries that make sense on any project folders (the generic part of SPEC §7.2,
# plus the ASCII spelling "oe" of "ø", schema v3 / SPEC §15.2).
GENERIC_QUERIES = ["klip", "final", "2026", "mxf", "FX9", "infomøde", "koeb billet"]

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.GetVolumeInformationW.argtypes = [
    wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR,
    wintypes.DWORD]
_kernel32.GetVolumeInformationW.restype = wintypes.BOOL


def env_list(name: str) -> list[str]:
    """The ";"-separated values of the environment variable ``name`` (empty ones dropped)."""
    return [part.strip() for part in os.environ.get(name, "").split(";") if part.strip()]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", action="append", metavar="PATH",
                        help="folder or UNC share to scan (PROJEKTSOG_SMOKE_ROOTS)")
    parser.add_argument("--query", action="append", metavar="TEXT",
                        help="extra query to report (PROJEKTSOG_SMOKE_QUERIES)")
    parser.add_argument("--expect", action="append", metavar="QUERY=SUFFIX",
                        help="the first result of QUERY must end in SUFFIX "
                             "(PROJEKTSOG_SMOKE_EXPECT)")
    args = parser.parse_args(argv)
    args.root = [clean_path(r) for r in args.root or env_list("PROJEKTSOG_SMOKE_ROOTS")]
    if not args.root:
        parser.error("give at least one --root (or set PROJEKTSOG_SMOKE_ROOTS)")
    args.query = args.query or env_list("PROJEKTSOG_SMOKE_QUERIES")
    expectations = []
    for item in args.expect or env_list("PROJEKTSOG_SMOKE_EXPECT"):
        query, sep, suffix = item.partition("=")
        if not (sep and query.strip() and suffix.strip()):
            parser.error(f"--expect needs QUERY=SUFFIX, not {item!r}")
        expectations.append((query.strip(), suffix.strip()))
    args.expect = expectations
    return args


def describe_root(path: str, pathmap: PathMap) -> tuple[str, str, str, str | None]:
    """(kind, host, display name, unc path) of a root, as discovery would describe it."""
    unc = split_unc(path)
    if unc:
        host, share, rest = unc
        return "share", host, (rest.rsplit("\\", 1)[-1] if rest else share), path
    name = path.rstrip("\\").rsplit("\\", 1)[-1]
    return "local", config.hostname(), name if name != path[:2] else path, pathmap.unc_for(path)


def _first_ends_with(results, suffix):
    if not results:
        return False, "no results"
    top = results[0]
    path = top["rel_path"] or top["name"]
    ok = path.casefold().endswith(suffix.casefold())
    return ok, f"first = {top['kind']} {top['rel_path']!r} ({top['source']['name']})"


def volume_info(root: str, timeout: float = 10.0) -> dict | None:
    """Label/serial/fs via GetVolumeInformationW, on a thread (a dead share must not hang)."""
    out: dict = {}

    def probe() -> None:
        label = ctypes.create_unicode_buffer(261)
        fs = ctypes.create_unicode_buffer(261)
        serial, maxlen, flags = wintypes.DWORD(), wintypes.DWORD(), wintypes.DWORD()
        # The volume root: "C:\" for local folders, "\\host\share\" for shares.
        if root[1:2] == ":":
            path = root[:3]
        else:
            host, share, _rest = split_unc(root)
            path = f"\\\\{host}\\{share}\\"
        if _kernel32.GetVolumeInformationW(path, label, 261, ctypes.byref(serial),
                                           ctypes.byref(maxlen), ctypes.byref(flags), fs, 261):
            out.update(label=label.value, serial=f"{serial.value:08X}", fs=fs.value)

    thread = threading.Thread(target=probe, daemon=True)
    thread.start()
    thread.join(timeout)
    return out or None


class WorkerProcess:
    """The real scan worker under pythonw.exe, talking JSON lines."""

    def __init__(self, db_path: str, app_dir: str, localappdata: str) -> None:
        exe = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if not os.path.exists(exe):
            exe = sys.executable
        env = dict(os.environ, LOCALAPPDATA=localappdata, PYTHONPATH=REPO_ROOT)
        self.proc = subprocess.Popen([exe, "-m", "projektsog.scanworker", "--db", db_path],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, cwd=app_dir, env=env,
                                     creationflags=CREATE_NO_WINDOW, close_fds=True)
        self.events: queue.Queue = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        self.exe = exe

    def _pump(self) -> None:
        for line in self.proc.stdout:
            self.events.put(json.loads(line))

    def send(self, message: dict) -> None:
        self.proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def wait(self, predicate, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            event = self.events.get(timeout=max(0.1, deadline - time.monotonic()))
            if predicate(event):
                return event

    def close(self) -> int:
        self.proc.stdin.close()
        try:
            return self.proc.wait(20)
        finally:
            self.proc.stdout.close()


def main() -> int:
    args = parse_args()
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    # query, check(results) -> (ok, note); None = informational only
    queries: list[tuple[str, object]] = [
        (query, lambda r, s=suffix: _first_ends_with(r, s)) for query, suffix in args.expect]
    queries += [(query, None) for query in dict.fromkeys(args.query + GENERIC_QUERIES)
                if query not in {q for q, _ in args.expect}]
    queries.append(("kundenavn", lambda r: (not r, "no results" if not r else "templates leaked")))
    load_queries = [q for q, _ in queries[:2]]

    pathmap = PathMap(config.hostname())
    pathmap.update(winfs.local_shares(), {}, {}, [])
    with tempfile.TemporaryDirectory(prefix="projektsog-smoke-") as tmp:
        os.environ["LOCALAPPDATA"] = tmp
        db_path = config.db_path()
        conn = db.connect(db_path, writer=True)
        db.ensure_schema(conn)
        registry: dict[int, dict] = {}
        plans = []
        for path in args.root:
            kind, host, name, unc = describe_root(path, pathmap)
            info = volume_info(path)
            if info is None:
                print(f"SKIP {path}: not reachable")
                continue
            key = (f"vol:{info['serial']}:{path[2:]}" if kind == "local"
                   else f"unc:{path[2:]}")
            sid = db.upsert_source(conn, {"key": key, "kind": kind, "host": host,
                                          "display_name": name, "current_path": path,
                                          "unc_path": unc, "volume_label": info["label"],
                                          "volume_serial": info["serial"], "fs": info["fs"],
                                          "online": True})
            registry[sid] = {"id": sid, "key": key, "kind": kind, "host": host,
                             "display_name": name, "path": path, "unc_path": unc,
                             "volume_label": info["label"] if kind == "local" else None,
                             "volume_size": None, "last_drive": path[:2] if kind == "local"
                             else None, "online": True, "included": True,
                             "last_seen": time.time()}
            # Local scans carry the volume serial (SPEC §15.5), exactly as the indexer sends it.
            plans.append((sid, path, kind == "share", info["fs"],
                          info["serial"] if kind == "local" else None))
            print(f"source {sid}: {path}  label={info['label']!r} fs={info['fs']} "
                  f"serial={info['serial']}")
        conn.close()
        if not plans:
            print("no reachable root")
            return 1

        worker = WorkerProcess(db_path, config.app_dir(), tmp)
        ready = worker.wait(lambda e: e["ev"] == "ready", 20)
        print(f"worker ready: pid {ready['pid']} ({os.path.basename(worker.exe)})")
        worker.send({"cmd": "config", "cfg": config.DEFAULTS})
        reader = db.connect(db_path)
        job = 0
        results: dict[tuple[int, str], dict] = {}

        def run(label: str, kind: str, extra: dict, concurrent_search: bool = False) -> None:
            nonlocal job
            pending = {}
            for sid, path, is_network, fs, serial in plans:
                job += 1
                pending[job] = sid
                worker.send({"cmd": "scan", "job": job, "source_id": sid, "root_path": path,
                             "kind": kind, "is_network": is_network, "fs": fs,
                             **({"expected_serial": serial} if serial else {}), **extra})
            latencies = []
            while pending:
                if concurrent_search:
                    for query in load_queries:
                        started = time.perf_counter()
                        search.search(reader, registry, query, limit=200)
                        latencies.append((time.perf_counter() - started) * 1000)
                try:
                    event = worker.wait(lambda e: e["ev"] in ("done", "failed"),
                                        0.1 if concurrent_search else 600)
                except queue.Empty:
                    continue
                sid = pending.pop(event["job"])
                results[(sid, label)] = event.get("result") or event
            if latencies:
                print(f"  search latency while the worker scans ({len(latencies)} searches): "
                      f"p50 {statistics.median(latencies):.1f} ms, "
                      f"max {max(latencies):.1f} ms")

        print("\nshallow scans (first_time):")
        run("shallow", "shallow", {"first_time": True, "max_listings": 2000})
        for sid, path, *_ in plans:
            r = results[(sid, "shallow")]
            print(f"  {path}: {r['listings']} listings, {r['entries']} entries, "
                  f"{r['seconds']:.2f} s, ok={r['ok']} error={r['error']}")
        print("\ndeep scans (first):")
        run("deep", "deep", {"full": False}, concurrent_search=True)
        for sid, path, *_ in plans:
            r = results[(sid, "deep")]
            seqs = reader.execute("SELECT count(*), coalesce(sum(seq_count), 0) FROM entries "
                                  "WHERE source_id = ? AND is_seq = 1", (sid,)).fetchone()
            print(f"  {path}: {r['counts']['entry_count']:,} entries ({r['dirs']:,} dirs, "
                  f"{r['files']:,} files, {r['counts']['project_count']} projects), "
                  f"{seqs[0]} sequences collapsing {seqs[1]:,} frames, {r['seconds']:.2f} s, "
                  f"changed {r['changed']:,}, failed dirs {r['failed_dirs']}, "
                  f"ok={r['ok']} error={r['error']}")
        print("\ndeep scans (second; incremental where network NTFS):")
        run("deep2", "deep", {"full": False})
        for sid, path, *_ in plans:
            r = results[(sid, "deep2")]
            print(f"  {path}: changed {r['changed']}, reused leaf dirs {r['reused_dirs']}, "
                  f"incremental={r['incremental']}, {r['seconds']:.2f} s")
        failures = 0
        local = [p for p in plans if p[4]]
        if local:                    # another disk at the letter: nothing may be written
            sid, path, _net, fs, _serial = local[0]
            before = reader.execute("SELECT count(*), coalesce(sum(size), 0) FROM entries "
                                    "WHERE source_id = ?", (sid,)).fetchone()
            job += 1
            worker.send({"cmd": "scan", "job": job, "source_id": sid, "root_path": path,
                         "kind": "shallow", "first_time": True, "is_network": False, "fs": fs,
                         "expected_serial": "00000000"})
            event = worker.wait(lambda e: e["ev"] in ("done", "failed") and e["job"] == job, 60)
            r = event.get("result") or {}
            after = reader.execute("SELECT count(*), coalesce(sum(size), 0) FROM entries "
                                   "WHERE source_id = ?", (sid,)).fetchone()
            ok = (r.get("error") == "Disken er skiftet" and r.get("volume_changed")
                  and r.get("changed") == 0 and after == before)
            failures += not ok
            print(f"\n{'PASS' if ok else 'FAIL'} wrong expected_serial on {path}: "
                  f"error={r.get('error')!r} changed={r.get('changed')} rows {before} -> {after}")
        exit_code = worker.close()
        print(f"worker exit code after stdin EOF: {exit_code}")

        print("\nSPEC §7.2-style queries:")
        for query, check in queries:
            result = search.search(reader, registry, query)
            top = ", ".join(f"{r['name']} [{r['kind']}, {r['source']['name']}, {r['score']:.0f}]"
                            for r in result["results"][:3]) or "–"
            if check is None:
                verdict = "info"
            else:
                ok, note = check(result["results"])
                verdict = "PASS" if ok else "FAIL"
                failures += not ok
                top += f"  ({note})"
            print(f"  {verdict:4} {query!r}: {result['total']} hits, {result['took_ms']} ms: {top}")
        shown = search.search(reader, registry, "kundenavn", include_templates=True)
        print(f"  info 'kundenavn' with templates: {shown['total']} hits "
              f"({', '.join(sorted({r['kind'] for r in shown['results']}))})")
        reader.close()
        writer = db.connect(db_path, writer=True)      # integrity-check is an INSERT command
        writer.execute("INSERT INTO entries_fts(entries_fts, rank) VALUES ('integrity-check', 1)")
        writer.close()
        print("\nFTS integrity-check against entries: OK")
        print(f"worker log: {os.path.join(config.log_dir(), 'scanworker.log')} "
              f"({os.path.getsize(os.path.join(config.log_dir(), 'scanworker.log'))} bytes)")
        return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
