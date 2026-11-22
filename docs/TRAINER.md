# Language trainer and native assessment

What the backend serves for the English A1 course: a native check-in and final
exam, a result passback to Moodle, and a practice trainer. Choices that needed
a judgment call are in `DECISIONS-trainer.md`.

## Turning it on

Everything is off until a course map's settings say so (appearance settings):

- `trainer.enabled` (+ `language`, `level`): the practice trainer.
- `assessments.enabled` with `checkIn` / `final` (`formId`, `maxAttempts`,
  `timeLimitMin`), `passPercent` (default 80), `passOperator` (`gt` default,
  `gte`), `exemption.enabled`.

Students read the **published** settings of the first map of their course that
enables the feature, so saving in the editor is not enough: **publish the map**.
Editors (preview) read the drafts. A welcome flow can hold
an `assessment` step (`purpose: check_in`); a closing flow (`closing`, see
`WELCOME-FLOW.md`) can hold one with `purpose: final`.

## Content

`content/trainer/<language>/<level>/` (JSON, versioned in the repo): `pack.json`,
`lessons.json`, `vocabulary.json`, `activities/*.json`, `assessments/*.json`,
`rubrics.json`, `course_words.json`, `audio_manifest.json`, `audio/`. The app
loads the whole catalog at start-up and refuses to start on an invalid pack.
`content_version` is a hash of every file. Drafts are served only with
`TRAINER_SERVE_DRAFTS=true`; the exception is an editor in preview, who always
sees `draft` and `reviewed` content (each item carries `publication_state`) so
the team can review it in the real UI. Check a pack with
`python -m app.scripts.lint_content` (CI runs it).

Audio: `tools/build_audio_manifest.py` lists what is needed,
`tools/synthesize_audio.py` makes the files offline with a local text-to-speech
command (`TRAINER_TTS_COMMAND`; the docstring lists open-source engines and their
licences). No speech or AI runs at request time.

## Exam (`/api/v1/trainer`)

| Call | Meaning |
| --- | --- |
| `GET /assessments` | state of check-in and final for the caller |
| `POST /assessments/{purpose}/attempts` | 201 new attempt, 200 resumes the open one |
| `GET /attempts/{id}/sections/{key}` | the section's items (shuffled by the attempt's seed) and saved answers |
| `PUT /attempts/{id}/answers` | autosave (last write wins per item) |
| `POST /attempts/{id}/submit` | scores and closes; repeating it returns the same result |
| `GET /attempts/{id}/result` | the result of a closed attempt |

Rules: the server clock is the only clock (`deadline_at`); a request after the
deadline closes the attempt as `expired` with what was saved; one attempt open
per user and purpose and dense attempt numbers (unique indexes); a pass locks the
exam; editors' attempts are preview (never counted, purged by
`assessments purge-preview`). Nothing sent to a student carries a key, an
accepted answer, a rubric, a transcript or a per-item verdict. Scores are exact
fractions shown with two decimals; passing is strictly above `passPercent` under
`gt`.

Errors: 404 (unknown id or feature off), 409 `{code}` in `no_attempts_left`,
`locked`, `submitted`, `expired`, `in_progress`, 422 `invalid_response`, 429
with `Retry-After`.

## Passback to Moodle

A real (non-preview) closed attempt writes one row in `passback_outbox` in the
same transaction. `python -m app.scripts.passback sync-run` signs each statement
(ES256, `PASSBACK_SIGNING_KEY_PEM`, `PASSBACK_KEY_ID`) and sends it through the
`MoodleResultPort`; `sync-reconcile` requeues what Moodle does not have; `status`
counts the outbox. Transient failures retry forever with backoff (1 min to 6 h),
a refusal parks the row as `failed_permanent`, and `completion_audit` records
every step. `Result.sync.state` is `not_required` (preview), `pending`, `synced`
or `failed`. The real HTTP sender is **not written**: the plugin audit has not
fixed the receiving endpoint. Key rotation: add the new public key to Moodle,
change `PASSBACK_KEY_ID` and the key, restart; pending rows are signed at send
time with the new key.

## Saving the settings

`PUT /course-maps/{id}/appearance` checks the trainer and exam settings against
the deployed content. 422 `trainer_settings_invalid` carries `problems` and
`errors[{path, rule, message_key}]` (`unknown_form`, `unknown_pack`,
`no_exam_configured`, `assessment_step_needs_check_in`,
`assessment_step_needs_final`). A form or level that
exists but is not published saves, with `warnings[{path, rule:
"content_not_published", message_key}]` in the answer. Forms: `a1-form-a`.

## Practice

