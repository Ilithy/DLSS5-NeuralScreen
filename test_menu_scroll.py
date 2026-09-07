"""Прокрутка меню и растягивание по вертикали.

Панель выросла до ~1000 px по содержимому: на 1080p она занимала почти весь
экран, и деться от этого было некуда. Теперь высоту можно тянуть за нижнюю
кромку, а лишнее прокручивается.

Проверяется: полоса появляется только когда есть куда прокручивать, колесо
двигает содержимое и упирается в границы, невидимые (прокрученные) строки не
принимают клик, высота зажимается содержимым и экраном.
"""
import os
import sys

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import pygame  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
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

    # 1. Высокий экран: содержимое влезает, полосы нет
    menu = build()
    menu.draw(pygame.Surface((1920, 2400)))
    print(f"экран 2400: панель {menu.panel_rect.h}, содержимое "
          f"{menu.content_height}, прокрутка {menu._max_scroll}")
    if menu._max_scroll != 0:
        failures.append("на высоком экране появилась прокрутка")
    if menu._scroll_thumb.h != 0:
        failures.append("полоса нарисована без нужды")

    # 2. Пользователь задал высоту меньше содержимого
    menu = build()
    menu.draw(surf)
    full_h = menu.panel_rect.h
    menu.user_height = full_h // 2
    menu.draw(surf)
    print(f"высота {full_h} -> {menu.panel_rect.h}, "
          f"прокрутка до {menu._max_scroll}, полоса {menu._scroll_thumb.h} px")
    if menu.panel_rect.h >= full_h:
        failures.append("высота не уменьшилась")
    if menu._max_scroll <= 0 or menu._scroll_thumb.h <= 0:
        failures.append("полоса не появилась при обрезанной высоте")

    # 3. Колесо двигает и упирается
    wheel(menu, -3)
    menu.draw(surf)
    after_down = menu.scroll
    print(f"колесо вниз: прокрутка {after_down}")
    if after_down <= 0:
        failures.append("колесо вниз не сдвинуло содержимое")
    for _ in range(40):
        wheel(menu, -3)
    menu.draw(surf)
    if menu.scroll != menu._max_scroll:
        failures.append(f"прокрутка не упёрлась в конец "
                        f"({menu.scroll} != {menu._max_scroll})")
    for _ in range(60):
        wheel(menu, 3)
    menu.draw(surf)
    if menu.scroll != 0:
        failures.append(f"прокрутка не вернулась в начало ({menu.scroll})")

    # 4. Прокрученная за верх строка не принимает клик
    menu.scroll = menu._max_scroll
    menu.draw(surf)
    above = [i for i in menu.items if i.rect.bottom < menu._viewport.top]
    print(f"строк уехало под заголовок: {len(above)}")
    if above:
        target = above[0]
        got = menu.hit(target.rect.center)
        if got is not None:
            failures.append(f"клик попал в невидимую строку {got.key}")
    else:
        print("  (нечего проверять: ни одна строка не ушла целиком)")

    # 5. Видимая строка клик принимает
    inside = [i for i in menu.items
              if menu._viewport.collidepoint(i.rect.center)]
    if not inside:
        failures.append("в видимой области не осталось ни одной строки")
    elif menu.hit(inside[0].rect.center) is None:
        failures.append("видимая строка не принимает клик")

    # 6. Высоту не растянуть выше содержимого
    menu.user_height = menu.content_height * 3
    menu.draw(surf)
    print(f"запрошено {menu.content_height * 3}, получено {menu.panel_rect.h} "
          f"(содержимое {menu.content_height})")
    if menu.panel_rect.h > menu.content_height:
        failures.append("панель выше содержимого — внизу пустота")

    pygame.quit()
    if failures:
        for f in failures:
            print("ПРОВАЛ:", f)
        return 1
    print("OK: прокрутка, зажим высоты и попадания работают")
    return 0


if __name__ == "__main__":
    sys.exit(main())
