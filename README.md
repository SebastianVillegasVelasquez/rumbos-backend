# Rumbos Backend

FastAPI + async SQLAlchemy 2 + Alembic backend for the Rumbos course maps. It
stores maps and their bubbles in PostgreSQL and reads course contents from
Moodle's Web Services. The JSON API is camelCase.

## Running

```bash
cp .env.example .env        # then fill in the real values
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
```

Checks (all must stay green): `uv run pytest`, `uv run ruff check .`,
`uv run mypy .`. Tests need the PostgreSQL server from the `POSTGRES_*` settings; they use
separate `<name>_test` and `<name>_migration_test` databases and never touch
the configured one. `RUN_MOODLE_SMOKE=1 uv run pytest tests/test_moodle_smoke.py`
runs an opt-in check against the real Moodle.

## Configuration

Environment variables (or `.env`); see `.env.example`.

| Variable | Default | Meaning |
| --- | --- | --- |
| `POSTGRES_USER` | required | PostgreSQL user. |
| `POSTGRES_PASSWORD` | required | PostgreSQL password. |
| `POSTGRES_DB` | required | PostgreSQL database name. |
| `POSTGRES_HOST` | `localhost` | PostgreSQL host. |
| `POSTGRES_PORT` | `5432` | PostgreSQL port. |
| `MOODLE_BASE_URL` | required | Moodle instance the service talks to. |
| `MOODLE_SERVICE_TOKEN` | required | Web Services token. Server-side only. |
| `MOODLE_CONNECT_TIMEOUT` | `5` | Seconds to connect to Moodle. |
| `MOODLE_READ_TIMEOUT` | `15` | Seconds to wait for Moodle's response. |
| `MOODLE_CONTENTS_TTL_SECONDS` | `60` | A cached copy of a course's contents is served without asking Moodle for this long. |
| `MOODLE_STALE_MAX_SECONDS` | `3600` | When Moodle fails, a cached copy up to this old is still served (reported as `cached`). |
| `ASSETS_UPLOADS_ENABLED` | `false` | Master switch for `POST /assets`. **Keep it off on anything reachable from the internet** (see "Uploads and security"). Serving existing assets never depends on it. |
| `ASSETS_DIR` | `data/assets` | Directory of the local-disk asset storage (git-ignored). |
| `ASSETS_MAX_BACKGROUND_BYTES` | `8388608` | Largest accepted background upload (8 MB). |
| `ASSETS_MAX_BUBBLE_BYTES` | `2097152` | Largest accepted bubble image upload (2 MB). |
| `ASSETS_MAX_BACKGROUND_SIDE` | `8192` | Longest side, in pixels, of a background. |
| `ASSETS_MAX_BUBBLE_SIDE` | `1024` | Longest side, in pixels, of a bubble image. |
| `ASSETS_MAX_TOTAL_BYTES` | `2147483648` | Quota: sum of every stored file (2 GB). Over it, uploads answer 507. |
| `PUBLICATIONS_KEEP` | `20` | Publications kept per map; older ones are pruned when a new one is published. The latest is never pruned (minimum 1). |
| `CORS_ALLOWED_ORIGINS` | empty | Comma-separated browser origins. Empty adds no CORS middleware. Credentials are never allowed. `If-Match` is an allowed request header and `ETag` an exposed response header. The dev frontend uses a Vite proxy and needs nothing here. |

### Moodle cache

Course contents are cached in memory, per course, with single-flight (a burst
of requests for one course makes one Moodle call) and stale-on-error. The cache
is **per process**: with several workers each has its own copy, so Moodle may be
called once per worker per TTL. A shared cache (Redis) is a later decision.

### Uploads and security

Teachers can upload their own backgrounds and bubble images (`POST /assets`,
`multipart/form-data` with `file` and `kind` = `background` | `bubble`).

> **There is no authentication yet, so the upload endpoint is anonymous.** The
> quota and `ASSETS_UPLOADS_ENABLED` are damage limiters, not protection.
> Uploads are **off by default** and must stay disabled on any publicly
> reachable deployment until auth exists.

What the pipeline does with an upload:

- Streams the body and cuts it off as soon as it passes the size cap (it is
  never read whole into memory); checks the per-kind size.
- Never trusts the `Content-Type`, file name or extension: the format is decided
  from the bytes. Only PNG, JPEG and static WebP are accepted (no SVG, GIF or
  animations).
- Decodes with Pillow in a worker thread, refuses images over the per-kind side
  limit before decoding any pixel (decompression bombs), and caps concurrent
  decodes.
- **Re-encodes** the pixels: EXIF (GPS included), XMP, comments and any data
  appended after the image are gone; the EXIF orientation is applied first.
  Palette/CMYK/grayscale become RGB(A).
- De-duplicates by the SHA-256 of the stored bytes and kind (the same upload
  again answers `200` with the existing asset).
- Backgrounds also get a WebP thumbnail, at most 640 px wide.

