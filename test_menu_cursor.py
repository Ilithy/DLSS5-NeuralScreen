"""While the menu is open the mouse pointer is ours, and there is exactly one.

Reported with DOOM maximised: Num2 brings the menu up and there is no mouse
pointer at all - a fullscreen game hides the cursor for its own input queue
and it stays hidden while our menu is over it. Reported again with DOOM
windowed: a pointer that does not move, which is the game's OWN cursor frozen
inside the captured frame (an unfocused game pauses).

Asking GetCursorInfo first was not good enough for either case, so the rule is
simple now: with the menu open we hide the system cursor over our window and
draw our own. Both halves are worth pinning - the pointer is there while the
menu is open, and gone when it is closed.

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
        # The menu is open: the pointer is drawn, whatever the system cursor
        # happens to be doing.
        disp.draw_overlay(0.0)
        with_menu = white_pixels_near_mouse(disp)

        # The menu is closed: nothing to point at, no pointer.
        disp.menu.visible = False
        disp.draw_overlay(0.0)
        without_menu = white_pixels_near_mouse(disp)

        print(f"white pixels at the mouse: {with_menu} with the menu open, "
              f"{without_menu} with it closed")
        if with_menu < 40:
            failures.append(f"no pointer drawn while the menu is open "
                            f"({with_menu} pixels)")
        if without_menu >= with_menu:
            failures.append(f"a pointer is still drawn with the menu closed "
                            f"({without_menu} pixels)")
    finally:
        disp.close()

    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print("OK: one pointer while the menu is open, none when it is closed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
