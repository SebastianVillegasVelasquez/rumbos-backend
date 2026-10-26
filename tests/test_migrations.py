"""Alembic migrations against a database that already contains rows.

Each test gets its own throwaway `<name>_migration_test` database and runs the
real `alembic` CLI in a subprocess (env.py calls `asyncio.run`, which cannot
nest inside pytest-asyncio's loop). `POSTGRES_*` in the child's environment
override `.env`, so it never touches development data.
"""

import asyncio
import json
import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.models.tenant import LEGACY_TENANT_ID
from app.tenancy.scope import Scope, bind_scope
from tests.conftest import _test_database_url, recreate_database

INITIAL = "ffbafbaf2f65"  # course_maps + bubbles, before titles
TITLE = "3b7d1c9e5a42"


@pytest.fixture
def migration_db() -> Iterator[str]:
    url = _test_database_url("migration_test")
    asyncio.run(recreate_database(url))
    yield url


def alembic(db_url: str, *args: str) -> subprocess.CompletedProcess[str]:
    url = make_url(db_url)
    env = {
        **os.environ,
        "POSTGRES_USER": url.username or "",
        "POSTGRES_PASSWORD": url.password or "",
        "POSTGRES_DB": url.database or "",
        "POSTGRES_HOST": url.host or "",
        "POSTGRES_PORT": str(url.port),
    }
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def run_sql(db_url: str, statements: list[tuple[str, dict[str, Any]]]) -> None:
    async def go() -> None:
        engine = create_async_engine(db_url)
        async with engine.begin() as conn:
            for sql, params in statements:
                await conn.execute(text(sql), params)
        await engine.dispose()

    asyncio.run(go())


def fetch(db_url: str, sql: str) -> list[tuple[Any, ...]]:
    async def go() -> list[tuple[Any, ...]]:
        engine = create_async_engine(db_url)
        async with engine.connect() as conn:
            rows = [tuple(r) for r in (await conn.execute(text(sql))).all()]
        await engine.dispose()
        return rows

    return asyncio.run(go())


def insert_map(
    course_id: int, *, with_title: bool = False
) -> tuple[str, dict[str, Any]]:
    """A map row; `with_title` for databases already past the title migration."""
    columns, values = ("title, ", "'Seeded', ") if with_title else ("", "")
    return (
        (
            f"INSERT INTO course_maps (id, {columns}moodle_course_id, image_url,"
            f" created_at, updated_at) VALUES (:id, {values}:c, '/x.png', now(), now())"
        ),
        {"id": uuid.uuid4(), "c": course_id},
    )


def test_title_migration_backfills_existing_rows(migration_db: str) -> None:
    assert alembic(migration_db, "upgrade", INITIAL).returncode == 0
    run_sql(migration_db, [insert_map(8), insert_map(21)])

    result = alembic(migration_db, "upgrade", TITLE)
    assert result.returncode == 0, result.stderr

    rows = fetch(
        migration_db,
        "SELECT moodle_course_id, title FROM course_maps ORDER BY moodle_course_id",
    )
    assert rows == [(8, "Mapa del curso 8"), (21, "Mapa del curso 21")]
    nullable = fetch(
        migration_db,
        "SELECT is_nullable FROM information_schema.columns"
        " WHERE table_name = 'course_maps' AND column_name = 'title'",
    )
    assert nullable == [("NO",)]


def test_title_migration_downgrade_keeps_rows(migration_db: str) -> None:
    assert alembic(migration_db, "upgrade", INITIAL).returncode == 0
    run_sql(migration_db, [insert_map(8)])
    assert alembic(migration_db, "upgrade", TITLE).returncode == 0

    result = alembic(migration_db, "downgrade", INITIAL)

    assert result.returncode == 0, result.stderr
    assert fetch(migration_db, "SELECT moodle_course_id FROM course_maps") == [(8,)]
    columns = fetch(
        migration_db,
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_name = 'course_maps'",
    )
    assert ("title",) not in columns


UNIQUE = "c2e8f4a61d07"


def insert_bubble(map_id: uuid.UUID, activity_id: int) -> tuple[str, dict[str, Any]]:
    return (
        (
            "INSERT INTO bubbles (id, course_map_id, activity_id, x, y, status,"
            " created_at, updated_at) VALUES (:id, :m, :a, 0.5, 0.5, 'locked',"
            " now(), now())"
        ),
        {"id": uuid.uuid4(), "m": map_id, "a": activity_id},
    )


def seed_map_with_bubbles(db: str, activities: list[int]) -> uuid.UUID:
    map_id = uuid.uuid4()
    sql, params = insert_map(8, with_title=True)
    run_sql(db, [(sql, {**params, "id": map_id})])
    run_sql(db, [insert_bubble(map_id, a) for a in activities])
    return map_id


UNIQUE_CONSTRAINTS = (
    "SELECT conname FROM pg_constraint WHERE conrelid = 'bubbles'::regclass"
    " AND contype = 'u'"
)


def test_unique_migration_succeeds_on_clean_data_and_downgrades(
    migration_db: str,
) -> None:
    assert alembic(migration_db, "upgrade", TITLE).returncode == 0
    seed_map_with_bubbles(migration_db, [1, 2, 3])

    up = alembic(migration_db, "upgrade", UNIQUE)
    assert up.returncode == 0, up.stderr
    assert fetch(migration_db, UNIQUE_CONSTRAINTS) == [
        ("uq_bubbles_course_map_id_activity_id",)
    ]

    down = alembic(migration_db, "downgrade", TITLE)
    assert down.returncode == 0, down.stderr
    assert fetch(migration_db, "SELECT count(*) FROM bubbles") == [(3,)]
    assert not fetch(migration_db, UNIQUE_CONSTRAINTS)


def test_unique_migration_aborts_listing_duplicates_and_deletes_nothing(
    migration_db: str,
) -> None:
    assert alembic(migration_db, "upgrade", TITLE).returncode == 0
    map_id = seed_map_with_bubbles(migration_db, [5, 5, 6, 7, 7, 7])

    result = alembic(migration_db, "upgrade", UNIQUE)

    assert result.returncode != 0
    assert "duplicates exist" in result.stderr
    assert f"map {map_id}, activity 5: 2 bubbles" in result.stderr
    assert f"map {map_id}, activity 7: 3 bubbles" in result.stderr
    assert "activity 6" not in result.stderr  # only the duplicated ones
    # Nothing deleted, constraint not added, version not advanced.
    assert fetch(migration_db, "SELECT count(*) FROM bubbles") == [(6,)]
    assert not fetch(migration_db, UNIQUE_CONSTRAINTS)
    assert fetch(migration_db, "SELECT version_num FROM alembic_version") == [(TITLE,)]


def test_unique_migration_allows_same_activity_on_different_maps(
    migration_db: str,
) -> None:
    assert alembic(migration_db, "upgrade", TITLE).returncode == 0
    seed_map_with_bubbles(migration_db, [1])
    other = uuid.uuid4()
    sql, params = insert_map(9, with_title=True)
    run_sql(migration_db, [(sql, {**params, "id": other}), insert_bubble(other, 1)])

    assert alembic(migration_db, "upgrade", UNIQUE).returncode == 0


LEVELS = "a41f7c2d9b30"


def test_levels_migration_backfills_positions_and_bubble_courses(
    migration_db: str,
) -> None:
    assert alembic(migration_db, "upgrade", UNIQUE).returncode == 0
    seed_map_with_bubbles(migration_db, [1, 2, 3])  # course 8
    other = uuid.uuid4()
    sql, params = insert_map(9, with_title=True)
    run_sql(migration_db, [(sql, {**params, "id": other}), insert_bubble(other, 1)])

    result = alembic(migration_db, "upgrade", LEVELS)

    assert result.returncode == 0, result.stderr
    assert fetch(
        migration_db,
        "SELECT moodle_course_id, position, moodle_section_id FROM course_maps"
        " ORDER BY moodle_course_id",
    ) == [(8, 0, None), (9, 0, None)]
    assert fetch(
        migration_db,
        "SELECT moodle_course_id, count(*) FROM bubbles"
        " GROUP BY moodle_course_id ORDER BY moodle_course_id",
    ) == [(8, 3), (9, 1)]
    nullable = fetch(
        migration_db,
        "SELECT is_nullable FROM information_schema.columns"
        " WHERE table_name = 'bubbles' AND column_name = 'moodle_course_id'",
    )
    assert nullable == [("NO",)]


