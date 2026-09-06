"""Проверка: открытое меню попадает в кадр записи и скриншота.

Наше окно исключено из захвата (WDA_EXCLUDEFROMCAPTURE), поэтому меню
рисуется на кадр отдельно — draw_capture_overlay. Тест проверяет, что оно
действительно ложится на пиксели и что закрытое меню кадр не трогает.
"""
import os
import sys

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import numpy as np  # noqa: E402
import pygame  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from display import Display  # noqa: E402

W, H = 1280, 720


def make_frame():
    frame = np.zeros((H, W, 4), dtype=np.uint8)
    frame[..., 0] = 17
    frame[..., 1] = 34
    frame[..., 2] = 51
    frame[..., 3] = 255
    return np.ascontiguousarray(frame)


def surface_of(frame):
    return pygame.image.frombuffer(frame, (frame.shape[1], frame.shape[0]), "RGBX")


def main() -> int:
    disp = Display(W, H, fullscreen=False)
    disp.set_hud({"fps": 60.0, "status": "NR ON", "resolution": f"{W}x{H}",
                  "profile": "Strong / Cinematic", "frames": 10})
    disp.menu.set_state({
        "nr": True, "profile": "Strong / Cinematic",
        "profiles": ["Faithful", "Natural", "Strong / Cinematic"],
        "params": {"intensity": 1.65, "local_tone": 1.40,
                   "local_structure": 1.50, "skin_structure": 1.00},
        "work_size": "832x468", "open_on_start": True,
    })
    failures = []

    # 1. Меню закрыто — кадр не должен измениться
    frame = make_frame()
    before = frame.copy()
    disp.menu.visible = False
    disp.draw_capture_overlay(surface_of(frame))
    if not np.array_equal(frame, before):
        failures.append("закрытое меню изменило кадр")

    # 2. Меню открыто — панель обязана появиться
    frame = make_frame()
    disp.menu.visible = True
    disp.draw_capture_overlay(surface_of(frame))
    r = disp.menu.panel_rect
    if r.w == 0 or r.h == 0:
        failures.append("панель нулевого размера")
    else:
        inside = frame[r.y + 5:r.bottom - 5, r.x + 5:r.right - 5, :3]
        changed = float((inside != np.array([17, 34, 51], np.uint8)).any(axis=2).mean())
        print(f"панель {r.w}x{r.h} @ {r.x},{r.y} — изменено пикселей: {changed:.1%}")
        if changed < 0.9:
            failures.append(f"панель закрасила лишь {changed:.1%} своей площади")
        # За пределами панели кадр обязан остаться нетронутым
        outside_ok = True
        for y, x in ((2, 2), (H - 3, W - 3), (2, W - 3)):
            if r.collidepoint(x, y):
                continue
            if tuple(frame[y, x, :3]) != (17, 34, 51):
                outside_ok = False
        if not outside_ok:
            failures.append("кадр испорчен за пределами панели")

    # 3. Подсветка «взялся за край» в файл не попадает
    frame_hover = make_frame()
    disp.menu.hover = "title"
    disp.draw_capture_overlay(surface_of(frame_hover))
    if not np.array_equal(frame_hover, frame):
        failures.append("состояние наведения просочилось в кадр")
    if disp.menu.hover != "title":
        failures.append("состояние наведения не восстановлено")

    disp.close()
    if failures:
        for f in failures:
            print("ПРОВАЛ:", f)
        return 1
    print("OK: меню пекётся в кадр, закрытое — не пекётся, ховер не протекает")
    return 0


if __name__ == "__main__":
    sys.exit(main())
