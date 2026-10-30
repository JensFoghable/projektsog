"""Fun in the office (SPEC §23.1) in achievements.py: seasons and costumes, the new wardrobe slot and
items, counters and their trophies, treats, Halloween pranks, the Christmas calendar, the render
game's records and the year review's data."""

import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta

from projektsog import achievements as ach
from projektsog.config import Config
from tests.test_achievements import FakeTracker, seg
from tests.test_petplay import FakeBus


def earned(st: ach.Stats, trophy_id: str) -> bool:
    trophy = ach.TROPHIES_BY_ID[trophy_id]
    return trophy.measure(st) >= trophy.goal


class SeasonTests(unittest.TestCase):
    def test_the_halloween_week_is_the_week_of_31_october(self) -> None:
        self.assertEqual([d for d in range(20, 32) if ach.halloween_week(date(2026, 10, d))],
                         [26, 27, 28, 29, 30, 31])                         # uge 44 in 2026 …
        self.assertTrue(ach.halloween_week(date(2026, 11, 1)))             # … Monday to Sunday
        self.assertFalse(ach.halloween_week(date(2026, 11, 2)))
        self.assertTrue(ach.halloween_week(date(2027, 10, 25)))            # 2027: 31 Oct is a Sunday
        self.assertFalse(ach.halloween_week(date(2027, 11, 1)))
        self.assertEqual([ach.season(date(2026, 10, 28)), ach.season(date(2026, 12, 1)),
                          ach.season(date(2026, 12, 31)), ach.season(date(2026, 11, 15))],
                         ["halloween", "jul", "jul", None])

    def test_each_pc_its_own_costume(self) -> None:
        self.assertEqual(ach.costume("a" * 32, 2026), ach.costume("a" * 32, 2026))
        self.assertIn(ach.costume("a" * 32, 2026), ach.KOSTUMER)
        dressed = {ach.costume(f"{n:032x}", 2026) for n in range(40)}
        self.assertEqual(dressed, set(ach.KOSTUMER))                       # colleagues dress differently
        self.assertNotEqual([ach.costume("b" * 32, y) for y in range(2026, 2036)],
                            [ach.costume("b" * 32, 2026)] * 10)            # and next year another

    def test_fyraften_hours(self) -> None:
        self.assertEqual(ach.fyraften_hours("16:00"), 16.0)
        self.assertEqual(ach.fyraften_hours("17:30"), 17.5)
        for odd in ("", None, "fem", "25:00", 16):
            self.assertEqual(ach.fyraften_hours(odd), 16.0)

    def test_next_midnight(self) -> None:
        now = datetime(2026, 10, 28, 14, 30).timestamp()
        self.assertEqual(ach.next_midnight(now), datetime(2026, 10, 29).timestamp())


class WardrobeTests(unittest.TestCase):
    def test_the_hals_slot_and_the_new_things(self) -> None:
        self.assertEqual(ach.SLOTS, ("farve", "striber", "hat", "briller", "mund", "hals", "haand", "aura"))
        self.assertEqual((ach.SLOT_NAMES["hals"], ach.DEFAULTS["hals"]), ("Om halsen", "ingen-hals"))
        expected = {
            "heksehat": ("hat", "sjælden"), "nathue": ("hat", "almindelig"), "racerhjelm": ("hat", "sjælden"),
            "gevir": ("hat", "almindelig"), "glorie": ("hat", "almindelig"), "julestjerne": ("hat", "legendarisk"),
            "racerbriller": ("briller", "almindelig"), "julebriller": ("briller", "almindelig"),
            "vampyrtaender": ("mund", "sjælden"), "halstoerklaede": ("hals", "almindelig"),
            "julesloejfe": ("hals", "almindelig"), "graeskar": ("haand", "sjælden"), "klokke": ("haand", "almindelig"),
            "brev": ("haand", "almindelig"), "krystalkugle": ("haand", "sjælden"), "badeand": ("haand", "almindelig"),
            "slikstok": ("haand", "almindelig"), "julegave": ("haand", "almindelig"), "bobler": ("aura", "almindelig"),
            "snefnug": ("aura", "almindelig"), "julelys": ("aura", "almindelig"), "julerod": ("farve", "almindelig"),
            "polkagris": ("striber", "almindelig")}
        for item_id, (slot, rarity) in expected.items():
            with self.subTest(item=item_id):
                item = ach.ITEMS_BY_ID[item_id]
                self.assertEqual((item.slot, item.rarity), (slot, rarity))
        self.assertEqual(ach.ITEMS_BY_ID["vampyrtaender"].name, "Vampyrtænder")
        # every slot's items stay together (the wardrobe lists them in this order)
        slots = [item.slot for item in ach.ITEMS]
        self.assertEqual(slots, sorted(slots, key=ach.SLOTS.index))


