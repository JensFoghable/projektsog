"""The fun in the office (SPEC §23) in the search window and in Klippet <år> – headless Edge against
the mock backend (skipped without Edge; PROJEKTSOG_SKIP_UI_BROWSER=1 skips it too).

Headless Edge shows no window and uses a throwaway profile; nothing on the user's desktop is touched.
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
SLIDE = "(window.__aar ? window.__aar.state.index : -1)"
ACTIVE_KEY = "document.querySelector('.slide.is-active')?.dataset.key ?? null"
ROD = {"aktiv": True, "projekt": "Rikke Lindholm - Testimonial", "mappe": "C:\\Kunder 2026 (STUDIO)\\Rikke Lindholm",
       "antal": 4, "niveau": "beskidt", "mere": 0, "bad": False, "filer": [
           {"navn": "C0012.MP4", "forslag": "Klip", "ny": False, "slags": "video"},
           {"navn": "Speak_Rikke.wav", "forslag": "Speak", "ny": False, "slags": "lyd"},
           {"navn": "Font-Bold.otf", "forslag": "Font", "ny": True, "slags": "font"},
           {"navn": "gammelt.zip", "forslag": None, "ny": False, "slags": "andet"}]}

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


class FunCase(unittest.TestCase):
    scenario = "asked"

    def setUp(self) -> None:
        assert _page is not None
        self.page = _page
        self.server = MockServer(scenario=self.scenario).start()
        self.page.set_viewport(1180, 780)
        self.page.color_scheme("dark")
        self.reduced_motion(False)

    def tearDown(self) -> None:
        self.page.navigate("about:blank")
        self.reduced_motion(False)
        self.server.stop()
        self.assertEqual(self.server.backend.errors, [], "mock backend raised")

    # -- helpers -----------------------------------------------------------------------
    def open(self, path: str = "", ready: str = ROWS, **params: str) -> None:
        query = f"?{urllib.parse.urlencode(params)}" if params else ""
        self.page.navigate(self.server.url + path + query)
        self.page.wait_for(ready)

    def js(self, expression: str) -> Any:
        return self.page.evaluate(expression)

    def wait(self, expression: str, timeout: float = 6.0) -> Any:
        return self.page.wait_for(expression, timeout)

    def text(self, selector: str) -> str:
        return self.js(f"document.querySelector({json.dumps(selector)})?.textContent ?? ''")

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

    def get(self, path: str) -> Any:
        with urllib.request.urlopen(self.server.url.rstrip("/") + path, timeout=5) as response:
            return json.loads(response.read())

    def settings(self, **changes: Any) -> None:
        self.control("/api/settings", changes)

    def reduced_motion(self, on: bool) -> None:
        self.page.call("Emulation.setEmulatedMedia", {"features": [
            {"name": "prefers-reduced-motion", "value": "reduce" if on else "no-preference"}]})

    def type_query(self, text: str) -> None:
        self.page.click("#q")
        self.js("document.querySelector('#q').select()")
        self.page.type(text)


class FunSettingsTests(FunCase):
    def test_every_fun_thing_has_a_switch_and_fyraften_a_choice(self) -> None:
        self.open(ready="document.querySelector('#fun-title')", panel="settings", tab="generelt")
        keys = ["widget_saeson", "widget_dans", "widget_bad", "widget_frokost", "widget_post", "widget_vaeddemaal",
                "widget_renderspil", "widget_aarsrapport", "search_paaskeaeg", "search_minder"]
        self.wait("document.querySelector('[data-setting=\"widget_dans\"]').getAttribute('aria-checked') === 'true'")
        box = "document.querySelector('#fun-title').closest('.box')"
        found = self.js(f"[...{box}.querySelectorAll('[data-setting]')].map(n => [n.dataset.setting, n.getAttribute('aria-checked')])")
        self.assertEqual(found, [[key, "true"] for key in keys])
        self.assertEqual(self.js("document.querySelector('#pet-fyraften').value"), "16:00")
        # A change made elsewhere shows here – checked before the page saves anything itself, so a late
        # answer to its own save can never put the old value back meanwhile.
        self.settings(widget_fyraften="17:30")
        self.wait("document.querySelector('#pet-fyraften').value === '17:30'")
        self.page.click("[data-setting='widget_dans']")
        self.wait_request("/api/settings", where=lambda r: r["body"] == {"widget_dans": False})
        self.wait("document.querySelector('[data-setting=\"widget_dans\"]').getAttribute('aria-checked') === 'false'")
        # Natte-Klippe off ("Fra")
        self.js("(() => { const s = document.querySelector('#pet-fyraften'); s.value = '';"
                " s.dispatchEvent(new Event('change', { bubbles: true })); })()")
        self.wait_request("/api/settings", where=lambda r: r["body"] == {"widget_fyraften": ""})
        self.assertEqual(self.server.backend.settings["widget_fyraften"], "")


class EasterEggTests(FunCase):
    def test_kaffe_tells_klippe_once_a_minute_and_the_search_still_runs(self) -> None:
        self.settings(widget_enabled=True)
        self.open()
        self.type_query("kaffe")
        request = self.wait_request("/api/paaskeaeg")
        self.assertEqual(request["body"], {"ord": "kaffe"})
        self.wait("document.querySelector('#toast:not([hidden])')?.textContent.includes('☕ Klippe kommer med kaffe!')")
        self.assertTrue(any(r["query"].get("q") == "kaffe" for r in self.requests("/api/search", "GET")))
        self.type_query("Kaffe!")          # the same egg again within the minute: Klippe is not asked twice
        time.sleep(1.2)
        self.assertEqual(len(self.requests("/api/paaskeaeg", "POST")), 1)
        self.type_query("Hjælp!")
        self.assertEqual(self.wait_request("/api/paaskeaeg", count=2)["body"], {"ord": "hjaelp"})

    def test_the_window_has_its_own_effects(self) -> None:
        self.open()
        self.type_query("fredag")
        self.wait("document.querySelector('canvas.egg-canvas')")
        self.type_query("deadline")
        self.wait("document.querySelector('.search.is-shaking')")
        self.type_query("festkat")
        self.wait("document.querySelector('.egg-cat .egg-cat__figure')")
        self.assertEqual([r["body"]["ord"] for r in self.requests("/api/paaskeaeg", "POST")], ["fredag", "deadline", "festkat"])
        # Klippe is off: an egg Klippe acts out shows no toast in the window
        self.type_query("durum")
        self.wait_request("/api/paaskeaeg", count=4)
        time.sleep(0.3)
        self.assertFalse(self.js("!!document.querySelector('#toast:not([hidden])')?.textContent.includes('savler')"))

    def test_reduced_motion_says_it_in_words(self) -> None:
        self.reduced_motion(True)
        self.open()
        self.type_query("fredag")
        self.wait("document.querySelector('#toast:not([hidden])')?.textContent.includes('God fredag')")
        self.assertIsNone(self.js("document.querySelector('canvas.egg-canvas')"))

    def test_no_egg_for_other_words_or_when_switched_off(self) -> None:
        self.open()
        for word in ("kaffebar", "Rikke kaffe"):
            self.type_query(word)
        time.sleep(1.2)
        self.assertEqual(self.requests("/api/paaskeaeg", "POST"), [])
        self.settings(search_paaskeaeg=False)
        self.wait("true")
        time.sleep(0.3)
        self.type_query("kaffe")
        time.sleep(1.2)
        self.assertEqual(self.requests("/api/paaskeaeg", "POST"), [])


class MemoryTests(FunCase):
    def memory(self, index: int = 4) -> dict[str, Any]:
        recent = self.get("/api/recent?limit=30")["results"]
        memory = dict(recent[index])
        memory["minde"] = {"aar_siden": 3, "dato": "2023-10-12", "antal": 112, "slags": "klip"}
        self.control("/api/_mock/set", {"key": "minder", "data": {"dato": "2026-10-12", "minder": [memory]}})
        return memory

    def test_the_memory_sits_after_resolve_and_is_never_preselected(self) -> None:
        memory = self.memory()
        self.open(ready="document.querySelector('.group--minde .row__memory')")
        self.assertEqual(self.text("#section-minde"), "📸 I dag for 3 år siden")
        self.assertEqual(self.text(".group--minde .row__memory"), "112 klip fra 12. oktober 2023")
        groups = self.js("[...document.querySelectorAll('#results .group')].map(g => g.className.replace('group group--', ''))")
        self.assertEqual(groups, ["resolve", "minde", "recent"])
        self.assertEqual(self.js(SELECTED), "Rikke Lindholm")                 # Resolve's project, not the memory
        names = self.js("[...document.querySelectorAll('#results .row .row__name')].map(n => n.textContent)")
        self.assertEqual(names.count(memory["name"]), 1)                      # not also among the recent ones
        self.page.key("ArrowDown")
        self.assertEqual(self.js(SELECTED), memory["name"])
        self.assertEqual(len(self.requests("/api/minder", "GET")), 1)

    def test_without_resolve_the_first_recent_project_is_selected(self) -> None:
        self.scenario = "asked,resolve-off"
        self.server.stop()
        self.server = MockServer(scenario=self.scenario).start()
        memory = self.memory(2)
        self.open(ready="document.querySelector('.group--minde')")
        groups = self.js("[...document.querySelectorAll('#results .group')].map(g => g.className.replace('group group--', ''))")
        self.assertEqual(groups, ["minde", "recent"])
        self.assertNotEqual(self.js(SELECTED), memory["name"])
        self.assertEqual(self.js(SELECTED), self.text(".group--recent .row .row__name"))

    def test_switched_off_the_memory_goes(self) -> None:
        self.memory()
        self.open(ready="document.querySelector('.group--minde')")
        self.settings(search_minder=False)
        self.wait("!document.querySelector('.group--minde')")
        self.settings(search_minder=True)
        self.wait("document.querySelector('.group--minde')")


class RodTests(FunCase):
    def test_line_panel_open_folder_and_the_bath(self) -> None:
        self.control("/api/_mock/set", {"key": "rod", "data": ROD})
        self.open(ready="document.querySelector('.resolve__rod')")
        self.assertEqual(self.text(".resolve__rod-text"), "4 løse filer i roden af Rikke Lindholm – Klippe er beskidt")
        self.page.click("[data-resolve='rod-open']")
        self.wait("document.querySelector('#resolve-rod')")
        self.assertEqual(self.text(".rod__title"), "Ryd op i Rikke Lindholm")
        rows = self.js("[...document.querySelectorAll('.rod__row')].map(r => [r.querySelector('.rod__name').textContent,"
                       " r.querySelector('.rod__to').textContent])")
        self.assertEqual(rows, [["C0012.MP4", "→ Klip"], ["Speak_Rikke.wav", "→ Speak"],
                                ["Font-Bold.otf", "→ Font (findes ikke endnu)"], ["gammelt.zip", "Ingen forslag"]])
        self.assertIn("flytter aldrig selv filer", self.text(".rod__hint"))
        self.page.click("[data-resolve='rod-aabn']")
        self.wait_request("/api/rod/aabn")
        # (its own toast first – else, on a busy machine, it could come after the bath's and replace it)
        self.wait("document.querySelector('#toast:not([hidden])')?.textContent.includes('åbnet i Stifinder')")
        # tidied up: the bath closes the panel and the line goes
        self.control("/api/_mock/publish", {"type": "rod", "data": {**ROD, "antal": 0, "niveau": "ren", "filer": [], "bad": True}})
        # one look at both: the toast only stays 2.8 s, a busy machine may not catch it in a second wait
        self.wait("document.querySelector('#toast:not([hidden])')?.textContent.includes('er i bad')"
                  " && !document.querySelector('.resolve__rod') && !document.querySelector('#resolve-rod')")

    def test_escape_closes_the_panel_and_the_switch_hides_the_line(self) -> None:
        self.control("/api/_mock/set", {"key": "rod", "data": ROD})
        self.open(ready="document.querySelector('.resolve__rod')")
        self.page.click("[data-resolve='rod-open']")
        self.wait("document.querySelector('#resolve-rod')")
        self.js("document.querySelector('[data-resolve=\"rod-close\"]').focus()")
        self.page.key("Escape")
        self.wait("!document.querySelector('#resolve-rod')")
        self.assertEqual(self.js("document.activeElement.id"), "q")
        self.settings(widget_bad=False)
        self.wait("!document.querySelector('.resolve__rod')")


class YearReviewTests(FunCase):
    def open_review(self) -> None:
        self.open("aar.html", ready="window.__aar && window.__aar.state.slides.length > 0 && !!document.querySelector('.slide.is-active')",
                  aar="2026")

    def test_the_story_runs_counts_up_and_can_be_shared(self) -> None:
        self.open_review()
        self.assertEqual(self.js("document.title"), "Klippet 2026")
        keys = self.js("window.__aar.state.slides.map(s => s.key)")
        self.assertEqual(keys, ["intro", "timer", "top", "top5", "sider", "musik", "rytme", "kort", "leveret", "mad",
                                "trofaeer", "kontor", "outro"])
        self.assertEqual(self.js("document.querySelectorAll('#bars .seg').length"), len(keys))
        # Klippe cheers in its own drawing (widget.html in a frame)
        self.wait("!!document.querySelector('.slide.is-active .klippe__frame')?.contentDocument?.querySelector('svg.pet')")
        self.page.key("ArrowRight")
        self.wait(f"{SLIDE} === 1")
        self.wait("document.querySelector('.slide.is-active .big__value').textContent === '312'")
        self.assertEqual(self.text(".slide.is-active .big__unit"), "timer")
        box = self.js("(() => { const r = document.querySelector('#story').getBoundingClientRect();"
                      " return [r.left, r.top, r.width, r.height]; })()")
        self.tap(box[0] + box[2] * 0.8, box[1] + box[3] * 0.5)              # right: next
        self.wait(f"{SLIDE} === 2")
        self.tap(box[0] + box[2] * 0.1, box[1] + box[3] * 0.5)              # left: back
        self.wait(f"{SLIDE} === 1")
        self.page.key("End")
        self.wait(f"{ACTIVE_KEY} === 'outro'")
        # the buttons slide in: click them once they stand still
        still = "['none', 'matrix(1, 0, 0, 1, 0, 0)'].includes(getComputedStyle(document.querySelector({})).transform)"
        self.wait(still.format("'.slide.is-active .actions'") + " && " + still.format("'.slide.is-active'"))
        self.page.click(".slide.is-active #share")                          # Klippe is off: it says so
        request = self.wait_request("/api/aar/del")
        self.assertEqual(request["body"], {"aar": 2026})
        self.wait("document.querySelector('#share-note').textContent === 'Slå Klippe til først'")
        self.assertFalse(self.js("document.querySelector('#share').disabled"))
        self.settings(widget_enabled=True)
        self.page.click(".slide.is-active #share")
        self.wait_request("/api/aar/del", count=2)
        self.wait("document.querySelector('#share').textContent === 'Delt med kontoret ✓'")
        self.assertTrue(self.js("document.querySelector('#share').disabled"))
        self.page.click(".slide.is-active #again")
        self.wait(f"{SLIDE} === 0")

    def tap(self, x: float, y: float, hold: float = 0.0) -> None:
        self.page.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
        self.page.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
        if hold:
            time.sleep(hold)
        self.page.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})

    def test_slides_run_on_their_own_and_a_hold_pauses(self) -> None:
        self.open_review()
        self.js("window.__aar.state.elapsed = 5700")
        self.wait(f"{SLIDE} === 1", timeout=3)
        self.js("window.__aar.state.elapsed = 5800")
        box = self.js("(() => { const r = document.querySelector('#story').getBoundingClientRect();"
                      " return [r.left + r.width * 0.6, r.top + r.height * 0.5]; })()")
        self.page.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": box[0], "y": box[1], "button": "left",
                                                    "clickCount": 1})
        time.sleep(0.8)
        self.assertEqual(self.js(SLIDE), 1)                                     # held: paused
        self.assertTrue(self.js("document.querySelector('#story').classList.contains('is-paused')"))
        self.page.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": box[0], "y": box[1], "button": "left",
                                                    "clickCount": 1})
        self.assertEqual(self.js(SLIDE), 1)                                     # a hold is no tap
        self.wait(f"{SLIDE} === 2", timeout=3)                                  # … and the story runs on
        self.page.key("p")
        self.js("window.__aar.state.elapsed = 5900")
        time.sleep(0.6)
        self.assertEqual(self.js(SLIDE), 2)                                     # paused with P

    def test_reduced_motion_shows_the_numbers_at_once_and_waits_for_you(self) -> None:
        self.reduced_motion(True)
        self.open_review()
        self.page.key("ArrowRight")
        self.wait(f"{SLIDE} === 1")
        self.assertEqual(self.text(".slide.is-active .big__value"), "312")
        self.js("window.__aar.state.elapsed = 9000")
        time.sleep(0.5)
        self.assertEqual(self.js(SLIDE), 1)

    def test_an_empty_year_and_an_error_with_a_retry(self) -> None:
        self.control("/api/_mock/set", {"key": "aar", "data": {"aar": 2026, "timer": 0, "klippe": {"navn": "Bobby"}}})
        self.open_review()
        self.assertEqual(self.js("window.__aar.state.slides.map(s => s.key)"), ["intro", "tom", "outro"])
        self.control("/api/_mock/set", {"key": "aar", "data": "fejl"})
        self.open("aar.html", ready="document.querySelector('.slide.is-active .title')", aar="2026")
        self.wait("document.querySelector('.slide.is-active .title').textContent === 'Klippet kunne ikke hentes'")
        self.assertEqual(self.text(".slide.is-active .sub"), "Klippet kunne ikke regnes ud lige nu")
        self.control("/api/_mock/set", {"key": "aar", "data": None})
        self.page.click(".slide.is-active .btn--primary")
        self.wait("window.__aar.state.slides.length === 13")


if __name__ == "__main__":
    unittest.main()
