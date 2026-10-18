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

## Tenants and authentication

Rumbos is a Moodle course format: a thin plugin (`format_rumbos`) embeds the app
in the course page. Each Moodle site that uses it is a **tenant**, registered on
this service by an operator. Nothing here is anonymous for a tenant: the plugin
signs a short-lived assertion, this service trades it for a bearer token bound
to `(tenant, course, role)`, and every request is scoped to that.

### Why the tenant model is the real protection against reuse

The plugin's code is public and copyable, and so is the frontend bundle. What a
copy cannot have is a **registered tenant**: an assertion is only accepted if its
`iss` is a registered Moodle `wwwroot` and its signature verifies against a public
key the operator registered for that site. A copied plugin on an unregistered
site (or one that holds a different private key) gets `401 launch_invalid`, and
the SPA's `frame-ancestors` policy only lets registered sites embed it.

### Registering a Moodle site (end to end with the plugin)

1. In the Moodle site, the plugin generates an RSA key pair (at least 2048 bits)
   and keeps the private half in the site's configuration. It shows the public
   key (PEM) and its `kid` to the administrator.
2. The operator saves the public key to a file and registers the site:

   ```
   python -m app.scripts.tenants add --name "Universidad Demo" \
       --issuer https://moodle.demo.edu --host-origin https://moodle.demo.edu \
       --public-key-file demo.pub.pem [--kid <kid>] [--asset-quota-bytes N]
   ```

   `--issuer` is the Moodle `wwwroot` exactly as the plugin sends it as `iss`
   (https, no trailing slash; the host is lower-cased and a `:443` dropped).
   `--host-origin` is the origin that embeds the app. Without `--kid`, the kid is
   the first 16 hex digits of the SHA-256 of the key's DER `SubjectPublicKeyInfo`;
   the plugin must put the same `kid` in its JWS header. The key must be a
   parseable RSA public key of at least 2048 bits. `--asset-quota-bytes` defaults
   to `ASSETS_MAX_TOTAL_BYTES`.
3. Run `python -m app.scripts.tenants frame-ancestors`, put the printed header in
   the nginx that serves the SPA, and reload it (see below).
4. In the plugin, set the Rumbos URL. Opening a course page now posts a signed
   assertion to `POST /api/v1/auth/launch`.

Other commands: `list`, `disable --issuer ...` / `enable --issuer ...` (a disabled
tenant gets `403 tenant_disabled` on launch and all its tokens stop working at
once), `add-key`, `revoke-key`. There is deliberately **no admin HTTP surface**.

### The launch

Assertion: a JWS, `RS256` only (anything else, `none` and `HS*` included, is
rejected), header `{alg, typ: "JWT", kid}` (no other header), claims
`iss` (Moodle wwwroot), `aud` (`AUTH_AUDIENCE`, default `rumbos`), `sub` (32 hex
characters, pseudonymous), `course` (positive int), `role` (`editor` | `student`),
`iat`, `exp` (at most 60 s after `iat`; 30 s of clock skew tolerated) and `jti`
(at least 128 random bits, base64url or hex).

`POST /api/v1/auth/launch` with `{"assertion": "<JWS>"}` checks, in order: size
(at most 4 KB) → parse without trusting → tenant by `iss` → the key named by `kid`
among the tenant's **active** keys → signature → `aud`, times, `course`, `role`,
`sub`, `jti` format → tenant is active → **single use of the `jti`** (a primary
key on `used_jtis`, so a replay, even a simultaneous one, fails) → token.

- `200 { accessToken, tokenType: "Bearer", expiresIn, role, courseId, hostOrigin }`
- `401 launch_invalid`: **one answer for every verification failure**. The reason
  (`bad_signature`, `expired`, `replay`, `unknown_issuer`, ...) is only in the
  server's `rumbos.audit` log, with the tenant and client address, never the
  assertion or the token (untrusted values are logged with `repr`, truncated).
- `403 tenant_disabled`, only after the signature verified (an outsider cannot
  probe which sites exist), `429 rate_limited` (with `Retry-After`).

