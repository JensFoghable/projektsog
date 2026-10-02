"""Messages from other programs on this PC – the Claude sessions' Resolve queue (SPEC §19).

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

A button opens its uri through Windows (the program's registered protocol handler), so
Projektsøg never runs anything itself, and only the schemes in ``URI_SCHEMES`` are accepted.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

URI_SCHEMES = frozenset({"resolvekoe"})     # the Claude sessions' Resolve queue (koe.py)
MAX_BUTTONS = 3
MAX_MESSAGES = 20
TAG_MAX, TITLE_MAX, TEXT_MAX, BUTTON_MAX, SESSION_MAX, URI_MAX = 80, 120, 400, 40, 40, 2000
DEFAULT_TTL_S = 3600
MAX_TTL_S = 86400

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


def check_uri(uri: Any) -> str:
    """A button's uri: one of URI_SCHEMES, no spaces or control characters."""
    if not isinstance(uri, str) or not uri or len(uri) > URI_MAX:
        raise ValueError("Ugyldig uri")
    match = _SCHEME.match(uri)
    if match is None or match.group(1).lower() not in URI_SCHEMES:
        raise ValueError("Knappen må kun åbne " + ", ".join(f"{s}:" for s in sorted(URI_SCHEMES)))
    if _CONTROL.search(uri) or " " in uri:
        raise ValueError("Ugyldig uri")
    return uri


def clean_message(data: Any) -> dict[str, Any]:
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
                              "uri": check_uri(button.get("uri"))})
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


class MessageBoard:
    """The messages Klippe shows (newest first); ``shown()`` says whether Klippe is on screen."""

    def __init__(self, cfg: Any, bus: Any, *, shown: Callable[[], bool] = lambda: False,
                 open_uri: Callable[[str], None] = shell_open,
                 sound: Callable[[], None] = notification_sound,
                 clock: Callable[[], float] = time.time) -> None:
        self.cfg = cfg
        self.bus = bus
        self._shown = shown
        self._open_uri = open_uri
        self._sound = sound
        self._clock = clock
        self._lock = threading.Lock()
        self._messages: dict[str, dict[str, Any]] = {}

    # -- API ---------------------------------------------------------------------------------
    def post(self, data: Any) -> dict[str, Any]:
        message = clean_message(data)
        now = self._clock()
        ttl, sound, view = message.pop("ttl"), message.pop("lyd"), message.pop("visning")
        message.update(tid=now, udloeber_ved=now + ttl, lyd=sound)
        shown = self._is_shown()
        if view == "boble":                    # a passing note: said, not kept
            with self._lock:
                old = self._messages.pop(message["tag"], None)
                snapshot = self._snapshot()
            if old is not None:
                self.bus.publish("messages", {"messages": snapshot})
            line = " – ".join(part for part in (message["titel"], message["tekst"]) if part)
            self.bus.publish("say", {"tekst": line})
            log.info("note %s: %s", message["tag"], line[:80])
            return {"ok": True, "vist": shown}
        with self._lock:
            self._prune(now)
            old = self._messages.pop(message["tag"], None)
            self._messages[message["tag"]] = message
            while len(self._messages) > MAX_MESSAGES:
                self._messages.pop(next(iter(self._messages)))
            snapshot = self._snapshot()
        news = old is None or (old["titel"], old["tekst"]) != (message["titel"], message["tekst"])
        self.bus.publish("messages", {"messages": snapshot})
        if shown and news and sound:
            self._sound()
        log.info("message %s%s: %s", message["tag"], "" if shown else " (Klippe is not shown)",
                 message["titel"] or message["tekst"][:60])
        return {"ok": True, "vist": shown}

    def remove(self, tag: Any) -> dict[str, Any]:
        if not isinstance(tag, str) or not tag.strip():
            raise ValueError("tag mangler")
        with self._lock:
            gone = self._messages.pop(" ".join(tag.split()), None)
            snapshot = self._snapshot()
        if gone is not None:
            self.bus.publish("messages", {"messages": snapshot})
        return {"ok": True}

    def list(self) -> dict[str, Any]:
        with self._lock:
            if self._prune(self._clock()):
                self.bus.publish("messages", {"messages": self._snapshot()})
            return {"messages": self._snapshot()}

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
            snapshot = self._snapshot()
        self.bus.publish("messages", {"messages": snapshot})
        try:
            self._open_uri(check_uri(uri))
        except OSError as exc:
            log.warning("could not open %s: %s", uri.split("?", 1)[0], exc)
            raise ValueError("Knappen kunne ikke åbnes – kører køen?") from exc
        log.info("message %s answered: %s", tag, message["knapper"][index]["tekst"])
        return {"ok": True}

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
        return bool(expired)

    def _snapshot(self) -> list[dict[str, Any]]:
        return [dict(m, knapper=[dict(b) for b in m["knapper"]])
                for m in reversed(list(self._messages.values()))]
