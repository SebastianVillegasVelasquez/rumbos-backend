# English A2 in the trainer: what the frontend needs to know

**There is no contract change.** `openapi.json` is identical to `main`
(`python -m app.scripts.export_openapi --check` passes). A2 is a second pack
behind the same endpoints, payloads and states as A1; nothing here is new. See
`docs/TRAINER-API-EXAMPLES.md` for the exchanges themselves. This page only says
what is different about A2 and how its audio is used.

All paths are under `/api/v1`, all calls carry `Authorization: Bearer <token>`,
field names are snake_case.

## Turning A2 on

A2 is chosen per map, by the editor, in the map settings (never by the student):

```bash
curl -X PUT "$API/api/v1/course-maps/$MAP_ID/appearance" \
  -H "Authorization: Bearer $EDITOR_TOKEN" -H "Content-Type: application/json" \
  -d '{"settings": {
        "trainer": {"enabled": true, "language": "en", "level": "A2"},
        "assessments": {"enabled": true,
          "checkIn": {"formId": "a2-form-a", "maxAttempts": 1, "timeLimitMin": 90}}},
       "defaultSkinId": null, "skinRules": []}'
```

(The settings body is the one the A1 map already uses, as in the tests; only
`level` and `formId` change.) While the pack is a draft the response carries
warnings with rule `content_not_published` for `trainer.level` and
`assessments.check_in.form_id`. They are warnings, not errors.

Which exam forms exist, and in which state, for the editor's picker:

```bash
curl "$API/api/v1/trainer/assessments/forms" -H "Authorization: Bearer $EDITOR_TOKEN"
# [{"id":"a1-form-a",...},{"id":"a2-form-a","title":"...","publication_state":"draft"}]
```

## Who sees what while A2 is a draft

| | Editor (preview) | Student |
| --- | --- | --- |
| `GET /trainer/catalog` | whole pack; each activity has `publication_state: "draft"`, `preview: true` | `{"enabled": false, "reason": "no_published_content", "preview": false}` |
| `GET /trainer/assessments` | item with `publication_state: "draft"`, startable | item `state: "locked"`, no `publication_state` |
| `POST /trainer/assessments/check_in/attempts` | `201` | `404` |
| `GET /trainer/audio/{id}` | `200` | `404` |

This is checked in `tests/test_a2_api.py`. Nothing changes for the student until
the content is published by the reviewed CLI (`python -m app.scripts.content
publish`), which is a content-only deploy.

## The practice catalog

```bash
curl "$API/api/v1/trainer/catalog" -H "Authorization: Bearer $TOKEN"
```

`language: "en"`, `level: "A2"`, `modules[]` with keys `M1`..`M4`, 13 lessons
(`1.1`..`4.4`), each with `vocabulary_count: 8` and three activities
(`dialogue`, `listening`, `grammar`) of 6-turn dialogue, 3 listening items and 6
exercises. Same shapes as A1 (`CatalogModule`, `CatalogLesson`,
`CatalogActivity`).

## Audio: how a clip is referenced and fetched

Audio is never an id the client builds. A step or exam item that has a clip
carries its own reference, and that is all there is:

```json
"audio": {"url": "/api/v1/trainer/audio/a2fa-89955aab8f?v=<content_version>", "duration_s": 7.92}
```

* Use `url` exactly as given (it already has the prefix and the `v=` cache key).
  Do not parse the id: exam ids (`a2fa-…`) are opaque on purpose and say nothing
  about the item or the answer. Practice ids are readable but still not part of
  the contract.
* `duration_s` is `null` until the file exists (A1 today); show no duration then.
* In the exam, a matching listening item (`match_audio`) carries several clips,
  each `{"id": "c<hash>", "audio": {url, duration_s}}`; the order is shuffled per
  attempt like the options.
* Fetch with the bearer token. An `<audio src>` cannot send an `Authorization` header, so
  fetch the bytes and play a blob URL (not verified against a frontend here).

```bash
curl -i "$API$URL" -H "Authorization: Bearer $TOKEN" -H "Range: bytes=0-1023"
# 206, Content-Type: audio/mpeg, Accept-Ranges: bytes, ETag, Cache-Control
```

`Range` is honoured, so seeking works; `v=<content_version>` makes the response
cacheable for a year (`private, immutable`) and a new content version gets a new
URL. An unknown id, a file that is missing, audio of unpublished content for a
student, or a map with the trainer and the exams both off, is `404`.

Format is MP3, mono, 24 kHz, loudness-normalised, which every browser plays.

### Slow audio (no backend work)

The curriculum asks for slow audio. Do it in the player:
`audio.playbackRate = 0.75; audio.preservesPitch = true;` Both exist in current
Chromium and WebKit. There is no second file and no field for it.

### Transcripts are not sent

Neither practice steps nor exam sections carry the transcript, the keys or the
spoken text. Do not look for them. Showing a transcript for practice items would
be an additive field decided with product; for the exam it would turn Listening
into Reading and is a product and legal decision (see `DECISIONS-trainer.md`,
D38).

## The A2 check-in exam (`a2-form-a`)

Same endpoints as A1: `GET /trainer/assessments`, `POST
/trainer/assessments/check_in/attempts`, `GET
/trainer/attempts/{id}/sections/{key}`, `PUT /trainer/attempts/{id}/answers`,
`POST /trainer/attempts/{id}/submit`, `GET /trainer/attempts/{id}/result`.

* Sections, in order: `listening` (11 items), `reading` (15), `use_of_english`
  (15), `writing` (2), `speaking` (1). 5 sections, 44 items, 45 scorable units
  (the 5-clip matching item counts as 5). Weights 25/25/20/15/15.
* **Speaking is typed.** It is one item with two short questions, answered as
  text like Writing. Nothing is recorded and nothing measures pronunciation. The
  UI must not call it a speaking test or show a microphone. The internal label
  `written_equivalent` is not sent.
* Result: the same `Result` as A1 (`percent` string, `passed`, `band`,
  `by_section[]`, `by_module[]`, `attempts`). Pass is strictly above 80.
* The form has no images yet (the source gave no figures), so no step needs an
  image or alt text.
* Time limit comes from the map settings (`time_limit_min`), not the form.

## What does not exist

No endpoint to record audio, no transcript field, no `delivery` field, no
`slow_audio` field, no per-level route: the level is the editor's map setting.
