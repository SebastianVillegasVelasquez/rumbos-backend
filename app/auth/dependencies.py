"""Who is calling: the bearer token, resolved to a `Principal`."""

import re
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.principal import Principal
from app.auth.tokens import hash_token
from app.core.database import get_session
from app.exceptions import UnauthorizedError
from app.repositories.sqlalchemy.auth_repository import SqlAlchemyAuthRepository

# What `new_token` produces: URL-safe base64, 43 characters. Anything else
# cannot be one of ours, so it is refused before touching the database.
_TOKEN = re.compile(r"[A-Za-z0-9_-]{20,128}")


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
