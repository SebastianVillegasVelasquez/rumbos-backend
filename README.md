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
| `ASSETS_MAX_IMAGE_BYTES` | `4194304` | Largest accepted content image upload (`kind=image`, 4 MB). |
| `ASSETS_MAX_IMAGE_SIDE` | `2048` | Longest side, in pixels, of a content image. |
| `ASSETS_MAX_TOTAL_BYTES` | `2147483648` | Quota: sum of every stored file (2 GB). Over it, uploads answer 507. |
| `AUDIO_UPLOADS_ENABLED` | on in dev, **off in production** | Switch for editors uploading **their own audio** (`POST /assets` with `kind=audio`). Unset, it follows `APP_ENV`; set it to `true`/`false` to decide explicitly. It blocks only uploading (503 `audio_uploads_disabled`): serving, playing and choosing library tracks never depend on it. Independent of `ASSETS_UPLOADS_ENABLED`. See "Music". |
| `ASSETS_MAX_AUDIO_BYTES` | `8388608` | Largest accepted audio upload (8 MB). |
| `ASSETS_MAX_AUDIO_SECONDS` | `600` | Longest accepted track (ten minutes). |
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
- Image and audio bytes are fetchable by anyone who knows the id (above).

### Uploads and security

Teachers can upload their own backgrounds and bubble images (`POST /assets`,
`multipart/form-data` with `file` and `kind` = `background` | `bubble` | `image` | `audio`).
`image` is for pictures inside content bubbles (see "Content bubbles"): up to
2048 px per side and 4 MB, through the same pipeline as a background, with a
WebP thumbnail (`thumbUrl`).

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
latest one through `/published/...`. Content bubbles are part of it (see
"Content bubbles"). Live data is never in the snapshot: bubble
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

### Music

Each map can play background music. Music is a copyright exposure (we host and
stream audio to students of public institutions), so the model is built around
control and traceability.

**Two sources.** A map's music is an `audio` asset that is either

- a **library** track: curated by the operator with the CLI below, owned by no
  tenant, readable and selectable by every tenant, and impossible for an editor
  to change or delete (403 `asset_is_library`); or
- an editor's **own upload**, which belongs to their tenant (listing, quota,
  isolation as for images) and can be switched off with `AUDIO_UPLOADS_ENABLED`.

