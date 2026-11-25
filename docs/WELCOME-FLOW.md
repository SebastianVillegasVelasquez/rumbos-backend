# Welcome flow

A map can open with an ordered **flow of steps** instead of the single welcome
curtain: a greeting, a video, a piece of HTML, a PDF document, a quiz. This page
is the reference for what the backend stores, what it refuses, and why. The
backend stores and validates the flow; it never stores what a student did with
it (progress, acknowledgements, quiz results live elsewhere).

Everything here is part of `settings.welcome`, so it is written with
`PUT /course-maps/{id}/appearance` (same `If-Match` rules) and travels through
draft, publish, history, discard and restore like the rest of the settings.

## The model

```
welcome: {
  steps: [Step] | null    1..12 steps; null or absent: the legacy curtain
  show: "first_visit" | "always"
  enabled: bool
  title, message, video, playOnEnter     legacy fields, see below
}
```

**Legacy and steps are exclusive.** With `steps`, the legacy fields `title`,
`message`, `video` and `playOnEnter` must be null or absent
(`422 welcome_invalid`, problem code `legacy_fields_with_steps`). Reads of a
flow return them as `null`. Without `steps`, nothing changed: a legacy curtain
reads, writes and hashes exactly as before.

Every step has:

| field | rule |
| --- | --- |
| `key` | `^[a-z0-9]{6,16}$`, unique in the flow (`step_key_duplicate`) |
| `type` | `welcome`, `video`, `html`, `pdf`, `quiz` or `moodle_activity` (`step_type_invalid`) |
| `title` | 1..120 characters |
| `mandatory` | default `true` |
| `revision` | 1..9999, default 1. Bump it to make students see a changed step again |

| type | fields | rules |
| --- | --- | --- |
| `welcome` | `message` (<=300), `buttonLabel` (<=40) | first step only, at most one (`welcome_step_position`); `{curso}` is the only placeholder |
| `video` | `video {provider: youtube\|vimeo, videoId}`, `allowFallback` (default true) | same video rules as a content bubble (`video_invalid`) |
| `html` | `html` (<=100 KB), `css` (<=50 KB or null), `ack {label 1..160}` or null, `minSeconds` 0..600 | see "HTML and CSS" |
| `pdf` | `assetId`, `ack`, `minSeconds` | the asset must be a usable document (`document_unavailable`) |
| `quiz` | `cmid` (positive or null), `intro`, `outcomeAbove`, `outcomeBelow` (<=300 each) | at most 3 (`quiz_too_many`); the backend never grades |
| `moodle_activity` | `cmid` (integer > 0, required) | an activity of the course opened from Moodle, see "The `moodle_activity` step" |

Flow limits: at most 12 steps (`steps_too_many`) and at most 400 KB of HTML plus
CSS together (`flow_too_large`).

## The `moodle_activity` step

```json
{"type": "moodle_activity", "key": "actividad1", "title": "Guia de estudio", "cmid": 42}
```

A Moodle activity (a PDF `resource`, a page, a SCORM...) that the student opens
from the flow. It is allowed in the welcome **and** the closing flow (one
validator table), counts toward the 12-step limit and shares the key space of the
other steps. The file lives in Moodle, so a PDF shown this way needs no upload at
all. Nothing else is stored: no URL, no name, no type. A `url`, `link` or any
other field is refused (`unknown_field`), `cmid` <= 0 is `out_of_range`, a
missing `cmid` or `title` is `required`.

**`cmid` is specific to one course.** It is the Moodle course-module id, and a
duplicated or restored course gives its activities new ids. The backend cannot
check that an id exists or is the activity the author meant (it saves the step
without asking Moodle), so the frontend must degrade gracefully: when the plugin
says it cannot open the activity (`not_found`, `forbidden` or `unsupported`)
show "Actividad no disponible" and let the student continue if the step is not
mandatory. Opening an activity is the plugin's job (host contract), which
builds the URL itself from a `cmid` it validated: the backend never produces or
accepts one. A future export and import of flows must reset these ids as it
does the quiz one (see "Not done here").

## Errors

`422 welcome_invalid` with `detail.problems: [{path, code, message}]`. Paths are
JSON-style (`welcome.steps[2].html`), every problem is listed, and messages are
fixed texts that never echo the input (only the name of an offending tag or
attribute may be shown). Codes: `legacy_fields_with_steps`, `step_key_duplicate`,
`step_type_invalid`, `welcome_step_position`, `html_forbidden_markup`,
`css_forbidden`, `document_unavailable`, `steps_too_many`, `flow_too_large`,
`video_invalid`, `quiz_too_many`, plus the generic `required`, `too_short`,
`too_long`, `invalid_type`, `invalid_value`, `unknown_field`.

Nothing is written unless the whole appearance is valid.

> The problem `path` of every `welcome_invalid` error now starts with `welcome.`
> and uses `[n]` for list positions (`welcome.video.videoId`, not `video.videoId`).
> Clients that matched the old flat paths must be updated.

