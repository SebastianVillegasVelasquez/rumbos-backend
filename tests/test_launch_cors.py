"""The browser calls the API from the iframe's own origin (the Rumbos app),
never from Moodle's. `POST /auth/launch` must pass its preflight from there and
its answers, errors included, must carry the CORS headers."""

from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import get_settings
from app.main import create_app
from tests.api_client import ApiClient

RUMBOS = "https://rumbos.objdigital.com.co"
MOODLE = "https://academiaturismo.mincit.gov.co"


@pytest.fixture
async def client(engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    settings = get_settings().model_copy(
        update={"cors_allowed_origins": [RUMBOS], "legacy_anonymous_mode": False}
    )
    app = create_app(settings)
    app.state.db_engine = engine
    async with ApiClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t/api/v1"
    ) as c:
        yield c


async def test_the_launch_preflight_from_the_app_origin_succeeds(
    client: httpx.AsyncClient,
) -> None:
    r = await client.options(
        "/auth/launch",
        headers={
            "Origin": RUMBOS,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,authorization",
        },
    )

    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == RUMBOS
    assert "POST" in r.headers["access-control-allow-methods"]
    allowed = r.headers["access-control-allow-headers"].lower()
    assert "content-type" in allowed
    assert "authorization" in allowed


async def test_moodles_own_origin_is_not_what_the_browser_sends(
    client: httpx.AsyncClient,
) -> None:
    r = await client.options(
        "/auth/launch",
        headers={
            "Origin": MOODLE,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert r.status_code == 400  # only the app origin is configured here


async def test_a_rejected_launch_still_carries_the_cors_headers(
    client: httpx.AsyncClient,
) -> None:
    r = await client.post("/auth/launch", json={"nope": 1}, headers={"Origin": RUMBOS})

    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "launch_invalid"
    assert r.headers["access-control-allow-origin"] == RUMBOS
