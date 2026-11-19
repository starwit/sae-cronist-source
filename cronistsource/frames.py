import logging
import threading
from contextlib import ExitStack
from dataclasses import dataclass
from fractions import Fraction
from itertools import chain
from pathlib import Path
from typing import Iterator, Optional

import av
import numpy as np
from av.format import Flags
from prometheus_client import Counter, Histogram

logger = logging.getLogger(__name__)

DECODE_DURATION = Histogram('cronist_source_decode_duration_seconds', 'The time it takes to decode and colour-convert one frame',
                            buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5))
FRAMES_DECODED = Counter('cronist_source_frames_decoded', 'How many frames have been decoded, including discarded ones')
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
                stop_event: Optional[threading.Event] = None,
                plan: Optional['RangePlan'] = None) -> Iterator[SourceFrame]:
    '''Decode a video file, yielding frames with wall-clock timestamps.

    `start_epoch_ms` is the recording time of the first frame. Timestamps are derived from the PTS
    relative to the first decoded frame, so a stream whose PTS does not start at 0 still starts
    exactly at `start_epoch_ms`. Raises FrameTimingError if a frame has no PTS or its PTS does not
    increase; run `check_frame_timing` (or `plan_range`) first to reject such videos before
    publishing anything.

    `target_fps` thins the output down to roughly that many frames per second. Every frame is still
    decoded -- inter-frame compression means a frame cannot be reconstructed without its
    predecessors -- but discarded ones skip colour conversion, which is the expensive part.

    With a `plan` (from `plan_range`) only the frames of that range are yielded: decoding starts at
    the keyframe found by `plan_range`, and frames before the range are dropped without colour
    conversion. Timestamps stay relative to the first frame of the whole video.
    '''
    rate_limiter = _FrameRateLimiter(target_fps)

    with av.open(str(path)) as container:
        stream = _check_container_timing(container)
        stream.thread_type = 'AUTO'     # let libavcodec spread decoding across cores
        time_base = stream.time_base
        first_pts_s = plan.first_pts_s if plan is not None else None
        last_pts_s = None

        decoded = 0
        in_range = 0
        kept = 0
        for frame_index, frame in enumerate(_decode(container, stream, plan)):
            if stop_event is not None and stop_event.is_set():
                return

            decoded = frame_index + 1
            FRAMES_DECODED.inc()

            with DECODE_DURATION.time():
                if frame.pts is None:
                    raise FrameTimingError(f'Frame {frame_index} has no presentation timestamp (PTS), '
                                           'cannot derive its recording time')
                pts_s = float(frame.pts * time_base)
                if last_pts_s is not None and pts_s <= last_pts_s:
                    raise FrameTimingError(f'Frame {frame_index} has PTS {pts_s:.3f}s, which is not after the '
                                           f'previous frame ({last_pts_s:.3f}s), cannot derive its recording time')
                last_pts_s = pts_s

                if plan is not None:
                    if frame.pts < plan.start_pts:
                        continue    # between the keyframe and the start of the range
                    if plan.end_pts is not None and frame.pts >= plan.end_pts:
                        break
                in_range += 1

                if first_pts_s is None:
                    first_pts_s = pts_s

                # The rate limiter gets the same relative times as in check_frame_timing/plan_range,
                # so its float arithmetic, and therefore the frame count, matches exactly.
                relative_ms = round((pts_s - first_pts_s) * 1000)
                if not rate_limiter.accept(relative_ms):
                    FRAMES_SKIPPED.inc()
                    continue

                image_bgr = _to_bgr(frame, scale_width)

            kept += 1
            yield SourceFrame(frame_index, pts_s, start_epoch_ms + relative_ms, image_bgr)

    if plan is not None:
        logger.info('Decoded %s: %d frames decoded, %d in range, %d kept (target_fps=%s)',
                    plan.describe(), decoded, in_range, kept, target_fps)
    elif kept == decoded:
        logger.info('Decoded %s (%d frames)', path, decoded)
    else:
        logger.info('Decoded %s (%d of %d frames kept for target_fps=%s)', path, kept, decoded, target_fps)


