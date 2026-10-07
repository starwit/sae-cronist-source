from unittest.mock import MagicMock

from cronistsource.cronistclient import SaeDesiredState, SaeObservedState
from cronistsource.stage import sync


def test_sync_reconciles_and_reports():
    client = MagicMock()
    reconciler = MagicMock()
    desired = SaeDesiredState(sae_id='sae-1', generation=1)
    observed = SaeObservedState(sae_id='sae-1', observed_generation=1)
    client.get_desired_state.return_value = desired
    reconciler.observed_state.return_value = observed

    sync(client, reconciler)

    reconciler.reconcile.assert_called_once_with(desired)
    client.put_observed_state.assert_called_once_with(observed)

def test_sync_reports_even_if_desired_state_is_unavailable():
    client = MagicMock()
    reconciler = MagicMock()
    client.get_desired_state.side_effect = ConnectionError('cronist down')
    reconciler.observed_state.return_value = SaeObservedState(sae_id='sae-1')

    sync(client, reconciler)

    reconciler.reconcile.assert_not_called()
    client.put_observed_state.assert_called_once()

def test_sync_survives_report_failure():
    client = MagicMock()
    reconciler = MagicMock()
    reconciler.observed_state.return_value = SaeObservedState(sae_id='sae-1')
    client.put_observed_state.side_effect = ConnectionError('cronist down')

    sync(client, reconciler)
