"""Windows file-system plumbing for discovery (SPEC §4.2).

* :func:`call_with_timeout` runs a call that can hang (dead disk, powered-off host) on a
  daemon thread with at most ONE in-flight thread per key, so stuck calls never pile up.
* :func:`list_volumes` lists ready local volumes incl. hot-plug detection (storage bus type).
* :func:`volume_info`, :func:`volume_size`, :func:`local_shares`, :func:`remote_shares`,
  :func:`mapped_drives`, :func:`resolve_host_ips`.

Every worker thread switches off the "There is no disk in the drive" critical-error dialog
for itself (``SetThreadErrorMode``), and local drives are checked for media with
``IOCTL_STORAGE_CHECK_VERIFY2`` on a handle opened with no access rights before anything
touches their file system.  Nothing here writes to any drive.
"""

from __future__ import annotations

import ctypes
import functools
import logging
import os
import socket
import struct
import threading
import time
import winreg
from ctypes import wintypes
from typing import Any, Callable, Sequence, TypeVar

from .pathmap import clean_path, is_drive_path, split_unc

log = logging.getLogger(__name__)

T = TypeVar("T")

# --------------------------------------------------------------------------------------
# Win32 declarations (own WinDLL objects, see SPEC §1 rule 8)
# --------------------------------------------------------------------------------------

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_netapi32 = ctypes.WinDLL("netapi32", use_last_error=True)
_mpr = ctypes.WinDLL("mpr", use_last_error=True)

_LPDWORD = ctypes.POINTER(wintypes.DWORD)
_PULARGE_INTEGER = ctypes.POINTER(ctypes.c_ulonglong)


def _declare(fn: Any, argtypes: list[Any], restype: Any) -> Any:
    fn.argtypes = argtypes
    fn.restype = restype
    return fn


_GetLogicalDrives = _declare(_kernel32.GetLogicalDrives, [], wintypes.DWORD)
_GetDriveTypeW = _declare(_kernel32.GetDriveTypeW, [wintypes.LPCWSTR], wintypes.UINT)
_QueryDosDeviceW = _declare(_kernel32.QueryDosDeviceW,
                            [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD], wintypes.DWORD)
_CreateFileW = _declare(_kernel32.CreateFileW,
                        [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                         wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE], wintypes.HANDLE)
_CloseHandle = _declare(_kernel32.CloseHandle, [wintypes.HANDLE], wintypes.BOOL)
_DeviceIoControl = _declare(_kernel32.DeviceIoControl,
                            [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                             wintypes.LPVOID, wintypes.DWORD, _LPDWORD, wintypes.LPVOID],
                            wintypes.BOOL)
_GetVolumeInformationW = _declare(_kernel32.GetVolumeInformationW,
                                  [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD, _LPDWORD,
                                   _LPDWORD, _LPDWORD, wintypes.LPWSTR, wintypes.DWORD],
                                  wintypes.BOOL)
_GetDiskFreeSpaceExW = _declare(_kernel32.GetDiskFreeSpaceExW,
                                [wintypes.LPCWSTR, _PULARGE_INTEGER, _PULARGE_INTEGER,
                                 _PULARGE_INTEGER], wintypes.BOOL)
_GetSystemWindowsDirectoryW = _declare(_kernel32.GetSystemWindowsDirectoryW,
                                       [wintypes.LPWSTR, wintypes.UINT], wintypes.UINT)
_SetThreadErrorMode = _declare(_kernel32.SetThreadErrorMode,
                               [wintypes.DWORD, _LPDWORD], wintypes.BOOL)
_NetShareEnum = _declare(_netapi32.NetShareEnum,
                         [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
                          wintypes.DWORD, _LPDWORD, _LPDWORD, _LPDWORD], wintypes.DWORD)
