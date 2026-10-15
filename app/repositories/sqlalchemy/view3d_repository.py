import uuid

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import VersionConflictError, View3dInvalidError
from app.models import CourseMap, CourseMapView3dBinding
from app.models.view3d import UQ_VIEW3D_BUBBLE, UQ_VIEW3D_ZONE
from app.repositories.sqlalchemy._errors import (
    FOREIGN_KEY_VIOLATION,
    constraint_name,
    sqlstate,
)
from app.schemas.view3d import LocalizedText, View3d, View3dBinding


def stored_text(text: LocalizedText | None) -> dict[str, str] | None:
    """What goes in the JSONB column: only the languages given, or NULL."""
    if text is None:
        return None
    return text.model_dump(mode="json") or None


def to_binding(row: CourseMapView3dBinding) -> View3dBinding:
    return View3dBinding(
        zone_id=row.zone_id,
        bubble_id=row.bubble_id,
        title=LocalizedText.model_validate(row.title) if row.title else None,
        description=(
            LocalizedText.model_validate(row.description) if row.description else None
        ),
    )


class SqlAlchemyView3dRepository:
    """Implements `View3dRepository`. Writes commit; failures roll back."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, course_map_id: uuid.UUID) -> View3d | None:
        scene = await self._session.scalar(
            select(CourseMap.view3d_scene).where(CourseMap.id == course_map_id)
        )
        if scene is None:
            return None
        rows = await self._session.scalars(
            select(CourseMapView3dBinding)
            .where(CourseMapView3dBinding.course_map_id == course_map_id)
            .order_by(CourseMapView3dBinding.zone_id)
            .execution_options(populate_existing=True)
        )
        return View3d(scene=scene, bindings=[to_binding(r) for r in rows])

    async def replace(
        self,
        course_map_id: uuid.UUID,
        view3d: View3d | None,
        expected_revision: int,
    ) -> bool:
        # One transaction: the guarded UPDATE (which also takes the map's row
        # lock and bumps the revision), the removal of the old bindings and the
        # new ones. Any failure rolls all of it back.
        try:
            updated = await self._session.execute(
                update(CourseMap)
                .where(
                    CourseMap.id == course_map_id,
                    CourseMap.revision == expected_revision,
                )
                .values(
                    view3d_scene=view3d.scene if view3d else None,
                    revision=CourseMap.revision + 1,
                )
                .returning(CourseMap.id)
                .execution_options(synchronize_session="fetch")
            )
            if updated.scalar_one_or_none() is None:
                current = await self._session.scalar(
                    select(CourseMap.revision).where(CourseMap.id == course_map_id)
                )
                await self._session.rollback()
                if current is not None:
                    raise VersionConflictError(current_revision=current)
                return False
            await self._session.execute(
                delete(CourseMapView3dBinding).where(
                    CourseMapView3dBinding.course_map_id == course_map_id
                )
            )
            if view3d is not None:
                self._session.add_all(
                    CourseMapView3dBinding(
                        course_map_id=course_map_id,
                        zone_id=b.zone_id,
                        bubble_id=b.bubble_id,
                        title=stored_text(b.title),
                        description=stored_text(b.description),
                    )
                    for b in view3d.bindings
                )
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            name = constraint_name(exc)
            if name == UQ_VIEW3D_ZONE:
                raise View3dInvalidError(
                    "duplicate_zone", "A zone can have only one bubble"
                ) from exc
            if name == UQ_VIEW3D_BUBBLE:
                raise View3dInvalidError(
                    "duplicate_bubble", "A bubble can be bound to only one zone"
                ) from exc
            if sqlstate(exc) == FOREIGN_KEY_VIOLATION:
                # A bubble was deleted after the service looked at the map.
                raise View3dInvalidError(
                    "bubble_not_in_map", "A bound bubble does not belong to this map"
                ) from exc
            raise
        except BaseException:
            await self._session.rollback()
            raise
        return True
