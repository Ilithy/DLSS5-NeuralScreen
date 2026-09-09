"""Check what an external screen capture sees of the overlay.

The overlay window carries SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)
because the input is Desktop Duplication of the whole screen: without the flag
the pipeline would capture its own output. The flag is therefore load-bearing
in two opposite ways, and both are worth a test:

  * while it is set, no outside capture may see the overlay - otherwise the
    self-capture loop is back;
  * clearing it must make the overlay visible - that is what the single-window
    (WGC) mode is built on, and measured on a real recorder the flag turned out
    to be the whole story: with it the NVIDIA App refuses to record at all.

Method: raise the real overlay (the same display.Display the program uses),
paint a rectangle in a colour nothing else on a desktop has, and grab the
screen through dxcam - Desktop Duplication, the path OBS display capture uses.
Nothing is written to disk: the frame is reduced to numbers in memory, because
the desktop behind the marker is none of this test's business.

Run:  runtime\\python.exe test_capture_visibility.py
"""
import ctypes
import sys
import time
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent.parent  # the project root
sys.path.insert(0, str(BASE))  # the project modules (main.py, display.py, ...)
sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests/ (autocheck)

import display as D  # noqa: E402

WDA_NONE = 0x0

# Not in the brand palette, not the chroma key, and nothing on a real desktop
# looks like it - so a match cannot come from the wallpaper.
MARKER = (7, 231, 149)
MARKER_W, MARKER_H = 480, 240
# The layer is 235/255 opaque, so the captured marker is the marker blended
# with 8% of whatever is behind it. 40 per channel covers that and is still
# far away from any other colour.
TOL = 40
# The screen must be given a moment: the compositor and Desktop Duplication
# both run behind us, and grab() answers None until something changes.
FRAMES = 40
GRAB_AT = 20


def measure(capturable: bool, cam) -> dict:
    """Raise the overlay in one of the two states and look for the marker."""
    import pygame

    user32 = ctypes.windll.user32
    w, h = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    disp = D.Display(w, h, click_through=True)
    # The overlay is created HIDDEN on purpose (no blank flash during the
    # NGX warm-up) and shown only after the first real frame - reveal() is
    # that moment. The visibility test needs the window physically up in
    # BOTH states; only the WDA flag differs between them.
    disp.reveal()
    if capturable:
        if not user32.SetWindowDisplayAffinity(disp.get_hwnd(), WDA_NONE):
            disp.close()
            return {"error": "SetWindowDisplayAffinity(NONE) failed"}
    disp.set_hud_only(True, force=True)
    disp.menu.visible = False

    x0, y0 = (w - MARKER_W) // 2, (h - MARKER_H) // 2
    rect = pygame.Rect(x0, y0, MARKER_W, MARKER_H)
    frame = None
    for i in range(FRAMES):
        disp.draw_overlay(0.0)
        pygame.draw.rect(disp.screen, MARKER, rect)
        pygame.display.flip()
        if i == GRAB_AT:
            deadline = time.monotonic() + 3.0
            while frame is None and time.monotonic() < deadline:
                frame = cam.grab()
                if frame is None:
                    time.sleep(0.05)
        time.sleep(0.02)
    disp.close()

    if frame is None:
        return {"error": "Desktop Duplication returned no frame"}
    fh, fw = frame.shape[:2]
    if (fw, fh) != (w, h):
        return {"error": f"the capture is {fw}x{fh}, the screen is {w}x{h}"}
    patch = frame[y0:y0 + MARKER_H, x0:x0 + MARKER_W].astype(np.int16)
    dist = np.abs(patch - np.array(MARKER, dtype=np.int16)).max(axis=2)
    return {"share": float((dist <= TOL).mean()),
            "mean": tuple(int(v) for v in patch.reshape(-1, 3).mean(axis=0))}


def main() -> int:
    try:
        import dxcam
    except Exception as exc:
        print(f"SKIP: dxcam is unavailable ({exc!r})")
        return 0

    cam = dxcam.create(output_idx=0, output_color="RGB")
    failures = []
    results = {}
    try:
        for capturable in (False, True):
            name = "capturable" if capturable else "hidden"
            res = measure(capturable, cam)
            results[name] = res
            if "error" in res:
                failures.append(f"{name}: {res['error']}")
                print(f"{name}: ERROR {res['error']}")
                continue
            print(f"{name}: marker {res['share'] * 100:.1f}% of the rectangle "
                  f"(mean {res['mean']})")
            time.sleep(0.5)
    finally:
        del cam

    hidden = results.get("hidden", {}).get("share")
    shown = results.get("capturable", {}).get("share")
    if hidden is not None and hidden > 0.01:
        failures.append(f"the hidden overlay leaks into an outside capture "
                        f"({hidden * 100:.1f}% of the marker) - the self-capture "
                        f"loop is back")
    if shown is not None and shown < 0.95:
        failures.append(f"clearing WDA_EXCLUDEFROMCAPTURE did not make the overlay "
                        f"visible ({shown * 100:.1f}% of the marker)")

    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print("OK: hidden while the flag is set, fully visible without it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
