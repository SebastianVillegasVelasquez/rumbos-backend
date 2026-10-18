"""RSA key pairs for tests. Generating one takes a moment, so they are cached."""

from functools import cache

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


@cache
def keypair(name: str = "default", bits: int = 2048) -> tuple[str, str]:
    """(private PEM, public PEM); the same `name` and `bits` give the same pair."""
    private = rsa.generate_private_key(public_exponent=65537, key_size=bits)
    return (
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
        private.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode(),
    )
