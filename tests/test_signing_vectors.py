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

from app.passback.statement import sign
from tests import signing_vectors

DIRECTORY = Path(__file__).resolve().parent.parent / "contracts" / "signing-vectors"
NAMES = ["result", "definition", "attempt"]


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
