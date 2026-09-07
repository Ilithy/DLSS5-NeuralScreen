"""Строки интерфейса и фирменная палитра — общие для всех окон.

Вынесено из settings_ui, потому что то же самое нужно оверлейному меню,
а settings_ui тянет tkinter. Единственный источник правды: правки строк и
цветов делаются здесь.
"""

from __future__ import annotations

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

DEFAULT_LANG = "en"

# --- Локализация: ВСЕ строки UI + HUD/алертов ----------------------------
STRINGS = {
    "en": {
        "theme": "Theme",
        "monitor": "Monitor",
        "autostart": "Autostart with Windows",
        "autostart_on": "Autostart ON",
        "autostart_off": "Autostart OFF",
        "autostart_err": "Autostart failed",
        "open_on_start": "Open menu on launch",
        "github": "Help",
        "split": "Before / after wipe",
        "gpu_ok": "Neural Rendering works",
        "gpu_no": "Neural Rendering unavailable",
        "gpu_wait": "checking...",
        "split_hint": "share of the frame left unprocessed; 0 — off",
        "github_opened": "Opened in browser",
        "started": "NeuralScreen is running · F8 for menu",
        "hotkeys": "F9 NR · F8 menu · Insert record · Ctrl+Alt+Q quit",
        "title": "NeuralScreen — Settings",
        "work_scale": "Work scale",
        "work_scale_hint": "higher = sharper, FPS unaffected",
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
        "record": "Record",
        "record_stop": "Stop recording",
        "nr_on": "NR ON",
        "nr_off": "NR OFF",
        "record_on": "REC ● (Insert to stop)",
        "record_off": "Recording saved",
        "work_scale_changed": "Work scale {:.2f} ({:d}x{:d})",
        "settings_applied": "Settings applied",
        "lang_ru": "Русский",
        "lang_en": "English",
    },
    "ru": {
        "theme": "Тема",
        "monitor": "Монитор",
        "autostart": "Автозапуск с Windows",
        "autostart_on": "Автозапуск включён",
        "autostart_off": "Автозапуск выключен",
        "autostart_err": "Автозапуск не настроен",
        "open_on_start": "Открывать меню при запуске",
        "github": "Справка",
        "split": "Шторка до / после",
        "gpu_ok": "Neural Rendering работает",
        "gpu_no": "Neural Rendering недоступен",
        "gpu_wait": "проверяется...",
        "split_hint": "доля кадра без обработки; 0 — выключено",
        "github_opened": "Открыто в браузере",
        "started": "NeuralScreen работает · F8 — меню",
        "hotkeys": "F9 NR · F8 меню · Insert запись · Ctrl+Alt+Q выход",
        "title": "NeuralScreen — Настройки",
        "work_scale": "Масштаб обработки",
        "work_scale_hint": "выше = чётче, на FPS не влияет",
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
        "record": "Запись",
        "record_stop": "Остановить запись",
        "nr_on": "NR ВКЛ",
        "nr_off": "NR ВЫКЛ",
        "record_on": "ЗАПИСЬ ● (Insert — стоп)",
        "record_off": "Запись сохранена",
        "work_scale_changed": "Масштаб {:.2f} ({:d}x{:d})",
        "settings_applied": "Настройки применены",
        "lang_ru": "Русский",
        "lang_en": "English",
    },
}


def tr(lang: str, key: str) -> str:
    """Перевести ключ STRINGS; неизвестный ключ вернуть как есть."""
    return STRINGS.get(lang, STRINGS[DEFAULT_LANG]).get(key, key)
