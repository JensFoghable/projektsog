"""Klippe's window (widget.py): which monitor, where, and following widget_enabled."""

import os
import tempfile
import unittest

from projektsog import widget
from projektsog.config import Config
from projektsog.widget import Monitor

MAIN = Monitor(0, 0, 2560, 1400, True)
SECOND = Monitor(2560, 0, 4480, 1040, False)
LEFT = Monitor(-1920, 0, 0, 1040, False)


class PlacementTests(unittest.TestCase):
    def test_the_second_monitor_is_chosen(self) -> None:
        self.assertEqual(widget.choose_monitor([MAIN, SECOND]), SECOND)
        self.assertEqual(widget.choose_monitor([SECOND, MAIN, LEFT]), LEFT)       # left to right
        self.assertEqual(widget.choose_monitor([MAIN]), MAIN)                     # only one screen
        self.assertEqual(widget.choose_monitor([MAIN, SECOND], "primary"), MAIN)
        self.assertEqual(widget.choose_monitor([]).primary, True)

    def test_bottom_right_with_a_margin(self) -> None:
        self.assertEqual(widget.bottom_right(SECOND, (300, 500)), (4480 - 300 - 16, 1040 - 500 - 16))
        self.assertEqual(widget.bottom_right(Monitor(0, 0, 200, 300, True), (300, 500)), (0, 0))

    def test_a_saved_position_counts_only_while_it_is_on_a_monitor(self) -> None:
        self.assertEqual(widget.parse_position("3000,400", [MAIN, SECOND]), (3000, 400))
        self.assertIsNone(widget.parse_position("3000,400", [MAIN]))              # screen 2 unplugged
        self.assertIsNone(widget.parse_position("", [MAIN]))
        self.assertIsNone(widget.parse_position("x,y", [MAIN]))


class FakePet(widget.PetWindow):
    """PetWindow without Edge: the window 'exists' while self.present is True."""

    def __init__(self, cfg: Config) -> None:
        super().__init__(cfg, "http://127.0.0.1:1/widget.html", profile_dir=tempfile.gettempdir(),
                         monitors_fn=lambda: [MAIN, SECOND])
        self.present = False
        self.calls: list[str] = []

    def _window(self):
        return 4242 if self.present else None

    def _launch_and_place(self) -> None:
        self.calls.append("launch")
        self.present = True
        self._hwnd = 4242

    def _close_window(self) -> None:
        self.calls.append("close")
        self._closing = True
        self._hwnd = None
        self.present = False

    def _apply_on_top(self, hwnd) -> None:
        self.calls.append("top")

    def _remember_position(self, hwnd) -> None:
        pass


class FollowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.cfg = Config(path=os.path.join(self.dir.name, "config.json"))
        self.pet = FakePet(self.cfg)

    def tearDown(self) -> None:
        self.dir.cleanup()

    def test_off_by_default(self) -> None:
        self.pet.step()
        self.assertEqual(self.pet.calls, [])

    def test_follows_the_setting(self) -> None:
        self.cfg.update({"widget_enabled": True})
        self.pet.step()
        self.assertEqual(self.pet.calls, ["launch"])
        self.pet.step()
        self.assertEqual(self.pet.calls, ["launch", "top"])
        self.cfg.update({"widget_enabled": False})
        self.pet.step()
        self.assertEqual(self.pet.calls[-1], "close")
        self.cfg.update({"widget_enabled": True})     # switched on again: it comes back
        self.pet.step()
        self.assertEqual(self.pet.calls[-1], "launch")

    def test_closing_it_with_x_switches_it_off(self) -> None:
        self.cfg.update({"widget_enabled": True})
        self.pet.step()
        self.pet.present = False                      # the user clicked X
        self.pet.step()
        self.assertIs(self.cfg["widget_enabled"], False)
        self.pet.step()
        self.assertEqual(self.pet.calls, ["launch"])  # not relaunched

    def test_settings(self) -> None:
        from projektsog import config
        self.assertEqual(config.validate({"widget_pet_name": "  "})["widget_pet_name"], "Klippe")
        self.assertEqual(config.validate({"widget_pet_name": "x" * 40})["widget_pet_name"], "x" * 20)
        for bad in ({"widget_monitor": "third"}, {"widget_daily_goal_hours": 0}, {"widget_daily_goal_hours": 20}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                config.validate(bad)
        self.assertEqual(widget.widget_url("http://127.0.0.1:47811/"), "http://127.0.0.1:47811/widget.html")


if __name__ == "__main__":
    unittest.main()
