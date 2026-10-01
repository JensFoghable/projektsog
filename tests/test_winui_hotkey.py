"""Hotkey grammar, state machine, capture buffer and HotkeyManager (fake child, no hooks)."""

import ctypes
import json
import logging
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from projektsog.hotkey import (
    CaptureBuffer, HotkeyManager, HotkeyStateMachine, KeyEvent, format_hotkey, is_typing_chord,
    needs_menu_mask, parse_hotkey,
)

_tmp: tempfile.TemporaryDirectory | None = None
_logger = logging.getLogger("projektsog.hotkey")
_saved_level = _logger.level


def setUpModule() -> None:
    global _tmp
    _tmp = tempfile.TemporaryDirectory()
    os.environ["LOCALAPPDATA"] = _tmp.name
    _logger.setLevel(logging.CRITICAL)      # crashes/timeouts below are provoked on purpose


def tearDownModule() -> None:
    _logger.setLevel(_saved_level)
    if _tmp is not None:
        _tmp.cleanup()


VK_SPACE, VK_LSHIFT, VK_SHIFT, VK_LCTRL, VK_LALT, VK_LWIN = 0x20, 0xA0, 0x10, 0xA2, 0xA4, 0x5B
VK_E, VK_H, VK_J, VK_K = 0x45, 0x48, 0x4A, 0x4B
SHIFT = frozenset({"shift"})
NONE = frozenset()


class ParseFormatTests(unittest.TestCase):
    def test_parse_valid(self):
        self.assertEqual(parse_hotkey("shift+space"), (SHIFT, VK_SPACE))
        self.assertEqual(parse_hotkey(" Ctrl + ALT + k "), (frozenset({"ctrl", "alt"}), VK_K))
        self.assertEqual(parse_hotkey("win+shift+0"), (frozenset({"win", "shift"}), 0x30))
        self.assertEqual(parse_hotkey("alt+f1"), (frozenset({"alt"}), 0x70))
        self.assertEqual(parse_hotkey("ctrl+F24"), (frozenset({"ctrl"}), 0x87))

    def test_parse_invalid_raises_danish(self):
        cases = {
            "": "tom",
            "   ": "tom",
            "space": "mindst én modifikatortast",
            "shift+": "modifikator+tast",
            "+space": "modifikator+tast",
            "shift++space": "modifikator+tast",
            "hyper+space": "Ukendt modifikatortast",
            "shift+shift+space": "flere gange",
            "shift+enter": "Ukendt tast",
            "shift+f25": "Ukendt tast",
            "ctrl+shift": "almindelig tast",
        }
        for spec, fragment in cases.items():
            with self.subTest(spec=spec):
                with self.assertRaises(ValueError) as ctx:
                    parse_hotkey(spec)
                self.assertIn(fragment, str(ctx.exception))
        with self.assertRaises(ValueError):
            parse_hotkey(None)  # type: ignore[arg-type]

    def test_format(self):
        self.assertEqual(format_hotkey("shift+space"), "Shift+Mellemrum")
        self.assertEqual(format_hotkey("SHIFT+CTRL+k"), "Ctrl+Shift+K")
        self.assertEqual(format_hotkey("alt+win+f5"), "Win+Alt+F5")
        self.assertEqual(format_hotkey("ctrl+7"), "Ctrl+7")
        with self.assertRaises(ValueError):
            format_hotkey("shift+bogus")

    def test_needs_menu_mask(self):
        # HK-1: a lone Win/Alt release (Start menu, menu bar, Alt+Shift language switch).
        for spec in ("win+f", "alt+space", "win+shift+k", "alt+shift+f1", "win+alt+0"):
            with self.subTest(spec=spec):
                self.assertTrue(needs_menu_mask(parse_hotkey(spec)[0]))
        for spec in ("shift+space", "ctrl+shift+space", "ctrl+f12", "ctrl+alt+k", "ctrl+win+k"):
            with self.subTest(spec=spec):
                self.assertFalse(needs_menu_mask(parse_hotkey(spec)[0]))

    def test_is_typing_chord(self):
        # R2-HK-1: only a press that types a character in the search field is typing.
        for spec in ("shift+space", "SHIFT+k", "shift+7"):
            with self.subTest(spec=spec):
                self.assertTrue(is_typing_chord(*parse_hotkey(spec)))
        for spec in ("shift+f5", "ctrl+shift+space", "ctrl+f12", "alt+space", "win+f",
                     "win+shift+k", "ctrl+k"):
            with self.subTest(spec=spec):
                self.assertFalse(is_typing_chord(*parse_hotkey(spec)))


class Keys:
    """Feeds a state machine with timed events (t in ms)."""

    def __init__(self, machine: HotkeyStateMachine, start: float = 1000.0) -> None:
        self.m = machine
        self.t = start

    def wait(self, ms: float) -> "Keys":
        self.t += ms
        return self

    def down(self, vk: int, **kw) -> tuple[bool, bool]:
        return self.m.on_event(vk, True, kw.pop("injected", False), self.t, **kw)

    def up(self, vk: int, **kw) -> tuple[bool, bool]:
        return self.m.on_event(vk, False, kw.pop("injected", False), self.t, **kw)

    def space(self, held=SHIFT, was_down=False, **kw) -> tuple[bool, bool]:
        return self.down(VK_SPACE, held_mods=held, key_was_down=was_down, **kw)


PASS = (False, False)
FIRE = (True, True)
SWALLOW = (True, False)


