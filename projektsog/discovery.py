"""Candidate roots, the auto-include probe and source keys (SPEC §4.1, §4.3, §4.4).

The candidate builders take volume/share lists as input.  Only :func:`local_candidates`
and :func:`probe` read the file system (directory listings only), always through
``winfs.call_with_timeout``.
"""

from __future__ import annotations

import collections
import fnmatch
import functools
import logging
import os
import re
import stat
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable

from . import winfs
from .config import DEFAULTS, Config, hostname
from .pathmap import (PathMap, clean_path, is_drive_path, join_path, long_path, path_parts,
                      relative_parts, split_unc)
from .textutil import fold

log = logging.getLogger(__name__)

CfgLike = Config | dict[str, Any]     # a Config or a Config.snapshot()

# User-facing probe reasons (Danish).
REASON_SELF = "Mappen er selv et projekt"
# A folder named like a template sub-folder ("Klip") whose own sub-folders follow the project
# template: project material, but never a project itself (SPEC §15.7, §15.12).
REASON_PART = "Mappen er en del af et projekt"
REASON_TEMPLATE = "Projektskabelon fundet"
REASON_MEDIA = "Mediefiler fundet"
REASON_NONE = "Ingen projektmapper fundet"
REASON_RESOLVE_CACHE = "Ingen projektmapper fundet (DaVinci Resolve-cache)"
REASON_NO_ACCESS = "Ingen adgang"
REASON_NO_RESPONSE = "Svarer ikke"
REASON_MISSING = "Mappen findes ikke"

# How a (non-system) volume is split into sources, decided at its first sighting and then kept
# (SPEC §15.6): the whole volume as one source, or its top-level folders/shares.
LAYOUT_WHOLE = "whole"
LAYOUT_FOLDERS = "folders"

_PROJECT_DEPTH = 2          # children (1) and grandchildren (2) are checked for projects
_MEDIA_DEPTH = 4            # hot-plug media search goes deeper (PRIVATE\AVCHD\BDMV\STREAM)
_PROBE_TIMEOUT = 30.0
_PROBE_GRACE = 5.0          # extra wait for a listing that is running when time is up
_LOCAL_TIMEOUT = 10.0
_HIDDEN_SYSTEM = stat.FILE_ATTRIBUTE_HIDDEN | stat.FILE_ATTRIBUTE_SYSTEM
# DaVinci Resolve working-folder names.  A candidate containing one of them is (or holds) a
# Resolve cache, so render/proxy/still files in it do not count as footage.
_RESOLVE_CACHE_DIRS = frozenset({"cacheclip", "optimizedmedia", "proxymedia", ".gallery"})
_MISSING_ERRORS = frozenset({2, 3, 67, 267})
_NO_RESPONSE_ERRORS = frozenset({21, 53, 59, 64, 121, 1222, 1231, 1232})


# --------------------------------------------------------------------------------------
# Source keys (§4.1)
# --------------------------------------------------------------------------------------

def key_local(serial: str, rel: str) -> str:
    """``vol:<SERIAL>:<rel>``, e.g. ``vol:5E3A0B21:\\2024 Disk Sølv`` (``\\`` = whole volume)."""
    return f"vol:{serial.strip().upper()}:{_rel(rel)}"


def key_share(host: str, share: str, rel: str = "") -> str:
    """``unc:<HOST>\\<share><rel>``, e.g. ``unc:GRAFIK-PC\\Forår 2026 (HDD)``."""
    name = share.strip("\\")
    suffix = _rel(rel)
    if suffix == "\\":
        suffix = ""  # the share root itself
    return f"unc:{_host(host)}\\{name}{suffix}"


def _rel(rel: str) -> str:
    return "\\" + "\\".join(s for s in rel.replace("/", "\\").split("\\") if s)


def _host(host: str) -> str:
    return host.strip().strip("\\").upper()


# --------------------------------------------------------------------------------------
# Project heuristics (§4.4)
# --------------------------------------------------------------------------------------

def _setting(cfg: Any, key: str) -> Any:
    """``cfg[key]`` for a Config or a snapshot dict, falling back to the default."""
    try:
        value = cfg[key]
    except (KeyError, TypeError):
        value = None
    return DEFAULTS[key] if value is None else value


def _name_key(name: str) -> str:
    """Case-insensitive, Unicode-normalised folder name (NFD names from Macs compare equal)."""
    return unicodedata.normalize("NFC", name).strip().casefold()


@functools.lru_cache(maxsize=64)
def _name_set(names: tuple[str, ...]) -> frozenset[str]:
    return frozenset(_name_key(n) for n in names)


def _names(cfg: Any, key: str) -> frozenset[str]:
    return _name_set(tuple(_setting(cfg, key)))


@functools.lru_cache(maxsize=8)
def _compile(pattern: str) -> re.Pattern[str] | None:
    try:
        return re.compile(pattern)
    except re.error as exc:
        log.warning("Invalid template_folder_regex %r: %s", pattern, exc)
        return None


def is_template_name(name: str, cfg: CfgLike) -> bool:
    """True for project templates such as ``1. KUNDENAVN`` (regex on ``fold(name)``)."""
    regex = _compile(str(_setting(cfg, "template_folder_regex")))
    return regex is not None and regex.search(fold(name)) is not None


def is_project_part(name: str, cfg: CfgLike) -> bool:
    """True for a folder named like a project template sub-folder (``Klip``, ``Musik`` …): part
    of a project and never a project itself, whatever it contains (SPEC §15.7, §15.12)."""
    return _name_key(name) in _names(cfg, "project_template_dirs")


def looks_like_project(child_dir_names: Iterable[str], cfg: CfgLike) -> bool:
    """True when at least ``project_min_template_dirs`` of the direct sub-folder names are
    in ``project_template_dirs`` (case-insensitive)."""
    templates = _names(cfg, "project_template_dirs")
    needed = max(1, int(_setting(cfg, "project_min_template_dirs")))
    hits: set[str] = set()
    for name in child_dir_names:
        key = _name_key(name)
        if key in templates:
            hits.add(key)
            if len(hits) >= needed:
                return True
    return False


# --------------------------------------------------------------------------------------
# Directory listing helpers
# --------------------------------------------------------------------------------------

@dataclass(slots=True)
class _Listing:
    dirs: list[str]     # plain sub-directories (no junctions/symlinks, not hidden+system)
    files: list[str]
    hidden_dirs: list[str] = field(default_factory=list)    # merely hidden (``skip_hidden``)


def _list_dir(path: str, *, skip_hidden: bool = False) -> _Listing:
    """List one directory completely; its handle is closed before the entries are used.

    Entries that are both hidden and system are always skipped; ``skip_hidden`` also leaves
    merely hidden ones out of ``dirs``/``files`` (used for volume roots: hidden top-level
    folders are no new candidates) and reports those folders in ``hidden_dirs``.
    """
    with os.scandir(long_path(path)) as it:
        entries = list(it)
    listing = _Listing([], [])
    for entry in entries:
        try:
            if entry.is_symlink() or entry.is_junction():
                continue
            attributes = entry.stat(follow_symlinks=False).st_file_attributes
            if attributes & _HIDDEN_SYSTEM == _HIDDEN_SYSTEM:
                continue
            is_dir = entry.is_dir(follow_symlinks=False)
            if skip_hidden and attributes & stat.FILE_ATTRIBUTE_HIDDEN:
                if is_dir:
                    listing.hidden_dirs.append(entry.name)
                continue
            (listing.dirs if is_dir else listing.files).append(entry.name)
        except OSError:
            continue
    return listing


def _is_dir(path: str) -> bool:
    return os.path.isdir(long_path(path))


def _sorted_names(names: Iterable[str]) -> list[str]:
    return sorted(names, key=lambda n: (n.casefold(), n))


def _first_media_file(files: Iterable[str], cfg: Any) -> str | None:
    """First file with a ``media_exts`` extension that the scanner would not skip."""
    exts = _names(cfg, "media_exts")
    skip_names = _names(cfg, "exclude_file_names")
    globs = [g.lower() for g in _setting(cfg, "exclude_file_globs")]
    for name in files:
        dot = name.rfind(".")
        if dot < 0 or name[dot + 1:].lower() not in exts:
            continue
        lower = name.lower()
        if _name_key(name) in skip_names or any(fnmatch.fnmatchcase(lower, g) for g in globs):
            continue
        return name
    return None


