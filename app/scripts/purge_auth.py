"""Deletes expired bearer tokens and redeemed launch `jti`s.

    python -m app.scripts.purge_auth

Run it periodically (a daily cron is plenty): expired rows are already refused,
this only keeps the tables small. A `jti` is kept until its assertion could no
longer pass the time checks, so purging never reopens a replay window.
"""

import asyncio
import sys
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import create_engine
from app.repositories.protocols import AuthRepository
from app.repositories.sqlalchemy.auth_repository import SqlAlchemyAuthRepository


async def run(
    auth: AuthRepository,
    out: Callable[[str], None] = print,
    now: datetime | None = None,
) -> tuple[int, int]:
    tokens, jtis = await auth.purge_expired(now or datetime.now(UTC))
    out(f"Deleted {tokens} expired token(s) and {jtis} expired jti(s)")
    return tokens, jtis


async def _main() -> int:
    engine = create_engine(get_settings().build_database_url)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            await run(SqlAlchemyAuthRepository(session))
    finally:
        await engine.dispose()
    return 0


def main() -> int:
    return asyncio.run(_main())


if __name__ == "__main__":
    sys.exit(main())