Files are served from `GET /assets/{id}` (and `/thumb`) with the stored
`Content-Type`, an `ETag`, `Cache-Control: public, max-age=31536000, immutable`,
`X-Content-Type-Options: nosniff` and `Content-Security-Policy: default-src
'none'; sandbox`. Any id that is not a known asset is a 404.

Storage sits behind the `AssetStorage` Protocol (`LocalDiskAssetStorage` today;
an S3 adapter needs no service changes).

**Deleting is anonymous too.** `DELETE /assets/{id}` and `PATCH /assets/{id}`
need no authentication (and, unlike uploads, no switch turns them off), so
anyone who can reach the API can delete an image that nothing uses or rename
any image. What is protected is images **in use**: a delete is refused with
409 `asset_in_use` (and `usage: { maps, skins, publications }`) while a draft
map, a skin or any publication references it, so a published map can never
lose its picture. The check and the row delete are one SQL statement; it is not
a lock, so a map that starts using the image in the very same instant can still
lose it. The row is deleted first and the files after: if removing the files
fails the asset is simply gone (the failure is logged and the bytes become
unreachable garbage), never listed-but-broken. Until auth exists, treat the API
as trusted-network only.

Every asset has a `title` (default: the file name without extension), an
optional `credit` and the sanitized `originalFilename` (display text; stored
files are named by id only). A map whose `imageUrl` is `/assets/{id}` must name
an existing `background` asset (422 `background_asset_not_found` /
`background_asset_wrong_kind`); any other URL is accepted as before.

### Draft and published maps

Every editor endpoint reads and writes the **draft** (the normalized rows).
**Publishing** stores an immutable snapshot of it (title, background, settings,
default skin and rules, the skins involved as frozen copies, and the bubbles'
position/icon/skin/order) as publication `n` of that map; students read the
latest one through `/published/...`. Live data is never in the snapshot: bubble
`status`, the level `position`, `moodleCourseId` and `moodleSectionId` are
joined from the live rows. Bubble order is stored as the dense rank 0..n-1.

The draft's `draftHash` is the SHA-256 of the canonical JSON of the snapshot it
would produce (sorted keys, no whitespace, floats normalized to 12 significant
digits), so it does not depend on key order or number formatting. An editor
reviews `GET /course-maps/{id}/publication-state` and publishes with that
`draftHash`; if the draft moved in between, publish answers 409
`draft_changed_since_review`. Publishing runs in one `REPEATABLE READ`
transaction (retried once on a serialization failure) and refuses unchanged
drafts (409 `no_changes_to_publish`) and drafts whose background or skin images
no longer exist (422 `publish_invalid`, every problem listed).
`discard-changes` and `restore` load a snapshot back into the draft in one
transaction (they do **not** publish); skins that no longer exist are reset
with `skin_missing` warnings.

Because skins are global, editing or deleting a skin a map uses changes that
map's draft (it shows as `skinsChanged`) but never what students see until the
map is published again; `discard-changes` cannot undo a skin edit.

> **This is not access control.** There is no authentication: a draft is not
> hidden from anyone who calls the editor endpoints directly. "Published" only
> means "what students are shown, consistently".

### Concurrent edits (`If-Match`)

`CourseMapRead.revision` and `BubbleRead.version` start at 1 and travel as the
`ETag` (`"3"`) of single-resource responses. Guarded writes must send the number
they are based on as `If-Match: "3"`: `PATCH /course-maps/{id}`,
`PUT .../appearance`, `POST .../discard-changes`, `POST .../publications/{n}/restore`
(map revision) and `PATCH`/`DELETE .../bubbles/{id}` (bubble version; the PATCH
only when the body has `x`, `y`, `icon` or `skinId`). Missing header: 428
`precondition_required`; malformed: 400 `invalid_if_match`; stale: 412
`version_conflict` with `currentRevision` / `currentVersion`. Each guarded write
is a single `UPDATE ... WHERE revision = :expected` that also adds 1, so of two
editors on the same number exactly one wins. A PATCH with **only** `status` is
live demo data: it needs no `If-Match` and does not bump the version. Creating
maps/bubbles and the two `order` endpoints are not guarded.

### Operations

Run from the project root with the app's environment (`POSTGRES_*`, ...).
Both are **dry runs unless `--apply` is given**.

- **Required deploy step:** `python -m app.scripts.publish_existing_maps --apply`
  after migrating. Maps created before publishing existed have no publication
  and are invisible to students until this publishes them as #1 ("Publicación
  inicial (migración)"). Only maps with at least one bubble and no publication
  are touched, so it is safe to run again.
- `python -m app.scripts.gc_assets [--older-than-days 7] [--apply]` lists, and
  with `--apply` deletes, assets that no map, skin or publication uses and that
  are older than the threshold, reporting the bytes reclaimed. It uses the same
  usage rule as `DELETE /assets/{id}`.

### Demo skin pack

