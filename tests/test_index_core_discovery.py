"""Unit tests for projektsog.discovery: keys, heuristics, candidates and the probe.

Fake volumes are temporary directories; nothing outside them is touched.
"""

import _winapi
import ctypes
import os
import tempfile
import threading
import unittest
from ctypes import wintypes
from unittest import mock

from projektsog import discovery, winfs
from projektsog.config import DEFAULTS, Config
from projektsog.pathmap import PathMap, clean_path

_tmp: tempfile.TemporaryDirectory | None = None
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.SetFileAttributesW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
_kernel32.SetFileAttributesW.restype = wintypes.BOOL
_HIDDEN, _SYSTEM = 0x2, 0x4


def setUpModule() -> None:
    global _tmp
    _tmp = tempfile.TemporaryDirectory()
    os.environ["LOCALAPPDATA"] = _tmp.name


def tearDownModule() -> None:
    if _tmp is not None:
        _tmp.cleanup()


def mkdirs(root: str, *rels: str) -> None:
    for rel in rels:
        os.makedirs(os.path.join(root, rel), exist_ok=True)


def touch(root: str, *rels: str) -> None:
    for rel in rels:
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb"):
            pass


def set_attributes(path: str, attributes: int) -> None:
    if not _kernel32.SetFileAttributesW(path, attributes):
        raise ctypes.WinError(ctypes.get_last_error())


def volume(root: str, **overrides) -> dict:
    vol = {"drive": "T:", "root": root, "label": "Testdisk", "serial": "1234abcd", "fs": "NTFS",
           "drive_type": 3, "is_system": False, "hotplug": False, "size": 1000}
    vol.update(overrides)
    return vol


class TempDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = tmp.name
        self.root = os.path.join(self.base, "vol")
        os.makedirs(self.root)
        self.cfg = Config(path=os.path.join(self.base, "config.json"))

    def path(self, *rel: str) -> str:
        return clean_path(os.path.join(self.root, *rel))


# --------------------------------------------------------------------------------------
# Keys and heuristics
# --------------------------------------------------------------------------------------

class KeyTests(unittest.TestCase):
    def test_key_local(self):
        self.assertEqual(discovery.key_local("5e3a0b21", "\\2024 Disk Sølv"),
                         "vol:5E3A0B21:\\2024 Disk Sølv")
        self.assertEqual(discovery.key_local("5E3A0B21", "2024 Disk Sølv/Sub/"),
                         "vol:5E3A0B21:\\2024 Disk Sølv\\Sub")
        self.assertEqual(discovery.key_local("5E3A0B21", ""), "vol:5E3A0B21:\\")
        self.assertEqual(discovery.key_local("5E3A0B21", "\\"), "vol:5E3A0B21:\\")

    def test_key_share(self):
        self.assertEqual(discovery.key_share("grafik-pc", "Forår 2026 (HDD)"),
                         "unc:GRAFIK-PC\\Forår 2026 (HDD)")
        self.assertEqual(discovery.key_share("\\\\grafik-pc", "Users", "Mette/Videos"),
                         "unc:GRAFIK-PC\\Users\\Mette\\Videos")
        self.assertEqual(discovery.key_share("h", "s", "\\"), "unc:H\\s")


class HeuristicTests(unittest.TestCase):
    def test_looks_like_project(self):
        cfg = dict(DEFAULTS)
        self.assertTrue(discovery.looks_like_project(["Klip", "grafik", "Andet"], cfg))
        self.assertTrue(discovery.looks_like_project(iter(["FINAL", "Speak"]), cfg))
        self.assertFalse(discovery.looks_like_project(["Klip"], cfg))
        self.assertFalse(discovery.looks_like_project(["Klip", "KLIP"], cfg))  # distinct names
        self.assertFalse(discovery.looks_like_project([], cfg))
        # Names from macOS may be decomposed (NFD).
        self.assertTrue(discovery.looks_like_project(["Ra\u030amateriale", "Klip"], cfg))
        cfg["project_min_template_dirs"] = 3
        self.assertFalse(discovery.looks_like_project(["Klip", "Grafik"], cfg))
        self.assertTrue(discovery.looks_like_project(["Klip", "Grafik", "Logo"], cfg))

    def test_is_template_name(self):
        cfg = dict(DEFAULTS)
        for name in ("1. KUNDENAVN", "12 Kundenavn", "3_kundenavn"):
            self.assertTrue(discovery.is_template_name(name, cfg), name)
        for name in ("Kundenavn", "1. KUNDENAVN kopi", "Rikke Lindholm"):
            self.assertFalse(discovery.is_template_name(name, cfg), name)
        cfg["template_folder_regex"] = "(("
        with self.assertLogs("projektsog.discovery", "WARNING"):
            self.assertFalse(discovery.is_template_name("1. KUNDENAVN", cfg))

    def test_partial_settings_fall_back_to_defaults(self):
        self.assertTrue(discovery.looks_like_project(["Klip", "Final"], {}))
        self.assertTrue(discovery.is_template_name("1. KUNDENAVN", {"template_folder_regex": None}))


