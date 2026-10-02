from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine


def create_engine(database_url: str) -> AsyncEngine:
    """Build the process-wide engine. Called once, from the app lifespan."""
    return create_async_engine(database_url, pool_pre_ping=True)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Unit of Work: one fresh session per request.

    The engine is shared (read from `app.state`), the session is not: an
    AsyncSession must never be used by concurrent requests. Commits on
    success, rolls back on any exception, and always closes.
    """
    engine: AsyncEngine = request.app.state.db_engine
    async with AsyncSession(engine, expire_on_commit=False) as session:
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise
