"""Deep scan: sequences, kinds, excludes, aggregates, unit-wise apply, errors, incremental."""

import _winapi
import os
import shutil
import tempfile
import threading
import time
import unittest

from projektsog import scanner
from projektsog.db import (KIND_DIR, KIND_FILE, KIND_GROUP, KIND_PROJECT, KIND_TEMPLATE,
                           KIND_TOPLEVEL, Entry)
from tests._index_store_fixtures import (FILE_ATTRIBUTE_HIDDEN, FILE_ATTRIBUTE_SYSTEM,
                                         SETTLED_MTIME, CountingLister, TempIndex, cfg,
                                         make_tree, module_env, set_attributes,
                                         set_mtime, settle)

_env = None
TEMPLATE_DIRS = ["Final", "Grafik", "Klip", "Logo", "Musik", "Project", "Speak", "Tekst"]


def setUpModule():
    global _env
    _env = module_env()


def tearDownModule():
    _env.cleanup()


class ScanTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.root = os.path.join(self.tmp, "Kunder 2026 (STUDIO)")
        os.makedirs(self.root)
        self.index = TempIndex(self.tmp)
        self.addCleanup(self.index.close)
        self.sid = self.index.add_source(self.root)

    def build(self, paths, size=1):
        make_tree(self.root, paths, size=size)

    def p(self, rel):
        return os.path.join(self.root, rel)

    def rows(self):
        return self.index.rows(self.sid)


class SequenceTests(ScanTestCase):
    def test_sequences_are_collapsed_per_prefix_width_and_extension(self):
        paths = [f"shot\\render_{i:04d}.exr" for i in range(1, 26)]       # 25 frames
        paths += [f"shot\\clip_{i:04d}.mxf" for i in range(1, 31)]        # not a sequence ext
        paths += [f"shot\\frame_{i:04d}.jpg" for i in range(1, 20)]       # 19 < 20
        paths += [f"shot\\take_{i}.png" for i in range(1, 10)]            # width 1: 9 files
        paths += [f"shot\\take_{i}.png" for i in range(10, 41)]           # width 2: 31 files
        paths += [f"shot\\A001_{i:05d}.DPX" for i in range(100, 125)]     # upper-case ext
        self.build(paths, size=10)
        settle(self.root)
        result = self.index.deep(self.sid, self.root)
        rows = self.rows()

        seq = rows["shot\\render_[0001-0025].exr"]
        self.assertEqual((seq["kind"], seq["is_seq"], seq["seq_count"]), (KIND_FILE, 1, 25))
        self.assertEqual((seq["size"], seq["ext"], seq["mtime"]), (250, "exr", SETTLED_MTIME))
        self.assertNotIn("shot\\render_0001.exr", rows)
        self.assertEqual(sum(1 for r in rows if r.endswith(".mxf")), 30)
        self.assertEqual(sum(1 for r in rows if r.endswith(".jpg")), 19)
        self.assertIn("shot\\take_9.png", rows)
        self.assertEqual(rows["shot\\take_[10-40].png"]["seq_count"], 31)
        dpx = rows["shot\\A001_[00100-00124].DPX"]
        self.assertEqual((dpx["ext"], dpx["seq_count"]), ("dpx", 25))

        files = 25 + 30 + 19 + 9 + 31 + 25
        self.assertEqual(rows["shot"]["file_count"], files)
        self.assertEqual(rows["shot"]["size"], files * 10)
        self.assertEqual(result["files"], files)
        self.assertEqual(result["counts"]["file_count"], files)
        self.assertEqual(result["entries"], len(rows))

    def test_minimum_count_comes_from_config(self):
        self.build([f"f\\v_{i:03d}.tif" for i in range(1, 6)])
        settle(self.root)
        self.index.deep(self.sid, self.root, config_=cfg(sequence_min_files=5))
        self.assertIn("f\\v_[001-005].tif", self.rows())

    def test_existing_file_with_the_sequence_name_blocks_collapsing(self):
        rules = scanner.ScanRules.from_config(cfg())
        files = [scanner.FileInfo(f"a_{i:04d}.tif", 1, 1.0) for i in range(1, 21)]
        files.append(scanner.FileInfo("A_[0001-0020].tif", 1, 1.0))
        singles, sequences = scanner.group_sequences(files, rules)
        self.assertEqual(sequences, [])
        self.assertEqual(len(singles), 21)

    def test_first_frame_of_a_sequence_name(self):
        self.assertEqual(scanner.sequence_first_frame("render_[0001-4500].exr"), "render_0001.exr")
        self.assertEqual(scanner.sequence_first_frame("A001_[00100-00124].DPX"), "A001_00100.DPX")
        self.assertIsNone(scanner.sequence_first_frame("plain.exr"))

    def test_sparse_photo_numbers_stay_single_files(self):
        # IDX-6: camera stills (DSC01616, DSC01646, …) are no image sequence.
        paths = [f"Final\\Billeder\\DSC{1616 + 30 * i:05d}.jpg" for i in range(30)]
        self.build(paths)
        settle(self.root)
        self.index.deep(self.sid, self.root)
        rows = self.rows()
        self.assertFalse([r for r in rows.values() if r["is_seq"]])
        self.assertIn("Final\\Billeder\\DSC01646.jpg", rows)          # findable by its number
        self.assertEqual(sum(1 for r in rows if r.endswith(".jpg")), 30)

    def test_only_dense_runs_are_collapsed(self):
        frames = [i for i in range(1, 101) if i != 50]          # one dropped frame: one run
        frames += list(range(200, 230))                          # a second dense run
        frames += [300, 310, 320, 330, 340]                      # sparse leftovers
        frames += list(range(400, 419))                          # dense but 19 < 20
        self.build([f"shot\\render_{i:04d}.exr" for i in frames], size=10)
        settle(self.root)
        self.index.deep(self.sid, self.root)
        rows = self.rows()
        seqs = {rel.rpartition("\\")[2]: (r["seq_count"], r["size"])
                for rel, r in rows.items() if r["is_seq"]}
        self.assertEqual(seqs, {"render_[0001-0100].exr": (99, 990),
                                "render_[0200-0229].exr": (30, 300)})
        singles = sorted(rel.rpartition("\\")[2] for rel, r in rows.items()
                         if r["kind"] == KIND_FILE and not r["is_seq"])
        self.assertEqual(singles, [f"render_{i:04d}.exr" for i in [300, 310, 320, 330, 340]
                                   + list(range(400, 419))])
        self.assertEqual(rows["shot"]["file_count"], len(frames))
        self.assertEqual([scanner.sequence_first_frame(n) for n in sorted(seqs)],
                         ["render_0001.exr", "render_0200.exr"])

    def test_dense_runs_unit(self):
        rules = scanner.ScanRules.from_config(cfg(sequence_min_files=3))
        files = [scanner.FileInfo(f"f_{n:03d}.png", 1, float(n)) for n in (7, 1, 2, 4, 9, 20)]
        singles, sequences = scanner.group_sequences(files, rules)
        # 1, 2, 4: gaps ≤ 2 → one run; 4 → 7 is a gap of 3; 7, 9 is a run of only 2
        self.assertEqual([(s.name, s.count, s.mtime) for s in sequences],
                         [("f_[001-004].png", 3, 4.0)])
        self.assertEqual([f.name for f in singles], ["f_007.png", "f_009.png", "f_020.png"])


