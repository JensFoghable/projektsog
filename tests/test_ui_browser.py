"""Behaviour of the web UI in headless Edge against the mock backend (skipped without Edge).

Headless Edge shows no window and uses a throwaway profile; the clipboard is stubbed in the
page (tests/_ui_browser.py), so nothing on the user's desktop is touched. The module takes
~12 s; set PROJEKTSOG_SKIP_UI_BROWSER=1 to skip it.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
import urllib.parse
import urllib.request
from typing import Any

from tests._ui_browser import CDPError, Edge, Page, find_edge
from tests._ui_mock_server import MockServer

ROWS = "document.querySelectorAll('#results .row').length > 0"
SELECTED = "document.querySelector('#results .row[aria-selected=true] .row__name')?.textContent ?? null"
SETTINGS_SCROLL = "Math.round(document.querySelector('.settings__body').scrollTop)"
# Nowhere a result key could act from: <body> or a control inside the settings (UI2-2).
FOCUS_OFF_RESULTS = ("document.activeElement === document.body"
                     " || document.querySelector('#settings').contains(document.activeElement)")
# Page-side switches for visibility and clock, to exercise the §12 query-lifetime rules.
SIMULATE_VISIBILITY = """
(() => {
  let hidden = false;
  let offset = 0;
  const realNow = Date.now.bind(Date);
  Date.now = () => realNow() + offset;
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => (hidden ? 'hidden' : 'visible') });
  window.__hide = () => { hidden = true; document.dispatchEvent(new Event('visibilitychange')); };
  window.__show = () => { hidden = false; document.dispatchEvent(new Event('visibilitychange')); };
  window.__advance = (ms) => { offset += ms; };
})()
"""

_tmp: tempfile.TemporaryDirectory | None = None
_edge: Edge | None = None
_page: Page | None = None


def setUpModule() -> None:
    global _tmp, _edge, _page
    _tmp = tempfile.TemporaryDirectory()
    os.environ["LOCALAPPDATA"] = _tmp.name
    reason = None
    if os.environ.get("PROJEKTSOG_SKIP_UI_BROWSER") == "1":
        reason = "PROJEKTSOG_SKIP_UI_BROWSER=1"
    elif find_edge() is None:
        reason = "Microsoft Edge is not installed"
    if reason:
        _tmp.cleanup()
        raise unittest.SkipTest(reason)
    _edge = Edge()
    try:
        _page = _edge.start()
    except (CDPError, OSError) as exc:
        _edge.close()
        _tmp.cleanup()
        raise unittest.SkipTest(f"headless Edge unavailable: {exc}") from exc


def tearDownModule() -> None:
    if _edge is not None:
        _edge.close()
    if _tmp is not None:
        _tmp.cleanup()


class UiCase(unittest.TestCase):
    scenario = "asked"

    def setUp(self) -> None:
        assert _page is not None
        self.page = _page
        self.server = MockServer(scenario=self.scenario).start()
        self.page.set_viewport(1180, 780)
        self.page.color_scheme("dark")

    def tearDown(self) -> None:
        self.page.navigate("about:blank")  # closes the page's SSE stream first
        self.server.stop()
        self.assertEqual(self.server.backend.errors, [], "mock backend raised")

    # -- helpers -----------------------------------------------------------------------
    def open(self, ready: str = ROWS, **params: str) -> None:
        query = f"?{urllib.parse.urlencode(params)}" if params else ""
        self.page.navigate(self.server.url + query)
        self.page.wait_for(ready)

    def js(self, expression: str) -> Any:
        return self.page.evaluate(expression)

    def wait(self, expression: str, timeout: float = 6.0) -> Any:
        return self.page.wait_for(expression, timeout)

    def text(self, selector: str) -> str:
        return self.js(f"document.querySelector({json.dumps(selector)})?.textContent ?? ''")

    def names(self) -> list[str]:
        return self.js("[...document.querySelectorAll('#results .row .row__name')].map(n => n.textContent)")

    def requests(self, path: str, method: str | None = None) -> list[dict[str, Any]]:
        backend = self.server.backend
        with backend.lock:
            found = list(backend.requests)
        return [r for r in found if r["path"] == path and (method is None or r["method"] == method)]

    def wait_request(self, path: str, method: str = "POST", count: int = 1, timeout: float = 5.0,
                     where: Any = None) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            found = [r for r in self.requests(path, method) if where is None or where(r)]
            if len(found) >= count:
                return found[count - 1]
            if time.monotonic() > deadline:
                self.fail(f"expected {count}× {method} {path}, got {len(found)}")
            time.sleep(0.03)

    def control(self, path: str, body: dict[str, Any]) -> None:
        request = urllib.request.Request(self.server.url.rstrip("/") + path, data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json", "X-Projektsog": "1"})
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 200)

    def item_path(self, name: str) -> str:
        return self.js(f"""(() => {{ const row = [...document.querySelectorAll('#results .row')]
            .find(r => r.querySelector('.row__name').textContent === {json.dumps(name)});
            return row ? row.querySelector('.row__loc').title : null; }})()""")

    def focus_page(self) -> None:
        """Give the page window focus first: the first input event of a session focuses the
        window, and the app's window 'focus' handler then (rightly) focuses the search field –
        which would undo a control focused from script, as if with Tab."""
        self.page.click("#q")

    def visible(self, selector: str) -> bool:
        return self.js(f"(() => {{ const n = document.querySelector({json.dumps(selector)});"
                       " return !!n && n.checkVisibility(); })()")  # also false inside a closed <details>

    def set_online(self, source_id: int, online: bool = True, path: str | None = None, events: str = "all") -> None:
        """The mock's location goes on/offline like the Indexer reports it (§15.1)."""
        body: dict[str, Any] = {"source_id": source_id, "online": online, "events": events}
        if path:
            body["path"] = path
        self.control("/api/_mock/set-online", body)


