from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from cronistsource.config import AuthConfig, CronistConfig
from cronistsource.cronistclient import (CronistClient, PlaybackStatus,
                                         SaeObservedState)


def _response(status_code=200, json=None):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = json
    if status_code >= 400:
        response.raise_for_status.side_effect = Exception(f'HTTP {status_code}')
    return response

def _token_response(token, expires_in=300):
    return _response(json={'access_token': token, 'expires_in': expires_in})

@pytest.fixture
def session():
    return MagicMock()

def test_get_desired_state(session):
    session.request.return_value = _response(json={
        'saeId': 'sae-1', 'instanceId': 'instance-1', 'generation': 3, 'taskId': 't1', 'videoId': 'v1',
        'videoUrl': 'http://s3/video.mp4', 'videoStart': '2026-01-01T12:00:00Z',
    })
    client = CronistClient(CronistConfig(base_url='http://cronist/cronist/'), 'sae-1', session=session)

    desired = client.get_desired_state()

    assert session.request.call_args.args == ('GET', 'http://cronist/cronist/api/sae/sae-1/desired-state')
    assert 'Authorization' not in session.request.call_args.kwargs['headers']
    assert desired.instance_id == 'instance-1'
    assert desired.generation == 3
    assert desired.task_id == 't1'
    assert desired.video_url == 'http://s3/video.mp4'
    assert desired.video_start == datetime(2026, 1, 1, 12, tzinfo=timezone.utc)

def test_get_idle_desired_state(session):
    session.request.return_value = _response(json={
        'saeId': 'sae-1', 'instanceId': None, 'generation': 0, 'taskId': None, 'videoId': None, 'videoUrl': None, 'videoStart': None,
    })
    client = CronistClient(CronistConfig(), 'sae-1', session=session)

    desired = client.get_desired_state()

    assert desired.task_id is None
    assert desired.instance_id is None
    assert desired.video_start is None

def test_put_observed_state_sends_camel_case_with_nulls(session):
    session.request.return_value = _response()
    client = CronistClient(CronistConfig(), 'sae-1', session=session)

    client.put_observed_state(SaeObservedState(sae_id='sae-1', instance_id='instance-1', observed_generation=2,
                                               playback_status=PlaybackStatus.IDLE))

    assert session.request.call_args.args == ('PUT', 'http://localhost:8081/cronist/api/sae/sae-1/observed-state')
    assert session.request.call_args.kwargs['json'] == {
        'saeId': 'sae-1', 'instanceId': 'instance-1', 'observedGeneration': 2, 'taskId': None, 'videoId': None,
        'playbackStatus': 'IDLE', 'processedFrames': None, 'totalFrames': None, 'message': None,
    }

def test_http_error_raises(session):
    session.request.return_value = _response(status_code=500)
    client = CronistClient(CronistConfig(), 'sae-1', session=session)

    with pytest.raises(Exception):
        client.get_desired_state()

def _auth_config():
    return CronistConfig(auth=AuthConfig(token_url='http://kc/token', client_id='sae', client_secret='secret'))

def test_bearer_token_is_fetched_and_cached(session):
    session.post.return_value = _token_response('token-1')
    session.request.return_value = _response()
    client = CronistClient(_auth_config(), 'sae-1', session=session)

    client.put_observed_state(SaeObservedState(sae_id='sae-1', instance_id='instance-1'))
    client.put_observed_state(SaeObservedState(sae_id='sae-1', instance_id='instance-1'))

    assert session.post.call_count == 1
    assert session.post.call_args.kwargs['data']['grant_type'] == 'client_credentials'
    assert session.request.call_args.kwargs['headers']['Authorization'] == 'Bearer token-1'

def test_expired_token_is_refreshed(session):
    # expires_in below the refresh margin, so the token is stale immediately
    session.post.side_effect = [_token_response('token-1', expires_in=10), _token_response('token-2')]
    session.request.return_value = _response()
    client = CronistClient(_auth_config(), 'sae-1', session=session)

    client.put_observed_state(SaeObservedState(sae_id='sae-1', instance_id='instance-1'))
    client.put_observed_state(SaeObservedState(sae_id='sae-1', instance_id='instance-1'))

    assert session.post.call_count == 2
    assert session.request.call_args.kwargs['headers']['Authorization'] == 'Bearer token-2'

def test_unauthorized_retries_once_with_fresh_token(session):
    session.post.side_effect = [_token_response('token-1'), _token_response('token-2')]
    session.request.side_effect = [_response(status_code=401), _response()]
    client = CronistClient(_auth_config(), 'sae-1', session=session)

    client.put_observed_state(SaeObservedState(sae_id='sae-1', instance_id='instance-1'))

    assert session.request.call_count == 2
    assert session.request.call_args.kwargs['headers']['Authorization'] == 'Bearer token-2'
