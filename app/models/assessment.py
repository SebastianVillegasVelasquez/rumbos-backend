import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    false,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseORM

UQ_ATTEMPT_ORDINAL = "uq_assessment_attempts_user_purpose_ordinal"
UQ_ATTEMPT_OPEN = "uq_assessment_attempts_one_open"
UQ_ANSWER_ITEM = "uq_assessment_answers_attempt_item"


class AssessmentAttempt(BaseORM):
    """One try at an exam (check-in or final) by one student of one course.

    The server clock is the only clock: `deadline_at = started_at + limit`, and
    an attempt past it is closed as `expired` with whatever was saved. `seed`
    fixes the shuffle, so a reload shows the same order. `policy` freezes the
    rules in force when it started (and, once closed, `result` the outcome), so
    a later change of settings never rewrites history. `preview` rows belong to
    editors: ephemeral, purged within a day, never reported or synced.
    """

    __tablename__ = "assessment_attempts"
    __table_args__ = (
        # Attempt numbers grow per (user, purpose): two tabs racing to start
        # the same one cannot both win. Purged previews leave holes.
        UniqueConstraint(
            "tenant_id",
            "moodle_course_id",
            "sub",
            "purpose",
            "preview",
            "ordinal",
            name=UQ_ATTEMPT_ORDINAL,
        ),
        # At most one attempt in progress per (user, purpose).
        Index(
            UQ_ATTEMPT_OPEN,
            "tenant_id",
            "moodle_course_id",
            "sub",
            "purpose",
            "preview",
            unique=True,
            postgresql_where=text("status = 'in_progress'"),
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    moodle_course_id: Mapped[int] = mapped_column(Integer)
    # The user reference of the launch assertion; no personal data beyond it.
    sub: Mapped[str] = mapped_column(String(128))
    purpose: Mapped[str] = mapped_column(String(20))
    ordinal: Mapped[int] = mapped_column(Integer)
    form_id: Mapped[str] = mapped_column(String(80))
    content_version: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(20), default="in_progress")
    preview: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    seed: Mapped[str] = mapped_column(String(32))
    policy: Mapped[dict[str, Any]] = mapped_column(JSONB)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class AssessmentAnswer(BaseORM):
    """The latest saved response to one item; last write wins."""

    __tablename__ = "assessment_answers"
    __table_args__ = (UniqueConstraint("attempt_id", "item_id", name=UQ_ANSWER_ITEM),)

    attempt_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assessment_attempts.id", ondelete="CASCADE"), index=True
    )
    item_id: Mapped[str] = mapped_column(String(80))
    response: Mapped[dict[str, Any]] = mapped_column(JSONB)
    saved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