class SearchAndKeyboardTests(UiCase):
    def test_title_focus_and_first_result(self) -> None:
        self.open(q="lindholm")
        self.assertEqual(self.js("document.title"), "Projektsøg")
        self.assertEqual(self.js("document.activeElement.id"), "q")
        self.assertEqual(self.names()[0], "Rikke Lindholm")
        self.assertEqual(self.js(SELECTED), "Rikke Lindholm")
        self.assertEqual(self.js("document.querySelector('#results .row mark').textContent"), "Lindholm")
        active = self.js("document.querySelector('#q').getAttribute('aria-activedescendant')")
        self.assertEqual(active, self.js("document.querySelector('#results .row[aria-selected=true]').id"))
        search = self.wait_request("/api/search", "GET")
        self.assertNotIn("online", search["query"])  # only sent after the user toggles it
        self.assertEqual(search["query"]["limit"], "200")

    def test_enter_and_ctrl_enter_send_open_path_and_action(self) -> None:
        self.open(q="rikke lindholm")
        names = self.names()
        self.assertEqual(names[0], "Rikke Lindholm")
        file_name = names[1]
        self.page.key("ArrowDown")
        self.assertEqual(self.js(SELECTED), file_name)
        self.page.key("Enter")
        first = self.wait_opened(1)
        self.assertEqual(first["action"], "reveal")
        self.assertEqual(first["path"], self.item_path(file_name))
        self.page.key("Enter", ctrl=True)
        self.assertEqual(self.wait_opened(2)["action"], "file")
        self.page.key("ArrowUp")
        self.page.key("Enter")
        self.assertEqual(self.wait_opened(3), {"path": "C:\\Kunder 2026 (STUDIO)\\Rikke Lindholm", "action": "folder"})
        self.page.key("Enter", ctrl=True)
        self.assertEqual(self.wait_opened(4)["action"], "reveal")
        self.assertFalse(self.requests("/api/window/hide"), "the server hides after an open, not the UI")

    def test_second_open_is_ignored_while_the_first_is_pending(self) -> None:
        self.open(q="rikke lindholm")
        self.page.key("Enter")
        self.page.key("Enter", ctrl=True)
        self.wait_opened(1)
        time.sleep(0.4)
        self.assertEqual(len(self.requests("/api/open")), 1)

    def wait_opened(self, count: int) -> dict[str, Any]:
        """Body of the count-th /api/open, once the mock (120 ms) has answered it."""
        body = self.wait_request("/api/open", count=count)["body"]
        time.sleep(0.2)
        return body

    def test_sequence_opens_first_frame(self) -> None:
        self.open(q="render exr")
        self.assertIn("4.500 billeder", self.text("#results .row .row__title"))
        self.page.key("Enter")
        body = self.wait_request("/api/open")["body"]
        self.assertEqual(body["path"], "\\\\MEDIESERVER\\2025Arkiv\\Unreal Showreel 2025\\Render\\render_0001.exr")

    def test_long_lists_render_fully_and_keep_the_selection_in_view(self) -> None:
        self.open(q="a")
        self.wait("document.querySelectorAll('#results .row').length === 200")
        self.assertTrue(self.text(".results__note").startswith("Viser de første 200 af"))
        index = "[...document.querySelectorAll('#results .row')].findIndex(r => r.getAttribute('aria-selected') === 'true')"
        in_view = """(() => { const row = document.querySelector('#results .row[aria-selected=true]').getBoundingClientRect();
            const box = document.querySelector('#results').getBoundingClientRect();
            return row.top >= box.top - 1 && row.bottom <= box.bottom + 1; })()"""
        for _ in range(35):
            self.page.key("ArrowDown")
        self.assertEqual(self.js(index), 35)
        self.assertTrue(self.js(in_view))
        self.page.key("PageDown")
        self.assertGreater(self.js(index), 36)
        self.assertTrue(self.js(in_view))
        self.page.key("End", ctrl=True)
        self.assertEqual(self.js(index), 199)
        self.assertTrue(self.js(in_view))
        self.page.key("Home", ctrl=True)
        self.assertEqual(self.js(index), 0)
        self.assertEqual(self.js("document.querySelector('#results').scrollTop"), 0)

    def test_escape_clears_query_then_hides_window(self) -> None:
        self.open(q="lindholm")
        self.page.key("Escape")
        self.wait("document.querySelector('#q').value === '' && document.querySelector('#section-recent')")
        self.assertFalse(self.requests("/api/window/hide"))
        self.page.key("Escape")
        self.assertEqual(self.wait_request("/api/window/hide")["body"], {"restore_previous": True})

    def test_offline_row_shows_hint_and_never_calls_open(self) -> None:
        self.open(q="pixelbro radio")
        self.assertIn("Offline – sidst set", self.text("#results .row .row__meta"))
        self.page.key("Enter")
        self.wait("document.querySelector('.row__notice.is-active')")
        self.assertEqual(self.text(".row__notice.is-active"), "Tilslut disken ‘2024 Disk Sølv’")
        time.sleep(0.25)
        self.assertFalse(self.requests("/api/open"))

    def test_zero_results_explain_filters_with_one_click_fix(self) -> None:
        self.open(q="grafik")
        self.page.key("4", ctrl=True)
        self.wait("!document.querySelector('#empty').hidden")
        self.assertEqual(self.wait_request("/api/search", "GET", count=2)["query"]["kind"], "file")
        self.assertEqual(self.text(".empty__title"), "Ingen resultater for ‘grafik’")
        reasons = self.js("[...document.querySelectorAll('.reason')].map(r => r.textContent)")
        self.assertTrue(any("skjules af filteret ‘Filer’" in r and r.endswith("Vis alle") for r in reasons), reasons)
        self.assertTrue(any("2025Arkiv indekseres stadig" in r for r in reasons), reasons)
        self.page.click(".reason .link")
        self.wait(ROWS)
        self.assertEqual(self.js("document.querySelector('[data-kind=all]').getAttribute('aria-checked')"), "true")

    def test_matches_hidden_by_two_filters_get_a_clear_all_fix(self) -> None:
        """XMC-4 (UI side of §15.7): the project fails 'Filer' and 'Kun online' at once."""
        self.open(q="fagmesse")
        self.page.key("4", ctrl=True)
        self.wait_request("/api/search", "GET", where=lambda r: r["query"].get("kind") == "file")
        self.page.click("#online-only")
        self.wait_request("/api/search", "GET", where=lambda r: r["query"].get("online") == "1")
        self.wait("[...document.querySelectorAll('.reason')].some(r => r.textContent.includes('Ryd alle filtre'))")
        self.assertIn("4 resultater skjules af filtrene", self.text(".reasons"))
        index = self.js("[...document.querySelectorAll('.reason')].findIndex(r => r.textContent.includes('Ryd alle'))")
        self.page.click(f".reason:nth-child({index + 1}) .link")
        self.wait_request("/api/search", "GET", where=lambda r: r["query"].get("kind") == "all"
                          and r["query"].get("online") == "0" and "source" not in r["query"])
        self.wait(ROWS)
        self.assertIn("Fagmesse 2024", self.names())
        self.assertEqual(self.js("document.querySelector('[data-kind=all]').getAttribute('aria-checked')"), "true")
        self.assertEqual(self.js("document.querySelector('#online-only').getAttribute('aria-checked')"), "false")

    def test_online_toggle_and_offline_reason(self) -> None:
        self.open(q="fagmesse")
        self.page.click("#online-only")
        self.wait_request("/api/search", "GET", where=lambda r: r["query"].get("online") == "1")
        self.wait("!document.querySelector('#empty').hidden")
        self.assertIn("4 på offline placeringer", self.text(".reasons"))
        self.page.click(".reason .link")
        self.wait_request("/api/search", "GET", where=lambda r: r["query"].get("online") == "0")
        self.wait(ROWS)

    def test_location_filter_and_kind_shortcuts(self) -> None:
        self.open(q="klar tand")
        self.wait("document.querySelectorAll('#location option').length > 5")
        self.js("const s = document.querySelector('#location'); s.value = '11'; s.dispatchEvent(new Event('change'))")
        self.wait_request("/api/search", "GET", where=lambda r: r["query"].get("source") == "11")
        self.wait("[...document.querySelectorAll('#results .badge__text')].every(b => !b.textContent.includes('Disk:'))")
        self.page.key("2", ctrl=True)
        self.wait_request("/api/search", "GET", where=lambda r: r["query"].get("kind") == "project")

    def test_enter_right_after_typing_opens_the_new_top_result(self) -> None:
        self.open(q="lindholm")
        self.page.type(" klip")
        self.page.key("Enter")  # before the 80 ms debounce has fired
        body = self.wait_request("/api/open")["body"]
        self.assertEqual(body["path"], "C:\\Kunder 2026 (STUDIO)\\Rikke Lindholm\\Klip")

    def test_typing_while_a_button_has_focus_goes_to_the_search(self) -> None:
        self.open(q="")
        self.focus_page()
        self.js("document.querySelector('[data-kind=dir]').focus()")  # reached with Tab
        self.page.key(" ")
        self.wait("document.querySelector('[data-kind=dir]').getAttribute('aria-checked') === 'true'")
        self.assertEqual(self.js("document.activeElement.dataset.kind"), "dir")  # keyboard use keeps focus
        for char in "lin":
            self.page.key(char)
        self.wait("document.querySelector('#q').value === 'lin' && document.activeElement.id === 'q'")
        self.wait_request("/api/search", "GET", where=lambda r: r["query"].get("q") == "lin"
                          and r["query"].get("kind") == "dir")

    def test_subfolder_chip_opens_folder(self) -> None:
        self.open(q="rikke lindholm")
        self.page.click("#results .row .chip[data-chip='Klip']")
        body = self.wait_request("/api/open")["body"]
        self.assertEqual(body, {"path": "C:\\Kunder 2026 (STUDIO)\\Rikke Lindholm\\Klip", "action": "folder"})

    def test_copy_path_and_unc_path(self) -> None:
        self.open(q="rikke lindholm")
        self.page.key("c", ctrl=True)
        self.wait("window.__copied.at(-1) === 'C:\\\\Kunder 2026 (STUDIO)\\\\Rikke Lindholm'")
        self.page.key("C", ctrl=True, shift=True)
        self.wait("window.__copied.at(-1) === '\\\\\\\\STUDIO-PC\\\\Kunder 2026 (STUDIO)\\\\Rikke Lindholm'")
        self.assertEqual(self.text("#toast .toast__body div"), "Netværksstien er kopieret")

    def test_copy_falls_back_when_the_clipboard_api_refuses(self) -> None:
        self.open(q="rikke lindholm")
        self.js("navigator.clipboard.writeText = () => Promise.reject(new DOMException('nope', 'NotAllowedError'))")
        self.page.key("c", ctrl=True)
        self.wait("window.__copied.at(-1) === 'C:\\\\Kunder 2026 (STUDIO)\\\\Rikke Lindholm'")
        self.assertEqual(self.js("document.activeElement.id"), "q")
        self.assertEqual(self.text("#toast .toast__body div"), "Stien er kopieret")

    def test_context_menu_copies_row_path(self) -> None:
        self.open(q="rikke lindholm")
        second = self.names()[1]
        self.page.click("#results .row:nth-child(2)", button="right")
        self.wait("document.querySelector('.menu')")
        self.assertEqual(self.js(SELECTED), second)
        self.page.click(".menu__item:nth-of-type(3)")
        self.wait(f"window.__copied.at(-1) === {json.dumps(self.item_path(second))}")
        self.assertFalse(self.js("!!document.querySelector('.menu')"))

    def test_selection_kept_by_id_on_index_updated(self) -> None:
        self.server.stop()
        self.server = MockServer(scenario="asked,idle").start()
        self.open(q="klar tand")
        self.page.key("ArrowDown")
        self.page.key("ArrowDown")
        chosen = self.js(SELECTED)
        self.js("document.querySelector('#results .row').dataset.marker = 'old'")
        self.control("/api/_mock/publish", {"type": "index_updated", "data": {"source_id": 2}})
        time.sleep(0.5)
        self.assertTrue(self.js("!!document.querySelector('#results .row[data-marker]')"),
                        "no re-run within 1.5 s of a key press")
        self.wait("!document.querySelector('#results .row[data-marker]')", timeout=5)
        self.assertEqual(self.js(SELECTED), chosen)

    def test_events_reconnect_and_resync(self) -> None:
        self.open(mock="asked,idle")
        streams = len(self.requests("/api/events", "GET"))
        statuses = len(self.requests("/api/status", "GET"))
        self.control("/api/_mock/drop-events", {})
        self.wait_request("/api/events", "GET", count=streams + 1, timeout=6)
        self.wait_request("/api/status", "GET", count=statuses + 1, timeout=6)


