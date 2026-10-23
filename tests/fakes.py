"""In-memory test doubles for the repository and Moodle client Protocols.

No inheritance from the Protocols: they satisfy them structurally, which
mypy checks wherever a fake is passed to `CourseMapService`.
"""

import asyncio
import uuid
from collections.abc import Collection
from datetime import UTC, datetime
from typing import Any, Never

from uuid_utils.compat import uuid7

from app.enums import AssetKind, AssetScope, BubbleStatus, KeyStatus, TenantStatus
from app.exceptions import (
    ActivityAlreadyPlacedError,
    CourseMapNotFoundError,
    LaunchReplayedError,
    SectionAlreadyMappedError,
    TenantIssuerTakenError,
    TenantKeyTakenError,
    TenantNotFoundError,
    VersionConflictError,
)
from app.moodle.exceptions import MoodleCourseNotFoundError
from app.moodle.schemas import MoodleSection, MoodleSiteInfo
from app.publishing.snapshot import DraftInputs
from app.schemas.asset import AssetAuditEntry, AssetRecord, AssetUpdate, AssetUsage
from app.schemas.auth import NewAccessToken, TokenGrant
from app.schemas.bubble import BubbleCreate, BubbleRead, BubbleUpdate
from app.schemas.course_map import (
    CourseMapBase,
    CourseMapCore,
    CourseMapCreate,
    CourseMapSummary,
    CourseMapUpdate,
    SkinRule,
)
from app.schemas.map_settings import MapSettings
from app.schemas.publication import (
    PublicationMeta,
    PublicationRecord,
    PublicationSummary,
    RestoreWarning,
)
from app.schemas.published import PublishedRow
from app.schemas.skin import ImageSkin, ProceduralSkin, SkinRead
from app.schemas.tenant import NewTenantKey, TenantKeyRecord, TenantRecord
from app.schemas.view3d import View3d


class InMemoryCourseMapRepository:
    def __init__(self) -> None:
        self.items: dict[uuid.UUID, CourseMapBase] = {}
        self.bubble_counts: dict[uuid.UUID, int] = {}
        self.complete_counts: dict[uuid.UUID, int] = {}
        self.rules: dict[uuid.UUID, list[SkinRule]] = {}
        self.appearance_writes = 0

    async def get_by_id(self, course_map_id: uuid.UUID) -> CourseMapBase | None:
        return self.items.get(course_map_id)

    def _level_order(self, moodle_course_id: int) -> list[CourseMapBase]:
        maps = [
            m for m in self.items.values() if m.moodle_course_id == moodle_course_id
        ]
        return sorted(maps, key=lambda m: (m.position, m.created_at, m.id))

    def _summary(self, m: CourseMapBase) -> CourseMapSummary:
        return CourseMapSummary(
            **CourseMapCore.model_validate(m).model_dump(),
            bubble_count=self.bubble_counts.get(m.id, 0),
            complete_count=self.complete_counts.get(m.id, 0),
        )

    async def list_for_course(self, moodle_course_id: int) -> list[CourseMapSummary]:
        return [self._summary(m) for m in self._level_order(moodle_course_id)]

    async def create(self, data: CourseMapCreate) -> CourseMapBase:
        siblings = self._level_order(data.moodle_course_id)
        if data.moodle_section_id is not None and any(
            m.moodle_section_id == data.moodle_section_id for m in siblings
        ):
            raise SectionAlreadyMappedError(data.moodle_section_id)
        now = datetime.now(UTC)
        item = CourseMapBase(
            id=uuid7(),
            created_at=now,
            updated_at=now,
            position=max((m.position for m in siblings), default=-1) + 1,
            **data.model_dump(),
        )
        self.items[item.id] = item
        return item

    async def list_skin_rules(self, course_map_id: uuid.UUID) -> list[SkinRule]:
        return sorted(self.rules.get(course_map_id, []), key=lambda r: r.modname)

    async def replace_appearance(
        self,
        course_map_id: uuid.UUID,
        settings: MapSettings,
        default_skin_id: uuid.UUID | None,
        rules: list[SkinRule],
        expected_revision: int,
    ) -> bool:
        current = self.items.get(course_map_id)
        if current is None:
            return False
        if current.revision != expected_revision:
            raise VersionConflictError(current_revision=current.revision)
        self.appearance_writes += 1
        self.items[course_map_id] = current.model_copy(
            update={
                "settings": settings,
                "default_skin_id": default_skin_id,
                "revision": current.revision + 1,
            }
        )
        self.rules[course_map_id] = list(rules)
        return True

    async def set_order(
        self, moodle_course_id: int, ordered_ids: list[uuid.UUID]
    ) -> None:
        for position, map_id in enumerate(ordered_ids):
            current = self.items[map_id]
            if current.moodle_course_id == moodle_course_id:
                self.items[map_id] = current.model_copy(update={"position": position})

    async def update(
        self, course_map_id: uuid.UUID, data: CourseMapUpdate, expected_revision: int
    ) -> CourseMapBase | None:
        current = self.items.get(course_map_id)
        if current is None:
            return None
        if current.revision != expected_revision:
            raise VersionConflictError(current_revision=current.revision)
        changes = data.model_dump(exclude_unset=True)
        updated = current.model_copy(
            update={
                **changes,
                "updated_at": datetime.now(UTC),
                "revision": current.revision + 1,
            }
        )
        self.items[course_map_id] = updated
        return updated

    async def delete(self, course_map_id: uuid.UUID) -> bool:
        return self.items.pop(course_map_id, None) is not None

    async def list(
        self,
        moodle_course_id: int | None,
        q: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[CourseMapSummary], int]:
        matches = [
            m
            for m in self.items.values()
            if (moodle_course_id is None or m.moodle_course_id == moodle_course_id)
            and (not q or q.lower() in m.title.lower())
        ]
        if moodle_course_id is None:
            matches.sort(key=lambda m: (m.updated_at, m.id), reverse=True)
        else:
            matches.sort(key=lambda m: (m.position, m.created_at, m.id))
        page = [self._summary(m) for m in matches[offset : offset + limit]]
        return page, len(matches)


