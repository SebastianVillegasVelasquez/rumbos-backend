from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings, read from environment variables and `.env`."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    moodle_base_url: str
    moodle_service_token: SecretStr
    # Seconds. Moodle calls fail fast: there are no automatic retries.
    moodle_connect_timeout: float = 5.0
    moodle_read_timeout: float = 15.0
    # In-process cache of Moodle course contents (see `app.moodle.cache`).
    # A cached copy is served as-is for this long...
    moodle_contents_ttl_seconds: float = Field(default=60.0, ge=0)
    # ...and, when Moodle fails, a copy up to this old is served as "stale".
    moodle_stale_max_seconds: float = Field(default=3600.0, ge=0)


@lru_cache
def get_settings() -> Settings:
    return Settings()
