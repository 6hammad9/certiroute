"""Application configuration loaded from environment variables."""

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings; secrets are never stored in source control."""

    fortyguard_api_key: SecretStr = Field(min_length=1)
    fortyguard_api_base_url: str = "https://api.fortyguard.com/v1"
    fortyguard_timeout_seconds: float = Field(default=30.0, gt=0)
    fortyguard_poll_interval_seconds: float = Field(default=5.0, gt=0)
    fortyguard_max_poll_attempts: int = Field(default=60, ge=1, le=300)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    """Return a cached settings object without exposing secret values."""

    return Settings()  # type: ignore[call-arg]
