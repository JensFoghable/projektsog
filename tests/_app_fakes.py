"""Fakes and helpers for the app agent's tests (tests/test_app_*.py)."""

from __future__ import annotations

import http.client
import importlib
import importlib.util
import json
import sys
import threading
import time
import types
from typing import Any, Callable

# Modules owned by other agents that projektsog.app imports at module level.
_COLLABORATOR_MODULES = ("hotkey", "indexer", "resolve_bridge", "tray", "window", "winfs",
                         "winui")
# Never evicted after an import with stand-ins: ours, and the given shared base (which cannot
# depend on a stand-in; a second copy would duplicate classes such as Config).
_KEEP_IMPORTED = frozenset({"projektsog.app", "projektsog.server", "projektsog.config",
                            "projektsog.events", "projektsog.textutil"})


class _Unavailable:
    """Placeholder for a name of a module that is not written yet (never called in tests)."""

    def __init__(self, qualname: str) -> None:
        self._qualname = qualname

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(f"{self._qualname} is not available in unit tests")

    def __repr__(self) -> str:
        return f"<unavailable {self._qualname}>"


def _stand_in(name: str) -> types.ModuleType:
    module = types.ModuleType(name)

    def __getattr__(attr: str) -> Any:          # PEP 562
        if attr.startswith("__"):
            raise AttributeError(attr)
        return _Unavailable(f"{name}.{attr}")

    module.__getattr__ = __getattr__  # type: ignore[attr-defined]
    return module


def _missing_module(exc: ImportError) -> str | None:
    if isinstance(exc, ModuleNotFoundError):
        return exc.name
    name_from = getattr(exc, "name_from", None)             # "cannot import name X from P"
    return f"{exc.name}.{name_from}" if exc.name and name_from else None


def import_app() -> types.ModuleType:
    """Import projektsog.app even while other agents' modules do not exist yet.

    Missing ``projektsog.*`` modules are replaced by in-memory stand-ins for the duration of
    the import only; afterwards every other-agent module imported on the way is removed from
    sys.modules again, so other test modules always import the real ones.
    When all modules exist, this is a plain import.
    """
    if "projektsog.app" in sys.modules:
        return sys.modules["projektsog.app"]
    before = set(sys.modules)
    stand_ins: dict[str, types.ModuleType] = {}
    for short in _COLLABORATOR_MODULES:
        name = f"projektsog.{short}"
        if name not in sys.modules and importlib.util.find_spec(name) is None:
            stand_ins[name] = sys.modules[name] = _stand_in(name)
    try:
        while True:
            try:
                module = importlib.import_module("projektsog.app")
                break
            except ImportError as exc:
                name = _missing_module(exc)
                if not name or not name.startswith("projektsog.") or name in stand_ins:
                    raise
                stand_ins[name] = sys.modules[name] = _stand_in(name)
    finally:
        if stand_ins:
            for name in set(sys.modules) - before:
                if name.startswith("projektsog.") and name not in _KEEP_IMPORTED:
                    removed = sys.modules.pop(name)
                    parent, _, attr = name.rpartition(".")
                    if getattr(sys.modules.get(parent), attr, None) is removed:
                        delattr(sys.modules[parent], attr)
    return module


# ------------------------------------------------------------------------------------------
# Fakes (signatures follow SPEC.md)
# ------------------------------------------------------------------------------------------

class Fake:
    """Records calls; per-method return values (or callables) and exceptions are settable."""

    def __init__(self, journal: list[str] | None = None, name: str = "") -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self.returns: dict[str, Any] = {}
        self.raises: dict[str, BaseException] = {}
        self.delays: dict[str, float] = {}
        self._journal = journal
        self._name = name or type(self).__name__

    def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((method, args, kwargs))
        if self._journal is not None:
            self._journal.append(f"{self._name}.{method}")
        if method in self.delays:
            time.sleep(self.delays[method])
        if method in self.raises:
            raise self.raises[method]
        value = self.returns.get(method)
        return value(*args, **kwargs) if callable(value) else value

    def called(self, method: str) -> list[tuple[tuple, dict]]:
        return [(args, kwargs) for name, args, kwargs in self.calls if name == method]


