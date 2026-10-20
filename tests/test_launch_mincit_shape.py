"""A launch assertion shaped exactly like the one the Mincit Moodle site sends
(RS256, `kid` `2d2c9eb9b9c06ebd`, a lifetime of exactly 60 s, editor of course
4) is accepted, and the near misses are not. Signed with a generated key."""

import logging
import time
from typing import Any

import jwt
import pytest

from app.auth.launch import MAX_ASSERTION_BYTES, LaunchService, LaunchSettings
from app.auth.rate_limit import FixedWindowLimiter
from app.core.config import Settings
from app.enums import Role
from app.exceptions import LaunchInvalidError
from app.schemas.tenant import NewTenantKey
from app.tenancy.validation import load_public_key, normalize_issuer
from tests.fakes import InMemoryAuthRepository, InMemoryTenantRepository
from tests.rsa_keys import keypair

ISSUER = "https://academiaturismo.mincit.gov.co"
KID = "2d2c9eb9b9c06ebd"
SUB = "0123456789abcdef0123456789abcdef"
CLIENT = "203.0.113.9"
SETTINGS = LaunchSettings(audience="rumbos", token_ttl_seconds=1800, leeway_seconds=30)


def _assertion(*, key: str | None = None, iss: str = ISSUER, **overrides: Any) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": iss,
        "aud": "rumbos",
        "sub": SUB,
        "course": 4,
        "role": "editor",
        "iat": now,
        "exp": now + 60,
        "jti": "fedcba9876543210fedcba9876543210",
        **overrides,
    }
    return jwt.encode(
        claims,
        key or keypair("mincit")[0],
        algorithm="RS256",
        headers={"typ": "JWT", "kid": KID},
    )


async def _service() -> LaunchService:
    tenants = InMemoryTenantRepository()
    pem, _ = load_public_key(keypair("mincit")[1])
    await tenants.create(
        "Mincit",
        normalize_issuer(ISSUER),
        ISSUER,
        1000,
        NewTenantKey(kid=KID, public_key_pem=pem),
    )
    return LaunchService(
        tenants,
        InMemoryAuthRepository(tenants),
        SETTINGS,
        FixedWindowLimiter(1000),
        FixedWindowLimiter(1000),
    )


async def _reason(service: LaunchService, token: str) -> str:
    with pytest.raises(LaunchInvalidError) as caught:
        await service.launch(token, client=CLIENT)
    return caught.value.reason


def test_the_default_audience_is_rumbos() -> None:
    assert Settings.model_fields["auth_audience"].default == "rumbos"


async def test_the_observed_assertion_shape_is_accepted() -> None:
    token = _assertion()
    header = jwt.get_unverified_header(token)
    payload = jwt.decode(token, options={"verify_signature": False})
    assert header == {"typ": "JWT", "alg": "RS256", "kid": KID}
    assert payload["exp"] - payload["iat"] == 60

    response = await (await _service()).launch(token, client=CLIENT)

    assert response.role is Role.EDITOR
    assert response.course_id == 4
    assert response.host_origin == ISSUER


@pytest.mark.parametrize(
    ("what", "make", "reason"),
    [
        (
            "issuer with a trailing slash",
            lambda: _assertion(iss=ISSUER + "/"),
            "unknown_issuer",
        ),
        (
            "issuer in another case",
            lambda: _assertion(iss=ISSUER.upper()),
            "unknown_issuer",
        ),
        ("wrong audience", lambda: _assertion(aud="moodle"), "bad_audience"),
        (
            "key that is not the registered one",
            lambda: _assertion(key=keypair("attacker")[0]),
            "bad_signature",
        ),
        (
            "expired beyond the leeway",
            lambda: _assertion(iat=int(time.time()) - 200, exp=int(time.time()) - 140),
            "expired",
        ),
        (
            "a lifetime of 61 s",
            lambda: _assertion(iat=int(time.time()), exp=int(time.time()) + 61),
            "bad_lifetime",
        ),
        (
            "over 4 KB",
            lambda: _assertion(pad="x" * MAX_ASSERTION_BYTES),
            "oversize",
        ),
    ],
)
async def test_the_near_misses_are_rejected(what: str, make: Any, reason: str) -> None:
    service = await _service()
    assert (await _reason(service, make())).startswith(reason), what


async def test_a_replayed_jti_is_rejected() -> None:
    service = await _service()
    token = _assertion()
    await service.launch(token, client=CLIENT)
    assert (await _reason(service, token)).startswith("replay")


async def test_a_failed_launch_logs_its_reason_and_no_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = await _service()
    token = _assertion(aud="moodle")
    with caplog.at_level(logging.WARNING, logger="rumbos.audit"):
        await _reason(service, token)
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "launch_rejected reason=bad_audience" in logged
    assert all(part not in logged for part in token.split("."))