The token is opaque, 256 random bits, stored only as its SHA-256, valid
`ACCESS_TOKEN_TTL_SECONDS` (30 min), bound to tenant + course + role. No cookies
anywhere; send `Authorization: Bearer <token>`. `GET /auth/me` returns
`{ role, courseId }`. Responses under `/auth` carry `Cache-Control: no-store`.

**Rate limits** (`/auth/launch`): `LAUNCH_RATE_PER_IP` (30/min, an IPv6 client is
keyed by its /64) counts every attempt; `LAUNCH_RATE_PER_TENANT` (300/min) counts
only assertions whose signature verified, so a forger cannot use up a site's
allowance. The counters are in **process memory** (fixed one-minute windows): they
reset on restart and are not shared between workers, so with `WORKERS=n` the
effective limit is up to n times higher. Behind nginx, run uvicorn with
`--proxy-headers --forwarded-allow-ips=<nginx>` or every client looks like the proxy.

### What each role can do

| | no token | student | editor |
|---|---|---|---|
| `GET /published/**` | 401 | its own course | its own course |
| draft endpoints (`/course-maps/**`, `/skins`, `/assets`, `/view3d/**`) | 401 | 403 | its own tenant (maps: its own course) |
| `GET /assets/{id}`, `/thumb` | public by id | | |

Foreign resources (another course or another tenant) are **404, never 403**.
Scoping is enforced below the repositories (`app/tenancy/scope.py`): a session
carries the caller's tenant and course and every query is rewritten with it, new
rows are stamped with it, and a session without a scope that touches tenant data
raises instead of leaking. Custom skins and the asset library belong to the
tenant (shared by its courses); built-in skins are visible to all. The asset
quota is per tenant (`asset_quota_bytes`). In plugin mode the course-maps list
returns only the token's course.

The Moodle-web-service endpoints (`/course-maps/{id}/activities`,
`/course-maps/{id}/resolved`, `/published/course-maps/{id}/resolved`) answer
`501 moodle_source_not_provided` for plugin tenants: per-user Moodle state
(visibility, restrictions, completion) comes from the plugin through the browser,
this service never calls Moodle for them and stores no student personal data.
Activity ids in bubbles are design data; the backend never accepts Moodle data
from clients as proof of anything.

**Image ids are the access control for images.** `GET /assets/{id}` and the
thumbnail need no header (an `<img>` or a canvas cannot send one); the random
UUID is the only protection. Ids are shown only to the tenant's editors and
inside the maps its students may read.

### `frame-ancestors`

`python -m app.scripts.tenants frame-ancestors` prints
`Content-Security-Policy: frame-ancestors https://a https://b` built from the
`host_origin` of every active tenant (`'none'` when there are none). In the nginx
that serves the SPA: `add_header Content-Security-Policy "frame-ancestors ...;" always;`.
Re-run it and reload nginx after every `add`, `enable`, `disable` (the policy does
not update itself).

### Legacy mode and how to retire it

`LEGACY_ANONYMOUS_MODE` (default: on when `APP_ENV=dev`, off in `production`)
keeps the old behaviour for the **legacy tenant**, which every pre-existing row
belongs to: a request without a token acts as an anonymous editor of that tenant
over every course, and the Moodle endpoints still use `MOODLE_SERVICE_TOKEN`. A
token that is sent but invalid is refused even then. The legacy tenant has no keys,
so no launch can ever name it. To retire it: move each site to the plugin
(register it, re-create its maps under its tenant), set `LEGACY_ANONYMOUS_MODE=false`
(the production default), and finally drop the Moodle settings and client.

### Key rotation

`add-key` the new public key, switch the plugin to sign with it (`kid` in the
header), then `revoke-key` the old one. Both work in between (the key is chosen by
`kid`). A `kid` is never reused, even after revocation. Revoking a key stops
launches with it; tokens already issued live out their 30 minutes. `disable`
kills tokens at once.

### Cleanup

`python -m app.scripts.purge_auth` deletes expired tokens and redeemed `jti`s
(expired rows are already refused; this keeps the tables small). Run it daily from
cron. A `jti` is kept until its assertion could no longer pass the time checks.

### What is NOT protected

