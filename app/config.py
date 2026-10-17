from pydantic import Field
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
