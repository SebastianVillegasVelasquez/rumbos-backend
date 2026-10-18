"""Verifies a launch assertion and trades it for a bearer token.

The plugin (running inside a Moodle site) signs a short-lived JWT with the
site's private key. Here, in this order: the size is capped, the token is
parsed WITHOUT trusting it just to learn the issuer, the issuer's tenant and
the key named by `kid` are looked up, the RS256 signature is verified, the
claims are checked, and finally the `jti` is redeemed (once) and a token is
issued. Every verification failure raises `LaunchInvalidError`; the caller only
ever sees one code, while `reason` goes to the audit log.

SECURITY: the assertion and the token are never logged. Values taken from an
unverified token are logged with `repr` and truncated, so they cannot forge
log lines.
"""

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.auth.rate_limit import FixedWindowLimiter
from app.auth.tokens import hash_token, new_token
from app.enums import Role, TenantStatus
from app.exceptions import (
    LaunchInvalidError,
    LaunchRateLimitedError,
    LaunchReplayedError,
    TenantDisabledError,
)
from app.repositories.protocols import AuthRepository, TenantRepository
from app.schemas.auth import LaunchResponse, NewAccessToken

audit = logging.getLogger("rumbos.audit")

MAX_ASSERTION_BYTES = 4096
MAX_LIFETIME_SECONDS = 60
ALGORITHM = "RS256"  # the only one ever accepted

_HEADER_KEYS = frozenset({"alg", "typ", "kid"})
_KID = re.compile(r"[A-Za-z0-9._-]{1,128}")
_SUB = re.compile(r"[0-9a-f]{32}")
# At least 128 bits when hex or base64url encoded (UUIDs pass as well).
_JTI = re.compile(r"[A-Za-z0-9_-]{22,128}")
_REQUIRED_CLAIMS = ["iss", "aud", "sub", "course", "role", "iat", "exp", "jti"]
_MAX_COURSE_ID = 2**31 - 1  # the course id columns are 32-bit
_LOG_LIMIT = 200


@dataclass(frozen=True)
class LaunchSettings:
    audience: str
    token_ttl_seconds: int
    leeway_seconds: int


def _shown(value: object) -> str:
    """An untrusted value, safe to put in a log line."""
    return repr(str(value)[:_LOG_LIMIT])


def _is_int(value: object) -> bool:
    return type(value) is int  # not bool, not float


