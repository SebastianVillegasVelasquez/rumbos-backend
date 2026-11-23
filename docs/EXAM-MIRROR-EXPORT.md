# Exam mirror export: the contract

The plugin mirrors each published exam as a native Moodle quiz and records each
student's attempt in it. The backend stays the **only decider** of score and
pass/fail (strictly above the configured mark); the mirror stores what it is
told and never recomputes it.

Everything travels **from the backend to the plugin, signed, through the
existing outbox**. There is no pull endpoint and no credential for one: the
correct answers can be read nowhere except inside these signed statements.

Machine-readable parts, for the plugin agent: `contracts/README.md` (index),
`contracts/schemas/*.schema.json`, `contracts/examples/*.json`,
`contracts/sizes.json`, `contracts/signing-vectors/` (golden signed examples).

## What changed from the first proposal

| Proposal | Now |
|---|---|
| Definition by authenticated pull, signed response (option A/B) | **Push** through the outbox; no pull endpoint, no credential |
| Definition keyed by `definition_hash` only | Same, kept: one per (tenant, form, `definition_hash`). The owner asked for one per `content_version`; a pack's `content_version` moves when any file does, so identical definitions would be re-sent. See D56 |
| Written text sent, behind legal review | Behind a **per-tenant switch, off by default**; points are always sent |
| Over the cap: undefined | Over 64 KB the written text goes first (`truncated: true`); an answer is never dropped |
| Snapshot table for old definitions | The `mirror_definitions` outbox row *is* the immutable copy |
| Void attempts: "leave out" | Left out; only closed attempts are exported (D57) |
| Signing | Unchanged algorithm (ES256); golden vectors and a verification guide added |

## Switch: off by default

A tenant has `mirror_endpoint` (null) and `mirror_written_text` (false). With no
endpoint **nothing is built, queued or sent for the tenant**, and the attempt
statement is the plain result one (schema 1), byte for byte as before. A deploy
changes nothing until an operator runs:

```
python -m app.scripts.mirror configure --tenant <id|issuer> --endpoint https://moodle.example/local/rumbos/mirror.php
python -m app.scripts.mirror configure --tenant <id|issuer> --written-text on   # only after legal review
python -m app.scripts.mirror sync --tenant <id|issuer> [--form <id>] [--apply]   # dry run unless --apply
```

Only **published** forms are mirrored. A draft or reviewed form gives the plain
result statement and no definition. Preview attempts are never exported.

`PASSBACK_SENDER=http` makes the worker POST. The existing setting had only
`none` and `fake` and no endpoint (D13): the endpoint is now per tenant, and
`http` skips tenants that have none. Result statements (schema 1) of a tenant
with a mirror go to the same endpoint, so the receiver must accept a statement
with no `schema` claim as schema 1.

## Delivery

```
POST <tenant mirror endpoint>
Content-Type: application/jwt
<compact JWS: ES256, header kid, see contracts/signing-vectors/README.md>
```

| Receiver answers | The backend does |
|---|---|
| 2xx (`{"status":"accepted"}` or `"duplicate"`, or no body) | done |
| 409 or 422 with `{"error":"unknown_definition"}` | queues the definition again, retries the attempt after its backoff. **Never drops the attempt** |
| 408, 425, 429, 5xx, no answer | retry with backoff: 1 min doubling to 6 h, forever |
| any other 4xx, a redirect | parked as `failed_permanent` for an operator. Redirects are not followed |

Idempotency: every statement has a `jti` the receiver accepts once. An attempt's
`jti` is the attempt id; a definition's is fixed by (tenant, form,
`definition_hash`). A resend carries identical claims with a fresh signature.

Ordering: the definition is queued when a real attempt **starts** and sent
before the attempt. An attempt whose definition is not yet delivered waits and is
not counted as a try. If the receiver says `unknown_definition` anyway (it lost
it, or never stored it), the definition goes again first. The receiver has no
listing of what it holds, so the daily reconcile skips this sender.

## Definition statement (`kind: "definition"`, schema 1)

Claims: `iss`, `aud: "moodle"`, `iat`, `jti`, `kind`, `schema`, `tenant`,
`form_id`, `content_version`, `definition_hash`, `definition`.

`definition_hash` is the sha256 of the canonical JSON of `definition` (sorted
keys, no spaces, UTF-8 not escaped). It is the exam's identity for the mirror;
`content_version` is the whole pack's hash and is only informative. A form edited
after publication has a new hash and is sent again; attempts already made keep the
hash they were taken against.

`definition.sections[].parts[].questions[]` holds one **question per scorable
unit**: a choice item is one, a matching item is one **per pair**
(`question_id = "<item_id>:<clip index from 0>"`, `clip` from 1), a written task
is one worth 5. Per question:

- `question_id` (stable), `item_id`, `type` (`mcq`, `listen_choose`,
  `gap_choice`, `match_pair`, `form_fields`, `template_text`, `written_answers`),
  `prompt`;
- choice and matching: `options` (the places, for matching), `correct` (the id of
  the right option), `audio_id`, `gap_label`;
- written: `written` (fields, questions, template, word and character limits);
- `max_points` (1, or 5 for a written task) and `weight_points`, its **exact
  share of the 100** as a fraction string (`"5/3"`, `"2"`) plus a 6-decimal
  `weight_points_decimal` for Moodle's `maxmark`.

