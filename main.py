"""DLSS 5 Desktop NR — интеграционный каркас прототипа.

Петля: захват рабочего стола (capture.ScreenCapture) → motion guides
(guides.TemporalGuideGenerator) → NGX-воркер (native/nvngx.dll,
режим --live) → вывод на весь экран (display.Display).

Управление (глобальные хоткеи, RegisterHotKey — нажатие не доходит до
активного приложения, см. hotkeys.py):
    F9            — NR вкл/выкл
    F8            — окно настроек
    Ctrl+Alt+Up/Down — масштаб обработки
    Ctrl+Alt+Q    — выход (он же пункт «Выход» в трее)

Запуск:
    python main.py [--config config.json]
"""

from __future__ import annotations

import argparse
import ctypes
import json
import mmap
import os
import queue
import struct
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

# --- Лог в файл вместо консоли ------------------------------------------
# Релиз запускается через pythonw.exe (без консольного окна): stdout/stderr
# там равны None, и любой print упал бы. Перенаправляем их в NeuralScreen.log
# рядом с main.py — все print продолжают работать, пользователь видит лог
# файлом, а не окном. Ошибки старта (нет DLL и т.п.) дополнительно
# показываются messagebox-ом (см. _fatal_dialog внизу).
LOG_PATH = Path(__file__).resolve().parent / "NeuralScreen.log"


def _init_logging() -> None:
    """Перенаправить stdout/stderr в NeuralScreen.log (utf-8)."""
    try:
        log_file = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
        sys.stdout = log_file
        sys.stderr = log_file
    except Exception:
        pass  # не получилось — print'ы просто пропадут, не упадём

# DPI-awareness ДО любых импортов (cv2, capture, display, tray): если какой-то
# модуль выставит awareness раньше (например, dxcam вызывает
# SetProcessDpiAwareness(2) при создании Output), повторный вызов вернёт
# ERROR_ACCESS_DENIED и окно pygame будет масштабировано (125% → 3072x1728).
# PER_MONITOR_AWARE_V2 = -4. Ошибки игнорируем: display.py дублирует вызов.
try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

# Embed-python (python313._pth) не добавляет cwd в sys.path — добавляем папку
# скрипта вручную, чтобы работали локальные модули (capture, display, guides).
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cv2
import numpy as np
import pygame  # HUD-наложение на записываемый кадр (image.frombuffer)

from capture import ScreenCapture, list_monitors
from display import Display
from guides import TemporalGuideGenerator
from hotkeys import HotkeyController, build_bindings, describe as describe_hotkeys
from recorder import VideoRecorder
from gpuinfo import describe as gpu_describe, probe as gpu_probe
from i18n import STRINGS as UI_STRINGS

# Страница проекта: README, хоткеи, требования. Открывается кнопкой в меню.
REPO_URL = "https://github.com/perseval-BLR/DLSS5-NeuralScreen"
from tray import TrayController

# --- Протокол воркера (совпадает с dlss5_converter/core.py) --------------
# v3 (magic D5V3): заголовок с full_w/full_h — воркер сам ресайзит кадры
# на GPU (NGX Upscaling), Python не ресайзит на CPU.
VIDEO_MAGIC = 0x33563544  # 'DV5' v3
FRAME_MAGIC = 0x314D5246  # 'FMR1'
OUT_MAGIC = 0x3154554F    # 'OUT1'

HEADER_FMT = "<10I4f2I"   # magic, w, h, warmup, frame_count, profile, preset,
                          # style, auto_mask, ui_correction, intensity,
                          # local_tone, local_structure, skin_structure,
                          # full_w, full_h
FRAME_FMT = "<4Iq"        # magic, index, reset, reserved, pts
OUT_FMT = "<5Iq"          # magic, index, ok, bytes, ngx_result, pts

# SHMI: кадр передаётся через общую память, в пайп уходит только заголовок
# FRM1 с флагом FRAME_FLAG_SHM. Воркер грузит пиксели в текстуру прямо из
# маппинга — исчезают две копии 33 МБ (запись в пайп и чтение из него).
SHM_MAGIC = 0x494D4853      # 'SHMI'
SHM_ACK_MAGIC = 0x4B434153  # 'SACK'
SHM_FMT = "<4Iq64s"         # magic, color_bytes, motion_bytes, flags, pts, name (88 байт)
SHM_ACK_FMT = "<4Iq"        # magic, ok, reserved0, reserved1, pts (24 байта)
FRAME_FLAG_SHM = 0x1         # бит в поле reserved заголовка кадра
FRAME_FLAG_WANT_PIXELS = 0x2  # вернуть пиксели даже в режиме окна (для скриншота)
FRAME_FLAG_MOTION_SMALL = 0x4  # поле движения в разрешении потока, растянет воркер
FRAME_FLAG_SPLIT = 0x20        # шторка до/после; позиция — в старших 16 битах reserved

# MOTS: поле движения приходит в разрешении оптического потока (~320x180), а
# растягивает его до work-разрешения воркер на GPU. С CPU снимается resize и
# конвертация 6 миллионов значений — по замерам ~8 мс на кадр.
MOTION_MAGIC = 0x53544F4D      # 'MOTS'
MOTION_ACK_MAGIC = 0x4B43414D  # 'MACK'
MOTION_FMT = "<4Iq"            # magic, width, height, flags, pts (24 байта)
MOTION_ACK_FMT = "<4Iq"


# WNDO: воркер сам показывает результат в своём окне поверх экрана. Пока окно
# поднято, OUT1 приходит с bytes=0 — пиксели не возвращаются в Python вообще,
# исчезают readback на стороне воркера, обратный пайп и blit в pygame.
WINDOW_MAGIC = 0x4F444E57      # 'WNDO'
WINDOW_ACK_MAGIC = 0x4B434157  # 'WACK'
WINDOW_FMT = "<4Iq"            # magic, width, height, flags, pts (24 байта)
WINDOW_ACK_FMT = "<4Iq"        # magic, ok, reserved0, reserved1, pts
WINDOW_FLAG_CAPTURABLE = 0x1   # отладка: НЕ прятать окно от захвата экрана
WINDOW_FLAG_DISABLE = 0x2      # закрыть окно, вернуться к передаче пикселей

# RNSZ: смена work-разрешения на лету (без рестарта процесса воркера).
# Воркер пересоздаёт NGX feature по новым размерам и отвечает RACK.
RESIZE_MAGIC = 0x5A534E52  # 'RNSZ'
RESIZE_ACK_MAGIC = 0x4B434152  # 'RACK'
RESIZE_FMT = "<10I4f2I"   # та же раскладка, что HEADER_FMT (magic вместо VIDEO_MAGIC)
RACK_FMT = "<4Iq"         # magic, ok, ngx_result, reserved, pts (24 байта)

# DDA1: воркер сам захватывает экран (Desktop Duplication) — цвет идёт
# напрямую в GPU-текстуру, Python больше не передаёт 33 МБ кадра. Кадры
# FRM1 уходят с флагом FRAME_FLAG_NO_COLOR: только motion, без цвета.
DDA_MAGIC = 0x31414444  # 'DDA1'
DDA_ACK_MAGIC = 0x4B434144  # 'DACK'
DDA_FMT = "<4Iq"        # magic, width, height, flags, pts (24 байта)
DDA_ACK_FMT = "<4Iq"    # magic, ok, reserved0, reserved1, pts
FRAME_FLAG_NO_COLOR = 0x8  # в DDA-режиме: цвет не шлём (воркер берёт сам)
FRAME_FLAG_BYPASS = 0x10  # NR OFF: пропустить NGX, показать сырой захват

# GRAY: воркер пишет luminance (даунсэмпл экрана, ~320x180) в обратный
# маппинг Python — для оптического потока guides. В DDA-режиме это
# заменяет dxcam-захват: gray приходит прямо с GPU.
GRAY_MAGIC = 0x59415247  # 'GRAY'
GRAY_ACK_MAGIC = 0x4B434147  # 'GAK'
GRAY_FMT = "<4Iq64s"    # magic, width, height, flags, pts, name (88 байт)
GRAY_ACK_FMT = "<4Iq"   # magic, ok, reserved0, reserved1, pts

# --- Профили DLSS 5 NR (порядок полей как в конвертере) -------------------
PROFILES = {
    "Faithful": dict(profile=0, preset=0, style=0, auto_mask=0, ui_correction=0,
                     intensity=0.70, local_tone=0.75, local_structure=0.75, skin_structure=-1.0),
    "Natural": dict(profile=1, preset=0, style=1, auto_mask=0, ui_correction=0,
                    intensity=1.00, local_tone=1.00, local_structure=1.00, skin_structure=-1.0),
    "Strong / Cinematic": dict(profile=2, preset=2, style=2, auto_mask=1, ui_correction=0,
                               intensity=1.65, local_tone=1.40, local_structure=1.50, skin_structure=1.0),
    "Extreme / Overdrive": dict(profile=2, preset=2, style=2, auto_mask=1, ui_correction=0,
                                intensity=2.50, local_tone=2.00, local_structure=2.00, skin_structure=1.5),
}

BASE_DIR = Path(__file__).resolve().parent
NATIVE_DIR = BASE_DIR / "native"
# ВАЖНО: NGX Core возвращает FAIL_PlatformError на Init_Ext для ЛЮБОГО имени
# процесса, кроме nvngx.dll (проверено экспериментально; merserk-0.1 собирает
# воркер так же — /Fe:bin\runtime\nvngx.dll). Имя файла — часть контракта NGX.
WORKER_EXE = NATIVE_DIR / "nvngx.dll"

FPS_LOG_INTERVAL = 2.0  # сек, лог FPS в консоль
PERF_LOG_INTERVAL = 5.0  # сек, лог средних таймингов этапов конвейера
PERF_KEYS = ("grab", "resize_full", "guides", "send", "recv", "show")

# Глобальные хоткеи живут в hotkeys.py (RegisterHotKey). Раскладка и
# причины выбора комбинаций — там же, в docstring модуля.
WORK_SCALE_STEP = 0.05
WORK_SCALE_MIN = 0.1
WORK_SCALE_MAX = 1.0
# NGX feature 18 молчит на 3840x2160 (проверено изолированно: воркер
# зависает на кадре 0 при work=4K, и в legacy, и в upscale-режиме).
# Ограничиваем work-разрешение 2560x1440 — гарантированно работает.
WORK_MAX_W = 2560
WORK_MAX_H = 1440

DEFAULT_LANG = "en"


def _work_size(width: int, height: int, scale: float) -> tuple[int, int]:
    """Work-разрешение NGX: scale от full, но не больше WORK_MAX_W/H
    (NGX молчит на 4K — ограничение проверено изолированно)."""
    w = max(64, int(round(width * scale / 2) * 2))
    h = max(64, int(round(height * scale / 2) * 2))
    if w > WORK_MAX_W or h > WORK_MAX_H:
        k = min(WORK_MAX_W / w, WORK_MAX_H / h)
        w = max(64, int(round(w * k / 2) * 2))
        h = max(64, int(round(h * k / 2) * 2))
    return w, h


