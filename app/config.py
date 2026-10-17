from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://tracemind:tracemind@localhost:5432/tracemind"
    correlation_window_seconds: int = Field(default=300, ge=1, le=86400)
    error_burst_threshold: int = Field(default=5, ge=2)
    service_dependencies: dict[str, list[str]] = {
        "payment-service": ["database-service"],
        "user-service": ["database-service"],
    }
    rca_provider: Literal["disabled", "openai", "gemini"] = "disabled"
    openai_api_key: SecretStr | None = None
    openai_model: str = Field(default="", max_length=200)
    gemini_api_key: SecretStr | None = None
    gemini_model: str = Field(default="", max_length=200)
    rca_timeout_seconds: float = Field(default=30, ge=1, le=120)
    rca_max_logs: int = Field(default=40, ge=1, le=100)
    rca_max_evidence_chars: int = Field(default=40000, ge=2000, le=100000)
    rca_max_output_tokens: int = Field(default=4000, ge=512, le=16000)
