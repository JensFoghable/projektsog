"""Messages from other programs (messages.py, SPEC §19, §21.1): the Claude sessions' Resolve queue
shows its questions by Klippe, and a call rings. The HTTP side is in test_app_server_messages.py.

Every board here gets recorders for its sounds and a timer that only fires when told: these tests
never make a sound and never start a thread (except the one test of the real timer, which rings
into a recorder)."""

import json
import os
import sys
import tempfile
import threading
import types
import unittest
import wave
from unittest import mock

from projektsog import messages

ASK = {"tag": "koe:venter", "titel": "Mette vil bruge Resolve", "tekst": "Portræt · ca. 10 min",
       "knapper": [{"tekst": "Byg nu", "uri": "resolvekoe:byg?navn=Mette&id=7"}], "session": "Mette"}
WAIT = {"tag": "koe:lov-Mette", "titel": "🔐 Mette skal have lov", "tekst": "Bash: git push", "session": "Mette"}
DEMO = {"tag": "demo:opkald", "titel": "🎬 Demo vil bruge Resolve",
        "knapper": [{"tekst": "Byg nu", "uri": "projektsog:demo"}]}


class FakeBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def publish(self, kind: str, data=None) -> None:
        self.events.append((kind, data))


class FakeTimer:
    """threading.Timer's shape; ``fire()`` is its time being up."""

    def __init__(self, delay: float, action) -> None:
        self.delay = delay
        self.action = action
        self.started = False
        self.cancelled = False

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        if self.started and not self.cancelled:
            self.action()


class BoardCase(unittest.TestCase):
    """A board whose sounds, uris and timers are all recorded."""

    def setUp(self) -> None:
        self.bus = FakeBus()
        self.cfg: dict | None = None
        self.shown = True
        self.opened: list[str] = []
        self.internal: list[str] = []
        self.sounds = 0
        self.rings: list[bool] = []
        self.under_lock: list[bool] = []          # was the board's lock held while it made a sound?
        self.timers: list[FakeTimer] = []
        self.now = 1000.0
        self.board = self.make()

    def tearDown(self) -> None:
        self.assertNotIn(True, self.under_lock, "a sound was played under the board's lock")

    def make(self, **kwargs) -> messages.MessageBoard:
        def sound() -> None:
            self.sounds += 1
            self.under_lock.append(self.board._lock.locked())

        def ring(on: bool) -> None:
            self.rings.append(on)
            self.under_lock.append(self.board._lock.locked())

        def timer(delay, action) -> FakeTimer:
            self.timers.append(FakeTimer(delay, action))
            return self.timers[-1]
        options = dict(shown=lambda: self.shown, open_uri=self.opened.append, sound=sound, ring=ring,
                       on_internal=self.internal.append, clock=lambda: self.now, timer=timer)
        options.update(kwargs)
        return messages.MessageBoard(self.cfg, self.bus, **options)

    def listed(self):
        return self.board.list()["messages"]

    def one(self, tag: str = "koe:venter") -> dict:
        (message,) = [m for m in self.listed() if m["tag"] == tag]
        return message

    def state(self, tag: str = "koe:venter") -> tuple[bool, bool, bool]:
        message = self.one(tag)
        return message["opkald"], message["besvaret"], message["ringer"]