_NetApiBufferFree = _declare(_netapi32.NetApiBufferFree, [ctypes.c_void_p], wintypes.DWORD)
_WNetGetConnectionW = _declare(_mpr.WNetGetConnectionW,
                               [wintypes.LPCWSTR, wintypes.LPWSTR, _LPDWORD], wintypes.DWORD)


class _SHARE_INFO_1(ctypes.Structure):
    _fields_ = [("shi1_netname", wintypes.LPWSTR), ("shi1_type", wintypes.DWORD),
                ("shi1_remark", wintypes.LPWSTR)]


class _SHARE_INFO_2(ctypes.Structure):
    _fields_ = [("shi2_netname", wintypes.LPWSTR), ("shi2_type", wintypes.DWORD),
                ("shi2_remark", wintypes.LPWSTR), ("shi2_permissions", wintypes.DWORD),
                ("shi2_max_uses", wintypes.DWORD), ("shi2_current_uses", wintypes.DWORD),
                ("shi2_path", wintypes.LPWSTR), ("shi2_passwd", wintypes.LPWSTR)]


DRIVE_REMOVABLE = 2
DRIVE_FIXED = 3
DRIVE_REMOTE = 4

_SEM_FAILCRITICALERRORS = 0x0001
_SEM_NOOPENFILEERRORBOX = 0x8000
_FILE_SHARE_READ_WRITE = 0x0001 | 0x0002
_OPEN_EXISTING = 3
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_IOCTL_STORAGE_CHECK_VERIFY2 = 0x2D0800        # FILE_ANY_ACCESS: works on a 0-access handle
_IOCTL_STORAGE_QUERY_PROPERTY = 0x2D1400
_BUS_TYPE_OFFSET = 28                           # STORAGE_DEVICE_DESCRIPTOR.BusType
_HOTPLUG_BUS_TYPES = frozenset({0x4, 0x7, 0xC, 0xD})   # 1394, USB, SD, MMC
_NO_MEDIA_ERRORS = frozenset({21, 1112})        # ERROR_NOT_READY, ERROR_NO_MEDIA_IN_DRIVE

_ERROR_ACCESS_DENIED = 5
_ERROR_MORE_DATA = 234
_ERROR_CONNECTION_UNAVAIL = 1201
_MAX_PREFERRED_LENGTH = 0xFFFFFFFF
_STYPE_MASK = 0xFF
_STYPE_DISKTREE = 0
_STYPE_SPECIAL = 0x80000000
_SHARES_REG_KEY = r"SYSTEM\CurrentControlSet\Services\LanmanServer\Shares"

# --------------------------------------------------------------------------------------
# call_with_timeout
# --------------------------------------------------------------------------------------


class _Call:
    """One in-flight call; owned by its worker thread, observed by the waiting caller."""

    __slots__ = ("key", "done", "value", "error")

    def __init__(self, key: str) -> None:
        self.key = key
        self.done = threading.Event()
        self.value: Any = None
        self.error: BaseException | None = None


_calls: dict[str, _Call] = {}
_calls_lock = threading.Lock()
_tls = threading.local()


def call_with_timeout(key: str, fn: Callable[[], T], timeout: float) -> tuple[str, T | None]:
    """Run ``fn`` on a daemon thread and wait at most ``timeout`` seconds.

    Returns ``("ok", value)``, ``("timeout", None)``, ``("busy", None)`` or ``("error", None)``.
    At most one thread per ``key`` (compared case-insensitively) is in flight: while an
    earlier call for the key still runs, ``("busy", None)`` is returned immediately.  A call
    that times out keeps its key busy until it really finishes.  Exceptions raised by ``fn``
    are logged at debug level and available through :func:`last_exception`.
    """
    _tls.error = None
    call = _start(key, fn)
    if call is None:
        log.debug("%s: previous call still running", key)
        return "busy", None
    status, value = _outcome(call, timeout)
    if status == "error":
        _tls.error = call.error
    return status, value


