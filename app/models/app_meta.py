from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseORM


class AppMeta(BaseORM):
    """Small facts about this installation, by key (`storage_id`, ...)."""

    __tablename__ = "app_meta"

    key: Mapped[str] = mapped_column(String(64), unique=True)
    value: Mapped[str] = mapped_column(Text)
