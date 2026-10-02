import os
import sqlite3
import tempfile
import unittest
from datetime import date, datetime

from projektsog import config, timetrack
from projektsog.config import Config

_tmp: tempfile.TemporaryDirectory | None = None
_old_appdata: str | None = None


def setUpModule() -> None:
    global _tmp, _old_appdata
    _tmp = tempfile.TemporaryDirectory()
    _old_appdata = os.environ.get("LOCALAPPDATA")
    os.environ["LOCALAPPDATA"] = _tmp.name


def tearDownModule() -> None:
    if _old_appdata is not None:
        os.environ["LOCALAPPDATA"] = _old_appdata
    if _tmp is not None:
        _tmp.cleanup()


class World:
    """Fake signals: the window in front, the last input, Resolve's state and two clocks."""

    def __init__(self) -> None:
        self.t = datetime(2026, 10, 1, 9, 0, 0).timestamp()
        self.mono = 1000.0
        self.last_input = self.t
        self.exe, self.title = "resolve.exe", "DaVinci Resolve Studio - Rikke Lindholm"
        self.act = {"project": "Rikke Lindholm - Testimonial", "database": "Kunder 2026",
                    "uid": "u1", "page": "edit", "timeline": "Testimonial v1", "timecode": "01:00:00:00",
                    "rendering": False, "folder": "Rikke Lindholm"}
        self.resolve_running = True
        self.frozen: dict | None = None      # Resolve busy: its last answer, and since when
        self.frozen_at: float | None = None

    # signal callbacks
    def activity(self, max_age: float = 15.0):
        """Like ResolveBridge.activity(): the last answer, or None when older than max_age."""
        if not self.resolve_running:
            return None
        if self.frozen is not None:
            age = self.t - self.frozen_at
            return None if age > max_age else {**self.frozen, "age": age}
        return {**self.act, "age": 0.0}

    def freeze(self) -> None:
        """Resolve stops answering scripts (busy) - the window stays in front."""
        self.frozen, self.frozen_at = dict(self.act), self.t

    def foreground(self):
        return self.exe, self.title

    def idle(self):
        return max(0.0, self.t - self.last_input)

    def wall(self):
        return self.t

    def monotonic(self):
        return self.mono

    def advance(self, seconds: float, *, typing: bool = True, playing: bool = False) -> None:
        """Let time pass in ticks; the user types (or not) and the playhead moves (or not)."""
        steps = int(seconds // timetrack.TICK_S)
        for _ in range(steps):
            self.t += timetrack.TICK_S
            self.mono += timetrack.TICK_S
            if typing:
                self.last_input = self.t
            if playing:
                frames = int(self.act["timecode"][-2:]) + 1
                self.act["timecode"] = f"01:00:{frames // 25:02d}:{frames % 25:02d}"
            self.tracker.tick()


class TrackerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.cfg = Config(path=os.path.join(self.dir.name, "config.json"))
        self.store = timetrack.TimeStore(os.path.join(self.dir.name, "time.db"))
        self.w = World()
        bridge = type("Bridge", (), {"activity": lambda _self, max_age=15.0: self.w.activity(max_age)})()
        self.tracker = timetrack.TimeTracker(
            self.cfg, bridge, store=self.store, foreground_fn=self.w.foreground,
            idle_fn=self.w.idle, wall=self.w.wall, mono=self.w.monotonic)
        self.w.tracker = self.tracker

    def tearDown(self) -> None:
        self.tracker.stop()
        self.store.close()
        self.dir.cleanup()

    def report(self):
        day = date(2026, 10, 1)
        return self.tracker.report(day, day)

    def minutes(self, bucket: str | None = None) -> float:
        projects = self.report()["projects"]
        if not projects:
            return 0.0
        p = projects[0]
        return round((p["buckets"].get(bucket, 0.0) if bucket else p["total_s"]) / 60.0, 2)


class CountingTests(TrackerCase):
    def test_time_in_resolve_counts_per_page(self) -> None:
        self.w.advance(600)
        self.w.act["page"] = "color"
        self.w.advance(300)
        self.assertAlmostEqual(self.minutes("edit"), 10.0, delta=0.1)
        self.assertAlmostEqual(self.minutes("color"), 5.0, delta=0.1)
        p = self.report()["projects"][0]
        self.assertEqual(p["project"], "Rikke Lindholm - Testimonial")
        self.assertEqual(p["folder"], "Rikke Lindholm")
        self.assertEqual(p["database"], "Kunder 2026")

    def outlook(self) -> None:
        self.w.exe, self.w.title = "outlook.exe", "Indbakke - Outlook"

    def back_to_resolve(self) -> None:
        self.w.exe, self.w.title = "resolve.exe", "DaVinci Resolve Studio"

    def test_a_short_trip_to_another_program_counts(self) -> None:
        self.w.advance(300)
        self.outlook()
        self.w.advance(420)                    # 7 min of mail: no hard pause
        status = self.tracker.status()
        self.assertEqual(status["state"], "away")
        self.assertAlmostEqual(status["away_until"] - status["away_since"], 600, delta=1)
        self.back_to_resolve()
        self.w.advance(300)
        self.assertAlmostEqual(self.minutes(), 17.0, delta=0.2)
        self.assertEqual(len(self.report()["projects"][0]["timelines"]), 1)

    def test_a_long_trip_to_another_program_is_not_counted(self) -> None:
        self.w.advance(300)
        self.outlook()
        self.w.advance(900)                    # 15 min elsewhere: longer than the pause limit
        self.assertEqual(self.tracker.status()["state"], "paused")
        self.back_to_resolve()
        self.w.advance(300)
        self.assertAlmostEqual(self.minutes(), 10.0, delta=0.2)

    def test_leaving_for_good_ends_the_time_when_resolve_was_left(self) -> None:
        self.w.advance(300)
        self.outlook()
        self.w.advance(3600)
        self.assertAlmostEqual(self.minutes(), 5.0, delta=0.1)

    def test_closing_resolve_while_away_ends_the_time_when_it_was_left(self) -> None:
        self.w.advance(300)
        self.outlook()
        self.w.advance(120)
        self.w.resolve_running = False
        self.w.advance(60)
        self.assertEqual(self.tracker.status()["state"], "no-resolve")
        self.assertAlmostEqual(self.minutes(), 5.0, delta=0.1)

    def test_a_busy_resolve_that_answers_slowly_keeps_counting(self) -> None:
        self.w.advance(300)
        self.w.freeze()                        # Resolve busy for 100 s: its last answer still holds
        self.w.advance(100)
        self.w.frozen = None
        self.w.advance(200)
        self.assertAlmostEqual(self.minutes(), 10.0, delta=0.2)
        self.assertEqual(len(self.report()["projects"][0]["timelines"]), 1)

    def test_a_resolve_that_stops_answering_stops_the_clock(self) -> None:
        self.w.advance(300)
        self.w.freeze()                        # no answer for 10 min: after 2 min it is not trusted
        self.w.advance(600)
        self.assertLess(self.minutes(), 5.0 + 2.2 + 0.1)
        self.assertGreater(self.minutes(), 5.0 + 1.8)

    def test_music_sites_count_on_the_open_project(self) -> None:
        self.w.exe, self.w.title = "chrome.exe", "Royalty Free Music for Videos | Artlist - Google Chrome"
        self.w.advance(600)
        self.w.title = "YouTube - Google Chrome"
        self.w.advance(600)
        self.assertAlmostEqual(self.minutes("musik"), 10.0, delta=0.2)
        self.assertAlmostEqual(self.minutes(), 10.0, delta=0.2)

    def test_music_site_without_an_open_project_does_not_count(self) -> None:
        self.w.resolve_running = False
        self.w.exe, self.w.title = "chrome.exe", "Artlist - Google Chrome"
        self.w.advance(600)
        self.assertEqual(self.report()["projects"], [])

    def test_a_short_pause_counts_fully(self) -> None:
        self.w.advance(300)
        self.w.advance(480, typing=False)     # 8 min thinking/watching without input
        self.w.advance(300)
        self.assertAlmostEqual(self.minutes(), 18.0, delta=0.2)

    def test_a_long_pause_is_not_counted_at_all(self) -> None:
        self.w.advance(300)
        self.w.advance(1200, typing=False)    # 20 min away from the desk
        self.w.advance(300)
        self.assertAlmostEqual(self.minutes(), 10.0, delta=0.3)

    def test_playback_counts_as_activity(self) -> None:
        self.w.advance(60)
        self.w.advance(1200, typing=False, playing=True)   # watching a 20 min cut
        self.assertAlmostEqual(self.minutes(), 21.0, delta=0.2)

    def test_playback_left_looping_counts_for_an_hour_at_most(self) -> None:
        self.w.advance(60)
        self.w.advance(5 * 3600, typing=False, playing=True)   # went home, timeline loops all night
        self.assertAlmostEqual(self.minutes(), 61.0, delta=0.2)
        self.assertEqual(self.tracker.status()["state"], "idle")

    def test_rendering_alone_is_not_activity(self) -> None:
        self.w.advance(60)
        self.w.act["rendering"] = True
        self.w.act["page"] = "deliver"
        self.w.advance(1800, typing=False, playing=True)  # 30 min render, nobody there
        self.assertLess(self.minutes(), 1.5)

    def test_the_project_manager_placeholder_does_not_count(self) -> None:
        self.w.act["project"] = "Untitled Project"
        self.w.advance(600)
        self.assertEqual(self.report()["projects"], [])

    def test_sleep_closes_the_running_segment(self) -> None:
        self.w.advance(300)
        self.w.t += 3 * 3600                  # the PC slept for 3 hours
        self.w.mono += 3 * 3600
        self.w.last_input = self.w.t
        self.tracker.tick()
        self.w.advance(60)
        self.assertAlmostEqual(self.minutes(), 6.0, delta=0.3)

    def test_switching_projects_splits_the_time(self) -> None:
        self.w.advance(300)
        self.w.act.update(project="Klar Tand - Silkeborg", uid="u2", folder="Klar Tand - Silkeborg")
        self.w.advance(600)
        names = {p["project"]: round(p["total_s"] / 60) for p in self.report()["projects"]}
        self.assertEqual(names, {"Klar Tand - Silkeborg": 10, "Rikke Lindholm - Testimonial": 5})

    def test_disabled_tracking_records_nothing(self) -> None:
        self.cfg.update({"time_tracking_enabled": False})
        self.w.advance(600)
        self.assertEqual(self.report()["projects"], [])
        self.assertEqual(self.tracker.status()["state"], "off")

    def test_status_reports_the_running_stretch(self) -> None:
        self.w.advance(120)
        status = self.tracker.status()
        self.assertEqual(status["state"], "recording")
        self.assertEqual(status["project"], "Rikke Lindholm - Testimonial")
        self.assertEqual(status["bucket_label"], "Edit")
        self.assertAlmostEqual(status["today_s"], 120, delta=6)


class TimelineTests(TrackerCase):
    def test_time_is_split_by_timeline(self) -> None:
        self.w.advance(600)
        self.w.act["timeline"] = "Teaser"
        self.w.advance(300)
        p = self.report()["projects"][0]
        self.assertEqual([(t["name"], round(t["total_s"] / 60)) for t in p["timelines"]],
                         [("Testimonial v1", 10), ("Teaser", 5)])
        self.assertEqual(set(p["timelines"][1]["buckets"]), {"edit"})
        self.assertEqual(self.tracker.status()["timeline"], "Teaser")

    def test_music_time_counts_on_the_open_timeline(self) -> None:
        self.w.act["timeline"] = "Teaser"
        self.w.exe, self.w.title = "chrome.exe", "Artlist - Google Chrome"
        self.w.advance(300)
        t, = self.report()["projects"][0]["timelines"]
        self.assertEqual((t["name"], set(t["buckets"])), ("Teaser", {"musik"}))

    def test_csv_per_timeline(self) -> None:
        self.w.advance(1800)
        self.w.act["timeline"] = "Teaser"
        self.w.advance(900)
        lines = self.tracker.export_csv(date(2026, 10, 1), date(2026, 10, 1), round_minutes=15,
                                        per_timeline=True).lstrip("﻿").splitlines()
        self.assertEqual(lines[0].split(";")[:4], ["Projekt", "Tidslinje", "Projektmappe", "Database"])
        rows = [line.split(";") for line in lines[1:]]
        self.assertEqual([(r[1], r[-1]) for r in rows], [("Testimonial v1", "0,50"), ("Teaser", "0,25")])

    def test_an_older_time_db_gets_the_timeline_column(self) -> None:
        path = os.path.join(self.dir.name, "old.db")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE segments(id INTEGER PRIMARY KEY, project TEXT NOT NULL, database TEXT,"
                     " uid TEXT, folder TEXT, bucket TEXT NOT NULL, start REAL NOT NULL, end REAL NOT NULL,"
                     " host TEXT)")
        start = datetime(2026, 10, 1, 9, 0).timestamp()
        conn.execute("INSERT INTO segments(project, database, uid, folder, bucket, start, end, host)"
                     " VALUES ('Rikke Lindholm - Testimonial', 'Kunder 2026', 'u1', 'Rikke Lindholm', 'edit', ?, ?, 'PC')",
                     (start, start + 3600))
        conn.commit()
        conn.close()
        store = timetrack.TimeStore(path)
        try:
            tracker = timetrack.TimeTracker(self.cfg, None, store=store)
            p, = tracker.report(date(2026, 10, 1), date(2026, 10, 1))["projects"]
            self.assertEqual(p["timelines"], [{"name": "", "total_s": 3600.0, "buckets": {"edit": 3600.0}}])
            csv_text = tracker.export_csv(date(2026, 10, 1), date(2026, 10, 1), per_timeline=True)
            self.assertIn("Rikke Lindholm - Testimonial;(ukendt);", csv_text)
        finally:
            store.close()


class ReportTests(TrackerCase):
    def test_time_is_split_at_midnight(self) -> None:
        self.w.t = datetime(2026, 10, 1, 23, 50).timestamp()
        self.w.last_input = self.w.t
        self.w.advance(1200)                  # 23:50 -> 00:10
        rep = self.tracker.report(date(2026, 10, 1), date(2026, 10, 2))
        days = rep["projects"][0]["days"]
        self.assertAlmostEqual(days["2026-10-01"] / 60, 10, delta=0.2)
        self.assertAlmostEqual(days["2026-10-02"] / 60, 10, delta=0.2)

    def test_csv_is_excel_friendly_and_rounds_up(self) -> None:
        self.w.advance(1000)                  # 16 min 40 s
        text = self.tracker.export_csv(date(2026, 10, 1), date(2026, 10, 1), round_minutes=15)
        self.assertTrue(text.startswith("﻿"))
        lines = text.lstrip("﻿").splitlines()
        self.assertEqual(lines[0].split(";")[:3], ["Projekt", "Projektmappe", "Database"])
        row = lines[1].split(";")
        self.assertEqual(row[0], "Rikke Lindholm - Testimonial")
        self.assertEqual(row[-1], "0,50")      # 16:40 rounded up to 30 min
        self.assertEqual(row[-2], "0:17")
        per_day = self.tracker.export_csv(date(2026, 10, 1), date(2026, 10, 1), per_day=True)
        self.assertEqual(per_day.lstrip("﻿").splitlines()[1].split(";")[0], "2026-10-01")

    def test_per_day_rows_hold_that_day_s_pages(self) -> None:
        self.w.advance(3600)                  # Thursday: an hour in Edit
        self.w.t = datetime(2026, 10, 2, 9, 0).timestamp()
        self.w.mono += 3600 * 20
        self.w.last_input = self.w.t
        self.w.act["page"] = "color"
        self.w.advance(1800)                  # Friday: half an hour in Color
        p = self.tracker.report(date(2026, 10, 1), date(2026, 10, 2))["projects"][0]
        self.assertEqual(set(p["day_buckets"]["2026-10-01"]), {"edit"})
        self.assertEqual(set(p["day_buckets"]["2026-10-02"]), {"color"})
        lines = self.tracker.export_csv(date(2026, 10, 1), date(2026, 10, 2), per_day=True) \
            .lstrip("﻿").splitlines()
        self.assertEqual(lines[0].split(";")[4:6], ["Edit (t)", "Color (t)"])
        self.assertEqual(lines[1].split(";")[4:6], ["1,00", "0,00"])
        self.assertEqual(lines[2].split(";")[4:6], ["0,00", "0,50"])


class SettingsTests(unittest.TestCase):
    def test_defaults(self) -> None:
        self.assertIs(config.DEFAULTS["time_tracking_enabled"], True)
        self.assertEqual(config.DEFAULTS["time_idle_minutes"], 10)
        self.assertIn("Artlist", config.DEFAULTS["time_music_sites"])

    def test_bounds(self) -> None:
        self.assertEqual(config.validate({"time_idle_minutes": 15, "time_round_minutes": 0}),
                         {"time_idle_minutes": 15, "time_round_minutes": 0})
        for changes, error in (({"time_idle_minutes": 0}, "Pausegrænsen"),
                               ({"time_idle_minutes": 500}, "Pausegrænsen"),
                               ({"time_round_minutes": 300}, "Afrunding")):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, error):
                config.validate(changes)


if __name__ == "__main__":
    unittest.main()