class TrophyTests(unittest.TestCase):
    def test_the_twelve_new_trophies(self) -> None:
        self.assertEqual(len(ach.TROPHIES), 59)
        new = {"halloween": "heksehat", "slikmutter": "graeskar", "ballademager": "vampyrtaender",
               "julekalender": None, "hjemtid": "nathue", "frokost10": "klokke", "postbud": "brev",
               "spaamand": "krystalkugle", "racer": "racerbriller", "fejlfri": "racerhjelm", "bad1": "badeand",
               "bad10": "bobler"}
        for trophy_id, reward in new.items():
            with self.subTest(trophy=trophy_id):
                self.assertEqual(ach.TROPHIES_BY_ID[trophy_id].reward, reward)
        self.assertTrue(ach.TROPHIES_BY_ID["ballademager"].secret and ach.TROPHIES_BY_ID["fejlfri"].secret)
        groups = list(dict.fromkeys(t.group for t in ach.TROPHIES))       # the panel's order
        self.assertEqual(groups, ["Vækst", "Rytme", "Mål", "Fokus", "Sider", "Projekter", "Kort", "Klippe", "Mad",
                                  "Kontoret", "Sæson", "Hemmelig"])

    def test_counters_earn_trophies(self) -> None:
        st = ach.compute_stats([], counters={"frokost_ringet": 10, "post_sendt": 9, "vaeddemaal_vundet": 3,
                                             "bade": 1, "spil_bedste": 1200, "slik_givet": 5, "hatte_stjaalet": 1,
                                             "jul_laager": 24, "noget": "x"})
        self.assertTrue(all(earned(st, t) for t in ("frokost10", "spaamand", "bad1", "racer", "slikmutter",
                                                     "ballademager", "julekalender")))
        self.assertFalse(any(earned(st, t) for t in ("postbud", "bad10", "fejlfri")))
        self.assertNotIn("noget", st.counters)

    def test_the_halloween_week_trophy(self) -> None:
        self.assertTrue(earned(ach.compute_stats([seg("2026-10-27", 9, 61)]), "halloween"))
        self.assertFalse(earned(ach.compute_stats([seg("2026-10-27", 9, 30)]), "halloween"))    # under an hour
        self.assertFalse(earned(ach.compute_stats([seg("2026-10-20", 9, 61)]), "halloween"))

    def test_home_on_time_follows_fyraften(self) -> None:
        days = [date(2026, 3, 2) + timedelta(days=n) for n in range(14) if (date(2026, 3, 2) + timedelta(days=n)).weekday() < 5]
        self.assertEqual(len(days), 10)
        on_time = [seg(d.isoformat(), 9, 7 * 60 + 10) for d in days]        # 9:00–16:10
        self.assertTrue(earned(ach.compute_stats(on_time), "hjemtid"))
        late = [seg(d.isoformat(), 9, 7 * 60 + 30) for d in days]           # until 16:30
        self.assertFalse(earned(ach.compute_stats(late), "hjemtid"))
        self.assertTrue(earned(ach.compute_stats(late, fyraften=17.0), "hjemtid"))
        weekend = [seg(f"2026-03-{d:02d}", 9, 120) for d in (7, 8, 14, 15, 21, 22, 28, 29)] + on_time[:9]
        self.assertFalse(earned(ach.compute_stats(weekend), "hjemtid"))      # weekends never count

    def test_a_night_past_midnight_is_never_home_on_time(self) -> None:
        # 9:00–15:50, and back for one unbroken stretch 22:30–00:40: that day ends at midnight, not 15:50.
        days = [date(2026, 3, 2) + timedelta(days=n) for n in range(14) if (date(2026, 3, 2) + timedelta(days=n)).weekday() < 5]
        nights = [s for d in days for s in (seg(d.isoformat(), 9, 6 * 60 + 50), seg(d.isoformat(), 22.5, 130))]
        st = ach.compute_stats(nights)
        self.assertEqual(st.day_last["2026-03-02"], 24.0)
        self.assertAlmostEqual(st.day_last["2026-03-03"], 24.0)          # (its own night, after 00:40)
        self.assertFalse(earned(st, "hjemtid"))
        self.assertFalse(earned(ach.compute_stats(nights, goal_hours=3), "fyraften"))
        # A stretch that ends exactly at midnight ends that day at 24:00 too.
        st = ach.compute_stats([seg("2026-03-02", 22, 120)])
        self.assertEqual((st.day_last["2026-03-02"], "2026-03-03" in st.day_last), (24.0, False))
        # A day's own stretches are as before.
        self.assertEqual(ach.compute_stats([seg("2026-03-02", 9, 120)]).day_last["2026-03-02"], 11.0)

    def test_overtime_is_still_never_rewarded(self) -> None:
        heavy = ach.compute_stats([seg("2026-10-31", 8, 14 * 60), seg("2026-11-01", 20, 4 * 60)])   # a Halloween weekend
        steady = ach.compute_stats([seg("2026-10-27", 9, 6 * 60), seg("2026-10-28", 9, 6 * 60)])
        unlocked = lambda st: {t.id for t in ach.TROPHIES if t.measure(st) >= t.goal}   # noqa: E731
        self.assertEqual(unlocked(heavy) - unlocked(steady) - {"lv5"}, set())