Left out on purpose: the rubric, its checks and vocabulary, every listening
transcript, reviewer notes. The mirror gets the points the backend awarded and no
way to recompute written ones.

**Reproducing the percentage:** `sum over questions of points_exact / max_points *
weight_points` equals `percent_exact` of the attempt, exactly (the tests do this
for matching, written, boundary values and a version change). Moodle marks keep
few decimals, so **take the grade from `percent_exact`, not from the sum of rounded
marks**, and take pass/fail from `passed`. Moodle's own "grade to pass" is `>=`;
ours is strictly above (80.00 fails at 80): never use Moodle's.

## Attempt statement (`kind: "attempt"`, schema 2)

The schema 1 result claims, **unchanged**, plus:

| Claim | Meaning |
|---|---|
| `schema: 2`, `kind: "attempt"` | |
| `status` | `submitted` or `expired` (closed with what was saved) |
| `definition_hash` | which definition it was taken against |
| `percent_exact` | exact fraction of 100, e.g. `"401/5"`; the grade |
| `sections[]` | `key`, `points_exact`, `max`, `weight`, `percent_exact` |
| `answers[]` | per objective question: `question_id`, `answer` (**source** option id, or null), `correct`, `points_exact`, `max_points` |
| `written[]` | per written task: `question_id`, `points_exact` (the rubric's), `max_points`, `text` (field id to text, or null) |
| `written_text_included` | false unless the tenant's switch is on |
| `truncated` | true only if the cap forced the text out |

Every question of the definition appears, answered or not. Frozen when the
attempt closes: later content changes never rewrite it.

Not sent: names, emails, addresses, timings, shuffle seeds, the student's
opaque option ids. `sub` is the same pseudonymous reference the result statement
has always carried.

### Size

Measured on the three exams we have (canonical JSON bytes; the signed token is
about a third larger). Cap per statement: **64 KB**.

| Exam | Definition | Attempt, no text | Attempt, worst case* |
|---|---|---|---|
| A1 Form A | 22,229 | 5,922 | 22,807 |
| A2 Form A | 20,914 | 5,490 | 12,678 |
| B2 Form A | 25,135 | 5,488 | 21,101 |

\* every written answer filled to its limit with four-byte characters. The
largest attempt (A1) is a third of the cap. **Truncation cannot happen for any
current exam**; it would take a form about three times larger, or a raise of the
per-answer limits. The result statement alone is about 0.6 KB.

### Privacy: for legal review before the text switch is turned on

- The text a student typed in Writing and in the written Speaking tasks can hold
  personal data. It leaves the backend for the first time only when the tenant's
  switch is on. Off by default; with it off the mirror gets rubric points and
  `written_text_included: false`, and shows the mark without the response.
- **The correct answers will be stored in Moodle's question bank and are readable
  by anyone with question-bank access in that course.** That is inherent to
  mirroring the exam as a native quiz, and it is the client's requirement. The
  exam is only as secret as the course's roles.
- There is no way yet to tell the mirror to erase a student's text: an erasure
  request has to be handled in Moodle as well as here.

## Edge cases

| Case | Behaviour |
|---|---|
| Exactly at the mark | `passed` is decided here on the exact fraction with the frozen operator (`gt`: 80 fails, 80.2 passes, 79.8 fails). Tested at all three |
| Form edited after publication | new `definition_hash`, sent again; old attempts keep theirs |
| Preview attempt | never queued, so never exported |
| Void / cancel | does not exist and is not exported (D57) |
| Restarted attempt | a new attempt, its own `jti`; every closed one is exported |
| Expired attempt | exported with `status: "expired"` |
| Unknown definition | see Delivery |
| Tenant without a mirror | plain schema 1 result statement; nothing else |

## Fields to double-check against the plugin's provisional fixtures

The plugin's fixtures were modelled on the B2 Form A. Check these first:

1. `question_id` of a matching pair: `<item_id>:<clip index from 0>`; `clip` is
   from 1.
2. `correct` and `answer` are **source** option ids (the ids in `options`), never
   the opaque ids the student saw.
3. Points and percentages are **strings holding exact fractions** (`"17/4"`,
   `"401/5"`), not floats. `weight_points_decimal` is the only decimal.
4. `written[].points_exact` is out of `max_points: 5`, a fraction.
5. `status` is `submitted` or `expired`; there is no `voided`.
6. A schema 1 result statement has no `schema` claim; an attempt has `schema: 2`.
7. The HTTP contract in Delivery (content type, status codes, the
   `unknown_definition` body) is the backend's proposal: confirm it against
   `mirror.php`.
8. There is no `exp`; do not refuse an old `iat`.

## Deploy order

1. **Backend first**, with `PASSBACK_SENDER=none` and no tenant endpoint: it
   changes nothing (a migration adds two tenant columns and one table).
2. Give the plugin the contract and the signing vectors; it verifies ES256.
3. Set `PASSBACK_SIGNING_KEY_PEM`, `PASSBACK_KEY_ID`, then
   `PASSBACK_SENDER=http`.
4. `mirror configure --endpoint` for a pilot tenant, then `mirror sync --apply`.
5. Only after legal review: `--written-text on`.
