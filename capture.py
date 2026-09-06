"""ScreenCapture — захват рабочего стола для прототипа DLSS 5 NR (desktop-nr).

Бэкенд: DXCamera (Windows Desktop Duplication API, DXGI).
Кадры отдаются как np.ndarray shape (H, W, 4) dtype uint8 в RGBA
(конвертацию из BGRA делает сам dxcam, в переиспользуемый буфер).

Пример:
    cap = ScreenCapture(monitor_idx=0)
    frame = cap.grab()          # (2160, 3840, 4) uint8 RGBA
    cap.close()
"""

from __future__ import annotations

import numpy as np


class ScreenCapture:
    """Захват монитора через DXCamera (Desktop Duplication API)."""

    def __init__(self, monitor_idx: int = 0):
        import dxcam

        self._dxcam = dxcam
        self.monitor_idx = monitor_idx
        # output_color="RGBA": конвертацию BGRA→RGBA делает dxcam в свой
        # переиспользуемый буфер. Раньше это делал cv2.cvtColor здесь же —
        # лишние 33 МБ аллокации на каждый 4K-кадр.
        self._camera = dxcam.create(
            output_idx=monitor_idx,
            output_color="RGBA",
        )
        if self._camera is None:
            raise RuntimeError(
                f"dxcam.create(output_idx={monitor_idx}) вернул None — "
                "монитор не найден или захват недоступен"
            )
        # Разрешение монитора (W, H) из описания вывода
        output = getattr(self._camera, "_output", None)
        res = getattr(output, "resolution", None)
        if res is not None:
            self.resolution = (int(res[0]), int(res[1]))
        else:
            # Фолбэк: первый кадр
            probe = self._camera.grab()
            if probe is None:
                raise RuntimeError("Не удалось получить первый кадр для определения разрешения")
            self.resolution = (probe.shape[1], probe.shape[0])

    def grab(self) -> np.ndarray:
        """Захватить текущий кадр монитора.

        Returns:
            np.ndarray shape (H, W, 4) dtype uint8, каналы RGBA,
            C-contiguous (frombuffer/tobytes без копии).
            Может вернуть None, если кадр ещё не готов (редко).

        Каждый grab() отдаёт отдельный массив: соседние кадры не делят
        память (проверено — _work/test_capture_rgba.py), так что кадр
        можно держать через итерацию цикла.
        """
        return self._camera.grab()

    def close(self) -> None:
        """Освободить ресурсы захвата."""
        if self._camera is not None:
            self._camera.release()
            self._camera = None

    def __enter__(self) -> "ScreenCapture":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