class KindTests(ScanTestCase):
    def test_kinds_and_project_rel(self):
        paths = [f"Rikke Lindholm\\{d}\\" for d in TEMPLATE_DIRS]
        paths += [f"1. KUNDENAVN\\{d}\\" for d in TEMPLATE_DIRS]
        paths += ["Klar Tand 2026\\Klar Tand - Silkeborg\\Klip\\x.mxf",
                  "Klar Tand 2026\\Klar Tand - Silkeborg\\final\\",       # case-insensitive
                  "Klar Tand 2026\\Noter\\"]
        paths += ["Sound Effects\\boom.wav", "Only Klip\\Klip\\",
                  "Arkiv\\2024\\Pixelbro\\KLIP\\", "Arkiv\\2024\\Pixelbro\\Grafik\\",
                  "Rikke Lindholm\\Arkiv\\Sub Projekt\\Final\\f.mp4",
                  "Rikke Lindholm\\Arkiv\\Sub Projekt\\Speak\\", "readme.txt"]
        self.build(paths)
        settle(self.root)
        self.index.deep(self.sid, self.root)
        rows = self.rows()
        expected = {
            "Rikke Lindholm": KIND_PROJECT, "Rikke Lindholm\\Klip": KIND_DIR,
            "Rikke Lindholm\\Arkiv": KIND_GROUP, "Rikke Lindholm\\Arkiv\\Sub Projekt": KIND_PROJECT,
            "1. KUNDENAVN": KIND_TEMPLATE, "1. KUNDENAVN\\Klip": KIND_DIR,
            "Klar Tand 2026": KIND_GROUP, "Klar Tand 2026\\Klar Tand - Silkeborg": KIND_PROJECT,
            "Klar Tand 2026\\Noter": KIND_DIR, "Sound Effects": KIND_TOPLEVEL,
            "Only Klip": KIND_TOPLEVEL, "Arkiv": KIND_TOPLEVEL, "Arkiv\\2024": KIND_GROUP,
            "Arkiv\\2024\\Pixelbro": KIND_PROJECT, "readme.txt": KIND_FILE,
            "Sound Effects\\boom.wav": KIND_FILE,
        }
        self.assertEqual({rel: rows[rel]["kind"] for rel in expected}, expected)
        project_rel = {
            "Rikke Lindholm": "Rikke Lindholm", "Rikke Lindholm\\Klip": "Rikke Lindholm",
            "Rikke Lindholm\\Arkiv": "Rikke Lindholm",
            "Rikke Lindholm\\Arkiv\\Sub Projekt\\Final\\f.mp4":
                "Rikke Lindholm\\Arkiv\\Sub Projekt",
            "1. KUNDENAVN\\Klip": None, "Klar Tand 2026": None,
            "Klar Tand 2026\\Klar Tand - Silkeborg\\Klip\\x.mxf":
                "Klar Tand 2026\\Klar Tand - Silkeborg",
            "readme.txt": None, "Sound Effects\\boom.wav": None,
        }
        self.assertEqual({rel: rows[rel]["project_rel"] for rel in project_rel}, project_rel)
        self.assertEqual(rows["Rikke Lindholm\\Klip"]["depth"], 2)
        self.assertEqual(rows["Rikke Lindholm\\Klip"]["parent_rel"], "Rikke Lindholm")
        self.assertEqual(rows["Rikke Lindholm"]["parent_rel"], "")

    def test_folders_named_like_template_folders_are_never_projects(self):
        # ux IDX-3: 'Klip' holding A7S/FX9/Råmateriale/Stills is part of the project above it.
        self.build(["Sørens Malerfirma\\Final\\", "Sørens Malerfirma\\Grafik\\",
                    "Sørens Malerfirma\\Klip\\A7S\\a.mp4", "Sørens Malerfirma\\Klip\\FX9\\",
                    "Sørens Malerfirma\\Klip\\Råmateriale\\", "Sørens Malerfirma\\Klip\\Stills\\",
                    "Lyngbakken Kro\\Klip\\", "Lyngbakken Kro\\Final\\",
                    "Lyngbakken Kro\\Musik\\Raw\\m.wav", "Lyngbakken Kro\\Musik\\Stills\\",
                    "Final\\Klip\\", "Final\\Grafik\\"])                # a depth-1 'Final'
        settle(self.root)
        self.index.deep(self.sid, self.root)
        rows = self.rows()
        kinds = {rel: rows[rel]["kind"] for rel in (
            "Sørens Malerfirma", "Sørens Malerfirma\\Klip", "Lyngbakken Kro",
            "Lyngbakken Kro\\Musik", "Final")}
        self.assertEqual(kinds, {"Sørens Malerfirma": KIND_PROJECT,
                                 "Sørens Malerfirma\\Klip": KIND_DIR,
                                 "Lyngbakken Kro": KIND_PROJECT,
                                 "Lyngbakken Kro\\Musik": KIND_DIR, "Final": KIND_TOPLEVEL})
        self.assertEqual(rows["Sørens Malerfirma\\Klip\\A7S\\a.mp4"]["project_rel"],
                         "Sørens Malerfirma")
        self.assertEqual(rows["Lyngbakken Kro\\Musik\\Raw\\m.wav"]["project_rel"],
                         "Lyngbakken Kro")
        self.assertEqual(self.index.deep(self.sid, self.root)["counts"]["project_count"], 2)

    def test_template_regex_and_minimum_from_config(self):
        self.build(["Skabelon\\Klip\\", "Skabelon\\Final\\", "1. KUNDENAVN\\Klip\\",
                    "1. KUNDENAVN\\Final\\", "Only Klip\\Klip\\"])
        settle(self.root)
        self.index.deep(self.sid, self.root,
                        config_=cfg(template_folder_regex=r"^skabelon$",
                                    project_min_template_dirs=1))
        rows = self.rows()
        self.assertEqual(rows["Skabelon"]["kind"], KIND_TEMPLATE)
        self.assertEqual(rows["1. KUNDENAVN"]["kind"], KIND_PROJECT)
        self.assertEqual(rows["Only Klip"]["kind"], KIND_PROJECT)