def call_many_with_timeout(calls: Sequence[tuple[str, Callable[[], T]]],
                           timeout: float) -> list[tuple[str, T | None]]:
    """Run several independent ``(key, fn)`` calls in parallel with one shared deadline.

    Same per-key rules and result tuples as :func:`call_with_timeout`, in input order.
    """
    started = [_start(key, fn) for key, fn in calls]
    deadline = time.monotonic() + timeout
    return [("busy", None) if call is None else _outcome(call, deadline - time.monotonic())
            for call in started]


def last_exception() -> BaseException | None:
    """The exception behind the calling thread's latest ``("error", None)`` result."""
    return getattr(_tls, "error", None)


def _start(key: str, fn: Callable[[], Any]) -> _Call | None:
    folded = key.casefold()
    with _calls_lock:
        if folded in _calls:
            return None
        call = _Call(folded)
        _calls[folded] = call
    thread = threading.Thread(target=_run, args=(call, fn), name=f"winfs[{key}]", daemon=True)
    try:
        thread.start()
    except RuntimeError as exc:  # interpreter shutting down / no more threads
        call.error = exc
        _release(call)
    return call


def _run(call: _Call, fn: Callable[[], Any]) -> None:
    _suppress_error_dialogs()
    try:
        call.value = fn()
    except BaseException as exc:  # reported to the caller as ("error", None)
        call.error = exc
        log.debug("%s failed: %r", call.key, exc, exc_info=exc)
    finally:
        _release(call)


def _release(call: _Call) -> None:
    with _calls_lock:
        if _calls.get(call.key) is call:
            del _calls[call.key]
    call.done.set()


def _outcome(call: _Call, timeout: float) -> tuple[str, Any]:
    if not call.done.wait(max(0.0, timeout)):
        log.debug("%s: no answer within %.1f s", call.key, timeout)
        return "timeout", None
    if call.error is not None:
        return "error", None
    return "ok", call.value


def _suppress_error_dialogs() -> None:
    """Never show "no disk in drive"/open-file error boxes for this thread's I/O."""
    old = wintypes.DWORD()
    if not _SetThreadErrorMode(_SEM_FAILCRITICALERRORS | _SEM_NOOPENFILEERRORBOX,
                               ctypes.byref(old)):
        log.debug("SetThreadErrorMode failed: %s", ctypes.WinError(ctypes.get_last_error()))


# --------------------------------------------------------------------------------------
# "Last known good" results for cheap, rarely changing enumerations
# --------------------------------------------------------------------------------------

_last_good: dict[str, Any] = {}
_failing: set[str] = set()
_last_good_lock = threading.Lock()


def _remember(what: str, status: str, value: Any, default: Any) -> Any:
    """Store ``value`` on success; otherwise return the last good value (warn once)."""
    with _last_good_lock:
        if status == "ok":
            _last_good[what] = value
            _failing.discard(what)
            return value
        first_failure = what not in _failing
        _failing.add(what)
        fallback = _last_good.get(what, default)
    (log.warning if first_failure else log.debug)(
        "%s failed (%s) - using the last known result", what, status)
    return fallback


# --------------------------------------------------------------------------------------
# Recurring failures are logged once (volumes are polled every few seconds)
# --------------------------------------------------------------------------------------

_noted: set[str] = set()
_noted_lock = threading.Lock()


def _note_failure(key: str, message: str, *args: Any) -> None:
    """Log ``message`` (debug) the first time ``key`` fails; later failures stay silent until
    :func:`_note_success` clears the key.  E.g. a virtual drive whose storage query always
    fails would otherwise add a log line on every volume poll."""
    with _noted_lock:
        if key in _noted:
            return
        _noted.add(key)
    log.debug(message, *args)


def _note_success(key: str) -> None:
    with _noted_lock:
        _noted.discard(key)


# --------------------------------------------------------------------------------------
# Volumes
# --------------------------------------------------------------------------------------

