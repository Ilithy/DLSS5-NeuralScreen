"""The menu survives a window-mode switch: open at start, Num5, still visible.

The regression: with the menu open at launch (open_menu_on_start) the saved
offset was computed for the full desktop and landed the panel OUTSIDE a small
captured window - the menu came back clipped off the right edge on the first
window-mode activation. The fix: _rebuild_pipeline gives the restored menu
the same treatment as the settings handler - expand the layer to the whole
monitor and place the panel in the bottom-right corner.

How it is checked: in window mode the overlay is visible to an outside
capture (WDA is off), so a Desktop Duplication frame shows the menu. The
panel is a light cream (#F0EEE6) rectangle; when it is placed correctly it
reaches the right edge of the screen (margin 24), when it is clipped it
stops ~300 px short. A strip along the right edge must be mostly panel.

The test forces a light theme and the menu-open config, and restores the
user's config afterwards.
"""
import ctypes
import json
import os
import struct
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent.parent  # the project root
sys.path.insert(0, str(BASE))  # the project modules (main.py, display.py, ...)
sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests/ (autocheck)

import autocheck  # noqa: E402

# Physical pixels, before pygame loads: SDL freezes the process DPI awareness
# at import, and a window measured in logical units would not match what the
# capture produces (960x540 logical = 1200x675 physical at 125%).
try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

W, H = 960, 540
VK_NUMPAD2 = 0x62
VK_NUMPAD5 = 0x65
KEYEVENTF_KEYUP = 0x0002
CFG = BASE / "config.json"
CFG_BACKUP = BASE / "_work" / "config-window-menu-test.json"

# The light theme panel background.
PANEL_RGB = (240, 238, 230)


def numlock_on() -> bool:
    return bool(ctypes.windll.user32.GetKeyState(0x90) & 1)


def toggle_numlock() -> None:
    u = ctypes.windll.user32
    u.keybd_event(0x90, 0, 0, 0)
    u.keybd_event(0x90, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.1)


def grab_region(cam, rect):
    frame = None
    deadline = time.monotonic() + 3.0
    while frame is None and time.monotonic() < deadline:
        frame = cam.grab()
        if frame is None:
            time.sleep(0.05)
    if frame is None or rect is None:
        return frame
    x, y, w, h = rect
    fh, fw = frame.shape[:2]
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(fw, x + w), min(fh, y + h)
    if x1 <= x0 or y1 <= y0:
        return None
    return frame[y0:y1, x0:x1].copy()


def main() -> int:
    busy = autocheck.running_instances()
    if busy:
        print(f"FAIL: NeuralScreen is already running ({busy}) - stop it first")
        return 1

    # Force the menu-open + light theme config; restore the user's after.
    user_cfg = json.loads(CFG.read_text(encoding="utf-8"))
    CFG_BACKUP.write_text(json.dumps(user_cfg, indent=2), encoding="utf-8")
    test_cfg = dict(user_cfg)
    test_cfg["open_menu_on_start"] = True
    test_cfg["theme"] = "light"
    test_cfg["split"] = 0.0
    test_cfg["nr_small"] = True
    test_cfg["work_scale"] = 0.65
    CFG.write_text(json.dumps(test_cfg, indent=2), encoding="utf-8")

    restore_numlock = False
    if not numlock_on():
        toggle_numlock()
        restore_numlock = True

    import pygame
    pygame.init()
    screen = pygame.display.set_mode((W, H), pygame.NOFRAME)
    pygame.display.set_caption("NeuralScreen window-menu test target")
    hwnd = pygame.display.get_wm_info()["window"]
    user32 = ctypes.windll.user32

    tick = [0]

    def pump(seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            tick[0] += 1
            wob = tick[0] % 7
            screen.fill((31 + wob, 97, 211 - wob))
            pygame.display.flip()
            pygame.event.pump()
            time.sleep(0.02)

    def focus_target() -> bool:
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
            user32.mouse_event(0x0002, 0, 0, 0, 0)
            user32.mouse_event(0x0004, 0, 0, 0, 0)
            pump(0.4)
            if user32.GetForegroundWindow() == hwnd:
                return True
        return False

    failures = []
    offset = autocheck.launch()
    try:
        if not autocheck.wait_for(offset, "NR ON | FPS", 30.0):
            print("FAIL: NeuralScreen did not start processing")
            return 1
        # The menu is open at start (the config says so). The overlay holds
        # the focus, so the target window is focused for real first.
        if not focus_target():
            print("FAIL: could not bring the target window to the front")
            return 1
        pump(1.0)

        # Num5: into window mode. The menu is STILL OPEN - this is the
        # regression scenario (the first activation clipped it).
        rect = ctypes.wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        user32.SetCursorPos((rect.left + rect.right) // 2,
                            (rect.top + rect.bottom) // 2)
        pump(0.3)
        autocheck.send_key(VK_NUMPAD5)
        if autocheck.wait_for(offset, "window capture inside the worker (WGCW)", 30.0) is None:
            print("FAIL: the hotkey did not put the program into window mode")
            return 1
        # The switch overlay (blur + spinner) is up while the new worker
        # warms up; the test must not capture the screen under the veil. It
        # comes down on the first processed frame, so wait for the pipeline
        # to actually deliver one (NR ON | FPS) - a fixed sleep races the
        # warm-up, and a dying NGX (evaluation failed on frame 0, worker
        # restart 1/3) makes it longer than 2 s (flaky 0.0% in the suite).
        if autocheck.wait_for(offset, "NR ON | FPS", 30.0) is None:
            print("FAIL: no processed frame after the window-mode switch")
            return 1
        pump(0.5)

        # The menu must be fully visible: in window mode the overlay is
        # visible to an outside capture, and the panel reaches the right
        # edge of the screen (bottom-right placement, margin 24).
        import dxcam
        cam = dxcam.create(output_idx=0, output_color="RGB")
        try:
            frame = grab_region(cam, None)
            if frame is None:
                failures.append("no capture frame to inspect the menu")
            else:
                fh, fw = frame.shape[:2]
                # A strip along the right edge, the height of the panel.
                strip = frame[max(0, fh - 700):fh, max(0, fw - 100):fw]
                light = (strip[..., 0] > 200) & (strip[..., 1] > 200) & (strip[..., 2] > 200)
                share = float(light.mean())
                print(f"right-edge strip: {share * 100:.1f}% panel-coloured "
                      f"({fw}x{fh} screen)")
                if share < 0.30:
                    failures.append(
                        f"the menu is not at the right edge after the window-mode "
                        f"switch ({share * 100:.1f}% panel in the strip) - it is "
                        f"clipped off the screen (the regression)")
                # And the panel must actually be there: a light blob somewhere
                # in the bottom-right quadrant.
                quad = frame[fh // 2:, fw // 2:]
                light_q = (quad[..., 0] > 200) & (quad[..., 1] > 200) & (quad[..., 2] > 200)
                qshare = float(light_q.mean())
                print(f"bottom-right quadrant: {qshare * 100:.1f}% panel-coloured")
                if qshare < 0.01:
                    failures.append("no panel visible in the bottom-right "
                                    "quadrant at all")
        finally:
            del cam
    finally:
        autocheck.quit_app()
        pygame.quit()
        if restore_numlock:
            toggle_numlock()
        # Restore the user's config.
        CFG.write_text(json.dumps(user_cfg, indent=2), encoding="utf-8")

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: the menu stays fully visible across the window-mode switch")
    return 0


if __name__ == "__main__":
    sys.exit(main())