class FakeIndexer(Fake):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.returns.update({
            "status": {"hostname": "TESTPC", "version": "1.0.0", "sources_total": 2},
            "list_sources": [{"id": 1, "display_name": "Kunder"}],
            "hosts": [{"name": "TESTPC", "online": True, "shares": 1, "last_seen": None,
                       "self": True}],
            "search": lambda q, **kw: {"query": q, "results": []},
            "recent_projects": lambda limit=30: [{"id": 7, "name": "Rikke Lindholm"}],
            "children": lambda source_id, rel_path: [{"id": 8, "rel_path": rel_path}],
            "set_source_mode": lambda source_id, mode: {"id": source_id, "mode": mode},
            "add_root": lambda path: {"ok": True, "source": None},
            "remove_host": lambda name: {"ok": True, "forgotten": 0},     # SPEC §15.12
        })

    def start(self) -> None: return self._call("start")
    def stop(self, timeout: float = 5) -> None: return self._call("stop", timeout=timeout)
    def status(self) -> dict: return self._call("status")
    def list_sources(self) -> list: return self._call("list_sources")
    def hosts(self) -> list: return self._call("hosts")

    def search(self, q: str, kind: str = "all", online_only: bool | None = None,
               source_id: int | None = None, limit: int | None = None,
               include_templates: bool = False) -> dict:
        return self._call("search", q, kind=kind, online_only=online_only, source_id=source_id,
                          limit=limit, include_templates=include_templates)

    def recent_projects(self, limit: int = 30) -> list:
        return self._call("recent_projects", limit=limit)

    def children(self, source_id: int, rel_path: str) -> list:
        return self._call("children", source_id, rel_path)

    def locate(self, path: str) -> dict | None: return self._call("locate", path)
    def scan_now(self, source_id: int | None = None, full: bool = False) -> None:
        return self._call("scan_now", source_id, full=full)
    def on_window_shown(self) -> None: return self._call("on_window_shown")
    def set_source_mode(self, source_id: int, mode: str) -> dict:
        return self._call("set_source_mode", source_id, mode)
    def forget_source(self, source_id: int) -> None: return self._call("forget_source", source_id)
    def add_root(self, path: str) -> dict: return self._call("add_root", path)
    def remove_root(self, path: str) -> None: return self._call("remove_root", path)
    def add_host(self, name: str) -> None: return self._call("add_host", name)
    def remove_host(self, name: str) -> dict: return self._call("remove_host", name)
    def path_missing(self, path: str) -> None: return self._call("path_missing", path)


class FakeBridge(Fake):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.returns.update({
            "state": {"enabled": True, "running": False, "connected": False, "project": None},
            "refresh": lambda wait=True: {"enabled": True, "refreshed": True},
            "open_primary": {"ok": True, "path": "C:\\Kunder\\Rikke Lindholm", "error": None},
        })

    def start(self) -> None: return self._call("start")
    def stop(self) -> None: return self._call("stop")
    def state(self) -> dict: return self._call("state")
    def refresh(self, wait: bool = True) -> dict: return self._call("refresh", wait=wait)
    def on_window_shown(self) -> None: return self._call("on_window_shown")

    def open_primary(self, *args: Any, **kwargs: Any) -> dict:
        # SPEC §15.9: open_primary(project=None, database=None); records exactly what was passed.
        return self._call("open_primary", *args, **kwargs)


