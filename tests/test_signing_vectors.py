# ruff: noqa: F811
"""The golden signing vectors, verified without the code that signs them.

The backend signs with PyJWT (ES256). These tests check the files under
`contracts/signing-vectors/` with `cryptography`'s ECDSA primitives and plain
base64 and JSON, which is what a receiver in another language does: split the
compact JWS, rebuild the signature from the raw R||S bytes, verify it over the
signing input with the public key of the JWK. They also fail when the claims the
production code builds drift from the vectors that were handed to the plugin.
"""

import base64
import json
from pathlib import Path
from typing import Any

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from app.mirror.schemas import SCHEMAS
from app.passback.port import Outcome, SendResult
from app.passback.statement import sign
from app.passback.worker import sync_run
from app.tenancy.scope import SYSTEM
from tests import signing_vectors
from tests.sessions import new_session
from tests.test_assessment_api import (  # noqa: F401
    clock,
    draft_content_is_served,
    env,
)
from tests.test_mirror_flow import A2, configure, finish
from tests.test_passback import NOW

DIRECTORY = Path(__file__).resolve().parent.parent / "contracts" / "signing-vectors"
NAMES = ["result", "result-failed", "definition", "attempt"]
SCHEMA_OF = {
    "result": "result-statement",
    "result-failed": "result-statement",
    "definition": "definition-statement",
    "attempt": "attempt-statement",
}


def b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def public_key(jwk: dict[str, str]) -> ec.EllipticCurvePublicKey:
    assert (jwk["kty"], jwk["crv"], jwk["alg"]) == ("EC", "P-256", "ES256")
    x = int.from_bytes(b64decode(jwk["x"]), "big")
    y = int.from_bytes(b64decode(jwk["y"]), "big")
    return ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()


def verify_independently(token: str, jwks: dict[str, Any]) -> dict[str, Any]:
    """What a receiver does, with no JWT library: returns the claims or raises."""
    head, body, signature = token.split(".")
    header = json.loads(b64decode(head))
    assert header["alg"] == "ES256"
    key = next(k for k in jwks["keys"] if k["kid"] == header["kid"])
    raw = b64decode(signature)
    assert len(raw) == 64, "an ES256 JWS signature is raw R||S, 64 bytes"
    r, s = int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")
    public_key(key).verify(
        encode_dss_signature(r, s),
        f"{head}.{body}".encode("ascii"),
        ec.ECDSA(hashes.SHA256()),
    )
    claims: dict[str, Any] = json.loads(b64decode(body))
    return claims


def load(name: str) -> dict[str, Any]:
    found: dict[str, Any] = json.loads(
        (DIRECTORY / f"{name}.json").read_text(encoding="utf-8")
    )
    return found


