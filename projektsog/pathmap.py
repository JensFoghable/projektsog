"""Path normalisation and aliases between local paths and UNC paths (SPEC §4.5).

The same folder can be reached under several spellings: ``C:\\Kunder 2026 (STUDIO)\\x``,
``\\\\studio-pc\\Kunder 2026 (STUDIO)\\x`` (DaVinci Resolve stores local media like this),
``\\\\192.0.2.99\\...``, ``\\\\?\\C:\\...`` or a mapped drive letter.  :class:`PathMap`
turns all of them into one canonical form so paths can be compared by :meth:`PathMap.key`.

The module-level helpers only rewrite strings; nothing here touches the file system.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Iterable, Mapping

_SEP = "\\"
_UNC_PREFIXES = ("\\\\?\\UNC\\", "\\\\.\\UNC\\", "\\??\\UNC\\")
_DEVICE_PREFIXES = ("\\\\?\\", "\\\\.\\", "\\??\\")
_LOCAL_ALIASES = ("LOCALHOST", "127.0.0.1")


# --------------------------------------------------------------------------------------
# String helpers (no aliases)
# --------------------------------------------------------------------------------------

def _is_drive_spec(p: str) -> bool:
    """True for ``X:`` or ``X:\\...`` (drive-absolute)."""
    return (len(p) >= 2 and p[1] == ":" and p[0].isascii() and p[0].isalpha()
            and (len(p) == 2 or p[2] == _SEP))


def _resolve_dots(segments: list[str], keep: int) -> list[str]:
    """Drop ``.`` and apply ``..`` lexically, never above the first ``keep`` segments."""
    out = segments[:keep]
    for seg in segments[keep:]:
        if seg == ".":
            continue
        if seg == "..":
            if len(out) > keep:
                out.pop()
            continue
        out.append(seg)
    return out


def clean_path(path: str) -> str:
    """Canonical spelling of ``path`` without resolving any aliases.

    Strips ``\\\\?\\`` / ``\\\\?\\UNC\\`` (and ``\\\\.\\`` / ``\\??\\``) prefixes, turns ``/``
    into ``\\``, collapses duplicate separators (except the leading ``\\\\`` of UNC paths),
    removes trailing separators (``C:\\`` keeps its backslash), resolves ``.``/``..`` and
    upper-cases the drive letter and the UNC host.
    """
    p = path.replace("/", _SEP)
    if p[:8].upper() in _UNC_PREFIXES:
        p = _SEP * 2 + p[8:]
    elif p[:4] in _DEVICE_PREFIXES and _is_drive_spec(p[4:]):
        p = p[4:]
    if p.startswith(_SEP * 2):
        segments = _resolve_dots([s for s in p[2:].split(_SEP) if s], keep=2)
        if not segments:
            return _SEP * 2
        segments[0] = segments[0].upper()
        return _SEP * 2 + _SEP.join(segments)
    if _is_drive_spec(p):
        segments = _resolve_dots([s for s in p[2:].split(_SEP) if s], keep=0)
        return p[0].upper() + ":" + _SEP + _SEP.join(segments)
    lead = _SEP if p.startswith(_SEP) else ""
    return lead + _SEP.join(s for s in p.split(_SEP) if s)


def is_drive_path(path: str) -> bool:
    """True if ``path`` is an absolute drive-letter path (``X:\\...``)."""
    return _is_drive_spec(clean_path(path))


def split_unc(path: str) -> tuple[str, str, str] | None:
    """``(HOST, share, rest)`` of a UNC path, else None.

    ``rest`` has no leading separator and is ``""`` at the share root; ``share`` is ``""``
    for a bare ``\\\\HOST``.  Device paths (``\\\\?\\Volume{…}``, ``\\\\.\\X``) are not UNC.
    """
    return _split_clean(clean_path(path))


def _split_clean(p: str) -> tuple[str, str, str] | None:
    """:func:`split_unc` for an already cleaned path."""
    if not p.startswith(_SEP * 2) or p[2:3] in ("?", "."):
        return None
    host, _, tail = p[2:].partition(_SEP)
    if not host:
        return None
    share, _, rest = tail.partition(_SEP)
    return host, share, rest


def _join_clean(base: str, rest: str) -> str:
    """Append a cleaned relative ``rest`` to a cleaned ``base``."""
    if not rest:
        return base
    return base + rest if base.endswith(_SEP) else base + _SEP + rest


def path_parts(path: str) -> list[str]:
    """Components of the cleaned path: ``["C:", "a"]`` or ``["\\\\HOST", "share", "a"]``."""
    p = clean_path(path)
    if p.startswith(_SEP * 2):
        segments = p[2:].split(_SEP)
        return [_SEP * 2 + segments[0], *segments[1:]] if segments[0] else []
    if _is_drive_spec(p):
        return [p[:2], *(s for s in p[3:].split(_SEP) if s)]
    return [s for s in p.split(_SEP) if s]


def relative_parts(path: str, parent: str) -> list[str] | None:
    """Components of ``path`` below ``parent`` (case-insensitive, whole components only).

    ``[]`` when both are the same folder, None when ``path`` is not inside ``parent``
    (``C:\\Kunder 2026`` is not inside ``C:\\Kunder``).
    """
    child = path_parts(path)
    base = path_parts(parent)
    if not base or len(child) < len(base):
        return None
    if any(a.casefold() != b.casefold() for a, b in zip(child, base)):
        return None
    return child[len(base):]


def is_within(path: str, parent: str) -> bool:
    """True if ``path`` is ``parent`` or lies below it (case-insensitive)."""
    return relative_parts(path, parent) is not None


def join_path(base: str, *parts: str) -> str:
    """Join cleaned ``base`` and plain name components without doubling separators."""
    p = clean_path(base)
    names = [s for s in parts if s]
    if not names:
        return p
    return p + ("" if p.endswith(_SEP) else _SEP) + _SEP.join(names)


def long_path(path: str) -> str:
    """Extended-length form for file system calls (paths > 260 chars).

    ``C:\\x`` → ``\\\\?\\C:\\x``; ``\\\\host\\share\\x`` → ``\\\\?\\UNC\\host\\share\\x``.
    """
    p = clean_path(path)
    if _is_drive_spec(p):
        return "\\\\?\\" + p
    unc = _split_clean(p)
    if unc is not None and unc[1]:
        return "\\\\?\\UNC\\" + p[2:]
    return p


def _is_ip_address(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return False
    return True


def _host_name(host: str) -> str:
    return host.strip().strip(_SEP).upper()


# --------------------------------------------------------------------------------------
# PathMap
# --------------------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class _Tables:
    own: frozenset[str]                  # upper-case names/IPs that mean "this computer"
    hosts: frozenset[str]                # known remote host names (upper case)
    ip_host: Mapping[str, str]           # upper-case IP -> HOST
    share_paths: Mapping[str, str]       # casefolded share name -> clean local path
    shares: tuple[tuple[str, tuple[str, ...]], ...]  # (share name, casefolded parts), deepest first
    mapped: Mapping[str, str]            # "Z:" -> clean UNC target


class PathMap:
    """Canonical paths across local, UNC, IP and mapped-drive spellings.

    Thread-safe: :meth:`update` builds a new immutable table and swaps it in with one
    assignment, so readers never lock and always see a consistent snapshot.
    """

    def __init__(self, hostname: str) -> None:
        self.hostname = _host_name(hostname)
        self._tables = self._build((), {}, {}, ())

    def update(self, local_shares: list[dict], mapped_drives: dict[str, str],
               host_ips: dict[str, list[str]], own_ips: list[str]) -> None:
        """Replace the alias tables.

        ``local_shares``: ``winfs.local_shares()``; ``mapped_drives``: ``winfs.mapped_drives()``;
        ``host_ips``: ``{host: resolve_host_ips(host)}`` for known hosts; ``own_ips``: this
        computer's addresses.
        """
        self._tables = self._build(local_shares, mapped_drives, host_ips, own_ips)

    def normalize(self, path: str) -> str:
        """Canonical form of ``path`` (see module doc and SPEC §4.5)."""
        tables = self._tables
        p = clean_path(path)
        if _is_drive_spec(p):
            target = tables.mapped.get(p[:2])
            if target is None:
                return p
            p = _join_clean(target, p[3:])
        unc = _split_clean(p)
        if unc is None:
            return p
        host, share, rest = unc
        host = self._canonical_host(host, tables)
        if host == self.hostname and share:
            local = self._own_share_path(share, tables)
            if local is not None:
                return _join_clean(local, rest)
        return _SEP * 2 + _SEP.join(s for s in (host, share, rest) if s)

    def key(self, path: str) -> str:
        """Comparison key: ``casefold(normalize(path))``."""
        return self.normalize(path).casefold()

    def unc_for(self, local_path: str) -> str | None:
        """UNC path under which other computers reach ``local_path``.

        Local paths inside a local share become ``\\\\HOSTNAME\\<share>\\<rest>`` (the
        deepest share wins); UNC input is returned normalized; anything else gives None.
        """
        p = self.normalize(local_path)
        if _split_clean(p) is not None:
            return p
        if not _is_drive_spec(p):
            return None
        parts = path_parts(p)
        folded = tuple(s.casefold() for s in parts)
        for name, share_parts in self._tables.shares:
            if folded[:len(share_parts)] == share_parts:
                return _SEP * 2 + _SEP.join([self.hostname, name, *parts[len(share_parts):]])
        return None

    # -- internals ----------------------------------------------------------------------
    def _canonical_host(self, host: str, tables: _Tables) -> str:
        if host in tables.own:
            return self.hostname
        owner = tables.ip_host.get(host)
        if owner is not None:
            return owner
        if "." in host and not _is_ip_address(host):
            first = host.split(".", 1)[0]  # FQDN of a known computer
            if first in tables.own:
                return self.hostname
            if first in tables.hosts:
                return first
        return host

    @staticmethod
    def _own_share_path(share: str, tables: _Tables) -> str | None:
        path = tables.share_paths.get(share.casefold())
        if path is not None:
            return path
        if len(share) == 2 and share[1] == "$" and share[0].isascii() and share[0].isalpha():
            return share[0].upper() + ":" + _SEP  # administrative drive share (C$)
        return None

    def _build(self, local_shares: Iterable[dict], mapped_drives: Mapping[str, str],
               host_ips: Mapping[str, list[str]], own_ips: Iterable[str]) -> _Tables:
        shares: list[tuple[str, str, tuple[str, ...]]] = []
        for share in local_shares:
            name = str(share.get("name") or "")
            path = clean_path(str(share.get("path") or ""))
            if name and _is_drive_spec(path):
                shares.append((name, path, tuple(s.casefold() for s in path_parts(path))))
        # Deepest share first; for equal paths prefer the share named like the folder.
        shares.sort(key=lambda s: (-len(s[2]), s[2][-1] != s[0].casefold(), s[0].casefold()))
        share_paths: dict[str, str] = {}
        for name, path, _ in shares:
            share_paths.setdefault(name.casefold(), path)

        mapped: dict[str, str] = {}
        for drive, target in mapped_drives.items():
            letter = clean_path(drive)
            unc = clean_path(target)
            if _is_drive_spec(letter) and _split_clean(unc) is not None:
                mapped[letter[:2]] = unc

        own = {self.hostname, *_LOCAL_ALIASES, *(ip.strip().upper() for ip in own_ips)}
        hosts: set[str] = set()
        ip_host: dict[str, str] = {}
        for host, ips in host_ips.items():
            name = _host_name(host)
            if not name:
                continue
            if name == self.hostname:
                own.update(ip.strip().upper() for ip in ips)
                continue
            hosts.add(name)
            for ip in ips:
                ip_host.setdefault(ip.strip().upper(), name)
        own.discard("")
        return _Tables(
            own=frozenset(own),
            hosts=frozenset(hosts),
            ip_host={ip: h for ip, h in ip_host.items() if ip and ip not in own},
            share_paths=share_paths,
            shares=tuple((name, parts) for name, _, parts in shares),
            mapped=mapped,
        )
