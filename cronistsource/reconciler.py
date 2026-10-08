import logging
from typing import Callable, Optional

from .cronistclient import PlaybackStatus, SaeDesiredState, SaeObservedState
from .playback import PlaybackTask

logger = logging.getLogger(__name__)


class Reconciler:
    '''Drives playback towards cronist's desired state, similar to a Kubernetes controller.

    `reconcile()` never blocks. If an old task has not stopped yet, the generation is simply not
    adopted, so cronist sees that the SAE has not caught up, and the next call tries again.
    '''

    def __init__(self, sae_id: str, task_factory: Callable[[SaeDesiredState], PlaybackTask]) -> None:
        self._sae_id = sae_id
        self._task_factory = task_factory
        self._observed_generation = 0
        self._current: Optional[PlaybackTask] = None
        self._stopping: Optional[PlaybackTask] = None

    def reconcile(self, desired: SaeDesiredState) -> None:
        current_task_id = self._current.task_id if self._current is not None else None

        if desired.task_id != current_task_id:
            if self._current is not None:
                logger.info('Stopping task %s (desired task: %s)', current_task_id, desired.task_id)
                self._current.stop()
                self._stopping = self._current
                self._current = None

            if self._stopping is not None:
                if self._stopping.is_alive():
                    logger.debug('Waiting for task %s to stop', self._stopping.task_id)
                    return
                self._stopping = None

            if desired.task_id is not None:
                self._current = self._task_factory(desired)
                self._current.start()

        if desired.generation != self._observed_generation:
            logger.info('Reconciled generation %d (task: %s)', desired.generation, desired.task_id)
        self._observed_generation = desired.generation

    def observed_state(self) -> SaeObservedState:
        state = SaeObservedState(sae_id=self._sae_id, observed_generation=self._observed_generation)
        task = self._current
        if task is not None:
            state.task_id = task.task_id
            state.video_id = task.video_id
            state.playback_status = task.status
            state.processed_frames = task.processed_frames
            state.total_frames = task.total_frames
            state.message = task.message
        else:
            state.playback_status = PlaybackStatus.IDLE
        return state

    def shutdown(self, timeout: float) -> None:
        for task in (self._current, self._stopping):
            if task is not None:
                task.stop()
        for task in (self._current, self._stopping):
            if task is not None:
                task.join(timeout)
                if task.is_alive():
                    logger.warning('Task %s did not stop within %.0fs', task.task_id, timeout)
