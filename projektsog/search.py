"""Search, recent projects and folder listings over the index (SPEC §7).

Runs in the main process on read-only connections (one snapshot per call).  CPU work is bounded:
at most :data:`CANDIDATE_CAP` candidate rows are fetched and scored per query; candidates carry
only the columns ranking needs, full rows are read for the results that are returned.

Matching rules: every query token must occur in the entry's folded name, in its folded ancestor
path within the source, or in the folded source display name / volume label; at least one token
must occur in the entry's own name.  "Occur" is :func:`token_matches` (SPEC §15.2): a token
``t`` matches a folded text ``n`` when ``t in n`` or ``alt(t) in alt(n)``, so the ASCII
spelling "oe" of "ø" finds both spellings ("boegely" ~ "Bøgely", "infomøde" ~ "Infomoede") –
but only when ``alt(t)`` has ≥ 3 characters ("joe", "zoe", "koe" match literally only), and a
token found only through its "oe" spelling ranks below the real spelling (SPEC §15.12).
Retrieval (every candidate is re-checked against the rules):

* the most selective token with ≥ 3 characters drives an FTS trigram ``MATCH`` (``"t"`` or
  ``"t" OR "alt(t)"`` over ``name_fold`` and ``name_alt``); tokens that a source name satisfies
  are only chosen when every long token is such a token;
* descendants of matched directories whose names contain another token are added (``lindholm
  klip`` → ``Rikke Lindholm\\Klip``) by rel-path range scans, or – when the subtrees are huge – by FTS
  on the other tokens restricted to those subtrees;
* when the driver token matches source names, entries of those sources whose names contain
  another token are added (``forar pixelbro`` → ``Pixelbro`` on "Forår 2026 RØD");
* queries with only short tokens (< 3 characters) use one sequential substring scan.

A source whose root folder is itself a project (``root_is_project`` in the registry, SPEC §15.3)
contributes a synthetic project item for its root (id ``-source_id``, rel path ``""``, name =
display name) that is matched, filtered and ranked like an entry; it is the project of the
source's entries.

When more rows match than the cap allows, directories are kept before files (``truncated``).
"""

from __future__ import annotations

import heapq
import ntpath
import re
import sqlite3
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import Any

from . import config, db, textutil
from .db import KIND_DIR, KIND_FILE, KIND_GROUP, KIND_PROJECT, KIND_TEMPLATE, KIND_TOPLEVEL
from .scanner import sequence_first_frame

KIND_NAMES: dict[int, str] = {KIND_FILE: "file", KIND_DIR: "dir", KIND_PROJECT: "project",
                              KIND_GROUP: "group", KIND_TEMPLATE: "template",
                              KIND_TOPLEVEL: "toplevel"}
KIND_FILTERS: dict[str, frozenset[int] | None] = {
    "all": None,
    "project": frozenset({KIND_PROJECT, KIND_GROUP, KIND_TOPLEVEL}),
    "dir": frozenset({KIND_DIR, KIND_PROJECT, KIND_GROUP, KIND_TOPLEVEL}),
    "file": frozenset({KIND_FILE}),
}
_BASE_SCORE = {KIND_PROJECT: 1000, KIND_GROUP: 800, KIND_TOPLEVEL: 700, KIND_DIR: 400,
               KIND_TEMPLATE: 400, KIND_FILE: 100}
# search() "hidden": per filter the matches only it hides; "any" (alias "all") = hidden by any
_NO_HIDDEN = {"kind": 0, "offline": 0, "source": 0, "any": 0, "all": 0}

CANDIDATE_CAP = 30_000
# Estimated subtree rows above which descendants are found via FTS instead of range scans.
EXPANSION_ROW_BUDGET = 150_000
SUBFOLDER_LIMIT = 40
SYSTEM_DISK_NAME = "Systemdisk"          # disk_name of an unlabeled system volume (SPEC §15.4)
# SPEC §15.12: a token's "oe" spelling (alt) is only used when it has this many characters
# ("joe" ~ "jo" would match almost everything), and a token that matches a name only through
# it ranks below the real spelling: no exact/prefix/word-start bonus and this penalty.
ALT_MIN_CHARS = 3
ALT_ONLY_PENALTY = 60
_ROOTS_PER_QUERY = 200
_DAY = 86_400.0
# Search runs without the settings, so a template-named root project (a shared or added copy
# of "1. KUNDENAVN") is recognised with the default template regex.
_TEMPLATE_RE = re.compile(config.DEFAULTS["template_folder_regex"])

# Candidate rows: the columns ranking needs (fetching all 16 costs ~60 % more at 30k rows).
_CAND_FIELDS = ("id", "source_id", "rel_path", "parent_rel", "name_fold", "name_alt", "kind",
                "depth", "mtime", "file_count")
