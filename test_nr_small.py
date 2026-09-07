"""Neural Rendering at the work resolution: the picture survives, the menu drives it.

Two halves, because the feature has two halves that fail differently.

The worker half feeds a detailed frame through the reduced-resolution path and
checks the result is a real picture. That check exists for a specific reason:
the first working version produced a completely blank frame while reporting a
52% FPS gain, because both scaling passes shared two descriptors in one command
list and the GPU read them after both had been recorded. Nothing but looking at
the pixels catches that.

The menu half checks the controls actually emit the actions main listens for,
and that the slider stops where the work size hits its cap instead of running
into a dead top end.
"""
import os
import struct
import subprocess
import sys
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from main import (FRAME_FLAG_WANT_PIXELS, FRAME_FMT, FRAME_MAGIC,  # noqa: E402
                  HEADER_FMT, NATIVE_DIR, OUT_FMT, OUT_MAGIC, PROFILES,
                  VIDEO_MAGIC, WORKER_EXE)

FULL_W, FULL_H = 1920, 1080
WORK_W, WORK_H = 1280, 720
WARMUP = 8
FRAMES = 4


def make_frame(w: int, h: int) -> np.ndarray:
    """Detail everywhere: on flat colour a broken scaler looks just like a good one."""
    rng = np.random.default_rng(3)
    yy, xx = np.mgrid[0:h, 0:w]
    f = np.zeros((h, w, 4), dtype=np.uint8)
    base = ((xx * 7 + yy * 13) % 256).astype(np.uint8)
    f[..., 0] = base
    f[..., 1] = (base // 2 + rng.integers(0, 48, (h, w), dtype=np.uint8))
    f[..., 2] = (255 - base).astype(np.uint8)
    f[..., 3] = 255
    return np.ascontiguousarray(f)


def read_exact(pipe, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = pipe.read(n - len(buf))
        if not chunk:
            raise EOFError(f"worker closed stdout ({len(buf)} of {n})")
        buf += chunk
    return buf


def run_worker(small: bool) -> dict:
    params = dict(PROFILES["Strong / Cinematic"])
    header = struct.pack(
        HEADER_FMT, VIDEO_MAGIC, WORK_W, WORK_H, WARMUP, 0,
        0, 0, int(params["style"]), int(params["auto_mask"]),
        int(params["ui_correction"]),
        float(params["intensity"]), float(params["local_tone"]),
        float(params["local_structure"]), float(params["skin_structure"]),
        FULL_W, FULL_H)
    frame = make_frame(FULL_W, FULL_H)
    motion = np.zeros((WORK_H, WORK_W, 2), dtype=np.float16)
    body = frame.tobytes() + motion.tobytes()

    env = dict(os.environ)
    env["NS_NR_SMALL"] = "1" if small else "0"
    proc = subprocess.Popen([str(WORKER_EXE), "--live"], cwd=str(NATIVE_DIR),
                            env=env, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out = None
    try:
        proc.stdin.write(header)
        proc.stdin.flush()
        for i in range(FRAMES):
            proc.stdin.write(struct.pack(FRAME_FMT, FRAME_MAGIC, i,
                                         1 if i == 0 else 0,
                                         FRAME_FLAG_WANT_PIXELS, i))
            proc.stdin.write(body)
            proc.stdin.flush()
            head = read_exact(proc.stdout, struct.calcsize(OUT_FMT))
            magic, _idx, ok, nbytes, ngx, _pts = struct.unpack(OUT_FMT, head)
            if magic != OUT_MAGIC or not ok:
                raise RuntimeError(f"bad reply magic=0x{magic:08X} ok={ok} ngx=0x{ngx:08X}")
            if nbytes:
                data = read_exact(proc.stdout, nbytes)
                out = np.frombuffer(data, dtype=np.uint8).reshape(FULL_H, FULL_W, 4).copy()
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
        err = proc.stderr.read().decode("utf-8", "replace")
    nr_line = next((l for l in err.splitlines() if "[nr]" in l), "")
    return {"out": out, "nr": nr_line.strip(), "log": err, "input": frame}


def detail(img: np.ndarray) -> float:
    """Variance of a Laplacian - zero means a flat frame, which is the bug."""
    g = img[..., :3].astype(np.float32).mean(axis=2)
    lap = (-4 * g[1:-1, 1:-1] + g[:-2, 1:-1] + g[2:, 1:-1]
           + g[1:-1, :-2] + g[1:-1, 2:])
    return float(lap.var())


def check_worker(failures: list) -> None:
    base = run_worker(small=False)
    if base["out"] is None:
        failures.append("no pixels came back with the mode off")
        return
    small = run_worker(small=True)
    if small["out"] is None:
        failures.append("no pixels came back with the mode on")
        return

    d_in = detail(base["input"])
    d_base = detail(base["out"])
    d_small = detail(small["out"])
    print(f"detail: input {d_in:.0f}, full-res NR {d_base:.0f}, reduced NR {d_small:.0f}")
    print(f"worker: {small['nr'] or '(no [nr] line)'}")

    if small["out"].shape != (FULL_H, FULL_W, 4):
        failures.append(f"reduced mode returned {small['out'].shape}, "
                        f"expected the full size")
    if "[nr] network runs at" not in small["nr"]:
        failures.append("the worker did not report running the network smaller")
    # The blank-frame bug: a flat result, which no FPS number would reveal.
    # The margin is generous on purpose. The test pattern changes by 7 levels
    # per pixel - almost Nyquist - so a 1.5x downscale wipes out far more of it
    # than real desktop content loses (measured at 4K: 4074 -> 1057, a quarter,
    # against a fifteenth here). The bug being guarded against gives exactly
    # 0.0, so there is no need to sit close to the real value.
    if d_small < d_in * 0.02:
        failures.append(f"the reduced-resolution frame is flat ({d_small:.1f} "
                        f"against {d_in:.1f} in the input) - scaling is broken")
    # It must still resemble the input rather than being noise or a shifted copy.
    diff = float(np.abs(small["out"][..., :3].astype(np.int16)
                        - base["input"][..., :3].astype(np.int16)).mean())
    print(f"mean |reduced - input|: {diff:.1f} of 255")
    if diff > 40:
        failures.append(f"the reduced-resolution frame does not resemble the "
                        f"input (mean difference {diff:.1f})")


def check_menu(failures: list) -> None:
    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    import pygame
    import overlay_ui

    pygame.init()
    pygame.display.set_mode((64, 64))
    try:
        def font_loader(size):
            try:
                return pygame.font.SysFont("consolas", size)
            except Exception:
                return pygame.font.Font(None, size)

        menu = overlay_ui.OverlayMenu(1.0, font_loader)
        menu.set_state({"nr_small": False, "work_scale": 0.50,
                        "work_scale_max": 0.67, "work_size": "2560x1440",
                        "profiles": ["Faithful"], "profile": "Faithful",
                        "params": {"intensity": 1.0, "local_tone": 1.0,
                                   "local_structure": 1.0, "skin_structure": 1.0}})
        menu.visible = True
        menu.draw(pygame.Surface((1920, 1080)))

        items = {i.key: i for i in menu.items}
        if "nr_small" not in items:
            failures.append("no reduced-resolution toggle in the menu")
        if "work_scale" not in items:
            failures.append("no processing-resolution slider in the menu")
        if not failures:
            ws = items["work_scale"]
            print(f"slider range {ws.lo:.2f}..{ws.hi:.2f} at value {ws.value:.2f}")
            if abs(ws.hi - 0.67) > 0.01:
                failures.append(f"the slider runs to {ws.hi:.2f}, not to the "
                                f"0.67 cap - its top end would be dead")

            got = menu.handle_event(pygame.event.Event(
                pygame.MOUSEBUTTONDOWN,
                {"pos": items["nr_small"].rect.center, "button": 1}))
            if ("toggle", "nr_small") not in got:
                failures.append(f"the toggle emitted {got}, not ('toggle', 'nr_small')")

            # Drag the slider to its left end: the value must come back as a
            # work_scale action rather than being taken for an NR parameter.
            track = ws.extra.get("track")
            got = menu.handle_event(pygame.event.Event(
                pygame.MOUSEBUTTONDOWN, {"pos": (track.x + 1, track.centery),
                                         "button": 1}))
            kinds = [g[0] for g in got]
            print(f"slider emitted {got}")
            if "work_scale" not in kinds:
                failures.append(f"the slider emitted {kinds}, not a work_scale action")
            elif "param" in kinds:
                failures.append("the slider was taken for an NR parameter")
    finally:
        pygame.quit()


def main() -> int:
    if not WORKER_EXE.is_file():
        print(f"FAIL: worker not found: {WORKER_EXE}")
        return 1
    failures: list = []
    check_worker(failures)
    check_menu(failures)
    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print("OK: the reduced-resolution frame is a real picture and the menu drives it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