class BoardTests(BoardCase):
    def test_shown_by_klippe_with_a_sound(self) -> None:
        self.assertEqual(self.board.post(WAIT), {"ok": True, "vist": True})
        (message,) = self.listed()
        self.assertEqual((message["tag"], message["titel"], message["knapper"]), (WAIT["tag"], WAIT["titel"], []))
        self.assertEqual(message["udloeber_ved"], 1000 + 3600)
        self.assertEqual(self.bus.events[-1], ("messages", {"messages": [message]}))
        self.assertEqual((message["opkald"], message["besvaret"], message["ringer"]), (False, True, False))
        self.assertEqual(self.sounds, 1)
        self.board.post(WAIT)                                  # the same again: no second sound
        self.assertEqual(self.sounds, 1)
        self.board.post({**WAIT, "tekst": "Bash: git push --tags", "lyd": False})   # silently replaced
        self.assertEqual(self.sounds, 1)
        self.assertEqual([m["tekst"] for m in self.listed()], ["Bash: git push --tags"])
        self.assertEqual((self.rings, self.timers), ([], []))  # a message that waits never rings

    def test_a_quiet_message_makes_no_sound(self) -> None:
        self.board.post({**ASK, "tag": "koe:hook-Mette", "prioritet": "stille"})
        self.assertEqual((self.sounds, self.rings), (0, []))
        (message,) = self.listed()
        self.assertEqual((message["prioritet"], message["lyd"]), ("stille", False))
        self.assertEqual(self.state("koe:hook-Mette"), (False, True, False))   # buttons, but no call
        self.board.post(WAIT)
        self.assertEqual(self.sounds, 1)
        with self.assertRaises(ValueError):
            self.board.post({**ASK, "prioritet": "haster"})

    def test_not_shown_when_klippe_is_off(self) -> None:
        self.shown = False
        self.assertEqual(self.board.post(ASK), {"ok": True, "vist": False})
        self.board.post(WAIT)
        self.assertEqual((self.sounds, self.rings), (0, []))
        self.assertEqual(len(self.listed()), 2)               # kept for when Klippe comes back
        self.assertEqual(self.state(), (True, False, False))  # a phone that did not ring

    def test_newest_first_remove_and_expiry(self) -> None:
        self.board.post({**WAIT, "udloeber": 60})
        self.now += 1
        self.board.post(ASK)
        self.assertEqual([m["tag"] for m in self.listed()], ["koe:venter", "koe:lov-Mette"])
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
        self.assertEqual(self.internal, [])

    def test_a_passing_note_is_said_not_kept(self) -> None:
        self.board.post({**WAIT, "tag": "koe:info"})
        self.board.post({"tag": "koe:info", "titel": "▶ Mette går i gang", "visning": "boble"})
        self.assertEqual(self.listed(), [])
        self.assertEqual(self.bus.events[-1], ("say", {"tekst": "▶ Mette går i gang"}))

    def test_only_the_queues_own_links(self) -> None:
        for uri in ("https://example.com", "file:///C:/Windows/notepad.exe", "C:\\Windows\\notepad.exe",
                    "cmd:/c calc", "resolvekoe:byg?x=1 2", "resolvekoe:byg\n", "projektsog:demo",
                    "PROJEKTSOG:demo"):
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
        self.assertEqual([t.cancelled for t in self.timers], [True] * 5 + [False] * 20)   # dropped: silent
        self.assertEqual(self.rings, [True])


class FakeConfig(dict):
    """Config's get/on_change: ``set`` runs the listeners like Config.update does."""

    def __init__(self, **values) -> None:
        super().__init__(widget_ring=True, widget_enabled=True, **values)
        self.listeners = []

    def on_change(self, callback) -> None:
        self.listeners.append(callback)

    def set(self, **changes) -> None:
        self.update(changes)
        for callback in self.listeners:
            callback(dict(self))


