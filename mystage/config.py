import os
from pydantic import BaseModel, Field
from pydantic_settings import (BaseSettings, SettingsConfigDict,
                               YamlConfigSettingsSource)
from typing_extensions import Annotated
from visionlib.pipeline.settings import LogLevel


class RedisConfig(BaseModel):
    host: str = 'localhost'
    port: Annotated[int, Field(ge=1, le=65536)] = 6379
    stream_id: str
    input_stream_prefix: str
    output_stream_prefix: str = 'mystage'

class MyStageConfig(BaseSettings):
    log_level: LogLevel = LogLevel.WARNING
    redis: RedisConfig
    prometheus_port: Annotated[int, Field(ge=1024, le=65536)] = 8000

    model_config = SettingsConfigDict(env_nested_delimiter='__')

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings):
        YAML_LOCATION = os.environ.get('SETTINGS_FILE', 'settings.yaml')
        return (init_settings, env_settings, YamlConfigSettingsSource(settings_cls, yaml_file=YAML_LOCATION), file_secret_settings)