def test_levels_migration_allows_levels_and_enforces_the_new_rules(
    migration_db: str,
) -> None:
    assert alembic(migration_db, "upgrade", LEVELS).returncode == 0
    first, second = uuid.uuid4(), uuid.uuid4()

    def level(map_id: uuid.UUID, section: int | None) -> tuple[str, dict[str, Any]]:
        return (
            (
                "INSERT INTO course_maps (id, title, moodle_course_id,"
                " moodle_section_id, image_url, created_at, updated_at)"
                " VALUES (:id, 'L', 8, :s, '/x.png', now(), now())"
            ),
            {"id": map_id, "s": section},
        )

    def bubble(
        map_id: uuid.UUID, course: int, activity: int
    ) -> tuple[str, dict[str, Any]]:
        sql, params = insert_bubble(map_id, activity)
        return (
            sql.replace("activity_id,", "activity_id, moodle_course_id,").replace(
                ":a,", ":a, :c,"
            ),
            {**params, "c": course},
        )

    # Several maps per course; NULL sections repeat.
    run_sql(migration_db, [level(first, 1), level(second, None)])
    run_sql(migration_db, [bubble(first, 8, 5)])
    # An activity is unique per course, across maps...
    with pytest.raises(Exception, match="uq_bubbles_moodle_course_id_activity_id"):
        run_sql(migration_db, [bubble(second, 8, 5)])
    # ...a section is unique per course...
    with pytest.raises(Exception, match="uq_course_maps_course_section"):
        run_sql(migration_db, [level(uuid.uuid4(), 1)])
    # ...and a bubble's course must be its map's.
    with pytest.raises(Exception, match="fk_bubbles_course_map"):
        run_sql(migration_db, [bubble(second, 9, 6)])


def test_levels_migration_downgrade_refuses_to_merge_maps(migration_db: str) -> None:
    assert alembic(migration_db, "upgrade", LEVELS).returncode == 0
    for _ in range(2):
        sql, params = insert_map(8, with_title=True)
        run_sql(migration_db, [(sql, {**params, "id": uuid.uuid4()})])

    result = alembic(migration_db, "downgrade", UNIQUE)

    assert result.returncode != 0
    assert "course 8: 2 maps" in result.stderr
    assert fetch(migration_db, "SELECT count(*) FROM course_maps") == [(2,)]


def test_levels_migration_downgrades_when_each_course_has_one_map(
    migration_db: str,
) -> None:
    assert alembic(migration_db, "upgrade", UNIQUE).returncode == 0
    seed_map_with_bubbles(migration_db, [1, 2])
    assert alembic(migration_db, "upgrade", LEVELS).returncode == 0

    result = alembic(migration_db, "downgrade", UNIQUE)

    assert result.returncode == 0, result.stderr
    assert fetch(migration_db, "SELECT count(*) FROM bubbles") == [(2,)]
    assert fetch(migration_db, UNIQUE_CONSTRAINTS) == [
        ("uq_bubbles_course_map_id_activity_id",)
    ]


ASSETS = "b7d3e9a15c42"


def test_assets_migration_creates_a_deduplicating_table_and_downgrades(
    migration_db: str,
) -> None:
    assert alembic(migration_db, "upgrade", UNIQUE).returncode == 0
    seed_map_with_bubbles(migration_db, [1])

    up = alembic(migration_db, "upgrade", ASSETS)

    assert up.returncode == 0, up.stderr

    def asset(kind: str, digest: str) -> tuple[str, dict[str, Any]]:
        return (
            (
                "INSERT INTO assets (id, kind, mime, width, height, bytes, sha256,"
                " created_at, updated_at)"
                " VALUES (:id, :k, 'image/png', 1, 1, 10, :h, now(), now())"
            ),
            {"id": uuid.uuid4(), "k": kind, "h": digest},
        )

    run_sql(
        migration_db,
        [asset("background", "a" * 64), asset("bubble", "a" * 64)],
    )  # same hash, different kind: fine
    with pytest.raises(Exception, match="uq_assets_kind_sha256"):
        run_sql(migration_db, [asset("bubble", "a" * 64)])

    down = alembic(migration_db, "downgrade", LEVELS)
    assert down.returncode == 0, down.stderr
    assert not fetch(migration_db, "SELECT 1 FROM pg_tables WHERE tablename = 'assets'")
    assert fetch(migration_db, "SELECT count(*) FROM bubbles") == [(1,)]


SKINS = "d5b8a31c7e90"


def test_skins_migration_seeds_four_valid_builtins_with_exactly_one_default(
    migration_db: str,
) -> None:
    from app.schemas.skin import ProceduralSkin, skin_config_adapter

    assert alembic(migration_db, "upgrade", SKINS).returncode == 0

    rows = fetch(
        migration_db,
        "SELECT id::text, name, kind, is_builtin, is_default, config::text FROM skins"
        " ORDER BY id",
    )
    assert [(r[0], r[1], r[2], r[3], r[4]) for r in rows] == [
        ("01900000-0000-7000-8000-000000000001", "Orbe", "procedural", True, True),
        ("01900000-0000-7000-8000-000000000002", "Insignia", "procedural", True, False),
        ("01900000-0000-7000-8000-000000000003", "Pin", "procedural", True, False),
        ("01900000-0000-7000-8000-000000000004", "Hexágono", "procedural", True, False),
    ]
    assert fetch(migration_db, "SELECT count(*) FROM skins WHERE is_default") == [(1,)]
    configs = {r[1]: skin_config_adapter.validate_json(r[5]) for r in rows}
    assert {
        n: c.shape for n, c in configs.items() if isinstance(c, ProceduralSkin)
    } == {
        "Orbe": "circle",
        "Insignia": "badge",
        "Pin": "pin",
        "Hexágono": "hexagon",
    }
    orbe = configs["Orbe"]
    assert isinstance(orbe, ProceduralSkin)
    assert orbe.size == 56 and orbe.effects.idle == "breathe"
    assert orbe.effects.ring and orbe.effects.glow
    assert orbe.palette.available.fill == "#FFB703"
    assert orbe.palette.in_progress.glow == "#5ED3DD"
    assert orbe.label.mode == "hover" and orbe.effects.completion == "burst"
    pin = configs["Pin"]
    assert isinstance(pin, ProceduralSkin) and pin.effects.idle == "float"
    hexagon = configs["Hexágono"]
    assert isinstance(hexagon, ProceduralSkin) and hexagon.effects.idle == "pulse"


def test_skins_migration_backfills_sequence_in_creation_order_per_map(
    migration_db: str,
) -> None:
    assert alembic(migration_db, "upgrade", ASSETS).returncode == 0
    first, second = uuid.uuid4(), uuid.uuid4()
    for map_id, course in ((first, 8), (second, 9)):
        sql, params = insert_map(course, with_title=True)
        run_sql(migration_db, [(sql, {**params, "id": map_id})])

    def bubble(map_id: uuid.UUID, course: int, activity: int, minute: int):  # type: ignore[no-untyped-def]
        return (
            (
                "INSERT INTO bubbles (id, course_map_id, moodle_course_id, activity_id,"
                " x, y, status, created_at, updated_at)"
                " VALUES (:id, :m, :c, :a, 0.5, 0.5, 'locked',"
                " now() + make_interval(mins => :t), now())"
            ),
            {"id": uuid.uuid4(), "m": map_id, "c": course, "a": activity, "t": minute},
        )

    # Inserted out of order: the sequence follows created_at, not insertion.
    run_sql(
        migration_db,
        [
            bubble(first, 8, 30, 3),
            bubble(first, 8, 10, 1),
            bubble(first, 8, 20, 2),
            bubble(second, 9, 40, 9),
        ],
    )

    assert alembic(migration_db, "upgrade", SKINS).returncode == 0

    rows = fetch(
        migration_db,
        "SELECT moodle_course_id, activity_id, sequence FROM bubbles"
        " ORDER BY moodle_course_id, sequence",
    )
    assert rows == [(8, 10, 0), (8, 20, 1), (8, 30, 2), (9, 40, 0)]
    assert fetch(
        migration_db,
        "SELECT is_nullable FROM information_schema.columns"
        " WHERE table_name = 'bubbles' AND column_name = 'sequence'",
    ) == [("NO",)]


def test_skins_migration_foreign_keys_clear_and_cascade_on_the_real_schema(
    migration_db: str,
) -> None:
    assert alembic(migration_db, "upgrade", SKINS).returncode == 0
    map_id, bubble_id = uuid.uuid4(), uuid.uuid4()
    orbe = "01900000-0000-7000-8000-000000000001"
    sql, params = insert_map(8, with_title=True)
    run_sql(
        migration_db,
        [
            (sql, {**params, "id": map_id}),
            (
                "UPDATE course_maps SET default_skin_id = :s WHERE id = :m",
                {"s": uuid.UUID(orbe), "m": map_id},
            ),
            (
                (
                    "INSERT INTO bubbles (id, course_map_id, moodle_course_id,"
                    " activity_id, x, y, status, skin_id, created_at, updated_at)"
                    " VALUES (:id, :m, 8, 1, 0, 0, 'locked', :s, now(), now())"
                ),
                {"id": bubble_id, "m": map_id, "s": uuid.UUID(orbe)},
            ),
            (
                (
                    "INSERT INTO course_map_skin_rules (id, course_map_id, modname,"
                    " skin_id, created_at, updated_at)"
                    " VALUES (:id, :m, 'quiz', :s, now(), now())"
                ),
                {"id": uuid.uuid4(), "m": map_id, "s": uuid.UUID(orbe)},
            ),
        ],
    )

    run_sql(migration_db, [("DELETE FROM skins WHERE id = :s", {"s": uuid.UUID(orbe)})])

    assert fetch(migration_db, "SELECT skin_id FROM bubbles") == [(None,)]
    assert fetch(migration_db, "SELECT default_skin_id FROM course_maps") == [(None,)]
    assert fetch(migration_db, "SELECT count(*) FROM course_map_skin_rules") == [(0,)]