_volumes_lock = threading.Lock()
_last_volumes: dict[str, dict] = {}   # drive -> last successfully queried volume


def list_volumes(*, timeout: float = 5.0) -> list[dict]:
    """Ready local volumes with a drive letter (SPEC §4.2).

    Each: ``{"drive": "H:", "root": "H:\\\\", "label", "serial": "5E3A0B21", "fs",
    "drive_type": 2|3, "is_system", "hotplug", "size"}`` (``size`` 0 when unknown).
    Remote, CD-ROM, RAM-disk, SUBST, unformatted and not-ready drives are skipped.  Drives
    are queried in parallel (one ``call_with_timeout`` key per drive letter).  A drive that is
    still mounted but slow to answer is reported with its last known facts plus
    ``"stale": True`` instead of flapping offline: its medium may have been swapped meanwhile
    (a card reader keeps its letter), so a stale entry's serial is NOT verified and callers
    must neither list its root nor attribute anything to that serial (SPEC §15.5).
    """
    with _volumes_lock:
        system_drive = _system_drive()
        drives = [(d, t) for d in _logical_drives()
                  if (t := _GetDriveTypeW(d + "\\")) in (DRIVE_REMOVABLE, DRIVE_FIXED)]
        outcomes = call_many_with_timeout(
            [(d, functools.partial(_query_volume, d, t, system_drive)) for d, t in drives],
            timeout)
        volumes: list[dict] = []
        for (drive, drive_type), (status, info) in zip(drives, outcomes):
            if status == "ok" and info is not None:
                _last_volumes[drive] = info
                volumes.append(dict(info))
                continue
            previous = _last_volumes.get(drive)
            if (status in ("busy", "timeout") and previous is not None
                    and previous["drive_type"] == drive_type):
                log.debug("%s answers slowly (%s) - reporting its last known facts as stale",
                          drive, status)
                volumes.append({**previous, "stale": True})
                continue
            _last_volumes.pop(drive, None)
            if status != "ok":
                log.debug("%s skipped (%s)", drive, status)
        present = {d for d, _ in drives}
        for drive in [d for d in _last_volumes if d not in present]:
            del _last_volumes[drive]
        return volumes


def volume_info(root: str, timeout: float = 5.0) -> dict | None:
    """``{"label", "serial", "fs"}`` of the volume holding ``root`` (drive or UNC path)."""
    vroot = _volume_root(root)
    if vroot is None:
        return None
    status, value = call_with_timeout(f"volinfo:{vroot}", lambda: _volume_information(vroot),
                                      timeout)
    return value if status == "ok" else None


def volume_size(root: str, *, timeout: float = 5.0) -> int | None:
    """Total size in bytes (``GetDiskFreeSpaceExW``) of the volume holding ``root``."""
    vroot = _volume_root(root)
    if vroot is None:
        return None
    status, value = call_with_timeout(
        f"volsize:{vroot}", lambda: _disk_size(vroot) if _media_ready(vroot) else None, timeout)
    return value if status == "ok" else None


def _query_volume(drive: str, drive_type: int, system_drive: str) -> dict | None:
    """Facts about one drive letter, or None when it is not a usable local volume."""
    if _is_subst(drive):
        return None
    ready, bus_type, problem = _probe_device(drive)
    if not ready:
        return None
    root = drive + "\\"
    info = _read_volume_information(root)
    if info is None:
        return None
    if problem is not None:     # e.g. Google Drive's virtual disk: fails on every poll
        _note_failure(f"storage:{drive}:{info['serial']}",
                      "Storage property query on %s (serial %s) failed: %s", drive,
                      info["serial"], problem)
    return {
        "drive": drive, "root": root, "label": info["label"], "serial": info["serial"],
        "fs": info["fs"], "drive_type": drive_type, "is_system": drive == system_drive,
        "hotplug": _is_hotplug(bus_type, drive_type), "size": _disk_size(root) or 0,
    }


