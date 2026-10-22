"""Defaults of the upload settings: safe by default."""

import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import Settings


def make(**overrides: object) -> Settings:
    return Settings.model_validate(
        {
            "postgres_user": "u",
            "postgres_password": SecretStr("p"),
            "postgres_db": "d",
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
        postgres_user="u",
        postgres_password=SecretStr("p"),
        postgres_db="d",
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


def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("AUDIO_UPLOADS_ENABLED", "APP_ENV"):
        monkeypatch.delenv(name, raising=False)


def audio_settings(**overrides: object) -> Settings:
    return Settings(
        _env_file=None,
        postgres_user="u",
        postgres_password=SecretStr("p"),
        postgres_db="d",
        moodle_base_url="https://m.example",
        moodle_service_token=SecretStr("t"),
        **overrides,  # type: ignore[arg-type]
    )


def test_audio_uploads_are_on_in_dev_and_off_in_production_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clean_env(monkeypatch)

    assert audio_settings().audio_uploads_on is True  # dev is the default
    assert audio_settings(app_env="dev").audio_uploads_on is True
    assert audio_settings(app_env="production").audio_uploads_on is False


def test_the_audio_switch_can_be_set_either_way_in_any_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clean_env(monkeypatch)

    assert audio_settings(
        app_env="production", audio_uploads_enabled=True
    ).audio_uploads_on
    assert not audio_settings(
        app_env="dev", audio_uploads_enabled=False
    ).audio_uploads_on
    monkeypatch.setenv("AUDIO_UPLOADS_ENABLED", "true")
    monkeypatch.setenv("APP_ENV", "production")
    assert audio_settings().audio_uploads_on is True


def test_the_audio_limits_default_to_eight_megabytes_and_ten_minutes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clean_env(monkeypatch)
    settings = audio_settings()

    assert settings.assets_max_audio_bytes == 8 * 1024 * 1024
    assert settings.assets_max_audio_seconds == 600


@pytest.mark.parametrize(
    "field", ["assets_max_audio_bytes", "assets_max_audio_seconds"]
)
def test_audio_limits_must_be_positive(field: str) -> None:
    with pytest.raises(ValidationError):
        audio_settings(**{field: 0})


def test_the_audio_switch_does_not_move_the_image_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clean_env(monkeypatch)
    monkeypatch.delenv("ASSETS_UPLOADS_ENABLED", raising=False)

    settings = audio_settings(audio_uploads_enabled=True)

    assert settings.audio_uploads_on is True
    assert settings.assets_uploads_enabled is False
