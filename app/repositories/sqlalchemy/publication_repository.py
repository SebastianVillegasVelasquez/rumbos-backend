import uuid
from collections.abc import Collection
from typing import Any

from sqlalchemy import and_, delete, func, select, update
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.assets.references import audio_info_of, skin_asset_ids, stored_asset_id
from app.enums import BubbleIcon, BubbleStatus
from app.exceptions import (
    CourseMapNotFoundError,
    DraftChangedSinceReviewError,
    NoChangesToPublishError,
    PublishConflictError,
    PublishInvalidError,
    VersionConflictError,
)
from app.models import (
    Asset,
    Bubble,
    CourseMap,
    CourseMapSkinRule,
    CourseMapView3dBinding,
    Publication,
    Skin,
)
from app.models.bubble import UQ_BUBBLE_ACTIVITY
from app.models.publication import UQ_PUBLICATION_NUMBER
from app.publishing.snapshot import (
    DraftInputs,
    build_snapshot,
    collect_asset_ids,
    content_hash,
    snapshot_audio_id,
    snapshot_skin_ids,
)
from app.repositories.sqlalchemy._errors import constraint_name, sqlstate
from app.repositories.sqlalchemy.skin_repository import to_skin_read
from app.repositories.sqlalchemy.view3d_repository import to_binding
from app.schemas.asset import AssetRecord
from app.schemas.bubble import BubbleRead
from app.schemas.content import image_asset_ids
from app.schemas.course_map import SkinRule
from app.schemas.map_settings import MapSettings
from app.schemas.publication import (
    PublicationMeta,
    PublicationRecord,
    PublicationSummary,
    RestoreWarning,
)
from app.schemas.published import PublishedRow
from app.schemas.skin import SkinRead
from app.schemas.view3d import View3d
from app.view3d import scenes

SERIALIZATION_FAILURE = "40001"  # PostgreSQL SQLSTATE


class _RetryPublish(Exception):
    """A concurrent publish got in the way; the whole attempt can be redone."""


async def load_drafts(
    session: AsyncSession, course_map_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, DraftInputs]:
    """The draft of each map, for several maps in a handful of queries.

    One query each for the maps, their skin rules, their bubbles, their 3D
    bindings and the skins involved (the ones the drafts name plus the
    application's default skin): the same six whatever the number of maps, plus
    one for the tracks of the maps that have music. Maps that do not exist are
    absent from the result.
    """
    if not course_map_ids:
        return {}
    ids = list(course_map_ids)
    maps = (
        await session.scalars(
            select(CourseMap)
            .where(CourseMap.id.in_(ids))
            .execution_options(populate_existing=True)
        )
    ).all()
    rules = (
        await session.scalars(
            select(CourseMapSkinRule)
            .where(CourseMapSkinRule.course_map_id.in_(ids))
            .execution_options(populate_existing=True)
        )
    ).all()
    bubbles = (
        await session.scalars(
            select(Bubble)
            .where(Bubble.course_map_id.in_(ids))
            .execution_options(populate_existing=True)
        )
    ).all()
    bindings = (
        await session.scalars(
            select(CourseMapView3dBinding)
            .where(CourseMapView3dBinding.course_map_id.in_(ids))
            .execution_options(populate_existing=True)
        )
    ).all()
    wanted = {r.skin_id for r in rules}
    wanted.update(m.default_skin_id for m in maps if m.default_skin_id)
    wanted.update(b.skin_id for b in bubbles if b.skin_id)
    skin_rows = (
        await session.scalars(
            select(Skin)
            .where(Skin.id.in_(wanted) | Skin.is_default)
            .execution_options(populate_existing=True)
        )
    ).all()
    skins: dict[uuid.UUID, SkinRead] = {s.id: to_skin_read(s) for s in skin_rows}
    settings_of = {m.id: MapSettings.model_validate(m.settings) for m in maps}
    tracks = await _tracks(
        session, {s.audio.asset_id for s in settings_of.values() if s.audio}
    )
    audio_infos = {
        map_id: audio_info_of(tracks.get(s.audio.asset_id)) if s.audio else None
        for map_id, s in settings_of.items()
    }

    rules_by_map: dict[uuid.UUID, list[SkinRule]] = {}
    for rule in rules:
        rules_by_map.setdefault(rule.course_map_id, []).append(
            SkinRule.model_validate(rule)
        )
    bubbles_by_map: dict[uuid.UUID, list[BubbleRead]] = {}
    for bubble in bubbles:
        bubbles_by_map.setdefault(bubble.course_map_id, []).append(
            BubbleRead.model_validate(bubble)
        )
    bindings_by_map: dict[uuid.UUID, list[CourseMapView3dBinding]] = {}
    for binding in bindings:
        bindings_by_map.setdefault(binding.course_map_id, []).append(binding)
    return {
        m.id: DraftInputs(
            title=m.title,
            image_url=m.image_url,
            settings=settings_of[m.id],
            default_skin_id=m.default_skin_id,
            skin_rules=rules_by_map.get(m.id, []),
            bubbles=bubbles_by_map.get(m.id, []),
            skins=skins,
            view3d=(
                View3d(
                    scene=m.view3d_scene,
                    bindings=[
                        to_binding(b)
                        for b in sorted(
                            bindings_by_map.get(m.id, []), key=lambda b: b.zone_id
                        )
                    ],
                )
                if m.view3d_scene is not None
                else None
            ),
            audio_info=audio_infos[m.id],
        )
        for m in maps
    }