# --------------------------------------------------------------------------------------
# Probe (§4.4)
# --------------------------------------------------------------------------------------

@dataclass
class ProbeResult:
    """Outcome of :func:`probe_details`; :func:`probe` returns ``as_tuple()``."""

    include: bool = False
    reason: str = REASON_NONE
    project_count: int = 0
    listings: int = 0
    projects: list[str] = field(default_factory=list)    # rel paths of project folders
    templates: list[str] = field(default_factory=list)   # rel paths of template folders
    media_file: str | None = None                        # rel path of the first media file
    cache_dir: str | None = None                         # rel path of a Resolve cache folder
    exhausted: bool = False                              # stopped by budget or time limit

    def as_tuple(self) -> tuple[bool, str, int]:
        return self.include, self.reason, self.project_count


def probe(path: str, cfg: CfgLike, *, hotplug: bool = False, max_listings: int = 400,
          timeout: float = _PROBE_TIMEOUT) -> tuple[bool, str, int]:
    """Decide whether a candidate root is auto-included: ``(include, reason_da, project_count)``.

    Lists the folder, its children and (budget permitting) grandchildren.  Include when one
    of them looks like a project (or is a template folder); else, on hot-plug volumes, when
    a media file is seen within the budget (unless the folder is a Resolve cache).
    """
    return probe_details(path, cfg, hotplug=hotplug, max_listings=max_listings,
                         timeout=timeout).as_tuple()


def probe_details(path: str, cfg: CfgLike, *, hotplug: bool = False, max_listings: int = 400,
                  timeout: float = _PROBE_TIMEOUT) -> ProbeResult:
    """:func:`probe` with the evidence (which folders/files decided it)."""
    root = clean_path(path)
    budget = max(1, max_listings)

    def walk() -> ProbeResult:
        return _probe_walk(root, cfg, hotplug, budget, time.monotonic() + timeout)

    status, result = winfs.call_with_timeout(f"probe:{root}", walk, timeout + _PROBE_GRACE)
    if status == "ok" and result is not None:
        return result
    if status == "error":
        exc = winfs.last_exception()
        if not isinstance(exc, OSError):
            log.error("Probe of %s failed", root, exc_info=exc)
        reason = _error_reason(exc) if isinstance(exc, OSError) else REASON_NO_ACCESS
        return ProbeResult(reason=reason)
    return ProbeResult(reason=REASON_NO_RESPONSE)


def _error_reason(exc: OSError) -> str:
    # The Win32 code decides first: Python maps e.g. ERROR_BAD_NETPATH (53, host
    # unreachable) to FileNotFoundError.
    code = getattr(exc, "winerror", None)
    if code in _NO_RESPONSE_ERRORS or isinstance(exc, TimeoutError):
        return REASON_NO_RESPONSE
    if code in _MISSING_ERRORS or isinstance(exc, (FileNotFoundError, NotADirectoryError)):
        return REASON_MISSING
    return REASON_NO_ACCESS


