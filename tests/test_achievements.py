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


class WorkStore:
    """A time store whose total work the test moves forward."""

    def __init__(self) -> None:
        self.work = 0.0

    def total_s(self) -> float:
        return self.work

    def between(self, start, end):
        return []


class FoodTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.cfg = Config(path=os.path.join(self.dir.name, "config.json"))
        self.path = os.path.join(self.dir.name, "pet.json")
        self.now = datetime(2026, 3, 2, 9).timestamp()        # a Monday morning
        self.tracker = FakeTracker([])
        self.tracker.store = WorkStore()
        self.bus = FakeBus()

    def pet(self) -> ach.PetProgress:
        return ach.PetProgress(self.cfg, self.bus, tracker=self.tracker, path=self.path, clock=lambda: self.now,
                               today=lambda: date(2026, 3, 2))

    def later(self, *, work_h: float = 0.0, rest_h: float = 0.0) -> None:
        self.now += (work_h + rest_h) * 3600
        self.tracker.store.work += work_h * 3600

    def test_hunger_burns_on_work_and_only_a_little_otherwise(self) -> None:
        self.assertEqual(ach.satiety_after(100, 3600, 0), 100 - ach.WORK_BURN)
        self.assertEqual(ach.satiety_after(100, 0, 3600), 100 - ach.REST_BURN)
        self.assertEqual(ach.satiety_after(50, 0, 10 * 24 * 3600), ach.REST_FLOOR)   # a holiday: peckish, no worse
        self.assertEqual(ach.satiety_after(20, 0, 3600), 20)                         # rest never makes it worse
        self.assertEqual(ach.satiety_after(10, 5 * 3600, 0), 0)
        pet = self.pet()
        self.assertEqual(pet.food()["maet"], ach.START_SATIETY)                      # a new Klippe is a bit hungry
        self.later(work_h=1)
        self.assertEqual(pet.food()["maet"], ach.START_SATIETY - ach.WORK_BURN)
        self.later(rest_h=12)
        self.assertEqual(pet.food()["maet"], ach.START_SATIETY - ach.WORK_BURN)      # below the floor already

    def test_feeding_counts_keeps_and_says_no_when_full(self) -> None:
        pet = self.pet()
        answer = pet.feed("durum")
        self.assertEqual((answer["spiste"], answer["grund"], answer["mad"]["maet"]), (True, None, 95.0))
        self.assertEqual(self.bus.events[-1], ("pet_mad", answer["mad"]))
        self.assertEqual([m["id"] for m in answer["mad"]["menu"]], [f.id for f in ach.MENU])
        refused = pet.feed("bigmac")                                                 # ≥ 90: no more food
        self.assertEqual((refused["spiste"], refused["grund"], refused["mad"]["maet"]), (False, "maet", 95.0))
        self.later(work_h=2)
        self.assertTrue(pet.feed("bigmac")["spiste"])
        again = self.pet()                                                           # a restart
        self.assertEqual(again.food()["spist"], {"durum": 1, "bigmac": 1})
        self.assertEqual(again.food()["maet"], 95 - 2 * ach.WORK_BURN + 45)
        for item in (None, "pizza", 3):
            with self.subTest(item=item), self.assertRaisesRegex(ValueError, "Ugyldig værdi: item"):
                pet.feed(item)

    def test_energy_drinks_the_drip_and_a_pounding_heart(self) -> None:
        pet = self.pet()
        first = pet.feed("booster")["mad"]
        self.assertEqual(first["energi"]["item"], "booster")
        self.assertEqual(first["energi"]["til"] - self.now, 20 * 60)
        self.assertEqual(first["energi"]["fra"], self.now)
        self.later(rest_h=0.1)
        drip = pet.feed("drop")["mad"]["energi"]                                     # stacks on what is left …
        self.assertEqual((drip["item"], drip["til"] - self.now), ("drop", 14 * 60 + 45 * 60))
        mango = pet.feed("mangoloco")["mad"]["energi"]                               # … but at most an hour
        self.assertEqual((mango["item"], mango["til"] - self.now), ("mangoloco", ach.ENERGY_MAX_S))
        heart = pet.feed("booster")                                                  # a fourth within two hours
        self.assertEqual((heart["spiste"], heart["grund"]), (False, "hjerte"))
        self.assertTrue(pet.feed("pommes")["spiste"])                                # food is still fine
        self.later(rest_h=2)
        self.assertTrue(pet.feed("booster")["spiste"])
        self.later(rest_h=1.1)
        self.assertIsNone(pet.food()["energi"])                                      # the rush is over

    def test_food_trophies(self) -> None:
        pet = self.pet()
        for _ in range(3):
            pet.feed("booster")
        pet.feed("pommes")
        news = {n["id"]: n for n in pet.refresh()}
        self.assertIn("velbekomme", news)
        self.assertEqual(news["sukkerchok"]["reward"]["id"], "lyn")                  # secret: 3 on one day
        st = pet.stats()
        self.assertEqual((st.eaten, st.drinks_day), ({"booster": 3, "pommes": 1}, 3))
        st = ach.compute_stats([], eaten={"durum": 10, "bigmac": 4, "nuggets": 3, "pommes": 3, "mangoloco": 9,
                                          "pizza": 99})
        self.assertTrue(earned(st, "durum10") and earned(st, "mcd10"))
        self.assertFalse(earned(st, "mango10"))
        self.assertEqual(trophy("durum10").reward, "durum")
        self.assertTrue(trophy("sukkerchok").secret)

    def test_an_unreadable_time_store_leaves_the_anchor_alone(self) -> None:
        self.tracker.store.work = 200 * 3600.0                                       # a long history
        pet = self.pet()
        pet.feed("durum")
        broken = mock.patch.object(WorkStore, "total_s", side_effect=RuntimeError("closed"))
        with broken:                                                                  # time.db closed on exit
            self.assertEqual(pet.food()["maet"], 95.0)
            pet.note_game("quit", True)                                              # … and saved
        self.assertEqual(self.pet().food()["maet"], 95.0)                            # not the whole history burnt

    def test_a_trimmed_pause_is_given_back(self) -> None:
        pet = self.pet()
        pet.food()
        self.later(work_h=0.5)                                                       # a pause counted as work …
        self.assertEqual(pet.food()["maet"], ach.START_SATIETY - 10)
        self.tracker.store.work -= 0.5 * 3600                                        # … and taken back
        self.assertEqual(pet.food()["maet"], ach.START_SATIETY)

    def test_the_drip_keeps_its_own_bag(self) -> None:
        pet = self.pet()
        bag = pet.feed("drop")["mad"]["drop"]
        self.assertEqual((bag["fra"], bag["til"] - self.now), (self.now, 45 * 60))
        self.later(rest_h=10 / 60)
        state = pet.feed("booster")["mad"]                                           # a drink meanwhile
        self.assertEqual((state["energi"]["item"], state["drop"]), ("booster", bag))   # the bag stays
        self.later(rest_h=36 / 60)
        self.assertIsNone(pet.food()["drop"])                                        # empty after 45 min
        self.assertEqual(pet.food()["energi"]["item"], "booster")                    # the rush goes on

    def test_a_broken_food_record_starts_over(self) -> None:
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write('{"mad": {"maet": "x", "spist": [], "log": [[1, "durum"], ["x", "durum"], [2, "pizza"], 5]}}')
        pet = self.pet()
        self.assertEqual(pet._data["mad"]["log"], [[1, "durum"]])
        self.assertEqual(pet.food()["maet"], ach.START_SATIETY)
        self.assertEqual(pet.food()["spist"], {})


if __name__ == "__main__":
    unittest.main()
