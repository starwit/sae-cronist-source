import threading
from fractions import Fraction
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from prometheus_client import REGISTRY

from cronistsource.frames import FrameTimingError, check_frame_timing, iter_frames, plan_range
from tests.conftest import CLIP_FRAMES, CLIP_HEIGHT, CLIP_WIDTH, LONG_CLIP_FRAMES

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
    container.demux.side_effect = lambda *_: iter([MagicMock(size=100, pts=pts, dts=None) for pts in pts_values])
    container.decode.side_effect = lambda *_: iter([MagicMock(pts=pts) for pts in pts_values])
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


# --- Ranges ---

def _decoded_count():
    return REGISTRY.get_sample_value('cronist_source_frames_decoded_total')

def _play_range(path, offset_us, duration_us, target_fps=None):
    plan = plan_range(path, offset_us, duration_us, target_fps)
    return plan, list(iter_frames(path, START_MS, target_fps=target_fps, plan=plan))

def test_range_in_the_middle(long_clip):
    plan, frames = _play_range(long_clip, 6_500_000, 1_000_000)

    relative_ms = [f.timestamp_utc_ms - START_MS for f in frames]
    # 25 fps: frames every 40 ms, so 6.52 s is the first frame at or after 6.5 s
    assert relative_ms == list(range(6520, 7500, 40))
    assert plan.total_frames == len(frames) == 25

def test_range_frames_have_the_same_timestamps_as_in_full_playback(long_clip):
    full = {f.pts_s: f.timestamp_utc_ms for f in iter_frames(long_clip, START_MS)}

    for offset_us, duration_us in [(0, 2_000_000), (400_000, 300_000), (3_210_000, 2_000_000), (6_500_000, None)]:
        _, frames = _play_range(long_clip, offset_us, duration_us)
        assert frames
        assert all(full[f.pts_s] == f.timestamp_utc_ms for f in frames), (offset_us, duration_us)

def test_range_boundaries_are_inclusive_start_exclusive_end(long_clip):
    _, frames = _play_range(long_clip, 2_000_000, 1_000_000)

    assert frames[0].timestamp_utc_ms == START_MS + 2000
    assert frames[-1].timestamp_utc_ms == START_MS + 2960
    assert len(frames) == 25

def test_range_running_past_the_end_plays_to_the_end(long_clip):
    plan, frames = _play_range(long_clip, 9_000_000, 5_000_000)

    assert frames[-1].timestamp_utc_ms == START_MS + (LONG_CLIP_FRAMES - 1) * 40
    assert plan.total_frames == len(frames) == 25

def test_range_without_duration_plays_to_the_end(long_clip):
    plan, frames = _play_range(long_clip, 8_000_000, None)

    assert plan.total_frames == len(frames) == 50

def test_range_without_frames_is_rejected(long_clip):
    with pytest.raises(FrameTimingError, match=r'no frames in the range 10.000s..11.000s \(the last frame is at 9.960s\)'):
        plan_range(long_clip, 10_000_000, 1_000_000)

@pytest.mark.parametrize('offset_us, duration_us, message', [
    (-1, None, 'playbackOffsetMicros must not be negative'),
    (0, 0, 'playbackDurationMicros must be positive'),
    (0, -5, 'playbackDurationMicros must be positive'),
])
def test_invalid_range_is_rejected(video_clip, offset_us, duration_us, message):
    with pytest.raises(ValueError, match=message):
        plan_range(video_clip, offset_us, duration_us)

@pytest.mark.parametrize('target_fps', [10, 7.5, 1])
def test_target_fps_sampling_starts_at_the_range(long_clip, target_fps):
    plan, frames = _play_range(long_clip, 3_210_000, 2_000_000, target_fps=target_fps)

    assert frames[0].timestamp_utc_ms == START_MS + 3240
    assert plan.total_frames == len(frames)
    assert len(frames) == pytest.approx(2 * target_fps, abs=1)

def test_range_from_zero_matches_full_playback(long_clip):
    full = [f.timestamp_utc_ms for f in iter_frames(long_clip, START_MS)]

    plan, frames = _play_range(long_clip, 0, None)

    assert plan.seek_pts is None
    assert [f.timestamp_utc_ms for f in frames] == full

def test_frames_before_the_seek_point_are_not_decoded(long_clip):
    before = _decoded_count()

    _, frames = _play_range(long_clip, 8_000_000, 1_000_000)

    decoded = _decoded_count() - before
    assert len(frames) == 25
    # At most the GOP before the range (up to 1 s at 25 fps, plus B-frame/thread latency), not the 200 frames before it
    assert decoded < 25 + 25 + 25

def test_range_with_missing_pts_is_rejected():
    with patch('cronistsource.frames.av.open', return_value=_fake_container([0, 1, None, 3])):
        with pytest.raises(FrameTimingError, match='has no presentation timestamp'):
            plan_range(Path('fake.mp4'), 0, None)

def test_range_with_duplicate_pts_is_rejected():
    with patch('cronistsource.frames.av.open', return_value=_fake_container([0, 2, 1, 2])):
        with pytest.raises(FrameTimingError, match='share the presentation timestamp 0.080s'):
            plan_range(Path('fake.mp4'), 0, None)