class SharedFrameBuffer:
    """Общая память под входной кадр воркера (команда SHMI).

    Раскладка фиксирована и НЕ зависит от work_scale:
        [0 .. color_capacity)                — RGBA8 full-res
        [color_capacity .. +motion_capacity) — motion float16 work-res
    Смещение motion постоянно, поэтому смена разрешения (RNSZ) не требует
    перевыговаривания SHMI — меняется только используемая длина.

    ИНВАРИАНТ: слот один. Нельзя класть кадр N+1, пока воркер не вернул
    результат кадра N, иначе перезапишем пиксели у него под руками. Цикл
    main строго парный (send -> recv), так что инвариант соблюдается.
    Появится конвейеризация — понадобится второй слот.
    """

    def __init__(self, full_w: int, full_h: int,
                 max_work_w: int = WORK_MAX_W, max_work_h: int = WORK_MAX_H):
        self.color_capacity = full_w * full_h * 4
        self.motion_capacity = max_work_w * max_work_h * 4
        self.size = self.color_capacity + self.motion_capacity
        # Имя секции: ASCII, уникальное на процесс — воркер открывает его
        # через OpenFileMappingA в том же сеансе Windows.
        self.name = f"NeuralScreen_{os.getpid()}_{uuid.uuid4().hex[:8]}"
        self._mm = mmap.mmap(-1, self.size, tagname=self.name)
        self._buf = np.ndarray((self.size,), dtype=np.uint8, buffer=self._mm)
        self.negotiated = False  # выставляет start_worker после SACK

        # --- Обратный канал: gray (luminance) для guides в DDA-режиме ---
        # Воркер пишет сюда даунсэмпл экрана (320x180 = размер потока),
        # Python читает его вместо dxcam-захвата для DISOpticalFlow.
        self.gray_w, self.gray_h = 0, 0
        self.gray_bytes = 0
        self.gray_name = f"NeuralScreenGray_{os.getpid()}_{uuid.uuid4().hex[:6]}"
        self._gray_mm: mmap.mmap | None = None
        self._gray_buf: np.ndarray | None = None  # (gray_bytes,) uint8

    def open_gray(self, w: int, h: int) -> None:
        """Открыть gray-секцию размером w*h (создать, если не была).

        При смене размера имя секции МЕНЯЕТСЯ: воркер держит старый handle,
        и CreateFileMapping с тем же именем вернул бы старую секцию — mmap
        большего размера упал бы, и канал тихо умер (H2 аудита).
        send_gray() передаёт воркеру свежее имя после open_gray().
        """
        if self._gray_mm is not None and self.gray_w == w and self.gray_h == h:
            return
        self.close_gray()
        self.gray_w, self.gray_h = w, h
        self.gray_bytes = w * h
        self.gray_name = f"NeuralScreenGray_{os.getpid()}_{uuid.uuid4().hex[:6]}"
        self._gray_mm = mmap.mmap(-1, self.gray_bytes, tagname=self.gray_name)
        self._gray_buf = np.ndarray((self.gray_bytes,), dtype=np.uint8, buffer=self._gray_mm)

    def read_gray(self) -> np.ndarray | None:
        """Вернуть копию gray-кадра (320x180 uint8) или None, если не открыт.

        Воркер пишет memcpy без разделяемого барьера — теоретически возможен
        tear. На 320×180 это микросекунды; один разъехавшийся кадр
        оптического потока некритичен (guides переживают, следующий кадр
        чинится). Принятый риск — seqlock был бы overengineering'ом.
        """
        if self._gray_buf is None:
            return None
        return self._gray_buf.copy()

    def close_gray(self) -> None:
        if self._gray_buf is not None:
            self._gray_buf = None
        if self._gray_mm is not None:
            try:
                self._gray_mm.close()
            except Exception:
                pass
            self._gray_mm = None

    def put(self, rgba: np.ndarray, motion: np.ndarray) -> None:
        """Положить кадр и motion в маппинг (по одному memcpy на каждый)."""
        color = rgba.reshape(-1)
        if color.nbytes > self.color_capacity:
            raise ValueError(f"кадр {color.nbytes} Б не влезает в "
                             f"{self.color_capacity} Б общей памяти")
        mv = motion.reshape(-1).view(np.uint8)
        if mv.nbytes > self.motion_capacity:
            raise ValueError(f"motion {mv.nbytes} Б не влезает в "
                             f"{self.motion_capacity} Б общей памяти")
        np.copyto(self._buf[:color.nbytes], color)
        off = self.color_capacity
        np.copyto(self._buf[off:off + mv.nbytes], mv)

    def close(self) -> None:
        self.negotiated = False
        self.close_gray()
        self._buf = None  # numpy держит буфер: без сброса mmap.close() бросит BufferError
        try:
            self._mm.close()
        except Exception as exc:
            print(f"[main] Не удалось закрыть общую память: {exc}", file=sys.stderr)


