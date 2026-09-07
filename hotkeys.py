"""HotkeyController — глобальные хоткеи через RegisterHotKey.

Отличие от опроса GetAsyncKeyState принципиальное: система доставляет
WM_HOTKEY только нам и НЕ передаёт нажатие активному приложению. F9 в игре
переключает NR, и сама игра этой клавиши не видит. Опрос так не умеет — он
лишь подсматривает состояние клавиши, а нажатие всё равно уходит игре.

Побочный эффект того же свойства: пока NeuralScreen запущен, F8 и F9
принадлежат ему, и другие программы (отладчики, игры) их не получат.

Комбинации со стрелками и выходом сделаны на Ctrl+Alt намеренно: голые
стрелки регистрировать нельзя — они перестали бы работать во всей системе,
а выход по одиночной клавише слишком легко нажать случайно.

RegisterHotKey(NULL, ...) кладёт WM_HOTKEY в очередь сообщений ВЫЗВАВШЕГО
потока, поэтому окно не нужно — только цикл сообщений в своём потоке.
Команды уходят в ту же очередь, что и у трея: словарь команд общий
("quit", "settings", "toggle", "scale_up", "scale_down").
"""

from __future__ import annotations

import ctypes
import queue
import threading
from ctypes import wintypes

user32 = ctypes.windll.user32

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000  # удержание клавиши не сыплет повторами

VK_F7 = 0x76
VK_F8 = 0x77
VK_F9 = 0x78
VK_UP = 0x26
VK_DOWN = 0x28
VK_Q = 0x51
VK_INSERT = 0x2D

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
PM_NOREMOVE = 0x0000

# id -> (модификаторы, VK, команда, человекочитаемое имя)
DEFAULT_BINDINGS = {
    1: (MOD_NOREPEAT, VK_F9, "toggle", "F9"),
    2: (MOD_NOREPEAT, VK_F8, "settings", "F8"),
    3: (MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, VK_UP, "scale_up", "Ctrl+Alt+Up"),
    4: (MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, VK_DOWN, "scale_down", "Ctrl+Alt+Down"),
    5: (MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, VK_Q, "quit", "Ctrl+Alt+Q"),
    6: (MOD_NOREPEAT, VK_INSERT, "record", "Insert"),
    # Скриншот жил только кнопкой в меню, и подписать её было нечем.
    # F7 — рядом с F8/F9 и свободна. PrtScr не берём: её перехватывает
    # «Набросок на фрагменте», RegisterHotKey на неё может не встать.
    7: (MOD_NOREPEAT, VK_F7, "screenshot_menu", "F7"),
}

# Имя клавиши -> VK (для парсинга config)
_KEY_NAMES = {
    "F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73, "F5": 0x74,
    "F6": 0x75, "F7": 0x76, "F8": 0x77, "F9": 0x78, "F10": 0x79,
    "F11": 0x7A, "F12": 0x7B,
    "INSERT": VK_INSERT, "DELETE": 0x2E, "HOME": 0x24, "END": 0x23,
    "PGUP": 0x21, "PGDN": 0x22,
    "UP": VK_UP, "DOWN": VK_DOWN, "LEFT": 0x25, "RIGHT": 0x27,
    "Q": VK_Q, "W": 0x57, "E": 0x45, "R": 0x52, "T": 0x54, "Y": 0x59,
    "U": 0x55, "I": 0x49, "O": 0x4F, "P": 0x50, "A": 0x41, "S": 0x53,
    "D": 0x44, "F": 0x46, "G": 0x47, "H": 0x48, "J": 0x4A, "K": 0x4B,
    "L": 0x4C, "Z": 0x5A, "X": 0x58, "C": 0x43, "V": 0x56, "B": 0x42,
    "N": 0x4E, "M": 0x4D,
    "0": 0x30, "1": 0x31, "2": 0x32, "3": 0x33, "4": 0x34,
    "5": 0x35, "6": 0x36, "7": 0x37, "8": 0x38, "9": 0x39,
}


def parse_binding(text: str) -> tuple[int, int] | None:
    """Разобрать строку вида 'F9', 'Ctrl+Alt+Q', 'Insert' -> (mods, vk).

    Возвращает None, если строка не распознана (тогда биндинг не меняется).
    """
    if not text:
        return None
    parts = [p.strip().upper() for p in text.split("+") if p.strip()]
    if not parts:
        return None
    mods = 0
    for p in parts[:-1]:
        if p == "CTRL" or p == "CONTROL":
            mods |= MOD_CONTROL
        elif p == "ALT":
            mods |= MOD_ALT
        elif p == "SHIFT":
            mods |= MOD_SHIFT
        else:
            return None
    vk = _KEY_NAMES.get(parts[-1])
    if vk is None:
        return None
    return mods | MOD_NOREPEAT, vk


def build_bindings(overrides: dict | None = None) -> dict:
    """Биндинги с учётом пользовательских переопределений из config.

    overrides: {"toggle": "F9", "record": "Insert", ...} — команда -> строка.
    Неизвестные/битые строки игнорируются (остаётся дефолт).
    """
    bindings = {hk_id: tuple(entry) for hk_id, entry in DEFAULT_BINDINGS.items()}
    if not overrides:
        return bindings
    for hk_id, (mods, vk, cmd, name) in list(bindings.items()):
        text = overrides.get(cmd)
        if not text:
            continue
        parsed = parse_binding(text)
        if parsed is None:
            continue
        new_mods, new_vk = parsed
        bindings[hk_id] = (new_mods, new_vk, cmd, text)
    return bindings


def describe(bindings: dict | None = None) -> str:
    """Строка вида 'F9 NR, F8 настройки, ...' — для лога при старте."""
    src = bindings or DEFAULT_BINDINGS
    return ", ".join(f"{name}={cmd}" for _, (_, _, cmd, name) in sorted(src.items()))


class HotkeyController:
    """Регистрация глобальных хоткеев; команды кладутся в очередь."""

    def __init__(self, commands: queue.Queue, bindings: dict | None = None):
        self._commands = commands
        self._bindings = bindings or DEFAULT_BINDINGS
        self._thread: threading.Thread | None = None
        self._tid = 0
        self.registered: list[str] = []
        self.failed: list[str] = []
        self._ready = threading.Event()

    def start(self, timeout: float = 3.0) -> None:
        """Запустить поток и дождаться результата регистрации."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="hotkeys")
        self._thread.start()
        self._ready.wait(timeout)

    def _run(self) -> None:
        self._tid = ctypes.windll.kernel32.GetCurrentThreadId()
        msg = wintypes.MSG()
        # Очередь сообщений у потока создаётся лениво — заставляем её появиться
        # ДО RegisterHotKey, иначе первые WM_HOTKEY могут уйти в никуда.
        user32.PeekMessageW(ctypes.byref(msg), None, WM_HOTKEY, WM_HOTKEY, PM_NOREMOVE)
        for hk_id, (mods, vk, _cmd, name) in self._bindings.items():
            if user32.RegisterHotKey(None, hk_id, mods, vk):
                self.registered.append(name)
            else:
                # Комбинацию уже занял кто-то другой — не смертельно,
                # остальные хоткеи продолжают работать.
                self.failed.append(name)
        self._ready.set()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                binding = self._bindings.get(msg.wParam)
                if binding is not None:
                    self._commands.put(binding[2])
        for hk_id in self._bindings:
            user32.UnregisterHotKey(None, hk_id)

    def stop(self) -> None:
        if self._tid:
            user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
            self._tid = 0
