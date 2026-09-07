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

import math
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
        "ok": "#5E8C61",       # зелёный индикатора поддержки
        "danger": "#BC4C2E",
    },
    "dark": {
        "bg": "#262624",
        "surface": "#32312E",
        "border": "#403E3A",
        "text": "#F5F4EF",
        "muted": "#A3A099",
        "accent": "#D97757",
        "ok": "#7FB07F",
        "danger": "#E06C4F",
    },
}


# Что показываем в переназначении и в каком порядке. Слева — команда, под
# которой хоткей живёт в hotkeys.DEFAULT_BINDINGS и в config["hotkeys"].
HOTKEY_ROWS = (
    ("toggle", "hk_nr"),
    ("settings", "hk_menu"),
    ("screenshot_menu", "hk_shot"),
    ("record", "hk_record"),
    ("quit", "hk_quit"),
)


# pygame.key.name() даёт «page up», а разбор в hotkeys.parse_binding ждёт
# «PGUP». Расходятся только эти.
_KEY_ALIASES = {"page up": "PGUP", "page down": "PGDN",
                "return": "ENTER", "escape": "ESC"}


def key_text(event) -> str | None:
    """Событие клавиатуры -> строка вида «Ctrl+Alt+Q» для parse_binding.

    None — если нажат только модификатор: биндинг из одного Ctrl не бывает.
    """
    name = pygame.key.name(event.key)
    if name in ("left ctrl", "right ctrl", "left alt", "right alt",
                "left shift", "right shift", "left meta", "right meta"):
        return None
    base = _KEY_ALIASES.get(name, name.upper())
    mods = pygame.key.get_mods()
    parts = []
    if mods & pygame.KMOD_CTRL:
        parts.append("Ctrl")
    if mods & pygame.KMOD_ALT:
        parts.append("Alt")
    if mods & pygame.KMOD_SHIFT:
        parts.append("Shift")
    parts.append(base)
    return "+".join(parts)


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
ACTION_H = 46      # кнопка действия: название + подпись хоткея под ним
EXIT_H = 64        # выход: ещё и пояснение третьей строкой
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
            "split": 0.0,
            # Что за карта и работает ли на ней NR. gpu_ok: True/False/None
            # (None — воркер ещё не ответил).
            "gpu_text": "",
            "gpu_ok": None,
            "monitor": "0",
            "monitors": [],
            "autostart": False,
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
        # Высота панели: None — по содержимому. Задаётся протяжкой нижней
        # кромки; содержимое выше высоты прокручивается.
        self.user_height: int | None = None
        self.scroll = 0
        self.content_height = 0
        self._max_scroll = 0
        self._resize_h_from = None
        # Последняя позиция мыши: колесо в pygame приходит без координат, а
        # pygame.mouse.get_pos() в click-through окне доверия не вызывает.
        self._mouse = (0, 0)
        self._edge = pygame.Rect(0, 0, 0, 0)
        self._viewport = pygame.Rect(0, 0, 0, 0)
        self._scroll_track = pygame.Rect(0, 0, 0, 0)
        self._scroll_thumb = pygame.Rect(0, 0, 0, 0)
        # Какой список сейчас раскрыт (профиль / язык / тема). Стрелками
        # перебирать неудобно, когда вариантов больше двух.
        self.open_choice: str | None = None
        # Страница меню: основное окно или настройки за шестерёнкой.
        self.page = "main"
        # Команда, для которой сейчас ждём нажатие клавиши (или None).
        self.capturing: str | None = None
        # Подписи хоткеев: команда -> «F9». Приходят из main вместе с
        # биндингами, поэтому переназначение видно на кнопках сразу.
        self.hotkeys: dict = {}
        self._sections: list = []
        self._hint_rel = pygame.Rect(0, 0, 0, 0)
        self._rule_rel = pygame.Rect(0, 0, 0, 0)
        self._rule2_rel = pygame.Rect(0, 0, 0, 0)
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

    def set_hotkeys(self, mapping: dict) -> None:
        """Подписи хоткеев: команда -> «F9». Источник — реальные биндинги."""
        self.hotkeys = dict(mapping or {})

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
        # Иконки в шапке: справка и настройки. Крестика нет намеренно — он
        # закрывал меню и стоял рядом с выходом из программы.
        ir = self._u(15)
        icons = [("gear", pad + inner_w - ir), ("help", pad + inner_w - ir - self._u(38))]
        if self.page == "settings":
            icons = [("close", pad + inner_w - ir)]
        for kind, ix in icons:
            items.append(Item("icon", kind,
                              pygame.Rect(ix - ir, self._u(18) - ir, ir * 2, ir * 2),
                              extra={"r": ir}))
        cy = self._u(TITLE_H) + self._u(SECTION_GAP)

        # Блок показаний
        stat_h = self._u(STAT_LINE_H) * 2 + self._u(STAT_PAD) * 2
        self._stats_rel = pygame.Rect(pad, cy, inner_w, stat_h)
        cy += stat_h + self._u(6)

        # Строка про GPU: точка состояния и модель карты. Отдельной строкой, а
        # не ячейкой в блоке показаний — это не показание конвейера, а ответ на
        # вопрос «а на моей карте это вообще работает».
        gpu_h = self._u(SMALL_SIZE) + self._u(8)
        self._gpu_rel = pygame.Rect(pad, cy, inner_w, gpu_h)
        cy += gpu_h + gap

        # Содержимое разбито на озаглавленные блоки: восемь однотипных строк
        # подряд глазу не за что было зацепить. Заголовки не интерактивны,
        # поэтому живут отдельным списком, а не в items.
        self._sections: list[tuple[str, pygame.Rect]] = []
        sec_h = self._u(SMALL_SIZE) + self._u(10)

        def section(title: str) -> None:
            nonlocal cy
            cy += self._u(6)
            self._sections.append((title, pygame.Rect(pad, cy, inner_w, sec_h)))
            cy += sec_h

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

        def choice(key: str, label: str, current: str, options: list) -> None:
            nonlocal cy
            items.append(Item("choice", key,
                              pygame.Rect(pad, cy, inner_w, label_h + ctrl_h),
                              payload=list(options),
                              extra={"label": label, "current": current,
                                     "label_h": label_h}))
            cy += label_h + ctrl_h + gap

        def segmented(key: str, label: str, current: str, options: list,
                      labels: list | None = None) -> None:
            """Переключатель на два-три варианта — вместо выпадающего списка.

            Список ради двух значений это лишний клик и лишняя механика
            раскрытия; здесь оба варианта видны сразу.
            """
            nonlocal cy
            seg_w = min(inner_w - self._u(150), self._u(60) * len(options) + self._u(60))
            rect = pygame.Rect(pad + inner_w - seg_w, cy, seg_w, ctrl_h)
            items.append(Item("segmented", key, rect, payload=list(options),
                              extra={"label": label, "current": current,
                                     "labels": list(labels or options)}))
            cy += ctrl_h + gap

        def toggle(key: str, label: str, on: bool) -> None:
            nonlocal cy
            items.append(Item("toggle", key,
                              pygame.Rect(pad, cy, inner_w, ctrl_h),
                              value=1.0 if on else 0.0,
                              extra={"label": label}))
            cy += ctrl_h + gap

        if self.page == "settings":
            section(s["sec_capture"])
            monitors = self.state.get("monitors") or []
            if monitors:
                choice("monitor", s.get("monitor", "Monitor"),
                       str(self.state.get("monitor", "0")), monitors)

            section(s["sec_behaviour"])
            toggle("open_on_start", s["open_on_start"],
                   bool(self.state.get("open_on_start")))
            toggle("autostart", s.get("autostart", "Autostart with Windows"),
                   bool(self.state.get("autostart")))

            section(s["sec_hotkeys"])
            # Поля переназначения. Подписи на кнопках берутся из этих же
            # значений, поэтому смена клавиши видна сразу во всём меню.
            field_h = self._u(CTRL_H)
            for cmd, label in HOTKEY_ROWS:
                items.append(Item("hotkey", cmd,
                                  pygame.Rect(pad, cy, inner_w, field_h),
                                  extra={"label": s.get(label, label),
                                         "key": self.hotkeys.get(cmd, "—"),
                                         "capturing": self.capturing == cmd}))
                cy += field_h + self._u(6)
            cy += gap
            self._hint_rel = pygame.Rect(pad, cy, inner_w,
                                         self._u(SMALL_SIZE) + self._u(6))
            cy += self._hint_rel.h + gap
        else:
            section(s["sec_processing"])
            nr_on = bool(self.state.get("nr"))
            hk_nr = self.hotkeys.get("toggle", "")
            toggle("nr", f"{s['nr_on'] if nr_on else s['nr_off']}   {hk_nr}".rstrip(),
                   nr_on)
            choice("profile", s["profile"], str(self.state.get("profile", "")),
                   list(self.state.get("profiles") or []))
            params = self.state.get("params") or {}
            for key in PARAM_KEYS:
                lo = SKIN_MIN if key == "skin_structure" else PARAM_MIN
                val = float(params.get(key, 0.0))
                slider(key, lo, PARAM_MAX, val, s[key], value_text=f"{val:.2f}")

            section(s["sec_compare"])
            split_val = float(self.state.get("split", 0.0))
            slider("split", 0.0, 1.0, split_val, s["split"], hint=s["split_hint"],
                   value_text=("выкл" if split_val <= 0.0 and self.lang == "ru"
                               else "off" if split_val <= 0.0
                               else f"{split_val:.2f}"))

            section(s["sec_view"])
            segmented("lang", s["language"], self.lang, ["en", "ru"],
                      ["EN", "RU"])
            segmented("theme", s["theme"], self.state.get("theme", "light"),
                      ["light", "dark"], [s["theme_light"], s["theme_dark"]])

        # Подвал: действия с подписью хоткея. Раньше «Закрыть» и «Выход»
        # выглядели одинаково безобидно, хотя одно прячет меню, а другое
        # выгружает программу.
        cy += self._u(6)
        self._rule_rel = pygame.Rect(pad, cy, inner_w, 1)
        cy += self._u(14)
        act_h = self._u(ACTION_H)
        if self.page == "settings":
            items.append(Item("action", "back",
                              pygame.Rect(pad, cy, inner_w, act_h),
                              extra={"label": s["back"],
                                     "hotkey": self.hotkeys.get("settings", ""),
                                     "filled": False}))
            cy += act_h + pad
        else:
            row = [("screenshot", s["screenshot"], self.hotkeys.get("screenshot_menu", ""), False),
                   ("record", s["record_stop"] if self.state.get("recording")
                    else s["record"], self.hotkeys.get("record", ""), True),
                   ("collapse", s["collapse"], self.hotkeys.get("settings", ""), False)]
            bgap = self._u(BTN_GAP)
            bw = (inner_w - bgap * (len(row) - 1)) // len(row)
            for idx, (key, label, hk, filled) in enumerate(row):
                items.append(Item("action", key,
                                  pygame.Rect(pad + idx * (bw + bgap), cy, bw, act_h),
                                  extra={"label": label, "hotkey": hk,
                                         "filled": filled}))
            cy += act_h + self._u(16)
            self._rule2_rel = pygame.Rect(pad, cy, inner_w, 1)
            cy += self._u(14)
            exit_h = self._u(EXIT_H)
            items.append(Item("action", "exit",
                              pygame.Rect(pad, cy, inner_w, exit_h),
                              extra={"label": s["exit_full"],
                                     "hotkey": self.hotkeys.get("quit", ""),
                                     "note": s["exit_note"], "danger": True}))
            cy += exit_h + pad

        # Высота содержимого известна. Панель может быть ниже — тогда
        # содержимое прокручивается: на 1080p полная панель занимала почти
        # весь экран, а деться от этого было некуда.
        content_h = cy
        title_h = self._u(TITLE_H)
        min_h = title_h + self._u(140)
        # Выше содержимого не растягиваем: пустое место внизу выглядит
        # сломанным, а не просторным.
        h = content_h if self.user_height is None else int(self.user_height)
        h = max(min(h, content_h, max(0, screen_h - self._u(40))), min(min_h, content_h))
        self.content_height = content_h
        self._max_scroll = max(0, content_h - h)
        self.scroll = min(max(self.scroll, 0), self._max_scroll)
        # Смещение от центра + зажим, чтобы панель нельзя было утащить
        # за край экрана целиком.
        x = (screen_w - w) // 2 + self.offset[0]
        y = (screen_h - h) // 2 + self.offset[1]
        x = min(max(x, -w + self._u(80)), screen_w - self._u(80))
        y = min(max(y, 0), max(0, screen_h - self._u(60)))
        self.panel_rect = pygame.Rect(x, y, w, h)
        self._title_bar = pygame.Rect(x, y, w, title_h)
        grip = self._u(26)
        self._grip = pygame.Rect(x + w - grip, y + h - grip, grip, grip)
        # Нижняя кромка тянет высоту, уголок остаётся за масштаб — поэтому
        # зона кромки не доходит до уголка.
        edge = self._u(7)
        self._edge = pygame.Rect(x, y + h - edge, max(0, w - grip), edge)
        self._viewport = pygame.Rect(x, y + title_h, w, max(0, h - title_h))
        # Всё содержимое живёт со сдвигом на прокрутку; заголовок — нет.
        sy = y - self.scroll
        self._stats_rect = self._stats_rel.move(x, sy)
        self._gpu_rect = self._gpu_rel.move(x, sy)
        self._hint_rect = self._hint_rel.move(x, sy)
        self._rule_rect = self._rule_rel.move(x, sy)
        self._rule2_rect = self._rule2_rel.move(x, sy)
        self._section_rects = [(t, r.move(x, sy)) for t, r in self._sections]
        if self._max_scroll > 0:
            bar_w = max(2, self._u(3))
            view_h = self._viewport.h
            track = pygame.Rect(x + w - self._u(7) - bar_w,
                                y + title_h + self._u(4),
                                bar_w, max(1, view_h - self._u(8)))
            thumb_h = max(self._u(26), int(track.h * view_h / content_h))
            travel = track.h - thumb_h
            ty = track.y + int(travel * (self.scroll / self._max_scroll))
            self._scroll_track = track
            self._scroll_thumb = pygame.Rect(track.x, ty, bar_w, thumb_h)
        else:
            self._scroll_track = pygame.Rect(0, 0, 0, 0)
            self._scroll_thumb = pygame.Rect(0, 0, 0, 0)
        for it in items:
            # Иконки шапки прибиты к панели, а не к содержимому: они лежат
            # выше области прокрутки, и вместе с ней уезжали под заголовок.
            it.rect = it.rect.move(x, y if it.kind == "icon" else sy)
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
        if self.capturing is not None and event.type == pygame.KEYDOWN:
            # Пока ждём клавишу, клавиатура принадлежит полю. Esc — отмена,
            # иначе меню закрылось бы вместо отмены назначения.
            if event.key == pygame.K_ESCAPE:
                self.capturing = None
                out.append(("capture", None))
                return out
            text = key_text(event)
            if text is None:
                return out
            cmd, self.capturing = self.capturing, None
            out.append(("hotkey", cmd, text))
            out.append(("capture", None))
            return out
        if event.type == pygame.MOUSEMOTION:
            self._mouse = event.pos
            if self._grip.collidepoint(event.pos):
                self.hover = "grip"
            elif self._edge.collidepoint(event.pos):
                self.hover = "edge"
            elif self._title_bar.collidepoint(event.pos):
                self.hover = "title"
                for it in self.items:
                    if it.kind == "icon" and it.rect.collidepoint(event.pos):
                        self.hover = f"icon:{it.key}"
                        break
            else:
                self.hover = None
                for it in self.items:
                    if it.kind in ("action", "hotkey") and \
                            it.rect.collidepoint(event.pos):
                        self.hover = f"{it.kind}:{it.key}"
                        break
        if event.type == pygame.MOUSEWHEEL:
            # Прокрутка только когда курсор над панелью: иначе колесо в игре
            # уезжало бы в меню.
            if self._max_scroll > 0 and self.panel_rect.collidepoint(self._mouse):
                self.scroll = min(max(self.scroll - event.y * self._u(48), 0),
                                  self._max_scroll)
            return out
        if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            # Иконки лежат в шапке, а шапка — ручка перетаскивания, которая
            # возвращается сразу. Поэтому иконки проверяем первыми.
            for it in self.items:
                if it.kind == "icon" and it.rect.collidepoint(event.pos):
                    out.extend(self._icon_click(it.key))
                    return out
            if self._grip.collidepoint(event.pos):
                self._resize_from = (event.pos, self.user_scale)
                return out
            if self._edge.collidepoint(event.pos):
                # Тянем высоту. Если высота ещё не задавалась, берём текущую
                # — иначе первый же пиксель протяжки схлопнул бы панель.
                base = self.user_height if self.user_height is not None \
                    else self.panel_rect.h
                self._resize_h_from = (event.pos[1], int(base))
                return out
            if self._title_bar.collidepoint(event.pos):
                self._move_from = (event.pos, tuple(self.offset))
                return out
            item = self.hit(event.pos)
            if item is None:
                self._drag_item = None
                self.open_choice = None
                return out
            if item.kind == "action":
                out.extend(self._action_click(item.key))
            elif item.kind == "hotkey":
                self.capturing = item.key
                out.append(("capture", item.key))
            elif item.kind == "segmented":
                cells = item.extra.get("cells") or []
                for idx, cr in enumerate(cells):
                    if cr.collidepoint(event.pos) and idx < len(item.payload or []):
                        out.extend(self._pick(item.key, str(item.payload[idx])))
                        break
            elif item.kind == "toggle":
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
        elif event.type == pygame.MOUSEMOTION and self._resize_h_from is not None:
            start_y, base = self._resize_h_from
            self.user_height = max(1, base + event.pos[1] - start_y)
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
            if self._resize_h_from is not None:
                self._resize_h_from = None
                # Раскладка зажимает высоту содержимым и экраном — забираем
                # зажатое значение, чтобы в конфиг не уехало сырое.
                self.user_height = (None if self._max_scroll == 0
                                    else self.panel_rect.h)
        elif event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
            out.append(("button", "close"))
        return out

    def _icon_click(self, key: str) -> list[tuple]:
        """Шапка: справка, вход в настройки, возврат из них."""
        if key == "help":
            return [("button", "github")]
        if key == "gear":
            self.page = "settings"
            self.scroll = 0
            self.capturing = None
            return [("capture", None)]
        if key == "close":
            self.page = "main"
            self.scroll = 0
            self.capturing = None
            return [("capture", None)]
        return []

    def _action_click(self, key: str) -> list[tuple]:
        """Подвал. «Свернуть» прячет меню, «Выход» выгружает программу —
        поэтому это разные действия с разными подписями, а не один крестик."""
        if key == "back":
            self.page = "main"
            self.scroll = 0
            self.capturing = None
            return [("capture", None)]
        if key == "collapse":
            return [("button", "close")]
        if key == "screenshot":
            return [("button", "screenshot")]
        return [("button", key)]

    def _pick(self, key: str, value: str) -> list[tuple]:
        """Выбран пункт списка."""
        if key == "profile":
            return [("profile", value)]
        if key == "lang":
            return [("lang", value)]
        if key == "theme":
            self.state["theme"] = value
            return [("theme", value)]
        if key == "monitor":
            return [("monitor", value)]
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
        if item.key == "split":
            self.state["split"] = value
            return [("split", value)]
        params = dict(self.state.get("params") or {})
        params[item.key] = value
        self.state["params"] = params
        return [("param", item.key, value)]

    @property
    def desired_cursor(self):
        """Курсор под текущей зоной. Ставит display — он владеет pygame."""
        if self.hover == "grip" or self._resize_from is not None:
            return pygame.SYSTEM_CURSOR_SIZENWSE
        if self.hover == "edge" or self._resize_h_from is not None:
            return pygame.SYSTEM_CURSOR_SIZENS
        if isinstance(self.hover, str) and self.hover.startswith(
                ("icon:", "action:", "hotkey:")):
            return pygame.SYSTEM_CURSOR_HAND
        if self.hover == "title" or self._move_from is not None:
            return pygame.SYSTEM_CURSOR_SIZEALL
        return pygame.SYSTEM_CURSOR_ARROW

    def hit(self, pos: tuple[int, int]) -> Item | None:
        # Прокрученное содержимое рисуется с обрезкой по _viewport, поэтому
        # и попадания за его пределами считать нельзя: строка, уехавшая под
        # заголовок, невидима, но её прямоугольник ещё существует.
        for it in self.items:
            if it.kind == "icon" and it.rect.collidepoint(pos):
                return it
        if self._viewport.h > 0 and not self._viewport.collidepoint(pos):
            return None
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
        head = (s.get("settings_title", "Settings") if self.page == "settings"
                else s["title"])
        title = self._title_font.render(head, True, _rgb(self.c["text"]))
        surface.blit(title, (r.x + pad, r.y + self._u(16)))
        # Автор — сразу за названием: правый верхний угол занят иконками.
        brand = self._small_font.render("· @perseval_BLR", True,
                                        _rgb(self.c["muted"]))
        surface.blit(brand, (r.x + pad + title.get_width() + self._u(10),
                             r.y + self._u(22)))

        # Содержимое рисуем с обрезкой по области прокрутки, иначе
        # прокрученные строки вылезали бы за панель.
        prev_clip = surface.get_clip()
        surface.set_clip(self._viewport)
        self._draw_stats(surface)
        self._draw_gpu(surface, s)
        self._draw_sections(surface)
        self._draw_rules(surface, s)
        # Уголок растягивания: три коротких штриха, как принято у ресайза
        g = self._grip
        active = self.hover == "grip" or self._resize_from is not None
        if active:
            # Подложка под уголком: три штриха сами по себе теряются на фоне
            # панели, и зону не видно, пока в неё не ткнёшь.
            # НЕ SRCALPHA: полупрозрачная подложка блендится с magenta-фоном
            # (CHROMA_KEY) → цвет ≠ key → colorkey не вырезает → розовая
            # плашка. Непрозрачная подложка в цвет панели вырезается вместе
            # с фоном, а акцентная рамка остаётся.
            pad = pygame.Surface(g.size)
            pygame.draw.rect(pad, _rgb(self.c["bg"]), pad.get_rect(),
                             border_bottom_right_radius=self._u(RADIUS))
            pygame.draw.rect(pad, _rgb(self.c["accent"]), pad.get_rect(),
                             self._u(1),
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
             "choice": self._draw_choice, "button": self._draw_button,
             "segmented": self._draw_segmented,
             "action": self._draw_action,
             "hotkey": self._draw_hotkey,
             # Иконки шапки лежат выше области прокрутки — рисуем их после
             # снятия обрезки, иначе их срезает.
             "icon": lambda *_: None}[item.kind](surface, item, s)
        self._draw_options(surface)
        surface.set_clip(prev_clip)
        for item in self.items:
            if item.kind == "icon":
                self._draw_icon(surface, item, s)
        self._draw_scrollbar(surface)

    def _draw_scrollbar(self, surface) -> None:
        """Тонкая полоса у правого края. Появляется только когда есть куда
        прокручивать — постоянная полоса была бы шумом."""
        if self._max_scroll <= 0:
            return
        radius = self._scroll_thumb.w // 2
        pygame.draw.rect(surface, _rgb(self.c["surface"]), self._scroll_track,
                         border_radius=radius)
        active = (self.hover in ("edge", "scroll")
                  or self._resize_h_from is not None)
        color = self.c["accent"] if active else self.c["muted"]
        pygame.draw.rect(surface, _rgb(color), self._scroll_thumb,
                         border_radius=radius)

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

    def _draw_gpu(self, surface, s: dict) -> None:
        """Точка состояния и модель карты: зелёная — NR работает, красная — нет."""
        rect = getattr(self, "_gpu_rect", None)
        if rect is None:
            return
        ok = self.state.get("gpu_ok")
        color = (self.c["muted"] if ok is None
                 else self.c["ok"] if ok else self.c["danger"])
        r = max(3, self._u(5))
        cy = rect.y + rect.h // 2
        pygame.draw.circle(surface, _rgb(color), (rect.x + r, cy), r)
        text = self.state.get("gpu_text") or "—"
        hint = (s["gpu_wait"] if ok is None
                else s["gpu_ok"] if ok else s["gpu_no"])
        name = self._small_font.render(text, True, _rgb(self.c["text"]))
        surface.blit(name, (rect.x + r * 2 + self._u(8),
                            cy - name.get_height() // 2))
        note = self._small_font.render(hint, True, _rgb(color))
        surface.blit(note, (rect.right - note.get_width(),
                            cy - note.get_height() // 2))

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

    def _draw_sections(self, surface) -> None:
        """Заголовок блока: мелкие капсы и волосяная линия до правого края."""
        for title, rect in getattr(self, "_section_rects", []):
            img = self._small_font.render(title.upper(), True,
                                          _rgb(self.c["muted"]))
            surface.blit(img, (rect.x, rect.y))
            ly = rect.y + img.get_height() // 2
            x0 = rect.x + img.get_width() + self._u(10)
            if x0 < rect.right:
                pygame.draw.line(surface, _rgb(self.c["border"]),
                                 (x0, ly), (rect.right, ly), 1)

    def _draw_rules(self, surface, s: dict) -> None:
        """Разделители перед подвалом и перед выходом, плюс подсказка."""
        for rect in (getattr(self, "_rule_rect", None),
                     getattr(self, "_rule2_rect", None)):
            if rect is not None and rect.w > 0:
                pygame.draw.line(surface, _rgb(self.c["border"]),
                                 (rect.x, rect.y), (rect.right, rect.y), 1)
        hint = getattr(self, "_hint_rect", None)
        if self.page == "settings" and hint is not None and hint.w > 0:
            img = self._small_font.render(s["hotkey_hint"], True,
                                          _rgb(self.c["muted"]))
            surface.blit(img, (hint.x, hint.y))

    def _draw_segmented(self, surface, item: Item, s: dict) -> None:
        """Два-три варианта рядом: выбранный залит акцентом."""
        label = item.extra.get("label")
        if label:
            img = self._font.render(label, True, _rgb(self.c["muted"]))
            surface.blit(img, (self.panel_rect.x + self._u(PAD),
                               item.rect.centery - img.get_height() // 2))
        pygame.draw.rect(surface, _rgb(self.c["surface"]), item.rect,
                         border_radius=self._u(RADIUS // 2))
        options = item.payload or []
        labels = item.extra.get("labels") or options
        if not options:
            return
        cell = item.rect.w // len(options)
        current = str(item.extra.get("current", ""))
        cells = []
        for idx, opt in enumerate(options):
            cr = pygame.Rect(item.rect.x + idx * cell, item.rect.y,
                             cell, item.rect.h)
            cells.append(cr)
            active = str(opt) == current
            if active:
                pygame.draw.rect(surface, _rgb(self.c["accent"]), cr,
                                 border_radius=self._u(RADIUS // 2))
            txt = self._small_font.render(
                str(labels[idx]), True,
                _rgb(self.c["bg"] if active else self.c["muted"]))
            surface.blit(txt, (cr.centerx - txt.get_width() // 2,
                               cr.centery - txt.get_height() // 2))
        item.extra["cells"] = cells

    def _draw_icon(self, surface, item: Item, s: dict) -> None:
        """Круглая иконка в шапке: справка, настройки, закрыть страницу."""
        r = item.extra.get("r", self._u(15))
        cx, cy = item.rect.centerx, item.rect.centery
        hot = self.hover == f"icon:{item.key}"
        pygame.draw.circle(surface, _rgb(self.c["surface"]), (cx, cy), r)
        col = self.c["accent"] if hot else self.c["muted"]
        if item.key == "help":
            img = self._font.render("?", True, _rgb(col))
            surface.blit(img, (cx - img.get_width() // 2,
                               cy - img.get_height() // 2))
        elif item.key == "close":
            d = max(3, self._u(5))
            pygame.draw.line(surface, _rgb(col), (cx - d, cy - d),
                             (cx + d, cy + d), max(2, self._u(2)))
            pygame.draw.line(surface, _rgb(col), (cx + d, cy - d),
                             (cx - d, cy + d), max(2, self._u(2)))
        else:
            inner = max(4, self._u(7))
            pygame.draw.circle(surface, _rgb(col), (cx, cy), inner,
                               max(2, self._u(2)))
            for i in range(8):
                a = i * math.pi / 4
                x1, y1 = cx + inner * math.cos(a), cy + inner * math.sin(a)
                x2 = cx + (inner + self._u(3)) * math.cos(a)
                y2 = cy + (inner + self._u(3)) * math.sin(a)
                pygame.draw.line(surface, _rgb(col), (int(x1), int(y1)),
                                 (int(x2), int(y2)), max(2, self._u(3)))

    def _draw_action(self, surface, item: Item, s: dict) -> None:
        """Кнопка подвала: название, под ним хоткей, у выхода — пояснение."""
        rect = item.rect
        filled = bool(item.extra.get("filled"))
        danger = bool(item.extra.get("danger"))
        hot = self.hover == f"action:{item.key}"
        radius = self._u(RADIUS // 2)
        if filled:
            pygame.draw.rect(surface, _rgb(self.c["accent"]), rect,
                             border_radius=radius)
            name_col = key_col = self.c["bg"]
        else:
            pygame.draw.rect(surface, _rgb(self.c["surface"]), rect,
                             border_radius=radius)
            pygame.draw.rect(surface,
                             _rgb(self.c["accent"] if hot else self.c["border"]),
                             rect, self._u(1), border_radius=radius)
            name_col = self.c["danger"] if danger else self.c["text"]
            key_col = self.c["muted"]
        name = self._font.render(item.extra.get("label", ""), True,
                                 _rgb(name_col))
        surface.blit(name, (rect.centerx - name.get_width() // 2,
                            rect.y + self._u(6)))
        hk = item.extra.get("hotkey")
        if hk:
            img = self._small_font.render(hk, True, _rgb(key_col))
            surface.blit(img, (rect.centerx - img.get_width() // 2,
                               rect.y + self._u(26)))
        note = item.extra.get("note")
        if note:
            img = self._small_font.render(note, True, _rgb(key_col))
            surface.blit(img, (rect.centerx - img.get_width() // 2,
                               rect.y + self._u(44)))

    def _draw_hotkey(self, surface, item: Item, s: dict) -> None:
        """Строка переназначения: действие слева, поле с клавишей справа."""
        label = self._small_font.render(item.extra.get("label", ""), True,
                                        _rgb(self.c["text"]))
        surface.blit(label, (item.rect.x,
                             item.rect.centery - label.get_height() // 2))
        fw = self._u(170)
        field = pygame.Rect(item.rect.right - fw, item.rect.y, fw, item.rect.h)
        capturing = bool(item.extra.get("capturing"))
        radius = self._u(RADIUS // 2)
        if capturing:
            pygame.draw.rect(surface, _rgb(self.c["bg"]), field,
                             border_radius=radius)
            pygame.draw.rect(surface, _rgb(self.c["accent"]), field,
                             max(2, self._u(2)), border_radius=radius)
            txt = self._small_font.render(s["hotkey_press"], True,
                                          _rgb(self.c["accent"]))
        else:
            hot = self.hover == f"hotkey:{item.key}"
            pygame.draw.rect(surface, _rgb(self.c["surface"]), field,
                             border_radius=radius)
            pygame.draw.rect(surface,
                             _rgb(self.c["accent"] if hot else self.c["border"]),
                             field, self._u(1), border_radius=radius)
            txt = self._small_font.render(str(item.extra.get("key", "—")), True,
                                          _rgb(self.c["text"]))
        surface.blit(txt, (field.centerx - txt.get_width() // 2,
                           field.centery - txt.get_height() // 2))
        item.extra["field"] = field

    def _draw_button(self, surface, item: Item, s: dict) -> None:
        pygame.draw.rect(surface, _rgb(self.c["surface"]), item.rect,
                         border_radius=self._u(RADIUS // 2))
        pygame.draw.rect(surface, _rgb(self.c["border"]), item.rect, self._u(1),
                         border_radius=self._u(RADIUS // 2))
        label = self._font.render(item.extra.get("label", item.key), True,
                                  _rgb(item.extra.get("color", self.c["text"])))
        surface.blit(label, (item.rect.centerx - label.get_width() // 2,
                             item.rect.centery - label.get_height() // 2))