class FocusAfterMouseTests(UiCase):
    """UI-1: filters and bar buttons clicked with the mouse leave ↑/↓, Enter and typing to the list."""

    def test_arrow_down_after_clicking_a_kind_chip_moves_the_selection(self) -> None:
        self.open(q="klar tand")
        self.page.click("[data-kind=project]")
        self.wait_request("/api/search", "GET", where=lambda r: r["query"].get("kind") == "project")
        self.wait("!document.querySelector('#results .row--file, #results .tile--folder')")
        names = self.names()
        self.assertGreater(len(names), 2)
        self.assertEqual(self.js("document.activeElement.id"), "q")
        self.page.key("ArrowDown")
        self.assertEqual(self.js(SELECTED), names[1])
        self.assertEqual(self.js("document.querySelector('[data-kind=project]').getAttribute('aria-checked')"), "true")
        self.page.key("Enter")
        self.assertEqual(self.wait_request("/api/open")["body"]["path"], self.item_path(names[1]))

    def test_enter_after_clicking_online_only_opens_the_selected_row(self) -> None:
        self.open(q="rikke lindholm")
        self.page.click("#online-only")
        self.wait_request("/api/search", "GET", where=lambda r: r["query"].get("online") == "1")
        self.assertEqual(self.js("document.activeElement.id"), "q")
        self.page.key("Enter")
        self.assertEqual(self.wait_request("/api/open")["body"],
                         {"path": "C:\\Kunder 2026 (STUDIO)\\Rikke Lindholm", "action": "folder"})
        self.assertEqual(self.js("document.querySelector('#online-only').getAttribute('aria-checked')"), "true")

    def test_typing_after_picking_a_location_goes_to_the_search(self) -> None:
        self.open(q="klar tand")
        self.wait("document.querySelectorAll('#location option').length > 5")
        self.focus_page()
        self.js("const s = document.querySelector('#location'); s.focus(); s.value = '11';"
                "s.dispatchEvent(new Event('change'))")
        self.wait_request("/api/search", "GET", where=lambda r: r["query"].get("source") == "11")
        self.assertEqual(self.js("document.activeElement.id"), "q")
        self.page.key("ArrowDown")
        self.assertEqual(self.js("document.querySelector('#location').value"), "11")
        self.page.key("x")
        self.wait("document.querySelector('#q').value === 'klar tandx'")

    def test_typing_on_the_location_list_goes_to_the_search(self) -> None:
        self.open(q="")
        self.wait("document.querySelectorAll('#location option').length > 5")
        self.focus_page()
        self.js("document.querySelector('#location').focus()")
        for char in "lin":
            self.page.key(char)
        self.wait("document.querySelector('#q').value === 'lin' && document.activeElement.id === 'q'")
        self.assertEqual(self.js("document.querySelector('#location').value"), "")

    def test_arrow_keys_on_the_location_list_keep_the_keyboard_there(self) -> None:
        self.open(q="klar tand")
        self.wait("document.querySelectorAll('#location option').length > 5")
        self.focus_page()
        self.js("document.querySelector('#location').focus()")
        self.page.key("ArrowDown")
        self.wait_request("/api/search", "GET", where=lambda r: "source" in r["query"])
        self.assertEqual(self.js("document.activeElement.id"), "location")

    def close_location_list_unchanged(self, key: str = "Escape") -> None:
        """Open the location list with the mouse and close it without a new choice (Esc, or Enter
        on the entry already chosen – like clicking it): no 'change', the focus stays on it."""
        self.wait("document.querySelectorAll('#location option').length > 5")
        self.focus_page()
        self.page.click("#location")
        self.wait("document.querySelector('#location').matches(':open')")
        self.page.key(key)  # taken by the open list itself: the query stays
        self.wait("!document.querySelector('#location').matches(':open')")
        self.assertEqual(self.js("document.activeElement.id"), "location")
        self.assertEqual(self.js("document.querySelector('#q').value"), "klar tand")
        self.assertEqual(self.js("document.querySelector('#location').value"), "")

    def test_arrow_keys_after_the_location_list_closed_unchanged_move_the_selection(self) -> None:
        """UI2-1: ↓ acts on the results again – it no longer switches the location silently."""
        self.open(q="klar tand")
        names = self.names()
        self.close_location_list_unchanged()
        self.page.key("ArrowDown")
        self.assertEqual(self.js("document.activeElement.id"), "q")
        self.assertEqual(self.js(SELECTED), names[1])
        self.close_location_list_unchanged()
        self.page.key("End")  # would pick the last location on the list
        self.assertEqual(self.js("document.activeElement.id"), "q")
        time.sleep(0.3)
        self.assertEqual(self.js("document.querySelector('#location').value"), "")
        self.assertEqual(self.js(SELECTED), names[1])
        self.assertEqual([r for r in self.requests("/api/search", "GET") if "source" in r["query"]], [])

    def test_enter_after_the_location_list_closed_unchanged_opens_the_selected_row(self) -> None:
        """UI2-1: Enter opens the row, as the footer says – it no longer re-opens the list."""
        self.open(q="klar tand")
        first = self.names()[0]
        self.close_location_list_unchanged("Enter")
        time.sleep(0.3)
        self.assertFalse(self.requests("/api/open"), "the Enter that closed the list opened nothing")
        self.page.key("Enter")
        self.assertEqual(self.wait_request("/api/open")["body"], {"path": self.item_path(first), "action": "folder"})
        self.assertFalse(self.js("document.querySelector('#location').matches(':open')"))
        self.assertEqual(self.js("document.activeElement.id"), "q")

    def test_clicking_a_row_takes_the_keys_back_from_a_filter(self) -> None:
        self.open(q="rikke lindholm")
        self.focus_page()
        self.js("document.querySelector('[data-kind=all]').focus()")  # reached with Tab earlier
        names = self.names()
        self.page.click("#results .row:nth-child(2)")
        self.assertEqual(self.js("document.activeElement.id"), "q")
        self.page.key("ArrowDown")
        self.assertEqual(self.js(SELECTED), names[2])
        self.assertEqual(self.js("document.querySelector('[data-kind=all]').getAttribute('aria-checked')"), "true")

    def test_resolve_bar_buttons_leave_the_keys_to_the_list(self) -> None:
        self.open(ready="document.querySelector('#section-resolve') && document.querySelector('#section-recent')")
        self.page.click("[data-resolve=toggle]")
        self.wait("document.querySelector('.resolve__details')")
        self.assertEqual(self.js("document.activeElement.id"), "q")
        first = self.js(SELECTED)
        self.page.key("ArrowDown")
        self.assertNotEqual(self.js(SELECTED), first)
        self.assertTrue(self.js("!!document.querySelector('.resolve__details')"), "Enter/arrows left the bar alone")


