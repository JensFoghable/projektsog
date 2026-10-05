"""Klippe's trophies and wardrobe (achievements.py): what counts, finds, keeping and wearing."""

import os
import tempfile
import unittest
from datetime import date, datetime
from unittest import mock

from projektsog import achievements as ach
from projektsog.config import Config
from tests.test_petplay import FakeBus


def seg(day: str, hour: float, minutes: float, *, project: str = "Rikke Lindholm", bucket: str = "edit",
        timeline: str = "Tidslinje 1") -> tuple:
    start = datetime.fromisoformat(day).timestamp() + hour * 3600
    return (project, "Local", "u1", None, bucket, start, start + minutes * 60, timeline)


class StatsTests(unittest.TestCase):
    def test_days_pages_projects_and_timelines(self) -> None:
        st = ach.compute_stats([seg("2026-03-02", 9, 60), seg("2026-03-02", 10.5, 30, bucket="color", timeline="T2"),
                                seg("2026-03-03", 9, 90, project="Mette Juhl", bucket="fusion")])
        self.assertEqual(st.days, {"2026-03-02": 5400.0, "2026-03-03": 5400.0})
        self.assertEqual(st.buckets, {"edit": 3600.0, "color": 1800.0, "fusion": 5400.0})
        self.assertEqual(st.projects["Rikke Lindholm"], 5400.0)
        self.assertEqual(st.timelines, 3)
        self.assertEqual(st.week_projects, 2)
        self.assertEqual((st.day_first["2026-03-02"], st.day_last["2026-03-02"]), (9.0, 11.0))
        self.assertEqual(st.level, 1 + 2)                     # 3 hours: 1 + ⌊√6⌋

    def test_a_day_split_at_midnight(self) -> None:
        st = ach.compute_stats([seg("2026-03-02", 23.5, 60)])
        self.assertEqual(st.days, {"2026-03-02": 1800.0, "2026-03-03": 1800.0})

    def test_workday_streaks_skip_weekends(self) -> None:
        week = ["2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05", "2026-03-06", "2026-03-09"]  # Mon–Fri, Mon
        self.assertEqual(ach.longest_weekday_run(week), 6)
        self.assertEqual(ach.longest_weekday_run(week + ["2026-03-07"]), 6)        # a Saturday adds nothing
        self.assertEqual(ach.longest_weekday_run(["2026-03-02", "2026-03-04"]), 1)  # a Tuesday missing
        self.assertEqual(ach.longest_weekday_run([]), 0)

    def test_focus_and_breaks(self) -> None:
        rows = [seg("2026-03-02", 9, 45), seg("2026-03-02", 9.76, 50),            # 1 min apart: one stretch
                seg("2026-03-02", 11, 30),                                         # 10 min break after 96 min
                seg("2026-03-02", 13, 20)]
        st = ach.compute_stats(rows)
        self.assertEqual((st.focus_90, st.good_breaks), (1, 1))

    def test_cards_and_games(self) -> None:
        st = ach.compute_stats([], [{"mode": "copy", "files": 10, "bytes": 2 * 10 ** 12},
                                    {"mode": "move", "files": 3, "bytes": 5}, {"mode": "prepare", "files": 0}],
                               counters={"games": 4, "caught": 1})
        self.assertEqual((st.cards, st.card_bytes, st.moves, st.games, st.caught), (2, 2 * 10 ** 12 + 5, 1, 4, 1))


def trophy(id_: str) -> ach.Trophy:
    return ach.TROPHIES_BY_ID[id_]


def earned(st: ach.Stats, id_: str) -> bool:
    t = trophy(id_)
    return t.measure(st) >= t.goal


