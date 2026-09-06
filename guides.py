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

    def __init__(self, width: int, height: int, flow_width: int = 320) -> None:
        self.width = width
        self.height = height
        scale = min(1.0, flow_width / width)
        self.flow_width = max(64, int(round(width * scale / 2) * 2))
        self.flow_height = max(64, int(round(height * scale / 2) * 2))
        self.previous_gray: np.ndarray | None = None
        self._zero_motion = np.zeros((self.height, self.width, 2), dtype=np.float16)
        self.dis = cv2.DISOpticalFlow_create(cv2.DISOPTICAL_FLOW_PRESET_FAST)
        self.dis.setUseSpatialPropagation(True)
        self.dis.setFinestScale(1)

    def _small_gray(self, rgba: np.ndarray) -> np.ndarray:
        gray = cv2.cvtColor(rgba, cv2.COLOR_RGBA2GRAY)
        return cv2.resize(gray, (self.flow_width, self.flow_height), interpolation=cv2.INTER_AREA)

    def process(self, rgba: np.ndarray) -> GuideFrame:
        current = self._small_gray(rgba)
        pixels = self.width * self.height
        if self.previous_gray is None:
            motion = self._zero_motion
            reset = True
            scene_score = 1.0
        else:
            scene_score = float(np.mean(cv2.absdiff(current, self.previous_gray))) / 255.0
            reset = scene_score > 0.24
            if reset or scene_score < 0.001:
                # Reset (scene cut) or static screen (desktop/text): no flow needed.
                # 0.001: above capture noise (~0.0002 @ +-2 LSB) and static 0.0,
                # below real motion: 2px scroll 0.03, 2px shift 0.002, fast cursor 0.0013.
                motion = self._zero_motion
            else:
                # NGX consumes current-to-previous motion in pixel units.
                # Only the forward flow is sent. A backward flow with a
                # forward/backward consistency + residual mask used to be
                # computed here, but the mask was never applied to `motion` —
                # it cost a second DIS pass, two remaps and a dilate per frame
                # and was thrown away. Removed; reinstate it only together
                # with the code that actually masks the motion vectors.
                cur_to_prev = self.dis.calc(current, self.previous_gray, None)
                motion = cv2.resize(cur_to_prev, (self.width, self.height), interpolation=cv2.INTER_LINEAR)
                motion[..., 0] *= self.width / self.flow_width
                motion[..., 1] *= self.height / self.flow_height
        self.previous_gray = current
        assert motion.size == pixels * 2
        if motion.dtype == np.float16:
            out_motion = motion  # cached zero buffer: already float16, contiguous
        else:
            out_motion = np.ascontiguousarray(motion.astype(np.float16))
        return GuideFrame(
            motion=out_motion,
            reset=reset,
            scene_score=scene_score,
        )