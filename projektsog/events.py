"""Tiny thread-safe publish/subscribe bus used to push live updates to the UI (SSE).

Event types (full table with publishers/consumers in SPEC.md §3.1):

    status         Indexer.status()                                   Indexer, ≤ 2/s
    sources        {"changed": [source_id, ...]}                       Indexer
    index_updated  {"source_id": int}                                  Indexer (after committed changes)
    scan_progress  {"source_id", "name", "entries", "dirs", "units_done", "units_total", "started"}
    new_volume     {"disk_name", "drive", "source_ids", "included", "reason"}   Indexer
    resolve        ResolveBridge.state()                               ResolveBridge (on any change)
    focus          {"from_app": str | None, "reason": str}             app (controller.show_window)
    notify         {"title": str, "text": str, "level": "info"|"warn"|"error"}  forwarded to tray by app
    settings       Config.snapshot() + {"run_at_login": bool}          app (config listener)
    hotkey         {"spec", "label", "enabled", "active", "mode"}      app
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Any


class EventBus:
    """Fan-out bus. Each subscriber gets its own bounded queue of (type, data, ts) tuples.

    Slow subscribers never block publishers: when a subscriber queue is full the oldest
    item is dropped.
    """

    def __init__(self, maxsize: int = 500) -> None:
        self._lock = threading.Lock()
        self._subs: list[queue.Queue] = []
        self._maxsize = maxsize

    def publish(self, type: str, data: Any = None) -> None:
        item = (type, data if data is not None else {}, time.time())
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            while True:
                try:
                    q.put_nowait(item)
                    break
                except queue.Full:
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        pass

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=self._maxsize)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            try:
                self._subs.remove(q)
            except ValueError:
                pass

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)
