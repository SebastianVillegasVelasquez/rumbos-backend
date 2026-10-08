"""Helpers for reading PostgreSQL constraint violations off SQLAlchemy errors."""

from sqlalchemy.exc import IntegrityError

FOREIGN_KEY_VIOLATION = "23503"  # PostgreSQL SQLSTATE


def constraint_name(exc: IntegrityError) -> str | None:
    """Name of the violated constraint (or unique index), as the driver reports it.

    asyncpg's exception (the SQLAlchemy adapter's `__cause__`) carries it.
    """
    cause = exc.orig.__cause__ if exc.orig else None
    return getattr(cause, "constraint_name", None)


def sqlstate(exc: IntegrityError) -> str | None:
    return getattr(exc.orig, "sqlstate", None)
