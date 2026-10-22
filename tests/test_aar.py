"""Klippet <år> (aar.py, SPEC §23.11): one year in review from the time, the cards, the meals and
the counters – only that year, nothing about late nights."""

import unittest
from datetime import date, datetime

from projektsog import aar
from tests.test_achievements import seg


def at(day: str, hour: float = 12.0) -> float:
    return datetime.fromisoformat(day).timestamp() + hour * 3600


class YearReviewTests(unittest.TestCase):
    def review(self, **kwargs):
        kwargs.setdefault("today", date(2026, 12, 10))
        return aar.year_review(2026, **kwargs)

    def test_an_empty_year(self) -> None:
        r = self.review()
        self.assertEqual((r["aar"], r["fra"], r["til"], r["siden"], r["dage"], r["timer"], r["projekter"]),
                         (2026, "2026-01-01", "2026-12-31", None, 0, 0.0, 0))
        self.assertEqual((r["top"], r["sider"], r["bedste_dag"], r["ugedag"], r["tidligst"]), ([], [], None, None, None))
        self.assertEqual(r["kort"], {"antal": 0, "tb": 0.0, "filer": 0, "kamera": None})
        self.assertEqual(r["mad"], {"i_alt": 0, "spist": {}, "yndlings": None})
        self.assertEqual(r["kontor"], {k: 0 for k in aar.KONTOR_TAELLERE})
        self.assertEqual(r["klippe"], {"navn": "Klippe", "stage": "egg", "level": 1, "trofaeer": 0, "fund": 0})

    def test_time_projects_pages_and_days(self) -> None:
        rows = [seg("2026-09-30", 9, 60, project="Rikke Lindholm"),                 # a Wednesday
                seg("2026-10-06", 7.7, 6 * 60, project="Rikke Lindholm", bucket="color"),   # a Tuesday, 07:42
                seg("2026-10-07", 9, 120, project="Mette Juhl", bucket="fusion"),
                seg("2026-10-08", 9, 2, project="Åbnet ved en fejl"),            # 2 minutes: no project of the year
                seg("2026-10-09", 9, 30, project="Klar Tand", bucket="musik")]
        r = self.review(segments=rows)
        self.assertEqual(r["siden"], "2026-09-30")
        self.assertEqual((r["timer"], r["dage"], r["projekter"]), (9.5, 3, 3))      # 572 minutes in all
        self.assertEqual(r["top"], [{"navn": "Rikke Lindholm", "timer": 7.0}, {"navn": "Mette Juhl", "timer": 2.0},
                                    {"navn": "Klar Tand", "timer": 0.5}])
        self.assertEqual([(s["side"], s["navn"]) for s in r["sider"]],
                         [("color", "Color"), ("fusion", "Fusion"), ("edit", "Edit"), ("musik", "Musik og lyd")])
        self.assertEqual(r["bedste_dag"], {"dato": "2026-10-06", "timer": 6.0})
        self.assertEqual(r["ugedag"], "tirsdag")
        self.assertEqual(r["tidligst"], "07:42")

    def test_only_this_year_counts(self) -> None:
        rows = [seg("2025-12-31", 23, 120, project="Nytår"),                     # one hour each side of midnight
                seg("2025-06-01", 9, 60, project="Sidste år")]
        r = self.review(segments=rows)
        self.assertEqual((r["timer"], r["siden"], [t["navn"] for t in r["top"]]), (1.0, "2026-01-01", ["Nytår"]))

    def test_focus_and_breaks(self) -> None:
        rows = [seg("2026-03-02", 9, 95), seg("2026-03-02", 10.75, 30)]          # 95 min, a 10-min break, more
        r = self.review(segments=rows)
        self.assertEqual((r["fokus_90"], r["pauser"]), (1, 1))

    def test_cards_meals_trophies_and_the_office(self) -> None:
        imports = [{"at": at("2026-10-02"), "mode": "copy", "camera": "FX9", "files": 120, "bytes": 2 * 10 ** 12},
                   {"at": at("2026-10-03"), "mode": "move", "camera": "FX9", "files": 80, "bytes": 10 ** 12},
                   {"at": at("2026-10-04"), "mode": "copy", "camera": "Drone", "files": 10, "bytes": 5 * 10 ** 10},
                   {"at": at("2026-10-05"), "mode": "prepare", "camera": "A7S", "files": 0},
                   {"at": at("2025-10-05"), "mode": "copy", "camera": "A7S", "files": 50, "bytes": 1}]
        meals = [(at("2026-10-07"), "durum"), (at("2026-10-08"), "booster"), (at("2026-10-09"), "durum"),
                 (at("2025-10-09"), "bigmac"), (at("2026-10-10"), "pizza")]
        counters = {"leveringer": 19, "renders": 64, "frokost_ringet": 9, "spil_bedste": 1480, "sne": 2, "x": "y"}
        unlocked = {"maal1": at("2026-10-05"), "kort1": at("2025-01-01")}
        found = {"kosmos": {"at": at("2026-10-20"), "day": "2026-10-20"}, "neon": {"at": 1.0, "day": "2025-03-03"}}
        r = self.review(imports=imports, meals=meals, counters=counters, unlocked=unlocked, found=found,
                        name="Bobby", stage="junior", level=25)
        self.assertEqual(r["kort"], {"antal": 3, "tb": 3.05, "filer": 210, "kamera": "FX9"})
        self.assertEqual(r["mad"], {"i_alt": 3, "spist": {"durum": 2, "booster": 1},
                                    "yndlings": {"id": "durum", "navn": "Durum", "antal": 2}})
        self.assertEqual((r["leveringer"], r["renders"]), (19, 64))
        self.assertEqual(r["kontor"]["frokost_ringet"], 9)
        self.assertEqual((r["kontor"]["spil_bedste"], r["kontor"]["sne"], r["kontor"]["post_sendt"]), (1480, 2, 0))
        self.assertEqual(r["klippe"], {"navn": "Bobby", "stage": "junior", "level": 25, "trofaeer": 1, "fund": 1})

    def test_a_tie_at_the_table_goes_by_the_menu(self) -> None:
        meals = [(at("2026-10-07"), "booster"), (at("2026-10-08"), "durum")]
        self.assertEqual(self.review(meals=meals)["mad"]["yndlings"]["id"], "durum")


if __name__ == "__main__":
    unittest.main()
