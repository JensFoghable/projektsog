"""Shallow scan (SPEC §5.2): new projects at depth 1/2, vanished entries, untouched depth ≥ 3."""

import os
import shutil
import tempfile
import unittest

from projektsog import db, scanner, search
from projektsog.db import KIND_DIR, KIND_GROUP, KIND_PROJECT, KIND_TOPLEVEL
from tests._index_store_fixtures import (SETTLED_MTIME, CountingLister, TempIndex, make_tree,
                                         module_env, set_mtime, settle)

_env = None
T2 = SETTLED_MTIME + 3600


def setUpModule():
    global _env
    _env = module_env()


def tearDownModule():
    _env.cleanup()


class ShallowTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.root = os.path.join(self.tmp, "Kunder 2026 (Grafik)")
        make_tree(self.root, [
            "Rikke Lindholm\\Final\\f.mp4", "Rikke Lindholm\\Grafik\\",
            "Rikke Lindholm\\Klip\\FX9\\clip.mxf",
            "Klar Tand 2026\\Klar Tand - Silkeborg\\Klip\\k.mxf",
            "Klar Tand 2026\\Klar Tand - Silkeborg\\Final\\",
            "Arkiv\\Old\\Deep\\file.txt", "Arkiv\\keep.txt", "readme.txt"])
        settle(self.root)
        self.index = TempIndex(self.tmp)
        self.addCleanup(self.index.close)
        self.sid = self.index.add_source(self.root)
        self.index.deep(self.sid, self.root)

    def p(self, rel):
        return os.path.join(self.root, rel)

    def shallow(self, index=None, sid=None, **kwargs):
        lister = CountingLister()
        result = (index or self.index).shallow(sid or self.sid, self.root, lister=lister,
                                               **kwargs)
        return result, lister

    def rows(self):
        return self.index.rows(self.sid)

    def test_unchanged_tree_needs_one_listing_and_changes_nothing(self):
        before = self.index.snapshot(self.sid)
        result, _lister = self.shallow()
        self.assertEqual((result["ok"], result["changed"], result["listings"]), (True, 0, 1))
        self.assertEqual(self.index.snapshot(self.sid), before)

    def test_new_project_at_depth_1(self):
        make_tree(self.root, ["Pixelbro\\Klip\\x.mxf", "Pixelbro\\Final\\"])
        settle(self.p("Pixelbro"), T2)
        commits = []
        result, lister = self.shallow(on_commit=commits.append)
        rows = self.rows()
        project = rows["Pixelbro"]
        self.assertEqual(project["kind"], KIND_PROJECT)
        self.assertEqual((project["size"], project["mtime"], project["file_count"]),
                         (None, T2, None))              # provisional mtime: its own (IDX-5)
        self.assertEqual((project["dir_mtime"], project["project_rel"]), (T2, "Pixelbro"))
        klip = rows["Pixelbro\\Klip"]
        self.assertEqual((klip["kind"], klip["dir_mtime"], klip["project_rel"]),
                         (KIND_DIR, None, "Pixelbro"))
        self.assertNotIn("Pixelbro\\Klip\\x.mxf", rows)          # depth 3: left to deep
        self.assertFalse(lister.listed("Rikke Lindholm"))
        self.assertEqual(result["listings"], 4)                 # root, Pixelbro, Klip, Final
        self.assertEqual(commits, [3])
        self.index.check_fts()

    def test_new_project_at_depth_2_inside_a_group(self):
        new = "Klar Tand 2026\\Klar Tand - Voxpop Silkeborg"
        make_tree(self.root, [new + "\\Klip\\", new + "\\Final\\"])
        settle(self.p(new), T2)
        set_mtime(self.p("Klar Tand 2026"), T2)
        before = self.rows()["Klar Tand 2026\\Klar Tand - Silkeborg"]
        self.shallow()
        rows = self.rows()
        self.assertEqual(rows[new]["kind"], KIND_PROJECT)
        self.assertEqual((rows[new]["dir_mtime"], rows[new]["size"]), (None, None))
        self.assertEqual(rows[new]["mtime"], T2)          # provisional, dir_mtime stays NULL
        self.assertEqual(rows["Klar Tand 2026"]["kind"], KIND_GROUP)
        self.assertEqual(rows["Klar Tand 2026"]["dir_mtime"], T2)
        self.assertEqual(tuple(rows["Klar Tand 2026\\Klar Tand - Silkeborg"]), tuple(before))
        self.assertNotIn(new + "\\Klip", rows)                   # depth 3

    def test_vanished_entries_are_removed_with_their_subtrees(self):
        shutil.rmtree(self.p("Arkiv\\Old"))
        set_mtime(self.p("Arkiv"), T2)
        os.remove(self.p("readme.txt"))
        shutil.rmtree(self.p("Klar Tand 2026"))
        result, _lister = self.shallow()
        rows = self.rows()
        self.assertFalse([r for r in rows if r.startswith(("Arkiv\\Old", "Klar Tand 2026"))])
        self.assertNotIn("readme.txt", rows)
        self.assertIn("Arkiv\\keep.txt", rows)
        self.assertEqual(result["deleted"], 3 + 1 + 5)
        self.index.check_fts()

    def test_rows_below_depth_2_are_never_touched(self):
        deep_before = {rel: tuple(r) for rel, r in self.rows().items() if r["depth"] >= 3}
        os.remove(self.p("Rikke Lindholm\\Klip\\FX9\\clip.mxf"))
        make_tree(self.root, ["Rikke Lindholm\\Klip\\new.mxf"])
        result, lister = self.shallow(fs="exFAT")        # lists every depth-1 dir
        self.assertTrue(lister.listed("Rikke Lindholm"))
        deep_after = {rel: tuple(r) for rel, r in self.rows().items() if r["depth"] >= 3}
        self.assertEqual(deep_after, deep_before)
        self.assertEqual(result["changed"], 0)

    def test_non_ntfs_sources_list_every_depth_1_directory(self):
        _result, lister = self.shallow(fs="exFAT")
        for name in ("Rikke Lindholm", "Klar Tand 2026", "Arkiv"):
            self.assertTrue(lister.listed(name), name)

    def test_failed_root_listing_writes_nothing(self):
        before = self.index.snapshot(self.sid)
        os.remove(self.p("readme.txt"))
        lister = CountingLister()
        lister.fail.add(os.path.basename(self.root))
        result = self.index.shallow(self.sid, self.root, lister=lister)
        self.assertEqual((result["ok"], result["aborted"]), (False, True))
        self.assertIn("adgang nægtet", result["error"])
        self.assertEqual(self.index.snapshot(self.sid), before)

    def test_first_time_classifies_depth_1_and_2(self):
        fresh = TempIndex(self.tmp, "fresh.db")
        self.addCleanup(fresh.close)
        sid = fresh.add_source(self.root)
        result, _lister = self.shallow(index=fresh, sid=sid, first_time=True)
        rows = fresh.rows(sid)
        self.assertEqual(rows["Rikke Lindholm"]["kind"], KIND_PROJECT)
        self.assertEqual(rows["Klar Tand 2026"]["kind"], KIND_GROUP)
        self.assertEqual(rows["Klar Tand 2026\\Klar Tand - Silkeborg"]["kind"], KIND_PROJECT)
        self.assertEqual(rows["Arkiv"]["kind"], KIND_TOPLEVEL)
        self.assertEqual(rows["readme.txt"]["size"], 1)
        self.assertIsNone(rows["Rikke Lindholm"]["size"])
        self.assertEqual(max(r["depth"] for r in rows.values()), 2)
        self.assertEqual(result["listings"], 1 + 3 + 5)

    def test_listing_budget(self):
        fresh = TempIndex(self.tmp, "budget.db")
        self.addCleanup(fresh.close)
        sid = fresh.add_source(self.root)
        result, _lister = self.shallow(index=fresh, sid=sid, first_time=True, max_listings=2)
        rows = fresh.rows(sid)
        self.assertEqual(result["listings"], 2)
        listed = [n for n in ("Rikke Lindholm", "Klar Tand 2026", "Arkiv")
                  if rows[n]["dir_mtime"] is not None]
        self.assertEqual(len(listed), 1)
        for name in {"Rikke Lindholm", "Klar Tand 2026", "Arkiv"} - set(listed):
            self.assertEqual(rows[name]["kind"], KIND_TOPLEVEL)       # unclassified yet

    def test_new_projects_are_in_recent_projects_at_once(self):
        # IDX-5 / ux IDX-2: no deep scan needed for "Seneste projekter".
        make_tree(self.root, ["Brand New Client\\Klip\\", "Brand New Client\\Final\\"])
        settle(self.p("Brand New Client"), T2 + 10)
        new2 = "Klar Tand 2026\\Klar Tand - Nyt"
        make_tree(self.root, [new2 + "\\Klip\\", new2 + "\\Final\\"])
        settle(self.p(new2), T2)
        set_mtime(self.p("Klar Tand 2026"), T2)
        self.shallow()
        reader = db.connect(self.index.path)
        self.addCleanup(reader.close)
        registry = {self.sid: {"id": self.sid, "display_name": "Kunder 2026 (Grafik)",
                               "kind": "share", "path": self.root, "online": True}}
        recent = search.recent_projects(reader, registry, limit=3)
        self.assertEqual([(r["name"], r["mtime"]) for r in recent[:2]],
                         [("Brand New Client", T2 + 10), ("Klar Tand - Nyt", T2)])
        self.assertEqual(recent[2]["mtime"], SETTLED_MTIME)      # the deep-scanned ones
        self.assertIsNone(self.rows()[new2]["dir_mtime"])
        self.index.deep(self.sid, self.root, is_network=True)  # the aggregate replaces it
        self.assertEqual(self.rows()["Brand New Client"]["mtime"], T2 + 10)
        self.assertEqual(self.rows()["Brand New Client"]["file_count"], 0)

    def test_folders_named_like_template_folders_are_never_projects(self):
        # ux IDX-3 in the shallow classification (depth 1 and depth 2).
        make_tree(self.root, ["Klip\\Raw\\", "Klip\\Stills\\", "Arkiv\\Musik\\Raw\\",
                              "Arkiv\\Musik\\Stills\\"])
        settle(self.p("Klip"), T2)
        settle(self.p("Arkiv\\Musik"), T2)
        set_mtime(self.p("Arkiv"), T2)
        self.shallow()
        rows = self.rows()
        self.assertEqual((rows["Klip"]["kind"], rows["Klip"]["project_rel"]),
                         (KIND_TOPLEVEL, None))
        self.assertEqual((rows["Arkiv\\Musik"]["kind"], rows["Arkiv\\Musik"]["project_rel"]),
                         (KIND_DIR, None))
        self.assertEqual(rows["Arkiv"]["kind"], KIND_TOPLEVEL)

    def test_another_volume_at_the_root_writes_nothing(self):
        # IDX-3: checked before the root listing and before the one transaction.
        before = self.index.snapshot(self.sid)
        make_tree(self.root, ["Pixelbro\\Klip\\", "Pixelbro\\Final\\"])
        settle(self.p("Pixelbro"), T2)
        result, lister = self.shallow(verify=lambda: False)
        self.assertEqual((result["ok"], result["aborted"], result["error"], result["listings"]),
                         (False, True, scanner.DISK_CHANGED, 0))
        self.assertEqual(lister.calls, [])
        answers = iter([True, False])
        result, lister = self.shallow(verify=lambda: next(answers))
        self.assertEqual((result["aborted"], result["changed"], result["volume_changed"]),
                         (True, 0, True))
        self.assertTrue(lister.listed("Pixelbro"))
        self.assertEqual(self.index.snapshot(self.sid), before)

    def test_incremental_deep_after_shallow_equals_a_full_scan(self):
        make_tree(self.root, ["Rikke Lindholm\\Final\\g.mp4"])
        set_mtime(self.p("Rikke Lindholm\\Final\\g.mp4"), SETTLED_MTIME)
        set_mtime(self.p("Rikke Lindholm\\Final"), T2)
        make_tree(self.root, ["Pixelbro\\Klip\\x.mxf", "Pixelbro\\Final\\"])
        settle(self.p("Pixelbro"), T2)
        new = "Klar Tand 2026\\Klar Tand - Voxpop Silkeborg"
        make_tree(self.root, [new + "\\Klip\\v.mxf", new + "\\Final\\"])
        settle(self.p(new), T2)
        set_mtime(self.p("Klar Tand 2026"), T2)
        shutil.rmtree(self.p("Arkiv\\Old"))
        set_mtime(self.p("Arkiv"), T2)

        self.shallow()
        self.index.check_fts()
        result = self.index.deep(self.sid, self.root, is_network=True)
        self.assertTrue(result["incremental"])
        self.assertGreater(result["reused_dirs"], 0)

        fresh = TempIndex(self.tmp, "full.db")
        self.addCleanup(fresh.close)
        sid = fresh.add_source(self.root)
        fresh.deep(sid, self.root, full=True)
        self.assertEqual(self.index.snapshot(self.sid), fresh.snapshot(sid))


if __name__ == "__main__":
    unittest.main()