(_ID, _SID, _REL, _PARENT, _FOLD, _ALT, _KIND, _DEPTH, _MTIME,
 _FILE_COUNT) = range(len(_CAND_FIELDS))
_E_COLUMNS = ", ".join("e." + name for name in _CAND_FIELDS)
_FTS_SQL = (f"SELECT {_E_COLUMNS} FROM entries_fts JOIN entries e ON e.id = entries_fts.rowid "
            "WHERE entries_fts MATCH ?")
# Full rows (db.ROW_FIELDS order) for items.
_R_ID, _R_SID, _R_REL, _R_KIND, _R_MTIME = (
    db.ROW_FIELDS.index(n) for n in ("id", "source_id", "rel_path", "kind", "mtime"))

Row = tuple[Any, ...]


# --------------------------------------------------------------------------------------
# Item shapes (SPEC §7.1)
# --------------------------------------------------------------------------------------

def drive_of(path: str | None) -> str | None:
    """``"H:"`` when ``path`` starts with a drive letter, else None."""
    if path and len(path) >= 2 and path[1] == ":" and path[0].isalpha():
        return path[:2].upper()
    return None


def format_size(size: int) -> str:
    """Danish decimal size, e.g. ``2 TB``, ``1,5 TB``, ``500 GB``."""
    units = ("B", "kB", "MB", "GB", "TB", "PB")
    value = float(size)
    i = 0
    while value >= 1000 and i < len(units) - 1:
        value /= 1000
        i += 1
    text = f"{value:.1f}" if value < 10 and i > 0 else f"{value:.0f}"
    return f"{text.replace('.', ',').removesuffix(',0')} {units[i]}"


def disk_name(source: Mapping[str, Any]) -> str | None:
    """SourceRef/Source ``disk_name`` (local sources only); ``"Systemdisk"`` for an unlabeled
    system volume (``is_system``)."""
    if source.get("kind") != "local":
        return None
    label = (source.get("volume_label") or "").strip()
    if label:
        return label
    if source.get("is_system"):
        return SYSTEM_DISK_NAME
    parts = []
    if source.get("volume_size"):
        parts.append(format_size(int(source["volume_size"])))
    last_drive = source.get("last_drive") or drive_of(source.get("path"))
    if last_drive:
        parts.append(f"sidst som {last_drive}")
    return "disk uden navn" + (f" ({', '.join(parts)})" if parts else "")


def source_ref(source: Mapping[str, Any]) -> dict[str, Any]:
    """SourceRef (SPEC §7.1, + ``is_system`` §15.4, + ``volume_present`` §15.12) of a registry
    Source; a registry without ``volume_present`` counts the disk as present when online."""
    online = bool(source.get("online"))
    return {
        "id": source.get("id"),
        "name": source.get("display_name"),
        "host": source.get("host"),
        "kind": source.get("kind"),
        "online": online,
        "drive": drive_of(source.get("path")),
        "disk_name": disk_name(source),
        "volume_label": source.get("volume_label"),
        "last_seen": source.get("last_seen"),
        "is_system": bool(source.get("is_system")),
        "volume_present": bool(source.get("volume_present", online)),
    }


def _join(base: str, rel: str) -> str:
    return ntpath.join(base, rel) if rel else base


def token_alt(token: str) -> str | None:
    """The ``alt()`` form a folded query token also matches through (SPEC §15.2), or None
    when its "oe" spelling is not used: shorter than :data:`ALT_MIN_CHARS` (§15.12)."""
    alt = textutil.alt(token)
    return alt if len(alt) >= ALT_MIN_CHARS else None


def token_matches(token: str, text: str, text_alt: str | None = None) -> bool:
    """:func:`textutil.token_matches` with the §15.12 rule for short "oe" spellings:
    ``token in text`` or ``token_alt(token) in alt(text)`` (``text_alt`` when given)."""
    if token in text:
        return True
    alt = token_alt(token)
    return alt is not None and alt in (textutil.alt(text) if text_alt is None else text_alt)


def _highlight(name: str, tokens: Sequence[str]) -> list[list[int]]:
    """Highlight ranges of ``tokens`` in ``name``.  :func:`textutil.highlight_ranges` falls
    back to a token's "oe" spelling, so tokens without one (§15.12) are left out unless the
    name contains them literally."""
    folded = textutil.fold(name)
    usable = [t for t in tokens if t in folded or token_alt(t) is not None]
    return textutil.highlight_ranges(name, usable) if usable else []


def root_kind(source: Mapping[str, Any]) -> int | None:
    """``KIND_PROJECT`` when the source's root folder is itself a project (``root_is_project``,
    SPEC §15.3), ``KIND_TEMPLATE`` when that root is a project template, else None."""
    if not source.get("root_is_project"):
        return None
    name_fold = textutil.fold(source.get("display_name") or "")
    return KIND_TEMPLATE if _TEMPLATE_RE.search(name_fold) else KIND_PROJECT


