"""VideoRecorder — запись кадров NR-оверлея в MP4 (AV1 NVENC).

Пишет кадры, которые Python получает от воркера (output_rgba) во время
записи (Insert). Кадры приходят full-res RGBA8 каждые ~30 мс; PyAV
конвертирует их в yuv420p и кодирует AV1 через NVENC.

Запись не зависит от ShadowPlay/OBS: оверлей исключён из внешнего
захвата (WDA_EXCLUDEFROMCAPTURE), поэтому видео пишется изнутри —
ровно тот NR-результат, что виден на экране.
"""

from __future__ import annotations

import time
from fractions import Fraction

import av
import numpy as np


class VideoRecorder:
    """Пишет кадры в MP4 (av1_nvenc). Создаётся на старте записи, закрывается
    по Insert/выходу. НЕ потокобезопасен — вызывается только из main-цикла."""

    #: Битрейт и параметры кодировщика вынесены в атрибуты класса, чтобы их
    #: можно было менять без правки конструктора (замеры, эксперименты).
    BIT_RATE = 120_000_000
    ENCODER_OPTIONS = {
        "preset": "p6",     # p1 быстрый ... p7 качественный
        "tune": "hq",
        "rc": "vbr",        # не фиксированный битрейт: на резком движении
                            # кодер должен иметь право потратить больше
        "cq": "16",         # целевое качество; битрейт — потолок, а не цель
        "maxrate": "250M",
        "bufsize": "500M",
    }

    def __init__(self, path: str, width: int, height: int, fps: float = 60.0):
        self.path = path
        self.width = width
        self.height = height
        self.fps = fps
        self._container = av.open(path, mode="w")
        self._stream = self._container.add_stream("av1_nvenc", rate=int(round(fps)))
        self._stream.width = width
        self._stream.height = height
        self._stream.pix_fmt = "yuv420p"
        # MP4 (mov) muxer + nvenc: постоянная time_base 1/fps, pts — счётчик.
        # (Питфолл NUT с time_base != 1/30 не касается: пишем напрямую в MP4,
        # без субпроцесса ffmpeg.)
        self._stream.time_base = Fraction(1, int(round(fps)))
        # Цветовые метаданные ОБЯЗАТЕЛЬНЫ: без них плееры интерпретируют
        # кадры по-разному (контраст/цвета «плавают»). Захват рабочего
        # стола — sRGB FULL range (не limited/BT.709-tv: limited-теги при
        # full-range данных дают «сильный контраст» — плеер растягивает
        # 16-235 на весь 0-255).
        # Числовые enum FFmpeg: range JPEG/full=2; colorspace BT709=1;
        # primaries BT709=1 (sRGB primaries == BT.709); transfer
        # IEC61966_2_1 (sRGB)=13 (НЕ 14 — 14 это BT2020_10, проверено
        # ffprobe: при 14 файл помечается bt2020-10).
        # Имена атрибутов PyAV: color_range/colorspace/color_primaries/
        # color_trc (НЕ color_space/color_transfer — их не существует).
        try:
            self._stream.color_range = 2        # AVCOL_RANGE_JPEG = full
            self._stream.colorspace = 1         # AVCOL_SPC_BT709
            self._stream.color_primaries = 1    # AVCOL_PRI_BT709
            self._stream.color_trc = 13         # AVCOL_TRC_IEC61966_2_1 = sRGB
        except Exception as exc:
            print(f"[record] color metadata failed: {exc}", file=__import__("sys").stderr)
        # NVENC: битрейт 50 Мбит/с — запаса качества для интерфейса/текста
        # (пользовательский выбор). «Рассыпание» картинки на длинных
        # прогонах лечится НЕ только битрейтом, а коротким GOP и без
        # B-фреймов: на переменном fps конвейера B-фреймы рассинхронизируют
        # кадры, а длинный GOP без keyframe даёт артефакты на смене сцен.
        # Битрейт как ПОТОЛОК при VBR с целевым качеством (cq), а не как цель:
        # на резком движении (шутер) фиксированный битрейт заставляет кодер
        # ронять качество, чтобы попасть в цифру. GOP короткий и без
        # B-фреймов: на переменном fps конвейера B-фреймы рассинхронизируют
        # кадры, а длинный GOP даёт артефакты на смене сцен.
        try:
            self._stream.bit_rate = self.BIT_RATE
            self._stream.gop_size = max(30, int(round(fps)) * 2)  # keyframe раз в 2 с
            self._stream.max_b_frames = 0
        except Exception as exc:
            print(f"[record] encoder params failed: {exc}", file=__import__("sys").stderr)
        # Опции кодировщика идут строками через options — атрибутов
        # max_bit_rate/rc_buffer_size у PyAV не существует.
        if self.ENCODER_OPTIONS:
            try:
                self._stream.options = dict(self.ENCODER_OPTIONS)
            except Exception as exc:
                print(f"[record] encoder options failed: {exc}",
                      file=__import__("sys").stderr)
        self._frame_idx = 0
        self.written = 0
        self._started = time.perf_counter()

    def write(self, rgba: np.ndarray) -> None:
        """Закодировать один кадр (RGBA8 full-res, 4 канала).

        PTS строим от РЕАЛЬНОГО времени записи, а не от счётчика кадров:
        кадры приходят с фактическим fps конвейера (~16-32), а не ровно 30,
        и контейнер обязан отражать реальную длительность — иначе видео
        проигрывается ускоренно. Гарантируем строгую монотонность.
        """
        if rgba.shape[0] != self.height or rgba.shape[1] != self.width:
            # Режим дисплея сменился — кадры другой формы. Молча пропускать
            # нельзя: запись «тихо» пишет пустоту. Исключение останавливает
            # запись (main.py: recorder.close() + recorder = None).
            raise ValueError(
                f"display mode changed: frame {rgba.shape[1]}x{rgba.shape[0]} "
                f"!= recorder {self.width}x{self.height}")
        frame = av.VideoFrame.from_ndarray(rgba, format="rgba")
        # Цветовые теги ОБЯЗАТЕЛЬНО на кадре, а не только на потоке:
        # swscale при конвертации RGBA->yuv420p берёт матрицу из кадра,
        # а плеер интерпретирует по тегам потока. Рассинхрон (кадр без
        # тегов -> дефолт swscale, поток с тегами) и даёт «контраст».
        try:
            frame.color_range = 2        # AVCOL_RANGE_JPEG = full (sRGB)
            frame.colorspace = 1         # AVCOL_SPC_BT709
            frame.color_primaries = 1    # AVCOL_PRI_BT709
            frame.color_trc = 13         # AVCOL_TRC_IEC61966_2_1 = sRGB
        except Exception as exc:
            print(f"[record] frame color tags failed: {exc}", file=__import__("sys").stderr)
        elapsed = time.perf_counter() - self._started
        pts_by_time = int(round(elapsed * self.fps))
        frame.pts = max(self._frame_idx + 1, pts_by_time)
        self._frame_idx = frame.pts
        for packet in self._stream.encode(frame):
            self._container.mux(packet)
        self.written += 1

    def close(self) -> None:
        """Дописать трейлер и закрыть контейнер. Идемпотентно."""
        if self._container is None:
            return
        try:
            for packet in self._stream.encode(None):  # flush encoder
                self._container.mux(packet)
            self._container.close()
        except Exception as exc:
            print(f"[record] close failed: {exc}", file=__import__("sys").stderr)
        self._container = None

    @property
    def duration_ms(self) -> float:
        return (time.perf_counter() - self._started) * 1000.0
