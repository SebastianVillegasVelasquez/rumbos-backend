# Signing vectors

Golden examples of the three statements the backend signs for the Moodle
plugin, made with the production signing code (`app.passback.statement.sign`).
Use them to prove a verifier before it meets the real backend.

| File | Statement | Notes |
|---|---|---|
| `result.json` | result passback, schema 1 | what the passback has always carried; no `schema` claim |
| `definition.json` | exam definition, `kind: "definition"`, schema 1 | one per (tenant, form, `definition_hash`) |
| `attempt.json` | attempt record, `kind: "attempt"`, schema 2 | a superset of the result claims |
| `jwks.json` | the public key (JWK) with its `kid` | the vectors' own test key |

Each vector holds `jws` (the compact token), its decoded `header` and its
`claims`. The key is derived from a fixed number inside `tests/signing_vectors.py`
and exists only for these files. It is **not** the production key.

## Algorithm: ES256, not RS256

- JWS compact serialisation, `alg: "ES256"` (ECDSA, curve P-256, SHA-256),
  header `{"alg":"ES256","kid":"<kid>","typ":"JWT"}`.
- **The signature is the raw `R || S` form: 64 bytes, 32 for R then 32 for S,
  big-endian** (RFC 7518 section 3.4). It is not an ASN.1 DER signature. A
  verifier that wants DER (OpenSSL, PHP `openssl_verify`) must convert the 64
  bytes into `SEQUENCE { INTEGER r, INTEGER s }` first.
- The public key is a JWK: `{"kty":"EC","crv":"P-256","alg":"ES256","use":"sig",
  "kid":..., "x":..., "y":...}` with base64url, unpadded, 32-byte coordinates.
- A verifier that only knows RS256 will reject every one of these. The plugin
  must be adapted to ES256; the backend does not change its algorithm.

## How to verify

1. Split the token at the dots: `head.body.signature`. Decode the header and read
   `alg` (must be `ES256`) and `kid`.
2. Pick the key with that `kid` from the set you trust. Refuse an unknown `kid`.
3. Decode the signature (base64url). It must be exactly 64 bytes.
4. Verify ECDSA-SHA256 over the ASCII bytes of `head + "." + body`.
5. Decode the body. Require `aud == "moodle"` and `iss` equal to the issuer the
   operator configured (`PASSBACK_ISSUER`, default `rumbos-backend`).
6. Accept each `jti` once: a repeated `jti` is a duplicate, answer success.

`tests/test_signing_vectors.py` does exactly this with only the `cryptography`
package and the standard library, and also checks that a changed payload and a
different key are refused.

## Expiry and clock rules

- **No statement has `exp` or `nbf`.** A row can wait in the backend's outbox
  for as long as the plugin is down (retries back off to 6 hours and never
  stop), so an expiry would turn an outage into lost results. Replay is stopped
  by `jti`, not by time.
- `iat` is when the statement was **created** (when the attempt closed, or the
  definition was queued), not when it was last signed or sent. **Do not reject a
  statement for an old `iat`.** It is fine to reject one whose `iat` is more than
  5 minutes in the future (clock skew between servers is assumed to be under
  that).
- A resend carries the same claims, byte for byte, with a fresh signature
  (ECDSA signatures are randomised, so the token text differs and the claims do
  not).

## Keys, `kid` and rotation

- The backend signs with one key at a time: `PASSBACK_SIGNING_KEY_PEM`
  (EC P-256, PKCS#8 PEM) under the `kid` in `PASSBACK_KEY_ID` (default
  `passback-1`). The `kid` is chosen by the operator, is unique per key and is
  never reused for another key.
- Statements are **signed when they are sent**, not when they are created. A row
  that waits is signed with the key current at the moment it goes out.
- Rotation: (1) add the new public key to the plugin's trusted set, next to the
  old one; (2) change the backend's key and `kid`; (3) once the outbox has
  drained (`python -m app.scripts.passback status` shows nothing pending), remove
  the old key from the plugin. The plugin must accept several `kid`s at once.
- The public key of production is given to the plugin by the operator; this
  repository does not contain it.

## Has the real passback ever been verified end to end?

No. The sender did not exist until the mirror work (`PASSBACK_SENDER=http`), the
plugin's endpoint was never decided (D13), and the backend's own tests verified
statements only with the same library that signs them. The plugin has only been
exercised with its own development signer, and it verifies RS256. These vectors
are the first independent check of the backend's format.

## Regenerating

```
python tools/make_signing_vectors.py           # rewrite the files
python tools/make_signing_vectors.py --check   # fail if the claims drifted
```

Rewrite them only when a claim is meant to change, and tell the plugin: the
vectors are part of the contract.