def root_project_ref(source: Mapping[str, Any]) -> dict[str, Any]:
    """ProjectRef of a source whose root is a project (rel path ``""``)."""
    unc_base = source.get("unc_path") or None
    return {"name": source.get("display_name"), "rel_path": "",
            "path": source.get("path") or "", "unc_path": unc_base}


def make_item(row: Mapping[str, Any], source: Mapping[str, Any],
              tokens: Sequence[str] | None = None, *,
              subfolders: Iterable[str] | None = None,
              score: float | None = None) -> dict[str, Any]:
    """Item (SPEC §7.1) for an entry row (mapping with :data:`db.ROW_FIELDS` keys).

    ``tokens`` → highlight ranges (``[]`` without); ``subfolders`` is only kept for directories.
    In a source whose root is a project, entries outside any inner project get the root as their
    project, and its top-level folders are plain ``dir`` items.
    """
    name = row["name"]
    rel = row["rel_path"]
    kind = row["kind"]
    base = source.get("path") or ""
    unc_base = source.get("unc_path") or None
    path = _join(base, rel)
    open_path = path
    if row["is_seq"]:
        frame = sequence_first_frame(name)
        if frame is not None:
            open_path = _join(base, _join(row["parent_rel"], frame))
    project_rel = row["project_rel"]
    project = None
    root_is_project = root_kind(source) == KIND_PROJECT
    if project_rel is not None:
        project = {"name": project_rel.rpartition("\\")[2], "rel_path": project_rel,
                   "path": _join(base, project_rel),
                   "unc_path": _join(unc_base, project_rel) if unc_base else None}
    elif root_is_project:
        project = root_project_ref(source)
    if root_is_project and kind == KIND_TOPLEVEL:
        kind = KIND_DIR
    return {
        "id": row["id"],
        "kind": KIND_NAMES.get(kind, "file"),
        "name": name,
        "hl": _highlight(name, tokens) if tokens else [],
        "path": path,
        "open_path": open_path,
        "unc_path": _join(unc_base, rel) if unc_base else None,
        "rel_path": rel,
        "parent": row["parent_rel"],
        "depth": row["depth"],
        "source": source_ref(source),
        "project": project,
        "size": row["size"],
        "mtime": row["mtime"],
        "file_count": row["file_count"],
        "ext": row["ext"],
        "is_seq": bool(row["is_seq"]),
        "seq_count": row["seq_count"],
        "subfolders": list(subfolders) if kind != KIND_FILE and subfolders is not None else None,
        "score": score,
    }


def root_item(conn: sqlite3.Connection, source: Mapping[str, Any],
              tokens: Sequence[str] | None = None, *, mtime: float | None = None,
              score: float | None = None) -> dict[str, Any]:
    """Item for the root of a source that is itself a project (SPEC §15.3): id ``-source_id``,
    rel path ``""``, depth 0; size and file count are the source's (after its first deep
    scan), ``mtime`` the newest in the source (:func:`_root_mtime`)."""
    sid = int(source["id"])
    kind = root_kind(source) or KIND_PROJECT
    name = source.get("display_name") or ""
    base = source.get("path") or ""
    scanned = source.get("last_scan_end") is not None
    return {
        "id": -sid,
        "kind": KIND_NAMES[kind],
        "name": name,
        "hl": _highlight(name, tokens) if tokens else [],
        "path": base,
        "open_path": base,
        "unc_path": source.get("unc_path") or None,
        "rel_path": "",
        "parent": "",
        "depth": 0,
        "source": source_ref(source),
        "project": root_project_ref(source) if kind == KIND_PROJECT else None,
        "size": source.get("total_size") if scanned else None,
        "mtime": mtime,
        "file_count": source.get("file_count") if scanned else None,
        "ext": None,
        "is_seq": False,
        "seq_count": None,
        "subfolders": db.child_dir_names(conn, sid, "", SUBFOLDER_LIMIT),
        "score": score,
    }


def _root_mtime(conn: sqlite3.Connection, source_id: int) -> float | None:
    """Newest mtime in a source (its top-level entries carry their subtree's newest)."""
    return conn.execute("SELECT max(mtime) FROM entries WHERE source_id = ? AND parent_rel = ''",
                        (source_id,)).fetchone()[0]


def _item(conn: sqlite3.Connection, row: Row, source: Mapping[str, Any],
          tokens: Sequence[str] | None = None, score: float | None = None) -> dict[str, Any]:
    """Item for a full row (db.ROW_FIELDS order), with its sub-folder names."""
    subfolders = (db.child_dir_names(conn, row[_R_SID], row[_R_REL], SUBFOLDER_LIMIT)
                  if row[_R_KIND] != KIND_FILE else None)
    return make_item(dict(zip(db.ROW_FIELDS, row)), source, tokens, subfolders=subfolders,
                     score=score)


