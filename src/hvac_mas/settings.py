"""Runtime configuration shared across backend and agents."""
from pathlib import Path
import re
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)
    deployment_mode: Literal["local", "hosted"] = "local"
    evaluator_token: SecretStr | None = None
    storage: Literal["mongo", "memory"] = "mongo"
    mongodb_uri: SecretStr | None = None
    mongodb_database: str = "hvac_mas"
    mongodb_telemetry_collection: str = "hvac_telemetry"
    mongodb_ttl_seconds: Literal[7200] = 7200
    mongodb_operation_timeout_ms: int = Field(default=30000, ge=1000, le=120000)
    building_path: Path = Path("config/building.yaml")
    scenarios_path: Path = Path("config/scenarios.yaml")
    rules_path: Path = Path("config/control_rules.yaml")
    api_url: str = "http://127.0.0.1:8000"
    cors_origins: str = ""
    runtime_token: SecretStr = SecretStr("local-development")
    decision_timeout: float = Field(default=30, ge=5)
    agent_reply_timeout: float = Field(default=5, gt=0)
    heartbeat_timeout: float = Field(default=10, ge=3)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    step_interval: float = Field(default=2, gt=0)
    xmpp_domain: str = "localhost"
    xmpp_port: int = Field(default=5222, ge=1, le=65535)
    xmpp_embedded: bool = True
    xmpp_password: SecretStr | None = None

    @model_validator(mode="after")
    def validate_credentials(self):
        runtime = self.runtime_token.get_secret_value()
        evaluator = self.evaluator_token.get_secret_value() if self.evaluator_token is not None else None
        if self.deployment_mode == "hosted" or evaluator is not None:
            for name, token in (("RUNTIME_TOKEN", runtime), ("EVALUATOR_TOKEN", evaluator)):
                if not token or not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", token):
                    raise ValueError(f"{name} must be a random URL-safe token of 43–128 characters")
            if evaluator == runtime:
                raise ValueError("EVALUATOR_TOKEN and RUNTIME_TOKEN must be different")
        return self