def _decode(container: av.container.InputContainer, stream: av.VideoStream,
            plan: Optional['RangePlan']) -> Iterator[av.VideoFrame]:
    if plan is None or plan.seek_pts is None:
        yield from container.decode(stream)
        return

    container.seek(plan.seek_pts, stream=stream)    # also flushes the decoder
    packets = container.demux(stream)
    keyframe = _next_keyframe(packets)
    if keyframe is None or keyframe.pts != plan.keyframe_pts:
        found = 'no keyframe' if keyframe is None else f'the keyframe at PTS {keyframe.pts}'
        raise FrameTimingError(f'Seeking for {plan.describe()} found {found} instead of the one at PTS '
                               f'{plan.keyframe_pts} found while checking the video')
    yield from keyframe.decode()
    for packet in packets:
        yield from packet.decode()


def _next_keyframe(packets: Iterator[av.Packet]) -> Optional[av.Packet]:
    '''Skip to the first keyframe: after a seek, the demuxer may land on frames that cannot be decoded.'''
    for packet in packets:
        if packet.size and packet.is_keyframe:
            return packet
    return None


@dataclass(frozen=True)
class RangePlan:
    '''Where and how to decode a range of a video, worked out by `plan_range`.

    PTS values are in the video stream's time base. `start_pts`/`end_pts` are exact (Fractions).
    '''
    offset_us: int
    duration_us: Optional[int]
    first_pts: int
    first_pts_s: float
    # Where to seek before decoding, or None to decode from the start of the video
    seek_pts: Optional[int]
    # The keyframe decoding starts at after seeking to seek_pts
    keyframe_pts: Optional[int]
    start_pts: Fraction
    end_pts: Optional[Fraction]
    total_frames: int

    def describe(self) -> str:
        return describe_range(self.offset_us, self.duration_us)


def describe_range(offset_us: Optional[int], duration_us: Optional[int]) -> str:
    start = f'{(offset_us or 0) / 1e6:.3f}s'
    end = 'end' if duration_us is None else f'{((offset_us or 0) + duration_us) / 1e6:.3f}s'
    return f'range {start}..{end}'


