"""Поднять воркер и вывалить весь его stderr — видно ли прокси-nvapi.

Нужен потому, что test_split.py фильтрует лог воркера по «pure/error», и
строки [nvapi-shim] в его вывод не попадают.

Запуск:  runtime\\python.exe _probe_shim.py
"""
import os
import struct
import subprocess
import sys
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
from main import (FRAME_FMT, FRAME_MAGIC, HEADER_FMT, OUT_FMT,  # noqa: E402
                  PROFILES, VIDEO_MAGIC, WORKER_EXE)

W = H = 512


def main() -> int:
    params = dict(PROFILES["Strong / Cinematic"])
    header = struct.pack(HEADER_FMT, VIDEO_MAGIC, W, H, 4, 0, 0, 0,
                         int(params.get("style", 0)),
                         int(params.get("auto_mask", 0)),
                         int(params.get("ui_correction", 0)),
                         float(params["intensity"]), float(params["local_tone"]),
                         float(params["local_structure"]),
                         float(params["skin_structure"]), 0, 0)
    env = dict(os.environ, NS_PHASE="1")
    worker = subprocess.Popen([str(WORKER_EXE), "--live"],
                              cwd=str(WORKER_EXE.parent), env=env,
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE)
    frame = np.zeros((H, W, 4), dtype=np.uint8)
    frame[..., 3] = 255
    motion = np.zeros((H, W, 2), dtype=np.float16)
    try:
        worker.stdin.write(header)
        worker.stdin.write(struct.pack(FRAME_FMT, FRAME_MAGIC, 0, 1, 0, 0))
        worker.stdin.write(frame.tobytes())
        worker.stdin.write(motion.tobytes())
        worker.stdin.flush()
        worker.stdout.read(struct.calcsize(OUT_FMT))
    except Exception as exc:
        print(f"обмен не удался: {exc}")
    finally:
        try:
            worker.stdin.close()
        except OSError:
            pass
        try:
            worker.wait(timeout=15)
        except subprocess.TimeoutExpired:
            worker.kill()
        err = worker.stderr.read().decode("utf-8", "replace")

    shim = [l for l in err.splitlines() if "nvapi-shim" in l]
    print(f"строк от прокси: {len(shim)}")
    for line in shim[:25]:
        print("  ", line.strip())
    if not shim:
        print("прокси не сработал; последние строки воркера:")
        for line in err.splitlines()[-8:]:
            print("  ", line.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
