import os
from typing import Optional

from pydantic import BaseModel, DirectoryPath, Field
from pydantic_settings import (BaseSettings, SettingsConfigDict,
                               YamlConfigSettingsSource)
from typing_extensions import Annotated
from visionlib.pipeline.settings import LogLevel


class AuthConfig(BaseModel):
    '''OAuth2 client credentials, e.g. a Keycloak client whose service account has the realm role `sae`.'''
    token_url: str
    client_id: str
    client_secret: str

class CronistConfig(BaseModel):
    base_url: str = 'http://localhost:8081/cronist'
    # Must stay well below cronist's `sae.state-expiry` (default 10s), otherwise the SAE is considered unavailable
    sync_interval: Annotated[float, Field(gt=0)] = 2.0
    request_timeout: Annotated[float, Field(gt=0)] = 5.0
    auth: Optional[AuthConfig] = None

class BackpressureConfig(BaseModel):
    threshold: Optional[Annotated[int, Field(ge=1)]] = None
    fail_open_timeout: Optional[Annotated[float, Field(ge=0)]] = 30.0
    monitor_interval: Annotated[float, Field(gt=0)] = 0.05
    consumer_idle_timeout_ms: Annotated[int, Field(ge=0)] = 10_000

class RedisConfig(BaseModel):
    host: str = 'localhost'
    port: Annotated[int, Field(ge=1, le=65536)] = 6379
    output_stream_prefix: str = 'cronistsource'
    stream_maxlen: Annotated[int, Field(ge=2)] = 200
    reset_stream_on_task_start: bool = True
    backpressure: BackpressureConfig = BackpressureConfig()

class EncodingConfig(BaseModel):
    jpeg_quality: Annotated[int, Field(ge=1, le=100)] = 90
    scale_width: Annotated[int, Field(ge=0)] = 0

class SourceConfig(BaseModel):
    target_fps: Optional[Annotated[float, Field(gt=0)]] = None
    encoding: EncodingConfig = EncodingConfig()
    # Where videos are downloaded to before playback. Defaults to the system temp directory.
    download_dir: Optional[DirectoryPath] = None
    download_timeout: Annotated[float, Field(gt=0)] = 30.0

class CronistSourceConfig(BaseSettings):
    log_level: LogLevel = LogLevel.WARNING
    sae_id: Annotated[str, Field(min_length=1)]
    stream_id: Annotated[str, Field(min_length=1)]
    cronist: CronistConfig = CronistConfig()
    redis: RedisConfig = RedisConfig()
    source: SourceConfig = SourceConfig()
    prometheus_port: Annotated[int, Field(ge=1024, le=65536)] = 8000

    model_config = SettingsConfigDict(env_nested_delimiter='__')

    @property
    def output_stream_key(self) -> str:
        return f'{self.redis.output_stream_prefix}:{self.stream_id}'

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings):
        YAML_LOCATION = os.environ.get('SETTINGS_FILE', 'settings.yaml')
        return (init_settings, env_settings, YamlConfigSettingsSource(settings_cls, yaml_file=YAML_LOCATION), file_secret_settings)