class TrophyTests(unittest.TestCase):
    def test_the_catalogue_holds_together(self) -> None:
        self.assertEqual(len({t.id for t in ach.TROPHIES}), len(ach.TROPHIES))
        rewards = [t.reward for t in ach.TROPHIES if t.reward]
        self.assertEqual(len(rewards), len(set(rewards)))                 # one item, one trophy
        self.assertTrue(all(r in ach.ITEMS_BY_ID for r in rewards))
        finds = {i for i, _chance in ach.FINDS}
        for item in ach.ITEMS:
            with self.subTest(item=item.id):
                self.assertTrue(item.default or item.id in rewards or item.id in finds, "cannot be had")
                self.assertIn(item.slot, ach.SLOTS)
        self.assertEqual(set(ach.DEFAULTS), set(ach.SLOTS))
        legendary = {i.id for i in ach.ITEMS if i.rarity == "legendarisk"}
        self.assertTrue({"awp", "solbriller", "cigaret"} <= legendary)

    def test_goal_and_going_home_on_time(self) -> None:
        days = [f"2026-03-0{d}" for d in (2, 3, 4, 5, 6)]
        st = ach.compute_stats([seg(d, 9, 6 * 60) for d in days], goal_hours=6)      # 9–15 every day
        self.assertTrue(earned(st, "maal1") and earned(st, "fyraften") and earned(st, "uge"))
        late = ach.compute_stats([seg(d, 12, 6 * 60) for d in days], goal_hours=6)  # until 18:00
        self.assertFalse(earned(late, "fyraften"))
        self.assertTrue(earned(ach.compute_stats([seg(d, 7.5, 61) for d in days]), "morgen"))

    def test_overtime_is_never_rewarded(self) -> None:
        # Long days, nights and weekends earn nothing that steady days do not.
        heavy = ach.compute_stats([seg("2026-03-07", 8, 14 * 60), seg("2026-03-08", 20, 4 * 60)])
        steady = ach.compute_stats([seg("2026-03-02", 9, 6 * 60), seg("2026-03-03", 9, 6 * 60)])
        unlocked = lambda st: {t.id for t in ach.TROPHIES if t.measure(st) >= t.goal}   # noqa: E731
        self.assertEqual(unlocked(heavy) - unlocked(steady) - {"lv5"}, set())

    def test_allround_seasons_and_secrets(self) -> None:
        day = "2026-03-02"
        st = ach.compute_stats([seg(day, 9 + i, 6, bucket=b) for i, b in
                                enumerate(("edit", "color", "fusion", "fairlight", "deliver"))])
        self.assertTrue(earned(st, "allround"))
        self.assertTrue(earned(ach.compute_stats([seg("2026-12-03", 9, 61)]), "jul"))
        self.assertFalse(earned(ach.compute_stats([seg("2026-11-30", 9, 61)]), "jul"))
        self.assertTrue(earned(ach.compute_stats([seg("2026-03-13", 9, 61)]), "fredag13"))        # a Friday
        self.assertTrue(earned(ach.compute_stats([seg("2028-02-29", 9, 31)]), "skuddag"))
        exact = ach.compute_stats([seg("2026-03-02", 9, 6 * 60 + 0.5)], goal_hours=6, today=date(2026, 3, 3))
        self.assertTrue(earned(exact, "praecis"))
        self.assertTrue(trophy("fredag13").secret)

    def test_hatched_by_hand(self) -> None:
        self.assertFalse(earned(ach.compute_stats([]), "klaekket"))
        self.assertTrue(earned(ach.compute_stats([], hatched=True), "klaekket"))


class FindTests(unittest.TestCase):
    def test_each_pc_its_own_luck(self) -> None:
        days = [seg(f"2026-{m:02d}-{d:02d}", 9, 90) for m in range(1, 13) for d in range(1, 29)]
        st = ach.compute_stats(days)
        self.assertEqual(ach.finds("a" * 32, st), ach.finds("a" * 32, st))          # the same every time
        lucky = [ach.finds(f"{n:032x}", st) for n in range(40)]
        self.assertGreater(len({tuple(sorted(f.items())) for f in lucky}), 20)       # PCs differ
        for found in lucky:
            for item, day in found.items():
                self.assertLess(date.fromisoformat(day).weekday(), 5)                # only on workdays
        with mock.patch.object(ach, "FINDS", [("awp", 1.0)]):
            self.assertEqual(ach.finds("x" * 32, st), {"awp": min(d for d in st.days if date.fromisoformat(d).weekday() < 5)})
        self.assertEqual(ach.finds("x" * 32, ach.compute_stats([seg("2026-03-02", 9, 30)])), {})   # < 1 hour


class FakeStore:
    def __init__(self, rows) -> None:
        self.rows = rows

    def between(self, start, end):
        return list(self.rows)


