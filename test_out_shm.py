"""Пиксели результата через общую память (OUTS) совпадают с пайповыми.

Кадр записи на 4K весит 33 МБ, и прогон его через пайп стоил ~7 мс. Канал
OUTS кладёт те же байты в секцию. Тест гоняет воркера напрямую и требует,
чтобы один и тот же входной кадр дал БАЙТ В БАЙТ одинаковый результат по
обоим путям — иначе «оптимизация» тихо портила бы запись.

Проверяется ещё и переключение обратно: после OUTS с нулевыми размерами
пиксели снова должны идти телом в пайп.
"""
import mmap
import os
import struct
import subprocess
import sys
import uuid
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
from main import (FRAME_FLAG_WANT_PIXELS, FRAME_FMT, FRAME_MAGIC,  # noqa: E402
                  HEADER_FMT, OUT_BYTES_IN_SHM, OUT_FMT, OUT_MAGIC,
                  OUTS_ACK_FMT, OUTS_ACK_MAGIC, OUTS_FMT, OUTS_MAGIC,
                  PROFILES, VIDEO_MAGIC, WORKER_EXE)

W, H = 1280, 720
WARMUP = 8


def make_frame(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:H, 0:W]
    frame = np.zeros((H, W, 4), dtype=np.uint8)
    base = ((xx * 5 + yy * 11) % 256).astype(np.uint8)
    frame[..., 0] = base
    frame[..., 1] = (base // 2 + rng.integers(0, 40, (H, W), dtype=np.uint8))
    frame[..., 2] = (255 - base).astype(np.uint8)
    frame[..., 3] = 255
    return np.ascontiguousarray(frame)


def read_exact(pipe, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = pipe.read(n - len(buf))
        if not chunk:
            raise EOFError(f"воркер закрыл stdout ({len(buf)} из {n})")
        buf += chunk
    return buf


def send_frame(worker, index: int, frame: np.ndarray, motion: np.ndarray,
               reset: int = 0):
    """reset=1 — сбросить временную историю модели.

    Без сброса один и тот же вход даёт РАЗНЫЙ выход: feature 18 накапливает
    историю между кадрами. Сравнивать два кадра можно только после сброса.
    """
    worker.stdin.write(struct.pack(FRAME_FMT, FRAME_MAGIC, index,
                                   1 if (index == 0 or reset) else 0,
                                   FRAME_FLAG_WANT_PIXELS, index))
    worker.stdin.write(frame.tobytes())
    worker.stdin.write(motion.tobytes())
    worker.stdin.flush()


def recv_result(worker, view):
    """Вернуть (пиксели, откуда) — 'shm' или 'pipe'."""
    head = read_exact(worker.stdout, struct.calcsize(OUT_FMT))
    magic, _idx, ok, nbytes, ngx, _pts = struct.unpack(OUT_FMT, head)
    assert magic == OUT_MAGIC, f"чужой ответ 0x{magic:08X}"
    assert ok, f"ok=0, ngx=0x{ngx:08X}"
    if nbytes == OUT_BYTES_IN_SHM:
        return np.array(view, copy=True), "shm"
    data = read_exact(worker.stdout, nbytes)
    return np.frombuffer(data, dtype=np.uint8).reshape(H, W, 4).copy(), "pipe"


def main() -> int:
    if not WORKER_EXE.is_file():
        print(f"ПРОВАЛ: воркер не найден: {WORKER_EXE}")
        return 1
    params = dict(PROFILES["Strong / Cinematic"])
    header = struct.pack(HEADER_FMT, VIDEO_MAGIC, W, H, WARMUP, 0, 0, 0,
                         int(params.get("style", 0)),
                         int(params.get("auto_mask", 0)),
                         int(params.get("ui_correction", 0)),
                         float(params["intensity"]), float(params["local_tone"]),
                         float(params["local_structure"]),
                         float(params["skin_structure"]), 0, 0)

    name = f"NeuralScreenTestOut_{os.getpid()}_{uuid.uuid4().hex[:6]}"
    mm = mmap.mmap(-1, W * H * 4, tagname=name)
    view = np.ndarray((H, W, 4), dtype=np.uint8, buffer=mm)

    worker = subprocess.Popen([str(WORKER_EXE), "--live"],
                              cwd=str(WORKER_EXE.parent),
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE)
    failures = []
    try:
        worker.stdin.write(header)
        worker.stdin.flush()
        motion = np.zeros((H, W, 2), dtype=np.float16)
        frame = make_frame(7)

        # 1. Пайповый путь — эталон
        pipe_px, src = None, None
        for i in range(2):
            send_frame(worker, i, frame, motion)
            pipe_px, src = recv_result(worker, view)
        # Эталон — со сбросом истории, иначе сравнивать не с чем
        send_frame(worker, 2, frame, motion, reset=1)
        pipe_px, src = recv_result(worker, view)
        print(f"без OUTS: пиксели пришли через {src}")
        if src != "pipe":
            failures.append(f"до согласования пиксели пошли через {src}")

        # 2. Согласовать OUTS
        worker.stdin.write(struct.pack(OUTS_FMT, OUTS_MAGIC, W, H, 0, 0,
                                       name.encode("ascii")))
        worker.stdin.flush()
        ack = read_exact(worker.stdout, struct.calcsize(OUTS_ACK_FMT))
        magic, ok, _r0, _r1, _pts = struct.unpack(OUTS_ACK_FMT, ack)
        print(f"OUTS: magic 0x{magic:08X}, ok={ok}")
        if magic != OUTS_ACK_MAGIC or not ok:
            print("ПРОВАЛ: воркер не принял OUTS")
            return 1

        # 3. Тот же кадр — теперь через секцию, и он обязан совпасть
        send_frame(worker, 3, frame, motion, reset=1)
        shm_px, src = recv_result(worker, view)
        print(f"с OUTS: пиксели пришли через {src}")
        if src != "shm":
            failures.append("после согласования пиксели всё ещё идут по пайпу")
        elif pipe_px is not None:
            same = bool(np.array_equal(shm_px, pipe_px))
            diff = int((shm_px != pipe_px).sum())
            print(f"совпадение с пайповым кадром: "
                  f"{'бит в бит' if same else f'РАСХОЖДЕНИЕ, {diff} байт'}")
            if not same:
                failures.append(f"кадр через секцию отличается ({diff} байт)")

        # 4. Выключение канала — снова пайп
        worker.stdin.write(struct.pack(OUTS_FMT, OUTS_MAGIC, 0, 0, 0, 0,
                                       name.encode("ascii")))
        worker.stdin.flush()
        ack = read_exact(worker.stdout, struct.calcsize(OUTS_ACK_FMT))
        _m, ok, _r0, _r1, _p = struct.unpack(OUTS_ACK_FMT, ack)
        send_frame(worker, 4, frame, motion, reset=1)
        back_px, src = recv_result(worker, view)
        print(f"после выключения: пиксели через {src}")
        if src != "pipe":
            failures.append("канал не выключился, пиксели всё ещё в секции")
        elif not np.array_equal(back_px, shm_px):
            failures.append("кадр после выключения не совпал с предыдущим")
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
        for line in err.splitlines():
            if "outs" in line.lower():
                print("лог воркера:", line.strip())
        view = None
        mm.close()

    if failures:
        for f in failures:
            print("ПРОВАЛ:", f)
        return 1
    print("OK: канал OUTS отдаёт те же байты и корректно выключается")
    return 0


if __name__ == "__main__":
    sys.exit(main())
