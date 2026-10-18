"""Sessions for tests that look at the database directly."""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.models.tenant import LEGACY_TENANT_ID
from app.tenancy.scope import Scope, bind_scope


def new_session(
    engine: AsyncEngine, scope: Scope | None = None, **options: Any
) -> AsyncSession:
    """A session bound to `scope` (default: the legacy tenant, every course)."""
    session = AsyncSession(engine, **options)
    bind_scope(session, scope or Scope(LEGACY_TENANT_ID))
    return session
