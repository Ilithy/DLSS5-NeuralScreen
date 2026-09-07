"""ScreenCapture - desktop capture for the DLSS 5 NR prototype (desktop-nr).

Backend: DXCamera (Windows Desktop Duplication API, DXGI).
Frames come back as np.ndarray shape (H, W, 4) dtype uint8 in RGBA (dxcam
does the BGRA conversion itself, into a reusable buffer).

Example:
    cap = ScreenCapture(monitor_idx=0)
    frame = cap.grab()          # (2160, 3840, 4) uint8 RGBA
    cap.close()
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

import numpy as np


def list_monitors() -> list[tuple[int, int, int]]:
    """Monitors as [(idx, w, h), ...] via EnumDisplayMonitors.

    The index is assumed to match dxcam's output_idx (output order). DPI
    awareness must already be set in the calling process, otherwise the sizes
    come back in scaled pixels.
    """
    monitors: list[tuple[int, int, int, int]] = []

    def _cb(_hmon, _hdc, lprect, _lparam) -> bool:
        r = lprect.contents
        monitors.append((r.left, r.top, r.right - r.left, r.bottom - r.top))
        return True

    MONITORENUMPROC = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
        ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
    ctypes.windll.user32.EnumDisplayMonitors(0, 0, MONITORENUMPROC(_cb), 0)
    return [(i, w, h) for i, (_x, _y, w, h) in enumerate(monitors)]


class ScreenCapture:
    """Monitor capture through DXCamera (Desktop Duplication API)."""

    def __init__(self, monitor_idx: int = 0):
        import dxcam

        self._dxcam = dxcam
        self.monitor_idx = monitor_idx
        # output_color="RGBA": dxcam converts BGRA->RGBA into its own reusable
        # buffer. This used to be a cv2.cvtColor right here — an extra 33 MB
        # allocated for every 4K frame.
        self._camera = dxcam.create(
            output_idx=monitor_idx,
            output_color="RGBA",
        )
        if self._camera is None:
            raise RuntimeError(
                f"dxcam.create(output_idx={monitor_idx}) returned None — "
                "monitor not found or capture unavailable"
            )
        # Monitor resolution (W, H) from the output description
        output = getattr(self._camera, "_output", None)
        res = getattr(output, "resolution", None)
        if res is not None:
            self.resolution = (int(res[0]), int(res[1]))
        else:
            # Fallback: the first frame
            probe = self._camera.grab()
            if probe is None:
                raise RuntimeError(
                    "could not grab a first frame to determine the resolution")
            self.resolution = (probe.shape[1], probe.shape[0])

    def grab(self) -> np.ndarray:
        """Grab the monitor's current frame.

        Returns:
            np.ndarray shape (H, W, 4) dtype uint8, RGBA channels,
            C-contiguous (frombuffer/tobytes without a copy).
            May return None when the frame is not ready yet (rare).

        Every grab() hands back a separate array: neighbouring frames do not
        share memory (checked — _work/test_capture_rgba.py), so a frame can be
        held across a loop iteration.
        """
        return self._camera.grab()

    def close(self) -> None:
        """Release the capture resources."""
        if self._camera is not None:
            self._camera.release()
            self._camera = None

    def __enter__(self) -> "ScreenCapture":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
