"""The golden signing vectors, built with the production signing code.

`build()` returns the vectors (result passback passed and failed, definition,
attempt) as they are kept under `contracts/signing-vectors/`. The key is derived from a
fixed number so the public half never changes; it signs nothing but these
vectors. ECDSA signatures are randomised, so a regenerated file has new `jws`
values and the same claims: what the tests pin is the claims and the verifier.

    python tools/make_signing_vectors.py          # rewrite the files
    python tools/make_signing_vectors.py --check  # fail if the claims drifted
"""

import base64
import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.assessment.engine import ScoredAttempt, score_form
from app.assessment.scoring import decimal_string, passes
from app.mirror.attempt import attempt_claims
from app.mirror.definition import build_definition, definition_hash
from app.mirror.statements import definition_claims
from app.passback.statement import claims_for, sign
from app.trainer.assessment_form import AssessmentForm
from app.trainer.language import ENGLISH
from tests.test_mirror_attempt import synthetic, synthetic_responses

DIRECTORY = Path(__file__).resolve().parent.parent / "contracts" / "signing-vectors"
KID = "vectors-1"
ISSUER = "rumbos-backend"
IAT = 1795000000  # 2026-11-18T10:13:20Z
TENANT = uuid.UUID("0190a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b")
ATTEMPT = uuid.UUID("0190a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5c")
# Another attempt, by another student: a `jti` of its own.
FAILED_ATTEMPT = uuid.UUID("0190a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5d")
FINISHED = datetime(2026, 11, 18, 10, 13, 20, tzinfo=UTC)
POLICY = {
    "pass_percent": 80,
    "pass_operator": "gt",
    "time_limit_min": 60,
    "max_attempts": 3,
    "exemption": True,
}


def private_key() -> ec.EllipticCurvePrivateKey:
    scalar = int.from_bytes(hashlib.sha256(b"rumbos signing vectors v1").digest())
    return ec.derive_private_key(scalar, ec.SECP256R1())


def private_pem() -> str:
    return (
        private_key()
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode()
    )


def _b64(number: int) -> str:
    raw = number.to_bytes(32, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def jwk() -> dict[str, str]:
    numbers = private_key().public_key().public_numbers()
    return {
        "kty": "EC",
        "crv": "P-256",
        "alg": "ES256",
        "use": "sig",
        "kid": KID,
        "x": _b64(numbers.x),
        "y": _b64(numbers.y),
    }


def _closed(
    attempt_id: uuid.UUID, sub: str, listening: int, reading: int
) -> tuple[dict[str, Any], AssessmentForm, dict[str, Any], ScoredAttempt]:
    """A closed final attempt with that many right answers per section, scored,
    judged and turned into result claims as `AssessmentService._close` does."""
    runtime = synthetic()
    responses = synthetic_responses(listening, reading)
    scored = score_form(runtime, responses, ENGLISH)
    percent = scored.total.percent
    result = {
        "percent_exact": str(percent),
        "percent": decimal_string(percent),
        "passed": passes(percent, 80, "gt"),
    }
    base = claims_for(
        attempt_id=attempt_id,
        tenant_id=TENANT,
        course_id=7,
        sub=sub,
        purpose="final",
        ordinal=1,
        form_id=runtime.form.form_id,
        content_version="0123456789abcdef",
        policy=POLICY,
        result=result,
        finished_at=FINISHED.isoformat(),
        issuer=ISSUER,
        issued_at=IAT,
    )
    return base, runtime.form, responses, scored


def claims() -> dict[str, dict[str, Any]]:
    """The claims of each statement, from the production builders."""
    base, form, responses, scored = _closed(ATTEMPT, "s-4f9d2c", 7, 9)  # 80.2
    # Exactly at the mark under `gt`: shown as "80.00" against 80, and failed.
    failed = _closed(FAILED_ATTEMPT, "s-81b7ae", 8, 8)[0]
    definition = build_definition(form)
    digest = definition_hash(definition)
    return {
        "result": base,
        "result-failed": failed,
        "definition": definition_claims(
            tenant_id=TENANT,
            form_id=form.form_id,
            content_version="0123456789abcdef",
            definition_hash=digest,
            definition=definition,
            issuer=ISSUER,
            issued_at=IAT,
        ),
        "attempt": attempt_claims(
            base,
            form=form,
            definition_hash=digest,
            status="submitted",
            responses=responses,
            scored=scored,
            include_written_text=False,
        ),
    }


DESCRIPTIONS = {
    "result": "Result passback, schema 1, a passed final: what the backend has "
    "sent since the passback was built (no `schema` claim).",
    "result-failed": "Result passback, schema 1, a failed final: exactly at the "
    "mark under `gt`, so `percent` reads as the pass mark and `passed` is false.",
    "definition": "Exam definition for the mirror, schema 1.",
    "attempt": "Attempt record for the mirror, schema 2 (a superset of the "
    "result claims).",
}


def build() -> dict[str, dict[str, Any]]:
    """Every vector: description, the signed `jws`, its header and claims."""
    pem = private_pem()
    vectors: dict[str, dict[str, Any]] = {}
    for name, payload in claims().items():
        token = sign(payload, pem, KID)
        header = json.loads(
            base64.urlsafe_b64decode(token.split(".")[0] + "==").decode()
        )
        vectors[name] = {
            "description": DESCRIPTIONS[name],
            "header": header,
            "claims": json.loads(json.dumps(payload)),
            "jws": token,
        }
    return vectors


def write() -> None:
    DIRECTORY.mkdir(parents=True, exist_ok=True)
    for name, vector in build().items():
        (DIRECTORY / f"{name}.json").write_text(
            json.dumps(vector, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    (DIRECTORY / "jwks.json").write_text(
        json.dumps({"keys": [jwk()]}, indent=2) + "\n", encoding="utf-8"
    )