def _live_sources(sources: Mapping[int, Mapping[str, Any]]) -> dict[int, Mapping[str, Any]]:
    """Registry sources whose entries may be shown (unknown/excluded sources are hidden)."""
    return {sid: src for sid, src in sources.items() if src.get("included", True)}


def _template_prefixes(conn: sqlite3.Connection,
                       live: Mapping[int, Mapping[str, Any]]) -> dict[int, tuple[str, ...]]:
    """Template subtrees to hide; a source whose root is a template is hidden entirely."""
    templates = db.template_prefixes(conn)
    for sid, src in live.items():
        if root_kind(src) == KIND_TEMPLATE:
            templates[sid] = ("",)
    return templates


def _under_template(source_id: int, rel_path: str,
                    templates: Mapping[int, tuple[str, ...]]) -> bool:
    prefixes = templates.get(source_id)
    return bool(prefixes) and rel_path.startswith(prefixes)


# --------------------------------------------------------------------------------------
# Retrieval
# --------------------------------------------------------------------------------------

class _Candidates:
    """Candidate rows by id, bounded by :data:`CANDIDATE_CAP`."""

    def __init__(self) -> None:
        self.rows: dict[int, Row] = {}
        self.left = CANDIDATE_CAP
        self.truncated = False

    @property
    def limit(self) -> int:          # one extra row reveals an overflow
        return self.left + 1

    def add(self, fetched: list[Row]) -> list[Row]:
        """Keep rows up to the cap; returns the rows that fit."""
        if len(fetched) > self.left:
            self.truncated = True
            fetched = fetched[:self.left]
        rows = self.rows
        for row in fetched:
            if row[_ID] not in rows:
                rows[row[_ID]] = row
                self.left -= 1
        return fetched


def _phrase(token: str) -> str:
    return '"' + token.replace('"', '""') + '"'


def _fts_ok(token: str) -> bool:
    """Whether FTS trigrams can find every name ``token`` matches: the token has ≥ 3
    characters, and so has its "oe" spelling whenever that is used (:func:`token_alt`)."""
    return len(token) >= 3


def _fts_query(token: str) -> str:
    """FTS5 query for one folded token: ``"t"`` or ``"t" OR "alt(t)"`` (both columns)."""
    alt = token_alt(token)
    return _phrase(token) if alt in (None, token) else f"{_phrase(token)} OR {_phrase(alt)}"


def _match_sql(tokens: Sequence[str]) -> tuple[str, list[str]]:
    """SQL condition "the name matches any of ``tokens``" (SPEC §15.2, §15.12) and its
    parameters.

    ``alt(name_fold)`` is ``name_alt``, or ``name_fold`` itself where that is NULL.  For a
    token without "oe" only names containing "oe" can match through ``alt``: those are found
    with ``replace()`` (= :func:`textutil.alt`) after a cheap ``instr`` – reading ``name_alt``,
    the last column, for every row of a full scan costs ~10 % more.  A token whose "oe"
    spelling is too short (:func:`token_alt`) matches literally only.
    """
    parts: list[str] = []
    params: list[str] = []
    for token in tokens:
        alt = token_alt(token)
        if alt is None:
            parts.append("instr(e.name_fold, ?) > 0")
            params.append(token)
            continue
        if alt == token:
            parts.append("instr(e.name_fold, ?) > 0 OR (instr(e.name_fold, 'oe') > 0 "
                         "AND instr(replace(e.name_fold, 'oe', 'o'), ?) > 0)")
        else:
            parts.append("instr(e.name_fold, ?) > 0 "
                         "OR instr(coalesce(e.name_alt, e.name_fold), ?) > 0")
        params += (token, alt)
    return "(" + " OR ".join(parts) + ")", params


def _fts_count(conn: sqlite3.Connection, token: str) -> int:
    return conn.execute("SELECT count(*) FROM entries_fts WHERE entries_fts MATCH ?",
                        (_fts_query(token),)).fetchone()[0]


def _fetch_fts(conn: sqlite3.Connection, token: str, count: int, cands: _Candidates,
               where: str = "", params: Sequence[Any] = ()) -> list[Row]:
    """Rows whose name matches ``token`` (FTS-able); directories first when over the cap.

    ``count`` is the token's FTS count (all sources).  Returns the rows that were kept.
    """
    if count <= 0:
        return []
    if cands.left <= 0:
        cands.truncated = True
        return []
    query = _fts_query(token)
    if count <= cands.left:
        return cands.add(conn.execute(_FTS_SQL + where + " LIMIT ?",
                                      (query, *params, cands.limit)).fetchall())
    rows = cands.add(conn.execute(_FTS_SQL + " AND e.kind >= 1" + where + " LIMIT ?",
                                  (query, *params, cands.limit)).fetchall())
    if cands.left <= 0:
        cands.truncated = True
        return rows
    return rows + cands.add(conn.execute(_FTS_SQL + " AND e.kind = 0" + where + " LIMIT ?",
                                         (query, *params, cands.limit)).fetchall())