class StateMachineTests(unittest.TestCase):
    def machine(self, spec: str = "shift+space", **kw) -> Keys:
        mods, vk = parse_hotkey(spec)
        return Keys(HotkeyStateMachine(mods, vk, **kw))

    # -- required cases (SPEC §10.3) -------------------------------------------------------
    def test_idle_shift_space_fires_and_swallows_down_repeat_up(self):
        k = self.machine()
        k.wait(1000)
        self.assertEqual(k.down(VK_LSHIFT, key_was_down=False), PASS)
        self.assertEqual(k.wait(80).space(), FIRE)
        # Auto-repeats: the OS may or may not have registered the swallowed press.
        self.assertEqual(k.wait(500).space(was_down=True), SWALLOW)
        self.assertEqual(k.wait(33).space(was_down=False), SWALLOW)
        self.assertEqual(k.wait(33).space(was_down=False), SWALLOW)
        self.assertEqual(k.wait(40).up(VK_SPACE), SWALLOW)
        self.assertEqual(k.up(VK_LSHIFT), PASS)
        self.assertFalse(k.m.main_key_swallowed)

    def test_same_without_os_cross_checks(self):
        k = self.machine()
        k.wait(1000)
        self.assertEqual(k.down(VK_LSHIFT), PASS)
        self.assertEqual(k.wait(80).down(VK_SPACE), FIRE)
        self.assertEqual(k.wait(500).down(VK_SPACE), SWALLOW)
        self.assertEqual(k.wait(50).up(VK_SPACE), SWALLOW)

    def test_typing_just_before_shift_space_blocks(self):
        k = self.machine()
        self.assertEqual(k.down(VK_E), PASS)
        self.assertEqual(k.wait(40).up(VK_E), PASS)
        self.assertEqual(k.wait(90).down(VK_LSHIFT, key_was_down=False), PASS)
        self.assertEqual(k.wait(60).space(), PASS)
        self.assertEqual(k.wait(80).up(VK_SPACE), PASS)

    def test_shift_then_letters_then_space_blocks(self):
        for gap in (60, 400):          # fast typing and slow typing
            with self.subTest(gap=gap):
                k = self.machine()
                k.wait(1000).down(VK_LSHIFT, key_was_down=False)
                for vk in (VK_H, VK_E, VK_J):
                    self.assertEqual(k.wait(gap).down(vk), PASS)
                    k.wait(20).up(vk)
                self.assertEqual(k.wait(gap).space(), PASS)

    def test_ctrl_shift_space_does_not_fire(self):
        k = self.machine()
        k.wait(1000).down(VK_LCTRL, key_was_down=False)
        k.wait(30).down(VK_LSHIFT, key_was_down=False)
        self.assertEqual(k.wait(80).space(held=frozenset({"ctrl", "shift"})), PASS)
        k2 = self.machine()                         # internal tracking only
        k2.wait(1000).down(VK_LCTRL)
        k2.wait(30).down(VK_LSHIFT)
        self.assertEqual(k2.wait(80).down(VK_SPACE), PASS)

    def test_lost_shift_up_then_space_neither_fires_nor_swallows(self):
        k = self.machine()
        k.wait(1000).down(VK_LSHIFT, key_was_down=False)
        # (Shift-up lost, e.g. while a UAC prompt had the secure desktop)
        self.assertEqual(k.wait(2000).space(held=NONE), PASS)
        self.assertEqual(k.wait(50).up(VK_SPACE), PASS)
        # The next real Shift+Space works again.
        k.wait(1000).down(VK_LSHIFT, key_was_down=False)
        self.assertEqual(k.wait(80).space(), FIRE)

    def test_lost_space_up_then_space_fires(self):
        k = self.machine()
        k.wait(1000).down(VK_LSHIFT, key_was_down=False)
        self.assertEqual(k.wait(80).space(), FIRE)
        # (Space-up lost)
        k.wait(100).up(VK_LSHIFT)
        k.wait(3000).down(VK_LSHIFT, key_was_down=False)
        self.assertEqual(k.wait(80).space(was_down=False), FIRE)

    def test_passthrough_single_passes_double_fires(self):
        k = self.machine()
        k.wait(1000).down(VK_LSHIFT, key_was_down=False)
        self.assertEqual(k.wait(80).space(passthrough=True), PASS)
        self.assertEqual(k.wait(60).up(VK_SPACE), PASS)
        self.assertEqual(k.wait(200).space(passthrough=True), FIRE)
        self.assertEqual(k.wait(60).up(VK_SPACE), SWALLOW)

    # -- more cases ------------------------------------------------------------------------
    def test_passthrough_double_tap_window(self):
        k = self.machine()
        k.wait(1000).down(VK_LSHIFT, key_was_down=False)
        self.assertEqual(k.wait(80).space(passthrough=True), PASS)
        k.wait(50).up(VK_SPACE)
        self.assertEqual(k.wait(450).space(passthrough=True), PASS)   # too late: new single
        k.wait(50).up(VK_SPACE)
        self.assertEqual(k.wait(350).space(passthrough=True), FIRE)    # 400 ms after the last one

    def test_passthrough_double_tap_broken_by_other_key(self):
        k = self.machine()
        k.wait(1000).down(VK_LSHIFT, key_was_down=False)
        self.assertEqual(k.wait(80).space(passthrough=True), PASS)
        k.wait(30).up(VK_SPACE)
        k.wait(40).down(VK_E)
        k.wait(20).up(VK_E)
        self.assertEqual(k.wait(60).space(passthrough=True), PASS)

    def test_passthrough_double_tap_with_shift_released_between(self):
        k = self.machine()
        k.wait(1000).down(VK_LSHIFT, key_was_down=False)
        self.assertEqual(k.wait(80).space(passthrough=True), PASS)
        k.wait(40).up(VK_SPACE)
        k.wait(20).up(VK_LSHIFT)
        k.wait(40).down(VK_LSHIFT, key_was_down=False)
        self.assertEqual(k.wait(100).space(passthrough=True), FIRE)

    def test_alt_and_win_combinations_do_not_fire(self):
        for vk, mod in ((VK_LALT, "alt"), (VK_LWIN, "win")):
            with self.subTest(mod=mod):
                k = self.machine()
                k.wait(1000).down(vk, key_was_down=False)
                k.wait(30).down(VK_LSHIFT, key_was_down=False)
                self.assertEqual(k.wait(80).space(held=frozenset({mod, "shift"})), PASS)

    def test_other_modifier_while_shift_held_blocks(self):
        k = self.machine()
        k.wait(1000).down(VK_LSHIFT, key_was_down=False)
        k.wait(50).down(VK_LALT, key_was_down=False)
        k.wait(50).up(VK_LALT)
        self.assertEqual(k.wait(500).space(), PASS)

    def test_ctrl_combo_hotkey_fires_and_has_no_other_key_rule(self):
        k = self.machine("ctrl+alt+k")
        mods = frozenset({"ctrl", "alt"})
        k.wait(1000).down(VK_LCTRL, key_was_down=False)
        k.wait(20).down(VK_LALT, key_was_down=False)
        k.wait(20).down(VK_H)                          # typing guard only (not Shift-only)
        self.assertEqual(k.wait(100).down(VK_K, held_mods=mods, key_was_down=False), PASS)
        k.wait(20).up(VK_K)
        self.assertEqual(k.wait(400).down(VK_K, held_mods=mods, key_was_down=False), FIRE)
        self.assertEqual(k.wait(30).up(VK_K), SWALLOW)
        self.assertEqual(k.wait(400).down(VK_K, held_mods=SHIFT | mods, key_was_down=False), PASS)

    def test_auto_repeat_of_passed_press_never_fires(self):
        k = self.machine()
        self.assertEqual(k.wait(1000).space(held=NONE), PASS)          # plain space
        k.wait(200).down(VK_LSHIFT, key_was_down=False)
        for _ in range(3):
            self.assertEqual(k.wait(33).space(was_down=True), PASS)     # repeats, now with Shift
        self.assertEqual(k.wait(30).up(VK_SPACE), PASS)

    def test_repeat_of_press_seen_before_install_passes(self):
        k = self.machine()
        k.wait(1000).down(VK_LSHIFT, key_was_down=True)
        self.assertEqual(k.wait(33).space(was_down=True), PASS)

    def test_injected_events_are_ignored(self):
        k = self.machine()
        k.wait(1000).down(VK_LSHIFT, key_was_down=False)
        self.assertEqual(k.wait(80).space(injected=True), PASS)
        self.assertEqual(k.wait(10).up(VK_SPACE, injected=True), PASS)
        k.wait(500).down(VK_E, injected=True)                           # no typing-guard effect
        self.assertEqual(k.wait(50).space(), FIRE)

    def test_guard_reset_after_lost_shift_up(self):
        k = self.machine()
        k.wait(1000).down(VK_LSHIFT, key_was_down=False)
        k.wait(50).down(VK_H)                     # typing a capital H …
        k.wait(40).up(VK_H)
        # … Shift-up lost; later a fresh Shift press (the OS says it was up)
        k.wait(2000).down(VK_LSHIFT, key_was_down=False)
        self.assertEqual(k.wait(80).space(), FIRE)

    def test_guard_reset_after_lost_shift_up_seen_at_space(self):
        k = self.machine()
        k.wait(1000).down(VK_LSHIFT, key_was_down=False)
        k.wait(50).down(VK_H)
        self.assertEqual(k.wait(2000).space(held=NONE), PASS)   # Shift shown up → reset
        k.wait(40).up(VK_SPACE)
        k.wait(500).down(VK_LSHIFT)                              # internal tracking only
        self.assertEqual(k.wait(80).space(), FIRE)

    def test_lost_ctrl_up_is_overridden_by_held_mods(self):
        k = self.machine()
        k.wait(1000).down(VK_LCTRL, key_was_down=False)          # Ctrl-up lost
        k.wait(2000).down(VK_LSHIFT, key_was_down=False)
        self.assertEqual(k.wait(80).space(held=SHIFT), FIRE)

    def test_typing_guard_boundary(self):
        for gap, expected in ((299, PASS), (300, FIRE)):
            with self.subTest(gap=gap):
                k = self.machine()
                k.down(VK_E)
                k.wait(1).down(VK_LSHIFT, key_was_down=False)    # after 'e': Shift rule not hit
                self.assertEqual(k.wait(gap - 1).space(), expected)

    def test_plain_space_counts_as_typing(self):
        k = self.machine()
        k.wait(1000).space(held=NONE)
        k.wait(40).up(VK_SPACE)
        k.wait(40).down(VK_LSHIFT, key_was_down=False)
        self.assertEqual(k.wait(60).space(), PASS)

    def test_toggle_with_shift_held(self):
        k = self.machine()
        k.wait(1000).down(VK_LSHIFT, key_was_down=False)
        self.assertEqual(k.wait(80).space(), FIRE)
        self.assertEqual(k.wait(90).up(VK_SPACE), SWALLOW)
        self.assertEqual(k.wait(150).space(), FIRE)               # show … and hide again

    def test_generic_and_side_specific_modifier_codes(self):
        k = self.machine()
        k.wait(1000).down(VK_SHIFT)                              # generic VK_SHIFT
        self.assertEqual(k.wait(80).down(VK_SPACE), FIRE)
        k.wait(40).up(VK_SPACE)
        k.wait(40).up(VK_LSHIFT)                                 # released as side-specific
        self.assertEqual(k.wait(500).down(VK_SPACE), PASS)       # Shift no longer held

    def test_stale_swallowed_state(self):
        # HK-2: a held key auto-repeats; no event for longer than the repeat window means the
        # key-up was lost (dropped hook), so the swallowed state may be forgotten.
        k = self.machine(repeat_window_ms=700)
        k.wait(1000).down(VK_LSHIFT, key_was_down=False)
        self.assertEqual(k.wait(80).space(), FIRE)
        self.assertTrue(k.m.main_key_swallowed)
        self.assertFalse(k.m.main_key_stale(k.t + 700))
        self.assertTrue(k.m.main_key_stale(k.t + 701))
        self.assertEqual(k.wait(600).space(was_down=False), SWALLOW)   # a repeat: fresh again
        self.assertFalse(k.m.main_key_stale(k.t + 650))
        k.m.forget_main_key()
        self.assertFalse(k.m.main_key_swallowed)
        self.assertFalse(k.m.main_key_stale(k.t + 5000))               # nothing left to forget
        self.assertEqual(k.wait(3000).up(VK_SPACE), PASS)              # a late up is not ours
        k.wait(10).up(VK_LSHIFT)
        k.wait(1000).down(VK_LSHIFT, key_was_down=False)
        self.assertEqual(k.wait(80).space(), FIRE)                     # and it fires again

    # -- R2-APP-1: the hotkey while the keys typed after a fire are captured -------------------
    def test_typing_chord_during_the_capture_is_typing(self):
        k = self.machine()
        k.wait(1000).down(VK_LSHIFT, key_was_down=False)
        self.assertEqual(k.wait(80).space(), FIRE)
        self.assertEqual(k.wait(60).up(VK_SPACE), SWALLOW)
        self.assertEqual(k.wait(400).space(capturing=True), PASS)      # a space, never a fire
        self.assertEqual(k.wait(60).up(VK_SPACE), PASS)
        # Nor a double tap (in our window): 300 ms apart, clear of the typing guard.
        self.assertEqual(k.wait(400).space(capturing=True, passthrough=True), PASS)
        self.assertEqual(k.wait(60).up(VK_SPACE), PASS)
        self.assertEqual(k.wait(240).space(capturing=True, passthrough=True), PASS)
        self.assertEqual(k.wait(60).up(VK_SPACE), PASS)
        self.assertEqual(k.wait(400).space(), FIRE)                    # capture over: fires again

    def test_other_chord_during_the_capture_is_absorbed(self):
        k = self.machine("ctrl+f12")
        ctrl, f12 = frozenset({"ctrl"}), 0x7B
        k.wait(1000).down(VK_LCTRL, key_was_down=False)
        self.assertEqual(k.wait(80).down(f12, held_mods=ctrl, key_was_down=False), FIRE)
        self.assertEqual(k.wait(60).up(f12), SWALLOW)
        again = dict(held_mods=ctrl, key_was_down=False, capturing=True)
        self.assertEqual(k.wait(400).down(f12, **again), SWALLOW)     # no fire, not typed
        self.assertEqual(k.wait(500).down(f12, **again), SWALLOW)     # its auto-repeat
        self.assertEqual(k.wait(60).up(f12), SWALLOW)
        self.assertEqual(k.wait(400).down(f12, held_mods=NONE, key_was_down=False,
                                          capturing=True), PASS)      # a plain F12 is any key
        k.wait(60).up(f12)
        self.assertEqual(k.wait(400).down(f12, held_mods=ctrl, key_was_down=False), FIRE)