class CallTests(BoardCase):
    """A call rings (SPEC §21.1): Windows' call sound until it is answered or 30 s have passed."""

    def test_switching_klippe_off_stops_a_ringing_call(self) -> None:
        self.cfg = FakeConfig()
        self.board = self.make()
        self.board.post(ASK)
        self.assertEqual(self.rings, [True])
        self.cfg.set(widget_daily_goal_hours=7, widget_ring=False)     # the short ring is over: rings on
        self.assertEqual(len(self.timers), 1)
        self.cfg.set(widget_enabled=False)                              # Klippe closed with X
        quiet = self.timers[-1]
        self.assertEqual(quiet.delay, 0.0)                             # not on the settings thread
        self.assertEqual(self.rings, [True])
        quiet.fire()
        self.assertEqual(self.rings, [True, False])
        self.assertEqual(self.state(), (True, False, False))           # a missed call
        self.assertFalse(self.bus.events[-1][1]["messages"][0]["ringer"])

    def test_a_call_that_rings_while_another_rings_gets_the_notification(self) -> None:
        self.board.post(ASK)
        self.assertEqual((self.rings, self.sounds), ([True], 0))   # the ring is this call's sound
        self.board.post({**ASK, "tag": "koe:venter-2"})              # the phone already rings
        self.assertEqual((self.rings, self.sounds), ([True], 1))
        self.assertEqual(self.state("koe:venter-2"), (True, False, True))

    def test_a_call_rings_instead_of_the_notification(self) -> None:
        self.assertEqual(self.board.post(ASK), {"ok": True, "vist": True})
        self.assertEqual((self.sounds, self.rings), (0, [True]))
        self.assertEqual(self.state(), (True, False, True))
        self.assertEqual(self.bus.events[-1][1]["messages"][0]["ringer"], True)
        (timer,) = self.timers
        self.assertEqual((timer.delay, timer.started), (messages.RING_S, True))
        self.board.post(ASK)                                   # the same again: rings on, not anew
        self.assertEqual((self.rings, len(self.timers), self.sounds), ([True], 1, 0))

    def test_answering_stops_the_ring(self) -> None:
        self.board.post(ASK)
        events = len(self.bus.events)
        self.assertEqual(self.board.answer("koe:venter"), {"ok": True})
        self.assertEqual(self.state(), (True, True, False))   # now a plain card with "Byg nu"
        self.assertEqual(self.rings, [True, False])
        self.assertTrue(self.timers[0].cancelled)
        self.assertEqual(self.bus.events[events][0], "messages")
        self.assertEqual(self.board.answer("koe:venter"), {"ok": True})   # twice: nothing more
        self.assertEqual(self.rings, [True, False])
        for tag in ("koe:nothing", None, 5):
            with self.subTest(tag=tag), self.assertRaisesRegex(ValueError, "Beskeden er der ikke længere"):
                self.board.answer(tag)
        self.board.click("koe:venter", 0)                      # and then "Byg nu"
        self.assertEqual(self.opened, ["resolvekoe:byg?navn=Mette&id=7"])

    def test_unanswered_after_thirty_seconds_is_a_missed_call(self) -> None:
        self.board.post(ASK)
        events = len(self.bus.events)
        self.now += messages.RING_S
        self.timers[0].fire()
        self.assertEqual(self.state(), (True, False, False))  # still unanswered: a missed call
        self.assertEqual(self.rings, [True, False])
        self.assertEqual(self.bus.events[events][0], "messages")
        self.assertEqual(self.bus.events[events][1]["messages"][0]["ringer"], False)
        self.board.post({**ASK, "lyd": False})                 # the queue re-posts it: no new ring
        self.assertEqual((self.rings, len(self.timers)), ([True, False], 1))
        self.board.answer("koe:venter")
        self.assertEqual(self.state(), (True, True, False))

    def test_a_late_timer_does_not_end_a_new_ring(self) -> None:
        self.board.post(ASK)
        old = self.timers[0]
        self.board.post({**ASK, "tekst": "Portræt · ca. 20 min"})     # news: it rings anew
        self.assertEqual((old.cancelled, len(self.timers)), (True, 2))
        old.action()                                            # the old timer's thread was already running
        self.assertEqual((self.state()[2], self.rings), (True, [True]))
        self.timers[1].fire()
        self.assertEqual((self.state()[2], self.rings), (False, [True, False]))

    def test_click_remove_note_card_and_expiry_end_the_ringing(self) -> None:
        cases = {
            "click": lambda: self.board.click("koe:venter", 0),
            "remove": lambda: self.board.remove(" koe:venter "),
            "note": lambda: self.board.post({"tag": "koe:venter", "titel": "▶ Mette går i gang",
                                             "visning": "boble"}),
            "plain card": lambda: self.board.post({**WAIT, "tag": "koe:venter"}),
            "silent answered card": lambda: self.board.post({**ASK, "knapper": [], "lyd": False}),
        }
        for name, end in cases.items():
            with self.subTest(name):
                self.rings.clear()
                self.board.post({**ASK, "tekst": name})
                timer = self.timers[-1]
                end()
                self.assertEqual(self.rings, [True, False])
                self.assertTrue(timer.cancelled)
                self.assertFalse(any(m["ringer"] for m in self.listed()))
                self.board.remove("koe:venter")

    def test_expiry_ends_the_ringing(self) -> None:
        self.board.post({**ASK, "udloeber": 10})
        self.assertEqual(self.timers[0].delay, 10)           # it expires before RING_S
        self.now += 10
        self.timers[0].fire()
        self.assertEqual((self.listed(), self.rings), ([], [True, False]))
        self.board.post({**ASK, "udloeber": 20})
        self.now += 20
        self.assertEqual(self.listed(), [])                  # pruned by a read before its timer
        self.assertEqual(self.rings, [True, False, True, False])
        self.assertTrue(self.timers[1].cancelled)

    def test_two_calls_ring_until_neither_does(self) -> None:
        self.board.post(ASK)
        self.board.post({**ASK, "tag": "koe:venter-2", "session": "Mette"})
        self.assertEqual(self.rings, [True])                   # one ringtone for both
        self.board.answer("koe:venter")
        self.assertEqual(self.rings, [True])
        self.assertEqual(self.state("koe:venter-2"), (True, False, True))
        self.timers[1].fire()
        self.assertEqual(self.rings, [True, False])

    def test_who_answered_what(self) -> None:
        self.board.post({**ASK, "lyd": False})                 # silent and new: a plain card
        self.assertEqual((self.state(), self.rings, self.sounds), ((True, True, False), [], 0))
        self.board.post({**ASK, "lyd": False, "tekst": "Portræt · ca. 20 min"})
        self.assertEqual(self.state(), (True, True, False))   # a silent re-post keeps the answer
        self.board.post({**ASK, "tekst": "Portræt · ca. 20 min"})
        self.assertEqual(self.state(), (True, True, False))   # nothing new to ring about
        self.board.post(ASK)                                   # a new text with a sound: rings
        self.assertEqual((self.state(), self.rings), ((True, False, True), [True]))
        self.board.post({**ASK, "lyd": False})                 # silently again: still ringing
        self.assertEqual((self.state(), self.rings, len(self.timers)), ((True, False, True), [True], 1))
        self.board.remove("koe:venter")
        self.board.post({**WAIT, "tag": "koe:venter", "titel": ASK["titel"], "tekst": ASK["tekst"]})
        self.board.post(ASK)                                   # a card turned into a call: it rings
        self.assertEqual(self.state(), (True, False, True))

    def test_other_messages_sound_while_the_phone_rings(self) -> None:
        self.board.post(ASK)
        self.board.post(WAIT)                                  # the short ring is long over
        self.assertEqual((self.sounds, self.rings), (1, [True]))

    def test_without_widget_ring_a_call_plays_the_notification_once(self) -> None:
        self.cfg = {"widget_ring": False}
        self.board = self.make()
        self.board.post(ASK)
        self.assertEqual((self.sounds, self.rings, len(self.timers)), (1, [], 1))
        self.assertEqual(self.state(), (True, False, True))   # the phone still rings in Klippe
        self.board.post(ASK)
        self.assertEqual(self.sounds, 1)
        self.board.answer("koe:venter")
        self.cfg["widget_ring"] = True                         # read when the phone starts ringing
        self.board.post({**ASK, "tag": "koe:venter-2"})
        self.assertEqual((self.sounds, self.rings), (1, [True]))

    def test_close_stops_the_ring(self) -> None:
        self.board.post(ASK)
        self.board.close()
        self.assertEqual(self.rings, [True, False])
        self.assertTrue(self.timers[0].cancelled)
        self.board.post({**ASK, "tag": "koe:venter-2"})
        self.assertEqual(self.rings, [True, False])

    def test_the_real_timer_ends_the_ring(self) -> None:
        stopped = threading.Event()

        def ring(on: bool) -> None:
            self.rings.append(on)
            if not on:
                stopped.set()
        board = messages.MessageBoard(None, self.bus, shown=lambda: True, sound=lambda: None, ring=ring,
                                      open_uri=self.opened.append)
        with mock.patch.object(messages, "RING_S", 0.05):
            board.post(ASK)
        self.assertTrue(stopped.wait(5))
        self.assertEqual(self.rings, [True, False])
        (message,) = board.list()["messages"]
        self.assertEqual((message["besvaret"], message["ringer"]), (False, False))

    def test_the_default_timer_is_a_daemon_thread_not_started(self) -> None:
        timer = messages.ring_timer(60, lambda: None)
        self.assertIsInstance(timer, threading.Timer)
        self.assertTrue(timer.daemon)
        self.assertFalse(timer.is_alive())
        timer.cancel()