def _fetch_substring(conn: sqlite3.Connection, tokens: Sequence[str], cands: _Candidates,
                     where: str = "", params: Sequence[Any] = ()) -> None:
    """Rows whose name matches any of ``tokens``; on overflow all directories are kept.

    Without a source filter the table is scanned sequentially: walking ``ix_entries_kind``
    instead costs a random row lookup per entry (measured 15x slower at 500k rows).
    """
    if cands.left <= 0:
        cands.truncated = True
        return
    cond, cond_params = _match_sql(tokens)
    table = "entries e" if where else "entries e NOT INDEXED"
    sql = f"SELECT {_E_COLUMNS} FROM {table} WHERE "
    rows = conn.execute(sql + cond + where + " LIMIT ?",
                        (*cond_params, *params, cands.limit)).fetchall()
    if len(rows) > cands.left:
        cands.add(conn.execute(sql + "e.kind >= 1 AND " + cond + where + " LIMIT ?",
                               (*cond_params, *params, cands.limit)).fetchall())
        rows = [row for row in rows if row[_KIND] == KIND_FILE]
        cands.truncated = True
    cands.add(rows)


def _outermost_dirs(rows: Iterable[Row]) -> list[Row]:
    """Directories among ``rows`` that are not inside another of them (same source)."""
    dirs = sorted((r for r in rows if r[_KIND] != KIND_FILE), key=lambda r: (r[_SID], r[_REL]))
    kept: dict[int, set[str]] = {}
    out = []
    for row in dirs:
        seen = kept.setdefault(row[_SID], set())
        parts = row[_REL].split("\\")
        if any("\\".join(parts[:i]) in seen for i in range(1, len(parts))):
            continue
        seen.add(row[_REL])
        out.append(row)
    return out


def _expand_descendants(conn: sqlite3.Connection, driver_rows: list[Row],
                        others: Sequence[str], counts: Mapping[str, int],
                        cands: _Candidates) -> None:
    """Add descendants of matched directories whose names contain another token.

    Cost model: range scans read about ``file_count`` rows per subtree (+ a per-root
    overhead); the alternative reads every FTS match of the other tokens (all FTS-able).
    """
    roots = _outermost_dirs(driver_rows)
    if not roots:
        return
    range_cost = sum((r[_FILE_COUNT] or 0) + 1 for r in roots) + 25 * len(roots)
    use_fts = False
    if all(t in counts for t in others):
        fts_cost = 2 * sum(counts[t] for t in others)
        use_fts = range_cost > EXPANSION_ROW_BUDGET or (
            fts_cost < range_cost and all(counts[t] <= CANDIDATE_CAP for t in others))
    if use_fts:
        under: dict[int, set[str]] = {}
        for r in roots:
            under.setdefault(r[_SID], set()).add(r[_REL])
        for token in others:
            probe = _Candidates()
            for row in _fetch_fts(conn, token, counts[token], probe):
                parts = row[_REL].split("\\")
                inside = under.get(row[_SID], ())
                if inside and any("\\".join(parts[:i]) in inside for i in range(1, len(parts))):
                    cands.add([row])
            cands.truncated |= probe.truncated
        return
    cond, cond_params = _match_sql(others)
    for i in range(0, len(roots), _ROOTS_PER_QUERY):
        if cands.left <= 0:
            cands.truncated = True
            return
        chunk = roots[i:i + _ROOTS_PER_QUERY]
        params: list[Any] = []
        for root in chunk:
            params.extend((root[_SID], *db.descendant_bounds(root[_REL])))
        # One statement per chunk; each VALUES row is served by an index range scan.
        sql = (f"WITH roots(sid, lo, hi) AS (VALUES {', '.join(['(?, ?, ?)'] * len(chunk))}) "
               f"SELECT {_E_COLUMNS} FROM roots JOIN entries e ON e.source_id = roots.sid "
               f"AND e.rel_path >= roots.lo AND e.rel_path < roots.hi WHERE {cond} LIMIT ?")
        cands.add(conn.execute(sql, (*params, *cond_params, cands.limit)).fetchall())


