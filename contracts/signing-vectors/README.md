# Signing vectors

Golden examples of the three statements the backend signs for the Moodle
plugin. Their claims come from the production builders and they are signed by the
production signer (`app.passback.statement.sign`).
Use them to prove a verifier before it meets the real backend.

| File | Statement | Notes |
|---|---|---|
| `result.json` | result passback, schema 1, **passed** | what the passback has always carried; no `schema` claim |
| `result-failed.json` | result passback, schema 1, **failed** | exactly 80 under `gt`: `percent` is `"80.00"`, `pass_percent` 80, `passed` false |
| `definition.json` | exam definition, `kind: "definition"`, schema 1 | one per (tenant, form, `definition_hash`) |
| `attempt.json` | attempt record, `kind: "attempt"`, schema 2 | a superset of the result claims |
| `jwks.json` | the public key (JWK) with its `kid` | the vectors' own test key |

Each vector holds `jws` (the compact token), its decoded `header` and its
`claims`. The key is derived from a fixed number inside `tests/signing_vectors.py`
and exists only for these files. It is **not** the production key.

`attempt.json` is the same attempt as `result.json`, so they share a `jti`: for a
real attempt the backend sends **one** of the two, never both. A receiver that
loads both into one replay store will answer "duplicate" to the second.

## Delivery, and which statement arrives

`POST` to the tenant's mirror endpoint, `Content-Type: application/jwt`, the body
is the compact JWS and nothing else. Answers are in `../README.md` and
`docs/EXAM-MIRROR-EXPORT.md`. With the real sender (`PASSBACK_SENDER=http`) only
tenants that have an endpoint are sent to:

- an attempt closed while the tenant's mirror was on, on a published form, goes
  as an **attempt** record (schema 2), after its **definition**;
- an attempt closed while the mirror was off (its row waits until the tenant has
  an endpoint) or on an unpublished form goes as a plain **result** (schema 1).

So a receiver needs both. Preview attempts are never sent.

## Every claim the backend sends

No other claim is sent: `../schemas/` refuse unknown ones and a test holds the
vectors to them. (A receiver should still ignore a claim it does not know.)

Top level. R = result (passed or failed), A = attempt, D = definition.

| Claim | In | Type | Meaning |
|---|---|---|---|
| `iss` | R A D | string | the configured `PASSBACK_ISSUER` |
| `aud` | R A D | string | always `"moodle"` |
| `iat` | R A D | integer | Unix seconds when the statement was created |
| `jti` | R A D | string | R and A: the attempt id. D: a UUID fixed by (tenant, form, `definition_hash`) |
| `tenant` | R A D | string | tenant UUID |
| `form_id` | R A D | string | exam form |
| `content_version` | R A D | string | version of the content pack |
| `sub` | R A | string | the student's reference, as in the launch assertion |
| `course_id` | R A | integer | Moodle course id |
| `purpose` | R A | string | `check_in` or `final` |
| `ordinal` | R A | integer | attempt number for that purpose; unique, not a count |
| `percent` | R A | string | two decimals, **for display only** |
| `passed` | R A | boolean | the verdict; never recompute it from `percent` |
| `completes_course` | R A | boolean | `passed`, and the exemption setting was on when the attempt started; this applies to either `purpose`, so a passed `final` with the exemption off carries `false` |
| `pass_percent` | R A | integer | pass mark the attempt was graded with |
| `pass_operator` | R A | string | `gt` or `gte` |
| `finished_at` | R A | string | ISO 8601, UTC |
| `kind` | A D | string | `attempt` or `definition`; a result has none |
| `schema` | A D | integer | attempt 2, definition 1; a result has none |
| `definition_hash` | A D | string | sha256 of the canonical definition |
| `status` | A | string | `submitted` or `expired` |
| `percent_exact` | A | string | exact fraction of 100, e.g. `401/5` |
| `sections` | A | array | `key`, `points_exact`, `max`, `weight`, `percent_exact` |
| `answers` | A | array | `question_id`, `answer` (source option id or null), `correct`, `points_exact`, `max_points` |
| `written` | A | array | `question_id`, `points_exact`, `max_points`, `text` (null unless the tenant allows text) |
| `written_text_included` | A | boolean | whether `written[].text` was allowed to travel |
| `truncated` | A | boolean | the 64 KB cap forced the text out |
| `definition` | D | object | the exam, below. **It contains the answer key** |

Inside `definition` (types in `../schemas/definition-statement.schema.json`):

- the object: `schema`, `form_id`, `purpose`, `language`, `level`, `title`, `sections`
- a section: `key`, `title`, `instructions`, `weight`, `delivery`, `max_points`, `parts`
- a part: `part_key`, `title`, `instructions`, `shared` (null, or `kind`, `title`, `text`, `image_id`, `alt_text`), `questions`
- a question: `question_id`, `item_id`, `type`, `prompt`, `max_points`, `weight_points`, `weight_points_decimal`, and by type `options` (`id`, `text`), `correct`, `audio_id`, `gap_label`, `clip`, `written` (`fields` and `questions` as `id` and `text`, `template`, `min_words`, `max_words`, `max_chars`)

The header has `alg`, `kid` and `typ` (`"JWT"`).

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

`tests/test_signing_vectors.py` verifies without the JWT library that signs:
base64 and JSON from the standard library and the ECDSA primitive of
`cryptography`. It checks steps 1, 3, 4 and the claims of step 5, that a changed
payload and a different key are refused, and that tokens produced by the real
path (an exam submitted, queued and sent by the worker) verify the same way and
carry the same claim names. Step 2's refusal and step 6 are the receiver's.

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
