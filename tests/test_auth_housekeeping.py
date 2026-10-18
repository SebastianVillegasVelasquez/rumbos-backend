"""Rate limiter, token helpers and the expired-row cleanup."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.rate_limit import FixedWindowLimiter, client_key
from app.auth.tokens import hash_token, new_token
from app.enums import Role
from app.models import AccessToken, UsedJti
from app.repositories.sqlalchemy.auth_repository import SqlAlchemyAuthRepository
from app.repositories.sqlalchemy.tenant_repository import SqlAlchemyTenantRepository
from app.schemas.auth import NewAccessToken
from app.scripts import purge_auth
from tests.auth_helpers import HOST_ORIGIN, ISSUER, SUB, default_key
from tests.fakes import InMemoryAuthRepository, InMemoryTenantRepository


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_the_limiter_allows_up_to_the_limit_then_says_how_long_to_wait() -> None:
    clock = Clock()
    limiter = FixedWindowLimiter(3, window_seconds=60, clock=clock)
    assert [limiter.hit("a") for _ in range(3)] == [0, 0, 0]
    wait = limiter.hit("a")
    assert 1 <= wait <= 60
    assert limiter.hit("b") == 0  # keys are independent


def test_the_limiter_resets_with_the_window() -> None:
    clock = Clock()
    limiter = FixedWindowLimiter(1, window_seconds=60, clock=clock)
    assert limiter.hit("a") == 0
    assert limiter.hit("a") > 0
    clock.now += 60
    assert limiter.hit("a") == 0


def test_the_retry_after_ends_with_the_window() -> None:
    clock = Clock()
    clock.now = 1190.0  # the window 1140-1200 ends in 10 s
    limiter = FixedWindowLimiter(1, window_seconds=60, clock=clock)
    limiter.hit("a")
    assert limiter.hit("a") == 10


def test_the_limiter_refuses_new_keys_when_full_of_live_ones() -> None:
    clock = Clock()
    limiter = FixedWindowLimiter(5, window_seconds=60, clock=clock, max_keys=2)
    assert limiter.hit("a") == 0
    assert limiter.hit("b") == 0
    assert limiter.hit("c") > 0  # no room: refused rather than unbounded
    assert limiter.hit("a") == 0  # known keys carry on
    clock.now += 60  # old windows are pruned to make room
    assert limiter.hit("c") == 0


@pytest.mark.parametrize(
    ("host", "key"),
    [
        ("203.0.113.9", "203.0.113.9"),
        ("2001:db8:1:2:3:4:5:6", "2001:db8:1:2::"),
        ("2001:db8:1:2:ffff::1", "2001:db8:1:2::"),
        ("not-an-ip", "not-an-ip"),
        (None, "unknown"),
    ],
)
def test_ipv6_clients_share_a_key_per_64(host: str | None, key: str) -> None:
    assert client_key(host) == key


def test_tokens_are_long_unique_and_hashed_deterministically() -> None:
    tokens = {new_token() for _ in range(100)}
    assert len(tokens) == 100
    sample = next(iter(tokens))
    assert len(sample) >= 43  # 256 bits in base64url
    assert hash_token(sample) == hash_token(sample)
    assert len(hash_token(sample)) == 64
    assert sample not in hash_token(sample)


def _grant(tenant_id: uuid.UUID, token: str, expires_at: datetime) -> NewAccessToken:
    return NewAccessToken(
        token_hash=hash_token(token),
        tenant_id=tenant_id,
        course_id=1,
        role=Role.STUDENT,
        sub=SUB,
        expires_at=expires_at,
    )


async def test_purge_removes_only_expired_rows(session: AsyncSession) -> None:
    tenants = SqlAlchemyTenantRepository(session)
    tenant = await tenants.create("U", ISSUER, HOST_ORIGIN, 1, default_key())
    auth = SqlAlchemyAuthRepository(session)
    now = datetime.now(UTC)
    minute = timedelta(minutes=1)
    await auth.register_launch(
        tenant.id,
        "old-jti",
        now - timedelta(seconds=1),
        _grant(tenant.id, "old", now - minute),
    )
    await auth.register_launch(
        tenant.id,
        "new-jti",
        now + minute,
        _grant(tenant.id, "new", now + 30 * minute),
    )

    lines: list[str] = []
    assert await purge_auth.run(auth, lines.append, now) == (1, 1)

    assert await session.scalar(select(func.count()).select_from(AccessToken)) == 1
    remaining = (await session.scalars(select(UsedJti.jti))).all()
    assert list(remaining) == ["new-jti"]
    assert await auth.find_grant(hash_token("new"), now) is not None
    assert await auth.find_grant(hash_token("old"), now) is None
    assert lines == ["Deleted 1 expired token(s) and 1 expired jti(s)"]


async def test_the_fake_purges_like_the_real_thing() -> None:
    tenants = InMemoryTenantRepository()
    tenant = await tenants.create("U", ISSUER, HOST_ORIGIN, 1, default_key())
    auth = InMemoryAuthRepository(tenants)
    now = datetime.now(UTC)
    gone = now - timedelta(seconds=1)
    await auth.register_launch(tenant.id, "j", gone, _grant(tenant.id, "t", gone))
    assert await auth.purge_expired(now) == (1, 1)
