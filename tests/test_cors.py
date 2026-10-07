"""CORS is opt-in: no middleware unless `CORS_ALLOWED_ORIGINS` is set."""

import httpx
import pytest
from pydantic import SecretStr

from app.core.config import Settings, get_settings
from app.main import create_app

ALLOWED = "https://rumbos.example.com"
OTHER = "https://evil.example.com"


def _client(origins: list[str]) -> httpx.AsyncClient:
    settings = get_settings().model_copy(update={"cors_allowed_origins": origins})
    transport = httpx.ASGITransport(app=create_app(settings))
    return httpx.AsyncClient(transport=transport, base_url="http://t")


def _preflight(origin: str) -> dict[str, str]:
    return {
        "Origin": origin,
        "Access-Control-Request-Method": "PATCH",
        "Access-Control-Request-Headers": "content-type",
    }


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", []),
        ("   ", []),
        (ALLOWED, [ALLOWED]),
        (f"{ALLOWED}, {OTHER} ,,", [ALLOWED, OTHER]),
    ],
)
def test_origins_setting_is_comma_separated(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: list[str]
) -> None:
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", raw)
    assert Settings().cors_allowed_origins == expected


def test_origins_default_to_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CORS_ALLOWED_ORIGINS", raising=False)
    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://u:p@h/d",
        moodle_base_url="https://m.example",
        moodle_service_token=SecretStr("t"),
    )
    assert settings.cors_allowed_origins == []


async def test_allowed_origin_passes_preflight_without_credentials() -> None:
    async with _client([ALLOWED]) as client:
        r = await client.options("/course-maps/x", headers=_preflight(ALLOWED))

    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == ALLOWED
    assert "PATCH" in r.headers["access-control-allow-methods"]
    assert "access-control-allow-credentials" not in r.headers


async def test_allowed_origin_gets_cors_headers_on_real_responses() -> None:
    async with _client([ALLOWED]) as client:
        r = await client.get("/health", headers={"Origin": ALLOWED})

    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == ALLOWED
    assert "access-control-allow-credentials" not in r.headers


async def test_other_origin_is_refused() -> None:
    async with _client([ALLOWED]) as client:
        preflight = await client.options("/course-maps", headers=_preflight(OTHER))
        simple = await client.get("/health", headers={"Origin": OTHER})

    assert preflight.status_code == 400
    assert "access-control-allow-origin" not in preflight.headers
    assert "access-control-allow-origin" not in simple.headers


async def test_no_origins_means_no_cors_middleware() -> None:
    app = create_app(get_settings().model_copy(update={"cors_allowed_origins": []}))
    assert not app.user_middleware

    async with _client([]) as client:
        simple = await client.get("/health", headers={"Origin": ALLOWED})
        preflight = await client.options("/course-maps", headers=_preflight(ALLOWED))

    assert simple.status_code == 200
    assert "access-control-allow-origin" not in simple.headers
    assert "access-control-allow-origin" not in preflight.headers