def plan_range(path: Path, offset_us: int, duration_us: Optional[int],
               target_fps: Optional[float] = None) -> RangePlan:
    '''Check the timing of a range of a video and work out how to decode it, without decoding it.

    The range covers the frames with `offset <= pts - first_pts < offset + duration`, where
    `first_pts` is the PTS of the first frame of the whole video, i.e. the same reference that
    `iter_frames` uses for full playback. `duration_us` None means until the end of the video.

    Seeks to the last keyframe at or before the range and only demuxes from there to the end of the
    range, so the rest of the video is neither read nor decoded. Like `check_frame_timing`, raises
    FrameTimingError if any frame from the keyframe up to the end of the range has no PTS, or if PTS
    values repeat, and if the range contains no frame at all.
    '''
    if offset_us < 0:
        raise ValueError(f'playbackOffsetMicros must not be negative, got {offset_us}')
    if duration_us is not None and duration_us <= 0:
        raise ValueError(f'playbackDurationMicros must be positive, got {duration_us}')

    with av.open(str(path)) as container:
        first_pts = _first_frame_pts(container, _check_container_timing(container))

    with ExitStack() as containers:
        # Reopened rather than rewound: seeking back to the very start is not exact for every format
        container = containers.enter_context(av.open(str(path)))
        stream = _check_container_timing(container)
        time_base = stream.time_base
        start_pts = first_pts + Fraction(offset_us, 1_000_000) / time_base
        end_pts = start_pts + Fraction(duration_us, 1_000_000) / time_base if duration_us is not None else None

        seek_pts, keyframe, packets = _seek_to_keyframe_before(container, stream, first_pts, start_pts)
        if seek_pts is None:
            # The range starts in the first GOP: demux from the start, like full playback
            container = containers.enter_context(av.open(str(path)))
            packets = container.demux(_check_container_timing(container))

        pts_values = []
        last_pts = None
        for packet in packets if keyframe is None else chain([keyframe], packets):
            if packet.size == 0:
                continue    # flush packet at the end of the stream
            # DTS only increase and are never after the PTS, so no later packet can be in the range
            if end_pts is not None and packet.dts is not None and packet.dts >= end_pts:
                break
            if packet.pts is None:
                raise FrameTimingError(f'A video packet after {float((last_pts or first_pts) * time_base):.3f}s '
                                       'has no presentation timestamp (PTS), cannot derive its recording time')
            last_pts = packet.pts if last_pts is None else max(last_pts, packet.pts)
            if packet.pts >= start_pts and (end_pts is None or packet.pts < end_pts):
                pts_values.append(packet.pts)

    description = describe_range(offset_us, duration_us)
    if not pts_values:
        video_end = '' if last_pts is None else f' (the last frame is at {float((last_pts - first_pts) * time_base):.3f}s)'
        raise FrameTimingError(f'The video contains no frames in the {description}{video_end}')

    pts_values.sort()
    for previous, current in zip(pts_values, pts_values[1:]):
        if current == previous:
            raise FrameTimingError(f'Several video frames share the presentation timestamp '
                                   f'{float(current * time_base):.3f}s, cannot derive distinct frame times')

    # Same timestamp derivation and frame selection as iter_frames: sampling starts at the first
    # frame of the range.
    rate_limiter = _FrameRateLimiter(target_fps)
    first_pts_s = float(first_pts * time_base)
    total_frames = sum(1 for pts in pts_values
                       if rate_limiter.accept(round((float(pts * time_base) - first_pts_s) * 1000)))

    return RangePlan(offset_us=offset_us, duration_us=duration_us, first_pts=first_pts, first_pts_s=first_pts_s,
                     seek_pts=seek_pts, keyframe_pts=keyframe.pts if keyframe is not None else None,
                     start_pts=start_pts, end_pts=end_pts, total_frames=total_frames)


def _first_frame_pts(container: av.container.InputContainer, stream: av.VideoStream) -> int:
    '''PTS of the first frame the decoder outputs, exactly what full playback uses as its reference.

    Decoded rather than taken from `stream.start_time` or the first packet: with B-frames or a
    stream that starts mid-GOP, those can differ from the first frame that is actually shown.
    '''
    for frame in container.decode(stream):
        if frame.pts is None:
            raise FrameTimingError('Frame 0 has no presentation timestamp (PTS), cannot derive its recording time')
        return frame.pts
    raise FrameTimingError('Video stream contains no frames')


def _seek_to_keyframe_before(container: av.container.InputContainer, stream: av.VideoStream,
                             first_pts: int, start_pts: Fraction) -> tuple:
    '''Seek to the last keyframe at or before `start_pts`.

    Returns (seek_pts, keyframe packet, iterator over the packets after it), or (None, None, None) if
    decoding has to start at the beginning of the video. The container cannot be used for that
    then, because a seek to the very start is not exact for every format: open a new one.

    Seeking is not exact everywhere: MP4 seeks by DTS (a keyframe's PTS may be later with B-frames),
    MPEG-TS seeks by byte position and may land after the target or on a non-keyframe. So the
    keyframe found is checked, and the seek is repeated further back until it is not after the start.
    '''
    target = int(start_pts)     # floor; PTS are integers
    back_off = max(1, round(1 / stream.time_base))   # one second
    while target > first_pts:
        container.seek(target, stream=stream)
        packets = container.demux(stream)
        keyframe = _next_keyframe(packets)
        if keyframe is not None:
            if keyframe.pts is None:
                raise FrameTimingError(f'The keyframe found when seeking to {float(target * stream.time_base):.3f}s '
                                       'has no presentation timestamp (PTS)')
            if keyframe.pts <= start_pts:
                return target, keyframe, packets
        logger.debug('Seeking to PTS %d found keyframe %s after the range start %s, seeking further back',
                     target, keyframe.pts if keyframe is not None else None, start_pts)
        target -= back_off
        back_off *= 2
    return None, None, None
