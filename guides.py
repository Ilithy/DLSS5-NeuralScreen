from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass(slots=True)
class GuideFrame:
    motion: np.ndarray
    reset: bool
    scene_score: float


class TemporalGuideGenerator:
    """Estimate the guide buffers an encoded video does not contain."""

    def __init__(self, width: int, height: int, flow_width: int = 320,
                 emit_small: bool = False) -> None:
        """width/height — work-разрешение NGX.

        emit_small=True: отдавать поле движения в разрешении оптического
        потока (~320x180) вместо work-разрешения. Растяжение тогда делает
        воркер на GPU, а с CPU снимается ~8 мс на кадр — resize и конвертация
        6 миллионов значений. Векторы в обоих режимах в одних и тех же
        единицах (пиксели work-разрешения), меняется только сетка.
        """
        self.width = width
        self.height = height
        scale = min(1.0, flow_width / width)
        self.flow_width = max(64, int(round(width * scale / 2) * 2))
        self.flow_height = max(64, int(round(height * scale / 2) * 2))
        self.emit_small = emit_small
        self.previous_gray: np.ndarray | None = None
        self._zero_motion = np.zeros((self.height, self.width, 2), dtype=np.float16)
        self._zero_small = np.zeros((self.flow_height, self.flow_width, 2), dtype=np.float16)
        self._flow_f16 = np.empty((self.flow_height, self.flow_width, 2), dtype=np.float16)
        # Буферы под растяжение поля движения. Каждый кадр это 3 М пикселей на
        # 2 канала: без переиспользования уходило ~11.6 мс на аллокацию,
        # умножение уже растянутого поля и astype (замерено, _work/bench_guides.py).
        self._flow_scaled = np.empty((self.flow_height, self.flow_width, 2), dtype=np.float32)
        self._motion_f32 = np.empty((self.height, self.width, 2), dtype=np.float32)
        self._motion_f16 = np.empty((self.height, self.width, 2), dtype=np.float16)
        self.dis = cv2.DISOpticalFlow_create(cv2.DISOPTICAL_FLOW_PRESET_FAST)
        self.dis.setUseSpatialPropagation(True)
        self.dis.setFinestScale(1)

    @property
    def motion_width(self) -> int:
        """Ширина поля движения, которое отдаёт process()."""
        return self.flow_width if self.emit_small else self.width

    @property
    def motion_height(self) -> int:
        return self.flow_height if self.emit_small else self.height

    def _small_gray(self, rgba: np.ndarray) -> np.ndarray:
        gray = cv2.cvtColor(rgba, cv2.COLOR_RGBA2GRAY)
        return cv2.resize(gray, (self.flow_width, self.flow_height), interpolation=cv2.INTER_AREA)

    def zero_guide(self) -> GuideFrame:
        """Фолбэк при устойчивом сбое process(): нулевой motion, reset=True.

        Кадр продолжает идти в воркер (картинка не замирает), NGX получает
        нулевое поле движения вместо свежего.
        """
        motion = self._zero_small if self.emit_small else self._zero_motion
        return GuideFrame(motion=motion, reset=True, scene_score=1.0)

    def process(self, rgba: np.ndarray | None = None,
                gray: np.ndarray | None = None) -> GuideFrame:
        """Посчитать guides: motion/reset/scene_score.

        Либо rgba (BGR/RGBA full-res — сам даунсэмплит), либо готовый gray
        (flow-размер, uint8 2D) — приходит из обратного канала воркера
        (GRAY/DDA). Приоритет: gray (уже правильного размера).
        """
        if gray is not None:
            current = gray.reshape(self.flow_height, self.flow_width).astype(np.uint8)
            if current.shape != (self.flow_height, self.flow_width):
                raise ValueError(
                    f"gray {current.shape} != ожидаемый {(self.flow_height, self.flow_width)}")
        else:
            current = self._small_gray(rgba)
        pixels = self.width * self.height
        if self.previous_gray is None:
            motion = self._zero_small if self.emit_small else self._zero_motion
            reset = True
            scene_score = 1.0
        else:
            scene_score = float(np.mean(cv2.absdiff(current, self.previous_gray))) / 255.0
            reset = scene_score > 0.24
            if reset or scene_score < 0.001:
                # Reset (scene cut) or static screen (desktop/text): no flow needed.
                # 0.001: above capture noise (~0.0002 @ +-2 LSB) and static 0.0,
                # below real motion: 2px scroll 0.03, 2px shift 0.002, fast cursor 0.0013.
                motion = self._zero_small if self.emit_small else self._zero_motion
            else:
                # NGX consumes current-to-previous motion in pixel units.
                # Only the forward flow is sent. A backward flow with a
                # forward/backward consistency + residual mask used to be
                # computed here, but the mask was never applied to `motion` —
                # it cost a second DIS pass, two remaps and a dilate per frame
                # and was thrown away. Removed; reinstate it only together
                # with the code that actually masks the motion vectors.
                cur_to_prev = self.dis.calc(current, self.previous_gray, None)
                # Scale BEFORE the upscale: 115k elements instead of 3M, and
                # exactly equivalent because resize is linear (verified: the
                # two orders differ by 0.002, i.e. float16 rounding).
                np.multiply(cur_to_prev[..., 0], self.width / self.flow_width,
                            out=self._flow_scaled[..., 0])
                np.multiply(cur_to_prev[..., 1], self.height / self.flow_height,
                            out=self._flow_scaled[..., 1])
                if self.emit_small:
                    # Растянет воркер на GPU — тут остаётся только перевод
                    # 115 тысяч значений во float16.
                    np.copyto(self._flow_f16, self._flow_scaled, casting="same_kind")
                    motion = self._flow_f16
                else:
                    cv2.resize(self._flow_scaled, (self.width, self.height),
                               dst=self._motion_f32, interpolation=cv2.INTER_LINEAR)
                    np.copyto(self._motion_f16, self._motion_f32, casting="same_kind")
                    motion = self._motion_f16
        self.previous_gray = current
        expected = (self.flow_width * self.flow_height if self.emit_small else pixels)
        assert motion.size == expected * 2
        assert motion.dtype == np.float16 and motion.flags["C_CONTIGUOUS"]
        # ИНВАРИАНТ: motion — переиспользуемый буфер (либо _motion_f16, либо
        # кэш нулей). Следующий вызов process() его перезапишет, поэтому
        # потребитель обязан скопировать данные до этого. Цикл main так и
        # делает: send_frame копирует кадр в общую память сразу же.
        return GuideFrame(
            motion=motion,
            reset=reset,
            scene_score=scene_score,
        )