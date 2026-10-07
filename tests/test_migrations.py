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


def insert_map(course_id: int) -> tuple[str, dict[str, Any]]:
    return (
        (
            "INSERT INTO course_maps (id, moodle_course_id, image_url, created_at,"
            " updated_at) VALUES (:id, :c, '/x.png', now(), now())"
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
