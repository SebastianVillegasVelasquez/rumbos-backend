import uuid
from collections.abc import Collection

from app.enums import PublicationStatus
from app.exceptions import (
    CourseMapNotFoundError,
    NeverPublishedError,
    PublicationNotFoundError,
)
from app.publishing.snapshot import (
    Changes,
    build_snapshot,
    content_hash,
    diff_snapshots,
)
from app.repositories.protocols import (
    BubbleRepository,
    CourseMapRepository,
    PublicationRepository,
    View3dRepository,
)
from app.schemas.course_map import CourseMapRead, PublicationInfo
from app.schemas.publication import (
    PublicationChanges,
    PublicationList,
    PublicationState,
    PublicationSummary,
    PublishRequest,
    RestoreResult,
)

MAX_PUBLICATIONS_LISTED = 100


async def publication_infos(
    publications: PublicationRepository, course_map_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, PublicationInfo]:
    """Where each map stands against its latest publication, for a whole page.

    The latest publications of all the maps, then the drafts of those that have
    one, then the hashes computed here: a fixed handful of queries however many
    maps there are. Maps never published need no draft at all.
    """
    metas = await publications.latest_meta(course_map_ids)
    drafts = await publications.load_drafts(list(metas))
    infos: dict[uuid.UUID, PublicationInfo] = {}
    for map_id in course_map_ids:
        meta = metas.get(map_id)
        if meta is None:
            infos[map_id] = PublicationInfo()
            continue
        draft = drafts.get(map_id)
        current = draft is not None and (
            content_hash(build_snapshot(draft)) == meta.content_hash
        )
        infos[map_id] = PublicationInfo(
            status=(
                PublicationStatus.UP_TO_DATE
                if current
                else PublicationStatus.UNPUBLISHED_CHANGES
            ),
            number=meta.number,
            published_at=meta.created_at,
        )
    return infos


def _changes(changes: Changes) -> PublicationChanges:
    return PublicationChanges(
        background_changed=changes.background_changed,
        title_changed=changes.title_changed,
        settings_changed=changes.settings_changed,
        skins_changed=changes.skins_changed,
        bubbles_added=changes.bubbles_added,
        bubbles_removed=changes.bubbles_removed,
        bubbles_moved=changes.bubbles_moved,
        bubbles_restyled=changes.bubbles_restyled,
        bubbles_reordered=changes.bubbles_reordered,
    )


class PublicationService:
    """Publishes a map's draft, compares it with what students see, and brings
    an earlier publication back into the draft.

    The draft is the normalized rows every editor endpoint already uses; a
    publication is an immutable snapshot of it. There is no authentication, so
    none of this is access control: it keeps students' view consistent.
    """

    def __init__(
        self,
        publications: PublicationRepository,
        course_maps: CourseMapRepository,
        bubbles: BubbleRepository,
        view3d: View3dRepository,
        keep: int,
    ) -> None:
        self._publications = publications
        self._course_maps = course_maps
        self._bubbles = bubbles
        self._view3d = view3d
        self._keep = keep

    async def state(self, course_map_id: uuid.UUID) -> PublicationState:
        draft = await self._publications.load_draft(course_map_id)
        if draft is None:
            raise CourseMapNotFoundError(course_map_id)
        snapshot = build_snapshot(draft)
        digest = content_hash(snapshot)
        latest = await self._publications.latest(course_map_id)
        if latest is None:
            return PublicationState(
                status=PublicationStatus.NEVER_PUBLISHED,
                current_number=None,
                current_published_at=None,
                current_note=None,
                draft_hash=digest,
                changes=None,
            )
        return PublicationState(
            status=(
                PublicationStatus.UP_TO_DATE
                if digest == latest.content_hash
                else PublicationStatus.UNPUBLISHED_CHANGES
            ),
            current_number=latest.number,
            current_published_at=latest.created_at,
            current_note=latest.note,
            draft_hash=digest,
            changes=_changes(diff_snapshots(latest.snapshot, snapshot)),
        )

    async def publish(
        self, course_map_id: uuid.UUID, data: PublishRequest
    ) -> PublicationSummary:
        return await self._publications.publish(
            course_map_id, data.draft_hash, data.note, self._keep
        )

    async def list_publications(
        self, course_map_id: uuid.UUID, limit: int
    ) -> PublicationList:
        if await self._course_maps.get_by_id(course_map_id) is None:
            raise CourseMapNotFoundError(course_map_id)
        return PublicationList(
            items=await self._publications.list_summaries(course_map_id, limit)
        )

    async def discard_changes(
        self, course_map_id: uuid.UUID, expected_revision: int
    ) -> RestoreResult:
        """Puts the latest publication back into the draft (409 `never_published`
        if there is none)."""
        if await self._course_maps.get_by_id(course_map_id) is None:
            raise CourseMapNotFoundError(course_map_id)
        latest = await self._publications.latest(course_map_id)
        if latest is None:
            raise NeverPublishedError
        return await self._load(course_map_id, latest.snapshot, expected_revision)

    async def restore(
        self, course_map_id: uuid.UUID, number: int, expected_revision: int
    ) -> RestoreResult:
        """Loads publication `number` into the draft. It does NOT publish."""
        if await self._course_maps.get_by_id(course_map_id) is None:
            raise CourseMapNotFoundError(course_map_id)
        record = await self._publications.get(course_map_id, number)
        if record is None:
            raise PublicationNotFoundError(number)
        return await self._load(course_map_id, record.snapshot, expected_revision)

    async def _load(
        self,
        course_map_id: uuid.UUID,
        snapshot: dict[str, object],
        expected_revision: int,
    ) -> RestoreResult:
        warnings = await self._publications.restore_into_draft(
            course_map_id, snapshot, expected_revision
        )
        if warnings is None:  # deleted between the lookup and the write
            raise CourseMapNotFoundError(course_map_id)
        course_map = await self._course_maps.get_by_id(course_map_id)
        if course_map is None:
            raise CourseMapNotFoundError(course_map_id)
        return RestoreResult(
            map=CourseMapRead(
                **course_map.model_dump(),
                skin_rules=await self._course_maps.list_skin_rules(course_map_id),
                bubbles=await self._bubbles.list_by_course_map(course_map_id),
                view3d=await self._view3d.get(course_map_id),
            ),
            warnings=warnings,
        )
