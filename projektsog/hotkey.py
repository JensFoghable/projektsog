"""Global hotkey (SPEC §10.3).

* ``parse_hotkey`` / ``format_hotkey`` – the ``mod(+mod)*+key`` grammar and its Danish label.
* ``HotkeyStateMachine`` – pure decision logic per keyboard event: swallow it? fire?
  (exact modifiers, typing guard, double-tap passthrough, OS key-state cross-checks).
* ``CaptureBuffer`` – pure logic for the keys typed between a fire and the search field
  getting focus.
* The hook child (``python -m projektsog.hotkey --child [--dry-run] [--log-file F]``) owns the
  WH_KEYBOARD_LL hook on its main thread. It imports only the standard library and this
  module, so keyboard latency never depends on the main process.
* ``HotkeyManager`` – main-process side: starts, configures and supervises the child.

Protocol (JSON lines, UTF-8). Parent → child::

    {"cmd": "config", "spec", "passthrough": [...], "typing_guard_ms", "double_tap_ms",
     "enabled", "seq": int}
    {"cmd": "end_capture", "ok": bool}          {"cmd": "quit"}
    {"cmd": "extend_capture", "ms": int}        # SPEC §15.10: Edge is being cold-launched

Child → parent::

    {"ev": "ready", "mode": "ll" | "registerhotkey" | null, "seq": int | null}
    {"ev": "fire", "from_app": "Resolve.exe" | null}
    {"ev": "error", "msg": "<Danish>", "seq": int | null}

``seq`` extends SPEC §10.3: the child echoes the sequence number of the config it answers,
so ``HotkeyManager.update()`` can wait until a change is really in effect. A ``ready`` or
``error`` without ``seq`` reports a later state change (e.g. the fallback mode recovered).

SPEC §15.10 additions in the child: for a typing chord (Shift plus space, a letter or a digit)
our own app window (msedge.exe, class ``Chrome_WidgetWin_1``, title exactly ``Projektsøg``) in
the foreground is a passthrough typing context (a single press types a space, a quick double
press toggles); a Ctrl, Alt, Win or F-key hotkey types nothing there and keeps toggling with
one press. A hotkey whose modifiers include Win or Alt but not Ctrl sends an unassigned mask
key when it fires, so the later release of Win/Alt does not open the Start menu or a menu bar.
While a capture runs, pressing the hotkey again never fires – a second fire would restart the
capture and hide the window just shown: a typing chord is captured and replayed into the
search field like any other key, any other chord is absorbed.
"""

from __future__ import annotations

import argparse
import collections
import ctypes
import json
import logging
import logging.handlers
import msvcrt
import os
import queue
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import IO, Any, Callable, Iterable, Sequence

from . import APP_NAME       # the package __init__ is imported by the child anyway (constants only)

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------------------
# Hotkey grammar
# --------------------------------------------------------------------------------------

MODIFIERS: tuple[str, ...] = ("win", "ctrl", "alt", "shift")   # also the display order
_MOD_LABELS = {"win": "Win", "ctrl": "Ctrl", "alt": "Alt", "shift": "Shift"}

VK_SPACE = 0x20
_KEYS: dict[str, int] = {"space": VK_SPACE}
_KEYS.update({chr(c): c - 0x20 for c in range(ord("a"), ord("z") + 1)})   # VK_A..VK_Z
_KEYS.update({str(d): 0x30 + d for d in range(10)})                         # VK_0..VK_9
_KEYS.update({f"f{n}": 0x6F + n for n in range(1, 25)})                     # VK_F1..VK_F24
_KEY_LABELS: dict[int, str] = {vk: name.upper() for name, vk in _KEYS.items()}
_KEY_LABELS[VK_SPACE] = "Mellemrum"


def parse_hotkey(spec: str) -> tuple[frozenset[str], int]:
    """Parse ``"shift+space"`` into ``(frozenset({"shift"}), VK_SPACE)``.

    Grammar ``mod(+mod)*+key`` with mod ∈ ctrl|alt|shift|win and key ∈ space|a–z|0–9|f1–f24,
    case-insensitive, blanks around tokens allowed. Raises ``ValueError`` (Danish message).
    """
    if not isinstance(spec, str) or not spec.strip():
        raise ValueError("Genvejstasten er tom – skriv fx shift+space")
    shown = spec.strip()
    parts = [p.strip().lower() for p in shown.split("+")]
    if any(not p for p in parts):
        raise ValueError(f"Ugyldig genvejstast ‘{shown}’ – brug formen modifikator+tast, "
                         "fx shift+space")
    *mods, key = parts
    seen: set[str] = set()
    for mod in mods:
        if mod not in MODIFIERS:
            raise ValueError(f"Ukendt modifikatortast ‘{mod}’ i ‘{shown}’ – "
                             "brug ctrl, alt, shift eller win")
        if mod in seen:
            raise ValueError(f"Modifikatortasten ‘{mod}’ er angivet flere gange i ‘{shown}’")
        seen.add(mod)
    vk = _KEYS.get(key)
    if vk is None:
        if key in MODIFIERS:
            raise ValueError(f"Genvejstasten ‘{shown}’ skal slutte med en almindelig tast, "
                             "fx space")
        raise ValueError(f"Ukendt tast ‘{key}’ i ‘{shown}’ – brug space, a–z, 0–9 eller f1–f24")
    if not seen:
        raise ValueError(f"Genvejstasten ‘{shown}’ skal have mindst én modifikatortast "
                         "(ctrl, alt, shift eller win)")
    return frozenset(seen), vk


def format_hotkey(spec: str) -> str:
    """Danish display label: ``"shift+space"`` → ``"Shift+Mellemrum"``. Raises ``ValueError``."""
    mods, vk = parse_hotkey(spec)
    return "+".join([_MOD_LABELS[m] for m in MODIFIERS if m in mods] + [_KEY_LABELS[vk]])


def needs_menu_mask(mods: Iterable[str]) -> bool:
    """Does a hotkey with these modifiers need a mask key when it fires (HK-1)?

    The hook swallows the main key, so Windows sees Win↓ Win↑ (or Alt↓ Alt↑, Alt+Shift) with
    nothing in between – which opens the Start menu, activates a menu bar or toggles the input
    language. A physically pressed Ctrl already counts as a key in between, so only hotkeys
    with Win or Alt and without Ctrl need the mask."""
    mods = frozenset(mods)
    return bool(mods & {"win", "alt"}) and "ctrl" not in mods


_TYPING_VKS = frozenset({VK_SPACE, *range(0x30, 0x3A), *range(0x41, 0x5B)})   # space, 0–9, A–Z


def is_typing_chord(mods: Iterable[str], vk: int) -> bool:
    """Does a press of this hotkey type something (SPEC §15.10)? Only Shift plus a key that
    types a character: Shift held for the next capitalised word makes Shift+Space a space in the
    search field. With Ctrl, Alt or Win – or on an F-key – a press types nothing; it is a
    command, so in our own window one press still hides it (R2-HK-1)."""
    return frozenset(mods) == {"shift"} and vk in _TYPING_VKS


# --------------------------------------------------------------------------------------
# Pure decision logic
# --------------------------------------------------------------------------------------

# Low-level hooks report side-specific codes; the generic ones are mapped to the left key.
_MOD_OF_VK: dict[int, str] = {
    0xA0: "shift", 0xA1: "shift", 0xA2: "ctrl", 0xA3: "ctrl",
    0xA4: "alt", 0xA5: "alt", 0x5B: "win", 0x5C: "win",
}
_GENERIC_MOD_VK = {0x10: 0xA0, 0x11: 0xA2, 0x12: 0xA4}   # VK_SHIFT/CONTROL/MENU → left key


def _canonical_vk(vk: int) -> int:
    return _GENERIC_MOD_VK.get(vk, vk)


def is_modifier_vk(vk: int) -> bool:
    return _canonical_vk(vk) in _MOD_OF_VK


