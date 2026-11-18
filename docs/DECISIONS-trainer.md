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
`unknown_pack`, `no_exam_configured`, `assessment_step_needs_check_in`.
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