class InMemoryBubbleRepository:
    def __init__(self, course_maps: InMemoryCourseMapRepository) -> None:
        self.items: dict[uuid.UUID, BubbleRead] = {}
        self._course_maps = course_maps
        self.writes = 0  # counts write calls, to assert on what the service did

    async def get_by_id(self, bubble_id: uuid.UUID) -> BubbleRead | None:
        return self.items.get(bubble_id)

    async def list_by_course_map(self, course_map_id: uuid.UUID) -> list[BubbleRead]:
        own = [b for b in self.items.values() if b.course_map_id == course_map_id]
        return sorted(own, key=lambda b: (b.sequence, b.created_at, b.id))

    async def list_by_course(self, moodle_course_id: int) -> list[BubbleRead]:
        return [
            b
            for b in self.items.values()
            if self._course_maps.items[b.course_map_id].moodle_course_id
            == moodle_course_id
        ]

    async def create(
        self, course_map_id: uuid.UUID, moodle_course_id: int, data: BubbleCreate
    ) -> BubbleRead:
        if course_map_id not in self._course_maps.items:
            raise CourseMapNotFoundError(course_map_id)
        for other in await self.list_by_course(moodle_course_id):
            if data.activity_id is not None and other.activity_id == data.activity_id:
                owner = self._course_maps.items[other.course_map_id]
                raise ActivityAlreadyPlacedError(
                    data.activity_id, owner.id, owner.title
                )
        self.writes += 1
        counts = self._course_maps.bubble_counts
        counts[course_map_id] = counts.get(course_map_id, 0) + 1
        now = datetime.now(UTC)
        siblings = await self.list_by_course_map(course_map_id)
        item = BubbleRead(
            id=uuid7(),
            course_map_id=course_map_id,
            skin_id=None,
            sequence=max((b.sequence for b in siblings), default=-1) + 1,
            created_at=now,
            updated_at=now,
            **data.model_dump(),
        )
        self.items[item.id] = item
        return item

    async def set_order(
        self, course_map_id: uuid.UUID, ordered_ids: list[uuid.UUID]
    ) -> None:
        self.writes += 1
        for sequence, bubble_id in enumerate(ordered_ids):
            current = self.items[bubble_id]
            if current.course_map_id == course_map_id:
                self.items[bubble_id] = current.model_copy(
                    update={"sequence": sequence}
                )

    async def update(
        self, bubble_id: uuid.UUID, data: BubbleUpdate, expected_version: int | None
    ) -> BubbleRead | None:
        current = self.items.get(bubble_id)
        if current is None:
            return None
        bump = 0
        if expected_version is not None:
            if current.version != expected_version:
                raise VersionConflictError(current_version=current.version)
            bump = 1
        self.writes += 1
        changes = data.model_dump(exclude_unset=True)
        updated = current.model_copy(
            update={
                **changes,
                "updated_at": datetime.now(UTC),
                "version": current.version + bump,
            }
        )
        self.items[bubble_id] = updated
        return updated

    async def delete(self, bubble_id: uuid.UUID, expected_version: int) -> bool:
        current = self.items.get(bubble_id)
        if current is None:
            return False
        if current.version != expected_version:
            raise VersionConflictError(current_version=current.version)
        self.writes += 1
        del self.items[bubble_id]
        self._course_maps.bubble_counts[current.course_map_id] -= 1
        return True