class OnlineAgainTests(UiCase):
    """XMC-1 / §15.1: rows follow a location that comes back; never refused on stale data."""
    # No scan: no periodic index_updated hides a missing refresh. No focus event: it would
    # re-fetch the Resolve state and hide a stale Resolve bar.
    scenario = "asked,idle,nofocus"

    ROW_PATH = "I:\\2024 Disk Sølv\\Pixelbro Radio"  # the disk comes back under another letter

    def test_row_follows_its_disk_back_online_and_drops_the_old_hint(self) -> None:
        self.open(q="pixelbro radio")
        self.assertEqual(self.names()[0], "Pixelbro Radio")
        self.page.key("Enter")
        self.wait("document.querySelector('.row__notice.is-active')")
        self.set_online(3, path="I:\\2024 Disk Sølv")  # sources + index_updated + status
        self.wait("!document.querySelector('#results .row--offline') && !document.querySelector('.row__notice')",
                  timeout=8)
        self.assertEqual(self.text("#results .row .badge__text"), "Disk: 2024 Disk Sølv (I:)")
        self.page.key("Enter")
        self.assertEqual(self.wait_request("/api/open")["body"], {"path": self.ROW_PATH, "action": "folder"})

    def test_sources_event_alone_brings_the_rows_back(self) -> None:
        self.open(q="pixelbro radio")
        self.set_online(3, path="I:\\2024 Disk Sølv", events="sources")  # a backend without §15.1
        self.wait("!document.querySelector('#results .row--offline')", timeout=8)
        self.assertEqual(self.item_path("Pixelbro Radio"), self.ROW_PATH)

    def test_enter_on_a_row_that_still_looks_offline_opens_it_at_the_new_path(self) -> None:
        self.open(q="pixelbro radio")
        self.wait("document.querySelectorAll('#location option').length > 5")
        self.page.key("ArrowDown")
        self.page.key("ArrowUp")  # a key press holds the automatic re-run back for 1.5 s
        self.set_online(3, path="I:\\2024 Disk Sølv", events="sources")
        self.wait("[...document.querySelectorAll('#location option')].some(o => o.textContent === '2024 Disk Sølv')")
        searches = len(self.requests("/api/search", "GET"))
        self.assertTrue(self.js("!!document.querySelector('#results .row--offline[aria-selected=true]')"),
                        "the row is still dimmed when Enter comes")
        self.page.key("Enter")
        self.assertEqual(self.wait_request("/api/open")["body"], {"path": self.ROW_PATH, "action": "folder"})
        self.assertGreater(len(self.requests("/api/search", "GET")), searches, "re-fetched before opening")
        self.assertFalse(self.js("!!document.querySelector('.row__notice.is-active')"))

    def test_resolve_folder_opens_when_its_disk_is_back_before_the_bar_updates(self) -> None:
        self.open(ready="document.querySelector('.resolve__warn') && document.querySelector('#section-resolve')",
                  mock="resolve-offline,asked,idle,nofocus")
        self.wait("document.querySelectorAll('#location option').length > 5")
        self.set_online(3, path="I:\\2024 Disk Sølv", events="sources")
        self.wait("[...document.querySelectorAll('#location option')].some(o => o.textContent === '2024 Disk Sølv')")
        self.page.click("[data-resolve=open]")
        self.assertEqual(self.wait_request("/api/open")["body"], {"path": self.ROW_PATH, "action": "folder"})
        time.sleep(0.3)  # the mock answers after 120 ms; a second open while one is pending is ignored
        self.page.click("[data-resolve=toggle]")
        self.wait("document.querySelector('.rd__row[data-list=folders]')")
        self.page.click(".rd__row[data-list=folders][data-index='0']")
        self.assertEqual(self.wait_request("/api/open", count=2)["body"], {"path": self.ROW_PATH, "action": "folder"})

    def test_offline_row_is_still_refused_while_the_disk_is_away(self) -> None:
        self.open(q="pixelbro radio")
        self.set_online(1, online=True)  # another location changes – source 3 stays offline
        time.sleep(0.6)
        self.page.key("Enter")
        self.wait("document.querySelector('.row__notice.is-active')")
        self.assertEqual(self.text(".row__notice.is-active"), "Tilslut disken ‘2024 Disk Sølv’")
        time.sleep(0.3)
        self.assertFalse(self.requests("/api/open"))


class FolderGoneTests(UiCase):
    """§15.12: offline while its disk is there (volume_present) – the folder was moved, renamed or
    deleted, so no hint to connect the (always connected) Windows disk."""
    scenario = "asked,idle,nofocus"

    def setUp(self) -> None:
        super().setUp()
        self.set_online(1, online=False)  # C:\Kunder 2026 (STUDIO) is gone; C: is still there

    def test_rows_say_the_folder_is_gone_and_are_not_opened(self) -> None:
        self.open(q="rikke lindholm")
        self.assertEqual(self.names()[0], "Rikke Lindholm")
        self.assertTrue(self.js("document.querySelector('#results .row').classList.contains('row--offline')"))
        self.assertEqual(self.text("#results .row .row__notice"), "Mappen findes ikke længere")
        self.assertEqual(self.js("document.querySelector('#results .row .row__notice use').getAttribute('href')"),
                         "#i-folder")
        self.page.key("Enter")
        self.wait("document.querySelector('.row__notice.is-active')")
        self.assertEqual(self.text(".row__notice.is-active"), "Mappen findes ikke længere")
        time.sleep(0.25)
        self.assertFalse(self.requests("/api/open"))

    def test_resolve_bar_and_settings_name_the_folder_not_the_disk(self) -> None:
        self.open(ready="document.querySelectorAll('.resolve__warn').length === 2")
        warnings = self.js("[...document.querySelectorAll('.resolve__warn')].map(w => w.textContent)")
        self.assertEqual(warnings, ["160 klip ligger i mappen ‘Kunder 2026 (STUDIO)’, som ikke findes længere",
                                    "6 klip ligger på disken ‘2024 Disk Sølv’, som ikke er tilsluttet"])
        self.assertEqual(self.js("document.querySelector('[data-resolve=open]').title"), "Mappen findes ikke længere")
        self.page.click("[data-resolve=open]")
        self.wait("document.querySelector('.row__notice.is-active')")
        self.assertEqual(self.text(".row__notice.is-active"), "Mappen findes ikke længere")
        self.page.click("#settings-button")
        self.wait("document.querySelectorAll('.src').length >= 12")
        self.assertEqual(self.text(".src[data-id='1'] .src__scan > div"), "Mappen findes ikke længere")
        self.assertEqual(self.text(".src[data-id='1'] [data-source-action]"), "Glem")
        self.assertEqual(self.text(".src[data-id='3'] .src__scan > div")[:20], "Offline – sidst set ")
        time.sleep(0.25)
        self.assertFalse(self.requests("/api/open"))


class QueryLifetimeTests(UiCase):
    def _prepare(self) -> None:
        self.open(q="bøgely")
        self.js(SIMULATE_VISIBILITY)

    def test_reset_after_30_s_hidden(self) -> None:
        self._prepare()
        self.page.key("2", ctrl=True)
        self.js("__hide()")
        self.js("__advance(31000)")
        self.js("__show()")
        self.wait("document.querySelector('#q').value === '' && document.querySelector('#section-recent')")
        self.assertEqual(self.js("document.querySelector('[data-kind=all]').getAttribute('aria-checked')"), "true")
        self.assertEqual(self.js("document.activeElement.id"), "q")

    def test_short_hide_keeps_and_selects_query(self) -> None:
        self._prepare()
        self.js("__hide()")
        self.js("__advance(5000)")
        self.js("__show()")
        state = self.js("(() => { const q = document.querySelector('#q'); return [q.value, q.selectionStart, q.selectionEnd]; })()")
        self.assertEqual(state, ["bøgely", 0, 6])

    def test_keys_replayed_before_show_replace_the_old_query(self) -> None:
        self._prepare()
        self.js("__hide()")
        self.page.type("lin")
        self.js("__show()")
        self.wait("document.querySelector('#q').value === 'lin'")
        self.wait_request("/api/search", "GET", where=lambda r: r["query"].get("q") == "lin")


class ResolveAndCardTests(UiCase):
    def test_resolve_bar_primary_preselected_and_details(self) -> None:
        self.open(ready="document.querySelector('#section-resolve') && document.querySelector('#section-recent')")
        self.assertEqual(self.text("#section-resolve"), "Fra DaVinci Resolve")
        self.assertEqual(self.js(SELECTED), "Rikke Lindholm")
        self.assertIn("160 klip", self.text("#results .row[aria-selected=true] .row__title"))
        self.assertIn("Rikke Lindholm - Testimonial", self.text(".resolve__text"))
        self.assertEqual(self.text(".resolve__warn"), "6 klip ligger på disken ‘2024 Disk Sølv’, som ikke er tilsluttet")
        self.page.click("[data-resolve=toggle]")
        self.wait("document.querySelector('.resolve__details')")
        self.assertIn("C:\\Github\\undertekster", self.text(".resolve__details"))
        self.page.click("[data-resolve=open]")
        self.assertEqual(self.wait_request("/api/open")["body"],
                         {"path": "C:\\Kunder 2026 (STUDIO)\\Rikke Lindholm", "action": "folder"})
        self.page.click("[data-resolve=refresh]")
        self.wait_request("/api/resolve/refresh")

    def test_name_match_is_labelled_and_never_preselected(self) -> None:
        self.open(mock="resolve-suggestion,asked")
        primary = self.js("document.querySelector('#section-resolve + .row')?.getAttribute('aria-selected')")
        self.assertEqual(primary, "false")
        self.assertIn("Muligt match", self.text("#section-resolve + .row"))
        self.assertEqual(self.js(SELECTED), self.text("#section-recent + .row .row__name"))

    def test_error_text_and_hidden_bar(self) -> None:
        self.open(ready="document.querySelector('.resolve__error')", mock="resolve-error,asked")
        self.assertTrue(self.text(".resolve__error").startswith("Slå ekstern scripting til i DaVinci Resolve"))
        self.open(mock="resolve-off,asked")
        self.assertTrue(self.js("document.querySelector('#resolve').hidden"))
        self.assertFalse(self.js("!!document.querySelector('#section-resolve')"))

    def test_offline_warnings_for_disk_and_host(self) -> None:
        self.open(ready="document.querySelectorAll('.resolve__warn').length === 2", mock="resolve-offline,asked")
        warnings = self.js("[...document.querySelectorAll('.resolve__warn')].map(w => w.textContent)")
        self.assertEqual(warnings, ["48 klip ligger på disken ‘2024 Disk Sølv’, som ikke er tilsluttet",
                                    "12 klip ligger på MEDIESERVER, som ikke svarer"])
        self.page.key("Enter")  # the pre-selected primary is offline
        self.wait("document.querySelector('.row__notice.is-active')")
        self.assertFalse(self.requests("/api/open"))

    def test_status_pill_states(self) -> None:
        self.wait_for_pill("?mock=asked", "Scanner 2025Arkiv – ")
        self.wait_for_pill("?mock=asked,idle", "12 placeringer online · 1 offline")
        self.wait_for_pill("?mock=asked,first", "11 af 12 placeringer klar · Scanner 2025Arkiv – ")
        self.assertEqual(self.text("#hotkey-hint"), "Shift+Mellemrum åbner Projektsøg overalt · Esc skjuler")

    def wait_for_pill(self, query: str, prefix: str) -> None:
        self.page.navigate(self.server.url + query)
        self.wait(f"document.querySelector('.pill__text').textContent.startsWith({json.dumps(prefix)})")


