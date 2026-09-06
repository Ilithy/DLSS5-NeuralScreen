"""Отрисовать меню оверлея в PNG — чтобы смотреть вёрстку, не запуская программу.

Оверлей помечен WDA_EXCLUDEFROMCAPTURE, внешним скриншотером его не снять,
поэтому проверка вёрстки идёт через offscreen-рендер.

Использование:  python _render_menu.py [выходной.png] [scale]
"""
import os
import sys

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import pygame  # noqa: E402

sys.path.insert(0, ".")
import overlay_ui  # noqa: E402

OUT = sys.argv[1] if len(sys.argv) > 1 else "_menu.png"
SCALE = float(sys.argv[2]) if len(sys.argv) > 2 else 1.5
SCREEN_W, SCREEN_H = 3840, 2160

pygame.init()
pygame.display.set_mode((64, 64))


def loader(size):
    try:
        return pygame.font.SysFont("consolas", size)
    except Exception:
        return pygame.font.Font(None, size)


menu = overlay_ui.OverlayMenu(SCALE, loader)
menu.set_state({
    "nr": True,
    "work_scale": 0.65,
    "profile": "Strong / Cinematic",
    "profiles": ["Faithful", "Natural", "Strong / Cinematic", "Extreme / Overdrive"],
    "params": {"intensity": 1.65, "local_tone": 1.40,
               "local_structure": 1.50, "skin_structure": 1.00},
    "lang": "ru",
    "work_size": "2496x1404",
    "recording": True,
    "rec_seconds": 95.0,
    "theme": __import__('os').environ.get('MENU_THEME', 'light'),
    "open_on_start": True,
})
menu.set_stats({"fps": 54.3, "status": "NR ВКЛ",
                "resolution": "3840x2160", "frames": 12345})
menu.visible = True
if os.environ.get('MENU_OPEN'):
    menu.open_choice = os.environ['MENU_OPEN']
menu.hover = os.environ.get('MENU_HOVER') or None

# Фон под панелью — имитация кадра рабочего стола, чтобы было видно,
# просвечивает панель или нет.
screen = pygame.Surface((SCREEN_W, SCREEN_H))
for y in range(0, SCREEN_H, 8):
    shade = 40 + int(120 * (y / SCREEN_H))
    pygame.draw.rect(screen, (shade, shade // 2, 90), (0, y, SCREEN_W, 8))

menu.draw(screen)

r = menu.panel_rect
margin = int(40 * SCALE)
crop = screen.subsurface(pygame.Rect(
    max(0, r.x - margin), max(0, r.y - margin),
    min(SCREEN_W - r.x + margin, r.w + margin * 2),
    min(SCREEN_H - r.y + margin, r.h + margin * 2))).copy()
pygame.image.save(crop, OUT)
print(f"панель {r.w}x{r.h}, файл {OUT} ({crop.get_width()}x{crop.get_height()})")

# Проверка наползаний: пересечения прямоугольников элементов
overlaps = []
for i, a in enumerate(menu.items):
    for b in menu.items[i + 1:]:
        if a.rect.colliderect(b.rect):
            overlaps.append(f"{a.key} x {b.key}")
print("наползания прямоугольников:", ", ".join(overlaps) if overlaps else "нет")
pygame.quit()
