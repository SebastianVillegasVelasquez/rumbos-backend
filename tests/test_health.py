"""Liveness and readiness probes."""

from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.main import app
from app.moodle.exceptions import MoodleUnavailableError
from tests.fakes import InMemoryMoodleClient


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


async def test_ready_is_ok_when_the_database_answers(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    app.state.db_engine = engine

    r = await client.get("/health/ready")

    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_ready_is_503_when_the_database_is_down(
    client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    # Nothing listens on port 1: the connection is refused immediately.
    dead = create_async_engine("postgresql+asyncpg://u:secretpw@127.0.0.1:1/db")
    app.state.db_engine = dead
    try:
        r = await client.get("/health/ready")
    finally:
        await dead.dispose()

    assert r.status_code == 503
    assert r.json() == {"status": "unavailable"}
    assert "secretpw" not in caplog.text and "secretpw" not in r.text


async def test_ready_does_not_call_moodle(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    app.state.db_engine = engine
    moodle = InMemoryMoodleClient()
    moodle.error = MoodleUnavailableError("down")
    app.state.moodle_client = moodle

    r = await client.get("/health/ready")

    assert r.status_code == 200
    assert moodle.calls == 0


async def test_liveness_ignores_the_database(client: httpx.AsyncClient) -> None:
    dead = create_async_engine("postgresql+asyncpg://u:p@127.0.0.1:1/db")
    app.state.db_engine = dead
    try:
        r = await client.get("/health")
    finally:
        await dead.dispose()

    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
