"""projektsog.relink: where did the offline clips go? (SPEC §22.2) - pure ranking, and the
extended ``Indexer.find_files`` rows it is made from (real Indexer, temp dirs only)."""

from __future__ import annotations

import os
import unittest
from typing import Any

from projektsog.relink import MAX_ALTERNATIVES, file_name, make_plan
from tests._index_engine_fixtures import EngineTestCase, forbid_fs_calls, module_env, project
from tests._resolve_fakes import indexed_file

_env: Any = None

DISK = "H:\\2024 Disk Sølv"
OLD = DISK + "\\Pixelbro Radio\\Klip"
ARKIV = "\\\\MEDIESERVER\\2026Arkiv"
NEW = ARKIV + "\\Pixelbro Radio\\Klip"
STUDIO = "C:\\Kunder 2026 (STUDIO)"
STUDIO_UNC = "\\\\STUDIO-PC\\Kunder 2026 (STUDIO)"


def setUpModule() -> None:
    global _env
    _env = module_env()


def tearDownModule() -> None:
    _env.cleanup()


def clip(path: str, uid: str | None = None, name: str | None = None) -> dict[str, Any]:
    return {"uid": uid or "u-" + file_name(path), "name": name or file_name(path), "path": path,
            "dir": path.rsplit("\\", 1)[0], "type": "Video", "frames": "250", "fps": 25.0,
            "resolution": "1920x1080", "status": "Offline"}


def files_in(folder: str, *names: str, **kwargs: Any) -> list[dict[str, Any]]:
    root = kwargs.pop("root", folder)
    return [indexed_file(folder + "\\" + n, root=root, **kwargs) for n in names]


