"""Messages from other programs on this PC – the Claude sessions' Resolve queue (SPEC §19, §21.1).

A program POSTs ``/api/messages`` (``{"tag", "titel", "tekst", "knapper": [{"tekst", "uri"}],
"session", "udloeber", "lyd", "visning", "prioritet"}``); Klippe shows it as a card with its
buttons on the second screen and Windows' notification sound plays (``"lyd": false``: silently).
One card at a time – what needs the user first, the others behind ‹ 1/3 ›. ``"prioritet":
"stille"`` (a session that is done and needs no answer) never opens a card by itself: it waits,
silently, behind a small "📬 2 beskeder" line. A message with the same tag replaces the earlier
one; ``DELETE /api/messages {"tag"}`` takes it away.
``"visning": "boble"`` is a passing note: Klippe says it in its speech bubble and nothing stays
(little clutter). The answer says whether it was shown (``vist``): only then may the sender skip
its own notification.

A **call** is a normal card with at least one button ("🎬 Mette vil bruge Resolve · Byg nu"):
Klippe holds a phone that rings until it is answered (``POST /api/messages/svar {"tag"}``),
clicked, removed or expired, or for ``RING_S`` at most (then it is a missed call and stays
unanswered). Its sound is Klippe's own short, quiet ring – "trrring-trrring … klap!", played once
instead of the notification (made here as a little WAV file); with ``widget_ring`` off it is the
notification sound. Every message carries ``opkald`` and ``besvaret`` (kept
in messages.json) and the snapshot adds ``ringer``; a call kept from before a restart never rings
again. The sound calls are injected (``sound()``, ``ring(on)``), so tests never make a sound.

A button opens its uri through Windows (the program's registered protocol handler), so
Projektsøg never runs anything itself, and only the schemes in ``URI_SCHEMES`` are accepted.
Projektsøg's own messages (``post(..., internal=True)``) may also use ``projektsog:``; such a
button is handed to ``on_internal(uri)`` instead of Windows. The messages are kept in
``messages.json``: a restart of Projektsøg does not lose a question a session is waiting on
(Projektsøg's own messages are not reloaded).
"""

from __future__ import annotations

import json
import logging
import math
import os
import random
import re
import struct
import threading
import time
import wave
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

URI_SCHEMES = frozenset({"resolvekoe"})     # the Claude sessions' Resolve queue (koe.py)
INTERNAL_SCHEME = "projektsog"              # Projektsøg's own messages only (post(internal=True))
MAX_BUTTONS = 3
MAX_MESSAGES = 20
TAG_MAX, TITLE_MAX, TEXT_MAX, BUTTON_MAX, SESSION_MAX, URI_MAX = 80, 120, 400, 40, 40, 2000
DEFAULT_TTL_S = 3600
MAX_TTL_S = 86400
RING_S = 30.0                               # a call rings this long at most, then it is missed

_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*):")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

MSG_GONE = "Beskeden er der ikke længere"


def _text(data: dict[str, Any], key: str, limit: int, *, required: bool = False) -> str:
    value = data.get(key, "")
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError(f"{key} skal være tekst")
    value = " ".join(value.split()) if key != "tekst" else value.strip()
    if required and not value:
        raise ValueError(f"{key} mangler")
    if len(value) > limit:
        raise ValueError(f"{key} er for lang (højst {limit} tegn)")
    return value


def check_uri(uri: Any, *, internal: bool = False) -> str:
    """A button's uri: one of URI_SCHEMES (or ``projektsog:`` when internal), no spaces or
    control characters."""
    if not isinstance(uri, str) or not uri or len(uri) > URI_MAX:
        raise ValueError("Ugyldig uri")
    match = _SCHEME.match(uri)
    scheme = match.group(1).lower() if match is not None else ""
    if scheme not in URI_SCHEMES and not (internal and scheme == INTERNAL_SCHEME):
        raise ValueError("Knappen må kun åbne " + ", ".join(f"{s}:" for s in sorted(URI_SCHEMES)))
    if _CONTROL.search(uri) or " " in uri:
        raise ValueError("Ugyldig uri")
    return uri


def _is_internal(uri: str) -> bool:
    match = _SCHEME.match(uri)
    return match is not None and match.group(1).lower() == INTERNAL_SCHEME


