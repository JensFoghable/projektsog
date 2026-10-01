"""Unit tests for projektsog.winfs: call_with_timeout, parsers, local read-only sanity checks."""

import ctypes
import os
import re
import struct
import tempfile
import threading
import time
import unittest
from ctypes import wintypes
from unittest import mock

from projektsog import winfs

_tmp: tempfile.TemporaryDirectory | None = None
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.GetThreadErrorMode.argtypes = []
_kernel32.GetThreadErrorMode.restype = wintypes.DWORD


def setUpModule() -> None:
    global _tmp
    _tmp = tempfile.TemporaryDirectory()
    os.environ["LOCALAPPDATA"] = _tmp.name


def tearDownModule() -> None:
    if _tmp is not None:
        _tmp.cleanup()


def wait_until_free(key: str, limit: float = 5.0) -> None:
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        status, _ = winfs.call_with_timeout(key, lambda: None, 1.0)
        if status != "busy":
            return
        time.sleep(0.01)
    raise AssertionError(f"key {key} stayed busy")


class CallWithTimeoutTests(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(winfs.call_with_timeout("t-ok", lambda: 42, 2.0), ("ok", 42))
        self.assertIsNone(winfs.last_exception())

    def test_error_is_reported_and_exception_available(self):
        def boom():
            raise ValueError("nope")

        self.assertEqual(winfs.call_with_timeout("t-error", boom, 2.0), ("error", None))
        self.assertIsInstance(winfs.last_exception(), ValueError)
        winfs.call_with_timeout("t-error", lambda: 1, 2.0)
        self.assertIsNone(winfs.last_exception())

    def test_timeout_then_busy_until_the_stuck_call_ends(self):
        release = threading.Event()
        start = time.monotonic()
        self.assertEqual(winfs.call_with_timeout("T-Stuck", release.wait, 0.05), ("timeout", None))
        self.assertLess(time.monotonic() - start, 1.0)
        start = time.monotonic()
        # Same key (case-insensitive) while the first call still runs -> busy at once.
        self.assertEqual(winfs.call_with_timeout("t-stuck", lambda: 1, 5.0), ("busy", None))
        self.assertLess(time.monotonic() - start, 0.1)
        # Other keys are unaffected.
        self.assertEqual(winfs.call_with_timeout("t-other", lambda: 2, 2.0), ("ok", 2))
        release.set()
        wait_until_free("t-stuck")
        self.assertEqual(winfs.call_with_timeout("t-stuck", lambda: 3, 2.0), ("ok", 3))

    def test_only_one_thread_per_key(self):
        release = threading.Event()
        before = threading.active_count()
        winfs.call_with_timeout("t-pile", release.wait, 0.01)
        for _ in range(20):
            self.assertEqual(winfs.call_with_timeout("t-pile", release.wait, 0.01), ("busy", None))
        self.assertLessEqual(threading.active_count(), before + 1)
        release.set()
        wait_until_free("t-pile")

    def test_call_many_runs_in_parallel_with_shared_deadline(self):
        release = threading.Event()
        start = time.monotonic()
        results = winfs.call_many_with_timeout(
            [("m-1", lambda: time.sleep(0.2) or "a"), ("m-2", lambda: time.sleep(0.2) or "b"),
             ("m-3", release.wait), ("m-3", lambda: "dup"),
             ("m-4", lambda: 1 / 0)], 0.5)
        elapsed = time.monotonic() - start
        self.assertEqual(results, [("ok", "a"), ("ok", "b"), ("timeout", None), ("busy", None),
                                   ("error", None)])
        self.assertLess(elapsed, 0.9)
        release.set()
        wait_until_free("m-3")

    def test_worker_threads_suppress_critical_error_dialogs(self):
        status, mode = winfs.call_with_timeout("t-mode", _kernel32.GetThreadErrorMode, 2.0)
        self.assertEqual(status, "ok")
        self.assertTrue(mode & 0x0001, "SEM_FAILCRITICALERRORS")
        self.assertTrue(mode & 0x8000, "SEM_NOOPENFILEERRORBOX")


class ParserTests(unittest.TestCase):
    def test_bus_type_from_storage_descriptor(self):
        descriptor = struct.pack("<IIBBBBIIIIII", 1, 64, 0, 0, 0, 0, 0, 0, 0, 0, 7, 0)
        self.assertEqual(winfs._parse_bus_type(descriptor), 7)
        self.assertIsNone(winfs._parse_bus_type(descriptor[:20]))

    def test_hotplug_rule(self):
        for bus in (0x7, 0xC, 0xD, 0x4):
            self.assertTrue(winfs._is_hotplug(bus, winfs.DRIVE_FIXED), bus)
        for bus in (0x11, 0xB, 0x3, 0xE):
            self.assertFalse(winfs._is_hotplug(bus, winfs.DRIVE_REMOVABLE), bus)
        self.assertTrue(winfs._is_hotplug(None, winfs.DRIVE_REMOVABLE))
        self.assertFalse(winfs._is_hotplug(None, winfs.DRIVE_FIXED))

    def test_share_filter(self):
        self.assertTrue(winfs._is_plain_disk_share("Kunder 2026 (STUDIO)", 0))
        self.assertTrue(winfs._is_plain_disk_share("Temp", 0x40000000))  # STYPE_TEMPORARY
        self.assertFalse(winfs._is_plain_disk_share("C$", 0x80000000))
        self.assertFalse(winfs._is_plain_disk_share("IPC$", 0x80000003))
        self.assertFalse(winfs._is_plain_disk_share("Printer", 1))
        self.assertFalse(winfs._is_plain_disk_share("Hidden$", 0))
        self.assertFalse(winfs._is_plain_disk_share("", 0))

    def test_registry_share_value(self):
        lines = ["CATimeout=0", "Path=C:\\Kunder 2026 (STUDIO)", "Remark=a=b", "Type=0"]
        self.assertEqual(winfs._parse_share_value("Kunder", lines),
                         {"name": "Kunder", "path": "C:\\Kunder 2026 (STUDIO)"})
        self.assertIsNone(winfs._parse_share_value("Printer", ["Path=x", "Type=1"]))
        self.assertIsNone(winfs._parse_share_value("Hidden$", ["Path=C:\\x", "Type=0"]))
        self.assertIsNone(winfs._parse_share_value("NoPath", ["Type=0"]))
        self.assertIsNone(winfs._parse_share_value("Bad", ["Path=C:\\x", "Type=zz"]))

    def test_volume_root(self):
        self.assertEqual(winfs._volume_root(r"h:\2024 Disk Sølv\x"), "H:\\")
        self.assertEqual(winfs._volume_root("C:"), "C:\\")
        self.assertEqual(winfs._volume_root(r"\\?\UNC\host\Share\x"), "\\\\HOST\\Share\\")
        self.assertIsNone(winfs._volume_root(r"\\host"))
        self.assertIsNone(winfs._volume_root("relative"))

    def test_invalid_host_names_need_no_network(self):
        self.assertIsNone(winfs.remote_shares("bad\\host"))
        self.assertIsNone(winfs.remote_shares("  "))
        self.assertEqual(winfs.resolve_host_ips(""), [])


class VolumeListTests(unittest.TestCase):
    """list_volumes/_query_volume with the Win32 layer replaced (no real drive involved)."""

    INFO = {"drive": "Q:", "root": "Q:\\", "label": "Kort", "serial": "AAAA0001", "fs": "exFAT",
            "drive_type": 2, "is_system": False, "hotplug": True, "size": 1}

    def test_a_slow_drive_is_reported_stale_with_its_last_facts(self):
        outcomes = [[("ok", dict(self.INFO))], [("timeout", None)], [("busy", None)],
                    [("ok", None)], [("busy", None)]]
        self.addCleanup(winfs._last_volumes.pop, "Q:", None)
        with mock.patch.object(winfs, "_logical_drives", return_value=["Q:"]), \
                mock.patch.object(winfs, "_GetDriveTypeW", return_value=2), \
                mock.patch.object(winfs, "_system_drive", return_value="C:"), \
                mock.patch.object(winfs, "call_many_with_timeout",
                                  side_effect=lambda calls, timeout: outcomes.pop(0)):
            self.assertEqual(winfs.list_volumes(), [self.INFO])             # fresh: no flag
            stale = {**self.INFO, "stale": True}    # its serial is NOT verified (§15.5)
            self.assertEqual(winfs.list_volumes(), [stale])
            self.assertEqual(winfs.list_volumes(), [stale])
            self.assertEqual(winfs.list_volumes(), [])                      # no medium now
            self.assertEqual(winfs.list_volumes(), [])      # nothing known to fall back on

    def test_a_failing_storage_query_is_logged_once_per_drive_and_serial(self):
        for key in ("storage:Y:4E7D2C90", "storage:Y:22220000"):
            self.addCleanup(winfs._note_success, key)
        infos = iter([{"label": "Google Drive", "serial": "4E7D2C90", "fs": "FAT32"}] * 3
                     + [{"label": "Andet", "serial": "22220000", "fs": "FAT32"}] * 2)
        with mock.patch.object(winfs, "_is_subst", return_value=False), \
                mock.patch.object(winfs, "_probe_device",
                                  return_value=(True, None, "[WinError 122] for småt")), \
                mock.patch.object(winfs, "_read_volume_information",
                                  side_effect=lambda root: next(infos)), \
                mock.patch.object(winfs, "_disk_size", return_value=1), \
                self.assertLogs("projektsog.winfs", "DEBUG") as logs:
            for _ in range(5):          # every volume poll; the 4th sees another medium
                info = winfs._query_volume("Y:", 3, "C:")
                self.assertFalse(info["hotplug"])
        lines = [line for line in logs.output if "Storage property query on Y:" in line]
        self.assertEqual(len(lines), 2)
        self.assertIn("4E7D2C90", lines[0])
        self.assertIn("22220000", lines[1])

    def test_a_failing_volume_query_is_logged_again_only_after_a_success(self):
        answers = iter([0, 0, 1, 0])
        self.addCleanup(winfs._note_success, "volinfo:x:\\")

        def fake_info(root, label, size, serial, *rest):
            return next(answers)

        with mock.patch.object(winfs, "_GetVolumeInformationW", side_effect=fake_info), \
                self.assertLogs("projektsog.winfs", "DEBUG") as logs:
            results = [winfs._read_volume_information("X:\\") for _ in range(4)]
        self.assertEqual([r is None for r in results], [True, True, False, True])
        self.assertEqual(len([m for m in logs.output if "GetVolumeInformationW(X:" in m]), 2)


class LocalSanityTests(unittest.TestCase):
    """Read-only checks against this computer's own volumes/shares (no network)."""

    def test_list_volumes_contains_the_system_volume(self):
        volumes = winfs.list_volumes()
        system = [v for v in volumes if v["is_system"]]
        self.assertEqual(len(system), 1)
        vol = system[0]
        self.assertEqual(vol["root"], vol["drive"] + "\\")
        self.assertRegex(vol["serial"], re.compile(r"^[0-9A-F]{8}$"))
        self.assertIn(vol["drive_type"], (2, 3))
        self.assertGreater(vol["size"], 0)
        self.assertEqual(set(vol), {"drive", "root", "label", "serial", "fs", "drive_type",
                                    "is_system", "hotplug", "size"})
        info = winfs.volume_info(vol["root"])
        self.assertEqual(info, {"label": vol["label"], "serial": vol["serial"], "fs": vol["fs"]})
        self.assertEqual(winfs.volume_size(vol["drive"]), vol["size"])

    def test_local_shares_and_mapped_drives_shapes(self):
        for share in winfs.local_shares():
            self.assertEqual(set(share), {"name", "path"})
            self.assertFalse(share["name"].endswith("$"))
        for drive, target in winfs.mapped_drives().items():
            self.assertRegex(drive, r"^[A-Z]:$")
            self.assertTrue(target.startswith("\\\\"))


if __name__ == "__main__":
    unittest.main()
