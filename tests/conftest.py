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

@pytest.fixture(scope='session')
def video_clip(tmp_path_factory) -> Path:
    '''A one-second 25 fps clip, small enough to decode instantly.'''
    return _write_clip(tmp_path_factory.mktemp('video') / 'clip.mp4')

@pytest.fixture(scope='session')
def raw_video_clip(tmp_path_factory) -> Path:
    '''The same clip as a raw MPEG-4 elementary stream, which carries no frame timestamps.'''
    return _write_clip(tmp_path_factory.mktemp('video') / 'clip.m4v', format='m4v')

def _write_clip(path: Path, format: str = None) -> Path:
    with av.open(str(path), 'w', format=format) as container:
        stream = container.add_stream('mpeg4', rate=CLIP_FPS)
        stream.width = CLIP_WIDTH
        stream.height = CLIP_HEIGHT
        stream.pix_fmt = 'yuv420p'
        stream.time_base = Fraction(1, CLIP_FPS)
        for i in range(CLIP_FRAMES):
            image = np.full((CLIP_HEIGHT, CLIP_WIDTH, 3), i * 10 % 256, dtype=np.uint8)
            frame = av.VideoFrame.from_ndarray(image, format='rgb24')
            frame.pts = i
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return path
