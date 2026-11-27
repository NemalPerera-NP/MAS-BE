"""Runtime configuration shared across backend and agents."""
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    storage: Literal["mongo", "memory"] = "mongo"
    mongodb_uri: SecretStr | None = None
    mongodb_database: str = "hvac_mas"
    mongodb_telemetry_collection: str = "hvac_telemetry"
    mongodb_ttl_seconds: Literal[7200] = 7200
    building_path: Path = Path("config/building.yaml")
    scenarios_path: Path = Path("config/scenarios.yaml")
    rules_path: Path = Path("config/control_rules.yaml")
    api_url: str = "http://127.0.0.1:8000"
    cors_origins: str = ""
    runtime_token: SecretStr = SecretStr("local-development")
    decision_timeout: float = Field(default=8, gt=0)
    step_interval: float = Field(default=2, gt=0)
    xmpp_domain: str = "localhost"
    xmpp_port: int = Field(default=5222, ge=1, le=65535)
    xmpp_embedded: bool = True
    xmpp_password: SecretStr | None = None