class ExcludeTests(ScanTestCase):
    def test_excluded_names_and_globs(self):
        self.build(["P\\.git\\config", "P\\node_modules\\x.js", "P\\CacheClip\\c.bin",
                    "P\\keep.txt", "P\\THUMBS.DB", "P\\desktop.ini", "P\\._keep.txt",
                    "P\\~$doc.docx", "P\\x.tmp", "P\\Sub.tmp\\f.txt"])
        settle(self.root)
        self.index.deep(self.sid, self.root)
        self.assertEqual(set(self.rows()),
                         {"P", "P\\keep.txt", "P\\Sub.tmp", "P\\Sub.tmp\\f.txt"})

    def test_hidden_and_system_entries_are_skipped_hidden_only_kept(self):
        self.build(["P\\hs.txt", "P\\h.txt", "P\\HSDir\\inner.txt", "P\\ok.txt"])
        set_attributes(self.p("P\\hs.txt"), FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM)
        set_attributes(self.p("P\\h.txt"), FILE_ATTRIBUTE_HIDDEN)
        set_attributes(self.p("P\\HSDir"), FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM)
        settle(self.root)
        self.index.deep(self.sid, self.root)
        self.assertEqual(set(self.rows()), {"P", "P\\h.txt", "P\\ok.txt"})

    def test_excludes_from_config_remove_indexed_rows(self):
        self.build(["P\\Render\\a.exr", "P\\notes.bak", "P\\keep.txt"])
        settle(self.root)
        self.index.deep(self.sid, self.root)
        self.assertIn("P\\Render\\a.exr", self.rows())
        result = self.index.deep(self.sid, self.root,
                                 config_=cfg(exclude_dir_names=["render"],
                                             exclude_file_globs=["*.BAK"]))
        self.assertEqual(set(self.rows()), {"P", "P\\keep.txt"})
        self.assertEqual(result["deleted"], 3)
        self.index.check_fts()


