"""Static checks of projektsog/web: self-contained, fixed title, consistent ids, icons and keys."""

from __future__ import annotations

import os
import re
import tempfile
import unittest

from projektsog import config

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(REPO_ROOT, "projektsog", "web")
SVG_NAMESPACE = "http://www.w3.org/2000/svg"

_tmp: tempfile.TemporaryDirectory | None = None


def setUpModule() -> None:
    global _tmp
    _tmp = tempfile.TemporaryDirectory()
    os.environ["LOCALAPPDATA"] = _tmp.name


def tearDownModule() -> None:
    if _tmp is not None:
        _tmp.cleanup()


def _read(name: str) -> str:
    with open(os.path.join(WEB_DIR, name), encoding="utf-8") as fh:
        return fh.read()


class StaticUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = _read("index.html")
        cls.js = _read("app.js")
        cls.css = _read("style.css")

    def test_title_language_and_entry_points(self) -> None:
        self.assertEqual(re.findall(r"<title>(.*?)</title>", self.html), ["Projektsøg"])
        self.assertIn('<html lang="da">', self.html)
        self.assertIn('<link rel="stylesheet" href="/style.css">', self.html)
        self.assertIn('<script src="/app.js" defer></script>', self.html)
        self.assertIn('placeholder="Søg efter projekt, mappe eller fil …"', self.html)

    def test_document_title_is_never_changed(self) -> None:
        self.assertIsNone(re.search(r"document\.title\s*=(?!=)", self.js))
        self.assertNotIn("<title", self.js)

    def test_no_external_resources(self) -> None:
        for name, text in (("index.html", self.html), ("app.js", self.js), ("style.css", self.css)):
            with self.subTest(file=name):
                urls = [u for u in re.findall(r"(?:https?:)?//[\w.-]+\.[a-z]{2,}[^\s\"')]*", text)
                        if u != SVG_NAMESPACE and not u.startswith("//www.w3.org/2000/svg")]
                self.assertEqual(urls, [])
                self.assertNotIn("@import", text)
        for target in re.findall(r"url\(([^)]*)\)", self.css):
            self.assertRegex(target.strip("'\""), r"^(data:|#)")
        for ref in re.findall(r'(?:src|href)="([^"]+)"', self.html):
            with self.subTest(ref=ref):
                self.assertRegex(ref, r"^(/[\w./-]+|#[\w-]+)$")

    def test_ids_used_by_the_script_exist(self) -> None:
        html_ids = set(re.findall(r'\sid="([\w-]+)"', self.html))
        for element_id in re.findall(r"\$\('([\w-]+)'\)", self.js):
            self.assertIn(element_id, html_ids)
        for target in re.findall(r'aria-(?:controls|labelledby|describedby)="([\w-]+)"', self.html):
            self.assertIn(target, html_ids)
        for selector_id in re.findall(r"querySelector\('#([\w-]+)", self.js):
            self.assertIn(selector_id, html_ids)

    def test_every_icon_exists_and_every_symbol_is_used(self) -> None:
        symbols = set(re.findall(r'<symbol id="i-([\w-]+)"', self.html))
        requested = set(re.findall(r'href="#i-([\w-]+)"', self.html))
        requested |= set(re.findall(r"svgIcon\('([\w-]+)'", self.js))
        requested |= set(re.findall(r"\bicon: '([\w-]+)'", self.js))
        type_icons = re.search(r"const TYPE_ICONS = \{(.*?)\};", self.js, re.S).group(1)
        requested |= set(re.findall(r":\s*'([\w-]+)'", type_icons))
        self.assertEqual(requested - symbols, set(), "icons without a symbol")
        referenced = set(re.findall(r"'([\w-]+)'", self.js)) | set(re.findall(r'href="#i-([\w-]+)"', self.html))
        self.assertEqual(symbols - referenced, set(), "symbols nobody uses")

    def test_settings_switches_map_to_config_keys(self) -> None:
        keys = set(re.findall(r'data-setting="([\w]+)"', self.html))
        self.assertEqual(keys, {"hotkey_enabled", "hide_after_open", "show_offline", "run_at_login",
                                "resolve_enabled"})
        self.assertLessEqual(keys - {"run_at_login"}, set(config.DEFAULTS))
        self.assertEqual(set(re.findall(r'data-follow="(\w+)"', self.html)), set(config.VALID_RESOLVE_FOLLOW))

    def test_spec_copy_is_present(self) -> None:
        text = self.html + self.js
        for phrase in (
            "Indstillinger", "Kun online", "Alle placeringer", "Placeringer", "Generelt", "DaVinci Resolve",
            "Automatisk", "Medtag altid", "Medtag aldrig", "Scan nu", "Glem", "Global genvejstast",
            "Lad DaVinci Resolve beholde Shift+Mellemrum (tryk to gange hurtigt for Projektsøg)",
            "Start med Windows", "Vis besked", "Åbn mappe automatisk", "Seneste projekter",
            "Muligt match", "Åbn mappe", "Opdater", "Medtag", "Luk", "Ingen resultater for",
            "DaVinci Resolve bruger selv Shift+Mellemrum til effektsøgning.",
            "Hvad skal genvejen gøre, når Resolve er aktiv?", "Åbn Projektsøg",
            "Lad Resolve beholde den (tryk to gange hurtigt for Projektsøg)",
            "indekseres stadig", "tilsluttet", "Offline – sidst set", "åbner Projektsøg overalt",
            # v2.1 (SPEC §15): folded-away folders, removing a computer, truthful new-disk card
            "Ikke medtaget (", "og glem dens", "fra listen?", "Annuller", "Ryd alle filtre",
            "Ingen mapper at medtage endnu – nye projektmapper på disken findes automatisk.",
            "disken scannes, når den er tilsluttet igen.",
            # v2.2 (SPEC §15.12): a folder gone from a disk/computer that is there
            "Mappen findes ikke længere", "som ikke findes længere", "er fjernet, og dens",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)

    def test_writes_carry_the_csrf_header_and_no_debug_leftovers(self) -> None:
        self.assertIn("init.headers['X-Projektsog'] = '1'", self.js)
        self.assertNotIn("console.log", self.js)
        self.assertNotIn("debugger", self.js)
        # Settings live in the app's config, never in browser storage. The one exception is a
        # cached copy of the theme, read before the first paint so the window never flashes.
        uses = re.findall(r"localStorage\.\w+\(([^)]*)\)", self.js)
        self.assertTrue(uses)
        for args in uses:
            self.assertTrue(args.startswith("'projektsog.theme'"), args)
        self.assertEqual(self.js.count("localStorage"), len(uses))

    def test_dark_default_with_light_override(self) -> None:
        self.assertRegex(self.css, r":root \{\s*color-scheme: dark;")
        # Light only on request (the "theme" setting, applied by app.js) – never just because
        # Windows is in light mode.
        self.assertRegex(self.css, r':root\[data-theme="light"\] \{\s*color-scheme: light;')
        self.assertNotIn("prefers-color-scheme", self.css)
        self.assertIn("@media (prefers-reduced-motion: reduce)", self.css)


if __name__ == "__main__":
    unittest.main()
