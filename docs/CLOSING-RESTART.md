# Restart after three failed finals: a design (not built)

Status: **proposal.** Nothing here is implemented, no switch exists, no table or
column was added. Written from an audit of `feat/outcomes-activity-steps-congrats`
(from `bac6ed5`) so the product owner can decide.

The product wants: after the third failed final attempt the student sees **all the
map's content again from zero** (the map, not the welcome flow), and the check-in
stays consumed. Today the third failure ends the journey: `outcome.final.status`
is `exhausted` (see `docs/CLOSING-GATE.md`), `POST .../final/attempts` answers
`409 no_attempts_left`, and nothing resets in Moodle.

It is the riskiest piece of the set because it needs **two writes that do not
exist yet**: a new cycle of final attempts here, and a reset of the student's
completion in Moodle. The second is irreversible from this side, so by C4 it may
only follow a verified signed statement from this backend, never a message from the
browser.

## What the audit found

* An attempt is a row of `assessment_attempts` (`app/models/assessment.py`):
  `(tenant, course, sub, purpose, preview, ordinal)` is unique and `ordinal` is
  dense, so the history of a student's finals is `ordinal` 1, 2, 3...
* The limit is **counted, not stored**: `create` counts the student's attempts of
  that purpose and refuses when `used >= max_attempts`; the limit is the **current**
  `assessments.final.maxAttempts` (a copy frozen on each attempt is only history).
  There is no per-student state besides the attempts themselves (D29).
* The outbox is one row per attempt (`uq_passback_outbox_attempt`) carrying one
  signed statement of that attempt. Statements are ES256 with `kid`, `jti` is the
  idempotency key, `aud: moodle`; schema 2 adds `kind` (`attempt`, `definition`).
* Attempts of one course are not visible from another (checked: a student who
  passed in course 7 has `used: 0` in course 8).
* The SCORM gate reads completion from Moodle **live**, so a reset in Moodle closes
  the exam at once with no backend change (`scorm_incomplete`).

## (a) How a "cycle" scopes the final's attempts

Two options.

| | column `cycle` on `assessment_attempts` | separate marker table |
| --- | --- | --- |
| history | untouched: every existing row is cycle 1 by the column default | untouched |
| count | `used` = attempts of the **current** cycle | `used` = attempts after the latest marker (a time boundary, or an FK on each attempt) |
| races | the unique `(…, ordinal)` stays; a cycle is part of the attempt row, written in the same transaction as the attempt | a marker and an attempt are two writes; a time boundary is fragile with a clock |
| migration | one additive `smallint NOT NULL DEFAULT 1`; downgrade drops it | a new table, per-student rows |
| per-student state (D29) | none beyond attempts | a new per-student row |

**Recommendation: the column**, with `ordinal` left global and dense. The current
cycle is derived, never stored on its own:

* the latest final attempt's `cycle`, **if that cycle still has attempts left**;
* otherwise, with `CLOSING_RESTART_MODE=enforce` **and** the restart acknowledged
  (see (b)), `cycle + 1`;
* otherwise (mode `off`, or restart not yet acknowledged) the cycle stays as it is
  and the student is `exhausted` / `restart_pending`.

`attempts` in the API becomes the current cycle's `{used, max}`; an extra
`attempts_total` and `cycle` keep the history readable. The exports gain `cycle`
(an additive claim; the attempt schema's tolerance of extra claims must be checked
with the plugin first, see "Open questions").

A cap on cycles is needed or a student could retry for ever: `max_cycles`,
proposed default **2** (one restart), after which the student is `exhausted` for
good and an admin decides. That is a product decision.

## (b) The signed `cycle.restart` statement

A new `kind`, same signer, same `kid`, same outbox mechanics (retry, backoff,
`failed_permanent`, audit). The outbox needs a `kind` column and its unique index
becomes `(attempt_id, kind)`.

```json
{
  "iss": "rumbos-backend", "aud": "moodle", "iat": 1790000000,
  "kind": "cycle.restart",
  "jti": "<uuid5 of '<exhausting attempt id>:cycle.restart'>",
  "sub": "<student>", "tenant": "<tenant>", "course_id": 7,
  "cycle": 1, "next_cycle": 2,
  "reason": "final_exhausted",
  "exhausting_attempt": "<attempt id>",
  "scope": ["course_content"],
  "finished_at": "2026-12-01T10:00:00Z"
}
```

* **Emitted once**, in the same transaction that closes the third failed attempt
  (like the result statement), so it cannot be lost or sent twice; `jti` is derived
  from the attempt, so a retry or a resend is byte-identical.
* **`scope` is explicit and closed.** `course_content` means the completion state
  of the course's tracked activities. It must never include the welcome flow's or
  the check-in's progress: the check-in stays consumed. Whether the closing flow's
  own step progress is also reset is open (see below).