class AggregateTests(ScanTestCase):
    def test_sizes_counts_and_mtimes(self):
        make_tree(self.root, ["Rikke Lindholm\\Klip\\a.mxf"], size=100)
        make_tree(self.root, ["Rikke Lindholm\\Klip\\FX9\\b.mxf"], size=50)
        make_tree(self.root, ["Rikke Lindholm\\Final\\c.mp4"], size=7)
        make_tree(self.root, [f"Rikke Lindholm\\Grafik\\r_{i:03d}.png" for i in range(25)], size=2)
        make_tree(self.root, ["loose.txt"], size=3)
        settle(self.root)
        set_mtime(self.p("Rikke Lindholm\\Klip\\FX9\\b.mxf"), SETTLED_MTIME + 500)
        set_mtime(self.p("Rikke Lindholm\\Klip\\FX9"), SETTLED_MTIME + 10)
        result = self.index.deep(self.sid, self.root)
        rows = self.rows()

        fx9 = rows["Rikke Lindholm\\Klip\\FX9"]
        self.assertEqual((fx9["size"], fx9["file_count"]), (50, 1))
        self.assertEqual((fx9["mtime"], fx9["dir_mtime"]),
                         (SETTLED_MTIME + 500, SETTLED_MTIME + 10))
        klip = rows["Rikke Lindholm\\Klip"]
        self.assertEqual((klip["size"], klip["file_count"]), (150, 2))
        self.assertEqual((klip["mtime"], klip["dir_mtime"]), (SETTLED_MTIME + 500, SETTLED_MTIME))
        rikke = rows["Rikke Lindholm"]
        self.assertEqual((rikke["size"], rikke["file_count"]), (207, 28))
        self.assertEqual(rikke["mtime"], SETTLED_MTIME + 500)
        mxf = rows["Rikke Lindholm\\Klip\\a.mxf"]
        self.assertEqual((mxf["size"], mxf["file_count"], mxf["ext"]), (100, None, "mxf"))
        self.assertIsNone(mxf["dir_mtime"])
        self.assertEqual(result["counts"], {"entry_count": len(rows), "dir_count": 5,
                                            "file_count": 29, "project_count": 1,
                                            "total_size": 210})
        self.assertEqual(result["files"], 29)


