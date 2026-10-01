"""Read-only smoke test of winfs / pathmap / discovery against THIS PC's drives and hosts.

Run from the repo root:

    python tests/smoke_index_core.py [--host NAME] [--include PATH] [--exclude PATH]
                                     [--hotplug DRIVE] [--empty-drive DRIVE]

Lists volumes, shares, remote shares, all candidates and their probe verdicts, and checks the
verdicts you expect for your own setup (SPEC §0).  Nothing about a particular computer or
network is built in: the computers to ask and the expected verdicts come from the command line
or from environment variables.  Every option may be repeated; a variable holds several values
separated by ";", and an option given on the command line replaces its variable:

    --host        PROJEKTSOG_SMOKE_HOSTS        computers whose shares are listed and probed
    --include     PROJEKTSOG_SMOKE_INCLUDE      candidate paths that must be included
    --exclude     PROJEKTSOG_SMOKE_EXCLUDE      paths that must not be included (or no candidate)
    --hotplug     PROJEKTSOG_SMOKE_HOTPLUG      drives that must be listed as hotplug (USB)
    --empty-drive PROJEKTSOG_SMOKE_EMPTY_DRIVE  a drive without a medium (e.g. an empty card
                                                reader) that must be skipped without a dialog

Example (PowerShell):

    $env:PROJEKTSOG_SMOKE_HOSTS = "GRAFIK-PC;KLIPPER-PC"
    python tests/smoke_index_core.py --include "C:\\Kunder 2026" --include "\\\\GRAFIK-PC\\Arkiv" `
        --exclude C:\\Github --hotplug H:

Generic checks run without any option: the system volume, a local share read over UNC, the
PathMap mapping of this computer's own UNC paths, skipped volume labels, stale shares and
C:\\Users.  It only lists directories and reads metadata (no writes, no windows).  Exit code 1
when a check fails.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BOGUS_HOST = "PROJEKTSOG-NO-SUCH-HOST"


def env_list(name: str) -> list[str]:
    """The ";"-separated values of the environment variable ``name`` (empty ones dropped)."""
    return [part.strip() for part in os.environ.get(name, "").split(";") if part.strip()]


def drive_letter(value: str) -> str:
    return value.strip().rstrip(":\\").upper() + ":"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", action="append", metavar="NAME",
                        help="computer to ask for its shares (PROJEKTSOG_SMOKE_HOSTS)")
    parser.add_argument("--include", action="append", metavar="PATH",
                        help="path that must be an included candidate (PROJEKTSOG_SMOKE_INCLUDE)")
    parser.add_argument("--exclude", action="append", metavar="PATH",
                        help="path that must not be included (PROJEKTSOG_SMOKE_EXCLUDE)")
    parser.add_argument("--hotplug", action="append", metavar="DRIVE",
                        help="drive that must be listed as hotplug (PROJEKTSOG_SMOKE_HOTPLUG)")
    parser.add_argument("--empty-drive", metavar="DRIVE",
                        help="drive letter without a medium (PROJEKTSOG_SMOKE_EMPTY_DRIVE)")
    args = parser.parse_args(argv)
    args.host = args.host or env_list("PROJEKTSOG_SMOKE_HOSTS")
    args.include = args.include or env_list("PROJEKTSOG_SMOKE_INCLUDE")
    args.exclude = args.exclude or env_list("PROJEKTSOG_SMOKE_EXCLUDE")
    args.hotplug = [drive_letter(d) for d in args.hotplug or env_list("PROJEKTSOG_SMOKE_HOTPLUG")]
    empty = args.empty_drive or os.environ.get("PROJEKTSOG_SMOKE_EMPTY_DRIVE", "").strip()
    args.empty_drive = drive_letter(empty) if empty else None
    return args


def main() -> int:
    args = parse_args()
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["LOCALAPPDATA"] = tmp
        sys.path.insert(0, REPO)
        from projektsog import config, discovery, winfs
        from projektsog.pathmap import PathMap
        cfg = config.Config(path=os.path.join(tmp, "config.json"))
        return run(args, cfg, config, discovery, winfs, PathMap)


def timed(fn, *args, **kwargs):
    start = time.perf_counter()
    value = fn(*args, **kwargs)
    return value, (time.perf_counter() - start) * 1000


def run(args, cfg, config, discovery, winfs, PathMap) -> int:
    failures: list[str] = []

    def check(ok: bool, text: str) -> None:
        print(("  PASS " if ok else "  FAIL ") + text)
        if not ok:
            failures.append(text)

    own = config.hostname()
    print(f"== Host {own}; computers to ask: {args.host or 'none (see --host)'}")

    volumes, ms = timed(winfs.list_volumes)
    print(f"\n== list_volumes() {ms:.1f} ms")
    for v in volumes:
        print(f"  {v['drive']} {v['label']!r:20} serial={v['serial']} fs={v['fs']:6} "
              f"type={v['drive_type']} system={v['is_system']!s:5} hotplug={v['hotplug']!s:5} "
              f"size={v['size'] / 1e9:,.0f} GB")
    drives = {v["drive"]: v for v in volumes}
    for letter in args.hotplug:
        check(bool(drives.get(letter, {}).get("hotplug")),
              f"{letter} {drives.get(letter, {}).get('label')!r} hotplug=True (USB)")
    system_drive = drive_letter(os.environ.get("SystemDrive", "C:"))
    check(bool(drives.get(system_drive, {}).get("is_system")),
          f"{system_drive} is the system volume")
    again, ms2 = timed(winfs.list_volumes)
    print(f"  second call {ms2:.1f} ms, same drives: {[v['drive'] for v in again] == list(drives)}")
    if args.empty_drive:
        root = args.empty_drive + "\\"
        check(args.empty_drive not in drives,
              f"{args.empty_drive} (no medium) skipped as not ready")
        empty_slot, ms = timed(winfs.volume_info, root)
        check(empty_slot is None and ms < 500,
              f"volume_info({root!r}) without a medium -> None in {ms:.1f} ms (no dialog)")

    shares, ms = timed(winfs.local_shares)
    print(f"\n== local_shares() {ms:.1f} ms")
    for s in shares:
        print(f"  {s['name']!r:36} -> {s['path']!r} exists={os.path.isdir(s['path'])}")
    mapped = winfs.mapped_drives()
    print(f"== mapped_drives(): {mapped}")
    # Shares of this computer that exist on a listed volume: the generic UNC/PathMap samples.
    live_shares = [s for s in shares
                   if s["path"][:2].upper() in drives and os.path.isdir(s["path"])]
    if live_shares:
        share = live_shares[0]
        unc_info = winfs.volume_info(rf"\\{own}\{share['name']}")
        serial = drives[share["path"][:2].upper()]["serial"]
        check(bool(unc_info) and unc_info["serial"] == serial,
              f"volume_info over UNC of share {share['name']!r} matches its drive ({unc_info})")
    else:
        print("  (no local share on a listed volume: the UNC checks are skipped)")

    print("\n== remote_shares()")
    remote: dict[str, list[str] | None] = {}
    for host in args.host:
        names, ms = timed(winfs.remote_shares, host)
        remote[host] = names
        print(f"  {host:16} {ms:7.1f} ms {names}")
        check(names is not None, f"{host} answers")
    # Unique names per run: Windows caches failed name lookups for a while.
    bogus_host = f"{BOGUS_HOST}-{os.getpid()}"
    bogus, ms = timed(winfs.remote_shares, bogus_host)
    print(f"  {bogus_host} {ms:7.1f} ms {bogus}")
    check(bogus is None and ms < 8500, f"bogus host -> None in {ms:.0f} ms")
    slow_host = bogus_host + "-B"
    short, ms_short = timed(winfs.remote_shares, slow_host, timeout=0.3)
    busy, ms_busy = timed(winfs.remote_shares, slow_host, timeout=5)
    check(short is None and busy is None and 250 <= ms_short < 1000 and ms_busy < 50,
          f"lookup still running: timeout after {ms_short:.0f} ms, repeat call busy after "
          f"{ms_busy:.1f} ms")

    host_ips = {h: winfs.resolve_host_ips(h) for h in args.host}
    own_ips = winfs.resolve_host_ips(own)
    print(f"== resolve_host_ips(): {host_ips} own={own_ips}")

    pathmap = PathMap(own)
    pathmap.update(shares, mapped, host_ips, own_ips)
    print("\n== PathMap")
    samples = []
    for share in live_shares[:1]:
        samples += [rf"\\{own.lower()}\{share['name']}\Klip\A001.MOV",
                    rf"\\?\UNC\127.0.0.1\{share['name']}\x",
                    share["path"].lower().replace("\\", "/") + "/"]
    for host in args.host[:1]:
        samples.append(rf"\\{host.lower()}\{(remote.get(host) or ['share'])[0]}\\x\\")
    for sample in samples:
        print(f"  normalize({sample!r}) = {pathmap.normalize(sample)!r}")
    for share in live_shares[:1]:
        local_sample = os.path.join(share["path"], "x")
        print(f"  unc_for({local_sample!r}) = {pathmap.unc_for(local_sample)!r}")
        expected = os.path.join(share["path"], "Klip", "A001.MOV")
        check(pathmap.normalize(samples[0]).casefold() == expected.casefold(),
              "Resolve UNC path of a local file maps to the local path")

    verdicts: dict[str, tuple[bool, str]] = {}

    def probe_all(cands: list[dict], title: str) -> None:
        print(f"\n== {title}: {len(cands)} candidates")
        for c in cands:
            result, ms = timed(discovery.probe_details, c["path"], cfg, hotplug=c["hotplug"])
            verdicts[c["path"].casefold()] = (result.include, result.reason)
            evidence = (result.projects[:2] or result.templates[:1]
                        or ([result.media_file] if result.media_file else []))
            extra = f" cache={result.cache_dir!r}" if result.cache_dir else ""
            print(f"  {'INCLUDE' if result.include else 'exclude'} {c['path']!r:44} "
                  f"{result.reason!r} [{result.listings} listings, {ms:.0f} ms"
                  f"{', budget/time limit' if result.exhausted else ''}]{extra}")
            print(f"          key={c['key']!r} name={c['display_name']!r} unc={c['unc_path']!r} "
                  f"evidence={evidence}")

    local, ms = timed(discovery.local_candidates, cfg, volumes, shares, own)
    print(f"\nlocal_candidates() {ms:.1f} ms")
    probe_all(local, "Local candidates")
    local_paths = {c["path"].casefold() for c in local}
    for s in shares:
        if not os.path.isdir(s["path"]):
            check(s["path"].casefold() not in local_paths, f"stale share {s['path']!r} skipped")
    skip_labels = {label.casefold() for label in cfg["skip_volume_labels"]}
    for v in volumes:
        if v["label"].casefold() in skip_labels:
            check(not any(c["drive"] == v["drive"] for c in local),
                  f"{v['drive']} {v['label']!r} skipped by label")
    check(r"c:\users" not in local_paths, r"C:\Users is not a candidate (skip_top_level_dirs)")

    for host in args.host:
        if remote[host]:
            probe_all(discovery.remote_candidates(host, remote[host]), f"Remote candidates {host}")
    probe_all(discovery.mapped_candidates(mapped, pathmap=pathmap, own_host=own), "Mapped drives")

    print("\n== Required verdicts")
    if not (args.include or args.exclude):
        print("  (none given: see --include / --exclude)")
    for path in args.include:
        verdict = verdicts.get(path.casefold())
        check(verdict is not None and verdict[0], f"include {path} ({verdict})")
    for path in args.exclude:
        verdict = verdicts.get(path.casefold())
        check(verdict is None or not verdict[0], f"exclude {path} ({verdict or 'no candidate'})")
    print(f"\n{len(failures)} failed check(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
