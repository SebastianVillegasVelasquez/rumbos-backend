from functools import cached_property, lru_cache
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sqlalchemy.engine import URL


class Settings(BaseSettings):
    """Application settings, read from environment variables and `.env`."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    postgres_user: str
    postgres_password: SecretStr
    postgres_db: str
    postgres_host: str
    postgres_port: int = Field(default=5432, ge=1, le=65535)
    # Origins allowed to call the API from a browser, comma-separated in the
    # environment. Empty (the default) means no CORS middleware at all. `NoDecode`
    # stops pydantic-settings from expecting JSON for this list.
    cors_allowed_origins: Annotated[list[str], NoDecode] = []
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

    # User-uploaded images (see `app.services.asset_service`). There is no
    # authentication yet, so uploads are anonymous: the quota and this switch
    # only limit the damage. Off by default; keep it off on any deployment
    # reachable from the internet until auth exists.
    assets_uploads_enabled: bool = False
    # Where `LocalDiskAssetStorage` keeps the files.
    assets_dir: str = "data/assets"
    # Upload limits: per-file bytes and longest side, by kind, and the sum of
    # every stored file.
    assets_max_background_bytes: int = Field(default=8 * 1024 * 1024, ge=1)
    assets_max_bubble_bytes: int = Field(default=2 * 1024 * 1024, ge=1)
    assets_max_background_side: int = Field(default=8192, ge=1, le=16384)
    assets_max_bubble_side: int = Field(default=1024, ge=1, le=16384)
    assets_max_total_bytes: int = Field(default=2 * 1024**3, ge=1)

    @cached_property
    def build_database_url(self) -> str:
        """Async SQLAlchemy URL (asyncpg) built from the `POSTGRES_*` settings."""
        return URL.create(
            "postgresql+asyncpg",
            username=self.postgres_user,
            password=self.postgres_password.get_secret_value(),
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        ).render_as_string(hide_password=False)

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
