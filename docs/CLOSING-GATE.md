# The closing gate: the final exam waits for the SCORM content

The final exam is a step of the closing flow (`closing`, see `WELCOME-FLOW.md`),
not an entry of the trainer. A student reaches it only after completing the
SCORM content shown on the map. The rule is enforced by the backend, so calling
the API directly gets the same answer as the screen.

Status: **phase 1 + the user id.** The rule, the endpoint, the guard, the
editor's trial run and the signed Moodle user id (`uid`) are built and tested
against a fake completion provider. The real read of Moodle's completion is
**not** built (see "Before the real provider").

## Switch

`CLOSING_GATE_MODE` (environment):

| value | behaviour |
| --- | --- |
| `off` (default) | exactly as before the gate: a final attempt starts without any check, the final stays in `GET /trainer/assessments`. Startup and every final start log a warning (`rumbos.closing`, `CLOSING_GATE_MODE=off`) |
| `enforce` | the gate runs at every start of a final attempt; the final leaves the trainer list |

`enforce` with no completion provider keeps the exam **closed** for students
(`progress_unavailable`). Do not turn it on in production until the provider
exists. Editors in preview are not affected (they skip the SCORM condition).

## The rule (`app/closing/gate.py`, the only place)

`decide(...)` answers for both calls. In order:

1. `not_enabled`: assessments off, no `final` policy, or no enabled closing flow
2. `no_final_step`: the closing flow has no `assessment` step with `purpose: final`
3. `form_unavailable`: the final form is not published (an editor sees drafts)
4. editor in preview: **available**, nothing else is checked
5. `already_passed`: passed the final, or the check-in while `exemption.enabled`
6. `no_attempts_left`
7. SCORM: `progress_unavailable` if Moodle could not be read (closed, never open);
   `user_unlinked` if there is SCORM to check but the launch carried no Moodle
   user id (see "The Moodle user id"); `scorm_incomplete` with `completed` /
   `total`; otherwise available

Required SCORM content = the **activity bubbles of the map that holds the final
exam** that point at a module with `modname: scorm`, are visible to students
(section and module visible, not a label or a link-less module) and that Moodle
tracks for completion (`completion != 0`). Hidden, deleted, non-SCORM and
untracked ones do not count. The required set is built from those bubbles
only, and only over that set is an activity that Moodle's answer omits counted
as **not completed**; an activity that is not in the set never blocks, whatever
Moodle says about it. A map
with **no** required SCORM opens the exam at once (logged). Non-SCORM bubbles
never block. To widen the rule change `REQUIRED_MODNAMES`.

### Open question: restricted activities (not decided)

An activity hidden from students by its own `visible` flag, or whose section is
hidden, is out of the set. One that is only **restricted** (`availability`:
group, grouping, date, completion, ...) looks visible to the service account,
which sees everything, so it is in the set and **blocks** a student who can
never open it (typical: a group or grouping restriction). The backend cannot
tell these apart without a per-student view of the course. Run the report and
decide per case (exclude it, or fix the course):

```
python -m app.scripts.closing restricted --course <moodle course id>
```

It lists the required SCORM that carry a restriction, the kinds of condition,
and flags group and grouping ones. Read-only.

## The Moodle user id

The plugin signs `uid`, an integer, optional, in the launch assertion. The
backend keeps it only on the short-lived access token (`access_tokens.
moodle_user_id`, expired rows are purged with the token), never returns it and
never logs it (it is out of the repr of the principal and the caller). A
missing `uid` is normal for an older plugin; one that is not an integer in
`1..2147483647` is ignored with a log line that does not carry its value.
Claims the backend does not know are not refused (PyJWT validates only the
required ones), so a plugin may add claims before the backend learns them.

With `CLOSING_GATE_MODE=enforce`, SCORM to check and no `uid`, the exam is
closed with reason `user_unlinked` and the message "Your Moodle account could
not be identified in this session. Open the course again from Moodle; if it
keeps happening, contact support." (`message_key`
`closing.not_available.user_unlinked`). A map with no required SCORM does not
need the id.

## `GET /trainer/closing`

Student or editor in preview. Always 200 once authenticated.

```
{
  "enforced": true,          false while CLOSING_GATE_MODE=off (progress is then null)
  "preview": false,          true for an editor: a start is a trial run
  "available": false,
  "reason": "scorm_incomplete",   null when available; see the rule above
  "progress": {"completed": 1, "total": 2},   null unless it was computed
  "final": { ...exam list item... } | null    state, attempts, already_passed, best
}
```

`progress` counts are safe to show. Which activities are missing is never told.
`final` has the shape of an item of `GET /trainer/assessments`.