class UnitApplyTests(ScanTestCase):
    def setUp(self):
        super().setUp()
        self.build(["A\\Klip\\a.mxf", "A\\Final\\a.mp4", "B\\Klip\\b.mxf", "B\\sub\\deep\\x.txt",
                    "C\\c.txt", "root.txt"])
        settle(self.root)

    def test_each_unit_is_one_commit_and_a_rescan_changes_nothing(self):
        commits, progress = [], []
        result = self.index.deep(self.sid, self.root, on_commit=commits.append,
                                 progress=progress.append)
        self.assertEqual(len(commits), 4)                 # root files + units A, B, C
        self.assertEqual(sum(commits), result["changed"])
        self.assertEqual(result["changed"], len(self.rows()))
        self.assertEqual((result["units_done"], result["units_total"]), (4, 4))
        self.assertTrue(result["ok"])
        self.assertEqual(set(progress[-1]), {"entries", "dirs", "units_done", "units_total"})
        self.assertEqual(progress[-1]["units_done"], 4)
        before = self.index.snapshot(self.sid)
        again = []
        result = self.index.deep(self.sid, self.root, on_commit=again.append)
        self.assertEqual((result["changed"], again), (0, []))
        self.assertEqual(self.index.snapshot(self.sid), before)
        self.index.check_fts()

    def test_progress_is_rate_limited(self):
        make_tree(self.root, [f"D{i:02d}\\f.txt" for i in range(40)])
        settle(self.root)
        stamps = []

        def slow_lister(path):
            time.sleep(0.01)
            return scanner.list_dir(path)

        started = time.monotonic()
        self.index.deep(self.sid, self.root, lister=slow_lister,
                        progress=lambda p: stamps.append(time.monotonic()))
        elapsed = time.monotonic() - started
        # at most 4/s, plus the forced reports after the root unit and at the end
        self.assertLessEqual(len(stamps), elapsed / scanner.PROGRESS_INTERVAL_S + 3)
        self.assertGreaterEqual(len(stamps), 2)

    def test_cancel_keeps_committed_units(self):
        cancel = threading.Event()
        commits = []

        def on_commit(n):
            commits.append(n)
            if len(commits) == 2:        # root unit + first directory unit
                cancel.set()

        result = self.index.deep(self.sid, self.root, cancel=cancel, on_commit=on_commit)
        self.assertTrue(result["aborted"])
        self.assertFalse(result["ok"])
        rows = self.rows()
        self.assertIn("root.txt", rows)
        self.assertIn("A\\Klip\\a.mxf", rows)
        self.assertNotIn("B", rows)
        self.assertNotIn("C", rows)

    def test_vanished_unit_is_deleted_with_its_subtree(self):
        self.index.deep(self.sid, self.root)
        shutil.rmtree(self.p("B"))
        result = self.index.deep(self.sid, self.root)
        rows = self.rows()
        self.assertFalse([rel for rel in rows if rel == "B" or rel.startswith("B\\")])
        self.assertIn("A\\Klip\\a.mxf", rows)
        self.assertEqual(result["deleted"], 6)
        self.index.check_fts()

    def test_failed_root_listing_writes_nothing(self):
        self.index.deep(self.sid, self.root)
        before = self.index.snapshot(self.sid)
        shutil.rmtree(self.p("B"))
        lister = CountingLister()
        lister.fail.add(os.path.basename(self.root))
        result = self.index.deep(self.sid, self.root, lister=lister)
        self.assertEqual((result["ok"], result["aborted"], result["changed"]), (False, True, 0))
        self.assertIn("adgang nægtet", result["error"])
        self.assertTrue(result["error"].startswith("Kunne ikke læse"))
        self.assertEqual(self.index.snapshot(self.sid), before)
        shutil.rmtree(self.root)                         # root gone entirely (unplugged disk)
        result = self.index.deep(self.sid, self.root)
        self.assertIn("findes ikke", result["error"])
        self.assertEqual(self.index.snapshot(self.sid), before)

    def test_another_volume_at_the_root_writes_nothing(self):
        # IDX-3: verify() fails before the first listing → nothing listed, nothing written.
        lister = CountingLister()
        result = self.index.deep(self.sid, self.root, lister=lister, verify=lambda: False)
        self.assertEqual((result["ok"], result["aborted"], result["error"], result["changed"]),
                         (False, True, scanner.DISK_CHANGED, 0))
        self.assertTrue(result["volume_changed"])
        self.assertEqual((lister.calls, self.rows()), ([], {}))

    def test_disk_swapped_during_the_scan_keeps_committed_units_only(self):
        self.index.deep(self.sid, self.root)
        before = self.index.snapshot(self.sid)
        make_tree(self.root, ["A\\new.mxf", "B\\new.mxf", "C\\new.mxf", "new root.txt"])
        settle(self.root)
        checks = []

        def verify():                    # the disk is swapped after the second transaction
            checks.append(1)
            return len(checks) <= 3      # 1: before the listing, 2: root unit, 3: unit A

        result = self.index.deep(self.sid, self.root, verify=verify)
        self.assertEqual((result["ok"], result["aborted"], result["error"]),
                         (False, True, scanner.DISK_CHANGED))
        self.assertEqual(result["units_done"], 2)
        rows = self.rows()
        self.assertIn("new root.txt", rows)
        self.assertIn("A\\new.mxf", rows)
        self.assertNotIn("B\\new.mxf", rows)             # read from the other disk: dropped
        self.assertNotIn("C\\new.mxf", rows)
        self.assertEqual({rel: v for rel, v in self.index.snapshot(self.sid).items()
                          if rel.startswith(("B", "C"))},
                         {rel: v for rel, v in before.items() if rel.startswith(("B", "C"))})
        self.assertEqual(len(checks), 4)
        self.index.check_fts()

    def test_file_and_directory_swapping_places(self):
        self.index.deep(self.sid, self.root)
        os.remove(self.p("root.txt"))
        make_tree(self.root, ["root.txt\\inside.txt"])   # the file is now a directory
        shutil.rmtree(self.p("C"))
        make_tree(self.root, ["C"])                      # the directory is now a file
        settle(self.root)
        self.index.deep(self.sid, self.root)
        rows = self.rows()
        self.assertEqual(rows["root.txt"]["kind"], KIND_TOPLEVEL)
        self.assertIn("root.txt\\inside.txt", rows)
        self.assertEqual(rows["C"]["kind"], KIND_FILE)
        self.assertNotIn("C\\c.txt", rows)
        self.index.check_fts()
        self.assertEqual(self.index.deep(self.sid, self.root)["changed"], 0)


