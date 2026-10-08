"""Where did the offline clips go? (SPEC §22.2) - pure planning, no I/O.

The bridge collects the clips DaVinci Resolve reports offline (the helper's ``offline`` command)
and asks the index for files of the same names (``Indexer.find_files``). This module groups the
clips by the folder they used to be in and ranks the indexed folders that hold their files:

(a) how many of the group's file names a folder holds, (b) how many trailing path parts it shares
with the old folder (``…\\Rikke Lindholm\\Klip\\FX9``), (c) whether it lies in a project of the
same name, (d) a UNC path when the old path was UNC (Resolve keeps local media as UNC paths of
this computer), (e) online and not on a hot-plug disk.

A group is ``auto`` (ticked in the UI) only when the best folder holds every name, is online and
wins alone - an equal runner-up makes it a choice between up to five ``alternatives``. The old
folder itself (the same unplugged disk) can be a candidate too; its ``to`` is then the group's
``from``, which the UI shows as "samme mappe".
"""

from __future__ import annotations

import ntpath
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

MAX_ALTERNATIVES = 5
_MIN_PROJECT_PREFIX = 3        # a Resolve project "Rikke Lindholm - Testimonial" ~ folder "Rikke Lindholm"


def _backslashes(path: str) -> str:
    return path.replace("/", "\\")


def _norm(path: str) -> str:
    """Comparable form of a folder path: backslashes, no trailing separator, casefolded."""
    return _backslashes(path).rstrip("\\").casefold()


def _parts(path: str) -> list[str]:
    """The casefolded folder names after the drive or ``\\\\host\\share``."""
    rest = ntpath.splitdrive(_backslashes(path))[1]
    return [p.casefold() for p in rest.split("\\") if p]


def _shared_tail(a: list[str], b: list[str]) -> int:
    n = 0
    for x, y in zip(reversed(a), reversed(b)):
        if x != y:
            break
        n += 1
    return n


def _is_unc(path: str) -> bool:
    return path.startswith("\\\\") or path.startswith("//")


def file_name(path: str) -> str:
    """The file name of a clip path (Resolve paths use backslashes; tolerate slashes)."""
    return ntpath.basename(_backslashes(path))


@dataclass
class _Candidate:
    folder: str | None                  # live path of the folder (drive letter or UNC)
    unc: str | None                     # the same folder through the location's share
    online: bool
    hotplug: bool
    project: str | None
    names: set[str] = field(default_factory=set)
    newest: float = 0.0                 # newest file of the group there (orders equal folders)

    def target(self, old: str) -> tuple[str, bool]:
        """(path to relink to, is it the old folder itself) - in the old path's spelling when
        it is the old folder, else UNC when the old path was UNC and the location has one."""
        old_norm = _norm(old)
        for path in (self.folder, self.unc):
            if path and _norm(path) == old_norm:
                return old, True
        if _is_unc(old) and self.unc:
            return self.unc, False
        return (self.folder or self.unc or ""), False


def _same_project(project: str | None, old_parts: list[str], resolve_project: str | None) -> bool:
    if not project:
        return False
    name = project.casefold()
    if name in old_parts:
        return True
    if not resolve_project or len(name) < _MIN_PROJECT_PREFIX:
        return False
    wanted = resolve_project.casefold()
    return wanted == name or wanted.startswith(name + " ")


def _clip_out(clip: Mapping[str, Any]) -> dict[str, Any]:
    path = clip["path"]
    return {"uid": clip["uid"], "name": clip.get("name") or file_name(path), "old_path": path}


