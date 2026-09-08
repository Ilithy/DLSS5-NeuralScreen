"""Menu scrolling and vertical stretching.

The panel grew to ~1000 px of content: at 1080p it took up almost the whole
screen and there was no way around it. Now the height can be dragged by the
bottom edge and the surplus scrolls.

Checked: the bar appears only when there is somewhere to scroll, the wheel
moves the content and stops at the boundaries, invisible (scrolled-away) rows
do not take clicks, the height is clamped by the content and the screen.
"""
import os
import sys

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import pygame  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # the project root
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # tests/ (autocheck)
import overlay_ui  # noqa: E402

STATE = {
    "nr": True,
    "profile": "Strong / Cinematic",
    "profiles": ["Faithful", "Natural", "Strong / Cinematic"],
    "params": {"intensity": 1.65, "local_tone": 1.40,
               "local_structure": 1.50, "skin_structure": 1.00},
    "split": 0.0, "open_on_start": True,
    "gpu_text": "RTX 5070 Ti · Blackwell", "gpu_ok": True,
}


def font_loader(size):
    try:
        return pygame.font.SysFont("consolas", size)
    except Exception:
        return pygame.font.Font(None, size)


def build():
    menu = overlay_ui.OverlayMenu(1.0, font_loader)
    menu.set_state(dict(STATE))
    menu.set_stats({"fps": 55.0, "status": "NR ON", "resolution": "3840x2160",
                    "frames": 100})
    menu.visible = True
    return menu


def wheel(menu, dy):
    menu.handle_event(pygame.event.Event(
        pygame.MOUSEMOTION, {"pos": menu.panel_rect.center, "rel": (0, 0),
                             "buttons": (0, 0, 0)}))
    menu.handle_event(pygame.event.Event(
        pygame.MOUSEWHEEL, {"x": 0, "y": dy, "flipped": False, "which": 0}))


def main() -> int:
    pygame.init()
    pygame.display.set_mode((64, 64))
    failures = []
    surf = pygame.Surface((1920, 1080))

    # 1. A tall screen: the content fits, there is no bar
    menu = build()
    menu.draw(pygame.Surface((1920, 2400)))
    print(f"screen 2400: panel {menu.panel_rect.h}, content "
          f"{menu.content_height}, scroll {menu._max_scroll}")
    if menu._max_scroll != 0:
        failures.append("scrolling appeared on a tall screen")
    if menu._scroll_thumb.h != 0:
        failures.append("the bar was drawn without need")

    # 2. The user set a height smaller than the content
    menu = build()
    menu.draw(surf)
    full_h = menu.panel_rect.h
    menu.user_height = full_h // 2
    menu.draw(surf)
    print(f"height {full_h} -> {menu.panel_rect.h}, "
          f"scroll up to {menu._max_scroll}, bar {menu._scroll_thumb.h} px")
    if menu.panel_rect.h >= full_h:
        failures.append("the height did not shrink")
    if menu._max_scroll <= 0 or menu._scroll_thumb.h <= 0:
        failures.append("the bar did not appear with a clipped height")

    # 3. The wheel moves and stops at the ends
    wheel(menu, -3)
    menu.draw(surf)
    after_down = menu.scroll
    print(f"wheel down: scroll {after_down}")
    if after_down <= 0:
        failures.append("the wheel down did not move the content")
    for _ in range(40):
        wheel(menu, -3)
    menu.draw(surf)
    if menu.scroll != menu._max_scroll:
        failures.append(f"the scroll did not stop at the end "
                        f"({menu.scroll} != {menu._max_scroll})")
    for _ in range(60):
        wheel(menu, 3)
    menu.draw(surf)
    if menu.scroll != 0:
        failures.append(f"the scroll did not return to the top ({menu.scroll})")

    # 4. A row scrolled above the top does not take a click
    menu.scroll = menu._max_scroll
    menu.draw(surf)
    # The header icons sit above the scroll area DELIBERATELY and are
    # clickable - they are not part of the scrolled content.
    above = [i for i in menu.items
             if i.rect.bottom < menu._viewport.top and i.kind != "icon"]
    print(f"rows moved under the title bar: {len(above)}")
    if above:
        target = above[0]
        got = menu.hit(target.rect.center)
        if got is not None:
            failures.append(f"the click landed in the invisible row {got.key}")
    else:
        print("  (nothing to check: no row moved out entirely)")

    # 5. A visible row does take a click
    inside = [i for i in menu.items
              if menu._viewport.collidepoint(i.rect.center)]
    if not inside:
        failures.append("no row is left in the visible area")
    elif menu.hit(inside[0].rect.center) is None:
        failures.append("a visible row does not take a click")

    # 5b. A header icon is clickable even though it lies outside the scroll area
    icons = [i for i in menu.items if i.kind == "icon"]
    if not icons:
        failures.append("there are no icons in the header")
    else:
        got = menu.hit(icons[0].rect.center)
        print(f"icon {icons[0].key}: the click "
              f"{'lands' if got is not None else 'does NOT land'}")
        if got is None:
            failures.append("the header icon does not take a click")

    # 5c. The header icons do not travel with the scroll
    for pos in (0, menu._max_scroll // 2, menu._max_scroll):
        menu.scroll = pos
        menu.draw(surf)
        for ic in [i for i in menu.items if i.kind == "icon"]:
            if not menu._title_bar.contains(ic.rect):
                failures.append(f"icon {ic.key} drifted out of the header "
                                f"at scroll {pos}: {tuple(ic.rect)} "
                                f"outside {tuple(menu._title_bar)}")
                break
    print("header icons while scrolling: in place"
          if not any("drifted" in f for f in failures) else "header icons: DRIFTED")
    menu.scroll = 0
    menu.draw(surf)

    # 6. The height cannot be stretched past the content
    menu.user_height = menu.content_height * 3
    menu.draw(surf)
    print(f"requested {menu.content_height * 3}, got {menu.panel_rect.h} "
          f"(content {menu.content_height})")
    if menu.panel_rect.h > menu.content_height:
        failures.append("the panel is taller than the content - empty space below")

    pygame.quit()
    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print("OK: scrolling, the height clamp and hit testing all work")
    return 0


if __name__ == "__main__":
    sys.exit(main())