class RankingTests(unittest.TestCase):
    def test_one_folder_holds_everything(self) -> None:
        groups, not_found = make_plan([clip(OLD + "\\a.mov"), clip(OLD + "\\b.mov")],
                                      files_in(NEW, "a.mov", "B.MOV", root=ARKIV, sid=7))
        self.assertEqual(groups, [{
            "from": OLD, "to": NEW, "to_display": NEW, "online": True,
            "clips": [{"uid": "u-a.mov", "name": "a.mov", "old_path": OLD + "\\a.mov"},
                      {"uid": "u-b.mov", "name": "b.mov", "old_path": OLD + "\\b.mov"}],
            "auto": True, "alternatives": []}])
        self.assertEqual(not_found, [])

    def test_two_equal_copies_are_a_choice(self) -> None:
        other = "\\\\KLIPPER-PC\\Backup\\Pixelbro Radio\\Klip"
        files = (files_in(NEW, "a.mov", root=ARKIV, sid=7)
                 + files_in(other, "a.mov", root="\\\\KLIPPER-PC\\Backup", sid=8))
        (group,), _ = make_plan([clip(OLD + "\\a.mov")], files)
        self.assertFalse(group["auto"])
        self.assertEqual(sorted(a["to"] for a in group["alternatives"]), sorted([NEW, other]))
        self.assertEqual(group["to"], group["alternatives"][0]["to"])
        self.assertEqual({a["holds"] for a in group["alternatives"]}, {1})

    def test_a_folder_holding_only_some_names_is_no_sure_thing(self) -> None:
        clips = [clip(OLD + "\\a.mov"), clip(OLD + "\\b.mov"), clip(OLD + "\\c.mov")]
        files = (files_in(NEW, "a.mov", "b.mov", root=ARKIV, sid=7)
                 + files_in("D:\\Andet\\Klip", "c.mov", root="D:\\Andet", sid=2))
        (group,), not_found = make_plan(clips, files)
        self.assertEqual((group["to"], group["auto"]), (NEW, False))
        self.assertEqual([(a["to"], a["holds"]) for a in group["alternatives"]],
                         [(NEW, 2), ("D:\\Andet\\Klip", 1)])
        self.assertEqual(len(group["clips"]), 3)
        self.assertEqual(not_found, [])

    def test_clips_found_nowhere(self) -> None:
        groups, not_found = make_plan(
            [clip(OLD + "\\a.mov"), clip(DISK + "\\Pixelbro Radio\\Musik\\x.wav", uid="u-x"),
             clip(OLD + "\\z.mov")],
            files_in(NEW, "a.mov", root=ARKIV, sid=7))
        self.assertEqual([c["uid"] for c in groups[0]["clips"]], ["u-a.mov"])
        self.assertEqual(not_found, [                    # by old path
            {"uid": "u-z.mov", "name": "z.mov", "old_path": OLD + "\\z.mov"},
            {"uid": "u-x", "name": "x.wav", "old_path": DISK + "\\Pixelbro Radio\\Musik\\x.wav"}])

    def test_shared_trailing_path_parts_win(self) -> None:
        files = (files_in("D:\\Andet projekt\\Klip", "a.mov", root="D:\\", sid=2)
                 + files_in(NEW, "a.mov", root=ARKIV, sid=7))
        (group,), _ = make_plan([clip(OLD + "\\a.mov")], files)
        self.assertEqual((group["to"], group["auto"]), (NEW, True))

    def test_the_same_project_name_wins(self) -> None:
        files = (files_in("D:\\Kunder\\Rikke Lindholm\\Musik", "song.wav", root="D:\\Kunder", sid=2,
                          project="Rikke Lindholm")
                 + files_in("D:\\Kunder\\Andet\\Musik", "song.wav", root="D:\\Kunder", sid=2,
                            project="Andet"))
        (group,), _ = make_plan([clip("E:\\Gammel\\Musik\\song.wav")], files,
                                project="Rikke Lindholm - Testimonial")
        self.assertEqual((group["to"], group["auto"]), ("D:\\Kunder\\Rikke Lindholm\\Musik", True))

    def test_unc_when_the_old_path_was_unc(self) -> None:
        old = STUDIO_UNC.lower() + "\\Gammel\\Klip"
        local = STUDIO + "\\Rikke Lindholm\\Klip"
        (group,), _ = make_plan([clip(old + "\\a.mov")],
                                files_in(local, "a.mov", root=STUDIO, unc_root=STUDIO_UNC, sid=1))
        self.assertEqual((group["to"], group["to_display"]), (STUDIO_UNC + "\\Rikke Lindholm\\Klip", local))
        (group,), _ = make_plan([clip("D:\\Gammel\\Klip\\a.mov")],
                                files_in(local, "a.mov", root=STUDIO, unc_root=STUDIO_UNC, sid=1))
        self.assertEqual(group["to"], local, "a drive-letter path stays a drive-letter path")
        # Two otherwise equal folders: the one reachable as UNC wins.
        files = (files_in("D:\\Kopi\\Klip", "a.mov", root="D:\\Kopi", sid=2)
                 + files_in("\\\\NAS\\Kopi\\Klip", "a.mov", root="\\\\NAS\\Kopi", sid=3))
        (group,), _ = make_plan([clip("\\\\gammel\\x\\Klip\\a.mov")], files)
        self.assertEqual((group["to"], group["auto"]), ("\\\\NAS\\Kopi\\Klip", True))

    def test_online_and_not_hot_plug_wins(self) -> None:
        files = (files_in("E:\\Kopi\\Klip", "a.mov", root="E:\\Kopi", sid=4)
                 + files_in("D:\\Kopi\\Klip", "a.mov", root="D:\\Kopi", sid=5))
        sources = [{"id": 4, "hotplug": True}, {"id": 5, "hotplug": False}]
        (group,), _ = make_plan([clip("F:\\Kopi\\Klip\\a.mov")], files, sources=sources)
        self.assertEqual((group["to"], group["auto"]), ("D:\\Kopi\\Klip", True))
        offline = files_in("D:\\Kopi\\Klip", "a.mov", root="D:\\Kopi", sid=5, online=False)
        (group,), _ = make_plan([clip("F:\\Kopi\\Klip\\a.mov")], offline)
        self.assertEqual((group["online"], group["auto"]), (False, False),
                         "a folder that is not online is never a sure thing")

    def test_the_same_unplugged_disk(self) -> None:
        """The index still has the old folder (its disk is unplugged): reported as that folder
        (to == from, offline), never auto; a copy that is online is the alternative."""
        files = (files_in(OLD, "a.mov", "b.mov", root=DISK, sid=5, online=False)
                 + files_in(NEW, "a.mov", "b.mov", root=ARKIV, sid=7))
        (group,), _ = make_plan([clip(OLD.upper() + "\\a.mov"), clip(OLD + "\\b.mov")], files)
        self.assertEqual((group["to"], group["to_display"], group["online"], group["auto"]),
                         (OLD.upper(), OLD.upper(), False, False))
        self.assertEqual([(a["to"], a["online"]) for a in group["alternatives"]],
                         [(OLD.upper(), False), (NEW, True)])

    def test_groups_per_old_folder_most_clips_first(self) -> None:
        clips = [clip("H:\\A\\x.mov"), clip("H:\\B\\y.mov"), clip("H:\\B\\z.mov"),
                 {"uid": "", "path": "H:\\B\\no-uid.mov"}, {"uid": "u", "path": ""}]
        files = files_in("D:\\A", "x.mov", sid=1) + files_in("D:\\B", "y.mov", "z.mov", sid=1)
        groups, not_found = make_plan(clips, files)
        self.assertEqual([(g["from"], len(g["clips"])) for g in groups], [("H:\\B", 2), ("H:\\A", 1)])
        self.assertEqual(not_found, [])

    def test_at_most_five_alternatives(self) -> None:
        files = [f for i in range(8) for f in files_in(f"D:\\K{i}\\Klip", "a.mov", root=f"D:\\K{i}", sid=i)]
        (group,), _ = make_plan([clip("H:\\Gammel\\Klip\\a.mov")], files)
        self.assertEqual(len(group["alternatives"]), MAX_ALTERNATIVES)

    def test_slashes_and_names(self) -> None:
        self.assertEqual(file_name("H:/x/y/a.mov"), "a.mov")
        (group,), _ = make_plan([clip("H:/Gammel/Klip/a.mov")], files_in("D:\\Ny\\Klip", "a.mov", sid=1))
        self.assertEqual((group["from"], group["to"]), ("H:\\Gammel\\Klip", "D:\\Ny\\Klip"))