class QuestionCardTests(UiCase):
    scenario = ""  # resolve_hotkey_asked is still false

    def test_resolve_question_card_keeps_shift_space_for_resolve(self) -> None:
        self.open(ready="document.querySelector('.card')")
        self.assertIn("DaVinci Resolve bruger selv Shift+Mellemrum til effektsøgning.", self.text(".card"))
        self.page.click("[data-card-focus='ask:keep']")
        body = self.wait_request("/api/settings")["body"]
        self.assertEqual(body, {"hotkey_passthrough_apps": ["Resolve.exe", "Fusion.exe"], "resolve_hotkey_asked": True})
        self.wait("!document.querySelector('.card')")

    def test_new_disk_card_includes_the_disk(self) -> None:
        self.open(ready="document.querySelector('.card [data-card-focus$=include]')", mock="newdisk,asked")
        self.assertEqual(self.text(".card .card__title"), "Ny disk ‘ARKIV’ (F:) tilsluttet – Ingen projektmapper fundet")
        self.page.click(".card [data-card-focus$=include]")
        self.assertEqual(self.wait_request("/api/sources/5/mode")["body"], {"mode": "include"})
        self.wait("document.querySelector('.card')?.textContent.includes('Medtaget i søgningen')")


class NewDiskCardTests(UiCase):
    """XMC-3 / UI-2: the new-disk card offers [Medtag] only when there is something to include,
    and says what really happened."""
    scenario = "asked,nofocus,idle"
    INCLUDE = ".card [data-card-focus$=include]"

    def publish_disk(self, **data: Any) -> None:
        payload = {"disk_name": "Ny SSD", "drive": "G:", "source_ids": [], "included": False,
                   "reason": "Ingen projektmapper fundet", **data}
        self.control("/api/_mock/publish", {"type": "new_volume", "data": payload})
        self.wait("document.querySelector('.card')")

    def mode_calls(self) -> list[dict[str, Any]]:
        return [c for c in self.server.backend.calls if c["path"].startswith("/api/sources/")]

    def test_disk_without_folders_offers_nothing_to_include(self) -> None:
        self.open()
        self.publish_disk()
        self.assertEqual(self.text(".card .card__title"), "Ny disk ‘Ny SSD’ (G:) tilsluttet – Ingen projektmapper fundet")
        self.assertEqual(self.text(".card .card__text"),
                         "Ingen mapper at medtage endnu – nye projektmapper på disken findes automatisk.")
        self.assertFalse(self.js(f"!!document.querySelector({json.dumps(self.INCLUDE)})"))
        self.assertNotIn("Medtaget", self.text(".card"))
        self.page.click(".card .card__close")
        self.wait("!document.querySelector('.card')")
        self.assertEqual(self.mode_calls(), [])

    def test_a_failed_include_is_reported_and_can_be_retried(self) -> None:
        self.open()
        self.publish_disk(source_ids=[999])
        self.page.click(self.INCLUDE)
        self.wait("document.querySelector('.card .card__error')")
        self.assertEqual(self.text(".card .card__error"), "Placeringen findes ikke")
        self.assertNotIn("Medtaget", self.text(".card"))
        self.assertTrue(self.js(f"!!document.querySelector({json.dumps(self.INCLUDE + ':not(:disabled)')})"))

    def test_partial_include_says_how_much_was_included(self) -> None:
        self.open()
        self.publish_disk(source_ids=[14, 999])
        self.page.click(self.INCLUDE)
        self.wait("document.querySelector('.card .card__text')")
        self.assertEqual(self.text(".card .card__text"),
                         "1 af 2 mapper er medtaget i søgningen – disken bliver scannet nu.")
        self.assertEqual(self.text(".card .card__error"), "Placeringen findes ikke")
        self.assertEqual([c["path"] for c in self.mode_calls()], ["/api/sources/14/mode", "/api/sources/999/mode"])

    def test_including_a_disk_that_was_unplugged_again_says_when_it_is_scanned(self) -> None:
        self.open()
        self.publish_disk(disk_name="2024 Disk Sølv", drive="H:", source_ids=[3])
        self.page.click(self.INCLUDE)
        self.wait("document.querySelector('.card .card__text')")
        self.assertEqual(self.text(".card .card__text"),
                         "Medtaget i søgningen – disken scannes, når den er tilsluttet igen.")

    def test_disk_included_by_the_indexer_just_says_so(self) -> None:
        self.open()
        self.publish_disk(disk_name="ARKIV", drive="F:", source_ids=[5], included=True, reason="Mediefiler fundet")
        self.assertEqual(self.text(".card .card__text"), "Medtaget i søgningen – disken bliver scannet nu.")
        self.assertFalse(self.js(f"!!document.querySelector({json.dumps(self.INCLUDE)})"))


class RootProjectTests(UiCase):
    """§15.3: a folder that is itself a project (synthesised Item: rel_path "", no id)."""

    def test_folder_that_is_itself_a_project_is_listed_and_opened(self) -> None:
        self.open(q="julefrokost 2026")
        self.assertEqual(self.names()[0], "Dækcentret Julefrokost 2026")
        self.assertEqual(self.text("#results .row .badge__text"), "Disk: T7 Shield (E:) · Dækcentret Julefrokost 2026")
        self.page.key("Enter")
        self.assertEqual(self.wait_request("/api/open")["body"],
                         {"path": "E:\\Dækcentret Julefrokost 2026", "action": "folder"})
        self.open(q="c0002 dækcentret julefrokost")  # 'Julefrokost 2023' has a C0002.MP4 as well
        self.assertEqual(self.names(), ["C0002.MP4"])
        self.assertEqual(self.text("#results .row .crumbs"), "Klip › A7S")

    def test_resolve_primary_without_index_id_gets_its_own_clip_count(self) -> None:
        self.open(ready="document.querySelector('#section-resolve')")
        root = {"id": None, "kind": "project", "name": "Dækcentret Julefrokost 2026", "hl": [],
                "path": "E:\\Dækcentret Julefrokost 2026", "open_path": "E:\\Dækcentret Julefrokost 2026",
                "unc_path": None, "rel_path": "", "parent": "", "depth": 0,
                "source": {"id": 15, "name": "Dækcentret Julefrokost 2026", "host": "STUDIO-PC", "kind": "local",
                           "online": True, "drive": "E:", "disk_name": "T7 Shield", "volume_label": "T7 Shield",
                           "last_seen": None, "is_system": False},
                "project": {"name": "Dækcentret Julefrokost 2026", "rel_path": "",
                            "path": "E:\\Dækcentret Julefrokost 2026", "unc_path": None},
                "size": 1, "mtime": None, "file_count": 13, "ext": None, "is_seq": False, "seq_count": None,
                "subfolders": ["Klip"], "score": None}
        other = {**root, "name": "Arkiv", "path": "F:\\Arkiv", "open_path": "F:\\Arkiv",
                 "project": {**root["project"], "name": "Arkiv", "path": "F:\\Arkiv"}}
        state = {"enabled": True, "running": True, "connected": True, "error": None,
                 "project": "Dækcentret Julefrokost", "database": "Kunder 2026 (Projektserver)", "clip_count": 10,
                 "updated": time.time(), "other_dirs": [], "suggestions": [], "offline_clips": 0, "offline_disks": [],
                 "folders": [{"project": other["project"], "source": root["source"], "online": True, "count": 7,
                              "item": other},
                             {"project": root["project"], "source": root["source"], "online": True, "count": 3,
                              "item": root}],
                 "primary": {**root, "match": "media"}}
        self.control("/api/_mock/publish", {"type": "resolve", "data": state})
        self.wait("document.querySelector('#section-resolve + .row .row__name')?.textContent === 'Dækcentret Julefrokost 2026'")
        self.assertIn("3 klip", self.text("#section-resolve + .row .row__title"))


