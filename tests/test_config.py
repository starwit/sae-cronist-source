import pytest
from pydantic import ValidationError

from cronistsource.config import AuthConfig, CronistConfig, CronistSourceConfig


def test_incomplete_config():
    with pytest.raises(ValidationError):
        CronistSourceConfig(
            log_level="INFO",
            # Missing sae_id and stream_id
        )

def test_complete_config():
    config = CronistSourceConfig(
        log_level="INFO",
        sae_id="sae-1",
        stream_id="stream1",
        cronist=CronistConfig(
            base_url="http://cronist:8081/cronist",
            auth=AuthConfig(token_url="http://kc/token", client_id="sae", client_secret="secret"),
        ),
        prometheus_port=9000
    )

    assert config.log_level.name == "INFO"
    assert config.sae_id == "sae-1"
    assert config.cronist.base_url == "http://cronist:8081/cronist"
    assert config.cronist.auth.client_id == "sae"
    assert config.redis.host == "localhost"
    assert config.output_stream_key == "cronistsource:stream1"
    assert config.prometheus_port == 9000

def test_sync_interval_must_be_positive():
    with pytest.raises(ValidationError):
        CronistConfig(sync_interval=0)
