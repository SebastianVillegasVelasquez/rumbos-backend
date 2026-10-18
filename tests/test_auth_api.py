"""`POST /auth/launch` and `GET /auth/me` end to end, against real Postgres."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.auth.tokens import hash_token
from app.core.config import Settings, get_settings
from app.enums import TenantStatus
from app.main import app as fastapi_app
from app.models import AccessToken, Tenant, UsedJti
from app.repositories.sqlalchemy.tenant_repository import SqlAlchemyTenantRepository
from tests.api_client import ApiClient
from tests.auth_helpers import (
    HOST_ORIGIN,
    ISSUER,
    KID,
    assertion,
    claims,
    default_key,
    forged,
)
from tests.rsa_keys import keypair


@pytest.fixture
def settings_for_test() -> Settings:
    return get_settings().model_copy(update={"legacy_anonymous_mode": False})


@pytest.fixture
async def http(
    engine: AsyncEngine, settings_for_test: Settings
) -> AsyncIterator[httpx.AsyncClient]:
    fastapi_app.state.db_engine = engine
    fastapi_app.state.launch_limiters = None  # each test starts with fresh counters
    fastapi_app.dependency_overrides[get_settings] = lambda: settings_for_test
    transport = httpx.ASGITransport(app=fastapi_app)
    async with ApiClient(transport=transport, base_url="http://t/api/v1") as c:
        yield c
    fastapi_app.dependency_overrides.pop(get_settings, None)
    fastapi_app.state.launch_limiters = None


@pytest.fixture
async def tenant(session: AsyncSession) -> Tenant:
    repo = SqlAlchemyTenantRepository(session)
    record = await repo.create("Uni", ISSUER, HOST_ORIGIN, 10_000, default_key())
    row = await session.get(Tenant, record.id)
    assert row is not None
    return row


async def _launch(http: httpx.AsyncClient, token: str) -> httpx.Response:
    return await http.post("/auth/launch", json={"assertion": token})


async def test_launch_returns_a_bearer_token_that_identifies_its_holder(
    http: httpx.AsyncClient, tenant: Tenant
) -> None:
    r = await _launch(http, assertion(course=12, role="student"))

    assert r.status_code == 200
    body = r.json()
    assert set(body) == {
        "accessToken",
        "tokenType",
        "expiresIn",
        "role",
        "courseId",
        "hostOrigin",
    }
    assert body["tokenType"] == "Bearer"
    assert body["expiresIn"] == 1800
    assert (body["role"], body["courseId"]) == ("student", 12)
    assert body["hostOrigin"] == HOST_ORIGIN

    me = await http.get(
        "/auth/me", headers={"Authorization": f"Bearer {body['accessToken']}"}
    )
    assert me.status_code == 200
    assert me.json() == {"role": "student", "courseId": 12}


async def test_the_token_is_stored_only_as_a_hash(
    http: httpx.AsyncClient, tenant: Tenant, engine: AsyncEngine
) -> None:
    token = (await _launch(http, assertion())).json()["accessToken"]

    async with AsyncSession(engine) as s:
        rows = (await s.scalars(select(AccessToken))).all()
        jtis = (await s.scalars(select(UsedJti))).all()
    assert [r.token_hash for r in rows] == [hash_token(token)]
    assert token not in rows[0].token_hash
    assert rows[0].tenant_id == tenant.id
    assert len(jtis) == 1


async def test_auth_responses_are_never_cached(
    http: httpx.AsyncClient, tenant: Tenant
) -> None:
    ok = await _launch(http, assertion())
    bad = await _launch(http, "garbage")
    me = await http.get("/auth/me")
    for r in (ok, bad, me):
        assert r.headers["cache-control"] == "no-store"
        assert r.headers["pragma"] == "no-cache"
    health = await http.get("/health")
    assert "no-store" not in health.headers.get("cache-control", "")


def _bad_assertions() -> list[tuple[str, str]]:
    return [
        ("garbage", "garbage"),
        ("wrong key", assertion(key=keypair("attacker")[0])),
        ("unknown kid", assertion(kid="nope")),
        ("unknown issuer", assertion(iss="https://nobody.example.org")),
        ("wrong audience", assertion(aud="x")),
        ("expired", assertion(iat=1_000_000, exp=1_000_060)),
        ("alg none", forged({"alg": "none", "typ": "JWT", "kid": KID}, claims())),
        ("hs256", forged({"alg": "HS256", "typ": "JWT", "kid": KID}, claims(), b"k")),
        ("bad role", assertion(role="admin")),
        ("oversize", assertion(extra="x" * 5000)),
    ]


async def test_every_verification_failure_looks_the_same(
    http: httpx.AsyncClient, tenant: Tenant
) -> None:
    answers = []
    for _, token in _bad_assertions():
        r = await _launch(http, token)
        assert r.status_code == 401
        answers.append(r.json())
    assert answers[0] == {
        "detail": {
            "code": "launch_invalid",
            "message": "The launch could not be verified",
        }
    }
    assert all(a == answers[0] for a in answers)


async def test_a_replayed_assertion_is_refused(
    http: httpx.AsyncClient, tenant: Tenant
) -> None:
    token = assertion()
    assert (await _launch(http, token)).status_code == 200
    again = await _launch(http, token)
    assert again.status_code == 401
    assert again.json()["detail"]["code"] == "launch_invalid"


async def test_a_revoked_key_is_refused(
    http: httpx.AsyncClient, tenant: Tenant, session: AsyncSession
) -> None:
    await SqlAlchemyTenantRepository(session).revoke_key(tenant.id, KID)
    r = await _launch(http, assertion())
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "launch_invalid"


async def test_a_disabled_tenant_gets_403_and_loses_its_tokens(
    http: httpx.AsyncClient, tenant: Tenant, session: AsyncSession
) -> None:
    token = (await _launch(http, assertion())).json()["accessToken"]
    auth = {"Authorization": f"Bearer {token}"}
    assert (await http.get("/auth/me", headers=auth)).status_code == 200

    await SqlAlchemyTenantRepository(session).set_status(
        tenant.id, TenantStatus.DISABLED
    )

    r = await _launch(http, assertion())
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "tenant_disabled"
    assert (await http.get("/auth/me", headers=auth)).status_code == 401

    await SqlAlchemyTenantRepository(session).set_status(tenant.id, TenantStatus.ACTIVE)
    assert (await http.get("/auth/me", headers=auth)).status_code == 200


@pytest.mark.parametrize(
    "content",
    [b"", b"not json", b"[]", b'{"assertion": 5}', b"{}", b'{"assertion": null}'],
)
async def test_a_malformed_request_is_launch_invalid(
    http: httpx.AsyncClient, tenant: Tenant, content: bytes
) -> None:
    r = await http.post(
        "/auth/launch", content=content, headers={"Content-Type": "application/json"}
    )
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "launch_invalid"


async def test_an_oversize_body_is_refused_without_reading_it_all(
    http: httpx.AsyncClient, tenant: Tenant
) -> None:
    r = await http.post("/auth/launch", json={"assertion": "x" * 100_000})
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "launch_invalid"


async def test_launches_are_rate_limited_per_client(
    http: httpx.AsyncClient, tenant: Tenant, settings_for_test: Settings
) -> None:
    fastapi_app.dependency_overrides[get_settings] = lambda: (
        settings_for_test.model_copy(update={"launch_rate_per_ip": 3})
    )
    codes = [(await _launch(http, "garbage")).status_code for _ in range(5)]
    assert codes == [401, 401, 401, 429, 429]
    limited = await _launch(http, "garbage")
    assert limited.json()["detail"]["code"] == "rate_limited"
    assert int(limited.headers["retry-after"]) >= 1
    assert limited.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "Bearer",
        "Bearer ",
        "Basic dXNlcjpwYXNz",
        "Bearer short",
        "Bearer " + "x" * 200,
        "Bearer " + "x" * 43,  # well-formed but unknown
        "Token " + "x" * 43,
    ],
)
async def test_me_needs_a_known_bearer_token(
    http: httpx.AsyncClient, tenant: Tenant, header: str | None
) -> None:
    headers = {} if header is None else {"Authorization": header}
    r = await http.get("/auth/me", headers=headers)
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "unauthorized"
    assert r.headers["www-authenticate"] == "Bearer"


async def test_an_expired_token_stops_working(
    http: httpx.AsyncClient, tenant: Tenant, engine: AsyncEngine
) -> None:
    token = (await _launch(http, assertion())).json()["accessToken"]
    async with engine.begin() as conn:
        await conn.execute(
            update(AccessToken).values(
                expires_at=datetime.now(UTC) - timedelta(seconds=1)
            )
        )
    r = await http.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


async def test_the_scheme_is_case_insensitive(
    http: httpx.AsyncClient, tenant: Tenant
) -> None:
    token = (await _launch(http, assertion())).json()["accessToken"]
    r = await http.get("/auth/me", headers={"Authorization": f"bearer {token}"})
    assert r.status_code == 200
