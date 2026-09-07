"""VideoRecorder - writes NR overlay frames into an MP4 (AV1 NVENC).

Records the frames Python receives from the worker (output_rgba) while
recording is on (Insert). Frames arrive full-res RGBA8 every ~30 ms; PyAV
converts them to yuv420p and encodes AV1 through NVENC.

Recording does not depend on ShadowPlay/OBS: the overlay is excluded from
external capture (WDA_EXCLUDEFROMCAPTURE), so the video is written from the
inside - exactly the NR result that is on screen.
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
    """Writes frames into an MP4 (av1_nvenc). Created when recording starts,
    closed on Insert/exit. write()/close() are called from the main loop only.

    Encoding runs in its own thread. A 4K measurement showed a synchronous
    write() cost 19.9 ms per frame - the RGBA->yuv420p conversion and the
    hand-off to nvenc, both on the CPU - and dropped the pipeline from 56 to
    21 FPS. Bitrate had nothing to do with it: the time went into the colour
    conversion, not the encoder.

    The frame is handed to the thread by reference, without a copy: the worker
    sends every frame in a fresh buffer (WorkerReader.recv -> np.frombuffer
    over new bytes) and the main loop never mutates it - it only reads it for
    display and screenshots.
    """

    #: How many frames wait for the encoder. More means more memory (33 MB per
    #: frame at 4K), less means we start dropping frames earlier on spikes.
    QUEUE_DEPTH = 4
    #: How long to wait for room in the queue before dropping a frame. Stalling
    #: the pipeline for the sake of the recording is not acceptable: the user
    #: looks at the screen, not at the file. A dropped frame does not affect
    #: timing - pts comes from the clock.
    PUT_TIMEOUT_S = 0.25

    #: Bitrate and encoder parameters live as class attributes so they can be
    #: changed without touching the constructor (measurements, experiments).
    BIT_RATE = 120_000_000
    ENCODER_OPTIONS = {
        "preset": "p6",     # p1 fast ... p7 high quality
        "tune": "hq",
        "rc": "vbr",        # not a fixed bitrate: on fast motion the encoder
                            # must be allowed to spend more
        "cq": "16",         # target quality; bitrate is a ceiling, not a goal
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
        # MP4 (mov) muxer + nvenc: constant time_base 1/fps, pts is a counter.
        # (The NUT pitfall with time_base != 1/30 does not apply: we write
        # straight into MP4, without an ffmpeg subprocess.)
        self._stream.time_base = Fraction(1, int(round(fps)))
        # Colour metadata is MANDATORY: without it players interpret the frames
        # differently (contrast/colours "drift"). Desktop capture is sRGB FULL
        # range (not limited/BT.709-tv: limited tags over full-range data give
        # "heavy contrast" - the player stretches 16-235 across 0-255).
        # FFmpeg numeric enums: range JPEG/full=2; colorspace BT709=1;
        # primaries BT709=1 (sRGB primaries == BT.709); transfer
        # IEC61966_2_1 (sRGB)=13 (NOT 14 - 14 is BT2020_10, verified with
        # ffprobe: at 14 the file is tagged bt2020-10).
        # PyAV attribute names: color_range/colorspace/color_primaries/
        # color_trc (NOT color_space/color_transfer - those do not exist).
        try:
            self._stream.color_range = 2        # AVCOL_RANGE_JPEG = full
            self._stream.colorspace = 1         # AVCOL_SPC_BT709
            self._stream.color_primaries = 1    # AVCOL_PRI_BT709
            self._stream.color_trc = 13         # AVCOL_TRC_IEC61966_2_1 = sRGB
        except Exception as exc:
            print(f"[record] color metadata failed: {exc}", file=sys.stderr)
        # Bitrate as a CEILING under VBR with a quality target (cq), not as a
        # goal: on fast motion (a shooter) a fixed bitrate forces the encoder to
        # sacrifice quality to hit the number. The GOP is short and has no
        # B-frames: at the pipeline's variable fps B-frames desynchronise the
        # frames, and a long GOP gives artefacts on scene changes.
        try:
            self._stream.bit_rate = self.BIT_RATE
            self._stream.gop_size = max(30, int(round(fps)) * 2)  # keyframe every 2 s
            self._stream.max_b_frames = 0
        except Exception as exc:
            print(f"[record] encoder params failed: {exc}", file=sys.stderr)
        # Encoder options are passed as strings through options - PyAV has no
        # max_bit_rate/rc_buffer_size attributes.
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
        """The sole owner of the container while recording is running."""
        while True:
            item = self._queue.get()
            if item is None:
                return
            pts, rgba = item
            try:
                self._encode_one(pts, rgba)
            except BaseException as exc:   # noqa: BLE001 - report back to main
                self._encode_error = exc
                print(f"[record] encoding aborted: {exc}", file=sys.stderr)
                return

    def write(self, rgba: np.ndarray) -> None:
        """Queue a frame for the encoder (RGBA8 full-res, 4 channels).

        PTS is built from the REAL recording time, not from a frame counter:
        frames arrive at the pipeline's actual fps (~16-32), not exactly 30,
        and the container must reflect the real duration - otherwise the video
        plays back sped up. We compute it HERE, when the frame arrives: inside
        the thread it would reflect the moment of encoding, i.e. it would be
        off by the whole queue depth.
        """
        if rgba.shape[0] != self.height or rgba.shape[1] != self.width:
            # The display mode changed - frames have a different shape. Skipping
            # them silently is not an option: the recording would "quietly"
            # write nothing. The exception stops the recording (main.py:
            # recorder.close() + recorder = None).
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
            # The encoder cannot keep up. Dropping the frame is more honest than
            # holding up the main loop: the user would notice on screen, not in
            # the file.
            self.dropped += 1
            self._frame_idx = pts - 1   # number unused, hand it to the next one

    def _encode_one(self, pts: int, rgba: np.ndarray) -> None:
        """The encoding proper - only from the _encode_loop thread."""
        frame = av.VideoFrame.from_ndarray(rgba, format="rgba")
        # The colour tags are MANDATORY on the frame, not only on the stream:
        # when converting RGBA->yuv420p swscale takes the matrix from the frame,
        # while the player interprets the result by the stream tags. That
        # mismatch (an untagged frame -> swscale default, a tagged stream) is
        # what produces the "contrast".
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
        """Wait for the encoder, write the trailer and close the container.

        The thread is stopped BEFORE we touch the container: it is the sole
        owner while recording runs, and the container must not be touched from
        two threads.
        """
        if self._container is None:
            return
        if self._thread is not None:
            self._queue.put(None)
            self._thread.join(timeout=30.0)
            if self._thread.is_alive():
                print("[record] encoder did not finish within 30 s",
                      file=sys.stderr)
            self._thread = None
        if self.dropped:
            print(f"[record] frames dropped: {self.dropped} "
                  f"(encoder could not keep up)", file=sys.stderr)
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