class HotkeyStateMachine:
    """Decides for every keyboard event whether to swallow it and whether the hotkey fires.

    Pure logic without Windows calls. ``t_ms`` is any monotonic millisecond clock (the hook
    passes the event timestamps). Rules (SPEC §10.3):

    * Fire only on a fresh main-key down while exactly the required modifiers are held.
      ``held_mods`` (from ``GetAsyncKeyState``) overrides internal tracking; a required
      modifier shown up resets the modifier state (lost key-ups).
    * Typing guard: no fire (and no swallow) if a non-modifier key went down less than
      ``typing_guard_ms`` before, or – for Shift-only hotkeys – any other key went down
      since Shift went down.
    * Swallow the firing down, its auto-repeats and its up; ignore injected events.
    * ``passthrough`` (a passthrough app is in the foreground): a single press passes
      through; a second press within ``double_tap_ms`` (no other key in between) fires.
    * ``capturing`` (the keys typed after a fire are being captured): a fresh press never
      fires (R2-APP-1). For a typing chord it is typing – not swallowed here, so the capture
      takes it; any other chord types nothing, so its press is swallowed and dropped.

    ``key_was_down`` is the OS view "the key was already down before this event". Because
    a swallowed key-down never reaches the OS key state, the auto-repeats of a press we
    swallowed may arrive with ``key_was_down=False``; they are told apart from a new press
    after a lost key-up by timing: a repeat follows the previous down of the same key within
    ``repeat_window_ms`` (keyboard repeat delay + slack).
    """

    def __init__(self, mods: frozenset[str], vk: int, *, typing_guard_ms: int = 300,
                 double_tap_ms: int = 400, repeat_window_ms: int = 1150) -> None:
        self.mods = frozenset(mods)
        self.vk = vk
        self.typing_guard_ms = typing_guard_ms
        self.double_tap_ms = double_tap_ms
        self.repeat_window_ms = repeat_window_ms
        self._shift_only = self.mods == {"shift"}
        self._typing_chord = is_typing_chord(self.mods, vk)
        self.reset()

    def reset(self) -> None:
        self._down_mod_vks: set[int] = set()       # modifier keys held (internal tracking)
        self._other_since_mod = False              # other key went down while a required mod was held
        self._last_typing_t: float | None = None   # last non-modifier key down that counts as typing
        self._main_down: str | None = None         # None | "swallowed" | "passed"
        self._main_last_t: float | None = None     # last main-key down event (incl. repeats)
        self._tap_t: float | None = None           # passed-through press that may start a double tap

    @property
    def main_key_swallowed(self) -> bool:
        """The main key went down, was swallowed, and its key-up has not been seen yet."""
        return self._main_down == "swallowed"

    def main_key_stale(self, t_ms: float) -> bool:
        """Is the swallowed main-key state older than ``repeat_window_ms`` at ``t_ms``?

        A key that is really held keeps producing auto-repeat downs; when none arrived for
        longer than the repeat window, its key-up was lost (e.g. Windows silently removed a
        slow hook) and the key cannot still be down. (The OS key state is no proof: a
        swallowed key-down never reaches it.)"""
        return (self._main_down == "swallowed"
                and (self._main_last_t is None or t_ms - self._main_last_t > self.repeat_window_ms))

    def forget_main_key(self) -> None:
        """Drop a stale 'main key swallowed' state (its key-up will never be seen)."""
        self._main_down = None

    def on_event(self, vk: int, down: bool, injected: bool, t_ms: float, *,
                 held_mods: frozenset[str] | None = None, key_was_down: bool | None = None,
                 passthrough: bool = False, capturing: bool = False) -> tuple[bool, bool]:
        """Process one event; returns ``(swallow, fire)``."""
        if injected:
            return False, False
        vk = _canonical_vk(vk)
        mod = _MOD_OF_VK.get(vk)
        if mod is not None:
            self._on_modifier(vk, mod, down, key_was_down)
            return False, False
        if vk != self.vk:
            if down:
                self._on_other_key_down(t_ms)
            return False, False
        if not down:
            swallowed = self._main_down == "swallowed"
            self._main_down = None
            return swallowed, False
        return self._on_main_down(t_ms, held_mods, key_was_down, passthrough, capturing)

    # -- helpers ---------------------------------------------------------------------------
    def _held(self) -> frozenset[str]:
        return frozenset(_MOD_OF_VK[v] for v in self._down_mod_vks)

    def _required_held(self) -> bool:
        return any(_MOD_OF_VK[v] in self.mods for v in self._down_mod_vks)

    def _on_modifier(self, vk: int, mod: str, down: bool, key_was_down: bool | None) -> None:
        if not down:
            self._down_mod_vks.discard(vk)
            return
        if mod in self.mods:
            # The modifier becoming held restarts "other key since modifier". For modifier
            # events ``key_was_down`` means "this modifier (either side) was already held";
            # it tells a new press from an auto-repeat even after a lost key-up.
            fresh = (not key_was_down) if key_was_down is not None else mod not in self._held()
            if fresh:
                self._other_since_mod = False
        elif self._required_held():
            self._other_since_mod = True
        self._down_mod_vks.add(vk)

    def _on_other_key_down(self, t_ms: float) -> None:
        self._last_typing_t = t_ms
        self._tap_t = None
        if self._required_held():
            self._other_since_mod = True

    def _sync_mods(self, held_mods: frozenset[str]) -> None:
        if any(m not in held_mods for m in self.mods):
            self._other_since_mod = False
        self._down_mod_vks = {v for v in self._down_mod_vks if _MOD_OF_VK[v] in held_mods}

    def _is_repeat(self, t_ms: float, key_was_down: bool | None) -> bool:
        if self._main_down is None:
            return bool(key_was_down)      # repeat of a press we never saw → pass it on
        if key_was_down or key_was_down is None:
            return True
        # The OS says "was up": a repeat of our swallowed press, or a new press after a
        # lost key-up.
        if (self._main_down == "swallowed" and self._main_last_t is not None
                and 0 <= t_ms - self._main_last_t <= self.repeat_window_ms):
            return True
        self._main_down = None
        return False

    def _typing(self, t_ms: float) -> bool:
        if self._last_typing_t is not None and t_ms - self._last_typing_t < self.typing_guard_ms:
            return True
        return self._shift_only and self._other_since_mod

    def _on_main_down(self, t_ms: float, held_mods: frozenset[str] | None,
                      key_was_down: bool | None, passthrough: bool,
                      capturing: bool) -> tuple[bool, bool]:
        repeat = self._is_repeat(t_ms, key_was_down)
        self._main_last_t = t_ms
        if repeat:
            return self._main_down == "swallowed", False
        if held_mods is not None:
            self._sync_mods(held_mods)
            held = frozenset(held_mods)
        else:
            held = self._held()
        # Typing: the wrong modifiers, the typing guard – or a typing chord while the keys typed
        # after a fire are captured, e.g. a space for the search field (R2-APP-1).
        if held != self.mods or self._typing(t_ms) or (capturing and self._typing_chord):
            self._main_down = "passed"
            self._last_typing_t = t_ms
            self._tap_t = None
            return False, False
        if capturing:
            # The hotkey again while its keys are captured: never a second fire, which would
            # drop them and hide the window just shown. A chord that types nothing is absorbed.
            self._tap_t = None
            self._main_down = "swallowed"
            return True, False
        if passthrough:
            if self._tap_t is not None and 0 <= t_ms - self._tap_t <= self.double_tap_ms:
                self._tap_t = None
                self._main_down = "swallowed"
                return True, True
            self._tap_t = t_ms
            self._main_down = "passed"
            return False, False
        self._tap_t = None
        self._main_down = "swallowed"
        return True, True


@dataclass(frozen=True, slots=True)
class KeyEvent:
    vk: int
    scan: int
    extended: bool
    down: bool


