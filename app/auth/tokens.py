"""Opaque bearer tokens: 256 random bits, stored only as a SHA-256 digest."""

import hashlib
import secrets

TOKEN_BYTES = 32  # 256 bits


def new_token() -> str:
    """A fresh token (URL-safe, 43 characters). Shown to the client once."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    """What is stored and looked up. A fast hash is right here: the token is
    already 256 bits of randomness, so there is nothing to brute-force."""
    return hashlib.sha256(token.encode()).hexdigest()