class LaunchService:
    def __init__(
        self,
        tenants: TenantRepository,
        auth: AuthRepository,
        settings: LaunchSettings,
        ip_limiter: FixedWindowLimiter,
        tenant_limiter: FixedWindowLimiter,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._tenants = tenants
        self._auth = auth
        self._settings = settings
        self._ip_limiter = ip_limiter
        self._tenant_limiter = tenant_limiter
        self._now = now

    def _count(self, client: str) -> None:
        retry = self._ip_limiter.hit(client)
        if retry:
            audit.warning("launch_rate_limited scope=ip client=%s", _shown(client))
            raise LaunchRateLimitedError(retry)

    async def reject(self, reason: str, *, client: str) -> None:
        """Counts and logs a request that never reached verification (a body
        that is not `{"assertion": "..."}`). `client` is the limiter key of the
        caller's address."""
        self._count(client)
        audit.warning("launch_rejected reason=%s client=%s", reason, _shown(client))

    async def launch(self, assertion: str, *, client: str) -> LaunchResponse:
        self._count(client)
        try:
            return await self._launch(assertion)
        except LaunchInvalidError as exc:
            audit.warning(
                "launch_rejected reason=%s client=%s", exc.reason, _shown(client)
            )
            raise

    async def _launch(self, assertion: str) -> LaunchResponse:
        if len(assertion.encode()) > MAX_ASSERTION_BYTES:
            raise LaunchInvalidError("oversize")

        # Parse without trusting anything: only to learn which tenant to ask.
        try:
            header = jwt.get_unverified_header(assertion)
            unverified: dict[str, Any] = jwt.decode(
                assertion, options={"verify_signature": False}
            )
        except jwt.PyJWTError as exc:
            raise LaunchInvalidError("malformed") from exc
        if header.get("alg") != ALGORITHM:
            raise LaunchInvalidError("alg_not_allowed")
        if header.get("typ") != "JWT" or not set(header) <= _HEADER_KEYS:
            raise LaunchInvalidError("bad_header")
        kid = header.get("kid")
        if not isinstance(kid, str) or not _KID.fullmatch(kid):
            raise LaunchInvalidError("bad_kid")
        issuer = unverified.get("iss")
        if not isinstance(issuer, str):
            raise LaunchInvalidError("bad_issuer")

        tenant = await self._tenants.get_by_issuer(issuer)
        if tenant is None:
            raise LaunchInvalidError(f"unknown_issuer iss={_shown(issuer)}")
        key_row = await self._tenants.active_key(tenant.id, kid)
        if key_row is None:
            raise LaunchInvalidError(f"unknown_or_revoked_kid tenant={tenant.id}")
        public_key = serialization.load_pem_public_key(key_row.public_key_pem.encode())
        if not isinstance(public_key, rsa.RSAPublicKey):  # registered keys are RSA
            raise LaunchInvalidError(f"bad_stored_key tenant={tenant.id}")

        claims = self._verify(assertion, public_key, tenant.issuer)
        role, course = self._check_claims(claims)

        # Only now, with a genuine assertion in hand, may the answer differ.
        if tenant.status is not TenantStatus.ACTIVE:
            audit.warning("launch_refused reason=tenant_disabled tenant=%s", tenant.id)
            raise TenantDisabledError
        retry = self._tenant_limiter.hit(str(tenant.id))
        if retry:
            audit.warning("launch_rate_limited scope=tenant tenant=%s", tenant.id)
            raise LaunchRateLimitedError(retry)

        now = self._now()
        token = new_token()
        grant = NewAccessToken(
            token_hash=hash_token(token),
            tenant_id=tenant.id,
            course_id=course,
            role=role,
            sub=claims["sub"],
            expires_at=now + timedelta(seconds=self._settings.token_ttl_seconds),
        )
        # Kept until the assertion could no longer pass the time checks.
        jti_expires = datetime.fromtimestamp(
            claims["exp"] + self._settings.leeway_seconds, UTC
        )
        try:
            await self._auth.register_launch(
                tenant.id, claims["jti"], jti_expires, grant
            )
        except LaunchReplayedError as exc:
            raise LaunchInvalidError(f"replay tenant={tenant.id}") from exc
        audit.info(
            "launch_ok tenant=%s course=%d role=%s", tenant.id, course, role.value
        )
        return LaunchResponse(
            access_token=token,
            expires_in=self._settings.token_ttl_seconds,
            role=role,
            course_id=course,
            host_origin=tenant.host_origin,
        )

    def _verify(
        self, assertion: str, key: rsa.RSAPublicKey, issuer: str
    ) -> dict[str, Any]:
        try:
            claims: dict[str, Any] = jwt.decode(
                assertion,
                key,
                algorithms=[ALGORITHM],  # pinned: never taken from the token
                audience=self._settings.audience,
                issuer=issuer,
                leeway=self._settings.leeway_seconds,
                options={"require": _REQUIRED_CLAIMS},
            )
        except jwt.ExpiredSignatureError as exc:
            raise LaunchInvalidError("expired") from exc
        except jwt.InvalidSignatureError as exc:
            raise LaunchInvalidError("bad_signature") from exc
        except jwt.InvalidAudienceError as exc:
            raise LaunchInvalidError("bad_audience") from exc
        except jwt.InvalidIssuerError as exc:
            raise LaunchInvalidError("bad_issuer") from exc
        except (jwt.ImmatureSignatureError, jwt.InvalidIssuedAtError) as exc:
            raise LaunchInvalidError("not_yet_valid") from exc
        except jwt.MissingRequiredClaimError as exc:
            raise LaunchInvalidError("missing_claim") from exc
        except jwt.PyJWTError as exc:
            raise LaunchInvalidError("invalid") from exc
        return claims

    def _check_claims(self, claims: dict[str, Any]) -> tuple[Role, int]:
        """The claims PyJWT does not check. Returns (role, course)."""
        iat, exp = claims["iat"], claims["exp"]
        if not (_is_int(iat) and _is_int(exp)):
            raise LaunchInvalidError("bad_time_claims")
        if not 0 < exp - iat <= MAX_LIFETIME_SECONDS:
            raise LaunchInvalidError("bad_lifetime")
        if not isinstance(claims["aud"], str):  # a list of audiences is not ours
            raise LaunchInvalidError("bad_audience")
        course = claims["course"]
        if not _is_int(course) or not 1 <= course <= _MAX_COURSE_ID:
            raise LaunchInvalidError("bad_course")
        try:
            role = Role(claims["role"])
        except ValueError as exc:
            raise LaunchInvalidError("bad_role") from exc
        sub, jti = claims["sub"], claims["jti"]
        if not isinstance(sub, str) or not _SUB.fullmatch(sub):
            raise LaunchInvalidError("bad_sub")
        if not isinstance(jti, str) or not _JTI.fullmatch(jti):
            raise LaunchInvalidError("bad_jti")
        return role, course
