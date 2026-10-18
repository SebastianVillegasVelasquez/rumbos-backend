"""Launch verification against in-memory repositories: every way an assertion
can be wrong, and that each one is rejected with its own logged reason."""

import logging
import time
from typing import Any

import pytest

from app.auth.launch import LaunchService, LaunchSettings
from app.auth.rate_limit import FixedWindowLimiter
from app.auth.tokens import hash_token
from app.enums import Role, TenantStatus
from app.exceptions import (
    LaunchInvalidError,
    LaunchRateLimitedError,
    TenantDisabledError,
)
from app.schemas.tenant import NewTenantKey, TenantRecord
from tests.auth_helpers import (
    HOST_ORIGIN,
    ISSUER,
    KID,
    SUB,
    assertion,
    claims,
    default_key,
    forged,
    new_jti,
)
from tests.fakes import InMemoryAuthRepository, InMemoryTenantRepository
from tests.rsa_keys import keypair

CLIENT = "203.0.113.9"
SETTINGS = LaunchSettings(audience="rumbos", token_ttl_seconds=1800, leeway_seconds=30)


class Env:
    def __init__(self) -> None:
        self.tenants = InMemoryTenantRepository()
        self.auth = InMemoryAuthRepository(self.tenants)
        self.tenant: TenantRecord | None = None
        self.service = self.make_service()

    def make_service(
        self, ip_limit: int = 1000, tenant_limit: int = 1000
    ) -> LaunchService:
        return LaunchService(
            self.tenants,
            self.auth,
            SETTINGS,
            FixedWindowLimiter(ip_limit),
            FixedWindowLimiter(tenant_limit),
        )

    async def register(self) -> TenantRecord:
        self.tenant = await self.tenants.create(
            "Uni", ISSUER, HOST_ORIGIN, 1000, default_key()
        )
        return self.tenant


@pytest.fixture
async def env() -> Env:
    environment = Env()
    await environment.register()
    return environment


async def _rejected(env: Env, token: str, caplog: pytest.LogCaptureFixture) -> str:
    """The audit reason an assertion is rejected with."""
    caplog.clear()
    with (
        caplog.at_level(logging.WARNING, logger="rumbos.audit"),
        pytest.raises(LaunchInvalidError) as caught,
    ):
        await env.service.launch(token, client=CLIENT)
    return caught.value.reason


async def test_a_valid_assertion_issues_a_bound_token(env: Env) -> None:
    response = await env.service.launch(
        assertion(course=12, role="student"), client=CLIENT
    )

    assert response.token_type == "Bearer"
    assert response.expires_in == 1800
    assert response.role is Role.STUDENT
    assert response.course_id == 12
    assert response.host_origin == HOST_ORIGIN
    assert len(response.access_token) >= 43  # 256 bits, URL-safe
    stored = env.auth.tokens[hash_token(response.access_token)]
    assert env.tenant is not None
    assert stored.tenant_id == env.tenant.id
    assert (stored.course_id, stored.role, stored.sub) == (12, Role.STUDENT, SUB)
    # Only the digest is kept.
    assert response.access_token not in env.auth.tokens


async def test_every_token_is_different(env: Env) -> None:
    first = await env.service.launch(assertion(), client=CLIENT)
    second = await env.service.launch(assertion(), client=CLIENT)
    assert first.access_token != second.access_token


