"""The import helper's HTTP API (/api/import…, SPEC §17)."""

import unittest

from tests.test_app_server import ServerTestBase, setUpModule, tearDownModule  # noqa: F401

CARD = {"id": "7E3A91C4@E:", "drive": "E:", "camera": "FX9", "clips": 99}


class FakeImporter:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def cards(self):
        return [CARD]

    def job(self):
        return None

    def history(self, limit):
        self.calls.append(("history", limit))
        return []

    def options(self, card):
        self.calls.append(("options", card))
        if card != CARD["id"]:
            raise ValueError("Kortet er ikke sat i længere")
        return {"card": CARD, "suggestions": [], "disks": []}

    def plan(self, card, project, separate=False):
        self.calls.append(("plan", card, project, separate))
        return {"target": project + "\\Klip\\FX9"}

    def create_project(self, root, name):
        self.calls.append(("create", root, name))
        return {"path": root + "\\" + name}

    def start_import(self, card, project, separate=False, mode="copy"):
        self.calls.append(("start", card, project, separate, mode))
        return {"state": "copying"}

    def cancel(self):
        self.calls.append(("cancel",))

    def dismiss(self, card):
        self.calls.append(("dismiss", card))


class ImportEndpointTests(ServerTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.helper = FakeImporter()
        self.server.importer = self.helper

    def post(self, path: str, body: dict):
        return self.req("POST", path, body=body)

    def test_state_options_and_plan(self) -> None:
        self.assertEqual(self.req("GET", "/api/import").json(), {"cards": [CARD], "job": None, "history": []})
        self.assertEqual(self.req("GET", "/api/import/options?card=7E3A91C4%40E%3A").json()["card"], CARD)
        plan = self.req("GET", "/api/import/plan?card=7E3A91C4%40E%3A&project=F%3A%5CK%5CMette&separate=1").json()
        self.assertEqual(plan, {"target": "F:\\K\\Mette\\Klip\\FX9"})
        self.assertIn(("plan", CARD["id"], "F:\\K\\Mette", True), self.helper.calls)

    def test_commands(self) -> None:
        self.assertEqual(self.post("/api/import/project", {"root": "F:\\K", "name": "Mette"}).json(),
                         {"path": "F:\\K\\Mette"})
        self.assertEqual(self.post("/api/import/start", {"card": CARD["id"], "project": "F:\\K\\Mette"}).json(),
                         {"state": "copying"})
        self.post("/api/import/start", {"card": CARD["id"], "project": "F:\\K\\Mette", "separate": True,
                                        "mode": "prepare"})
        self.post("/api/import/start", {"card": CARD["id"], "project": "F:\\K\\Mette", "mode": "move"})
        self.assertEqual(self.post("/api/import/cancel", {}).json(), {"ok": True})
        self.assertEqual(self.post("/api/import/dismiss", {"card": CARD["id"]}).json(), {"ok": True})
        self.assertEqual(self.helper.calls, [
            ("create", "F:\\K", "Mette"), ("start", CARD["id"], "F:\\K\\Mette", False, "copy"),
            ("start", CARD["id"], "F:\\K\\Mette", True, "prepare"), ("start", CARD["id"], "F:\\K\\Mette", False, "move"),
            ("cancel",), ("dismiss", CARD["id"])])

    def test_bad_requests(self) -> None:
        for method, path, body, error in (
                ("GET", "/api/import/options", None, "card mangler"),
                ("GET", "/api/import/options?card=X", None, "Kortet er ikke sat i længere"),
                ("POST", "/api/import/start", {"card": "X"}, "project mangler"),
                ("POST", "/api/import/start", {"card": "X", "project": "F:\\", "mode": "delete"}, "Ugyldig værdi: mode"),
                ("POST", "/api/import/project", {"root": "F:\\K"}, "name mangler")):
            with self.subTest(path=path, body=body):
                response = self.req(method, path, body=body)
                self.assertEqual(response.status, 400)
                self.assertEqual(response.json(), {"error": error})

    def test_without_the_helper(self) -> None:
        self.server.importer = None
        response = self.req("GET", "/api/import")
        self.assertEqual((response.status, response.json()), (400, {"error": "Import er ikke tilgængelig"}))


if __name__ == "__main__":
    unittest.main()