`uv run python scripts/make_demo_skin_pack.py` writes an original set of six
256x256 transparent PNGs (`available`, `locked`, `next`, `inProgress`,
`complete`, `hover`, all on identical artboards) to `tmp/demo-skin-pack/`
(git-ignored). Upload them as `kind=bubble` assets and reference them from an
image skin to exercise the whole pipeline. It is **test-fixture material, not
production art**: real skin art is a separate deliverable.

## API

A Moodle course can have several maps ("levels"). Each map has a `position`
among its course's maps and may be tied to one Moodle section
(`moodleSectionId`, a section id, never a module id). A map's `moodleCourseId`
never changes after creation.

- `GET /course-maps?moodleCourseId=&q=&limit=&offset=`: list (`limit` default
  24, max 100). With `moodleCourseId` the items are the course's levels in
  `position` order; otherwise `updatedAt` desc. Items carry `bubbleCount` and
  `completeCount`.
- `POST /course-maps` (appended as the course's last level),
  `PATCH /course-maps/{id}`, `DELETE /course-maps/{id}` (its bubbles go with
  it, freeing their activities), `GET /course-maps/{id}` (database only, never
  calls Moodle).
- `PUT /course-maps/order` `{ moodleCourseId, mapIds }`: reorders a course's
  maps; `mapIds` must be exactly its maps (422 `order_mismatch`).
- `PUT /course-maps/{id}/appearance` `{ settings, defaultSkinId, skinRules }`:
  replaces the map's presentation settings (mode, fit, initial view, path style,
  ambient effects, intro), its default skin and its per-activity-type skin rules
  (`{ modname, skinId }`) in **one transaction**: all or nothing (422
  `skin_not_found`, `duplicate_skin_rule`). Settings are always read through the
  `MapSettings` model, so missing keys (or an empty `{}` column) read as defaults;
  unknown keys are rejected on write.
- `PUT /course-maps/{id}/bubbles/order` `{ bubbleIds }`: the guided path.
  Must be exactly the map's bubbles (422 `order_mismatch`); sets `sequence`
  0..n-1. Only this endpoint changes `sequence`.
- `POST|PATCH|DELETE /course-maps/{id}/bubbles[/{bubbleId}]`. An activity can
  have one bubble per **course**, on any of its maps.
- `GET /course-maps/{id}/activities?includeHidden=&onlySection=`: the course's
  activities, with `sectionId`, `placed` (on any map of the course) and
  `placedInMapId`. `onlySection=true` keeps only the map's section (ignored for
  a map without one).
- `GET /course-maps/{id}/resolved?includeHidden=`: each bubble's
  `availability` (`available`, `hidden`, `missing`, `unknown`) and the
  `moodleStatus` (`live`, `cached`, `unavailable`). Moodle outages answer 200.
- `POST /assets` (201, or 200 for already-known content; optional form fields
  `title`, `credit`), `GET /assets?kind=&q=&limit=&offset=` (the library, newest
  first, with `usage` counts), `PATCH /assets/{id}` (`title`, `credit`),
  `DELETE /assets/{id}` (409 while in use), `GET /assets/{id}`,
  `GET /assets/{id}/thumb` (backgrounds only). A map's `imageUrl` may be a
  bundled frontend path or `/assets/{id}`.
- `GET /course-maps/{id}/publication-state`, `POST .../publish`,
  `GET .../publications?limit=`, `POST .../discard-changes`,
  `POST .../publications/{n}/restore`: see "Draft and published maps". Map list
  items carry `publication: { status, number, publishedAt }`.
- `GET /published/course-maps?moodleCourseId=&limit=&offset=`,
  `GET /published/course-maps/{id}` (404 `not_published`) and
  `GET /published/course-maps/{id}/resolved?includeHidden=`: the student view
  (maps with at least one publication only). `resolved` uses the same resolver
  and Moodle cache as the editor's.
- `GET /skins`, `POST /skins`, `PATCH /skins/{id}`, `DELETE /skins/{id}`: how
  bubbles look. A skin is a validated `config` (`kind` `procedural` or
  `image`; every key is checked, unknown ones rejected). Image skins reference
  bubble assets per state, and all of a skin's state images must have the same
  width and height. Four built-in skins (Orbe, the default, Insignia, Pin,
  Hexágono) are seeded by a migration; they cannot be edited or deleted (403
  `skin_is_builtin`). Deleting a skin clears it from the bubbles and maps that
  used it. `PATCH` on a bubble also takes `skinId` (nullable); bubbles carry a
  `sequence` (their place in the guided path; new ones go last).
- `GET /health` (liveness) and `GET /health/ready` (database `SELECT 1`; 503 if
  it fails; never calls Moodle).

Conflicts (409) carry `detail: { code, message }`: `map_already_exists_for_section`,
or `activity_already_placed` (plus `courseMapId` and `courseMapTitle` of the map
that already holds the activity).

`includeHidden=true` currently needs no authentication; it must require a
teacher/editor role once auth exists.
