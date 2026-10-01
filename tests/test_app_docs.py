"""README.md and the installer's own instructions work as written (DOC-1)."""

import os
import re
import tempfile
import unittest

from tests import _app_fakes as fakes

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
app = None      # projektsog.app, imported once LOCALAPPDATA points to a temp dir
_saved_env: dict[str, str | None] = {}
_tmp: tempfile.TemporaryDirectory | None = None


def setUpModule() -> None:
    global app, _tmp
    _tmp = tempfile.TemporaryDirectory()
    _saved_env["LOCALAPPDATA"] = os.environ.get("LOCALAPPDATA")
    os.environ["LOCALAPPDATA"] = _tmp.name
    app = fakes.import_app()


def tearDownModule() -> None:
    if _saved_env.get("LOCALAPPDATA") is None:
        os.environ.pop("LOCALAPPDATA", None)
    else:
        os.environ["LOCALAPPDATA"] = _saved_env["LOCALAPPDATA"]
    _tmp.cleanup()


def _read(name: str) -> str:
    with open(os.path.join(REPO, name), encoding="utf-8-sig") as fh:
        return fh.read()


class ReadmeTests(unittest.TestCase):
    def test_script_commands_run_under_the_default_execution_policy(self) -> None:
        # A bare ".\install.ps1 …" fails where scripts are disabled (the Windows default).
        for name in ("README.md", "install.ps1", "uninstall.ps1"):
            text = _read(name)
            commands = re.findall(r"[^\n`'\"]*\.\\(?:un)?install\.ps1[^\n`']*", text)
            self.assertTrue(commands, name)
            for command in commands:
                with self.subTest(file=name, command=command.strip()):
                    self.assertRegex(command, r"powershell -ExecutionPolicy Bypass -File "
                                              r"\.\\(?:un)?install\.ps1")

    def test_documented_command_line_matches_the_app(self) -> None:
        text = _read("README.md")
        documented = set(re.findall(r"^\| `(--[a-z-]+)", text, re.M))
        self.assertEqual(documented, {"--background", "--no-window", "--port", "--debug",
                                      "--rescan"})
        usage = re.search(r"python -m projektsog (\[.*\])", text).group(1)
        self.assertEqual(set(re.findall(r"--[a-z-]+", usage)), documented)
        args = app.parse_args(["--background", "--no-window", "--port", "1", "--debug",
                               "--rescan"])
        self.assertTrue(args.rescan and args.debug and args.background and args.no_window)

    def test_no_claim_that_a_location_name_alone_finds_the_location(self) -> None:
        # Locations (source roots) are not search results; only entries inside them are.
        self.assertNotIn("`forar` finder\n  *Forår 2026 RØD*", _read("README.md"))
        self.assertNotRegex(_read("README.md"), r"`forar` finder\s+\*Forår 2026 RØD\*")


class InstallerTests(unittest.TestCase):
    def test_autostart_clears_task_managers_disabled_flag(self) -> None:
        # Otherwise "Start med Windows … Til" stays disabled after Task Manager switched it off.
        for name in ("install.ps1", "uninstall.ps1"):
            text = _read(name)
            with self.subTest(file=name):
                self.assertIn(r"Explorer\StartupApproved\Run'", text)
                self.assertIn("Remove-ItemProperty -LiteralPath $StartupApprovedKey -Name $AppName",
                              text)
        install = _read("install.ps1")
        self.assertLess(install.index("New-ItemProperty @runValue"),
                        install.index("Remove-ItemProperty -LiteralPath $StartupApprovedKey"))


if __name__ == "__main__":
    unittest.main()