## HTML and CSS

The author's markup is **rejected, never sanitized**: a flow is either exactly
what the author wrote or it is refused with the reason. Scripts are out of scope.

**HTML** (`app/welcome_flow/html.py`) is checked against an allowlist:

- Tags: headings, paragraphs, lists, tables, `a`, `img`, `figure`, `blockquote`,
  `details`/`summary` and similar text-level elements. No `script`, `style`,
  `iframe`, `object`, `embed`, `form`, `svg`, `math`, `base`, `meta`, `link`.
- Attributes: a short allowlist plus `aria-*`. No `on*` handlers, no `style`
  attribute, no `srcset`.
- `a href` must be `https:`. No `javascript:`, `data:`, relative or protocol-
  relative links.
- `img src` must be one of this API's assets (`/api/v1/assets/{id}`, with or
  without `/thumb`) or a `data:image/(png|jpeg|webp|gif);base64,...` of at most
  200 KB whose bytes match the declared type.
- Comments, declarations, processing instructions, CDATA, control characters
  and nesting deeper than 100 levels are refused. A second, independent scan of
  every `<` backs up the parser, so a form the parser reads differently from a
  browser is still caught.

**CSS** (`app/welcome_flow/css.py`) is parsed with `tinycss2`, which decodes
escapes, so `\69mport` or `expr\65ssion(` cannot hide a forbidden construct:

- No `@import`, no remote `url()` or fonts (`@font-face` accepts only `data:`),
  no `expression()`, `behavior`, `-moz-binding`, `javascript:`.
- `url()` accepts only `data:image/(png|jpeg|webp|gif)` and this API's assets.
- `</` and `<!` are refused anywhere, and unbalanced or unterminated input is
  refused.

**The security boundary is the frontend**, which shows the HTML in a sandboxed
frame under a strict CSP. These checks are defense in depth for the case where
that is misconfigured; they are not a substitute for it.

