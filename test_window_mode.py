"""The one-window hotkey really points the pipeline at one window.

Step 2 of the single-window mode, end to end through the running program
rather than through the worker alone: a window is focused, Num5 is pressed,
and the whole pipeline has to come back up sized to that window - and Num5
again has to put it back on the whole screen.

What this pins down, because all three have already been got wrong once:
  * the capture size is the window's PHYSICAL size, taken from the worker's
    acknowledgement rather than guessed from GetWindowRect (which includes the
    invisible resize border);
  * the window that gets captured is the last one that was NOT ours - by the
    time the hotkey is pressed our own menu may hold the focus, and capturing
    our own overlay is the loop this mode exists to avoid;
  * switching back is clean, not a dead pipeline stuck on a window.

The overlay geometry is deliberately not checked: in this step the overlay is
still fullscreen and the processed window sits in its corner.

Requires NeuralScreen not to be running. ~40 seconds.

Run:  runtime\\python.exe test_window_mode.py
"""
import ctypes
import ctypes.wintypes
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

# Physical pixels, before pygame loads (SDL freezes DPI awareness at import).
try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

import autocheck  # noqa: E402  - launch/log/quit helpers live there

W, H = 960, 540
TARGET = (31, 97, 211)
VK_NUMLOCK = 0x90
VK_NUMPAD2 = 0x62
VK_NUMPAD5 = 0x65
KEYEVENTF_KEYUP = 0x0002


def numlock_on() -> bool:
    return bool(ctypes.windll.user32.GetKeyState(VK_NUMLOCK) & 1)


def toggle_numlock() -> None:
    user32 = ctypes.windll.user32
    user32.keybd_event(VK_NUMLOCK, 0, 0, 0)
    user32.keybd_event(VK_NUMLOCK, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.1)


def main() -> int:
    busy = autocheck.running_instances()
    if busy:
        print(f"FAIL: NeuralScreen is already running ({busy}) - stop it first")
        return 1

    import pygame
    pygame.init()
    screen = pygame.display.set_mode((W, H), pygame.NOFRAME)
    pygame.display.set_caption("NeuralScreen window-mode test target")
    hwnd = pygame.display.get_wm_info()["window"]
    user32 = ctypes.windll.user32

    tick = [0]

    def pump(seconds: float) -> None:
        """Keep the target window alive and repainting.

        A window that never redraws produces one capture frame and then
        nothing, so the colour wobbles a little - inside any tolerance, but
        enough for the compositor to hand over a new frame.
        """
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            tick[0] += 1
            wob = tick[0] % 7
            screen.fill((TARGET[0] + wob, TARGET[1], TARGET[2] - wob))
            pygame.display.flip()
            pygame.event.pump()
            time.sleep(0.02)

    def focus_target() -> bool:
        """Bring the target window to the front - for real.

        SetForegroundWindow from a background process is refused by Windows,
        and the first version of this test failed for exactly that reason: the
        overlay kept the focus, so the program had no foreign window to
        capture. A click is real user input, and the window is ours and at a
        known place.
        """
        for attempt in range(6):
            user32.SetForegroundWindow(hwnd)
            pump(0.3)
            if user32.GetForegroundWindow() == hwnd:
                return True
            rect = ctypes.wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            cx = (rect.left + rect.right) // 2
            cy = (rect.top + rect.bottom) // 2
            user32.SetCursorPos(cx, cy)
            pump(0.2)
            user32.mouse_event(0x0002, 0, 0, 0, 0)   # LEFTDOWN
            user32.mouse_event(0x0004, 0, 0, 0, 0)   # LEFTUP
            pump(0.4)
            if user32.GetForegroundWindow() == hwnd:
                return True
        return False

    def wait_log(offset: int, needle: str, timeout: float) -> str:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            text = autocheck.log_since(offset)
            if needle in text:
                return text
            pump(0.2)
        return ""

    restore_numlock = False
    if not numlock_on():
        # The defaults live on the numpad, which needs Num Lock; the test puts
        # it back the way it found it.
        toggle_numlock()
        restore_numlock = True

    failures = []
    offset = autocheck.launch()
    try:
        if not wait_log(offset, "NR ON | FPS", 30.0):
            print("FAIL: NeuralScreen did not start processing")
            return 1
        # The overlay opens its menu on start and takes both the focus and the
        # mouse, so the menu is closed first (Num2) - after that the overlay is
        # click-through again and the target window can be focused for real.
        autocheck.send_key(VK_NUMPAD2)
        pump(1.0)
        if not focus_target():
            print("FAIL: could not bring the target window to the front")
            return 1
        pump(1.0)

        # 1. Into window mode.
        autocheck.send_key(VK_NUMPAD5)
        text = wait_log(offset, "window capture inside the worker (WGCW)", 30.0)
        if not text:
            print("FAIL: the hotkey did not put the program into window mode")
            print(autocheck.log_since(offset)[-800:])
            return 1
        line = [l for l in text.splitlines() if "(WGCW)" in l][-1]
        print("in :", line.strip())
        if f"{W}x{H}" not in line:
            failures.append(f"the capture is not the window's size ({W}x{H}): {line.strip()}")
        rebuilt = [l for l in text.splitlines() if "pipeline rebuilt" in l]
        if not rebuilt:
            failures.append("the pipeline was not rebuilt for the window")
        elif f"{W}x{H}" not in rebuilt[-1]:
            failures.append(f"the pipeline was rebuilt at the wrong size: {rebuilt[-1].strip()}")
        else:
            print("    ", rebuilt[-1].strip())
        # It has to keep running in that mode, not fall over after one frame.
        mark = autocheck.log_offset()
        pump(4.0)
        after = autocheck.log_since(mark)
        if "NR ON | FPS" not in after:
            failures.append("no frames in window mode - the pipeline stalled")
        if "back to full screen" in after:
            failures.append("window mode dropped itself back to full screen")

        # 2. And back out.
        mark = autocheck.log_offset()
        autocheck.send_key(VK_NUMPAD5)
        text = wait_log(mark, "pipeline rebuilt", 30.0)
        if not text:
            failures.append("the hotkey did not switch back to the whole screen")
        else:
            line = [l for l in text.splitlines() if "pipeline rebuilt" in l][-1]
            print("out:", line.strip())
            if f"{W}x{H}" in line:
                failures.append("switching back kept the window size")
        mark = autocheck.log_offset()
        pump(4.0)
        if "NR ON | FPS" not in autocheck.log_since(mark):
            failures.append("no frames after switching back")
    finally:
        left = autocheck.quit_app()
        pygame.quit()
        if restore_numlock:
            toggle_numlock()
        if left:
            failures.append(f"processes left behind: {left}")

    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print("OK: the hotkey switches to one window and back, at the right size")
    return 0


if __name__ == "__main__":
    sys.exit(main())