class CaptureBuffer:
    """Keys typed between a fire and the search field getting focus (SPEC §10.3).

    While active, every non-injected, non-modifier key event offered via ``feed`` is
    swallowed and buffered – except key-ups whose key-down was not captured (those follow
    their key-down to wherever it went, so no key looks stuck). ``finish(True)`` returns the
    events for replay in order; anything else drops them. Capture ends by itself
    ``timeout_ms`` after ``start``, unless ``extend`` moved that deadline (SPEC §15.10: the
    main process asks for more time while Edge is cold-launched). An extension reaches at
    most ``max_extend_ms`` beyond the moment it is asked for and never more than
    ``max_total_ms`` beyond the fire, so a hung main process cannot swallow keys for long.
    """

    def __init__(self, timeout_ms: float = 1500, max_events: int = 256, *,
                 max_extend_ms: float = 5000, max_total_ms: float = 12000) -> None:
        self.timeout_ms = timeout_ms
        self.max_events = max_events
        self.max_extend_ms = max_extend_ms
        self.max_total_ms = max_total_ms
        self._started: float | None = None
        self._deadline = 0.0
        self._events: list[KeyEvent] = []
        self._downs: set[int] = set()

    @property
    def active(self) -> bool:
        return self._started is not None

    def start(self, t_ms: float) -> None:
        self.cancel()
        self._started = t_ms
        self._deadline = t_ms + self.timeout_ms

    def cancel(self) -> None:
        self._started = None
        self._events = []
        self._downs = set()

    def extend(self, ms: float, t_ms: float) -> bool:
        """Keep capturing until ``ms`` after ``t_ms`` (see the class doc for the caps).
        Never revives an expired capture; True when a capture is running."""
        self.expire(t_ms)
        if self._started is None:
            return False
        ms = min(max(0.0, float(ms)), self.max_extend_ms)
        self._deadline = max(self._deadline,
                             min(t_ms + ms, self._started + self.max_total_ms))
        return True

    def _expired(self, t_ms: float) -> bool:
        return self._started is not None and t_ms > self._deadline

    def expire(self, t_ms: float) -> None:
        """Drop a capture whose time is up (also when no key arrived to notice it)."""
        if self._expired(t_ms):
            self.cancel()

    def feed(self, event: KeyEvent, t_ms: float) -> bool:
        """Offer an event; True means it was captured and must be swallowed."""
        self.expire(t_ms)
        if self._started is None:
            return False
        if event.down:
            self._downs.add(event.vk)
        elif event.vk in self._downs:
            self._downs.discard(event.vk)
        else:
            return False
        if len(self._events) < self.max_events:
            self._events.append(event)
        return True

    def finish(self, ok: bool, t_ms: float) -> list[KeyEvent]:
        """End the capture; returns the events to replay (empty unless ``ok`` in time)."""
        events = self._events if ok and self.active and not self._expired(t_ms) else []
        self.cancel()
        return events


# --------------------------------------------------------------------------------------
# Win32 (used by the child process only; declarations are cheap)
# --------------------------------------------------------------------------------------

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

