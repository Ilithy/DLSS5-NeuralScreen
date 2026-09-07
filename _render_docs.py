"""Сгенерировать картинки меню для README (docs/menu-light.png, menu-dark.png).

Живой оверлей скриншотером не снять: окна помечены WDA_EXCLUDEFROMCAPTURE,
иначе Desktop Duplication снимал бы собственный вывод. Поэтому картинки для
документации собираются offscreen — тем же кодом, что рисует настоящее меню,
на нейтральной подложке (реальный рабочий стол в публичный репозиторий класть
незачем).

Запуск:  runtime\\python.exe _render_docs.py
"""
import os
import sys

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import pygame  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import overlay_ui  # noqa: E402

OUT_DIR = "docs"
SCALE = 1.0          # базовая вёрстка 1:1, картинка выходит ~540 px по панели
MARGIN = 64          # поля вокруг панели

# Подложки: тёплая под светлую тему, холодная под тёмную — чтобы панель
# читалась как окно поверх рабочего стола, а не как плоский макет.
BACKDROPS = {
    "light": ((0x3A, 0x2A, 0x28), (0x6B, 0x44, 0x38)),
    "dark": ((0x14, 0x18, 0x20), (0x2A, 0x22, 0x30)),
}

STATE = {
    "nr": True,
    "profile": "Strong / Cinematic",
    "profiles": ["Faithful", "Natural", "Strong / Cinematic", "Extreme / Overdrive"],
    "params": {"intensity": 1.65, "local_tone": 1.40,
               "local_structure": 1.50, "skin_structure": 1.00},
    "lang": "en",
    "work_size": "2496x1404",
    "recording": False,
    "rec_seconds": 0.0,
    "open_on_start": True,
    "split": 0.0,
    # Индикатор поддержки: показываем рабочее состояние, оно и типично
    "gpu_text": "RTX 5070 Ti · Blackwell",
    "gpu_ok": True,
}
STATS = {"fps": 55.1, "status": "NR ON", "resolution": "3840x2160", "frames": 8214}


def font_loader(size):
    try:
        return pygame.font.SysFont("consolas", size)
    except Exception:
        return pygame.font.Font(None, size)


def gradient(size, top, bottom):
    w, h = size
    surf = pygame.Surface((w, h))
    for y in range(h):
        t = y / max(h - 1, 1)
        surf.fill(tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)),
                  (0, y, w, 1))
    return surf


def shadow(surface, rect, radius):
    """Мягкая тень под панелью: несколько рамок с падающей альфой.

    Настоящего блюра в pygame нет, поэтому набираем градиент кольцами.
    """
    layers = 14
    for i in range(layers, 0, -1):
        alpha = int(56 * (1 - i / (layers + 1)) ** 2)
        if alpha <= 0:
            continue
        grow = i * 2
        r = rect.inflate(grow, grow).move(0, i)
        pad = pygame.Surface(r.size, pygame.SRCALPHA)
        pygame.draw.rect(pad, (0, 0, 0, alpha), pad.get_rect(),
                         border_radius=radius + grow // 2)
        surface.blit(pad, r.topleft)


def render(theme: str, out_path: str) -> None:
    menu = overlay_ui.OverlayMenu(SCALE, font_loader)
    menu.set_state(dict(STATE, theme=theme))
    menu.set_stats(STATS)
    menu.visible = True

    # Размер панели известен только после раскладки — считаем её на заведомо
    # большом холсте, а потом делаем холст точно по панели с полями.
    menu.layout(4000, 4000)
    pw, ph = menu.panel_rect.w, menu.panel_rect.h

    canvas = gradient((pw + MARGIN * 2, ph + MARGIN * 2), *BACKDROPS[theme])
    menu.layout(canvas.get_width(), canvas.get_height())
    shadow(canvas, menu.panel_rect, menu._u(overlay_ui.RADIUS))
    menu.draw(canvas)

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    pygame.image.save(canvas, out_path)
    print(f"{out_path}: {canvas.get_width()}x{canvas.get_height()} "
          f"(панель {pw}x{ph})")


def main() -> int:
    pygame.init()
    pygame.display.set_mode((64, 64))
    for theme in ("light", "dark"):
        render(theme, os.path.join(OUT_DIR, f"menu-{theme}.png"))
    pygame.quit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
