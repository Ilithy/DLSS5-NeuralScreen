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

VK_F8 = 0x77
VK_F9 = 0x78
VK_UP = 0x26
VK_DOWN = 0x28
VK_Q = 0x51

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
PM_NOREMOVE = 0x0000

# id -> (модификаторы, VK, команда, человекочитаемое имя)
BINDINGS = {
    1: (MOD_NOREPEAT, VK_F9, "toggle", "F9"),
    2: (MOD_NOREPEAT, VK_F8, "settings", "F8"),
    3: (MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, VK_UP, "scale_up", "Ctrl+Alt+Up"),
    4: (MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, VK_DOWN, "scale_down", "Ctrl+Alt+Down"),
    5: (MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, VK_Q, "quit", "Ctrl+Alt+Q"),
}


def describe() -> str:
    """Строка вида 'F9 NR, F8 настройки, ...' — для лога при старте."""
    return ", ".join(f"{name}={cmd}" for _, (_, _, cmd, name) in sorted(BINDINGS.items()))


class HotkeyController:
    """Регистрация глобальных хоткеев; команды кладутся в очередь."""

    def __init__(self, commands: queue.Queue):
        self._commands = commands
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
        for hk_id, (mods, vk, _cmd, name) in BINDINGS.items():
            if user32.RegisterHotKey(None, hk_id, mods, vk):
                self.registered.append(name)
            else:
                # Комбинацию уже занял кто-то другой — не смертельно,
                # остальные хоткеи продолжают работать.
                self.failed.append(name)
        self._ready.set()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                binding = BINDINGS.get(msg.wParam)
                if binding is not None:
                    self._commands.put(binding[2])
        for hk_id in BINDINGS:
            user32.UnregisterHotKey(None, hk_id)

    def stop(self) -> None:
        if self._tid:
            user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
            self._tid = 0