class SettingsTests(UiCase):
    GRAFIK = '.sg[aria-label="GRAFIK-PC"]'

    def test_excluded_folders_are_folded_away_per_computer(self) -> None:
        """Known issue 2: 'Ikke medtaget (N)' per computer, one click from its mode select."""
        self.open(ready="document.querySelectorAll('.src').length >= 12", panel="settings")
        grafik = self.GRAFIK
        self.assertEqual(self.text(f"{grafik} .sg__more-label"), "Ikke medtaget (1)")
        self.assertEqual(self.text('.sg[aria-label="STUDIO-PC"] .sg__more-label'), "Ikke medtaget (1)")
        self.assertTrue(self.js("document.querySelector('.sg[aria-label=\"KLIPPER-PC\"] .sg__more').hidden"))
        self.assertFalse(self.visible(".src[data-id='13']"))
        self.assertTrue(self.visible(".src[data-id='11']"))
        self.page.click(f"{grafik} .sg__more-head")
        self.wait(f"document.querySelector({json.dumps(grafik + ' .sg__more')}).open")
        self.assertTrue(self.visible(".src[data-id='13']"))
        # Chosen with the keyboard: the select keeps focus while its row moves up to the list.
        self.js("const s = document.querySelector('.src[data-id=\"13\"] select'); s.focus(); s.value = 'include';"
                "s.dispatchEvent(new Event('change', {bubbles: true}))")
        self.assertEqual(self.wait_request("/api/sources/13/mode")["body"], {"mode": "include"})
        self.wait(f"document.querySelector({json.dumps(grafik + ' > .sg__list > .src[data-id=\"13\"]')})"
                  f" && document.querySelector({json.dumps(grafik + ' .sg__more')}).hidden")
        self.assertEqual(self.js("document.activeElement.dataset.sourceMode"), "13")

    def test_excluding_a_folder_with_the_keyboard_follows_it_into_the_group(self) -> None:
        self.open(ready="document.querySelectorAll('.src').length >= 12", panel="settings")
        more = json.dumps(self.GRAFIK + " .sg__more")
        self.assertFalse(self.js(f"document.querySelector({more}).open"))
        self.js("const s = document.querySelector('.src[data-id=\"11\"] select'); s.focus(); s.value = 'exclude';"
                "s.dispatchEvent(new Event('change', {bubbles: true}))")
        self.assertEqual(self.wait_request("/api/sources/11/mode")["body"], {"mode": "exclude"})
        self.wait(f"document.querySelector({more}).open"
                  f" && document.querySelector({json.dumps(self.GRAFIK + ' .sg__more .src[data-id=\"11\"]')})")
        self.assertEqual(self.text(f"{self.GRAFIK} .sg__more-label"), "Ikke medtaget (2)")
        self.assertEqual(self.js("document.activeElement.dataset.sourceMode"), "11")
        self.assertTrue(self.visible(".src[data-id='11'] select"))

    def test_zero_results_link_unfolds_the_excluded_folders(self) -> None:
        self.open(ready="!document.querySelector('#empty').hidden", q="zzqqzz")
        self.wait("[...document.querySelectorAll('.reason')].some(r => r.textContent.includes('ikke medtaget'))")
        index = self.js("[...document.querySelectorAll('.reason')].findIndex(r => r.textContent.includes('ikke medtaget'))")
        self.page.click(f".reason:nth-child({index + 1}) .link")
        self.wait(f"document.querySelector({json.dumps(self.GRAFIK + ' .sg__more')})?.open")
        self.assertTrue(self.js("document.querySelector('.sg[aria-label=\"STUDIO-PC\"] .sg__more').open"))
        self.assertTrue(self.visible(".src[data-id='13']"))

    def test_removing_a_computer_asks_first_and_forgets_its_shares(self) -> None:
        """§15.8: 'Fjern <HOST> og glem dens N delte mapper?' before DELETE /api/hosts."""
        self.open(ready="document.querySelectorAll('.src').length >= 12", panel="settings")
        grafik = self.GRAFIK
        confirm_shown = f"!document.querySelector({json.dumps(grafik + ' .sg__confirm')}).hidden"
        self.page.click(f"{grafik} [data-remove-host]")
        self.wait(confirm_shown)
        self.assertEqual(self.text(f"{grafik} .sg__confirm-text"), "Fjern GRAFIK-PC og glem dens 3 delte mapper?")
        self.assertFalse(self.requests("/api/hosts", "DELETE"))
        self.page.click(f"{grafik} [data-cancel-remove-host]")
        self.wait(f"!({confirm_shown})")
        time.sleep(0.2)
        self.assertFalse(self.requests("/api/hosts", "DELETE"))
        self.page.click(f"{grafik} [data-remove-host]")
        self.wait(confirm_shown)
        self.page.click(f"{grafik} [data-confirm-remove-host]")
        self.assertEqual(self.wait_request("/api/hosts", "DELETE")["body"], {"name": "GRAFIK-PC"})
        self.wait(f"!document.querySelector({json.dumps(grafik)})")
        self.assertEqual(self.text("#toast .toast__body div"), "GRAFIK-PC er fjernet, og dens 3 delte mapper er glemt")

    def test_the_toast_counts_the_shares_the_server_forgot(self) -> None:
        """§15.12: the toast follows DELETE /api/hosts' "forgotten", not the question's count."""
        self.server.backend.sources[10].mapped = True  # also reached via a mapped drive: kept
        self.open(ready="document.querySelectorAll('.src').length >= 12", panel="settings")
        grafik = self.GRAFIK
        self.page.click(f"{grafik} [data-remove-host]")
        self.wait(f"!document.querySelector({json.dumps(grafik + ' .sg__confirm')}).hidden")
        self.assertEqual(self.text(f"{grafik} .sg__confirm-text"), "Fjern GRAFIK-PC og glem dens 3 delte mapper?")
        self.page.click(f"{grafik} [data-confirm-remove-host]")
        self.wait("document.querySelector('#toast .toast__body div')?.textContent.startsWith('GRAFIK-PC er fjernet')")
        self.assertEqual(self.text("#toast .toast__body div"), "GRAFIK-PC er fjernet, og dens 2 delte mapper er glemt")
        self.wait(f"document.querySelectorAll({json.dumps(grafik + ' .src')}).length === 1")  # the mapped share
        self.assertTrue(self.js(f"document.querySelector({json.dumps(grafik + ' [data-remove-host]')}).hidden"))

    def test_a_refused_removal_shows_the_reason_instead_of_the_question(self) -> None:
        """§15.12: an added folder lies on the computer – the server's reason, [OK], nothing forgotten."""
        self.control("/api/roots", {"path": "\\\\GRAFIK-PC\\Arkiv"})
        self.open(ready="document.querySelectorAll('.src').length >= 13", panel="settings")
        grafik = self.GRAFIK
        self.focus_page()
        self.page.click(f"{grafik} [data-remove-host]")
        self.wait("document.activeElement.dataset.confirmRemoveHost === 'GRAFIK-PC'")
        self.page.key("Enter")  # confirms
        self.wait_request("/api/hosts", "DELETE")
        reason = "Mappen ‘\\\\GRAFIK-PC\\Arkiv’ ligger på GRAFIK-PC – fjern den først"
        self.wait(f"document.querySelector({json.dumps(grafik + ' .sg__confirm-text')}).textContent === {json.dumps(reason)}")
        self.assertTrue(self.visible(f"{grafik} .sg__confirm"))
        self.assertFalse(self.visible(f"{grafik} [data-confirm-remove-host]"))
        self.assertEqual(self.js("document.activeElement.dataset.cancelRemoveHost"), "GRAFIK-PC")
        self.assertEqual(self.text(f"{grafik} [data-cancel-remove-host]"), "OK")
        self.assertTrue(self.visible(".src[data-id='11']"), "nothing was forgotten")
        self.assertNotIn("er fjernet", self.text("#toast"))
        self.page.key("Enter")  # OK
        self.wait(f"document.querySelector({json.dumps(grafik + ' .sg__confirm')}).hidden")
        self.assertEqual(self.js("document.activeElement.dataset.removeHost"), "GRAFIK-PC")
        self.page.click(f"{grafik} [data-remove-host]")  # asked again: the question, not the old reason
        self.wait(f"!document.querySelector({json.dumps(grafik + ' .sg__confirm')}).hidden")
        self.assertEqual(self.text(f"{grafik} .sg__confirm-text"), "Fjern GRAFIK-PC og glem dens 3 delte mapper?")
        self.assertTrue(self.visible(f"{grafik} [data-confirm-remove-host]"))

    def test_keys_after_a_click_in_the_panel_never_reach_the_hidden_results(self) -> None:
        """UI2-2: ↓ scrolls the settings, Enter opens nothing from the list behind them."""
        self.open(q="klar tand")
        chosen = self.js(SELECTED)
        self.focus_page()
        self.page.key(",", ctrl=True)
        self.wait("document.querySelectorAll('.src').length >= 12")
        self.page.click(".sg .sg__name")  # plain text
        self.page.key("ArrowDown")
        self.page.key("ArrowDown")
        self.wait("document.querySelector('.settings__body').scrollTop > 0")
        self.page.key("Enter")
        self.page.key("c", ctrl=True)
        self.js("document.activeElement.blur()")  # <body> itself, the settings still open
        self.assertEqual(self.js("document.activeElement === document.body"), True)
        for key in ("ArrowDown", "PageDown", "Enter"):
            self.page.key(key)
        time.sleep(0.3)
        self.assertEqual(self.js(SELECTED), chosen)
        self.assertFalse(self.requests("/api/open"))
        self.assertEqual(self.js("window.__copied"), [], "Ctrl+C copied the hidden row")
        self.assertFalse(self.js("document.querySelector('#settings').hidden"))

    def open_settings_over_results(self, **params: str) -> str | None:
        """'klar tand' results behind the settings (Ctrl+,); returns the selected row's name."""
        self.open(q="klar tand", **params)
        chosen = self.js(SELECTED)
        self.focus_page()
        self.page.key(",", ctrl=True)
        self.wait("document.querySelectorAll('.src').length >= 12")
        return chosen

    def focus_in_settings(self, selector: str) -> None:
        """Centre ``selector`` in the settings list and focus it, as if reached with Tab."""
        self.js(f"document.querySelector({json.dumps(selector)}).scrollIntoView({{block: 'center'}})")
        self.js(f"document.querySelector({json.dumps(selector)}).focus()")
        self.assertTrue(self.js(f"document.activeElement === document.querySelector({json.dumps(selector)})"))

    def assert_no_result_key_acts(self, chosen: str | None, keys: tuple[str, ...] = ("Enter", "ArrowDown")) -> None:
        """UI2-2: from wherever the focus is now, these keys leave the hidden result list alone."""
        for key in keys:
            self.page.key(key)
        time.sleep(0.3)
        self.assertEqual(self.js(SELECTED), chosen)
        self.assertFalse(self.requests("/api/open"))
        self.assertFalse(self.js("document.querySelector('#settings').hidden"))

    def test_tab_goes_on_after_removing_a_computer_with_the_keyboard(self) -> None:
        """R3-UI-1 (UI2-2 kept): the confirmed 'Fjern' hides the focused button. No key reaches the
        hidden results, and Tab goes on to the next computer's 'Fjern' – not back to the top."""
        chosen = self.open_settings_over_results()
        group = '.sg[aria-label="KLIPPER-PC"]'
        self.focus_in_settings(f"{group} [data-remove-host]")
        self.page.key("Enter")  # asks
        self.wait("document.activeElement.dataset.confirmRemoveHost === 'KLIPPER-PC'")
        self.page.key("Enter")  # confirms
        self.assertEqual(self.wait_request("/api/hosts", "DELETE")["body"], {"name": "KLIPPER-PC"})
        self.wait(f"!document.querySelector({json.dumps(group)})")
        self.assertTrue(self.js(FOCUS_OFF_RESULTS), self.js("document.activeElement.outerHTML.slice(0, 80)"))
        self.assert_no_result_key_acts(chosen)
        scroll = self.js(SETTINGS_SCROLL)
        self.assertGreater(scroll, 0)
        self.page.key("Tab")
        self.assertEqual(self.js("document.activeElement.dataset.removeHost"), "MEDIESERVER")
        self.assertLessEqual(abs(self.js(SETTINGS_SCROLL) - scroll), 1, "the list jumped")

    def test_tab_goes_on_after_forgetting_a_folder_with_the_keyboard(self) -> None:
        """R3-UI-1 (UI2-2 kept): 'Bekræft' removes its own row – Tab goes on to the control after it
        (the last computer's row is gone: the 'Computernavn' field right below the list), not to
        'Scan alle nu' at the top, and the list keeps its place."""
        chosen = self.open_settings_over_results(mock="asked,idle,nofocus,host-offline")
        row = ".src[data-id='12']"  # MEDIESERVER does not answer: 'Efterår 2021' can be forgotten
        button = f"{row} [data-source-action]"
        self.focus_in_settings(button)
        self.page.key("Enter")  # Glem
        self.wait(f"document.querySelector({json.dumps(button)}).textContent === 'Bekræft'")
        self.page.key("Enter")  # Bekræft
        self.wait_request("/api/sources/12/forget")
        self.wait(f"!document.querySelector({json.dumps(row)})")
        self.assertTrue(self.js(FOCUS_OFF_RESULTS))
        self.assert_no_result_key_acts(chosen)
        scroll = self.js(SETTINGS_SCROLL)
        self.assertGreater(scroll, 0)
        self.page.key("Tab")
        self.assertEqual(self.js("document.activeElement.id"), "host-input")
        self.assertLessEqual(abs(self.js(SETTINGS_SCROLL) - scroll), 1, "the list jumped")

    def test_tab_after_removing_an_added_folder_goes_to_the_path_field(self) -> None:
        """R3-UI-1: 'Fjern' on an added folder – Tab lands in the 'Sti' field right below, ready for
        the next folder."""
        self.control("/api/roots", {"path": "D:\\Arkiv"})
        self.open(ready="document.querySelector('[data-remove-root]') && document.querySelectorAll('.src').length >= 13",
                  panel="settings")
        self.focus_page()
        self.focus_in_settings("[data-remove-root]")
        self.page.key("Enter")
        self.assertEqual(self.wait_request("/api/roots", "DELETE")["body"], {"path": "D:\\Arkiv"})
        self.wait("!document.querySelector('[data-remove-root]')")
        self.assertTrue(self.js(FOCUS_OFF_RESULTS))
        self.page.key("Tab")
        self.assertEqual(self.js("document.activeElement.id"), "root-input")

    def test_tab_after_a_click_on_text_in_the_settings_goes_on_from_there(self) -> None:
        """R3-UI-1: a click on plain text focuses nothing, so Tab goes on to the next control below
        it (GRAFIK-PC's 'Fjern') and the list stays where it is."""
        chosen = self.open_settings_over_results()
        name = f"{self.GRAFIK} .sg__name"
        self.js(f"document.querySelector({json.dumps(name)}).scrollIntoView({{block: 'center'}})")
        scroll = self.js(SETTINGS_SCROLL)
        self.assertGreater(scroll, 0)
        self.page.click(name)
        self.assertTrue(self.js("document.activeElement === document.body"))
        self.page.key("Tab")
        self.assertEqual(self.js("document.activeElement.dataset.removeHost"), "GRAFIK-PC")
        self.assertLessEqual(abs(self.js(SETTINGS_SCROLL) - scroll), 1, "the list jumped")
        self.assertEqual(self.js(SELECTED), chosen)

    def test_typing_after_a_click_on_text_in_the_settings_searches(self) -> None:
        """R3-UI-1: letters typed after a click on plain text in the settings go to the search field,
        which closes the settings – none is swallowed by a focused panel."""
        self.open()
        self.focus_page()
        self.page.key(",", ctrl=True)
        self.wait("document.querySelectorAll('.src').length >= 12")
        self.page.click(f"{self.GRAFIK} .sg__name")
        for char in "lin":
            self.page.key(char)
        self.wait("document.querySelector('#q').value === 'lin' && document.querySelector('#settings').hidden")
        self.assertEqual(self.js("document.activeElement.id"), "q")
        self.wait_request("/api/search", "GET", where=lambda r: r["query"].get("q") == "lin")

    def test_window_focus_with_the_settings_open_leaves_the_hidden_results_alone(self) -> None:
        """R3-UI-2: back from another window (Alt+Tab), §12 puts the caret in the search field – with
        the settings still covering the results, ↓/PageDown/Enter/Ctrl+C there act on nothing."""
        chosen = self.open_settings_over_results()
        self.page.click(".sg .sg__name")  # plain text: the focus leaves every control
        self.js("window.dispatchEvent(new Event('blur')); window.dispatchEvent(new Event('focus'))")
        self.assertEqual(self.js("document.activeElement.id"), "q")
        self.page.key("c", ctrl=True)
        self.assert_no_result_key_acts(chosen, ("ArrowDown", "PageDown", "ArrowUp", "Enter"))
        self.page.key("End", ctrl=True)
        self.page.key("Enter", ctrl=True)
        time.sleep(0.3)
        self.assertEqual(self.js(SELECTED), chosen)
        self.assertFalse(self.requests("/api/open"))
        self.assertEqual(self.js("window.__copied"), [], "Ctrl+C copied the hidden row")
        self.assertEqual(self.text("#actions"), "EscLuk indstillinger")

    def test_a_quick_re_show_with_the_settings_open_leaves_the_hidden_results_alone(self) -> None:
        """R3-UI-2: the hotkey re-show within 30 s focuses and selects the kept query – ↓ and Enter
        still act on nothing while the settings cover the results."""
        chosen = self.open_settings_over_results()
        self.js(SIMULATE_VISIBILITY)
        self.js("__hide()")
        self.js("__advance(5000)")
        self.js("__show()")
        self.assertEqual(self.js("document.activeElement.id"), "q")
        selection = "(() => { const q = document.querySelector('#q'); return [q.value, q.selectionStart, q.selectionEnd]; })()"
        self.assertEqual(self.js(selection), ["klar tand", 0, 9])
        self.assert_no_result_key_acts(chosen, ("ArrowDown", "PageDown", "Enter"))

    def test_tab_never_reaches_what_the_settings_cover(self) -> None:
        """R3-UI-2: the filters, the Resolve bar and the list under the open settings are inert –
        Shift+Tab from the tabs goes to the header, never to a hidden button such as 'Åbn mappe'."""
        self.open(ready=ROWS + " && document.querySelector('[data-resolve=open]')", q="klar tand")
        self.focus_page()
        self.page.key(",", ctrl=True)
        self.wait("document.querySelectorAll('.src').length >= 12")
        self.assertEqual(self.js("document.activeElement.id"), "tab-placeringer")
        self.page.key("Tab", shift=True)
        self.assertEqual(self.js("document.activeElement.id"), "settings-button")
        self.page.key("Tab")
        self.assertEqual(self.js("document.activeElement.id"), "tab-placeringer")
        self.page.key("Escape")  # closed again: the filters are back in the Tab order
        self.wait("document.querySelector('#settings').hidden && document.activeElement.id === 'q'")
        for _ in range(3):  # #pill, #settings-button, the kind chips
            self.page.key("Tab")
        self.assertEqual(self.js("document.activeElement.dataset.kind"), "all")
        self.assertFalse(self.requests("/api/open"))

    def test_result_keys_work_again_once_the_settings_are_closed(self) -> None:
        """R3-UI-2 control: Esc closes the settings, typing in the search field closes them too –
        then ↓ and Enter act on the results as before."""
        self.open_settings_over_results()
        self.js("window.dispatchEvent(new Event('focus'))")  # the caret in the search field
        self.assertEqual(self.js("document.activeElement.id"), "q")
        self.page.key("Escape")
        self.wait("document.querySelector('#settings').hidden && document.activeElement.id === 'q'")
        names = self.names()
        self.page.key("ArrowDown")
        self.assertEqual(self.js(SELECTED), names[1])
        self.page.key("Enter")
        self.assertEqual(self.wait_request("/api/open")["body"]["path"], self.item_path(names[1]))
        time.sleep(0.3)  # the mock answers after 120 ms; a second open while one is pending is ignored
        self.page.key(",", ctrl=True)
        self.wait("!document.querySelector('#settings').hidden")
        self.js("window.dispatchEvent(new Event('focus'))")
        self.page.key("a", ctrl=True)  # selects the query (no result key)
        self.page.type("lindholm")
        self.wait("document.querySelector('#settings').hidden && document.querySelector('#q').value === 'lindholm'")
        self.wait("document.querySelector('#results .row .row__name')?.textContent === 'Rikke Lindholm'")
        self.page.key("Enter")
        self.assertEqual(self.wait_request("/api/open", count=2)["body"],
                         {"path": "C:\\Kunder 2026 (STUDIO)\\Rikke Lindholm", "action": "folder"})
        self.assertEqual(len(self.requests("/api/open")), 2)

    def test_shortcut_opens_and_escape_closes(self) -> None:
        self.open(q="lindholm")
        self.page.key(",", ctrl=True)
        self.wait("!document.querySelector('#settings').hidden")
        self.page.key("Escape")
        self.wait("document.querySelector('#settings').hidden && document.activeElement.id === 'q'")
        self.assertEqual(self.js("document.querySelector('#q').value"), "lindholm")

    def test_tray_settings_focus_event_opens_the_panel(self) -> None:
        self.open(mock="asked,nofocus")
        self.control("/api/_mock/publish", {"type": "focus", "data": {"from_app": None, "reason": "tray"}})
        time.sleep(0.3)
        self.assertTrue(self.js("document.querySelector('#settings').hidden"))
        self.control("/api/_mock/publish", {"type": "focus", "data": {"from_app": None, "reason": "tray",
                                                                      "panel": "settings"}})
        self.wait("!document.querySelector('#settings').hidden")

    def test_general_and_resolve_controls_post_settings(self) -> None:
        self.open(ready="!document.querySelector('#panel-generelt').hidden && document.querySelector('#hotkey-input').value",
                  panel="settings", tab="generelt")
        self.page.click("[data-setting=hide_after_open]")
        self.assertEqual(self.wait_request("/api/settings")["body"], {"hide_after_open": False})
        self.page.click("[data-setting=run_at_login]")
        self.assertEqual(self.wait_request("/api/settings", count=2)["body"], {"run_at_login": False})
        self.js("document.querySelector('#hotkey-input').value = 'ctrl+'")
        self.page.click("#hotkey-form button[type=submit]")
        self.wait("!document.querySelector('#hotkey-error').hidden")
        self.assertTrue(self.text("#hotkey-error").startswith("Ugyldig genvejstast"))
        self.page.click("#passthrough-switch")
        body = self.wait_request("/api/settings", count=4)["body"]
        self.assertEqual(body["hotkey_passthrough_apps"], ["Resolve.exe", "Fusion.exe"])
        self.page.click("[data-tab=resolve]")
        self.page.click("[data-follow=open]")
        self.assertEqual(self.wait_request("/api/settings", count=5)["body"], {"resolve_follow": "open"})
        self.wait("document.querySelector('[data-follow=open]').getAttribute('aria-checked') === 'true'")

    def test_theme_is_dark_by_default_and_follows_the_setting(self) -> None:
        # Dark even when Windows is light: the default "theme" setting is "dark".
        self.page.color_scheme("light")
        checked = "document.querySelector('[data-theme-choice={}]').getAttribute('aria-checked') === 'true'"
        self.open(ready="!document.querySelector('#panel-generelt').hidden && " + checked.format("dark"),
                  panel="settings", tab="generelt")
        self.assertIsNone(self.js("document.documentElement.dataset.theme ?? null"))
        self.assertEqual(self.js("document.querySelector('#theme-color').content"), "#1a1918")

        self.page.click("[data-theme-choice=light]")
        self.assertEqual(self.wait_request("/api/settings")["body"], {"theme": "light"})
        self.wait("document.documentElement.dataset.theme === 'light' && " + checked.format("light"))
        self.assertEqual(self.js("document.querySelector('#theme-color').content"), "#fbfaf9")
        # Remembered for the next start, so the page is painted light before the settings load.
        self.assertEqual(self.js("localStorage.getItem('projektsog.theme')"), "light")
        self.open(ready="document.readyState !== 'loading'")
        self.assertEqual(self.js("document.documentElement.dataset.theme"), "light")

        # "Følg Windows" switches along with the system scheme.
        self.open(ready=checked.format("light"), panel="settings", tab="generelt")
        self.page.click("[data-theme-choice=system]")
        self.assertEqual(self.wait_request("/api/settings", count=2)["body"], {"theme": "system"})
        self.wait("document.documentElement.dataset.theme === 'light'")
        self.page.color_scheme("dark")
        self.wait("!document.documentElement.dataset.theme")

        self.page.click("[data-theme-choice=dark]")
        self.assertEqual(self.wait_request("/api/settings", count=3)["body"], {"theme": "dark"})
        self.page.color_scheme("light")
        time.sleep(0.2)
        self.assertIsNone(self.js("document.documentElement.dataset.theme ?? null"))

    def test_locations_modes_forget_hosts_and_roots(self) -> None:
        self.open(ready="document.querySelectorAll('.src').length >= 12", panel="settings")
        self.page.click(f"{self.GRAFIK} .sg__more-head")  # "Ikke medtaget (1)"
        self.wait("document.querySelector('.src[data-id=\"13\"] select').checkVisibility()")
        self.js("const s = document.querySelector('.src[data-id=\"13\"] select'); s.value = 'include';"
                "s.dispatchEvent(new Event('change', {bubbles: true}))")
        self.assertEqual(self.wait_request("/api/sources/13/mode")["body"], {"mode": "include"})
        self.page.click(".src[data-id='3'] [data-source-action]")
        self.wait("document.querySelector(\".src[data-id='3'] [data-source-action]\").textContent === 'Bekræft'")
        self.page.click(".src[data-id='3'] [data-source-action]")
        self.wait_request("/api/sources/3/forget")
        self.wait("!document.querySelector(\".src[data-id='3']\")")
        self.js("document.querySelector('#host-input').value = 'nypc'")
        self.page.click("#host-form button")
        self.assertEqual(self.wait_request("/api/hosts")["body"], {"name": "nypc"})
        self.wait("document.querySelector('.sg[aria-label=NYPC]')")
        self.js("document.querySelector('#root-input').value = 'Arkiv'")
        self.page.click("#root-form button")
        self.wait("!document.querySelector('#root-error').hidden")
        self.js("document.querySelector('#root-input').value = 'D:\\\\Arkiv'")
        self.page.click("#root-form button")
        self.wait("[...document.querySelectorAll('#root-list li span')].some(s => s.textContent === 'D:\\\\Arkiv')")


class LayoutTests(UiCase):
    def test_narrow_window_has_no_horizontal_overflow(self) -> None:
        self.page.set_viewport(700, 700)
        self.open(q="bøgely")
        self.assertLessEqual(self.js("document.documentElement.scrollWidth"), 700)
        for selector in ("#pill", "#settings-button", "#location", ".resolve__actions"):
            right = self.js(f"document.querySelector({json.dumps(selector)}).getBoundingClientRect().right")
            self.assertLessEqual(right, 700, selector)
        self.page.key(",", ctrl=True)
        self.wait("document.querySelectorAll('.src').length > 5")
        self.assertLessEqual(self.js("document.querySelector('.settings__body').scrollWidth"), 700)


if __name__ == "__main__":
    unittest.main()