class InternalTests(BoardCase):
    """``projektsog:`` – only Projektsøg's own messages (the demo call), handed to on_internal."""

    def test_an_internal_button_goes_to_on_internal(self) -> None:
        self.assertEqual(self.board.post(DEMO, internal=True)["ok"], True)
        self.assertEqual(self.state("demo:opkald"), (True, False, True))   # it rings like any call
        self.board.click("demo:opkald", 0)
        self.assertEqual((self.internal, self.opened), (["projektsog:demo"], []))
        self.assertEqual(self.rings, [True, False])
        with self.assertRaisesRegex(ValueError, "Knappen må kun åbne resolvekoe:"):
            self.board.post(DEMO)                              # never from outside
        with self.assertRaisesRegex(ValueError, "Ugyldig uri"):
            self.board.post({**DEMO, "knapper": [{"tekst": "Byg", "uri": "projektsog:demo x"}]}, internal=True)
        self.board.post({**ASK, "tag": "demo:opkald"}, internal=True)       # the queue's scheme too
        self.board.click("demo:opkald", 0)
        self.assertEqual(self.opened, ["resolvekoe:byg?navn=Mette&id=7"])

    def test_without_on_internal_or_when_it_refuses(self) -> None:
        self.board = self.make(on_internal=None)
        self.board.post(DEMO, internal=True)
        with self.assertRaisesRegex(ValueError, "Knappen kunne ikke åbnes"):
            self.board.click("demo:opkald", 0)

        def refuse(uri: str) -> None:
            raise ValueError("Slå Klippe til først")
        self.board = self.make(on_internal=refuse)
        self.board.post(DEMO, internal=True)
        with self.assertRaisesRegex(ValueError, "Slå Klippe til først"):
            self.board.click("demo:opkald", 0)
        self.assertEqual(self.opened, [])

    def test_check_uri(self) -> None:
        self.assertEqual(messages.check_uri("PROJEKTSOG:demo", internal=True), "PROJEKTSOG:demo")
        self.assertEqual(messages.check_uri("resolvekoe:byg", internal=True), "resolvekoe:byg")
        with self.assertRaises(ValueError):
            messages.check_uri("projektsog:demo")
        with self.assertRaises(ValueError):
            messages.check_uri("https://x", internal=True)


