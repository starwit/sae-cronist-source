import logging
import signal
import threading

from prometheus_client import Counter, Gauge, start_http_server

from .config import CronistSourceConfig
from .cronistclient import CronistClient
from .playback import PlaybackTask
from .reconciler import Reconciler

logging.basicConfig(format='%(asctime)s %(name)-15s %(levelname)-8s %(processName)-10s %(message)s')
logger = logging.getLogger(__name__)

SYNC_ERRORS = Counter('cronist_source_sync_errors', 'Failed requests to cronist', ['operation'])
OBSERVED_GENERATION = Gauge('cronist_source_observed_generation', 'Generation of the desired state last reconciled')

# How long to wait for a running task on shutdown before giving up on it
SHUTDOWN_TIMEOUT_S = 10.0


def run_stage():

    stop_event = threading.Event()

    # Register signal handlers
    def sig_handler(signum, _):
        signame = signal.Signals(signum).name
        print(f'Caught signal {signame} ({signum}). Exiting...')
        stop_event.set()

    signal.signal(signal.SIGTERM, sig_handler)
    signal.signal(signal.SIGINT, sig_handler)

    # Load config from settings.yaml / env vars
    CONFIG = CronistSourceConfig()

    logging.getLogger('cronistsource').setLevel(CONFIG.log_level.value)

    logger.info(f'Starting prometheus metrics endpoint on port {CONFIG.prometheus_port}')

    start_http_server(CONFIG.prometheus_port)

    logger.info(f'Starting cronist source stage. Config: {CONFIG.model_dump_json(indent=2, exclude={"cronist": {"auth": {"client_secret"}}})}')

    client = CronistClient(CONFIG.cronist, CONFIG.sae_id)
    reconciler = Reconciler(CONFIG.sae_id, lambda desired: PlaybackTask(desired, CONFIG))

    try:
        while not stop_event.is_set():
            sync(client, reconciler)
            stop_event.wait(CONFIG.cronist.sync_interval)
    finally:
        reconciler.shutdown(SHUTDOWN_TIMEOUT_S)


def sync(client: CronistClient, reconciler: Reconciler) -> None:
    '''One reconciliation round: fetch the desired state, act on it, report what we observe.

    Failures only skip the affected half of the round. The observed state is reported even if the
    desired state could not be fetched, so cronist keeps seeing the SAE and its progress.
    '''
    try:
        reconciler.reconcile(client.get_desired_state())
    except Exception as e:
        SYNC_ERRORS.labels(operation='get_desired_state').inc()
        logger.warning('Could not reconcile desired state: %s', e)

    observed = reconciler.observed_state()
    OBSERVED_GENERATION.set(observed.observed_generation)
    try:
        client.put_observed_state(observed)
    except Exception as e:
        SYNC_ERRORS.labels(operation='put_observed_state').inc()
        logger.warning('Could not report observed state: %s', e)