- Design data (maps, bubbles, published snapshots) is readable by **any enrolled
  student of that course**, and the ids of its images with it. It is not secret.
- The frontend bundle and the plugin's code are inspectable in a browser; the
  protection is the registered tenant and its private key, not obscurity.
- A leaked private key lets its holder launch as that site until the key is
  revoked.
- Image bytes are fetchable by anyone who knows the id (above).

### Uploads and security

Teachers can upload their own backgrounds and bubble images (`POST /assets`,
`multipart/form-data` with `file` and `kind` = `background` | `bubble`).

> Uploads, edits and deletes are **editor-only** and stay inside the editor's
> tenant (see "Tenants and authentication"); the quota is per tenant. In legacy
> mode the endpoint is anonymous: `ASSETS_UPLOADS_ENABLED` (off by default) must
> stay off on a publicly reachable legacy deployment.

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

Files are served from `GET /api/v1/assets/{id}` (and `/thumb`) with the stored
`Content-Type`, an `ETag`, `Cache-Control: public, max-age=31536000, immutable`,
`X-Content-Type-Options: nosniff` and `Content-Security-Policy: default-src
'none'; sandbox`. Any id that is not a known asset is a 404.

Storage sits behind the `AssetStorage` Protocol (`LocalDiskAssetStorage` today;
an S3 adapter needs no service changes).

**Deleting** needs an editor of the tenant. What is protected is images **in use**: a delete is refused with
409 `asset_in_use` (and `usage: { maps, skins, publications }`) while a draft
map, a skin or any publication of the tenant references it, so a published map
can never lose its picture (usage counts every course of the tenant). The check
and the row delete are one SQL statement; it is not a lock, so a map that starts
using the image in the very same instant can still lose it. The row is deleted
first and the files after: if removing the files fails the asset is simply gone
(the failure is logged and the bytes become unreachable garbage), never
listed-but-broken.

Every asset has a `title` (default: the file name without extension), an
optional `credit` and the sanitized `originalFilename` (display text; stored
files are named by id only). A map whose `imageUrl` is `/api/v1/assets/{id}` must name
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
with `skin_missing` warnings, and a 3D scene that is no longer in the registry
is dropped with a `scene_unknown` warning (see "3D view").

Because a tenant's skins are shared by its courses, editing or deleting a skin a map uses changes that
map's draft (it shows as `skinsChanged`) but never what students see until the
map is published again; `discard-changes` cannot undo a skin edit.

> For plugin tenants this **is** access control: drafts are editor-only and a
> student reads only the published map of its own course. In legacy mode there
> is no authentication, and "published" only means "what students are shown,
> consistently".

### 3D view

A map can show a hand-authored 3D world ("La Región de los Encuentros", built
with three.js) in addition to the flat map. The backend only stores **which
scene a map uses and which bubble is bound to which zone**, with optional ES/EN
text overrides. It does not serve 3D assets (textures and sprites are static
frontend files), does not call Moodle for this and stores no tokens or
credentials. Students only view; editors configure bindings in the editor.

**Scene registry.** `app/view3d/scenes.py` is a code constant mapping a scene id
to its number of zones (today `region-encuentros` -> 9). Adding a scene is a code
change deployed together with the frontend; maps store only the id, so it needs
no migration. `GET /view3d/scenes` serves it: `{ items: [{ id, zoneCount }] }`.
A generic 3D view for any course is not possible this way (the world is
hand-authored per scene).

**Contract.** `CourseMapRead.view3d` and `PublishedMapRead.view3d` are
`null` or `{ scene, bindings: [{ zoneId, bubbleId, title, description }] }`,
bindings ordered by `zoneId`. `title` / `description` are `null` or
`{ es?, en? }` (a language that was not given is absent, not `null`); the
frontend falls back to the activity name and the scene's own text.
`PUT /course-maps/{id}/view3d` takes that object (or a JSON `null`, which
removes the 3D view), needs `If-Match` (428 / 412 as everywhere), bumps the map
revision and replaces the scene and every binding in one transaction (a single
`UPDATE ... WHERE revision = :expected`), so one invalid binding changes
nothing. A request without any body is a 422, so a forgotten payload cannot
remove the view. 422 codes, checked in this order, with `detail = { code,
message, ...extras }`: `scene_unknown` (`scene`, `scenes`), `zone_out_of_range`
(`zoneIds`, `zoneCount`; valid zones are `0 <= zoneId < zoneCount`),
`duplicate_zone` (`zoneIds`), `duplicate_bubble` (`bubbleIds`),
`bubble_not_in_map` (`bubbleIds`). Malformed bodies (wrong types, unknown keys,
over-long text) are FastAPI's usual 422 with a list `detail`. Deleting a bubble
deletes its binding in the same transaction (foreign key cascade); the scene
stays.

