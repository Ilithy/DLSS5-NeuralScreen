"""Проверка шторки «до/после»: левая часть кадра остаётся необработанной.

Воркер гоняется напрямую, без окна и без Desktop Duplication: цвет мы
присылаем сами, поэтому «сырой захват» — это ровно наш входной кадр, и его
можно сравнить с результатом попиксельно.

Ожидание: слева от шторки выход совпадает со входом бит в бит, справа —
отличается (там прошёл NGX), а на самой границе стоит полоса-разделитель
цвета акцента.
"""
import struct
import subprocess
import sys
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from main import (FRAME_FLAG_SPLIT, FRAME_FLAG_WANT_PIXELS, FRAME_FMT,  # noqa: E402
                  FRAME_MAGIC, HEADER_FMT, OUT_FMT, OUT_MAGIC, PROFILES,
                  VIDEO_MAGIC, WORKER_EXE)

W, H = 1280, 720          # full-res кадр
WORK_W, WORK_H = 1280, 720  # 1:1, без апскейла — сравнение проще
SPLIT = 0.5
WARMUP = 8


def make_frame(seed: int) -> np.ndarray:
    """Кадр с деталями: на плоском цвете NGX нечего менять, тест ослепнет."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:H, 0:W]
    frame = np.zeros((H, W, 4), dtype=np.uint8)
    base = ((xx * 7 + yy * 13) % 256).astype(np.uint8)
    noise = rng.integers(0, 48, size=(H, W), dtype=np.uint8)
    frame[..., 0] = base
    frame[..., 1] = (base // 2 + noise).astype(np.uint8)
    frame[..., 2] = (255 - base).astype(np.uint8)
    frame[..., 3] = 255
    return np.ascontiguousarray(frame)


def read_exact(pipe, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = pipe.read(n - len(buf))
        if not chunk:
            raise EOFError(f"воркер закрыл stdout (получено {len(buf)} из {n})")
        buf += chunk
    return buf


def send_and_get(worker, index: int, frame: np.ndarray, motion: np.ndarray,
                 split: float) -> np.ndarray | None:
    flags = FRAME_FLAG_WANT_PIXELS
    if split > 0.0:
        frac = min(0xFFFF, max(0, int(round(split * 0xFFFF))))
        flags |= FRAME_FLAG_SPLIT | (frac << 16)
    worker.stdin.write(struct.pack(FRAME_FMT, FRAME_MAGIC, index,
                                   1 if index == 0 else 0, flags, index))
    worker.stdin.write(frame.tobytes())
    worker.stdin.write(motion.tobytes())
    worker.stdin.flush()

    head = read_exact(worker.stdout, struct.calcsize(OUT_FMT))
    magic, _idx, ok, nbytes, ngx, _pts = struct.unpack(OUT_FMT, head)
    if magic != OUT_MAGIC:
        raise RuntimeError(f"чужой ответ 0x{magic:08X}")
    if not ok:
        raise RuntimeError(f"воркер вернул ok=0, ngx=0x{ngx:08X}")
    if nbytes == 0:
        return None
    return np.frombuffer(read_exact(worker.stdout, nbytes),
                         dtype=np.uint8).reshape(H, W, 4).copy()


def main() -> int:
    if not WORKER_EXE.is_file():
        print(f"ПРОВАЛ: воркер не найден: {WORKER_EXE}")
        return 1
    params = dict(PROFILES["Strong / Cinematic"])
    header = struct.pack(
        HEADER_FMT, VIDEO_MAGIC, WORK_W, WORK_H, WARMUP, 0,
        0, 0, int(params.get("style", 0)), int(params.get("auto_mask", 0)),
        int(params.get("ui_correction", 0)),
        float(params["intensity"]), float(params["local_tone"]),
        float(params["local_structure"]), float(params["skin_structure"]),
        0, 0)

    worker = subprocess.Popen([str(WORKER_EXE), "--live"],
                              cwd=str(WORKER_EXE.parent),
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE)
    failures = []
    try:
        worker.stdin.write(header)
        worker.stdin.flush()
        motion = np.zeros((WORK_H, WORK_W, 2), dtype=np.float16)

        # Пара кадров без шторки — конвейер должен ожить и что-то менять
        frame = make_frame(1)
        plain = None
        for i in range(2):
            plain = send_and_get(worker, i, frame, motion, 0.0)
        if plain is None:
            print("ПРОВАЛ: воркер не вернул пиксели")
            return 1
        changed = float((plain[..., :3] != frame[..., :3]).any(axis=2).mean())
        print(f"без шторки NGX изменил {changed:.1%} пикселей")
        if changed < 0.10:
            failures.append(f"NGX почти ничего не менял ({changed:.1%}) — "
                            f"тест не смог бы отличить половины")

        out = send_and_get(worker, 2, frame, motion, SPLIT)
        if out is None:
            print("ПРОВАЛ: со шторкой пиксели не вернулись")
            return 1
        split_x = int(W * SPLIT)
        # Разделитель шириной lw стоит по обе стороны от границы, поэтому
        # половины сравниваем в стороне от него (см. SplitCompose).
        lw = 3 if H >= 1400 else 2
        d0, d1 = split_x - lw // 2, split_x - lw // 2 + lw
        left_same = bool(np.array_equal(out[:, :d0, :3], frame[:, :d0, :3]))
        right_diff = float((out[:, d1:, :3] != frame[:, d1:, :3]).any(axis=2).mean())
        print(f"шторка на x={split_x}: слева совпадает со входом — "
              f"{'да' if left_same else 'НЕТ'}; справа изменено {right_diff:.1%}")
        if not left_same:
            bad = int((out[:, :d0, :3] != frame[:, :d0, :3]).any(axis=2).sum())
            failures.append(f"левая половина не равна входу ({bad} пикселей)")
        if right_diff < 0.10:
            failures.append(f"правая половина почти не отличается ({right_diff:.1%})")

        # Разделитель: все пиксели полосы должны быть ровно цвета акцента
        accent = np.array([0xD9, 0x77, 0x57], dtype=np.uint8)
        band = out[:, d0:d1, :3]
        on_accent = float((band == accent).all(axis=2).mean())
        print(f"разделитель x={d0}..{d1 - 1}: цвета акцента {on_accent:.1%} полосы")
        if on_accent < 0.99:
            uniq = np.unique(band.reshape(-1, 3), axis=0)[:4]
            failures.append(f"полоса-разделитель не того цвета "
                            f"({on_accent:.1%}), первые цвета: {uniq.tolist()}")

        # И без шторки полосы быть не должно
        plain_band = plain[:, d0:d1, :3]
        if float((plain_band == accent).all(axis=2).mean()) > 0.5:
            failures.append("разделитель нарисован и при выключенной шторке")
    finally:
        try:
            worker.stdin.close()
        except OSError:
            pass
        try:
            worker.wait(timeout=10)
        except subprocess.TimeoutExpired:
            worker.kill()
        err = worker.stderr.read().decode("utf-8", "replace")
        tail = [l for l in err.splitlines() if "pure" in l or "error" in l.lower()]
        if tail:
            print("лог воркера:", " | ".join(tail[-3:]))

    if failures:
        for f in failures:
            print("ПРОВАЛ:", f)
        return 1
    print("OK: шторка работает — слева вход бит в бит, справа обработанный кадр")
    return 0


if __name__ == "__main__":
    sys.exit(main())