def _is_hotplug(bus_type: int | None, drive_type: int) -> bool:
    """USB/SD/MMC/1394 bus; a removable drive whose bus cannot be queried counts too."""
    if bus_type is None:
        return drive_type == DRIVE_REMOVABLE
    return bus_type in _HOTPLUG_BUS_TYPES


def _probe_device(drive: str) -> tuple[bool, int | None, str | None]:
    """``(media present, storage bus type, why the bus type is unknown)`` for a drive letter.

    The device is opened with desired access 0 (no admin needed, no media access) and
    closed again immediately.  If it cannot be opened, readiness is unknown and reported
    as True: the caller's GetVolumeInformationW then decides (dialogs are off anyway).
    Failures are returned, not logged: volumes are polled every few seconds.
    """
    handle = _CreateFileW("\\\\.\\" + drive, 0, _FILE_SHARE_READ_WRITE, None, _OPEN_EXISTING,
                          0, None)
    if handle is None or handle == _INVALID_HANDLE_VALUE:
        return True, None, f"cannot open the device: {ctypes.WinError(ctypes.get_last_error())}"
    try:
        returned = wintypes.DWORD()
        if not _DeviceIoControl(handle, _IOCTL_STORAGE_CHECK_VERIFY2, None, 0, None, 0,
                                ctypes.byref(returned), None):
            if ctypes.get_last_error() in _NO_MEDIA_ERRORS:
                return False, None, None
        query = (ctypes.c_uint32 * 3)()  # StorageDeviceProperty, PropertyStandardQuery
        out = ctypes.create_string_buffer(1024)
        if not _DeviceIoControl(handle, _IOCTL_STORAGE_QUERY_PROPERTY, query,
                                ctypes.sizeof(query), out, ctypes.sizeof(out),
                                ctypes.byref(returned), None):
            return True, None, str(ctypes.WinError(ctypes.get_last_error()))
        return True, _parse_bus_type(out.raw[:returned.value]), None
    finally:
        _CloseHandle(handle)


def _parse_bus_type(descriptor: bytes) -> int | None:
    """``BusType`` of a STORAGE_DEVICE_DESCRIPTOR, None if the buffer is too short."""
    if len(descriptor) < _BUS_TYPE_OFFSET + 4:
        return None
    return struct.unpack_from("<I", descriptor, _BUS_TYPE_OFFSET)[0]


def _media_ready(vroot: str) -> bool:
    """False only for a local removable/fixed drive that reports no media."""
    if vroot.startswith("\\\\"):
        return True
    drive = vroot[:2]
    if _GetDriveTypeW(vroot) not in (DRIVE_REMOVABLE, DRIVE_FIXED):
        return True
    return _probe_device(drive)[0]


def _volume_information(vroot: str) -> dict | None:
    return _read_volume_information(vroot) if _media_ready(vroot) else None


def _read_volume_information(vroot: str) -> dict | None:
    label = ctypes.create_unicode_buffer(261)
    fs_name = ctypes.create_unicode_buffer(261)
    serial = wintypes.DWORD()
    max_component = wintypes.DWORD()
    flags = wintypes.DWORD()
    key = f"volinfo:{vroot.casefold()}"
    if not _GetVolumeInformationW(vroot, label, len(label), ctypes.byref(serial),
                                  ctypes.byref(max_component), ctypes.byref(flags),
                                  fs_name, len(fs_name)):
        _note_failure(key, "GetVolumeInformationW(%s): %s", vroot,
                      ctypes.WinError(ctypes.get_last_error()))
        return None
    _note_success(key)
    return {"label": label.value, "serial": f"{serial.value:08X}", "fs": fs_name.value}


def _disk_size(vroot: str) -> int | None:
    total = ctypes.c_ulonglong()
    key = f"size:{vroot.casefold()}"
    if not _GetDiskFreeSpaceExW(vroot, None, ctypes.byref(total), None):
        _note_failure(key, "GetDiskFreeSpaceExW(%s): %s", vroot,
                      ctypes.WinError(ctypes.get_last_error()))
        return None
    _note_success(key)
    return total.value


