"""The present window must not flash blank: it stays hidden until the first
successful Present.

The worker's picture window (NeuralScreenPresent) used to be shown right at
creation - a blank topmost window flashed black over the desktop during the
NGX warm-up on every start and on every one-window mode switch. Now it is
created hidden and revealed only after the first real Present.

Checks (by window visibility, no log dependence):
* shortly after launch (warm-up still running) the window exists but is
  HIDDEN - the old code showed it immediately;
* after the warm-up the window becomes VISIBLE (the first Present revealed it).
"""
import ctypes
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent  # the project root
sys.path.insert(0, str(BASE))  # the project modules (main.py, display.py, ...)
sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests/ (autocheck)

import autocheck  # noqa: E402

user32 = ctypes.windll.user32


def find_present_window() -> int:
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(hwnd, _):
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, cls, 64)
        if cls.value == "NeuralScreenPresent":
            found.append(int(hwnd))
            return False
        return True

    user32.EnumWindows(cb, 0)
    return found[0] if found else 0


def main() -> int:
    busy = autocheck.running_instances()
    if busy:
        print(f"FAIL: NeuralScreen is already running ({busy}) - stop it first")
        return 1

    failures = []
    offset = autocheck.launch()
    try:
        # 1. Right after the launch the worker creates its window but the NGX
        #    warm-up is still running (120 discarded frames - at least ~1 s).
        #    The window must exist and be HIDDEN here: visible = blank flash.
        hwnd = 0
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline and not hwnd:
            hwnd = find_present_window()
            if not hwnd:
                time.sleep(0.05)
        if not hwnd:
            failures.append("the present window never appeared")
            return 1
        hidden_during_warmup = not user32.IsWindowVisible(ctypes.c_void_p(hwnd))
        print(f"during warm-up: window {'hidden' if hidden_during_warmup else 'VISIBLE'}")
        if not hidden_during_warmup:
            failures.append("the present window is visible during the warm-up - "
                            "a blank flash over the desktop (the regression)")

        # 2. After the first Present the window must become visible.
        visible = False
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if user32.IsWindowVisible(ctypes.c_void_p(hwnd)):
                visible = True
                break
            time.sleep(0.1)
        print(f"after warm-up: window {'visible' if visible else 'STILL HIDDEN'}")
        if not visible:
            failures.append("the present window never became visible after the "
                            "first Present - the picture is missing")
    finally:
        left = autocheck.quit_app()
        if left:
            failures.append(f"processes left behind: {left}")

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: the present window stays hidden until the first Present")
    return 0


if __name__ == "__main__":
    sys.exit(main())