class FailureTests(ScanTestCase):
    def test_unlistable_directory_keeps_its_rows(self):
        self.build(["P\\Klip\\a.mxf", "P\\Klip\\b.mxf", "P\\Final\\c.mp4", "P\\Grafik\\",
                    "P\\Logo\\"])
        settle(self.root)
        self.index.deep(self.sid, self.root)
        os.remove(self.p("P\\Klip\\a.mxf"))
        make_tree(self.root, ["P\\Final\\d.mp4"])
        settle(self.root)
        set_mtime(self.p("P\\Klip"), SETTLED_MTIME + 99)
        lister = CountingLister()
        lister.fail.add("P\\Klip")
        result = self.index.deep(self.sid, self.root, lister=lister)
        rows = self.rows()
        self.assertIn("P\\Klip\\a.mxf", rows)            # kept: its directory failed
        self.assertIn("P\\Final\\d.mp4", rows)           # the rest of the unit was applied
        self.assertEqual(rows["P\\Klip"]["dir_mtime"], SETTLED_MTIME)   # re-listed next time
        self.assertTrue(result["ok"])
        self.assertEqual(result["failed_dirs"], 1)
        self.assertEqual(result["error"], "1 mappe kunne ikke læses")
        self.index.deep(self.sid, self.root)
        self.assertNotIn("P\\Klip\\a.mxf", self.rows())

    def test_unit_is_not_applied_when_most_directories_fail(self):
        self.build(["P\\Klip\\a.mxf", "P\\Final\\c.mp4", "Q\\x.txt"])
        settle(self.root)
        self.index.deep(self.sid, self.root)
        make_tree(self.root, ["P\\new.mp4", "Q\\y.txt"])
        settle(self.root)
        lister = CountingLister()
        lister.fail.update({"P\\Klip", "P\\Final"})        # 2 of 3 directories
        result = self.index.deep(self.sid, self.root, lister=lister)
        rows = self.rows()
        self.assertNotIn("P\\new.mp4", rows)
        self.assertIn("P\\Klip\\a.mxf", rows)
        self.assertIn("Q\\y.txt", rows)
        self.assertFalse(result["ok"])
        self.assertFalse(result["aborted"])
        self.assertIn("øverste niveau", result["error"])

    def test_unlistable_unit_root_keeps_everything(self):
        self.build(["P\\Klip\\a.mxf"])
        settle(self.root)
        self.index.deep(self.sid, self.root)
        before = self.index.snapshot(self.sid)
        lister = CountingLister()
        lister.fail.add("P")
        result = self.index.deep(self.sid, self.root, lister=lister)
        self.assertEqual(self.index.snapshot(self.sid), before)
        self.assertEqual(result["failed_dirs"], 1)


