"""Messages from other programs (messages.py, /api/messages, SPEC §19): the Claude sessions'
Resolve queue shows its questions by Klippe."""

import unittest

from projektsog import messages
from tests.test_app_server import ServerTestBase, setUpModule, tearDownModule  # noqa: F401
from tests.test_petplay import FakeBus

ASK = {"tag": "koe:venter", "titel": "Mette vil bruge Resolve", "tekst": "Portræt · ca. 10 min",
       "knapper": [{"tekst": "Byg nu", "uri": "resolvekoe:byg?navn=Mette&id=7"}], "session": "Mette"}


class BoardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bus = FakeBus()
        self.shown = True
        self.opened: list[str] = []
        self.sounds = 0
        self.now = 1000.0

        def sound() -> None:
            self.sounds += 1
        self.board = messages.MessageBoard(None, self.bus, shown=lambda: self.shown, open_uri=self.opened.append,
                                           sound=sound, clock=lambda: self.now)

    def listed(self):
        return self.board.list()["messages"]

    def test_shown_by_klippe_with_a_sound(self) -> None:
        self.assertEqual(self.board.post(ASK), {"ok": True, "vist": True})
        (message,) = self.listed()
        self.assertEqual((message["tag"], message["titel"], message["knapper"]), (ASK["tag"], ASK["titel"], ASK["knapper"]))
        self.assertEqual(message["udloeber_ved"], 1000 + 3600)
        self.assertEqual(self.bus.events[-1], ("messages", {"messages": [message]}))
        self.assertEqual(self.sounds, 1)
        self.board.post(ASK)                                   # the same again: no second sound
        self.assertEqual(self.sounds, 1)
        self.board.post({**ASK, "tekst": "Portræt · ca. 20 min", "lyd": False})   # silently replaced
        self.assertEqual(self.sounds, 1)
        self.assertEqual([m["tekst"] for m in self.listed()], ["Portræt · ca. 20 min"])

    def test_a_quiet_message_makes_no_sound(self) -> None:
        self.board.post({**ASK, "tag": "koe:hook-Mette", "knapper": [], "prioritet": "stille"})
        self.assertEqual(self.sounds, 0)
        (message,) = self.listed()
        self.assertEqual((message["prioritet"], message["lyd"]), ("stille", False))
        self.board.post(ASK)
        self.assertEqual(self.sounds, 1)
        with self.assertRaises(ValueError):
            self.board.post({**ASK, "prioritet": "haster"})

    def test_not_shown_when_klippe_is_off(self) -> None:
        self.shown = False
        self.assertEqual(self.board.post(ASK), {"ok": True, "vist": False})
        self.assertEqual(self.sounds, 0)
        self.assertEqual(len(self.listed()), 1)               # kept for when Klippe comes back

    def test_newest_first_remove_and_expiry(self) -> None:
        self.board.post({**ASK, "tag": "koe:hook-Mette", "knapper": [], "udloeber": 60})
        self.now += 1
        self.board.post(ASK)
        self.assertEqual([m["tag"] for m in self.listed()], ["koe:venter", "koe:hook-Mette"])
        self.assertEqual(self.board.remove("koe:venter"), {"ok": True})
        self.assertEqual(self.board.remove("koe:nothing"), {"ok": True})
        self.now += 60
        self.assertEqual(self.listed(), [])

    def test_a_button_opens_its_uri_and_answers_the_message(self) -> None:
        self.board.post(ASK)
        self.assertEqual(self.board.click("koe:venter", 0), {"ok": True})
        self.assertEqual(self.opened, ["resolvekoe:byg?navn=Mette&id=7"])
        self.assertEqual(self.listed(), [])
        with self.assertRaisesRegex(ValueError, "Beskeden er der ikke længere"):
            self.board.click("koe:venter", 0)                 # an old button: nothing happens
        self.board.post(ASK)
        with self.assertRaises(ValueError):
            self.board.click("koe:venter", 1)
        self.assertEqual(self.opened, ["resolvekoe:byg?navn=Mette&id=7"])

    def test_a_passing_note_is_said_not_kept(self) -> None:
        self.board.post({**ASK, "tag": "koe:info"})
        self.board.post({"tag": "koe:info", "titel": "▶ Mette går i gang", "visning": "boble"})
        self.assertEqual(self.listed(), [])
        self.assertEqual(self.bus.events[-1], ("say", {"tekst": "▶ Mette går i gang"}))

    def test_only_the_queues_own_links(self) -> None:
        for uri in ("https://example.com", "file:///C:/Windows/notepad.exe", "C:\\Windows\\notepad.exe",
                    "cmd:/c calc", "resolvekoe:byg?x=1 2", "resolvekoe:byg\n"):
            with self.subTest(uri=uri), self.assertRaises(ValueError):
                self.board.post({**ASK, "knapper": [{"tekst": "Go", "uri": uri}]})
        self.assertEqual(self.listed(), [])

    def test_bad_messages(self) -> None:
        for bad in (None, [], {}, {"tag": "x"}, {**ASK, "tag": ""}, {**ASK, "tag": "x" * 81},
                    {**ASK, "knapper": [ASK["knapper"][0]] * 4}, {**ASK, "knapper": [{"tekst": "", "uri": "resolvekoe:a"}]},
                    {**ASK, "udloeber": 0}, {**ASK, "udloeber": 90000}, {**ASK, "lyd": "nej"},
                    {**ASK, "visning": "popup"}, {**ASK, "titel": 5}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.board.post(bad)

    def test_twenty_at_most(self) -> None:
        for i in range(25):
            self.board.post({**ASK, "tag": f"t{i}"})
        self.assertEqual(len(self.listed()), messages.MAX_MESSAGES)
        self.assertEqual(self.listed()[0]["tag"], "t24")


class MessageEndpointTests(ServerTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.opened: list[str] = []
        self.server.messages = messages.MessageBoard(None, FakeBus(), shown=lambda: True,
                                                     open_uri=self.opened.append, sound=lambda: None)

    def test_post_list_click_and_remove(self) -> None:
        self.assertEqual(self.req("POST", "/api/messages", body=ASK).json(), {"ok": True, "vist": True})
        listed = self.req("GET", "/api/messages").json()["messages"]
        self.assertEqual([m["tag"] for m in listed], ["koe:venter"])
        self.assertEqual(self.req("POST", "/api/messages/click", body={"tag": "koe:venter", "knap": 0}).json(),
                         {"ok": True})
        self.assertEqual(self.opened, ["resolvekoe:byg?navn=Mette&id=7"])
        self.req("POST", "/api/messages", body=ASK)
        self.assertEqual(self.req("DELETE", "/api/messages", body={"tag": "koe:venter"}).json(), {"ok": True})
        self.assertEqual(self.req("GET", "/api/messages").json(), {"messages": []})

    def test_guarded_and_checked(self) -> None:
        self.assertEqual(self.req("POST", "/api/messages", body=ASK, token=False).status, 403)
        response = self.req("POST", "/api/messages", body={**ASK, "knapper": [{"tekst": "x", "uri": "https://x"}]})
        self.assertEqual((response.status, response.json()), (400, {"error": "Knappen må kun åbne resolvekoe:"}))
        response = self.req("POST", "/api/messages/click", body={"tag": "koe:venter", "knap": "0"})
        self.assertEqual((response.status, response.json()), (400, {"error": "Ugyldig værdi: knap"}))

    def test_without_the_board(self) -> None:
        self.server.messages = None
        response = self.req("GET", "/api/messages")
        self.assertEqual((response.status, response.json()), (400, {"error": "Beskeder er ikke tilgængelige"}))


if __name__ == "__main__":
    unittest.main()