async def test_an_assertion_cannot_be_used_twice(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    token = assertion()
    await env.service.launch(token, client=CLIENT)
    assert (await _rejected(env, token, caplog)).startswith("replay")


async def test_the_same_jti_may_be_used_by_two_tenants(env: Env) -> None:
    other = await env.tenants.create(
        "Other",
        "https://other.example.org",
        "https://other.example.org",
        1000,
        NewTenantKey(kid=KID, public_key_pem=keypair("other")[1]),
    )
    jti = new_jti()
    await env.service.launch(assertion(jti=jti), client=CLIENT)
    await env.service.launch(
        assertion(
            key=keypair("other")[0],
            jti=jti,
            iss="https://other.example.org",
        ),
        client=CLIENT,
    )
    assert other.id in {tenant_id for tenant_id, _ in env.auth.jtis}


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"kid": "other-kid"}, "unknown_or_revoked_kid"),
        ({"kid": None}, "bad_kid"),
        ({"headers": {"typ": "JWE"}}, "bad_header"),
        ({"headers": {"jwk": {"kty": "RSA"}}}, "bad_header"),
        ({"headers": {"jku": "https://evil.example/keys"}}, "bad_header"),
        ({"iss": "https://nobody.example.org"}, "unknown_issuer"),
        ({"aud": "someone-else"}, "bad_audience"),
        ({"aud": ["rumbos", "x"]}, "bad_audience"),
        ({"exp": None}, "missing_claim"),
        ({"iat": None}, "missing_claim"),
        ({"jti": None}, "missing_claim"),
        ({"sub": None}, "missing_claim"),
        ({"course": None}, "missing_claim"),
        ({"role": None}, "missing_claim"),
        ({"course": 0}, "bad_course"),
        ({"course": -3}, "bad_course"),
        ({"course": "7"}, "bad_course"),
        ({"course": 7.0}, "bad_course"),
        ({"course": True}, "bad_course"),
        ({"course": 2**31}, "bad_course"),
        ({"role": "admin"}, "bad_role"),
        ({"role": ["editor"]}, "bad_role"),
        ({"role": "Editor"}, "bad_role"),
        ({"sub": "not-hex"}, "bad_sub"),
        ({"sub": "A" * 32}, "bad_sub"),
        ({"sub": "a" * 31}, "bad_sub"),
        ({"jti": "short"}, "bad_jti"),
        ({"jti": "has space " + "x" * 30}, "bad_jti"),
    ],
)
async def test_bad_assertions_are_rejected(
    env: Env,
    caplog: pytest.LogCaptureFixture,
    overrides: dict[str, Any],
    reason: str,
) -> None:
    got = await _rejected(env, assertion(**overrides), caplog)
    assert got.startswith(reason)
    assert env.auth.tokens == {}


@pytest.mark.parametrize(
    ("iat_offset", "exp_offset", "reason"),
    [
        (-200, -140, "expired"),
        (-100, -50, "expired"),
        (300, 340, "not_yet_valid"),
        (0, 61, "bad_lifetime"),
        (0, 3600, "bad_lifetime"),
        (-30, 61 - 30, "bad_lifetime"),
        (0, 0, "bad_lifetime"),
        (0, -1, "bad_lifetime"),
    ],
)
async def test_the_time_checks(
    env: Env,
    caplog: pytest.LogCaptureFixture,
    iat_offset: int,
    exp_offset: int,
    reason: str,
) -> None:
    now = int(time.time())
    token = assertion(iat=now + iat_offset, exp=now + exp_offset)
    assert await _rejected(env, token, caplog) == reason


async def test_the_clock_skew_allowance_and_the_maximum_lifetime_are_accepted(
    env: Env,
) -> None:
    now = int(time.time())
    await env.service.launch(assertion(iat=now, exp=now + 60), client=CLIENT)
    # Issued 10 s in the future by a Moodle clock slightly ahead of ours.
    await env.service.launch(assertion(iat=now + 10, exp=now + 70), client=CLIENT)
    # Expired 10 s ago: inside the 30 s leeway.
    await env.service.launch(assertion(iat=now - 70, exp=now - 10), client=CLIENT)


