import threading

from tests.conftest import CLIP_FRAMES, CLIP_HEIGHT, CLIP_WIDTH

from cronistsource.frames import iter_frames

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