**Text** is plain text. On write it is normalized to Unicode NFC, control
characters are removed (line breaks stay in descriptions, normalized to LF), it is
trimmed and limited to 120 characters (title) / 600 (description), counted after
cleaning. A language left empty after cleaning is not stored, and a title or
description with no language left is `null`. Nothing is ever interpreted; the
frontend renders it with `textContent`, but it is treated as untrusted anyway.

**Draft and published.** The 3D view is part of the map's content: the snapshot
holds it, `publication-state.changes.view3dChanged` reports a difference
(added, removed, another zone or bubble, or only a text), the published read
returns the frozen copy whatever the draft does afterwards, and publishing a
draft whose scene is not in the registry is a `publish_invalid` problem
(`scene_unknown`). `discard-changes` and `restore` recreate the scene and
bindings once the bubbles are back with their original ids; a binding whose
bubble could not be brought back (already reported as `bubble_not_restored`) or
whose zone is no longer in the scene is left out.

**Why the snapshot key is omitted, not `null`.** A map with no 3D view has no
`view3d` key in its snapshot. Snapshots published before this feature have no
such key either, so a draft that still has no 3D view builds the very same
canonical JSON, and hash, as that old publication: nothing shows up as an
unpublished change after the migration, and a map shows
`unpublished_changes` only once its 3D view actually changes. Writing
`"view3d": null` would change every existing hash. `tests/test_snapshot.py`
pins the hash of a snapshot in the old format, and `tests/test_migrations.py`
checks a publication made before the migration. The same rule applies to any
future optional snapshot field.

### Concurrent edits (`If-Match`)

`CourseMapRead.revision` and `BubbleRead.version` start at 1 and travel as the
`ETag` (`"3"`) of single-resource responses. Guarded writes must send the number
they are based on as `If-Match: "3"`: `PATCH /course-maps/{id}`,
`PUT .../appearance`, `PUT .../view3d`, `POST .../discard-changes`,
`POST .../publications/{n}/restore` (map revision) and `PATCH`/`DELETE .../bubbles/{id}` (bubble version; the PATCH
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
- `python -m app.scripts.tenants` and `python -m app.scripts.purge_auth`: see "Tenants and authentication".
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

**Every route is served under `/api/v1`** (`/api/v1/course-maps`,
`/api/v1/published/course-maps`, `/api/v1/assets`, `/api/v1/skins`,
`/api/v1/health`, ...), and so is the documentation: `/api/v1/docs`,
`/api/v1/redoc` and `/api/v1/openapi.json`. The paths listed below are written
relative to that prefix. The prefix is defined once, in `app/core/paths.py`.

The URLs the API returns include it too: an asset's `url` is
`/api/v1/assets/{id}` (and `thumbUrl` `/api/v1/assets/{id}/thumb`), and a map
background served by this API is stored as `imageUrl: "/api/v1/assets/{id}"`.
Maps created before the prefix existed are rewritten by migration
`c9d2e5f8a416`, which also fixes the stored publications and their hashes, so
nothing shows up as an unpublished change. A proxy in front of the API needs
no path rewriting.

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
- `GET /view3d/scenes` and `PUT /course-maps/{id}/view3d`: the 3D scene and
  zone bindings; see "3D view".
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
  bundled frontend path or `/api/v1/assets/{id}`.
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

`includeHidden=true` is only reachable by editors (the endpoints that take it are
editor-only, and Moodle-backed, so legacy tenant only).
