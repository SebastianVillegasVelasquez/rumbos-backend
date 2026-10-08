"""Defaults of the upload settings: safe by default."""

import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import Settings


def make(**overrides: object) -> Settings:
    return Settings.model_validate(
        {
            "database_url": "postgresql+asyncpg://u:p@h/d",
            "moodle_base_url": "https://m.example",
            "moodle_service_token": SecretStr("t"),
            **overrides,
        }
    )


def test_uploads_are_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ASSETS_UPLOADS_ENABLED", "ASSETS_DIR"):
        monkeypatch.delenv(name, raising=False)

    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://u:p@h/d",
        moodle_base_url="https://m.example",
        moodle_service_token=SecretStr("t"),
    )

    assert settings.assets_uploads_enabled is False
    assert settings.assets_dir == "data/assets"
    assert settings.assets_max_background_bytes == 8 * 1024 * 1024
    assert settings.assets_max_bubble_bytes == 2 * 1024 * 1024
    assert settings.assets_max_background_side == 8192
    assert settings.assets_max_bubble_side == 1024
    assert settings.assets_max_total_bytes == 2 * 1024**3


def test_the_switch_reads_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASSETS_UPLOADS_ENABLED", "true")
    monkeypatch.setenv("ASSETS_DIR", "/srv/rumbos/assets")

    settings = Settings()

    assert settings.assets_uploads_enabled is True
    assert settings.assets_dir == "/srv/rumbos/assets"


@pytest.mark.parametrize(
    "field", ["assets_max_background_bytes", "assets_max_bubble_side"]
)
def test_limits_must_be_positive(field: str) -> None:
    with pytest.raises(ValidationError):
        make(**{field: 0})
