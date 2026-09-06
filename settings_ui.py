"""SettingsWindow — окно настроек DLSS 5 Desktop NR (tkinter, отдельный поток).

pygame не поддерживает два окна в одном процессе, поэтому настройки —
отдельное tkinter-окно (фирменная тёмная тема) в собственном потоке.
Общение с main.py — две thread-safe очереди:

    outbox (settings -> main):
        ("settings_opened",) / ("settings_closed",)
        ("apply_settings", payload)  — payload: work_scale, profile, params, lang
        ("set_lang", lang)           — пользователь сменил язык в окне
        ("exit_app",)                — кнопка «Выход»: завершить программу

    inbox (main -> settings):
        "toggle"                     — открыть/закрыть окно (хоткей F8)
        ("sync", payload)            — обновить значения из main
        ("set_lang", lang)           — обновить язык UI (внешняя смена)
        ("lang_apply", lang)         — применить язык как выбор пользователя
        ("apply",)                   — нажать «Применить» (из main/теста)
        ("quit",)                    — закрыть поток

Фирменные цвета: фон #0D1117, акцент #FFBF00, текст #E6EDF3, muted #8B949E,
шрифт Consolas. Окно 420x520, topmost, перетаскиваемое за заголовок.
"""

from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from tkinter import ttk

# --- Фирменная палитра ---------------------------------------------------
BG = "#0D1117"            # тёмный фон
PANEL = "#161B22"         # поля ввода / комбобоксы
BORDER = "#30363D"        # разделители, рамки
ACCENT = "#FFBF00"        # янтарный акцент
ACCENT_HOVER = "#FFD24D"  # акцент при наведении
TEXT = "#E6EDF3"          # основной текст
MUTED = "#8B949E"         # приглушённый текст
DANGER = "#F85149"        # выход: действие необратимое, выделяем цветом
DANGER_HOVER = "#3D1D1D"
FONT = ("Consolas", 10)
FONT_SMALL = ("Consolas", 9)

WINDOW_W, WINDOW_H = 420, 660

WORK_SCALE_MIN, WORK_SCALE_MAX, WORK_SCALE_STEP = 0.1, 1.0, 0.05
PARAM_MIN, PARAM_MAX, PARAM_STEP = 0.0, 2.5, 0.05
# skin_structure у профилей Faithful/Natural = -1.0 — минимум ниже нуля
SKIN_MIN = -1.0
PARAM_KEYS = ("intensity", "local_tone", "local_structure", "skin_structure")

DEFAULT_LANG = "en"

# --- Локализация: ВСЕ строки UI + HUD/алерты ------------------------------
STRINGS = {
    "en": {
        "title": "NeuralScreen — Settings",
        "work_scale": "Work scale",
        "profile": "Profile",
        "intensity": "Intensity",
        "local_tone": "Local tone",
        "local_structure": "Local structure",
        "skin_structure": "Skin structure",
        "language": "Language",
        "apply": "Apply",
        "close": "Close",
        "exit": "Exit",
        "screenshot": "Screenshot",
        "nr_on": "NR ON",
        "nr_off": "NR OFF",
        "work_scale_changed": "Work scale {:.2f} ({:d}x{:d})",
        "settings_applied": "Settings applied",
        "lang_ru": "Русский",
        "lang_en": "English",
    },
    "ru": {
        "title": "NeuralScreen — Настройки",
        "work_scale": "Масштаб обработки",
        "profile": "Профиль",
        "intensity": "Интенсивность",
        "local_tone": "Локальный тон",
        "local_structure": "Локальная структура",
        "skin_structure": "Структура кожи",
        "language": "Язык",
        "apply": "Применить",
        "close": "Закрыть",
        "exit": "Выход",
        "screenshot": "Скриншот",
        "nr_on": "NR ВКЛ",
        "nr_off": "NR ВЫКЛ",
        "work_scale_changed": "Масштаб {:.2f} ({:d}x{:d})",
        "settings_applied": "Настройки применены",
        "lang_ru": "Русский",
        "lang_en": "English",
    },
}