# --------------------------------------------------------------------------------------
# Local candidates
# --------------------------------------------------------------------------------------

class LocalCandidateTests(TempDirTestCase):
    def candidates(self, *vols: dict, shares=()) -> list[dict]:
        return discovery.local_candidates(self.cfg, list(vols), list(shares), "testhost")

    def build_layout(self) -> list[dict]:
        mkdirs(self.root, r"Kunder 2026\Rikke Lindholm\Klip", r"Kunder 2026\Rikke Lindholm\Grafik",
               r"Github\repo\src", r"Users\Mette\Videos\Projekter\P\Klip", "Windows\\System32",
               "$RECYCLE.BIN", ".Trashes", "HiddenSys", "Hidden")
        touch(self.root, "readme.txt")
        set_attributes(os.path.join(self.root, "$RECYCLE.BIN"), _HIDDEN | _SYSTEM)
        set_attributes(os.path.join(self.root, "HiddenSys"), _HIDDEN | _SYSTEM)
        set_attributes(os.path.join(self.root, "Hidden"), _HIDDEN)
        _winapi.CreateJunction(os.path.join(self.root, "Kunder 2026"),
                               os.path.join(self.root, "Link"))
        other = os.path.join(self.base, "other")
        mkdirs(other, "x")
        return [
            {"name": "Kunder 2026", "path": os.path.join(self.root, "Kunder 2026")},
            {"name": "Projekter", "path": os.path.join(self.root, r"Users\Mette\Videos\Projekter")},
            {"name": "Users", "path": os.path.join(self.root, "Users")},
            {"name": "Gammel", "path": os.path.join(self.root, "Gone")},
            {"name": "Deep", "path": os.path.join(self.root, r"Github\repo")},
            {"name": "Whole", "path": self.root + "\\"},
            {"name": "Elsewhere", "path": os.path.join(other, "x")},
        ]

    def test_top_level_dirs_and_shares(self):
        shares = self.build_layout()
        cands = self.candidates(volume(self.root), shares=shares)
        by_path = {c["path"]: c for c in cands}
        # Hidden (and hidden+system) top-level folders are no candidates (known issue 1).
        self.assertEqual(sorted(by_path), sorted([
            self.path("Github"), self.path("Kunder 2026"),
            self.path(r"Users\Mette\Videos\Projekter")]))
        kunder = by_path[self.path("Kunder 2026")]
        self.assertEqual(kunder, {
            "key": "vol:1234ABCD:\\Kunder 2026", "kind": "local", "path": self.path("Kunder 2026"),
            "unc_path": "\\\\TESTHOST\\Kunder 2026", "host": "TESTHOST", "share": None,
            "volume_serial": "1234ABCD", "volume_label": "Testdisk", "fs": "NTFS",
            "display_name": "Kunder 2026", "drive": "T:", "hotplug": False, "volume_size": 1000,
            "manual": False})
        deep_share = by_path[self.path(r"Users\Mette\Videos\Projekter")]
        self.assertEqual(deep_share["key"], "vol:1234ABCD:\\Users\\Mette\\Videos\\Projekter")
        self.assertEqual(deep_share["display_name"], "Projekter")
        self.assertEqual(deep_share["unc_path"], "\\\\TESTHOST\\Projekter")
        # Inside the whole-volume share only.
        self.assertEqual(by_path[self.path("Github")]["unc_path"], "\\\\TESTHOST\\Whole\\Github")

    def test_nested_candidates_keep_the_outermost(self):
        mkdirs(self.root, r"Media\Kunder 2027\P\Klip")
        shares = [{"name": "Kunder 2027", "path": os.path.join(self.root, r"Media\Kunder 2027")}]
        cands = self.candidates(volume(self.root), shares=shares)
        self.assertEqual([c["path"] for c in cands], [self.path("Media")])
        self.assertEqual(cands[0]["unc_path"], None)

    def test_skip_top_level_dirs_setting(self):
        mkdirs(self.root, "Keep", "Program Files", "tmp", "Temp")
        self.assertEqual([c["display_name"] for c in self.candidates(volume(self.root))], ["Keep"])
        self.cfg.update({"skip_top_level_dirs": ["keep"]})
        names = [c["display_name"] for c in self.candidates(volume(self.root))]
        self.assertEqual(names, ["Program Files", "Temp", "tmp"])

    def test_whole_volume_when_root_contains_a_project(self):
        mkdirs(self.root, r"Rikke Lindholm\Klip", r"Rikke Lindholm\Grafik", r"Stuff\x")
        cands = self.candidates(volume(self.root))
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]["key"], "vol:1234ABCD:\\")
        self.assertEqual(cands[0]["path"], clean_path(self.root))
        self.assertEqual(cands[0]["display_name"], "Testdisk")
        unnamed = self.candidates(volume(self.root, label=""))
        self.assertEqual(unnamed[0]["display_name"], "T: (uden navn)")

    def test_whole_volume_when_root_holds_template_or_is_a_project(self):
        mkdirs(self.root, r"1. KUNDENAVN\Klip", "Stuff")
        self.assertEqual([c["key"] for c in self.candidates(volume(self.root))],
                         ["vol:1234ABCD:\\"])
        other = os.path.join(self.base, "card")
        mkdirs(other, "Klip", "Final", "Lyd")
        self.assertEqual([c["key"] for c in self.candidates(volume(other, serial="00000001"))],
                         ["vol:00000001:\\"])

    def test_whole_volume_on_hotplug_root_with_media(self):
        touch(self.root, "A001.MXF")
        mkdirs(self.root, "Other")
        self.assertEqual([c["key"] for c in self.candidates(volume(self.root, hotplug=True))],
                         ["vol:1234ABCD:\\"])
        self.assertEqual([c["display_name"] for c in self.candidates(volume(self.root))], ["Other"])

    def test_apple_double_files_are_not_media(self):
        touch(self.root, "._A001.MOV")
        mkdirs(self.root, "Other")
        cands = self.candidates(volume(self.root, hotplug=True))
        self.assertEqual([c["display_name"] for c in cands], ["Other"])

    def test_system_volume_is_never_a_whole_volume_candidate(self):
        mkdirs(self.root, r"Rikke Lindholm\Klip", r"Rikke Lindholm\Grafik", "Windows")
        cands = self.candidates(volume(self.root, is_system=True))
        self.assertEqual([c["display_name"] for c in cands], ["Rikke Lindholm"])

    def test_skip_volume_labels(self):
        mkdirs(self.root, "Folder")
        self.assertEqual(self.candidates(volume(self.root, label="google drive")), [])

    def test_unlistable_volume_gives_no_candidates(self):
        missing = os.path.join(self.base, "missing")
        with self.assertLogs("projektsog.discovery", "WARNING"):
            self.assertEqual(self.candidates(volume(missing)), [])

    def test_slow_volume_reuses_its_last_listing(self):
        mkdirs(self.root, "A", "B")
        vol = volume(self.root, serial="5EED0001")
        first = self.candidates(vol)
        self.assertEqual([c["display_name"] for c in first], ["A", "B"])
        with mock.patch.object(winfs, "call_many_with_timeout",
                               side_effect=lambda calls, timeout: [("timeout", None)] * len(calls)):
            self.assertEqual(self.candidates(vol), first)
            with self.assertLogs("projektsog.discovery", "WARNING"):
                self.assertEqual(self.candidates(volume(self.root, serial="5EED0002")), [])

    def test_hidden_top_level_folders_are_skipped_unless_shared(self):
        mkdirs(self.root, "Visible", "Hidden", "Shared hidden")
        set_attributes(os.path.join(self.root, "Hidden"), _HIDDEN)
        set_attributes(os.path.join(self.root, "Shared hidden"), _HIDDEN)
        shares = [{"name": "Delt", "path": os.path.join(self.root, "Shared hidden")}]
        cands = self.candidates(volume(self.root), shares=shares)
        self.assertEqual([c["display_name"] for c in cands], ["Shared hidden", "Visible"])

    def test_a_hidden_folder_that_already_is_a_source_stays_a_candidate(self):
        # R2-IDX-1: registered before hidden folders stopped being candidates (SPEC §15.8) -
        # dropping it would leave an offline ghost on a connected disk.
        mkdirs(self.root, "Visible", "Skjult", "Nyt skjult", r"Hidden sys\x")
        for name in ("Skjult", "Nyt skjult"):
            set_attributes(os.path.join(self.root, name), _HIDDEN)
        set_attributes(os.path.join(self.root, "Hidden sys"), _HIDDEN | _SYSTEM)
        known = ["VOL:1234abcd:\\skjult", "vol:1234ABCD:\\Hidden sys", "vol:FFFF0000:\\Nyt skjult"]
        cands = discovery.local_candidates(self.cfg, [volume(self.root)], [], "testhost",
                                           known_keys=known)
        self.assertEqual([(c["key"], c["display_name"]) for c in cands],
                         [("vol:1234ABCD:\\Skjult", "Skjult"),
                          ("vol:1234ABCD:\\Visible", "Visible")])
        with mock.patch.object(winfs, "call_many_with_timeout",       # the cached listing
                               side_effect=lambda calls, timeout: [("busy", None)] * len(calls)):
            again = discovery.local_candidates(self.cfg, [volume(self.root)], [], "testhost",
                                               known_keys=known)
        self.assertEqual(again, cands)
        self.assertEqual([c["display_name"] for c in self.candidates(volume(self.root))],
                         ["Visible"])

    def test_stale_volume_is_not_listed(self):
        mkdirs(self.root, "A")
        vol = volume(self.root, serial="5A1E0001")
        self.assertEqual([c["display_name"] for c in self.candidates(vol)], ["A"])
        mkdirs(self.root, "B")          # another medium may sit at the letter now
        with mock.patch.object(discovery, "_list_dir", side_effect=AssertionError("listed")):
            self.assertEqual(self.candidates({**vol, "stale": True}), [])
        self.assertEqual([c["display_name"] for c in self.candidates(vol)], ["A", "B"])

    def test_known_layout_is_kept(self):
        mkdirs(self.root, r"Kunder\P\Klip", r"Kunder\P\Grafik", "Cache")
        touch(self.root, "Spot.mp4")
        hot = volume(self.root, hotplug=True, serial="1A70A001")
        keys = lambda cands: [c["key"] for c in cands]           # noqa: E731
        self.assertEqual(keys(self.candidates(hot)), ["vol:1A70A001:\\"])   # first sighting

        def with_layout(layout: str) -> list[dict]:
            return discovery.local_candidates(self.cfg, [hot], [], "testhost",
                                              layouts={"1a70a001": layout})
        folders = with_layout(discovery.LAYOUT_FOLDERS)
        self.assertEqual(keys(folders), ["vol:1A70A001:\\Cache", "vol:1A70A001:\\Kunder"])
        mkdirs(self.root, r"Rikke\Klip", r"Rikke\Grafik")       # a project right in the root
        self.assertEqual(keys(with_layout(discovery.LAYOUT_FOLDERS)),
                         ["vol:1A70A001:\\Cache", "vol:1A70A001:\\Kunder",
                          "vol:1A70A001:\\Rikke"])
        os.remove(os.path.join(self.root, "Spot.mp4"))
        plain = volume(self.root, serial="1A70A001")         # nothing makes it whole now ...
        whole = discovery.local_candidates(self.cfg, [plain], [], "testhost",
                                           layouts={"1A70A001": discovery.LAYOUT_WHOLE})
        self.assertEqual(keys(whole), ["vol:1A70A001:\\"])    # ... but it stays whole
        system = discovery.local_candidates(self.cfg, [volume(self.root, is_system=True)], [],
                                            "testhost", layouts={"1234ABCD": "whole"})
        self.assertNotIn("vol:1234ABCD:\\", keys(system))     # never the system volume

    def test_a_known_layout_needs_only_the_root_listing(self):
        mkdirs(self.root, r"A\x", r"B\y", r"C\z")
        vol = volume(self.root, serial="1A70A002")
        with mock.patch.object(discovery, "_list_dir", wraps=discovery._list_dir) as spy:
            self.candidates(vol)
            first = spy.call_count
            discovery.local_candidates(self.cfg, [vol], [], "testhost",
                                       layouts={"1A70A002": discovery.LAYOUT_FOLDERS})
        self.assertEqual((first, spy.call_count - first), (4, 1))

    def test_duplicate_volume_serial_is_skipped(self):
        clone = os.path.join(self.base, "clone")
        mkdirs(self.root, "Projekter")
        mkdirs(clone, "Projekter")
        with self.assertLogs("projektsog.discovery", "WARNING"):
            cands = self.candidates(volume(self.root), volume(clone, drive="U:"))
        self.assertEqual([c["drive"] for c in cands], ["T:"])