def _probe_walk(root: str, cfg: Any, hotplug: bool, max_listings: int,
                deadline: float) -> ProbeResult:
    """Breadth-first walk behind :func:`probe`; an unreadable root raises OSError."""
    listing = _list_dir(root)
    result = ProbeResult(listings=1)
    parts = path_parts(root)
    root_name = parts[-1] if len(parts) > 1 else ""
    if (root_name and is_template_name(root_name, cfg)) or looks_like_project(listing.dirs, cfg):
        if root_name and is_project_part(root_name, cfg):
            # A shared "Klip" folder with project sub-folders: project material, no project.
            result.include, result.reason = True, REASON_PART
            return result
        result.include, result.reason, result.project_count = True, REASON_SELF, 1
        return result

    excluded = _names(cfg, "exclude_dir_names")

    def children(rel: tuple[str, ...], found: _Listing) -> list[tuple[str, ...]]:
        """Note media/cache evidence in ``found``; return the sub-folders worth listing."""
        if hotplug and result.media_file is None:
            media = _first_media_file(found.files, cfg)
            if media is not None:
                result.media_file = "\\".join((*rel, media))
        subdirs = []
        for name in _sorted_names(found.dirs):
            key = _name_key(name)
            if key in _RESOLVE_CACHE_DIRS:
                result.cache_dir = result.cache_dir or "\\".join((*rel, name))
            elif key not in excluded:
                subdirs.append((*rel, name))
        return subdirs

    def media_search_open() -> bool:
        return (hotplug and not result.projects and not result.templates
                and result.media_file is None and result.cache_dir is None)

    queue = collections.deque((rel, 1) for rel in children((), listing))
    while queue:
        rel, depth = queue[0]
        if depth > _PROJECT_DEPTH and not media_search_open():
            break
        if result.listings >= max_listings or time.monotonic() >= deadline:
            result.exhausted = True
            break
        queue.popleft()
        rel_path = "\\".join(rel)
        if depth <= _PROJECT_DEPTH and is_template_name(rel[-1], cfg):
            result.templates.append(rel_path)
            continue
        result.listings += 1
        try:
            sub = _list_dir(join_path(root, *rel))
        except OSError as exc:
            log.debug("Probe cannot list %s\\%s: %s", root, rel_path, exc)
            continue
        if depth <= _PROJECT_DEPTH and looks_like_project(sub.dirs, cfg):
            result.projects.append(rel_path)
            continue
        subdirs = children(rel, sub)
        if depth < _PROJECT_DEPTH or (hotplug and depth < _MEDIA_DEPTH):
            queue.extend((child, depth + 1) for child in subdirs)
    return _verdict(result, hotplug)


def _verdict(result: ProbeResult, hotplug: bool) -> ProbeResult:
    count = len(result.projects)
    if count:
        result.include, result.project_count = True, count
        result.reason = "1 projektmappe fundet" if count == 1 else f"{count} projektmapper fundet"
    elif result.templates:
        result.include, result.reason = True, REASON_TEMPLATE
    elif hotplug and result.media_file is not None and result.cache_dir is None:
        result.include, result.reason = True, REASON_MEDIA
    elif hotplug and result.media_file is not None:
        result.reason = REASON_RESOLVE_CACHE
    else:
        result.reason = REASON_NONE
    return result


# --------------------------------------------------------------------------------------
# Candidates (§4.3)
# --------------------------------------------------------------------------------------

def _candidate(*, key: str, kind: str, path: str, unc_path: str | None, host: str,
               share: str | None, volume_serial: str | None, volume_label: str | None,
               fs: str | None, display_name: str, drive: str | None, hotplug: bool,
               volume_size: int | None, manual: bool) -> dict:
    return {"key": key, "kind": kind, "path": path, "unc_path": unc_path, "host": host,
            "share": share, "volume_serial": volume_serial, "volume_label": volume_label,
            "fs": fs, "display_name": display_name, "drive": drive, "hotplug": hotplug,
            "volume_size": volume_size, "manual": manual}


def _local_candidate(vol: dict, root: str, rel: list[str], own_host: str,
                     pathmap: PathMap | None, manual: bool = False) -> dict:
    path = join_path(root, *rel)
    label = str(vol.get("label") or "")
    drive = vol.get("drive")
    display = rel[-1] if rel else (label or f"{drive} (uden navn)")
    return _candidate(
        key=key_local(str(vol["serial"]), "\\".join(rel)), kind="local", path=path,
        unc_path=pathmap.unc_for(path) if pathmap is not None else None, host=own_host,
        share=None, volume_serial=str(vol["serial"]).upper(), volume_label=label,
        fs=vol.get("fs"), display_name=display, drive=drive, hotplug=bool(vol.get("hotplug")),
        volume_size=vol.get("size") or None, manual=manual)