class FakeTracker:
    def __init__(self, rows) -> None:
        self.store = FakeStore(rows)


class FakeImports:
    def history(self, limit):
        return [{"mode": "copy", "files": 12, "bytes": 10 ** 9}]


class ProgressTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.cfg = Config(path=os.path.join(self.dir.name, "config.json"))
        self.path = os.path.join(self.dir.name, "pet.json")
        self.rows = [seg("2026-03-02", 9, 6 * 60), seg("2026-03-03", 9, 2 * 60)]
        self.bus = FakeBus()

    def progress(self) -> ach.PetProgress:
        return ach.PetProgress(self.cfg, self.bus, tracker=FakeTracker(self.rows), importer=FakeImports(),
                               path=self.path, clock=lambda: 1_000_000.0, today=lambda: date(2026, 3, 4))

    def test_earned_once_kept_and_announced(self) -> None:
        first = self.progress()
        news = first.refresh()
        ids = {n["id"] for n in news}
        self.assertTrue({"maal1", "kort1", "lv5"} <= ids)
        kind, data = self.bus.events[-1]
        self.assertEqual((kind, data["foerste"]), ("pet_progress", True))
        self.assertEqual(first.refresh(), [])                                  # nothing new
        again = self.progress()                                                # a restart
        self.assertEqual(again.refresh(), [])
        self.assertEqual(again.counts()["unlocked"], len([t for t in ach.TROPHIES if t.id in ids]))
        self.rows.append(seg("2026-12-07", 9, 70))                              # December: a new trophy
        news = again.refresh()
        self.assertEqual([n["id"] for n in news if n["kind"] == "trofae"], ["jul"])
        self.assertFalse(self.bus.events[-1][1]["foerste"])
        self.assertEqual(news[0]["reward"]["id"], "nissehue")

    def test_the_wardrobe(self) -> None:
        pet = self.progress()
        pet.refresh()
        self.assertEqual(pet.equipped(), ach.DEFAULTS)
        self.assertEqual(pet.equip("hat", "festhat")["equipped"]["hat"], "festhat")    # from "Mål!"
        self.assertEqual(self.bus.events[-1], ("pet_look", {"equipped": {**ach.DEFAULTS, "hat": "festhat"}}))
        for slot, item, message in (("hat", "awp", "Ugyldig værdi: item"), ("hat", "nissehue", "Nissehue er ikke låst op"),
                                    ("sko", "festhat", "Ugyldig værdi: slot"), ("haand", None, "Ugyldig værdi: item")):
            with self.subTest(item=item), self.assertRaisesRegex(ValueError, message):
                pet.equip(slot, item)
        self.assertEqual(self.progress().equipped()["hat"], "festhat")          # kept

    def test_state_hides_the_secrets(self) -> None:
        state = self.progress().state()
        secret = next(t for t in state["trophies"] if t["id"] == "fredag13")
        self.assertEqual((secret["name"], secret["text"], secret["reward"]), ("???", "Hemmelig", None))
        uge = next(t for t in state["trophies"] if t["id"] == "uge")
        self.assertEqual((uge["current"], uge["goal"], uge["unit"]), (2, 5, "dage"))
        awp = next(i for i in state["items"] if i["id"] == "awp")
        self.assertEqual((awp["owned"], awp["rarity"]), (False, "legendarisk"))
        self.assertIn("fund", awp["how"])
        zebra = next(i for i in state["items"] if i["id"] == "zebra")
        self.assertEqual(zebra["how"], "???")                                    # from a secret trophy
        self.assertEqual([s["id"] for s in state["slots"]], list(ach.SLOTS))

    def test_games_count(self) -> None:
        pet = self.progress()
        for _ in range(9):
            pet.note_game("done", True)
        pet.note_game("touched", False)                       # never came out: does not count
        pet.note_game("touched", True)
        news = pet.refresh()
        self.assertIn("leg10", {n["id"] for n in news})
        self.assertEqual(pet.stats().caught, 1)

    def test_a_broken_file_starts_over(self) -> None:
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{broken")
        pet = self.progress()
        self.assertEqual(len(pet._data["secret"]), 32)
        self.assertEqual(pet.equipped(), ach.DEFAULTS)


if __name__ == "__main__":
    unittest.main()
