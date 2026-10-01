import os
import tempfile
import unittest

from projektsog import config

_tmp: tempfile.TemporaryDirectory | None = None


def setUpModule() -> None:
    global _tmp
    _tmp = tempfile.TemporaryDirectory()
    os.environ["LOCALAPPDATA"] = _tmp.name


def tearDownModule() -> None:
    if _tmp is not None:
        _tmp.cleanup()


class ThemeSettingTests(unittest.TestCase):
    def test_dark_is_the_default(self) -> None:
        self.assertEqual(config.DEFAULTS["theme"], "dark")
        cfg = config.Config(path=os.path.join(_tmp.name, "fresh.json"))
        self.assertEqual(cfg["theme"], "dark")

    def test_valid_themes_are_saved(self) -> None:
        cfg = config.Config(path=os.path.join(_tmp.name, "themes.json"))
        for theme in ("light", "system", "dark"):
            with self.subTest(theme=theme):
                self.assertEqual(cfg.update({"theme": theme})["theme"], theme)
                self.assertEqual(config.Config(path=cfg.path)["theme"], theme)

    def test_unknown_theme_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "theme"):
            config.validate({"theme": "neon"})

    def test_an_older_config_without_theme_gets_dark(self) -> None:
        path = os.path.join(_tmp.name, "old.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"hosts": ["STUDIO-PC"], "show_offline": true}')
        cfg = config.Config(path=path)
        self.assertEqual(cfg["theme"], "dark")
        self.assertEqual(cfg["hosts"], ["STUDIO-PC"])


if __name__ == "__main__":
    unittest.main()
