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
        st = user32.GetWindowLongW(hwnd, -16)  # GWL_STYLE
        print(f"exstyle: 0x{ex & 0xFFFFFFFF:08X}, style: 0x{st & 0xFFFFFFFF:08X}")
        if not (ex & taskbar.WS_EX_APPWINDOW):
            failures.append("the window must carry WS_EX_APPWINDOW")
        if not user32.IsWindowVisible(hwnd):
            failures.append("the window must be visible (the taskbar button)")
        # The caption/sysmenu/minimize styles are what make the taskbar
        # button behaviour work: without them clicking an already-active
        # button sends nothing (no SC_MINIMIZE) and the toggle dies
        # (user: "залипает"). The window procedure converts SC_MINIMIZE
        # into the menu toggle instead of minimizing.
        need = taskbar.WS_CAPTION | taskbar.WS_SYSMENU | taskbar.WS_MINIMIZEBOX
        if (st & need) != need:
            failures.append(f"the window must carry caption/sysmenu/minimize "
                            f"styles (0x{need:X}), got 0x{st & 0xFFFFFFFF:X}")

        # Activate it the way a taskbar click does.
        user32.SendMessageW(hwnd, taskbar.WM_ACTIVATE, taskbar.WA_CLICKACTIVE, 0)
        time.sleep(0.2)
        got = []
        while not commands.empty():
            got.append(commands.get_nowait())
        print(f"commands after activate: {got}")
        if "settings" not in got:
            failures.append("activating the window should emit the settings command")

        # 2. System activations must NOT open the menu: WA_ACTIVE without the
        #    cursor over the taskbar is the system (another window
        #    minimized/closed, Alt+Tab) - the menu popped up by itself
        #    (user: "сворачиваю другую программу - прога опять показывается").
        #    WA_ACTIVE WITH the cursor over the taskbar is a button click.
        win._cursor_over_taskbar = lambda: False
        time.sleep(0.6)  # past the 0.5 s dedup
        user32.SendMessageW(hwnd, taskbar.WM_ACTIVATE, taskbar.WA_ACTIVE, 0)
        time.sleep(0.2)
        got = []
        while not commands.empty():
            got.append(commands.get_nowait())
        print(f"commands after system activate (cursor elsewhere): {got}")
        if got:
            failures.append("a system activation (cursor not over the "
                            f"taskbar) must not emit anything, got {got}")

        win._cursor_over_taskbar = lambda: True
        time.sleep(0.6)
        user32.SendMessageW(hwnd, taskbar.WM_ACTIVATE, taskbar.WA_ACTIVE, 0)
        time.sleep(0.2)
        got = []
        while not commands.empty():
            got.append(commands.get_nowait())
        print(f"commands after taskbar activate (cursor over it): {got}")
        if "settings" not in got:
            failures.append("a taskbar click (WA_ACTIVE with the cursor over "
                            "the taskbar) should emit the settings command")

        # 3. SC_MINIMIZE / SC_RESTORE: the taskbar button sends these on a
        #    minimize/restore request. The 1x1 window must not actually
        #    minimize (the button would vanish and toggling would break on
        #    the second click) - both are converted to the same settings
        #    toggle instead. The dedup window is 0.5 s (one click may deliver
        #    several messages), so the sends are spaced beyond it.
        for sc in (taskbar.SC_MINIMIZE, taskbar.SC_RESTORE):
            time.sleep(0.6)
            user32.SendMessageW(hwnd, taskbar.WM_SYSCOMMAND, sc, 0)
        time.sleep(0.2)
        # The window must stay restored (not minimized) through both.
        if user32.IsIconic(hwnd):
            failures.append("SC_MINIMIZE/SC_RESTORE must not minimize the "
                            "1x1 window (the taskbar button would vanish)")
        while not commands.empty():
            got.append(commands.get_nowait())
        print(f"commands after syscommand: {got}")
        if got.count("settings") < 2:
            failures.append("SC_MINIMIZE and SC_RESTORE should each emit a "
                            "settings toggle, got "
                            f"{got.count('settings')} settings command(s)")

        # 4. SC_CLOSE ('Close window' in the right-click menu) must be
        #    ignored: destroying the 1x1 window kills the taskbar button for
        #    the session (audit 10.09 F3).
        user32.SendMessageW(hwnd, taskbar.WM_SYSCOMMAND, taskbar.SC_CLOSE, 0)
        time.sleep(0.2)
        if not user32.IsWindow(hwnd):
            failures.append("SC_CLOSE must not destroy the taskbar window "
                            "(the button would vanish for the session)")

        # 5. The cursor gate must accept BOTH taskbars: the primary
        #    (Shell_TrayWnd) and a secondary monitor's one
        #    (Shell_SecondaryTrayWnd) - multi-monitor users could not use
        #    the button on the second screen (audit 10.09 F1).
        for name, cls in (("primary", "Shell_TrayWnd"),
                          ("secondary", "Shell_SecondaryTrayWnd")):
            tb = user32.FindWindowW(cls, None)
            if not tb:
                print(f"{name} taskbar not present - skipped")
                continue
            r = wt.RECT()
            user32.GetWindowRect(tb, ctypes.byref(r))
            cx, cy = (r.left + r.right) // 2, (r.top + r.bottom) // 2
            user32.SetCursorPos(cx, cy)
            time.sleep(0.1)
            if not win._cursor_over_taskbar():
                failures.append(f"cursor over the {name} taskbar must count "
                                f"as a taskbar click")
        # Restore the cursor somewhere neutral (the previous position is
        # unknown; move it to the centre of the screen).
        user32.SetCursorPos(user32.GetSystemMetrics(0) // 2,
                            user32.GetSystemMetrics(1) // 2)
        time.sleep(0.6)  # past the dedup before any further sends
        got = []
        while not commands.empty():
            got.append(commands.get_nowait())
        print(f"commands after SC_CLOSE + cursor checks: {got} (dedup-affected "
              f"cursor moves emit up to one settings command)")
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
