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

def test_download_progress_is_logged(config, publish, reset_stream, download, video_clip, caplog):
    content = video_clip.read_bytes()
    download(content)
    download.mock.return_value.__enter__.return_value.headers = {'Content-Length': str(len(content))}

    with patch('cronistsource.playback.DOWNLOAD_PROGRESS_INTERVAL_S', 0), caplog.at_level('INFO', 'cronistsource'):
        PlaybackTask(_desired(), config).run()

    progress = [r.getMessage() for r in caplog.records if r.getMessage().startswith('Downloading video v1:')]
    assert len(progress) == 2
    assert '(100%)' in progress[-1]

def test_download_progress_without_content_length(config, publish, reset_stream, download, video_clip, caplog):
    download(video_clip.read_bytes())
    download.mock.return_value.__enter__.return_value.headers = {}

    with patch('cronistsource.playback.DOWNLOAD_PROGRESS_INTERVAL_S', 0), caplog.at_level('INFO', 'cronistsource'):
        task = PlaybackTask(_desired(), config)
        task.run()

    assert task.status == PlaybackStatus.FINISHED
    assert any(r.getMessage().startswith('Downloading video v1:') and '%' not in r.getMessage()
               for r in caplog.records)

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



# --- Ranges ---

def _relative_ms(messages):
    start_ms = round(VIDEO_START.timestamp() * 1000)
    return [m.frame.timestamp_utc_ms - start_ms for m in messages]

def test_plays_range(config, publish, reset_stream, download, long_clips, tmp_path):
    download(long_clips['mp4-bframes'].read_bytes())
    task = PlaybackTask(_desired(playback_offset_micros=6_500_000, playback_duration_micros=1_000_000), config)

    task.run()

    assert task.status == PlaybackStatus.FINISHED, task.message
    assert task.total_frames == task.processed_frames == publish.call_count == 25
    assert _relative_ms(_published_messages(publish)) == list(range(6520, 7500, 40))
    assert list(tmp_path.iterdir()) == []

def test_range_keeps_timestamps_of_full_playback(config, publish, reset_stream, download, long_clips):
    content = long_clips['mpegts-bframes'].read_bytes()
    download(content)
    PlaybackTask(_desired(), config).run()
    full = _relative_ms(_published_messages(publish))
    publish.reset_mock()

    download(content)
    task = PlaybackTask(_desired(playback_offset_micros=3_000_000, playback_duration_micros=2_000_000), config)
    task.run()

    assert task.status == PlaybackStatus.FINISHED, task.message
    assert _relative_ms(_published_messages(publish)) == [t for t in full if 3000 <= t < 5000]

def test_range_with_target_fps(config, publish, reset_stream, download, long_clips):
    config.source.target_fps = 10
    download(long_clips['mkv-bframes'].read_bytes())
    task = PlaybackTask(_desired(playback_offset_micros=2_000_000, playback_duration_micros=3_000_000), config)

    task.run()

    assert task.status == PlaybackStatus.FINISHED, task.message
    assert task.total_frames == task.processed_frames == publish.call_count == 30
    assert _relative_ms(_published_messages(publish))[0] == 2000

def test_range_past_the_end_finishes(config, publish, reset_stream, download, long_clips):
    download(long_clips['mp4'].read_bytes())
    task = PlaybackTask(_desired(playback_offset_micros=9_000_000, playback_duration_micros=60_000_000), config)

    task.run()

    assert task.status == PlaybackStatus.FINISHED, task.message
    assert task.total_frames == task.processed_frames == 25

def test_range_without_frames_fails_before_publishing(config, publish, reset_stream, download, long_clips, tmp_path):
    download(long_clips['mp4'].read_bytes())
    task = PlaybackTask(_desired(playback_offset_micros=20_000_000), config)

    task.run()

    assert task.status == PlaybackStatus.FAILED
    assert 'no frames in the range 20.000s..end' in task.message
    assert task.total_frames is None
    publish.assert_not_called()
    reset_stream.assert_not_called()
    assert list(tmp_path.iterdir()) == []

@pytest.mark.parametrize('offset_us, duration_us, message', [
    (-1, None, 'playbackOffsetMicros must not be negative, got -1'),
    (None, 0, 'playbackDurationMicros must be positive, got 0'),
    (1_000_000, -1, 'playbackDurationMicros must be positive, got -1'),
])
def test_invalid_range_fails_without_download(config, publish, download, offset_us, duration_us, message):
    task = PlaybackTask(_desired(playback_offset_micros=offset_us, playback_duration_micros=duration_us), config)

    task.run()

    assert task.status == PlaybackStatus.FAILED
    assert task.message == message
    download.mock.assert_not_called()

def test_no_range_plays_the_whole_video(config, publish, reset_stream, download, video_clip):
    download(video_clip.read_bytes())
    task = PlaybackTask(_desired(playback_offset_micros=None, playback_duration_micros=None), config)

    task.run()

    assert task.status == PlaybackStatus.FINISHED
    assert task.total_frames == task.processed_frames == CLIP_FRAMES