* The plugin acts only on a **verified** statement, once per `jti` (C4), and should
  reset the *completion state*, not delete SCORM attempt data or grades, and keep
  an audit row. It answers like for results (accepted, duplicate, transient,
  permanent).
* **The new cycle opens only after the statement is `sent`.** Without that, a
  reset that Moodle never applied would leave every activity complete and the SCORM
  gate would let the student straight into cycle 2 with no content redone. Until
  then the verdict is a new gate reason `restart_pending`
  (`outcome.final.status: locked`). If the statement ends `failed_permanent`, an
  operator looks (as for results) and the student stays `restart_pending`.

## (c) The switch

`CLOSING_RESTART_MODE=off|enforce`, **default `off`**, read next to
`CLOSING_GATE_MODE` and meaningful only with `CLOSING_GATE_MODE=enforce`.

| mode | behaviour |
| --- | --- |
| `off` | exactly today: the third failure is `exhausted`, `no_attempts_left`, no statement, no `cycle`. The responses keep their bytes (the new keys are only present with `enforce`, like `outcome`) |
| `enforce` | the exhausting attempt enqueues `cycle.restart`; once sent the student is in the next cycle: `locked` (`scorm_incomplete`) until the content is redone, then `required` with a fresh `attempts` |

Frontend states under `enforce`: `restart_pending` (wait, retry), then the map
again (content incomplete), then the closing flow with the final step. Under `off`
nothing changes. Rollback order: mode back to `off` first (a student already in
cycle 2 keeps their rows; the count falls back to all attempts, so they read
`exhausted` again: tell support before rolling back), then plugin, then frontend.

## (d) Students already exhausted before the feature exists

They have three closed failed finals (cycle 1) and no statement. Three options:

1. **Leave them exhausted** (simplest, nothing happens at deploy).
2. **Operator backfill (recommended if they must be restarted):** a command (like
   `app.scripts.closing`) that lists students whose cycle 1 is exhausted for a
   course, with a dry run, and enqueues the same `cycle.restart` statement for the
   exhausting attempt, for the ones the operator names. It is idempotent by `jti`
   and runs with the mode `enforce`.
3. Restart them lazily the first time they are read: **rejected.** It would reset
   completion in Moodle for students who finished long ago or in another cohort,
   without anyone choosing it.

With the mode `off` at deploy, nobody is touched.

## (e) Recommendation: do not ship it before 2026-11-30; document a manual procedure

Six days (from 2026-11-24) is not enough for a change that needs, in order: a
migration here, a new signed statement kind, the plugin's verifier and the reset
itself (an irreversible write, to be reviewed and tried on a copy of a real
course), the frontend's new states, and an end-to-end test across the three. A
rushed reset is the failure that cannot be taken back, and C4's trust path for a
second kind of statement does not exist yet. **Build it after the date, behind
`off`; until then, use a manual procedure** that works with today's code:

1. In the map's settings raise `assessments.final.maxAttempts` by the number of
   attempts to grant (for example 3 to 6) and publish. The limit is read from the
   current settings, so the student immediately has attempts left. **It applies to
   every student of the map**, which is harmless to the ones who still have
   attempts but gives all of them more: do it only when that is acceptable, and
   lower it again afterwards.
2. In Moodle, reset the student's completion for the course's tracked activities
   (Course administration, Reports, Activity completion, override the cells; or the
   course completion reset). The gate reads completion live, so the final closes
   (`scorm_incomplete`) until the student redoes the content.
3. Tell the student. Nothing is deleted on either side: the three failed attempts
   stay in the history.

## Open questions for the product owner

* How many restarts at most (`max_cycles`)?
* After a restart, does the student see the closing flow's own steps again, or only
  the map's activities?
* What is "content" for a non-SCORM activity (a page, a quiz): reset its completion
  only, or its attempts too? Only completion is proposed.
* Does the plugin's statement verifier tolerate extra claims in the attempt
  statement (`cycle`) and a new `kind`? Nothing in this repo says so (what it
  documents is the backend ignoring unknown *launch* claims), so it has to be
  checked with the plugin before the backend sends either.

## Acceptance tests for when it is built

Third failure enqueues exactly one statement (and a resend is identical); no
statement in `off`; `restart_pending` until sent, `failed_permanent` keeps it
there; cycle 2 counts from zero and history stays readable; the check-in stays
consumed; `max_cycles` ends the loop; a student who passed the final or the check-in
never restarts; a statement with a bad signature or a replayed `jti` changes
nothing in the plugin; `off` responses are byte-identical; the migration upgrades
and downgrades on a populated database.
