"""HTTP API, live event stream (SSE) and static UI files for the app window (SPEC §11).

The server binds 127.0.0.1 only and trusts nothing it receives:

* the ``Host`` header must name this server exactly (defeats DNS rebinding);
* every non-GET request must carry ``X-Projektsog: 1`` – a foreign web page cannot add that
  header without a CORS preflight, and no CORS header is ever sent;
* JSON bodies are limited to 1 MB; the UI may only load resources from this server (CSP).

Collaborators (Indexer, ResolveBridge, Controller) are injected. This module imports no module
of another agent at import time; ``hotkey.parse_hotkey`` is injected or imported lazily.
"""

from __future__ import annotations

import json
import logging
import math
import os
import queue
import re
import selectors
import socket
import socketserver
import sys
import threading
from dataclasses import dataclass
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import parse_qs, quote, unquote

from . import __version__
from .config import Config, validate as validate_settings
from .events import EventBus

if TYPE_CHECKING:
    from .app import Controller
    from .indexer import Indexer
    from .resolve_bridge import ResolveBridge
    from .importer import Importer
    from .achievements import PetProgress
    from .updater import Updater
    from .messages import MessageBoard
    from .petplay import PetPlay
    from .timetrack import TimeTracker

log = logging.getLogger(__name__)

MAX_TIME_RANGE_DAYS = 400

HOST = "127.0.0.1"
PORT_FALLBACKS = 20                 # when the port is taken, try port+1 … port+20
MAX_BODY_BYTES = 1024 * 1024
MAX_QUERY_FIELDS = 50
SSE_HEARTBEAT_S = 15.0
SEARCH_KINDS = frozenset({"all", "project", "dir", "file"})
INTERNAL_ERROR = "Intern fejl – se loggen"
SHUTTING_DOWN = "Projektsøg lukker ned"

# WSAEADDRINUSE, plus WSAEACCES which Windows reports for ports inside a reserved range
# (Hyper-V/WinNAT "excluded port ranges").
_PORT_UNAVAILABLE = frozenset({10048, 10013})

_STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}
# The UI must never load anything from outside this server (SPEC §12).
_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; "
    "object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
)
# Characters never allowed in a decoded URL path segment (separators, drive/ADS colon, wildcards).
_UNSAFE_SEGMENT = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

_STOP = object()  # sentinel that ends an event stream


