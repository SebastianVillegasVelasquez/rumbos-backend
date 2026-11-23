import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseORM

UQ_OUTBOX_ATTEMPT = "uq_passback_outbox_attempt"
UQ_MIRROR_DEFINITION = "uq_mirror_definitions_tenant_form_hash"


class PassbackOutbox(BaseORM):
    """A signed result waiting to be told to Moodle. One row per attempt (the
    unique index makes enqueuing idempotent), written in the same transaction
    that closes the attempt, so a result is never lost between the two.

    `status`: `pending` (to send), `sent`, `failed_permanent` (Moodle refused
    it for good; an operator looks). Worker commands read this table in the
    system scope, so it is not tenant-filtered; `tenant_id` says whose it is.
    """

    __tablename__ = "passback_outbox"
    __table_args__ = (
        Index(UQ_OUTBOX_ATTEMPT, "attempt_id", unique=True),
        Index(
            "ix_passback_outbox_due",
            "next_try_at",
            postgresql_where=text("status = 'pending'"),
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    moodle_course_id: Mapped[int] = mapped_column(Integer)
    attempt_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assessment_attempts.id", ondelete="CASCADE")
    )
    # The payload that was signed, kept so a resend is byte-identical.
    statement: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_try_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class CompletionAudit(BaseORM):
    """Append-only trail of what was told to Moodle and what it answered.
    Rows are only ever inserted."""

    __tablename__ = "completion_audit"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    moodle_course_id: Mapped[int] = mapped_column(Integer)
    attempt_id: Mapped[uuid.UUID] = mapped_column(index=True)
    # enqueued | sent | retry | failed_permanent | reconciled
    event: Mapped[str] = mapped_column(String(24))
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MirrorDefinition(BaseORM):
    """An exam definition waiting to be told to the plugin's mirror, and the
    immutable copy of it. One row per (tenant, form, `definition_hash`): the
    hash is the form's content, so a form that did not change is never sent
    twice and an old version stays readable after a content deploy.

    Delivery follows the outbox pattern (`status`, `attempts`, `next_try_at`):
    `pending`, `sent`, `failed_permanent`. `statement` is the signed payload's
    claims, kept so a resend is byte-identical. Attempts that name this hash
    wait until it is `sent`."""

    __tablename__ = "mirror_definitions"
    __table_args__ = (
        Index(
            UQ_MIRROR_DEFINITION, "tenant_id", "form_id", "definition_hash", unique=True
        ),
        Index(
            "ix_mirror_definitions_due",
            "next_try_at",
            postgresql_where=text("status = 'pending'"),
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    form_id: Mapped[str] = mapped_column(String(80))
    definition_hash: Mapped[str] = mapped_column(String(64))
    content_version: Mapped[str] = mapped_column(String(32))
    statement: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_try_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
