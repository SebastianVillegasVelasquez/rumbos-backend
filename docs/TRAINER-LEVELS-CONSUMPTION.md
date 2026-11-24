# English levels in the trainer (A1, A2, B1, B2): what the frontend needs to know

Every level is a pack behind the same endpoints, payloads and states; there is
no per-level route and no per-level payload. The level is the editor's map
setting, never the student's. `docs/TRAINER-API-EXAMPLES.md` has the exchanges
themselves; this page says what depends on the level, what is new for
distinguishing levels, and how audio is used.

All paths are under `/api/v1`, all calls carry `Authorization: Bearer <token>`,
field names are snake_case (the map settings body is camelCase, as before).

## What changed with B2 (all additive; B1 adds nothing)

| Where | Change |
| --- | --- |
| `GET /trainer/assessments/forms` | each entry gains `language`, `level`, `purpose` |
| `PUT /course-maps/{id}/appearance` | new 422 when a form's language or level differs from `trainer` (below) |
| exam item views | a written task may carry `max_chars` (and `min_words`/`max_words`); B2 Writing and Speaking use them |

Existing fields are untouched: a client that ignores the new ones keeps working.

## Exam forms: the editor's picker

```bash
curl "$API/api/v1/trainer/assessments/forms" -H "Authorization: Bearer $EDITOR_TOKEN"
```

```json
[
  {"id": "a1-form-a", "title": "Inglés A1 · Evaluación diagnóstica · Forma A · Diagnóstico de entrada",
   "language": "en", "level": "A1", "purpose": "check_in", "publication_state": "..."},
  {"id": "a2-form-a", "language": "en", "level": "A2", "purpose": "check_in", "...": "..."},
  {"id": "b1-form-a", "language": "en", "level": "B1", "purpose": "check_in", "...": "..."},
  {"id": "b2-form-a", "language": "en", "level": "B2", "purpose": "check_in", "...": "..."}
]
```

`purpose` is `"check_in"` or `"final"`. Titles name the level in the content
itself, so two forms never look alike in a list. **The wording of those titles is
a proposal that product must approve.** Filter by `level` (and `language`) rather
than parsing the title.

## Turning a level on

```bash
curl -X PUT "$API/api/v1/course-maps/$MAP_ID/appearance" \
  -H "Authorization: Bearer $EDITOR_TOKEN" -H "Content-Type: application/json" \
  -d '{"settings": {
        "trainer": {"enabled": true, "language": "en", "level": "B2"},
        "assessments": {"enabled": true,
          "checkIn": {"formId": "b2-form-a", "maxAttempts": 1, "timeLimitMin": 90}}},
       "defaultSkinId": null, "skinRules": []}'
```

While the pack is a draft the response carries warnings with rule
`content_not_published` for `trainer.level` and `assessments.check_in.form_id`.
They are warnings, not errors.

### The guard: a form of another level is a 422

With the trainer on, `assessments.check_in.form_id` and
`assessments.final.form_id` must be forms of the map's `language` and `level`.
Otherwise the save is refused:

```bash
# trainer.level "A1" with formId "a2-form-a"  ->  HTTP 422
{"detail": {"code": "trainer_settings_invalid",
  "errors": [{"path": "assessments.check_in.form_id",
              "rule": "form_level_mismatch",
              "message_key": "trainer_settings.form_level_mismatch"}],
  "problems": [{"...": "same finding, with code form_level_mismatch"}]}}
```

* `path` is `assessments.check_in.form_id` or `assessments.final.form_id`.
* **Translation key to add:** `trainer_settings.form_level_mismatch`. Suggested
  Spanish: "El examen elegido es de otro idioma o nivel que el del mapa."
* No value of the request is echoed back.
* **With the trainer off nothing is refused**: `trainer.level` defaults to `A1`
  even if nobody set it, so it cannot be told from a deliberate A1. The editor
  should send `trainer.level` whenever a form is chosen (see
  `DECISIONS-trainer.md`, D41, for the alternative).

## Who sees what while a level is a draft

