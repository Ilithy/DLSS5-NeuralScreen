"""OverlayMenu — меню настроек прямо в оверлейном слое, как в ReShade.

Рисуется на той же pygame-поверхности, что HUD и алерты, поэтому окон
по-прежнему одно: никакой борьбы за topmost и фокус с игрой.

Разделение обязанностей: модуль СТРОИТ РАСКЛАДКУ и РИСУЕТ её. Он не трогает
pygame.display, не читает события и ничего не знает про воркер. Раскладка —
плоский список элементов с прямоугольниками, он же служит таблицей попаданий
для мыши (hit()).

Вёрстка построчная: у каждого контрола своя строка подписи и своя строка
самого контрола. Раньше они делили одну строку, и подпись налезала на
значение и на стрелки.

Все размеры заданы в базовых единицах 1440p и умножаются на scale — тот же
множитель, что у HUD (display.ui_scale_for).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import pygame

from i18n import STRINGS

# --- Темы. Акцент общий, меняются фон и текст ----------------------------
THEMES = {
    "light": {
        "bg": "#F0EEE6",       # тёплый кремовый фон панели
        "surface": "#E8E5DC",  # дорожки ползунков, поля
        "border": "#DCD8CC",
        "text": "#191919",
        "muted": "#79776F",
        "accent": "#D97757",   # глиняный акцент
        "danger": "#BC4C2E",
    },
    "dark": {
        "bg": "#262624",
        "surface": "#32312E",
        "border": "#403E3A",
        "text": "#F5F4EF",
        "muted": "#A3A099",
        "accent": "#D97757",
        "danger": "#E06C4F",
    },
}


def palette(theme: str) -> dict:
    """Палитра темы. Нужна и алертам в display.py — тот же вид."""
    return THEMES.get(theme, THEMES["light"])

# --- Базовая вёрстка (единицы 1440p) --------------------------------------
PANEL_W = 540
PAD = 26
TITLE_H = 54
LABEL_H = 24
CTRL_H = 26
ROW_GAP = 20
SECTION_GAP = 14
SLIDER_H = 6
KNOB_R = 9
BTN_H = 42
BTN_PAD = 18
BTN_GAP = 10
STAT_LINE_H = 24
STAT_PAD = 14
RADIUS = 10

FONT_SIZE = 17
TITLE_SIZE = 21
SMALL_SIZE = 14

PARAM_KEYS = ("intensity", "local_tone", "local_structure", "skin_structure")
PARAM_MIN, PARAM_MAX = 0.0, 2.5
SKIN_MIN = -1.0


def _rgb(color: str) -> tuple[int, int, int]:
    c = color.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


@dataclass
class Item:
    """Элемент раскладки: что это, где лежит, к чему относится."""
    kind: str                      # "slider" | "button" | "toggle" | "choice"
    key: str
    rect: pygame.Rect              # область попадания мыши
    lo: float = 0.0
    hi: float = 1.0
    value: float = 0.0
    payload: Any = None
    extra: dict = field(default_factory=dict)


class OverlayMenu:
    """Меню в оверлее: видимость, состояние, раскладка, отрисовка."""

    def __init__(self, scale: float, font_loader: Callable[[int], Any]):
        self.scale = scale
        # Ручной множитель: нужен до создания шрифтов, они считают размер через _u
        self.user_scale = 1.0
        self._load_font = font_loader
        self.visible = False
        self.lang = "en"
        self.state: dict = {
            "nr": True,
            "work_scale": 1.0,
            "profile": "",
            "profiles": [],
            "params": {},
            "recording": False,
            "work_size": "",
            "theme": "light",
            "rec_seconds": 0.0,
            "open_on_start": True,
        }
        # Показания конвейера: то же, что в HUD. Меню задумано как одно
        # место, где видно и настройки, и что происходит.
        self.stats: dict = {}
        self.items: list[Item] = []
        self._font = font_loader(self._u(FONT_SIZE))
        self._title_font = font_loader(self._u(TITLE_SIZE))
        self._small_font = font_loader(self._u(SMALL_SIZE))
        self.panel_rect = pygame.Rect(0, 0, 0, 0)
        self._stats_rect = pygame.Rect(0, 0, 0, 0)
        # Панель можно таскать за заголовок и тянуть за угол. Смещение
        # хранится относительно центра экрана, поэтому переживает смену
        # разрешения без «уехавшего за край» окна.
        self.offset = [0, 0]
        self._grip = pygame.Rect(0, 0, 0, 0)
        self._title_bar = pygame.Rect(0, 0, 0, 0)
        self._move_from = None
        self._resize_from = None
        # Какой список сейчас раскрыт (профиль / язык / тема). Стрелками
        # перебирать неудобно, когда вариантов больше двух.
        self.open_choice: str | None = None
        # Что под курсором: "title" (можно тащить) или "grip" (растягивать).
        # Без подсветки эти зоны невидимы и их не найти.
        self.hover: str | None = None

    @property
    def c(self) -> dict:
        """Цвета текущей темы."""
        return palette(self.state.get("theme", "light"))

    # -- вспомогательное ---------------------------------------------------

    def _u(self, base: float) -> int:
        """Базовые единицы 1440p -> пиксели экрана."""
        return max(1, int(round(base * self.scale * self.user_scale)))

    def set_user_scale(self, value: float) -> None:
        """Ручное растягивание панели. Шрифты приходится пересоздавать."""
        value = min(2.0, max(0.6, round(value, 2)))
        if abs(value - self.user_scale) < 0.01:
            return
        self.user_scale = value
        self._font = self._load_font(self._u(FONT_SIZE))
        self._title_font = self._load_font(self._u(TITLE_SIZE))
        self._small_font = self._load_font(self._u(SMALL_SIZE))

    def toggle(self) -> bool:
        self.visible = not self.visible
        if not self.visible:
            self._drag_item = None
        return self.visible

    @property
    def dragging(self) -> bool:
        """Тащат ли сейчас ползунок — во время перетаскивания состояние
        обновлять нельзя, иначе значение будет прыгать между тем, что
        показывает мышь, и тем, что уже применил main."""
        return getattr(self, "_drag_item", None) is not None

    def set_stats(self, hud: dict) -> None:
        self.stats = dict(hud or {})

    def set_state(self, payload: dict) -> None:
        for k, v in payload.items():
            if k == "lang":
                self.lang = v
            elif k == "params" and isinstance(v, dict):
                self.state["params"] = dict(v)
            elif k in self.state:
                self.state[k] = v

    # -- раскладка ---------------------------------------------------------

    def layout(self, screen_w: int, screen_h: int) -> None:
        """Пересчитать прямоугольники.

        Считаем сверху вниз в относительных координатах, в конце узнаём
        высоту и сдвигаем всё разом — так высота панели не может разойтись
        с содержимым (раньше она задавалась формулой и отставала).
        """
        s = STRINGS.get(self.lang, STRINGS["en"])
        w = self._u(PANEL_W)
        pad = self._u(PAD)
        label_h = self._u(LABEL_H)
        ctrl_h = self._u(CTRL_H)
        gap = self._u(ROW_GAP)
        inner_w = w - pad * 2

        items: list[Item] = []
        cy = self._u(TITLE_H) + self._u(SECTION_GAP)

        # Блок показаний
        stat_h = self._u(STAT_LINE_H) * 2 + self._u(STAT_PAD) * 2
        self._stats_rel = pygame.Rect(pad, cy, inner_w, stat_h)
        cy += stat_h + gap

        # NR вкл/выкл — одна строка
        items.append(Item("toggle", "nr", pygame.Rect(pad, cy, inner_w, ctrl_h),
                          value=1.0 if self.state.get("nr") else 0.0))
        cy += ctrl_h + gap

        def slider(key: str, lo: float, hi: float, value: float,
                   label: str, hint: str = "", value_text: str = "") -> None:
            nonlocal cy
            items.append(Item("slider", key,
                              pygame.Rect(pad, cy, inner_w, label_h + ctrl_h),
                              lo=lo, hi=hi, value=value,
                              extra={"label": label, "hint": hint,
                                     "value_text": value_text,
                                     "label_h": label_h}))
            cy += label_h + ctrl_h + (self._u(SMALL_SIZE) + 4 if hint else 0) + gap

        # Масштаб обработки из меню убран намеренно: замеры показали, что
        # время NGX от него не зависит (15.9-16.1 мс на всём диапазоне), то
        # есть регулировать нечего — работаем на максимуме. Значение осталось
        # в config.json и на Ctrl+Alt+стрелках для экспериментов.

        def choice(key: str, label: str, current: str, options: list) -> None:
            nonlocal cy
            items.append(Item("choice", key,
                              pygame.Rect(pad, cy, inner_w, label_h + ctrl_h),
                              payload=list(options),
                              extra={"label": label, "current": current,
                                     "label_h": label_h}))
            cy += label_h + ctrl_h + gap

        choice("profile", s["profile"], str(self.state.get("profile", "")),
               list(self.state.get("profiles") or []))

        params = self.state.get("params") or {}
        for key in PARAM_KEYS:
            lo = SKIN_MIN if key == "skin_structure" else PARAM_MIN
            val = float(params.get(key, 0.0))
            slider(key, lo, PARAM_MAX, val, s[key], value_text=f"{val:.2f}")

        choice("lang", s["language"], self.lang, ["en", "ru"])
        choice("theme", s["theme"], self.state.get("theme", "light"),
               ["light", "dark"])

        items.append(Item("toggle", "open_on_start",
                          pygame.Rect(pad, cy, inner_w, ctrl_h),
                          value=1.0 if self.state.get("open_on_start") else 0.0,
                          extra={"label": s["open_on_start"]}))
        cy += ctrl_h + gap

        # Хоткеи: узнать про них больше неоткуда, кроме README
        hint_h = self._u(SMALL_SIZE) + self._u(6)
        self._hotkeys_rel = pygame.Rect(pad, cy, inner_w, hint_h * 2)
        cy += hint_h * 2 + gap

        # Кнопки
        btn_h = self._u(BTN_H)
        c = self.c
        row = [("screenshot", s["screenshot"], c["text"]),
               ("github", s["github"], c["text"]),
               ("record", s["record_stop"] if self.state.get("recording") else s["record"],
                c["danger"] if self.state.get("recording") else c["text"]),
               ("exit", s["exit"], c["danger"])]
        # Кнопки текут построчно и переносятся, когда следующая не влезает.
        # Ширина подписей плавает: «Остановить запись» вдвое шире «Запись»,
        # плюс перевод — без переноса кнопки вылезали за край панели.
        close_label = s["close"]
        cw = self._font.size(close_label)[0] + self._u(BTN_PAD) * 2
        widths = [self._font.size(lbl)[0] + self._u(BTN_PAD) * 2 for _, lbl, _ in row]
        bgap = self._u(BTN_GAP)

        bx = pad
        for (key, label, color), bw in zip(row, widths):
            if bx > pad and bx + bw > pad + inner_w:
                bx = pad
                cy += btn_h + bgap
            items.append(Item("button", key, pygame.Rect(bx, cy, bw, btn_h),
                              extra={"label": label, "color": color}))
            bx += bw + bgap
        # «Закрыть» — всегда у правого края: в конце текущей строки, если
        # там осталось место, иначе на своей.
        if bx + cw > pad + inner_w:
            cy += btn_h + bgap
        items.append(Item("button", "close",
                          pygame.Rect(pad + inner_w - cw, cy, cw, btn_h),
                          extra={"label": close_label, "color": c["text"]}))
        cy += btn_h + pad

        # Высота известна — сдвигаем всё в экранные координаты
        h = cy
        # Смещение от центра + зажим, чтобы панель нельзя было утащить
        # за край экрана целиком.
        x = (screen_w - w) // 2 + self.offset[0]
        y = (screen_h - h) // 2 + self.offset[1]
        x = min(max(x, -w + self._u(80)), screen_w - self._u(80))
        y = min(max(y, 0), max(0, screen_h - self._u(60)))
        self.panel_rect = pygame.Rect(x, y, w, h)
        self._title_bar = pygame.Rect(x, y, w, self._u(TITLE_H))
        grip = self._u(26)
        self._grip = pygame.Rect(x + w - grip, y + h - grip, grip, grip)
        self._stats_rect = self._stats_rel.move(x, y)
        self._hotkeys_rect = self._hotkeys_rel.move(x, y)
        for it in items:
            it.rect = it.rect.move(x, y)
            if it.kind == "choice":
                # Поле выбора считаем здесь, а не при отрисовке: раскладка
                # раскрытого списка строится до первого draw.
                it.extra["strip"] = pygame.Rect(
                    it.rect.x, it.rect.y + label_h, it.rect.w, it.rect.h - label_h)
        self.items = items

        # Пункты раскрытого списка. Лежат поверх нижележащих строк, поэтому
        # добавляются последними и проверяются первыми при попадании мыши.
        self.options: list[Item] = []
        if self.open_choice:
            src = next((i for i in items if i.key == self.open_choice), None)
            if src is not None:
                strip = src.extra.get("strip")
                if strip is not None:
                    oh = self._u(CTRL_H) + self._u(6)
                    for idx, opt in enumerate(src.payload or []):
                        self.options.append(Item(
                            "option", src.key,
                            pygame.Rect(strip.x, strip.bottom + self._u(4) + idx * oh,
                                        strip.w, oh),
                            payload=opt,
                            extra={"label": str(opt),
                                   "selected": str(opt) == str(src.extra.get("current"))}))

    # -- ввод --------------------------------------------------------------

    def handle_event(self, event) -> list[tuple]:
        """Обработать событие pygame. Возвращает список действий для main.

        Действия: ("nr",), ("param", key, value), ("profile", name),
        ("lang", code), ("theme", name), ("button", key), ("drag", dx, dy).
        Сам модуль меняет только своё состояние отображения — всё
        остальное решает main, у него источник правды.
        """
        if not self.visible:
            return []
        out: list[tuple] = []
        if event.type == pygame.MOUSEMOTION:
            if self._grip.collidepoint(event.pos):
                self.hover = "grip"
            elif self._title_bar.collidepoint(event.pos):
                self.hover = "title"
            else:
                self.hover = None
        if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            if self._grip.collidepoint(event.pos):
                self._resize_from = (event.pos, self.user_scale)
                return out
            if self._title_bar.collidepoint(event.pos):
                self._move_from = (event.pos, tuple(self.offset))
                return out
            item = self.hit(event.pos)
            if item is None:
                self._drag_item = None
                self.open_choice = None
                return out
            if item.kind == "toggle":
                out.append(("nr",) if item.key == "nr" else ("toggle", item.key))
            elif item.kind == "button":
                out.append(("button", item.key))
            elif item.kind == "option":
                out.extend(self._pick(item.key, str(item.payload)))
                self.open_choice = None
            elif item.kind == "choice":
                self.open_choice = None if self.open_choice == item.key else item.key
            elif item.kind == "slider":
                self._drag_item = item
                out.extend(self._slide(item, event.pos[0]))
        elif event.type == pygame.MOUSEMOTION and self._move_from is not None:
            start, base = self._move_from
            self.offset = [base[0] + event.pos[0] - start[0],
                           base[1] + event.pos[1] - start[1]]
        elif event.type == pygame.MOUSEMOTION and self._resize_from is not None:
            start, base = self._resize_from
            # Тянем вправо-вниз — панель растёт. Шаг подобран так, чтобы
            # проход по диагонали экрана давал примерно двукратный размер.
            delta = ((event.pos[0] - start[0]) + (event.pos[1] - start[1])) / 900.0
            self.set_user_scale(base + delta)
        elif event.type == pygame.MOUSEMOTION and getattr(self, "_drag_item", None):
            if event.buttons and event.buttons[0]:
                out.extend(self._slide(self._drag_item, event.pos[0]))
            else:
                self._drag_item = None
        elif event.type == pygame.MOUSEBUTTONUP and event.button == 1:
            self._drag_item = None
            self._move_from = None
            self._resize_from = None
        elif event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
            out.append(("button", "close"))
        return out

    def _pick(self, key: str, value: str) -> list[tuple]:
        """Выбран пункт списка."""
        if key == "profile":
            return [("profile", value)]
        if key == "lang":
            return [("lang", value)]
        if key == "theme":
            self.state["theme"] = value
            return [("theme", value)]
        return []

    def _slide(self, item: Item, mouse_x: int) -> list[tuple]:
        """Значение ползунка по позиции мыши, с округлением до шага 0.05."""
        track = item.extra.get("track")
        if track is None or track.w <= 0:
            return []
        frac = min(1.0, max(0.0, (mouse_x - track.x) / track.w))
        value = item.lo + frac * (item.hi - item.lo)
        value = round(round(value / 0.05) * 0.05, 2)
        if abs(value - item.value) < 1e-9:
            return []
        item.value = value
        params = dict(self.state.get("params") or {})
        params[item.key] = value
        self.state["params"] = params
        return [("param", item.key, value)]

    @property
    def desired_cursor(self):
        """Курсор под текущей зоной. Ставит display — он владеет pygame."""
        if self.hover == "grip" or self._resize_from is not None:
            return pygame.SYSTEM_CURSOR_SIZENWSE
        if self.hover == "title" or self._move_from is not None:
            return pygame.SYSTEM_CURSOR_SIZEALL
        return pygame.SYSTEM_CURSOR_ARROW

    def hit(self, pos: tuple[int, int]) -> Item | None:
        for opt in getattr(self, "options", []):
            if opt.rect.collidepoint(pos):
                return opt
        for item in self.items:
            if item.rect.collidepoint(pos):
                return item
        return None

    def inside(self, pos: tuple[int, int]) -> bool:
        return self.panel_rect.collidepoint(pos)

    # -- отрисовка ---------------------------------------------------------

    def draw(self, surface: pygame.Surface) -> None:
        if not self.visible:
            return
        self.layout(surface.get_width(), surface.get_height())
        s = STRINGS.get(self.lang, STRINGS["en"])
        pad = self._u(PAD)
        r = self.panel_rect

        pygame.draw.rect(surface, _rgb(self.c["bg"]), r, border_radius=self._u(RADIUS))
        pygame.draw.rect(surface, _rgb(self.c["border"]), r, self._u(1),
                         border_radius=self._u(RADIUS))

        if self.hover == "title" or self._move_from is not None:
            # Заголовок — ручка перетаскивания. Подсвечиваем полосой и
            # акцентной кромкой: иначе о ней никак не догадаться.
            tb = self._title_bar
            pygame.draw.rect(surface, _rgb(self.c["surface"]), tb,
                             border_top_left_radius=self._u(RADIUS),
                             border_top_right_radius=self._u(RADIUS))
            pygame.draw.line(surface, _rgb(self.c["accent"]),
                             (tb.x + self._u(RADIUS), tb.bottom - 1),
                             (tb.right - self._u(RADIUS), tb.bottom - 1),
                             max(2, self._u(2)))
        title = self._title_font.render(s["title"], True, _rgb(self.c["text"]))
        surface.blit(title, (r.x + pad, r.y + self._u(16)))
        brand = self._small_font.render("@perseval_BLR", True, _rgb(self.c["muted"]))
        surface.blit(brand, (r.right - pad - brand.get_width(),
                             r.y + self._u(22)))

        self._draw_stats(surface)
        self._draw_hotkeys(surface, s)
        # Уголок растягивания: три коротких штриха, как принято у ресайза
        g = self._grip
        active = self.hover == "grip" or self._resize_from is not None
        if active:
            # Подложка под уголком: три штриха сами по себе теряются на фоне
            # панели, и зону не видно, пока в неё не ткнёшь.
            pad = pygame.Surface(g.size, pygame.SRCALPHA)
            pygame.draw.rect(pad, (*_rgb(self.c["accent"]), 46),
                             pad.get_rect(),
                             border_bottom_right_radius=self._u(RADIUS))
            surface.blit(pad, g.topleft)
        color = self.c["accent"] if active else self.c["muted"]
        width = max(2, self._u(3 if active else 2))
        step = max(3, self._u(6))
        for i in range(1, 4):
            off = i * step
            pygame.draw.line(surface, _rgb(color),
                             (g.right - off, g.bottom - self._u(3)),
                             (g.right - self._u(3), g.bottom - off), width)
        for item in self.items:
            {"toggle": self._draw_toggle, "slider": self._draw_slider,
             "choice": self._draw_choice, "button": self._draw_button}[item.kind](
                surface, item, s)
        self._draw_options(surface)

    def _draw_stats(self, surface, *_):
        st = self.stats or {}
        rect = self._stats_rect
        pygame.draw.rect(surface, _rgb(self.c["surface"]), rect,
                         border_radius=self._u(RADIUS // 2))
        fps = st.get("fps")
        rows = (
            (("FPS", f"{fps:.1f}" if isinstance(fps, (int, float)) else "—"),
             ("RES", str(st.get("resolution", "—"))),
             ("WORK", str(self.state.get("work_size", "—")))),
            (("FRAMES", str(st.get("frames", "—"))),
             ("REC", self._rec_text()),
             ("PROFILE", str(self.state.get("profile", "—")).split(" /")[0])),
        )
        pad = self._u(STAT_PAD)
        cell = (rect.w - pad * 2) // 3
        for ri, row in enumerate(rows):
            y = rect.y + pad + ri * self._u(STAT_LINE_H)
            for ci, (name, value) in enumerate(row):
                cx = rect.x + pad + ci * cell
                k = self._small_font.render(name, True, _rgb(self.c["muted"]))
                v = self._small_font.render(value, True, _rgb(self.c["accent"]))
                surface.blit(k, (cx, y))
                surface.blit(v, (cx + k.get_width() + self._u(6), y))

    def _rec_text(self) -> str:
        """Состояние записи: длительность полезнее, чем просто «on»."""
        if not self.state.get("recording"):
            return "off"
        secs = float(self.state.get("rec_seconds", 0.0))
        return f"{int(secs) // 60:d}:{int(secs) % 60:02d}"

    def _draw_hotkeys(self, surface, s: dict) -> None:
        """Строка с хоткеями. Иначе про F9/Insert/Ctrl+Alt+Q узнать неоткуда."""
        rect = getattr(self, "_hotkeys_rect", None)
        if rect is None:
            return
        text = s.get("hotkeys", "")
        line = self._small_font.render(text, True, _rgb(self.c["muted"]))
        surface.blit(line, (rect.x, rect.y))

    def _draw_toggle(self, surface, item: Item, s: dict) -> None:
        on = item.value > 0.5
        size = self._u(20)
        box = pygame.Rect(item.rect.x, item.rect.centery - size // 2, size, size)
        pygame.draw.rect(surface, _rgb(self.c["accent"] if on else self.c["surface"]), box,
                         border_radius=self._u(4))
        if not on:
            pygame.draw.rect(surface, _rgb(self.c["border"]), box, self._u(1),
                             border_radius=self._u(4))
        text = item.extra.get("label")
        if not text:
            text = s["nr_on"] if on else s["nr_off"]
        label = self._font.render(text, True,
                                  _rgb(self.c["text"] if on else self.c["muted"]))
        surface.blit(label, (box.right + self._u(12),
                             item.rect.centery - label.get_height() // 2))

    def _draw_slider(self, surface, item: Item, s: dict) -> None:
        label_h = item.extra.get("label_h", self._u(LABEL_H))
        label = self._font.render(item.extra.get("label", item.key), True, _rgb(self.c["text"]))
        surface.blit(label, (item.rect.x, item.rect.y))
        value_text = item.extra.get("value_text") or f"{item.value:.2f}"
        val = self._font.render(value_text, True, _rgb(self.c["accent"]))
        surface.blit(val, (item.rect.right - val.get_width(), item.rect.y))

        track_y = item.rect.y + label_h + self._u(10)
        track = pygame.Rect(item.rect.x, track_y, item.rect.w, self._u(SLIDER_H))
        pygame.draw.rect(surface, _rgb(self.c["surface"]), track,
                         border_radius=self._u(SLIDER_H // 2 or 1))
        span = max(1e-6, item.hi - item.lo)
        frac = min(1.0, max(0.0, (item.value - item.lo) / span))
        fill = pygame.Rect(track.x, track.y, int(track.w * frac), track.h)
        pygame.draw.rect(surface, _rgb(self.c["accent"]), fill,
                         border_radius=self._u(SLIDER_H // 2 or 1))
        cx = int(track.x + frac * track.w)
        pygame.draw.circle(surface, _rgb(self.c["accent"]), (cx, track.centery), self._u(KNOB_R))
        pygame.draw.circle(surface, _rgb(self.c["bg"]), (cx, track.centery), self._u(KNOB_R) // 2)
        item.extra["track"] = track

        hint = item.extra.get("hint")
        if hint:
            h = self._small_font.render(hint, True, _rgb(self.c["muted"]))
            surface.blit(h, (item.rect.x, track.bottom + self._u(6)))

    def _draw_choice(self, surface, item: Item, s: dict) -> None:
        label_h = item.extra.get("label_h", self._u(LABEL_H))
        label = self._font.render(item.extra.get("label", item.key), True, _rgb(self.c["text"]))
        surface.blit(label, (item.rect.x, item.rect.y))

        strip = pygame.Rect(item.rect.x, item.rect.y + label_h,
                            item.rect.w, item.rect.h - label_h)
        pygame.draw.rect(surface, _rgb(self.c["surface"]), strip,
                         border_radius=self._u(RADIUS // 2))
        pygame.draw.rect(surface, _rgb(self.c["border"]), strip, self._u(1),
                         border_radius=self._u(RADIUS // 2))
        cur = self._font.render(str(item.extra.get("current", "")), True,
                                _rgb(self.c["text"]))
        surface.blit(cur, (strip.x + self._u(12),
                           strip.centery - cur.get_height() // 2))
        # Треугольник-стрелка справа: список раскрывается, а не перебирается
        cx = strip.right - self._u(16)
        cy = strip.centery
        size = self._u(5)
        up = self.open_choice == item.key
        pts = ([(cx - size, cy + size // 2), (cx + size, cy + size // 2), (cx, cy - size)]
               if up else
               [(cx - size, cy - size // 2), (cx + size, cy - size // 2), (cx, cy + size)])
        pygame.draw.polygon(surface, _rgb(self.c["accent"]), pts)
        item.extra["strip"] = strip

    def _draw_options(self, surface) -> None:
        """Пункты раскрытого списка — поверх остального содержимого."""
        for opt in getattr(self, "options", []):
            selected = opt.extra.get("selected")
            pygame.draw.rect(surface,
                             _rgb(self.c["accent"] if selected else self.c["bg"]),
                             opt.rect, border_radius=self._u(RADIUS // 2))
            pygame.draw.rect(surface, _rgb(self.c["border"]), opt.rect, self._u(1),
                             border_radius=self._u(RADIUS // 2))
            color = self.c["bg"] if selected else self.c["text"]
            label = self._font.render(opt.extra.get("label", ""), True, _rgb(color))
            surface.blit(label, (opt.rect.x + self._u(12),
                                 opt.rect.centery - label.get_height() // 2))

    def _draw_button(self, surface, item: Item, s: dict) -> None:
        pygame.draw.rect(surface, _rgb(self.c["surface"]), item.rect,
                         border_radius=self._u(RADIUS // 2))
        pygame.draw.rect(surface, _rgb(self.c["border"]), item.rect, self._u(1),
                         border_radius=self._u(RADIUS // 2))
        label = self._font.render(item.extra.get("label", item.key), True,
                                  _rgb(item.extra.get("color", self.c["text"])))
        surface.blit(label, (item.rect.centerx - label.get_width() // 2,
                             item.rect.centery - label.get_height() // 2))
