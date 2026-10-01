"""Starts Projektsøg without a console window (Start-menu shortcut and autostart use this).

Import or startup errors happen before the app's own logging exists and pythonw.exe has no
stderr, so they are appended to %LOCALAPPDATA%\\Projektsog\\logs\\startup-error.log.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _report_startup_error() -> None:
    import time
    import traceback

    # Same location as projektsog.config.log_dir(), which may itself be what failed to import.
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
    log_dir = os.path.join(base, "Projektsog", "logs")
    try:
        os.makedirs(log_dir, exist_ok=True)
        with open(os.path.join(log_dir, "startup-error.log"), "a", encoding="utf-8") as fh:
            fh.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')}\n{traceback.format_exc()}\n")
    except OSError:
        pass


try:
    from projektsog.app import main

    exit_code = main()
except SystemExit:
    raise
except BaseException:
    _report_startup_error()
    exit_code = 1
raise SystemExit(exit_code)
