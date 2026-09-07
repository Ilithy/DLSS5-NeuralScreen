"""Кодирование в потоке: write() не блокирует, файл получается целым.

Проверяет три вещи, из-за которых правка и делалась:
  * write() возвращается быстро — иначе смысла в потоке нет;
  * кадры не теряются на нормальном темпе и попадают в файл;
  * close() дожидается кодировщика, и файл читается обратно.

Плюс контроль честности: кадры отдаются потоку БЕЗ копии, поэтому проверяем,
что записанная картинка соответствует отданной, а не последней в очереди.
"""
import sys
import tempfile
import time
from pathlib import Path

import av
import numpy as np

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
from recorder import VideoRecorder  # noqa: E402

W, H = 1920, 1080
FRAMES = 40
FPS = 60.0


def make_frame(i: int) -> np.ndarray:
    """Каждый кадр своего оттенка — чтобы отличить их в файле."""
    frame = np.zeros((H, W, 4), dtype=np.uint8)
    frame[..., 0] = (i * 6) % 256      # R растёт с номером
    frame[..., 1] = 40
    frame[..., 2] = 90
    frame[..., 3] = 255
    return np.ascontiguousarray(frame)


def main() -> int:
    failures = []
    out = Path(tempfile.gettempdir()) / "ns-test-recorder.mp4"
    out.unlink(missing_ok=True)

    rec = VideoRecorder(str(out), W, H, fps=FPS)
    worst = 0.0
    try:
        for i in range(FRAMES):
            frame = make_frame(i)
            t0 = time.perf_counter()
            rec.write(frame)
            worst = max(worst, (time.perf_counter() - t0) * 1000.0)
            time.sleep(1.0 / FPS)      # темп конвейера
    finally:
        rec.close()

    print(f"худший write(): {worst:.1f} мс, выброшено {rec.dropped}, "
          f"записано {rec.written}")
    # Синхронный путь стоил ~20 мс на 4K; на 1080p — около 5. Порог берём с
    # запасом: если правка работает, write() это только put в очередь.
    if worst > 3.0:
        failures.append(f"write() блокировал {worst:.1f} мс — поток не помог")
    if rec.dropped > FRAMES * 0.1:
        failures.append(f"выброшено {rec.dropped} из {FRAMES} — очередь мала")
    if rec.written + rec.dropped != FRAMES:
        failures.append(f"кадров учтено {rec.written}+{rec.dropped}, "
                        f"а отдано {FRAMES}")

    if not out.is_file() or out.stat().st_size == 0:
        print("ПРОВАЛ: файл не создан")
        return 1
    with av.open(str(out)) as container:
        stream = container.streams.video[0]
        decoded = [f for f in container.decode(stream)]
    print(f"файл {out.stat().st_size / 1024:.0f} КБ, декодировано "
          f"{len(decoded)} кадров, {stream.codec_context.name}")
    if len(decoded) < rec.written:
        failures.append(f"в файле {len(decoded)} кадров, а записано "
                        f"{rec.written}")
    if decoded:
        # Первый кадр должен быть первым отданным, а не каким-то из очереди
        rgb = decoded[0].to_ndarray(format="rgb24")
        r = int(rgb[..., 0].mean())
        print(f"первый кадр: R={r} (ожидалось около 0)")
        if r > 40:
            failures.append(f"первый кадр не первый отданный (R={r})")

    out.unlink(missing_ok=True)
    if failures:
        for f in failures:
            print("ПРОВАЛ:", f)
        return 1
    print("OK: write() не блокирует, кадры на месте, файл читается")
    return 0


if __name__ == "__main__":
    sys.exit(main())
