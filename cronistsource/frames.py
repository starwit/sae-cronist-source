import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

import av
import numpy as np
from prometheus_client import Counter, Histogram

logger = logging.getLogger(__name__)

DECODE_DURATION = Histogram('cronist_source_decode_duration_seconds', 'The time it takes to decode and colour-convert one frame',
                            buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5))
NON_MONOTONIC_TIMESTAMPS = Counter('cronist_source_non_monotonic_timestamps',
                                   'Frames whose derived timestamp was not greater than the previous one')
FRAMES_SKIPPED = Counter('cronist_source_frames_skipped',
                         'Decoded frames discarded to meet the configured target frame rate')

# Used for frames without a PTS when the container does not state a frame rate either.
FALLBACK_FPS = 25.0


@dataclass
class SourceFrame:
    frame_index: int
    pts_s: float
    timestamp_utc_ms: int
    image_bgr: np.ndarray


def _to_bgr(frame: av.VideoFrame, scale_width: int) -> np.ndarray:
    if scale_width and scale_width != frame.width:
        # Scale and colour-convert in a single swscale pass. Height is kept even for chroma.
        height = round(frame.height * scale_width / frame.width) & ~1
        frame = frame.reformat(width=scale_width, height=height, format='bgr24')
    else:
        frame = frame.reformat(format='bgr24')
    return frame.to_ndarray()


class _FrameRateLimiter:
    '''Picks the frames closest to a target frame rate.

    Selection is driven by presentation time rather than by counting frames, because the ratio is
    usually not an integer (25 -> 10 fps means keeping 2 of every 5) and because a variable-rate
    source has no meaningful frame count to divide. A frame is taken as soon as its timestamp
    reaches the next slot, and the slot advances by a fixed step, so the sampling never drifts even
    over hours of footage.

    Kept frames carry their real presentation time, not the slot time. That leaves the spacing
    slightly uneven (25 -> 10 fps alternates 80 and 120 ms) but keeps every timestamp faithful to
    when the frame was actually captured, which is what downstream speed and count calculations
    depend on.
    '''

    def __init__(self, target_fps: Optional[float]) -> None:
        self._step_ms = 1000.0 / target_fps if target_fps else None
        self._next_slot_ms = None

    def accept(self, timestamp_utc_ms: int) -> bool:
        if self._step_ms is None:
            return True

        if self._next_slot_ms is None:
            self._next_slot_ms = float(timestamp_utc_ms)

        if timestamp_utc_ms < self._next_slot_ms:
            return False

        # Skip any slots the source raced past, so a gap in the source cannot cause a burst here.
        while self._next_slot_ms <= timestamp_utc_ms:
            self._next_slot_ms += self._step_ms
        return True


def iter_frames(path: Path, start_epoch_ms: int, scale_width: int = 0,
                target_fps: Optional[float] = None,
                stop_event: Optional[threading.Event] = None) -> Iterator[SourceFrame]:
    '''Decode a video file, yielding frames with wall-clock timestamps.

    `start_epoch_ms` is the recording time of the first frame. Timestamps are derived from the PTS
    relative to the first decoded frame, so a stream whose PTS does not start at 0 still starts
    exactly at `start_epoch_ms`.

    `target_fps` thins the output down to roughly that many frames per second. Every frame is still
    decoded -- inter-frame compression means a frame cannot be reconstructed without its
    predecessors -- but discarded ones skip colour conversion, which is the expensive part.
    '''
    last_timestamp = -1
    rate_limiter = _FrameRateLimiter(target_fps)

    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        stream.thread_type = 'AUTO'     # let libavcodec spread decoding across cores
        time_base = stream.time_base
        fallback_step_s = 1.0 / float(stream.average_rate or FALLBACK_FPS)
        first_pts_s = None

        decoded = 0
        kept = 0
        for frame_index, frame in enumerate(container.decode(stream)):
            if stop_event is not None and stop_event.is_set():
                return

            decoded = frame_index + 1

            with DECODE_DURATION.time():
                pts_s = float(frame.pts * time_base) if frame.pts is not None else frame_index * fallback_step_s
                if first_pts_s is None:
                    first_pts_s = pts_s

                timestamp = start_epoch_ms + round((pts_s - first_pts_s) * 1000)
                if timestamp <= last_timestamp:
                    # Broken or variable-rate PTS. Nudge forward so the stream stays ordered.
                    NON_MONOTONIC_TIMESTAMPS.inc()
                    timestamp = last_timestamp + 1

                if not rate_limiter.accept(timestamp):
                    FRAMES_SKIPPED.inc()
                    continue

                last_timestamp = timestamp
                image_bgr = _to_bgr(frame, scale_width)

            kept += 1
            yield SourceFrame(frame_index, pts_s, timestamp, image_bgr)

    if kept == decoded:
        logger.info('Decoded %s (%d frames)', path, decoded)
    else:
        logger.info('Decoded %s (%d of %d frames kept for target_fps=%s)', path, kept, decoded, target_fps)