# --------------------------------------------------------------------------------------
# Remote, mapped and extra-root candidates
# --------------------------------------------------------------------------------------

class OtherCandidateTests(TempDirTestCase):
    def test_remote_candidates(self):
        cands = discovery.remote_candidates("grafik-pc", ["Forår 2026 (HDD)", "Users", "C$",
                                                          "forår 2026 (hdd)", ""])
        self.assertEqual([c["key"] for c in cands],
                         ["unc:GRAFIK-PC\\Forår 2026 (HDD)", "unc:GRAFIK-PC\\Users"])
        self.assertEqual(cands[0], {
            "key": "unc:GRAFIK-PC\\Forår 2026 (HDD)", "kind": "share",
            "path": "\\\\GRAFIK-PC\\Forår 2026 (HDD)",
            "unc_path": "\\\\GRAFIK-PC\\Forår 2026 (HDD)",
            "host": "GRAFIK-PC", "share": "Forår 2026 (HDD)", "volume_serial": None,
            "volume_label": None, "fs": None, "display_name": "Forår 2026 (HDD)", "drive": None,
            "hotplug": False, "volume_size": None, "manual": False})

    def test_mapped_candidates_share_keys_with_host_shares(self):
        pathmap = PathMap("TESTHOST")
        pathmap.update([{"name": "Lokal", "path": r"C:\Lokal"}], {},
                       {"GRAFIK-PC": ["192.0.2.53"]}, ["10.0.0.5"])
        mapped = {"V:": "\\\\grafik-pc\\Forår 2026 (HDD)", "W:": "\\\\192.0.2.53\\Kunder\\Sub",
                  "X:": "\\\\TESTHOST\\Lokal", "Y:": "\\\\10.0.0.5\\Other",
                  "Z:": "\\\\grafik-pc\\forår 2026 (hdd)"}
        cands = discovery.mapped_candidates(mapped, pathmap=pathmap, own_host="testhost")
        # X: and Y: point at this computer (name / own IP); Z: duplicates V:.
        self.assertEqual([c["key"] for c in cands],
                         ["unc:GRAFIK-PC\\Forår 2026 (HDD)", "unc:GRAFIK-PC\\Kunder\\Sub"])
        remote = discovery.remote_candidates("GRAFIK-PC", ["Forår 2026 (HDD)"])
        self.assertEqual(cands[0], remote[0])
        self.assertEqual(cands[1]["display_name"], "Sub")
        self.assertEqual(cands[1]["path"], "\\\\GRAFIK-PC\\Kunder\\Sub")
        self.assertEqual(cands[1]["share"], "Kunder")
        # Without a PathMap own-host targets are still skipped by name.
        plain = discovery.mapped_candidates({"X:": "\\\\localhost\\x", "Q:": "\\\\h\\s"},
                                            own_host="testhost")
        self.assertEqual([c["key"] for c in plain], ["unc:H\\s"])

    def test_extra_root_candidates(self):
        mkdirs(self.root, r"Projekter\2027")
        extra = [os.path.join(self.root, "Projekter", "2027") + "\\", "\\\\nas\\Video\\Arkiv",
                 "\\\\nas\\video\\arkiv", r"Q:\Not mounted", "relative\\path", "\\\\nas"]
        self.cfg.update({"extra_roots": extra})
        pathmap = PathMap("TESTHOST")
        pathmap.update([{"name": "Projekter", "path": os.path.join(self.root, "Projekter")}],
                       {}, {}, [])
        cands = discovery.extra_root_candidates(self.cfg, [volume(self.root)], "testhost",
                                                pathmap=pathmap)
        self.assertEqual(len(cands), 2)
        local, share = cands
        self.assertEqual(local["key"], "vol:1234ABCD:\\Projekter\\2027")
        self.assertEqual(local["path"], self.path(r"Projekter\2027"))
        self.assertEqual(local["unc_path"], "\\\\TESTHOST\\Projekter\\2027")
        self.assertEqual(local["display_name"], "2027")
        self.assertTrue(local["manual"] and share["manual"])
        self.assertEqual(share["key"], "unc:NAS\\Video\\Arkiv")
        self.assertEqual(share["kind"], "share")
        self.assertEqual(share["display_name"], "Arkiv")
        self.assertEqual(share["share"], "Video")

    def test_extra_root_on_volume_root_and_own_share(self):
        self.cfg.update({"extra_roots": [self.root, "\\\\localhost\\Projekter\\A"]})
        mkdirs(self.root, r"Projekter\A")
        pathmap = PathMap("TESTHOST")
        pathmap.update([{"name": "Projekter", "path": os.path.join(self.root, "Projekter")}],
                       {}, {}, [])
        cands = discovery.extra_root_candidates(self.cfg, [volume(self.root)], "TESTHOST",
                                                pathmap=pathmap)
        self.assertEqual([c["key"] for c in cands],
                         ["vol:1234ABCD:\\", "vol:1234ABCD:\\Projekter\\A"])
        self.assertEqual(cands[0]["display_name"], "Testdisk")


