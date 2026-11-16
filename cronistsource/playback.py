import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

import requests
import valkey
from prometheus_client import Counter, Gauge, Histogram
from visionlib.pipeline import ValkeyPublisher

from .config import CronistSourceConfig, RedisConfig
from .cronistclient import PlaybackStatus, SaeDesiredState
from .encoding import build_encoder
from .frames import RangePlan, check_frame_timing, describe_range, iter_frames, plan_range
from .saemessage import to_sae_message

logger = logging.getLogger(__name__)

TASKS_COMPLETED = Counter('cronist_source_tasks_completed', 'How many tasks have ended, by outcome', ['status'])
DOWNLOAD_BYTES = Counter('cronist_source_download_bytes', 'Bytes of video downloaded')
DOWNLOAD_DURATION = Histogram('cronist_source_download_duration_seconds', 'The time it takes to download one video',
                              buckets=(0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600))
FRAMES_PUBLISHED = Counter('cronist_source_frames_published', 'How many frames have been published to the output stream')
LAST_FRAME_TIMESTAMP_MS = Gauge('cronist_source_last_frame_timestamp_ms', 'timestamp_utc_ms of the most recently published frame')
PUBLISH_DURATION = Histogram('cronist_source_publish_duration_seconds',
                             'Wall time spent in publish(), including time blocked by backpressure',
                             buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60))

DOWNLOAD_CHUNK_SIZE = 1024 * 1024
DOWNLOAD_PROGRESS_INTERVAL_S = 10.0


def _content_length(response: requests.Response) -> Optional[int]:
    try:
        return int(response.headers['Content-Length'])
    except (KeyError, TypeError, ValueError):
        return None   # e.g. chunked transfer encoding


class TaskStopped(Exception):
    '''Raised inside the playback thread when the task was stopped from outside.'''


def _reset_stream(config: RedisConfig, stream_key: str) -> None:
    '''Discard any leftover entries so this task does not feed the previous task's backlog downstream.

    Deliberately XTRIM rather than DEL: DEL would take the consumer group with it, and consumers
    that are already attached would get a NOGROUP error. Trimming leaves the group intact, and since
    stream IDs keep increasing, everything published afterwards is still delivered to it.
    '''
    client = valkey.Valkey(config.host, config.port)
    try:
        trimmed = client.xtrim(stream_key, maxlen=0, approximate=False)
        if trimmed:
            logger.info('Discarded %d leftover entries from %s', trimmed, stream_key)
    finally:
        client.close()