def _volume_root(path: str) -> str | None:
    """``X:\\`` or ``\\\\HOST\\share\\`` (trailing backslash required by the APIs)."""
    p = clean_path(path)
    if is_drive_path(p):
        return p[:3]
    unc = split_unc(p)
    if unc is not None and unc[1]:
        return f"\\\\{unc[0]}\\{unc[1]}\\"
    return None


def _logical_drives() -> list[str]:
    mask = _GetLogicalDrives()
    return [f"{chr(ord('A') + i)}:" for i in range(26) if mask & (1 << i)]


def _is_subst(drive: str) -> bool:
    """SUBST drives alias a folder of another volume (``\\??\\C:\\x``); never list them."""
    target = ctypes.create_unicode_buffer(1024)
    if not _QueryDosDeviceW(drive, target, len(target)):
        return False
    return target.value.startswith("\\??\\")


@functools.cache
def _system_drive() -> str:
    buf = ctypes.create_unicode_buffer(261)
    n = _GetSystemWindowsDirectoryW(buf, len(buf))
    path = buf.value if 0 < n < len(buf) else os.environ.get("SystemRoot", "C:\\Windows")
    return path[:2].upper()


# --------------------------------------------------------------------------------------
# Shares, mapped drives, host names
# --------------------------------------------------------------------------------------

def local_shares(*, timeout: float = 5.0) -> list[dict]:
    """This computer's plain disk shares: ``[{"name", "path"}]`` (no ``$``/special shares).

    Uses NetShareEnum level 2 (falls back to the LanmanServer registry list if that is
    denied).  On failure the last known list is returned, since shares rarely change.
    """
    status, value = call_with_timeout("netshare-local", _enum_local_shares, timeout)
    shares = _remember("local share enumeration", status, value, [])
    return [dict(s) for s in shares]


def remote_shares(host: str, timeout: float = 8.0) -> list[str] | None:
    """Plain disk share names of ``host`` (NetShareEnum level 1); None = unreachable/busy."""
    name = _host_arg(host)
    if name is None:
        return None
    status, value = call_with_timeout(f"netshare:{name}", lambda: _enum_remote_shares(name),
                                      timeout)
    if status != "ok":
        log.debug("Shares of %s unavailable (%s: %r)", name, status, last_exception())
        return None
    return value


def mapped_drives(*, timeout: float = 3.0) -> dict[str, str]:
    """``{"Z:": "\\\\\\\\HOST\\\\share"}`` for mapped network drives (WNetGetConnectionW).

    Only local provider lookups, no network I/O; on failure the last known map is returned.
    """
    status, value = call_with_timeout("mapped-drives", _enum_mapped_drives, timeout)
    return dict(_remember("mapped drive enumeration", status, value, {}))


def resolve_host_ips(host: str, timeout: float = 3.0) -> list[str]:
    """IP addresses of ``host`` (IPv4 first); ``[]`` if it cannot be resolved in time."""
    name = _host_arg(host)
    if name is None:
        return []
    status, infos = call_with_timeout(
        f"dns:{name}", lambda: socket.getaddrinfo(name, None, type=socket.SOCK_STREAM), timeout)
    if status != "ok" or not infos:
        return []
    ipv4: list[str] = []
    ipv6: list[str] = []
    for family, _type, _proto, _canon, sockaddr in infos:
        ip = str(sockaddr[0]).split("%", 1)[0]
        bucket = ipv4 if family == socket.AF_INET else ipv6
        if ip not in bucket:
            bucket.append(ip)
    return ipv4 + ipv6


def _host_arg(host: str) -> str | None:
    name = host.strip().lstrip("\\")
    if not name or any(c in name for c in "\\/ "):
        return None
    return name


