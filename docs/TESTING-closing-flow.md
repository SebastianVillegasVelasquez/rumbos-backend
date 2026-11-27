# Testing the closing flow with a test student (S9, service side)

The one runbook for putting a test student in each state of the closing journey
and for starting again. The rules it exercises are in `CLOSING-GATE.md`. It
covers the Rumbos service; what Moodle keeps is named only where it changes what
you see.

## Read this first: use a dedicated test course

**A pass by a test student is a real pass.** The service cannot tell a test
student from any other. When the report reaches Moodle, a passing attempt can:

* **complete the course** for that student (the first pass, check-in or final),
  with whatever hangs from that: certificates, the next level's prerequisite;
* write the "Rumbos result" grade, which a later, worse attempt never lowers;
* with the plugin's content auto-complete setting on, **mark the course's SCORM
  activities complete**.

Nothing below undoes any of it. So:

* use a **dedicated test course** that gates nothing and issues no certificate;
* use a **dedicated test student**, with the `student` role. A teacher, manager or
  administrator is an `editor`: their attempts are previews, report nothing and
  never become a state (`checkin.status` stays `not_taken`);
* with `PASSBACK_SENDER=none` nothing reaches Moodle **yet**: the results wait in
  the outbox and are all sent the day a sender is configured and `sync-run` runs.
  `python -m app.scripts.passback status` counts what is waiting;
* a tenant with a mirror endpoint also sends the attempts to the plugin's mirror.

**Prefer a new test student for every run.** A new Moodle user has a new `sub`,
so nothing is left over in the service or in Moodle. Reuse one (with the reset
below) only when you must. In both cases use a private browser window: the
frontend remembers in the browser, **per map and not per student**, that it
already showed a congratulations or "attempts used" screen, and does not show it
again.

## Setting the mode

`CLOSING_GATE_MODE=off | observe | enforce` in the backend's environment, then
restart the backend. It applies to every tenant and course of that environment.
Check which one is running from `GET /trainer/closing`:

| the answer | mode |
| --- | --- |
| no `outcome` key | `off` |
| `outcome.scorm_gate.reason` is `not_checked` | `observe` |
| `enforced: true` | `enforce` |

Changing the mode changes no data: a student's attempts are read the same way in
all three, so you can build a state in one mode and look at it in another.

## The test course

1. A map with the assessments on (`checkIn` with `maxAttempts: 1`, `final` with
   `maxAttempts: 3`, `exemption.enabled: true`), a closing flow that is enabled
   and has an `assessment` step with `purpose: final`, and, to test the SCORM
   rule, activity bubbles on SCORM activities that track completion.
2. The exam forms published (or `TRAINER_SERVE_DRAFTS=true` on dev and staging).
3. **Publish the map.** Students read publications; an unpublished change does
   nothing for them. The settings come from the first published map of the course
   that has the assessments on.

### Passing and failing on purpose

There is no endpoint to force a result. Use the exam and the map's own pass rule:

* **To fail:** start the attempt and submit it without answering anything. It
  scores `0.00`, which no rule passes (`passPercent` is at least 1).
* **To pass:** set `passPercent: 1`, `passOperator: gte` on the **test course**
  and publish, then answer the multiple-choice questions and submit. Check
  `passed` in the result: a sheet of wrong answers can still score `0.00`. (Or keep
  the real rule and answer correctly.)

Each attempt keeps the rule it **started** with, so results show
`pass_percent: 1` for attempts made that way, and so does what Moodle receives.
Put the rule back and publish when you are done.

## Calling the API

The screens are enough for most checks. To look at the answers, use the access
token of the test student's own session (browser developer tools, the
`Authorization` header of any API call). It is a credential: never paste it into a
ticket, a chat or a file.

```bash
read -rs TOKEN                       # paste it; it stays out of the history
API=https://<backend host>/api/v1
curl -sS -H "Authorization: Bearer $TOKEN" "$API/trainer/closing"
# start (201, or 200 resumes the open one), then submit
curl -sS -X POST -H "Authorization: Bearer $TOKEN" \
  "$API/trainer/assessments/check_in/attempts"      # or .../final/attempts
curl -sS -X POST -H "Authorization: Bearer $TOKEN" \
  "$API/trainer/attempts/<attempt_id>/submit"
```

An attempt that is left open expires at its time limit with what was saved (on
the student's next request, or with `python -m app.scripts.assessments sweep`).

## Getting each state

Go down the list in order; each one starts where the one it names ends.

| state | how to get there |
| --- | --- |
| **A. nothing sat** | a new test student opens the course |
| **B. check-in failed** | from A, submit the check-in empty |
| **C. check-in passed** | from A (a new student, or after a reset), pass the check-in (see above) |
| **D. final failed once** | from B, submit a final empty |
| **E. final failed twice** | from D, submit another final empty |
| **F. exhausted** | from E, submit a third final empty |
| **G. final passed** | from B, D or E, pass a final |

In `enforce` a final cannot be started while the content condition is not met
(see "SCORM incomplete and complete"). To build D to G, use a test course whose
map has no required SCORM, or build them in `observe` and then switch.

### What `GET /trainer/closing` says in `observe`

`enforced: false`, `progress: null` and
`outcome.scorm_gate: {"satisfied": null, "reason": "not_checked"}` in all of them.

| state | `available` / `reason` | `checkin.status` | `final.status` | `final.attempts.used` of 3 | `final.percent` |
| --- | --- | --- | --- | --- | --- |
| A | `true` / `null` | `not_taken` | `required` | 0 | `null` |
| B | `true` / `null` | `not_passed` | `required` | 0 | `null` |
| C | `false` / `already_passed` | `passed` | `not_required_passed_checkin` | 0 | `null` |
| D | `true` / `null` | `not_passed` | `required` | 1 | `"0.00"` |
| E | `true` / `null` | `not_passed` | `required` | 2 | `"0.00"` |
| F | `false` / `no_attempts_left` | `not_passed` | `exhausted` | 3 | `"0.00"` |
| G | `false` / `already_passed` | `not_passed` | `passed` | 1 to 3 | the passing score |

All the `outcome.` prefixes are left out of the column names. In `observe`,
`required` in A and B says nothing about the content: it is not checked.

Starting a final: A, B, D, E start (`201`); C and G answer
`409 closing_not_available` with `reason: already_passed`; F answers
`409 no_attempts_left`.

After D and E, check the failure text's values: `{attempt}` 1 then 2,
`{attempts_max}` 3, `{attempts_left}` 2 then 1, `{threshold}` the result's
`pass_percent`.

### The same states in `off`

There is **no `outcome`**. `enforced: false`, `progress: null`. Look at `available`,
`reason`, `final.attempts`, `final.state` and `final.already_passed`:

| state | `available` / `reason` | `final.state` |
| --- | --- | --- |
| A, B, D, E | `true` / `null` | `available` |
| C | `false` / `already_passed` | `available` (`already_passed: true`) |
| F | `false` / `no_attempts_left` | `exhausted` |
| G | `false` / `already_passed` | `passed` |

In `off` the start is not gated: **in C a direct call still starts a final.** A
second final after G answers `409 locked`; F answers `409 no_attempts_left`.

### The same states in `enforce`

States C, F and G read as in `observe` for `checkin` and `final`, with
`enforced: true`; `scorm_gate` is `{true, "passed_checkin"}` in C and
`{null, null}` in F (the verdict stops before the content). In G it is
`{true, "passed_checkin"}` only if the check-in had passed too, which with the
exemption on cannot lead to G; so expect `{null, null}`. In A, B, D and E the
answer depends on the content:

## SCORM incomplete and complete, in each mode

**The service has no real completion provider yet.** On any deployed
environment `enforce` cannot read a student's completion, whatever Moodle says.

| mode | map with required SCORM, content **not** done | same map, content **done** in Moodle | map with **no** required SCORM |
| --- | --- | --- | --- |
| `off` | nothing is checked: final starts | identical | identical |
| `observe` | not checked: `required`, `not_checked`, final starts | identical | identical |
| `enforce`, deployed today | `locked`, `progress_unavailable` (`user_unlinked` if the launch had no `uid`); `scorm_gate {null, null}` (`null` reason); start `409 closing_not_available` | **identical: it stays closed** | `required`, `available: true`, `progress {0, 0}`, `scorm_gate {true, null}`, start `201` |
| `enforce` with a provider (not built) | `locked`, `scorm_incomplete`, `progress {completed, total}`, `scorm_gate {false, null}` | `required`, `progress {n, n}`, `scorm_gate {true, null}` | as above |

So the last row can only be exercised by the automated tests, which use a fake
provider (`tests/test_closing_api.py`, `tests/test_closing_outcome.py`). Do not
report it as tested on a deployed environment.

Marking content in Moodle still matters for what **Moodle and the frontend** show.
On a staging site the plugin has a helper for the test student (it needs
`$CFG->format_rumbos_staging_tools = true`, which a production site must never
have; it is a dry run without `--apply`):

```bash
php public/course/format/rumbos/cli/staging_completion.php \
  --course=<id> --user=<moodle user id> --state=complete          # then --apply
```

Marking `incomplete` does not hold for a SCORM the student really took (see
`CLOSING-RESTART.md`).

Two read-only reports help when `enforce` is prepared:
`python -m app.scripts.closing restricted --course <id>` (required SCORM with an
access restriction) and `python -m app.scripts.closing check-service-account`
(what the service account can read of a student's completion: see
`CLOSING-GATE.md`, "The check command").

## Resetting a test student: `assessments erase`

Run these inside the backend's environment (they use its database settings).

```bash
python -m app.scripts.tenants list                                  # the tenant id
python -m app.scripts.assessments list --tenant <tenant id> --sub <sub>
python -m app.scripts.assessments erase --tenant <tenant id> --sub <sub>
```

**Finding the `sub`.** It is a pseudonym only the plugin can compute from a Moodle
user. In the service's database, while the student's session is alive and the
launch carried the Moodle user id:

```sql
SELECT DISTINCT sub FROM access_tokens
WHERE tenant_id = '<tenant id>' AND course_id = <course id>
  AND role = 'student' AND moodle_user_id = <moodle user id>;
```

Otherwise, on the dedicated test course, take it from the latest attempts, and
make sure the times are your test's:

```sql
SELECT sub, purpose, ordinal, status, started_at FROM assessment_attempts
WHERE tenant_id = '<tenant id>' AND moodle_course_id = <course id> AND NOT preview
ORDER BY started_at DESC LIMIT 10;
```

**Always run `list` first and read the course of every line.** There is no dry run
and no undo.

### Exactly what it does

* Deletes every attempt and answer of that `sub` **in every course of the
  tenant**, not only the test course. Check-in and final, real and preview. A
  `sub` is the same person in all courses of one Moodle site: **never erase a
  person who has real results anywhere on that site.**
* Deletes the outbox rows of those attempts with them. A result **not sent yet is
  never sent**. A row already sent is deleted too, which takes nothing back from
  Moodle.
* Keeps the completion audit trail and the exam definitions queued for the mirror.
  Touches no practice-trainer data.

### What it does not do

It changes **nothing in Moodle**:

* course completion stays, with its time;
* activity completion stays, including what a check-in pass auto-completed;
* the "Rumbos result" grade stays, and a later failed run does not lower it;
* the plugin keeps its received results, its audit rows and its mirror copy;
* the closing and welcome flows' progress stays in Moodle (the plugin can reset it
  per map and user, `format_rumbos_reset_flow_progress`, for someone with
  `format/rumbos:resetflow`), so a closing flow may read as already finished.

And in the browser, the "already shown" screens stay: use a private window.

After an erase the attempts are numbered from 1 again and have new ids, so the
plugin takes their statements as new results.

**Do not use Moodle's course reset** to clean up after a test: it deletes the
data of **every** student of the course.

## Checklist for a run

1. Dedicated test course, new student, private window.
2. Mode set and confirmed from the answer.
3. Walk A, B, D, E, F with one student; C with a second; G with a third.
4. Note for each: the screen, the `GET /trainer/closing` body (no token), and the
   start's status code.
5. Restore the pass rule and publish. Set the mode back.