def test_skins_migration_downgrades_and_keeps_bubbles(migration_db: str) -> None:
    assert alembic(migration_db, "upgrade", UNIQUE).returncode == 0
    seed_map_with_bubbles(migration_db, [1, 2])
    assert alembic(migration_db, "upgrade", SKINS).returncode == 0

    down = alembic(migration_db, "downgrade", ASSETS)

    assert down.returncode == 0, down.stderr
    assert fetch(migration_db, "SELECT count(*) FROM bubbles") == [(2,)]
    assert not fetch(migration_db, "SELECT 1 FROM pg_tables WHERE tablename = 'skins'")


SETTINGS = "e3c7a94d2b18"


def test_settings_migration_gives_existing_maps_an_empty_object_that_reads_as_defaults(
    migration_db: str,
) -> None:
    from app.schemas.map_settings import MapSettings

    assert alembic(migration_db, "upgrade", SKINS).returncode == 0
    sql, params = insert_map(8, with_title=True)
    run_sql(migration_db, [(sql, {**params, "id": uuid.uuid4()})])

    up = alembic(migration_db, "upgrade", SETTINGS)

    assert up.returncode == 0, up.stderr
    assert fetch(migration_db, "SELECT settings::text FROM course_maps") == [("{}",)]
    assert MapSettings.model_validate({}).mode == "explorative"
    nullable = fetch(
        migration_db,
        "SELECT is_nullable FROM information_schema.columns"
        " WHERE table_name = 'course_maps' AND column_name = 'settings'",
    )
    assert nullable == [("NO",)]
    down = alembic(migration_db, "downgrade", SKINS)
    assert down.returncode == 0, down.stderr
    assert fetch(migration_db, "SELECT count(*) FROM course_maps") == [(1,)]


ASSET_METADATA = "f1a9c3d7b2e4"


def test_asset_metadata_migration_backfills_a_title_for_existing_assets(
    migration_db: str,
) -> None:
    assert alembic(migration_db, "upgrade", SETTINGS).returncode == 0

    def old_asset(kind: str, digest: str) -> tuple[str, dict[str, Any]]:
        return (
            (
                "INSERT INTO assets (id, kind, mime, width, height, bytes, sha256,"
                " created_at, updated_at) VALUES (:id, :k, 'image/png', 1, 1, 10, :h,"
                " '2026-03-04 10:00:00+00', now())"
            ),
            {"id": uuid.uuid4(), "k": kind, "h": digest * 64},
        )

    run_sql(migration_db, [old_asset("background", "a"), old_asset("bubble", "b")])

    up = alembic(migration_db, "upgrade", ASSET_METADATA)

    assert up.returncode == 0, up.stderr
    rows = fetch(
        migration_db,
        "SELECT kind, title, credit, original_filename FROM assets ORDER BY kind",
    )
    assert rows == [
        ("background", "Fondo 2026-03-04", None, None),
        ("bubble", "Imagen 2026-03-04", None, None),
    ]
    nullable = fetch(
        migration_db,
        "SELECT column_name, is_nullable FROM information_schema.columns"
        " WHERE table_name = 'assets'"
        " AND column_name IN ('title', 'credit', 'original_filename')"
        " ORDER BY column_name",
    )
    assert nullable == [
        ("credit", "YES"),
        ("original_filename", "YES"),
        ("title", "NO"),
    ]

    down = alembic(migration_db, "downgrade", SETTINGS)
    assert down.returncode == 0, down.stderr
    assert fetch(migration_db, "SELECT count(*) FROM assets") == [(2,)]


CONCURRENCY = "a3f6d2b8c914"


def test_concurrency_migration_gives_existing_rows_revision_and_version_one(
    migration_db: str,
) -> None:
    assert alembic(migration_db, "upgrade", ASSET_METADATA).returncode == 0
    map_id = uuid.uuid4()
    sql, params = insert_map(8, with_title=True)
    run_sql(migration_db, [(sql, {**params, "id": map_id})])
    run_sql(
        migration_db,
        [
            (
                (
                    "INSERT INTO bubbles (id, course_map_id, moodle_course_id,"
                    " activity_id, x, y, status, created_at, updated_at)"
                    " VALUES (:id, :m, 8, :a, 0.5, 0.5, 'locked', now(), now())"
                ),
                {"id": uuid.uuid4(), "m": map_id, "a": activity},
            )
            for activity in (1, 2)
        ],
    )

    up = alembic(migration_db, "upgrade", CONCURRENCY)

    assert up.returncode == 0, up.stderr
    assert fetch(migration_db, "SELECT revision FROM course_maps") == [(1,)]
    assert fetch(migration_db, "SELECT version FROM bubbles") == [(1,), (1,)]
    nullable = fetch(
        migration_db,
        "SELECT table_name, is_nullable FROM information_schema.columns"
        " WHERE (table_name, column_name) IN"
        " (('course_maps', 'revision'), ('bubbles', 'version'))"
        " ORDER BY table_name",
    )
    assert nullable == [("bubbles", "NO"), ("course_maps", "NO")]

    down = alembic(migration_db, "downgrade", ASSET_METADATA)
    assert down.returncode == 0, down.stderr
    assert fetch(migration_db, "SELECT count(*) FROM bubbles") == [(2,)]


PUBLICATIONS = "b8e4c1a7d035"


def test_publications_migration_adds_the_table_for_existing_maps_and_downgrades(
    migration_db: str,
) -> None:
    assert alembic(migration_db, "upgrade", CONCURRENCY).returncode == 0
    map_id = uuid.uuid4()
    sql, params = insert_map(8, with_title=True)
    run_sql(migration_db, [(sql, {**params, "id": map_id})])

    up = alembic(migration_db, "upgrade", PUBLICATIONS)

    assert up.returncode == 0, up.stderr
    # Existing maps start unpublished: the table is empty.
    assert fetch(migration_db, "SELECT count(*) FROM publications") == [(0,)]
    indexes = dict(
        fetch(
            migration_db,
            "SELECT indexname, indexdef FROM pg_indexes"
            " WHERE tablename = 'publications'",
        )
    )
    assert "USING gin (asset_ids)" in indexes["ix_publications_asset_ids"]
    run_sql(
        migration_db,
        [
            (
                (
                    "INSERT INTO publications (id, course_map_id, number, snapshot,"
                    " content_hash, asset_ids, created_at, updated_at) VALUES"
                    " (:id, :m, 1, '{}'::jsonb, 'h', ARRAY[]::uuid[], now(), now())"
                ),
                {"id": uuid.uuid4(), "m": map_id},
            )
        ],
    )
    with pytest.raises(Exception, match="uq_publications_course_map_number"):
        run_sql(
            migration_db,
            [
                (
                    (
                        "INSERT INTO publications (id, course_map_id, number, snapshot,"
                        " content_hash, asset_ids, created_at, updated_at) VALUES"
                        " (:id, :m, 1, '{}'::jsonb, 'h', ARRAY[]::uuid[], now(), now())"
                    ),
                    {"id": uuid.uuid4(), "m": map_id},
                )
            ],
        )
    # Deleting the map takes its publications along.
    run_sql(migration_db, [("DELETE FROM course_maps WHERE id = :m", {"m": map_id})])
    assert fetch(migration_db, "SELECT count(*) FROM publications") == [(0,)]

    down = alembic(migration_db, "downgrade", CONCURRENCY)
    assert down.returncode == 0, down.stderr
    assert not fetch(
        migration_db, "SELECT 1 FROM pg_tables WHERE tablename = 'publications'"
    )


API_PREFIX_REVISION = "c9d2e5f8a416"
ASSET_ID = "0192f3a0-0000-7000-8000-000000000001"