def _is_plain_disk_share(name: str | None, share_type: int) -> bool:
    return (bool(name) and (share_type & _STYPE_MASK) == _STYPE_DISKTREE
            and not share_type & _STYPE_SPECIAL and not name.endswith("$"))


def _net_share_enum(server: str | None, level: int) -> list[tuple[str, int, str | None]]:
    """``[(name, type, path|None)]`` from NetShareEnum; raises OSError on failure."""
    info_type = _SHARE_INFO_2 if level == 2 else _SHARE_INFO_1
    rows: list[tuple[str, int, str | None]] = []
    resume = wintypes.DWORD(0)
    while True:
        buf = ctypes.c_void_p()
        read = wintypes.DWORD()
        total = wintypes.DWORD()
        rc = _NetShareEnum(server, level, ctypes.byref(buf), _MAX_PREFERRED_LENGTH,
                           ctypes.byref(read), ctypes.byref(total), ctypes.byref(resume))
        try:
            if rc not in (0, _ERROR_MORE_DATA):
                raise ctypes.WinError(rc)
            if buf.value and read.value:
                items = ctypes.cast(buf, ctypes.POINTER(info_type * read.value)).contents
                for item in items:
                    if level == 2:
                        rows.append((item.shi2_netname, item.shi2_type, item.shi2_path))
                    else:
                        rows.append((item.shi1_netname, item.shi1_type, None))
        finally:
            if buf.value:
                _NetApiBufferFree(buf)
        if rc != _ERROR_MORE_DATA:
            return rows


def _enum_local_shares() -> list[dict]:
    try:
        rows = _net_share_enum(None, 2)
    except OSError as exc:
        if exc.winerror != _ERROR_ACCESS_DENIED:
            raise
        log.debug("NetShareEnum level 2 denied - reading the share list from the registry")
        return _registry_shares()
    return [{"name": name, "path": path} for name, stype, path in rows
            if path and _is_plain_disk_share(name, stype)]


def _registry_shares() -> list[dict]:
    shares: list[dict] = []
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _SHARES_REG_KEY) as key:
        index = 0
        while True:
            try:
                name, data, value_type = winreg.EnumValue(key, index)
            except OSError:
                break
            index += 1
            if value_type == winreg.REG_MULTI_SZ:
                share = _parse_share_value(name, data)
                if share is not None:
                    shares.append(share)
    return shares


def _parse_share_value(name: str, lines: list[str]) -> dict | None:
    """One ``LanmanServer\\Shares`` value (``["Path=C:\\x", "Type=0", …]``) → share dict."""
    fields = dict(line.split("=", 1) for line in lines if "=" in line)
    try:
        share_type = int(fields.get("Type", "0"))
    except ValueError:
        return None
    path = fields.get("Path", "")
    if not path or not _is_plain_disk_share(name, share_type):
        return None
    return {"name": name, "path": path}


def _enum_remote_shares(host: str) -> list[str]:
    names = {name for name, stype, _ in _net_share_enum("\\\\" + host, 1)
             if _is_plain_disk_share(name, stype)}
    return sorted(names, key=str.casefold)


def _enum_mapped_drives() -> dict[str, str]:
    mapped: dict[str, str] = {}
    for drive in _logical_drives():
        if _GetDriveTypeW(drive + "\\") != DRIVE_REMOTE:
            continue
        remote = _wnet_connection(drive)
        if remote:
            mapped[drive] = remote
    return mapped


def _wnet_connection(drive: str) -> str | None:
    size = wintypes.DWORD(512)
    for _attempt in range(2):
        buf = ctypes.create_unicode_buffer(size.value)
        rc = _WNetGetConnectionW(drive, buf, ctypes.byref(size))
        if rc in (0, _ERROR_CONNECTION_UNAVAIL):  # unavailable = remembered, not connected
            return buf.value or None
        if rc != _ERROR_MORE_DATA:
            return None
    return None
