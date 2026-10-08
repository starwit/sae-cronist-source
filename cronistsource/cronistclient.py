'''Client for cronist's SAE API (/api/sae/{saeId}/desired-state and /observed-state).'''
import logging
import time
from datetime import datetime
from enum import Enum
from typing import Optional

import requests
from pydantic import AwareDatetime, BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from .config import AuthConfig, CronistConfig

logger = logging.getLogger(__name__)

# Refresh tokens this long before they expire, so a request never goes out with a token about to lapse.
TOKEN_EXPIRY_MARGIN_S = 30.0


class _CamelCaseModel(BaseModel):
    '''Base for cronist API models: snake_case fields in Python, camelCase keys in JSON (e.g. task_id <-> taskId).'''

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class PlaybackStatus(str, Enum):
    IDLE = 'IDLE'
    PLAYING = 'PLAYING'
    FINISHED = 'FINISHED'
    FAILED = 'FAILED'


class SaeDesiredState(_CamelCaseModel):
    sae_id: str
    # Instance the task is bound to. A task bound to another instance (e.g. the one before a restart)
    # must not be processed. None if task_id is None.
    instance_id: Optional[str] = None
    generation: int = 0
    # The only field that signals idle. The other task fields are not relevant if it is None.
    task_id: Optional[str] = None
    video_id: Optional[str] = None
    video_url: Optional[str] = None
    video_start: Optional[AwareDatetime] = None


class SaeObservedState(_CamelCaseModel):
    sae_id: str
    instance_id: str
    observed_generation: int = 0
    task_id: Optional[str] = None
    video_id: Optional[str] = None
    playback_status: PlaybackStatus = PlaybackStatus.IDLE
    processed_frames: Optional[int] = None
    total_frames: Optional[int] = None
    message: Optional[str] = None


class TokenProvider:
    '''Fetches and caches a bearer token via the OAuth2 client credentials grant.'''

    def __init__(self, config: AuthConfig, session: requests.Session, timeout: float) -> None:
        self._config = config
        self._session = session
        self._timeout = timeout
        self._token: Optional[str] = None
        self._expires_at = 0.0

    def get(self) -> str:
        if self._token is None or time.monotonic() >= self._expires_at:
            self._fetch()
        return self._token

    def invalidate(self) -> None:
        self._token = None

    def _fetch(self) -> None:
        response = self._session.post(self._config.token_url, timeout=self._timeout, data={
            'grant_type': 'client_credentials',
            'client_id': self._config.client_id,
            'client_secret': self._config.client_secret,
        })
        response.raise_for_status()
        body = response.json()
        self._token = body['access_token']
        expires_in = float(body.get('expires_in', 60))
        self._expires_at = time.monotonic() + max(0.0, expires_in - TOKEN_EXPIRY_MARGIN_S)
        logger.debug('Fetched access token, expires in %.0fs', expires_in)


class CronistClient:
    def __init__(self, config: CronistConfig, sae_id: str, session: Optional[requests.Session] = None) -> None:
        self._base_url = config.base_url.rstrip('/')
        self._sae_id = sae_id
        self._timeout = config.request_timeout
        self._session = session or requests.Session()
        self._tokens = TokenProvider(config.auth, self._session, self._timeout) if config.auth else None

    def get_desired_state(self) -> SaeDesiredState:
        response = self._request('GET', 'desired-state', headers={'Accept': 'application/json'})
        return SaeDesiredState.model_validate(response.json())

    def put_observed_state(self, state: SaeObservedState) -> None:
        self._request('PUT', 'observed-state', json=state.model_dump(mode='json', by_alias=True))

    def _request(self, method: str, resource: str, headers: Optional[dict] = None, **kwargs) -> requests.Response:
        url = f'{self._base_url}/api/sae/{self._sae_id}/{resource}'
        headers = dict(headers or {})

        response = self._send(method, url, headers, **kwargs)
        if response.status_code == 401 and self._tokens is not None:
            # The token may have been revoked or the auth server restarted; retry once with a fresh one.
            self._tokens.invalidate()
            response = self._send(method, url, headers, **kwargs)

        response.raise_for_status()
        return response

    def _send(self, method: str, url: str, headers: dict, **kwargs) -> requests.Response:
        if self._tokens is not None:
            headers['Authorization'] = f'Bearer {self._tokens.get()}'
        return self._session.request(method, url, headers=headers, timeout=self._timeout, **kwargs)
