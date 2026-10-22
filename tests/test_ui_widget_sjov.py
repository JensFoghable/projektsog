"""Klippe's fun in the office (SPEC §23) in headless Edge against the mock backend (skipped without Edge).

Natte-Klippe, the dance, Halloween and trick or treat, the Christmas calendar, the bath, lunch, Klippe-post,
render bets, easter eggs, the daily memory, the render game, the year and the Prøv tab – and that what Klippe
wears and holds moves with it while it dances, nods, yawns and works. The widget's clock is
fixed with ``widget.html?nu=…`` (a Monday in October unless a test needs another moment), so the tests do not
depend on the day they run. Each test gets its own mock server – a new origin, so a fresh localStorage.
Set PROJEKTSOG_SKIP_UI_BROWSER=1 to skip.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
import urllib.request
from typing import Any

from tests._ui_browser import CDPError, Edge, Page, find_edge
from tests._ui_mock_server import MockServer

MONDAY = "2026-10-12T10:00:00"            # an ordinary Monday morning: no season, no lunch, no fyraften
SAID = ("[document.querySelector('#bubble').textContent, ...window.__klippe.state.bubbles.map(b => b.text)]"
        ".join(' | ')")
APP = "document.querySelector('#app').dataset"
SHOWN = "(() => {{ const n = document.querySelector({sel!r}); return !!n && n.checkVisibility(); }})()"
MUSIC = "{...window.__klippe.state.status, bucket: 'musik'}"     # on a music site – same project, so no jump
# One cycle of a motion, sampled `steps` + 1 times: `data` (#app data-*, null removes one) and `pulses`
# (#pet classes) are set first and put back afterwards. Every animation is paused and stepped on from where
# it was – so a hand that has got out of step with its arm shows. A fixed point of each thing (the centre
# of its own box) is followed on screen (getScreenCTM): how far it strays from where it would be if it
# were sewn onto the slate (`slate`) or onto the right arm (`hand`), how far the slate's centre travels
# (`moved`, the extent of its path) and turns (`turn`), the arm's own angle (`arm`) and the widest `gap`
# between two points [selector, x, y] – in screen px; `unit` is how many px one unit of the drawing is.
FOLLOW = r"""
((spec) => {
  const app = document.querySelector('#app'), pet = document.querySelector('#pet');
  const before = {};
  for (const [key, value] of Object.entries(spec.data)) {
    before[key] = key in app.dataset ? app.dataset[key] : null;
    if (value === null) delete app.dataset[key]; else app.dataset[key] = value;
  }
  spec.pulses.forEach((c) => pet.classList.add(c));
  const q = (s) => pet.querySelector(s);
  const shown = (n) => {
    for (let e = n; e && e !== pet; e = e.parentElement) if (getComputedStyle(e).display === 'none') return false;
    return !!n;
  };
  const screen = (node, x, y) => new DOMPoint(x, y).matrixTransform(node.getScreenCTM());
  const local = (node, p) => p.matrixTransform(node.getScreenCTM().inverse());
  const angle = (m) => Math.atan2(m.b, m.a) * 180 / Math.PI;
  const slate = q('.slate'), arm = q('.arm--right');
  const things = Object.entries(spec.things).map(([slot, sel]) => ({ slot, node: q(sel) }));
  const hidden = things.filter((t) => !shown(t.node)).map((t) => t.slot);
  const anims = document.getAnimations();
  anims.forEach((a) => a.pause());
  const start = anims.map((a) => a.currentTime || 0);
  const at = (k) => anims.forEach((a, i) => { a.currentTime = start[i] + k * spec.ms / spec.steps; });
  at(0);
  const path = [[Infinity, -Infinity], [Infinity, -Infinity]];
  for (const t of things.filter((t) => shown(t.node))) {
    const b = t.node.getBBox();
    t.ref = [b.x + b.width / 2, b.y + b.height / 2];
    const p = screen(t.node, ...t.ref);
    Object.assign(t, { onSlate: local(slate, p), onArm: local(arm, p), slate: 0, hand: 0 });
  }
  const m0 = slate.getScreenCTM();
  const out = { samples: 0, hidden, moved: 0, turn: [180, -180], arm: [180, -180], gap: 0, slate: {}, hand: {},
                unit: Math.hypot(m0.a, m0.b) };
  for (let k = 0; k <= spec.steps; k += 1) {
    at(k);
    out.samples += 1;
    const c = screen(slate, 100, 126);
    path[0] = [Math.min(path[0][0], c.x), Math.max(path[0][1], c.x)];
    path[1] = [Math.min(path[1][0], c.y), Math.max(path[1][1], c.y)];
    out.moved = Math.hypot(path[0][1] - path[0][0], path[1][1] - path[1][0]);
    const turn = angle(slate.getScreenCTM()), own = angle(slate.getScreenCTM().inverse().multiply(arm.getScreenCTM()));
    out.turn = [Math.min(out.turn[0], turn), Math.max(out.turn[1], turn)];
    out.arm = [Math.min(out.arm[0], own), Math.max(out.arm[1], own)];
    if (spec.gap) {
      const [a, b] = spec.gap.map(([sel, x, y]) => screen(q(sel), x, y));
      out.gap = Math.max(out.gap, Math.hypot(a.x - b.x, a.y - b.y));
    }
    for (const t of things.filter((t) => t.ref)) {
      const p = screen(t.node, ...t.ref), s = t.onSlate.matrixTransform(slate.getScreenCTM());
      const h = t.onArm.matrixTransform(arm.getScreenCTM());
      t.slate = Math.max(t.slate, Math.hypot(p.x - s.x, p.y - s.y));
      t.hand = Math.max(t.hand, Math.hypot(p.x - h.x, p.y - h.y));
      out.slate[t.slot] = t.slate;
      out.hand[t.slot] = t.hand;
    }
  }
  at(0);
  anims.forEach((a) => a.play());
  spec.pulses.forEach((c) => pet.classList.remove(c));
  for (const [key, value] of Object.entries(before)) {
    if (value === null) delete app.dataset[key]; else app.dataset[key] = value;
  }
  return out;
})
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


