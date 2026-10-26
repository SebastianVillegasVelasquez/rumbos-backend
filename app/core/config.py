from functools import cached_property, lru_cache
from typing import Annotated, Literal

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
    # Refuse to start when the storage directory does not match the database
    # (an unmounted or replaced volume; see `app.assets.fingerprint`). Off: the
    # app starts anyway, logs an error, refuses uploads and reports
    # `storage: "degraded"` on `/health/ready`.
    storage_fail_fast: bool = False
    # Upload limits: per-file bytes and longest side, by kind, and the sum of
    # every stored file.
    assets_max_background_bytes: int = Field(default=8 * 1024 * 1024, ge=1)
    assets_max_bubble_bytes: int = Field(default=2 * 1024 * 1024, ge=1)
    assets_max_background_side: int = Field(default=8192, ge=1, le=16384)
    assets_max_bubble_side: int = Field(default=1024, ge=1, le=16384)
    # Pictures inside content bubbles.
    assets_max_image_bytes: int = Field(default=4 * 1024 * 1024, ge=1)
    assets_max_image_side: int = Field(default=2048, ge=1, le=16384)
    assets_max_total_bytes: int = Field(default=2 * 1024**3, ge=1)
    # Audio uploads (music for maps) are a copyright exposure, so they have
    # their own switch. Unset: on in dev, off in production. It blocks only
    # uploading; playing and selecting library tracks never depend on it.
    audio_uploads_enabled: bool | None = None
    assets_max_audio_bytes: int = Field(default=8 * 1024 * 1024, ge=1)
    # Longest accepted track, in seconds.
    assets_max_audio_seconds: float = Field(default=600.0, gt=0)

    # `dev` or `production`. Only decides the default of `legacy_anonymous_mode`.
    app_env: Literal["dev", "production"] = "dev"
    # Keeps the old behaviour for the "legacy" tenant: requests without a bearer
    # token are served anonymously, as an editor of that tenant, and talk to
    # Moodle with the service token. Unset: on in dev, off in production.
    legacy_anonymous_mode: bool | None = None
    # The `aud` a launch assertion must carry.
    auth_audience: str = Field(default="rumbos", min_length=1)
    # How long a bearer token lives (seconds).
    access_token_ttl_seconds: int = Field(default=1800, ge=60, le=86400)
    # Clock skew tolerated when checking `iat` / `exp` of an assertion (seconds).
    launch_leeway_seconds: int = Field(default=30, ge=0, le=30)
    # `/auth/launch` limits per minute, kept in process memory (see
    # `app.auth.rate_limit`): per client address, and per tenant once the
    # assertion's signature has been verified.
    launch_rate_per_ip: int = Field(default=30, ge=1)
    launch_rate_per_tenant: int = Field(default=300, ge=1)

    # How many publications of each map to keep (the latest is never pruned).
    publications_keep: int = Field(default=20, ge=1)

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

    @property
    def audio_uploads_on(self) -> bool:
        """Whether editors may upload their own audio."""
        if self.audio_uploads_enabled is not None:
            return self.audio_uploads_enabled
        return self.app_env == "dev"

    @property
    def legacy_mode(self) -> bool:
        """Whether anonymous requests are served as the legacy tenant."""
        if self.legacy_anonymous_mode is not None:
            return self.legacy_anonymous_mode
        return self.app_env == "dev"

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
