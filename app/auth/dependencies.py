"""Who is calling, what they may do, and the session that sees only their data.

Every route declares exactly one access level through one of the guards below
(`public_route`, `bearer_route`, `student_route`, `editor_route`); a test walks
the application's routes and fails for one that declares none. The data
sessions (`get_scoped_session`) are bound to the caller's tenant and course,
so a handler cannot reach what the caller may not see.
"""

import re
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.principal import Principal
from app.auth.tokens import hash_token
from app.core.config import Settings, get_settings
from app.core.database import get_session
from app.enums import Role
from app.exceptions import (
    ForbiddenError,
    MoodleSourceNotProvidedError,
    UnauthorizedError,
)
from app.models.tenant import LEGACY_TENANT_ID
from app.repositories.sqlalchemy.auth_repository import SqlAlchemyAuthRepository
from app.tenancy.scope import SYSTEM, Scope, bind_scope

# What `new_token` produces: URL-safe base64, 43 characters. Anything else
# cannot be one of ours, so it is refused before touching the database.
_TOKEN = re.compile(r"[A-Za-z0-9_-]{20,128}")

LEGACY_PRINCIPAL = Principal(LEGACY_TENANT_ID, None, Role.EDITOR, None)


def parse_bearer(authorization: str | None) -> str | None:
    """The token in an `Authorization: Bearer <token>` header; None if there is
    no header; `UnauthorizedError` if there is one that is not a bearer token."""
    if authorization is None:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not _TOKEN.fullmatch(token):
        raise UnauthorizedError
    return token


async def principal_for_token(session: AsyncSession, token: str) -> Principal:
    grant = await SqlAlchemyAuthRepository(session).find_grant(
        hash_token(token), datetime.now(UTC)
    )
    if grant is None:
        raise UnauthorizedError
    return Principal(grant.tenant_id, grant.course_id, grant.role, grant.sub)


async def get_bearer_principal(
    session: Annotated[AsyncSession, Depends(get_session)],
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    """The principal behind a bearer token; there is no anonymous fallback."""
    token = parse_bearer(authorization)
    if token is None:
        raise UnauthorizedError
    return await principal_for_token(session, token)


async def get_principal(
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    """The bearer token's principal, or, when `LEGACY_ANONYMOUS_MODE` is on and
    no token is sent, the legacy tenant's anonymous editor. A token that is
    sent but is not valid is refused even in legacy mode."""
    token = parse_bearer(authorization)
    if token is not None:
        return await principal_for_token(session, token)
    if settings.legacy_mode:
        return LEGACY_PRINCIPAL
    raise UnauthorizedError


PrincipalDep = Annotated[Principal, Depends(get_principal)]


# --- access levels ----------------------------------------------------------------


def public_route() -> None:
    """Declares a route anyone may call (health, the launch, an image by its
    unguessable id)."""


async def bearer_route(
    principal: Annotated[Principal, Depends(get_bearer_principal)],
) -> Principal:
    """A valid bearer token is needed; nothing else (no legacy fallback)."""
    return principal


async def student_route(principal: PrincipalDep) -> Principal:
    """Students, and editors (who have every student right)."""
    return principal


async def editor_route(principal: PrincipalDep) -> Principal:
    """Editors only; a student gets 403."""
    if principal.role is not Role.EDITOR:
        raise ForbiddenError
    return principal


GUARDS = frozenset({public_route, bearer_route, student_route, editor_route})


async def moodle_source_route(principal: PrincipalDep) -> None:
    """For the routes that answer from Moodle's web services. Only the legacy
    tenant has such a source; for a plugin tenant the per-user Moodle state
    comes from the plugin through the browser, and this service never calls
    Moodle for it."""
    if principal.tenant_id != LEGACY_TENANT_ID:
        raise MoodleSourceNotProvidedError


# --- sessions ---------------------------------------------------------------------


async def get_scoped_session(
    session: Annotated[AsyncSession, Depends(get_session)],
    principal: PrincipalDep,
) -> AsyncSession:
    """A session that sees only the principal's tenant (and course)."""
    bind_scope(session, Scope(principal.tenant_id, principal.course_id))
    return session


async def get_system_session(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AsyncSession:
    """A session with no tenant filter. Only for what is not a tenant's
    business (an image served by its unguessable id)."""
    bind_scope(session, SYSTEM)
    return session


ScopedSession = Annotated[AsyncSession, Depends(get_scoped_session)]
SystemSession = Annotated[AsyncSession, Depends(get_system_session)]