Images an HTML step shows count as uses of the asset (see "Documents and
deletion"), so an image in a flow cannot be deleted from under it.

## Documents

A PDF is uploaded as an asset of kind `document` and referenced by a `pdf` step.

```
POST /assets   multipart: file, kind=document, title, rightsDeclared=true
  201 created  |  200 an identical document was already there
```

- `title` is required (<=200 characters; other kinds keep 120).
  `rightsDeclared` must be exactly `"true"` (`422
  document_rights_declaration_required`). The declaration time and the
  pseudonymous uploader id are recorded.
- Size: at most `DOCUMENT_MAX_BYTES` (default 10 MB), else `413
  asset_too_large`. Documents count against the tenant's quota (`507`) and
  follow `ASSETS_UPLOADS_ENABLED`.
- Inspection (`app/assets/document.py`) never renders the PDF. It must start
  with `%PDF-`, parse strictly, not be encrypted, have at most 200 pages and
  contain no active content. Otherwise `422 document_invalid` with
  `detail.reason`:

| reason | meaning |
| --- | --- |
| `not_pdf` | empty, or not a PDF by its bytes (the declared type and extension decide nothing) |
| `encrypted` | the file is encrypted |
| `active_content` | JavaScript, Launch, embedded files, rich media, forms that submit or import, remote or embedded go-to actions, XFA, renditions, movies, sound, screens, 3D, file attachments, or actions that run on open (`OpenAction`, `AA`, annotation actions). Names are decoded first, so `/J#61vaScript` is caught. Plain `URI` links are allowed |
| `too_many_pages` | over 200 pages, declared or counted |
| `corrupt` | does not parse, loops, or inspecting it took longer than `DOCUMENT_SCAN_TIMEOUT_SECONDS` |

The inspection runs in a worker thread under a timeout. A timeout cannot stop
the thread itself, only stop waiting for it.

- The file is **stored exactly as uploaded**: it is neither rewritten nor
  compressed, so its metadata (author, producer, dates) is not stripped. The
  uploader's file name is dropped and never reaches a client.
- Served from `GET /api/v1/assets/{id}` as `application/pdf`,
  `Content-Disposition: inline` (no file name), `nosniff`, immutable cache and
  `Range`. Unlike other assets it carries no `sandbox` CSP, which would stop a
  browser's own PDF viewer.
- Documents belong to a tenant; there is no library of shared documents
  (`GET /assets?kind=document&scope=tenant`). A quarantined document is not
  served or listed, and uploading the same bytes again does not bring it back.
- `available` (in listings and in `welcomeDocuments`) is false when the file
  is missing from storage.

> **Documents are reachable by their unguessable URL.** Like every asset, anyone
> holding the URL can read it. Do not upload confidential material, and tell
> editors so.

### Documents and deletion

A document, or an image shown by an HTML step, is **in use** while a draft map's
flow or any publication's snapshot mentions it. Deleting it answers `409
asset_in_use` with `usage {maps, skins, publications, tracks}`. A published map
keeps its documents after the draft moves on.

When a flow is saved, every PDF step's document must exist, be a document of
this tenant, not be quarantined and have its file (`document_unavailable` at
`welcome.steps[i].assetId`).

## Read models

`CourseMapRead` and `PublishedMapRead` carry a **live** list:

```
welcomeDocuments: [{ assetId, url, title, pageCount, bytes, available }]
```

one entry per distinct PDF step document, in step order. It is computed at
request time and is **not** in the snapshot or its hash: taking a document down
or losing its file shows at once and never makes a publication `unpublished
changes`. An unavailable document keeps its entry with `available: false` and
empty `title`, `pageCount` and `bytes`; the student side should skip that step.

## The closing flow

`settings.closing` is the welcome flow's twin, shown when the student finishes
the level. It has the same shape (`steps`, `show`, `enabled`) and goes through
the same validators, with the same error shape (`422 welcome_invalid`, paths
under `closing.`). `null` or absent: there is no closing flow, and the map is
stored and hashed exactly as before (`to_stored()` leaves the key out).

What it may hold: every content step the welcome flow has (`html`, `pdf`,
`video`, the Moodle `quiz` step and the `moodle_activity` step, same rules and limits, one shared validator
table in `app.welcome_flow.flow`) and at most one `assessment` step with
`purpose: "final"`. Refused: the `welcome` greeting
(`step_type_not_allowed`), an assessment step with another purpose
(`assessment_purpose`, which also refuses `final` in the welcome flow), and a
closing flow without steps (`closing_needs_steps`). Steps reorder freely.

The final-exam step needs `assessments.enabled` and a `final` policy (422
`trainer_settings_invalid`, rule `assessment_step_needs_final`, path
`closing.steps[i]`). The form it points at is the one of `assessments.final`: an
unknown form is a 422 `unknown_form` at `assessments.final.form_id`, an
unpublished one saves with a `content_not_published` warning.

**Step keys are unique across both flows.** Moodle keeps the progress of a step
in `format_rumbos_flow` under (course, user, map, step key), with no flow in
the key, so a key shared by a welcome step and a closing step would share one
row. The duplicate is reported at `closing.steps[i].key`.

The PDFs of the closing flow are listed in `welcomeDocuments` next to those of
the welcome flow (welcome steps first, then closing ones, each document once),
in the editor read, the publication read and the published read, resolved by the
same code as the welcome ones (`usable_documents`, read live, `available: false`
when the asset is gone). The assets of both flows keep a publication or a draft
from losing them. The frontend therefore needs no second list.

**The OpenAPI tells the two flows apart.** `MapSettings.closing` is the
`ClosingFlow` schema: its `steps` offer `video`, `html`, `pdf`, `quiz`, `moodle_activity` and an
`assessment` step whose `purpose` is `final`, and not the `welcome` greeting.
At runtime a step outside that is still reported as a `{path, code, message}`
problem (`422 welcome_invalid`), never as a generic validation error, so the
error shape is the welcome flow's. Maps stored before this load unchanged.

**Where the closing progress lives.** The backend stores none of it: which
steps a student has seen or acknowledged is kept in Moodle (`format_rumbos_flow`,
D29), keyed by course, user, map and step key. No endpoint is needed or planned.
The only closing state the backend answers is the final exam's, through
`GET /trainer/closing`.

**Not decided here: when the level is finished.** The backend has no such
signal, and per-student progress of the flows lives in Moodle (see
`docs/DECISIONS-trainer.md`, D29). `closing` only carries the definition.

## Hash stability

A flow with no `steps` is stored and hashed exactly as before: `to_stored()`
leaves `welcome.steps` out when it is null, and a default curtain is left out
altogether. Publications made before flows existed still read as `up_to_date`.
The same holds for `closing`: a map without one has no `closing` key.
`tests/test_welcome_flow.py::PRE_FLOW_HASHES` pins the hashes of five legacy
cases that were computed with the code before this feature.

## Migration

Revision `a5d8c3f1e692` adds `assets.page_count`, widens `assets.title` to 200,
adds the `document` branch to `ck_assets_kind_fields` and adds
`ck_assets_page_count`. Settings are JSON, so flows need no migration. The
downgrade is refused while documents exist.

Before applying it on a real database, back it up:

```
pg_dump -Fc -h <host> -U <user> -d <db> -f rumbos-before-documents.dump
uv run alembic upgrade head
```

## Not done here

- **Export and import** of flows (`.rumbos` files) is not implemented: the
  exporter and importer do not exist yet on the branch this builds on. When
  they do, the agreed behavior is: flows travel; a quiz `cmid` is reset to null
  with the warning `flow_quiz_unlinked` (a `moodle_activity` step's `cmid` is
  course-specific too and would need the same treatment); a document that
  cannot travel is dropped with `document_dropped`.
- Per-student progress, quiz grading and scripts in HTML steps are out of scope.
