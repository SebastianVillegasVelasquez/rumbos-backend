from typing import Annotated

import httpx
from fastapi import Depends, FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.database import get_session
from app.main import app, lifespan
from tests.conftest import Widget


async def test_health() -> None:
    async with lifespan(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


SessionDep = Annotated[AsyncSession, Depends(get_session)]


def _uow_app(engine: AsyncEngine) -> FastAPI:
    test_app = FastAPI()
    test_app.state.db_engine = engine
    seen: list[AsyncSession] = []

    @test_app.post("/add-without-commit")
    async def add_without_commit(session: SessionDep) -> None:
        session.add(Widget())
        await session.flush()
        seen.append(session)

    @test_app.post("/boom")
    async def boom(session: SessionDep) -> None:
        session.add(Widget())
        await session.flush()
        seen.append(session)
        raise RuntimeError("fail after flush")

    test_app.state.seen = seen
    return test_app


async def _count(engine: AsyncEngine) -> int:
    async with AsyncSession(engine) as s:
        return (await s.execute(select(func.count()).select_from(Widget))).scalar_one()


async def test_session_is_fresh_per_request(engine: AsyncEngine) -> None:
    test_app = _uow_app(engine)
    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        assert (await c.post("/add-without-commit")).status_code == 200
        assert (await c.post("/add-without-commit")).status_code == 200
    seen = test_app.state.seen
    assert len(seen) == 2 and seen[0] is not seen[1]


async def test_session_does_not_commit_on_its_own(engine: AsyncEngine) -> None:
    transport = httpx.ASGITransport(app=_uow_app(engine))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        assert (await c.post("/add-without-commit")).status_code == 200
    assert await _count(engine) == 0


async def test_session_is_closed_after_request_even_on_exception(
    engine: AsyncEngine,
) -> None:
    test_app = _uow_app(engine)
    transport = httpx.ASGITransport(app=test_app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        assert (await c.post("/add-without-commit")).status_code == 200
        assert (await c.post("/boom")).status_code == 500
    ok_session, boom_session = test_app.state.seen
    assert not ok_session.in_transaction()
    assert not boom_session.in_transaction()
    assert await _count(engine) == 0


async def test_engine_created_once_on_app_state() -> None:
    async with lifespan(app):
        first = app.state.db_engine
        assert app.state.db_engine is first
        assert isinstance(first, AsyncEngine)