class FunCase(unittest.TestCase):
    """One mock server per test, the widget at 260×430 like the real window."""

    def setUp(self) -> None:
        assert _page is not None
        self.page = _page
        self.server = MockServer(scenario="asked").start()
        self.page.set_viewport(260, 430)
        self.page.color_scheme("dark")

    def tearDown(self) -> None:
        self.page.navigate("about:blank")
        self.server.stop()
        self.assertEqual(self.server.backend.errors, [], "mock backend raised")

    # -- helpers -----------------------------------------------------------------------
    def open_widget(self, nu: str = MONDAY, tempo: float | None = None) -> None:
        self.page.navigate(f"{self.server.url}widget.html?nu={nu}")
        self.page.wait_for(f"{APP}.mood === 'working' && window.__klippe.state.ready", timeout=8)
        if tempo is not None:
            self.js(f"window.__klippe.state.tempo = {tempo}")

    def js(self, expression: str) -> Any:
        return self.page.evaluate(expression)

    def wait(self, expression: str, timeout: float = 6.0) -> Any:
        return self.page.wait_for(expression, timeout)

    def text(self, selector: str) -> str:
        return self.js(f"document.querySelector({json.dumps(selector)})?.textContent ?? ''")

    def shown(self, selector: str) -> bool:
        return bool(self.js(SHOWN.format(sel=selector)))

    def said(self, words: str, timeout: float = 6.0) -> None:
        self.wait(f"{SAID}.includes({json.dumps(words)})", timeout)

    def control(self, path: str, body: dict[str, Any]) -> None:
        request = urllib.request.Request(self.server.url.rstrip("/") + path, data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json", "X-Projektsog": "1"})
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 200)

    def publish(self, kind: str, data: Any) -> None:
        self.control("/api/_mock/publish", {"type": kind, "data": data})

    def mock_set(self, key: str, data: Any) -> None:
        self.control("/api/_mock/set", {"key": key, "data": data})

    def requests(self, path: str, method: str | None = None) -> list[dict[str, Any]]:
        backend = self.server.backend
        with backend.lock:
            found = list(backend.requests)
        return [r for r in found if r["path"] == path and (method is None or r["method"] == method)]

    def wait_request(self, path: str, method: str = "POST", count: int = 1, timeout: float = 5.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            found = self.requests(path, method)
            if len(found) >= count:
                return found[count - 1]
            if time.monotonic() > deadline:
                self.fail(f"expected {count}× {method} {path}, got {len(found)}")
            time.sleep(0.03)

    def klippe_on(self) -> None:
        self.control("/api/settings", {"widget_enabled": True})
        self.wait("window.__klippe.state.settings.widget_enabled === true")

    def open_proev(self) -> None:
        self.page.click("#trophies")
        self.wait("!document.querySelector('#panel').hidden")
        self.page.click("[data-panel-tab=proev]")
        self.wait(SHOWN.format(sel="#panel-proev"))


class NatteKlippeTests(FunCase):
    def test_fyraften_at_16_yawns_and_puts_the_nightcap_on(self) -> None:
        self.open_widget(nu="2026-10-12T16:05:00")
        self.assertEqual(self.js(f"{APP}.nat"), "gaber")
        self.said("Klokken er 16 – fyraften! 🏠🥱")
        self.assertEqual(self.text("#mood"), "🥱 Det er fyraften – snart godnat")      # the mock records Color
        self.assertTrue(self.shown("#pet .hat--nathue"))
        # Switched off: Klippe is awake again at once.
        self.control("/api/settings", {"widget_fyraften": ""})
        self.wait(f"!('nat' in {APP})")
        self.assertEqual(self.text("#mood"), "Gør Testimonial v3 smuk i Color 😎")

    def test_asleep_after_45_minutes_and_a_click_wakes_it(self) -> None:
        self.open_widget(nu="2026-10-12T17:05:00")
        self.assertEqual(self.js(f"{APP}.nat"), "sover")
        self.assertEqual(self.text("#mood"), "💤 Klippe sover – fyraften var kl. 16. Gå hjem! 🏠")
        time.sleep(0.6)
        self.assertNotIn("fyraften! 🏠🥱", self.js(SAID))                            # asleep already: no line
        self.page.click("#pet")
        self.wait(f"{APP}.nat === 'gaber'")
        self.said("Hva'? 😴 … Er det ikke fyraften?")
        # Fyraften at 16:30: at 17:05 it only dozes.
        self.control("/api/settings", {"widget_fyraften": "16:30"})
        self.wait("window.__klippe.state.settings.widget_fyraften === '16:30'")
        self.assertEqual(self.js(f"{APP}.nat"), "doeser")

    def test_the_demo_yawns_dozes_and_sleeps(self) -> None:
        self.open_widget(tempo=0.3)
        self.assertFalse(self.js(f"'nat' in {APP}"))
        self.open_proev()
        self.page.click("#demo-nat")
        self.wait(f"{APP}.nat === 'gaber'")
        self.wait(f"{APP}.nat === 'doeser'", timeout=4)
        self.wait(f"{APP}.nat === 'sover'", timeout=4)
        self.wait(f"!('nat' in {APP})", timeout=4)


class DanceTests(FunCase):
    def test_klippe_dances_on_music_sites(self) -> None:
        self.open_widget()
        music = "{state: 'recording', enabled: true, project: 'X', bucket: 'musik', today_s: 100}"
        self.js(f"window.__klippe.applyStatus({music})")
        self.assertTrue(self.js(f"'dans' in {APP}"))
        self.assertEqual(self.js(f"{APP}.outfit"), "audio")                          # headphones on
        self.wait(f"['groove', 'Skru op', 'med i filmen'].some(w => {SAID}.includes(w))", timeout=2)
        self.js("window.__klippe.applyStatus({state: 'recording', enabled: true, project: 'X', bucket: 'edit', today_s: 100})")
        self.assertFalse(self.js(f"'dans' in {APP}"))
        self.control("/api/settings", {"widget_dans": False})
        self.wait("window.__klippe.state.settings.widget_dans === false")
        self.js(f"window.__klippe.applyStatus({music})")
        self.assertFalse(self.js(f"'dans' in {APP}"))


class HalloweenTests(FunCase):
    SAESON = {"saeson": "halloween", "kostume": "spoegelse", "kostume_aar": "spoegelse", "ballade": None, "bytte": None}
    GUEST = {"type": "slik", "id": "KLIPPER-PC:7", "pc": "KLIPPER-PC", "navn": "Bobby", "stage": "junior",
             "outfit": "none", "pynt": {"hat": "festhat"}, "kostume": "vampyr", "til_s": 25}

    def test_costume_pumpkin_and_a_treat(self) -> None:
        self.mock_set("saeson", self.SAESON)
        self.open_widget(nu="2026-10-28T11:00:00", tempo=0.3)                  # a Wednesday in Halloween week
        self.assertEqual(self.js(f"[{APP}.saeson, {APP}.kostume]"), ["halloween", "spoegelse"])
        self.assertTrue(self.shown("#pet .kostume--spoegelse"))
        self.assertTrue(self.shown("#pet .graeskar-lygte"))
        self.said("Bøh! 🎃 Glædelig Halloween")
        # Trick or treat: the guest in its costume asks; a Booster makes it happy.
        self.publish("besoeg", self.GUEST)
        guest = "document.querySelector('body > .gaest')"
        self.wait(f"!!{guest} && {guest}.dataset.kostume === 'vampyr'")
        self.wait(SHOWN.format(sel="#slik"))
        self.assertEqual(self.text("#slik-tekst"), "🎃 Bobby fra KLIPPER-PC vil have slik!")
        self.assertIn("ballade om", self.text("#slik-tid"))
        self.page.click("#slik-giv")
        self.assertEqual(self.wait_request("/api/kontor/slik")["body"], {"id": "KLIPPER-PC:7", "svar": "slik"})
        self.wait("document.querySelector('.gaest__boble')?.textContent === 'Tak! 🍬'")
        self.said("Glædelig Halloween! 🎃")
        self.wait(f"!{guest}", timeout=4)
        self.assertFalse(self.js(f"'ballade' in {APP}"))

    def test_no_booster_in_time_means_ballade(self) -> None:
        self.mock_set("saeson", self.SAESON)
        self.open_widget(nu="2026-10-28T11:00:00", tempo=0.12)
        self.publish("besoeg", {**self.GUEST, "id": "KLIPPER-PC:8"})
        self.wait(SHOWN.format(sel="#slik"))
        body = self.wait_request("/api/kontor/slik", timeout=8)["body"]               # 25 s × 0.12
        self.assertEqual(body, {"id": "KLIPPER-PC:8", "svar": "ballade"})
        self.wait(f"{APP}.ballade === 'toiletpapir'")                                 # the mock's Klippe has no hat
        self.said("pakkede mig ind i toiletpapir! 🧻")
        self.assertTrue(self.shown("#pet .toiletpapir"))

    def test_nobody_home_no_prank(self) -> None:
        self.mock_set("saeson", self.SAESON)
        self.open_widget(nu="2026-10-28T11:00:00", tempo=0.2)
        self.js("window.__klippe.applyStatus({state: 'off', enabled: true, today_s: 100})")
        self.publish("besoeg", {**self.GUEST, "id": "KLIPPER-PC:9"})
        self.wait("document.querySelector('.gaest__boble')?.textContent === 'Her er ingen hjemme … 👻'", timeout=5)
        self.wait("!document.querySelector('body > .gaest')", timeout=5)
        self.assertEqual(self.requests("/api/kontor/slik"), [])
        self.assertFalse(self.shown("#slik"))

    def test_the_trick_or_treater_gets_a_booster_or_a_hat(self) -> None:
        self.open_widget()
        self.publish("slik_svar", {"type": "godbid", "pc": "GRAFIK-PC", "navn": "Lise"})
        self.said("🍬 Lise (GRAFIK-PC) gav mig en Booster!")
        self.publish("slik_svar", {"type": "ballade", "pc": "GRAFIK-PC", "navn": "Lise", "hat": "festhat"})
        self.said("😈 Hehe – jeg fik Lises festhat!")
        # The hat it won is worn till midnight (pet_saeson), over its own.
        self.publish("pet_saeson", {**self.SAESON, "saeson": None, "kostume": None,
                                    "bytte": {"hat": "festhat", "fra_pc": "GRAFIK-PC", "fra_navn": "Lise",
                                              "til": time.time() + 3600}})
        self.wait(f"{APP}.hat === 'festhat'")

    def test_the_halloween_demo(self) -> None:
        self.open_widget(tempo=0.3)
        self.klippe_on()
        self.open_proev()
        self.page.click("#demo-halloween")
        self.wait_request("/api/kontor/slik/demo")
        self.wait(f"{APP}.saeson === 'halloween' && {APP}.kostume === 'vampyr'")      # this PC's costume (mock)
        self.wait("!!document.querySelector('body > .gaest[data-kostume]')")


class ChristmasTests(FunCase):
    @staticmethod
    def calendar(dag: int = 5) -> dict[str, Any]:
        doors = []
        for nr in range(1, 25):
            opened = nr < 4
            doors.append({"nr": nr, "aaben": opened, "kan": not opened and nr <= dag,
                          "gave": {"id": "gevir", "kind": "ting", "name": "Rensdyrgevir", "item": "gevir"} if opened else None})
        return {"aar": 2026, "aktiv": True, "dag": dag, "uaabnede": sum(1 for d in doors if d["kan"]), "laager": doors}

    def test_the_calendar_in_december(self) -> None:
        self.mock_set("jul", self.calendar())
        self.open_widget(nu="2026-12-05T10:00:00", tempo=0.4)
        self.assertEqual(self.js(f"{APP}.saeson"), "jul")
        self.wait(SHOWN.format(sel="#jul-knap") + " && document.querySelector('#jul-antal').textContent === '2'")
        self.said("🎄 Dagens låge i julekalenderen venter")
        self.page.click("#jul-knap")
        self.wait("document.querySelectorAll('.kalender .laage').length === 24")
        self.assertEqual(self.js("document.querySelector('[data-panel-tab=jul]').getAttribute('aria-selected')"), "true")
        door = "document.querySelector('.laage[data-nr=\"{}\"]')"
        self.assertTrue(self.js(f"{door.format(1)}.classList.contains('is-aaben')"))
        self.assertEqual(self.js(f"{door.format(1)}.querySelector('.laage__gave').textContent"), "🦌")
        self.assertTrue(self.js(f"{door.format(4)}.classList.contains('is-klar')"))
        self.assertTrue(self.js(f"{door.format(6)}.classList.contains('is-laast') && {door.format(6)}.disabled"))
        self.assertEqual(self.js(f"{door.format(6)}.querySelector('.laage__tekst').textContent"), "I morgen")
        self.page.click(".laage[data-nr='4']")
        self.assertEqual(self.wait_request("/api/pet/jul")["body"], {"nr": 4})
        self.wait("document.querySelector('#panel').hidden", timeout=4)                # Klippe shows the gift
        self.said("Hvorfor kan nissen ikke klippe film?")                             # the mock's door 4 is a joke
        # Snow: a snowstorm in the box while it snows on the monitor.
        self.publish("sne", {"sekunder": 2, "demo": False})
        self.wait(f"'snestorm' in {APP}")
        self.wait(f"!('snestorm' in {APP})", timeout=4)

    def test_a_peek_at_the_calendar_in_october(self) -> None:
        self.open_widget(tempo=0.4)
        self.assertFalse(self.shown("#jul-knap"))
        self.open_proev()
        self.assertFalse(self.shown("[data-panel-tab=jul]"))
        self.page.click("#demo-jul")
        self.wait("document.querySelectorAll('.kalender .laage.is-klar').length === 24")
        self.assertTrue(self.shown("[data-panel-tab=jul]"))
        self.assertIn("Smugkig", self.text("#panel-body .panel__summary"))
        self.page.click(".laage[data-nr='2']")
        self.assertEqual(self.wait_request("/api/pet/jul/demo")["body"], {"nr": 2})
        self.said("🎁 Låge 2: Rensdyrgevir!", timeout=5)
        self.said("Smugkig – i december åbner du én låge om dagen")
        self.page.click("#trophies")
        self.page.click("[data-panel-tab=jul]")
        self.wait("document.querySelector('.laage[data-nr=\"2\"]')?.classList.contains('is-aaben')")
        self.assertEqual(self.requests("/api/pet/jul", "POST"), [])                     # nothing kept


class BathTests(FunCase):
    ROD = {"aktiv": True, "projekt": "Rikke Lindholm", "mappe": "C:\\Kunder 2026 (STUDIO)\\Rikke Lindholm", "antal": 7,
           "niveau": "beskidt", "filer": [], "mere": 0, "bad": False}

    def test_loose_files_make_klippe_dirty_and_tidying_up_gives_a_bath(self) -> None:
        self.mock_set("rod", self.ROD)
        self.open_widget(tempo=0.25)
        self.wait(f"{APP}.beskidt === 'beskidt'")
        self.said("Uh, der ligger 7 løse filer i roden af Rikke Lindholm – jeg bliver beskidt 🫣")
        self.assertTrue(self.shown("#pet .snavs"))
        self.publish("rod", {**self.ROD, "antal": 14, "niveau": "meget"})
        self.wait(f"{APP}.beskidt === 'meget'")
        self.publish("rod", {**self.ROD, "antal": 0, "niveau": "ren", "bad": True})
        self.wait(f"'bad' in {APP}")
        self.assertTrue(self.shown("#pet .badekar"))
        self.said("Ahh … rent! ✨ Tak, fordi du ryddede op", timeout=5)
        self.wait(f"!('bad' in {APP}) && !('beskidt' in {APP})", timeout=4)
        # Switched off: no dirt.
        self.publish("rod", self.ROD)
        self.wait(f"{APP}.beskidt === 'beskidt'")
        self.control("/api/settings", {"widget_bad": False})
        self.wait(f"!('beskidt' in {APP})")

    def test_the_bath_demo(self) -> None:
        self.open_widget()
        self.klippe_on()
        self.open_proev()
        self.page.click("#demo-bad")
        self.wait_request("/api/rod/demo")
        self.wait(f"{APP}.beskidt === 'meget'")


class LunchTests(FunCase):
    def test_the_bell_rings_and_klippe_goes_to_lunch(self) -> None:
        self.open_widget(tempo=0.25)
        self.publish("frokost", {"pc": "KLIPPER-PC", "navn": "Bobby", "selv": False})
        self.wait(f"{APP}.frokost === 'ringer'")
        self.said("🍽️ FROKOST! (Bobby ringede fra KLIPPER-PC)")
        self.wait(f"{APP}.frokost === 'loeber'", timeout=3)
        self.wait(f"{APP}.frokost === 'ude'", timeout=3)
        self.assertTrue(self.shown("#frokost-skilt"))
        self.assertEqual(self.text("#mood"), "🍽️ Klippe er til frokost – klik på skiltet, så kommer Klippe hjem")
        # A visit while it is out brings it home first.
        self.page.click("#frokost-skilt")
        self.wait(f"{APP}.frokost === 'tilbage'")
        self.said("Mæt og glad! 😋")
        self.wait(f"!('frokost' in {APP})", timeout=3)
        self.assertFalse(self.shown("#frokost-skilt"))

    def test_the_bell_button_at_lunchtime(self) -> None:
        self.open_widget()
        self.assertFalse(self.shown("#frokost-knap"))                                 # 10:00 – not yet
        self.open_widget(nu="2026-10-12T11:30:00")
        self.wait(SHOWN.format(sel="#frokost-knap"))
        self.klippe_on()
        self.page.click("#frokost-knap")
        self.wait_request("/api/kontor/frokost")
        self.wait(f"{APP}.frokost === 'ringer'")
        self.said("🔔 Du ringede til frokost!")
        self.control("/api/settings", {"widget_frokost": False})
        self.wait("!" + SHOWN.format(sel="#frokost-knap"))


class PostTests(FunCase):
    def test_sending_klippe_with_a_letter(self) -> None:
        self.control("/api/settings", {"widget_enabled": True})
        self.open_widget()
        self.wait(SHOWN.format(sel="#post-knap"))                                     # a colleague is about
        self.page.click("#post-knap")
        self.wait(SHOWN.format(sel="#post-panel"))
        self.wait("document.querySelector('.post__modtager')?.getAttribute('aria-pressed') === 'true'")
        self.assertEqual(self.text(".post__modtager"), "Bobby (KLIPPER-PC)")
        self.page.click("#post-send")
        self.wait("document.querySelector('#post-fejl')?.textContent === 'Skriv en besked'")
        self.page.click(".post__hurtig")
        self.assertEqual(self.js("document.querySelector('#post-tekst').value"), "Kan du lige kigge på det her? 👀")
        self.page.click("#post-send")
        self.assertEqual(self.wait_request("/api/kontor/post")["body"],
                         {"til": "KLIPPER-PC", "tekst": "Kan du lige kigge på det her? 👀"})
        self.wait("!" + SHOWN.format(sel="#post-panel"))
        self.wait(f"{APP}.post === 'ud'")
        self.said("✉️ Jeg løber over til Bobby med posten!")
        # "Svar" on a post that came: the panel opens with the sender chosen.
        self.publish("post_svar", {"til": "KLIPPER-PC"})
        self.wait(SHOWN.format(sel="#post-panel"))

    def test_a_post_arrives(self) -> None:
        self.open_widget(tempo=0.3)
        self.klippe_on()
        self.open_proev()
        self.page.click("#demo-post")
        self.wait_request("/api/kontor/post/demo")
        self.wait("!!document.querySelector('body > .gaest[data-post]')")
        self.wait("document.querySelector('.message__title')?.textContent === '✉️ Post fra Klippe (DEMO-PC)'")
        self.assertEqual(self.js("[...document.querySelectorAll('.message__button')].map(b => b.textContent)"), ["Svar"])
        self.assertFalse(self.js(f"'telefon' in {APP}"))                              # a post is no call


class BetTests(FunCase):
    def test_guessing_when_a_colleagues_render_is_done(self) -> None:
        self.open_widget()
        now = int(time.time())
        bet = {"pc": "GRAFIK-PC", "vid": 3, "navn": "Lise", "maskine": "GRAFIK-PC", "fil": "Portræt_v3.mp4", "pct": 12,
               "start": now - 60, "lukker": now + 170, "status": "aaben", "egen": False, "mit_gaet": None,
               "antal_gaet": 1, "resultat": None}
        self.publish("vaeddemaal", bet)
        self.wait(SHOWN.format(sel="#vaeddemaal"))
        self.assertEqual(self.text("#vm-titel"), "🎲 GRAFIK-PC renderer Portræt_v3.mp4 (12 %) – hvornår er den færdig?")
        self.assertEqual(self.js("document.querySelectorAll('.vm__valg').length"), 4)
        self.page.click(".vm__valg:nth-child(3)")
        chosen = self.js("document.querySelector('.vm__valg:nth-child(3)').textContent")
        self.assertEqual(self.js("document.querySelector('#vm-tid').value"), chosen)
        self.page.click("#vm-gaet")
        body = self.wait_request("/api/kontor/gaet")["body"]
        self.assertEqual((body["pc"], body["vid"]), ("GRAFIK-PC", 3))
        self.assertEqual(time.strftime("%H:%M", time.localtime(body["tid"])), chosen)
        self.wait(f"document.querySelector('#vm-status')?.textContent.startsWith('🎲 Dit gæt: {chosen}')")
        self.assertFalse(self.shown("#vm-gaet"))
        # It ends: we won.
        self.publish("vaeddemaal", {**bet, "status": "slut", "mit_gaet": body["tid"],
                                    "resultat": {"vinder_pc": "STUDIO-PC", "vinder_navn": "Klippe", "tid": body["tid"],
                                                 "slut": body["tid"] + 60, "mig": True}})
        self.said("Du vandt render-væddemålet! 🔮")
        self.assertIn("Du vandt", self.text("#vm-titel"))

    def test_a_bet_that_is_called_off(self) -> None:
        self.open_widget()
        now = int(time.time())
        bet = {"pc": "GRAFIK-PC", "vid": 4, "navn": "Lise", "maskine": "GRAFIK-PC", "fil": "X.mp4", "pct": 5,
               "start": now - 60, "lukker": now + 100, "status": "aflyst", "egen": False, "mit_gaet": None,
               "antal_gaet": 0, "resultat": None}
        self.publish("vaeddemaal", bet)
        self.wait("document.querySelector('#vm-titel')?.textContent === 'Renderen stoppede – væddemålet er aflyst'")
        self.control("/api/settings", {"widget_vaeddemaal": False})
        self.wait("document.querySelector('#vaeddemaal').hidden")


class EasterEggTests(FunCase):
    def test_klippe_reacts_to_the_search_field(self) -> None:
        self.open_widget()
        self.publish("paaskeaeg", {"ord": "kaffe"})
        self.wait(f"{APP}.haand === 'kaffe'")
        self.said("Værsgo! ☕")
        self.publish("paaskeaeg", {"ord": "deadline"})
        self.wait(f"'panik' in {APP}")
        self.said("DEADLINE?! 😱")
        self.publish("paaskeaeg", {"ord": "hjaelp"})
        self.wait(f"'helt' in {APP}")
        self.publish("paaskeaeg", {"ord": "sne"})
        self.wait(f"'snestorm' in {APP}")
        self.publish("paaskeaeg", {"ord": "fyraften"})
        self.wait(f"{APP}.nat === 'gaber'")
        self.wait(f"!('nat' in {APP})", timeout=7)
        self.publish("paaskeaeg", {"ord": "<script>"})                              # not an egg: nothing
        # Not while it eats: the egg waits.
        self.js("window.__klippe.state.eating = {id: 'durum', name: 'Durum', kind: 'mad'}")
        self.publish("paaskeaeg", {"ord": "klippe"})
        time.sleep(1.2)
        self.assertFalse(self.js(f"'vink' in {APP}"))
        self.js("window.__klippe.state.eating = null")
        self.wait(f"'vink' in {APP}", timeout=3)
        self.said("Det er mig! 👋")


class MemoryAndYearTests(FunCase):
    def test_a_memory_once_a_day(self) -> None:
        item = {"id": 5, "name": "Rikke Lindholm", "path": "C:\\Kunder 2026 (STUDIO)\\Rikke Lindholm",
                "minde": {"aar_siden": 3, "dato": "2023-10-12", "antal": 112, "slags": "klip"}}
        self.mock_set("minder", {"dato": "2026-10-12", "minder": [item]})
        self.open_widget()
        self.said("📸 I dag for 3 år siden: 112 klip i Rikke Lindholm", timeout=10)

    def test_klippet_in_december(self) -> None:
        self.open_widget(nu="2026-12-05T10:00:00")
        self.wait(SHOWN.format(sel="#aar-knap"))
        self.said("🎬 Dit Klippet 2026 er klar! Tryk på 🎬 øverst")
        self.page.click("#aar-knap")
        self.assertEqual(self.wait_request("/api/aar/aabn")["body"], {"aar": 2026})
        self.said("🎬 Klippet 2026 åbner i din browser")

    def test_a_colleagues_year_comes_by(self) -> None:
        self.open_widget(tempo=0.3)
        self.publish("besoeg", {"type": "aar", "pc": "KLIPPER-PC", "navn": "Bobby", "stage": "pro", "outfit": "none",
                                "pynt": {}, "aar": {"aar": 2026, "timer": 311.6, "side": "color", "projekter": 23,
                                                    "durum": 31, "kort": 38, "leveringer": 19, "trofaeer": 21}})
        self.wait("document.querySelector('.gaest__boble')?.textContent === "
                  "'Hej fra KLIPPER-PC! 📊 Mit Klippet 2026: 312 timer · mest i Color · 31 durum!'")


class RenderGameTests(FunCase):
    RENDER = {"aktiv": True, "pct": 30, "eta_s": 400, "navn": "Portræt_v3.mp4", "tidslinje": "Portræt v3",
              "projekt": "Rikke Lindholm", "af_claude": None, "faerdig": None}

    def test_only_while_resolve_renders(self) -> None:
        self.open_widget()
        self.assertFalse(self.shown("#spil-knap"))
        self.publish("render", self.RENDER)
        self.wait(SHOWN.format(sel="#spil-knap"))
        self.page.click("#spil-knap")
        self.wait(SHOWN.format(sel="#spil") + " && " + SHOWN.format(sel="#spil-canvas"))
        self.assertEqual(self.js("document.querySelector('#spil-render').style.width"), "30%")
        self.page.click("#spil-start")
        self.wait("window.__klippe.state.game?.running === true")
        self.page.key("ArrowUp")
        self.wait("window.__klippe.state.game.g.jumps >= 1")
        self.wait("window.__klippe.state.game.g.dist > 20")
        # A klippefejl right in front: the round is over and its score is sent.
        self.js("(() => { const g = window.__klippe.state.game.g; g.y = 0; g.vy = 0; g.ground = true;"
                " g.obstacles.push({kind: 'offline', x: 40, w: 30, h: 40, b: 0}); })()")
        self.wait("window.__klippe.state.game.running === false")
        body = self.wait_request("/api/pet/spil")["body"]
        self.assertEqual(body["demo"], False)
        self.assertGreater(body["score"], 0)
        self.wait("document.querySelector('#spil-besked')?.textContent.includes('point')")
        # The render ends: the game ends with it and closes.
        self.page.click("#spil-start")
        self.wait("window.__klippe.state.game?.running === true")
        self.publish("render", {**self.RENDER, "aktiv": False, "faerdig": {
            "udfald": "done", "fil": "Portræt_v3.mp4", "sti": None, "mappe": None, "levering": False, "fejl": None, "seq": 9}})
        self.wait("document.querySelector('#spil-besked')?.textContent.startsWith('Renderen er færdig! 🎬')")
        self.wait("document.querySelector('#spil').hidden", timeout=7)
        self.assertFalse(self.shown("#spil-knap"))

    def test_a_demo_round(self) -> None:
        self.open_widget()
        self.open_proev()
        self.page.click("#demo-spil")
        self.wait(SHOWN.format(sel="#spil"))
        self.assertEqual(self.text("#spil-bedste"), "Prøverunde")
        self.page.click("#spil-canvas")
        self.wait("window.__klippe.state.game?.running === true")
        self.js("(() => { const g = window.__klippe.state.game.g; g.dist = 500;"
                " g.obstacles.push({kind: 'sort', x: 40, w: 30, h: 40, b: 0}); })()")
        self.wait("window.__klippe.state.game.running === false")
        self.assertEqual(self.wait_request("/api/pet/spil")["body"]["demo"], True)
        self.assertEqual(self.text("#spil-start"), "Luk")
        self.page.click("#spil-start")
        self.wait("document.querySelector('#spil').hidden")


class ProevTabTests(FunCase):
    DEMOS = ("demo-robots", "demo-call", "demo-party", "demo-visit", "demo-halloween", "demo-jul", "demo-sne",
             "demo-frokost", "demo-vaeddemaal", "demo-spil", "demo-bad", "demo-nat", "demo-dans", "demo-aar", "demo-post")

    def test_every_demo_in_the_proev_tab(self) -> None:
        self.open_widget()
        self.page.click("#trophies")
        self.wait("!document.querySelector('#panel').hidden")
        self.assertFalse(self.shown("#panel-proev"))
        self.page.click("[data-panel-tab=proev]")
        self.wait(SHOWN.format(sel="#panel-proev"))
        self.assertTrue(self.js("document.querySelector('#panel-body').hidden"))
        for demo in self.DEMOS:
            with self.subTest(demo=demo):
                self.assertTrue(self.shown(f"#{demo}"), demo)
        self.page.click("[data-panel-tab=trophies]")
        self.wait("!document.querySelector('#panel-body').hidden && !" + SHOWN.format(sel="#panel-proev"))

    def test_snow_frokost_and_bet_demos(self) -> None:
        self.open_widget(tempo=0.25)
        self.klippe_on()
        self.open_proev()
        self.page.click("#demo-sne")
        self.wait_request("/api/sne/demo")
        self.wait(f"'snestorm' in {APP}")
        self.open_proev()
        self.page.click("#demo-frokost")
        self.wait(f"{APP}.frokost === 'ringer'")
        self.open_proev()
        self.page.click("#demo-vaeddemaal")
        self.wait(SHOWN.format(sel="#vaeddemaal"))
        self.open_proev()
        self.page.click("#demo-dans")
        self.wait(f"'dans' in {APP}")


class ReviewFixTests(FunCase):
    """What the review of the widget found (2026-10-12): stale bets and calendars, a hidden or late
    trick-or-treater, a second prank, Danish possessives, a GET that came after newer news."""

    GUEST = HalloweenTests.GUEST
    SAESON = HalloweenTests.SAESON

    def bet(self, **changes: Any) -> dict[str, Any]:
        now = int(time.time())
        return {"pc": "GRAFIK-PC", "vid": 3, "navn": "Lise", "maskine": "GRAFIK-PC", "fil": "Portræt_v3.mp4",
                "pct": 60, "start": now - 400, "lukker": now - 200, "status": "lukket", "egen": False,
                "mit_gaet": None, "antal_gaet": 2, "resultat": None, **changes}

    def test_a_bet_the_server_forgot_or_that_never_ended_goes(self) -> None:
        self.open_widget()
        self.publish("vaeddemaal", self.bet())
        self.wait(SHOWN.format(sel="#vaeddemaal"))
        # A reload of the bets (the stream came back): the server no longer lists it – it goes.
        self.mock_set("vaeddemaal", [])
        self.js("window.__klippe.loadBets()")
        self.wait("document.querySelector('#vaeddemaal').hidden")
        # A colleague's bet that closed 3 hours ago and never ended (its PC went to sleep) is not shown.
        self.publish("vaeddemaal", self.bet(vid=4, lukker=int(time.time()) - 3 * 3600 - 10))
        time.sleep(0.4)
        self.js("window.__klippe.renderBetCard()")
        self.assertTrue(self.js("document.querySelector('#vaeddemaal').hidden"))
        # The same PC and number after a restart is another bet (its start differs): both are known.
        self.publish("vaeddemaal", self.bet(vid=5, status="aaben", lukker=int(time.time()) + 100))
        self.publish("vaeddemaal", self.bet(vid=5, status="aaben", start=int(time.time()), lukker=int(time.time()) + 150))
        self.wait("[...window.__klippe.state.bets.keys()].filter(k => k.startsWith('GRAFIK-PC:5:')).length === 2")

    def test_the_calendar_follows_the_days(self) -> None:
        # Left on overnight: after midnight the calendar is asked for again (a new door).
        self.mock_set("jul", ChristmasTests.calendar(dag=1))
        self.open_widget(nu="2026-12-01T23:59:57")
        self.wait("window.__klippe.state.jul?.dag === 1")
        asked = len(self.requests("/api/pet/jul", "GET"))
        self.wait("window.__klippe.clock().getDate() === 2", timeout=8)
        self.js("window.__klippe.funTick()")
        self.wait_request("/api/pet/jul", "GET", count=asked + 1)

    def test_no_calendar_after_new_year(self) -> None:
        # Open over New Year; the calendar (loaded on 31 Dec) is over, whatever a server still says.
        self.mock_set("jul", ChristmasTests.calendar(dag=24))
        self.open_widget(nu="2026-12-31T23:59:57")
        self.wait("window.__klippe.state.jul?.aktiv === true")
        self.wait("window.__klippe.clock().getMonth() === 0", timeout=8)
        self.js("window.__klippe.funTick()")
        self.page.click("#trophies")
        self.wait("!document.querySelector('#panel').hidden")
        self.assertFalse(self.shown("[data-panel-tab=jul]"))
        self.assertFalse(self.shown("#jul-knap"))
        self.assertFalse(self.shown("#jul-antal"))

    def test_a_trick_or_treater_is_never_hidden_and_never_late(self) -> None:
        self.mock_set("saeson", self.SAESON)
        self.open_widget(nu="2026-10-28T11:00:00", tempo=0.3)
        # The 🏆 panel was left open: the guest closes it.
        self.page.click("#trophies")
        self.wait("!document.querySelector('#panel').hidden")
        self.publish("besoeg", self.GUEST)
        self.wait(SHOWN.format(sel="#slik"))
        self.assertTrue(self.js("document.querySelector('#panel').hidden"))
        self.page.click("#slik-giv")
        self.wait_request("/api/kontor/slik")
        self.wait("!document.querySelector('body > .gaest')", timeout=5)
        # A guest that waited 50 s behind a meal has gone on: no card, nothing sent, only Klippe tells.
        self.js("window.__klippe.state.eating = {id: 'durum', name: 'Durum', kind: 'mad'}")
        self.publish("besoeg", {**self.GUEST, "id": "KLIPPER-PC:12", "navn": "Lars"})
        self.wait("window.__klippe.state.visitWaiting?.id === 'KLIPPER-PC:12'")
        self.js("window.__klippe.state.visitWaitingAt -= 50000; window.__klippe.state.eating = null")
        self.said("🎃 Lars fra KLIPPER-PC var forbi efter slik, mens du havde travlt")
        self.assertFalse(self.js("!!document.querySelector('body > .gaest')"))
        self.assertEqual(len(self.requests("/api/kontor/slik")), 1)
        # One that waited 40 s gets the 15 s that are left.
        self.js("window.__klippe.state.eating = {id: 'durum', name: 'Durum', kind: 'mad'}")
        self.publish("besoeg", {**self.GUEST, "id": "KLIPPER-PC:13"})
        self.wait("window.__klippe.state.visitWaiting?.id === 'KLIPPER-PC:13'")
        self.js("window.__klippe.state.visitWaitingAt -= 40000; window.__klippe.state.eating = null")
        self.wait(SHOWN.format(sel="#slik"))
        self.assertLessEqual(int(self.text("#slik-tid").split()[-2]), 15)

    def test_the_halloween_demo_without_resolve(self) -> None:
        self.open_widget(tempo=0.3)
        self.klippe_on()
        self.js("window.__klippe.applyStatus({state: 'off', enabled: true, today_s: 100})")
        self.open_proev()
        self.page.click("#demo-halloween")
        self.wait(SHOWN.format(sel="#slik"))                                           # not "Her er ingen hjemme"

    def test_a_second_prank_keeps_the_hat_away_and_lars_gets_an_apostrophe(self) -> None:
        self.mock_set("saeson", {**self.SAESON, "ballade": {"hat": "festhat", "toiletpapir": True, "af_pc": "KLIPPER-PC",
                                                            "af_navn": "Bobby", "til": time.time() + 3600}})
        self.publish("pet_look", {"equipped": {"hat": "festhat"}})
        self.open_widget(nu="2026-10-28T11:00:00")
        self.wait(f"{APP}.ballade === 'hat toiletpapir'")
        # (checkVisibility() says true for an SVG group with display: none – ask the style)
        self.assertEqual(self.js("getComputedStyle(document.querySelector('#pet .hat--festhat')).display"), "none")
        self.assertNotEqual(self.js("getComputedStyle(document.querySelector('#pet .toiletpapir')).display"), "none")
        self.publish("slik_svar", {"type": "ballade", "pc": "GRAFIK-PC", "navn": "Lars", "hat": "festhat"})
        self.said("😈 Hehe – jeg fik Lars' festhat!")

    def test_a_get_never_overwrites_newer_news(self) -> None:
        self.mock_set("rod", {**BathTests.ROD, "antal": 3, "niveau": "lidt"})
        self.open_widget()
        self.wait(f"{APP}.beskidt === 'lidt'")
        # The GET's answer comes late; meanwhile the event says 12 files – the event wins.
        self.js("""(() => { const real = window.fetch; window.fetch = (url, opts) => {
            const answer = real(url, opts);
            return String(url).includes('/api/rod') ? answer.then((r) => new Promise((ok) => setTimeout(() => ok(r), 900)))
              : answer; }; })()""")
        self.js("window.__klippe.loadRod()")
        self.publish("rod", {**BathTests.ROD, "antal": 12, "niveau": "meget"})
        self.wait(f"{APP}.beskidt === 'meget'")
        time.sleep(1.4)
        self.assertEqual(self.js(f"{APP}.beskidt"), "meget")
        self.assertEqual(self.js("window.__klippe.state.rod.antal"), 12)

    def test_the_demo_post_is_not_kept_as_the_recipient(self) -> None:
        self.control("/api/settings", {"widget_enabled": True})
        self.open_widget(tempo=0.3)
        self.publish("post_svar", {"til": "DEMO-PC"})                                   # "Svar" on the demo's post
        self.wait(SHOWN.format(sel="#post-panel"))
        self.wait("document.querySelector('.post__modtager[aria-pressed=true]')?.dataset.pc === 'DEMO-PC'")
        self.page.click("#post-luk")
        self.wait("!" + SHOWN.format(sel="#post-panel"))
        self.page.click("#post-knap")
        self.wait(SHOWN.format(sel="#post-panel"))
        self.wait("document.querySelector('.post__modtager[aria-pressed=true]')?.dataset.pc === 'KLIPPER-PC'")
        self.assertEqual(self.js("[...document.querySelectorAll('.post__modtager')].map(b => b.dataset.pc)"), ["KLIPPER-PC"])

    def test_snow_that_cannot_fall_says_why_and_eggs_wait_for_lunch(self) -> None:
        self.open_widget(tempo=0.25)
        self.klippe_on()
        self.js("""(() => { const real = window.fetch; window.fetch = (url, opts) => String(url).includes('/api/sne/demo')
            ? Promise.resolve(new Response(JSON.stringify({ok: false, grund: 'Noget kører i fuld skærm'}),
                {headers: {'Content-Type': 'application/json'}}))
            : real(url, opts); })()""")
        self.open_proev()
        self.page.click("#demo-sne")
        self.said("Øv – Noget kører i fuld skærm")
        # Klippe is out at lunch: an easter egg waits until it is back.
        self.publish("frokost", {"pc": "KLIPPER-PC", "navn": "Bobby", "selv": False})
        self.wait(f"{APP}.frokost === 'ude'", timeout=4)
        self.publish("paaskeaeg", {"ord": "klippe"})
        time.sleep(1.2)
        self.assertFalse(self.js(f"'vink' in {APP}"))
        self.page.click("#frokost-skilt")
        self.wait(f"'vink' in {APP}", timeout=4)

    def test_the_dance_demo_after_fyraften_and_the_game_switch(self) -> None:
        self.open_widget(nu="2026-10-12T17:30:00")
        self.assertEqual(self.js(f"{APP}.nat"), "sover")
        self.open_proev()
        self.page.click("#demo-dans")
        self.wait(f"'dans' in {APP} && !('nat' in {APP})")
        # widget_renderspil off: no 🎮 and an open game closes at once.
        self.publish("render", RenderGameTests.RENDER)
        self.wait(SHOWN.format(sel="#spil-knap"))
        self.page.click("#spil-knap")
        self.wait(SHOWN.format(sel="#spil"))
        self.control("/api/settings", {"widget_renderspil": False})
        self.wait("document.querySelector('#spil').hidden && !" + SHOWN.format(sel="#spil-knap"))


class WornThingsFollowTests(FunCase):
    """Everything Klippe wears or holds moves with it (.figur, widget.css): while it dances, nods off,
    yawns and bobs at work, a point on the hat, the glasses, the headphones, the witch's hat, the scarf
    and the lollipop stays within half a pixel of where it would be if it were sewn onto the slate –
    and the cup in the hand within half a pixel of where it would be on the arm."""

    WARDROBE = {"hat": "baret", "briller": "hornbriller", "mund": "slikkepind", "hals": "halstoerklaede"}
    # what is on show at a time: the headphones hide the hat, the witch's hat hides both
    KITS = {"hovedtelefoner": {"outfit": "audio", "kostume": None},
            "hat": {"outfit": "none", "kostume": None},
            "heksehat": {"outfit": "none", "kostume": "heks"}}
    SLOT = {"hovedtelefoner": ".acc--audio", "hat": ".hat--baret", "heksehat": ".kostume--heks",
            "briller": ".briller--hornbriller", "hals": ".hals--halstoerklaede", "mund": ".mund--slikkepind"}

    def follow(self, ms: int, things: dict[str, str], data: dict[str, Any] | None = None,
               pulses: tuple[str, ...] = (), gap: list[list[Any]] | None = None, steps: int = 40) -> dict[str, Any]:
        spec = {"ms": ms, "steps": steps, "things": things, "data": data or {}, "pulses": list(pulses), "gap": gap}
        return self.js(f"{FOLLOW}({json.dumps(spec)})")

    def dress(self, haand: str) -> None:
        self.publish("pet_look", {"equipped": {**self.WARDROBE, "haand": haand}})
        self.wait(f"{APP}.haand === {json.dumps(haand)} && {APP}.hals === 'halstoerklaede'")

    def dance(self) -> None:
        """The music dance – kept going by the demo's clock, since the mock's status polls say Color."""
        self.js(f"window.__klippe.state.dansDemoUntil = Date.now() + 60e3; window.__klippe.applyStatus({MUSIC})")
        self.wait(f"'dans' in {APP} && {APP}.outfit === 'audio'")

    def assert_worn_things_follow(self, motion: str, ms: int, hand: str | None = ".haand--kaffe",
                                  data: dict[str, Any] | None = None, pulses: tuple[str, ...] = (),
                                  kits: tuple[str, ...] = tuple(KITS)) -> list[dict[str, Any]]:
        found = []
        for kit in kits:
            with self.subTest(motion=motion, kit=kit):
                things = {slot: self.SLOT[slot] for slot in (kit, "briller", "hals", "mund")}
                if hand:
                    things["haand"] = hand
                r = self.follow(ms, things, {**self.KITS[kit], **(data or {})}, pulses)
                self.assertEqual(r["hidden"], [], f"not on show during {motion}")
                self.assertGreaterEqual(r["samples"], 20)
                self.assertGreater(r["moved"], 1.5, f"Klippe does not move during {motion}")
                for slot, off in r["slate"].items():
                    if slot != "haand":
                        self.assertLessEqual(off, 0.5, f"{slot} strays {off:.2f}px from Klippe during {motion}")
                if hand:
                    self.assertLessEqual(r["hand"]["haand"], 0.5,
                                         f"{hand} strays {r['hand']['haand']:.2f}px from the hand during {motion}")
                found.append(r)
        return found

    def test_the_dance(self) -> None:
        self.open_widget()
        self.publish("pet_look", {"equipped": dict(self.WARDROBE)})
        self.wait(f"{APP}.hals === 'halstoerklaede'")
        self.dance()
        # Nothing in the hand: the arms dance as they always have (the right one from -100° to -20°).
        r = self.follow(1000, {"hat": self.SLOT["hat"]}, self.KITS["hat"])
        self.assertAlmostEqual(r["arm"][0], -100, delta=0.5)
        self.assertAlmostEqual(r["arm"][1], -20, delta=0.5)
        self.assertLessEqual(r["slate"]["hat"], 0.5)
        # A cup put in the hand mid-dance: the arm only lifts it (≤ 30°), in step with the hand at once.
        time.sleep(0.35)
        self.dress("kaffe")
        time.sleep(0.3)
        for r in self.assert_worn_things_follow("dans", 1000):
            self.assertGreater(r["turn"][1] - r["turn"][0], 9.5)                       # it sways ±5°
            self.assertTrue(-31 < r["arm"][0] < -29 and -7 < r["arm"][1] < -5, r["arm"])
        # Something else in the hand while it dances: no step lost.
        self.dress("durum")
        time.sleep(0.3)
        self.assert_worn_things_follow("dans", 1000, hand=".haand--durum", kits=("hovedtelefoner",))

    def test_a_nod_and_a_yawn(self) -> None:
        self.open_widget()
        self.dress("kaffe")
        self.assert_worn_things_follow("nikker", 1200, pulses=("nikker",))
        for r in self.assert_worn_things_follow("gaab", 1600, pulses=("gaab",)):
            self.assertTrue(-31 < r["arm"][0] < -29 and r["arm"][1] > -0.5, r["arm"])   # a gentle raise
        # Natte-Klippe's nightcap goes along too.
        for pulse, ms in (("nikker", 1200), ("gaab", 1600)):
            with self.subTest(nathue=pulse):
                r = self.follow(ms, {"nathue": ".hat--nathue", "haand": ".haand--kaffe"}, {"nat": "doeser"}, (pulse,))
                self.assertEqual(r["hidden"], [])
                self.assertLessEqual(r["slate"]["nathue"], 0.5)
                self.assertLessEqual(r["hand"]["haand"], 0.5)
        # Nothing in the hand: the big stretch, as before.
        self.dress("ingen-haand")
        r = self.follow(1600, {"hat": self.SLOT["hat"]}, self.KITS["hat"], ("gaab",))
        self.assertAlmostEqual(r["arm"][0], -115, delta=0.5)

    def test_the_working_bob(self) -> None:
        self.open_widget()                                      # working: it bobs and types
        self.dress("kaffe")
        for r in self.assert_worn_things_follow("arbejde", 900):
            self.assertTrue(r["arm"][0] > -0.5 and r["arm"][1] > 13.5, r["arm"])        # typing moves the cup
        # An energy drink: faster typing, and the cup keeps up.
        r = self.follow(400, {"haand": ".haand--kaffe"}, {"energi": "booster"})
        self.assertGreater(r["arm"][1], 13.5)
        self.assertLessEqual(r["hand"]["haand"], 0.5)

    def test_on_the_drip_the_dance_keeps_the_hand_on_the_tube(self) -> None:
        self.open_widget()
        self.js("""(() => { const now = Date.now() / 1000; window.__klippe.applyFood({...window.__klippe.state.food,
            energi: {item: 'drop', name: 'Booster-drop', til: now + 600}, drop: {fra: now - 60, til: now + 600}}); })()""")
        self.wait(f"'drop' in {APP}")
        self.dance()
        # the end of the left arm and the end of the tube (both at 34,150 in the drawing)
        r = self.follow(1000, {}, gap=[[".arm--left", 34, 150], [".drop__slange", 34, 150]])
        self.assertLess(r["turn"][1] - r["turn"][0], 0.01)                               # no turning …
        self.assertAlmostEqual(r["moved"] / r["unit"], 2, delta=0.05)                   # … a 2-unit bob to the beat
        self.assertLessEqual(r["gap"] / r["unit"], 2.05, "the left hand leaves the drip's tube")


if __name__ == "__main__":
    unittest.main()
