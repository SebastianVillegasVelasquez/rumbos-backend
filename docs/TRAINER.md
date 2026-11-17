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
enables the feature; editors (preview) read the drafts. A welcome flow can hold
an `assessment` step (`purpose: check_in`).

## Content

`content/trainer/<language>/<level>/` (JSON, versioned in the repo): `pack.json`,
`lessons.json`, `vocabulary.json`, `activities/*.json`, `assessments/*.json`,
`rubrics.json`, `course_words.json`, `audio_manifest.json`, `audio/`. The app
loads the whole catalog at start-up and refuses to start on an invalid pack.
`content_version` is a hash of every file. Drafts are served only with
`TRAINER_SERVE_DRAFTS=true`. Check a pack with
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

## Practice

`GET /catalog`, `GET /progress`, `POST /sessions` (201/200), `GET /sessions/{id}`,
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

Retention: practice sessions 90 days; saved words and reports until erased; exam
attempts are records of a certification and stay until an erasure request.
Student free text exists only in exam answers; logs never contain it.

## Releasing content

1. A person checks every answer key against the client's document.
2. Set `status` to `reviewed`, then `published` (with `reviewed_by` and
   `reviewed_at` on activities) in the form or activity file.
3. `python -m app.scripts.lint_content` must report 0 errors.
4. Produce the audio files and durations.
5. Keep `TRAINER_SERVE_DRAFTS` off in production.
