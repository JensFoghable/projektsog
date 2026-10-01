"""winui: pure logic with injected enumerations/fakes (no windows, no Explorer, no registry writes)."""

import logging
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from projektsog import winui
from projektsog.winui import WindowInfo

_tmp: tempfile.TemporaryDirectory | None = None
_logger = logging.getLogger("projektsog.winui")
_saved_level = _logger.level


def setUpModule() -> None:
    global _tmp
    _tmp = tempfile.TemporaryDirectory()
    os.environ["LOCALAPPDATA"] = _tmp.name
    _logger.setLevel(logging.CRITICAL)      # failures below are provoked on purpose


def tearDownModule() -> None:
    _logger.setLevel(_saved_level)
    if _tmp is not None:
        _tmp.cleanup()


class FakeRunValues:
    def __init__(self, values: dict[str, object]) -> None:
        self.values = dict(values)

    def items(self):
        return list(self.values.items())

    def set(self, name, data):
        for existing in list(self.values):
            if existing.casefold() == name.casefold():
                del self.values[existing]
        self.values[name] = data

    def delete(self, name):
        for existing in list(self.values):
            if existing.casefold() == name.casefold():
                del self.values[existing]


COMMAND = r'"C:\Python\pythonw.exe" "C:\Github\Search\Projektsøg.pyw" --background'


class RunKeyTests(unittest.TestCase):
    def test_legacy_recognition(self):
        legacy = [
            COMMAND,
            r'"C:\Python\pythonw.exe" "C:\Github\Search\PROJEKTSØG.PYW"',
            r'pythonw.exe C:\old\projektsog.pyw',
            r'"C:\Python\pythonw.exe" -m projektsog --background',
            r'C:\Python\pythonw.exe -m projektsog.hotkey --child',
        ]
        for data in legacy:
            with self.subTest(data=data):
                self.assertTrue(winui.is_legacy_run_value(data))
        for data in (r'"C:\Program Files\OneDrive\OneDrive.exe" /background',
                     r'C:\Tools\projekt-sog.exe', b"-m projektsog", None, 17,
                     r'pythonw.exe -m projektsoeg'):
            with self.subTest(data=data):
                self.assertFalse(winui.is_legacy_run_value(data))

    def test_enable_writes_value_and_drops_duplicates(self):
        store = FakeRunValues({
            "OneDrive": r'"C:\OneDrive.exe" /background',
            "Projektsog": r'pythonw.exe -m projektsog --background',
            "Projektsøg (gammel)": r'pythonw.exe "D:\old\Projektsøg.pyw"',
        })
        winui.apply_run_at_login(store, True, COMMAND)
        self.assertEqual(store.values, {"OneDrive": r'"C:\OneDrive.exe" /background',
                                        "Projektsøg": COMMAND})

    def test_disable_removes_ours_and_legacy_values(self):
        store = FakeRunValues({
            "OneDrive": "x",
            "PROJEKTSØG": COMMAND,                       # names are case-insensitive
            "Old": r'pythonw -m projektsog',
        })
        winui.apply_run_at_login(store, False, "")
        self.assertEqual(store.values, {"OneDrive": "x"})

    def test_enable_requires_command(self):
        with self.assertRaises(ValueError):
            winui.set_run_at_login(True, "  ")


def info(hwnd=1, cls="Chrome_WidgetWin_1", title="Projektsøg", pid=100, exe="msedge.exe",
         visible=False, owner=None) -> WindowInfo:
    return WindowInfo(hwnd, cls, title, pid, exe, visible, owner)


