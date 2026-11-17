"""The assessment tables: migration up and down, and parity with the models."""

from tests.test_migrations import alembic, fetch, migration_db  # noqa: F401

TABLES = "('assessment_attempts', 'assessment_answers')"


def _tables(db: str) -> list[str]:
    rows = fetch(
        db,
        "SELECT table_name FROM information_schema.tables "
        f"WHERE table_name IN {TABLES} ORDER BY 1",
    )
    return [r[0] for r in rows]


def test_the_migrations_match_the_models_and_the_new_one_is_reversible(
    migration_db: str,  # noqa: F811
) -> None:
    up = alembic(migration_db, "upgrade", "head")
    assert up.returncode == 0, up.stderr
    assert _tables(migration_db) == ["assessment_answers", "assessment_attempts"]
    parity = alembic(migration_db, "check")
    assert parity.returncode == 0, parity.stdout + parity.stderr

    down = alembic(migration_db, "downgrade", "a5d8c3f1e692")
    assert down.returncode == 0, down.stderr
    assert _tables(migration_db) == []

    again = alembic(migration_db, "upgrade", "head")
    assert again.returncode == 0, again.stderr
    assert _tables(migration_db) == ["assessment_answers", "assessment_attempts"]