async def test_a_signature_from_another_key_is_rejected(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    token = assertion(key=keypair("attacker")[0])
    assert await _rejected(env, token, caplog) == "bad_signature"


async def test_a_tampered_payload_is_rejected(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    header, _, signature = assertion(role="student").split(".", 2)[0:3]
    forged_payload = assertion(role="editor").split(".")[1]
    token = f"{header}.{forged_payload}.{signature}"
    assert await _rejected(env, token, caplog) == "bad_signature"


async def test_a_revoked_key_is_rejected(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    assert env.tenant is not None
    await env.tenants.revoke_key(env.tenant.id, KID)
    reason = await _rejected(env, assertion(), caplog)
    assert reason.startswith("unknown_or_revoked_kid")


async def test_a_rotated_in_key_works_beside_the_old_one(env: Env) -> None:
    assert env.tenant is not None
    await env.tenants.add_key(
        env.tenant.id, NewTenantKey(kid="kid-2", public_key_pem=keypair("rot")[1])
    )
    await env.service.launch(assertion(), client=CLIENT)
    await env.service.launch(
        assertion(key=keypair("rot")[0], kid="kid-2"), client=CLIENT
    )


async def test_alg_none_is_rejected(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    token = forged({"alg": "none", "typ": "JWT", "kid": KID}, claims())
    assert await _rejected(env, token, caplog) == "alg_not_allowed"


async def test_hs256_signed_with_the_public_key_is_rejected(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    secret = keypair()[1].encode()  # the classic key-confusion attack
    token = forged({"alg": "HS256", "typ": "JWT", "kid": KID}, claims(), secret)
    assert await _rejected(env, token, caplog) == "alg_not_allowed"


@pytest.mark.parametrize("alg", ["RS384", "RS512", "PS256", "ES256", "HS512", "RS256 "])
async def test_only_rs256_is_accepted(
    env: Env, caplog: pytest.LogCaptureFixture, alg: str
) -> None:
    token = forged({"alg": alg, "typ": "JWT", "kid": KID}, claims())
    assert await _rejected(env, token, caplog) == "alg_not_allowed"


@pytest.mark.parametrize(
    "token",
    ["", "abc", "a.b", "a.b.c", "....", "eyJ.eyJ.sig", "ÿþ", "null"],
)
async def test_malformed_input_is_rejected(
    env: Env, caplog: pytest.LogCaptureFixture, token: str
) -> None:
    assert await _rejected(env, token, caplog) == "malformed"


async def test_an_oversize_assertion_is_rejected_before_parsing(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    token = assertion(extra="x" * 5000)
    assert len(token) > 4096
    assert await _rejected(env, token, caplog) == "oversize"


async def test_a_disabled_tenant_is_refused_only_after_the_signature_checks(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    assert env.tenant is not None
    await env.tenants.set_status(env.tenant.id, TenantStatus.DISABLED)

    with pytest.raises(TenantDisabledError):
        await env.service.launch(assertion(), client=CLIENT)
    # An unsigned (or wrongly signed) request learns nothing about the tenant.
    forged_token = assertion(key=keypair("attacker")[0])
    assert await _rejected(env, forged_token, caplog) == "bad_signature"
    assert env.auth.tokens == {}
    assert env.auth.jtis == {}  # a refused launch does not burn the jti


async def test_ip_rate_limit() -> None:
    env = Env()
    await env.register()
    service = env.make_service(ip_limit=2)
    await service.launch(assertion(), client=CLIENT)
    await service.launch(assertion(), client=CLIENT)
    with pytest.raises(LaunchRateLimitedError) as caught:
        await service.launch(assertion(), client=CLIENT)
    assert caught.value.retry_after >= 1
    await service.launch(assertion(), client="198.51.100.1")  # other client: fine


async def test_failed_attempts_count_against_the_ip() -> None:
    env = Env()
    await env.register()
    service = env.make_service(ip_limit=2)
    for _ in range(2):
        with pytest.raises(LaunchInvalidError):
            await service.launch("garbage", client=CLIENT)
    with pytest.raises(LaunchRateLimitedError):
        await service.launch("garbage", client=CLIENT)


async def test_tenant_rate_limit_counts_only_verified_assertions() -> None:
    env = Env()
    await env.register()
    service = env.make_service(tenant_limit=1)
    # Forged requests naming the tenant cannot use up its allowance.
    for _ in range(3):
        with pytest.raises(LaunchInvalidError):
            await service.launch(assertion(key=keypair("attacker")[0]), client=CLIENT)
    await service.launch(assertion(), client=CLIENT)
    with pytest.raises(LaunchRateLimitedError):
        await service.launch(assertion(), client="198.51.100.2")


async def test_nothing_secret_reaches_the_audit_log(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    token = assertion()
    with caplog.at_level(logging.DEBUG, logger="rumbos.audit"):
        response = await env.service.launch(token, client=CLIENT)
        with pytest.raises(LaunchInvalidError):
            await env.service.launch(token, client=CLIENT)  # replay
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "launch_ok" in logged
    assert "replay" in logged
    for secret in (token, response.access_token, *token.split(".")):
        assert secret not in logged


async def test_an_untrusted_issuer_cannot_forge_log_lines(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    token = assertion(iss="https://x.example\nlaunch_ok tenant=forged")
    reason = await _rejected(env, token, caplog)
    assert reason.startswith("unknown_issuer")
    for record in caplog.records:
        assert "\n" not in record.getMessage()