async def _tracks(
    session: AsyncSession, asset_ids: set[uuid.UUID]
) -> dict[uuid.UUID, AssetRecord]:
    """The assets the maps use as music, as the session's scope sees them (the
    tenant's own and the library's)."""
    if not asset_ids:
        return {}
    rows = await session.scalars(
        select(Asset)
        .where(Asset.id.in_(asset_ids))
        .execution_options(populate_existing=True)
    )
    return {a.id: AssetRecord.model_validate(a) for a in rows}


def _record(row: Publication) -> PublicationRecord:
    return PublicationRecord.model_validate(row)


class SqlAlchemyPublicationRepository:
    """Implements `PublicationRepository`. Writes commit; failures roll back."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- reads -----------------------------------------------------------

    async def load_draft(self, course_map_id: uuid.UUID) -> DraftInputs | None:
        drafts = await load_drafts(self._session, [course_map_id])
        return drafts.get(course_map_id)

    async def load_drafts(
        self, course_map_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, DraftInputs]:
        return await load_drafts(self._session, course_map_ids)

    async def latest(self, course_map_id: uuid.UUID) -> PublicationRecord | None:
        row = await self._session.scalar(
            select(Publication)
            .where(Publication.course_map_id == course_map_id)
            .order_by(Publication.number.desc())
            .limit(1)
            .execution_options(populate_existing=True)
        )
        return _record(row) if row else None

    async def latest_meta(
        self, course_map_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, PublicationMeta]:
        if not course_map_ids:
            return {}
        rows = await self._session.execute(
            select(
                Publication.course_map_id,
                Publication.number,
                Publication.content_hash,
                Publication.created_at,
                Publication.note,
            )
            .where(Publication.course_map_id.in_(list(course_map_ids)))
            .ext(distinct_on(Publication.course_map_id))
            .order_by(Publication.course_map_id, Publication.number.desc())
        )
        return {
            r.course_map_id: PublicationMeta(
                course_map_id=r.course_map_id,
                number=r.number,
                content_hash=r.content_hash,
                created_at=r.created_at,
                note=r.note,
            )
            for r in rows
        }

    async def get(
        self, course_map_id: uuid.UUID, number: int
    ) -> PublicationRecord | None:
        row = await self._session.scalar(
            select(Publication)
            .where(
                Publication.course_map_id == course_map_id,
                Publication.number == number,
            )
            .execution_options(populate_existing=True)
        )
        return _record(row) if row else None

    async def list_summaries(
        self, course_map_id: uuid.UUID, limit: int
    ) -> list[PublicationSummary]:
        rows = await self._session.execute(
            select(
                Publication.number,
                Publication.created_at,
                Publication.note,
                Publication.snapshot["title"].astext.label("title"),
                func.jsonb_array_length(Publication.snapshot["bubbles"]).label("n"),
            )
            .where(Publication.course_map_id == course_map_id)
            .order_by(Publication.number.desc())
            .limit(limit)
        )
        return [
            PublicationSummary(
                number=r.number,
                created_at=r.created_at,
                note=r.note,
                title=r.title,
                bubble_count=r.n,
            )
            for r in rows
        ]

    # --- what students see -----------------------------------------------

    @staticmethod
    def _latest_numbers() -> Any:
        """Subquery: each published map with the number of its latest publication."""
        return (
            select(
                Publication.course_map_id,
                func.max(Publication.number).label("latest"),
            )
            .group_by(Publication.course_map_id)
            .subquery()
        )

    def _published_select(self, latest: Any) -> Any:
        return (
            select(
                CourseMap.id,
                CourseMap.moodle_course_id,
                CourseMap.moodle_section_id,
                CourseMap.position,
                Publication.number,
                Publication.created_at,
                Publication.snapshot,
            )
            .join(latest, latest.c.course_map_id == CourseMap.id)
            .join(
                Publication,
                and_(
                    Publication.course_map_id == CourseMap.id,
                    Publication.number == latest.c.latest,
                ),
            )
        )

    @staticmethod
    def _published_row(r: Any) -> PublishedRow:
        return PublishedRow(
            course_map_id=r.id,
            moodle_course_id=r.moodle_course_id,
            moodle_section_id=r.moodle_section_id,
            position=r.position,
            number=r.number,
            published_at=r.created_at,
            snapshot=r.snapshot,
        )

    async def list_published(
        self, moodle_course_id: int | None, limit: int, offset: int
    ) -> tuple[list[PublishedRow], int]:
        latest = self._latest_numbers()
        filters = (
            [CourseMap.moodle_course_id == moodle_course_id]
            if moodle_course_id is not None
            else []
        )
        total = await self._session.scalar(
            select(func.count())
            .select_from(CourseMap)
            .join(latest, latest.c.course_map_id == CourseMap.id)
            .where(*filters)
        )
        rows = await self._session.execute(
            self._published_select(latest)
            .where(*filters)
            .order_by(CourseMap.position, CourseMap.created_at, CourseMap.id)
            .limit(limit)
            .offset(offset)
        )
        return [self._published_row(r) for r in rows], total or 0

    async def get_published(self, course_map_id: uuid.UUID) -> PublishedRow | None:
        latest = self._latest_numbers()
        row = (
            await self._session.execute(
                self._published_select(latest).where(CourseMap.id == course_map_id)
            )
        ).first()
        return self._published_row(row) if row else None

    async def bubble_statuses(
        self, bubble_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, BubbleStatus]:
        if not bubble_ids:
            return {}
        rows = await self._session.execute(
            select(Bubble.id, Bubble.status).where(Bubble.id.in_(list(bubble_ids)))
        )
        return {r.id: r.status for r in rows}

    # --- publish ---------------------------------------------------------

    async def publish(
        self,
        course_map_id: uuid.UUID,
        expected_hash: str | None,
        note: str | None,
        keep: int,
    ) -> PublicationSummary:
        # Two attempts: a concurrent publish of the same map may make the first
        # fail on serialization or on the unique number; the second then sees
        # the winner's work and answers accordingly (usually "no changes").
        for _ in range(2):
            try:
                return await self._publish_once(
                    course_map_id, expected_hash, note, keep
                )
            except _RetryPublish:
                continue
        raise PublishConflictError

    async def _publish_once(
        self,
        course_map_id: uuid.UUID,
        expected_hash: str | None,
        note: str | None,
        keep: int,
    ) -> PublicationSummary:
        if self._session.in_transaction():
            await self._session.rollback()  # the isolation level must come first
        try:
            # REPEATABLE READ: every read below sees one consistent snapshot of
            # the database, so the draft cannot change halfway through.
            await self._session.connection(
                execution_options={"isolation_level": "REPEATABLE READ"}
            )
            inputs = await load_drafts(self._session, [course_map_id])
            draft = inputs.get(course_map_id)
            if draft is None:
                raise CourseMapNotFoundError(course_map_id)
            snapshot = build_snapshot(draft)
            digest = content_hash(snapshot)
            if expected_hash is not None and expected_hash != digest:
                raise DraftChangedSinceReviewError(digest)
            latest = (
                await self._session.execute(
                    select(Publication.number, Publication.content_hash)
                    .where(Publication.course_map_id == course_map_id)
                    .order_by(Publication.number.desc())
                    .limit(1)
                )
            ).first()
            if latest is not None and latest.content_hash == digest:
                raise NoChangesToPublishError
            await self._validate(snapshot)

            number = (latest.number if latest else 0) + 1
            row = Publication(
                course_map_id=course_map_id,
                number=number,
                snapshot=snapshot,
                content_hash=digest,
                asset_ids=collect_asset_ids(snapshot),
                note=note,
            )
            self._session.add(row)
            await self._session.flush()
            created_at = row.created_at  # read before the commit can expire it
            await self._session.execute(
                delete(Publication).where(
                    Publication.course_map_id == course_map_id,
                    Publication.number <= number - keep,
                )
            )
            await self._session.commit()
        except DBAPIError as exc:
            await self._session.rollback()
            if sqlstate(exc) == SERIALIZATION_FAILURE or (
                isinstance(exc, IntegrityError)
                and constraint_name(exc) == UQ_PUBLICATION_NUMBER
            ):
                raise _RetryPublish from exc
            raise
        except BaseException:
            await self._session.rollback()
            raise
        return PublicationSummary(
            number=number,
            created_at=created_at,
            note=note,
            title=snapshot["title"],
            bubble_count=len(snapshot["bubbles"]),
        )

    async def _validate(self, snapshot: dict[str, Any]) -> None:
        """Everything the snapshot points at must still exist. Raises
        `PublishInvalidError` listing EVERY problem, not just the first."""
        problems: list[tuple[str, str]] = []
        asset_ids = collect_asset_ids(snapshot)
        existing_assets = set(
            await self._session.scalars(select(Asset.id).where(Asset.id.in_(asset_ids)))
        )
        background = stored_asset_id(snapshot["imageUrl"])
        if background is not None and background not in existing_assets:
            problems.append(
                (
                    "background_asset_missing",
                    f"The background image {background} no longer exists",
                )
            )
        for skin_id, skin in snapshot["skins"].items():
            for asset_id in sorted(skin_asset_ids(skin["config"]), key=str):
                if asset_id not in existing_assets:
                    problems.append(
                        (
                            "skin_asset_missing",
                            (
                                f"Skin '{skin['name']}' ({skin_id}) uses the image "
                                f"{asset_id}, which no longer exists"
                            ),
                        )
                    )
        music = snapshot_audio_id(snapshot)
        if music is not None and music not in existing_assets:
            problems.append(
                ("audio_asset_missing", f"The music {music} no longer exists")
            )
        view3d = snapshot.get("view3d")
        if view3d is not None and scenes.zone_count(view3d["scene"]) is None:
            problems.append(
                (
                    "scene_unknown",
                    f"The 3D scene '{view3d['scene']}' is not available any more",
                )
            )
        for bubble in snapshot["bubbles"]:
            for asset_id in image_asset_ids(bubble.get("content")):
                if asset_id not in existing_assets:
                    problems.append(
                        (
                            "content_asset_missing",
                            (
                                f"The content bubble {bubble['id']} shows the image "
                                f"{asset_id}, which no longer exists"
                            ),
                        )
                    )
        known = {uuid.UUID(k) for k in snapshot["skins"]}
        for skin_id in sorted(snapshot_skin_ids(snapshot) - known, key=str):
            problems.append(("skin_missing", f"The skin {skin_id} no longer exists"))
        if problems:
            raise PublishInvalidError(problems)

    # --- discard / restore -------------------------------------------------

    async def restore_into_draft(
        self,
        course_map_id: uuid.UUID,
        snapshot: dict[str, Any],
        expected_revision: int,
    ) -> list[RestoreWarning] | None:
        """Loads `snapshot` into the draft, in ONE transaction. None if the map
        does not exist; `VersionConflictError` if `expected_revision` is stale;
        `PublishInvalidError` (nothing changed) if the background is gone.

        The map's title, image, settings, default skin and rules are replaced;
        bubbles are updated by id, recreated with their original ids when they
        were deleted, and deleted when the snapshot does not have them; `sequence`
        comes from the snapshot. Music that was deleted or taken down is dropped
        with an `audio_unavailable` warning. Skins that no longer exist are reset (with a
        `skin_missing` warning). The 3D scene and bindings are recreated from the
        snapshot once the bubbles are back (a scene that is no longer in the
        registry is dropped, with a `scene_unknown` warning). Bumps the map's revision, and the version of
        every bubble that changed.
        """
        warnings: list[RestoreWarning] = []
        try:
            # The guarded UPDATE comes first: it checks the revision and takes
            # the map's row lock, so nobody else edits it while we rewrite it.
            row = (
                await self._session.execute(
                    update(CourseMap)
                    .where(
                        CourseMap.id == course_map_id,
                        CourseMap.revision == expected_revision,
                    )
                    .values(revision=CourseMap.revision + 1)
                    .returning(CourseMap.moodle_course_id)
                    .execution_options(synchronize_session=False)
                )
            ).first()
            if row is None:
                current = await self._session.scalar(
                    select(CourseMap.revision).where(CourseMap.id == course_map_id)
                )
                await self._session.rollback()
                if current is None:
                    return None
                raise VersionConflictError(current_revision=current)
            moodle_course_id: int = row.moodle_course_id

            background = stored_asset_id(snapshot["imageUrl"])
            if background is not None and not await self._session.scalar(
                select(Asset.id).where(Asset.id == background)
            ):
                raise PublishInvalidError(
                    [
                        (
                            "background_asset_missing",
                            f"The background image {background} no longer exists",
                        )
                    ]
                )

            existing_skins = set(
                await self._session.scalars(
                    select(Skin.id).where(Skin.id.in_(snapshot_skin_ids(snapshot)))
                )
            )

            settings = MapSettings.model_validate(snapshot["settings"])
            music = snapshot_audio_id(snapshot)
            if music is not None:
                track = await self._session.scalar(
                    select(Asset).where(Asset.id == music)
                )
                if (
                    audio_info_of(AssetRecord.model_validate(track) if track else None)
                    is None
                ):
                    settings = settings.model_copy(update={"audio": None})
                    warnings.append(
                        RestoreWarning(
                            code="audio_unavailable",
                            message=(
                                f"The music {music} is no longer available, so "
                                "the map was restored without music"
                            ),
                        )
                    )

            def skin_or_none(raw: str | None, where: str) -> uuid.UUID | None:
                if raw is None:
                    return None
                skin_id = uuid.UUID(raw)
                if skin_id in existing_skins:
                    return skin_id
                warnings.append(
                    RestoreWarning(
                        code="skin_missing",
                        message=(
                            f"The skin {skin_id} no longer exists; {where} "
                            "was reset to none"
                        ),
                    )
                )
                return None

            await self._session.execute(
                update(CourseMap)
                .where(CourseMap.id == course_map_id)
                .values(
                    title=snapshot["title"],
                    image_url=snapshot["imageUrl"],
                    settings=settings.to_stored(),
                    default_skin_id=skin_or_none(
                        snapshot["defaultSkinId"], "the map's default skin"
                    ),
                )
                .execution_options(synchronize_session=False)
            )

            await self._session.execute(
                delete(CourseMapSkinRule).where(
                    CourseMapSkinRule.course_map_id == course_map_id
                )
            )
            for rule in snapshot["skinRules"]:
                skin_id = skin_or_none(
                    rule["skinId"], f"the rule for '{rule['modname']}'"
                )
                if skin_id is not None:
                    self._session.add(
                        CourseMapSkinRule(
                            course_map_id=course_map_id,
                            modname=rule["modname"],
                            skin_id=skin_id,
                        )
                    )

            await self._restore_bubbles(
                course_map_id, moodle_course_id, snapshot, skin_or_none, warnings
            )
            await self._restore_view3d(course_map_id, snapshot, warnings)
            await self._session.commit()
            # Core UPDATEs bypass the identity map; forget what it holds so the
            # next read of this map or its bubbles cannot return stale objects.
            self._session.expunge_all()
        except IntegrityError as exc:
            await self._session.rollback()
            if constraint_name(exc) == UQ_BUBBLE_ACTIVITY:
                # Another editor placed one of the activities while we were
                # restoring: the map moved on, which is what a stale revision is.
                current = await self._session.scalar(
                    select(CourseMap.revision).where(CourseMap.id == course_map_id)
                )
                await self._session.rollback()
                if current is not None:
                    raise VersionConflictError(current_revision=current) from exc
            raise
        except BaseException:
            await self._session.rollback()
            raise
        return warnings

    async def _restore_view3d(
        self,
        course_map_id: uuid.UUID,
        snapshot: dict[str, Any],
        warnings: list[RestoreWarning],
    ) -> None:
        """The snapshot's 3D scene and bindings replace the draft's. Runs after
        the bubbles are restored, so their ids exist again. A binding whose
        bubble could not be brought back (that case already has its own
        warning), or whose zone is no longer in the scene, is left out."""
        view3d = snapshot.get("view3d")
        scene: str | None = view3d["scene"] if view3d is not None else None
        zones = scenes.zone_count(scene) if scene is not None else None
        if scene is not None and zones is None:
            warnings.append(
                RestoreWarning(
                    code="scene_unknown",
                    message=(
                        f"The 3D scene '{scene}' is not available any more, so "
                        "the map's 3D view was removed"
                    ),
                )
            )
            scene = None
        await self._session.execute(
            update(CourseMap)
            .where(CourseMap.id == course_map_id)
            .values(view3d_scene=scene)
            .execution_options(synchronize_session=False)
        )
        await self._session.execute(
            delete(CourseMapView3dBinding).where(
                CourseMapView3dBinding.course_map_id == course_map_id
            )
        )
        if scene is None or zones is None:
            return
        assert view3d is not None  # a scene came from it
        present = set(
            await self._session.scalars(
                select(Bubble.id).where(Bubble.course_map_id == course_map_id)
            )
        )
        self._session.add_all(
            CourseMapView3dBinding(
                course_map_id=course_map_id,
                zone_id=b["zoneId"],
                bubble_id=uuid.UUID(b["bubbleId"]),
                title=b["title"],
                description=b["description"],
            )
            for b in view3d["bindings"]
            if uuid.UUID(b["bubbleId"]) in present and 0 <= b["zoneId"] < zones
        )
        await self._session.flush()

    async def _restore_bubbles(
        self,
        course_map_id: uuid.UUID,
        moodle_course_id: int,
        snapshot: dict[str, Any],
        skin_or_none: Any,
        warnings: list[RestoreWarning],
    ) -> None:
        wanted = {uuid.UUID(b["id"]): b for b in snapshot["bubbles"]}
        existing = {
            b.id: b
            for b in (
                await self._session.scalars(
                    select(Bubble)
                    .where(Bubble.course_map_id == course_map_id)
                    .execution_options(populate_existing=True)
                )
            )
        }
        # 1. Bubbles the snapshot does not have go first, so their activities
        #    are free for the ones being brought back.
        gone = [i for i in existing if i not in wanted]
        if gone:
            await self._session.execute(
                delete(Bubble)
                .where(Bubble.id.in_(gone))
                .execution_options(synchronize_session=False)
            )
        # 2. Bubbles that exist: bring back position, look and place on the path.
        for bubble_id, data in wanted.items():
            current = existing.get(bubble_id)
            if current is None:
                continue
            values = self._bubble_values(data, skin_or_none, bubble_id)
            if any(getattr(current, k) != v for k, v in values.items()):
                await self._session.execute(
                    update(Bubble)
                    .where(Bubble.id == bubble_id)
                    .values(**values, version=Bubble.version + 1)
                    .execution_options(synchronize_session=False)
                )
        # 3. Bubbles that were deleted: recreate them with their original ids,
        #    unless their activity has since been placed somewhere else.
        missing = {i: d for i, d in wanted.items() if i not in existing}
        if not missing:
            return
        taken = set(
            await self._session.scalars(
                select(Bubble.activity_id).where(
                    Bubble.moodle_course_id == moodle_course_id,
                    Bubble.activity_id.in_([d["activityId"] for d in missing.values()]),
                )
            )
        )
        for bubble_id, data in missing.items():
            if data["activityId"] in taken:
                warnings.append(
                    RestoreWarning(
                        code="bubble_not_restored",
                        message=(
                            f"Activity {data['activityId']} is already on another "
                            "map of the course, so its bubble was not brought back"
                        ),
                    )
                )
                continue
            self._session.add(
                Bubble(
                    id=bubble_id,
                    course_map_id=course_map_id,
                    moodle_course_id=moodle_course_id,
                    activity_id=data["activityId"],
                    status=BubbleStatus.LOCKED,
                    **self._bubble_values(data, skin_or_none, bubble_id),
                )
            )
        await self._session.flush()

    @staticmethod
    def _bubble_values(
        data: dict[str, Any], skin_or_none: Any, bubble_id: uuid.UUID
    ) -> dict[str, Any]:
        return {
            "x": data["x"],
            "y": data["y"],
            "icon": BubbleIcon(data["icon"]) if data["icon"] else None,
            "skin_id": skin_or_none(data["skinId"], f"the skin of bubble {bubble_id}"),
            "sequence": data["sequence"],
        }
