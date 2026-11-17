"""The signed statement that tells Moodle how an exam attempt ended.

Claims (JWS payload, ES256, header `kid`):

    iss, aud ("moodle"), iat
    jti            the attempt id: Moodle must accept each `jti` once, so a
                   replayed or resent statement changes nothing
    sub            the student's user reference (as in the launch assertion)
    tenant, course_id
    purpose        "check_in" | "final"
    ordinal        which attempt of that purpose
    form_id, content_version
    percent        exact result as a decimal string, e.g. "80.17"
    passed         whether the frozen policy was met
    completes_course  passed and the frozen policy had the exemption on
    pass_percent, pass_operator   the rule that was in force
    finished_at    ISO 8601, UTC

Everything comes from the attempt as it was closed (its frozen policy and
result), never from the settings of today.
"""

import uuid
from collections.abc import Mapping
from typing import Any

import jwt

AUDIENCE = "moodle"


def claims_for(
    *,
    attempt_id: uuid.UUID,
    tenant_id: uuid.UUID,
    course_id: int,
    sub: str,
    purpose: str,
    ordinal: int,
    form_id: str,
    content_version: str,
    policy: Mapping[str, Any],
    result: Mapping[str, Any],
    finished_at: str,
    issuer: str,
    issued_at: int,
) -> dict[str, Any]:
    passed = bool(result["passed"])
    return {
        "iss": issuer,
        "aud": AUDIENCE,
        "iat": issued_at,
        "jti": str(attempt_id),
        "sub": sub,
        "tenant": str(tenant_id),
        "course_id": course_id,
        "purpose": purpose,
        "ordinal": ordinal,
        "form_id": form_id,
        "content_version": content_version,
        "percent": result["percent"],
        "passed": passed,
        "completes_course": passed and bool(policy.get("exemption", True)),
        "pass_percent": policy["pass_percent"],
        "pass_operator": policy["pass_operator"],
        "finished_at": finished_at,
    }


def sign(claims: Mapping[str, Any], private_key_pem: str, key_id: str) -> str:
    return jwt.encode(
        dict(claims), private_key_pem, algorithm="ES256", headers={"kid": key_id}
    )


def verify(token: str, public_key_pem: str, issuer: str) -> dict[str, Any]:
    """What Moodle does with a statement: checks signature, audience and
    issuer, and returns the claims."""
    claims: dict[str, Any] = jwt.decode(
        token,
        public_key_pem,
        algorithms=["ES256"],
        audience=AUDIENCE,
        issuer=issuer,
    )
    return claims