class PlaybackTask(threading.Thread):
    '''Plays one task: downloads the video, decodes it and publishes every frame (or every frame of the
    requested range) with backpressure.

    The control loop reads `status`, `processed_frames` and `message` while the task runs. These are
    plain attribute assignments, which are atomic under the GIL.
    '''

    def __init__(self, desired: SaeDesiredState, config: CronistSourceConfig) -> None:
        super().__init__(name=f'playback-{desired.task_id}', daemon=True)
        self.task_id = desired.task_id
        self.video_id = desired.video_id
        self.status = PlaybackStatus.PLAYING
        self.processed_frames = 0
        # Frames this task will publish, known once the downloaded video has been checked
        self.total_frames: Optional[int] = None
        self.message: Optional[str] = None

        self._video_url = desired.video_url
        self._video_start = desired.video_start
        self._offset_us = desired.playback_offset_micros
        self._duration_us = desired.playback_duration_micros
        self._config = config
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    @property
    def stopped(self) -> bool:
        return self._stop_event.is_set()

    @property
    def plays_range(self) -> bool:
        return self._offset_us is not None or self._duration_us is not None

    def run(self) -> None:
        if self.plays_range:
            logger.info('Starting task %s (video %s, %s)', self.task_id, self.video_id,
                        describe_range(self._offset_us, self._duration_us))
        else:
            logger.info('Starting task %s (video %s, whole video)', self.task_id, self.video_id)
        video_path = None
        try:
            if not self._video_url:
                raise ValueError('videoUrl missing')
            if self._video_start is None:
                # Never guess frame times: they would look plausible downstream but be wrong.
                raise ValueError('videoStart missing')
            if self._offset_us is not None and self._offset_us < 0:
                raise ValueError(f'playbackOffsetMicros must not be negative, got {self._offset_us}')
            if self._duration_us is not None and self._duration_us <= 0:
                raise ValueError(f'playbackDurationMicros must be positive, got {self._duration_us}')
            start_epoch_ms = round(self._video_start.timestamp() * 1000)

            self.message = 'Downloading video'
            video_path = self._download()

            # Reject videos without usable frame times before feeding any of their frames downstream
            self.message = 'Checking video'
            plan = None
            if self.plays_range:
                plan = plan_range(video_path, self._offset_us or 0, self._duration_us, self._config.source.target_fps)
                self.total_frames = plan.total_frames
            else:
                self.total_frames = check_frame_timing(video_path, self._config.source.target_fps)

            self.message = None
            self._publish_frames(video_path, start_epoch_ms, plan)

            self.status = PlaybackStatus.FINISHED
            logger.info('Finished task %s: published %d frames', self.task_id, self.processed_frames)
        except TaskStopped:
            logger.info('Stopped task %s after %d frames', self.task_id, self.processed_frames)
        except Exception as e:
            if self.stopped:
                logger.info('Stopped task %s after %d frames (%s)', self.task_id, self.processed_frames, e)
            else:
                logger.error('Task %s failed', self.task_id, exc_info=e)
                self.message = str(e) or type(e).__name__
                self.status = PlaybackStatus.FAILED
        finally:
            if video_path is not None:
                video_path.unlink(missing_ok=True)
            if not self.stopped:
                TASKS_COMPLETED.labels(status=self.status.value).inc()

    def _download(self) -> Path:
        '''Download the whole video first, so playback does not depend on the URL staying valid.

        Presigned URLs expire, and an HTTP connection left idle while backpressure stalls playback
        may be dropped; reconnecting after the signature expired would then fail mid-video.
        '''
        started = time.perf_counter()
        fd, name = tempfile.mkstemp(prefix=f'cronistsource-{self.task_id}-', dir=self._config.source.download_dir)
        path = Path(name)
        size = 0
        try:
            with os.fdopen(fd, 'wb') as file, \
                    requests.get(self._video_url, stream=True, timeout=self._config.source.download_timeout) as response:
                response.raise_for_status()
                total = _content_length(response)
                next_progress_log = started + DOWNLOAD_PROGRESS_INTERVAL_S
                for chunk in response.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
                    if self.stopped:
                        raise TaskStopped()
                    file.write(chunk)
                    size += len(chunk)
                    DOWNLOAD_BYTES.inc(len(chunk))

                    now = time.perf_counter()
                    if now >= next_progress_log:
                        next_progress_log = now + DOWNLOAD_PROGRESS_INTERVAL_S
                        self._log_download_progress(size, total, now - started)
        except BaseException:
            path.unlink(missing_ok=True)
            raise

        elapsed = time.perf_counter() - started
        DOWNLOAD_DURATION.observe(elapsed)
        logger.info('Downloaded video %s (%.1f MB) in %.1fs', self.video_id, size / 1024 / 1024, elapsed)
        return path

    def _log_download_progress(self, size: int, total: Optional[int], elapsed: float) -> None:
        elapsed = max(elapsed, 1e-6)
        rate = size / elapsed / 1024 / 1024
        if total:
            remaining_s = (total - size) / (size / elapsed)
            logger.info('Downloading video %s: %.1f of %.1f MB (%.0f%%), %.1f MB/s, about %.0fs left', self.video_id,
                        size / 1024 / 1024, total / 1024 / 1024, 100 * size / total, rate, remaining_s)
        else:
            logger.info('Downloading video %s: %.1f MB, %.1f MB/s', self.video_id, size / 1024 / 1024, rate)

    def _publish_frames(self, video_path: Path, start_epoch_ms: int, plan: Optional[RangePlan] = None) -> None:
        redis = self._config.redis
        stream_key = self._config.output_stream_key
        encoding = self._config.source.encoding
        encoder = build_encoder(encoding)

        if redis.reset_stream_on_task_start:
            _reset_stream(redis, stream_key)

        publisher = ValkeyPublisher(
            redis.host, redis.port,
            stream_maxlen=redis.stream_maxlen,
            # Always on. Playback that silently drops frames produces results that look plausible
            # and are wrong, so there is no sensible reason to run without it.
            enable_backpressure=True,
            backpressure_threshold=redis.backpressure.threshold,
            monitor_interval=redis.backpressure.monitor_interval,
            consumer_idle_timeout_ms=redis.backpressure.consumer_idle_timeout_ms,
            fail_open_timeout=redis.backpressure.fail_open_timeout,
        )

        with publisher as publish:
            for frame in iter_frames(video_path, start_epoch_ms, scale_width=encoding.scale_width,
                                     target_fps=self._config.source.target_fps, stop_event=self._stop_event,
                                     plan=plan):
                proto_data = to_sae_message(
                    source_id=self._config.stream_id,
                    timestamp_utc_ms=frame.timestamp_utc_ms,
                    image_bgr=frame.image_bgr,
                    jpeg_bytes=encoder.encode(frame.image_bgr),
                )

                # Blocks while the consumer group is too far behind. This cannot be interrupted, so a
                # stop takes effect once lag drops or the publisher fails open.
                with PUBLISH_DURATION.time():
                    publish(stream_key, proto_data)

                self.processed_frames += 1
                FRAMES_PUBLISHED.inc()
                LAST_FRAME_TIMESTAMP_MS.set(frame.timestamp_utc_ms)

        if self.stopped:
            raise TaskStopped()