def _retrieve(conn: sqlite3.Connection, tokens: list[str],
              source_hits: Mapping[str, frozenset[int]],
              source_id: int | None) -> _Candidates:
    cands = _Candidates()
    where, params = ("", ()) if source_id is None else (" AND e.source_id = ?", (source_id,))
    long_tokens = [t for t in tokens if _fts_ok(t)]
    if not long_tokens:
        _fetch_substring(conn, tokens, cands, where, params)
        return cands
    counts = {t: _fts_count(conn, t) for t in long_tokens}
    name_only = [t for t in long_tokens if not source_hits[t]]
    driver = min(name_only or long_tokens, key=counts.__getitem__)
    driver_rows = _fetch_fts(conn, driver, counts[driver], cands, where, params)
    others = [t for t in tokens if t != driver]
    if not others:
        return cands
    _expand_descendants(conn, driver_rows, others, counts, cands)
    source_ids = source_hits[driver]
    if source_id is not None:
        source_ids = source_ids & {source_id}
    if source_ids:
        # The driver may be satisfied by the source name alone: add that source's entries
        # whose names contain another token.
        ids = sorted(source_ids)
        in_sql = f" AND e.source_id IN ({', '.join('?' * len(ids))})"
        for token in others:
            if token in counts:
                _fetch_fts(conn, token, counts[token], cands, in_sql, ids)
        short = [t for t in others if t not in counts]
        if short:
            _fetch_substring(conn, short, cands, in_sql, ids)
    return cands


def _root_rows(conn: sqlite3.Connection, live: Mapping[int, Mapping[str, Any]],
               tokens: Sequence[str]) -> list[Row]:
    """Candidate rows for the roots of sources that are projects (SPEC §15.3), when a token
    matches the root's name (the display name); the ranker applies the full rules."""
    rows = []
    for sid, src in live.items():
        kind = root_kind(src)
        if kind is None:
            continue
        name_fold = textutil.fold(src.get("display_name") or "")
        if any(token_matches(t, name_fold) for t in tokens):
            rows.append((-sid, sid, "", "", name_fold, db.name_alt(name_fold), kind, 0,
                         _root_mtime(conn, sid), src.get("file_count")))
    return rows


def _full_rows(conn: sqlite3.Connection, ids: Sequence[int]) -> dict[int, Row]:
    if not ids:
        return {}
    rows = conn.execute(f"SELECT {db.ROW_COLUMNS} FROM entries "
                        f"WHERE id IN ({', '.join('?' * len(ids))})", list(ids)).fetchall()
    return {row[_R_ID]: row for row in rows}


# --------------------------------------------------------------------------------------
# Ranking
# --------------------------------------------------------------------------------------