class AppWindowIdentificationTests(unittest.TestCase):
    def test_exact_identification(self):
        self.assertTrue(winui.is_app_window(info(), "Projektsøg"))
        self.assertTrue(winui.is_app_window(info(exe="MSEDGE.EXE"), "Projektsøg"))
        self.assertTrue(winui.is_app_window(info(pid=7), "Projektsøg", pid=7))
        rejected = {
            "chrome with same title": info(exe="chrome.exe"),
            "vs code": info(exe="Code.exe", title="Projektsøg"),
            "edge tab title": info(title="Projektsøg – Microsoft Edge"),
            "loading title": info(title="127.0.0.1:47811/"),
            "other class": info(cls="Chrome_WidgetWin_0"),
            "owned popup": info(owner=55),
            "unknown process": info(exe=None),
        }
        for label, candidate in rejected.items():
            with self.subTest(label):
                self.assertFalse(winui.is_app_window(candidate, "Projektsøg"))
        self.assertFalse(winui.is_app_window(info(pid=8), "Projektsøg", pid=7))

    def test_find_uses_z_order_and_skips_look_alikes(self):
        windows = [
            info(hwnd=10, exe="chrome.exe"),
            info(hwnd=11, title="Projektsøg – Microsoft Edge"),
            info(hwnd=12, pid=200),
            info(hwnd=13, pid=300),
        ]
        self.assertEqual(winui.find_app_window_info("Projektsøg", windows=windows).hwnd, 12)
        self.assertEqual(winui.find_app_window_info("Projektsøg", pid=300, windows=windows).hwnd, 13)
        self.assertIsNone(winui.find_app_window_info("Projektsøg", pid=999, windows=windows))
        self.assertIsNone(winui.find_app_window_info("Andet", windows=windows))