class FakeController(Fake):
    """Stands in for app.Controller in the server tests."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.returns.update({
            "show_window": True,
            "hotkey_status": {"spec": "shift+space", "label": "Shift+Mellemrum",
                              "enabled": True, "active": True, "mode": "ll"},
            "get_run_at_login": False,
            "open_path": lambda path, action: {"ok": True, "path": path},
        })
        self.exiting = threading.Event()        # as app.Controller.exiting

    def show_window(self, from_app: str | None = None, reason: str = "api", *,
                    panel: str | None = None) -> bool:
        return self._call("show_window", from_app, reason, panel=panel)

    def hide_window(self, restore_previous: bool = False) -> None:
        return self._call("hide_window", restore_previous=restore_previous)

    def open_path(self, path: str, action: str) -> dict: return self._call("open_path", path, action)
    def hotkey_status(self) -> dict: return self._call("hotkey_status")
    def get_run_at_login(self) -> bool: return self._call("get_run_at_login")
    def set_run_at_login(self, enabled: bool) -> None: return self._call("set_run_at_login", enabled)
    def request_exit(self) -> None: return self._call("request_exit")


class FakeWindow(Fake):
    def __init__(self, *args: Any, visible: bool = False, foreground: bool = False,
                 **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.returns.update({"show": True, "is_visible": visible, "is_foreground": foreground,
                             "needs_launch": False})

    def preload(self) -> None: return self._call("preload")
    def needs_launch(self) -> bool: return self._call("needs_launch")     # SPEC §15.10
    def show(self) -> bool: return self._call("show")
    def hide(self, restore_previous: bool = False) -> None:
        return self._call("hide", restore_previous=restore_previous)
    def is_visible(self) -> bool: return self._call("is_visible")
    def is_foreground(self) -> bool: return self._call("is_foreground")
    def close(self) -> None: return self._call("close")


class FakeHotkeys(Fake):
    def __init__(self, *args: Any, active: bool = True, mode: str | None = "ll",
                 **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.returns.update({"start": True, "update": True})
        self.active = active
        self.mode = mode

    def start(self) -> bool: return self._call("start")
    def stop(self) -> None: return self._call("stop")
    def update(self, **changes: Any) -> bool: return self._call("update", **changes)
    def end_capture(self, ok: bool) -> None: return self._call("end_capture", ok)
    def extend_capture(self, seconds: float) -> None:              # SPEC §15.10
        return self._call("extend_capture", seconds)


class FakeServer(Fake):
    """Stands in for server.Server in the App lifecycle tests."""

    def __init__(self, *args: Any, port: int = 4711, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.returns["start"] = port

    def start(self, port: int | None = None) -> int: return self._call("start", port)
    def stop(self) -> None: return self._call("stop")


class FakeTray(Fake):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.returns["start"] = True
        self.notified = threading.Event()

    def start(self) -> bool: return self._call("start")
    def stop(self) -> None: return self._call("stop")

    def notify(self, title: str, text: str, level: str = "info") -> None:
        self._call("notify", title, text, level)
        self.notified.set()


# ------------------------------------------------------------------------------------------
# HTTP helpers
# ------------------------------------------------------------------------------------------

class Response:
    def __init__(self, status: int, headers: http.client.HTTPMessage, body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.body = body

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


def request(port: int, method: str, path: str, *, body: Any = None, raw_body: bytes | None = None,
            host: str | None = None, headers: dict[str, str] | None = None,
            token: bool = True) -> Response:
    """One HTTP request with full control over Host and X-Projektsog (token=False omits it)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        if host != "":
            conn.putheader("Host", host if host is not None else f"127.0.0.1:{port}")
        if token and method != "GET":
            conn.putheader("X-Projektsog", "1")
        data = raw_body if raw_body is not None else (
            json.dumps(body).encode("utf-8") if body is not None else b"")
        if data or method != "GET":
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Content-Length", str(len(data)))
        for name, value in (headers or {}).items():
            conn.putheader(name, value)
        conn.endheaders(data or None)
        response = conn.getresponse()
        return Response(response.status, response.headers, response.read())
    finally:
        conn.close()


def wait_until(predicate: Callable[[], bool], timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()
