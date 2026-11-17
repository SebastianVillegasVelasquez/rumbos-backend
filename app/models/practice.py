import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseORM

UQ_PRACTICE_ANSWER = "uq_practice_answers_session_attempt"
UQ_SAVED_WORD = "uq_saved_words_user_entry"
UQ_PRACTICE_OPEN = "uq_practice_sessions_one_open"


class PracticeSession(BaseORM):
    """One sitting of one practice activity by one student. `state` is where
    a dialogue is (`{"node": id}`); `summary` is written when it finishes."""

    __tablename__ = "practice_sessions"
    __table_args__ = (
        Index(
            UQ_PRACTICE_OPEN,
            "tenant_id",
            "moodle_course_id",
            "sub",
            "activity_id",
            unique=True,
            postgresql_where=text("status = 'in_progress'"),
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    moodle_course_id: Mapped[int] = mapped_column(Integer)
    sub: Mapped[str] = mapped_column(String(128))
    activity_id: Mapped[str] = mapped_column(String(80))
    content_version: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(20), default="in_progress")
    seed: Mapped[str] = mapped_column(String(32))
    state: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class PracticeAnswer(BaseORM):
    """One answer to one step. `attempt_id` is chosen by the client, so a
    retried request is answered again, never counted twice."""

    __tablename__ = "practice_answers"
    __table_args__ = (
        UniqueConstraint("session_id", "attempt_id", name=UQ_PRACTICE_ANSWER),
    )

    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("practice_sessions.id", ondelete="CASCADE"), index=True
    )
    step_id: Mapped[str] = mapped_column(String(80))
    attempt_id: Mapped[uuid.UUID] = mapped_column()
    outcome: Mapped[str] = mapped_column(String(20))
    earned: Mapped[int] = mapped_column(Integer)
    total: Mapped[int] = mapped_column(Integer)
    # Only the kind of mistake is kept (for the review), never the typed text.
    error_tag: Mapped[str | None] = mapped_column(String(60))
    answered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SavedWord(BaseORM):
    """A word the student keeps, in a Leitner box 1 to 5."""

    __tablename__ = "saved_words"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "moodle_course_id", "sub", "entry_id", name=UQ_SAVED_WORD
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    moodle_course_id: Mapped[int] = mapped_column(Integer)
    sub: Mapped[str] = mapped_column(String(128))
    entry_id: Mapped[str] = mapped_column(String(80))
    box: Mapped[int] = mapped_column(Integer, default=1)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reviews: Mapped[int] = mapped_column(Integer, default=0)


class PracticeReport(BaseORM):
    """A note from a student that an activity or step looks wrong."""

    __tablename__ = "practice_reports"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    moodle_course_id: Mapped[int] = mapped_column(Integer)
    sub: Mapped[str] = mapped_column(String(128))
    activity_id: Mapped[str] = mapped_column(String(80))
    step_id: Mapped[str | None] = mapped_column(String(80))
    content_version: Mapped[str] = mapped_column(String(32))
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="new")