def test_api_prefix_migration_rewrites_asset_urls_and_rehashes_publications(
    migration_db: str,
) -> None:
    from app.publishing.snapshot import content_hash

    assert alembic(migration_db, "upgrade", PUBLICATIONS).returncode == 0
    uploaded, external = uuid.uuid4(), uuid.uuid4()
    for map_id, course, url in (
        (uploaded, 8, f"/assets/{ASSET_ID}"),
        (external, 9, "https://cdn.example.com/a.png"),
    ):
        sql, params = insert_map(course, with_title=True)
        run_sql(
            migration_db,
            [
                (sql, {**params, "id": map_id}),
                (
                    "UPDATE course_maps SET image_url = :u WHERE id = :i",
                    {"u": url, "i": map_id},
                ),
            ],
        )
    old_snapshot = {"imageUrl": f"/assets/{ASSET_ID}", "title": "T", "bubbles": []}
    other_snapshot = {"imageUrl": "https://cdn.example.com/a.png", "title": "T"}
    for map_id, snapshot in ((uploaded, old_snapshot), (external, other_snapshot)):
        run_sql(
            migration_db,
            [
                (
                    (
                        "INSERT INTO publications (id, course_map_id, number, snapshot,"
                        " content_hash, asset_ids, created_at, updated_at) VALUES"
                        " (:id, :m, 1, CAST(:s AS jsonb), 'old', ARRAY[]::uuid[],"
                        " now(), now())"
                    ),
                    {"id": uuid.uuid4(), "m": map_id, "s": json.dumps(snapshot)},
                )
            ],
        )

    up = alembic(migration_db, "upgrade", API_PREFIX_REVISION)

    assert up.returncode == 0, up.stderr
    urls = dict(
        fetch(migration_db, "SELECT moodle_course_id, image_url FROM course_maps")
    )
    assert urls == {
        8: f"/api/v1/assets/{ASSET_ID}",
        9: "https://cdn.example.com/a.png",
    }
    [(snapshot, digest)] = fetch(
        migration_db,
        f"SELECT snapshot, content_hash FROM publications WHERE course_map_id = '{uploaded}'",
    )
    assert snapshot["imageUrl"] == f"/api/v1/assets/{ASSET_ID}"
    assert digest == content_hash(snapshot)  # what the app would compute now
    [(untouched,)] = fetch(
        migration_db,
        f"SELECT content_hash FROM publications WHERE course_map_id = '{external}'",
    )
    assert untouched == "old"  # not an asset url: left exactly as it was

    down = alembic(migration_db, "downgrade", PUBLICATIONS)
    assert down.returncode == 0, down.stderr
    restored = dict(
        fetch(migration_db, "SELECT moodle_course_id, image_url FROM course_maps")
    )
    assert restored[8] == f"/assets/{ASSET_ID}"
    [(back,)] = fetch(
        migration_db,
        f"SELECT snapshot->>'imageUrl' FROM publications WHERE course_map_id = '{uploaded}'",
    )
    assert back == f"/assets/{ASSET_ID}"


VIEW3D_REVISION = "a7c4e1d90b35"


def test_view3d_migration_leaves_existing_maps_without_a_3d_view_and_downgrades(
    migration_db: str,
) -> None:
    assert alembic(migration_db, "upgrade", API_PREFIX_REVISION).returncode == 0
    map_id, other_id = uuid.uuid4(), uuid.uuid4()
    for new_id, course in ((map_id, 8), (other_id, 9)):
        sql, params = insert_map(course, with_title=True)
        run_sql(migration_db, [(sql, {**params, "id": new_id})])
    bubble_id, other_bubble_id = uuid.uuid4(), uuid.uuid4()
    for new_bubble, on_map, course, activity in (
        (bubble_id, map_id, 8, 5),
        (other_bubble_id, other_id, 9, 6),
    ):
        run_sql(
            migration_db,
            [
                (
                    (
                        "INSERT INTO bubbles (id, course_map_id, moodle_course_id,"
                        " activity_id, x, y, status, created_at, updated_at) VALUES"
                        " (:id, :m, :c, :a, 0.5, 0.5, 'locked', now(), now())"
                    ),
                    {"id": new_bubble, "m": on_map, "c": course, "a": activity},
                )
            ],
        )

    up = alembic(migration_db, "upgrade", VIEW3D_REVISION)

    assert up.returncode == 0, up.stderr
    assert fetch(migration_db, "SELECT view3d_scene FROM course_maps") == [
        (None,),
        (None,),
    ]
    assert fetch(migration_db, "SELECT count(*) FROM course_map_view3d_bindings") == [
        (0,)
    ]

    def bind(on_map: uuid.UUID, zone: int, bubble: uuid.UUID) -> None:
        run_sql(
            migration_db,
            [
                (
                    (
                        "INSERT INTO course_map_view3d_bindings (id, course_map_id,"
                        " zone_id, bubble_id, created_at, updated_at)"
                        " VALUES (:i, :m, :z, :b, now(), now())"
                    ),
                    {"i": uuid.uuid4(), "m": on_map, "z": zone, "b": bubble},
                )
            ],
        )

    bind(map_id, 0, bubble_id)
    with pytest.raises(Exception, match="uq_course_map_view3d_bindings_map_zone"):
        bind(map_id, 0, bubble_id)
    with pytest.raises(Exception, match="uq_course_map_view3d_bindings_map_bubble"):
        bind(map_id, 1, bubble_id)
    with pytest.raises(Exception, match="ck_course_map_view3d_bindings_zone"):
        bind(map_id, -1, bubble_id)
    # Deleting the bubble takes its binding along; so does deleting the map.
    bind(other_id, 3, other_bubble_id)
    run_sql(migration_db, [("DELETE FROM bubbles WHERE id = :b", {"b": bubble_id})])
    assert fetch(migration_db, "SELECT zone_id FROM course_map_view3d_bindings") == [
        (3,)
    ]
    run_sql(migration_db, [("DELETE FROM course_maps WHERE id = :m", {"m": other_id})])
    assert fetch(migration_db, "SELECT count(*) FROM course_map_view3d_bindings") == [
        (0,)
    ]

    down = alembic(migration_db, "downgrade", API_PREFIX_REVISION)
    assert down.returncode == 0, down.stderr
    assert not fetch(
        migration_db,
        "SELECT 1 FROM pg_tables WHERE tablename = 'course_map_view3d_bindings'",
    )
    assert not fetch(
        migration_db,
        "SELECT 1 FROM information_schema.columns"
        " WHERE table_name = 'course_maps' AND column_name = 'view3d_scene'",
    )


def test_a_map_published_before_3d_views_is_up_to_date_after_the_migration(
    migration_db: str,
) -> None:
    from app.enums import PublicationStatus
    from app.publishing.snapshot import content_hash
    from app.repositories.sqlalchemy.asset_repository import (
        SqlAlchemyAssetRepository,
    )
    from app.repositories.sqlalchemy.bubble_repository import (
        SqlAlchemyBubbleRepository,
    )
    from app.repositories.sqlalchemy.course_map_repository import (
        SqlAlchemyCourseMapRepository,
    )
    from app.repositories.sqlalchemy.publication_repository import (
        SqlAlchemyPublicationRepository,
    )
    from app.repositories.sqlalchemy.view3d_repository import (
        SqlAlchemyView3dRepository,
    )
    from app.schemas.map_settings import MapSettings
    from app.schemas.view3d import View3d, View3dBinding
    from app.services.publication_service import PublicationService

    assert alembic(migration_db, "upgrade", API_PREFIX_REVISION).returncode == 0
    map_id, bubble_id = uuid.uuid4(), uuid.uuid4()
    sql, params = insert_map(8, with_title=True)
    run_sql(
        migration_db,
        [
            (sql, {**params, "id": map_id}),
            (
                (
                    "INSERT INTO bubbles (id, course_map_id, moodle_course_id,"
                    " activity_id, x, y, status, created_at, updated_at) VALUES"
                    " (:b, :m, 8, 5, 0.5, 0.25, 'locked', now(), now())"
                ),
                {"b": bubble_id, "m": map_id},
            ),
        ],
    )
    [(skin_id, skin_name, skin_config)] = fetch(
        migration_db, "SELECT id, name, config FROM skins WHERE is_default"
    )
    # A snapshot exactly as the application wrote them before this change: no
    # `view3d` key at all.
    legacy = {
        "schemaVersion": 1,
        "title": "Seeded",
        "imageUrl": "/x.png",
        "settings": MapSettings().to_stored(),
        "defaultSkinId": None,
        "skinRules": [],
        "skins": {
            str(skin_id): {"name": skin_name, "isDefault": True, "config": skin_config}
        },
        "bubbles": [
            {
                "id": str(bubble_id),
                "activityId": 5,
                "x": 0.5,
                "y": 0.25,
                "icon": None,
                "skinId": None,
                "sequence": 0,
            }
        ],
    }
    run_sql(
        migration_db,
        [
            (
                (
                    "INSERT INTO publications (id, course_map_id, number, snapshot,"
                    " content_hash, asset_ids, created_at, updated_at) VALUES"
                    " (:id, :m, 1, CAST(:s AS jsonb), :h, ARRAY[]::uuid[],"
                    " now(), now())"
                ),
                {
                    "id": uuid.uuid4(),
                    "m": map_id,
                    "s": json.dumps(legacy),
                    "h": content_hash(legacy),
                },
            )
        ],
    )

    assert alembic(migration_db, "upgrade", "head").returncode == 0

    async def check() -> None:
        engine = create_async_engine(migration_db)
        try:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                bind_scope(session, Scope(LEGACY_TENANT_ID))
                maps = SqlAlchemyCourseMapRepository(session)
                view3d = SqlAlchemyView3dRepository(session)
                service = PublicationService(
                    SqlAlchemyPublicationRepository(session),
                    maps,
                    SqlAlchemyBubbleRepository(session),
                    view3d,
                    SqlAlchemyAssetRepository(session),
                    keep=10,
                )

                untouched = await service.state(map_id)
                assert untouched.status is PublicationStatus.UP_TO_DATE
                assert untouched.draft_hash == content_hash(legacy)
                assert untouched.changes is not None
                assert untouched.changes.view3d_changed is False

                current = await maps.get_by_id(map_id)
                assert current is not None
                await view3d.replace(
                    map_id,
                    View3d(
                        scene="region-encuentros",
                        bindings=[View3dBinding(zone_id=0, bubble_id=bubble_id)],
                    ),
                    current.revision,
                )
                changed = await service.state(map_id)
                assert changed.status is PublicationStatus.UNPUBLISHED_CHANGES
                assert changed.changes is not None
                assert changed.changes.view3d_changed is True

                await view3d.replace(map_id, None, current.revision + 1)
                back = await service.state(map_id)
                assert back.status is PublicationStatus.UP_TO_DATE
        finally:
            await engine.dispose()

    asyncio.run(check())