class _Ranker:
    """Applies the matching rules, the filters and the SPEC §7 ranking to candidate rows."""

    def __init__(self, tokens: list[str], query_fold: str,
                 live: Mapping[int, Mapping[str, Any]], source_folds: Mapping[int, str],
                 templates: Mapping[int, tuple[str, ...]] | None) -> None:
        self.tokens = tokens
        # (token, its "oe" spelling or None when that is not used, §15.12)
        self.pairs = [(t, token_alt(t)) for t in tokens]
        self.alt_of = dict(self.pairs)
        self.any_alt = any(a is not None and a != t for t, a in self.pairs)
        self.query_fold = query_fold
        self.online = {sid: bool(src.get("online")) for sid, src in live.items()}
        self.root_projects = frozenset(sid for sid, src in live.items()
                                       if root_kind(src) == KIND_PROJECT)
        self.source_folds = source_folds
        self.templates = templates            # None: templates are shown
        self._paths: dict[tuple[int, str], tuple[str, str]] = {}

    def kind_of(self, row: Row) -> int:
        """The kind shown: top-level folders of a root project are plain folders."""
        kind = row[_KIND]
        if kind == KIND_TOPLEVEL and row[_SID] in self.root_projects:
            return KIND_DIR
        return kind

    def _check(self, row: Row) -> tuple[list[str], int] | None:
        """``(tokens not in the entry's own name, number of tokens found only through their
        "oe" spelling)``, or None if the row does not match."""
        name_fold, name_alt = row[_FOLD], row[_ALT]
        alt_only = 0
        if name_alt is None and not self.any_alt:         # one spelling: a plain substring test
            missing = [t for t in self.tokens if t not in name_fold]
        else:
            name_alt = name_alt or name_fold
            missing = []
            for t, a in self.pairs:
                if t in name_fold:
                    continue
                if a is not None and a in name_alt:
                    alt_only += 1
                else:
                    missing.append(t)
        if not missing:
            return missing, alt_only
        if len(missing) == len(self.tokens):
            return None                                   # no token in the own name
        key = (row[_SID], row[_PARENT])
        path = self._paths.get(key)
        if path is None:
            text = textutil.fold(row[_PARENT]) + " " + self.source_folds[row[_SID]]
            text_alt = textutil.alt(text)
            path = self._paths[key] = (text, text_alt if text_alt != text else "")
        text, text_alt = path
        if not text_alt and not self.any_alt:
            return (missing, alt_only) if all(t in text for t in missing) else None
        for t in missing:
            if t not in text:
                a = self.alt_of[t]
                if a is None or a not in (text_alt or text):
                    return None
                alt_only += 1
        return missing, alt_only

    def matches(self, rows: Iterable[Row]) -> Iterator[tuple[Row, list[str], int]]:
        """``(row, tokens matched via the path, tokens matched only via "oe")`` per match."""
        online, templates = self.online, self.templates
        for row in rows:
            if row[_SID] not in online:
                continue
            if templates is not None and (row[_KIND] == KIND_TEMPLATE
                                          or _under_template(row[_SID], row[_REL], templates)):
                continue
            checked = self._check(row)
            if checked is not None:
                yield row, *checked

    def rank(self, rows: Iterable[Row], kinds: frozenset[int] | None, online_only: bool,
             source_id: int | None, limit: int) -> tuple[list[tuple[float, Row]], int]:
        """``(top candidates by score then mtime, total matches after the filters)``.

        The name bonuses (exact, starts with the first token, word starts) count the real
        spelling only: a token found only through its "oe" spelling earns none of them and
        costs :data:`ALT_ONLY_PENALTY`, so "shoe" ranks "Shoemaker" above "Aftenshowet"
        and "boegely" ranks "Boegely Havn" above "Bøgely Jul" (SPEC §15.12).
        """
        now = time.time()
        first, query_fold = self.tokens[0], self.query_fold
        word_starts = [(t, " " + t) for t in self.tokens]
        online_of = self.online
        heap: list[tuple[float, float, int, Row]] = []
        total = 0
        for row, missing, alt_only in self.matches(rows):
            kind = self.kind_of(row)
            online = online_of[row[_SID]]
            if ((kinds is not None and kind not in kinds) or (online_only and not online)
                    or (source_id is not None and row[_SID] != source_id)):
                continue
            total += 1
            name_fold = row[_FOLD]
            score = (_BASE_SCORE.get(kind, 100) - 4 * row[_DEPTH] - 120 * len(missing)
                     - ALT_ONLY_PENALTY * alt_only)
            if not missing:
                score += 300
            if name_fold == query_fold:
                score += 250
            if name_fold.startswith(first):
                score += 100
            for t, spaced in word_starts:
                if spaced in name_fold or name_fold.startswith(t):
                    score += 40
            if online:
                score += 200
            mtime = row[_MTIME]
            if mtime is not None:
                age = now - mtime
                score += 80 if age < 30 * _DAY else 40 if age < 180 * _DAY else (
                    15 if age < 365 * _DAY else 0)
            item = (float(score), mtime or 0.0, -row[_ID], row)   # ties at the cut: oldest id
            if len(heap) < limit:
                heapq.heappush(heap, item)
            elif limit and item > heap[0]:
                heapq.heapreplace(heap, item)
        heap.sort(key=lambda it: (-it[0], -it[1], it[3][_FOLD], -it[2]))  # exact ties: by name
        return [(score, row) for score, _mtime, _id, row in heap], total

    def hidden(self, rows: Iterable[Row], kinds: frozenset[int] | None, online_only: bool,
               source_id: int | None) -> dict[str, int]:
        """What each active filter hides *on its own* (IDX-7): the matches that fail only that
        filter, i.e. exactly what switching just that filter off reveals.  ``any`` = matches
        hidden by at least one filter (more than the sum when some fail several filters, so
        only clearing all filters shows them); ``all`` is the same number (alias)."""
        out = dict(_NO_HIDDEN)
        for row, _missing, _alt_only in self.matches(rows):
            failed = [name for name, fails in (
                ("kind", kinds is not None and self.kind_of(row) not in kinds),
                ("offline", online_only and not self.online[row[_SID]]),
                ("source", source_id is not None and row[_SID] != source_id)) if fails]
            if failed:
                out["any"] += 1
                if len(failed) == 1:
                    out[failed[0]] += 1
        out["all"] = out["any"]
        return out


# --------------------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------------------

