from fractions import Fraction
from pathlib import Path

import av
import numpy as np
import pytest

# This is necessary to prevent tests from accidentally loading real config files
@pytest.fixture(autouse=True)
def set_settings_file_location(monkeypatch):
    monkeypatch.setenv('SETTINGS_FILE', '/tmp/should_not_exist.yaml')
    # Same for env vars, which pydantic-settings would otherwise pick up from the shell
    monkeypatch.delenv('LOG_LEVEL', raising=False)

CLIP_FRAMES = 25
CLIP_FPS = 25
CLIP_WIDTH = 64
CLIP_HEIGHT = 48

LONG_CLIP_FRAMES = 250  # 10 s

@pytest.fixture(scope='session')
def video_clip(tmp_path_factory) -> Path:
    '''A one-second 25 fps clip, small enough to decode instantly.'''
    return _write_clip(tmp_path_factory.mktemp('video') / 'clip.mp4')

@pytest.fixture(scope='session')
def raw_video_clip(tmp_path_factory) -> Path:
    '''The same clip as a raw MPEG-4 elementary stream, which carries no frame timestamps.'''
    return _write_clip(tmp_path_factory.mktemp('video') / 'clip.m4v', format='m4v')

LONG_CLIPS = {
    'mp4': dict(suffix='mp4'),
    'mp4-bframes': dict(suffix='mp4', bframes=2),
    'fragmented-mp4': dict(suffix='mp4', bframes=2, options={'movflags': 'frag_keyframe+empty_moov'}),
    'mkv-bframes': dict(suffix='mkv', bframes=2),
    'mpegts': dict(suffix='ts'),
    'mpegts-bframes': dict(suffix='ts', bframes=2),
}

@pytest.fixture(scope='session')
def long_clips(tmp_path_factory) -> dict:
    '''10 s, 25 fps clips with a keyframe every second, in each container format accepted for playback.'''
    directory = tmp_path_factory.mktemp('long')
    return {name: _write_clip(directory / f'{name}.{spec["suffix"]}', frames=LONG_CLIP_FRAMES, gop=CLIP_FPS,
                              bframes=spec.get('bframes', 0), options=spec.get('options'))
            for name, spec in LONG_CLIPS.items()}

@pytest.fixture(params=list(LONG_CLIPS))
def long_clip(request, long_clips) -> Path:
    return long_clips[request.param]

def _write_clip(path: Path, format: str = None, frames: int = CLIP_FRAMES, gop: int = None,
                bframes: int = 0, options: dict = None) -> Path:
    with av.open(str(path), 'w', format=format, options=options or {}) as container:
        stream = container.add_stream('mpeg4', rate=CLIP_FPS)
        stream.width = CLIP_WIDTH
        stream.height = CLIP_HEIGHT
        stream.pix_fmt = 'yuv420p'
        stream.time_base = Fraction(1, CLIP_FPS)
        if gop:
            stream.codec_context.gop_size = gop
        if bframes:
            stream.codec_context.max_b_frames = bframes
        for i in range(frames):
            image = np.full((CLIP_HEIGHT, CLIP_WIDTH, 3), i * 10 % 256, dtype=np.uint8)
            frame = av.VideoFrame.from_ndarray(image, format='rgb24')
            frame.pts = i
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return path
