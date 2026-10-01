"""Tray: menu model, text fitting, struct layout and command dispatch (no window is created)."""

import ctypes
import os
import tempfile
import unittest

from projektsog import tray
from projektsog.tray import TrayIcon, build_menu, fit_utf16

_tmp: tempfile.TemporaryDirectory | None = None


def setUpModule() -> None:
    global _tmp
    _tmp = tempfile.TemporaryDirectory()
    os.environ["LOCALAPPDATA"] = _tmp.name


def tearDownModule() -> None:
    if _tmp is not None:
        _tmp.cleanup()


class MenuTests(unittest.TestCase):
    def test_menu_layout(self):
        items = build_menu({"hotkey_label": "Shift+Mellemrum", "follow": "notify",
                            "autostart": True})
        texts = [i.text for i in items]
        self.assertEqual(texts, ["Åbn Projektsøg\tShift+Mellemrum", "Indstillinger …",
                                 "Scan alle nu", "DaVinci Resolve", "Start med Windows", "",
                                 "Afslut"])
        self.assertTrue(items[0].default)
        self.assertTrue(items[5].separator)
        self.assertTrue(items[4].checked)
        resolve = items[3]
        self.assertEqual([(c.text, c.radio, c.checked) for c in resolve.children],
                         [("Fra", True, False), ("Vis besked", True, True),
                          ("Åbn mappe automatisk", True, False)])

    def test_menu_without_label_and_unknown_follow(self):
        items = build_menu({"hotkey_label": "", "follow": "bogus", "autostart": False})
        self.assertEqual(items[0].text, "Åbn Projektsøg")
        self.assertFalse(any(c.checked for c in items[3].children))
        self.assertFalse(items[4].checked)


class TextFitTests(unittest.TestCase):
    def test_fit(self):
        self.assertEqual(fit_utf16("Ny disk ‘ARKIV’", 64), "Ny disk ‘ARKIV’")
        long = "x" * 300
        fitted = fit_utf16(long, 256)
        self.assertEqual(len(fitted), 255)
        self.assertTrue(fitted.endswith("…"))
        emoji = "🎬" * 40                      # 2 UTF-16 units each
        fitted = fit_utf16(emoji, 64)
        self.assertLess(len(fitted.encode("utf-16-le")) // 2, 64)
        self.assertTrue(fitted.endswith("…"))
        self.assertEqual(fit_utf16("a\0b", 8), "a b")

    def test_fitted_text_fits_the_struct(self):
        data = tray.NOTIFYICONDATAW()
        data.szInfoTitle = fit_utf16("DaVinci Resolve: " + "Lang projekttitel " * 10, 64)
        data.szInfo = fit_utf16("🎬 " * 300, 256)
        data.szTip = fit_utf16("Projektsøg " * 30, 128)

    def test_struct_size(self):
        expected = 976 if ctypes.sizeof(ctypes.c_void_p) == 8 else 956
        self.assertEqual(ctypes.sizeof(tray.NOTIFYICONDATAW), expected)


class DispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.calls: list[tuple] = []
        self.icon = TrayIcon(
            os.path.join(_tmp.name, "icon.ico"), "Projektsøg",
            on_show=lambda: self.calls.append(("show",)),
            on_settings=lambda: self.calls.append(("settings",)),
            on_scan_all=lambda: self.calls.append(("scan",)),
            on_set_follow=lambda v: self.calls.append(("follow", v)),
            on_set_autostart=lambda v: self.calls.append(("autostart", v)),
            on_exit=lambda: self.calls.append(("exit",)),
            menu_state=lambda: {"hotkey_label": "Shift+Mellemrum", "follow": "off",
                                "autostart": True})

    def test_commands(self):
        state = {"autostart": True}
        for command in (tray.ID_SHOW, tray.ID_SETTINGS, tray.ID_SCAN_ALL, tray.ID_FOLLOW_OFF,
                        tray.ID_FOLLOW_NOTIFY, tray.ID_FOLLOW_OPEN, tray.ID_AUTOSTART,
                        tray.ID_EXIT, 4711):
            self.icon._dispatch(command, state)
        self.assertEqual(self.calls, [("show",), ("settings",), ("scan",), ("follow", "off"),
                                      ("follow", "notify"), ("follow", "open"),
                                      ("autostart", False), ("exit",)])
        self.icon._dispatch(tray.ID_AUTOSTART, {"autostart": False})
        self.assertEqual(self.calls[-1], ("autostart", True))

    def test_click_routing_for_both_notification_versions(self):
        menus: list[tuple[int, int]] = []
        self.icon._show_menu = lambda hwnd, x, y: menus.append((x, y))
        callback = tray.WM_TRAY_CALLBACK
        self.icon._v4 = True
        self.icon._on_message(0, callback, 0, tray.NIN_SELECT | (tray.ICON_ID << 16))
        self.icon._on_message(0, callback, 0, tray.WM_LBUTTONUP)          # not used in v4
        self.icon._on_message(0, callback, (200 << 16) | 100, tray.WM_CONTEXTMENU)
        self.icon._on_message(0, callback, (0xFFF6 << 16) | 0xFF9C, tray.WM_CONTEXTMENU)
        self.icon._v4 = False
        self.icon._on_message(0, callback, 0, tray.WM_LBUTTONUP)
        self.icon._on_message(0, callback, 0, tray.WM_RBUTTONUP)
        self.icon._on_message(0, callback, 0, tray.NIN_BALLOONUSERCLICK)
        self.assertEqual(self.calls, [("show",), ("show",), ("show",)])
        self.assertEqual(menus, [(100, 200), (-100, -10), (0, 0)])  # left/above the primary

    def test_failing_or_slow_callbacks_are_contained(self):
        def boom():
            raise RuntimeError("boom")

        def slow():
            import time
            time.sleep(0.08)

        with self.assertLogs("projektsog.tray", level="WARNING") as logs:
            TrayIcon._call(boom)
            TrayIcon._call(slow)
        self.assertTrue(any("failed" in line for line in logs.output))
        self.assertTrue(any("limit 50 ms" in line for line in logs.output))

    def test_unstarted_icon(self):
        self.icon.notify("Titel", "Tekst")        # queued, no window, no error
        self.assertEqual(len(self.icon._pending), 1)
        self.icon.stop()                          # idempotent no-op
        self.icon.stop()


if __name__ == "__main__":
    unittest.main()