| | Editor (preview) | Student |
| --- | --- | --- |
| `GET /trainer/catalog` | whole pack; each activity has `publication_state: "draft"`, `preview: true` | `{"enabled": false, "reason": "no_published_content", "preview": false}` |
| `GET /trainer/assessments` | item with `publication_state: "draft"`, startable | item `state: "locked"`, no `publication_state` |
| `POST /trainer/assessments/check_in/attempts` | `201` | `404` |
| `GET /trainer/audio/{id}` | `200` | `404` |

Nothing changes for the student until the content is published with the reviewed
CLI (`python -m app.scripts.content publish`), a content-only deploy.

## The practice catalog, by `{language, level}`

```bash
curl "$API/api/v1/trainer/catalog" -H "Authorization: Bearer $TOKEN"
```

The catalog is the pack of the map's `trainer.language` and `trainer.level`.
Shapes (`CatalogModule`, `CatalogLesson`, `CatalogActivity`) are the same at
every level; only the numbers differ:

| level | modules | lessons | per lesson |
| --- | --- | --- | --- |
| A1 | M1..M4 | as in the pack | 8 words, dialogue, listening, grammar |
| A2 | M1..M4 | 13 (`1.1`..`4.4`) | 8 words, 6-turn dialogue, 3 listening items, 6 exercises |
| B1 | M1..M4 | 15 (M1 has 4, M2 4, M3 4, M4 3) | same as A2 |
| B2 | M1..M4 | 18 (M1 has 6, M2 5, M3 4, M4 3) | same as A2 |

Read the lists from the response; do not hard-code them.

## Audio: how a clip is referenced and fetched

Audio is never an id the client builds. A step or exam item that has a clip
carries its own reference:

```json
"audio": {"url": "/api/v1/trainer/audio/b2fa-89955aab8f?v=<content_version>", "duration_s": 7.92}
```

* Use `url` exactly as given (prefix and `v=` cache key included). Do not parse
  the id: exam ids (`a2fa-…`, `b2fa-…`) are opaque on purpose.
* `duration_s` is `null` until the file exists; show no duration then.
* A matching listening item (`match_audio`) carries several clips, each
  `{"id": "c<hash>", "audio": {url, duration_s}}`, shuffled per attempt.
* An `<audio src>` cannot send `Authorization`: fetch the bytes and play a blob
  URL (not verified against a frontend here).

```bash
curl -i "$API$URL" -H "Authorization: Bearer $TOKEN" -H "Range: bytes=0-1023"
# 206, Content-Type: audio/mpeg, Accept-Ranges: bytes, ETag, Cache-Control
```

`Range` is honoured. `v=<content_version>` makes it cacheable for a year
(`private, immutable`); a new content version is a new URL. An unknown id, a
missing file, audio of unpublished content for a student, or a map with the
trainer and exams both off, is `404`. Format: MP3, mono, 24 kHz.

**Slow audio** is done in the player (`audio.playbackRate = 0.75;
audio.preservesPitch = true`); there is no second file and no field.

**Transcripts are not sent**, neither for practice nor for the exam (it would
turn Listening into Reading; `DECISIONS-trainer.md`, D38).

## Written tasks: length hints

A written task in an exam section may carry `min_words`, `max_words` and
`max_chars`. They are hints for the UI (counter, limit); the server enforces the
character limit (`max_chars`, default 300 when absent). B2 Writing sends 120,
160 and 1500; B1 Writing sends 70, 100 and 900. A task with a template (`template_text`) never carries them.

## The exams

Same endpoints at every level: `GET /trainer/assessments`, `POST
/trainer/assessments/check_in/attempts`, `GET
/trainer/attempts/{id}/sections/{key}`, `PUT /trainer/attempts/{id}/answers`,
`POST /trainer/attempts/{id}/submit`, `GET /trainer/attempts/{id}/result`.