`GET /assets?kind=audio&scope=library|tenant` lists them (`tenant`, the default,
is the caller's own uploads). Quarantined tracks are never listed.

**Upload** (`POST /assets`, `multipart/form-data`, editors only): `file`,
`kind=audio`, `title`, `credit` (required, at most 300 characters), `license`
(optional, at most 120) and `rightsDeclared=true`. Errors: 503
`audio_uploads_disabled`; 422 `audio_rights_declaration_required` (missing or
not exactly `true`), `audio_credit_required`, `audio_invalid`, `audio_too_long`
(over ten minutes, with `maxSeconds`), `audio_quarantined` (the same audio was
taken down) and `invalid_asset_metadata` (a missing or over-long title, credit
or licence); 413 `asset_too_large` (8 MB); 507 when the tenant quota is full.
The same bytes are one asset: sending them again answers `200` with the
existing one.

**What is kept about the person:** only `uploaded_by_sub`, the launch's
pseudonymous user id (nothing for the anonymous legacy editor), and
`rights_declared_at`, with the licence text. The uploaded file name is not
stored. Neither appears in the API; the operator sees them with `assets list`.

**Parsing and cleaning** (`app/assets/audio.py`, no extra runtime dependency).
Nothing the client says is trusted: the container is decided from the bytes and
parsed strictly, and what is stored is **rebuilt from the audio alone**.

- *MP3*: only the run of MPEG Layer III frames is kept, so ID3v2/ID3v1/APE tags,
  cover pictures and anything appended to the file are gone. The frame
  parameters must stay constant and the audio must not be cut by data that is
  not audio.
- *M4A (AAC in MP4)*: `ftyp`, a `moov` rebuilt from an allow-list of boxes (so
  `udta`/`meta` with tags, artwork and GPS atoms, `uuid` XMP packets and `free`
  padding are gone, and the clock fields are zeroed) and an `mdat` holding only
  the bytes the sample tables point at. The index goes first ("fast start").
  Encrypted tracks (`drms`/`enca`/`sinf`, the `M4P ` brand), fragmented files,
  non-AAC codecs (ALAC, MP3-in-MP4), several tracks and references to data
  outside the file are refused.
- The duration comes from the frames or the sample table, never from a header
  that can claim anything.
- The SHA-256 (also the `ETag`) is computed over the stored bytes.
- Raw AAC (ADTS), WAV, OGG and everything else are `audio_invalid`.

`mutagen` is a development dependency only: tests use it as an independent
parser to check that cleaned files still read and carry no tags.

**Serving and `Range`.** `GET /assets/{id}` (and `HEAD`) honours a single
`Range`: `206` with `Content-Range`, `416` with `Content-Range: bytes */size`
when nothing of the file lies in the range, and the whole file (`200`) for
ranges it does not honour (other units, several ranges, nonsense). `If-Range`
with the current `ETag` keeps the range, anything else gets the whole file;
`If-None-Match` is evaluated first. Every response says `Accept-Ranges: bytes`
and keeps `X-Content-Type-Options: nosniff`, the stored `Content-Type` and the
immutable cache headers. iOS Safari will not play audio it cannot seek in. With
CORS on, `Range`/`If-Range` are allowed request headers and
`Content-Range`/`Accept-Ranges`/`ETag` are exposed. (A ranged request reads the
whole stored file, at most 8 MB, and slices it.)

*Behind nginx:* a plain `proxy_pass` forwards `Range` and the `206` back
untouched. If you turn on `proxy_cache` for `/api/v1/assets/`, make sure ranged
requests still work (the cache must either hold the full file or use the
`slice` module), and do not gzip audio. This repository does not contain the
nginx configuration, so this was not checked against it.

**In the map.** `settings.audio` is `null` or `{ assetId, volume (0..1, default
0.4), loop (default true), startAfterInteraction (default false) }`, set through
`PUT /course-maps/{id}/appearance` like the rest of the settings (one
transaction, nothing is written if anything is invalid). The asset must exist,
be `audio`, belong to the tenant or the library and not be quarantined, else
422 `audio_asset_not_found` / `audio_asset_wrong_kind` (another tenant's track
simply does not exist for you). `CourseMapRead.audioInfo` and
`PublishedMapRead.audioInfo` are `null` or `{ title, credit, durationSeconds }`
(read-only): a draft reads them live from the asset, a publication freezes them.
`publication-state.changes.audioChanged` reports any difference in the music,
its settings or the frozen description; `settingsChanged` does not include music.

A map's music counts as a use of the asset: the `maps` and `publications`
counts, `DELETE /assets/{id}` (409 `asset_in_use`) and `gc_assets` all see it.
`discard-changes` and `restore` put the snapshot's music back, or drop it with
an `audio_unavailable` warning if the track has been deleted or taken down.

**Hash stability.** `settings.audio` and the top-level `audioInfo` are omitted,
never `null`, when a map has no music (the same rule as `view3d`), so every map
published before this feature still reports `up_to_date`.
`tests/test_snapshot.py` pins the stored form of settings without music and
`tests/test_migrations.py` checks a publication made before the migration.

**Curating the library and taking tracks down** (the operator's CLI, run with
the app's environment):

```
python -m app.scripts.assets import-audio --file song.mp3 \
    --title "Brisa" --credit "Ana Perez, CC BY 4.0" [--license "CC BY 4.0"]
python -m app.scripts.assets list
python -m app.scripts.assets quarantine <asset-id> --reason "Complaint from X, ticket 42"
python -m app.scripts.assets release <asset-id>
python -m app.scripts.assets history <asset-id>
```

`import-audio` runs the same checks as an upload and makes a library track; it
does not depend on `AUDIO_UPLOADS_ENABLED` or any quota, the operator vouches
for the rights, and library tracks are never garbage-collected. `list` shows
every audio track (the library's and each tenant's) with its tenant, the
uploader's pseudonymous id, when the rights were declared, its state and its
usage.

**Takedown procedure.**

1. Find the asset: the id is in the URL (`/api/v1/assets/<id>`); `assets list`
   shows who uploaded it and when they declared the rights.
2. `assets quarantine <id> --reason "<who asked, ticket>"`. It takes effect at
   once: the asset answers 404 (ranged requests included), disappears from
   lists and pickers, cannot be chosen (and cannot be uploaded again:
   `audio_quarantined`), and maps that use it keep working without music.
   Published snapshots keep their frozen reference, but readers are no longer
   shown the music; the draft's `publication-state` then reports `audioChanged`
   so an editor can republish.
3. **Purge the proxy or CDN cache** for `/api/v1/assets/<id>`: responses are
   `immutable` for a year, so a cache in front will keep serving the file.
   Browsers that already hold a copy cannot be recalled, but nothing refers to
   the track any more.
4. Keep the complaint. Every quarantine and release is recorded with its reason
   and time in the asset's audit trail (`assets history <id>`) and in the log
   (`rumbos.assets`).
5. `assets release <id>` undoes it, for example if the complaint is withdrawn.
   The bytes stay on disk while any map or publication still refers to the
   track; once nothing does (editors republish, old publications are pruned by
   `PUBLICATIONS_KEEP`), an own upload is removed by `gc_assets --apply`. There
   is no command yet to erase a library track.

> **Legal notice.** The code only checks that `rightsDeclared` is `true` and that
> a credit exists; it cannot know whether the declaration is true. The text the
> editor sees when declaring their rights (in the frontend), the credit
> conventions, the terms under which institutions upload music and the way
> complaints are handled are legal matters, and the wording has to be reviewed by
> whoever is responsible for them before audio uploads are enabled in
> production.

### Content bubbles

Not every bubble is a Moodle activity: an editor can also write an introduction
or a note. A bubble has a `kind`: `activity` (the default; every bubble that
existed before) or `content`, which has `activityId: null` and a `content`
object that belongs to that one bubble (no reuse across bubbles). Content
bubbles take part in the guided path (`sequence`), the order endpoint, the 3D
zone bindings, publishing and restore like any other; they open nothing in
Moodle, so `activities` and `resolved` ignore them.

`POST /course-maps/{id}/bubbles` takes `{ kind: "content", content, x, y, icon? }`
and `PATCH .../bubbles/{id}` takes `content` (the whole object is replaced; it is
a content edit, so `If-Match` applies). `GET /auth/me` answers
`{ role, courseId, features: { widgets } }` so the frontend knows what to offer.

There are **two tiers**, on purpose:

1. **Blocks** (`type: "blocks"`): data, validated by the server, never HTML.
   `body = { schemaVersion: 1, blocks }` with at most 40 blocks and 20 KB, each
   one of `heading` (level 2 or 3), `paragraph` (plain text, newlines kept),
   `image` (an `assetId` of a `kind=image` or `background` asset of the tenant
   that is not taken down, and **required** `alt`), `video` (`provider`
   `youtube` or `vimeo` plus a `videoId` matching `^[A-Za-z0-9_-]{6,32}$`; a
   free URL is never accepted), `link` (`https` only, no credentials, no
   whitespace or control characters, host in ASCII/punycode) and `divider`.
   Text is Unicode NFC with control and direction-override characters removed
   (newlines survive only in paragraphs) and its length is counted after that.
   Unknown fields are rejected and only the camelCase spelling is accepted. The
   frontend renders blocks with its own components, so there is nothing for the
   server to sanitize.
2. **Widgets** (`type: "widget"`): author-written HTML, CSS and JavaScript
   (`html` up to 200 KB, plus a required `altText` up to 300 characters). They
   are **off for every tenant** until an operator turns them on.

Both have a `title` (1 to 120) and `autoOpen` (`never` | `first_visit`).

**The server does not sanitize widget HTML, and that is deliberate.** Removing
the dangerous parts of arbitrary HTML and JavaScript on the server is an arms
race that cannot be won, and a half-sanitized widget would look safe while not
being so. The server only checks that the `html` is non-empty, valid UTF-8 text
within the size limit, and stores and publishes it byte for byte. **The real
isolation lives in the frontend**, which must run a widget only inside an
`<iframe sandbox>` with no `allow-same-origin` (so it has an opaque origin and
cannot read the app's token, storage or DOM), preferably served with its own
restrictive CSP. This service sends no widget to anybody's origin itself: it
returns the string. A frontend that renders a widget outside that sandbox
breaks the model; there is nothing the backend can do about it.

**The flag.** `tenants.widgets_enabled` defaults to `false`.

    python -m app.scripts.tenants set --issuer https://moodle.uni.edu --widgets on
    python -m app.scripts.tenants set --issuer https://moodle.uni.edu --widgets off
    python -m app.scripts.tenants list      # shows widgets=on|off

The change is logged. With the flag off, creating a widget bubble or replacing a
bubble's content with a widget answers 403 `widgets_disabled`; everything else
keeps working: widgets that already exist stay, can be moved, restyled, deleted
or replaced by blocks, and **published maps keep returning the frozen content
whatever the flag says now** (the frontend decides whether to run it).
Restoring a publication brings widget bubbles back as published and adds a
`widgets_disabled` warning when the flag is off.

*Who should be allowed to turn widgets on.* A widget is code written by a
teacher and run in every student's browser. Turn it on only for an institution
that accepts that: it has editors it trusts (the editor role is whatever the
Moodle plugin grants), a frontend deployed with the sandbox above, and someone
accountable for what its teachers publish. It is per tenant and not per user on
purpose: the server cannot tell a careful author from a careless one. Do not
enable it for a site you cannot reach if something goes wrong; turning it off
stops new widgets immediately, but a widget already published keeps being served
until its bubble is replaced and the map published again.

**Errors.** Content problems are `422 content_invalid` with
`detail.problems: [{ path, code, message }]`, every problem at once (`path` such
as `body.blocks[2].url`; codes include `required`, `unknown_field`, `too_short`,
`too_long`, `too_many_blocks`, `unknown_type`, `invalid_value`, `invalid_url`,
`invalid_video_id`, `invalid_utf8`, `asset_not_found`, `asset_wrong_kind`,
`not_a_content_bubble`). Messages never echo the input. Sizes are
`413 content_too_large` with `limitBytes` and `scope` (`bubble`: a blocks body
over 20 KB or widget HTML over 200 KB; `map`: all the content of a map over
1 MB, the bubble being replaced not counted twice). The map budget is checked
before the write; two simultaneous writes can overshoot it slightly.

**Publishing.** A content bubble's snapshot entry has `kind: "content"` and its
`content` and no `activityId`; an activity bubble's entry has exactly the shape
it always had, so every publication made before content bubbles still hashes the
same and reads as `up_to_date` (a migration test checks it). `publication-state`
gains `changes.contentChanged`. The images a map's content shows are listed in
the publication's asset ids, so they cannot be deleted while a publication
uses them, and they count under `maps` in an asset's `usage` while a draft does.

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
maps/bubbles and the two `order` endpoints are not guarded. Replacing a content
bubble's `content` is a content edit and so is guarded like `x` or `icon`.

### Operations

Run from the project root with the app's environment (`POSTGRES_*`, ...).
Both are **dry runs unless `--apply` is given**.

- **Required deploy step:** `python -m app.scripts.publish_existing_maps --apply`
  after migrating. Maps created before publishing existed have no publication
  and are invisible to students until this publishes them as #1 ("Publicación
  inicial (migración)"). Only maps with at least one bubble and no publication
  are touched, so it is safe to run again.
- `python -m app.scripts.tenants` and `python -m app.scripts.purge_auth`: see "Tenants and authentication".
  `python -m app.scripts.tenants set --issuer <iss> --widgets on|off` is how a
  tenant is allowed widgets (see "Content bubbles").
- `python -m app.scripts.assets`: the audio library and takedowns, see "Music".
- `python -m app.scripts.gc_assets [--older-than-days 7] [--apply]` lists, and
  with `--apply` deletes, assets that no map, skin or publication uses and that
  are older than the threshold, reporting the bytes reclaimed. It uses the same
  usage rule as `DELETE /assets/{id}` (maps' music and the images of content
  bubbles included). Library tracks are
  never collected.

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
  have one bubble per **course**, on any of its maps. A bubble is `kind`
  `activity` (default) or `content`, whose `content` is written in Rumbos: see
  "Content bubbles".
- `GET /course-maps/{id}/activities?includeHidden=&onlySection=`: the course's
  activities, with `sectionId`, `placed` (on any map of the course) and
  `placedInMapId`. `onlySection=true` keeps only the map's section (ignored for
  a map without one).
- `GET /course-maps/{id}/resolved?includeHidden=`: each bubble's
  `availability` (`available`, `hidden`, `missing`, `unknown`) and the
  `moodleStatus` (`live`, `cached`, `unavailable`). Moodle outages answer 200.
- `POST /assets` (201, or 200 for already-known content; optional form fields
  `title`, `credit`; audio also takes `license` and needs `rightsDeclared=true`,
  see "Music"), `GET /assets?kind=&q=&scope=tenant|library&limit=&offset=` (the
  tenant's own assets, or the shared audio library; newest first, with `usage`
  counts), `PATCH /assets/{id}` (`title`, `credit`),
  `DELETE /assets/{id}` (409 while in use), `GET /assets/{id}` (also `HEAD`;
  honours `Range`), `GET /assets/{id}/thumb` (backgrounds and images). A map's
  `imageUrl` may be a bundled frontend path or `/api/v1/assets/{id}`; its music
  is `settings.audio` (see "Music").
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
