"""Display module for the DLSS 5 Desktop NR prototype.

Borderless fullscreen window with a branded HUD overlay (dark #0D1117,
amber #FFBF00 accent, Consolas). The window is excluded from screen
capture via SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE) so that
screen-capture tools (dxcam, OBS) do not see it.

Usage:
    disp = Display(2560, 1440)
    disp.set_hud({"fps": 60, "status": "NR ON", "resolution": "2560x1440",
                  "params": {"intensity": 0.5, "local_tone": 0.3,
                             "local_structure": 0.7}, "frames": 1234})
    while True:
        disp.show(frame_bgra)
        for ev in disp.poll_events():
            if ev == "quit":
                break
    disp.close()
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
from ctypes import wintypes
from typing import Dict, List

# --- argtypes для user32: БЕЗ них ctypes передаёт int как 32-битный c_int.
# HWND_TOPMOST=-1 превращается в 0xFFFFFFFF вместо 0xFFFFFFFFFFFFFFFF и
# SetWindowPos молча FAIL (ret=0) на 64-бит Windows — topmost не применяется.
# hwnd (64-бит) тоже обрезался бы при значениях >= 2^31.
user32 = ctypes.windll.user32
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.SetWindowPos.restype = wintypes.BOOL
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = wintypes.LONG
user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]
user32.SetWindowLongW.restype = wintypes.LONG
user32.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.COLORREF,
                                              wintypes.BYTE, wintypes.DWORD]
user32.SetLayeredWindowAttributes.restype = wintypes.BOOL
user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.SetWindowDisplayAffinity.restype = wintypes.BOOL

# DPI-aware ДО import pygame: SDL при загрузке фиксирует awareness процесса,
# повторный SetProcessDpiAwarenessContext позже уже не сработает (возвращает
# ошибку). Без этого Windows масштабирует окно: 125% → 3072x1728 вместо
# 3840x2160, кадр не совпадает с экраном. Проверено диагностикой.
try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
except Exception:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

import numpy as np
import pygame

from overlay_ui import OverlayMenu, palette as ui_palette

from i18n import STRINGS

# --- Brand palette (DLSS5-Video-Converter) -------------------------------
BG_COLOR = (0x0D, 0x11, 0x17)      # #0D1117 dark background
BG_ALPHA = 235                     # HUD panel translucency (почти непрозрачный — текст не сливается)
WDA_EXCLUDEFROMCAPTURE = 0x00000011

# Цвет-ключ прозрачности для HUD-режима: пиксели ровно этого цвета layered-окно
# не рисует вовсе, сквозь них виден оверлей воркера. Взят заведомо не
# встречающийся в палитре HUD (#0D1117 / #FFBF00 / #E6EDF3 / #8B949E).
CHROMA_KEY = (0xFF, 0x00, 0xFF)
LWA_COLORKEY = 0x1
LWA_ALPHA = 0x2

FONT_NAME = "consolas"
# Базовые размеры вёрстки заданы для 1440p. На экранах выше интерфейс
# масштабируется, ниже — остаётся как есть: уменьшать уже некуда, текст
# станет нечитаемым. Отсюда правило «только вверх» (см. ui_scale).
FONT_SIZE = 18
UI_BASE_HEIGHT = 1800
ALERT_FONT_SIZE = 28


def ui_scale_for(height: int) -> float:
    """Множитель интерфейса по высоте экрана.

    Только вверх: на 1800p и ниже — 1.0, на 4K — 1.2. Пропорциональное
    уменьшение на 1080p сделало бы шрифт девятипиксельным, поэтому вниз не
    масштабируем. База поднята с 1440 до 1800 — на 1.5 интерфейс выходил
    крупноват.
    """
    return max(1.0, float(height) / UI_BASE_HEIGHT)


class Display:
    """Fullscreen borderless window that renders frames + branded HUD."""

    def __init__(self, width: int, height: int, fullscreen: bool = True, click_through: bool = True):
        # DPI-awareness уже установлена на уровне модуля (до import pygame).
        # Здесь повторный вызов не сработает — оставлен как fallback для
        # случаев, когда модуль импортирован без верхнего блока.
        try:
            ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:
            pass
        # SDL-хинты ДО pygame.init():
        # SDL_WINDOWS_DPI_AWARENESS=permonitorv2 — без этого Windows
        # масштабирует окно (125% → 3072x1728 вместо 3840x2160).
        # SDL_MOUSE_FOCUS_CLICKTHROUGH=1 — клик по окну не перехватывается SDL
        # (окно не активируется, фокус не уводится) — клики проходят
        # к приложениям под оверлеем.
        try:
            os.environ["SDL_WINDOWS_DPI_AWARENESS"] = "permonitorv2"
        except Exception:
            pass
        try:
            os.environ["SDL_MOUSE_FOCUS_CLICKTHROUGH"] = "1"
        except Exception:
            pass
        pygame.init()
        pygame.display.set_caption("NeuralScreen")
        # Borderless windowed вместо FULLSCREEN: fullscreen-окно pygame при
        # клике/фокусе теряет отрисовку (экран замирает, цикл крутится).
        # Окно размером с монитор, позиция (0,0) — визуально то же самое,
        # но стабильно и click-through работает.
        flags = pygame.NOFRAME
        self.screen = pygame.display.set_mode((width, height), flags)
        self.width, self.height = self.screen.get_size()
        self._move_to_origin()
        # Принудительный физический размер окна: даже если DPI-awareness
        # не применился (окно 3072x1728 вместо 3840x2160), растягиваем
        # окно до запрошенного размера — pygame-поверхность совпадёт.
        try:
            hwnd = pygame.display.get_wm_info()["window"]
            ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, width, height, 0x0004)  # SWP_NOZORDER
        except Exception:
            pass
        self._set_topmost()
        self.clock = pygame.time.Clock()
        self._hud: Dict = {}
        self._alerts: List[tuple[str, float]] = []  # (текст, expires_at)
        # Масштаб интерфейса и производные от него размеры вёрстки.
        self.ui_scale = ui_scale_for(self.height)
        self.font_size = max(8, int(round(FONT_SIZE * self.ui_scale)))
        # Меню настроек живёт в этом же слое. Отдельное окно поверх игры
        # воровало бы фокус и дралось за topmost, а тут мы уже поверх кадра
        # и уже прозрачны по ключу.
        self.menu = OverlayMenu(self.ui_scale, self._load_font)
        self._font = self._load_font(size=self.font_size)
        self._alert_font = self._load_font(
            size=max(10, int(round(ALERT_FONT_SIZE * self.ui_scale))))
        # Отключаем vsync: flip() не должен ждать vblank (иначе FPS привязан
        # к частоте монитора и теряются кадры на медленном конвейере).
        try:
            pygame.display.set_swap_interval(0)
        except Exception:
            pass
        self._excluded = self._exclude_from_capture()
        self._click_through = False
        if click_through:
            self._set_click_through()
        self._lang = "ru"  # язык HUD (NR ON/NR OFF), см. set_lang()
        self._visible = True
        # HUD-режим: кадр рисует воркер, окно показывает только HUD
        self._hud_only = False
        self._last_overlay = 0.0
        self._last_alert_count = 0

    def _sync_cursor(self) -> None:
        """Курсор под зоной меню: стрелки перемещения и растягивания.

        Ставим только при смене — set_cursor на каждом кадре заметно
        мигает курсором.
        """
        want = (self.menu.desired_cursor if self.menu.visible
                else pygame.SYSTEM_CURSOR_ARROW)
        if want == getattr(self, "_cursor", None):
            return
        self._cursor = want
        try:
            pygame.mouse.set_cursor(want)
        except Exception:
            pass

    @property
    def theme(self) -> dict:
        """Палитра темы меню — алерты держим в том же виде."""
        return ui_palette(self.menu.state.get("theme", "light"))

    def get_hwnd(self) -> int:
        """HWND окна оверлея — родитель для нативных диалогов."""
        try:
            return int(pygame.display.get_wm_info()["window"])
        except Exception:
            return 0

    @staticmethod
    def _rgb(color: str) -> tuple[int, int, int]:
        c = color.lstrip("#")
        return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)

    def set_lang(self, lang: str) -> None:
        """Сменить язык HUD-статуса (en/ru)."""
        if lang in STRINGS and lang != self._lang:
            self._lang = lang

    def set_visible(self, visible: bool) -> None:
        """Показать/скрыть окно (SW_SHOW/SW_HIDE).

        При NR OFF окно прячется полностью — рабочий стол не тормозит
        (нет захвата/блита/flip). При NR ON — показывается снова.
        """
        try:
            hwnd = pygame.display.get_wm_info()["window"]
            user32.ShowWindow(hwnd, 5 if visible else 0)  # SW_SHOW=5, SW_HIDE=0
            self._visible = visible
        except Exception:
            pass

    def is_visible(self) -> bool:
        return getattr(self, "_visible", True)

    def _set_topmost(self) -> None:
        """HWND_TOPMOST — окно всегда поверх остальных (оверлей)."""
        try:
            hwnd = pygame.display.get_wm_info()["window"]
            ctypes.windll.user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0,
                                              0x0001 | 0x0002)  # SWP_NOSIZE|NOMOVE
        except Exception:
            pass

    def _move_to_origin(self) -> None:
        """Переместить окно в (0,0) — верхний левый угол монитора."""
        try:
            hwnd = pygame.display.get_wm_info()["window"]
            ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                                              0x0001 | 0x0002 | 0x0040)  # SWP_NOSIZE|NOMOVE|SHOWWINDOW
        except Exception:
            pass

    # -- window plumbing --------------------------------------------------

    def _load_font(self, size: int = FONT_SIZE):
        try:
            return pygame.font.SysFont(FONT_NAME, size)
        except Exception:
            return pygame.font.Font(None, size)

    def _set_click_through(self) -> bool:
        """Click-through: WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_LAYERED.

        Проверено диагностикой (_work/diag_click.py, Win11 4K): WS_EX_TRANSPARENT
        БЕЗ WS_EX_LAYERED НЕ работает — WindowFromPoint всё равно возвращает
        оверлей, клик идёт оверлею. Рабочая комбинация (по MSDN "Layered
        Windows"): layered-окно + WS_EX_TRANSPARENT → hit-testing игнорирует
        форму окна и клики уходят окну под курсором.

        Порядок обязателен:
          1. SetWindowLongW(GWL_EXSTYLE, ... | WS_EX_LAYERED | WS_EX_TRANSPARENT
             | WS_EX_NOACTIVATE)
          2. SetLayeredWindowAttributes(alpha=255, LWA_ALPHA) — активирует
             layered-режим (без вызова окно остаётся обычным, флаг LAYERED
             в exstyle есть, но hit-testing не меняется)
          3. SetWindowPos(SWP_FRAMECHANGED) — сбрасывает кэш стилей окна
             (требование документации SetWindowLongW)
        WS_EX_NOACTIVATE: окно не забирает фокус, клавиатура остаётся у
        активного приложения. Управление — глобальные хоткеи
        (GetAsyncKeyState в main.py).
        """
        try:
            hwnd = pygame.display.get_wm_info()["window"]
            GWL_EXSTYLE = -20
            WS_EX_TRANSPARENT = 0x00000020
            WS_EX_NOACTIVATE = 0x08000000
            WS_EX_LAYERED = 0x00080000
            LWA_ALPHA = 0x2
            SWP_NOMOVE = 0x0002
            SWP_NOSIZE = 0x0001
            SWP_NOZORDER = 0x0004
            SWP_FRAMECHANGED = 0x0020
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                                  style | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_LAYERED)
            # Активировать layered-режим: alpha=255 (непрозрачное окно,
            # только hit-testing меняется, визуально ничего не трогаем)
            user32.SetLayeredWindowAttributes(hwnd, 0, 255, LWA_ALPHA)
            # Сбросить кэш стилей — без SWP_FRAMECHANGED изменения
            # SetWindowLongW могут не примениться
            user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED)
            self._click_through = True
            return True
        except Exception as exc:
            print(f"Display: WARNING click-through failed: {exc}")
            return False

    def _exclude_from_capture(self) -> bool:
        """SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE) on the window.

        Makes the window invisible to screen capture (dxcam, OBS, etc.).
        Best-effort: warn on failure, never crash.
        """
        try:
            hwnd = pygame.display.get_wm_info()["window"]
            user32 = ctypes.windll.user32
            ok = user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
            if not ok:
                print("Display: WARNING SetWindowDisplayAffinity failed "
                      "(window may be visible to screen capture)")
                return False
            return True
        except Exception as exc:
            print(f"Display: WARNING cannot exclude window from capture: {exc}")
            return False

    # -- public API -------------------------------------------------------

    def set_menu_input(self, enabled: bool) -> None:
        """Пропускать ли ввод в наш слой (пока открыто меню).

        Обычно окно click-through: WS_EX_TRANSPARENT отдаёт клики тому, что
        под ним. На время меню флаг снимается — клики достаются нам, а игра
        под оверлеем их не получает. Ровно поведение ReShade.
        WS_EX_NOACTIVATE снимаем тоже, иначе не будет клавиатуры.
        """
        try:
            hwnd = pygame.display.get_wm_info()["window"]
        except Exception as exc:
            print(f"Display: WARNING нет hwnd для ввода в меню: {exc}")
            return
        GWL_EXSTYLE = -20
        WS_EX_TRANSPARENT = 0x00000020
        WS_EX_NOACTIVATE = 0x08000000
        SWP_NOMOVE, SWP_NOSIZE, SWP_NOZORDER, SWP_FRAMECHANGED = 0x2, 0x1, 0x4, 0x20
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if enabled:
            style &= ~(WS_EX_TRANSPARENT | WS_EX_NOACTIVATE)
        else:
            style |= WS_EX_TRANSPARENT | WS_EX_NOACTIVATE
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
        user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED)
        if enabled:
            # Фокус нужен для клавиатуры. Мышь работает и без него — клик
            # уходит окну под курсором, раз оно больше не прозрачное.
            try:
                user32.SetForegroundWindow(hwnd)
                user32.SetActiveWindow(hwnd)
            except Exception:
                pass
        self._click_through = not enabled

    def set_menu_opaque(self, opaque: bool) -> None:
        """Убрать глобальную полупрозрачность окна, пока открыто меню.

        Слой живёт с альфой BG_ALPHA, чтобы HUD не залеплял картинку. Но
        панель меню почти чёрная, и сквозь неё просвечивает яркий кадр —
        читается как «слишком прозрачное». На время меню ставим 255.
        """
        if not self._hud_only:
            return
        try:
            hwnd = pygame.display.get_wm_info()["window"]
        except Exception:
            return
        r, g, b = CHROMA_KEY
        key = (b << 16) | (g << 8) | r
        alpha = 255 if opaque else BG_ALPHA
        user32.SetLayeredWindowAttributes(hwnd, key, alpha, LWA_COLORKEY | LWA_ALPHA)

    def set_hud_only(self, enabled: bool, force: bool = False) -> None:
        """HUD-режим: кадр рисует воркер в своём окне, тут остаётся только HUD.

        Фон заливается CHROMA_KEY и делается прозрачным через
        SetLayeredWindowAttributes(LWA_COLORKEY) — сквозь него виден оверлей
        воркера, а HUD, алерты и водяной знак рисуются поверх как раньше.
        Выключение возвращает обычный непрозрачный режим (LWA_ALPHA).
        force=True: переприменить атрибуты даже если режим не менялся —
        z-order-операции (SetWindowPos/TopMost после меню настроек) могут
        сбросить LWA_COLORKEY, а ранний return оставил бы окно непрозрачным.
        """
        if enabled == self._hud_only and not force:
            return
        self._hud_only = enabled
        try:
            hwnd = pygame.display.get_wm_info()["window"]
        except Exception as exc:
            print(f"Display: WARNING не получить hwnd для HUD-режима: {exc}")
            return
        if enabled:
            # COLORREF — это 0x00BBGGRR, а не RGB
            r, g, b = CHROMA_KEY
            key = (b << 16) | (g << 8) | r
            # Прозрачность панели — через ГЛОБАЛЬНУЮ альфу окна (LWA_ALPHA),
            # а НЕ через альфу пикселей панели: полупрозрачный бленд SRCALPHA
            # с magenta-фоном дал бы цвет ≠ key и розовую плашку (colorkey
            # не вырезает смешанный цвет). Колоркей убирает фон целиком,
            # а непрозрачная панель (+текст) становится слегка просвечивающей
            # за счёт глобальной альфы.
            ok = user32.SetLayeredWindowAttributes(hwnd, key, BG_ALPHA, LWA_COLORKEY | LWA_ALPHA)
        else:
            ok = user32.SetLayeredWindowAttributes(hwnd, 0, 255, LWA_ALPHA)
        if not ok:
            print(f"Display: WARNING SetLayeredWindowAttributes failed "
                  f"(HUD-режим {'вкл' if enabled else 'выкл'})")
        self._last_overlay = 0.0  # ближайший draw_overlay перерисует немедленно

    def raise_topmost(self) -> None:
        """Поднять окно над окном воркера.

        Оба окна topmost, и внутри этой группы наверху оказывается поднятое
        последним. Воркер создаёт своё окно позже нас, поэтому после каждого
        поднятия его оверлея HUD нужно вернуть наверх, иначе он окажется под
        кадром и станет невидим.
        """
        self._set_topmost()

    def refresh_colorkey(self) -> None:
        """Переприменить LWA_COLORKEY на окне pygame (HUD-слой).

        Нужно после операций, которые могут сбросить layered-атрибуты окна
        (z-order-перестановки с tkinter-меню настроек: окно остаётся
        непрозрачным, HUD не виден). Ничего не пересоздаёт — только
        SetLayeredWindowAttributes, в отличие от set_hud_only(force=True).
        """
        if not self._hud_only:
            return
        try:
            hwnd = pygame.display.get_wm_info()["window"]
        except Exception:
            return
        r, g, b = CHROMA_KEY
        key = (b << 16) | (g << 8) | r
        user32.SetLayeredWindowAttributes(hwnd, key, BG_ALPHA, LWA_COLORKEY | LWA_ALPHA)

    def draw_capture_overlay(self, surface: pygame.Surface) -> None:
        """Наложить открытое меню на кадр записи или скриншота.

        Кадр снимается БЕЗ нашего pygame-слоя: окно помечено
        WDA_EXCLUDEFROMCAPTURE, иначе DDA снимал бы собственный вывод.
        Поэтому всё, что должно попасть в файл, рисуется здесь, поверх
        уже полученных пикселей.

        Пекём только меню. HUD и водяной знак раньше запекались, но на
        экране их нет — в файле они выглядели как чужая надпись.
        """
        if not self.menu.visible:
            return
        # Подсветка «взялся за край» — состояние курсора, в файле ей не
        # место: она бы застыла на кадре без видимой причины.
        hover, self.menu.hover = self.menu.hover, None
        try:
            self.menu.set_stats(self._hud)
            self.menu.draw(surface)
        finally:
            self.menu.hover = hover

    def draw_overlay(self, min_interval: float = 0.1) -> None:
        """Перерисовать HUD поверх кадра, который показывает воркер.

        Троттлинг: заливка 4K + flip стоит ~5 мс, а HUD меняется пару раз в
        секунду — на каждом кадре это выброшенное время. Алерты появляются
        и исчезают вне очереди, для них перерисовка немедленная.
        """
        try:
            pygame.event.pump()
        except Exception:
            pass
        now = time.monotonic()
        alerts = len(self._alerts)
        # С открытым меню троттлинг выключаем: 10 Гц хватает статике HUD,
        # но ползунок под мышью на такой частоте дёргается.
        if self.menu.visible:
            min_interval = 0.0
        if now - self._last_overlay < min_interval and alerts == self._last_alert_count:
            return
        self._last_overlay = now
        self._last_alert_count = alerts
        self.screen.fill(CHROMA_KEY)
        self._draw_alerts()
        self.menu.set_stats(self._hud)
        self.menu.draw(self.screen)
        self._sync_cursor()
        pygame.display.flip()

    def set_hud(self, data: dict) -> None:
        """Update HUD data: fps, status, resolution, params, frames."""
        self._hud = dict(data)

    def alert(self, text: str, duration: float = 2.5) -> None:
        """Показать всплывающий алерт по центру экрана (янтарная рамка)."""
        self._alerts.append((text, time.monotonic() + duration))

    def show(self, frame_rgba: np.ndarray) -> None:
        """Blit frame (RGBA uint8) fullscreen and draw the HUD on top.

        Кадр: pygame.image.frombuffer(frame_rgba, ..., "RGBX") — zero-copy
        (surface ссылается на numpy-буфер, без tobytes/копии 33 МБ на 4K).
        Формат RGBX (32-бит, без альфа-канала): blit на экран быстрее, чем
        RGBA-поверхность с SRCALPHA (не нужен альфа-блендинг). Альфа кадра
        не используется — кадр непрозрачный (A=255).

        ВАЖНО: frombuffer НЕ копирует данные — surface живёт, пока жив
        numpy-буфер. Кадр приходит из WorkerReader отдельным массивом на
        каждый кадр, поэтому поверхность создаётся заново (2 мс на 4K).
        event.pump() вычитывает очередь сообщений Windows (WM_PAINT и др.) —
        без этого окно замирает при клике/фокусе.
        """
        if frame_rgba is None:
            return
        try:
            pygame.event.pump()
        except Exception:
            pass
        try:
            surface = pygame.image.frombuffer(
                frame_rgba, (frame_rgba.shape[1], frame_rgba.shape[0]), "RGBX")
        except Exception:
            # Фолбэк: не numpy-массив / несовместимая форма — через tobytes
            surface = pygame.image.frombuffer(
                frame_rgba.tobytes(), (frame_rgba.shape[1], frame_rgba.shape[0]),
                "RGBA")
        # Один blit+flip для обоих путей (основной RGBX и фолбэк RGBA)
        self.screen.blit(surface, (0, 0))
        self._draw_alerts()
        self.menu.set_stats(self._hud)
        self.menu.draw(self.screen)
        self._sync_cursor()
        pygame.display.flip()

    def poll_events(self) -> List[str]:
        """Return event names: 'quit' (Esc / window close), 'toggle' (F9)."""
        events = []
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                events.append("quit")
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    events.append("quit")
                elif event.key == pygame.K_F9:
                    events.append("toggle")
        return events

    def close(self) -> None:
        pygame.quit()

    # -- HUD --------------------------------------------------------------

    def _draw_alerts(self) -> None:
        """Всплывающий алерт: палитра меню, сверху по центру.

        Раньше висел на трети высоты по центру тёмной плашкой с янтарной
        рамкой — выбивался из общего вида и лез в середину кадра.
        """
        now = time.monotonic()
        self._alerts = [(text, expires) for text, expires in self._alerts if expires > now]
        if not self._alerts:
            return
        c = self.theme
        text, _ = self._alerts[-1]
        surf = self._alert_font.render(text, True, self._rgb(c["text"]))
        pad_x = int(round(26 * self.ui_scale))
        pad_y = int(round(14 * self.ui_scale))
        w = surf.get_width() + pad_x * 2
        h = surf.get_height() + pad_y * 2
        rect = pygame.Rect((self.width - w) // 2, int(round(self.height * 0.045)), w, h)
        radius = int(round(10 * self.ui_scale))
        # Панель непрозрачная: полупрозрачность смешалась бы с цветом-ключом
        # и дала грязный оттенок (colorkey не вырезает смешанный цвет).
        pygame.draw.rect(self.screen, self._rgb(c["bg"]), rect, border_radius=radius)
        pygame.draw.rect(self.screen, self._rgb(c["border"]), rect,
                         max(1, int(round(self.ui_scale))), border_radius=radius)
        self.screen.blit(surf, (rect.x + pad_x, rect.y + pad_y))

def main() -> int:
    """Standalone smoke test: gradient frames + HUD until Esc/F9."""
    disp = Display(2560, 1440)
    disp.set_hud({
        "fps": 60.0,
        "status": "NR ON",
        "resolution": "2560x1440",
        "params": {"intensity": 0.5, "local_tone": 0.3,
                   "local_structure": 0.7},
        "frames": 0,
    })
    h, w = disp.height, disp.width
    yy, xx = np.mgrid[0:h, 0:w]
    frame = np.zeros((h, w, 4), dtype=np.uint8)
    frame[..., 0] = (xx * 255 // max(w - 1, 1)).astype(np.uint8)   # B
    frame[..., 1] = (yy * 255 // max(h - 1, 1)).astype(np.uint8)   # G
    frame[..., 2] = 40                                              # R
    frame[..., 3] = 255                                             # A
    n = 0
    running = True
    while running:
        disp.show(frame)
        for ev in disp.poll_events():
            if ev == "quit":
                running = False
        n += 1
        disp.set_hud({"fps": disp.clock.get_fps(), "status": "NR ON",
                      "resolution": f"{w}x{h}",
                      "params": {"intensity": 0.5, "local_tone": 0.3,
                                 "local_structure": 0.7},
                      "frames": n})
        disp.clock.tick(60)
    disp.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
