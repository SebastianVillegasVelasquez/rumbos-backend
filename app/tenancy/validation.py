"""Strict validation of what an operator registers for a tenant."""

import hashlib
import re
from urllib.parse import urlsplit

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

MIN_RSA_BITS = 2048
_KID = re.compile(r"[A-Za-z0-9._-]{1,128}")


class TenantInputError(ValueError):
    """An operator-supplied value is not acceptable; the message says why."""


def _split_https(value: str, what: str) -> tuple[str, str]:
    """(origin, path) of a plain https URL, or `TenantInputError`."""
    raw = value.strip()
    if any(ch.isspace() or ord(ch) < 0x20 for ch in raw):
        raise TenantInputError(f"{what} must not contain whitespace")
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError as exc:
        raise TenantInputError(f"{what} is not a valid URL") from exc
    if parts.scheme != "https":
        raise TenantInputError(f"{what} must use https")
    if not parts.hostname or parts.username or parts.password:
        raise TenantInputError(f"{what} must be a host without credentials")
    if parts.query or parts.fragment or "?" in raw or "#" in raw:
        raise TenantInputError(f"{what} must not have a query or fragment")
    host = parts.hostname.lower()
    if ":" in host:  # an IPv6 literal needs its brackets back
        host = f"[{host}]"
    origin = f"https://{host}" + (f":{port}" if port not in (None, 443) else "")
    return origin, parts.path


def normalize_issuer(value: str) -> str:
    """A Moodle `wwwroot`: https, optional sub-path, no trailing slash.

    Compared byte for byte with the assertion's `iss`, so it is stored in the
    one canonical form the plugin must send (lower-case host, default port
    dropped).
    """
    origin, path = _split_https(value, "issuer")
    if path.endswith("/"):
        raise TenantInputError("issuer must not end with a slash")
    if "//" in path or "/./" in path or "/../" in path:
        raise TenantInputError("issuer has an irregular path")
    return origin + path


def normalize_origin(value: str) -> str:
    """A browser origin (`https://host[:port]`): no path at all."""
    origin, path = _split_https(value, "host origin")
    if path not in ("", "/"):
        raise TenantInputError("host origin must not have a path")
    return origin


def validate_kid(kid: str) -> str:
    if not _KID.fullmatch(kid):
        raise TenantInputError(
            "kid must be 1-128 characters of letters, digits, '.', '_' or '-'"
        )
    return kid


def load_public_key(pem: str) -> tuple[str, str]:
    """(normalized PEM, default `kid`) of an RSA public key of at least 2048 bits.

    A private key, a certificate, another key type or a smaller key is refused.
    The default `kid` is the first 16 hex digits of the SHA-256 of the key's
    DER `SubjectPublicKeyInfo`, so both sides can compute it from the key alone.
    """
    try:
        key = serialization.load_pem_public_key(pem.encode())
    except (ValueError, UnsupportedAlgorithm) as exc:
        raise TenantInputError("not a parseable PEM public key") from exc
    if not isinstance(key, rsa.RSAPublicKey):
        raise TenantInputError("the public key must be RSA")
    if key.key_size < MIN_RSA_BITS:
        raise TenantInputError(f"the RSA key must be at least {MIN_RSA_BITS} bits")
    der = key.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    normalized = key.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    return normalized, hashlib.sha256(der).hexdigest()[:16]