class CalendarOrderTests(unittest.TestCase):
    def test_24_gifts_snow_on_1_and_13_the_star_on_24(self) -> None:
        self.assertEqual(len(ach.JULEKALENDER), 24)
        self.assertEqual(len({g.id for g in ach.JULEKALENDER}), 24)
        order = ach.calendar_order("a" * 32, 2026)
        self.assertEqual(len({g.id for g in order}), 24)
        self.assertEqual([order[0].id, order[12].id, order[23].id], ["sne1", "lucia", "julestjerne"])
        self.assertEqual((order[0].kind, order[23].item, order[23].sne_s), ("sne", "julestjerne", 60))
        kinds = [g.kind for g in ach.JULEKALENDER]
        self.assertEqual({k: kinds.count(k) for k in set(kinds)}, {"sne": 2, "ting": 13, "mad": 6, "vits": 3})
        self.assertTrue(all(g.tekst and g.name and g.ikon for g in ach.JULEKALENDER))

    def test_shuffled_per_pc_and_year(self) -> None:
        order = [g.id for g in ach.calendar_order("a" * 32, 2026)]
        self.assertEqual(order, [g.id for g in ach.calendar_order("a" * 32, 2026)])
        others = {tuple(g.id for g in ach.calendar_order(f"{n:032x}", 2026)) for n in range(10)}
        self.assertGreater(len(others), 5)
        self.assertNotEqual(order, [g.id for g in ach.calendar_order("a" * 32, 2027)])

    def test_public_shape(self) -> None:
        gift = ach.JULEKALENDER_BY_ID["gevir"].public()
        self.assertEqual((gift["kind"], gift["item"], gift["reward"]["slot"]), ("ting", "gevir", "hat"))
        self.assertNotIn("points", gift)
        self.assertEqual(ach.JULEKALENDER_BY_ID["risalamande"].public()["points"], 30)


class ProgressCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.cfg = Config(path=os.path.join(self.dir.name, "config.json"))
        self.path = os.path.join(self.dir.name, "pet.json")
        self.rows: list[tuple] = []
        self.bus = FakeBus()
        self.day = date(2026, 10, 28)
        self.now = datetime(2026, 10, 28, 11).timestamp()
        self.snow: list[tuple] = []

    def pet(self) -> ach.PetProgress:
        pet = ach.PetProgress(self.cfg, self.bus, tracker=FakeTracker(self.rows), path=self.path,
                              clock=lambda: self.now, today=lambda: self.day)
        pet.on_snow = lambda seconds, demo=False: self.snow.append((seconds, demo))
        return pet

    def at(self, day: date, hour: int = 11) -> None:
        self.day = day
        self.now = datetime(day.year, day.month, day.day, hour).timestamp()

    def events(self, kind: str) -> list:
        return [data for k, data in self.bus.events if k == kind]


class CounterTests(ProgressCase):
    def test_counted_in_all_and_per_year(self) -> None:
        pet = self.pet()
        for _ in range(9):
            pet.note("frokost_ringet")
        self.assertNotIn("frokost10", {n["id"] for n in pet.refresh()})
        self.at(date(2027, 1, 4))
        pet.note("frokost_ringet")
        pet.note("post_sendt", 2)
        self.assertIn("frokost10", {n["id"] for n in pet.refresh()})
        again = self.pet()                                                   # kept
        self.assertEqual(again._data["counters"]["frokost_ringet"], 10)
        self.assertEqual(again._data["aar"], {"2026": {"frokost_ringet": 9}, "2027": {"frokost_ringet": 1, "post_sendt": 2}})
        for bad in (("", 1), ("x", 0), ("x", 1.5), (None, 1)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                pet.note(*bad)

    def test_games_count_per_year_too(self) -> None:
        pet = self.pet()
        pet.note_game("touched", True)
        self.assertEqual(pet._data["aar"]["2026"], {"games": 1, "caught": 1})
        self.assertEqual(pet.stats().games, 1)

    def test_a_treat(self) -> None:
        pet = self.pet()
        start = pet.food()["maet"]
        state = pet.treat(10, "Booster")
        self.assertEqual(state["maet"], start + 10)
        self.assertEqual(self.events("pet_mad")[-1], state)
        self.assertEqual(pet.food()["spist"], {})                           # no meal logged
        self.assertEqual(pet.treat(500 / 5, "Risalamande")["maet"], 100.0)   # never over full
        for bad in (0, -5, 101, "10", None, True):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                pet.treat(bad, "x")


class HalloweenTests(ProgressCase):
    def wearing(self, pet: ach.PetProgress, hat: str) -> None:
        with pet._lock:
            pet._data["unlocked"]["maal1"] = 1.0                              # owns the Festhat
            pet._data["equipped"]["hat"] = hat

    def test_the_season_and_this_pcs_costume(self) -> None:
        pet = self.pet()
        state = pet.season_state()
        mine = ach.costume(pet._data["secret"], 2026)
        self.assertEqual(state, {"saeson": "halloween", "kostume": mine, "kostume_aar": mine,
                                 "ballade": None, "bytte": None})
        november = pet.season_state(date(2026, 11, 10))
        self.assertEqual((november["saeson"], november["kostume"], november["kostume_aar"]), (None, None, mine))
        self.assertEqual(pet.season_state(date(2026, 12, 2))["saeson"], "jul")

    def test_a_stolen_hat_comes_back_at_midnight(self) -> None:
        pet = self.pet()
        self.wearing(pet, "festhat")
        self.assertEqual(pet.prank_received("GRAFIK-PC", "Bobby"), {"hat": "festhat", "toiletpapir": False})
        ballade = pet.season_state()["ballade"]
        self.assertEqual((ballade["hat"], ballade["toiletpapir"], ballade["af_pc"], ballade["af_navn"]),
                         ("festhat", False, "GRAFIK-PC", "Bobby"))
        self.assertEqual(ballade["til"], datetime(2026, 10, 29).timestamp())
        self.assertEqual(self.events("pet_saeson")[-1]["ballade"]["hat"], "festhat")
        self.assertEqual(pet.equipped()["hat"], "festhat")                  # still owned and worn
        # A second prank the same day: the hat is gone already – toilet paper
        self.assertEqual(pet.prank_received("KLIPPER-PC", "Rolf"), {"hat": None, "toiletpapir": True})
        ballade = self.pet().season_state()["ballade"]                       # (kept over a restart)
        self.assertEqual((ballade["hat"], ballade["toiletpapir"], ballade["af_pc"]), ("festhat", True, "KLIPPER-PC"))
        self.now = datetime(2026, 10, 29, 0, 0, 1).timestamp()
        self.assertIsNone(pet.season_state()["ballade"])

    def test_no_hat_toilet_paper(self) -> None:
        pet = self.pet()
        self.assertEqual(pet.prank_received("GRAFIK-PC", "x" * 40), {"hat": None, "toiletpapir": True})
        self.assertEqual(pet.season_state()["ballade"]["af_navn"], "x" * 20)

    def test_the_thief_wears_it_and_it_counts(self) -> None:
        pet = self.pet()
        state = pet.prank_won("nissehue", "GRAFIK-PC", "Bobby")
        self.assertEqual((state["bytte"]["hat"], state["bytte"]["fra_pc"]), ("nissehue", "GRAFIK-PC"))
        self.assertNotIn("nissehue", pet.owned())                           # worn, never owned
        self.assertIn("ballademager", {n["id"] for n in pet.refresh()})
        for nothing in (None, "ingen", "awp", "ukendt", 7):                 # toilet paper, or not a hat
            with self.subTest(hat=nothing):
                self.assertEqual(pet.prank_won(nothing, "GRAFIK-PC", "Bobby")["bytte"]["hat"], "nissehue")
        self.assertEqual(pet._data["counters"]["hatte_stjaalet"], 1)
        self.now += 86400
        self.assertIsNone(pet.season_state()["bytte"])


class CalendarTests(ProgressCase):
    def test_closed_outside_december(self) -> None:
        pet = self.pet()
        cal = pet.calendar()
        self.assertEqual((cal["aar"], cal["aktiv"], cal["dag"], cal["uaabnede"]), (2026, False, None, 0))
        self.assertEqual(len(cal["laager"]), 24)
        self.assertFalse(any(d["kan"] or d["aaben"] or d["gave"] for d in cal["laager"]))
        with self.assertRaisesRegex(ValueError, "kun åben i december"):
            pet.open_door(1)

    def test_a_door_a_day_and_missed_ones_later(self) -> None:
        self.at(date(2026, 12, 5))
        pet = self.pet()
        cal = pet.calendar()
        self.assertEqual(([d["nr"] for d in cal["laager"] if d["kan"]], cal["uaabnede"], cal["dag"]),
                         ([1, 2, 3, 4, 5], 5, 5))
        order = ach.calendar_order(pet._data["secret"], 2026)
        answer = pet.open_door(3)
        self.assertEqual(answer["gave"], order[2].public())
        self.assertEqual(self.events("pet_jul")[-1], answer["jul"])
        door = answer["jul"]["laager"][2]
        self.assertEqual((door["aaben"], door["kan"], door["gave"]["id"]), (True, False, order[2].id))
        for nr, message in ((6, "Låge 6 kan først åbnes den 6. december"), (3, "allerede åbnet"),
                            (0, "Ugyldig værdi"), (25, "Ugyldig værdi"), ("1", "Ugyldig værdi"), (True, "Ugyldig værdi")):
            with self.subTest(nr=nr), self.assertRaisesRegex(ValueError, message):
                pet.open_door(nr)
        self.at(date(2026, 12, 31))                                          # missed doors until New Year's Eve
        self.assertEqual(self.pet().calendar()["uaabnede"], 23)

    def test_what_the_doors_do(self) -> None:
        self.at(date(2026, 12, 24))
        pet = self.pet()
        order = ach.calendar_order(pet._data["secret"], 2026)
        by_kind = {}
        for nr, gift in enumerate(order, 1):
            by_kind.setdefault(gift.kind, nr)
        # a wardrobe item: owned for good, celebrated like a find
        nr = next(n for n, g in enumerate(order, 1) if g.kind == "ting" and g.item not in ("julestjerne", "nissehue"))
        item = order[nr - 1].item
        pet.open_door(nr)
        self.assertIn(item, pet.owned())
        news = self.events("pet_progress")[-1]
        self.assertEqual((news["nye"][0]["kind"], news["nye"][0]["id"], news["foerste"]), ("jul", item, False))
        self.assertEqual(news["nye"][0]["text"], f"Fra julekalenderen, låge {nr}")
        self.assertEqual(pet.equip(ach.ITEMS_BY_ID[item].slot, item)["equipped"][ach.ITEMS_BY_ID[item].slot], item)
        # a treat
        before = pet.food()["maet"]
        pet.open_door(by_kind["mad"])
        self.assertEqual(pet.food()["maet"], min(100.0, before + order[by_kind["mad"] - 1].points))
        # snow (door 1) and the star (door 24): snow on the monitor, counted
        pet.open_door(1)
        pet.open_door(24)
        self.assertEqual(self.snow, [(45, False), (60, False)])
        self.assertIn("julestjerne", pet.owned())
        self.assertEqual(pet._data["counters"]["sne"], 2)
        # a joke: just the words
        joke = pet.open_door(by_kind["vits"])["gave"]
        self.assertEqual(joke["kind"], "vits")
        self.assertIn("?", joke["tekst"])

    def test_all_24_doors_and_a_restart(self) -> None:
        self.at(date(2026, 12, 24))
        pet = self.pet()
        for nr in range(1, 25):
            pet.open_door(nr)
        self.assertIn("julekalender", {n["id"] for n in pet.refresh()})
        again = self.pet()
        self.assertTrue(all(d["aaben"] for d in again.calendar()["laager"]))
        self.assertEqual(again.stats().counters["jul_laager"], 24)
        self.at(date(2027, 12, 1))                                          # a new year: a new calendar
        cal = self.pet().calendar()
        self.assertEqual(([d["nr"] for d in cal["laager"] if d["kan"]], sum(d["aaben"] for d in cal["laager"])), ([1], 0))

    def test_the_demo_keeps_nothing(self) -> None:
        pet = self.pet()                                                     # October: still a peek
        order = ach.calendar_order(pet._data["secret"], 2026)
        answer = pet.calendar_demo(1)
        self.assertEqual((answer["demo"], answer["gave"]["id"]), (True, order[0].id))
        self.assertEqual(self.snow, [(20, True)])
        nr = next(n for n, g in enumerate(order, 1) if g.kind == "ting" and g.item != "nissehue")
        pet.calendar_demo(nr)
        self.assertNotIn(order[nr - 1].item, pet.owned())
        self.assertEqual(pet._data["jul"], {})
        with self.assertRaises(ValueError):
            pet.calendar_demo(25)

    def test_without_snow_wired_nothing_breaks(self) -> None:
        self.at(date(2026, 12, 1))
        pet = self.pet()
        pet.on_snow = None
        self.assertEqual(pet.open_door(1)["gave"]["kind"], "sne")

        def broken(seconds, demo=False):
            raise ValueError("Slå Klippe til først")
        pet.on_snow = broken
        self.at(date(2026, 12, 13))
        self.assertEqual(pet.open_door(13)["gave"]["id"], "lucia")

    def test_calendar_things_say_where_they_come_from(self) -> None:
        items = {i["id"]: i for i in self.pet().state()["items"]}
        self.assertEqual(items["gevir"]["how"], "Fra julekalenderen")
        self.assertEqual(items["nissehue"]["how"], "Trofæ: Juleklipper – eller julekalenderen")
        self.assertEqual(items["ingen-hals"]["how"], "Fra start")
        self.assertIn({"id": "hals", "name": "Om halsen"}, self.pet().state()["slots"])


class GameTests(ProgressCase):
    def test_records(self) -> None:
        pet = self.pet()
        demo = pet.game_result(5000, True)
        self.assertEqual((demo["bedste"], demo["ny_rekord"], demo["spil"]["spillet"]), (0, False, 0))
        first = pet.game_result(150, False)
        self.assertEqual((first["bedste"], first["ny_rekord"], first["kontor_rekord"]), (150, True, True))
        self.assertEqual(pet.game_result(120, False)["ny_rekord"], False)
        self.assertEqual(pet.game_state()["spillet"], 2)
        self.assertTrue(pet.office_record("GRAFIK-PC", "Bobby", 400))      # beats everyone, me too
        self.assertFalse(pet.office_record("GRAFIK-PC", "Bobby", 300))     # not better than its own
        self.assertFalse(pet.office_record("KLIPPER-PC", "Rolf", 200))     # kept, but no office record
        mine = pet.game_result(300, False)
        self.assertEqual((mine["ny_rekord"], mine["kontor_rekord"]), (True, False))
        rows = pet.game_state()["kontor"]
        self.assertEqual([(r["pc"], r["score"], r["egen"]) for r in rows],
                         [("GRAFIK-PC", 400, False), (rows[1]["pc"], 300, True), ("KLIPPER-PC", 200, False)])
        self.assertEqual(self.pet().game_state()["bedste"], 300)            # kept
        self.assertEqual(pet._data["aar"]["2026"]["spil_bedste"], 300)
        self.assertEqual(pet._data["aar"]["2026"]["spil"], 3)

    def test_each_game_keeps_its_own_records(self) -> None:
        pet = self.pet()
        pet.game_result(2000, False)                                         # Hop
        skyd = pet.game_result(500, False, "skyd")
        self.assertEqual((skyd["bedste"], skyd["ny_rekord"], skyd["kontor_rekord"]), (500, True, True))
        self.assertEqual(skyd["spil"]["spil"], "skyd")
        self.assertEqual(pet.game_state()["bedste"], 2000)
        self.assertEqual(pet.game_state("kryds")["bedste"], 0)
        self.assertEqual(pet.game_state("kryds")["spillet"], 2)              # rounds of every game
        self.assertTrue(pet.office_record("GRAFIK-PC", "Bobby", 900, "skyd"))
        self.assertFalse(pet.office_record("KLIPPER-PC", "Rolf", 600, "skyd"))     # kept, but no office record
        self.assertEqual([r["pc"] for r in pet.game_state("kryds")["kontor"]], [])
        self.assertEqual([r["score"] for r in pet.game_state("skyd")["kontor"]], [900, 600, 500])
        self.assertEqual([r["score"] for r in pet.game_state()["kontor"]], [2000])
        self.assertFalse(pet.office_record("GRAFIK-PC", "Bobby", 900, "pong"))
        # only Hop counts for the year and the trophies
        pet.game_result(4000, False, "kryds")
        self.assertEqual(pet._data["aar"]["2026"]["spil_bedste"], 2000)
        self.assertNotIn("fejlfri", {n["id"] for n in pet.refresh()})
        self.assertEqual(self.pet().game_state("kryds")["bedste"], 4000)      # kept
        with self.assertRaises(ValueError):
            pet.game_result(100, False, "pong")
        with self.assertRaises(ValueError):
            pet.game_state("pong")

    def test_small_scores_are_no_records_and_bad_ones_are_refused(self) -> None:
        pet = self.pet()
        self.assertFalse(pet.game_result(99, False)["ny_rekord"])
        for bad in (-1, 10_000_001, 1.5, "100", True, None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                pet.game_result(bad, False)
        for pc, score in (("grafik-pc", 10), ("A" * 16, 10), ("GRAFIK-PC", 0), ("GRAFIK-PC", 1.5)):
            with self.subTest(pc=pc, score=score):
                self.assertFalse(pet.office_record(pc, "x", score))

    def test_the_trophies(self) -> None:
        pet = self.pet()
        pet.game_result(1200, False)
        news = {n["id"] for n in pet.refresh()}
        self.assertIn("racer", news)
        self.assertNotIn("fejlfri", news)
        pet.game_result(3000, False)
        self.assertIn("fejlfri", {n["id"] for n in pet.refresh()})

    def test_at_most_32_colleagues(self) -> None:
        pet = self.pet()
        for n in range(40):
            pet.office_record(f"PC-{n:02d}", "x", 100 + n)
        table = pet._data["spil"]["kontor"]
        self.assertEqual(len(table), 32)
        self.assertNotIn("PC-00", table)
        self.assertIn("PC-39", table)
        self.assertEqual(len(pet.game_state()["kontor"]), 10)


class StorageTests(ProgressCase):
    def test_a_broken_record_starts_over(self) -> None:
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"jul": {"2026": [1], "2025": {"1": 5}}, "julegaver": ["gevir", "awp", "gevir", 3],
                       "spil": {"bedste": "x", "kontor": {"A": {"score": "1"}, "B": {"score": 5}},
                                "andre": {"skyd": {"bedste": -4, "kontor": {"C": {"score": 7}, "D": 3}}, "kryds": 5}},
                       "aar": {"2026": 5}, "halloween": [], "counters": {"bade": 2}}, fh)
        pet = self.pet()
        self.assertEqual(pet._data["jul"], {"2025": {"1": 5}})
        self.assertEqual(pet._data["julegaver"], ["gevir"])
        self.assertEqual((pet._data["spil"]["bedste"], list(pet._data["spil"]["kontor"])), (0, ["B"]))
        self.assertEqual(pet._data["spil"]["andre"],
                         {"skyd": {"bedste": 0, "bedste_ved": None, "kontor": {"C": {"score": 7}}},
                          "kryds": {"bedste": 0, "bedste_ved": None, "kontor": {}}})
        self.assertEqual((pet._data["aar"], pet._data["halloween"]), ({}, {}))
        self.assertIn("gevir", pet.owned())
        self.assertEqual(pet.stats().counters["bade"], 2)
        self.assertEqual(pet.season_state()["ballade"], None)


