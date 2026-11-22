# Decisions: language trainer and native assessment

Judgment calls made while building the trainer and the assessment (English A1).
Each entry says what was decided, why, and what would change it. Decisions that
change what students see, or the certification rule, are marked **ASK** and are
listed in the final report.

## D1. Content packs are JSON, not YAML

The runtime has no YAML parser and new runtime dependencies are not allowed.
JSON is in the standard library, diffs well in git, and is validated by typed
pydantic models. Cost: no comments in the files; authoring notes go in a
`notes` field or in the docs.

## D2. Content lives in the repository, not in the database

Reviewable in git, deployed atomically with the code that reads it, no admin UI
to build or secure. Cost: a content change needs a deploy. The Dockerfile
copies `content/` into the image.

## D3. Client source documents are inputs, not part of the repository

The `.docx` and the curriculum `.pdf` sit in `docs/` for the import tool and are
ignored by git. Only the section "Evaluacion diagnostica", Forma A, of the
`.docx` is read; the rest of that file is never parsed or printed.

## D4. Assessment option ids are opaque, not letters **(design)**

In the source the correct option is very often A or B. The stored form keeps the
source letters; the API will serve ids derived from `item_id` and letter by a
hash, so an id carries no position and cannot reveal the bias even to someone
reading the network traffic. The shuffle then changes order only.

## D5. The module map leaves out two kinds of item