class RingSoundTests(unittest.TestCase):
    """The default ringtone, against a stand-in for winsound (never a real sound)."""

    def fake_winsound(self, error: Exception | None = None):
        played: list[tuple] = []

        def play(sound, flags) -> None:
            if error is not None:
                raise error
            played.append((sound, flags))
        module = types.SimpleNamespace(PlaySound=play, SND_ALIAS=0x10000, SND_FILENAME=0x20000, SND_ASYNC=0x1,
                                       SND_NODEFAULT=0x2)
        return module, played

    def test_klippes_short_ring_is_played_once(self) -> None:
        module, played = self.fake_winsound()
        with tempfile.TemporaryDirectory() as folder,                 mock.patch.object(messages, "ringtone_path", lambda: os.path.join(folder, "ring.wav")),                 mock.patch.dict(sys.modules, {"winsound": module}):
            messages.ring_sound(True)
            messages.ring_sound(False)                         # nothing to stop: it is short
        self.assertEqual(played, [(os.path.join(folder, "ring.wav"), 0x20000 | 0x1 | 0x2)])

    def test_the_ring_is_short_and_quiet(self) -> None:
        samples = messages.ringtone_samples()
        self.assertLess(len(samples) / messages.RINGTONE_RATE, 1.0)
        self.assertAlmostEqual(max(abs(v) for v in samples), messages.RINGTONE_PEAK)
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "ring.wav")
            messages.write_ringtone(path)
            with wave.open(path) as wav:
                self.assertEqual((wav.getnchannels(), wav.getsampwidth(), wav.getframerate()),
                                 (1, 2, messages.RINGTONE_RATE))
                self.assertEqual(wav.getnframes(), len(samples))

    def test_no_ring_file_falls_back_to_the_notification(self) -> None:
        module, played = self.fake_winsound()
        with mock.patch.object(messages, "ringtone_path", lambda: None),                 mock.patch.dict(sys.modules, {"winsound": module}):
            messages.ring_sound(True)
        self.assertEqual(played, [("SystemNotification", 0x10000 | 0x1 | 0x2)])

    def test_errors_are_swallowed(self) -> None:
        module, _ = self.fake_winsound(RuntimeError("no sound device"))
        with mock.patch.object(messages, "ringtone_path", lambda: "ring.wav"),                 mock.patch.dict(sys.modules, {"winsound": module}):
            messages.ring_sound(True)
            messages.ring_sound(False)