JWKS: dict[str, Any] = json.loads((DIRECTORY / "jwks.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", NAMES)
def test_each_vector_verifies_with_the_public_key_alone(name: str) -> None:
    vector = load(name)
    assert verify_independently(vector["jws"], JWKS) == vector["claims"]
    assert vector["header"] == {"alg": "ES256", "kid": "vectors-1", "typ": "JWT"}


@pytest.mark.parametrize("name", NAMES)
def test_the_claims_are_what_the_production_code_builds(name: str) -> None:
    built = json.loads(json.dumps(signing_vectors.claims()[name]))
    assert load(name)["claims"] == built


@pytest.mark.parametrize("name", NAMES)
def test_a_fresh_signature_of_the_production_signer_verifies_the_same_way(
    name: str,
) -> None:
    payload = signing_vectors.claims()[name]
    token = sign(payload, signing_vectors.private_pem(), signing_vectors.KID)
    assert verify_independently(token, JWKS) == json.loads(json.dumps(payload))


@pytest.mark.parametrize("name", NAMES)
def test_a_changed_payload_or_another_key_is_refused(name: str) -> None:
    head, body, signature = load(name)["jws"].split(".")
    tampered = json.loads(b64decode(body))
    tampered["jti"] = "00000000-0000-4000-8000-000000000000"
    forged = base64.urlsafe_b64encode(
        json.dumps(tampered, separators=(",", ":")).encode()
    ).rstrip(b"=")
    with pytest.raises(InvalidSignature):
        verify_independently(f"{head}.{forged.decode()}.{signature}", JWKS)
    other = ec.generate_private_key(ec.SECP256R1()).public_key().public_numbers()

    def b64(n: int) -> str:
        return base64.urlsafe_b64encode(n.to_bytes(32, "big")).rstrip(b"=").decode()

    wrong = {"keys": [{**JWKS["keys"][0], "x": b64(other.x), "y": b64(other.y)}]}
    with pytest.raises(InvalidSignature):
        verify_independently(load(name)["jws"], wrong)


def test_no_statement_carries_an_expiry() -> None:
    """A row may wait in the outbox for days; `iat` is when it was created and
    must not be used to refuse it."""
    for name in NAMES:
        claims = load(name)["claims"]
        assert "exp" not in claims and "nbf" not in claims
        assert claims["aud"] == "moodle" and claims["iss"] == "rumbos-backend"
        assert isinstance(claims["iat"], int)


def test_the_statements_tell_each_other_apart() -> None:
    assert "schema" not in load("result")["claims"]
    assert (
        load("definition")["claims"]["kind"],
        load("definition")["claims"]["schema"],
    ) == ("definition", 1)
    assert (load("attempt")["claims"]["kind"], load("attempt")["claims"]["schema"]) == (
        "attempt",
        2,
    )
    result, attempt = load("result")["claims"], load("attempt")["claims"]
    assert all(attempt[k] == v for k, v in result.items())  # a superset
    assert attempt["definition_hash"] == load("definition")["claims"]["definition_hash"]


def test_the_passed_and_the_failed_result_differ_where_they_should() -> None:
    passed, failed = load("result")["claims"], load("result-failed")["claims"]
    assert passed.keys() == failed.keys()
    assert (passed["percent"], passed["passed"], passed["completes_course"]) == (
        "80.20",
        True,
        True,
    )
    assert (failed["percent"], failed["passed"], failed["completes_course"]) == (
        "80.00",
        False,
        False,
    )
    assert (failed["pass_percent"], failed["pass_operator"]) == (80, "gt")
    assert failed["jti"] != passed["jti"]
    # One attempt is sent as one of these two, so they share its `jti`.
    assert load("attempt")["claims"]["jti"] == passed["jti"]


@pytest.mark.parametrize("name", NAMES)
def test_each_vector_fits_its_schema_with_no_claim_beyond_it(name: str) -> None:
    SCHEMAS[SCHEMA_OF[name]].model_validate(load(name)["claims"])


@pytest.mark.parametrize("name", NAMES)
def test_the_readme_lists_every_claim(name: str) -> None:
    readme = (DIRECTORY / "README.md").read_text(encoding="utf-8")
    table = readme.split("## Every claim the backend sends")[1].split("\n## ")[0]
    assert not [c for c in load(name)["claims"] if f"`{c}`" not in table]


class Keeper:
    """A receiver that accepts everything and keeps the tokens as they came."""

    supports_reconcile = False

    def __init__(self) -> None:
        self.tokens: list[str] = []

    async def send(
        self, tenant_id: object, course_id: int, statement_jws: str
    ) -> SendResult:
        self.tokens.append(statement_jws)
        return SendResult(Outcome.ACCEPTED)

    async def known(self, tenant_id: object, course_id: int) -> set[str]:
        return set()


@pytest.mark.parametrize("mirrored", [False, True])
async def test_what_the_worker_really_sends_matches_the_vectors(
    env: dict[str, Any], mirrored: bool
) -> None:
    """The production path from end to end (start, submit, outbox, `sync_run`),
    signed with the vectors' key and checked without the JWT library: the same
    header and the same claim names as the files handed to the plugin."""
    if mirrored:
        await configure(env["engine"])
    await env["setup"]({"assessments": A2})
    student = await env["student"]()
    await finish(student, right=True)
    await finish(await env["student"]("other-student"), right=False)
    keeper = Keeper()
    # The exam clock is the test's: send "later" than any row it queued.
    async with new_session(env["engine"], SYSTEM) as session:
        await sync_run(
            session,
            keeper,
            signing_vectors.private_pem(),
            signing_vectors.KID,
            NOW,
            lambda _: None,
        )
    sent = [verify_independently(token, JWKS) for token in keeper.tokens]
    for token in keeper.tokens:
        assert json.loads(b64decode(token.split(".")[0])) == load("result")["header"]
    kinds = [c.get("kind", "result") for c in sent]
    attempts = [c for c in sent if c.get("kind") != "definition"]
    expected = ["attempt"] * 2 if mirrored else ["result"] * 2
    assert sorted(c.get("kind", "result") for c in attempts) == expected
    assert kinds.count("definition") == (1 if mirrored else 0)
    assert {c["passed"] for c in attempts} == {True, False}
    for claims in sent:
        name = claims.get("kind", "result")
        assert claims.keys() == load(name)["claims"].keys()
        SCHEMAS[SCHEMA_OF[name]].model_validate(claims)