def clean_message(data: Any, *, internal: bool = False) -> dict[str, Any]:
    """A posted message, checked and trimmed (raises ValueError with a Danish message)."""
    if not isinstance(data, dict):
        raise ValueError("Forventede et JSON-objekt")
    buttons = data.get("knapper") or []
    if not isinstance(buttons, list) or len(buttons) > MAX_BUTTONS:
        raise ValueError(f"knapper skal være en liste med højst {MAX_BUTTONS}")
    clean_buttons = []
    for button in buttons:
        if not isinstance(button, dict):
            raise ValueError("En knap skal have tekst og uri")
        clean_buttons.append({"tekst": _text(button, "tekst", BUTTON_MAX, required=True),
                              "uri": check_uri(button.get("uri"), internal=internal)})
    ttl = data.get("udloeber", DEFAULT_TTL_S)
    if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or not 1 <= ttl <= MAX_TTL_S:
        raise ValueError(f"udloeber skal være 1–{MAX_TTL_S} sekunder")
    message = {"tag": _text(data, "tag", TAG_MAX, required=True),
               "titel": _text(data, "titel", TITLE_MAX), "tekst": _text(data, "tekst", TEXT_MAX),
               "knapper": clean_buttons, "session": _text(data, "session", SESSION_MAX)}
    if not message["titel"] and not message["tekst"]:
        raise ValueError("titel eller tekst mangler")
    sound, view = data.get("lyd", True), data.get("visning", "kort")
    priority = data.get("prioritet", "normal")
    if not isinstance(sound, bool):
        raise ValueError("lyd skal være sand/falsk")
    if view not in ("kort", "boble"):
        raise ValueError("visning skal være kort eller boble")
    if priority not in ("normal", "stille"):
        raise ValueError("prioritet skal være normal eller stille")
    message.update(ttl=float(ttl), lyd=sound and priority == "normal", visning=view, prioritet=priority)
    return message


def is_call(message: dict[str, Any]) -> bool:
    """A card that wants the user now (a normal one with a button) – it rings (SPEC §21.1)."""
    return message["prioritet"] == "normal" and bool(message["knapper"])


def shell_open(uri: str) -> None:
    """Hand the uri to Windows: its registered protocol handler runs (never a shell)."""
    os.startfile(uri)                         # noqa: S606 - checked scheme, ShellExecute "open"


