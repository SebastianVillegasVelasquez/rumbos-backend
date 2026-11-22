# The closing gate: the final exam waits for the SCORM content

The final exam is a step of the closing flow (`closing`, see `WELCOME-FLOW.md`),
not an entry of the trainer. A student reaches it only after completing the
SCORM content shown on the map. The rule is enforced by the backend, so calling
the API directly gets the same answer as the screen.

Status: **phase 1.** The rule, the endpoint, the guard and the editor's trial
run are built and tested against a fake completion provider. The real read of
Moodle's completion is **not** built (see "Before the real provider").

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
   `scorm_incomplete` with `completed` / `total`; otherwise available

Required SCORM content = the **activity bubbles of the map that holds the final
exam** that point at a module with `modname: scorm`, are visible to students
(section and module visible, not a label or a link-less module) and that Moodle
tracks for completion (`completion != 0`). Hidden, deleted, non-SCORM and
untracked ones do not count. A restricted one (`availability` set) **does**
count: the service account sees everything, so what a restriction means for one
student is unknown, and the safe side of an exam gate is to require it. A map
with **no** required SCORM opens the exam at once (logged). Non-SCORM bubbles
never block. To widen the rule change `REQUIRED_MODNAMES`.

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
