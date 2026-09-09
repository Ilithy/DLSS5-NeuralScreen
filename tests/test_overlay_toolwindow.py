"""The overlay must not create a second taskbar button / Alt+Tab entry.

The program's taskbar presence is the 1x1 WS_EX_APPWINDOW button
(taskbar.py). The overlay itself (a borderless click-through window)
used to lack WS_EX_TOOLWINDOW - Windows treated it as a regular
application window and showed it in the taskbar next to the 1x1 button:
two thumbnails (a narrow "settings" one and the big overlay one), and
the hover/click behaviour of the pair was confusing (user: "2 окошка в
панели задач").

Checks (on the real overlay window, no log dependence):
* the overlay carries WS_EX_TOOLWINDOW - hidden from the taskbar and
  Alt+Tab, regardless of the menu-input state (set_menu_input must keep
  the flag in both directions);
* the overlay does NOT carry WS_EX_APPWINDOW - otherwise it would force
  its own button regardless.

Run:  runtime\\python.exe test_overlay_toolwindow.py
"""
import ctypes
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent  # the project root
sys.path.insert(0, str(BASE))  # the project modules (main.py, display.py, ...)

import display as D  # noqa: E402

WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
GWL_EXSTYLE = -20

user32 = ctypes.windll.user32


def exstyle(hwnd: int) -> int:
    return user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & 0xFFFFFFFF


def main() -> int:
    failures = []
    w, h = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    disp = D.Display(w, h, click_through=True)
    try:
        disp.reveal()
        hwnd = disp.get_hwnd()
        print(f"overlay hwnd: {hwnd:#x}")
        if not hwnd:
            failures.append("no overlay window")
            return 1

        # 1. After creation the overlay must be a tool window and not an
        #    application window - one taskbar button only (the 1x1 one).
        ex = exstyle(hwnd)
        print(f"initial exstyle: 0x{ex:08X}")
        if not (ex & WS_EX_TOOLWINDOW):
            failures.append("the overlay lacks WS_EX_TOOLWINDOW - it "
                            "creates a second taskbar button")
        if ex & WS_EX_APPWINDOW:
            failures.append("the overlay carries WS_EX_APPWINDOW - it forces "
                            "its own taskbar button")

        # 2. Menu input ON (menu open: TRANSPARENT/NOACTIVATE removed) must
        #    keep TOOLWINDOW.
        disp.set_menu_input(True)
        ex = exstyle(hwnd)
        print(f"menu-input ON  exstyle: 0x{ex:08X}")
        if not (ex & WS_EX_TOOLWINDOW):
            failures.append("set_menu_input(True) dropped WS_EX_TOOLWINDOW")
        if ex & WS_EX_APPWINDOW:
            failures.append("set_menu_input(True) added WS_EX_APPWINDOW")

        # 3. Menu input OFF (click-through restored) must keep TOOLWINDOW
        #    too - the state the overlay lives in most of the time.
        disp.set_menu_input(False)
        ex = exstyle(hwnd)
        print(f"menu-input OFF exstyle: 0x{ex:08X}")
        if not (ex & WS_EX_TOOLWINDOW):
            failures.append("set_menu_input(False) dropped WS_EX_TOOLWINDOW")
        if ex & WS_EX_APPWINDOW:
            failures.append("set_menu_input(False) added WS_EX_APPWINDOW")
    finally:
        disp.close()

    print("=" * 60)
    if failures:
        print(f"FAIL: {len(failures)} - {failures}")
        return 1
    print("OK: the overlay stays a tool window in every input state - "
          "one taskbar button only")
    return 0


if __name__ == "__main__":
    sys.exit(main())