class InMemoryView3dRepository:
    """Keeps each map's 3D view; like the database, a binding whose bubble was
    deleted is gone (the cascade), so `get` never returns one."""

    def __init__(
        self,
        course_maps: InMemoryCourseMapRepository,
        bubbles: InMemoryBubbleRepository,
    ) -> None:
        self._course_maps = course_maps
        self._bubbles = bubbles
        self.items: dict[uuid.UUID, View3d] = {}
        self.writes = 0

    async def get(self, course_map_id: uuid.UUID) -> View3d | None:
        stored = self.items.get(course_map_id)
        if stored is None:
            return None
        alive = [b for b in stored.bindings if b.bubble_id in self._bubbles.items]
        return View3d(
            scene=stored.scene, bindings=sorted(alive, key=lambda b: b.zone_id)
        )

    async def replace(
        self,
        course_map_id: uuid.UUID,
        view3d: View3d | None,
        expected_revision: int,
    ) -> bool:
        current = self._course_maps.items.get(course_map_id)
        if current is None:
            return False
        if current.revision != expected_revision:
            raise VersionConflictError(current_revision=current.revision)
        self.writes += 1
        self._course_maps.items[course_map_id] = current.model_copy(
            update={"revision": current.revision + 1}
        )
        if view3d is None:
            self.items.pop(course_map_id, None)
        else:
            self.items[course_map_id] = view3d
        return True


class InMemorySkinRepository:
    """Knows which skin ids exist; `service` tests only need that."""

    def __init__(self) -> None:
        self.ids: set[uuid.UUID] = set()

    def add(self) -> uuid.UUID:
        skin_id: uuid.UUID = uuid7()
        self.ids.add(skin_id)
        return skin_id

    async def get_by_id(self, skin_id: uuid.UUID) -> SkinRead | None:
        raise NotImplementedError

    async def existing_ids(self, skin_ids: Collection[uuid.UUID]) -> set[uuid.UUID]:
        return {i for i in skin_ids if i in self.ids}

    async def create(self, name: str, config: ProceduralSkin | ImageSkin) -> SkinRead:
        raise NotImplementedError

    async def update(
        self,
        skin_id: uuid.UUID,
        name: str | None,
        config: ProceduralSkin | ImageSkin | None,
    ) -> SkinRead | None:
        raise NotImplementedError

    async def delete(self, skin_id: uuid.UUID) -> bool:
        raise NotImplementedError

    async def list(self, limit: int) -> list[SkinRead]:
        raise NotImplementedError


class InMemoryAssetRepository:
    """Knows which assets exist; service tests only need lookups."""

    def __init__(self) -> None:
        self.items: dict[uuid.UUID, AssetRecord] = {}

    def add(self, kind: AssetKind = AssetKind.BACKGROUND) -> AssetRecord:
        record = AssetRecord(
            id=uuid7(),
            kind=kind,
            mime="image/png",
            width=10,
            height=10,
            size_bytes=1,
            sha256=uuid.uuid4().hex * 2,
            title="Fake",
            credit=None,
            original_filename=None,
            created_at=datetime.now(UTC),
        )
        self.items[record.id] = record
        return record

    async def get_by_id(self, asset_id: uuid.UUID) -> AssetRecord | None:
        return self.items.get(asset_id)

    async def get_many(
        self, asset_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, AssetRecord]:
        return {i: self.items[i] for i in asset_ids if i in self.items}

    async def find_by_hash(
        self, kind: AssetKind, sha256: str, *, library: bool = False
    ) -> AssetRecord | None:
        raise NotImplementedError

    async def total_bytes(self) -> int:
        raise NotImplementedError

    async def create(self, *args: object, **kwargs: object) -> Never:
        raise NotImplementedError

    async def list_created_before(self, cutoff: datetime) -> list[AssetRecord]:
        raise NotImplementedError

    async def update_metadata(
        self, asset_id: uuid.UUID, data: AssetUpdate
    ) -> AssetRecord | None:
        raise NotImplementedError

    async def usage(
        self, asset_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, AssetUsage]:
        raise NotImplementedError

    async def delete_if_unused(self, asset_id: uuid.UUID) -> bool:
        raise NotImplementedError

    async def set_quarantine(
        self, asset_id: uuid.UUID, reason: str | None, now: datetime
    ) -> tuple[AssetRecord, bool] | None:
        raise NotImplementedError

    async def record_event(
        self, asset_id: uuid.UUID, action: str, reason: str | None
    ) -> None:
        raise NotImplementedError

    async def audit_events(self, asset_id: uuid.UUID) -> list[AssetAuditEntry]:
        raise NotImplementedError

    async def list_audio(self) -> list[AssetRecord]:
        raise NotImplementedError

    async def list(
        self,
        kind: AssetKind | None,
        q: str | None,
        limit: int,
        offset: int,
        scope: AssetScope = AssetScope.TENANT,
    ) -> tuple[list[AssetRecord], int]:
        raise NotImplementedError


