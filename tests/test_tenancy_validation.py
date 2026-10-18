import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.tenancy.validation import (
    TenantInputError,
    load_public_key,
    normalize_issuer,
    normalize_origin,
    validate_kid,
)
from tests.rsa_keys import keypair


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://moodle.example.com", "https://moodle.example.com"),
        ("  https://Moodle.Example.com:443  ", "https://moodle.example.com"),
        ("https://moodle.example.com:8443", "https://moodle.example.com:8443"),
        ("https://example.com/moodle", "https://example.com/moodle"),
    ],
)
def test_issuer_is_normalized(raw: str, expected: str) -> None:
    assert normalize_issuer(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "http://moodle.example.com",
        "moodle.example.com",
        "https://moodle.example.com/",
        "https://moodle.example.com/moodle/",
        "https://user:pw@moodle.example.com",
        "https://moodle.example.com?x=1",
        "https://moodle.example.com#frag",
        "https://moodle.example.com//moodle",
        "https://moodle.example.com/a/../b",
        "https://mood le.example.com",
        "https://moodle.example.com:99999",
        "https://",
        "",
    ],
)
def test_bad_issuers_are_refused(raw: str) -> None:
    with pytest.raises(TenantInputError):
        normalize_issuer(raw)


def test_origin_has_no_path() -> None:
    assert (
        normalize_origin("https://Moodle.example.com/") == "https://moodle.example.com"
    )
    with pytest.raises(TenantInputError):
        normalize_origin("https://moodle.example.com/moodle")
    with pytest.raises(TenantInputError):
        normalize_origin("http://moodle.example.com")


@pytest.mark.parametrize("kid", ["", "a b", "a/b", "x" * 129, "ñ"])
def test_bad_kids_are_refused(kid: str) -> None:
    with pytest.raises(TenantInputError):
        validate_kid(kid)


def test_a_good_kid_passes() -> None:
    assert validate_kid("2026-10.key_1") == "2026-10.key_1"


def test_a_2048_bit_rsa_key_is_accepted_with_a_stable_default_kid() -> None:
    _, public = keypair()
    pem, kid = load_public_key(public)
    assert pem.startswith("-----BEGIN PUBLIC KEY-----")
    assert len(kid) == 16
    assert load_public_key(public)[1] == kid


def test_a_small_rsa_key_is_refused() -> None:
    _, public = keypair("small", 1024)
    with pytest.raises(TenantInputError, match="2048"):
        load_public_key(public)


def test_a_private_key_is_refused() -> None:
    private, _ = keypair()
    with pytest.raises(TenantInputError):
        load_public_key(private)


def test_garbage_is_refused() -> None:
    with pytest.raises(TenantInputError):
        load_public_key("not a key")


def test_a_non_rsa_key_is_refused() -> None:
    ec_public = (
        ec.generate_private_key(ec.SECP256R1())
        .public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    with pytest.raises(TenantInputError, match="RSA"):
        load_public_key(ec_public)