The matching task's code spans modules ("M-I a M-IV") and Writing and Speaking
tasks have no code in the source. None of them enters `by_module`; they still
count in the section scores and the total. Revisit if the client wants the five
pairs assigned to modules (that would mean editing the source's coding).

## D6. Listening transcripts are not served in an assessment

The contract's practice `audio` carries a transcript. In an exam a transcript
would turn Listening into Reading, so assessment audio is served without one.
The transcript is kept for authoring and audit. **ASK**: a student who cannot
hear needs an accommodation (a documented alternative), not a hidden
transcript.

## D7. Speaking written equivalents are ours **(ASK: needs pedagogical sign-off)**

The document allows "la version escrita equivalente". For Task 1 (read aloud)
the written equivalent is: write the welcome note of Writing Task 2, scored for
completeness, sentence marks and intelligibility. For Task 2 the three
questions are answered in writing; the document's alternative "or describe the
image" is not offered. Neither is the client's wording.

## D8. A pass can come from the template itself unless the rubric stops it

A first version of the Writing Task 2 rubric gave 4 of 5 points to a note whose
blanks were all empty, because the template's own words matched the checks. The
fix is a `multiplier` check that scales a criterion by how much of the template
the student filled. A test pins it. Other proxy limits are in the final report.

## D9. The Writing 2 word range is tight against the template

The template has 19 fixed words and the document asks for 15 to 25 words, so
the five blanks may hold about 6 words in all (a place named "Posada El
Encuentro" already breaks it). The proxy keeps the document's range; it costs
at most half a point in the intelligibility criterion. Raise the range if the
client agrees.

## D10. Exactly 80 does not pass; results show two decimals

The rule is strictly greater than 80 (confirmed by the product owner in the
conversation; written confirmation still to be attached). Under
`pass_operator = "gt"` a total of exactly 80.00 is band `achieved` and does not
pass. Every reachable total is a multiple of 1/6, so the smallest passing total
is 80.17, and the result shows two decimals (`80.17`), so nobody sees a
"80.00" that passes. Rounding up to 81 was considered and not adopted: it would
change the number the student sees without changing who passes.

## D11. Rate limiting is per process

The existing limiter keeps counts in process memory, so with `WORKERS=n` the
effective limit is up to n times the configured one. The assessment endpoints
reuse it; a shared limit belongs in the reverse proxy.

## D12. The trainer API names its fields in snake_case

The shared contract v2 spells fields as `attempt_id`, `deadline_at`,
`pass_percent`, while the rest of this API uses camelCase aliases. The trainer
schemas follow the contract and do not use the alias generator. The map
settings the editor writes (`assessments.checkIn`, ...) stay camelCase because
they belong to the existing settings document.

## D13. The Moodle passback has a port and a fake, not a real sender

The plugin repository records no decision on which endpoint receives exam
statements, so the HTTP sender is not written. `MoodleResultPort` has a fake
for tests and development; with `PASSBACK_SENDER=none` (the default) results
wait in the outbox as `pending`. Statements are ES256 JWS tokens signed at send
time (so a key can be rotated while rows wait); `jti` is the attempt id, which
lets Moodle accept each once. Rows are never dropped on a transient failure.

## D14. Every real closed attempt is reported, not only passes

The outbox gets one row per non-preview closed attempt, passed or not. The
statement says `completes_course` (passed and the exemption was on when the
attempt started), so Moodle decides what to record; a pass-only feed would hide
failed attempts from the teacher. The rule comes from the attempt's frozen
policy, never from today's settings.

## D15. Which course settings an exam reads

A student reads the settings of the first published map of the course that
turns assessments on (then trainer, then defaults). An editor in preview reads
the drafts, so an exam can be tried before publishing. The anonymous legacy
principal has no user and gets 404.

## D16. Draft content is not served unless the server says so

A form or pack whose status is `draft` (all of them today) is served only with
`TRAINER_SERVE_DRAFTS=true`. Production stays off until a person has reviewed
the keys and flipped the status (see the final report for the steps).

## D17. The image ships the content directory

The app loads the packs at start-up and refuses to start if one is invalid, so
`content/` is copied into the image and CI runs the linter first.

## D18. Practice content was authored per module, not per lesson

The brief asked for one commit per lesson. The four lessons of a module share
`vocabulary.json` and `audio_manifest.json`, so a per-lesson commit would have
rewritten the same two files four times; there is one commit per module. Every
activity is `draft`. Each lesson has a scripted dialogue (6 turns), a listening
set (3 items), an exercise set (6 items) and 8 words. There are no Writing or
Vocabulary-skill activities: vocabulary is the saved-word flashcards.

## D19. Dialogue choices are all valid replies

A scripted dialogue is not graded: each "choose" turn offers natural replies
that all move on, and a "type" turn accepts any reply containing the key words
of one accepted set (hyphenated spellings such as R-U-I-Z are one token, so the
content also accepts that form). A test finishes every dialogue with its model
replies.

## D20. Vocabulary review is self-reported

`POST /vocabulary/{id}/review` takes `knew_it` from the student; the backend
cannot check it. Leitner boxes follow the answer (right: next box, wrong: box 1).

## D21. Exam attempts are not touched by `DELETE /me`

`DELETE /me` removes only practice data. Exam attempts are certification records
and are erased only through the operator command on a written request.

## D22. The linter's unfinished-text markers are upper case

`todo` is the Spanish word for "all" and appears in translations, so only TODO,
FIXME, TBD and XXX count.

## D23. Settings errors name the field, in the editor's spelling

A 422 `trainer_settings_invalid` keeps `problems[{path, code, message}]` as it
was and adds `errors[{path, rule, message_key}]`. `errors[].path` is snake_case
(`assessments.check_in.form_id`), the spelling of the editor's own model, while
`problems[].path` stays camelCase (the stored document). Rules: `unknown_form`,
`unknown_pack`, `no_exam_configured`, `assessment_step_needs_check_in`,
`form_level_mismatch` (D41).
`message_key` is `trainer_settings.<rule>`. No value of the request is echoed.
Only the appearance save runs this check; creating a map with settings in the
same call does not (it did not before either).

## D24. A form in any state can be configured; a warning says it is not live

Validation looks only at whether the form (or the pack for the level) exists.
A `draft` or `reviewed` form saves, and the answer carries
`warnings[{path, rule: "content_not_published", message_key}]` so the editor can
set the exam up before a person publishes it. A trainer whose pack has no
published activity warns at `trainer.level`. Unknown ids stay a 422.

## D25. The practice catalog says why it is empty

`GET /trainer/catalog` answers `200 {enabled: false, reason}` where it used to
answer 404 or an empty map: `disabled_for_map` (the course's settings do not
turn the trainer on) or `no_published_content` (on, but nothing the caller may
see). Two fixed words, so a student learns nothing about packs or drafts. A real
fault is still a 5xx. Every other trainer route keeps its 404 when the feature is
off. The frontend maps 404 to "closed", so it needs to read `enabled` too.

## D26. Editors review unpublished content in the real UI

For an editor in preview (`caller.preview`) drafts and reviewed content are
served whatever `TRAINER_SERVE_DRAFTS` says: practice activities and sessions,
the exam form and its audio. Each practice activity carries `publication_state`;
the exam list carries it for editors only. Students get published content only.
The environment flag stays for local development and must stay off in production.

## D27. Publishing is a reviewed, confirmed, audited file change

`python -m app.scripts.content publish` only moves `reviewed` content to
`published` (a draft is refused), prints a dry run, and writes only with
`--confirm`. It also flips `pack.json` because the audio route serves audio
only for a published pack. After writing it lints the pack and restores every
file if the linter objects (so an exam without its audio files cannot go live).
`--reviewer` overwrites `reviewed_by`/`reviewed_at` with the person confirming.
The audit line goes to `content/trainer/publication-audit.jsonl`, in the repo,
because publishing is a commit plus a deploy: the image ships `content/`.

## D28. What blocked the trainer for course 10

Read from the code and the repository, not from a deployment (none is reachable
from the development machine): every activity, the exam form and the pack are
`draft`, and production serves drafts only with `TRAINER_SERVE_DRAFTS`, which
stays off. The editor also builds its exam settings with a guessed form id
(`forma_a`; the real one is `a1-form-a`), so the save that would turn the trainer
on is refused as one document. The editor should send `a1-form-a` and, better,
read the available forms from the backend (a read-only list is not built yet).

## D29. The closing flow: what the backend can and cannot know

Audit before building (`feat/closing-flow`, from `af17d20`):

- The welcome flow is only a *definition* here (`MapSettings.welcome`). Who saw,
  completed or escaped a step is stored in Moodle (`format_rumbos_flow`, unique
  on course, user, map and step key; methods `completed`, `fallback`,
  `unavailable`) and read and written by the host protocol (`flow-step-done`,
  `flow-reset`). The backend has no table, endpoint or column for it.
- The backend has **no signal that a level is finished**. Its Moodle client only
  calls `core_webservice_get_site_info` and `core_course_get_contents`, whose
  modules carry the completion *configuration*, not any user's state. The
  completion counts and the per-user state go from the plugin to the frontend
  (`rumbos:host-state`, `request-stats`), never to the backend. The only
  per-user facts it owns are the exam attempts.
- So the closing flow's per-user state needs no backend table (the Moodle store
  is keyed by map and step key, and works for any step), and `closing.available`
  is **not implemented**: it would need an invented criterion. The frontend
  already has the course state from the host. If the backend must decide, the
  plugin has to send it a signed progress statement first (a new contract, like
  the exam passback).
- Generalising `welcome` into `flows[flow_key]` was not needed and was not done:
  a new optional key `closing` is backward compatible by construction (absent is
  disabled, left out of `to_stored()`, so no stored map or publication changes
  its hash). A `flows` map would have moved `welcome` and broken every client.

Judgment calls:

- "`content` steps" is read as the welcome flow's content types (`html`, `pdf`,
  `video`); there is no `content` type and none was invented. `welcome` and
  `quiz` are refused in the closing flow.