class InMemoryPublicationRepository:
    """Nothing is ever published; service tests of maps only need that."""

    async def load_draft(self, course_map_id: uuid.UUID) -> DraftInputs | None:
        raise NotImplementedError

    async def load_drafts(
        self, course_map_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, DraftInputs]:
        return {}

    async def latest(self, course_map_id: uuid.UUID) -> PublicationRecord | None:
        return None

    async def latest_meta(
        self, course_map_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, PublicationMeta]:
        return {}

    async def get(
        self, course_map_id: uuid.UUID, number: int
    ) -> PublicationRecord | None:
        return None

    async def list_summaries(
        self, course_map_id: uuid.UUID, limit: int
    ) -> list[PublicationSummary]:
        return []

    async def publish(
        self,
        course_map_id: uuid.UUID,
        expected_hash: str | None,
        note: str | None,
        keep: int,
    ) -> PublicationSummary:
        raise NotImplementedError

    async def restore_into_draft(
        self,
        course_map_id: uuid.UUID,
        snapshot: dict[str, Any],
        expected_revision: int,
    ) -> list[RestoreWarning] | None:
        raise NotImplementedError

    async def list_published(
        self, moodle_course_id: int | None, limit: int, offset: int
    ) -> tuple[list[PublishedRow], int]:
        return [], 0

    async def get_published(self, course_map_id: uuid.UUID) -> PublishedRow | None:
        return None

    async def bubble_statuses(
        self, bubble_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, BubbleStatus]:
        return {}


class InMemoryMoodleClient:
    """Serves canned course contents; no HTTP.

    Set `error` to make every call raise it, to simulate Moodle failures.
    """

    def __init__(self) -> None:
        self.courses: dict[int, list[MoodleSection]] = {}
        self.error: Exception | None = None
        self.calls = 0  # get_course_contents calls that reached this fake
        # When set, get_course_contents waits for it: lets a test hold a call
        # open while other callers pile up.
        self.gate: asyncio.Event | None = None

    async def get_site_info(self) -> MoodleSiteInfo:
        if self.error:
            raise self.error
        return MoodleSiteInfo(sitename="Fake Moodle")

    async def get_course_contents(self, course_id: int) -> list[MoodleSection]:
        self.calls += 1
        if self.gate is not None:
            await self.gate.wait()
        if self.error:
            raise self.error
        if course_id not in self.courses:
            raise MoodleCourseNotFoundError("Moodle course not found")
        return self.courses[course_id]


