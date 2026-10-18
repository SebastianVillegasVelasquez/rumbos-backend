"""Launch assertions and tenants for the auth tests."""

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

import jwt

from app.schemas.tenant import NewTenantKey
from tests.rsa_keys import keypair

ISSUER = "https://moodle.example.com"
HOST_ORIGIN = "https://moodle.example.com"
KID = "kid-1"
SUB = "a" * 32
AUDIENCE = "rumbos"


def new_jti() -> str:
    return secrets.token_urlsafe(16)  # 128 bits


def claims(**overrides: Any) -> dict[str, Any]:
    """A valid set of claims, with `overrides` applied (use `None` to drop one)."""
    now = int(time.time())
    base: dict[str, Any] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": SUB,
        "course": 7,
        "role": "editor",
        "iat": now,
        "exp": now + 60,
        "jti": new_jti(),
    }
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


def assertion(
    *,
    key: str | None = None,
    kid: str | None = KID,
    algorithm: str = "RS256",
    headers: dict[str, Any] | None = None,
    **overrides: Any,
) -> str:
    """A JWS signed with `key` (default: the default test key's private half)."""
    header: dict[str, Any] = {"typ": "JWT"}
    if kid is not None:
        header["kid"] = kid
    header.update(headers or {})
    return jwt.encode(
        claims(**overrides),
        key if key is not None else keypair()[0],
        algorithm=algorithm,
        headers=header,
    )


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def forged(header: dict[str, Any], payload: dict[str, Any], secret: bytes = b"") -> str:
    """A JWS assembled by hand, for algorithms PyJWT refuses to produce with a
    public key (the classic `HS256` with the public key as the secret)."""
    signing_input = (
        _b64(json.dumps(header).encode()) + "." + _b64(json.dumps(payload).encode())
    )
    if header.get("alg") == "HS256":
        signature = hmac.new(secret, signing_input.encode(), hashlib.sha256).digest()
    else:
        signature = b""
    return signing_input + "." + _b64(signature)


def default_key() -> NewTenantKey:
    return NewTenantKey(kid=KID, public_key_pem=keypair()[1])
