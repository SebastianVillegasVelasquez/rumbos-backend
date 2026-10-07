"""Alembic migrations against a database that already contains rows.

Each test gets its own throwaway `<name>_migration_test` database and runs the
real `alembic` CLI in a subprocess (env.py calls `asyncio.run`, which cannot
nest inside pytest-asyncio's loop). `DATABASE_URL` in the child's environment
overrides `.env`, so it never touches development data.
"""

import asyncio
import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.conftest import _test_database_url, recreate_database

INITIAL = "ffbafbaf2f65"  # course_maps + bubbles, before titles
TITLE = "3b7d1c9e5a42"


@pytest.fixture
def migration_db() -> Iterator[str]:
    url = _test_database_url("migration_test")
    asyncio.run(recreate_database(url))
    yield url


def alembic(db_url: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "DATABASE_URL": db_url}
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
