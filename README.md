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
`uv run mypy .`. Tests need the PostgreSQL server from `DATABASE_URL`; they use
separate `<name>_test` and `<name>_migration_test` databases and never touch
the configured one. `RUN_MOODLE_SMOKE=1 uv run pytest tests/test_moodle_smoke.py`
runs an opt-in check against the real Moodle.

## Configuration

Environment variables (or `.env`); see `.env.example`.

| Variable | Default | Meaning |
| --- | --- | --- |
| `DATABASE_URL` | required | Async SQLAlchemy URL (`postgresql+asyncpg://...`). |
| `MOODLE_BASE_URL` | required | Moodle instance the service talks to. |
| `MOODLE_SERVICE_TOKEN` | required | Web Services token. Server-side only. |
| `MOODLE_CONNECT_TIMEOUT` | `5` | Seconds to connect to Moodle. |
| `MOODLE_READ_TIMEOUT` | `15` | Seconds to wait for Moodle's response. |
| `MOODLE_CONTENTS_TTL_SECONDS` | `60` | A cached copy of a course's contents is served without asking Moodle for this long. |
| `MOODLE_STALE_MAX_SECONDS` | `3600` | When Moodle fails, a cached copy up to this old is still served (reported as `cached`). |
| `CORS_ALLOWED_ORIGINS` | empty | Comma-separated browser origins. Empty adds no CORS middleware. Credentials are never allowed. The dev frontend uses a Vite proxy and needs nothing here. |

### Moodle cache

Course contents are cached in memory, per course, with single-flight (a burst
of requests for one course makes one Moodle call) and stale-on-error. The cache
is **per process**: with several workers each has its own copy, so Moodle may be
called once per worker per TTL. A shared cache (Redis) is a later decision.

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
- `POST|PATCH|DELETE /course-maps/{id}/bubbles[/{bubbleId}]`. An activity can
  have one bubble per **course**, on any of its maps.
- `GET /course-maps/{id}/activities?includeHidden=&onlySection=`: the course's
  activities, with `sectionId`, `placed` (on any map of the course) and
  `placedInMapId`. `onlySection=true` keeps only the map's section (ignored for
  a map without one).
- `GET /course-maps/{id}/resolved?includeHidden=`: each bubble's
  `availability` (`available`, `hidden`, `missing`, `unknown`) and the
  `moodleStatus` (`live`, `cached`, `unavailable`). Moodle outages answer 200.
- `GET /health` (liveness) and `GET /health/ready` (database `SELECT 1`; 503 if
  it fails; never calls Moodle).

Conflicts (409) carry `detail: { code, message }`: `map_already_exists_for_section`,
or `activity_already_placed` (plus `courseMapId` and `courseMapTitle` of the map
that already holds the activity).

`includeHidden=true` currently needs no authentication; it must require a
teacher/editor role once auth exists.