class _HttpError(Exception):
    def __init__(self, status: int, message: str, headers: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.headers = headers or {}


@dataclass(frozen=True)
class _Request:
    args: tuple[str, ...]           # groups captured by the route pattern
    query: dict[str, str]           # last value of each query parameter
    body: dict[str, Any]            # parsed JSON object ({} when there is no body)


@dataclass(frozen=True)
class _Route:
    method: str
    pattern: re.Pattern[str]
    handler: Callable[[_Request], Any] | None     # None = the SSE stream (/api/events)


@dataclass(frozen=True)
class _FileResponse:
    """A handler result sent as a download instead of JSON (the time report export)."""
    data: bytes
    content_type: str
    filename: str


class Server:
    """HTTP API + SSE + static UI on 127.0.0.1 (see module docstring)."""

    def __init__(self, cfg: Config, bus: EventBus, indexer: "Indexer", bridge: "ResolveBridge",
                 controller: "Controller", *, web_dir: str | None = None,
                 assets_dir: str | None = None,
                 parse_hotkey: Callable[[str], object] | None = None,
                 sse_heartbeat_s: float = SSE_HEARTBEAT_S,
                 tracker: "TimeTracker | None" = None,
                 importer: "Importer | None" = None) -> None:
        package_dir = os.path.dirname(os.path.abspath(__file__))
        self.cfg = cfg
        self.bus = bus
        self.indexer = indexer
        self.bridge = bridge
        self.controller = controller
        self.tracker = tracker
        self.importer = importer
        self.petplay: "PetPlay | None" = None     # set by the app once the widget exists
        self.messages: "MessageBoard | None" = None
        self.progress: "PetProgress | None" = None
        self.updater: "Updater | None" = None
        self.web_dir = web_dir or os.path.join(package_dir, "web")
        self.assets_dir = assets_dir or os.path.join(package_dir, "assets")
        self.sse_heartbeat_s = sse_heartbeat_s
        self.port: int | None = None
        self._parse_hotkey = parse_hotkey
        self._httpd: _HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._streams_lock = threading.Lock()
        self._streams: set[queue.Queue] = set()
        self._routes = self._build_routes()

    # -- lifecycle --------------------------------------------------------------------------
    def start(self, port: int | None = None) -> int:
        """Bind (first free of port … port+20; 0 = any free port), serve on a thread."""
        if self._httpd is not None:
            raise RuntimeError("server already started")
        first = int(self.cfg["port"]) if port is None else port
        httpd = self._bind(first)
        self._httpd = httpd
        self.port = httpd.server_port
        self._stopping.clear()
        self._thread = threading.Thread(target=httpd.serve, name="http-server", daemon=True)
        self._thread.start()
        log.info("HTTP server listening on http://%s:%d/", HOST, self.port)
        return self.port

    def stop(self) -> None:
        """Stop accepting, end event streams, drop open connections. Idempotent."""
        httpd, self._httpd = self._httpd, None
        if httpd is None:
            return
        self._stopping.set()
        with self._streams_lock:
            streams = list(self._streams)
        for q in streams:
            _offer(q, _STOP)
        httpd.request_stop()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        httpd.server_close()
        httpd.close_connections()       # unblocks keep-alive and streaming handler threads
        log.info("HTTP server stopped")

    def _bind(self, first: int) -> "_HTTPServer":
        last = first if first == 0 else min(first + PORT_FALLBACKS, 65535)
        port = first
        while True:
            try:
                return _HTTPServer((HOST, port), self)
            except OSError as exc:
                if port >= last or getattr(exc, "winerror", None) not in _PORT_UNAVAILABLE:
                    raise
                log.info("Port %d is not available (%s) – trying %d", port, exc, port + 1)
                port += 1

    # -- used by the request handler --------------------------------------------------------
    @property
    def stopping(self) -> bool:
        return self._stopping.is_set()

    def match(self, method: str, path: str) -> tuple[_Route, tuple[str, ...]]:
        allowed: set[str] = set()
        for route in self._routes:
            m = route.pattern.fullmatch(path)
            if m is None:
                continue
            if route.method == method:
                return route, m.groups()
            allowed.add(route.method)
        if allowed:
            raise _HttpError(405, "Metoden er ikke tilladt", {"Allow": ", ".join(sorted(allowed))})
        raise _HttpError(404, "Ikke fundet")

    def static_file(self, url_path: str) -> str | None:
        """Map a URL path to a file under web_dir/assets_dir, or None (never escapes them)."""
        if url_path == "/":
            base, rel = self.web_dir, "index.html"
        elif url_path == "/favicon.ico":
            base, rel = self.assets_dir, "icon.ico"
        elif url_path.startswith("/assets/"):
            base, rel = self.assets_dir, url_path[len("/assets/"):]
        else:
            base, rel = self.web_dir, url_path[1:]
        parts = [unquote(part) for part in rel.split("/")]
        if any(part in ("", ".", "..") or _UNSAFE_SEGMENT.search(part) for part in parts):
            return None
        if os.path.splitext(parts[-1])[1].lower() not in _STATIC_TYPES:
            return None
        root = os.path.realpath(base)
        candidate = os.path.realpath(os.path.join(root, *parts))
        try:
            inside = os.path.normcase(os.path.commonpath([root, candidate])) == os.path.normcase(root)
        except ValueError:          # different drives, or a device path such as \\.\con
            return None
        return candidate if inside and os.path.isfile(candidate) else None

    def open_stream(self, q: queue.Queue) -> bool:
        with self._streams_lock:
            if self._stopping.is_set():
                return False
            self._streams.add(q)
            return True

    def close_stream(self, q: queue.Queue) -> None:
        with self._streams_lock:
            self._streams.discard(q)
        self.bus.unsubscribe(q)

    # -- routes -----------------------------------------------------------------------------
    def _build_routes(self) -> list[_Route]:
        table: list[tuple[str, str, Callable[[_Request], Any] | None]] = [
            ("GET", r"/api/search", self._search),
            ("GET", r"/api/recent", self._recent),
            ("GET", r"/api/children", self._children),
            ("GET", r"/api/status", self._status),
            ("GET", r"/api/sources", self._sources),
            ("POST", r"/api/sources/([0-9]{1,18})/mode", self._source_mode),
            ("POST", r"/api/sources/([0-9]{1,18})/scan", self._source_scan),
            ("POST", r"/api/sources/([0-9]{1,18})/forget", self._source_forget),
            ("POST", r"/api/scan", self._scan_all),
            ("POST", r"/api/roots", self._add_root),
            ("DELETE", r"/api/roots", self._remove_root),
            ("POST", r"/api/hosts", self._add_host),
            ("DELETE", r"/api/hosts", self._remove_host),
            ("POST", r"/api/open", self._open),
            ("GET", r"/api/resolve", self._resolve_state),
            ("POST", r"/api/resolve/refresh", self._resolve_refresh),
            ("POST", r"/api/resolve/open", self._resolve_open),
            ("GET", r"/api/settings", self._get_settings),
            ("POST", r"/api/settings", self._post_settings),
            ("POST", r"/api/window/hide", self._window_hide),
            ("POST", r"/api/window/show", self._window_show),
            ("POST", r"/api/quit", self._quit),
            ("GET", r"/api/time", self._time_report),
            ("GET", r"/api/time/status", self._time_status),
            ("GET", r"/api/time/export", self._time_export),
            ("GET", r"/api/import", self._import_state),
            ("GET", r"/api/import/options", self._import_options),
            ("GET", r"/api/import/plan", self._import_plan),
            ("POST", r"/api/import/project", self._import_project),
            ("POST", r"/api/import/start", self._import_start),
            ("POST", r"/api/import/cancel", self._import_cancel),
            ("POST", r"/api/import/dismiss", self._import_dismiss),
            ("GET", r"/api/widget/play", self._pet_status),
            ("POST", r"/api/widget/play", self._pet_play),
            ("POST", r"/api/widget/look", self._pet_look),
            ("GET", r"/api/pet", self._pet_progress),
            ("POST", r"/api/pet/equip", self._pet_equip),
            ("GET", r"/api/pet/mad", self._pet_food),
            ("POST", r"/api/pet/mad", self._pet_feed),
            ("GET", r"/api/update", self._update_state),
            ("POST", r"/api/update/check", self._update_check),
            ("POST", r"/api/update/install", self._update_install),
            ("GET", r"/api/messages", self._messages_list),
            ("POST", r"/api/messages", self._messages_post),
            ("DELETE", r"/api/messages", self._messages_remove),
            ("POST", r"/api/messages/click", self._messages_click),
            ("GET", r"/api/events", None),
        ]
        return [_Route(method, re.compile(pattern), handler) for method, pattern, handler in table]

    def _search(self, req: _Request) -> Any:
        q = req.query
        kind = q.get("kind") or "all"
        if kind not in SEARCH_KINDS:
            raise ValueError("Ugyldig værdi: kind")
        return self.indexer.search(
            q.get("q", ""), kind=kind, online_only=_query_bool(q, "online"),
            source_id=_query_int(q, "source"), limit=_query_int(q, "limit", minimum=1),
            include_templates=bool(_query_bool(q, "templates")))

    def _recent(self, req: _Request) -> Any:
        limit = _query_int(req.query, "limit", minimum=1)
        return {"results": self.indexer.recent_projects(limit=30 if limit is None else limit)}

    def _children(self, req: _Request) -> Any:
        source_id = _query_int(req.query, "source")
        if source_id is None:
            raise ValueError("Ugyldig værdi: source")
        return {"results": self.indexer.children(source_id, req.query.get("rel", ""))}

    def _status(self, req: _Request) -> Any:
        status = dict(self.indexer.status())
        status["resolve"] = self.bridge.state()
        status["hotkey"] = self.controller.hotkey_status()
        return status

    def _sources(self, req: _Request) -> Any:
        return {"sources": self.indexer.list_sources(), "hosts": self.indexer.hosts()}

    def _source_mode(self, req: _Request) -> Any:
        return self.indexer.set_source_mode(int(req.args[0]), _body_str(req.body, "mode"))

    def _source_scan(self, req: _Request) -> Any:
        self.indexer.scan_now(int(req.args[0]), full=_body_bool(req.body, "full", False))
        return {"ok": True}

    def _source_forget(self, req: _Request) -> Any:
        self.indexer.forget_source(int(req.args[0]))
        return {"ok": True}

    def _scan_all(self, req: _Request) -> Any:
        self.indexer.scan_now(None, full=_body_bool(req.body, "full", False))
        return {"ok": True}

    def _add_root(self, req: _Request) -> Any:
        return self.indexer.add_root(_body_str(req.body, "path"))

    def _remove_root(self, req: _Request) -> Any:
        self.indexer.remove_root(_body_str(req.body, "path"))
        return {"ok": True}

    def _add_host(self, req: _Request) -> Any:
        self.indexer.add_host(_body_str(req.body, "name"))
        return {"ok": True}

    def _remove_host(self, req: _Request) -> Any:
        # SPEC §15.12: {"ok": true, "forgotten": n} – how many shares were really forgotten
        # (the UI words its toast from it). A folder the user added on that computer makes the
        # Indexer refuse with a Danish ValueError (400) and change nothing.
        return self.indexer.remove_host(_body_str(req.body, "name"))

    def _open(self, req: _Request) -> Any:
        action = req.body.get("action", "folder")
        return self.controller.open_path(_body_str(req.body, "path"), action)

    def _resolve_state(self, req: _Request) -> Any:
        return self.bridge.state()

    def _resolve_refresh(self, req: _Request) -> Any:
        return self.bridge.refresh()

    def _resolve_open(self, req: _Request) -> Any:
        # The menu script names the Resolve project (database and unique id) it runs in, so
        # the bridge can refuse a mapping that belongs to another project (SPEC §15.9). A
        # request without them keeps the original call.
        names = {name: value for name in ("project", "database", "uid")
                 if (value := _body_optional_str(req.body, name)) is not None}
        return self.bridge.open_primary(**names)

    # -- time tracking ---------------------------------------------------------------------
    def _time_tracker(self) -> "TimeTracker":
        if self.tracker is None:
            raise ValueError("Tidsregistrering er ikke tilgængelig")
        return self.tracker

    def _time_report(self, req: _Request) -> Any:
        tracker = self._time_tracker()
        first, last = time_range(req.query)
        return {"report": tracker.report(first, last), "status": tracker.status()}

    def _time_status(self, req: _Request) -> Any:
        return self._time_tracker().status()

    def _time_export(self, req: _Request) -> Any:
        return time_export(self._time_tracker(), req.query)

    # -- import helper (SPEC §17) -------------------------------------------------------------
    def _import_helper(self) -> "Importer":
        if self.importer is None:
            raise ValueError("Import er ikke tilgængelig")
        return self.importer

    def _import_state(self, req: _Request) -> Any:
        helper = self._import_helper()
        return {"cards": helper.cards(), "job": helper.job(), "history": helper.history(8)}

    def _import_options(self, req: _Request) -> Any:
        return self._import_helper().options(_query_str(req.query, "card"))

    def _import_plan(self, req: _Request) -> Any:
        return self._import_helper().plan(_query_str(req.query, "card"), _query_str(req.query, "project"),
                                          separate=bool(_query_bool(req.query, "separate")))

    def _import_project(self, req: _Request) -> Any:
        return self._import_helper().create_project(_body_str(req.body, "root"), _body_str(req.body, "name"))

    def _import_start(self, req: _Request) -> Any:
        mode = req.body.get("mode", "copy")
        if mode not in ("copy", "prepare", "move"):
            raise ValueError("Ugyldig værdi: mode")
        return self._import_helper().start_import(
            _body_str(req.body, "card"), _body_str(req.body, "project"),
            separate=_body_bool(req.body, "separate", False), mode=mode)

    def _import_cancel(self, req: _Request) -> Any:
        self._import_helper().cancel()
        return {"ok": True}

    def _import_dismiss(self, req: _Request) -> Any:
        self._import_helper().dismiss(_body_str(req.body, "card"))
        return {"ok": True}

    # -- Klippe plays (SPEC §18.4) ----------------------------------------------------------
    def _pet(self) -> "PetPlay":
        if self.petplay is None:
            raise ValueError("Klippe er ikke startet")
        return self.petplay

    def _pet_status(self, req: _Request) -> Any:
        return self._pet().status()

    def _pet_play(self, req: _Request) -> Any:
        return self._pet().play_now()

    def _pet_look(self, req: _Request) -> Any:
        return self._pet().set_look(req.body)

    # -- Klippe's trophies and wardrobe (SPEC §18.5) -----------------------------------------
    def _wardrobe(self) -> "PetProgress":
        if self.progress is None:
            raise ValueError("Klippes trofæer er ikke startet")
        return self.progress

    def _pet_progress(self, req: _Request) -> Any:
        return self._wardrobe().state()

    def _pet_equip(self, req: _Request) -> Any:
        return self._wardrobe().equip(req.body.get("slot"), req.body.get("item"))

    def _pet_food(self, req: _Request) -> Any:
        return self._wardrobe().food()

    def _pet_feed(self, req: _Request) -> Any:
        return self._wardrobe().feed(req.body.get("item"))

    # -- new versions from GitHub (SPEC §20) --------------------------------------------------
    def _updates(self) -> "Updater":
        if self.updater is None:
            raise ValueError("Opdateringer er ikke startet")
        return self.updater

    def _update_state(self, req: _Request) -> Any:
        return self._updates().state()

    def _update_check(self, req: _Request) -> Any:
        return self._updates().request_check()

    def _update_install(self, req: _Request) -> Any:
        return self._updates().request_update()

    # -- messages from other programs (SPEC §19) ---------------------------------------------
    def _board(self) -> "MessageBoard":
        if self.messages is None:
            raise ValueError("Beskeder er ikke tilgængelige")
        return self.messages

    def _messages_list(self, req: _Request) -> Any:
        return self._board().list()

    def _messages_post(self, req: _Request) -> Any:
        return self._board().post(req.body)

    def _messages_remove(self, req: _Request) -> Any:
        return self._board().remove(req.body.get("tag"))

    def _messages_click(self, req: _Request) -> Any:
        return self._board().click(req.body.get("tag"), req.body.get("knap"))

    def _get_settings(self, req: _Request) -> Any:
        return self._settings_payload()

    def _post_settings(self, req: _Request) -> Any:
        changes = dict(req.body)
        run_at_login = changes.pop("run_at_login", None)
        if run_at_login is not None and not isinstance(run_at_login, bool):
            raise ValueError("run_at_login skal være sand/falsk")
        if "hotkey" in changes:
            if not isinstance(changes["hotkey"], str):
                raise ValueError("hotkey skal være tekst")
            self._hotkey_parser()(changes["hotkey"])
        validate_settings(changes)      # all or nothing: validate before changing anything
        if run_at_login is not None:
            self.controller.set_run_at_login(run_at_login)
        if changes:
            self.cfg.update(changes)
        return self._settings_payload()

    def _window_hide(self, req: _Request) -> Any:
        restore = _body_bool(req.body, "restore_previous", False)
        self.controller.hide_window(restore_previous=restore)
        return {"ok": True}

    def _window_show(self, req: _Request) -> Any:
        # While the app exits, a second launch must not get a false "ok": 503 makes it wait
        # for the mutex and start a new instance instead (APP-2).
        exiting = getattr(self.controller, "exiting", None)
        if exiting is not None and exiting.is_set():
            raise _HttpError(503, SHUTTING_DOWN)
        self.controller.show_window(reason="api")
        return {"ok": True}

    def _quit(self, req: _Request) -> Any:
        self.controller.request_exit()
        return {"ok": True}

    def _settings_payload(self) -> dict[str, Any]:
        settings = self.cfg.snapshot()
        settings["run_at_login"] = bool(self.controller.get_run_at_login())
        return {"settings": settings}

    def _hotkey_parser(self) -> Callable[[str], object]:
        if self._parse_hotkey is None:
            from .hotkey import parse_hotkey
            self._parse_hotkey = parse_hotkey
        return self._parse_hotkey


class _HTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    request_queue_size = 64

    def __init__(self, address: tuple[str, int], api: Server) -> None:
        self.api = api
        self._connections_lock = threading.Lock()
        self._connections: set[socket.socket] = set()
        self._stop_requested = False
        self._wake_reader, self._wake_writer = socket.socketpair()
        try:
            super().__init__(address, _Handler)
        except BaseException:
            self._wake_reader.close()
            self._wake_writer.close()
            raise

    def serve(self) -> None:
        """serve_forever() without its 0.5 s polling: select() sleeps until a client connects
        or request_stop() writes to the wake-up socket – no idle wake-ups, instant stop."""
        with selectors.DefaultSelector() as selector:
            selector.register(self.socket, selectors.EVENT_READ)
            selector.register(self._wake_reader, selectors.EVENT_READ)
            while not self._stop_requested:
                for key, _events in selector.select():
                    if key.fileobj is self.socket and not self._stop_requested:
                        self._handle_request_noblock()     # what serve_forever() calls

    def request_stop(self) -> None:
        self._stop_requested = True
        try:
            self._wake_writer.send(b"\0")
        except OSError:
            pass

    def server_close(self) -> None:
        super().server_close()
        self._wake_reader.close()
        self._wake_writer.close()

    def server_bind(self) -> None:
        # Exclusive: no other socket may bind this port while we hold it (Windows semantics).
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        socketserver.TCPServer.server_bind(self)    # skips HTTPServer's reverse DNS lookup
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        with self._connections_lock:
            self._connections.add(request)
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._connections_lock:
                self._connections.discard(request)

    def close_connections(self) -> None:
        with self._connections_lock:
            connections = list(self._connections)
        for conn in connections:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def handle_error(self, request: Any, client_address: Any) -> None:
        # The stock implementation prints to sys.stderr (None under pythonw.exe).
        if isinstance(sys.exception(), OSError):
            log.debug("Connection %s dropped: %r", client_address, sys.exception())
        else:
            log.exception("Error while serving %s", client_address)


class _Handler(BaseHTTPRequestHandler):
    server: _HTTPServer
    protocol_version = "HTTP/1.1"
    timeout = 60
    disable_nagle_algorithm = True      # small JSON replies must not wait for delayed ACKs

    def version_string(self) -> str:
        return f"Projektsog/{__version__}"

    def log_message(self, format: str, *args: Any) -> None:
        # The stock implementation writes to sys.stderr, which is None under pythonw.exe.
        # Control characters are escaped as it does, so a request cannot forge log lines.
        if log.isEnabledFor(logging.DEBUG):
            message = (format % args).translate(self._control_char_table)
            log.debug("%s %s", self.client_address[0], message)

    def do_GET(self) -> None:
        self._dispatch()

    do_POST = do_DELETE = do_PUT = do_PATCH = do_HEAD = do_OPTIONS = do_GET

    # -- dispatch ---------------------------------------------------------------------------
    def _dispatch(self) -> None:
        self._body_unread = self._announces_body()
        try:
            self._check_access()
            path, _, query = self.path.partition("?")
            if path.startswith("/api/"):
                self._handle_api(path, query)
            else:
                self._handle_static(path)
        except _HttpError as err:
            self._send_json(err.status, {"error": err.message}, err.headers)

    def _check_access(self) -> None:
        host = (self.headers.get("Host") or "").strip().lower()
        port = self.server.server_port
        if host not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            raise _HttpError(403, "Adgang nægtet")
        if self.command != "GET" and (self.headers.get("X-Projektsog") or "").strip() != "1":
            raise _HttpError(403, "Adgang nægtet")
        if not self.path.startswith("/"):
            raise _HttpError(400, "Ugyldig adresse")    # absolute-form targets: never a proxy

    def _handle_api(self, path: str, query: str) -> None:
        api = self.server.api
        route, args = api.match(self.command, path)
        if route.handler is None:
            self._stream_events(api)
            return
        try:
            params = {key: values[-1] for key, values in parse_qs(
                query, keep_blank_values=True, max_num_fields=MAX_QUERY_FIELDS).items()}
        except ValueError:
            raise _HttpError(400, "Ugyldig forespørgsel") from None
        body = self._read_json_body() if self.command != "GET" else {}
        try:
            result = route.handler(_Request(args, params, body))
        except _HttpError:
            raise                       # a deliberate status (e.g. 503 while shutting down)
        except ValueError as exc:
            self._send_json(400, {"error": str(exc) or "Ugyldig forespørgsel"})
        except Exception:
            log.exception("%s %s failed", self.command, path)
            self._send_json(500, {"error": INTERNAL_ERROR})
        else:
            if isinstance(result, _FileResponse):
                self._send_file(result)
            else:
                self._send_json(200, result)

    def _handle_static(self, path: str) -> None:
        file_path = self.server.api.static_file(path)
        if file_path is None:
            raise _HttpError(404, "Ikke fundet")
        if self.command != "GET":
            raise _HttpError(405, "Metoden er ikke tilladt", {"Allow": "GET"})
        try:
            with open(file_path, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            log.warning("Cannot read %s: %s", file_path, exc)
            raise _HttpError(404, "Ikke fundet") from None
        ext = os.path.splitext(file_path)[1].lower()
        self.send_response(200)
        self._send_standard_headers(_STATIC_TYPES[ext], len(data))
        if ext == ".html":
            self.send_header("Content-Security-Policy", _CONTENT_SECURITY_POLICY)
        self.end_headers()
        self.wfile.write(data)

    def _stream_events(self, api: Server) -> None:
        q = api.bus.subscribe()
        try:
            if not api.open_stream(q):
                raise _HttpError(503, "Serveren lukker ned")
            self.connection.settimeout(None)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b"retry: 2000\n\n")
            while not api.stopping:
                try:
                    item = q.get(timeout=api.sse_heartbeat_s)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    continue
                if item is _STOP:
                    break
                frame = _sse_frame(item)
                if frame:
                    self.wfile.write(frame)
        except OSError as exc:
            log.debug("Event stream to %s ended: %r", self.client_address, exc)
        finally:
            api.close_stream(q)
            self.close_connection = True

    # -- request body -----------------------------------------------------------------------
    def _announces_body(self) -> bool:
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            return True
        try:
            return int(self.headers.get("Content-Length") or 0) != 0
        except ValueError:
            return True

    def _read_json_body(self) -> dict[str, Any]:
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            raise _HttpError(411, "Content-Length mangler")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise _HttpError(400, "Ugyldig Content-Length") from None
        if length < 0:
            raise _HttpError(400, "Ugyldig Content-Length")
        if length > MAX_BODY_BYTES:
            raise _HttpError(413, "Forespørgslen er for stor")
        data = self.rfile.read(length) if length else b""
        if len(data) != length:
            raise _HttpError(400, "Ufuldstændig forespørgsel")
        self._body_unread = False
        if not data.strip():
            return {}
        try:
            payload = json.loads(data.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            raise _HttpError(400, "Ugyldig JSON") from None
        if not isinstance(payload, dict):
            raise _HttpError(400, "Forventede et JSON-objekt")
        return payload

    # -- responses --------------------------------------------------------------------------
    def _send_json(self, status: int, payload: Any, headers: dict[str, str] | None = None) -> None:
        try:
            body = _json_text(payload).encode("utf-8")
        except (TypeError, ValueError, RecursionError):
            log.exception("Response to %s %s is not JSON serialisable", self.command, self.path)
            status, body = 500, _json_text({"error": INTERNAL_ERROR}).encode("utf-8")
        self.send_response(status)
        self._send_standard_headers("application/json; charset=utf-8", len(body))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_file(self, result: _FileResponse) -> None:
        self.send_response(200)
        self._send_standard_headers(result.content_type, len(result.data))
        self.send_header("Content-Disposition", content_disposition(result.filename))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(result.data)

    def _send_standard_headers(self, content_type: str, length: int) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if self._body_unread:
            # Unread request bytes would be parsed as the next request on this connection.
            self.send_header("Connection", "close")


# -- helpers ----------------------------------------------------------------------------------

def _json_text(obj: Any) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except ValueError:
        # NaN/Infinity would make the whole response unparsable for JSON.parse(): send null.
        return json.dumps(_finite(obj), ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _finite(obj: Any) -> Any:
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {key: _finite(value) for key, value in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_finite(value) for value in obj]
    return obj


def _sse_frame(item: tuple[str, Any, float]) -> bytes:
    event_type, data, _ts = item
    try:
        payload = _json_text(data)
    except (TypeError, ValueError, RecursionError):
        log.warning("Event %r is not JSON serialisable – not sent", event_type)
        return b""
    return f"event: {event_type}\ndata: {payload}\n\n".encode("utf-8")


def _offer(q: queue.Queue, item: object) -> None:
    """put_nowait that drops the oldest entry of a full queue (as EventBus.publish does)."""
    while True:
        try:
            q.put_nowait(item)
            return
        except queue.Full:
            try:
                q.get_nowait()
            except queue.Empty:
                pass


def _query_int(query: dict[str, str], name: str, *, minimum: int | None = None) -> int | None:
    raw = query.get(name, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"Ugyldig værdi: {name}") from None
    if minimum is not None and value < minimum:
        raise ValueError(f"Ugyldig værdi: {name}")
    return value


def _query_str(query: dict[str, str], name: str) -> str:
    value = query.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} mangler")
    return value


def _query_date(query: dict[str, str], name: str, default: date) -> date:
    raw = query.get(name, "").strip()
    if not raw:
        return default
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise ValueError(f"Ugyldig dato: {name} (brug ÅÅÅÅ-MM-DD)") from None


def time_range(query: dict[str, str]) -> tuple[date, date]:
    """``from``/``to`` of a time report (local dates, inclusive; default: today)."""
    first = _query_date(query, "from", date.today())
    last = _query_date(query, "to", first)
    if last < first:
        raise ValueError("Slutdatoen ligger før startdatoen")
    if (last - first).days > MAX_TIME_RANGE_DAYS:
        raise ValueError(f"Vælg højst {MAX_TIME_RANGE_DAYS} dage ad gangen")
    return first, last


def time_export(tracker: "TimeTracker", query: dict[str, str]) -> _FileResponse:
    """The CSV download of /api/time/export (``round`` minutes; ``detail=day`` / ``timeline``
    for a row per project and day / per project and timeline)."""
    first, last = time_range(query)
    round_minutes = _query_int(query, "round", minimum=0) or 0
    if round_minutes > 240:
        raise ValueError("Afrunding må højst være 240 minutter")
    detail = query.get("detail") or ""
    if detail not in ("", "project", "day", "timeline"):
        raise ValueError("Ugyldig værdi: detail")
    text = tracker.export_csv(first, last, round_minutes=round_minutes, per_day=detail == "day",
                              per_timeline=detail == "timeline")
    span = first.isoformat() if first == last else f"{first.isoformat()} til {last.isoformat()}"
    return _FileResponse(text.encode("utf-8"), "text/csv; charset=utf-8", f"Projektsøg tid {span}.csv")


def content_disposition(filename: str) -> str:
    """``attachment`` with an ASCII fallback name and the real (UTF-8) one (RFC 6266)."""
    ascii_name = filename.encode("ascii", "replace").decode("ascii").replace('"', "")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


def _query_bool(query: dict[str, str], name: str) -> bool | None:
    raw = query.get(name, "").strip().lower()
    if not raw:
        return None
    if raw in ("1", "true"):
        return True
    if raw in ("0", "false"):
        return False
    raise ValueError(f"Ugyldig værdi: {name}")


def _body_bool(body: dict[str, Any], name: str, default: bool) -> bool:
    value = body.get(name, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise ValueError(f"{name} skal være sand/falsk")


def _body_str(body: dict[str, Any], name: str) -> str:
    value = body.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} mangler")
    return value


def _body_optional_str(body: dict[str, Any], name: str) -> str | None:
    value = body.get(name)
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{name} skal være tekst")
    return value
