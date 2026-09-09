"""The header collapse icon and the one-window footer button.

The collapse used to be a footer button next to "quit the program" - the
two looked equally harmless, even though one hides the menu and the other
unloads the program. It moved to the header as a minimise glyph ([help]
[gear] [min]), and the freed footer slot became the one-window mode
button (the feature was only reachable through the Num5 hotkey).

Checked: the header holds exactly help/gear/min on the main page, the
min icon emits the close command, the footer holds screenshot/record/
one-window, the one-window button emits the window_mode command, and the
settings page still shows the back (close) icon only.
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
    "windows": ["1A2B3C: Notepad", "4D5E6F: Chrome"],
    "window_current": "1A2B3C: Notepad",
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


def click(menu, item):
    """A left click at the item's centre; returns the emitted commands."""
    out = menu.handle_event(pygame.event.Event(
        pygame.MOUSEBUTTONDOWN, {"pos": item.rect.center, "button": 1}))
    menu.handle_event(pygame.event.Event(
        pygame.MOUSEBUTTONUP, {"pos": item.rect.center, "button": 1}))
    return out


def main() -> int:
    pygame.init()
    pygame.display.set_mode((64, 64))
    failures = []

    menu = build()
    menu.layout(3840, 2160)

    # 1. The header on the main page: exactly help, gear and min.
    # The layout walks right to left, so the items list holds them reversed:
    # the visual order (left to right) is the reverse of the list.
    icons = [i for i in menu.items if i.kind == "icon"]
    keys = [i.key for i in icons]
    visual = list(reversed(keys))
    print(f"header icons (list {keys}, visual {visual})")
    if visual != ["help", "gear", "min"]:
        failures.append(f"the header should be [help, gear, min], got {visual}")

    # 2. The min icon emits the close command (hide the menu).
    min_icon = next((i for i in icons if i.key == "min"), None)
    if min_icon is None:
        failures.append("no min icon in the header")
    else:
        out = click(menu, min_icon)
        print(f"min click -> {out}")
        if ("button", "close") not in out:
            failures.append(f"the min icon should emit (button, close), got {out}")

    # 3. The footer on the main page: screenshot, record, one-window.
    actions = [i for i in menu.items if i.kind == "action"]
    keys = [i.key for i in actions]
    print(f"footer actions: {keys}")
    if "window" not in keys:
        failures.append(f"the footer should hold the one-window button, got {keys}")
    if "collapse" in keys:
        failures.append("the collapse button should be gone from the footer")

    # 4. The one-window button emits the window_mode command.
    win_btn = next((i for i in actions if i.key == "window"), None)
    if win_btn is None:
        failures.append("no one-window button in the footer")
    else:
        out = click(menu, win_btn)
        print(f"one-window click -> {out}")
        if ("button", "window_mode") not in out:
            failures.append(f"the one-window button should emit "
                            f"(button, window_mode), got {out}")

    # 5. The settings page: only the back (close) icon, no min.
    menu.page = "settings"
    menu.layout(3840, 2160)
    icons = [i for i in menu.items if i.kind == "icon"]
    keys = [i.key for i in icons]
    print(f"settings header icons: {keys}")
    if keys != ["close"]:
        failures.append(f"the settings header should be [close], got {keys}")

    # 6. The back icon returns to the main page.
    close_icon = next((i for i in icons if i.key == "close"), None)
    if close_icon is None:
        failures.append("no close icon in the settings header")
    else:
        out = click(menu, close_icon)
        print(f"close click -> {out}, page now {menu.page}")
        if menu.page != "main":
            failures.append("the close icon should return to the main page")

    print("=" * 60)
    if failures:
        print(f"FAIL: {len(failures)} - {failures}")
        return 1
    print("OK: the header collapse and the one-window button behave")
    return 0


if __name__ == "__main__":
    sys.exit(main())