class CaptureBufferTests(unittest.TestCase):
    def ev(self, vk: int, down: bool) -> KeyEvent:
        return KeyEvent(vk, vk & 0x7F, False, down)

    def test_inactive_captures_nothing(self):
        buf = CaptureBuffer()
        self.assertFalse(buf.active)
        self.assertFalse(buf.feed(self.ev(VK_E, True), 0))
        self.assertEqual(buf.finish(True, 0), [])

    def test_order_preserved_and_replayed_on_ok(self):
        buf = CaptureBuffer()
        buf.start(1000)
        events = [self.ev(0x4C, True), self.ev(0x45, True), self.ev(0x4C, False),
                  self.ev(0x45, False), self.ev(VK_SPACE, True), self.ev(VK_SPACE, False)]
        for i, event in enumerate(events):
            self.assertTrue(buf.feed(event, 1000 + 10 * i))
        self.assertEqual(buf.finish(True, 1400), events)
        self.assertFalse(buf.active)

    def test_dropped_when_not_ok(self):
        buf = CaptureBuffer()
        buf.start(0)
        self.assertTrue(buf.feed(self.ev(VK_E, True), 10))
        self.assertEqual(buf.finish(False, 100), [])
        self.assertFalse(buf.active)

    def test_timeout(self):
        buf = CaptureBuffer(timeout_ms=1500)
        buf.start(0)
        self.assertTrue(buf.feed(self.ev(VK_E, True), 1500))
        self.assertFalse(buf.feed(self.ev(VK_H, True), 1501))     # expired: passes through
        self.assertFalse(buf.active)
        buf.start(0)
        buf.feed(self.ev(VK_E, True), 100)
        self.assertEqual(buf.finish(True, 1600), [])              # end_capture came too late

    def test_expire_without_further_keys(self):
        buf = CaptureBuffer(timeout_ms=1500)
        buf.start(0)
        buf.feed(self.ev(VK_E, True), 10)
        buf.expire(1400)
        self.assertTrue(buf.active)
        buf.expire(1501)
        self.assertFalse(buf.active)
        self.assertEqual(buf.finish(True, 1502), [])

    def test_up_without_captured_down_passes_through(self):
        buf = CaptureBuffer()
        buf.start(0)
        self.assertFalse(buf.feed(self.ev(VK_E, False), 10))      # 'e' went down before the fire
        self.assertTrue(buf.feed(self.ev(VK_H, True), 20))
        self.assertTrue(buf.feed(self.ev(VK_H, True), 50))        # auto-repeat
        self.assertTrue(buf.feed(self.ev(VK_H, False), 60))
        self.assertFalse(buf.feed(self.ev(VK_H, False), 70))      # stray second up
        self.assertEqual(len(buf.finish(True, 100)), 3)

    def test_overflow_is_swallowed_but_not_stored(self):
        buf = CaptureBuffer(max_events=4)
        buf.start(0)
        for i in range(6):
            self.assertTrue(buf.feed(self.ev(0x41 + i, True), i))
        self.assertEqual([e.vk for e in buf.finish(True, 10)], [0x41, 0x42, 0x43, 0x44])

    def test_restart_clears(self):
        buf = CaptureBuffer()
        buf.start(0)
        buf.feed(self.ev(VK_E, True), 1)
        buf.start(100)
        self.assertEqual(buf.finish(True, 150), [])

    # -- WIN-1: Edge is cold-launched, the main process asks for more time -------------------
    def test_extend_keeps_keys_typed_during_a_cold_launch(self):
        buf = CaptureBuffer(timeout_ms=1500)
        buf.start(0)
        self.assertTrue(buf.feed(self.ev(0x4C, True), 100))
        self.assertTrue(buf.extend(4000, 150))                    # deadline 4150
        typed = [self.ev(0x4C, False), self.ev(0x45, True), self.ev(0x45, False)]
        for t, event in zip((1600, 2500, 4100), typed):
            self.assertTrue(buf.feed(event, t))                   # after 1.5 s: still captured
        self.assertEqual(len(buf.finish(True, 4150)), 4)          # all replayed in the field
        buf.start(0)
        buf.extend(4000, 150)
        self.assertFalse(buf.feed(self.ev(0x4E, True), 4151))     # the new deadline holds

    def test_extend_is_capped(self):
        buf = CaptureBuffer(timeout_ms=1500, max_extend_ms=5000, max_total_ms=12000)
        buf.start(0)
        buf.extend(60_000, 100)                                   # at most 5 s from the ask
        self.assertTrue(buf.feed(self.ev(VK_E, True), 5100))
        self.assertFalse(buf.feed(self.ev(VK_H, True), 5101))
        buf.start(0)
        for t in range(1000, 12_000, 1000):                       # repeated asks: 12 s in total
            self.assertTrue(buf.extend(5000, t))
        self.assertTrue(buf.feed(self.ev(VK_E, True), 12_000))
        self.assertFalse(buf.feed(self.ev(VK_H, True), 12_001))
        self.assertFalse(buf.extend(5000, 12_002))                # over: cannot be revived
        buf.start(0)
        buf.extend(4000, 100)
        buf.extend(500, 200)                                      # never shortens
        self.assertTrue(buf.feed(self.ev(VK_E, True), 4000))

    def test_extend_never_revives_an_expired_or_missing_capture(self):
        buf = CaptureBuffer(timeout_ms=1500)
        self.assertFalse(buf.extend(4000, 10))                    # no capture running
        self.assertFalse(buf.feed(self.ev(VK_E, True), 20))
        buf.start(0)
        buf.feed(self.ev(VK_E, True), 10)
        self.assertFalse(buf.extend(4000, 1600))                  # too late: already expired
        self.assertFalse(buf.active)
        self.assertFalse(buf.feed(self.ev(VK_H, True), 1700))
        self.assertEqual(buf.finish(True, 1800), [])