`GET /catalog` answers `{enabled: false, reason: "disabled_for_map" |
"no_published_content", preview}` when there is nothing to practise, else the
map with `enabled: true`, `preview` and a `publication_state` per activity.
`preview` is `true` only for an editor in preview: read it, do not infer it from
the states received. Then `GET /progress`, `POST /sessions` (201/200), `GET /sessions/{id}`,
`POST /sessions/{id}/answers` (`attempt_id` makes a retry idempotent),
`POST /sessions/{id}/finish` (first-try summary and the mistake kinds to revise),
vocabulary (`GET /vocabulary`, `GET /vocabulary/saved?due=`,
`PUT|DELETE /vocabulary/{id}`, `POST /vocabulary/{id}/review` with `knew_it`:
Leitner boxes 1 to 5, due after 1, 2, 4, 8, 16 days), `POST /reports` (students),
`GET /reports` (editors), `DELETE /me` (the caller's practice data only).

## Operations

| Command | Use |
| --- | --- |
| `app.scripts.assessments sweep` | close overdue attempts (cron every 10 min) |
| `app.scripts.assessments purge-preview` | delete preview attempts older than 24 h |
| `app.scripts.assessments list / export / stats` | one user's attempts / Writing and Speaking text for review / counts |
| `app.scripts.assessments erase` | erase one user's exam data (written request only) |
| `app.scripts.passback sync-run / sync-reconcile / status` | the passback |
| `app.scripts.practice purge` | delete practice sessions older than `TRAINER_RETENTION_DAYS` (90) |
| `app.scripts.practice reports / erase` | flagged content / erase one user's practice data |
| `app.scripts.content publish` | publish reviewed content (dry run unless `--confirm`) |

Retention: practice sessions 90 days; saved words and reports until erased; exam
attempts are records of a certification and stay until an erasure request.
Student free text exists only in exam answers; logs never contain it.

## Releasing content

1. A person checks every answer key and practice item against the client's
   document, and listens to the audio.
2. That person sets `status` to `reviewed` in each activity or form file they
   checked (and nothing else).
3. Produce the audio files and durations (`tools/`), then
   `python -m app.scripts.lint_content` must report 0 errors.
4. Dry run: `python -m app.scripts.content publish --pack en/A1 --activities all
   --forms a1-form-a --reviewer "<name>"`. Read the list. `all` means every
   `reviewed` item; ids can be named instead.
5. Run it again with `--confirm`. It sets `published`, `reviewed_by`,
   `reviewed_at`, flips the pack, lints, undoes everything if the linter
   objects, and appends a line to `content/trainer/publication-audit.jsonl`.
6. Commit the content and the audit line, open a pull request, deploy.
7. Keep `TRAINER_SERVE_DRAFTS` off in production.

## Enabling it for a course

1. Deploy a build with the trainer migrations applied (`alembic upgrade head`).
2. In the editor, turn on the trainer and the exams for the map (form id
   `a1-form-a`); a yellow warning means the content is not published yet.
3. Review in the real UI as an editor: drafts and reviewed content show there.
4. Publish the content (above), deploy, then **publish the map**: students read
   published settings only.
5. As a student, `GET /trainer/catalog` should answer `enabled: true`.

## Contract notes (additive)

| Where | Field | Meaning |
| --- | --- | --- |
| 422 of `PUT /course-maps/{id}/appearance` | `detail.errors[{path, rule, message_key}]` | which field and rule; `problems` unchanged |
| 200 of that PUT | `warnings[{path, rule, message_key}]` | saved, but the content is not published |
| `GET /trainer/catalog` | `enabled`, `reason` | `false` + `disabled_for_map` / `no_published_content`; `true` with the map |
| `GET /trainer/catalog` | `preview` | `true` when the caller is an editor in preview, `false` otherwise; always present |
| catalog activity | `publication_state` | `draft`, `reviewed` or `published` |
| exam list item | `publication_state` | editors in preview only |
| exam list item | `already_passed` | **new.** `true` when the student has passed this exam, or the other one while `exemption.enabled`; always `false` for an editor in preview. A hint so the client does not force another attempt: `state`, the attempt policy and the pass rule are unchanged |
| `settings.closing` | `Welcome` or `null` | **new.** The closing flow, same shape as `settings.welcome`; `null` or absent: there is none |
| `AssessmentStep.purpose` | `check_in` or `final` | **widened.** `check_in` in the welcome flow, `final` in the closing flow; the other one is `assessment_purpose` |
| 422 `trainer_settings_invalid` | rule `assessment_step_needs_final` | a closing flow's final-exam step needs `assessments.enabled` and a `final` policy |
| 422 `welcome_invalid` | paths under `closing.` | the same codes as the welcome flow, plus `closing_needs_steps`, `step_type_not_allowed`, `assessment_purpose` |
| `welcomeDocuments` | now lists the PDFs of both flows | read live, as before; look a document up by `assetId` |
| `GET /trainer/assessments/forms` | `[{id, title, publication_state}]` | editors only (403 for students), read-only: every exam form in the deployed content, drafts included, ordered by `id`; `id` is what `assessments.check_in.form_id` takes |

| `GET /trainer/closing` | `{enforced, preview, available, reason, progress, final}` | **new.** Whether the closing flow's final exam may start; see `CLOSING-GATE.md` |
| 409 of `POST /assessments/final/attempts` | `code: closing_not_available`, `message_key`, `reason`, `completed?`, `total?` | **new.** Only with `CLOSING_GATE_MODE=enforce` |
| `AttemptStarted`, `Result` | `preview` | **new.** `true` for an editor's trial run, `false` for every real attempt |
| `GET /trainer/assessments` | no `final` item | with `CLOSING_GATE_MODE=enforce`; read the final from `/trainer/closing` |

`openapi.json` is generated from the code (`python -m app.scripts.export_openapi`);
CI runs it with `--check` and fails when the committed file is stale.
