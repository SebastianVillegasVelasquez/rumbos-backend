# Contracts for the Moodle plugin

The backend tells the plugin about exams in three signed statements. The
narrative contract, the delivery rules, the privacy notes and the fields to
double-check are in `docs/EXAM-MIRROR-EXPORT.md`; this folder holds the parts a
program can read.

| Path | What | Made by |
|---|---|---|
| `schemas/result-statement.schema.json` | result passback, schema 1 | `tools/export_contracts.py` |
| `schemas/definition-statement.schema.json` | exam definition, schema 1 | same |
| `schemas/attempt-statement.schema.json` | attempt record, schema 2 | same |
| `examples/definition-b2.json` | a real definition (B2 Form A) | same |
| `examples/attempt-b2.json` | an attempt, written text off (the default) | same |
| `examples/attempt-b2-with-text.json` | the same with the tenant's text switch on | same |
| `examples/attempt-b2-truncated.json` | the cap forced the text out | same |
| `sizes.json` | measured bytes per exam | same |
| `signing-vectors/` | golden signed statements, the public key, how to verify | `tools/make_signing_vectors.py` |

The schemas describe the **claims** (the JWS payload). Unknown claims must be
ignored by a receiver; the backend's own tests refuse them so the contract never
grows by accident. Versions: a statement with no `schema` claim is the result
statement, schema 1; `definition` has `schema: 1`; `attempt` has `schema: 2`. A
breaking change gets a new number, never a silent one.

## Error semantics: an unknown definition

An attempt names its definition by `definition_hash`. If the receiver has no
definition with that hash it answers **HTTP 409 or 422 with a JSON body
`{"error": "unknown_definition"}`** and stores nothing. The backend then queues
the definition again and retries the attempt after its backoff (1 minute, doubling
to 6 hours): the attempt is never dropped. The receiver should answer success
(`{"status": "duplicate"}` is fine) to a statement whose `jti` it already holds.

## Keeping the files current

```
python tools/export_contracts.py           # rewrite
python tools/export_contracts.py --check   # fail if stale (a test runs this)
```