| form | sections (items) | scorable units | weights L/R/U/W/S |
| --- | --- | --- | --- |
| `a1-form-a` | as the pack | as the pack | as the pack |
| `a2-form-a` | listening 11, reading 15, use_of_english 15, writing 2, speaking 1 | 45 | 25/25/20/15/15 |
| `b1-form-a` | listening 11, reading 15, use_of_english 15, writing 2, speaking 1 | 45 | 25/25/20/15/15 |
| `b2-form-a` | listening 11, reading 15, use_of_english 15, writing 2, speaking 1 | 45 | 25/25/20/15/15 |

* **Speaking is typed** at A2, B1 and B2. Nothing is recorded and no pronunciation
  is measured; the UI must not call it a speaking test or show a microphone.
  The internal label `written_equivalent` is not sent.
* B2 Writing Task 2 is one box that shows two prompts ((a) or (b)); the platform
  has no choice of task.
* B1 Writing is two tasks of 70 to 100 words (`min_words` 70, `max_words` 100,
  `max_chars` 900) and Speaking one task (clarify, compare, recommend); Task 1
  (read aloud) has no written form and is not in the form.
* B2 Speaking has one task (the chair's opening); Task 1 has no written form and
  is not in the form.
* Result: `percent` string, `passed`, `band`, `by_section[]`, `by_module[]`,
  `attempts`. Pass is strictly above 80.
* Time limit comes from the map settings (`time_limit_min`).
* The forms have no images (the sources gave no figures), so no step needs an
  image or alt text.

## B1, end to end with `curl`

Nothing is B1-specific except the values `"B1"` and `"b1-form-a"`. `$API` is the
server, `$MAP_ID` a course map, `$EDITOR_TOKEN` and `$TOKEN` bearer tokens.

```bash
# 1. The editor finds the B1 exam (filter by level, do not parse the title)
curl -s "$API/api/v1/trainer/assessments/forms" -H "Authorization: Bearer $EDITOR_TOKEN"   | jq '.[] | select(.level == "B1")'

# 2. The editor turns the B1 trainer and its check-in on
curl -X PUT "$API/api/v1/course-maps/$MAP_ID/appearance"   -H "Authorization: Bearer $EDITOR_TOKEN" -H "Content-Type: application/json"   -d '{"settings": {
        "trainer": {"enabled": true, "language": "en", "level": "B1"},
        "assessments": {"enabled": true,
          "checkIn": {"formId": "b1-form-a", "maxAttempts": 1, "timeLimitMin": 90}}},
       "defaultSkinId": null, "skinRules": []}'

# 3. The practice catalog of the map (15 lessons, 45 activities, 8 words each)
curl -s "$API/api/v1/trainer/catalog" -H "Authorization: Bearer $TOKEN"   | jq '{level, lessons: [.modules[].lessons[]] | length}'

# 4. The student starts the check-in and reads Listening
ATTEMPT=$(curl -s -X POST "$API/api/v1/trainer/assessments/check_in/attempts"   -H "Authorization: Bearer $TOKEN" | jq -r .attempt_id)
curl -s "$API/api/v1/trainer/attempts/$ATTEMPT/sections/listening" -H "Authorization: Bearer $TOKEN"

# 5. One clip (use the `url` the item carries, never a built id)
curl -i "$API$URL" -H "Authorization: Bearer $TOKEN" -H "Range: bytes=0-1023"

# 6. Save answers and submit
curl -X PUT "$API/api/v1/trainer/attempts/$ATTEMPT/answers" -H "Authorization: Bearer $TOKEN"   -H "Content-Type: application/json" -d '{"answers": [{"item_id": "...", "response": {"option_id": "..."}}]}'
curl -X POST "$API/api/v1/trainer/attempts/$ATTEMPT/submit" -H "Authorization: Bearer $TOKEN"
```

While the B1 pack is a draft the save in step 2 answers `200` with warnings
`content_not_published`, and a student gets `404` on step 4; both change when the
content is published with the reviewed CLI.

## What does not exist

No endpoint to record audio, no transcript field, no `delivery` field, no
`slow_audio` field, no per-level route, no `assessments.level` setting.