class IncrementalTests(ScanTestCase):
    def setUp(self):
        super().setUp()
        self.build(["A\\Klip\\FX9\\c1.mxf", "A\\Klip\\FX9\\c2.mxf", "A\\Final\\f.mp4",
                    "A\\Grafik\\", "B\\Leaf\\x.txt"])
        settle(self.root)

    def scan(self, **kwargs):
        lister = CountingLister()
        kwargs.setdefault("is_network", True)
        result = self.index.deep(self.sid, self.root, lister=lister, **kwargs)
        return result, lister

    def test_unchanged_leaves_are_not_listed_again(self):
        first, lister = self.scan()
        self.assertEqual(first["reused_dirs"], 0)
        self.assertTrue(lister.listed("A\\Klip\\FX9"))
        before = self.index.snapshot(self.sid)
        second, lister = self.scan()
        self.assertTrue(second["incremental"])
        self.assertFalse(lister.listed("A\\Klip\\FX9"))
        self.assertFalse(lister.listed("B\\Leaf"))
        self.assertTrue(lister.listed("A\\Klip"))         # has sub-directories: always listed
        self.assertEqual(second["reused_dirs"], 4)        # FX9, Final, Grafik, Leaf
        self.assertEqual(second["changed"], 0)
        self.assertEqual(self.index.snapshot(self.sid), before)

    def test_leaf_with_unchanged_mtime_is_trusted_until_a_full_scan(self):
        self.scan()
        make_tree(self.root, ["A\\Klip\\FX9\\c3.mxf"])
        set_mtime(self.p("A\\Klip\\FX9"), SETTLED_MTIME)
        self.scan()
        self.assertNotIn("A\\Klip\\FX9\\c3.mxf", self.rows())
        result, lister = self.scan(full=True)
        self.assertFalse(result["incremental"])
        self.assertIn("A\\Klip\\FX9\\c3.mxf", self.rows())
        self.assertEqual(self.rows()["A\\Klip\\FX9"]["file_count"], 3)

    def test_changed_leaf_is_listed(self):
        self.scan()
        make_tree(self.root, ["A\\Klip\\FX9\\c3.mxf"])
        set_mtime(self.p("A\\Klip\\FX9\\c3.mxf"), SETTLED_MTIME)
        set_mtime(self.p("A\\Klip\\FX9"), SETTLED_MTIME + 60)
        result, lister = self.scan()
        self.assertTrue(lister.listed("A\\Klip\\FX9"))
        rows = self.rows()
        self.assertIn("A\\Klip\\FX9\\c3.mxf", rows)
        self.assertEqual(rows["A\\Klip\\FX9"]["dir_mtime"], SETTLED_MTIME + 60)
        self.assertEqual(rows["A"]["file_count"], 4)

    def test_local_and_non_ntfs_sources_always_walk_fully(self):
        self.scan()
        for kwargs in ({"is_network": False}, {"fs": "exFAT"}):
            result, lister = self.scan(**kwargs)
            self.assertFalse(result["incremental"])
            self.assertEqual(result["reused_dirs"], 0)
            self.assertTrue(lister.listed("A\\Klip\\FX9"))

    def test_reused_files_follow_a_new_project_above_them(self):
        self.scan()
        self.assertIsNone(self.rows()["B\\Leaf\\x.txt"]["project_rel"])
        make_tree(self.root, ["B\\Final\\", "B\\Klip\\"])
        settle(self.p("B\\Final"))
        settle(self.p("B\\Klip"))
        set_mtime(self.p("B"), SETTLED_MTIME + 5)
        result, lister = self.scan()
        self.assertFalse(lister.listed("B\\Leaf"))
        rows = self.rows()
        self.assertEqual(rows["B"]["kind"], KIND_PROJECT)
        self.assertEqual(rows["B\\Leaf\\x.txt"]["project_rel"], "B")


