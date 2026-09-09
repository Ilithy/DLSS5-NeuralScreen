"""The taskbar window: a real taskbar button that opens the menu.

The overlay is a borderless click-through window and the worker window
is a tool window, so neither shows in the taskbar - the program lived
only in the tray. The taskbar window is a 1x1 WS_EX_APPWINDOW window:
it gives the program a taskbar button, and clicking it (WM_ACTIVATE)
sends the same "settings" command as a left click on the tray.

Checked: the window is created with APPWINDOW, it is visible to the
system, activating it emits the settings command, and stop() closes it.
"""
import ctypes
import ctypes.wintypes as wt
import os
import queue
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import taskbar  # noqa: E402

user32 = ctypes.windll.user32


def main() -> int:
    failures = []
    commands = queue.Queue()
    win = taskbar.TaskbarWindow(commands, "NeuralScreenTest")
    win.start()
    deadline = time.monotonic() + 5.0
    while win.hwnd is None and time.monotonic() < deadline:
        time.sleep(0.05)
    hwnd = win.hwnd
    print(f"taskbar hwnd: {hwnd}")
    if not hwnd:
        failures.append("the taskbar window was not created")
    else:
        ex = user32.GetWindowLongW(hwnd, -20)  # GWL_EXSTYLE
        print(f"exstyle: 0x{ex & 0xFFFFFFFF:08X}")
        if not (ex & taskbar.WS_EX_APPWINDOW):
            failures.append("the window must carry WS_EX_APPWINDOW")
        if not user32.IsWindowVisible(hwnd):
            failures.append("the window must be visible (the taskbar button)")
        # Activate it the way a taskbar click does.
        user32.SendMessageW(hwnd, taskbar.WM_ACTIVATE, taskbar.WA_CLICKACTIVE, 0)
        time.sleep(0.2)
        got = []
        while not commands.empty():
            got.append(commands.get_nowait())
        print(f"commands after activate: {got}")
        if "settings" not in got:
            failures.append("activating the window should emit the settings command")
    win.stop()
    time.sleep(0.2)
    if win.hwnd is not None:
        failures.append("stop() should close the window")

    print("=" * 60)
    if failures:
        print(f"FAIL: {len(failures)} - {failures}")
        return 1
    print("OK: the taskbar button exists, opens the menu, closes cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