class FakeClock:
    """A manually advanced monotonic clock, for cache tests."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class InMemoryTenantRepository:
    def __init__(self) -> None:
        self.tenants: dict[uuid.UUID, TenantRecord] = {}
        self.keys: list[TenantKeyRecord] = []

    async def get_by_id(self, tenant_id: uuid.UUID) -> TenantRecord | None:
        return self.tenants.get(tenant_id)

    async def get_by_issuer(self, issuer: str) -> TenantRecord | None:
        return next((t for t in self.tenants.values() if t.issuer == issuer), None)

    async def list_all(self) -> list[TenantRecord]:
        return sorted(self.tenants.values(), key=lambda t: (t.created_at, t.id))

    async def create(
        self,
        name: str,
        issuer: str,
        host_origin: str,
        asset_quota_bytes: int,
        key: NewTenantKey,
    ) -> TenantRecord:
        if await self.get_by_issuer(issuer) is not None:
            raise TenantIssuerTakenError(issuer)
        tenant = TenantRecord(
            id=uuid7(),
            name=name,
            issuer=issuer,
            host_origin=host_origin,
            status=TenantStatus.ACTIVE,
            asset_quota_bytes=asset_quota_bytes,
            created_at=datetime.now(UTC),
        )
        self.tenants[tenant.id] = tenant
        await self.add_key(tenant.id, key)
        return tenant

    async def set_status(
        self, tenant_id: uuid.UUID, status: TenantStatus
    ) -> TenantRecord | None:
        tenant = self.tenants.get(tenant_id)
        if tenant is None:
            return None
        self.tenants[tenant_id] = tenant.model_copy(update={"status": status})
        return self.tenants[tenant_id]

    async def set_widgets_enabled(
        self, tenant_id: uuid.UUID, enabled: bool
    ) -> TenantRecord | None:
        tenant = self.tenants.get(tenant_id)
        if tenant is None:
            return None
        self.tenants[tenant_id] = tenant.model_copy(update={"widgets_enabled": enabled})
        return self.tenants[tenant_id]

    async def add_key(self, tenant_id: uuid.UUID, key: NewTenantKey) -> TenantKeyRecord:
        if tenant_id not in self.tenants:
            raise TenantNotFoundError(tenant_id)
        if any(k.tenant_id == tenant_id and k.kid == key.kid for k in self.keys):
            raise TenantKeyTakenError(key.kid)
        record = TenantKeyRecord(
            id=uuid7(),
            tenant_id=tenant_id,
            kid=key.kid,
            public_key_pem=key.public_key_pem,
            status=KeyStatus.ACTIVE,
            created_at=datetime.now(UTC),
            revoked_at=None,
        )
        self.keys.append(record)
        return record

    async def revoke_key(
        self, tenant_id: uuid.UUID, kid: str
    ) -> TenantKeyRecord | None:
        for index, key in enumerate(self.keys):
            if key.tenant_id == tenant_id and key.kid == kid:
                if key.status != KeyStatus.REVOKED:
                    self.keys[index] = key.model_copy(
                        update={
                            "status": KeyStatus.REVOKED,
                            "revoked_at": datetime.now(UTC),
                        }
                    )
                return self.keys[index]
        return None

    async def list_keys(self, tenant_id: uuid.UUID) -> list[TenantKeyRecord]:
        return [k for k in self.keys if k.tenant_id == tenant_id]

    async def active_key(
        self, tenant_id: uuid.UUID, kid: str
    ) -> TenantKeyRecord | None:
        return next(
            (
                k
                for k in self.keys
                if k.tenant_id == tenant_id
                and k.kid == kid
                and k.status == KeyStatus.ACTIVE
            ),
            None,
        )

    async def active_host_origins(self) -> list[str]:
        return sorted(
            {
                t.host_origin
                for t in self.tenants.values()
                if t.status == TenantStatus.ACTIVE
            }
        )


class InMemoryAuthRepository:
    def __init__(self, tenants: InMemoryTenantRepository) -> None:
        self._tenants = tenants
        self.tokens: dict[str, NewAccessToken] = {}
        self.jtis: dict[tuple[uuid.UUID, str], datetime] = {}

    async def register_launch(
        self,
        tenant_id: uuid.UUID,
        jti: str,
        jti_expires_at: datetime,
        token: NewAccessToken,
    ) -> None:
        if (tenant_id, jti) in self.jtis:
            raise LaunchReplayedError(jti)
        self.jtis[(tenant_id, jti)] = jti_expires_at
        self.tokens[token.token_hash] = token

    async def find_grant(self, token_hash: str, now: datetime) -> TokenGrant | None:
        token = self.tokens.get(token_hash)
        if token is None or token.expires_at <= now:
            return None
        tenant = self._tenants.tenants.get(token.tenant_id)
        if tenant is None or tenant.status is not TenantStatus.ACTIVE:
            return None
        return TokenGrant(**token.model_dump())

    async def purge_expired(self, now: datetime) -> tuple[int, int]:
        old_tokens = [h for h, t in self.tokens.items() if t.expires_at <= now]
        old_jtis = [k for k, expires in self.jtis.items() if expires <= now]
        for token_hash in old_tokens:
            del self.tokens[token_hash]
        for key in old_jtis:
            del self.jtis[key]
        return len(old_tokens), len(old_jtis)