class PathTests(ScanTestCase):
    def test_paths_longer_than_260_characters(self):
        parts = [f"Lang mappe med et meget langt navn nummer {i:02d}" for i in range(8)]
        deep_rel = "\\".join(parts)
        os.makedirs("\\\\?\\" + os.path.join(self.root, deep_rel))
        with open("\\\\?\\" + os.path.join(self.root, deep_rel, "slut.mxf"), "wb") as fh:
            fh.write(b"x")
        self.addCleanup(shutil.rmtree, "\\\\?\\" + os.path.join(self.root, parts[0]), True)
        self.index.deep(self.sid, self.root)
        rel = deep_rel + "\\slut.mxf"
        self.assertGreater(len(os.path.join(self.root, rel)), 300)
        row = self.rows()[rel]
        self.assertEqual((row["depth"], row["ext"]), (9, "mxf"))

    def test_unicode_names_are_stored_verbatim_and_folded(self):
        self.build(["Forår 2026 RØD\\Bøgely Jul 2024\\Klip\\æøå.mxf"])
        settle(self.root)
        self.index.deep(self.sid, self.root)
        row = self.rows()["Forår 2026 RØD\\Bøgely Jul 2024"]
        self.assertEqual((row["name"], row["name_fold"]), ("Bøgely Jul 2024", "bogely jul 2024"))

    def test_junction_is_stored_but_never_followed(self):
        target = os.path.join(self.tmp, "target")
        make_tree(target, ["inside\\secret.txt"])
        self.build(["P\\a.txt"])
        settle(self.root)
        _winapi.CreateJunction(target, self.p("P\\Link"))
        self.index.deep(self.sid, self.root)
        rows = self.rows()
        self.assertEqual((rows["P\\Link"]["kind"], rows["P\\Link"]["size"]), (KIND_DIR, None))
        self.assertFalse([rel for rel in rows if rel.startswith("P\\Link\\")])

    def test_long_path_form(self):
        self.assertEqual(scanner.long_path("C:\\Kunder"), "\\\\?\\C:\\Kunder")
        self.assertEqual(scanner.long_path("C:/Kunder/x/"), "\\\\?\\C:\\Kunder\\x")
        self.assertEqual(scanner.long_path("H:"), "\\\\?\\H:\\")
        self.assertEqual(scanner.long_path("\\\\GRAFIK-PC\\Kunder 2026 (Grafik)"),
                         "\\\\?\\UNC\\GRAFIK-PC\\Kunder 2026 (Grafik)")
        self.assertEqual(scanner.long_path("\\\\?\\C:\\x"), "\\\\?\\C:\\x")
        for bad in ("relative\\dir", "C:relative", "\\rooted"):
            with self.assertRaises(ValueError):
                scanner.long_path(bad)


class DiffTests(unittest.TestCase):
    @staticmethod
    def entry(rel, kind=KIND_FILE, **kw):
        parent = rel.rpartition("\\")[0]
        name = rel.rpartition("\\")[2]
        values = dict(rel_path=rel, parent_rel=parent, name=name, name_fold=name.lower(),
                      kind=kind, depth=rel.count("\\") + 1, ext=None, size=1, mtime=1.0,
                      file_count=None, dir_mtime=None, is_seq=0, seq_count=None,
                      project_rel=None)
        values.update(kw)
        return Entry(**values)

    def stored(self, *entries):
        return [(i + 1, *e) for i, e in enumerate(entries)]

    def test_unchanged_updated_inserted_deleted(self):
        a, b, c = self.entry("a"), self.entry("b"), self.entry("c")
        diff = scanner.compute_diff(self.stored(a, b, c), [a, b._replace(size=5),
                                                           self.entry("d")],
                                    descendants_loaded=True)
        self.assertEqual([e.rel_path for e in diff.inserts], ["d"])
        self.assertEqual([(i, e.size) for i, e in diff.updates], [(2, 5)])
        self.assertEqual((diff.deletes, diff.delete_descendants), ([3], []))

    def test_keep_under_protects_rows_of_failed_directories(self):
        rows = self.stored(self.entry("X", KIND_DIR), self.entry("X\\f"), self.entry("Y"))
        diff = scanner.compute_diff(rows, [self.entry("X", KIND_DIR)], keep_under=["X"],
                                    descendants_loaded=True)
        self.assertEqual(diff.deletes, [3])

    def test_shallow_scope_deletes_descendants_of_vanished_or_retyped_dirs(self):
        rows = self.stored(self.entry("gone", KIND_DIR), self.entry("now_file", KIND_DIR))
        diff = scanner.compute_diff(rows, [self.entry("now_file")], descendants_loaded=False)
        self.assertEqual(diff.deletes, [1])
        self.assertEqual(sorted(diff.delete_descendants), ["gone", "now_file"])

    def test_changed_fold_becomes_delete_plus_insert(self):
        old = self.entry("a", name_fold="old")
        diff = scanner.compute_diff(self.stored(old), [self.entry("a")], descendants_loaded=True)
        self.assertEqual((diff.deletes, len(diff.inserts), diff.updates), ([1], 1, []))


if __name__ == "__main__":
    unittest.main()