def tr(lang: str, key: str) -> str:
    """Перевести ключ STRINGS; неизвестный ключ вернуть как есть."""
    return STRINGS.get(lang, STRINGS[DEFAULT_LANG]).get(key, key)


class SettingsWindow:
    """Окно настроек в отдельном tkinter-потоке; команды — через очереди."""

    def __init__(self, outbox: queue.Queue, initial: dict | None = None):
        self._outbox = outbox
        self._inbox: queue.Queue = queue.Queue()
        self._initial = dict(initial or {})
        self._lang = self._initial.get("lang", DEFAULT_LANG)
        if self._lang not in STRINGS:
            self._lang = DEFAULT_LANG
        self._root: tk.Tk | None = None
        self._thread: threading.Thread | None = None
        self._visible = False
        self._vars: dict = {}
        self._value_labels: dict = {}
        self._widgets: dict = {}
        self._drag = {"x": 0, "y": 0}
        self._syncing = False  # идёт запись значений из main — не применять

    # -- thread-safe API (main -> settings) --------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="settings-ui")
        self._thread.start()

    def toggle(self) -> None:
        """Открыть/закрыть окно (хоткей F8)."""
        self._inbox.put("toggle")

    def sync(self, payload: dict) -> None:
        """Обновить значения в окне из main (после apply / при открытии)."""
        self._inbox.put(("sync", dict(payload)))

    def apply(self) -> None:
        """Нажать «Применить» (команда обрабатывается в tk-потоке)."""
        self._inbox.put(("apply",))

    def set_lang(self, lang: str) -> None:
        """Обновить язык UI (внешняя смена языка)."""
        self._inbox.put(("set_lang", lang))

    def user_set_lang(self, lang: str) -> None:
        """Сменить язык как пользователь: обновить UI и уведомить main."""
        self._inbox.put(("lang_apply", lang))

    def is_visible(self) -> bool:
        return self._visible

    def stop(self) -> None:
        self._inbox.put(("quit",))

    # -- tkinter-поток -----------------------------------------------------

    def _run(self) -> None:
        self._root = tk.Tk()
        self._root.withdraw()  # скрыто до первого toggle
        self._root.overrideredirect(True)  # NOFRAME-стиль, свой заголовок
        self._root.attributes("-topmost", True)
        self._root.resizable(False, False)
        self._root.configure(bg=BG)
        self._build_style()
        self._build_ui()
        self._root.after(50, self._poll_inbox)
        self._root.mainloop()

    def _poll_inbox(self) -> None:
        try:
            while True:
                cmd = self._inbox.get_nowait()
                if cmd == "toggle":
                    self._toggle_visible()
                elif cmd == "quit":
                    self._root.destroy()
                    return
                elif isinstance(cmd, tuple):
                    name = cmd[0]
                    if name == "sync":
                        self._apply_sync(cmd[1])
                    elif name == "set_lang":
                        self._set_lang_ui(cmd[1])
                    elif name == "lang_apply":
                        self._on_lang_change(cmd[1])
                    elif name == "apply":
                        self._on_apply()
        except queue.Empty:
            pass
        self._root.after(50, self._poll_inbox)

    # -- показ/скрытие -----------------------------------------------------

    def _toggle_visible(self) -> None:
        if self._visible:
            self._hide()
        else:
            self._show()

    def _show(self) -> None:
        self._visible = True
        self._center()
        self._root.deiconify()
        self._outbox.put(("settings_opened",))

    def _hide(self) -> None:
        self._visible = False
        self._root.withdraw()
        self._outbox.put(("settings_closed",))

    def _center(self) -> None:
        self._root.update_idletasks()
        sw = self._root.winfo_screenwidth()
        sh = self._root.winfo_screenheight()
        x = max(0, (sw - WINDOW_W) // 2)
        y = max(0, (sh - WINDOW_H) // 2)
        self._root.geometry(f"{WINDOW_W}x{WINDOW_H}+{x}+{y}")

    # -- стиль -------------------------------------------------------------

    def _build_style(self) -> None:
        style = ttk.Style(self._root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("NR.TScale", background=BG, troughcolor="#21262D",
                        bordercolor=BG, lightcolor=ACCENT, darkcolor=ACCENT,
                        gripcount=0)
        # ttk.Scale ищет layout "Horizontal.<style>" — копируем из базового
        try:
            style.layout("NR.TScale", style.layout("Horizontal.TScale"))
        except tk.TclError:
            pass
        style.configure("NR.TCombobox", fieldbackground=PANEL, background=PANEL,
                        foreground=TEXT, arrowcolor=ACCENT, bordercolor=BORDER,
                        lightcolor=BG, darkcolor=BG, padding=4, font=FONT)
        style.map("NR.TCombobox", fieldbackground=[("readonly", PANEL)],
                  foreground=[("readonly", TEXT)])
        # Выпадающий список комбобокса — тоже тёмный
        self._root.option_add("*TCombobox*Listbox*Background", PANEL)
        self._root.option_add("*TCombobox*Listbox*Foreground", TEXT)
        self._root.option_add("*TCombobox*Listbox*SelectBackground", ACCENT)
        self._root.option_add("*TCombobox*Listbox*SelectForeground", BG)
        self._root.option_add("*TCombobox*Listbox*Font", FONT)

    # -- построение UI -----------------------------------------------------

    def _build_ui(self) -> None:
        s = STRINGS[self._lang]

        # Заголовок-бар (перетаскивание)
        header = tk.Frame(self._root, bg=BG, height=40)
        header.pack(fill="x")
        header.pack_propagate(False)
        dot = tk.Label(header, text="●", bg=BG, fg=ACCENT, font=FONT)
        dot.pack(side="left", padx=(14, 6), pady=10)
        self._widgets["title"] = tk.Label(header, text=s["title"], bg=BG,
                                          fg=TEXT, font=FONT)
        self._widgets["title"].pack(side="left", pady=10)
        # Пометка канала — слева вверху, приглушённая
        self._widgets["brand"] = tk.Label(header, text="@perseval_BLR", bg=BG,
                                          fg=MUTED, font=FONT_SMALL)
        self._widgets["brand"].pack(side="left", padx=(10, 0), pady=12)
        close_btn = tk.Label(header, text="✕", bg=BG, fg=MUTED, font=FONT,
                             cursor="hand2")
        close_btn.pack(side="right", padx=12, pady=8)
        close_btn.bind("<Button-1>", lambda e: self._hide())
        close_btn.bind("<Enter>", lambda e: close_btn.configure(fg=ACCENT))
        close_btn.bind("<Leave>", lambda e: close_btn.configure(fg=MUTED))
        # Метку канала тоже делаем «ручкой»: дочерний виджет перехватывает
        # клик у header, и без явной привязки окно за неё не тащилось.
        for w in (header, dot, self._widgets["title"], self._widgets["brand"]):
            w.bind("<Button-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)

        tk.Frame(self._root, bg=BORDER, height=1).pack(fill="x")

        body = tk.Frame(self._root, bg=BG)
        body.pack(fill="both", expand=True, padx=18, pady=12)
        # Переключатель NR ON/OFF — вверху, до остальных настроек
        nr_row = tk.Frame(body, bg=BG)
        nr_row.pack(fill="x", pady=(0, 8))
        self._vars["nr"] = tk.BooleanVar(value=True)
        self._widgets["nr_check"] = tk.Checkbutton(
            nr_row, text=s["nr_on"], variable=self._vars["nr"],
            bg=BG, fg=ACCENT, selectcolor=BG, activebackground=BG,
            activeforeground=ACCENT, font=FONT, bd=0, highlightthickness=0,
            command=self._on_nr_toggle)
        self._widgets["nr_check"].pack(side="left")
        self._build_row(body, "work_scale", WORK_SCALE_MIN, WORK_SCALE_MAX, WORK_SCALE_STEP)
        self._build_row(body, "profile")
        self._build_row(body, "intensity", PARAM_MIN, PARAM_MAX, PARAM_STEP)
        self._build_row(body, "local_tone", PARAM_MIN, PARAM_MAX, PARAM_STEP)
        self._build_row(body, "local_structure", PARAM_MIN, PARAM_MAX, PARAM_STEP)
        self._build_row(body, "skin_structure", SKIN_MIN, PARAM_MAX, PARAM_STEP)
        self._build_row(body, "language")

        btns = tk.Frame(self._root, bg=BG)
        btns.pack(fill="x", padx=18, pady=(0, 16))
        # Realtime-применение: кнопка «Применить» не нужна — слайдеры и
        # профиль применяются сразу (дебаунс 400 мс в _schedule_apply).
        # Слева — скриншот, справа — «Закрыть».
        self._widgets["screenshot"] = self._make_button(
            btns, s["screenshot"], "#21262D", TEXT, self._on_screenshot, BORDER)
        self._widgets["screenshot"].pack(side="left")
        self._widgets["close"] = self._make_button(
            btns, s["close"], "#21262D", TEXT, self._hide, BORDER)
        self._widgets["close"].pack(side="right")
        # «Выход» завершает программу целиком, «Закрыть» лишь прячет окно —
        # поэтому разнесены по разным краям и выход выделен красным, чтобы
        # не нажать его вместо соседней кнопки.
        self._widgets["exit"] = self._make_button(
            btns, s["exit"], "#21262D", DANGER, self._on_exit, DANGER_HOVER)
        self._widgets["exit"].pack(side="left", padx=(12, 0))

        self._apply_sync(self._initial)
        self._debounce_id = None

    def _build_row(self, parent: tk.Frame, key: str,
                   lo: float | None = None, hi: float | None = None,
                   step: float | None = None) -> None:
        s = STRINGS[self._lang]
        frame = tk.Frame(parent, bg=BG)
        frame.pack(fill="x", pady=6)
        label = tk.Label(frame, text=s[key], bg=BG, fg=TEXT, font=FONT, anchor="w")
        label.pack(fill="x")
        self._widgets[f"label_{key}"] = label

        if key in ("profile", "language"):
            self._vars[key] = tk.StringVar()
            if key == "profile":
                combo = ttk.Combobox(frame, textvariable=self._vars[key],
                                     state="readonly", style="NR.TCombobox", font=FONT)
                combo["values"] = self._initial.get("profiles") or []
                # Realtime: смена профиля применяется сразу (с дебаунсом)
                combo.bind("<<ComboboxSelected>>", lambda e: self._schedule_apply())
                combo.pack(fill="x", pady=(4, 0))
                self._widgets[key] = combo
            else:
                # Язык: сегмент-переключатель EN | RU вместо комбобокса —
                # комбобокс в ttk на тёмной теме съедается (стрелка/текст
                # сливаются с фоном), а два лейбла-кнопки всегда читаемы.
                seg = tk.Frame(frame, bg=BG)
                seg.pack(fill="x", pady=(4, 0))
                self._widgets["lang_en_btn"] = tk.Label(
                    seg, text="EN", bg=PANEL, fg=ACCENT, font=FONT, padx=14, pady=4,
                    cursor="hand2", relief="flat")
                self._widgets["lang_en_btn"].pack(side="left")
                self._widgets["lang_ru_btn"] = tk.Label(
                    seg, text="RU", bg=PANEL, fg=MUTED, font=FONT, padx=14, pady=4,
                    cursor="hand2", relief="flat")
                self._widgets["lang_ru_btn"].pack(side="left", padx=(6, 0))
                self._widgets["lang_en_btn"].bind(
                    "<Button-1>", lambda e: self._on_lang_change("en"))
                self._widgets["lang_ru_btn"].bind(
                    "<Button-1>", lambda e: self._on_lang_change("ru"))
                self._widgets[key] = seg
                self._vars[key].set("en")
        else:
            row2 = tk.Frame(frame, bg=BG)
            row2.pack(fill="x", pady=(4, 0))
            self._vars[key] = tk.DoubleVar()
            scale = ttk.Scale(row2, from_=lo, to=hi, variable=self._vars[key],
                              orient="horizontal", style="NR.TScale",
                              command=lambda v, k=key: self._on_scale(k, v))
            scale.pack(side="left", fill="x", expand=True)
            val = tk.Label(row2, text="", bg=BG, fg=ACCENT, font=FONT,
                           width=16, anchor="e")
            val.pack(side="right", padx=(10, 0))
            self._widgets[key] = scale
            self._value_labels[key] = val

    def _make_button(self, parent: tk.Frame, text: str, bg: str, fg: str,
                     cmd, hover: str) -> tk.Button:
        btn = tk.Button(parent, text=text, bg=bg, fg=fg, relief="flat", bd=0,
                        font=FONT, cursor="hand2", command=cmd, padx=20, pady=8,
                        activebackground=hover, activeforeground=fg)
        btn.bind("<Enter>", lambda e: btn.configure(bg=hover))
        btn.bind("<Leave>", lambda e: btn.configure(bg=bg))
        return btn

    # -- перетаскивание ----------------------------------------------------

    def _drag_start(self, event) -> None:
        self._drag["x"] = event.x_root - self._root.winfo_x()
        self._drag["y"] = event.y_root - self._root.winfo_y()

    def _drag_move(self, event) -> None:
        x = event.x_root - self._drag["x"]
        y = event.y_root - self._drag["y"]
        self._root.geometry(f"+{x}+{y}")

    # -- значения ----------------------------------------------------------

    def _on_nr_toggle(self) -> None:
        """NR ON/OFF из окна настроек — мгновенно, без дебаунса."""
        self._outbox.put(("toggle_nr", bool(self._vars["nr"].get())))

    def _on_exit(self) -> None:
        """Кнопка «Выход» — завершить программу, а не спрятать окно."""
        self._outbox.put(("exit_app",))

    def _on_screenshot(self) -> None:
        """Скриншот: диалог «Сохранить как» (JPEG 100%), путь — в main."""
        try:
            from tkinter import filedialog
            path = filedialog.asksaveasfilename(
                parent=self._root,
                title="Save screenshot",
                defaultextension=".jpg",
                filetypes=[("JPEG image", "*.jpg"), ("All files", "*.*")],
                initialfile=f"neuralscreen-{time.strftime('%Y%m%d-%H%M%S')}.jpg",
            )
            if path:
                self._outbox.put(("screenshot", path))
        except Exception as exc:
            print(f"Settings: screenshot dialog failed: {exc}")

    def _on_scale(self, key: str, value: str) -> None:
        """Обновить лейбл значения при движении слайдера (округление до шага)."""
        step = WORK_SCALE_STEP if key == "work_scale" else PARAM_STEP
        v = round(float(value) / step) * step
        if key == "work_scale":
            # Показываем разрешение модели (work-разрешение NGX) с учётом
            # лимита NGX: 2560x1440 максимум (на 4K feature 18 молчит)
            sw = self._initial.get("screen_w", 3840)
            sh = self._initial.get("screen_h", 2160)
            mw = max(64, int(round(sw * v / 2) * 2))
            mh = max(64, int(round(sh * v / 2) * 2))
            if mw > 2560 or mh > 1440:
                scale = min(2560 / mw, 1440 / mh)
                mw = max(64, int(round(mw * scale / 2) * 2))
                mh = max(64, int(round(mh * scale / 2) * 2))
            self._value_labels[key].configure(text=f"{v:.2f}  {mw}x{mh}")
        else:
            self._value_labels[key].configure(text=f"{v:.2f}")
        self._schedule_apply()

    def _schedule_apply(self) -> None:
        """Realtime-применение с дебаунсом 400 мс (слайдеры/профиль)."""
        if self._syncing:
            return  # значение пришло из main, а не от пользователя
        if self._debounce_id is not None:
            try:
                self._root.after_cancel(self._debounce_id)
            except Exception:
                pass
        self._debounce_id = self._root.after(400, self._on_apply)

    def _collect(self) -> dict:
        """Собрать текущие значения окна в payload apply_settings."""
        work_scale = round(round(float(self._vars["work_scale"].get()) / WORK_SCALE_STEP)
                           * WORK_SCALE_STEP, 2)
        params = {}
        for key in PARAM_KEYS:
            v = round(round(float(self._vars[key].get()) / PARAM_STEP) * PARAM_STEP, 2)
            params[key] = v
        return {
            "work_scale": work_scale,
            "profile": self._vars["profile"].get(),
            "params": params,
            "lang": self._lang,
        }

    def _on_apply(self) -> None:
        self._outbox.put(("apply_settings", self._collect()))

    # -- язык --------------------------------------------------------------

    def _on_lang_selected(self, event=None) -> None:
        s = STRINGS[self._lang]
        choice = self._vars["language"].get()
        lang = "ru" if choice == s["lang_ru"] else "en"
        self._on_lang_change(lang)

    def _on_lang_change(self, lang: str) -> None:
        """Пользователь сменил язык: обновить UI и уведомить main."""
        if lang not in STRINGS:
            lang = DEFAULT_LANG
        self._lang = lang
        self._apply_lang()
        self._outbox.put(("set_lang", lang))

    def _set_lang_ui(self, lang: str) -> None:
        """Внешняя смена языка (main -> settings): только обновить UI."""
        if lang not in STRINGS:
            lang = DEFAULT_LANG
        self._lang = lang
        self._apply_lang()

    def _apply_lang(self) -> None:
        s = STRINGS[self._lang]
        self._widgets["title"].configure(text=s["title"])
        for key in ("work_scale", "profile", "intensity", "local_tone",
                    "local_structure", "skin_structure", "language"):
            self._widgets[f"label_{key}"].configure(text=s[key])
        self._widgets["nr_check"].configure(
            text=s["nr_on"] if self._vars["nr"].get() else s["nr_off"])
        self._widgets["close"].configure(text=s["close"])
        self._widgets["exit"].configure(text=s["exit"])
        # Сегмент-переключатель: активный язык — янтарный, неактивный — muted
        self._widgets["lang_en_btn"].configure(
            fg=ACCENT if self._lang == "en" else MUTED)
        self._widgets["lang_ru_btn"].configure(
            fg=ACCENT if self._lang == "ru" else MUTED)

    # -- sync из main ------------------------------------------------------

    def _apply_sync(self, payload: dict) -> None:
        """Обёртка вокруг записи значений из main.

        ttk.Scale зовёт -command и при ПРОГРАММНОЙ записи variable, поэтому
        sync из main дёргал _on_scale -> _schedule_apply -> apply_settings
        обратно в main. Флаг разрывает эту петлю.
        """
        self._syncing = True
        try:
            self._apply_sync_impl(payload)
        finally:
            self._syncing = False

    def _apply_sync_impl(self, payload: dict) -> None:
        if not payload:
            return
        if payload.get("lang") in STRINGS:
            self._lang = payload["lang"]
        s = STRINGS[self._lang]
        if "nr" in payload:
            self._vars["nr"].set(bool(payload["nr"]))
            self._widgets["nr_check"].configure(text=s["nr_on"] if payload["nr"] else s["nr_off"])
        if "work_scale" in payload:
            v = float(payload["work_scale"])
            sw = self._initial.get("screen_w", 3840)
            sh = self._initial.get("screen_h", 2160)
            mw = max(64, int(round(sw * v / 2) * 2))
            mh = max(64, int(round(sh * v / 2) * 2))
            if mw > 2560 or mh > 1440:
                scale = min(2560 / mw, 1440 / mh)
                mw = max(64, int(round(mw * scale / 2) * 2))
                mh = max(64, int(round(mh * scale / 2) * 2))
            self._vars["work_scale"].set(v)
            self._value_labels["work_scale"].configure(text=f"{v:.2f}  {mw}x{mh}")
        if "profile" in payload:
            values = self._widgets["profile"]["values"] or ()
            if payload["profile"] in values:
                self._vars["profile"].set(payload["profile"])
        for key in PARAM_KEYS:
            if key in (payload.get("params") or {}):
                v = float(payload["params"][key])
                self._vars[key].set(v)
                self._value_labels[key].configure(text=f"{v:.2f}")
        self._apply_lang()
