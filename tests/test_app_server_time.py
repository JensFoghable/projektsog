"""The time-tracking endpoints of the HTTP API (/api/time, /api/time/status, /api/time/export)."""

import os
import unittest
from datetime import date
from urllib.parse import unquote

from tests import _app_fakes as fakes
from tests.test_app_server import ServerTestBase, setUpModule, tearDownModule  # noqa: F401


class FakeTimeTracker:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def report(self, first: date, last: date) -> dict:
        self.calls.append(("report", first, last))
        return {"from": first.isoformat(), "to": last.isoformat(), "projects": [], "total_s": 0.0,
                "buckets": {"edit": "Edit"}}

    def status(self) -> dict:
        self.calls.append(("status",))
        return {"state": "recording", "project": "Rikke Lindholm - Testimonial", "today_s": 3600}

    def export_csv(self, first: date, last: date, round_minutes: int = 0, per_day: bool = False,
                   per_timeline: bool = False) -> str:
        self.calls.append(("export", first, last, round_minutes, per_day, per_timeline))
        return "﻿Projekt;Total (t)\r\nRikke Lindholm - Testimonial;1,00\r\n"


class TimeEndpointTests(ServerTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.tracker = FakeTimeTracker()
        self.server.tracker = self.tracker

    def test_report_for_a_period_with_live_status(self) -> None:
        data = self.req("GET", "/api/time?from=2026-09-01&to=2026-09-30").json()
        self.assertEqual(data["report"]["from"], "2026-09-01")
        self.assertEqual(data["status"]["state"], "recording")
        self.assertIn(("report", date(2026, 9, 1), date(2026, 9, 30)), self.tracker.calls)

    def test_report_defaults_to_today(self) -> None:
        data = self.req("GET", "/api/time").json()
        today = date.today().isoformat()
        self.assertEqual((data["report"]["from"], data["report"]["to"]), (today, today))

    def test_status(self) -> None:
        self.assertEqual(self.req("GET", "/api/time/status").json()["today_s"], 3600)

    def test_export_is_a_csv_download(self) -> None:
        response = self.req("GET", "/api/time/export?from=2026-10-01&to=2026-10-31&round=15&detail=day")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Content-Type"], "text/csv; charset=utf-8")
        disposition = response.headers["Content-Disposition"]
        self.assertTrue(disposition.startswith("attachment;"))
        self.assertIn("Projektsøg tid 2026-10-01 til 2026-10-31.csv",
                      unquote(disposition.split("filename*=UTF-8''", 1)[1]))
        self.assertTrue(response.body.decode("utf-8").startswith("﻿Projekt;"))
        self.assertIn(("export", date(2026, 10, 1), date(2026, 10, 31), 15, True, False), self.tracker.calls)
        self.req("GET", "/api/time/export?from=2026-10-01&detail=timeline")
        self.assertIn(("export", date(2026, 10, 1), date(2026, 10, 1), 0, False, True), self.tracker.calls)

    def test_bad_requests_are_400_with_danish_errors(self) -> None:
        for path, error in (
                ("/api/time?from=1-10-2026", "Ugyldig dato: from (brug ÅÅÅÅ-MM-DD)"),
                ("/api/time?from=2026-10-05&to=2026-10-01", "Slutdatoen ligger før startdatoen"),
                ("/api/time?from=2024-01-01&to=2026-01-01", "Vælg højst 400 dage ad gangen"),
                ("/api/time/export?round=-5", "Ugyldig værdi: round"),
                ("/api/time/export?round=600", "Afrunding må højst være 240 minutter"),
                ("/api/time/export?detail=week", "Ugyldig værdi: detail")):
            with self.subTest(path=path):
                response = self.req("GET", path)
                self.assertEqual(response.status, 400)
                self.assertEqual(response.json(), {"error": error})

    def test_without_a_tracker(self) -> None:
        self.server.tracker = None
        response = self.req("GET", "/api/time")
        self.assertEqual(response.status, 400)
        self.assertEqual(response.json(), {"error": "Tidsregistrering er ikke tilgængelig"})


if __name__ == "__main__":
    unittest.main()