BEFORE_TENANTS = "a7c4e1d90b35"  # the last revision without tenants
BEFORE_SCOPING = "e5b2c8f4a136"
BEFORE_AUDIO = "f6c3d9a5b247"  # the last revision without audio


def _bubble(
    map_id: uuid.UUID, course: int, activity: int
) -> tuple[str, dict[str, Any]]:
    return (
        (
            "INSERT INTO bubbles (id, course_map_id, moodle_course_id, activity_id,"
            " x, y, status, created_at, updated_at) VALUES"
            " (:id, :m, :c, :a, 0.5, 0.5, 'locked', now(), now())"
        ),
        {"id": uuid.uuid4(), "m": map_id, "c": course, "a": activity},
    )


def test_scoping_attaches_every_existing_row_to_the_legacy_tenant(
    migration_db: str,
) -> None:
    assert alembic(migration_db, "upgrade", BEFORE_SCOPING).returncode == 0
    map_sql, map_params = insert_map(8, with_title=True)
    map_id = map_params["id"]
    run_sql(
        migration_db,
        [
            (map_sql, map_params),
            _bubble(map_id, 8, 5),
            _bubble(map_id, 8, 6),
            (
                (
                    "INSERT INTO assets (id, kind, mime, width, height, bytes, sha256,"
                    " title, created_at, updated_at) VALUES (:id, 'bubble', 'image/png',"
                    " 1, 1, 10, :h, 'T', now(), now())"
                ),
                {"id": uuid.uuid4(), "h": "a" * 64},
            ),
            (
                (
                    "INSERT INTO skins (id, name, kind, config, is_builtin, is_default,"
                    " created_at, updated_at) VALUES (:id, 'Mine', 'procedural',"
                    " '{}'::jsonb, false, false, now(), now())"
                ),
                {"id": uuid.uuid4()},
            ),
        ],
    )
    builtins_before = fetch(migration_db, "SELECT count(*) FROM skins WHERE is_builtin")

    result = alembic(migration_db, "upgrade", BEFORE_AUDIO)
    assert result.returncode == 0, result.stderr

    legacy = str(LEGACY_TENANT_ID)
    for table in ("course_maps", "bubbles", "assets"):
        rows = fetch(migration_db, f"SELECT DISTINCT tenant_id::text FROM {table}")
        assert rows == [(legacy,)], table
    assert fetch(
        migration_db, "SELECT tenant_id::text FROM skins WHERE name = 'Mine'"
    ) == [(legacy,)]
    # The built-in skins belong to nobody.
    assert fetch(migration_db, "SELECT count(*) FROM skins WHERE is_builtin") == (
        builtins_before
    )
    assert (
        fetch(
            migration_db,
            "SELECT count(*) FROM skins WHERE is_builtin AND tenant_id IS NULL",
        )
        == builtins_before
    )
    assert fetch(
        migration_db,
        "SELECT name, status FROM tenants WHERE id = :t".replace(":t", f"'{legacy}'"),
    ) == [("legacy", "active")]
    nullable = fetch(
        migration_db,
        "SELECT table_name, is_nullable FROM information_schema.columns"
        " WHERE column_name = 'tenant_id'"
        " AND table_name IN ('course_maps', 'bubbles', 'assets', 'skins')"
        " ORDER BY table_name",
    )
    assert nullable == [
        ("assets", "NO"),
        ("bubbles", "NO"),
        ("course_maps", "NO"),
        ("skins", "YES"),
    ]


def test_after_scoping_two_tenants_can_share_course_activity_and_hash(
    migration_db: str,
) -> None:
    assert alembic(migration_db, "upgrade", "head").returncode == 0
    other = uuid.uuid4()
    map_a, map_b = uuid.uuid4(), uuid.uuid4()

    def tenant_map(map_id: uuid.UUID, tenant: str) -> tuple[str, dict[str, Any]]:
        return (
            (
                "INSERT INTO course_maps (id, tenant_id, title, moodle_course_id,"
                " moodle_section_id, image_url, created_at, updated_at) VALUES"
                " (:id, CAST(:t AS uuid), 'M', 8, 1, '/x.png', now(), now())"
            ),
            {"id": map_id, "t": tenant},
        )

    def tenant_bubble(
        map_id: uuid.UUID, tenant: str, activity: int = 5
    ) -> tuple[str, dict[str, Any]]:
        return (
            (
                "INSERT INTO bubbles (id, tenant_id, course_map_id, moodle_course_id,"
                " activity_id, x, y, status, created_at, updated_at) VALUES"
                " (:id, CAST(:t AS uuid), :m, 8, :a, 0.5, 0.5, 'locked', now(), now())"
            ),
            {"id": uuid.uuid4(), "t": tenant, "m": map_id, "a": activity},
        )

    run_sql(
        migration_db,
        [
            (
                (
                    "INSERT INTO tenants (id, name, issuer, host_origin, status,"
                    " asset_quota_bytes, created_at, updated_at) VALUES (:id, 'o',"
                    " 'https://o.example.com', 'https://o.example.com', 'active', 1,"
                    " now(), now())"
                ),
                {"id": other},
            ),
            tenant_map(map_a, str(LEGACY_TENANT_ID)),
            tenant_map(map_b, str(other)),  # same course and section: fine
            tenant_bubble(map_a, str(LEGACY_TENANT_ID)),
            tenant_bubble(map_b, str(other)),  # same activity: fine
        ],
    )
    assert fetch(migration_db, "SELECT count(*) FROM bubbles") == [(2,)]
    # A bubble cannot sit on another tenant's map.
    with pytest.raises(Exception, match="fk_bubbles_course_map"):
        run_sql(migration_db, [tenant_bubble(map_b, str(LEGACY_TENANT_ID), 6)])


def test_scoping_downgrades_and_upgrades_again(migration_db: str) -> None:
    assert alembic(migration_db, "upgrade", "head").returncode == 0
    down = alembic(migration_db, "downgrade", BEFORE_SCOPING)
    assert down.returncode == 0, down.stderr
    columns = fetch(
        migration_db,
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_name = 'course_maps'",
    )
    assert ("tenant_id",) not in columns
    assert alembic(migration_db, "upgrade", "head").returncode == 0
    assert alembic(migration_db, "downgrade", BEFORE_TENANTS).returncode == 0
    tables = fetch(
        migration_db, "SELECT tablename FROM pg_tables WHERE schemaname='public'"
    )
    assert ("tenants",) not in tables


def _image_asset(sha: str) -> tuple[str, dict[str, Any]]:
    return (
        (
            "INSERT INTO assets (id, tenant_id, kind, mime, width, height, bytes,"
            " sha256, title, created_at, updated_at) VALUES (:id, :t, 'bubble',"
            " 'image/png', 8, 8, 10, :sha, 'Old', now(), now())"
        ),
        {"id": uuid.uuid4(), "t": LEGACY_TENANT_ID, "sha": sha},
    )


def _audio_asset(
    sha: str, *, tenant: uuid.UUID | None = None, credit: str | None = "Autor"
) -> tuple[str, dict[str, Any]]:
    return (
        (
            "INSERT INTO assets (id, tenant_id, kind, mime, bytes, sha256, title,"
            " credit, duration_seconds, created_at, updated_at) VALUES (:id, :t,"
            " 'audio', 'audio/mpeg', 10, :sha, 'Pista', :credit, 12.5, now(), now())"
        ),
        {"id": uuid.uuid4(), "t": tenant, "sha": sha, "credit": credit},
    )


def test_audio_migration_keeps_images_and_enforces_what_each_kind_needs(
    migration_db: str,
) -> None:
    assert alembic(migration_db, "upgrade", BEFORE_AUDIO).returncode == 0
    run_sql(migration_db, [_image_asset("a" * 64)])

    up = alembic(migration_db, "upgrade", "head")

    assert up.returncode == 0, up.stderr
    assert fetch(
        migration_db,
        "SELECT width, height, duration_seconds, license, uploaded_by_sub,"
        " rights_declared_at, quarantined_at, quarantine_reason FROM assets",
    ) == [(8, 8, None, None, None, None, None, None)]
    # A library track (no tenant) and a tenant's own are both fine...
    run_sql(
        migration_db,
        [
            _audio_asset("b" * 64),
            _audio_asset("b" * 64, tenant=LEGACY_TENANT_ID),
        ],
    )
    # ...but the library cannot hold the same bytes twice,
    with pytest.raises(Exception, match="uq_assets_library_kind_sha256"):
        run_sql(migration_db, [_audio_asset("b" * 64)])
    # audio needs a credit,
    with pytest.raises(Exception, match="ck_assets_kind_fields"):
        run_sql(migration_db, [_audio_asset("c" * 64, credit=None)])
    # and an image needs its tenant and its dimensions.
    sql, params = _image_asset("d" * 64)
    with pytest.raises(Exception, match="ck_assets_kind_fields"):
        run_sql(migration_db, [(sql, {**params, "t": None})])
    with pytest.raises(Exception, match="ck_assets_kind_fields"):
        run_sql(
            migration_db,
            [(sql.replace("8, 8, 10", "NULL, NULL, 10"), params)],
        )


def test_audio_migration_refuses_to_downgrade_over_audio_and_downgrades_without(
    migration_db: str,
) -> None:
    assert alembic(migration_db, "upgrade", "head").returncode == 0
    run_sql(migration_db, [_image_asset("a" * 64), _audio_asset("b" * 64)])

    refused = alembic(migration_db, "downgrade", BEFORE_AUDIO)

    assert refused.returncode != 0
    assert "audio asset" in refused.stderr
    assert fetch(migration_db, "SELECT count(*) FROM assets") == [(2,)]

    run_sql(migration_db, [("DELETE FROM assets WHERE kind = 'audio'", {})])
    down = alembic(migration_db, "downgrade", BEFORE_AUDIO)
    assert down.returncode == 0, down.stderr
    assert fetch(migration_db, "SELECT width FROM assets") == [(8,)]
    assert not fetch(
        migration_db,
        "SELECT 1 FROM pg_tables WHERE tablename = 'asset_audit_events'",
    )
    assert alembic(migration_db, "upgrade", "head").returncode == 0


def test_a_map_published_before_music_is_up_to_date_after_the_migration(
    migration_db: str,
) -> None:
    from app.enums import PublicationStatus
    from app.publishing.snapshot import content_hash
    from app.repositories.sqlalchemy.asset_repository import (
        SqlAlchemyAssetRepository,
    )
    from app.repositories.sqlalchemy.bubble_repository import (
        SqlAlchemyBubbleRepository,
    )
    from app.repositories.sqlalchemy.course_map_repository import (
        SqlAlchemyCourseMapRepository,
    )
    from app.repositories.sqlalchemy.publication_repository import (
        SqlAlchemyPublicationRepository,
    )
    from app.repositories.sqlalchemy.view3d_repository import (
        SqlAlchemyView3dRepository,
    )
    from app.services.publication_service import PublicationService

    assert alembic(migration_db, "upgrade", BEFORE_AUDIO).returncode == 0
    map_id, bubble_id, track_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    run_sql(
        migration_db,
        [
            (
                (
                    "INSERT INTO course_maps (id, tenant_id, title, moodle_course_id,"
                    " image_url, created_at, updated_at) VALUES (:id, :t, 'Seeded', 8,"
                    " '/x.png', now(), now())"
                ),
                {"id": map_id, "t": LEGACY_TENANT_ID},
            ),
            (
                (
                    "INSERT INTO bubbles (id, tenant_id, course_map_id,"
                    " moodle_course_id, activity_id, x, y, status, created_at,"
                    " updated_at) VALUES (:b, :t, :m, 8, 5, 0.5, 0.25, 'locked',"
                    " now(), now())"
                ),
                {"b": bubble_id, "t": LEGACY_TENANT_ID, "m": map_id},
            ),
        ],
    )
    [(skin_id, skin_name, skin_config)] = fetch(
        migration_db, "SELECT id, name, config FROM skins WHERE is_default"
    )
    # A snapshot exactly as the application wrote them before music existed,
    # spelled out so that it cannot drift with the code: no `audio` in the
    # settings and no `audioInfo` at all.
    legacy = {
        "schemaVersion": 1,
        "title": "Seeded",
        "imageUrl": "/x.png",
        "settings": {
            "schemaVersion": 1,
            "mode": "explorative",
            "fit": "fit-width",
            "initialView": None,
            "path": {
                "visible": True,
                "style": "dashed",
                "color": None,
                "animated": True,
            },
            "ambient": {"kind": "none", "intensity": 0.5},
            "intro": "none",
        },
        "defaultSkinId": None,
        "skinRules": [],
        "skins": {
            str(skin_id): {"name": skin_name, "isDefault": True, "config": skin_config}
        },
        "bubbles": [
            {
                "id": str(bubble_id),
                "activityId": 5,
                "x": 0.5,
                "y": 0.25,
                "icon": None,
                "skinId": None,
                "sequence": 0,
            }
        ],
    }
    run_sql(
        migration_db,
        [
            (
                (
                    "INSERT INTO publications (id, course_map_id, number, snapshot,"
                    " content_hash, asset_ids, created_at, updated_at) VALUES"
                    " (:id, :m, 1, CAST(:s AS jsonb), :h, ARRAY[]::uuid[],"
                    " now(), now())"
                ),
                {
                    "id": uuid.uuid4(),
                    "m": map_id,
                    "s": json.dumps(legacy),
                    "h": content_hash(legacy),
                },
            )
        ],
    )

    assert alembic(migration_db, "upgrade", "head").returncode == 0

    run_sql(
        migration_db,
        [
            (
                (
                    "INSERT INTO assets (id, tenant_id, kind, mime, bytes, sha256,"
                    " title, credit, duration_seconds, created_at, updated_at) VALUES"
                    " (:id, NULL, 'audio', 'audio/mpeg', 10, :sha, 'Pista', 'Ana',"
                    " 12.5, now(), now())"
                ),
                {"id": track_id, "sha": "c" * 64},
            )
        ],
    )

    async def check() -> None:
        engine = create_async_engine(migration_db)
        try:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                bind_scope(session, Scope(LEGACY_TENANT_ID))
                service = PublicationService(
                    SqlAlchemyPublicationRepository(session),
                    SqlAlchemyCourseMapRepository(session),
                    SqlAlchemyBubbleRepository(session),
                    SqlAlchemyView3dRepository(session),
                    SqlAlchemyAssetRepository(session),
                    keep=10,
                )

                untouched = await service.state(map_id)
                assert untouched.status is PublicationStatus.UP_TO_DATE
                assert untouched.draft_hash == content_hash(legacy)
                assert untouched.changes is not None
                assert untouched.changes.audio_changed is False

            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE course_maps SET settings = CAST(:s AS jsonb)"
                        " WHERE id = :m"
                    ),
                    {
                        "m": map_id,
                        "s": json.dumps({"audio": {"assetId": str(track_id)}}),
                    },
                )
            async with AsyncSession(engine, expire_on_commit=False) as session:
                bind_scope(session, Scope(LEGACY_TENANT_ID))
                service = PublicationService(
                    SqlAlchemyPublicationRepository(session),
                    SqlAlchemyCourseMapRepository(session),
                    SqlAlchemyBubbleRepository(session),
                    SqlAlchemyView3dRepository(session),
                    SqlAlchemyAssetRepository(session),
                    keep=10,
                )
                changed = await service.state(map_id)
                assert changed.status is PublicationStatus.UNPUBLISHED_CHANGES
                assert changed.changes is not None
                assert changed.changes.audio_changed is True
                assert changed.changes.settings_changed is False

            async with engine.begin() as conn:
                await conn.execute(
                    text("UPDATE course_maps SET settings = '{}'::jsonb")
                )
            async with AsyncSession(engine, expire_on_commit=False) as session:
                bind_scope(session, Scope(LEGACY_TENANT_ID))
                back = await PublicationService(
                    SqlAlchemyPublicationRepository(session),
                    SqlAlchemyCourseMapRepository(session),
                    SqlAlchemyBubbleRepository(session),
                    SqlAlchemyView3dRepository(session),
                    SqlAlchemyAssetRepository(session),
                    keep=10,
                ).state(map_id)
                assert back.status is PublicationStatus.UP_TO_DATE
        finally:
            await engine.dispose()

    asyncio.run(check())


BEFORE_WIDGETS = "c4f8a2d6e913"  # the last revision without the widgets flag


def test_widgets_migration_switches_the_flag_off_for_existing_tenants(
    migration_db: str,
) -> None:
    assert alembic(migration_db, "upgrade", BEFORE_WIDGETS).returncode == 0

    up = alembic(migration_db, "upgrade", "head")

    assert up.returncode == 0, up.stderr
    assert fetch(migration_db, "SELECT widgets_enabled FROM tenants") == [(False,)]
    run_sql(
        migration_db,
        [
            (
                (
                    "INSERT INTO tenants (id, name, issuer, host_origin, status,"
                    " asset_quota_bytes, created_at, updated_at) VALUES (:id, 'New',"
                    " 'https://n.example', 'https://n.example', 'active', 1,"
                    " now(), now())"
                ),
                {"id": uuid.uuid4()},
            )
        ],
    )
    assert fetch(
        migration_db, "SELECT widgets_enabled FROM tenants WHERE name = 'New'"
    ) == [(False,)]


def test_widgets_migration_downgrades_and_upgrades_again(migration_db: str) -> None:
    assert alembic(migration_db, "upgrade", "head").returncode == 0
    run_sql(migration_db, [("UPDATE tenants SET widgets_enabled = true", {})])

    down = alembic(migration_db, "downgrade", BEFORE_WIDGETS)

    assert down.returncode == 0, down.stderr
    assert not fetch(
        migration_db,
        "SELECT 1 FROM information_schema.columns"
        " WHERE table_name = 'tenants' AND column_name = 'widgets_enabled'",
    )
    assert alembic(migration_db, "upgrade", "head").returncode == 0
    assert fetch(migration_db, "SELECT widgets_enabled FROM tenants") == [(False,)]


BEFORE_CONTENT = "a9d3f7b2c814"  # the last revision without content bubbles


def _bubble_at_content_revision(
    map_id: uuid.UUID, activity_id: int
) -> tuple[str, dict[str, Any]]:
    return (
        (
            "INSERT INTO bubbles (id, tenant_id, course_map_id, moodle_course_id,"
            " activity_id, x, y, status, created_at, updated_at) VALUES (:id, :t,"
            " :m, 8, :a, 0.5, 0.25, 'locked', now(), now())"
        ),
        {"id": uuid.uuid4(), "t": LEGACY_TENANT_ID, "m": map_id, "a": activity_id},
    )


def _map_at_content_revision(map_id: uuid.UUID) -> tuple[str, dict[str, Any]]:
    return (
        (
            "INSERT INTO course_maps (id, tenant_id, title, moodle_course_id,"
            " image_url, created_at, updated_at) VALUES (:id, :t, 'Seeded', 8,"
            " '/x.png', now(), now())"
        ),
        {"id": map_id, "t": LEGACY_TENANT_ID},
    )


def _content_bubble_sql(
    map_id: uuid.UUID, **overrides: Any
) -> tuple[str, dict[str, Any]]:
    params: dict[str, Any] = {
        "id": uuid.uuid4(),
        "t": LEGACY_TENANT_ID,
        "m": map_id,
        "a": None,
        "k": "content",
        "c": json.dumps({"type": "blocks"}),
        **overrides,
    }
    return (
        (
            "INSERT INTO bubbles (id, tenant_id, course_map_id, moodle_course_id,"
            " kind, activity_id, content, x, y, status, created_at, updated_at)"
            " VALUES (:id, :t, :m, 8, :k, :a, CAST(:c AS jsonb), 0.5, 0.5, 'locked',"
            " now(), now())"
        ),
        params,
    )


def test_content_migration_keeps_existing_bubbles_as_activity_bubbles(
    migration_db: str,
) -> None:
    assert alembic(migration_db, "upgrade", BEFORE_CONTENT).returncode == 0
    map_id = uuid.uuid4()
    run_sql(
        migration_db,
        [_map_at_content_revision(map_id), _bubble_at_content_revision(map_id, 5)],
    )

    up = alembic(migration_db, "upgrade", "head")

    assert up.returncode == 0, up.stderr
    assert fetch(migration_db, "SELECT kind, activity_id, content FROM bubbles") == [
        ("activity", 5, None)
    ]


def test_content_migration_enforces_the_shape_and_the_partial_index(
    migration_db: str,
) -> None:
    assert alembic(migration_db, "upgrade", "head").returncode == 0
    map_id = uuid.uuid4()
    run_sql(
        migration_db,
        [_map_at_content_revision(map_id), _bubble_at_content_revision(map_id, 5)],
    )

    # content bubbles need no activity, and any number of them fit,
    run_sql(migration_db, [_content_bubble_sql(map_id), _content_bubble_sql(map_id)])
    # an activity still cannot be placed twice in the course,
    with pytest.raises(Exception, match="uq_bubbles_moodle_course_id_activity_id"):
        run_sql(migration_db, [_bubble_at_content_revision(map_id, 5)])
    # and the check refuses every mixed shape.
    for overrides in (
        {"a": 3},  # content with an activity
        {"c": None},  # content without content
        {"k": "activity"},  # activity without an activity
        {"k": "activity", "a": 3},  # activity carrying content
        {"k": "other"},
    ):
        with pytest.raises(Exception, match="ck_bubbles_kind_fields"):
            run_sql(migration_db, [_content_bubble_sql(map_id, **overrides)])
    [(definition,)] = fetch(
        migration_db,
        "SELECT indexdef FROM pg_indexes"
        " WHERE indexname = 'uq_bubbles_moodle_course_id_activity_id'",
    )
    assert "UNIQUE" in definition and "WHERE" in definition


def test_content_migration_refuses_to_downgrade_over_content_bubbles(
    migration_db: str,
) -> None:
    assert alembic(migration_db, "upgrade", "head").returncode == 0
    map_id = uuid.uuid4()
    run_sql(
        migration_db,
        [
            _map_at_content_revision(map_id),
            _bubble_at_content_revision(map_id, 5),
            _content_bubble_sql(map_id),
        ],
    )

    refused = alembic(migration_db, "downgrade", BEFORE_CONTENT)

    assert refused.returncode != 0
    assert "content bubble" in refused.stderr
    assert fetch(migration_db, "SELECT count(*) FROM bubbles") == [(2,)]

    run_sql(migration_db, [("DELETE FROM bubbles WHERE kind = 'content'", {})])
    down = alembic(migration_db, "downgrade", BEFORE_CONTENT)
    assert down.returncode == 0, down.stderr
    assert fetch(migration_db, "SELECT activity_id FROM bubbles") == [(5,)]
    with pytest.raises(Exception, match="uq_bubbles_moodle_course_id_activity_id"):
        run_sql(migration_db, [_bubble_at_content_revision(map_id, 5)])
    assert alembic(migration_db, "upgrade", "head").returncode == 0


def test_every_existing_publication_is_up_to_date_after_content_bubbles(
    migration_db: str,
) -> None:
    from app.enums import PublicationStatus
    from app.publishing.snapshot import content_hash
    from app.repositories.sqlalchemy.asset_repository import (
        SqlAlchemyAssetRepository,
    )
    from app.repositories.sqlalchemy.bubble_repository import (
        SqlAlchemyBubbleRepository,
    )
    from app.repositories.sqlalchemy.course_map_repository import (
        SqlAlchemyCourseMapRepository,
    )
    from app.repositories.sqlalchemy.publication_repository import (
        SqlAlchemyPublicationRepository,
    )
    from app.repositories.sqlalchemy.view3d_repository import (
        SqlAlchemyView3dRepository,
    )
    from app.services.publication_service import PublicationService

    assert alembic(migration_db, "upgrade", BEFORE_CONTENT).returncode == 0
    map_id, bubble_id = uuid.uuid4(), uuid.uuid4()
    run_sql(
        migration_db,
        [_map_at_content_revision(map_id)]
        + [
            (
                (
                    "INSERT INTO bubbles (id, tenant_id, course_map_id,"
                    " moodle_course_id, activity_id, x, y, status, created_at,"
                    " updated_at) VALUES (:b, :t, :m, 8, 5, 0.5, 0.25, 'locked',"
                    " now(), now())"
                ),
                {"b": bubble_id, "t": LEGACY_TENANT_ID, "m": map_id},
            )
        ],
    )
    [(skin_id, skin_name, skin_config)] = fetch(
        migration_db, "SELECT id, name, config FROM skins WHERE is_default"
    )
    # A snapshot exactly as the application wrote them before content bubbles
    # existed, spelled out so that it cannot drift with the code: bubbles have
    # no `kind` and no `content`.
    legacy = {
        "schemaVersion": 1,
        "title": "Seeded",
        "imageUrl": "/x.png",
        "settings": {
            "schemaVersion": 1,
            "mode": "explorative",
            "fit": "fit-width",
            "initialView": None,
            "path": {
                "visible": True,
                "style": "dashed",
                "color": None,
                "animated": True,
            },
            "ambient": {"kind": "none", "intensity": 0.5},
            "intro": "none",
        },
        "defaultSkinId": None,
        "skinRules": [],
        "skins": {
            str(skin_id): {"name": skin_name, "isDefault": True, "config": skin_config}
        },
        "bubbles": [
            {
                "id": str(bubble_id),
                "activityId": 5,
                "x": 0.5,
                "y": 0.25,
                "icon": None,
                "skinId": None,
                "sequence": 0,
            }
        ],
    }
    run_sql(
        migration_db,
        [
            (
                (
                    "INSERT INTO publications (id, course_map_id, number, snapshot,"
                    " content_hash, asset_ids, created_at, updated_at) VALUES"
                    " (:id, :m, 1, CAST(:s AS jsonb), :h, ARRAY[]::uuid[],"
                    " now(), now())"
                ),
                {
                    "id": uuid.uuid4(),
                    "m": map_id,
                    "s": json.dumps(legacy),
                    "h": content_hash(legacy),
                },
            )
        ],
    )

    assert alembic(migration_db, "upgrade", "head").returncode == 0

    async def check() -> None:
        engine = create_async_engine(migration_db)
        try:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                bind_scope(session, Scope(LEGACY_TENANT_ID))
                service = PublicationService(
                    SqlAlchemyPublicationRepository(session),
                    SqlAlchemyCourseMapRepository(session),
                    SqlAlchemyBubbleRepository(session),
                    SqlAlchemyView3dRepository(session),
                    SqlAlchemyAssetRepository(session),
                    keep=10,
                )
                untouched = await service.state(map_id)
                assert untouched.status is PublicationStatus.UP_TO_DATE
                assert untouched.draft_hash == content_hash(legacy)
                assert untouched.changes is not None
                assert untouched.changes.content_changed is False

            async with engine.begin() as conn:
                await conn.execute(*_content_bubble_sql_for(conn, map_id))
            async with AsyncSession(engine, expire_on_commit=False) as session:
                bind_scope(session, Scope(LEGACY_TENANT_ID))
                service = PublicationService(
                    SqlAlchemyPublicationRepository(session),
                    SqlAlchemyCourseMapRepository(session),
                    SqlAlchemyBubbleRepository(session),
                    SqlAlchemyView3dRepository(session),
                    SqlAlchemyAssetRepository(session),
                    keep=10,
                )
                changed = await service.state(map_id)
                assert changed.status is PublicationStatus.UNPUBLISHED_CHANGES
                assert changed.changes is not None
                assert changed.changes.bubbles_added == 1
        finally:
            await engine.dispose()

    asyncio.run(check())


def _content_bubble_sql_for(
    _conn: Any, map_id: uuid.UUID
) -> tuple[Any, dict[str, Any]]:
    valid = {
        "type": "blocks",
        "title": "Intro",
        "autoOpen": "never",
        "body": {"schemaVersion": 1, "blocks": []},
    }
    sql, params = _content_bubble_sql(map_id, c=json.dumps(valid))
    return text(sql), params


def test_every_existing_publication_is_up_to_date_after_the_welcome_curtain(
    migration_db: str,
) -> None:
    """No migration is needed (settings are JSON), and none must be implied:
    a publication written before the curtain existed, with a map whose stored
    settings have no `welcome` key, must still read as up to date."""
    from app.enums import PublicationStatus
    from app.publishing.snapshot import content_hash
    from app.repositories.sqlalchemy.asset_repository import (
        SqlAlchemyAssetRepository,
    )
    from app.repositories.sqlalchemy.bubble_repository import (
        SqlAlchemyBubbleRepository,
    )
    from app.repositories.sqlalchemy.course_map_repository import (
        SqlAlchemyCourseMapRepository,
    )
    from app.repositories.sqlalchemy.publication_repository import (
        SqlAlchemyPublicationRepository,
    )
    from app.repositories.sqlalchemy.view3d_repository import (
        SqlAlchemyView3dRepository,
    )
    from app.services.publication_service import PublicationService

    assert alembic(migration_db, "upgrade", BEFORE_CONTENT).returncode == 0
    map_id, bubble_id = uuid.uuid4(), uuid.uuid4()
    run_sql(
        migration_db,
        [
            _map_at_content_revision(map_id),
            (
                (
                    "INSERT INTO bubbles (id, tenant_id, course_map_id,"
                    " moodle_course_id, activity_id, x, y, status, created_at,"
                    " updated_at) VALUES (:b, :t, :m, 8, 5, 0.5, 0.25, 'locked',"
                    " now(), now())"
                ),
                {"b": bubble_id, "t": LEGACY_TENANT_ID, "m": map_id},
            ),
        ],
    )
    [(skin_id, skin_name, skin_config)] = fetch(
        migration_db, "SELECT id, name, config FROM skins WHERE is_default"
    )
    # Spelled out, so that it cannot drift with the code: no `audio`, no
    # `welcome`, as every publication was written until now.
    legacy = {
        "schemaVersion": 1,
        "title": "Seeded",
        "imageUrl": "/x.png",
        "settings": {
            "schemaVersion": 1,
            "mode": "explorative",
            "fit": "fit-width",
            "initialView": None,
            "path": {
                "visible": True,
                "style": "dashed",
                "color": None,
                "animated": True,
            },
            "ambient": {"kind": "none", "intensity": 0.5},
            "intro": "none",
        },
        "defaultSkinId": None,
        "skinRules": [],
        "skins": {
            str(skin_id): {"name": skin_name, "isDefault": True, "config": skin_config}
        },
        "bubbles": [
            {
                "id": str(bubble_id),
                "activityId": 5,
                "x": 0.5,
                "y": 0.25,
                "icon": None,
                "skinId": None,
                "sequence": 0,
            }
        ],
    }
    run_sql(
        migration_db,
        [
            (
                (
                    "INSERT INTO publications (id, course_map_id, number, snapshot,"
                    " content_hash, asset_ids, created_at, updated_at) VALUES"
                    " (:id, :m, 1, CAST(:s AS jsonb), :h, ARRAY[]::uuid[],"
                    " now(), now())"
                ),
                {
                    "id": uuid.uuid4(),
                    "m": map_id,
                    "s": json.dumps(legacy),
                    "h": content_hash(legacy),
                },
            )
        ],
    )

    # Every migration since then runs over the publication; none may move it.
    assert alembic(migration_db, "upgrade", "head").returncode == 0

    async def check() -> None:
        engine = create_async_engine(migration_db)
        try:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                bind_scope(session, Scope(LEGACY_TENANT_ID))
                service = PublicationService(
                    SqlAlchemyPublicationRepository(session),
                    SqlAlchemyCourseMapRepository(session),
                    SqlAlchemyBubbleRepository(session),
                    SqlAlchemyView3dRepository(session),
                    SqlAlchemyAssetRepository(session),
                    keep=10,
                )
                current = await service.state(map_id)
                assert current.status is PublicationStatus.UP_TO_DATE
                assert current.draft_hash == content_hash(legacy)
                assert current.changes is not None
                assert current.changes.settings_changed is False
        finally:
            await engine.dispose()

    asyncio.run(check())


BEFORE_APP_META = "b6e1c8a4d273"  # the last revision without `app_meta`


def test_app_meta_migration_adds_a_unique_key_value_table_and_downgrades(
    migration_db: str,
) -> None:
    assert alembic(migration_db, "upgrade", BEFORE_APP_META).returncode == 0

    up = alembic(migration_db, "upgrade", "head")

    assert up.returncode == 0, up.stderr
    assert fetch(migration_db, "SELECT count(*) FROM app_meta") == [(0,)]
    insert = (
        "INSERT INTO app_meta (id, key, value, created_at, updated_at)"
        " VALUES (:id, 'storage_id', 'x', now(), now())"
    )
    run_sql(migration_db, [(insert, {"id": uuid.uuid4()})])
    with pytest.raises(Exception, match="app_meta_key_key"):
        run_sql(migration_db, [(insert, {"id": uuid.uuid4()})])
    down = alembic(migration_db, "downgrade", BEFORE_APP_META)
    assert down.returncode == 0, down.stderr
    assert not fetch(
        migration_db,
        "SELECT 1 FROM information_schema.tables WHERE table_name = 'app_meta'",
    )
