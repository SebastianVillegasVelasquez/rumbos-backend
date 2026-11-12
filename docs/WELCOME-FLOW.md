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
| `type` | `welcome`, `video`, `html`, `pdf` or `quiz` (`step_type_invalid`) |
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

Flow limits: at most 12 steps (`steps_too_many`) and at most 400 KB of HTML plus
CSS together (`flow_too_large`).

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

## Hash stability

A flow with no `steps` is stored and hashed exactly as before: `to_stored()`
leaves `welcome.steps` out when it is null, and a default curtain is left out
altogether. Publications made before flows existed still read as `up_to_date`.
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
  with the warning `flow_quiz_unlinked`; a document that cannot travel is
  dropped with `document_dropped`.
- Per-student progress, quiz grading and scripts in HTML steps are out of scope.