def _share_candidate(host: str, share: str, rest: list[str], *, manual: bool) -> dict:
    path = "\\\\" + "\\".join([host, share, *rest])
    return _candidate(
        key=key_share(host, share, "\\".join(rest)), kind="share", path=path, unc_path=path,
        host=host, share=share, volume_serial=None, volume_label=None, fs=None,
        display_name=rest[-1] if rest else share, drive=None, hotplug=False, volume_size=None,
        manual=manual)


@dataclass(frozen=True, slots=True)
class _VolumeFacts:
    dirs: tuple[str, ...]              # plain, non-hidden folders directly in the volume root
    files: tuple[str, ...]             # non-hidden files directly in the volume root
    project_dirs: frozenset[str]       # root folders that look like a project / template
    existing_shares: frozenset[str]    # casefolded paths of this volume's shares that exist
    hidden_dirs: tuple[str, ...] = ()  # merely hidden folders directly in the volume root


_facts_cache: dict[tuple[str, str], _VolumeFacts] = {}
_facts_lock = threading.Lock()


def _serial(vol: dict) -> str:
    return str(vol.get("serial") or "").upper()


def _top_level_dirs(names: Iterable[str], cfg: Any) -> list[str]:
    skip = _names(cfg, "skip_top_level_dirs") | _names(cfg, "exclude_dir_names")
    return _sorted_names(n for n in names if _name_key(n) not in skip)


def _volume_facts(root: str, share_paths: list[str], is_system: bool, cfg: Any,
                  need_layout: bool = True) -> _VolumeFacts:
    """Listings behind :func:`local_candidates` for one volume (runs on a winfs thread).

    The top-level folders are only listed when the volume's layout still has to be decided
    (``need_layout``); a volume whose layout is known costs one root listing per pass.
    """
    listing = _list_dir(root, skip_hidden=True)
    project_dirs: set[str] = set()
    if need_layout and not is_system:
        for name in _top_level_dirs(listing.dirs, cfg):
            if is_template_name(name, cfg):
                project_dirs.add(name)
                continue
            try:
                sub = _list_dir(join_path(root, name))
            except OSError as exc:
                log.debug("Cannot list %s: %s", join_path(root, name), exc)
                continue
            if looks_like_project(sub.dirs, cfg):
                project_dirs.add(name)
    existing = frozenset(p.casefold() for p in share_paths if _is_dir(p))
    return _VolumeFacts(tuple(listing.dirs), tuple(listing.files), frozenset(project_dirs),
                        existing, tuple(listing.hidden_dirs))


def _outermost(paths: dict[tuple[str, ...], list[str]]) -> list[list[str]]:
    """Drop paths nested inside another path of the set (keys are casefolded parts)."""
    kept: list[tuple[str, ...]] = []
    for folded in sorted(paths, key=lambda f: (len(f), f)):
        if not any(folded[:len(k)] == k for k in kept):
            kept.append(folded)
    return [paths[k] for k in sorted(kept)]


def _volume_candidates(cfg: Any, vol: dict, root: str, facts: _VolumeFacts,
                       shares: list[tuple[str, str]], own_host: str,
                       pathmap: PathMap, layout: str | None = None,
                       known_keys: frozenset[str] = frozenset()) -> list[dict]:
    """Pure part of :func:`local_candidates` for one volume.

    ``layout`` is the volume's kept layout (:data:`LAYOUT_WHOLE`/:data:`LAYOUT_FOLDERS`);
    None decides it now (first sighting).  A hidden top-level folder is a candidate only when
    its (casefolded) key is in ``known_keys``.
    """
    # A project directly in the root (or media in a hot-plug root) makes the whole volume
    # one source.  Never on the system volume: that would index Windows and Program Files.
    # Once a volume has sources its layout is kept: a file saved to (or a project created
    # in) the root later must not replace its folder sources, nor the reverse (§15.6).
    if vol.get("is_system"):
        whole_volume = False
    elif layout is not None:
        whole_volume = layout == LAYOUT_WHOLE
    else:
        whole_volume = (
            looks_like_project(facts.dirs, cfg) or bool(facts.project_dirs)
            or (bool(vol.get("hotplug")) and _first_media_file(facts.files, cfg) is not None))
    if whole_volume:
        return [_local_candidate(vol, root, [], own_host, pathmap)]

    paths: dict[tuple[str, ...], list[str]] = {}
    for name in _top_level_dirs(facts.dirs, cfg):
        paths.setdefault((name.casefold(),), [name])
    # Hidden top-level folders are no new candidates (SPEC §15.8), but one that already is a
    # source (registered before that rule, R2-IDX-1) stays one: dropping it would leave an
    # offline ghost on a connected disk that nothing ever brings back or forgets.
    for name in _top_level_dirs(facts.hidden_dirs, cfg):
        if key_local(_serial(vol), name).casefold() in known_keys:
            paths.setdefault((name.casefold(),), [name])
    skip_top = _names(cfg, "skip_top_level_dirs")
    for name, share_path in shares:
        if share_path.casefold() not in facts.existing_shares:
            log.debug("Share %s (%s) is stale - skipped", name, share_path)
            continue
        rel = relative_parts(share_path, root)
        if not rel:
            continue  # a share of the whole volume: its folders are candidates already
        if len(rel) == 1 and _name_key(rel[0]) in skip_top:
            continue  # e.g. C:\Users: never a root, even when shared
        paths.setdefault(tuple(p.casefold() for p in rel), rel)
    return [_local_candidate(vol, root, rel, own_host, pathmap) for rel in _outermost(paths)]