class ExplorerTitleTests(unittest.TestCase):
    PATH = r"C:\Kunder 2026 (STUDIO)\Rikke Lindholm"

    def test_matching_titles(self):
        for title in ("Rikke Lindholm – Stifinder", "Rikke Lindholm - File Explorer",
                      "rikke lindholm – stifinder", "Rikke Lindholm",
                      "Rikke Lindholm og 1 fane mere – Stifinder",
                      "Rikke Lindholm og 3 flere faner – Stifinder",
                      "Rikke Lindholm and 2 more tabs - File Explorer",
                      r"C:\Kunder 2026 (STUDIO)\Rikke Lindholm – Stifinder"):
            with self.subTest(title=title):
                self.assertTrue(winui.explorer_title_matches(title, self.PATH))

    def test_non_matching_titles(self):
        for title in ("Rikke Lindholm 2 – Stifinder", "Rikke Lindholmsen – Stifinder",
                      "Lindholm – Stifinder", "Overførsler – Stifinder", "", "Rikke"):
            with self.subTest(title=title):
                self.assertFalse(winui.explorer_title_matches(title, self.PATH))

    def test_path_forms(self):
        self.assertTrue(winui.explorer_title_matches("Klip – Stifinder",
                                                     "\\\\?\\C:\\Kunder\\Rikke Lindholm\\Klip\\"))
        self.assertTrue(winui.explorer_title_matches(
            "Klar Tand – Stifinder", r"\\GRAFIK-PC\Kunder 2026 (Grafik)\Klar Tand"))
        self.assertTrue(winui.explorer_title_matches(
            "Klar Tand – Stifinder", "\\\\?\\UNC\\GRAFIK-PC\\Kunder 2026 (Grafik)\\Klar Tand"))
        share = r"\\GRAFIK-PC\Forår 2026 (HDD)"
        self.assertTrue(winui.explorer_title_matches(
            "Forår 2026 (HDD) (\\\\GRAFIK-PC) – Stifinder", share))
        self.assertTrue(winui.explorer_title_matches("Forår 2026 (HDD) – Stifinder", share))
        self.assertTrue(winui.explorer_title_matches("2024 Disk Sølv (H:) – Stifinder", "H:\\"))
        self.assertTrue(winui.explorer_title_matches("2024 Disk Sølv (H:) – Stifinder", "h:"))
        self.assertFalse(winui.explorer_title_matches("Lokal disk (C:) – Stifinder", "H:\\"))
        self.assertFalse(winui.explorer_title_matches("Klip – Stifinder", "Klip"))  # relative

    def test_pick_new_or_changed_window(self):
        before = {1: "Overførsler – Stifinder", 2: "Rikke Lindholm – Stifinder"}
        # A brand-new window showing the folder wins.
        now = [(3, "Rikke Lindholm – Stifinder"), (1, "Overførsler – Stifinder"),
               (2, "Rikke Lindholm – Stifinder")]
        self.assertEqual(winui.pick_explorer_window(before, now, self.PATH), 3)
        # A reused window / new tab: the title changed to the folder.
        now = [(1, "Rikke Lindholm og 1 fane mere – Stifinder"), (2, "Rikke Lindholm – Stifinder")]
        self.assertEqual(winui.pick_explorer_window(before, now, self.PATH), 1)
        # Nothing new, nothing changed (window 2 already showed it before).
        now = [(1, "Overførsler – Stifinder"), (2, "Rikke Lindholm – Stifinder")]
        self.assertIsNone(winui.pick_explorer_window(before, now, self.PATH))
        # A new window that shows something else is not taken here.
        now = [(4, "Hjem – Stifinder"), (1, "Overførsler – Stifinder")]
        self.assertIsNone(winui.pick_explorer_window(before, now, self.PATH))

    def test_explorer_window_for_injected(self):
        windows = [(7, "Overførsler – Stifinder"), (8, "Rikke Lindholm – Stifinder"),
                   (9, "Rikke Lindholm og 1 fane mere – Stifinder")]
        self.assertEqual(winui.explorer_window_for(self.PATH, windows=windows), 8)
        self.assertIsNone(winui.explorer_window_for(r"D:\Andet", windows=windows))

    # -- WIN-2: a sibling whose name starts with the target's name is not the target ----------
    def test_siblings_with_the_same_prefix_do_not_match(self):
        cases = [   # sibling pairs of the kind found in a live index (review WIN-2)
            ("Klar Tand - Silkeborg – Stifinder", r"\\GRAFIK-PC\Kunder 2026 (Grafik)\Klar Tand"),
            ("Rikke Lindholm - Testimonial – Stifinder", self.PATH),
            ("Bøgely Jul 2024 – Stifinder", r"C:\Kunder 2026 (STUDIO)\Bøgely"),
            ("Pixelbro Radio 2 – Stifinder", r"D:\Forår 2026 RØD\Pixelbro"),
            ("Vejbyg - Kampagne – Stifinder", r"C:\Kunder 2026 (STUDIO)\Vejbyg"),
            ("Kildedal - Grøn Koncert – Stifinder", r"C:\Kunder 2026 (STUDIO)\Kildedal"),
            ("Bøgely Jul 2024 Final – Stifinder", r"C:\Kunder 2026 (STUDIO)\Bøgely"),
            ("Klar Tand - Silkeborg og 2 flere faner – Stifinder", r"C:\x\Klar Tand"),
            ("Klar Tand - Silkeborg - File Explorer", r"C:\x\Klar Tand"),
            # Windows 10 titles carry no app name: the bare sibling name must not match either.
            ("Klar Tand - Silkeborg", r"C:\x\Klar Tand"),
            ("Bøgely og 2 venner", r"C:\x\Bøgely"),
        ]
        for title, path in cases:
            with self.subTest(title=title, path=path):
                self.assertFalse(winui.explorer_title_matches(title, path))

    def test_names_containing_a_dash_still_match_themselves(self):
        own = r"\\GRAFIK-PC\Kunder 2026 (Grafik)\Klar Tand - Silkeborg"
        for title in ("Klar Tand - Silkeborg – Stifinder", "Klar Tand - Silkeborg",
                      "Klar Tand - Silkeborg og 2 flere faner – Stifinder",
                      "Klar Tand - Silkeborg and 1 more tab - File Explorer",
                      r"\\GRAFIK-PC\Kunder 2026 (Grafik)\Klar Tand - Silkeborg – Stifinder"):
            with self.subTest(title=title):
                self.assertTrue(winui.explorer_title_matches(title, own))

    def test_all_title_formats_of_explorerframe(self):
        # The da-DK/en-US format strings of explorerframe.dll.mui (%1 = folder, %2 = count),
        # plus a localisation whose app name contains a dash.
        for title in ("Klip – Stifinder", "Klip og 1 fane mere – Stifinder",
                      "Klip og 12 flere faner – Stifinder", "Klip - File Explorer",
                      "Klip and 1 more tab - File Explorer", "Klip and 3 more tabs - File Explorer",
                      "Klip - Datei-Explorer", "Klip und 2 weitere Registerkarten - Datei-Explorer"):
            with self.subTest(title=title):
                self.assertTrue(winui.explorer_title_matches(title, r"C:\Rikke Lindholm\Klip"))
        self.assertFalse(winui.explorer_title_matches("Klip – Ukendt Program",
                                                      r"C:\Rikke Lindholm\Klip"))

    def test_explorer_window_for_skips_the_sibling(self):
        target = r"\\GRAFIK-PC\Kunder 2026 (Grafik)\Klar Tand"
        self.assertIsNone(winui.explorer_window_for(
            target, windows=[(1, "Klar Tand - Silkeborg – Stifinder")]))
        self.assertEqual(winui.explorer_window_for(
            target, windows=[(1, "Klar Tand - Silkeborg – Stifinder"),
                             (2, "Klar Tand – Stifinder")]), 2)
        # After an open: a sibling window whose title changed is not taken for the target.
        before = {1: "Overførsler – Stifinder"}
        now = [(1, "Klar Tand - Silkeborg – Stifinder")]
        self.assertIsNone(winui.pick_explorer_window(before, now, target))