class HookGlueTests(unittest.TestCase):
    """The child's hook handler with in-memory KBDLLHOOKSTRUCTs – no hook is installed and
    OS queries/SendInput are replaced by fakes."""

    def setUp(self) -> None:
        from projektsog import hotkey
        self.hotkey = hotkey
        self.emitted: list[dict] = []
        self.replayed: list[list[KeyEvent]] = []
        self.masks: list[int] = []                 # tick of every mask key sent
        self.os_down: set[int] = set()             # what GetAsyncKeyState would report
        self.foreground = "chrome.exe"
        self.own_window = False                    # our Projektsøg window in the foreground
        patches = [
            mock.patch.object(hotkey, "_key_down_now", lambda vk: vk in self.os_down),
            mock.patch.object(hotkey, "_foreground_exe", lambda: self.foreground),
            mock.patch.object(hotkey, "_own_window_in_foreground", lambda: self.own_window),
            mock.patch.object(hotkey, "_send_keys", self.replayed.append),
            mock.patch.object(hotkey, "_send_mask_key",
                              lambda: self.masks.append(self.tick) or True),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.child = hotkey._HookChild(self.emitted.append, dry_run=False)
        self.use("shift+space")
        self.child._passthrough = frozenset({"resolve.exe"})
        self.tick = int(hotkey._GetTickCount64())

    def use(self, spec: str) -> None:
        """What _apply_config sets up for ``spec`` (without touching a real hook)."""
        mods, vk = parse_hotkey(spec)
        self.child._mods, self.child._vk = mods, vk
        self.child._machine = HotkeyStateMachine(mods, vk)
        self.child._typing_chord = is_typing_chord(mods, vk)
        self.child._mask_on_fire = needs_menu_mask(mods)
        self.child._capture.cancel()

    def press_chord(self, spec: str, *, after_ms: int = 1000) -> bool:
        """Hold the modifiers of ``spec``, press its main key; returns whether the main-key
        down was swallowed. The key-ups follow (main key first)."""
        mods, vk = parse_hotkey(spec)
        mod_vks = [{"win": VK_LWIN, "ctrl": VK_LCTRL, "alt": VK_LALT, "shift": VK_LSHIFT}[m]
                   for m in ("win", "ctrl", "alt", "shift") if m in mods]
        for i, mod_vk in enumerate(mod_vks):
            self.key(mod_vk, True, after_ms=after_ms if i == 0 else 20)
        with mock.patch.object(self.hotkey, "_async_held_mods", lambda: mods):
            swallowed = self.key(vk, True, after_ms=80)
        self.key(vk, False, after_ms=60)
        for mod_vk in reversed(mod_vks):
            self.key(mod_vk, False, after_ms=20)
        return swallowed

    def replayed_keys(self) -> list[tuple[int, bool]]:
        return [(e.vk, e.down) for batch in self.replayed for e in batch]

    def type_text(self, text: str, *, gap_ms: int = 150) -> list[tuple[int, bool]]:
        """Type ``text`` (letters and spaces) key by key; asserts every event is captured."""
        typed = []
        for ch in text:
            vk = VK_SPACE if ch == " " else ord(ch.upper())
            for down, after in ((True, gap_ms), (False, 60)):
                self.assertTrue(self.key(vk, down, after_ms=after), f"{ch!r} was not captured")
                typed.append((vk, down))
        return typed

    def command(self, message: dict, *, after_ms: int = 0) -> None:
        """Deliver a parent command the way the reader thread does."""
        self.tick += after_ms
        self.child.post(message)
        with mock.patch.object(self.hotkey, "_GetTickCount64", lambda: self.tick):
            self.child._drain_commands()

    def key(self, vk: int, down: bool, *, after_ms: int = 50, injected: bool = False) -> bool:
        self.tick += after_ms
        kb = self.hotkey._KBDLLHOOKSTRUCT(vkCode=vk, scanCode=vk & 0x7F,
                                          flags=(0x10 if injected else 0) | (0 if down else 0x80),
                                          time=self.tick & 0xFFFFFFFF)
        message = self.hotkey.WM_KEYDOWN if down else 0x0101
        with mock.patch.object(self.hotkey, "_GetTickCount64", lambda: self.tick):
            swallow = self.child._handle_key(message, ctypes.addressof(kb))
        if not swallow and not injected:            # the OS state follows passed events only
            (self.os_down.add if down else self.os_down.discard)(vk)
            generic = {0xA0: 0x10, 0xA1: 0x10, 0xA2: 0x11, 0xA3: 0x11, 0xA4: 0x12,
                       0xA5: 0x12}.get(vk)
            if generic is not None:
                (self.os_down.add if down else self.os_down.discard)(generic)
        return swallow

    def test_fire_capture_and_replay(self):
        self.assertFalse(self.key(VK_LSHIFT, True, after_ms=1000))
        self.assertTrue(self.key(VK_SPACE, True, after_ms=80))
        self.assertEqual(self.emitted, [{"ev": "fire", "from_app": "chrome.exe"}])
        self.assertTrue(self.key(VK_SPACE, False))                 # swallowed key-up
        self.assertFalse(self.key(VK_LSHIFT, False))                # modifiers always pass
        typed = [(0x4C, True), (0x4C, False), (0x45, True), (0x45, False)]
        for vk, down in typed:
            self.assertTrue(self.key(vk, down, after_ms=30))        # captured
        self.assertFalse(self.key(0x4E, True, injected=True))       # injected: never captured
        with mock.patch.object(self.hotkey, "_GetTickCount64", lambda: self.tick + 100):
            self.child._end_capture(True)
        self.assertEqual([[(e.vk, e.down) for e in batch] for batch in self.replayed], [typed])
        self.assertFalse(self.key(0x4E, True))                      # capture is over

    def test_capture_dropped_when_not_ok_or_late(self):
        self.key(VK_LSHIFT, True, after_ms=1000)
        self.key(VK_SPACE, True, after_ms=80)
        self.assertTrue(self.key(0x4C, True))
        self.child._end_capture(False)
        self.assertEqual(self.replayed, [])
        self.key(VK_SPACE, False)
        self.assertFalse(self.key(VK_SPACE, True, after_ms=400))    # 'L' was typed with Shift held
        self.key(VK_SPACE, False)
        self.key(VK_LSHIFT, False)
        self.key(VK_LSHIFT, True, after_ms=400)
        self.assertTrue(self.key(VK_SPACE, True, after_ms=80))      # a fresh Shift+Space fires
        self.assertEqual(len(self.emitted), 2)
        self.assertTrue(self.key(0x4C, True))
        with mock.patch.object(self.hotkey, "_GetTickCount64", lambda: self.tick + 2000):
            self.child._end_capture(True)                           # too late: dropped
        self.assertEqual(self.replayed, [])

    def test_passthrough_app_reported_and_double_tap(self):
        self.foreground = "Resolve.exe"
        self.key(VK_LSHIFT, True, after_ms=1000)
        self.assertFalse(self.key(VK_SPACE, True, after_ms=80))     # single press → Resolve
        self.assertFalse(self.key(VK_SPACE, False, after_ms=60))
        self.assertTrue(self.key(VK_SPACE, True, after_ms=150))     # double tap → ours
        self.assertEqual(self.emitted, [{"ev": "fire", "from_app": "Resolve.exe"}])

    def test_typing_is_not_disturbed(self):
        self.assertFalse(self.key(VK_LSHIFT, True, after_ms=1000))
        for vk in (0x48, 0x45, 0x4A):                               # "HEJ" with Shift held
            self.assertFalse(self.key(vk, True, after_ms=90))
            self.assertFalse(self.key(vk, False, after_ms=30))
        self.assertFalse(self.key(VK_SPACE, True, after_ms=90))
        self.assertEqual(self.emitted, [])

    def test_event_time_unwraps_32_bit_timestamps(self):
        wrap = 1 << 32
        with mock.patch.object(self.hotkey, "_GetTickCount64", lambda: wrap + 5):
            self.assertEqual(self.hotkey._event_time_ms(wrap - 3), float(wrap - 3))
            self.assertEqual(self.hotkey._event_time_ms(2), float(wrap + 2))
            self.assertEqual(self.hotkey._event_time_ms(9), float(wrap + 5))   # "future": now

    # -- WIN-1: extend_capture keeps the keys while Edge is cold-launched ---------------------
    def test_extend_capture_command_keeps_keys_past_1_5_s(self):
        self.key(VK_LSHIFT, True, after_ms=1000)
        self.assertTrue(self.key(VK_SPACE, True, after_ms=80))      # fire at t0
        self.key(VK_SPACE, False, after_ms=60)
        self.key(VK_LSHIFT, False, after_ms=20)
        self.command({"cmd": "extend_capture", "ms": 4000}, after_ms=20)
        typed = [(0x4C, True), (0x4C, False), (0x45, True), (0x45, False)]
        self.tick += 1500                                           # Edge still starting …
        for vk, down in typed:
            self.assertTrue(self.key(vk, down, after_ms=100))       # … still captured at 1.6 s+
        self.command({"cmd": "end_capture", "ok": True}, after_ms=500)
        self.assertEqual([[(e.vk, e.down) for e in batch] for batch in self.replayed], [typed])

    def test_without_extension_late_keys_pass_to_the_previous_app(self):
        self.key(VK_LSHIFT, True, after_ms=1000)
        self.key(VK_SPACE, True, after_ms=80)
        self.key(VK_SPACE, False, after_ms=60)
        self.key(VK_LSHIFT, False, after_ms=20)
        self.assertFalse(self.key(0x4C, True, after_ms=1500))       # 1.66 s after the fire
        for bad in ({"cmd": "extend_capture", "ms": -5}, {"cmd": "extend_capture", "ms": "4000"},
                    {"cmd": "extend_capture"}, {"cmd": "extend_capture", "ms": True}):
            self.command(bad)                                       # ignored, nothing breaks
        self.command({"cmd": "end_capture", "ok": True})
        self.assertEqual(self.replayed, [])

    # -- APP-1: our own window is a passthrough typing context --------------------------------
    def test_own_window_shift_space_types_a_space_and_double_press_toggles(self):
        self.foreground, self.own_window = "msedge.exe", True
        for vk in (0x52, 0x49, 0x4B, 0x4B, 0x45):                   # typing "Rikke" …
            self.assertFalse(self.key(vk, True, after_ms=90))
            self.assertFalse(self.key(vk, False, after_ms=30))
        self.assertFalse(self.key(VK_LSHIFT, True, after_ms=350))   # … a pause, Shift for "L"
        self.assertFalse(self.key(VK_SPACE, True, after_ms=60))     # the space is typed
        self.assertFalse(self.key(VK_SPACE, False, after_ms=50))
        self.assertFalse(self.key(0x4C, True, after_ms=60))         # "L" – no hide, no capture
        self.key(0x4C, False, after_ms=30)
        self.key(VK_LSHIFT, False, after_ms=30)
        self.assertEqual(self.emitted, [])
        self.key(VK_LSHIFT, True, after_ms=2500)                    # a deliberate double press
        self.assertFalse(self.key(VK_SPACE, True, after_ms=80))
        self.key(VK_SPACE, False, after_ms=60)
        self.assertTrue(self.key(VK_SPACE, True, after_ms=150))     # … toggles (hides)
        self.assertEqual(self.emitted, [{"ev": "fire", "from_app": "msedge.exe"}])

    def test_other_edge_windows_are_not_a_typing_context(self):
        self.foreground, self.own_window = "msedge.exe", False      # e.g. the user's browser
        self.key(VK_LSHIFT, True, after_ms=1000)
        self.assertTrue(self.key(VK_SPACE, True, after_ms=80))
        self.assertEqual(self.emitted, [{"ev": "fire", "from_app": "msedge.exe"}])

    def test_own_window_check_only_for_edge(self):
        calls: list[int] = []
        with mock.patch.object(self.hotkey, "_own_window_in_foreground",
                               lambda: calls.append(1) or True):
            self.key(VK_LSHIFT, True, after_ms=1000)
            self.assertTrue(self.key(VK_SPACE, True, after_ms=80))   # chrome.exe: not asked
        self.assertEqual(calls, [])

    # -- HK-1: Win/Alt hotkeys disguise the modifier release ----------------------------------
    def test_win_hotkey_sends_the_mask_key_when_it_fires(self):
        self.use("win+f")
        self.key(VK_LWIN, True, after_ms=1000)
        self.os_down.add(VK_LWIN)
        fire_tick = self.tick + 80
        self.assertTrue(self.key(0x46, True, after_ms=80))
        self.assertEqual(self.masks, [fire_tick])                   # while Win is still down
        self.assertTrue(self.key(0x46, True, after_ms=500))         # auto-repeat: swallowed …
        self.assertTrue(self.key(0x46, False, after_ms=40))
        self.assertFalse(self.key(VK_LWIN, False, after_ms=40))     # the release passes
        self.assertEqual(len(self.masks), 1)                        # … and no second mask
        self.assertEqual(len(self.emitted), 1)

    def test_alt_hotkey_masks_but_a_passed_press_does_not(self):
        self.use("alt+space")
        self.foreground = "Resolve.exe"                             # passthrough app
        self.key(VK_LALT, True, after_ms=1000)
        self.assertFalse(self.key(VK_SPACE, True, after_ms=80))     # single press → Resolve
        self.assertEqual(self.masks, [])                            # Alt+Space reaches Resolve
        self.key(VK_SPACE, False, after_ms=50)
        self.assertTrue(self.key(VK_SPACE, True, after_ms=100))     # double press fires
        self.assertEqual(len(self.masks), 1)

    def test_shift_and_ctrl_hotkeys_send_no_mask(self):
        for spec, mod_vk in (("shift+space", VK_LSHIFT), ("ctrl+alt+k", VK_LCTRL)):
            with self.subTest(spec=spec):
                self.use(spec)
                self.os_down.clear()
                mods, vk = parse_hotkey(spec)
                for mod in (VK_LCTRL, VK_LALT) if "ctrl" in mods else (mod_vk,):
                    self.key(mod, True, after_ms=1000)
                with mock.patch.object(self.hotkey, "_async_held_mods", lambda: mods):
                    self.assertTrue(self.key(vk, True, after_ms=80))
                self.assertEqual(self.masks, [])

    # -- R2-HK-1: our own window is a typing context for typing chords only -------------------
    def test_ctrl_alt_win_hotkeys_hide_our_window_with_one_press(self):
        # They type nothing in the search field: one press is swallowed and toggles, and the
        # combination never reaches Edge (Alt+Space: window menu) or Windows (Win+Space).
        self.foreground, self.own_window = "msedge.exe", True
        asked: list[int] = []
        with mock.patch.object(self.hotkey, "_own_window_in_foreground",
                               lambda: asked.append(1) or True):
            for spec, masks in (("alt+space", 1), ("win+f", 1), ("win+space", 1),
                                ("ctrl+shift+space", 0), ("ctrl+f12", 0), ("shift+f5", 0)):
                with self.subTest(spec=spec):
                    self.use(spec)
                    self.os_down.clear()
                    self.emitted.clear()
                    self.masks.clear()
                    self.assertTrue(self.press_chord(spec))
                    self.assertEqual(self.emitted, [{"ev": "fire", "from_app": "msedge.exe"}])
                    self.assertEqual(len(self.masks), masks)
            self.assertEqual(asked, [])                             # never even asked
            self.use("shift+k")                                     # a typing chord: 'K'
            self.os_down.clear()
            self.emitted.clear()
            self.assertFalse(self.press_chord("shift+k"))
            self.assertEqual((self.emitted, asked), ([], [1]))

    # -- R2-APP-1: the hotkey pressed again while its capture runs -----------------------------
    def fire_and_release(self) -> int:
        """Shift+Space from chrome.exe; returns the tick of the fire."""
        self.key(VK_LSHIFT, True, after_ms=1000)
        self.assertTrue(self.key(VK_SPACE, True, after_ms=80))
        fired = self.tick
        self.key(VK_SPACE, False, after_ms=60)
        self.key(VK_LSHIFT, False, after_ms=20)
        return fired

    def test_shift_space_typed_during_a_cold_start_is_a_space_not_a_second_fire(self):
        # "rikke", a pause while the window does not appear yet, Shift held early for the "L".
        self.fire_and_release()
        self.command({"cmd": "extend_capture", "ms": 4000}, after_ms=10)
        typed = self.type_text("rikke")
        self.assertFalse(self.key(VK_LSHIFT, True, after_ms=500))
        self.assertTrue(self.key(VK_SPACE, True, after_ms=60))      # captured: no fire …
        self.assertTrue(self.key(VK_SPACE, False, after_ms=50))
        typed += [(VK_SPACE, True), (VK_SPACE, False)] + self.type_text("lindholm")
        self.key(VK_LSHIFT, False, after_ms=30)                     # (≈ 3.5 s after the fire)
        self.assertEqual(self.emitted, [{"ev": "fire", "from_app": "chrome.exe"}])
        self.command({"cmd": "end_capture", "ok": True}, after_ms=100)
        self.assertEqual(self.replayed_keys(), typed)               # … "rikke lindholm" complete

    def test_whatever_is_in_front_the_capture_decides(self):
        # A passthrough app, or our own window that has just appeared: still a space.
        for foreground, own in (("Resolve.exe", False), ("msedge.exe", True)):
            with self.subTest(foreground=foreground):
                self.use("shift+space")
                self.foreground, self.own_window = "chrome.exe", False
                self.emitted.clear()
                self.replayed.clear()
                self.key(VK_LSHIFT, True, after_ms=1000)
                self.assertTrue(self.key(VK_SPACE, True, after_ms=80))
                self.key(VK_SPACE, False, after_ms=60)
                self.foreground, self.own_window = foreground, own
                self.assertTrue(self.key(VK_SPACE, True, after_ms=400))     # Shift still held
                self.assertTrue(self.key(VK_SPACE, False, after_ms=60))
                self.assertTrue(self.key(VK_SPACE, True, after_ms=150))     # a quick second one
                self.assertTrue(self.key(VK_SPACE, False, after_ms=60))
                self.key(VK_LSHIFT, False, after_ms=20)
                self.assertEqual(len(self.emitted), 1)
                self.command({"cmd": "end_capture", "ok": True}, after_ms=50)
                self.assertEqual(self.replayed_keys(), [(VK_SPACE, True), (VK_SPACE, False)] * 2)

    def test_other_hotkeys_pressed_again_during_the_capture_are_absorbed(self):
        # Replayed without Ctrl, F12 would open Edge's developer tools; Win+F would type "f".
        for spec, masks in (("ctrl+f12", 0), ("win+f", 2), ("alt+space", 2)):
            with self.subTest(spec=spec):
                self.use(spec)
                self.os_down.clear()
                self.emitted.clear()
                self.masks.clear()
                self.replayed.clear()
                self.assertTrue(self.press_chord(spec))                  # fires
                self.command({"cmd": "extend_capture", "ms": 4000}, after_ms=10)
                typed = self.type_text("le")
                self.assertTrue(self.press_chord(spec, after_ms=500))    # again: swallowed …
                self.assertEqual(len(self.emitted), 1)                   # … no second fire
                self.assertEqual(len(self.masks), masks)                 # no lone Win/Alt
                typed += self.type_text("ne")                            # still captured
                self.command({"cmd": "end_capture", "ok": True}, after_ms=100)
                self.assertEqual(self.replayed_keys(), typed)            # … and not replayed

    def test_an_absorbed_press_is_masked_once(self):
        self.use("win+f")
        self.key(VK_LWIN, True, after_ms=1000)
        self.assertTrue(self.key(0x46, True, after_ms=80))          # fire: mask 1
        self.key(0x46, False, after_ms=60)
        self.assertTrue(self.key(0x46, True, after_ms=400))         # Win still held: absorbed
        for _ in range(3):
            self.assertTrue(self.key(0x46, True, after_ms=33))      # its auto-repeats
        self.assertTrue(self.key(0x46, False, after_ms=30))
        self.assertFalse(self.key(VK_LWIN, False, after_ms=20))
        self.assertEqual((len(self.emitted), len(self.masks)), (1, 2))

    # -- R2-WIN-1: the Controller's heartbeat keeps the capture for a slow cold show -----------
    def test_a_heartbeat_keeps_the_capture_while_the_show_takes_long(self):
        fired = self.fire_and_release()
        typed: list[tuple[int, bool]] = []
        for offset, what in ((30, "beat"), (1600, "l"), (2000, "beat"), (2600, "e"),
                             (4000, "beat"), (4600, "n"), (6000, "beat"), (7400, "e")):
            if what == "beat":                      # extend_capture(5) every 2 s
                self.command({"cmd": "extend_capture", "ms": 5000},
                             after_ms=fired + offset - self.tick)
            else:
                vk = ord(what.upper())
                self.assertTrue(self.key(vk, True, after_ms=fired + offset - self.tick))
                self.assertTrue(self.key(vk, False, after_ms=50))
                typed += [(vk, True), (vk, False)]
        self.command({"cmd": "end_capture", "ok": True}, after_ms=fired + 8000 - self.tick)
        self.assertEqual(self.replayed_keys(), typed)

    def test_without_heartbeat_the_capture_lapses_5_s_after_the_extension(self):
        # What a hung main process gets: the keys after that pass on, nothing replays late.
        fired = self.fire_and_release()
        self.command({"cmd": "extend_capture", "ms": 5000}, after_ms=fired + 30 - self.tick)
        self.assertTrue(self.key(0x4C, True, after_ms=fired + 5000 - self.tick))
        self.assertTrue(self.key(0x4C, False, after_ms=20))
        self.assertFalse(self.key(0x45, True, after_ms=fired + 5100 - self.tick))
        self.command({"cmd": "end_capture", "ok": True}, after_ms=fired + 6000 - self.tick)
        self.assertEqual(self.replayed, [])

    def test_no_heartbeat_keeps_a_capture_past_12_s(self):
        fired = self.fire_and_release()
        for offset in range(30, 11_000, 2000):
            self.command({"cmd": "extend_capture", "ms": 5000},
                         after_ms=fired + offset - self.tick)
        self.assertTrue(self.key(0x4C, True, after_ms=fired + 11_900 - self.tick))
        self.command({"cmd": "extend_capture", "ms": 5000}, after_ms=fired + 12_030 - self.tick)
        self.assertFalse(self.key(0x45, True, after_ms=fired + 12_100 - self.tick))
        self.command({"cmd": "end_capture", "ok": True}, after_ms=100)
        self.assertEqual(self.replayed, [])


class ChildMaintenanceTests(unittest.TestCase):
    """The child's hook upkeep with the hook install, key state, clock and timers faked."""

    def setUp(self) -> None:
        from projektsog import hotkey
        self.hotkey = hotkey
        self.now = 50_000_000
        self.installed: list[int] = []
        self.unhooked: list[int] = []
        self.timers: list[int] = []
        self.emitted: list[dict] = []
        patches = [
            mock.patch.object(hotkey, "_GetTickCount64", lambda: self.now),
            mock.patch.object(hotkey, "_key_down_now", lambda vk: False),
            mock.patch.object(hotkey, "_UnhookWindowsHookEx",
                              lambda h: self.unhooked.append(h) or True),
            mock.patch.object(hotkey, "_SetTimer",
                              lambda hwnd, tid, ms, cb: self.timers.append(tid) or 1),
            mock.patch.object(hotkey, "_KillTimer", lambda hwnd, tid: True),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.child = hotkey._HookChild(self.emitted.append, dry_run=True)
        self.child._install_hook = self.fake_install         # never a real hook
        self.child._hwnd = 0x1234                             # timers go to the fakes above
        self.child._repeat_window_ms = 700

    def fake_install(self) -> int:
        self.installed.append(len(self.installed) + 1)
        return self.installed[-1]

    def configure(self, spec: str, seq: int = 1) -> None:
        self.child._apply_config({"cmd": "config", "spec": spec, "passthrough": [],
                                  "typing_guard_ms": 300, "double_tap_ms": 400,
                                  "enabled": True, "seq": seq})

    def fire(self) -> float:
        """Shift+Space as the hook sees it; returns the fire time. The key-up never comes."""
        machine, t = self.child._machine, float(self.now)
        machine.on_event(VK_LSHIFT, True, False, t, key_was_down=False)
        self.assertEqual(machine.on_event(VK_SPACE, True, False, t + 80, held_mods=SHIFT,
                                          key_was_down=False), (True, True))
        self.child._capture.start(t + 80)
        machine.on_event(VK_LSHIFT, False, False, t + 150)
        return t + 80

    def test_lost_key_up_no_longer_blocks_the_reinstall(self):
        self.configure("shift+space")
        self.assertEqual((self.child._mode, self.installed), ("ll", [1]))
        fired = self.fire()                    # Windows dropped the hook before the key-up
        self.now = int(fired + 300)
        self.child._maintain()                 # capture still running: wait
        self.assertEqual(self.installed, [1])
        self.assertIn(self.child.TIMER_RETRY, self.timers)
        self.now = int(fired + 1600)           # capture over, no repeat for > 700 ms
        self.child._maintain()
        self.assertEqual(self.installed, [1, 2])          # fresh hook installed …
        self.assertEqual(self.unhooked, [1])              # … the dead one removed
        self.assertFalse(self.child._machine.main_key_swallowed)
        self.assertEqual(self.child._mode, "ll")

    def test_a_key_that_still_repeats_defers_the_reinstall(self):
        self.configure("shift+space")
        fired = self.fire()
        machine = self.child._machine
        for t in range(int(fired) + 500, int(fired) + 1700, 33):   # Space really held
            self.assertEqual(machine.on_event(VK_SPACE, True, False, float(t),
                                              key_was_down=False), (True, False))
        self.now = int(fired) + 1700
        self.timers.clear()
        self.child._maintain()
        self.assertEqual(self.installed, [1])
        self.assertEqual(self.timers, [self.child.TIMER_RETRY])     # retried a second later
        self.assertTrue(machine.main_key_swallowed)

    def test_settings_change_refreshes_an_existing_hook(self):
        self.configure("shift+space")
        self.assertFalse(self.child._mask_on_fire)
        self.timers.clear()
        self.configure("win+f", seq=2)
        self.assertTrue(self.child._mask_on_fire)                   # HK-1 wiring
        self.assertEqual(self.timers, [self.child.TIMER_RETRY])     # refresh via _maintain()
        self.child._maintain()
        self.assertEqual(self.installed, [1, 2])
        self.assertEqual([m.get("seq") for m in self.emitted], [1, 2])

    def test_typing_chord_follows_the_configured_hotkey(self):
        # R2-HK-1: only these make our own window a typing context.
        for seq, (spec, typing) in enumerate((("shift+space", True), ("ctrl+shift+space", False),
                                              ("shift+f5", False), ("shift+k", True),
                                              ("alt+space", False)), 1):
            self.configure(spec, seq=seq)
            self.assertIs(self.child._typing_chord, typing, spec)


class LowLevelHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        from projektsog import hotkey
        self.hotkey = hotkey

    def test_mask_key_input(self):
        sent: list[tuple] = []

        def fake_send(count, inputs, size):
            self.assertEqual(size, ctypes.sizeof(self.hotkey._INPUT))
            sent.extend((inputs[i].type, inputs[i].u.ki.wVk, inputs[i].u.ki.dwFlags,
                         inputs[i].u.ki.dwExtraInfo) for i in range(count))
            return count

        with mock.patch.object(self.hotkey, "_SendInput", fake_send):
            self.assertTrue(self.hotkey._send_mask_key())
        tag = self.hotkey._REPLAY_TAG
        self.assertEqual(sent, [(1, 0xE8, 0, tag), (1, 0xE8, 0x2, tag)])   # down, up; not Alt
        with mock.patch.object(self.hotkey, "_SendInput", lambda n, i, s: 0):
            self.assertFalse(self.hotkey._send_mask_key())

    def test_own_window_detection(self):
        state = {"hwnd": 0x77, "cls": "Chrome_WidgetWin_1", "title": "Projektsøg",
                 "visible": True, "iconic": False, "owner": None}

        def text(key):
            def fill(hwnd, buf, size):
                self.assertEqual(hwnd, state["hwnd"])
                buf.value = state[key]
                return len(state[key])
            return fill

        patches = [
            mock.patch.object(self.hotkey, "_GetForegroundWindow", lambda: state["hwnd"]),
            mock.patch.object(self.hotkey, "_GetClassNameW", text("cls")),
            mock.patch.object(self.hotkey, "_GetWindowTextW", text("title")),
            mock.patch.object(self.hotkey, "_IsWindowVisible", lambda h: state["visible"]),
            mock.patch.object(self.hotkey, "_IsIconic", lambda h: state["iconic"]),
            mock.patch.object(self.hotkey, "_GetWindow", lambda h, cmd: state["owner"]),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.assertTrue(self.hotkey._own_window_in_foreground())
        for key, value in (("cls", "Chrome_WidgetWin_0"), ("title", "Projektsøg – Microsoft Edge"),
                           ("title", "127.0.0.1:47811/"), ("visible", False), ("iconic", True),
                           ("owner", 0x55), ("hwnd", None)):
            with self.subTest(key=key, value=value):
                saved = state[key]
                state[key] = value
                self.assertFalse(self.hotkey._own_window_in_foreground())
                state[key] = saved


class StructLayoutTests(unittest.TestCase):
    """Sizes documented for 64-bit Windows; a wrong layout would break SendInput & co."""

    @unittest.skipUnless(ctypes.sizeof(ctypes.c_void_p) == 8, "64-bit layout")
    def test_sizes(self):
        from projektsog import hotkey, window, winui
        expected = {
            hotkey._INPUT: 40, hotkey._KBDLLHOOKSTRUCT: 24, hotkey._WNDCLASSEXW: 80,
            winui._INPUT: 40, winui._SHELLEXECUTEINFOW: 112, winui._PROCESSENTRY32W: 568,
            window._MONITORINFO: 40, window._WINDOWPLACEMENT: 44,
        }
        for struct, size in expected.items():
            with self.subTest(struct=f"{struct.__module__}.{struct.__name__}"):
                self.assertEqual(ctypes.sizeof(struct), size)


# A tiny stand-in for the hook child: speaks the protocol, installs nothing.
FAKE_CHILD = r"""
import json, sys
mode, log_path, count_path = sys.argv[1:4]
try:
    runs = int(open(count_path).read())
except (OSError, ValueError):
    runs = 0
runs += 1
with open(count_path, "w") as fh:
    fh.write(str(runs))

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

for line in sys.stdin:
    msg = json.loads(line)
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(msg) + "\n")
    cmd = msg.get("cmd")
    if cmd == "config":
        if mode == "silent":
            continue
        if mode == "error":
            send({"ev": "error", "msg": "Genvejstasten er optaget", "seq": msg["seq"]})
            continue
        send({"ev": "ready", "mode": "ll", "seq": msg["seq"]})
        if mode == "fire":
            send({"ev": "fire", "from_app": "Resolve.exe"})
        if mode == "crash_always" or (mode == "crash_once" and runs == 1):
            sys.exit(3)
    elif cmd == "quit":
        break
"""


class HotkeyManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(dir=_tmp.name)
        self.log_path = os.path.join(self.dir, "commands.jsonl")
        self.count_path = os.path.join(self.dir, "runs.txt")
        self.fires: list[tuple[dict, str]] = []
        self.fired = threading.Event()
        self.managers: list[HotkeyManager] = []

    def tearDown(self) -> None:
        for manager in self.managers:
            manager.stop()

    def callback(self, info: dict) -> None:
        self.fires.append((info, threading.current_thread().name))
        self.fired.set()

    def manager(self, mode: str, spec: str = "shift+space", **kw) -> HotkeyManager:
        manager = HotkeyManager(spec, self.callback, child_argv=[
            sys.executable, "-c", FAKE_CHILD, mode, self.log_path, self.count_path], **kw)
        manager.READY_TIMEOUT_S = 3.0
        manager.RESTART_DELAY_S = 0.1
        self.managers.append(manager)
        return manager

    def commands(self) -> list[dict]:
        try:
            with open(self.log_path, encoding="utf-8") as fh:
                return [json.loads(line) for line in fh]
        except FileNotFoundError:
            return []

    def runs(self) -> int:
        try:
            with open(self.count_path) as fh:
                return int(fh.read())
        except (OSError, ValueError):
            return 0

    def wait_until(self, predicate, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return predicate()

    def test_ready_fire_end_capture_stop(self):
        m = self.manager("fire", passthrough_apps=["Resolve.exe"], typing_guard_ms=250)
        self.assertTrue(m.start())
        self.assertTrue(m.active)
        self.assertEqual(m.mode, "ll")
        self.assertIsNone(m.last_error)
        self.assertTrue(self.fired.wait(3))
        info, thread = self.fires[0]
        self.assertEqual(set(info), {"from_app", "fired_at"})
        self.assertEqual(info["from_app"], "Resolve.exe")
        self.assertIsInstance(info["fired_at"], float)                 # arrival (R2-APP-1)
        self.assertLessEqual(info["fired_at"], time.monotonic())
        self.assertEqual(thread, "HotkeyManager-dispatch")             # never the reader/hook
        m.end_capture(True)
        self.assertTrue(self.wait_until(lambda: any(c.get("cmd") == "end_capture"
                                                    for c in self.commands())))
        config = self.commands()[0]
        self.assertEqual(config["cmd"], "config")
        self.assertEqual(config["spec"], "shift+space")
        self.assertEqual(config["passthrough"], ["Resolve.exe"])
        self.assertEqual(config["typing_guard_ms"], 250)
        self.assertEqual(config["double_tap_ms"], 400)
        self.assertTrue(config["enabled"])
        self.assertIsInstance(config["seq"], int)
        m.stop()
        self.assertFalse(m.active)
        self.assertIsNone(m.mode)
        self.assertEqual(self.commands()[-1], {"cmd": "quit"})
        m.stop()                                                       # idempotent

    def test_extend_capture_is_sent_to_the_child(self):
        m = self.manager("normal")
        m.extend_capture(3)                                # no child yet: nothing happens
        self.assertTrue(m.start())
        m.extend_capture(2.5)
        for bad in (0, -1, "x", None, float("nan"), float("inf")):
            m.extend_capture(bad)                          # ignored, never raises
        m.end_capture(True)
        self.assertTrue(self.wait_until(lambda: any(c.get("cmd") == "end_capture"
                                                    for c in self.commands())))
        self.assertEqual([c for c in self.commands() if c.get("cmd") == "extend_capture"],
                         [{"cmd": "extend_capture", "ms": 2500}])

    def test_update_sends_config_and_toggles_enabled(self):
        m = self.manager("normal")
        self.assertTrue(m.start())
        self.assertTrue(m.update(spec="ctrl+alt+k", double_tap_ms=300))
        last = [c for c in self.commands() if c.get("cmd") == "config"][-1]
        self.assertEqual((last["spec"], last["double_tap_ms"]), ("ctrl+alt+k", 300))
        self.assertTrue(m.update(enabled=False))
        self.assertFalse(m.active)
        self.assertIsNone(m.mode)
        self.assertEqual(self.runs(), 1)
        self.assertTrue(m.update(enabled=True))                       # fresh child
        self.assertTrue(m.active)
        self.assertEqual(self.runs(), 2)

    def test_update_rejects_invalid_values_without_changing_anything(self):
        m = self.manager("normal")
        self.assertTrue(m.start())
        with self.assertRaises(ValueError):
            m.update(spec="shift+bogus")
        with self.assertRaises(ValueError):
            m.update(typing_guard_ms=-5)
        with self.assertRaises(ValueError):
            m.update(passthrough_apps="Resolve.exe")
        with self.assertRaises(TypeError):
            m.update(colour="blue")
        self.assertEqual(len([c for c in self.commands() if c.get("cmd") == "config"]), 1)
        self.assertTrue(m.active)

    def test_crash_is_restarted(self):
        changes = threading.Event()
        m = self.manager("crash_once", on_status_change=changes.set)
        m.start()
        self.assertTrue(self.wait_until(lambda: self.runs() == 2 and m.active))
        self.assertEqual(m.mode, "ll")
        self.assertTrue(changes.wait(2))

    def test_restart_limit(self):
        m = self.manager("crash_always")
        m.start()
        self.assertTrue(self.wait_until(lambda: self.runs() >= 4))
        time.sleep(1.0)
        self.assertEqual(self.runs(), 4)            # first start + 3 restarts, then it waits
        self.assertFalse(m.active)

    def test_no_ready_times_out(self):
        m = self.manager("silent")
        m.READY_TIMEOUT_S = 0.5
        started = time.monotonic()
        self.assertFalse(m.start())
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertFalse(m.active)

    def test_child_error_reported(self):
        m = self.manager("error")
        self.assertFalse(m.start())
        self.assertFalse(m.active)
        self.assertEqual(m.last_error, "Genvejstasten er optaget")

    def test_invalid_spec_and_disabled_start(self):
        m = self.manager("normal", spec="shift+nope")
        self.assertFalse(m.start())
        self.assertIn("Ukendt tast", m.last_error)
        self.assertEqual(self.runs(), 0)
        m2 = self.manager("normal", enabled=False)
        self.assertTrue(m2.start())                 # nothing to do is not a failure
        self.assertFalse(m2.active)
        self.assertEqual(self.runs(), 0)
        self.assertTrue(m2.update(enabled=True))
        self.assertTrue(m2.active)

    def test_stopped_manager_starts_again_on_enable(self):
        m = self.manager("normal")
        self.assertTrue(m.start())
        m.stop()
        self.assertTrue(m.update(spec="alt+space"))  # stored only
        self.assertFalse(m.active)
        self.assertTrue(m.update(enabled=True))
        self.assertTrue(m.active)
        self.assertEqual([c for c in self.commands() if c.get("cmd") == "config"][-1]["spec"],
                         "alt+space")


if __name__ == "__main__":
    unittest.main()