def search(conn: sqlite3.Connection, sources: Mapping[int, Mapping[str, Any]], query: str, *,
           kind: str = "all", online_only: bool = False, source_id: int | None = None,
           limit: int = 200, include_templates: bool = False) -> dict[str, Any]:
    """Ranked search (SPEC §7).  ``sources`` is the Indexer registry (id → Source §7.1);
    online status and paths come from there.  Entries of sources missing from it, or with
    ``included`` False, are never returned.  Raises ValueError for an unknown ``kind``.

    ``hidden`` (only when nothing is shown and a filter is active) counts per filter the
    matches that switching off only that filter would show, plus ``any`` (alias ``all``): the
    matches hidden by any filter (SPEC §15.7).
    """
    started = time.perf_counter()
    if kind not in KIND_FILTERS:
        raise ValueError(f"Ukendt filter: {kind}")
    kinds = KIND_FILTERS[kind]
    source_id = None if source_id is None else int(source_id)
    limit = max(0, int(limit))
    tokens = textutil.tokenize(query or "")
    response: dict[str, Any] = {"query": query, "tokens": tokens, "took_ms": 0.0, "total": 0,
                                "truncated": False, "results": []}
    filtered = kinds is not None or online_only or source_id is not None
    hidden = dict(_NO_HIDDEN)
    if tokens:
        live = _live_sources(sources)
        fold = textutil.fold
        source_folds = {sid: f"{fold(src.get('display_name') or '')} "
                             f"{fold(src.get('volume_label') or '')}"
                        for sid, src in live.items()}
        source_hits = {t: frozenset(sid for sid, f in source_folds.items()
                                    if token_matches(t, f))
                       for t in tokens}
        with db.read_snapshot(conn):
            templates = None if include_templates else _template_prefixes(conn, live)
            ranker = _Ranker(tokens, fold(query), live, source_folds, templates)
            cands = _retrieve(conn, tokens, source_hits, source_id)
            roots = _root_rows(conn, live, tokens)
            pool = [*cands.rows.values(), *roots]
            top, total = ranker.rank(pool, kinds, online_only, source_id, limit)
            if total == 0 and filtered:
                # The source filter is applied in SQL: count its effect on an unfiltered pass.
                if source_id is not None:
                    pool = [*_retrieve(conn, tokens, source_hits, None).rows.values(), *roots]
                hidden = ranker.hidden(pool, kinds, online_only, source_id)
            full = _full_rows(conn, [row[_ID] for _score, row in top if row[_ID] > 0])
            results = []
            for score, row in top:
                src = live[row[_SID]]
                if row[_ID] < 0:
                    results.append(root_item(conn, src, tokens, mtime=row[_MTIME], score=score))
                elif row[_ID] in full:
                    results.append(_item(conn, full[row[_ID]], src, tokens, score))
            response["results"] = results
        response["total"] = total
        response["truncated"] = cands.truncated
    if response["total"] == 0 and filtered:
        response["hidden"] = hidden
    response["took_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return response


def recent_projects(conn: sqlite3.Connection, sources: Mapping[int, Mapping[str, Any]],
                    limit: int = 30, online_only: bool = False) -> list[dict[str, Any]]:
    """Newest projects (kind 2, and roots that are projects) by subtree mtime, as Items
    without highlights/score."""
    live = _live_sources(sources)
    limit = max(0, int(limit))
    picked: list[Row] = []
    with db.read_snapshot(conn):
        templates = _template_prefixes(conn, live)
        cur = conn.execute(f"SELECT {db.ROW_COLUMNS} FROM entries WHERE kind = ? "
                           "ORDER BY mtime DESC", (KIND_PROJECT,))
        try:
            while len(picked) < limit:
                batch = cur.fetchmany(max(64, 2 * limit))
                if not batch:
                    break
                for row in batch:
                    src = live.get(row[_R_SID])
                    if (src is not None and not (online_only and not src.get("online"))
                            and not _under_template(row[_R_SID], row[_R_REL], templates)):
                        picked.append(row)
        finally:
            cur.close()
        # Merge the roots that are projects (SPEC §15.3) by mtime: (mtime, order, row, root).
        ranked: list[tuple[float | None, int, Row | None, Mapping[str, Any] | None]] = [
            (row[_R_MTIME], i, row, None) for i, row in enumerate(picked[:limit])]
        for sid, src in live.items():
            if root_kind(src) == KIND_PROJECT and not (online_only and not src.get("online")):
                ranked.append((_root_mtime(conn, sid), len(ranked), None, src))
        ranked.sort(key=lambda r: (r[0] is None, -(r[0] or 0.0), r[1]))   # NULL mtime last
        return [_item(conn, row, live[row[_R_SID]]) if row is not None
                else root_item(conn, root, mtime=mtime)
                for mtime, _order, row, root in ranked[:limit]]


def children(conn: sqlite3.Connection, sources: Mapping[int, Mapping[str, Any]],
             source_id: int, rel_path: str, *, limit: int = 1000) -> list[dict[str, Any]]:
    """Direct children of ``rel_path`` (``''`` = the source root): folders first, by name."""
    src = _live_sources(sources).get(source_id)
    if src is None:
        return []
    with db.read_snapshot(conn):
        rows = conn.execute(f"SELECT {db.ROW_COLUMNS} FROM entries "
                            "WHERE source_id = ? AND parent_rel = ? "
                            "ORDER BY kind = 0, name COLLATE NOCASE LIMIT ?",
                            (source_id, db.normalize_rel(rel_path), limit)).fetchall()
        return [_item(conn, row, src) for row in rows]
