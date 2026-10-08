from datetime import datetime, timezone

import pytest

from cronistsource.cronistclient import PlaybackStatus, SaeDesiredState
from cronistsource.reconciler import Reconciler


class FakeTask:
    def __init__(self, desired: SaeDesiredState) -> None:
        self.task_id = desired.task_id
        self.video_id = desired.video_id
        self.status = PlaybackStatus.PLAYING
        self.processed_frames = 0
        self.total_frames = None
        self.message = None
        self.started = False
        self.stop_requested = False
        self.alive = False

    def start(self):
        self.started = True
        self.alive = True

    def stop(self):
        self.stop_requested = True

    def is_alive(self):
        return self.alive

    def join(self, timeout=None):
        pass


@pytest.fixture
def tasks():
    return []

@pytest.fixture
def reconciler(tasks):
    def factory(desired):
        task = FakeTask(desired)
        tasks.append(task)
        return task
    return Reconciler('sae-1', factory)

def _desired(generation, task_id=None):
    if task_id is None:
        return SaeDesiredState(sae_id='sae-1', generation=generation)
    return SaeDesiredState(sae_id='sae-1', generation=generation, task_id=task_id, video_id=f'video-{task_id}',
                           video_url='http://s3/video.mp4', video_start=datetime(2026, 1, 1, tzinfo=timezone.utc))


def test_initial_state_is_idle(reconciler):
    observed = reconciler.observed_state()

    assert observed.sae_id == 'sae-1'
    assert observed.observed_generation == 0
    assert observed.playback_status == PlaybackStatus.IDLE
    assert observed.task_id is None
    assert observed.processed_frames is None
    assert observed.total_frames is None

def test_idle_desired_state_adopts_generation(reconciler, tasks):
    reconciler.reconcile(_desired(5))

    assert reconciler.observed_state().observed_generation == 5
    assert reconciler.observed_state().playback_status == PlaybackStatus.IDLE
    assert tasks == []

def test_new_task_is_started(reconciler, tasks):
    reconciler.reconcile(_desired(1, 't1'))

    assert len(tasks) == 1 and tasks[0].started
    observed = reconciler.observed_state()
    assert observed.observed_generation == 1
    assert observed.task_id == 't1'
    assert observed.video_id == 'video-t1'
    assert observed.playback_status == PlaybackStatus.PLAYING
    assert observed.processed_frames == 0

def test_progress_is_reported(reconciler, tasks):
    reconciler.reconcile(_desired(1, 't1'))
    tasks[0].processed_frames = 42
    tasks[0].total_frames = 100

    assert reconciler.observed_state().processed_frames == 42
    assert reconciler.observed_state().total_frames == 100

def test_generation_bump_with_same_task_does_not_restart(reconciler, tasks):
    reconciler.reconcile(_desired(1, 't1'))
    reconciler.reconcile(_desired(2, 't1'))

    assert len(tasks) == 1
    assert not tasks[0].stop_requested
    assert reconciler.observed_state().observed_generation == 2

def test_task_change_waits_for_old_task_to_stop(reconciler, tasks):
    reconciler.reconcile(_desired(1, 't1'))

    reconciler.reconcile(_desired(2, 't2'))

    assert tasks[0].stop_requested
    assert len(tasks) == 1
    observed = reconciler.observed_state()
    assert observed.observed_generation == 1
    assert observed.task_id is None
    assert observed.playback_status == PlaybackStatus.IDLE

    tasks[0].alive = False
    reconciler.reconcile(_desired(2, 't2'))

    assert len(tasks) == 2 and tasks[1].started
    observed = reconciler.observed_state()
    assert observed.observed_generation == 2
    assert observed.task_id == 't2'
    assert observed.processed_frames == 0

@pytest.mark.parametrize('status', [PlaybackStatus.FINISHED, PlaybackStatus.FAILED])
def test_ended_task_is_reported_until_task_changes(reconciler, tasks, status):
    reconciler.reconcile(_desired(1, 't1'))
    tasks[0].status = status
    tasks[0].message = 'details'
    tasks[0].alive = False

    reconciler.reconcile(_desired(1, 't1'))
    reconciler.reconcile(_desired(2, 't1'))

    assert len(tasks) == 1
    observed = reconciler.observed_state()
    assert observed.task_id == 't1'
    assert observed.playback_status == status
    assert observed.message == 'details'

    reconciler.reconcile(_desired(3))

    observed = reconciler.observed_state()
    assert observed.observed_generation == 3
    assert observed.task_id is None
    assert observed.playback_status == PlaybackStatus.IDLE
    assert observed.message is None

def test_lower_generation_after_cronist_restart_is_adopted(reconciler, tasks):
    reconciler.reconcile(_desired(7, 't1'))
    reconciler.reconcile(_desired(1, 't1'))

    assert len(tasks) == 1
    assert reconciler.observed_state().observed_generation == 1

def test_idle_desired_state_stops_task(reconciler, tasks):
    reconciler.reconcile(_desired(1, 't1'))
    tasks[0].alive = False

    reconciler.reconcile(_desired(2))

    assert tasks[0].stop_requested
    observed = reconciler.observed_state()
    assert observed.observed_generation == 2
    assert observed.playback_status == PlaybackStatus.IDLE
    assert observed.task_id is None

def test_shutdown_stops_running_task(reconciler, tasks):
    reconciler.reconcile(_desired(1, 't1'))

    reconciler.shutdown(timeout=1)

    assert tasks[0].stop_requested