def local_candidates(cfg: CfgLike, volumes: list[dict], shares: list[dict], own_host: str,
                     *, timeout: float = _LOCAL_TIMEOUT,
                     layouts: dict[str, str] | None = None,
                     known_keys: Iterable[str] = ()) -> list[dict]:
    """Candidate roots on local volumes (SPEC §4.3.1).

    ``volumes`` from ``winfs.list_volumes()``, ``shares`` from ``winfs.local_shares()``.
    Per volume: its existing shares and its top-level folders (minus skip lists and hidden
    folders), or the whole volume when its root directly contains a project folder (or, on
    a hot-plug volume, media files); nested candidates are dropped (outermost wins).
    ``layouts`` (upper-case serial → :data:`LAYOUT_WHOLE`/:data:`LAYOUT_FOLDERS`) keeps the
    layout of volumes that already have sources.  ``known_keys`` are the keys of existing
    (auto-discovered) sources: a hidden top-level folder stays a candidate when its key is
    one of them.  Volumes are listed in parallel; a volume that is slow to answer reuses its
    last listing.  A ``stale`` volume (its facts are old, its medium may have been swapped)
    is not listed and gives no candidates.
    """
    own = _host(own_host)
    skip_labels = _names(cfg, "skip_volume_labels")
    layouts = {str(k).upper(): v for k, v in (layouts or {}).items()}
    known = frozenset(str(k).casefold() for k in known_keys)
    share_list: list[tuple[str, str]] = []
    for share in shares:
        name, path = str(share.get("name") or ""), clean_path(str(share.get("path") or ""))
        if name and is_drive_path(path):
            share_list.append((name, path))
    pathmap = PathMap(own)
    pathmap.update([{"name": n, "path": p} for n, p in share_list], {}, {}, [])

    jobs: list[tuple[dict, str, list[tuple[str, str]]]] = []
    for vol in volumes:
        if _name_key(str(vol.get("label") or "")) in skip_labels:
            continue
        if vol.get("stale"):
            log.debug("%s answers slowly - its sources stay as they are", vol.get("drive"))
            continue
        root = clean_path(str(vol.get("root") or f"{vol.get('drive', '')}\\"))
        jobs.append((vol, root, [(n, p) for n, p in share_list
                                 if relative_parts(p, root) is not None]))
    outcomes = winfs.call_many_with_timeout(
        [(f"discover:{root}", functools.partial(
            _volume_facts, root, [p for _, p in vs], bool(vol.get("is_system")), cfg,
            _serial(vol) not in layouts))
         for vol, root, vs in jobs], timeout)

    candidates: list[dict] = []
    seen: set[str] = set()
    for (vol, root, vol_shares), (status, facts) in zip(jobs, outcomes):
        cache_key = (_serial(vol), root.casefold())
        with _facts_lock:
            if status == "ok":
                _facts_cache[cache_key] = facts
            elif status in ("busy", "timeout"):
                facts = _facts_cache.get(cache_key)
            else:
                _facts_cache.pop(cache_key, None)
        if facts is None:
            log.warning("Cannot list %s (%s) - no candidates from it this time", root, status)
            continue
        for cand in _volume_candidates(cfg, vol, root, facts, vol_shares, own, pathmap,
                                       layouts.get(_serial(vol)), known):
            folded = cand["key"].casefold()
            if folded in seen:
                log.warning("Duplicate source key %s for %s - skipped", cand["key"], cand["path"])
                continue
            seen.add(folded)
            candidates.append(cand)
    return candidates


