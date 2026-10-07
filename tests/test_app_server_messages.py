"""Messages and calls over HTTP (/api/messages…, SPEC §19, §21.1)."""

import unittest

from projektsog import messages
from tests.test_app_server import ServerTestBase, setUpModule, tearDownModule  # noqa: F401
from tests.test_messages import ASK, DEMO, FakeBus, FakeTimer


class MessageEndpointTests(ServerTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.opened: list[str] = []
        self.internal: list[str] = []
        self.rings: list[bool] = []
        self.board = messages.MessageBoard(None, FakeBus(), shown=lambda: True, open_uri=self.opened.append,
                                           sound=lambda: None, ring=self.rings.append,
                                           on_internal=self.internal.append, timer=FakeTimer)
        self.server.messages = self.board

    def listed(self) -> list[dict]:
        return self.req("GET", "/api/messages").json()["messages"]

    def test_post_list_click_and_remove(self) -> None:
        self.assertEqual(self.req("POST", "/api/messages", body=ASK).json(), {"ok": True, "vist": True})
        self.assertEqual([m["tag"] for m in self.listed()], ["koe:venter"])
        self.assertEqual(self.req("POST", "/api/messages/click", body={"tag": "koe:venter", "knap": 0}).json(),
                         {"ok": True})
        self.assertEqual(self.opened, ["resolvekoe:byg?navn=Mette&id=7"])
        self.req("POST", "/api/messages", body=ASK)
        self.assertEqual(self.req("DELETE", "/api/messages", body={"tag": "koe:venter"}).json(), {"ok": True})
        self.assertEqual(self.req("GET", "/api/messages").json(), {"messages": []})

    def test_answer_a_call(self) -> None:
        self.req("POST", "/api/messages", body=ASK)
        (message,) = self.listed()
        self.assertEqual((message["opkald"], message["besvaret"], message["ringer"]), (True, False, True))
        self.assertEqual(self.req("POST", "/api/messages/svar", body={"tag": "koe:venter"}).json(), {"ok": True})
        (message,) = self.listed()
        self.assertEqual((message["opkald"], message["besvaret"], message["ringer"]), (True, True, False))
        self.assertEqual(self.rings, [True, False])
        for body in ({"tag": "koe:nothing"}, {}, {"tag": 7}):
            with self.subTest(body=body):
                response = self.req("POST", "/api/messages/svar", body=body)
                self.assertEqual((response.status, response.json()),
                                 (400, {"error": "Beskeden er der ikke længere"}))

    def test_guarded_and_checked(self) -> None:
        self.assertEqual(self.req("POST", "/api/messages", body=ASK, token=False).status, 403)
        self.req("POST", "/api/messages", body=ASK)
        response = self.req("POST", "/api/messages/svar", body={"tag": "koe:venter"}, token=False)
        self.assertEqual(response.status, 403)
        self.assertEqual(self.listed()[0]["ringer"], True)                     # a foreign page cannot answer
        response = self.req("POST", "/api/messages", body={**ASK, "knapper": [{"tekst": "x", "uri": "https://x"}]})
        self.assertEqual((response.status, response.json()), (400, {"error": "Knappen må kun åbne resolvekoe:"}))
        response = self.req("POST", "/api/messages/click", body={"tag": "koe:venter", "knap": "0"})
        self.assertEqual((response.status, response.json()), (400, {"error": "Ugyldig værdi: knap"}))

    def test_projektsogs_own_scheme_only_from_inside(self) -> None:
        response = self.req("POST", "/api/messages", body=DEMO)
        self.assertEqual((response.status, response.json()), (400, {"error": "Knappen må kun åbne resolvekoe:"}))
        self.assertEqual(self.listed(), [])
        self.board.post(DEMO, internal=True)                                   # the demo call (crew.py)
        self.assertEqual(self.req("POST", "/api/messages/click", body={"tag": "demo:opkald", "knap": 0}).json(),
                         {"ok": True})
        self.assertEqual((self.internal, self.opened), (["projektsog:demo"], []))

    def test_without_the_board(self) -> None:
        self.server.messages = None
        for method, path, body in (("GET", "/api/messages", None),
                                   ("POST", "/api/messages/svar", {"tag": "koe:venter"})):
            with self.subTest(path=path):
                response = self.req(method, path, body=body)
                self.assertEqual((response.status, response.json()),
                                 (400, {"error": "Beskeder er ikke tilgængelige"}))


if __name__ == "__main__":
    unittest.main()
