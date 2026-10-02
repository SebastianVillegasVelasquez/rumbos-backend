from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine


def create_engine(database_url: str) -> AsyncEngine:
    """Build the process-wide engine. Called once, from the app lifespan."""
    return create_async_engine(database_url, pool_pre_ping=True)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Unit of Work: one fresh session per request.

    Only responsible for opening and closing the session. Commit/rollback
    decisions belong to the service/repository layer that knows whether
    the unit of work actually succeeded.
    """
    engine: AsyncEngine = request.app.state.db_engine
    async with AsyncSession(engine, expire_on_commit=False) as session:
        yield session
