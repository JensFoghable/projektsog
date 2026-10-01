"""Unit tests for projektsog.pathmap (pure string logic, no file system)."""

import os
import tempfile
import threading
import unittest

from projektsog.pathmap import (PathMap, clean_path, is_within, join_path, long_path, path_parts,
                                relative_parts, split_unc)

_tmp: tempfile.TemporaryDirectory | None = None


def setUpModule() -> None:
    global _tmp
    _tmp = tempfile.TemporaryDirectory()
    os.environ["LOCALAPPDATA"] = _tmp.name


def tearDownModule() -> None:
    if _tmp is not None:
        _tmp.cleanup()

SHARES = [
    {"name": "Kunder 2026 (STUDIO)", "path": r"C:\Kunder 2026 (STUDIO)"},
    {"name": "2024 Disk Sølv", "path": "H:\\2024 Disk Sølv\\"},
    {"name": "Kunder", "path": r"C:\Kunder"},
    {"name": "Data", "path": "D:\\"},
    {"name": "Arkiv", "path": r"D:\Arkiv"},
]
HOST_IPS = {"GRAFIK-PC": ["192.0.2.53", "fe80::1"], "klipper-pc": ["192.0.2.18"]}
OWN_IPS = ["192.0.2.99", "192.0.2.10"]


def make_map(mapped: dict[str, str] | None = None) -> PathMap:
    pm = PathMap("studio-pc")
    pm.update(SHARES, mapped or {}, HOST_IPS, OWN_IPS)
    return pm


class CleanPathTests(unittest.TestCase):
    def test_extended_length_prefixes(self):
        self.assertEqual(clean_path(r"\\?\C:\Kunder\x"), r"C:\Kunder\x")
        self.assertEqual(clean_path(r"\\?\UNC\grafik-pc\Forår 2026 (HDD)\x"),
                         r"\\GRAFIK-PC\Forår 2026 (HDD)\x")
        self.assertEqual(clean_path(r"\\?\unc\host\share"), r"\\HOST\share")
        self.assertEqual(clean_path(r"\\.\C:\x"), r"C:\x")
        self.assertEqual(clean_path(r"\??\C:\x"), r"C:\x")
        # Device paths that are not drive/UNC paths are kept.
        self.assertEqual(clean_path(r"\\?\Volume{abc}\x\\"), r"\\?\Volume{abc}\x")

    def test_slashes_duplicates_and_trailing_separators(self):
        self.assertEqual(clean_path("C:/Kunder//Rikke Lindholm/"), r"C:\Kunder\Rikke Lindholm")
        self.assertEqual(clean_path(r"\\\\host\\share\\\x\\"), r"\\HOST\share\x")
        self.assertEqual(clean_path("//host/share/"), r"\\HOST\share")
        self.assertEqual(clean_path("C:\\"), "C:\\")
        self.assertEqual(clean_path("c:"), "C:\\")
        self.assertEqual(clean_path("C:\\\\\\"), "C:\\")
        self.assertEqual(clean_path(r"relative\\dir\\"), r"relative\dir")
        self.assertEqual(clean_path(""), "")

    def test_case_and_dots(self):
        self.assertEqual(clean_path(r"h:\2024 Disk Sølv"), r"H:\2024 Disk Sølv")
        self.assertEqual(clean_path(r"\\grafik-pc\Users"), r"\\GRAFIK-PC\Users")
        self.assertEqual(clean_path(r"C:\a\.\b\..\c"), r"C:\a\c")
        self.assertEqual(clean_path(r"C:\..\a"), r"C:\a")
        self.assertEqual(clean_path(r"\\host\share\..\x"), r"\\HOST\share\x")

    def test_split_and_parts(self):
        self.assertEqual(split_unc(r"\\grafik-pc\Users\Mette\x"),
                         ("GRAFIK-PC", "Users", r"Mette\x"))
        self.assertEqual(split_unc(r"\\host"), ("HOST", "", ""))
        self.assertIsNone(split_unc(r"C:\x"))
        self.assertIsNone(split_unc(r"\\?\Volume{abc}\x"))
        self.assertEqual(path_parts(r"C:\a\b"), ["C:", "a", "b"])
        self.assertEqual(path_parts("C:\\"), ["C:"])
        self.assertEqual(path_parts(r"\\h\s\a"), [r"\\H", "s", "a"])

    def test_prefix_boundaries_are_whole_components(self):
        self.assertTrue(is_within(r"C:\Kunder\x", r"C:\Kunder"))
        self.assertTrue(is_within(r"c:\kunder", r"C:\Kunder"))
        self.assertFalse(is_within(r"C:\Kunder 2026\x", r"C:\Kunder"))
        self.assertFalse(is_within(r"C:\Kunder", r"C:\Kunder\x"))
        self.assertTrue(is_within(r"C:\x", "C:\\"))
        self.assertEqual(relative_parts(r"C:\Kunder\A\B", r"c:\KUNDER"), ["A", "B"])
        self.assertEqual(relative_parts(r"\\h\s", r"\\H\S"), [])
        self.assertIsNone(relative_parts(r"\\h\s2", r"\\h\s"))

    def test_join_and_long_path(self):
        self.assertEqual(join_path("C:\\", "a", "b"), r"C:\a\b")
        self.assertEqual(join_path(r"C:\a", ""), r"C:\a")
        self.assertEqual(long_path(r"C:\a"), r"\\?\C:\a")
        self.assertEqual(long_path("c:"), "\\\\?\\C:\\")
        self.assertEqual(long_path(r"\\host\share\x"), r"\\?\UNC\HOST\share\x")
        self.assertEqual(long_path(r"\\?\UNC\host\share"), r"\\?\UNC\HOST\share")


class PathMapTests(unittest.TestCase):
    def test_own_host_aliases_map_to_local_share_path(self):
        pm = make_map()
        expected = r"C:\Kunder 2026 (STUDIO)\Rikke Lindholm\Klip"
        for host in ("studio-pc", "STUDIO-PC", "localhost", "127.0.0.1", "192.0.2.99",
                     "192.0.2.10", "studio-pc.lan"):
            with self.subTest(host=host):
                self.assertEqual(
                    pm.normalize(rf"\\{host}\kunder 2026 (studio)\Rikke Lindholm\Klip"), expected)
        self.assertEqual(pm.normalize(r"\\?\UNC\localhost\2024 Disk Sølv\\x\\"),
                         r"H:\2024 Disk Sølv\x")
        self.assertEqual(pm.normalize(r"\\LOCALHOST\Data\Foo"), r"D:\Foo")
        self.assertEqual(pm.normalize(r"\\localhost\c$\Windows"), r"C:\Windows")
        # An unknown own share keeps the UNC form with the canonical host name.
        self.assertEqual(pm.normalize(r"\\127.0.0.1\Nope\x"), r"\\STUDIO-PC\Nope\x")
        self.assertEqual(pm.normalize(r"\\localhost"), r"\\STUDIO-PC")

    def test_ip_and_fqdn_to_host(self):
        pm = make_map()
        self.assertEqual(pm.normalize(r"\\192.0.2.53\Forår 2026 (HDD)\\Klar Tand\\"),
                         r"\\GRAFIK-PC\Forår 2026 (HDD)\Klar Tand")
        self.assertEqual(pm.normalize(r"\\192.0.2.18\Rejsefilm"), r"\\KLIPPER-PC\Rejsefilm")
        self.assertEqual(pm.normalize(r"\\grafik-pc.local\Users"), r"\\GRAFIK-PC\Users")
        self.assertEqual(pm.normalize(r"\\10.0.0.9\s\x"), r"\\10.0.0.9\s\x")
        self.assertEqual(pm.normalize(r"\\other.example.com\s"), r"\\OTHER.EXAMPLE.COM\s")

    def test_mapped_drive_to_unc(self):
        pm = make_map({"Y:": r"\\grafik-pc\Forår 2026 (HDD)", "x:": r"\\STUDIO-PC\Kunder",
                       "W:": r"\\192.0.2.18\Rejsefilm\Sub"})
        self.assertEqual(pm.normalize(r"y:\Klar Tand\a.mov"),
                         r"\\GRAFIK-PC\Forår 2026 (HDD)\Klar Tand\a.mov")
        self.assertEqual(pm.normalize("Y:\\"), r"\\GRAFIK-PC\Forår 2026 (HDD)")
        self.assertEqual(pm.normalize(r"X:\a"), r"C:\Kunder\a")
        self.assertEqual(pm.normalize(r"W:\b"), r"\\KLIPPER-PC\Rejsefilm\Sub\b")
        self.assertEqual(pm.normalize(r"Z:\(Z) Kunder"), r"Z:\(Z) Kunder")  # not mapped

    def test_local_paths_are_only_cleaned(self):
        pm = make_map()
        self.assertEqual(pm.normalize("c:/Kunder 2026 (STUDIO)//x/"), r"C:\Kunder 2026 (STUDIO)\x")
        self.assertEqual(pm.normalize(r"\\?\H:\2024 Disk Sølv"), r"H:\2024 Disk Sølv")

    def test_key_equates_spellings(self):
        pm = make_map()
        spellings = [
            r"C:\Kunder 2026 (STUDIO)\Rikke Lindholm",
            r"c:\kunder 2026 (studio)\rikke lindholm\\",
            r"\\studio-pc\Kunder 2026 (STUDIO)\Rikke Lindholm",
            r"\\?\UNC\192.0.2.99\KUNDER 2026 (STUDIO)\Rikke Lindholm",
            "C:/Kunder 2026 (STUDIO)/Rikke Lindholm/",
        ]
        self.assertEqual(len({pm.key(s) for s in spellings}), 1)
        self.assertNotEqual(pm.key(r"C:\Kunder 2026 (STUDIO)"), pm.key(r"C:\Kunder 2026"))

    def test_unc_for(self):
        pm = make_map()
        self.assertEqual(pm.unc_for(r"C:\Kunder 2026 (STUDIO)\Rikke Lindholm"),
                         r"\\STUDIO-PC\Kunder 2026 (STUDIO)\Rikke Lindholm")
        self.assertEqual(pm.unc_for(r"c:\kunder\X"), r"\\STUDIO-PC\Kunder\X")
        self.assertEqual(pm.unc_for(r"C:\Kunder"), r"\\STUDIO-PC\Kunder")
        # C:\Kunder 2026 is not inside the share C:\Kunder.
        self.assertIsNone(pm.unc_for(r"C:\Kunder 2026\x"))
        self.assertIsNone(pm.unc_for(r"C:\Users\x"))
        # The deepest share wins over a whole-drive share.
        self.assertEqual(pm.unc_for(r"D:\Arkiv\2024"), r"\\STUDIO-PC\Arkiv\2024")
        self.assertEqual(pm.unc_for(r"D:\Andet"), r"\\STUDIO-PC\Data\Andet")
        self.assertEqual(pm.unc_for(r"\\192.0.2.53\Users\x"), r"\\GRAFIK-PC\Users\x")
        self.assertIsNone(pm.unc_for(r"relative\x"))

    def test_equal_share_paths_prefer_folder_name(self):
        pm = PathMap("HOST")
        pm.update([{"name": "Alias", "path": r"C:\Projekter"},
                   {"name": "Projekter", "path": r"C:\Projekter"}], {}, {}, [])
        self.assertEqual(pm.unc_for(r"C:\Projekter\a"), r"\\HOST\Projekter\a")
        self.assertEqual(pm.normalize(r"\\host\alias\a"), r"C:\Projekter\a")

    def test_update_replaces_tables_and_own_host_in_host_ips(self):
        pm = make_map({"Y:": r"\\GRAFIK-PC\s"})
        pm.update([], {}, {"STUDIO-PC": ["10.1.1.1"], "GRAFIK-PC": ["10.1.1.2"]}, [])
        self.assertEqual(pm.normalize(r"Y:\a"), r"Y:\a")
        self.assertEqual(pm.normalize(r"\\10.1.1.1\x"), r"\\STUDIO-PC\x")
        self.assertEqual(pm.normalize(r"\\10.1.1.2\x"), r"\\GRAFIK-PC\x")
        self.assertEqual(pm.normalize(r"\\192.0.2.53\x"), r"\\192.0.2.53\x")

    def test_concurrent_update_and_normalize(self):
        pm = make_map()
        errors: list[BaseException] = []
        stop = threading.Event()

        def reader() -> None:
            try:
                while not stop.is_set():
                    result = pm.normalize(r"\\localhost\Kunder\a")
                    if result not in (r"C:\Kunder\a", r"\\STUDIO-PC\Kunder\a"):
                        raise AssertionError(result)
            except BaseException as exc:  # reported below
                errors.append(exc)

        thread = threading.Thread(target=reader)
        thread.start()
        for i in range(300):
            pm.update(SHARES if i % 2 else [], {}, HOST_IPS, OWN_IPS)
        stop.set()
        thread.join(5)
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
