"""VideoRecorder — запись кадров NR-оверлея в MP4 (AV1 NVENC).

Пишет кадры, которые Python получает от воркера (output_rgba) во время
записи (Insert). Кадры приходят full-res RGBA8 каждые ~30 мс; PyAV
конвертирует их в yuv420p и кодирует AV1 через NVENC.

Запись не зависит от ShadowPlay/OBS: оверлей исключён из внешнего
захвата (WDA_EXCLUDEFROMCAPTURE), поэтому видео пишется изнутри —
ровно тот NR-результат, что виден на экране.
"""

from __future__ import annotations

import queue
import sys
import threading
import time
from fractions import Fraction

import av
import numpy as np


class VideoRecorder:
    """Пишет кадры в MP4 (av1_nvenc). Создаётся на старте записи, закрывается
    по Insert/выходу. write()/close() зовутся только из main-цикла.

    Кодирование идёт в своём потоке. Замер на 4K показал, что синхронный
    write() стоил 19.9 мс на кадр — перевод RGBA->yuv420p и отправка в nvenc
    на CPU — и ронял конвейер с 56 до 21 FPS. От битрейта это не зависело:
    время съедало цветовое преобразование, а не кодер.

    Кадр отдаётся потоку по ссылке, без копии: воркер присылает каждый кадр
    в свежем буфере (WorkerReader.recv -> np.frombuffer поверх нового bytes),
    и main-цикл его больше не меняет — только читает для показа и скриншота.
    """

    #: Сколько кадров ждёт кодировщика. Больше — больше памяти (на 4K это
    #: 33 МБ на кадр), меньше — раньше начнём терять кадры на всплесках.
    QUEUE_DEPTH = 4
    #: Сколько ждать место в очереди, прежде чем выбросить кадр. Ронять
    #: конвейер ради записи нельзя: пользователь смотрит на экран, а не в
    #: файл. Пропуск кадра на времени не отражается — pts от часов.
    PUT_TIMEOUT_S = 0.25

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
        self.dropped = 0
        self._queue: queue.Queue = queue.Queue(maxsize=self.QUEUE_DEPTH)
        self._thread: threading.Thread | None = None
        self._encode_error: BaseException | None = None
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
            print(f"[record] color metadata failed: {exc}", file=sys.stderr)
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
            print(f"[record] encoder params failed: {exc}", file=sys.stderr)
        # Опции кодировщика идут строками через options — атрибутов
        # max_bit_rate/rc_buffer_size у PyAV не существует.
        if self.ENCODER_OPTIONS:
            try:
                self._stream.options = dict(self.ENCODER_OPTIONS)
            except Exception as exc:
                print(f"[record] encoder options failed: {exc}",
                      file=sys.stderr)
        self._frame_idx = 0
        self.written = 0
        self._started = time.perf_counter()

    def _encode_loop(self) -> None:
        """Единственный владелец контейнера, пока запись идёт."""
        while True:
            item = self._queue.get()
            if item is None:
                return
            pts, rgba = item
            try:
                self._encode_one(pts, rgba)
            except BaseException as exc:   # noqa: BLE001 — донесём в main
                self._encode_error = exc
                print(f"[record] кодирование прервано: {exc}", file=sys.stderr)
                return

    def write(self, rgba: np.ndarray) -> None:
        """Поставить кадр в очередь кодировщика (RGBA8 full-res, 4 канала).

        PTS строим от РЕАЛЬНОГО времени записи, а не от счётчика кадров:
        кадры приходят с фактическим fps конвейера (~16-32), а не ровно 30,
        и контейнер обязан отражать реальную длительность — иначе видео
        проигрывается ускоренно. Считаем его ЗДЕСЬ, в момент прихода кадра:
        в потоке он отражал бы момент кодирования, то есть врал бы на всю
        длину очереди.
        """
        if rgba.shape[0] != self.height or rgba.shape[1] != self.width:
            # Режим дисплея сменился — кадры другой формы. Молча пропускать
            # нельзя: запись «тихо» пишет пустоту. Исключение останавливает
            # запись (main.py: recorder.close() + recorder = None).
            raise ValueError(
                f"display mode changed: frame {rgba.shape[1]}x{rgba.shape[0]} "
                f"!= recorder {self.width}x{self.height}")
        if self._encode_error is not None:
            exc, self._encode_error = self._encode_error, None
            raise RuntimeError(f"encoder thread failed: {exc}")
        if self._thread is None:
            self._thread = threading.Thread(target=self._encode_loop,
                                            name="nr-encode", daemon=True)
            self._thread.start()
        elapsed = time.perf_counter() - self._started
        pts_by_time = int(round(elapsed * self.fps))
        pts = max(self._frame_idx + 1, pts_by_time)
        self._frame_idx = pts
        try:
            self._queue.put((pts, rgba), timeout=self.PUT_TIMEOUT_S)
        except queue.Full:
            # Кодировщик не успевает. Выбросить кадр честнее, чем держать
            # main-цикл: на экране пользователь заметит, в файле — нет.
            self.dropped += 1
            self._frame_idx = pts - 1   # номер не занят, отдадим следующему

    def _encode_one(self, pts: int, rgba: np.ndarray) -> None:
        """Собственно кодирование — только из потока _encode_loop."""
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
            print(f"[record] frame color tags failed: {exc}", file=sys.stderr)
        frame.pts = pts
        for packet in self._stream.encode(frame):
            self._container.mux(packet)
        self.written += 1

    def close(self) -> None:
        """Дождаться кодировщика, дописать трейлер и закрыть контейнер.

        Поток останавливаем ДО работы с контейнером: он его единственный
        владелец, пока запись идёт, и трогать контейнер из двух потоков
        нельзя.
        """
        if self._container is None:
            return
        if self._thread is not None:
            self._queue.put(None)
            self._thread.join(timeout=30.0)
            if self._thread.is_alive():
                print("[record] кодировщик не завершился за 30 c",
                      file=sys.stderr)
            self._thread = None
        if self.dropped:
            print(f"[record] кадров выброшено: {self.dropped} "
                  f"(кодировщик не успевал)", file=sys.stderr)
        try:
            for packet in self._stream.encode(None):  # flush encoder
                self._container.mux(packet)
            self._container.close()
        except Exception as exc:
            print(f"[record] close failed: {exc}", file=sys.stderr)
        self._container = None

    @property
    def duration_ms(self) -> float:
        return (time.perf_counter() - self._started) * 1000.0
