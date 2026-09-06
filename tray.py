"""TrayController — иконка в системном трее для DLSS 5 Desktop NR.

Меню (правый клик): NR ON/OFF (с галочкой), Scale (состояние), Настройки,
Scale +0.05 / -0.05, Выход. Левый клик по иконке = default action =
открыть окно настроек (стандарт Windows: меню — правый клик, левый —
default). Команды кладутся в queue.Queue, main-цикл читает их.

Иконка генерируется через Pillow: тёмный квадрат #0D1117 с янтарным
квадратом-акцентом #FFBF00 (фирменные цвета проекта).
"""

from __future__ import annotations

import queue
import threading

import pystray
from PIL import Image, ImageDraw

ACCENT = (0xFF, 0xBF, 0x00)
BG = (0x0D, 0x11, 0x17)


def _make_icon(size: int = 64) -> Image.Image:
    img = Image.new("RGBA", (size, size), (*BG, 255))
    draw = ImageDraw.Draw(img)
    m = size // 8
    draw.rectangle((m, m, size - m, size - m), fill=(*ACCENT, 255))
    return img


class TrayController:
    """Трей-иконка: команды в очередь, состояние для отображения в меню."""

    def __init__(self, commands: queue.Queue):
        self._commands = commands
        self._state = {"nr": True, "scale": 0.5}
        self._icon = None
        self._thread = None

    def _set_state(self, **kw) -> None:
        self._state.update(kw)
        if self._icon is not None:
            try:
                self._icon.title = (f"NeuralScreen — NR {'ON' if self._state['nr'] else 'OFF'}"
                                    f" | scale {self._state['scale']:.2f}")
                self._icon.update_menu()
            except Exception:
                pass

    def _cmd(self, name: str) -> None:
        self._commands.put(name)

    def _toggle_nr(self, icon, item) -> None:
        self._set_state(nr=not self._state["nr"])
        self._cmd("toggle")

    def _open_settings(self, icon, item) -> None:
        self._cmd("settings")

    # Scale НЕ меняем оптимистично: границы и шаг знает только main
    # (WORK_SCALE_MIN/MAX), он же может отложить применение по кулдауну.
    # Фактическое значение прилетит обратно через _set_state(scale=...).
    def _scale_up(self, icon, item) -> None:
        self._cmd("scale_up")

    def _scale_down(self, icon, item) -> None:
        self._cmd("scale_down")

    def _quit(self, icon, item) -> None:
        self._cmd("quit")

    def _build_menu(self):
        return pystray.Menu(
            pystray.MenuItem("NR: ON", self._toggle_nr,
                             checked=lambda item: self._state["nr"]),
            pystray.MenuItem("NR: OFF", self._toggle_nr,
                             checked=lambda item: not self._state["nr"]),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(f"Scale: {self._state['scale']:.2f}", None, enabled=False),
            pystray.MenuItem("Scale +0.05", self._scale_up),
            pystray.MenuItem("Scale -0.05", self._scale_down),
            pystray.Menu.SEPARATOR,
            # default=True: левый клик по иконке = этот пункт (открыть настройки)
            pystray.MenuItem("Настройки", self._open_settings, default=True),
            pystray.MenuItem("Выход", self._quit),
        )

    def start(self) -> None:
        """Запустить трей в отдельном потоке (не блокирует main)."""
        self._icon = pystray.Icon("neuralscreen", _make_icon(),
                                  "NeuralScreen", self._build_menu())
        self._set_state()
        self._thread = threading.Thread(target=self._icon.run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass
