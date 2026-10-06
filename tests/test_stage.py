from unittest.mock import patch

import pytest
from visionapi.common_pb2 import MessageType
from visionapi.sae_pb2 import SaeMessage

from mystage.config import MyStageConfig, RedisConfig
from mystage.stage import run_stage


@pytest.fixture(autouse=True)
def disable_prometheus():
    # We don't want to start the Prometheus server during tests
    with patch('mystage.stage.start_http_server'):
        yield

@pytest.fixture
def set_config():
    with patch('mystage.stage.MyStageConfig') as mock_config:
        def _make_mock_config(stream_id: str, input_stream_prefix: str):
            mock_config.return_value = MyStageConfig(
                log_level='WARNING',
                redis=RedisConfig(
                    stream_id=stream_id,
                    input_stream_prefix=input_stream_prefix
                )
            )
        yield _make_mock_config

@pytest.fixture
def redis_publisher_mock():
    with patch('mystage.stage.ValkeyPublisher') as mock_publisher:
        yield mock_publisher.return_value.__enter__.return_value

@pytest.fixture
def inject_consumer_messages():
    with patch('mystage.stage.ValkeyConsumer') as mock_consumer:
        def _inject_messages(messages):
            mock_consumer.return_value.__enter__.return_value.return_value.__iter__.return_value = iter(messages)
        yield _inject_messages

def test_example(set_config, redis_publisher_mock, inject_consumer_messages):
    set_config(stream_id='test_stream', input_stream_prefix='test_prefix')
    
    inject_consumer_messages([
        ('test_prefix:test_stream', _make_sae_msg_bytes(1)),
        ('test_prefix:test_stream', _make_sae_msg_bytes(2)),
    ])
    
    # Run the stage (this will process the injected messages)
    run_stage()
    
    # Verify that messages were published
    assert redis_publisher_mock.call_count == 2
    assert redis_publisher_mock.call_args_list[0].args == ('mystage:test_stream', _make_sae_msg_bytes(1))
    assert redis_publisher_mock.call_args_list[1].args == ('mystage:test_stream', _make_sae_msg_bytes(2))

def _make_sae_msg_bytes(timestamp: int) -> bytes:
    sae_msg = SaeMessage()
    sae_msg.frame.timestamp_utc_ms = timestamp
    sae_msg.type = MessageType.SAE
    return sae_msg.SerializeToString()