class KeptTests(unittest.TestCase):
    """A restart of Projektsøg does not lose a question a session is waiting on."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "messages.json")
        self.clock = [1000.0]
        self.rings: list[bool] = []

    def board(self) -> messages.MessageBoard:
        return messages.MessageBoard(None, FakeBus(), shown=lambda: True, open_uri=lambda uri: None,
                                     sound=lambda: None, ring=self.rings.append, clock=lambda: self.clock[0],
                                     timer=FakeTimer, path=self.path)

    def test_kept_across_a_restart(self) -> None:
        first = self.board()
        first.post(ASK)
        first.post({**ASK, "tag": "koe:hook-Mette", "knapper": [], "prioritet": "stille", "udloeber": 60})
        first.post({**ASK, "tag": "koe:info", "visning": "boble"})          # a note is not kept
        second = self.board()
        self.assertEqual([m["tag"] for m in second.list()["messages"]], ["koe:hook-Mette", "koe:venter"])
        self.assertEqual(second.list()["messages"][0]["prioritet"], "stille")
        second.click("koe:venter", 0)
        self.clock[0] += 61                                                 # the quiet one expired
        self.assertEqual(self.board().list()["messages"], [])
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"messages": [{"tag": "x", "titel": "y", "udloeber_ved": 9e9,
                                     "knapper": [{"tekst": "Go", "uri": "https://evil"}]}, "junk"]}, fh)
        self.assertEqual(self.board().list()["messages"], [])               # checked like a POST
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertEqual(self.board().list()["messages"], [])

    def test_a_call_is_kept_answered_or_missed_and_never_rings_again(self) -> None:
        first = self.board()
        first.post(ASK)
        first.post({**ASK, "tag": "koe:venter-2", "session": "Mette"})
        first.answer("koe:venter-2")
        first.post(WAIT)
        first.post(DEMO, internal=True)
        with open(self.path, encoding="utf-8") as fh:
            saved = {m["tag"]: m for m in json.load(fh)["messages"]}
        self.assertEqual({tag: (m["opkald"], m["besvaret"]) for tag, m in saved.items()},
                         {"koe:venter": (True, False), "koe:venter-2": (True, True),
                          "koe:lov-Mette": (False, True), "demo:opkald": (True, False)})
        self.assertNotIn("ringer", saved["koe:venter"])
        self.rings.clear()
        second = self.board()
        kept = {m["tag"]: (m["opkald"], m["besvaret"], m["ringer"]) for m in second.list()["messages"]}
        self.assertEqual(kept, {"koe:venter": (True, False, False), "koe:venter-2": (True, True, False),
                                "koe:lov-Mette": (False, True, False)})     # Projektsøg's own: gone
        self.assertEqual(self.rings, [])
        second.answer("koe:venter")
        self.assertEqual(self.board().list()["messages"][-1]["besvaret"], True)

    def test_a_file_from_before_calls(self) -> None:
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"messages": [{**ASK, "tid": 900, "udloeber_ved": 9e9, "lyd": True},
                                    {**WAIT, "tid": 950, "udloeber_ved": 9e9, "besvaret": False}]}, fh)
        kept = {m["tag"]: (m["opkald"], m["besvaret"], m["ringer"]) for m in self.board().list()["messages"]}
        self.assertEqual(kept, {"koe:venter": (True, True, False), "koe:lov-Mette": (False, True, False)})


if __name__ == "__main__":
    unittest.main()
