import uuid
from typing import Any

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint, false, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseORM

# Exactly one skin is the default (the fallback for maps without one): a
# unique index over only the rows where `is_default` is true.
UQ_SKIN_SINGLE_DEFAULT = "uq_skins_single_default"
UQ_SKIN_RULE_MODNAME = "uq_course_map_skin_rules_map_modname"


class Skin(BaseORM):
    """How a bubble looks. `config` is a validated `SkinConfig` (camelCase JSON)."""

    __tablename__ = "skins"
    __table_args__ = (
        Index(
            UQ_SKIN_SINGLE_DEFAULT,
            "is_default",
            unique=True,
            postgresql_where=text("is_default"),
        ),
    )

    name: Mapped[str] = mapped_column(String(60))
    # Copy of config["kind"], so skins can be filtered without opening the JSON.
    kind: Mapped[str] = mapped_column(String(20))
    config: Mapped[dict[str, Any]] = mapped_column(JSONB)
    # Built-in skins ship with the app and can be neither edited nor deleted.
    is_builtin: Mapped[bool] = mapped_column(default=False, server_default=false())
    is_default: Mapped[bool] = mapped_column(default=False, server_default=false())


class CourseMapSkinRule(BaseORM):
    """On this map, bubbles of Moodle activity type `modname` use this skin."""

    __tablename__ = "course_map_skin_rules"
    __table_args__ = (
        UniqueConstraint("course_map_id", "modname", name=UQ_SKIN_RULE_MODNAME),
    )

    course_map_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("course_maps.id", ondelete="CASCADE")
    )
    modname: Mapped[str] = mapped_column(String(50))
    skin_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("skins.id", ondelete="CASCADE"), index=True
    )
