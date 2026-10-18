"""Tenants, bearer tokens and per-principal API clients for the access tests."""

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from app.auth.tokens import hash_token, new_token
from app.enums import Role, TenantStatus
from app.main import app as fastapi_app
from app.models import AccessToken, Tenant
from tests.editor_client import EditorClient
from tests.fakes import InMemoryMoodleClient

MakeTenant = Callable[..., Awaitable[uuid.UUID]]
MakeClient = Callable[..., Awaitable[EditorClient]]


async def insert_tenant(
    engine: AsyncEngine,
    name: str,
    *,
    quota: int = 10**9,
    status: TenantStatus = TenantStatus.ACTIVE,
) -> uuid.UUID:
    """A tenant row (no keys: these tests never launch)."""
    tenant_id = uuid.uuid4()
    now = datetime.now(UTC)
    async with engine.begin() as conn:
        await conn.execute(
            insert(Tenant).values(
                id=tenant_id,
                name=name,
                issuer=f"https://{name}.example.com",
                host_origin=f"https://{name}.example.com",
                status=status,
                asset_quota_bytes=quota,
                created_at=now,
                updated_at=now,
            )
        )
    return tenant_id


async def insert_token(
    engine: AsyncEngine,
    tenant_id: uuid.UUID,
    course_id: int,
    role: Role,
    *,
    ttl: timedelta = timedelta(minutes=30),
) -> str:
    """A bearer token as `/auth/launch` would have issued it."""
    token = new_token()
    now = datetime.now(UTC)
    async with engine.begin() as conn:
        await conn.execute(
            insert(AccessToken).values(
                token_hash=hash_token(token),
                tenant_id=tenant_id,
                course_id=course_id,
                role=role,
                sub="b" * 32,
                expires_at=now + ttl,
                created_at=now,
            )
        )
    return token


@pytest.fixture
async def make_client(
    engine: AsyncEngine,
) -> AsyncIterator[Callable[[str | None], EditorClient]]:
    """`make_client(token)` is an API client sending that bearer token (none:
    anonymous). Everything is closed when the test ends."""
    fastapi_app.state.db_engine = engine
    fastapi_app.state.moodle_client = InMemoryMoodleClient()
    transport = httpx.ASGITransport(app=fastapi_app)
    clients: list[EditorClient] = []

    def build(token: str | None = None) -> EditorClient:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        client = EditorClient(
            transport=transport, base_url="http://t/api/v1", headers=headers
        )
        clients.append(client)
        return client

    yield build
    async with AsyncExitStack() as stack:
        for client in clients:
            stack.push_async_callback(client.aclose)