def load_config(path: Path) -> dict:
    """Загрузить и провалидировать config.json."""
    with open(path, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    required = {"monitor", "width", "height", "fullscreen", "warmup", "profile",
                "intensity", "local_tone", "local_structure", "skin_structure"}
    missing = required - set(cfg)
    if missing:
        raise ValueError(f"config.json: отсутствуют поля: {sorted(missing)}")
    if cfg["profile"] not in PROFILES:
        raise ValueError(f"config.json: неизвестный профиль {cfg['profile']!r}; "
                         f"доступны: {sorted(PROFILES)}")
    for key in ("width", "height", "warmup"):
        if not isinstance(cfg[key], int) or cfg[key] <= 0:
            raise ValueError(f"config.json: поле {key} должно быть положительным целым")
    # work_scale: 0.25..1.0 — разрешение NGX-обработки относительно вывода
    scale = float(cfg.get("work_scale", 1.0))
    cfg["work_scale"] = min(WORK_SCALE_MAX, max(WORK_SCALE_MIN, scale))
    # lang: язык HUD/алертов/окна настроек (en/ru, по умолчанию ru)
    lang = str(cfg.get("lang", DEFAULT_LANG))
    if lang not in UI_STRINGS:
        lang = DEFAULT_LANG
    cfg["lang"] = lang
    return cfg


def resolve_params(cfg: dict) -> dict:
    """Профиль + кастомные NR-параметры из config (null = использовать профиль)."""
    params = dict(PROFILES[cfg["profile"]])
    for key in ("intensity", "local_tone", "local_structure", "skin_structure"):
        value = cfg.get(key)
        if value is not None:
            params[key] = float(value)
    return params


def _read_exact(stream, size: int) -> bytes:
    """Прочитать ровно size байт из потока (воркер может отдать меньше)."""
    chunks = bytearray()
    while len(chunks) < size:
        block = stream.read(size - len(chunks))
        if not block:
            raise EOFError(f"Воркер остановился после {len(chunks)} из {size} байт ответа")
        chunks.extend(block)
    return bytes(chunks)


def _drain_stderr(worker, logs: list[str], stop: threading.Event) -> None:
    """Фоновый сбор stderr воркера (иначе буфер переполнится и воркер зависнет).

    Один поток на воркера; завершается по EOF (процесс умер) или по
    stop-событию (shutdown_worker). Старый поток после рестарта читает
    из ЗАКРЫТОГО stderr старого воркера: readline() возвращает b""
    (EOF) и поток выходит — не висит и не читает stderr нового воркера.
    """
    try:
        for raw in iter(worker.stderr.readline, b""):
            if stop.is_set():
                break
            line = raw.decode("utf-8", "replace").rstrip()
            logs.append(line)
            # Список растёт неограниченно (NS_PHASE=1 добавляет строку на
            # кадр) — все потребители читают только хвост, держим 2000.
            if len(logs) > 2000:
                del logs[: len(logs) - 2000]
            # Лог воркера — в общий лог, но только когда включён профилировщик
            # (NS_PHASE=1): иначе он оседает в буфере и виден лишь когда
            # что-то упало. Кроме замеров фаз пропускаем и [pure]/[host]:
            # там код результата NGX и выбранный пресет модели, без них не
            # понять, что вообще создалось.
            if os.environ.get("NS_PHASE") == "1" and (
                    "[phase]" in line or "[pure]" in line or "[host]" in line):
                print(line)
    except Exception:
        pass


def start_worker(params: dict, width: int, height: int, warmup: int,
                 full_w: int = 0, full_h: int = 0,
                 shm: "SharedFrameBuffer | None" = None) -> tuple[subprocess.Popen, list[str]]:
    """Запустить NGX-воркер в режиме --live и отправить заголовок.

    width/height — work-разрешение (NGX feature), full_w/full_h — размер
    входных кадров от Python (воркер сам ресайзит на GPU через NGX
    Upscaling; full_w=0 → старый режим 1:1).

    Возвращает (worker, logs, reader, stop): reader — постоянный
    поток-читатель stdout (см. WorkerReader), stop — событие для
    завершения _drain_stderr при shutdown.
    """
    if not WORKER_EXE.is_file():
        raise FileNotFoundError(
            f"Воркер не найден: {WORKER_EXE}\n"
            "Скопируйте nvngx.dll (собранный воркер) и nvngx_dlssnr.dll в папку native/."
        )
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    worker = subprocess.Popen(
        [str(WORKER_EXE), "--live"],
        cwd=str(NATIVE_DIR),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=creation_flags,
    )
    logs: list[str] = []
    stop = threading.Event()
    threading.Thread(target=_drain_stderr, args=(worker, logs, stop), daemon=True).start()
    # Воркер в upscale-режиме (full_w>0) возвращает full-res кадры —
    # reader должен ждать full-размеры, иначе byte_count не сойдётся.
    out_w = full_w if full_w else width
    out_h = full_h if full_h else height
    reader = WorkerReader(worker, out_w, out_h)

    header = struct.pack(
        HEADER_FMT,
        VIDEO_MAGIC, width, height, int(warmup), 0,  # frame_count=0 → бесконечный цикл
        params["profile"], params["preset"], params["style"],
        params["auto_mask"], params["ui_correction"],
        params["intensity"], params["local_tone"],
        params["local_structure"], params["skin_structure"],
        int(full_w), int(full_h),
    )
    worker.stdin.write(header)
    worker.stdin.flush()
    if shm is not None:
        _negotiate_shm(worker, reader, shm)
    return worker, logs, reader, stop


def _negotiate_shm(worker: subprocess.Popen, reader: "WorkerReader",
                   shm: SharedFrameBuffer, timeout: float = 10.0) -> None:
    """Передать воркеру имя общей памяти (SHMI) и дождаться SACK.

    Отказ не смертелен: если воркер не смог открыть маппинг, остаёмся на
    передаче кадра через пайп — этот путь никуда не делся и работает.
    """
    shm.negotiated = False
    try:
        worker.stdin.write(struct.pack(
            SHM_FMT, SHM_MAGIC, shm.color_capacity, shm.motion_capacity, 0, 0,
            shm.name.encode("ascii")))
        worker.stdin.flush()
        reader.wait_sack(timeout)
        shm.negotiated = True
        print(f"[main] Общая память согласована: {shm.size / 1e6:.1f} МБ, "
              f"кадр не идёт через пайп")
    except Exception as exc:
        print(f"[main] Общая память недоступна ({exc}) — кадры через пайп",
              file=sys.stderr)


def send_frame(worker: subprocess.Popen, index: int, rgba: np.ndarray,
               motion: np.ndarray, reset: bool, pts: int,
               shm: "SharedFrameBuffer | None" = None,
               want_pixels: bool = False, motion_small: bool = False,
               no_color: bool = False, bypass: bool = False,
               split: float = 0.0) -> None:
    """Отправить кадр воркеру.

    С согласованной общей памятью в пайп уходит только 24-байтовый заголовок
    с флагом FRAME_FLAG_SHM, пиксели кладутся в маппинг. Иначе — старый путь:
    заголовок + RGBA8 + motion float16 телом в пайп.

    no_color (DDA-режим): цвет берёт воркер сам из Desktop Duplication —
    в пайп уходит только motion, rgba игнорируется.
    bypass (NR OFF): воркер пропускает NGX и показывает сырой захват —
    оверлей (окно, HUD) остаётся живым, эффект выключен.
    split (0..1): доля кадра слева, которую воркер оставит необработанной —
    шторка «до/после». 0 — выключено.
    """
    flags = (FRAME_FLAG_WANT_PIXELS if want_pixels else 0) | \
            (FRAME_FLAG_MOTION_SMALL if motion_small else 0) | \
            (FRAME_FLAG_NO_COLOR if no_color else 0) | \
            (FRAME_FLAG_BYPASS if bypass else 0)
    if split > 0.0:
        # Позиция шторки едет в старших 16 битах того же поля флагов:
        # отдельного поля в заголовке нет, а расширять его ради одного
        # числа значит менять протокол на обеих сторонах.
        frac = min(0xFFFF, max(0, int(round(min(1.0, split) * 0xFFFF))))
        flags |= FRAME_FLAG_SPLIT | (frac << 16)
    if no_color:
        # DDA-режим: только motion, без цвета (SHM не используется для цвета)
        worker.stdin.write(struct.pack(FRAME_FMT, FRAME_MAGIC, index, int(reset), flags, pts))
        worker.stdin.write(motion.tobytes())
        worker.stdin.flush()
        return
    if shm is not None and shm.negotiated:
        shm.put(rgba, motion)
        worker.stdin.write(struct.pack(FRAME_FMT, FRAME_MAGIC, index, int(reset),
                                       FRAME_FLAG_SHM | flags, pts))
        worker.stdin.flush()
        return
    worker.stdin.write(struct.pack(FRAME_FMT, FRAME_MAGIC, index, int(reset), flags, pts))
    worker.stdin.write(rgba.tobytes())
    worker.stdin.write(motion.tobytes())
    worker.stdin.flush()


def send_resize(worker: subprocess.Popen, params: dict, width: int, height: int,
                warmup: int, full_w: int = 0, full_h: int = 0) -> None:
    """Отправить RNSZ — смена work-разрешения/параметров на лету.

    Воркер пересоздаёт NGX feature по новым размерам (ReleaseFeature →
    CreateFeature в том же процессе) и отвечает RACK. Рестарт процесса
    не нужен — именно рестарт был источником зависаний/вылетов (exit 127).
    """
    worker.stdin.write(struct.pack(
        RESIZE_FMT,
        RESIZE_MAGIC, width, height, int(warmup), 0,
        params["profile"], params["preset"], params["style"],
        params["auto_mask"], params["ui_correction"],
        params["intensity"], params["local_tone"],
        params["local_structure"], params["skin_structure"],
        int(full_w), int(full_h),
    ))
    worker.stdin.flush()


def send_motion_size(worker: subprocess.Popen, width: int, height: int,
                     flags: int = 0, pts: int = 0) -> None:
    """MOTS: в каком разрешении будет приходить поле движения.

    0x0 — выключить: поле снова пойдёт в work-разрешении.
    """
    worker.stdin.write(struct.pack(MOTION_FMT, MOTION_MAGIC, int(width), int(height),
                                   int(flags), int(pts)))
    worker.stdin.flush()


def send_window(worker: subprocess.Popen, width: int, height: int,
                flags: int = 0, pts: int = 0) -> None:
    """WNDO: попросить воркер поднять своё окно вывода (или закрыть его).

    width=height=0 или флаг WINDOW_FLAG_DISABLE — закрыть окно и вернуться
    к передаче пикселей через пайп.
    """
    worker.stdin.write(struct.pack(WINDOW_FMT, WINDOW_MAGIC, int(width), int(height),
                                   int(flags), int(pts)))
    worker.stdin.flush()


def send_dda(worker: subprocess.Popen, width: int, height: int,
             flags: int = 0, pts: int = 0) -> None:
    """DDA1: попросить воркера захватывать экран самому (Desktop Duplication).

    width=height=0 — выключить захват и вернуться к передаче кадра из Python.
    Пока активен, кадры FRM1 несут FRAME_FLAG_NO_COLOR (только motion).
    """
    worker.stdin.write(struct.pack(DDA_FMT, DDA_MAGIC, int(width), int(height),
                                   int(flags), int(pts)))
    worker.stdin.flush()


def send_gray(worker: subprocess.Popen, width: int, height: int,
              name: str, flags: int = 0, pts: int = 0) -> None:
    """GRAY: передать воркеру имя обратного маппинга для luminance-кадра.

    В DDA-режиме воркер пишет сюда даунсэмпл экрана (ширина x высота,
    обычно 320x180 = размер поля потока), Python читает его для guides.
    width=height=0 — выключить обратный канал.
    """
    if len(name) >= 64:
        raise ValueError("имя gray-секции длиннее 63 символов")
    worker.stdin.write(struct.pack(GRAY_FMT, GRAY_MAGIC, int(width), int(height),
                                   int(flags), int(pts), name.encode("ascii")))
    worker.stdin.flush()


class WorkerReader:
    """Постоянный поток-читатель stdout воркера (один на воркера).

    Создаётся в start_worker, живёт пока жив воркер, умирает по EOF:
    shutdown_worker завершает процесс → pipe закрывается → read()
    возвращает b"" → _read_exact бросает EOFError → sentinel в очередь.

    Замена старого recv_frame (поток на КАЖДЫЙ кадр): при таймауте
    поток-читатель НЕ висит на read() — он продолжает читать следующие
    кадры, а main просто не получил ответ вовремя. При рестарте старый
    reader умирает по EOF старого stdout и физически не может прочитать
    данные нового воркера (разные pipes) — гонки чтения нет.
    """

    def __init__(self, worker: subprocess.Popen, width: int, height: int):
        self._worker = worker
        self._width = width
        self._height = height
        self._queue: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="worker-reader")
        self._thread.start()

    def _run(self) -> None:
        try:
            while True:
                magic_raw = _read_exact(self._worker.stdout, 4)
                magic = struct.unpack("<I", magic_raw)[0]
                if magic == MOTION_ACK_MAGIC:
                    # MACK: подтверждение MOTS
                    rest = _read_exact(self._worker.stdout, struct.calcsize(MOTION_ACK_FMT) - 4)
                    _magic, ok, _r0, _r1, _pts = struct.unpack(MOTION_ACK_FMT, magic_raw + rest)
                    self._queue.put(("mack", ok))
                elif magic == WINDOW_ACK_MAGIC:
                    # WACK: подтверждение WNDO — окно вывода поднято/закрыто
                    rest = _read_exact(self._worker.stdout, struct.calcsize(WINDOW_ACK_FMT) - 4)
                    _magic, ok, _r0, _r1, _pts = struct.unpack(WINDOW_ACK_FMT, magic_raw + rest)
                    self._queue.put(("wack", ok))
                elif magic == SHM_ACK_MAGIC:
                    # SACK: подтверждение SHMI — воркер открыл маппинг
                    rest = _read_exact(self._worker.stdout, struct.calcsize(SHM_ACK_FMT) - 4)
                    _magic, ok, _r0, _r1, _pts = struct.unpack(SHM_ACK_FMT, magic_raw + rest)
                    self._queue.put(("sack", ok))
                elif magic == RESIZE_ACK_MAGIC:
                    # RACK (24 байта): подтверждение RNSZ — кладём в очередь,
                    # main забирает через wait_rack()
                    rest = _read_exact(self._worker.stdout, struct.calcsize(RACK_FMT) - 4)
                    _magic, ok, ngx_result, _reserved, _pts = struct.unpack(RACK_FMT, magic_raw + rest)
                    self._queue.put(("rack", (ok, ngx_result)))
                elif magic == DDA_ACK_MAGIC:
                    # DACK (24 байта): подтверждение DDA1 — захват перешёл к воркеру
                    rest = _read_exact(self._worker.stdout, struct.calcsize(DDA_ACK_FMT) - 4)
                    _magic, ok, _r0, _r1, _pts = struct.unpack(DDA_ACK_FMT, magic_raw + rest)
                    self._queue.put(("dack", ok))
                elif magic == GRAY_ACK_MAGIC:
                    # GAK: подтверждение GRAY — обратный канал luminance открыт
                    rest = _read_exact(self._worker.stdout, struct.calcsize(GRAY_ACK_FMT) - 4)
                    _magic, ok, _r0, _r1, _pts = struct.unpack(GRAY_ACK_FMT, magic_raw + rest)
                    self._queue.put(("gak", ok))
                elif magic == OUT_MAGIC:
                    rest = _read_exact(self._worker.stdout, struct.calcsize(OUT_FMT) - 4)
                    _magic, out_index, ok, byte_count, ngx_result, _pts = struct.unpack(OUT_FMT, magic_raw + rest)
                    if not ok:
                        raise RuntimeError(f"Воркер ответил ошибкой на кадр {out_index}: ok={ok}")
                    if ngx_result != 1:
                        raise RuntimeError(
                            f"NGX evaluation failed на кадре {out_index}: 0x{ngx_result:08X}")
                    if byte_count == 0:
                        # Режим WNDO: воркер показал кадр сам в своём окне,
                        # пиксели через пайп не идут
                        self._queue.put((out_index, None))
                        continue
                    if byte_count != self._width * self._height * 4:
                        raise RuntimeError(
                            f"Воркер вернул {byte_count} байт вместо {self._width * self._height * 4}")
                    data = _read_exact(self._worker.stdout, byte_count)
                    frame = np.frombuffer(data, dtype=np.uint8).reshape(self._height, self._width, 4)
                    self._queue.put((out_index, frame))
                else:
                    raise RuntimeError(f"Неверная магия ответа воркера: 0x{magic:08X}")
        except Exception as exc:
            # EOF (воркер завершён/убит) или ошибка протокола — sentinel
            self._queue.put((None, exc))

    def set_output_size(self, width: int, height: int) -> None:
        """Сменить ожидаемый размер выходных кадров (сразу после RNSZ)."""
        self._width = width
        self._height = height

    def wait_mack(self, timeout: float) -> None:
        """Дождаться MACK — подтверждение размера поля движения (MOTS)."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"воркер не подтвердил MOTS за {timeout:.0f}с")
            try:
                got, payload = self._queue.get(timeout=remaining)
            except queue.Empty:
                continue
            if got is None:
                raise payload if isinstance(payload, Exception) else EOFError("воркер остановился")
            if got == "mack":
                if not payload:
                    raise RuntimeError("воркер не смог включить растяжение motion на GPU")
                return

    def wait_wack(self, timeout: float) -> None:
        """Дождаться WACK — подтверждение команды WNDO."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"воркер не подтвердил WNDO за {timeout:.0f}с")
            try:
                got, payload = self._queue.get(timeout=remaining)
            except queue.Empty:
                continue
            if got is None:
                raise payload if isinstance(payload, Exception) else EOFError("воркер остановился")
            if got == "wack":
                if not payload:
                    raise RuntimeError("воркер не смог поднять окно вывода")
                return

    def wait_dack(self, timeout: float) -> None:
        """Дождаться DACK — подтверждение команды DDA1 (захват у воркера)."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"воркер не подтвердил DDA1 за {timeout:.0f}с")
            try:
                got, payload = self._queue.get(timeout=remaining)
            except queue.Empty:
                continue
            if got is None:
                raise payload if isinstance(payload, Exception) else EOFError("воркер остановился")
            if got == "dack":
                if not payload:
                    raise RuntimeError("воркер не смог включить захват экрана")
                return

    def wait_gak(self, timeout: float) -> None:
        """Дождаться GAK — подтверждение открытия обратного gray-канала."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"воркер не подтвердил GRAY за {timeout:.0f}с")
            try:
                got, payload = self._queue.get(timeout=remaining)
            except queue.Empty:
                continue
            if got is None:
                raise payload if isinstance(payload, Exception) else EOFError("воркер остановился")
            if got == "gak":
                if not payload:
                    raise RuntimeError("воркер не смог открыть gray-канал")
                return

    def wait_sack(self, timeout: float) -> None:
        """Дождаться SACK — подтверждение общей памяти (SHMI)."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"воркер не подтвердил SHMI за {timeout:.0f}с")
            try:
                got, payload = self._queue.get(timeout=remaining)
            except queue.Empty:
                continue
            if got is None:
                raise payload if isinstance(payload, Exception) else EOFError("воркер остановился")
            if got == "sack":
                if not payload:
                    raise RuntimeError("воркер не смог открыть общую память")
                return

    def wait_rack(self, timeout: float) -> None:
        """Дождаться RACK — подтверждение смены разрешения (RNSZ).

        Кадры, пришедшие до RACK (после таймаута recv), пропускаются.
        """
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"Воркер не подтвердил смену разрешения за {timeout:.0f}с")
            try:
                got, payload = self._queue.get(timeout=remaining)
            except queue.Empty:
                continue
            if got == "rack":
                ok, ngx_result = payload
                if not ok:
                    raise RuntimeError(f"RNSZ отклонён воркером: ngx=0x{ngx_result:08X}")
                return
            # (index, frame) — кадр до RACK — пропустить

    def recv(self, index: int, timeout: float):
        """Дождаться кадр index; timeout > 0 — защита от зависания NGX.

        Возвращает np.ndarray с пикселями, либо None — если воркер показал
        кадр сам в своём окне (режим WNDO) и пикселей не присылал.

        Ответы с чужим index (кадры, которые main уже не ждёт после
        таймаута) отбрасываются — десинхронизация протокола невозможна.
        """
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"Воркер молчит {timeout:.0f}с на кадре {index} — NGX не ответил после рестарта")
            try:
                got_index, payload = self._queue.get(timeout=remaining)
            except queue.Empty:
                continue  # цикл сам бросит TimeoutError по истечении deadline
            if got_index is None:
                if isinstance(payload, Exception):
                    raise payload
                raise EOFError("Воркер остановился")
            if got_index == index:
                return payload
            # Ответ на кадр, который main уже не ждёт (после таймаута) — пропустить


def check_worker(worker: subprocess.Popen, logs: list[str]) -> None:
    """Если воркер упал — вывести последние строки stderr и поднять исключение."""
    code = worker.poll()
    if code is not None:
        tail = "\n".join(logs[-40:]) or "(stderr пуст)"
        raise RuntimeError(
            f"NGX-воркер завершился с кодом {code}.\n"
            f"Последние строки stderr:\n{tail}"
        )


def shutdown_worker(worker: subprocess.Popen, stop: threading.Event | None = None) -> None:
    """Graceful shutdown: закрыть stdin (EOF → воркер выходит с кодом 0), ждать 10 c.

    stop — событие завершения _drain_stderr (из start_worker): ставится
    сразу, чтобы drain-поток не висел на readline() закрытого stderr
    (на Windows закрытие pipe из другого потока не будит readline —
    поток выходит только по EOF после смерти процесса или по stop).
    """
    if worker.poll() is not None:
        if stop is not None:
            stop.set()
        return
    if stop is not None:
        stop.set()
    try:
        if worker.stdin and not worker.stdin.closed:
            worker.stdin.close()
    except OSError:
        pass
    try:
        code = worker.wait(timeout=10)
        print(f"[main] Воркер завершился корректно (код {code})")
    except subprocess.TimeoutExpired:
        print("[main] Воркер не вышел за 10 c — принудительное завершение")
        worker.terminate()
        try:
            worker.wait(timeout=5)
        except subprocess.TimeoutExpired:
            worker.kill()


def restart_worker(worker: subprocess.Popen, params: dict, width: int, height: int,
                   warmup: int, full_w: int = 0, full_h: int = 0,
                   stop: threading.Event | None = None,
                   shm: "SharedFrameBuffer | None" = None) -> tuple[subprocess.Popen, list[str], WorkerReader, threading.Event]:
    """Перезапустить воркер с новым разрешением (смена work_scale).

    Воркер создаёт NGX feature по размерам из заголовка и читает ровно
    w*h*4 байт на кадр — менять разрешение на лету нельзя, только рестарт.
    Warmup при рестарте берём меньше (30), чтобы не фризить экран.

    Пауза 2 c между shutdown и start: старый воркер держит GPU-ресурсы
    NGX (nvngx_dlssnr.dll, 165 МБ + D3D12 device) — конкурентная
    инициализация нового процесса на том же GPU зависает/роняет процесс
    (наблюдалось: exit 127 и зависание recv после apply_settings).

    Старый reader/drain умирают по EOF закрытых pipes старого воркера
    (shutdown_worker завершает процесс) — гонки чтения с новым воркером
    нет: pipes разные, старый поток физически не может прочитать stdout
    нового процесса.
    """
    shutdown_worker(worker, stop)
    time.sleep(2.0)
    return start_worker(params, width, height, warmup, full_w, full_h, shm)


def _autostart_enabled() -> bool:
    """Автозапуск сейчас включён? (HKCU Run, значение NeuralScreen)."""
    import winreg
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Run",
                             0, winreg.KEY_READ)
        try:
            winreg.QueryValueEx(key, "NeuralScreen")
            return True
        except FileNotFoundError:
            return False
        finally:
            winreg.CloseKey(key)
    except Exception:
        return False


def _set_autostart(enabled: bool) -> bool:
    """Включить/выключить автозапуск с Windows (HKCU Run).

    Запускаем NeuralScreen.vbs через wscript — скрытый лаунчер без консоли.
    Возвращает True при успехе.
    """
    import winreg
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Run",
                             0, winreg.KEY_SET_VALUE)
        if enabled:
            vbs = str(BASE_DIR / "NeuralScreen.vbs")
            winreg.SetValueEx(key, "NeuralScreen", 0, winreg.REG_SZ,
                              f'wscript.exe "{vbs}"')
        else:
            try:
                winreg.DeleteValue(key, "NeuralScreen")
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
        return True
    except Exception as exc:
        print(f"[main] Автозапуск не настроен: {exc}", file=sys.stderr)
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="DLSS 5 Desktop NR prototype")
    parser.add_argument("--config", type=Path, default=BASE_DIR / "config.json",
                        help="путь к config.json (по умолчанию рядом с main.py)")
    args = parser.parse_args()
    _init_logging()  # pythonw: stdout/stderr -> NeuralScreen.log

    cfg = load_config(args.config)
    params = resolve_params(cfg)
    width, height = int(cfg["width"]), int(cfg["height"])
    monitor = int(cfg["monitor"])
    warmup = int(cfg["warmup"])
    work_scale = float(cfg["work_scale"])
    lang = str(cfg["lang"])

    # Разрешение вывода — С РЕАЛЬНОГО МОНИТОРА, а не из застывшего
    # config.json (монитор могли переключить на 1440p, а конфиг помнит 4K —
    # оверлей/запись/окно воркера начнут расходиться с экраном).
    capture = ScreenCapture(monitor_idx=monitor)
    mon_w, mon_h = capture.resolution
    if mon_w > 0 and mon_h > 0 and (mon_w, mon_h) != (width, height):
        print(f"[main] Монитор {monitor} — {mon_w}x{mon_h} (конфиг: {width}x{height}), "
              f"беру реальное разрешение")
        width, height = mon_w, mon_h

    print(f"[main] NeuralScreen — профиль {cfg['profile']!r}, "
          f"разрешение {width}x{height}, монитор {monitor}")
    print(f"[main] Параметры NGX: {params}")
    print(f"[main] work_scale {work_scale:.2f} (NGX-разрешение "
          f"{int(width * work_scale)}x{int(height * work_scale)})")

    worker: subprocess.Popen | None = None
    reader: WorkerReader | None = None
    worker_stop: threading.Event | None = None
    shm: SharedFrameBuffer | None = None
    display: Display | None = None
    tray: TrayController | None = None
    hotkeys: HotkeyController | None = None
    recorder: VideoRecorder | None = None
    try:
        # Воркер и guides работают на work-разрешении (NGX feature создаётся
        # по размерам заголовка; guides.assert требует совпадения размеров)
        work_w, work_h = _work_size(width, height, work_scale)
        # v3-протокол (full_w/full_h) ТОЛЬКО при work != full: при work==full
        # (scale 1.0) воркер в upscale-режиме падает/зависает (проверено
        # изолированно) — используем legacy full_w=0, как в D5V2.
        full_w = width if (work_w != width or work_h != height) else 0
        full_h = height if (work_w != width or work_h != height) else 0
        # Общая память под входной кадр: размер не зависит от work_scale
        # (см. SharedFrameBuffer), поэтому создаётся один раз на процесс.
        shm = SharedFrameBuffer(width, height)
        worker, worker_logs, reader, worker_stop = start_worker(
            params, work_w, work_h, warmup, full_w, full_h, shm)
        print(f"[main] Воркер запущен (pid {worker.pid}), заголовок отправлен "
              f"({work_w}x{work_h})")

        print(f"[main] Захват монитора {monitor}: {capture.resolution}")

        display = Display(width, height, fullscreen=bool(cfg["fullscreen"]))
        display.set_lang(lang)
        # Программа рисует поверх рабочего стола и сама по себе никак себя не
        # проявляет — без этого после запуска непонятно, работает она или нет.
        startup_menu = bool(cfg.get("open_menu_on_start", True))
        # Шторка «до/после»: доля кадра слева, которую воркер оставляет сырой.
        split_pos = min(1.0, max(0.0, float(cfg.get("split", 0.0))))
        # Какая карта и работает ли на ней NR. Модель спрашиваем у nvapi, а
        # факт поддержки берём не из архитектуры, а из ответа воркера: он
        # единственный знает, создалась ли feature 18.
        gpu_info = gpu_probe()
        gpu_text = gpu_describe(gpu_info)
        gpu_ok: bool | None = None
        print(f"[main] GPU: {gpu_text} (группа 0x{gpu_info['arch_group']:X}, "
              f"официальная поддержка {'да' if gpu_info['official'] else 'нет'})")
        startup_pending = True
        # Размер, положение и тема меню — как их оставил пользователь.
        display.menu.set_user_scale(float(cfg.get("menu_scale", 1.0)))
        saved_theme = cfg.get("theme")
        if isinstance(saved_theme, str) and saved_theme in ("light", "dark"):
            display.menu.set_state({"theme": saved_theme})
        saved_offset = cfg.get("menu_offset")
        if isinstance(saved_offset, (list, tuple)) and len(saved_offset) == 2:
            display.menu.offset = [int(saved_offset[0]), int(saved_offset[1])]
        print(f"[main] Окно вывода {display.width}x{display.height}")

        # Трей-иконка: команды в очередь, main-цикл их читает
        tray_commands: queue.Queue = queue.Queue()
        tray = TrayController(tray_commands)
        tray._set_state(nr=True, scale=work_scale)
        tray.start()
        print("[main] Трей-иконка запущена")

        # Глобальные хоткеи: RegisterHotKey, а не опрос состояния клавиш.
        # Система отдаёт нажатие только нам и не передаёт его активному
        # приложению — F9 в игре переключает NR, и игра клавиши не видит.
        # Команды идут в ту же очередь, что и у трея. Пользовательские
        # биндинги — из config.json ("hotkeys": {"toggle": "F9", ...}).
        hotkey_overrides = cfg.get("hotkeys")
        if not isinstance(hotkey_overrides, dict):
            hotkey_overrides = {}
        hotkey_bindings = build_bindings(hotkey_overrides)
        hotkeys = HotkeyController(tray_commands, hotkey_bindings)
        hotkeys.start()
        if hotkeys.registered:
            print(f"[main] Хоткеи зарегистрированы: {', '.join(hotkeys.registered)} "
                  f"({describe_hotkeys(hotkey_bindings)})")
        if hotkeys.failed:
            print(f"[main] Хоткеи заняты другой программой: {', '.join(hotkeys.failed)}",
                  file=sys.stderr)

        # Настройки живут в оверлейном меню (F8). Отдельного окна больше
        # нет: оно было вторым интерфейсом с теми же полями, воровало фокус
        # у игры и тянуло за собой весь tcl/tk в runtime.

        guides = TemporalGuideGenerator(work_w, work_h)

        # Переиспользуемый буфер: каждый кадр аллоцирует ~100 МБ (захват 4K
        # + ресайзы + flow), GC не успевает → OOM на ~1900 кадрах. Буфер
        # переиспользуем через cv2.resize(dst=...). work/out-буферы не нужны:
        # в v3 ресайз full→work→full делает воркер на GPU (NGX Upscaling).
        buf_full = np.empty((height, width, 4), dtype=np.uint8)

        paused = False
        frame_index = 0
        pts = 0
        guide = None  # инициализация до цикла: F9 до первого NR-кадра не должен давать NameError
        output_rgba = None  # последний NR-кадр (для скриншота); None до первого
        # Режим WNDO: кадр показывает воркер, в Python пиксели не приходят.
        want_present = bool(cfg.get("worker_present", True))
        want_motion_small = bool(cfg.get("motion_on_gpu", True))
        want_dda = bool(cfg.get("capture_in_worker", True))  # DDA: цвет берёт воркер
        motion_small = False  # воркер растягивает поле движения сам
        motion_attempted = False  # пробовали для текущего воркера
        present_mode = False      # окно воркера сейчас поднято
        present_attempted = False  # пробовали для текущего воркера (не спамить)
        dda_mode = False          # воркер захватывает экран сам
        dda_attempted = False     # пробовали для текущего воркера (не спамить)
        gray_active = False       # guides берут luminance из gray-канала воркера
        pending_shot: Path | None = None  # скриншот ждёт кадр с пикселями
        recorder: VideoRecorder | None = None  # запись (Insert), MP4 AV1 NVENC
        work_frame = None  # текущий work-кадр; None → захватить в начале цикла
        fps_window: list[float] = []
        last_log = time.monotonic()
        last_fps = 0.0
        # Тайминги этапов: средние мс за PERF_LOG_INTERVAL (лог [perf])
        perf: dict[str, list[float]] = {k: [] for k in PERF_KEYS}
        last_perf_log = time.monotonic()

        def _ask_save_path(parent_hwnd: int, default_name: str) -> Path | None:
            """Нативный диалог «Сохранить как» (GetSaveFileNameW).

            Возвращает выбранный путь или None при отмене. JPEG-фильтр по
            умолчанию; расширение добавляется, если пользователь его не
            указал.
            """
            try:
                import ctypes
                from ctypes import wintypes

                class OPENFILENAME(ctypes.Structure):
                    _fields_ = [
                        ("lStructSize", wintypes.DWORD),
                        ("hwndOwner", wintypes.HWND),
                        ("hInstance", wintypes.HINSTANCE),
                        ("lpstrFilter", wintypes.LPCWSTR),
                        ("lpstrCustomFilter", wintypes.LPWSTR),
                        ("nMaxCustFilter", wintypes.DWORD),
                        ("nFilterIndex", wintypes.DWORD),
                        ("lpstrFile", wintypes.LPWSTR),
                        ("nMaxFile", wintypes.DWORD),
                        ("lpstrFileTitle", wintypes.LPWSTR),
                        ("nMaxFileTitle", wintypes.DWORD),
                        ("lpstrInitialDir", wintypes.LPCWSTR),
                        ("lpstrTitle", wintypes.LPCWSTR),
                        ("Flags", wintypes.DWORD),
                        ("nFileOffset", wintypes.WORD),
                        ("nFileExtension", wintypes.WORD),
                        ("lpstrDefExt", wintypes.LPCWSTR),
                        ("lCustData", wintypes.LPARAM),
                        ("lpfnHook", wintypes.LPVOID),
                        ("lpTemplateName", wintypes.LPCWSTR),
                        ("pvReserved", wintypes.LPVOID),
                        ("dwReserved", wintypes.DWORD),
                        ("FlagsEx", wintypes.DWORD),
                    ]

                buf = ctypes.create_unicode_buffer(1024)
                buf.value = default_name
                ofn = OPENFILENAME()
                ofn.lStructSize = ctypes.sizeof(OPENFILENAME)
                ofn.hwndOwner = parent_hwnd or None
                ofn.lpstrFilter = "JPEG image (*.jpg)\0*.jpg\0PNG image (*.png)\0*.png\0All files (*.*)\0*.*\0"
                ofn.lpstrFile = buf
                ofn.nMaxFile = 1024
                ofn.lpstrDefExt = "jpg"
                ofn.Flags = 0x00000002 | 0x00000008  # OFN_OVERWRITEPROMPT | OFN_PATHMUSTEXIST
                ok = ctypes.windll.comdlg32.GetSaveFileNameW(ctypes.byref(ofn))
                if not ok:
                    return None
                path = Path(buf.value.strip())
                if not path.suffix:
                    path = path.with_suffix(".jpg")
                return path
            except Exception as exc:
                print(f"[main] Диалог сохранения недоступен ({exc}) — "
                      f"скриншот в screenshots/", file=sys.stderr)
                shot_dir = BASE_DIR / "screenshots"
                shot_dir.mkdir(exist_ok=True)
                stamp = time.strftime("%Y%m%d-%H%M%S")
                stamp = f"{stamp}-{time.time() % 1 * 1000:03.0f}"
                return shot_dir / f"neuralscreen-{stamp}.jpg"

        def _save_screenshot(path: Path, rgba) -> None:
            """Сохранить кадр в JPEG максимального качества.

            Открытое меню попадает в скриншот: наш слой исключён из
            захвата, поэтому его рисуем на кадр сами.
            """
            try:
                surf = pygame.image.frombuffer(
                    rgba, (rgba.shape[1], rgba.shape[0]), "RGBX")
                display.draw_capture_overlay(surf)
            except Exception as exc:
                print(f"[main] Меню на скриншот не легло: {exc}", file=sys.stderr)
            try:
                import cv2 as _cv2
                path.parent.mkdir(parents=True, exist_ok=True)
                ok = _cv2.imwrite(str(path),
                                  _cv2.cvtColor(rgba, _cv2.COLOR_RGBA2BGRA),
                                  [_cv2.IMWRITE_JPEG_QUALITY, 100])
                if ok:
                    print(f"[main] Скриншот: {path}")
                    display.alert(f"Screenshot: {path.name}")
                else:
                    print(f"[main] Ошибка записи скриншота: {path}", file=sys.stderr)
            except Exception as exc:
                print(f"[main] Ошибка скриншота: {exc}", file=sys.stderr)

        def _perf(key: str, t0: float) -> None:
            """Записать длительность этапа (мс) в словарь таймингов."""
            perf[key].append((time.perf_counter() - t0) * 1000.0)
        running = True
        # Защита от быстрых изменений (автоповтор стрелок, дёрганье слайдера):
        # промежуточные значения coalescятся, применяется только последнее.
        # 0.5 c, а не 2 c: смена идёт через RNSZ в живом процессе воркера,
        # а не через рестарт с NGX init/shutdown + sleep(2) — дорогой путь
        # остался только фолбэком.
        RESTART_COOLDOWN = 0.5  # сек
        RESTART_WARMUP = 10     # warmup после смены разрешения (не фризить экран)
        RACK_TIMEOUT = 20.0     # сек, ожидание RACK после RNSZ
        last_restart = 0.0
        pending_apply: tuple | None = None  # отложенное (scale, profile, params)
        # Лимит авто-восстановления: если воркер умирает N раз подряд —
        # выключаем NR (пауза) и алертим, чтобы не крутить цикл рестартов.
        MAX_CONSECUTIVE_RESTARTS = 3
        consecutive_restarts = 0
        guide_fails = 0

        def _recreate_capture() -> None:
            """Пересоздать захват (свежая DDA-сессия) после сбоя/смены режима."""
            nonlocal capture
            try:
                capture.close()
            except Exception:
                pass
            capture = ScreenCapture(monitor_idx=monitor)

        def _safe_grab() -> np.ndarray | None:
            """grab() с пересозданием захвата при сбое.

            Запуск игры в fullscreen инвалидирует Desktop Duplication
            (DXGI_ERROR_ACCESS_LOST / смена режима) — dxcam может бросить
            исключение вместо None. Пересоздаём DDA-сессию и возвращаем
            None (цикл пропустит итерацию).
            """
            nonlocal capture
            try:
                return capture.grab()
            except Exception as exc:
                print(f"[main] Сбой захвата ({exc}) — пересоздаю DDA-сессию")
                try:
                    _recreate_capture()
                except Exception as exc2:
                    print(f"[main] Пересоздание захвата не удалось: {exc2}",
                          file=sys.stderr)
                return None

        def _do_restart(new_scale: float, new_profile: str, new_params: dict) -> None:
            """Сменить work_scale/профиль/параметры БЕЗ пересоздания pygame/захвата.

            Основной путь — RNSZ: воркер пересоздаёт NGX feature в том же
            процессе и отвечает RACK (~0.3 c вместо ~3 c на рестарт). Если
            RNSZ не прошёл — полный рестарт процесса воркера.

            КРИТИЧНО: guides пересоздаётся по НОВОМУ work-разрешению и
            присваивается во внешнюю переменную (nonlocal guides). Раньше
            присваивание было локальным — внешний guides оставался старого
            размера, и main слал motion старых размеров, тогда как воркер
            читает ровно new_w*new_h*4 байт:
              * scale вверх  → воркер ждёт недостающие байты и молчит, main
                виснет в reader.recv(60 c), окно не качает сообщения →
                Application Hang (Event Id 1002) → exit 127;
              * scale вниз   → лишние байты рассинхронизируют поток, воркер
                видит чужую магию и выходит → BrokenPipe → цикл рестартов.
            Воспроизведено изолированно: _work/test_stale_motion_repro.py
            (случай A — TimeoutError, B — BrokenPipeError, C — контроль OK).
            pygame/D3D11 к вылетам отношения не имел.
            """
            nonlocal work_scale, work_w, work_h, params, frame_index, pts, work_frame
            nonlocal worker, worker_logs, reader, worker_stop, last_restart
            nonlocal guides  # ← без этого main шлёт motion старого размера
            work_scale = new_scale
            cfg["profile"] = new_profile
            params = new_params
            new_w, new_h = _work_size(width, height, work_scale)
            new_full_w = width if (new_w != width or new_h != height) else 0
            new_full_h = height if (new_w != width or new_h != height) else 0
            print(f"[main] Применяю: профиль {new_profile!r}, "
                  f"work_scale {work_scale:.2f} ({new_w}x{new_h}), params {params}")
            display.alert(UI_STRINGS[lang]["settings_applied"])

            applied = False
            if worker.poll() is None:
                try:
                    t_rnsz = time.perf_counter()
                    send_resize(worker, params, new_w, new_h, RESTART_WARMUP,
                                new_full_w, new_full_h)
                    reader.wait_rack(timeout=RACK_TIMEOUT)
                    reader.set_output_size(new_full_w or new_w, new_full_h or new_h)
                    applied = True
                    print(f"[main] RNSZ применён: {new_w}x{new_h} за "
                          f"{(time.perf_counter() - t_rnsz) * 1000:.0f} мс")
                except Exception as exc:
                    print(f"[main] RNSZ не прошёл ({exc}) — полный рестарт воркера",
                          file=sys.stderr)
            if not applied:
                worker, worker_logs, reader, worker_stop = restart_worker(
                    worker, params, new_w, new_h, RESTART_WARMUP,
                    new_full_w, new_full_h, worker_stop, shm)
                _forget_present()
                # Новый воркер не знает про DDA/gray: сбросить флаги, чтобы
                # главный цикл заново послал DDA1/GRAY. Иначе кадры уходят с
                # NO_COLOR в воркер без захвата — рассинхрон, цикл рестартов.
                _forget_dda()

            # Порядок важен: work_w/work_h и guides меняются ВМЕСТЕ, иначе
            # размер motion разойдётся с тем, что ждёт воркер (см. docstring).
            work_w, work_h = new_w, new_h
            guides = TemporalGuideGenerator(work_w, work_h, emit_small=motion_small)
            _sync_motion_size()  # разрешение потока могло измениться
            _sync_gray()         # gray-канал живёт в воркере, размер = flow guides
            frame_index = 0
            pts = 0
            work_frame = None  # индексы сброшены — нужен свежий захват
            tray._set_state(scale=work_scale)
            last_restart = time.monotonic()

        def _switch_monitor(new_monitor: int) -> None:
            """Сменить монитор захвата/вывода — полный перезапуск конвейера.

            Разрешение, захват, окно, воркер и shm завязаны на монитор —
            на лету не переключить. Запись останавливается (размер кадра
            меняется). Меню пересоздаётся с сохранением темы/языка/раскладки.
            """
            nonlocal monitor, width, height, work_w, work_h
            nonlocal shm, worker, worker_logs, reader, worker_stop
            nonlocal capture, display, guides, buf_full
            nonlocal frame_index, pts, work_frame, recorder, pending_shot
            nonlocal present_mode, present_attempted, dda_mode, dda_attempted
            nonlocal gray_active, motion_small, motion_attempted, gpu_ok
            if new_monitor == monitor:
                return
            print(f"[main] Смена монитора: {monitor} -> {new_monitor}")
            # Запись: размер кадра изменится — закрываем честно (moov).
            if recorder is not None:
                try:
                    recorder.close()
                except Exception as exc:
                    print(f"[main] Ошибка закрытия записи: {exc}", file=sys.stderr)
                recorder = None
            pending_shot = None
            # Воркер и shm — старые размеры.
            shutdown_worker(worker, worker_stop)
            try:
                shm.close()
            except Exception:
                pass
            try:
                capture.close()
            except Exception:
                pass
            # Новый монитор: реальное разрешение.
            monitor = new_monitor
            cfg["monitor"] = monitor
            capture = ScreenCapture(monitor_idx=monitor)
            width, height = capture.resolution
            work_w, work_h = _work_size(width, height, work_scale)
            full_w = width if (work_w != width or work_h != height) else 0
            full_h = height if (work_w != width or work_h != height) else 0
            shm = SharedFrameBuffer(width, height)
            worker, worker_logs, reader, worker_stop = start_worker(
                params, work_w, work_h, warmup, full_w, full_h, shm)
            # Окно и меню — заново, с сохранением пользовательских настроек.
            try:
                display.close()
            except Exception:
                pass
            display = Display(width, height, fullscreen=bool(cfg["fullscreen"]))
            display.set_lang(lang)
            display.menu.set_user_scale(float(cfg.get("menu_scale", 1.0)))
            saved_theme = cfg.get("theme")
            if isinstance(saved_theme, str) and saved_theme in ("light", "dark"):
                display.menu.set_state({"theme": saved_theme})
            display.menu.set_state({"lang": lang})
            saved_offset = cfg.get("menu_offset")
            if isinstance(saved_offset, (list, tuple)) and len(saved_offset) == 2:
                display.menu.offset = [int(saved_offset[0]), int(saved_offset[1])]
            # guides и буферы — под новое разрешение.
            guides = TemporalGuideGenerator(work_w, work_h, emit_small=motion_small)
            buf_full = np.empty((height, width, 4), dtype=np.uint8)
            # Флаги конвейера — новый воркер ничего не знает.
            present_mode = False
            present_attempted = False
            dda_mode = False
            dda_attempted = False
            gray_active = False
            motion_small = False
            motion_attempted = False
            gpu_ok = None  # новый воркер — новый вердикт feature 18
            frame_index = 0
            pts = 0
            work_frame = None
            _save_menu_layout()
            print(f"[main] Монитор {monitor}: {width}x{height}, "
                  f"work {work_w}x{work_h}")
            display.alert(f"Monitor {monitor}: {width}x{height}")

        def _sync_motion_size() -> None:
            """MOTS: согласовать с воркером разрешение поля движения.

            Зовётся после создания guides и после каждого запуска воркера:
            команда живёт в процессе воркера и новый про неё не знает.
            Отказ не смертелен — считаем растяжение на CPU, как раньше.
            """
            nonlocal motion_small, motion_attempted
            motion_attempted = True
            if not want_motion_small:
                return
            guides.emit_small = True
            try:
                send_motion_size(worker, guides.motion_width, guides.motion_height)
                reader.wait_mack(timeout=15.0)
                motion_small = True
                print(f"[main] Поле движения {guides.motion_width}x{guides.motion_height} — "
                      f"растягивает воркер на GPU")
            except Exception as exc:
                guides.emit_small = False
                motion_small = False
                print(f"[main] Растяжение motion на GPU недоступно ({exc}) — считаем на CPU",
                      file=sys.stderr)

        def _enable_present() -> None:
            """Попросить воркера показывать кадр самому (WNDO).

            Отказ не смертелен: остаёмся на возврате пикселей в Python и
            обычной отрисовке в pygame — этот путь никуда не делся.
            """
            nonlocal present_mode, present_attempted
            present_attempted = True
            try:
                send_window(worker, width, height, 0)
                reader.wait_wack(timeout=15.0)
                present_mode = True
                display.set_hud_only(True)
                display.raise_topmost()  # HUD должен быть НАД окном воркера
                print("[main] Вывод в окне воркера: кадр не возвращается в Python")
            except Exception as exc:
                present_mode = False
                display.set_hud_only(False)
                print(f"[main] Окно воркера недоступно ({exc}) — вывод через pygame",
                      file=sys.stderr)

        def _disable_present() -> None:
            """Закрыть окно воркера и вернуться к отрисовке кадра в pygame."""
            nonlocal present_mode, present_attempted
            if not present_mode:
                return
            try:
                send_window(worker, 0, 0, WINDOW_FLAG_DISABLE)
                reader.wait_wack(timeout=10.0)
            except Exception as exc:
                print(f"[main] Не удалось закрыть окно воркера: {exc}", file=sys.stderr)
            present_mode = False
            present_attempted = False  # после паузы окно можно поднять снова
            display.set_hud_only(False)

        def _forget_present() -> None:
            """Воркер перезапущен — его окно и настройки умерли с процессом."""
            nonlocal present_mode, present_attempted, motion_small, motion_attempted
            present_mode = False
            present_attempted = False
            motion_small = False
            motion_attempted = False
            display.set_hud_only(False)

        def _sync_gray() -> None:
            """GRAY: перевыговорить обратный канал luminance под guides.

            Воркер пишет в маппинг ровно flow-размер guides. Канал меняется
            вместе с guides (после RNSZ flow может измениться), поэтому
            пересинхронизация нужна в _enable_dda и после apply.
            Отказ не смертелен — guides останутся на dxcam.
            """
            nonlocal gray_active
            if not dda_mode:
                return
            try:
                gw, gh = guides.flow_width, guides.flow_height
                shm.open_gray(gw, gh)
                send_gray(worker, gw, gh, shm.gray_name)
                reader.wait_gak(timeout=15.0)
                gray_active = True
                print(f"[main] Gray-канал {gw}x{gh}: guides берут luminance из воркера")
            except Exception as exc:
                gray_active = False
                print(f"[main] Gray-канал недоступен ({exc}) — guides через dxcam",
                      file=sys.stderr)

        def _enable_dda() -> None:
            """Попросить воркера захватывать экран самому (DDA1).

            Пока активен, кадры FRM1 несут FRAME_FLAG_NO_COLOR — цвет в пайп
            не идёт, воркер берёт его из Desktop Duplication прямо на GPU.
            Вместе с DDA активируем обратный gray-канал: воркер пишет туда
            luminance (размер поля потока), guides читают его и не зависят
            от dxcam. Отказ не смертелен: остаёмся на передаче из Python.
            """
            nonlocal dda_mode, dda_attempted, capture
            dda_attempted = True
            try:
                # Кадр в DDA-режиме всё равно должен быть у guides (motion),
                # поэтому dxcam продолжает работать — просто цвет не шлём воркеру.
                send_dda(worker, width, height, 0)
                reader.wait_dack(timeout=15.0)
                dda_mode = True
                _sync_gray()
                print("[main] Захват экрана в воркере (DDA1): цвет не идёт через пайп")
            except Exception as exc:
                dda_mode = False
                print(f"[main] Захват в воркере недоступен ({exc}) — кадры через Python",
                      file=sys.stderr)

        def _disable_dda() -> None:
            """Выключить захват в воркере и вернуться к передаче кадра из Python."""
            nonlocal dda_mode
            if not dda_mode:
                return
            try:
                send_dda(worker, 0, 0, 0)
                reader.wait_dack(timeout=10.0)
            except Exception as exc:
                print(f"[main] Не удалось выключить захват в воркере: {exc}", file=sys.stderr)
            dda_mode = False

        def _forget_dda() -> None:
            """Воркер перезапущен — его DDA-захват умер с процессом."""
            nonlocal dda_mode, dda_attempted, gray_active
            dda_mode = False
            dda_attempted = False
            gray_active = False

        def _save_menu_layout() -> None:
            """Запомнить размер и положение панели в config.json.

            Пишем на закрытии меню и на выходе, а не на каждое движение мыши:
            перетаскивание иначе молотило бы файл десятки раз в секунду.
            """
            try:
                data = json.loads(args.config.read_text(encoding="utf-8"))
                data["menu_scale"] = round(display.menu.user_scale, 2)
                data["open_menu_on_start"] = startup_menu
                data["split"] = round(split_pos, 2)
                data["theme"] = display.menu.state.get("theme", "light")
                data["lang"] = lang
                data["menu_offset"] = [int(display.menu.offset[0]),
                                       int(display.menu.offset[1])]
                args.config.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
            except Exception as exc:
                print(f"[main] Не удалось сохранить вид меню: {exc}", file=sys.stderr)

        def _refresh_gpu_ok() -> None:
            """Работает ли NR — по ответу воркера, а не по архитектуре.

            Наверняка это знает только воркер: он зовёт CreateFeature и
            получает код NGX. Архитектура говорит лишь о том, что обещает
            NVIDIA. Решённый ответ не пересматриваем — рестарты воркера
            добавляют строки, но вердикт от этого не меняется.
            """
            nonlocal gpu_ok
            if gpu_ok is not None:
                return
            for line in reversed(worker_logs[-80:]):
                if "feature 18 ready" in line:
                    gpu_ok = True
                    return
                # Реальная строка отказа воркера — «[pure] direct feature 18
                # create failed»; «Unsupported GPU architecture» живёт внутри
                # nvngx_dlssnr.dll и в stderr воркера не попадает.
                if "feature 18 create failed" in line:
                    gpu_ok = False
                    return

        def _menu_payload() -> dict:
            """Текущее состояние для меню — один источник правды."""
            _refresh_gpu_ok()
            return {
                "nr": not paused,
                "work_scale": work_scale,
                "profile": cfg["profile"],
                "profiles": list(PROFILES),
                "params": {k: params[k] for k in
                           ("intensity", "local_tone",
                            "local_structure", "skin_structure")},
                "lang": lang,
                "recording": recorder is not None,
                "work_size": f"{work_w}x{work_h}",
                "rec_seconds": (recorder.duration_ms / 1000.0) if recorder else 0.0,
                "open_on_start": startup_menu,
                "autostart": _autostart_enabled(),
                "split": split_pos,
                "gpu_text": gpu_text,
                "gpu_ok": gpu_ok,
                "monitor": str(monitor),
                "monitors": [f"{i}: {w}x{h}" for i, w, h in list_monitors()],
            }

        def _apply_menu_action(action: tuple) -> None:
            """Действие из меню -> реальная настройка.

            Меню ничего не меняет само: оно сообщает, чего хочет пользователь,
            а решение принимается здесь, там же где живут params и cfg.
            """
            nonlocal lang, running, startup_menu, split_pos
            kind = action[0]
            if kind == "nr":
                tray_commands.put("toggle")
            elif kind == "split":
                # Воркер пересоздавать не нужно: позиция шторки едет в
                # заголовке каждого кадра.
                split_pos = min(1.0, max(0.0, float(action[1])))
            elif kind == "toggle" and action[1] == "open_on_start":
                startup_menu = not startup_menu
                _save_menu_layout()
                print(f"[main] Меню при запуске: {'да' if startup_menu else 'нет'}")
            elif kind == "toggle" and action[1] == "autostart":
                # Автозапуск с Windows (HKCU Run). Состояние хранится в
                # реестре, не в config — читаем и инвертируем.
                new_state = not _autostart_enabled()
                if _set_autostart(new_state):
                    print(f"[main] Автозапуск с Windows: {'вкл' if new_state else 'выкл'}")
                    display.alert(UI_STRINGS[lang].get(
                        "autostart_on" if new_state else "autostart_off",
                        "Autostart ON" if new_state else "Autostart OFF"))
                else:
                    display.alert(UI_STRINGS[lang].get("autostart_err", "Autostart failed"))
            elif kind == "param":
                new_params = dict(params)
                new_params[action[1]] = float(action[2])
                request_apply(work_scale, cfg["profile"], new_params)
            elif kind == "profile":
                request_apply(work_scale, action[1], dict(PROFILES[action[1]]))
            elif kind == "lang":
                if action[1] in UI_STRINGS and action[1] != lang:
                    lang = action[1]
                    display.set_lang(lang)
                    display.menu.set_state({"lang": lang})
                    print(f"[main] Язык интерфейса -> {lang}")
            elif kind == "theme":
                # Меню уже применило тему к себе (overlay_ui), здесь только
                # запоминаем для config.json — _save_menu_layout() вызывается
                # при закрытии меню и на выходе.
                print(f"[main] Тема меню -> {action[1]}")
            elif kind == "monitor":
                # Значение приходит как "N: WxH" — берём индекс до двоеточия.
                try:
                    new_monitor = int(str(action[1]).split(":")[0])
                except (ValueError, IndexError):
                    print(f"[main] Неверный монитор: {action[1]!r}", file=sys.stderr)
                    return
                if new_monitor != monitor:
                    _switch_monitor(new_monitor)
            elif kind == "button":
                name = action[1]
                if name == "close":
                    display.menu.visible = False
                    display.set_menu_opaque(False)
                    display.set_menu_input(False)
                    _save_menu_layout()
                elif name == "exit":
                    print(f"[main] Выход: кнопка в меню оверлея "
                          f"(кадров обработано {frame_index})")
                    running = False
                elif name == "record":
                    tray_commands.put("record")
                elif name == "screenshot":
                    tray_commands.put("screenshot_menu")
                elif name == "github":
                    # Хоткеи, профили и требования описаны только в README —
                    # из самой программы про них узнать было неоткуда.
                    try:
                        import webbrowser
                        webbrowser.open(REPO_URL)
                        display.alert(UI_STRINGS[lang]["github_opened"])
                    except Exception as exc:
                        print(f"[main] Не удалось открыть {REPO_URL}: {exc}",
                              file=sys.stderr)

        def request_apply(new_scale: float, new_profile: str, new_params: dict) -> None:
            """Применить настройки с coalescing по RESTART_COOLDOWN.

            Единая точка для окна настроек, трея и хоткеев: раньше проверку
            кулдауна делал только путь настроек, а трей и стрелки звали
            _do_restart напрямую — автоповтор стрелки давал шквал RNSZ.
            """
            nonlocal pending_apply
            if time.monotonic() - last_restart < RESTART_COOLDOWN:
                pending_apply = (new_scale, new_profile, new_params)
                print(f"[main] Применение отложено (cooldown {RESTART_COOLDOWN:.1f} c), "
                      f"применится последнее значение")
            else:
                _do_restart(new_scale, new_profile, new_params)

        while running:
            loop_start = time.perf_counter()
            now = time.monotonic()

            # Команды из трея (thread-safe очередь)
            try:
                while True:
                    cmd = tray_commands.get_nowait()
                    if cmd == "quit":
                        print(f"[main] Выход: трей или Ctrl+Alt+Q "
                              f"(кадров обработано {frame_index})")
                        running = False
                    elif cmd == "settings":
                        # F8 и левый клик по трею открывают меню в оверлее —
                        # единственное место, где живут настройки.
                        display.menu.set_state(_menu_payload())
                        opened = display.menu.toggle()
                        display.set_menu_opaque(opened)
                        display.set_menu_input(opened)
                        if not opened:
                            _save_menu_layout()
                        print(f"[main] Меню в оверлее {'открыто' if opened else 'закрыто'}")
                    elif cmd == "toggle":
                        paused = not paused
                        if not paused:
                            work_frame = None  # свежий захват после паузы
                        print(f"[main] NR {'OFF (bypass NGX)' if paused else 'ON'}")
                        display.alert(UI_STRINGS[lang]["nr_off" if paused else "nr_on"])
                        tray._set_state(nr=not paused)
                    elif cmd == "screenshot_menu":
                        # Нативный диалог «Сохранить как» (родитель — окно
                        # оверлея). Блокирует main-цикл на время выбора —
                        # приемлемо: пользователь сам решает, куда класть.
                        shot_path = _ask_save_path(
                            display.get_hwnd(),
                            default_name=f"neuralscreen-{time.strftime('%Y%m%d-%H%M%S')}.jpg")
                        if shot_path is None:
                            print("[main] Скриншот отменён пользователем")
                            continue
                        if present_mode:
                            pending_shot = shot_path
                            print(f"[main] Скриншот со следующего кадра: {shot_path}")
                        elif output_rgba is not None:
                            _save_screenshot(shot_path, output_rgba)
                        else:
                            display.alert("No frame yet")
                    elif cmd == "record":
                        # Insert: запись NR-кадра в MP4. Кадры запрашиваем у
                        # воркера через FRAME_FLAG_WANT_PIXELS (механизм
                        # скриншота, но для каждого кадра записи).
                        if recorder is None:
                            rec_dir = BASE_DIR / "recordings"
                            rec_dir.mkdir(exist_ok=True)
                            stamp = time.strftime("%Y%m%d-%H%M%S")
                            # Две записи в одну секунду не должны перезаписывать
                            # друг друга — добавляем миллисекунды.
                            stamp = f"{stamp}-{time.time() % 1 * 1000:03.0f}"
                            path = str(rec_dir / f"neuralscreen-{stamp}.mp4")
                            try:
                                recorder = VideoRecorder(path, width, height, fps=60)
                            except Exception as exc:
                                print(f"[main] Запись не стартовала: {exc}", file=sys.stderr)
                                display.alert(f"REC ERROR: {exc}")
                                recorder = None
                            else:
                                print(f"[main] Запись начата: {path}")
                                display.alert(UI_STRINGS[lang]["record_on"])
                        else:
                            rec_path = recorder.path
                            try:
                                recorder.close()
                            except Exception as exc:
                                print(f"[main] Ошибка закрытия записи: {exc}", file=sys.stderr)
                            secs = recorder.duration_ms / 1000.0
                            print(f"[main] Запись завершена: {rec_path} "
                                  f"({recorder.written} кадров, {secs:.1f}с)")
                            display.alert(UI_STRINGS[lang]["record_off"])
                            recorder = None
                    elif cmd in ("scale_up", "scale_down"):
                        delta = WORK_SCALE_STEP if cmd == "scale_up" else -WORK_SCALE_STEP
                        new_scale = min(WORK_SCALE_MAX, max(WORK_SCALE_MIN, work_scale + delta))
                        if abs(new_scale - work_scale) > 1e-6:
                            new_w, new_h = _work_size(width, height, new_scale)
                            print(f"[main] work_scale -> {new_scale:.2f} ({new_w}x{new_h})")
                            display.alert(UI_STRINGS[lang]["work_scale_changed"].format(new_scale, new_w, new_h))
                            request_apply(new_scale, cfg["profile"], params)
            except queue.Empty:
                pass

            # Отложенное применение (coalescing): если рестарт был недавно,
            # применяем последнее значение после паузы
            if pending_apply is not None and time.monotonic() - last_restart >= RESTART_COOLDOWN:
                p_scale, p_profile, p_params = pending_apply
                pending_apply = None
                print("[main] Применяю отложенные настройки")
                _do_restart(p_scale, p_profile, p_params)

            if not running:
                break

            # NR OFF — bypass: конвейер продолжает крутиться (захват → показ
            # сырого кадра в окне воркера), но NGX-эффект пропущен. Оверлей
            # (картинка + HUD) остаётся живым и предсказуемым; прячем всё
            # только при реальном выходе. Bypass-кадр шлём как обычный (флаг
            # в заголовке), чтобы парность send/recv не нарушалась.
            bypass = paused
            # (для читаемости: в send_frame передаём bypass=bypass)

            if want_present and not present_mode and not present_attempted:
                _enable_present()
            if want_dda and not dda_mode and not dda_attempted:
                _enable_dda()
            if want_motion_small and not motion_small and not motion_attempted:
                motion_attempted = True
                _sync_motion_size()

            # --- Ввод в меню оверлея ---------------------------------
            # События читаем только когда меню открыто: в остальное время
            # окно click-through, событий нет, а лишний get() съедал бы
            # очередь у pump() внутри отрисовки.
            if display.menu.visible:
                for ev in pygame.event.get():
                    for action in display.menu.handle_event(ev):
                        _apply_menu_action(action)
                if not display.menu.dragging:
                    display.menu.set_state(_menu_payload())

            # --- Захват вперёд: пока NGX считает кадр N, захватываем N+1 ---
            # work_frame == None бывает: первый кадр, после рестарта воркера
            # (смена work_scale), после grab()==None. Тогда захват идёт в
            # начале итерации, ДО send — синхронизация с воркером не теряется
            # (send/recv всегда парные, recv обязателен после любого send).
            # Воркер (v3, NGX Upscaling) сам ресайзит full→work→full на GPU:
            # Python шлёт full-res кадр, motion — work-res (guides создан
            # с work_w/work_h и сам уменьшает вход), получает full-res.
            if work_frame is None and not gray_active:
                t0 = time.perf_counter()
                frame = _safe_grab()
                _perf("grab", t0)
                if frame is None:
                    continue  # кадр ещё не готов — пропускаем итерацию
                if frame.shape[1] != width or frame.shape[0] != height:
                    t0 = time.perf_counter()
                    try:
                        cv2.resize(frame, (width, height), interpolation=cv2.INTER_LANCZOS4, dst=buf_full)
                    except cv2.error:
                        # Смена разрешения монитора: buf_full предвыделен под
                        # старый размер — пересоздаём и повторяем без dst
                        buf_full = np.empty((height, width, 4), dtype=np.uint8)
                        cv2.resize(frame, (width, height), interpolation=cv2.INTER_LANCZOS4, dst=buf_full)
                    _perf("resize_full", t0)
                    frame = buf_full
                else:
                    frame = np.ascontiguousarray(frame, dtype=np.uint8)
                work_frame = frame

            # --- Отправка кадра с авто-восстановлением ---
            # Воркер может умереть/зависнуть (NGX после RNSZ, GPU-конфликт) —
            # вместо вылета main перезапускает воркера с текущими параметрами
            # и продолжает. Это финальная защита: программа не падает.
            try:
                t0 = time.perf_counter()
                if gray_active:
                    guide = guides.process(gray=shm.read_gray())
                else:
                    guide = guides.process(work_frame)
                _perf("guides", t0)
            except Exception as guide_exc:
                # guides — не критичен: ValueError/TypeError/cv2.error (форма
                # gray-кадра, деление на ноль) не должны валить процесс.
                # Пропускаем кадр — воркер получит следующий. Но устойчивая
                # ошибка (несовместимый gray-канал, битая форма) зациклит
                # main на 100% CPU — после 5 сбоев подряд уходим на нулевой
                # motion-фолбэк: кадр продолжит идти, картинка не замрёт.
                print(f"[main] guides.process упал ({guide_exc}) — кадр пропущен",
                      file=sys.stderr)
                guide_fails += 1
                if guide_fails >= 5:
                    print(f"[main] guides.process нестабилен — нулевой motion "
                          f"(кадры продолжают идти)", file=sys.stderr)
                    guide_fails = 0
                    guide = guides.zero_guide()
                else:
                    continue
            try:
                check_worker(worker, worker_logs)
                t0 = time.perf_counter()
                send_frame(worker, frame_index, work_frame, guide.motion, guide.reset,
                           pts, shm, want_pixels=(pending_shot is not None or recorder is not None),
                           motion_small=motion_small,
                           no_color=bool(dda_mode),
                           bypass=bypass,
                           split=split_pos)
                _perf("send", t0)
            except (BrokenPipeError, OSError, EOFError, RuntimeError) as exc:
                consecutive_restarts += 1
                if consecutive_restarts >= MAX_CONSECUTIVE_RESTARTS:
                    print(f"[main] Воркер умирает {consecutive_restarts} раз подряд — NR OFF")
                    paused = True
                    display.alert(UI_STRINGS[lang]["nr_off"])
                    tray._set_state(nr=False)
                    consecutive_restarts = 0
                    work_frame = None
                    continue
                print(f"[main] Воркер потерян при отправке ({exc}) — перезапуск "
                      f"({consecutive_restarts}/{MAX_CONSECUTIVE_RESTARTS})")
                if worker_logs:
                    print("[main] stderr воркера (хвост):")
                    for line in worker_logs[-15:]:
                        print(f"  {line}")
                worker, worker_logs, reader, worker_stop = restart_worker(
                    worker, params, work_w, work_h, 10,
                    width if (work_w != width or work_h != height) else 0,
                    height if (work_w != width or work_h != height) else 0,
                    worker_stop, shm)
                _forget_present()
                _forget_dda()
                _sync_motion_size()
                frame_index = 0
                pts = 0
                work_frame = None
                continue

            # Захват следующего кадра ПОКА воркер считает текущий (NGX
            # ~70-100 мс/кадр — узкое место). dxcam потокобезопасен в одном
            # потоке — второй поток не нужен, просто переставляем grab()
            # между send и recv. Буферы: send_frame копирует данные в pipe
            # (tobytes), guides.process не держит ссылок на вход — buf_full
            # можно переиспользовать сразу.
            # В DDA-режиме кадр берёт воркер сам — Python не захватывает.
            next_frame = None
            if not gray_active:
                t0 = time.perf_counter()
                next_frame = _safe_grab()
                _perf("grab", t0)
            if next_frame is not None:
                if next_frame.shape[1] != width or next_frame.shape[0] != height:
                    t0 = time.perf_counter()
                    try:
                        cv2.resize(next_frame, (width, height), interpolation=cv2.INTER_LANCZOS4, dst=buf_full)
                    except cv2.error:
                        buf_full = np.empty((height, width, 4), dtype=np.uint8)
                        cv2.resize(next_frame, (width, height), interpolation=cv2.INTER_LANCZOS4, dst=buf_full)
                    _perf("resize_full", t0)
                    next_frame = buf_full
                else:
                    next_frame = np.ascontiguousarray(next_frame, dtype=np.uint8)
            # next_frame == None: кадр не готов — следующий захват сделает
            # начало следующей итерации (work_frame = None). Синхронизация
            # с воркером не теряется: send уже отправлен, recv ниже обязателен.

            t0 = time.perf_counter()
            try:
                output_rgba = reader.recv(frame_index, timeout=5.0)
            except (TimeoutError, EOFError, RuntimeError, OSError) as exc:
                consecutive_restarts += 1
                if consecutive_restarts >= MAX_CONSECUTIVE_RESTARTS:
                    print(f"[main] Воркер молчит/умирает {consecutive_restarts} раз подряд — NR OFF")
                    paused = True
                    display.alert(UI_STRINGS[lang]["nr_off"])
                    tray._set_state(nr=False)
                    consecutive_restarts = 0
                    work_frame = None
                    continue
                print(f"[main] Воркер молчит/умер на кадре {frame_index} ({exc}) — перезапуск "
                      f"({consecutive_restarts}/{MAX_CONSECUTIVE_RESTARTS})")
                worker, worker_logs, reader, worker_stop = restart_worker(
                    worker, params, work_w, work_h, 10,
                    width if (work_w != width or work_h != height) else 0,
                    height if (work_w != width or work_h != height) else 0,
                    worker_stop, shm)
                _forget_present()
                _forget_dda()
                _sync_motion_size()
                frame_index = 0
                pts = 0
                work_frame = None
                continue
            _perf("recv", t0)
            # Кадр получен — цепочка сбоев прервана. Без сброса счётчик
            # копился за всю сессию, и три несвязанных сбоя (хоть с разницей
            # в час) выключали NR.
            consecutive_restarts = 0
            status = "NR OFF" if paused else "NR ON"
            pts += 1

            t0 = time.perf_counter()
            try:
                if recorder is not None and output_rgba is not None:
                    # Наш слой исключён из захвата (WDA_EXCLUDEFROM
                    # CAPTURE), поэтому открытое меню кладём на кадр сами.
                    # frombuffer ссылается на numpy-буфер (без копии):
                    # blit пишет прямо в output_rgba.
                    try:
                        surf = pygame.image.frombuffer(
                            output_rgba, (output_rgba.shape[1], output_rgba.shape[0]), "RGBX")
                        display.draw_capture_overlay(surf)
                    except Exception as menu_exc:
                        print(f"[main] Меню на кадр записи не легло: {menu_exc}",
                              file=sys.stderr)
                    # Запись — отдельный try: сбой кодера НЕ должен попадать в
                    # except «Сбой вывода» (тот пересоздаёт pygame-окно на
                    # каждом кадре — бесконечный цикл). Ошибка записи
                    # останавливает запись, а не окно.
                    try:
                        recorder.write(output_rgba)
                    except Exception as rec_exc:
                        print(f"[main] Ошибка записи кадра ({rec_exc}) — "
                              f"останавливаю запись", file=sys.stderr)
                        try:
                            recorder.close()
                        except Exception:
                            pass
                        recorder = None
                if present_mode:
                    # В WNDO-режиме кадр на экране рисует воркер; в Python
                    # пиксели приходят ТОЛЬКО по want_pixels (запись/скриншот).
                    # Показывать их в pygame не нужно: это лишний блендинг 4K
                    # (~22 мс) и мелькание кадра в HUD-слое поверх окна воркера.
                    # HUD обновляется draw_overlay() с троттлингом (не каждый кадр).
                    if pending_shot is not None and output_rgba is not None:
                        _save_screenshot(pending_shot, output_rgba)
                        pending_shot = None
                    display.draw_overlay()
                elif output_rgba is None:
                    # Кадр уже на экране — его показал воркер, тут только HUD
                    display.draw_overlay()
                else:
                    display.show(output_rgba)
                    if pending_shot is not None:
                        _save_screenshot(pending_shot, output_rgba)
                        pending_shot = None
            except Exception as exc:
                # Смена режима дисплея (запуск/выход из fullscreen-игры)
                # может убить контекст pygame/SDL — пересоздаём окно.
                print(f"[main] Сбой вывода ({exc}) — пересоздаю окно")
                try:
                    display.close()
                except Exception:
                    pass
                display = Display(width, height, fullscreen=bool(cfg["fullscreen"]))
                display.set_lang(lang)
                # Меню создаётся вместе с окном — возвращаем ему размер,
                # положение, тему и язык, иначе после запуска игры оно
                # прыгает в центр, светлеет и переходит на en.
                display.menu.set_user_scale(float(cfg.get("menu_scale", 1.0)))
                saved_theme = cfg.get("theme")
                if isinstance(saved_theme, str) and saved_theme in ("light", "dark"):
                    display.menu.set_state({"theme": saved_theme})
                display.menu.set_state({"lang": lang})
                saved = cfg.get("menu_offset")
                if isinstance(saved, (list, tuple)) and len(saved) == 2:
                    display.menu.offset = [int(saved[0]), int(saved[1])]
                if present_mode:
                    # Новое окно снова должно стать прозрачным слоем поверх воркера
                    display.set_hud_only(True)
                    display.raise_topmost()
                display.alert(UI_STRINGS[lang]["nr_on"])
            _perf("show", t0)
            display.set_hud({
                "fps": last_fps,
                "status": status,
                "resolution": f"{width}x{height}",
                "profile": cfg["profile"],
                "params": {k: v for k, v in params.items() if k not in ("profile", "preset", "style", "auto_mask", "ui_correction")},
                "frames": frame_index,
            })

            frame_index += 1
            if startup_pending and frame_index >= 2:
                # Ждём первый показанный кадр: открытое меню поверх ещё не
                # заполненного окна мигает чёрным.
                startup_pending = False
                if startup_menu:
                    display.menu.set_state(_menu_payload())
                    display.menu.visible = True
                    display.set_menu_opaque(True)
                    display.set_menu_input(True)
                    print("[main] Меню открыто при запуске")
                else:
                    display.alert(UI_STRINGS[lang]["started"], 3.5)
            work_frame = next_frame  # None → захват в начале следующей итерации
            fps_window.append(time.perf_counter() - loop_start)
            if len(fps_window) > 120:
                fps_window.pop(0)

            if now - last_log >= FPS_LOG_INTERVAL:
                last_fps = len(fps_window) / sum(fps_window) if fps_window else 0.0
                scene = f" | сцена {guide.scene_score:.3f}" if guide is not None else ""
                print(f"[main] {status} | FPS {last_fps:5.1f} | кадров {frame_index} | "
                      f"work {work_w}x{work_h}{scene}")
                last_log = now

            if now - last_perf_log >= PERF_LOG_INTERVAL:
                parts = []
                for key in PERF_KEYS:
                    samples = perf[key]
                    if samples:
                        parts.append(f"{key} {sum(samples) / len(samples):.1f}ms")
                    samples.clear()
                if parts:
                    print("[perf] " + " | ".join(parts))
                last_perf_log = now

        print("[main] Выход по запросу пользователя")
    except KeyboardInterrupt:
        print("\n[main] Прервано (Ctrl+C)")
    except Exception as exc:
        print(f"[main] ОШИБКА: {exc}", file=sys.stderr)
        if worker is not None and worker.poll() is not None:
            print("[main] Воркер упал; последние строки stderr:", file=sys.stderr)
            for line in worker_logs[-40:]:
                print(f"  {line}", file=sys.stderr)
        return 1
    finally:
        # Запись могла идти в момент выхода: без close() moov-атом не
        # допишется и файл останется битым (плееры его не откроют).
        if recorder is not None:
            try:
                recorder.close()
            except Exception as exc:
                print(f"[main] Ошибка закрытия записи: {exc}", file=sys.stderr)
        if worker is not None:
            shutdown_worker(worker, worker_stop)
        if shm is not None:
            shm.close()
        try:
            _save_menu_layout()
        except Exception:
            pass
        if capture is not None:
            try:
                capture.close()
            except Exception as exc:
                print(f"[main] Ошибка закрытия захвата: {exc}", file=sys.stderr)
        if display is not None:
            try:
                display.close()
            except Exception as exc:
                print(f"[main] Ошибка закрытия окна: {exc}", file=sys.stderr)
        try:
            hotkeys.stop()
        except Exception:
            pass
        try:
            tray.stop()
        except Exception:
            pass
        print("[main] Ресурсы освобождены")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        # pythonw: консоли нет — показать причину отказа пользователю окном,
        # детали — в NeuralScreen.log.
        import traceback
        traceback.print_exc()
        try:
            import ctypes as _ct
            _ct.windll.user32.MessageBoxW(
                None,
                f"NeuralScreen не запустился: {exc}\n\nПодробности в NeuralScreen.log рядом с программой.",
                "NeuralScreen", 0x10)  # MB_ICONERROR
        except Exception:
            pass
        sys.exit(1)
