"""With no system cursor, the overlay draws its own - and only then.

Reported with DOOM maximised: Num2 brings the menu up and there is no mouse
pointer at all. A fullscreen game hides the cursor for its own input queue and
it stays hidden while our menu is open, which leaves the menu unusable.

Two halves, both worth pinning: the pointer appears when the system has none,
and it does NOT appear when the system cursor is there - a second pointer on a
normal desktop would be its own bug.

Run:  runtime\\python.exe test_menu_cursor.py
"""
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import numpy as np  # noqa: E402
import pygame  # noqa: E402

import display as D  # noqa: E402

W, H = 800, 600


def white_pixels_near_mouse(disp, radius: int = 40) -> int:
    """Bright pixels around the mouse position - our pointer is white."""
    x, y = pygame.mouse.get_pos()
    arr = pygame.surfarray.array3d(disp.screen)      # (w, h, 3)
    x0, y0 = max(0, x - radius), max(0, y - radius)
    x1, y1 = min(arr.shape[0], x + radius), min(arr.shape[1], y + radius)
    patch = arr[x0:x1, y0:y1].astype(np.int16)
    return int(((patch[..., 0] > 220) & (patch[..., 1] > 220) &
                (patch[..., 2] > 220)).sum())


def main() -> int:
    disp = D.Display(W, H, fullscreen=False, click_through=False)
    disp.set_hud_only(True, force=True)
    disp.menu.visible = True
    failures = []
    try:
        # 1. The system cursor is there: we must not add a second one.
        D.system_cursor_visible = lambda: True
        disp.draw_overlay(0.0)
        with_system = white_pixels_near_mouse(disp)

        # 2. The system cursor is gone (a fullscreen game): draw ours.
        D.system_cursor_visible = lambda: False
        disp.draw_overlay(0.0)
        without_system = white_pixels_near_mouse(disp)

        print(f"white pixels at the mouse: {with_system} with a system cursor, "
              f"{without_system} without one")
        if without_system < 40:
            failures.append(f"no pointer drawn when the system has none "
                            f"({without_system} pixels)")
        if with_system >= without_system:
            failures.append(f"a pointer is drawn even when the system cursor is "
                            f"there ({with_system} pixels) - that would be two")
    finally:
        disp.close()

    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print("OK: the overlay draws a pointer exactly when the system has none")
    return 0


if __name__ == "__main__":
    sys.exit(main())