class YearReviewTests(ProgressCase):
    def test_the_year_is_gathered(self) -> None:
        self.rows += [seg("2026-10-05", 9, 6 * 60, project="Rikke Lindholm"),
                      seg("2026-10-06", 8.5, 90, project="Mette Juhl", bucket="color")]
        self.at(date(2026, 12, 10))
        pet = self.pet()
        pet.note("leveringer", 3)
        pet.note("frokost_ringet")
        pet.feed("durum")
        pet.game_result(500, False)
        with pet._lock:
            pet._data["unlocked"]["maal1"] = datetime(2026, 10, 5, 15).timestamp()
            pet._data["unlocked"]["kort1"] = datetime(2025, 5, 1).timestamp()
        review = pet.year_review(2026)
        self.assertEqual((review["aar"], review["timer"], review["projekter"]), (2026, 7.5, 2))
        self.assertEqual(review["top"][0], {"navn": "Rikke Lindholm", "timer": 6.0})
        self.assertEqual((review["leveringer"], review["kontor"]["frokost_ringet"], review["kontor"]["spil_bedste"]),
                         (3, 1, 500))
        self.assertEqual(review["mad"]["yndlings"], {"id": "durum", "navn": "Durum", "antal": 1})
        self.assertEqual(review["klippe"]["trofaeer"], 1)
        self.assertEqual(review["klippe"]["navn"], "Klippe")
        self.assertEqual(review["klippe"]["stage"], "baby")                  # 7.5 hours
        self.assertEqual(pet.year_review(2025)["timer"], 0.0)


if __name__ == "__main__":
    unittest.main()
