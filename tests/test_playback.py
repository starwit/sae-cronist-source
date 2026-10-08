from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
import requests
from visionapi.sae_pb2 import SaeMessage

from cronistsource.config import CronistSourceConfig, SourceConfig
from cronistsource.cronistclient import PlaybackStatus, SaeDesiredState
from cronistsource.playback import PlaybackTask
from tests.conftest import CLIP_FRAMES

VIDEO_START = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)


@pytest.fixture
def config(tmp_path):
    return CronistSourceConfig(sae_id='sae-1', stream_id='stream1', source=SourceConfig(download_dir=tmp_path))

@pytest.fixture
def publish():
    with patch('cronistsource.playback.ValkeyPublisher') as mock_publisher:
        yield mock_publisher.return_value.__enter__.return_value

@pytest.fixture
def reset_stream():
    with patch('cronistsource.playback._reset_stream') as mock_reset:
        yield mock_reset

@pytest.fixture
def download():
    with patch('cronistsource.playback.requests.get') as mock_get:
        def _serve(content: bytes = None, status_code: int = 200):
            response = mock_get.return_value.__enter__.return_value
            if status_code >= 400:
                response.raise_for_status.side_effect = requests.HTTPError(f'{status_code} Client Error')
            response.iter_content.return_value = iter([content[:1000], content[1000:]])
        _serve.mock = mock_get
        yield _serve

def _desired(**overrides):
    fields = dict(sae_id='sae-1', generation=1, task_id='t1', video_id='v1',
                  video_url='http://s3/video.mp4', video_start=VIDEO_START)
    fields.update(overrides)
    return SaeDesiredState(**fields)

def _published_messages(publish):
    messages = []
    for call in publish.call_args_list:
        msg = SaeMessage()
        msg.ParseFromString(call.args[1])
        messages.append(msg)
    return messages


def test_plays_whole_video(config, publish, reset_stream, download, video_clip, tmp_path):
    download(video_clip.read_bytes())
    task = PlaybackTask(_desired(), config)

    task.run()

    assert task.status == PlaybackStatus.FINISHED
    assert task.processed_frames == CLIP_FRAMES
    assert task.total_frames == CLIP_FRAMES
    assert task.message is None
    assert download.mock.call_args.args == ('http://s3/video.mp4',)
    reset_stream.assert_called_once_with(config.redis, 'cronistsource:stream1')
    assert publish.call_count == CLIP_FRAMES
    assert all(call.args[0] == 'cronistsource:stream1' for call in publish.call_args_list)
    assert list(tmp_path.iterdir()) == []

def test_first_frame_timestamp_is_video_start(config, publish, reset_stream, download, video_clip):
    download(video_clip.read_bytes())

    PlaybackTask(_desired(), config).run()

    messages = _published_messages(publish)
    assert messages[0].frame.timestamp_utc_ms == round(VIDEO_START.timestamp() * 1000)
    assert messages[0].frame.source_id == 'stream1'
    assert messages[0].frame.frame_data_jpeg

def test_missing_video_start_fails_without_download(config, publish, download):
    task = PlaybackTask(_desired(video_start=None), config)

    task.run()

    assert task.status == PlaybackStatus.FAILED
    assert task.message == 'videoStart missing'
    download.mock.assert_not_called()

def test_http_error_fails(config, publish, download, tmp_path):
    download(b'', status_code=403)
    task = PlaybackTask(_desired(), config)

    task.run()

    assert task.status == PlaybackStatus.FAILED
    assert '403' in task.message
    publish.assert_not_called()
    assert list(tmp_path.iterdir()) == []

def test_video_without_frame_times_fails_before_publishing(config, publish, reset_stream, download, raw_video_clip, tmp_path):
    download(raw_video_clip.read_bytes())
    task = PlaybackTask(_desired(), config)

    task.run()

    assert task.status == PlaybackStatus.FAILED
    assert 'carries no frame timestamps' in task.message
    assert task.total_frames is None
    publish.assert_not_called()
    reset_stream.assert_not_called()
    assert list(tmp_path.iterdir()) == []

def test_corrupt_video_fails(config, publish, reset_stream, download, tmp_path):
    download(b'not a video' * 200)
    task = PlaybackTask(_desired(), config)

    task.run()

    assert task.status == PlaybackStatus.FAILED
    assert task.message
    assert task.processed_frames == 0
    assert list(tmp_path.iterdir()) == []

def test_stop_ends_playback(config, publish, reset_stream, download, video_clip, tmp_path):
    download(video_clip.read_bytes())
    task = PlaybackTask(_desired(), config)
    publish.side_effect = lambda *_: task.stop() if publish.call_count == 3 else None

    task.start()
    task.join(timeout=10)

    assert not task.is_alive()
    assert task.processed_frames == 3
    assert task.status == PlaybackStatus.PLAYING
    assert list(tmp_path.iterdir()) == []
