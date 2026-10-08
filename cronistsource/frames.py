import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

import av
import numpy as np
from av.format import Flags
from prometheus_client import Counter, Histogram

logger = logging.getLogger(__name__)

DECODE_DURATION = Histogram('cronist_source_decode_duration_seconds', 'The time it takes to decode and colour-convert one frame',
                            buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5))
FRAMES_SKIPPED = Counter('cronist_source_frames_skipped',
                         'Decoded frames discarded to meet the configured target frame rate')


class FrameTimingError(ValueError):
    '''The video does not carry enough timing information to derive the recording time of every frame.

    Frame times are never guessed (e.g. from an assumed frame rate): downstream speed and count
    calculations depend on them, and plausible-looking but wrong times would silently corrupt them.
    '''


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


def _check_container_timing(container: av.container.InputContainer) -> av.VideoStream:
    if not container.streams.video:
        raise FrameTimingError('Video contains no video stream')

    if Flags.no_timestamps in Flags(container.format.flags):
        # Raw elementary streams (e.g. .h264, .m4v). ffmpeg would make up timestamps from an assumed
        # frame rate, which is exactly the kind of guess we must not feed downstream.
        raise FrameTimingError(f'Video format "{container.format.name}" carries no frame timestamps; '
                               'use a container format such as MP4, MKV or MPEG-TS')

    stream = container.streams.video[0]
    if stream.time_base is None:
        raise FrameTimingError('Video stream has no time base, cannot derive frame times')
    return stream


def check_frame_timing(path: Path, target_fps: Optional[float] = None) -> int:
    '''Verify, without decoding, that every frame of the video has a usable presentation timestamp.

    Only demuxes, which is cheap compared to decoding, so a broken video is rejected before any of
    its frames are fed downstream. `iter_frames` checks the decoded frames again.

    Returns how many frames `iter_frames` will yield with the same `target_fps`. This is exact for
    well-formed videos (one packet per frame); only a decoder dropping corrupt frames yields fewer.
    '''
    with av.open(str(path)) as container:
        stream = _check_container_timing(container)

        pts_values = []
        for packet in container.demux(stream):
            if packet.size == 0:
                continue    # flush packet at the end of the stream
            if packet.pts is None:
                raise FrameTimingError(f'Video packet {len(pts_values)} has no presentation timestamp (PTS), '
                                       'cannot derive its recording time')
            pts_values.append(packet.pts)

    if not pts_values:
        raise FrameTimingError('Video stream contains no frames')

    # Packets come in decode order, which differs from presentation order with B-frames.
    pts_values.sort()
    for previous, current in zip(pts_values, pts_values[1:]):
        if current == previous:
            raise FrameTimingError(f'Several video frames share the presentation timestamp '
                                   f'{float(current * stream.time_base):.3f}s, cannot derive distinct frame times')

    # Same timestamp derivation and frame selection as iter_frames. The selection only depends on
    # timestamp differences, so the actual start time does not matter here.
    rate_limiter = _FrameRateLimiter(target_fps)
    first_pts_s = float(pts_values[0] * stream.time_base)
    return sum(1 for pts in pts_values
               if rate_limiter.accept(round((float(pts * stream.time_base) - first_pts_s) * 1000)))


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
    exactly at `start_epoch_ms`. Raises FrameTimingError if a frame has no PTS or its PTS does not
    increase; run `check_frame_timing` first to reject such videos before publishing anything.

    `target_fps` thins the output down to roughly that many frames per second. Every frame is still
    decoded -- inter-frame compression means a frame cannot be reconstructed without its
    predecessors -- but discarded ones skip colour conversion, which is the expensive part.
    '''
    rate_limiter = _FrameRateLimiter(target_fps)

    with av.open(str(path)) as container:
        stream = _check_container_timing(container)
        stream.thread_type = 'AUTO'     # let libavcodec spread decoding across cores
        time_base = stream.time_base
        first_pts_s = None
        last_pts_s = None

        decoded = 0
        kept = 0
        for frame_index, frame in enumerate(container.decode(stream)):
            if stop_event is not None and stop_event.is_set():
                return

            decoded = frame_index + 1

            with DECODE_DURATION.time():
                if frame.pts is None:
                    raise FrameTimingError(f'Frame {frame_index} has no presentation timestamp (PTS), '
                                           'cannot derive its recording time')
                pts_s = float(frame.pts * time_base)
                if last_pts_s is not None and pts_s <= last_pts_s:
                    raise FrameTimingError(f'Frame {frame_index} has PTS {pts_s:.3f}s, which is not after the '
                                           f'previous frame ({last_pts_s:.3f}s), cannot derive its recording time')
                if first_pts_s is None:
                    first_pts_s = pts_s
                last_pts_s = pts_s

                timestamp = start_epoch_ms + round((pts_s - first_pts_s) * 1000)
                if not rate_limiter.accept(timestamp):
                    FRAMES_SKIPPED.inc()
                    continue

                image_bgr = _to_bgr(frame, scale_width)

            kept += 1
            yield SourceFrame(frame_index, pts_s, timestamp, image_bgr)

    if kept == decoded:
        logger.info('Decoded %s (%d frames)', path, decoded)
    else:
        logger.info('Decoded %s (%d of %d frames kept for target_fps=%s)', path, kept, decoded, target_fps)