HANDLE = wintypes.HANDLE
LRESULT = ctypes.c_ssize_t
_HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
_WNDPROC = ctypes.WINFUNCTYPE(LRESULT, HANDLE, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


class _WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT), ("lpfnWndProc", _WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", HANDLE), ("hIcon", HANDLE), ("hCursor", HANDLE),
                ("hbrBackground", HANDLE), ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", HANDLE)]


def _declare(dll: ctypes.WinDLL, name: str, restype: Any, *argtypes: Any) -> Any:
    fn = getattr(dll, name)
    fn.restype = restype
    fn.argtypes = list(argtypes)
    return fn


_SetWindowsHookExW = _declare(_user32, "SetWindowsHookExW", HANDLE,
                              ctypes.c_int, _HOOKPROC, HANDLE, wintypes.DWORD)
_UnhookWindowsHookEx = _declare(_user32, "UnhookWindowsHookEx", wintypes.BOOL, HANDLE)
_CallNextHookEx = _declare(_user32, "CallNextHookEx", LRESULT,
                           HANDLE, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
_GetAsyncKeyState = _declare(_user32, "GetAsyncKeyState", ctypes.c_short, ctypes.c_int)
_GetForegroundWindow = _declare(_user32, "GetForegroundWindow", HANDLE)
_GetWindowThreadProcessId = _declare(_user32, "GetWindowThreadProcessId", wintypes.DWORD,
                                     HANDLE, ctypes.POINTER(wintypes.DWORD))
_GetClassNameW = _declare(_user32, "GetClassNameW", ctypes.c_int, HANDLE, wintypes.LPWSTR,
                          ctypes.c_int)
_GetWindowTextW = _declare(_user32, "GetWindowTextW", ctypes.c_int, HANDLE, wintypes.LPWSTR,
                           ctypes.c_int)
_IsWindowVisible = _declare(_user32, "IsWindowVisible", wintypes.BOOL, HANDLE)
_IsIconic = _declare(_user32, "IsIconic", wintypes.BOOL, HANDLE)
_GetWindow = _declare(_user32, "GetWindow", HANDLE, HANDLE, wintypes.UINT)
_SendInput = _declare(_user32, "SendInput", wintypes.UINT,
                      wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int)
_RegisterHotKey = _declare(_user32, "RegisterHotKey", wintypes.BOOL,
                           HANDLE, ctypes.c_int, wintypes.UINT, wintypes.UINT)
_UnregisterHotKey = _declare(_user32, "UnregisterHotKey", wintypes.BOOL, HANDLE, ctypes.c_int)
_RegisterClassExW = _declare(_user32, "RegisterClassExW", wintypes.ATOM,
                             ctypes.POINTER(_WNDCLASSEXW))
_CreateWindowExW = _declare(_user32, "CreateWindowExW", HANDLE,
                            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                            HANDLE, HANDLE, HANDLE, wintypes.LPVOID)
_DestroyWindow = _declare(_user32, "DestroyWindow", wintypes.BOOL, HANDLE)
_DefWindowProcW = _declare(_user32, "DefWindowProcW", LRESULT,
                           HANDLE, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
_GetMessageW = _declare(_user32, "GetMessageW", wintypes.BOOL,
                        ctypes.POINTER(wintypes.MSG), HANDLE, wintypes.UINT, wintypes.UINT)
_TranslateMessage = _declare(_user32, "TranslateMessage", wintypes.BOOL,
                             ctypes.POINTER(wintypes.MSG))
_DispatchMessageW = _declare(_user32, "DispatchMessageW", LRESULT, ctypes.POINTER(wintypes.MSG))
_PostMessageW = _declare(_user32, "PostMessageW", wintypes.BOOL,
                         HANDLE, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
_PostQuitMessage = _declare(_user32, "PostQuitMessage", None, ctypes.c_int)
_SetTimer = _declare(_user32, "SetTimer", ctypes.c_size_t,
                     HANDLE, ctypes.c_size_t, wintypes.UINT, wintypes.LPVOID)
_KillTimer = _declare(_user32, "KillTimer", wintypes.BOOL, HANDLE, ctypes.c_size_t)
_SystemParametersInfoW = _declare(_user32, "SystemParametersInfoW", wintypes.BOOL,
                                  wintypes.UINT, wintypes.UINT, wintypes.LPVOID, wintypes.UINT)
_GetModuleHandleW = _declare(_kernel32, "GetModuleHandleW", HANDLE, wintypes.LPCWSTR)
_OpenProcess = _declare(_kernel32, "OpenProcess", HANDLE,
                        wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_CloseHandle = _declare(_kernel32, "CloseHandle", wintypes.BOOL, HANDLE)
_QueryFullProcessImageNameW = _declare(_kernel32, "QueryFullProcessImageNameW", wintypes.BOOL,
                                       HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                       ctypes.POINTER(wintypes.DWORD))
_GetTickCount64 = _declare(_kernel32, "GetTickCount64", ctypes.c_uint64)
_GetCurrentThread = _declare(_kernel32, "GetCurrentThread", HANDLE)
_SetThreadPriority = _declare(_kernel32, "SetThreadPriority", wintypes.BOOL, HANDLE, ctypes.c_int)
_GetStdHandle = _declare(_kernel32, "GetStdHandle", HANDLE, wintypes.DWORD)

WH_KEYBOARD_LL = 13
HC_ACTION = 0
WM_DESTROY = 0x0002
WM_TIMER = 0x0113
WM_HOTKEY = 0x0312
WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104
WM_POWERBROADCAST = 0x0218
WM_WTSSESSION_CHANGE = 0x02B1
WM_APP = 0x8000
LLKHF_EXTENDED = 0x01
LLKHF_LOWER_IL_INJECTED = 0x02
LLKHF_INJECTED = 0x10
INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
_MOD_FLAGS = {"alt": MOD_ALT, "ctrl": MOD_CONTROL, "shift": MOD_SHIFT, "win": MOD_WIN}
WTS_CONSOLE_CONNECT, WTS_REMOTE_CONNECT, WTS_SESSION_LOGON, WTS_SESSION_UNLOCK = 0x1, 0x3, 0x5, 0x8
PBT_APMRESUMESUSPEND, PBT_APMRESUMEAUTOMATIC = 0x7, 0x12
SPI_GETKEYBOARDDELAY = 0x0016
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
THREAD_PRIORITY_HIGHEST = 2
STD_INPUT_HANDLE = -10 & 0xFFFFFFFF
STD_OUTPUT_HANDLE = -11 & 0xFFFFFFFF
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
GW_OWNER = 4

# Keys whose OS state tells whether a modifier (either side) is held.
_ASYNC_MOD_VKS: dict[str, tuple[int, ...]] = {"shift": (0x10,), "ctrl": (0x11,), "alt": (0x12,),
                                              "win": (0x5B, 0x5C)}
_REPLAY_TAG = 0x50534F47   # dwExtraInfo of replayed keys ("PSOG"), useful when debugging
# Unassigned virtual key sent while Win/Alt is still held after a fire, so their release is not
# a "lone" press (the approach of AutoHotkey's #MenuMaskKey). It is not Alt and no real key.
MASK_VK = 0xE8
# Our own UI window (SPEC §10.2 / §15.10): exact class, title and process image.
_OWN_WINDOW_CLASS = "Chrome_WidgetWin_1"
_OWN_WINDOW_EXE = "msedge.exe"


def _key_down_now(vk: int) -> bool:
    return bool(_GetAsyncKeyState(vk) & 0x8000)


def _async_held_mods() -> frozenset[str]:
    return frozenset(mod for mod, vks in _ASYNC_MOD_VKS.items()
                     if any(_key_down_now(v) for v in vks))


def _modifier_held_now(vk: int) -> bool:
    """Is the modifier family of ``vk`` (either side) held according to the OS?"""
    return any(_key_down_now(v) for v in _ASYNC_MOD_VKS[_MOD_OF_VK[_canonical_vk(vk)]])


def _foreground_exe() -> str | None:
    """Image name of the foreground window's process, e.g. ``"Resolve.exe"``."""
    hwnd = _GetForegroundWindow()
    if not hwnd:
        return None
    pid = wintypes.DWORD()
    _GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    handle = _OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if not _QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return None
        return buf.value.rsplit("\\", 1)[-1] or None
    finally:
        _CloseHandle(handle)


def _own_window_in_foreground() -> bool:
    """Is the foreground window our own UI (visible top-level ``Chrome_WidgetWin_1`` titled
    exactly ``Projektsøg``)? The caller has checked the process image (msedge.exe). Safe in
    the hook: for other processes' windows these calls read cached data and send nothing."""
    hwnd = _GetForegroundWindow()
    if not hwnd:
        return False
    buf = ctypes.create_unicode_buffer(256)
    if not _GetClassNameW(hwnd, buf, len(buf)) or buf.value != _OWN_WINDOW_CLASS:
        return False
    _GetWindowTextW(hwnd, buf, len(buf))
    return (buf.value == APP_NAME and bool(_IsWindowVisible(hwnd)) and not _IsIconic(hwnd)
            and not _GetWindow(hwnd, GW_OWNER))


def _send_mask_key() -> bool:
    """Inject MASK_VK down+up (tagged, so our own hook ignores it). Called in the hook right
    after a fire while Win/Alt is still held: the mask reaches Windows before the modifier's
    release, which then no longer counts as a lone press. (Swallowing the release and
    re-injecting it would risk a stuck Win/Alt key when UIPI silently drops injected input.)
    No logging here – this runs inside the hook."""
    inputs = (_INPUT * 2)()
    for item, flags in zip(inputs, (0, KEYEVENTF_KEYUP)):
        item.type = INPUT_KEYBOARD
        item.u.ki = _KEYBDINPUT(wVk=MASK_VK, wScan=0, dwFlags=flags, time=0,
                                dwExtraInfo=_REPLAY_TAG)
    return _SendInput(2, inputs, ctypes.sizeof(_INPUT)) == 2


def _event_time_ms(raw: int) -> float:
    """Map a 32-bit event timestamp onto the 64-bit tick count (handles the 49.7-day wrap)."""
    now = int(_GetTickCount64())
    age = (now - raw) & 0xFFFFFFFF
    return float(now - (age if age < 0x80000000 else 0))


def _keyboard_repeat_window_ms() -> int:
    """Longest gap between auto-repeat events of a held key, plus slack."""
    delay = wintypes.UINT(1)
    if not _SystemParametersInfoW(SPI_GETKEYBOARDDELAY, 0, ctypes.byref(delay), 0):
        delay.value = 3
    # Repeat delay is 250–1000 ms (setting 0–3); the slowest repeat interval is 400 ms.
    return max(250 * (min(delay.value, 3) + 1), 400) + 200


def _send_keys(events: Sequence[KeyEvent]) -> None:
    inputs = (_INPUT * len(events))()
    for item, event in zip(inputs, events):
        item.type = INPUT_KEYBOARD
        item.u.ki = _KEYBDINPUT(
            wVk=event.vk, wScan=event.scan,
            dwFlags=(KEYEVENTF_EXTENDEDKEY if event.extended else 0)
            | (0 if event.down else KEYEVENTF_KEYUP),
            time=0, dwExtraInfo=_REPLAY_TAG)
    sent = _SendInput(len(events), inputs, ctypes.sizeof(_INPUT))
    if sent != len(events):
        log.warning("replayed only %d of %d key events: %s", sent, len(events),
                    ctypes.WinError(ctypes.get_last_error()))


# --------------------------------------------------------------------------------------
# Child process
# --------------------------------------------------------------------------------------

class _LineWriter:
    """Writes JSON lines to the parent from its own thread, so the hook never blocks on I/O."""

    def __init__(self, stream: IO[bytes]) -> None:
        self._stream = stream
        self._queue: queue.SimpleQueue[dict | None] = queue.SimpleQueue()
        self._thread = threading.Thread(target=self._run, name="hotkey-writer", daemon=True)
        self.on_broken: Callable[[], None] = lambda: None

    def start(self) -> None:
        self._thread.start()

    def put(self, message: dict) -> None:
        self._queue.put(message)

    def close(self, timeout: float) -> None:
        self._queue.put(None)
        self._thread.join(timeout)

    def _run(self) -> None:
        while (message := self._queue.get()) is not None:
            try:
                self._stream.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
                self._stream.flush()
            except (OSError, ValueError):
                log.info("pipe to the main process is closed")
                self.on_broken()
                return


class _HookChild:
    """The hook thread: hidden top-level window, LL hook (or RegisterHotKey), message loop."""

    WM_COMMAND_QUEUED = WM_APP + 1
    TIMER_MAINTAIN, TIMER_RETRY, TIMER_FIRST_CONFIG = 1, 2, 3
    MAINTAIN_MS = 5 * 60 * 1000     # re-install the hook this often (Windows drops slow hooks)
    RETRY_MS = 1000
    FIRST_CONFIG_MS = 10_000
    HOTKEY_ID = 1
    _CLASS_NAME = "Projektsog.HotkeyChild"

    def __init__(self, emit: Callable[[dict], None], *, dry_run: bool) -> None:
        self._emit = emit
        self._dry_run = dry_run
        self._commands: queue.SimpleQueue[dict] = queue.SimpleQueue()
        self._hwnd: int | None = None
        self._hook: int | None = None
        self._hotkey_registered = False
        self._mode: str | None = None
        self._configured = False
        self._enabled = False
        self._params: tuple | None = None
        self._mods: frozenset[str] = frozenset()
        self._vk = 0
        self._label = ""
        self._passthrough: frozenset[str] = frozenset()
        self._typing_chord = False               # Shift+Space & co.: our window types it
        self._mask_on_fire = False               # Win/Alt hotkey without Ctrl (HK-1)
        self._mask_failures = 0                  # counted in the hook, logged outside it
        self._machine: HotkeyStateMachine | None = None
        self._capture = CaptureBuffer()
        self._repeat_window_ms = _keyboard_repeat_window_ms()
        self._hook_errors = 0
        # ctypes callbacks must stay referenced for as long as Windows may call them.
        self._hook_proc = _HOOKPROC(self._on_hook)
        self._wnd_proc = _WNDPROC(self._on_message)

    # -- called from other threads -------------------------------------------------------------
    def post(self, command: dict) -> None:
        self._commands.put(command)
        hwnd = self._hwnd
        if hwnd:
            _PostMessageW(hwnd, self.WM_COMMAND_QUEUED, 0, 0)

    # -- main thread ---------------------------------------------------------------------------
    def run(self, commands_in: IO[bytes]) -> int:
        if not _SetThreadPriority(_GetCurrentThread(), THREAD_PRIORITY_HIGHEST):
            log.debug("could not raise the hook thread priority")
        hwnd = self._create_window()
        if not hwnd:
            self._emit({"ev": "error", "msg": "Genvejstasten kunne ikke aktiveres (intern fejl)",
                        "seq": None})
            return 3
        self._hwnd = hwnd
        wts = _register_session_notifications(hwnd)
        _SetTimer(hwnd, self.TIMER_MAINTAIN, self.MAINTAIN_MS, None)
        _SetTimer(hwnd, self.TIMER_FIRST_CONFIG, self.FIRST_CONFIG_MS, None)
        reader = threading.Thread(target=_read_commands, args=(commands_in, self.post),
                                  name="hotkey-reader", daemon=True)
        reader.start()
        msg = wintypes.MSG()
        try:
            while True:
                result = _GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result == 0:
                    break
                if result == -1:
                    log.error("GetMessageW failed: %s", ctypes.WinError(ctypes.get_last_error()))
                    break
                _TranslateMessage(ctypes.byref(msg))
                _DispatchMessageW(ctypes.byref(msg))
        finally:
            self._deactivate()
            if wts is not None:
                wts()
            self._hwnd = None
            _DestroyWindow(hwnd)
        log.info("hotkey child stopped")
        return 0

    def _create_window(self) -> int | None:
        hinstance = _GetModuleHandleW(None)
        wc = _WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(_WNDCLASSEXW)
        wc.lpfnWndProc = self._wnd_proc
        wc.hInstance = hinstance
        wc.lpszClassName = self._CLASS_NAME
        if not _RegisterClassExW(ctypes.byref(wc)):
            log.error("RegisterClassExW failed: %s", ctypes.WinError(ctypes.get_last_error()))
            return None
        # A hidden TOP-LEVEL window (not message-only): it must receive WM_POWERBROADCAST.
        hwnd = _CreateWindowExW(0, self._CLASS_NAME, "Projektsøg genvejstast", 0,
                                0, 0, 0, 0, None, None, hinstance, None)
        if not hwnd:
            log.error("CreateWindowExW failed: %s", ctypes.WinError(ctypes.get_last_error()))
        return hwnd

    def _on_message(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        try:
            if msg == self.WM_COMMAND_QUEUED:
                self._drain_commands()
                return 0
            if msg == WM_HOTKEY and wparam == self.HOTKEY_ID:
                self._emit({"ev": "fire", "from_app": _foreground_exe()})
                return 0
            if msg == WM_TIMER:
                self._on_timer(wparam)
                return 0
            if msg == WM_WTSSESSION_CHANGE and wparam in (
                    WTS_SESSION_UNLOCK, WTS_CONSOLE_CONNECT, WTS_REMOTE_CONNECT, WTS_SESSION_LOGON):
                self._schedule_retry()
            elif msg == WM_POWERBROADCAST and wparam in (PBT_APMRESUMEAUTOMATIC,
                                                         PBT_APMRESUMESUSPEND):
                self._schedule_retry()
            elif msg == WM_DESTROY:
                _PostQuitMessage(0)
                return 0
        except Exception:
            log.exception("hotkey child: message handling failed")
        return _DefWindowProcW(hwnd, msg, wparam, lparam)

    def _drain_commands(self) -> None:
        while True:
            try:
                command = self._commands.get_nowait()
            except queue.Empty:
                return
            kind = command.get("cmd")
            if kind == "config":
                self._apply_config(command)
            elif kind == "end_capture":
                self._end_capture(bool(command.get("ok")))
            elif kind == "extend_capture":
                self._extend_capture(command.get("ms"))
            elif kind == "quit":
                _PostQuitMessage(0)
                return
            else:
                log.warning("unknown command %r", kind)

    def _apply_config(self, command: dict) -> None:
        seq = command.get("seq")
        spec = command.get("spec")
        try:
            mods, vk = parse_hotkey(spec)
            guard = _non_negative_ms(command.get("typing_guard_ms", 300))
            tap = _non_negative_ms(command.get("double_tap_ms", 400))
            apps = command.get("passthrough") or []
            if isinstance(apps, str) or not all(isinstance(a, str) for a in apps):
                raise ValueError("passthrough skal være en liste af programnavne")
        except (TypeError, ValueError) as exc:
            self._emit({"ev": "error", "msg": str(exc), "seq": seq})
            return
        _KillTimer(self._hwnd, self.TIMER_FIRST_CONFIG)
        self._configured = True
        key_changed = (mods, vk) != (self._mods, self._vk)
        self._mods, self._vk, self._label = mods, vk, format_hotkey(spec)
        self._passthrough = frozenset(a.strip().casefold() for a in apps if a.strip())
        self._typing_chord = is_typing_chord(mods, vk)
        self._mask_on_fire = needs_menu_mask(mods)
        params = (mods, vk, guard, tap)
        if params != self._params:
            self._params = params
            self._machine = HotkeyStateMachine(mods, vk, typing_guard_ms=guard, double_tap_ms=tap,
                                               repeat_window_ms=self._repeat_window_ms)
            self._capture.cancel()
        self._enabled = bool(command.get("enabled", True))
        if not self._enabled:
            self._deactivate()
            self._emit({"ev": "ready", "mode": None, "seq": seq})
            return
        if key_changed and self._hotkey_registered:
            self._unregister_hotkey()
        had_hook = self._hook is not None
        error = self._activate()
        if had_hook and self._mode == "ll":
            # _activate() keeps an existing handle, which may be dead (Windows silently drops
            # a slow hook). A settings change is often an attempt to revive a hotkey that
            # stopped working: refresh the hook soon, through the guarded _maintain() path.
            self._schedule_retry()
        if self._mode is not None:
            log.info("hotkey %s active (mode %s%s)", self._label, self._mode,
                     ", dry run" if self._dry_run else "")
            self._emit({"ev": "ready", "mode": self._mode, "seq": seq})
        else:
            self._emit({"ev": "error", "msg": error, "seq": seq})

    def _activate(self) -> str | None:
        """Make the hotkey work: LL hook, else RegisterHotKey. Returns a Danish error or None."""
        if self._hook is None:
            self._hook = self._install_hook()
        if self._hook is not None:
            if self._hotkey_registered:
                self._unregister_hotkey()
            self._mode = "ll"
            return None
        if self._dry_run:
            self._mode = None
            return "Tastaturkrogen kunne ikke installeres"
        if self._hotkey_registered or self._register_hotkey():
            self._mode = "registerhotkey"
            return None
        self._mode = None
        return (f"Genvejstasten {self._label} kunne ikke aktiveres – den bruges måske af et "
                "andet program")

    def _deactivate(self) -> None:
        if self._hook is not None:
            _UnhookWindowsHookEx(self._hook)
            self._hook = None
        if self._hotkey_registered:
            self._unregister_hotkey()
        self._capture.cancel()
        self._mode = None

    def _install_hook(self) -> int | None:
        hook = _SetWindowsHookExW(WH_KEYBOARD_LL, self._hook_proc, _GetModuleHandleW(None), 0)
        if not hook:
            log.error("SetWindowsHookExW failed: %s", ctypes.WinError(ctypes.get_last_error()))
            return None
        return hook

    def _register_hotkey(self) -> bool:
        flags = MOD_NOREPEAT
        for mod in self._mods:
            flags |= _MOD_FLAGS[mod]
        self._hotkey_registered = bool(_RegisterHotKey(self._hwnd, self.HOTKEY_ID, flags, self._vk))
        if not self._hotkey_registered:
            log.error("RegisterHotKey(%s) failed: %s", self._label,
                      ctypes.WinError(ctypes.get_last_error()))
        return self._hotkey_registered

    def _unregister_hotkey(self) -> None:
        _UnregisterHotKey(self._hwnd, self.HOTKEY_ID)
        self._hotkey_registered = False

    def _on_timer(self, timer_id: int) -> None:
        if timer_id == self.TIMER_RETRY:
            _KillTimer(self._hwnd, self.TIMER_RETRY)
            self._maintain()
        elif timer_id == self.TIMER_MAINTAIN:
            self._maintain()
        elif timer_id == self.TIMER_FIRST_CONFIG:
            _KillTimer(self._hwnd, self.TIMER_FIRST_CONFIG)
            if not self._configured:
                log.error("no configuration received from the main process – exiting")
                _PostQuitMessage(0)

    def _schedule_retry(self) -> None:
        if self._hwnd:
            _SetTimer(self._hwnd, self.TIMER_RETRY, self.RETRY_MS, None)

    def _maintain(self) -> None:
        """Periodic/after-unlock upkeep: fresh LL hook, or retry after a fallback/failure."""
        if self._mask_failures:
            log.warning("the menu mask key could not be sent %d time(s)", self._mask_failures)
            self._mask_failures = 0
        if not self._configured or not self._enabled:
            return
        if self._mode == "ll":
            if self._keys_held():
                self._schedule_retry()
                return
            fresh = self._install_hook()        # install first, then unhook: no gap
            if fresh is None:
                return
            stale, self._hook = self._hook, fresh
            if stale is not None:
                _UnhookWindowsHookEx(stale)
            log.debug("keyboard hook re-installed")
            return
        previous = self._mode
        error = self._activate()
        if self._mode != previous:
            if self._mode is not None:
                log.info("hotkey %s active again (mode %s)", self._label, self._mode)
                self._emit({"ev": "ready", "mode": self._mode, "seq": None})
            else:
                self._emit({"ev": "error", "msg": error, "seq": None})

    def _keys_held(self) -> bool:
        now = float(_GetTickCount64())
        self._capture.expire(now)
        if self._capture.active:
            return True
        machine = self._machine
        if machine is not None and machine.main_key_swallowed:
            if not machine.main_key_stale(now):
                return True
            # No auto-repeat for longer than the repeat window: the key is not held, its
            # key-up never reached us (a dropped hook). Without this, the re-install that
            # recovers a dropped hook would wait for that key-up forever (HK-2).
            log.info("forgetting a swallowed hotkey press whose key-up was lost")
            machine.forget_main_key()
        return any(_key_down_now(vk) for vk in range(0x08, 0xFF))   # skips mouse buttons 1–6

    def _end_capture(self, ok: bool) -> None:
        events = self._capture.finish(ok, float(_GetTickCount64()))
        if events:
            _send_keys(events)

    def _extend_capture(self, ms: Any) -> None:
        if isinstance(ms, bool) or not isinstance(ms, (int, float)) or not ms > 0:
            log.warning("ignoring extend_capture with ms=%r", ms)
            return
        self._capture.extend(min(float(ms), self._capture.max_extend_ms), float(_GetTickCount64()))

    # -- the hook --------------------------------------------------------------------------------
    def _on_hook(self, ncode: int, wparam: int, lparam: int) -> int:
        if ncode == HC_ACTION and not self._dry_run and self._machine is not None:
            try:
                if self._handle_key(wparam, lparam):
                    return 1
            except Exception:
                self._hook_errors += 1
                if self._hook_errors <= 5:
                    log.exception("keyboard hook handler failed")
        return _CallNextHookEx(None, ncode, wparam, lparam)

    def _handle_key(self, wparam: int, lparam: int) -> bool:
        """Returns True to swallow the event. Never logs key data."""
        kb = _KBDLLHOOKSTRUCT.from_address(lparam)
        machine = self._machine
        down = wparam in (WM_KEYDOWN, WM_SYSKEYDOWN)
        injected = bool(kb.flags & (LLKHF_INJECTED | LLKHF_LOWER_IL_INJECTED))
        vk = int(kb.vkCode)
        t_ms = _event_time_ms(kb.time)
        held_mods: frozenset[str] | None = None
        key_was_down: bool | None = None
        from_app: str | None = None
        passthrough = False
        capturing = False
        if down and not injected:
            if vk == self._vk:
                held_mods = _async_held_mods()
                key_was_down = _key_down_now(vk)
                # The keys typed after a fire are still being captured (e.g. Edge is
                # cold-launched): this press never fires (R2-APP-1), whatever is in front.
                self._capture.expire(t_ms)
                capturing = self._capture.active
                if not capturing:
                    from_app = _foreground_exe()
                    app = from_app.casefold() if from_app else None
                    # A passthrough app, or our own window for a typing chord (SPEC §15.10:
                    # typing in the search field – a Shift-rolled space must type a space, not
                    # hide the window; a quick double press still toggles). Any other hotkey
                    # (Ctrl, Alt, Win, F-key) types nothing there: one press hides (R2-HK-1).
                    passthrough = app is not None and (
                        app in self._passthrough
                        or (self._typing_chord and app == _OWN_WINDOW_EXE
                            and _own_window_in_foreground()))
            elif is_modifier_vk(vk):
                key_was_down = _modifier_held_now(vk)
        # The hotkey pressed again during its capture (not a repeat of a swallowed press).
        again = capturing and not machine.main_key_swallowed
        swallow, fire = machine.on_event(vk, down, injected, t_ms, held_mods=held_mods,
                                         key_was_down=key_was_down, passthrough=passthrough,
                                         capturing=capturing)
        if fire:
            if self._mask_on_fire and not _send_mask_key():
                self._mask_failures += 1
            self._capture.start(t_ms)       # never while one runs: the machine does not fire
            self._emit({"ev": "fire", "from_app": from_app})
            return True
        if swallow:
            if again and self._mask_on_fire:
                # Absorbed like a fire, it must not leave a lone Win/Alt release behind (HK-1).
                if not _send_mask_key():
                    self._mask_failures += 1
            return True
        if injected or is_modifier_vk(vk):
            return False
        event = KeyEvent(vk, int(kb.scanCode) & 0xFF, bool(kb.flags & LLKHF_EXTENDED), down)
        return self._capture.feed(event, t_ms)


def _non_negative_ms(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValueError(f"ugyldig tidsgrænse: {value!r}")
    return int(value)


def _register_session_notifications(hwnd: int) -> Callable[[], None] | None:
    """Ask for WM_WTSSESSION_CHANGE (unlock); returns the matching unregister function."""
    try:
        wtsapi32 = ctypes.WinDLL("wtsapi32", use_last_error=True)
        register = _declare(wtsapi32, "WTSRegisterSessionNotification", wintypes.BOOL,
                            HANDLE, wintypes.DWORD)
        unregister = _declare(wtsapi32, "WTSUnRegisterSessionNotification", wintypes.BOOL, HANDLE)
    except (OSError, AttributeError):
        log.warning("session notifications unavailable")
        return None
    if not register(hwnd, 0):       # NOTIFY_FOR_THIS_SESSION
        log.warning("WTSRegisterSessionNotification failed: %s",
                    ctypes.WinError(ctypes.get_last_error()))
        return None
    return lambda: unregister(hwnd)


def _read_commands(stream: IO[bytes], deliver: Callable[[dict], None]) -> None:
    try:
        for raw in stream:
            line = raw.strip()
            if not line:
                continue
            try:
                message = json.loads(line.decode("utf-8"))
            except ValueError:
                log.warning("ignoring a malformed command line")
                continue
            if isinstance(message, dict):
                deliver(message)
    except OSError as exc:
        log.info("reading commands failed: %s", exc)
    deliver({"cmd": "quit"})       # stdin EOF: the main process is gone


def _std_binary_streams() -> tuple[IO[bytes] | None, IO[bytes] | None]:
    """Binary stdin/stdout, also under pythonw where ``sys.stdin``/``sys.stdout`` may be None."""
    def open_std(stream: Any, std_handle: int, mode: str, flags: int) -> IO[bytes] | None:
        if stream is not None and hasattr(stream, "buffer"):
            return stream.buffer
        handle = _GetStdHandle(std_handle)
        if not handle or handle == INVALID_HANDLE_VALUE:
            return None
        try:
            return os.fdopen(msvcrt.open_osfhandle(handle, flags), mode)
        except OSError:
            return None

    return (open_std(sys.stdin, STD_INPUT_HANDLE, "rb", os.O_RDONLY),
            open_std(sys.stdout, STD_OUTPUT_HANDLE, "wb", os.O_WRONLY))


def _configure_child_logging(log_file: str | None) -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not log_file:
        root.addHandler(logging.NullHandler())
        return
    try:
        os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=256 * 1024, backupCount=1, encoding="utf-8", delay=True)
    except OSError:
        root.addHandler(logging.NullHandler())
        return
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s hotkey[%(process)d]: "
                                           "%(message)s"))
    root.addHandler(handler)


def child_main(argv: Sequence[str]) -> int:
    """Entry point of ``python -m projektsog.hotkey --child``."""
    parser = argparse.ArgumentParser(prog="projektsog.hotkey", add_help=False)
    parser.add_argument("--child", action="store_true", required=True)
    parser.add_argument("--dry-run", action="store_true",
                        help="install the hook but pass every event through and never fire")
    parser.add_argument("--log-file")
    args = parser.parse_args(list(argv))
    _configure_child_logging(args.log_file)
    stdin, stdout = _std_binary_streams()
    if stdin is None or stdout is None:
        log.error("stdin/stdout pipes are missing")
        return 2
    log.info("hotkey child started%s", " (dry run)" if args.dry_run else "")
    writer = _LineWriter(stdout)
    child = _HookChild(writer.put, dry_run=args.dry_run)
    writer.on_broken = lambda: child.post({"cmd": "quit"})
    writer.start()
    try:
        return child.run(stdin)
    except Exception:
        log.exception("hotkey child crashed")
        return 1
    finally:
        writer.close(timeout=1.0)


# --------------------------------------------------------------------------------------
# Main-process side
# --------------------------------------------------------------------------------------

def _pythonw_executable() -> str:
    candidate = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return candidate if os.path.isfile(candidate) else sys.executable


def _default_child_argv() -> list[str]:
    from . import config
    return [_pythonw_executable(), "-m", "projektsog.hotkey", "--child",
            "--log-file", os.path.join(config.log_dir(), "hotkey.log")]


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = repo_root + (os.pathsep + existing if existing else "")
    return env


class _Child:
    """One running hook child process (main-process view)."""

    def __init__(self, proc: subprocess.Popen) -> None:
        self.proc = proc
        self.reader: threading.Thread | None = None
        self._write_lock = threading.Lock()

    def alive(self) -> bool:
        return self.proc.poll() is None

    def send(self, message: dict) -> bool:
        data = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        with self._write_lock:
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
                return True
            except (OSError, ValueError):
                return False

    def release_pipes(self) -> None:
        with self._write_lock:
            for stream in (self.proc.stdin, self.proc.stdout):
                try:
                    stream.close()
                except OSError:
                    pass

    def close(self, timeout: float) -> None:
        """Ask the child to quit (and close its stdin → EOF); kill it if it does not exit."""
        self.send({"cmd": "quit"})
        with self._write_lock:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
        try:
            self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            log.warning("hotkey child did not exit in time – killing it")
            self.proc.kill()
            try:
                self.proc.wait(1.0)
            except subprocess.TimeoutExpired:
                log.error("hotkey child %s could not be killed", self.proc.pid)
        if self.reader is not None and self.reader is not threading.current_thread():
            self.reader.join(1.0)
        self.release_pipes()


class HotkeyManager:
    """Starts, configures and supervises the hook child (main process).

    ``callback(info)`` runs on a manager thread (never in the hook) with
    ``info = {"from_app": "Resolve.exe" | None, "fired_at": <time.monotonic() on arrival>}``;
    while one callback runs, at most one further fire is queued (extra presses in that time
    are dropped) – ``fired_at`` lets the callback tell such a press from a later one.
    ``on_status_change()`` (optional, an addition to SPEC) is called on the same thread
    whenever ``active``/``mode``/``last_error`` change – also on its own, when the child
    crashed, was restarted or recovered from the RegisterHotKey fallback.
    """

    READY_TIMEOUT_S = 3.0
    RESTART_LIMIT = 3            # restarts …
    RESTART_WINDOW_S = 60.0      # … per window
    RESTART_DELAY_S = 0.5
    STOP_TIMEOUT_S = 1.0
    _UPDATE_KEYS = frozenset({"spec", "passthrough_apps", "typing_guard_ms", "double_tap_ms",
                              "enabled"})

    def __init__(self, spec: str, callback: Callable[[dict], None], *,
                 passthrough_apps: Iterable[str] = (), typing_guard_ms: int = 300,
                 double_tap_ms: int = 400, enabled: bool = True,
                 child_argv: list[str] | None = None,
                 on_status_change: Callable[[], None] | None = None) -> None:
        self._spec = spec
        self._callback = callback
        self._passthrough = [str(a) for a in passthrough_apps]
        self._typing_guard_ms = int(typing_guard_ms)
        self._double_tap_ms = int(double_tap_ms)
        self._enabled = bool(enabled)
        self._child_argv = list(child_argv) if child_argv is not None else None
        self._on_status_change = on_status_change
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._child: _Child | None = None
        self._generation = 0
        self._seq = 0
        self._acked = 0
        self._mode: str | None = None
        self._last_error: str | None = None
        self._running = False
        self._restart_times: collections.deque[float] = collections.deque()
        self._restart_timer: threading.Timer | None = None
        self._events: queue.SimpleQueue = queue.SimpleQueue()
        self._fire_pending = False
        self._dispatcher: threading.Thread | None = None

    # -- public API ----------------------------------------------------------------------------
    def start(self) -> bool:
        """Start the child; True once it reports ready (≤ 3 s), or when disabled by config."""
        with self._lock:
            self._running = True
            self._ensure_dispatcher()
            if not self._enabled:
                return True
            if self._child is not None and self._child.alive():
                seq = self._seq
            else:
                self._cancel_restart()
                seq = self._spawn()
            if seq is None:
                return False
        return self._wait_for_ack(seq) and self.active

    def stop(self) -> None:
        """Stop the child (idempotent). A hook never outlives this call by more than ~2 s."""
        with self._cond:
            self._running = False
            child = self._detach_child()
            dispatcher, self._dispatcher = self._dispatcher, None
            events = self._events
        if child is not None:
            child.close(self.STOP_TIMEOUT_S)
        if dispatcher is not None:
            events.put(None)
            if dispatcher is not threading.current_thread():
                dispatcher.join(1.0)

    def update(self, **changes: Any) -> bool:
        """Apply ``spec``/``passthrough_apps``/``typing_guard_ms``/``double_tap_ms``/``enabled``.

        Invalid values raise ``ValueError`` (Danish) before anything changes. Returns True when
        the change is in effect (hotkey active, or intentionally disabled), False when the
        hotkey could not be (re)activated. ``enabled=True`` also (re)starts a stopped manager.
        """
        clean = self._validate_changes(changes)
        seq: int | None = None
        to_close: _Child | None = None
        with self._lock:
            was_enabled = self._enabled
            self._spec = clean.get("spec", self._spec)
            self._passthrough = clean.get("passthrough_apps", self._passthrough)
            self._typing_guard_ms = clean.get("typing_guard_ms", self._typing_guard_ms)
            self._double_tap_ms = clean.get("double_tap_ms", self._double_tap_ms)
            self._enabled = clean.get("enabled", self._enabled)
            if not self._running:
                action = "start" if self._enabled and clean.get("enabled") is True else "done"
            elif not self._enabled:
                to_close = self._detach_child() if was_enabled else None
                action = "done"
            else:
                if self._child is not None and self._child.alive():
                    seq = self._send_config(self._child)
                else:
                    self._cancel_restart()
                    seq = self._spawn()
                action = "failed" if seq is None else "wait"
        if to_close is not None:
            to_close.close(self.STOP_TIMEOUT_S)
        if action == "start":
            return self.start()
        if action == "wait":
            return self._wait_for_ack(seq) and self.active
        return action == "done"

    def end_capture(self, ok: bool) -> None:
        """Tell the child to replay (ok) or drop the keys typed since the last fire."""
        with self._lock:
            child = self._child
        if child is not None:
            child.send({"cmd": "end_capture", "ok": bool(ok)})

    def extend_capture(self, seconds: float) -> None:
        """Keep the keys typed since the last fire captured for ``seconds`` more (SPEC §15.10),
        e.g. while Edge is cold-launched; ``end_capture`` still ends it. The child caps one
        extension at 5 s from its arrival and a capture at 12 s after the fire; it never
        revives a capture that already expired – so a caller that needs longer re-sends it
        (the Controller does every 2 s while ``show()`` waits), and a hung main process loses
        the capture within 5 s. Invalid or non-positive values are ignored. Does not wait for
        an answer (like ``end_capture``)."""
        try:
            ms = int(round(float(seconds) * 1000))
        except (TypeError, ValueError, OverflowError):
            log.warning("extend_capture(%r) ignored", seconds)
            return
        if ms <= 0:
            return
        with self._lock:
            child = self._child
        if child is not None:
            child.send({"cmd": "extend_capture", "ms": ms})

    @property
    def active(self) -> bool:
        with self._lock:
            return self._mode is not None and self._child_running()

    @property
    def mode(self) -> str | None:
        """``"ll"`` | ``"registerhotkey"`` | None (not active)."""
        with self._lock:
            return self._mode if self._child_running() else None

    @property
    def last_error(self) -> str | None:
        """Danish description of why the hotkey is not active (None when fine)."""
        with self._lock:
            return self._last_error

    # -- internals (lock held where noted) ------------------------------------------------------
    def _validate_changes(self, changes: dict[str, Any]) -> dict[str, Any]:
        unknown = set(changes) - self._UPDATE_KEYS
        if unknown:
            raise TypeError(f"unknown hotkey setting(s): {', '.join(sorted(unknown))}")
        clean: dict[str, Any] = {}
        if "spec" in changes:
            parse_hotkey(changes["spec"])
            clean["spec"] = changes["spec"].strip()
        if "passthrough_apps" in changes:
            apps = changes["passthrough_apps"]
            if isinstance(apps, str) or not all(isinstance(a, str) for a in apps):
                raise ValueError("Listen over programmer skal være en liste af programnavne")
            clean["passthrough_apps"] = list(apps)
        for key in ("typing_guard_ms", "double_tap_ms"):
            if key in changes:
                try:
                    clean[key] = _non_negative_ms(changes[key])
                except ValueError:
                    raise ValueError(f"{key} skal være et ikke-negativt antal millisekunder") \
                        from None
        if "enabled" in changes:
            clean["enabled"] = bool(changes["enabled"])
        return clean

    def _child_running(self) -> bool:
        return self._enabled and self._child is not None and self._child.alive()

    def _ensure_dispatcher(self) -> None:          # lock held
        if self._dispatcher is not None and self._dispatcher.is_alive():
            return
        self._events = queue.SimpleQueue()
        self._fire_pending = False
        self._dispatcher = threading.Thread(target=self._dispatch_loop, args=(self._events,),
                                            name="HotkeyManager-dispatch", daemon=True)
        self._dispatcher.start()

    def _spawn(self) -> int | None:               # lock held; returns the config seq
        try:
            parse_hotkey(self._spec)
        except ValueError as exc:
            self._last_error = str(exc)
            return None
        from . import config
        argv = self._child_argv or _default_child_argv()
        try:
            proc = subprocess.Popen(
                argv, cwd=config.app_dir(), env=_child_env(),
                creationflags=subprocess.CREATE_NO_WINDOW, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, close_fds=True)
        except OSError as exc:
            log.error("could not start the hotkey child: %s", exc)
            self._last_error = "Hjælpeprocessen til genvejstasten kunne ikke startes"
            return None
        self._generation += 1
        child = _Child(proc)
        self._child = child
        self._mode = None
        child.reader = threading.Thread(target=self._read_child, args=(child,),
                                        name=f"HotkeyManager-reader-{self._generation}",
                                        daemon=True)
        child.reader.start()
        log.info("hotkey child started (pid %s)", proc.pid)
        return self._send_config(child)

    def _send_config(self, child: _Child) -> int:   # lock held
        self._seq += 1
        child.send({"cmd": "config", "spec": self._spec, "passthrough": list(self._passthrough),
                    "typing_guard_ms": self._typing_guard_ms,
                    "double_tap_ms": self._double_tap_ms, "enabled": self._enabled,
                    "seq": self._seq})
        return self._seq

    def _wait_for_ack(self, seq: int) -> bool:
        deadline = time.monotonic() + self.READY_TIMEOUT_S
        with self._cond:
            while self._acked < seq:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not self._running:
                    log.warning("hotkey child did not answer within %.1f s", self.READY_TIMEOUT_S)
                    return False
                self._cond.wait(remaining)
            return True

    def _detach_child(self) -> _Child | None:       # lock held; caller closes it after unlocking
        self._cancel_restart()
        child, self._child = self._child, None
        self._mode = None
        self._cond.notify_all()
        return child

    def _cancel_restart(self) -> None:             # lock held
        if self._restart_timer is not None:
            self._restart_timer.cancel()
            self._restart_timer = None

    def _read_child(self, child: _Child) -> None:
        try:
            for raw in child.proc.stdout:
                try:
                    message = json.loads(raw.decode("utf-8"))
                except ValueError:
                    log.warning("ignoring a malformed line from the hotkey child")
                    continue
                if isinstance(message, dict):
                    self._on_child_event(child, message)
        except (OSError, ValueError):
            pass
        self._on_child_exit(child)
        child.release_pipes()

    def _on_child_event(self, child: _Child, message: dict) -> None:
        event = message.get("ev")
        status_changed = False
        with self._cond:
            if child is not self._child:
                return
            if event == "fire":
                if not self._fire_pending:
                    self._fire_pending = True
                    from_app = message.get("from_app")
                    self._events.put(("fire", {"from_app": from_app if isinstance(from_app, str)
                                               and from_app else None,
                                               "fired_at": time.monotonic()}))
                return
            if event == "ready":
                mode = message.get("mode")
                mode = mode if mode in ("ll", "registerhotkey") else None
                status_changed = mode != self._mode or self._last_error is not None
                self._mode, self._last_error = mode, None
            elif event == "error":
                text = str(message.get("msg") or "Genvejstasten kunne ikke aktiveres")
                log.warning("hotkey child: %s", text)
                status_changed = self._mode is not None or text != self._last_error
                self._mode, self._last_error = None, text
            else:
                log.debug("unknown event from the hotkey child: %r", event)
                return
            seq = message.get("seq")
            if isinstance(seq, int) and seq > self._acked:
                self._acked = seq
            self._cond.notify_all()
            if status_changed:
                self._events.put(("status", None))

    def _on_child_exit(self, child: _Child) -> None:
        try:
            code = child.proc.wait(2.0)
        except subprocess.TimeoutExpired:
            code = None
        with self._cond:
            if child is not self._child:
                return                              # stopped or replaced on purpose
            self._child = None
            self._mode = None
            self._cond.notify_all()
            log.warning("hotkey child exited unexpectedly (exit code %s)", code)
            if self._running and self._enabled:
                self._schedule_restart()
        self._events.put(("status", None))

    def _schedule_restart(self) -> None:            # lock held
        now = time.monotonic()
        while self._restart_times and now - self._restart_times[0] >= self.RESTART_WINDOW_S:
            self._restart_times.popleft()
        if len(self._restart_times) >= self.RESTART_LIMIT:
            delay = self.RESTART_WINDOW_S - (now - self._restart_times[0])
            log.error("hotkey child keeps failing – next restart in %.0f s", delay)
        else:
            delay = self.RESTART_DELAY_S
        self._cancel_restart()
        timer = threading.Timer(delay, self._restart, args=(self._generation,))
        timer.name = "HotkeyManager-restart"
        timer.daemon = True
        self._restart_timer = timer
        timer.start()

    def _restart(self, generation: int) -> None:
        with self._lock:
            if (not self._running or not self._enabled or self._child is not None
                    or generation != self._generation):
                return
            self._restart_timer = None
            self._restart_times.append(time.monotonic())
            if self._spawn() is None:
                self._schedule_restart()

    def _dispatch_loop(self, events: queue.SimpleQueue) -> None:
        while (item := events.get()) is not None:
            kind, info = item
            if kind == "fire":
                with self._lock:
                    self._fire_pending = False
                try:
                    self._callback(info)
                except Exception:
                    log.exception("hotkey callback failed")
            elif self._on_status_change is not None:
                try:
                    self._on_status_change()
                except Exception:
                    log.exception("hotkey status callback failed")


if __name__ == "__main__":
    sys.exit(child_main(sys.argv[1:]))