### `outcome`: the student's journey (enforce only)

While `CLOSING_GATE_MODE=enforce` the answer also carries `outcome`, read from the
student's attempts on every call (the backend stores no journey state, D29):

```
"outcome": {
  "checkin": {
    "status": "not_taken" | "passed" | "not_passed",
    "percent": "80.17" | null,       score of the deciding attempt (display, 2 decimals)
    "pass_percent": 80, "pass_operator": "gt" | "gte",
    "attempts": {"used": 1, "max": 1}
  } | null,                          null: the map has no check-in
  "final": {
    "status": "required" | "locked" | "not_required_passed_checkin" | "passed" | "exhausted",
    "reason": null | "scorm_incomplete" | "progress_unavailable" | "user_unlinked" |
              "form_unavailable" | "not_enabled" | "no_final_step",   only when locked
    "attempts": {"used": 0, "max": 3} | null
  },
  "scorm_gate": {"satisfied": true | false | null, "reason": "passed_checkin" | null}
}
```

With `off` the `outcome` key is absent and the answer is byte-for-byte what it was
before this existed (pinned by a test that compares bytes). The existing `final`
(the exam's list item), `available`, `reason` and `progress` are unchanged.

| the student | `checkin.status` | `final.status` | `scorm_gate` | `available` / `reason` |
| --- | --- | --- | --- | --- |
| has sat nothing | `not_taken` | `locked` (`scorm_incomplete`) until the content is done, then `required` | `false`, then `true` | same as `final.status` says |
| passed the check-in | `passed` | `not_required_passed_checkin` | `true`, reason `passed_checkin` | `false` / `already_passed` (unchanged) |
| did not pass the check-in | `not_passed` | `locked`, then `required` | as above | as above |
| passed the final | as it was | `passed` | as it was | `false` / `already_passed` |
| failed every final attempt | as it was | `exhausted` | as it was | `false` / `no_attempts_left` |

How the frontend reads it (product rules C3):

1. **Check-in passed** (`final.status == "not_required_passed_checkin"`): show the
   congratulations screen, then the closing flow **without** the final-exam step.
   The plugin marks every activity of the course complete from the signed
   statement of the passing attempt (see "What the plugin acts on"); the
   frontend never asks for that.
2. **Check-in not passed**: the student works through the map; when the content is
   done `final.status` is `required` and the closing flow opens **with** the
   final-exam step.
3. **Final passed**: `final.status == "passed"`: congratulations.
4. **Final exhausted**: `final.status == "exhausted"`. What happens next is not
   built: see `docs/CLOSING-RESTART.md`.

Rules worth knowing:

* `not_passed` is reported as soon as an attempt is closed without a pass, even if
  the map allows more check-in attempts; `attempts` says how many are left. An
  attempt still in progress is `not_taken`.
* `pass_percent` / `pass_operator` are the rule the deciding attempt was **graded
  with** (the passing one, else the best), frozen with the attempt: tightening the
  rule later does not change what a student was told. Never recompute the verdict
  from `percent` (rounded); read `status`.
* A passed check-in exempts the final only while `assessments.exemption.enabled`.
  With the exemption off the final stays `required`/`locked` as for anybody.
* An editor in preview sees `not_taken` and a `required`/`locked` final: a trial
  run is not a result.
* A passed check-in decides the status even while the final form is not published;
  it also skips the Moodle read (no completion is needed to say "not required").

## The congratulations text

`MapSettings.congratulations` (optional) holds what the congratulations screen
says, on the Rumbos card the frontend already has. One object per exam: `checkin`
(shown after a passed check-in) and `final` (after a passed final), each with
`title`, `body` and `ctaLabel`:

```json
"congratulations": {
  "checkin": {"title": "¡Lo lograste!", "body": "Sacaste {score} y el mínimo era {threshold}.", "ctaLabel": "Continuar"},
  "final": null
}
```

| rule | value |
| --- | --- |
| `title` | one line, up to 80 characters |
| `body` | up to 600 characters, line breaks allowed |
| `ctaLabel` | one line, up to 40 characters |
| markup | plain text only: `<` and `>` are refused |
| placeholders | `{score}` and `{threshold}`, nothing else: any other `{` or `}` is refused |
| `null` | the whole object, one exam, or one field: the frontend's default copy |

Texts are cleaned like every other text of the settings (normalised, control and
zero-width characters dropped, ends trimmed) **before** they are checked and
stored, so a hidden character cannot split a placeholder to slip past; a text
that ends up empty is `null`. Unset, the key is left out of the stored settings, so
no stored map or publication changes its hash.

Refusals are `422 trainer_settings_invalid` with the usual `errors[{path, rule,
message_key}]` and the same details in `problems[]`; no value is echoed. Paths are
`congratulations.checkin.title`, `.body`, `.cta_label`; rules (`message_key`
`trainer_settings.<rule>`): `congratulations_too_long`,
`congratulations_markup_not_allowed`, `congratulations_placeholder_unknown`. A
shape error (an unknown field such as `html`, a non-string) is the generic
validation 422. A stored text always loads, whatever the rules become.

**What the frontend does with it:** show it as **text** (never as HTML), and fill
the two placeholders itself, in one pass of plain string replacement over exactly
`{score}` and `{threshold}` and nothing else: `{score}` is the score it already
has (`outcome.checkin.percent`, or the result of the final attempt) and
`{threshold}` is `outcome.checkin.pass_percent` (the rule the attempt was graded
with). The backend never interpolates, formats or evaluates a text.

## What the plugin acts on (C4)

The only thing that may cause a write to a student's Moodle completion is a
**verified signed statement** from this backend, never anything the browser says.
The statement of every closed attempt (`app/passback/statement.py`, ES256, through
the existing outbox) already carries what the plugin needs to decide "mark all
the content complete":

| claim | use |
| --- | --- |
| `purpose` | `check_in` or `final` |
| `passed` | the frozen pass rule was met |
| `completes_course` | `passed` and the frozen policy had the exemption on |
| `jti` | the attempt id: accept each once (a resent statement changes nothing) |
| `sub`, `tenant`, `course_id` | whose course to complete |

So a passing `check_in` statement with `completes_course: true` is the signal to
mark the content complete, and the plugin must refuse to do it for anything else
(an unsigned message, a failed attempt, a `jti` already applied). A forged
frontend message that asked for completion would be a self-service completion
exploit. No endpoint is added for this and no claim was added: the statement is
unchanged, so the signing vectors and the plugin's verifier keep working.

## `POST /trainer/assessments/final/attempts`

Runs the same check first (`enforce` only). Refusals:

* `409 {"detail": {"code": "closing_not_available", "message_key":
  "closing.not_available.<reason>", "message", "reason", "completed"?, "total"?}}`
* `409 {"detail": {"code": "no_attempts_left"}}`: the existing code, unchanged

The check-in is not gated. Resuming an open final attempt passes through the
gate too. Reading sections, saving and submitting an attempt are not re-checked
(an attempt exists only if its start passed); an attempt opened while the gate
was off can still be finished and expires with its time limit.

## Editor's trial run

An editor in preview can run the whole closing flow and its final-exam step.
The attempt is stored with `preview: true` (as for the check-in) and the
response of start and result carries `"preview": true`. It never counts toward
a student's attempts (`max_attempts` is not applied, reads filter by `preview`),
never writes a row in `passback_outbox` (`sync.state` is `not_required`), is
left out of `assessments export`, counted apart by `assessments stats`, and deleted by
`app.scripts.assessments purge-preview` (cron, 24 h). An editor cannot read a
student's attempt, nor the reverse.

## Freshness

* The module list (which bubbles are SCORM and visible) comes from the shared
  course-contents cache: at most `MOODLE_CONTENTS_TTL_SECONDS` (60 s) old, and
  up to `MOODLE_STALE_MAX_SECONDS` only while Moodle is down. It describes the
  course, not the student.
* The student's completion is **never cached**: it is read at every call. A
  student who has just finished the last SCORM is let in on the next call, and a
  revoked completion closes the exam on the next call.
* If either read fails the exam stays closed (`progress_unavailable`, retry).

## Before the real provider (not built)

`CompletionProvider.completed_cmids(course_id, sub)` is the port
(`app/closing/providers.py`). The real implementation needs two things that do
not exist yet:

1. **A Moodle user id.** `sub` is a pseudonymous 32-hex id; Moodle cannot be
   asked about it. The plugin has to sign one more claim in the launch
   assertion (working name `uid`, the Moodle user id). Nothing in the backend
   reads it until the plugin signs it, and it must never reach a log.
2. **Proof that the service account may read other users' completion.** Run this
   once against a real site with a test student, before the provider is built.

```bash
# Moodle web service call, from any machine that can reach the site.
# MOODLE_URL is the backend's MOODLE_BASE_URL; the token is the backend's
# MOODLE_SERVICE_TOKEN (typed, not kept in shell history).
read -rs WSTOKEN; export WSTOKEN
MOODLE_URL=https://your-moodle.example
COURSE_ID=<course id>            # a course with at least one tracked SCORM
USER_ID=<Moodle id of the test student, enrolled in that course>

# (a) the function is enabled for the service: prints True
curl -sS "$MOODLE_URL/webservice/rest/server.php" \
  --data-urlencode "wstoken=$WSTOKEN" \
  --data-urlencode "wsfunction=core_webservice_get_site_info" \
  --data-urlencode "moodlewsrestformat=json" |
  python -c "import json,sys; print('core_completion_get_activities_completion_status' in [f['name'] for f in json.load(sys.stdin)['functions']])"

# (b) read the test student's completion
curl -sS "$MOODLE_URL/webservice/rest/server.php" \
  --data-urlencode "wstoken=$WSTOKEN" \
  --data-urlencode "wsfunction=core_completion_get_activities_completion_status" \
  --data-urlencode "moodlewsrestformat=json" \
  --data-urlencode "courseid=$COURSE_ID" \
  --data-urlencode "userid=$USER_ID"
```

What has to be true (all four) before the provider is written:

1. (a) prints `True`.
2. (b) answers `{"statuses":[...],"warnings":[]}`, not an `exception` /
   `errorcode` object (`accessexception`, `requiredcapability`, ... means the
   service account's role lacks the capability to view other users' completion
   in that course; the message names it: ask the Moodle admin to grant it).
3. Each SCORM `cmid` of the course appears with `modname: "scorm"` and
   `tracking` 1 or 2. `state` is `0` (incomplete), `1` (complete), `2` (complete,
   pass) or `3` (complete, fail); the provider will count only 1 and 2.
4. Freshness: complete a SCORM as the test student (or have it completed), run
   (b) again straight away: that `cmid` must read `1` or `2` at once; reset the
   student's attempt in Moodle and run (b) again: it must read `0`. A delay
   here is the real maximum age of the signal.

Report the output (without the token) and the numbers `state` took.

## Marking SCORM complete on staging (a Moodle step, not a backend tool)

The backend reads Moodle and never writes completion, so a staging helper does
not belong here. To exercise the gate end to end with a test student, in Moodle
(or with a plugin CLI if the plugin team provides one): complete the SCORM
activities as that student, or use *Course administration > Reports > Activity
completion* ("Override" cell) / *Course completion* to mark an activity
complete or incomplete for the user. The backend sees the change on its next
call (see the freshness check above).

## Order of deploy and of rollback

Deploy: **backend first** (migration `e1f4a7c2b630`, mode `off`), then the
plugin that signs `uid` and the frontend, then `enforce` once the completion
provider exists. After the deploy run `alembic current`: the entrypoint starts
the container even when a migration fails, and the new code selects the new
column, so with the column missing **every** authenticated request fails. If
`current` is not `e1f4a7c2b630`, roll the image back.

Rollback, in this order:

1. **Backend to `CLOSING_GATE_MODE=off` first** and restart it. The final exam
   is open as before and is listed in the trainer again, so whatever the
   frontend or the plugin does next cannot lock students out. `/trainer/closing`
   keeps answering.
2. Then revert the frontend and, if needed, the plugin. Leaving the plugin's
   `uid` in place is harmless: a backend without this change ignores the claim
   (verify it, see below).
3. Only then, if the whole change must go, roll the backend image back and, last,
   `alembic downgrade d2a9e5b7c316` (drops `access_tokens.moodle_user_id`).
   Never downgrade while the new image still runs: it selects that column.

## Checking the deployed service ignores claims it does not know

The code does (the tests send unknown claims), but what runs in the deployed
service is what counts. A launch needs the tenant's private key, which only the
plugin side holds; run this where that key is, against the deployed backend
(a test `sub`, role `student`). It redeems one assertion and creates one
short-lived token; a 200 means the extra claims were accepted.

```python
import json, secrets, sys, time, urllib.request
import jwt  # PyJWT, with cryptography

KEY = open(sys.argv[1]).read()  # the tenant's private key (PEM)
ISS, KID, COURSE = sys.argv[2], sys.argv[3], int(sys.argv[4])
URL = sys.argv[5].rstrip("/") + "/api/v1/auth/launch"
now = int(time.time())
claims = {
    "iss": ISS,
    "aud": "rumbos",
    "sub": "0" * 32,
    "course": COURSE,
    "role": "student",
    "iat": now,
    "exp": now + 60,
    "jti": secrets.token_urlsafe(16),
    "uid": 1,
    "claim_the_backend_does_not_know": {"x": [1, 2]},
}
token = jwt.encode(claims, KEY, algorithm="RS256", headers={"kid": KID})
req = urllib.request.Request(
    URL, json.dumps({"assertion": token}).encode(), {"Content-Type": "application/json"}
)
print(urllib.request.urlopen(req).status)  # expect 200
```

Run it before and after the backend deploy: a 200 both times means the old
backend tolerates the plugin's new claim and the new one stores it.
