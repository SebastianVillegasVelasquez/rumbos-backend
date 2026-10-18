import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Index, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.enums import Role
from app.models.base import BaseORM


class AuthORM(DeclarativeBase):
    """Base for the auth tables, which have natural keys instead of a UUIDv7 `id`.

    Shares `BaseORM`'s metadata, so `create_all` and Alembic see them as well.
    """

    __abstract__ = True
    metadata = BaseORM.metadata


class AccessToken(AuthORM):
    """A bearer token. Only its SHA-256 is stored, so a database leak does not
    yield usable tokens. Bound to (tenant, course, role); `sub` is the launch's
    pseudonymous user id, kept for the audit trail."""

    __tablename__ = "access_tokens"
    __table_args__ = (Index("ix_access_tokens_expires_at", "expires_at"),)

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    course_id: Mapped[int]
    role: Mapped[Role] = mapped_column(
        Enum(
            Role,
            native_enum=False,
            length=20,
            values_callable=lambda e: [m.value for m in e],
            validate_strings=True,
        )
    )
    sub: Mapped[str] = mapped_column(String(32))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class UsedJti(AuthORM):
    """A launch assertion's `jti` that was already redeemed: a second use is a
    replay. Kept until the assertion could no longer pass the time checks."""

    __tablename__ = "used_jtis"
    __table_args__ = (Index("ix_used_jtis_expires_at", "expires_at"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    jti: Mapped[str] = mapped_column(String(128), primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