# --------------------------------------------------------------------------------------
# Probe
# --------------------------------------------------------------------------------------

class ProbeTests(TempDirTestCase):
    def probe(self, *rel: str, **kwargs) -> tuple[bool, str, int]:
        return discovery.probe(os.path.join(self.root, *rel), self.cfg, **kwargs)

    def test_folder_itself_is_a_project(self):
        mkdirs(self.root, "Klip", "Grafik", "Andet")
        self.assertEqual(self.probe(), (True, "Mappen er selv et projekt", 1))
        mkdirs(self.root, "1. KUNDENAVN")
        self.assertEqual(self.probe("1. KUNDENAVN"), (True, "Mappen er selv et projekt", 1))

    def test_a_folder_named_like_a_template_part_is_no_project(self):
        # R2-IDX-6 / SPEC §15.12: a shared "Klip" folder is project material, not a project.
        mkdirs(self.root, r"Klip\Råmateriale", r"Klip\Stills", r"Kunde X\Råmateriale",
               r"Kunde X\Stills")
        self.assertEqual(self.probe("Klip"), (True, "Mappen er en del af et projekt", 0))
        self.assertEqual(self.probe("Kunde X"), (True, "Mappen er selv et projekt", 1))
        self.assertTrue(discovery.is_project_part("KLIP", self.cfg))
        self.assertTrue(discovery.is_project_part("Råmateriale", self.cfg))   # NFD
        self.assertFalse(discovery.is_project_part("Kunde X", self.cfg))

    def test_child_project(self):
        mkdirs(self.root, r"P1\Klip", r"P1\Final", r"Loose\Stuff")
        self.assertEqual(self.probe(), (True, "1 projektmappe fundet", 1))

    def test_grandchild_projects_in_group_folder(self):
        mkdirs(self.root, r"Klar Tand 2026\Silkeborg\Klip", r"Klar Tand 2026\Silkeborg\Speak",
               r"Klar Tand 2026\Vejle\Musik", r"Klar Tand 2026\Vejle\Tekst",
               r"Klar Tand 2026\Vejle\Klip\Sub")
        details = discovery.probe_details(self.root, self.cfg)
        self.assertEqual(details.as_tuple(), (True, "2 projektmapper fundet", 2))
        self.assertEqual(details.projects, [r"Klar Tand 2026\Silkeborg", r"Klar Tand 2026\Vejle"])

    def test_deeper_projects_are_not_found(self):
        mkdirs(self.root, r"A\B\C\Klip", r"A\B\C\Final")
        self.assertEqual(self.probe(), (False, "Ingen projektmapper fundet", 0))

    def test_one_template_dir_is_not_enough(self):
        mkdirs(self.root, r"P\Klip", r"P\Diverse")
        self.assertEqual(self.probe(), (False, "Ingen projektmapper fundet", 0))

    def test_template_folder(self):
        mkdirs(self.root, r"1. KUNDENAVN\Klip", r"1. KUNDENAVN\Final", "Loose")
        details = discovery.probe_details(self.root, self.cfg)
        self.assertEqual(details.as_tuple(), (True, "Projektskabelon fundet", 0))
        self.assertEqual(details.templates, ["1. KUNDENAVN"])
        mkdirs(self.root, r"Rikke\Klip", r"Rikke\Logo")
        self.assertEqual(self.probe(), (True, "1 projektmappe fundet", 1))

    def test_budget_limit(self):
        mkdirs(self.root, r"A\x", r"B\x", r"C\x", r"D\x", r"E\P\Klip", r"E\P\Final")
        details = discovery.probe_details(self.root, self.cfg, max_listings=3)
        self.assertEqual(details.as_tuple(), (False, "Ingen projektmapper fundet", 0))
        self.assertTrue(details.exhausted)
        self.assertEqual(details.listings, 3)
        self.assertEqual(self.probe(max_listings=400), (True, "1 projektmappe fundet", 1))

    def test_hotplug_media_rule(self):
        touch(self.root, r"Clips\A001.MXF")
        self.assertEqual(self.probe(), (False, "Ingen projektmapper fundet", 0))
        self.assertEqual(self.probe(hotplug=True), (True, "Mediefiler fundet", 0))

    def test_hotplug_media_rule_ignores_skipped_files(self):
        touch(self.root, r"Clips\._A001.MOV", r"Clips\notes.txt")
        self.assertEqual(self.probe(hotplug=True), (False, "Ingen projektmapper fundet", 0))

    def test_hotplug_media_search_reaches_camera_card_layouts(self):
        touch(self.root, r"PRIVATE\AVCHD\BDMV\STREAM\00001.MTS")
        self.assertEqual(self.probe("PRIVATE", hotplug=True), (True, "Mediefiler fundet", 0))
        self.assertEqual(self.probe("PRIVATE"), (False, "Ingen projektmapper fundet", 0))

    def test_resolve_cache_is_not_footage(self):
        mkdirs(self.root, r"Smid Cache her\.gallery", r"Smid Cache her\ProxyMedia")
        touch(self.root, r"Ferie\Afsnit 2\bounce.wav")
        details = discovery.probe_details(self.root, self.cfg, hotplug=True)
        self.assertEqual(details.as_tuple(),
                         (False, "Ingen projektmapper fundet (DaVinci Resolve-cache)", 0))
        self.assertEqual(details.cache_dir, r"Smid Cache her\.gallery")

    def test_links_hidden_system_and_excluded_dirs_are_ignored(self):
        mkdirs(self.root, r"target\x", r"P\Final", r"HS\Klip", r"HS\Final",
               r"node_modules\Q\Klip", r"node_modules\Q\Final")
        _winapi.CreateJunction(os.path.join(self.root, "target"),
                               os.path.join(self.root, r"P\Klip"))
        set_attributes(os.path.join(self.root, "HS"), _HIDDEN | _SYSTEM)
        self.assertEqual(self.probe(), (False, "Ingen projektmapper fundet", 0))

    def test_missing_path(self):
        self.assertEqual(self.probe("does not exist"), (False, "Mappen findes ikke", 0))

    def test_unreadable_path(self):
        denied = OSError(None, "Adgang nægtet", None, 5)
        with mock.patch.object(discovery, "_list_dir", side_effect=denied):
            self.assertEqual(self.probe(), (False, "Ingen adgang", 0))
        unreachable = OSError(None, "Netværksstien blev ikke fundet", None, 53)
        with mock.patch.object(discovery, "_list_dir", side_effect=unreachable):
            self.assertEqual(self.probe(), (False, "Svarer ikke", 0))

    def test_hanging_listing_answers_svarer_ikke(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def hang(path: str):
            release.wait(10)
            raise OSError(None, "late", None, 121)

        with mock.patch.object(discovery, "_list_dir", side_effect=hang), \
                mock.patch.object(discovery, "_PROBE_GRACE", 0.05):
            self.assertEqual(self.probe(timeout=0.1), (False, "Svarer ikke", 0))
            # The stuck probe keeps its key busy: a second probe answers at once.
            self.assertEqual(self.probe(timeout=5), (False, "Svarer ikke", 0))


if __name__ == "__main__":
    unittest.main()
