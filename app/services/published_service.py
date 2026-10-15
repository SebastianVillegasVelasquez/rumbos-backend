import uuid
from typing import Any

from app.enums import BubbleIcon, BubbleStatus
from app.exceptions import NotPublishedError
from app.repositories.protocols import PublicationRepository
from app.schemas.course_map import SkinRule
from app.schemas.map_settings import MapSettings
from app.schemas.published import (
    PublishedBubble,
    PublishedMapList,
    PublishedMapRead,
    PublishedMapSummary,
    PublishedRow,
    PublishedSkin,
)
from app.schemas.resolved import ResolvedMap
from app.schemas.view3d import View3d
from app.services.resolution_service import MapResolutionService


def _bubble_ids(row: PublishedRow) -> list[uuid.UUID]:
    return [uuid.UUID(b["id"]) for b in row.snapshot["bubbles"]]


class PublishedMapService:
    """The student side: the latest publication of each map, joined with live data.

    The content is the snapshot; `position`, `moodleSectionId` and bubble
    `status` are live. A bubble that no longer exists in the draft reads as
    `no_complete` (it was published, so it is shown, and it is not done).
    """

    def __init__(
        self, publications: PublicationRepository, resolver: MapResolutionService
    ) -> None:
        self._publications = publications
        self._resolver = resolver

    async def list_maps(
        self, *, moodle_course_id: int | None, limit: int, offset: int
    ) -> PublishedMapList:
        rows, total = await self._publications.list_published(
            moodle_course_id, limit, offset
        )
        statuses = await self._publications.bubble_statuses(
            [i for row in rows for i in _bubble_ids(row)]
        )
        items = []
        for row in rows:
            ids = _bubble_ids(row)
            items.append(
                PublishedMapSummary(
                    id=row.course_map_id,
                    title=row.snapshot["title"],
                    moodle_course_id=row.moodle_course_id,
                    moodle_section_id=row.moodle_section_id,
                    position=row.position,
                    image_url=row.snapshot["imageUrl"],
                    bubble_count=len(ids),
                    complete_count=sum(
                        1 for i in ids if statuses.get(i) is BubbleStatus.COMPLETE
                    ),
                    publication_number=row.number,
                    published_at=row.published_at,
                )
            )
        return PublishedMapList(items=items, total=total, limit=limit, offset=offset)

    async def get_map(self, course_map_id: uuid.UUID) -> PublishedMapRead:
        row = await self._require(course_map_id)
        statuses = await self._publications.bubble_statuses(_bubble_ids(row))
        snap: dict[str, Any] = row.snapshot
        return PublishedMapRead(
            id=row.course_map_id,
            title=snap["title"],
            moodle_course_id=row.moodle_course_id,
            moodle_section_id=row.moodle_section_id,
            position=row.position,
            image_url=snap["imageUrl"],
            settings=MapSettings.model_validate(snap["settings"]),
            default_skin_id=snap["defaultSkinId"],
            skin_rules=[
                SkinRule(modname=r["modname"], skin_id=r["skinId"])
                for r in snap["skinRules"]
            ],
            skins={
                skin_id: PublishedSkin(
                    name=skin["name"],
                    is_default=skin["isDefault"],
                    config=skin["config"],
                )
                for skin_id, skin in snap["skins"].items()
            },
            bubbles=[
                PublishedBubble(
                    id=b["id"],
                    activity_id=b["activityId"],
                    x=b["x"],
                    y=b["y"],
                    icon=BubbleIcon(b["icon"]) if b["icon"] else None,
                    skin_id=b["skinId"],
                    sequence=b["sequence"],
                    status=statuses.get(uuid.UUID(b["id"]), BubbleStatus.NO_COMPLETE),
                )
                for b in snap["bubbles"]
            ],
            # Snapshots published before 3D views existed have no such key.
            view3d=(
                View3d.model_validate(snap["view3d"]) if "view3d" in snap else None
            ),
            publication_number=row.number,
            published_at=row.published_at,
        )

    async def resolve(
        self, course_map_id: uuid.UUID, *, include_hidden: bool = False
    ) -> ResolvedMap:
        """The published bubbles resolved against Moodle, by the same resolver
        (and so the same visibility rules and cache) as the editor's."""
        row = await self._require(course_map_id)
        return await self._resolver.resolve_activities(
            row.moodle_course_id,
            [(uuid.UUID(b["id"]), b["activityId"]) for b in row.snapshot["bubbles"]],
            include_hidden=include_hidden,
        )

    async def _require(self, course_map_id: uuid.UUID) -> PublishedRow:
        row = await self._publications.get_published(course_map_id)
        if row is None:
            raise NotPublishedError(course_map_id)
        return row
