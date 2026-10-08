import threading
from fractions import Fraction
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cronistsource.frames import FrameTimingError, check_frame_timing, iter_frames
from tests.conftest import CLIP_FRAMES, CLIP_HEIGHT, CLIP_WIDTH

START_MS = 1_767_225_600_000


def test_all_frames_are_decoded(video_clip):
    frames = list(iter_frames(video_clip, START_MS))

    assert len(frames) == CLIP_FRAMES
    assert frames[0].image_bgr.shape == (CLIP_HEIGHT, CLIP_WIDTH, 3)

def test_timestamps_start_at_start_and_increase(video_clip):
    timestamps = [f.timestamp_utc_ms for f in iter_frames(video_clip, START_MS)]

    assert timestamps[0] == START_MS
    assert all(b > a for a, b in zip(timestamps, timestamps[1:]))
    assert timestamps[1] - timestamps[0] == 40
    assert timestamps[-1] == START_MS + (CLIP_FRAMES - 1) * 40

def test_target_fps_thins_frames(video_clip):
    frames = list(iter_frames(video_clip, START_MS, target_fps=10))

    assert len(frames) == 10
    assert frames[0].timestamp_utc_ms == START_MS

def test_scale_width(video_clip):
    frame = next(iter_frames(video_clip, START_MS, scale_width=32))

    assert frame.image_bgr.shape == (24, 32, 3)

def test_stop_event_ends_iteration(video_clip):
    stop_event = threading.Event()
    frames = []
    for frame in iter_frames(video_clip, START_MS, stop_event=stop_event):
        frames.append(frame)
        if len(frames) == 3:
            stop_event.set()

    assert len(frames) == 3


def test_timing_check_counts_frames(video_clip):
    assert check_frame_timing(video_clip) == CLIP_FRAMES

@pytest.mark.parametrize('target_fps', [10, 7.5, 24, 100])
def test_timing_check_counts_frames_kept_for_target_fps(video_clip, target_fps):
    expected = len(list(iter_frames(video_clip, START_MS, target_fps=target_fps)))

    assert check_frame_timing(video_clip, target_fps=target_fps) == expected

def test_raw_stream_without_timestamps_is_rejected(raw_video_clip):
    with pytest.raises(FrameTimingError, match='carries no frame timestamps'):
        check_frame_timing(raw_video_clip)
    with pytest.raises(FrameTimingError, match='carries no frame timestamps'):
        next(iter_frames(raw_video_clip, START_MS))


def _fake_container(pts_values, packets=True):
    '''A stand-in for av.open(), because real muxers refuse to write missing or duplicate PTS.'''
    container = MagicMock()
    container.__enter__.return_value = container
    container.format.flags = 0
    container.format.name = 'fake'
    stream = container.streams.video[0]
    stream.time_base = Fraction(1, 25)
    container.demux.return_value = [MagicMock(size=100, pts=pts) for pts in pts_values]
    container.decode.return_value = [MagicMock(pts=pts) for pts in pts_values]
    return container

def test_packet_without_pts_is_rejected():
    with patch('cronistsource.frames.av.open', return_value=_fake_container([0, 1, None, 3])):
        with pytest.raises(FrameTimingError, match='packet 2 has no presentation timestamp'):
            check_frame_timing(Path('fake.mp4'))

def test_duplicate_pts_is_rejected():
    # Decode order differs from presentation order with B-frames, so unsorted PTS alone are fine
    with patch('cronistsource.frames.av.open', return_value=_fake_container([0, 2, 1, 3])):
        check_frame_timing(Path('fake.mp4'))
    with patch('cronistsource.frames.av.open', return_value=_fake_container([0, 2, 1, 2])):
        with pytest.raises(FrameTimingError, match='share the presentation timestamp 0.080s'):
            check_frame_timing(Path('fake.mp4'))

def test_decoded_frame_without_pts_is_rejected():
    with patch('cronistsource.frames.av.open', return_value=_fake_container([0, None])), \
            patch('cronistsource.frames._to_bgr'):
        with pytest.raises(FrameTimingError, match='Frame 1 has no presentation timestamp'):
            list(iter_frames(Path('fake.mp4'), START_MS))

def test_decoded_frames_with_non_increasing_pts_are_rejected():
    with patch('cronistsource.frames.av.open', return_value=_fake_container([0, 1, 1])), \
            patch('cronistsource.frames._to_bgr'):
        with pytest.raises(FrameTimingError, match='Frame 2 has PTS 0.040s, which is not after'):
            list(iter_frames(Path('fake.mp4'), START_MS))