class PathAndFileTypeTests(unittest.TestCase):
    def test_clean_path(self):
        cases = {
            "C:\\Kunder\\": "C:\\Kunder",
            "c:": "C:\\",
            "c:\\\\": "C:\\",
            "C:/Kunder/Rikke Lindholm/": "C:\\Kunder\\Rikke Lindholm",
            "\\\\?\\D:\\Forår 2026 RØD\\x": "D:\\Forår 2026 RØD\\x",
            "\\\\?\\UNC\\GRAFIK-PC\\Kunder\\": "\\\\GRAFIK-PC\\Kunder",
            "  \\\\HOST\\share\\dir  ": "\\\\HOST\\share\\dir",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(winui.clean_path(raw), expected)
        for bad in ("", "relative\\dir", "\\\\HOST", "\\\\", "C:relative", None, 5):
            with self.subTest(bad=bad):
                self.assertIsNone(winui.clean_path(bad))

    def test_open_file_allow_list(self):
        for ok in (r"C:\x\FX9_7912.MXF", r"C:\x\klip.mov", r"\\H\s\lyd.wav", r"C:\x\logo.PSD",
                   r"C:\x\projekt.drp", r"C:\x\manus.docx", r"C:\x\undertekst.srt"):
            with self.subTest(ok=ok):
                self.assertTrue(winui.is_openable_file(ok))
        for bad in (r"C:\x\setup.exe", r"C:\x\run.bat", r"C:\x\a.cmd", r"C:\x\s.ps1",
                    r"C:\x\v.vbs", r"C:\x\j.js", r"C:\x\genvej.lnk", r"C:\x\i.msi",
                    r"C:\x\s.scr", r"C:\x\c.com", r"C:\x\h.hta", r"C:\x\r.reg",
                    r"C:\x\side.html", r"C:\x\makro.docm", r"C:\x\klip.mp4:evil.exe",
                    r"C:\x\klip.exe:skjult.mp4", r"C:\x\README", r"C:\x\slut.mp4."):
            with self.subTest(bad=bad):
                self.assertFalse(winui.is_openable_file(bad))

    def test_open_file_refuses_before_any_shell_call(self):
        with mock.patch.object(winui, "_shell_call") as shell_call, \
                mock.patch.object(winui, "_unlock_foreground") as unlock:
            self.assertFalse(winui.open_file(r"C:\x\setup.exe"))
            self.assertFalse(winui.open_folder("relative\\path"))
            self.assertFalse(winui.reveal(""))
        shell_call.assert_not_called()
        unlock.assert_not_called()

    def test_edge_path_takes_first_existing_candidate(self):
        existing = os.path.join(_tmp.name, "msedge.exe")
        with open(existing, "wb"):
            pass
        missing = os.path.join(_tmp.name, "missing", "msedge.exe")
        with mock.patch.object(winui, "_edge_candidates", return_value=[missing, existing]):
            self.assertEqual(winui.edge_path(), existing)
        with mock.patch.object(winui, "_edge_candidates", return_value=[missing]):
            self.assertIsNone(winui.edge_path())


class ShellWorkerTests(unittest.TestCase):
    """The request/timeout/abandon mechanics of the shell thread (COM left out here)."""

    def setUp(self) -> None:
        self.worker = winui._ShellWorker(com=False)
        self.worker.start()

    def tearDown(self) -> None:
        self.worker.retire()
        self.worker.join(2)

    def test_result_error_and_thread(self):
        names = []
        self.assertEqual(winui._run_shell(lambda: names.append(threading.current_thread().name)
                                          or 42, self.worker, 2.0), 42)
        self.assertEqual(names, ["ShellThread"])
        self.assertIsNone(winui._run_shell(lambda: 1 / 0, self.worker, 2.0))

    def test_timeout_abandons_request(self):
        gate = threading.Event()
        ran: list[int] = []
        blocker = threading.Thread(target=winui._run_shell,
                                   args=(lambda: gate.wait(2), self.worker, 5.0))
        blocker.start()
        time.sleep(0.05)
        started = time.monotonic()
        self.assertIsNone(winui._run_shell(lambda: ran.append(1), self.worker, 0.2))
        self.assertLess(time.monotonic() - started, 1.0)
        gate.set()
        blocker.join(2)
        self.assertEqual(winui._run_shell(lambda: "after", self.worker, 2.0), "after")
        self.assertEqual(ran, [])                 # the late request never ran

    def test_retired_worker_fails_queued_requests(self):
        gate = threading.Event()
        results: list[object] = []
        first = threading.Thread(target=lambda: results.append(
            winui._run_shell(lambda: gate.wait(2) and "first", self.worker, 5.0)))
        first.start()
        time.sleep(0.05)
        queued = winui._ShellRequest(lambda: "never")
        self.assertTrue(self.worker.submit(queued))
        self.worker.retire()
        gate.set()
        first.join(2)
        self.assertTrue(queued.done.wait(2))
        self.assertIsInstance(queued.error, RuntimeError)
        self.assertEqual(results, ["first"])      # the running call still completed
        self.worker.join(2)
        self.assertFalse(self.worker.submit(winui._ShellRequest(lambda: None)))


class ProcessQueryTests(unittest.TestCase):
    """Read-only process enumeration of this very process."""

    def test_process_running_and_uptime(self):
        own = os.path.basename(sys.executable)
        self.assertTrue(winui.process_running(own))
        self.assertTrue(winui.process_running(own.upper()))
        uptime = winui.process_uptime(own)
        self.assertIsInstance(uptime, float)
        self.assertGreaterEqual(uptime, 0.0)
        self.assertFalse(winui.process_running("projektsog-no-such-process.exe"))
        self.assertIsNone(winui.process_uptime("projektsog-no-such-process.exe"))


if __name__ == "__main__":
    unittest.main()