class FindFilesTests(EngineTestCase):
    """The extended ``Indexer.find_files`` rows (SPEC §22.2) feed the plan."""

    def setUp(self) -> None:
        super().setUp()
        root = os.path.join(self.tmp, "c")
        share = "Kunder 2026 (STUDIO)"
        self.kunder = os.path.join(root, share)
        tree = project(f"{share}\\Rikke Lindholm", "Klip\\FX9\\FX9_7912.MXF", "Klip\\FX9\\FX9_7913.MXF")
        self.world.hostname = "STUDIO-PC"
        self.world.volume("c", "1C4F9D02", drive="C:", tree=tree)
        self.world.shares = [{"name": share, "path": self.kunder}]
        self.ix = self.start()
        self.settled(self.ix)
        self.unc = f"\\\\STUDIO-PC\\{share}"

    def test_rows_and_plan(self) -> None:
        with forbid_fs_calls():
            rows = self.ix.find_files(["fx9_7912.mxf", "FX9_7913.MXF"])
        folder = os.path.join(self.kunder, "Rikke Lindholm", "Klip", "FX9")
        row = min(rows, key=lambda r: r["name"])
        self.assertEqual((row["name"], row["folder"], row["rel_path"], row["unc_folder"], row["is_seq"]),
                         ("FX9_7912.MXF", folder, "Rikke Lindholm\\Klip\\FX9\\FX9_7912.MXF",
                          f"{self.unc}\\Rikke Lindholm\\Klip\\FX9", False))
        self.assertEqual(row["source_id"], self.source(self.ix, "Kunder 2026 (STUDIO)")["id"])
        self.assertIsInstance(row["mtime"], float)
        old = "\\\\studio-pc\\Kunder 2026 (STUDIO)\\Rikke Lindholm (gammel)\\Klip\\FX9"
        (group,), not_found = make_plan(
            [clip(old + "\\FX9_7912.MXF"), clip(old + "\\FX9_7913.MXF")], rows,
            sources=self.ix.list_sources(), project="Rikke Lindholm - Testimonial")
        self.assertEqual((group["to"], group["to_display"], group["auto"], not_found),
                         (f"{self.unc}\\Rikke Lindholm\\Klip\\FX9", folder, True, []))


if __name__ == "__main__":
    unittest.main()