def remote_candidates(host: str, share_names: list[str]) -> list[dict]:
    """One share candidate per plain share of ``host`` (``remote_shares()`` result)."""
    name = _host(host)
    candidates: list[dict] = []
    seen: set[str] = set()
    for share in share_names:
        if not share or share.endswith("$") or share.casefold() in seen:
            continue
        seen.add(share.casefold())
        candidates.append(_share_candidate(name, share, [], manual=False))
    return candidates


def mapped_candidates(mapped: dict[str, str], *, pathmap: PathMap | None = None,
                      own_host: str | None = None) -> list[dict]:
    """Share candidates for mapped network drives (``{"Z:": "\\\\\\\\HOST\\\\share"}``).

    The candidate is the UNC target, so a drive mapped to a discovered share yields the same
    key as the host's share candidate.  With ``pathmap`` IP/FQDN hosts become host names;
    mappings to this computer are skipped (its volumes are discovered locally).
    """
    own = _host(own_host or hostname())
    local_names = {own, "LOCALHOST", "127.0.0.1"}
    candidates: list[dict] = []
    seen: set[str] = set()
    for drive in sorted(mapped):
        target = mapped[drive]
        unc = split_unc(pathmap.normalize(target) if pathmap is not None else target)
        if unc is None or not unc[1] or unc[0] in local_names:
            continue
        cand = _share_candidate(unc[0], unc[1], [s for s in unc[2].split("\\") if s],
                                manual=False)
        if cand["key"].casefold() not in seen:
            seen.add(cand["key"].casefold())
            candidates.append(cand)
    return candidates


def extra_root_candidates(cfg: CfgLike, volumes: list[dict], own_host: str, *,
                          pathmap: PathMap | None = None) -> list[dict]:
    """Candidates for ``cfg["extra_roots"]`` (``manual=True``, always included).

    Local roots need their volume in ``volumes`` (the key contains its serial) and are
    skipped while it is absent.  With ``pathmap`` aliases are resolved first (own-host UNC →
    local path, mapped drive → UNC) and local candidates get their ``unc_path``.
    """
    own = _host(own_host)
    candidates: list[dict] = []
    seen: set[str] = set()
    for raw in _setting(cfg, "extra_roots"):
        path = pathmap.normalize(raw) if pathmap is not None else clean_path(raw)
        cand = _extra_candidate(path, volumes, own, pathmap)
        if cand is None:
            log.debug("Extra root %r is not available now", raw)
            continue
        if cand["key"].casefold() not in seen:
            seen.add(cand["key"].casefold())
            candidates.append(cand)
    return candidates


def _extra_candidate(path: str, volumes: list[dict], own_host: str,
                     pathmap: PathMap | None) -> dict | None:
    unc = split_unc(path)
    if unc is not None:
        host, share, rest = unc
        if not share:
            return None
        return _share_candidate(host, share, [s for s in rest.split("\\") if s], manual=True)
    if not is_drive_path(path):
        return None
    best: tuple[dict, str, list[str]] | None = None
    for vol in volumes:
        root = clean_path(str(vol.get("root") or f"{vol.get('drive', '')}\\"))
        rel = relative_parts(path, root)
        if rel is not None and (best is None or len(rel) < len(best[2])):
            best = (vol, root, rel)
    if best is None:
        return None
    vol, root, rel = best
    return _local_candidate(vol, root, rel, own_host, pathmap, manual=True)
