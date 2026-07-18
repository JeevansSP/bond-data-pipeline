"""Application configuration, loaded from environment / ``.env`` via pydantic-settings."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repository root = three levels up from this file (src/bonds/config.py -> repo root).
REPO_ROOT = Path(__file__).resolve().parents[2]


class DatabaseSettings(BaseSettings):
    """Postgres connection settings (env prefix ``BONDS_DB_``)."""

    model_config = SettingsConfigDict(env_prefix="BONDS_DB_", env_file=".env", extra="ignore")

    host: str = "localhost"
    port: int = 5432
    user: str = "bonds"
    password: str = "bonds"
    name: str = "bonds"

    @property
    def url(self) -> str:
        """SQLAlchemy URL for the psycopg (v3) driver."""
        return (
            f"postgresql+psycopg://{self.user}:{self.password}@{self.host}:{self.port}/{self.name}"
        )


class HttpSettings(BaseSettings):
    """HTTP client behaviour (env prefix ``BONDS_HTTP_``).

    Defaults are deliberately gentle: several upstream sources (NSE, CCIL, SEBI) are
    Akamai-protected and rate-limit aggressively.
    """

    model_config = SettingsConfigDict(env_prefix="BONDS_HTTP_", env_file=".env", extra="ignore")

    min_interval_seconds: float = 0.7
    max_retries: int = 4
    timeout_seconds: float = 30.0
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    )


class Settings(BaseSettings):
    """Top-level settings (env prefix ``BONDS_``)."""

    model_config = SettingsConfigDict(env_prefix="BONDS_", env_file=".env", extra="ignore")

    data_root: Path = Field(default=Path("data"))
    log_level: str = "INFO"
    log_json: bool = False

    db: DatabaseSettings = Field(default_factory=DatabaseSettings)
    http: HttpSettings = Field(default_factory=HttpSettings)

    @property
    def data_dir(self) -> Path:
        """Absolute path to the (gitignored) on-disk data lake root."""
        root = self.data_root
        return root if root.is_absolute() else REPO_ROOT / root


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide cached settings instance."""
    return Settings()