def make_plan(clips: Iterable[Mapping[str, Any]], files: Iterable[Mapping[str, Any]], *,
              sources: Iterable[Mapping[str, Any]] | None = None,
              project: str | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(groups, not_found)`` for the offline ``clips`` (``{uid, name, path, …}`` from the
    helper) and the index's ``files`` of their names (``Indexer.find_files`` rows: ``name``,
    ``folder``, ``unc_folder``, ``online``, ``source_id``, ``project``, ``mtime`` …).

    ``sources`` (``Indexer.list_sources()``) tell hot-plug disks apart; ``project`` is the
    Resolve project's name. Groups: ``{from, to, to_display, online, clips: [{uid, name,
    old_path}], auto, alternatives: [{to, to_display, online, holds}]}``, most clips first;
    clips whose file the index holds nowhere are ``not_found`` (``{uid, name, old_path}``).
    """
    hotplug = {s.get("id"): bool(s.get("hotplug")) for s in sources or () if isinstance(s, Mapping)}
    by_name: dict[str, list[Mapping[str, Any]]] = {}
    for row in files:
        if isinstance(row, Mapping) and isinstance(row.get("name"), str):
            by_name.setdefault(row["name"].casefold(), []).append(row)

    old_folders: dict[str, tuple[str, list[Mapping[str, Any]]]] = {}
    for clip in clips:
        uid, path = clip.get("uid"), clip.get("path")
        if not (isinstance(uid, str) and uid and isinstance(path, str) and path.strip()):
            continue
        old = ntpath.dirname(_backslashes(path))
        old_folders.setdefault(_norm(old), (old, []))[1].append(clip)

    groups: list[dict[str, Any]] = []
    not_found: list[dict[str, Any]] = []
    for old, members in old_folders.values():
        candidates: dict[tuple[Any, str], _Candidate] = {}
        for name in {file_name(c["path"]).casefold() for c in members}:
            for row in by_name.get(name, ()):
                folder = row.get("folder") if isinstance(row.get("folder"), str) else None
                unc = row.get("unc_folder") if isinstance(row.get("unc_folder"), str) else None
                if not folder and not unc:
                    continue
                key = (row.get("source_id"), _norm(folder or unc or ""))
                cand = candidates.get(key)
                if cand is None:
                    ref = row.get("project")
                    cand = candidates[key] = _Candidate(
                        folder, unc, bool(row.get("online")), hotplug.get(row.get("source_id"), False),
                        ref.get("name") if isinstance(ref, Mapping) else None)
                cand.names.add(name)
                mtime = row.get("mtime")
                if isinstance(mtime, (int, float)) and not isinstance(mtime, bool):
                    cand.newest = max(cand.newest, float(mtime))
        held = set().union(*(c.names for c in candidates.values())) if candidates else set()
        found = [c for c in members if file_name(c["path"]).casefold() in held]
        not_found += [_clip_out(c) for c in members if file_name(c["path"]).casefold() not in held]
        if not found:
            continue
        old_parts = _parts(old)
        ranked = []
        for cand in candidates.values():
            to, same = cand.target(old)
            key = (len(cand.names), _shared_tail(old_parts, _parts(to)),
                   _same_project(cand.project, old_parts, project),
                   _is_unc(old) and _is_unc(to), cand.online and not cand.hotplug, cand.online)
            display = old if same else (cand.folder or to)
            ranked.append((key, cand.newest, to.casefold(), to, display, cand))
        ranked.sort(key=lambda r: (r[0], r[1]), reverse=True)
        unique: dict[str, tuple] = {}
        for entry in ranked:                     # one entry per folder, the best of them
            unique.setdefault(entry[2], entry)
        ranked = list(unique.values())
        best_key, _newest, _fold, to, display, best = ranked[0]
        alone = len(ranked) == 1 or ranked[1][0] < best_key
        auto = alone and best.online and len(best.names) == len(held)
        alternatives = [] if auto else [
            {"to": alt_to, "to_display": alt_display, "online": cand.online, "holds": len(cand.names)}
            for _key, _n, _fold, alt_to, alt_display, cand in ranked[:MAX_ALTERNATIVES]]
        groups.append({"from": old, "to": to, "to_display": display, "online": best.online,
                       "clips": [_clip_out(c) for c in found], "auto": auto,
                       "alternatives": alternatives})
    groups.sort(key=lambda g: (-len(g["clips"]), g["from"].casefold()))
    not_found.sort(key=lambda c: c["old_path"].casefold())
    return groups, not_found