- The flows share `AssessmentStep` and one validator (`flow_problems(name=...)`)
  instead of a copy. The purpose is checked per flow (`assessment_purpose`), so
  `final` in the welcome flow is still a 422, now from the flow check.
- Step keys must be unique across both flows (Moodle keys progress by map and
  step key, with no flow). Reported at `closing.steps[i].key`.
- The closing problems keep the code `welcome_invalid` (and its message) so the
  frontend handles one error shape; the path says which flow.
- The closing PDFs are listed in the existing `welcomeDocuments` (looked up by
  `assetId`) instead of adding a second list to three response models.
- `already_passed` is a hint on each exam list item and changes nothing else:
  `state`, the attempt limits, the lock after a pass and the pass rule are as
  before, so a student with a check-in pass can still start the final. It is
  `true` for a pass in this exam, or in the other while `exemption.enabled`,
  and always `false` for an editor in preview.

## English A2: the check-in exam, the practice pack and the audio

Audit before building (`feat/a2-pack`, from `fdc8920`): the A1 pack is the shape
to mirror (`pack.json`, `lessons.json`, `vocabulary.json`, `activities/`,
`assessments/`, `rubrics.json`, `course_words.json`, `audio_manifest.json`); the
catalog loads every `<language>/<level>/pack.json`, `MapSettings.trainer.level`
already accepts `A2`, and the editors' forms list, the catalog by `{language,
level}`, practice sessions and exam attempts work on A2 unchanged
(`tests/test_a2_api.py`). **No schema, endpoint or OpenAPI change.** A1 has a
manifest of 150 clips and **no audio file at all** (there is no `audio/` directory, every
`duration_s` is null), so there was no engine or voice to mirror.

## D30. A2 content is draft, built from the client's extracted source

`a2-form-a` and the 39 practice activities are `draft` with no reviewer, and
`TRAINER_SERVE_DRAFTS` stays off. The exam is generated by
`tools/import_a2_form_a.py` from `tools/sources/a2-form-a.source.json`
(verbatim extract; nothing corrected). Item ids (`a2-fa-l-01`) are ours; the
source number is kept as `source_number` (authoring only, never served). What the
source does not give (part titles and instructions, the Writing 1 box label) is
written in the importer and listed for review. The source has no figure briefs or
alt text, so no image item exists and `image_pending` is not set anywhere.

## D31. Speaking is typed answers (`delivery: written_equivalent`), Task 1 is not shipped **(ASK)**

The app never records and runs no AI, and the product owner confirmed Speaking
counts 15%. Following A1 (D7) the section is delivered as typed answers scored
by a deterministic rubric, labelled `delivery: "written_equivalent"` in the form
(an optional field of the authoring model; it is **not** in the HTTP responses,
so the result screen and the Moodle statement carry only `by_section[].key` and
the total `percent`, and never claim to measure pronunciation). Task 2 is
`speaking.tomorrow_now_written` (answers both, *going to*, *be* + *-ing*,
intelligibility). Task 1 ("read your Writing story aloud") has no natural written
equivalent; unlike A1 it is not invented. An unanswerable scored task would cap
the total at 92.5%, so the section holds Task 2 only at its full 15% weight
(weights untouched, section kept). Needs a pedagogical decision. Oral recording
and a Moodle-side oral component are future phases and need a privacy and
retention decision from the client first (who hears the recordings, where they
live, how long).

## D32. Two rubric checks were added to the same mechanism

The A2 Writing 2 and Speaking 2 rubrics ask for past-tense verbs and the present
continuous, which no existing check could express. `past_verbs_at_least` (a word
in *-ed* or one of a listed irregular set) and `be_plus_ing` were added to the
deterministic engine. Both are proxies (a noun ending in *-ed* counts); they need
pedagogical sign-off like the rest of the rubrics.

## D33. Spoken text and displayed text are separate, and decided by hand

`audio_manifest.json` keeps the source transcript in `transcript` (shown or
audited) and what the voice reads in `text` and `voices[].text`. Decided, not
left to the engine: "one-one-two" is read "one, one, two"; times and prices are
words; the Wi-Fi password `MACIZO2026` is read letter by letter then digit by
digit ("M, A, C, I, Z, O, two, zero, two, six"), because a password is copied
character by character. Dialogues have two voices, `host` and `guest`; every
single-sentence clip uses `host`, so the voice never hints at the answer. Practice
dialogue partners use `guest`, practice listening clips use `host`.

## D34. Exam audio ids are opaque hashes

`a2fa-` plus 10 hex of SHA-256 of `form|item|clip`: stable, collision-free across
packs (the audio route takes the first pack that knows an id), and they reveal
nothing: not the item, not the position of a matching clip. The file is
`audio/<id>.mp3`. Practice ids (`en-a2-1.1.l2`) are readable.

## D35. Engine: Kokoro-82M through ONNX Runtime, dev-time only

Chosen over Piper because it is **deterministic** (identical samples on repeat;
Piper samples noise per run and differed), its weights are Apache-2.0 (Piper's
engine is GPL-3.0, harmless for generated audio but heavier to reason about, and
its good voices are mostly research or non-commercial: ryan CC BY-NC-SA, lessac
Blizzard research licence), and an ASR round trip understood it best (Piper
heard "hike" as "height"). Pinned: `kokoro-onnx==0.6.1`, model files by SHA-256 in
`content/trainer/en/A2/tts.json`, stored outside the repository
(`TRAINER_TTS_MODEL_DIR`). It is the `tts` dependency group (`uv run --group
tts`), not a runtime dependency and not in the image; ffmpeg comes from
`imageio-ffmpeg`. Voices: `host` = `am_michael`, `guest` = `af_heart` (US
English), pace 0.85, 450 ms between turns. Output: MP3, mono, 24 kHz, 48 kbps,
loudness-normalised to -19 LUFS (two-pass, linear) so clips sit at the same level.
Not tested: cloud engines (they need a key, which must never be in the repository
or the image) and Windows SAPI voices (their licence does not clearly allow
redistributing the output).

## D36. MP3, from a browser check, not an assumption

Checked in Playwright Chromium 159 and WebKit 27.2: MP3 and AAC/m4a load and play
in both; Ogg Opus and Ogg Vorbis fail in WebKit (`canPlayType` "no", load error);
WebM Opus did not finish loading in WebKit. WAV works but is ten times larger.
A1's manifest and the audio route already use `.mp3`. Caveat: this is WebKit on
the development machine, not Safari on iOS.

## D37. Slow audio is the client's `playbackRate`

The curriculum's "slow audio (0.75x)" is `audio.playbackRate = 0.75` with
`preservesPitch = true` (both exist in the two browsers above). No second set of
files and no contract change: the audio route already honours byte `Range`.

## D38. The transcript is not served, in practice or in the exam

The manifest keeps a transcript per clip, but the practice and exam payloads carry
only `{url, duration_s}`. For practice items an additive `transcript` field could
be offered (the answer is not the point of practice). For the exam a transcript
turns Listening into Reading: whether a student who cannot hear gets an
alternative format is a **product and legal decision**, not ours.

## D39. Practice granularity follows A1, so 2.1 and 2.2 are thin

Every lesson has a six-turn dialogue, three listening items, six exercises and
eight words. Lessons 2.1 and 2.2 each list four topics in the curriculum and get
the same six exercises, so some topics get one item (see `REVIEW-A2-checkin.md`).
The curriculum rules out free narration, *will* versus *going to* nuance and
spontaneous planning; none is evaluated.

## D40. Publishing an A2 pack flips the whole pack

As for A1 (D27), the audio route serves a pack's audio when the **pack** is
published, whatever the state of each activity. Publishing only the exam would
also make the draft practice audio fetchable by id. Publish the practice content
and the exam together, or accept that.

## Form identity (`feat/b2-pack-and-form-identity`, from `592f082`)

Audit: the forms list returned `{id, title, publication_state}`. A form already
carried `language` and `level` in its file (and the catalog keys a pack by the
same pair), but the list did not expose them, the A1 and A2 check-in forms had
the same title, and nothing compared a map's `trainer.{language, level}` with
the level of the form it selected: an A1 map could save `a2-form-a`.

## D41. A form says what it is, and a map cannot pick a form of another level

- The forms list items gain `language`, `level` and `purpose` (`check_in` or
  `final`). Additive: `id`, `title`, `publication_state` are unchanged.
- `purpose` is a field of the form file (default `check_in`), written explicitly
  in the A1 and A2 forms. It is not checked against the slot a map puts the form
  in (`checkIn` or `final`): existing maps use one form in both slots.
- Titles start with the pack's name: "Inglés A1 · Evaluación diagnóstica · Forma A
  · Diagnóstico de entrada" (same for A2, B2). **ASK**: this is a proposal that
  follows the existing naming ("Inglés A2 · Turismo" for packs, the source's
  section name for the form); it is one line per form to change.
- Guard: saving settings with `trainer.enabled` and a form whose language or
  level differs from `trainer.language`/`trainer.level` is a `422
  trainer_settings_invalid` with `errors[{path, rule: "form_level_mismatch",
  message_key: "trainer_settings.form_level_mismatch"}]`, for `check_in` and
  `final`. Translation key to add on the frontend:
  `trainer_settings.form_level_mismatch`.
- **A map with no level.** `trainer.level` always has a value (it defaults to
  `A1`), so with the trainer off the level is an unchosen default, not a choice.
  The guard is therefore **not** applied while `trainer.enabled` is false, and a
  map with exams on and the trainer off can still hold a form of any level.
  Proposal, not done: make the editor send `trainer.level` whenever it enables
  exams, or add `assessments.level`, so the guard can always run. Not guessed.

## B2 (`feat/b2-pack-and-form-identity`)

## D42. A written task can keep a longer answer (`max_chars`)

The shared cap of 300 characters cuts a 120 to 160 word letter at about 45
words. `ProductiveItem.max_chars` (1 to 3000, not allowed on `template_text`)
overrides it per item; without it the cap is unchanged, so A1 and A2 behave as
before. The engine cleans the response, the rubric (`Context.limit`) and the
served item view (`max_chars`, plus `min_words`/`max_words` for written tasks)
with that limit. B2 Writing uses 1500, Speaking 900.

## D43. Writing Task 2 is one item that shows both prompts

The platform has no choice of task. The item shows (a) and (b) with one box and
the rubric credits the language of either option. It cannot check which option
was answered or that the text answers it (see the REVIEW file, e).

## D44. B2 Module I has six lessons

The exam tags lessons 1.1 to 1.6 and the linter requires every tagged lesson to
exist, so Module I has six (not four) and the pack has 18 lessons (6, 5, 4, 3).
The split of Module I is ours and needs review.

## D45. Proxy rubrics for B2 are wide and weak, on purpose and in the open

Length windows are 100 to 190 words (letters, stated 120 to 160) and 25 to 200
(Speaking), all or nothing per criterion. The proxy checks phrase presence,
sentence terminals, linking words and length; a list of the expected phrases
scores 5 of 5 in Writing Task 1 and a test pins that. Pass-rule numbers
(production needed with perfect receptive: more than 10 of 30; receptive needed
with perfect production: 31 of 45 items) are in `REVIEW-B2-checkin.md`.

## D46. Spoken text expansions are logged

Only three practice clips and the exam's `GDS` (two clips) differ between the
shown and the spoken text. `tests/test_b2_practice.py` fails if a clip is spoken
differently and is not in the REVIEW file's table.

## D47. B2 reuses the A2 audio tooling unchanged

Same `tts.json` (Kokoro-82M ONNX, `am_michael` host, `af_heart` guest, speed
0.85, 450 ms between turns, mp3 mono 24 kHz 48 kbps, -19 LUFS), same command
(`python -m app.scripts.content tts --pack en/B2`). Conversations use both
voices; dialogue partner lines use the guest voice; single-speaker clips use
the host voice.

## D31. The final exam is gated in the backend, in two phases

Audit before building (`feat/final-exam-gate`, from `9e35a2a`): the final exam
started with no precondition, the practice catalog never listed it (the
"Entrenar" entry is the `final` item of `GET /trainer/assessments`), and the
backend has no per-user completion signal: its Moodle client reads only site info
and course contents, and the launch `sub` is a pseudonym, not a Moodle user id.
Decided: Moodle is the source; the backend reads it live at attempt start, so the
plugin must sign a Moodle user id first.

Phase 1 (this change): the rule (`app/closing/gate.py`), a `CompletionProvider`
port with a refusing default and a fake for tests, `GET /trainer/closing`, the
guard on the final start, and the editor's trial run. The real Moodle read is
not built until the service account is shown able to read other users'
completion (`docs/CLOSING-GATE.md`). No per-user closing state is stored (D29).

Judgment calls:

- `CLOSING_GATE_MODE=off` is the default: no behaviour changes until it is set to
  `enforce`, and off logs a warning at startup and at every final start.
- With `off` the final **stays** in `GET /trainer/assessments`, because the
  deployed frontend reads it from there; it leaves the list only with `enforce`,
  when the frontend reads `/trainer/closing` instead. Flip after the frontend.
- Required SCORM = activity bubbles of the map holding the exam that are SCORM,
  visible, and tracked for completion. Untracked ones are excluded (Moodle
  records nothing for them, so they could never be completed). Restricted ones
  count (the service account cannot tell what a restriction means for one
  student; an exam gate fails closed). Zero required SCORM: available at once.
- Reason codes beyond the requested ones: `no_attempts_left` (the start keeps
  its existing 409 code), `form_unavailable`, `progress_unavailable` (Moodle or
  the provider failed: closed, never open).
- `already_passed` (final, or check-in with the exemption) is not available: the
  step is done and a passed student must not use attempts. Before the gate, a
  check-in pass did not stop a final start; with `enforce` it does.
- The gate runs at start and resume only. Reading, saving and submitting an
  attempt are not re-checked: an attempt cannot exist without passing the start.
  An attempt opened while the gate was off lives until its time limit.
- An editor's trial run reuses the existing `preview` attempts (stored,
  uncounted, never in the outbox, purged after 24 h) and now says
  `preview: true` in the start and result responses. It still needs a configured
  closing flow with a final step.
- The staff override for legacy cases is not built: it needs per-user storage,
  which D29 rules out for the closing flow. Pending a decision.