def notification_sound() -> None:
    try:
        import winsound
        winsound.PlaySound("SystemNotification",
                           winsound.SND_ALIAS | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
    except (ImportError, RuntimeError, OSError):
        log.debug("could not play the notification sound", exc_info=True)


RINGTONE_FILE = "klippe-ring-1.wav"     # a new drawing of the sound → a new name
RINGTONE_RATE = 22050
RINGTONE_PEAK = 0.2                     # of full scale: friendly, not loud


def ringtone_samples(rate: int = RINGTONE_RATE) -> list[float]:
    """Klippe's ring: two short trills of a little bell, then the clapper's "klap" (−1…1)."""
    out: list[float] = []

    def trill(seconds: float) -> None:
        n = int(rate * seconds)
        for i in range(n):
            t = i / rate
            bell = 0.6 * math.sin(2 * math.pi * 1180 * t) + 0.4 * math.sin(2 * math.pi * 1570 * t)
            hammer = 0.55 + 0.45 * math.sin(2 * math.pi * 24 * t)          # the bell's trrr
            envelope = min(1.0, t / 0.008, (seconds - t) / 0.04)
            out.append(bell * hammer * envelope)

    def pause(seconds: float) -> None:
        out.extend([0.0] * int(rate * seconds))

    def clap(seconds: float = 0.09) -> None:
        noise = random.Random(7)
        for i in range(int(rate * seconds)):
            t = i / rate
            out.append((noise.uniform(-1, 1) * 0.9 + math.sin(2 * math.pi * 640 * t) * 0.5)
                       * math.exp(-t / 0.014))

    trill(0.28)
    pause(0.09)
    trill(0.28)
    pause(0.07)
    clap()
    peak = max(abs(v) for v in out) or 1.0
    return [v / peak * RINGTONE_PEAK for v in out]


def write_ringtone(path: str) -> None:
    """The ring as a 16-bit mono WAV (written beside, then moved into place)."""
    temp = path + ".tmp"
    with wave.open(temp, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(RINGTONE_RATE)
        wav.writeframes(b"".join(struct.pack("<h", round(v * 32767)) for v in ringtone_samples()))
    os.replace(temp, path)


def ringtone_path() -> str | None:
    """Klippe's ring in Projektsøg's data folder, made the first time it is needed."""
    from . import config
    path = os.path.join(config.app_dir(), RINGTONE_FILE)
    if not os.path.isfile(path):
        try:
            write_ringtone(path)
        except OSError as exc:
            log.warning("could not make Klippe's ring: %s", exc)
            return None
    return path


def ring_sound(on: bool) -> None:
    """A call starts ringing: Klippe's short ring, once (the notification sound if it cannot be
    made). It is over long before the call stops ringing, so there is nothing to stop."""
    if not on:
        return
    path = ringtone_path()
    if path is None:
        notification_sound()
        return
    try:
        import winsound
        winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
    except (ImportError, RuntimeError, OSError):
        log.debug("could not play Klippe's ring", exc_info=True)


def ring_timer(delay: float, action: Callable[[], None]) -> threading.Timer:
    """The default timer that ends a call's ringing (not started yet)."""
    timer = threading.Timer(delay, action)
    timer.name = "MessageBoard-ring"
    timer.daemon = True
    return timer


class MessageBoard:
    """The messages Klippe shows (newest first); ``shown()`` says whether Klippe is on screen."""

    def __init__(self, cfg: Any, bus: Any, *, shown: Callable[[], bool] = lambda: False,
                 open_uri: Callable[[str], None] = shell_open,
                 sound: Callable[[], None] = notification_sound,
                 ring: Callable[[bool], None] = ring_sound,
                 on_internal: Callable[[str], None] | None = None,
                 clock: Callable[[], float] = time.time,
                 timer: Callable[[float, Callable[[], None]], Any] = ring_timer,
                 path: str | None = None) -> None:
        self.cfg = cfg
        self.bus = bus
        self._shown = shown
        self._open_uri = open_uri
        self._sound = sound
        self._ring = ring
        self._on_internal = on_internal
        self._clock = clock
        self._timer = timer
        self._path = path
        self._lock = threading.Lock()
        self._messages: dict[str, dict[str, Any]] = {}
        self._ringing: dict[str, tuple[int, Any]] = {}     # tag -> (generation, its RING_S timer)
        self._ring_generation = 0
        self._ring_lock = threading.Lock()                  # orders ring() calls; taken before _lock
        self._ringtone = False                              # the phone rings (any call)
        self._ring_told = False                             # … and ring() was told so
        self._closed = False
        self._load()
        if cfg is not None and callable(getattr(cfg, "on_change", None)):
            cfg.on_change(self._on_config)

    # -- API ---------------------------------------------------------------------------------
    def post(self, data: Any, *, internal: bool = False) -> dict[str, Any]:
        message = clean_message(data, internal=internal)
        now = self._clock()
        ttl, sound, view = message.pop("ttl"), message.pop("lyd"), message.pop("visning")
        message.update(tid=now, udloeber_ved=now + ttl, lyd=sound)
        shown = self._is_shown()
        if view == "boble":                    # a passing note: said, not kept
            with self._lock:
                old = self._messages.pop(message["tag"], None)
                self._stop_ringing(message["tag"])
                self._save()
                if old is not None:
                    self._publish()
            if old is not None:
                self._sync_ring()
            line = " – ".join(part for part in (message["titel"], message["tekst"]) if part)
            self.bus.publish("say", {"tekst": line})
            log.info("note %s: %s", message["tag"], line[:80])
            return {"ok": True, "vist": shown}
        call, ring_on = is_call(message), self._ring_enabled()
        tag = message["tag"]
        with self._lock:
            self._prune(now)
            old = self._messages.pop(tag, None)
            news = old is None or (old["titel"], old["tekst"]) != (message["titel"], message["tekst"])
            # A call that is news (or was no call before) rings anew; a silent re-post keeps the
            # answer of the one it replaces.
            new_call = call and sound and (news or not old.get("opkald"))
            answered = not call or (not new_call and (old is None or bool(old.get("besvaret", True))))
            message.update(opkald=call, besvaret=answered)
            self._messages[tag] = message
            if new_call and shown and not self._closed:     # the phone rings in Klippe, sound or not
                self._start_ringing(tag, min(RING_S, ttl))
            elif answered or new_call:
                self._stop_ringing(tag)
            # (else a silent re-post of an unanswered call: it rings on, or stays missed)
            while len(self._messages) > MAX_MESSAGES:
                self._stop_ringing(self._pop_oldest())
            self._save()
            self._publish()
        rang = self._sync_ring() and new_call      # this call's sound is the ring
        if shown and sound and (news or new_call) and not rang:
            self._sound()
        log.info("%s %s%s: %s", "call" if call else "message", tag,
                 "" if shown else " (Klippe is not shown)", message["titel"] or message["tekst"][:60])
        return {"ok": True, "vist": shown}

    def answer(self, tag: Any) -> dict[str, Any]:
        """The phone is picked up in Klippe: the call becomes a plain card and stops ringing."""
        with self._lock:
            pruned = self._prune(self._clock())
            message = self._messages.get(tag) if isinstance(tag, str) else None
            changed = message is not None and (not message["besvaret"] or tag in self._ringing)
            if message is not None:
                message["besvaret"] = True
                self._stop_ringing(tag)
            if changed or pruned:
                self._save()
                self._publish()
        if changed or pruned:
            self._sync_ring()
        if message is None:
            raise ValueError(MSG_GONE)
        if changed:
            log.info("call %s answered", tag)
        return {"ok": True}

    def remove(self, tag: Any) -> dict[str, Any]:
        if not isinstance(tag, str) or not tag.strip():
            raise ValueError("tag mangler")
        tag = " ".join(tag.split())
        with self._lock:
            gone = self._messages.pop(tag, None)
            self._stop_ringing(tag)
            if gone is not None:
                self._save()
                self._publish()
        if gone is not None:
            self._sync_ring()
        return {"ok": True}

    def list(self) -> dict[str, Any]:
        with self._lock:
            pruned = self._prune(self._clock())
            if pruned:
                self._save()
                self._publish()
            result = {"messages": self._snapshot()}
        if pruned:
            self._sync_ring()
        return result

    def click(self, tag: Any, index: Any) -> dict[str, Any]:
        """A button in Klippe: open its uri, and the message has been answered."""
        if isinstance(index, bool) or not isinstance(index, int):
            raise ValueError("Ugyldig værdi: knap")
        with self._lock:
            message = self._messages.get(tag) if isinstance(tag, str) else None
            if message is None or not 0 <= index < len(message["knapper"]):
                raise ValueError(MSG_GONE)
            uri = message["knapper"][index]["uri"]
            del self._messages[tag]
            self._stop_ringing(tag)
            self._save()
            self._publish()
        self._sync_ring()
        if _is_internal(uri):                  # only Projektsøg's own messages carry these
            if self._on_internal is None:
                raise ValueError("Knappen kunne ikke åbnes")
            self._on_internal(check_uri(uri, internal=True))
        else:
            try:
                self._open_uri(check_uri(uri))
            except OSError as exc:
                log.warning("could not open %s: %s", uri.split("?", 1)[0], exc)
                raise ValueError("Knappen kunne ikke åbnes – kører køen?") from exc
        log.info("message %s answered: %s", tag, message["knapper"][index]["tekst"])
        return {"ok": True}

    def close(self) -> None:
        """Stop the ringtone and its timers (at exit); later calls no longer ring."""
        with self._lock:
            self._closed = True
            for tag in list(self._ringing):
                self._stop_ringing(tag)
        self._sync_ring()

    # -- ringing (SPEC §21.1) ----------------------------------------------------------------
    def _ring_enabled(self) -> bool:
        if self.cfg is None:
            return True
        try:
            return bool(self.cfg.get("widget_ring", True))
        except Exception:
            log.debug("could not read widget_ring", exc_info=True)
            return True

    def _start_ringing(self, tag: str, delay: float) -> None:
        """(Re)start the call's ringing with its own timer (lock held)."""
        self._stop_ringing(tag)
        self._ring_generation += 1
        generation = self._ring_generation
        timer = self._timer(max(0.0, delay), lambda: self._ring_over(tag, generation))
        self._ringing[tag] = (generation, timer)
        timer.start()

    def _stop_ringing(self, tag: str) -> None:
        """The call no longer rings (lock held)."""
        entry = self._ringing.pop(tag, None)
        if entry is not None:
            entry[1].cancel()

    def _ring_over(self, tag: str, generation: int) -> None:
        """RING_S passed (or the call expired): it stays unanswered – a missed call."""
        with self._lock:
            entry = self._ringing.get(tag)
            if entry is None or entry[0] != generation:
                return                           # answered, removed or rung anew meanwhile
            del self._ringing[tag]
            if self._prune(self._clock()):
                self._save()
            self._publish()
        self._sync_ring()
        log.info("call %s was not answered", tag)

    def _on_config(self, settings: Any) -> None:
        """Klippe was switched off: a ringing call goes quiet (missed) – off this listener's
        thread, which must not wait for the sound."""
        get = settings.get if isinstance(settings, dict) else self.cfg.get
        if self._ringing and not get("widget_enabled", True):
            self._timer(0.0, self._quiet).start()

    def _quiet(self) -> None:
        with self._lock:
            if not self._ringing:
                return
            for tag in list(self._ringing):
                self._stop_ringing(tag)
            self._publish()
        self._sync_ring()
        log.info("the phone stopped ringing: Klippe was switched off")

    def _publish(self) -> None:
        """The board as it is now (lock held, so the snapshots reach the widget in order)."""
        self.bus.publish("messages", {"messages": self._snapshot()})

    def _sync_ring(self) -> bool:
        """Tell ``ring`` when the phone starts or stops ringing (never under _lock); True when it
        has just started – the ring is played then (unless ``widget_ring`` is off)."""
        with self._ring_lock:
            with self._lock:
                want = bool(self._ringing)
            if want == self._ringtone:
                return False
            self._ringtone = want
            if want and not self._ring_enabled():
                self._ring_told = False            # rings without the ring: nothing to stop later
                return False
            if not want and not self._ring_told:
                return False
            self._ring_told = want
            try:
                self._ring(want)
            except Exception:
                log.debug("could not %s the ringtone", "start" if want else "stop", exc_info=True)
            return want

    # -- keeping them across restarts -----------------------------------------------------
    def _load(self) -> None:
        if not self._path:
            return
        try:
            with open(self._path, encoding="utf-8") as fh:
                saved = json.load(fh)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.warning("could not read %s: %s", self._path, exc)
            return
        now = self._clock()
        for item in saved.get("messages", []) if isinstance(saved, dict) else []:
            try:
                message = clean_message({**item, "visning": "kort", "lyd": bool(item.get("lyd", True))})
                until = float(item["udloeber_ved"])
            except (ValueError, TypeError, KeyError):
                continue
            if until <= now:
                continue
            del message["ttl"], message["visning"]
            call = is_call(message)                # a kept call never rings again (ringer false)
            message.update(tid=float(item.get("tid") or now), udloeber_ved=until, opkald=call,
                           besvaret=not call or item.get("besvaret", True) is not False)
            self._messages[message["tag"]] = message
        if self._messages:
            log.info("%d message(s) kept from before the restart", len(self._messages))

    def _save(self) -> None:
        """Write the messages (lock held); a failure only costs them at the next restart."""
        if not self._path:
            return
        temp = self._path + ".tmp"
        try:
            with open(temp, "w", encoding="utf-8") as fh:
                json.dump({"messages": list(self._messages.values())}, fh, ensure_ascii=False)
            os.replace(temp, self._path)
        except OSError as exc:
            log.warning("could not save the messages: %s", exc)

    # -- helpers -----------------------------------------------------------------------------
    def _is_shown(self) -> bool:
        try:
            return bool(self._shown())
        except Exception:
            log.debug("could not tell whether Klippe is shown", exc_info=True)
            return False

    def _prune(self, now: float) -> bool:
        """Drop expired messages (lock held); True if any went."""
        expired = [tag for tag, m in self._messages.items() if m["udloeber_ved"] <= now]
        for tag in expired:
            del self._messages[tag]
            self._stop_ringing(tag)
        return bool(expired)

    def _pop_oldest(self) -> str:
        """Drop the oldest message (lock held); its tag."""
        tag = next(iter(self._messages))
        del self._messages[tag]
        return tag

    def _snapshot(self) -> list[dict[str, Any]]:
        return [dict(m, knapper=[dict(b) for b in m["knapper"]], ringer=m["tag"] in self._ringing)
                for m in reversed(list(self._messages.values()))]
