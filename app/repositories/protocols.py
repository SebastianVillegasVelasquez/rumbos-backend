"""Repository interfaces the service layer depends on.

These are structural `Protocol`s: any class with matching async methods
satisfies them, with no inheritance. Inputs and outputs are Pydantic
schemas so callers never import SQLAlchemy models.

Contract shared by all implementations:
- Reads (`get_*`, `list_*`) never commit.
- Writes (`create`, `update`, `delete`) are atomic: they commit on
  success and roll back before re-raising on failure.
- A missing target is reported as `None` / `False`, not an exception.
"""

import uuid
from collections.abc import Collection
from datetime import datetime
from typing import Any, Protocol

from app.enums import AssetKind, BubbleStatus, TenantStatus
from app.publishing.snapshot import DraftInputs
from app.schemas.asset import AssetRecord, AssetUpdate, AssetUsage
from app.schemas.auth import NewAccessToken, TokenGrant
from app.schemas.bubble import BubbleCreate, BubbleRead, BubbleUpdate
from app.schemas.course_map import (
    CourseMapBase,
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


class CourseMapRepository(Protocol):
    async def get_by_id(self, course_map_id: uuid.UUID) -> CourseMapBase | None: ...

    async def list_for_course(self, moodle_course_id: int) -> list[CourseMapSummary]:
        """Every map of the course, unpaged, in level order."""
        ...

    async def create(self, data: CourseMapCreate) -> CourseMapBase:
        """Appends the map to its course (position = last + 1).

        Raises `SectionAlreadyMappedError` if the course already has a map for
        `data.moodle_section_id`.
        """
        ...

    async def set_order(
        self, moodle_course_id: int, ordered_ids: list[uuid.UUID]
    ) -> None:
        """Sets `position` 0..n-1 following `ordered_ids`, atomically."""
        ...

    async def update(
        self, course_map_id: uuid.UUID, data: CourseMapUpdate, expected_revision: int
    ) -> CourseMapBase | None:
        """Applies only the fields set in `data`, all in one statement that also
        checks `revision = expected_revision` and adds 1 to it.

        Either every provided field is persisted or none is. Returns None if
        the map doesn't exist; raises `VersionConflictError` (with the current
        revision) if it exists at another revision.
        """
        ...

    async def delete(self, course_map_id: uuid.UUID) -> bool:
        """Deletes the map and its bubbles. Returns False if it didn't exist."""
        ...

    async def list_skin_rules(self, course_map_id: uuid.UUID) -> list[SkinRule]:
        """The map's skin rules, ordered by `modname`."""
        ...

    async def replace_appearance(
        self,
        course_map_id: uuid.UUID,
        settings: MapSettings,
        default_skin_id: uuid.UUID | None,
        rules: list[SkinRule],
        expected_revision: int,
    ) -> bool:
        """Replaces settings, default skin and every skin rule in ONE
        transaction: all or nothing, guarded and bumped like `update`. False if
        the map doesn't exist; `VersionConflictError` if the revision is stale.

        Raises `SkinReferenceNotFoundError` if a skin vanished since the
        caller checked (the foreign keys are the last line of defence).
        """
        ...

    async def list(
        self,
        moodle_course_id: int | None,
        q: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[CourseMapSummary], int]:
        """Maps and the total before paging.

        Ordered by `(position, created_at, id)` when `moodle_course_id` is
        given (the course's levels), otherwise by `updated_at` desc.
        `q` is a case-insensitive substring match on the title.
        """
        ...


class View3dRepository(Protocol):
    async def get(self, course_map_id: uuid.UUID) -> View3d | None:
        """The map's scene with its bindings ordered by zone, or None when the
        map has no 3D view (or doesn't exist)."""
        ...

    async def replace(
        self,
        course_map_id: uuid.UUID,
        view3d: View3d | None,
        expected_revision: int,
    ) -> bool:
        """Replaces the scene and every binding in ONE transaction: all or
        nothing, guarded and bumped like `CourseMapRepository.update`. `None`
        clears the 3D view. False if the map doesn't exist;
        `VersionConflictError` if the revision is stale.

        Does not check the content (the service does); the database's
        constraints are the last line of defence and are reported as
        `View3dInvalidError`.
        """
        ...


class BubbleRepository(Protocol):
    async def get_by_id(self, bubble_id: uuid.UUID) -> BubbleRead | None: ...

    async def list_by_course_map(
        self, course_map_id: uuid.UUID
    ) -> list[BubbleRead]: ...

    async def list_by_course(self, moodle_course_id: int) -> list[BubbleRead]:
        """Every bubble on any map of the Moodle course."""
        ...

    async def create(
        self, course_map_id: uuid.UUID, moodle_course_id: int, data: BubbleCreate
    ) -> BubbleRead:
        """`moodle_course_id` is the parent map's, copied onto the bubble.

        Raises `CourseMapNotFoundError` if the map doesn't exist (or is not of
        that course), and `ActivityAlreadyPlacedError` if the activity already
        has a bubble on any map of the course.
        """
        ...

    async def update(
        self, bubble_id: uuid.UUID, data: BubbleUpdate, expected_version: int | None
    ) -> BubbleRead | None:
        """Applies only the fields set in `data`, all in one statement.

        Either every provided field is persisted or none is. With
        `expected_version` the same statement checks `version = expected` and
        adds 1 (None if missing, `VersionConflictError` if stale). With None it
        is unguarded and does not bump: for `status`-only changes.
        """
        ...

    async def set_order(
        self, course_map_id: uuid.UUID, ordered_ids: list[uuid.UUID]
    ) -> None:
        """Sets `sequence` 0..n-1 following `ordered_ids`, atomically."""
        ...

    async def delete(self, bubble_id: uuid.UUID, expected_version: int) -> bool:
        """One guarded DELETE. False if missing; `VersionConflictError` if stale."""
        ...


class AssetRepository(Protocol):
    async def get_by_id(self, asset_id: uuid.UUID) -> AssetRecord | None: ...

    async def get_many(
        self, asset_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, AssetRecord]:
        """The assets that exist among `asset_ids`, by id."""
        ...

    async def find_by_hash(
        self, kind: AssetKind, sha256: str
    ) -> AssetRecord | None: ...

    async def total_bytes(self) -> int:
        """Sum of the stored size of every asset (what the quota counts)."""
        ...

    async def create(
        self,
        asset_id: uuid.UUID,
        kind: AssetKind,
        mime: str,
        width: int,
        height: int,
        size_bytes: int,
        sha256: str,
        title: str,
        credit: str | None,
        original_filename: str | None,
    ) -> AssetRecord:
        """Raises `AssetAlreadyExistsError` if the kind and hash already exist."""
        ...

    async def list_created_before(self, cutoff: datetime) -> list[AssetRecord]:
        """Every asset created before `cutoff`, oldest first."""
        ...

    async def update_metadata(
        self, asset_id: uuid.UUID, data: AssetUpdate
    ) -> AssetRecord | None:
        """Applies only the fields set in `data`. None if there is no such asset."""
        ...

    async def usage(
        self, asset_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, AssetUsage]:
        """What references each asset, counted in a handful of queries (never one
        per asset). Every requested id is in the result, zeros included."""
        ...

    async def delete_if_unused(self, asset_id: uuid.UUID) -> bool:
        """Removes the row in ONE statement that also checks nothing references
        it. False if there is no such asset; raises `AssetInUseError` if it is
        referenced. Does not touch the stored files."""
        ...

    async def list(
        self, kind: AssetKind | None, q: str | None, limit: int, offset: int
    ) -> tuple[list[AssetRecord], int]:
        """Assets newest first and the total before paging. `q` is a
        case-insensitive substring match on title and credit."""
        ...


class SkinRepository(Protocol):
    async def get_by_id(self, skin_id: uuid.UUID) -> SkinRead | None: ...

    async def existing_ids(self, skin_ids: Collection[uuid.UUID]) -> set[uuid.UUID]:
        """Which of `skin_ids` are skins."""
        ...

    async def create(
        self, name: str, config: ProceduralSkin | ImageSkin
    ) -> SkinRead: ...

    async def update(
        self,
        skin_id: uuid.UUID,
        name: str | None,
        config: ProceduralSkin | ImageSkin | None,
    ) -> SkinRead | None:
        """Changes only what is not None, in one statement."""
        ...

    async def delete(self, skin_id: uuid.UUID) -> bool:
        """Bubbles using the skin lose it (`skin_id` null), maps using it as
        their default lose that, and its skin rules go. Done by the database."""
        ...

    async def list(self, limit: int) -> list[SkinRead]:
        """Built-in skins first, then by creation."""
        ...


class PublicationRepository(Protocol):
    async def load_draft(self, course_map_id: uuid.UUID) -> DraftInputs | None:
        """The map's draft as snapshot inputs. None if the map doesn't exist."""
        ...

    async def load_drafts(
        self, course_map_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, DraftInputs]:
        """Several drafts in a fixed handful of queries (never one per map)."""
        ...

    async def latest(self, course_map_id: uuid.UUID) -> PublicationRecord | None:
        """The map's highest-numbered publication, snapshot included."""
        ...

    async def latest_meta(
        self, course_map_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, PublicationMeta]:
        """The latest publication of each map that has one, without snapshots."""
        ...

    async def get(
        self, course_map_id: uuid.UUID, number: int
    ) -> PublicationRecord | None: ...

    async def list_summaries(
        self, course_map_id: uuid.UUID, limit: int
    ) -> list[PublicationSummary]:
        """Newest first."""
        ...

    async def publish(
        self,
        course_map_id: uuid.UUID,
        expected_hash: str | None,
        note: str | None,
        keep: int,
    ) -> PublicationSummary:
        """Builds the draft's snapshot and stores it as the next publication, in
        ONE REPEATABLE READ transaction (retried once on a serialization
        failure), and prunes all but the latest `keep`.

        `expected_hash` is what the editor reviewed; None skips that check.
        Raises `CourseMapNotFoundError`, `DraftChangedSinceReviewError`,
        `NoChangesToPublishError`, `PublishInvalidError` (every problem listed)
        or `PublishConflictError`.
        """
        ...

    async def restore_into_draft(
        self,
        course_map_id: uuid.UUID,
        snapshot: dict[str, Any],
        expected_revision: int,
    ) -> list[RestoreWarning] | None:
        """Loads a snapshot into the draft in ONE transaction and bumps the map
        revision. None if the map doesn't exist; `VersionConflictError` if the
        revision is stale; `PublishInvalidError` (nothing changed) if the
        snapshot's background is gone."""
        ...

    async def list_published(
        self, moodle_course_id: int | None, limit: int, offset: int
    ) -> tuple[list[PublishedRow], int]:
        """Maps that have at least one publication, each with its latest, ordered
        by `(position, created_at, id)`, and the total before paging."""
        ...

    async def get_published(self, course_map_id: uuid.UUID) -> PublishedRow | None:
        """The map's latest publication with its live columns, or None if the
        map does not exist or was never published."""
        ...

    async def bubble_statuses(
        self, bubble_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, BubbleStatus]:
        """Live status of the bubbles that still exist (others are absent)."""
        ...


class TenantRepository(Protocol):
    """Tenants and their keys. Not tenant-scoped: this is what tenancy is made of."""

    async def get_by_id(self, tenant_id: uuid.UUID) -> TenantRecord | None: ...

    async def get_by_issuer(self, issuer: str) -> TenantRecord | None: ...

    async def list_all(self) -> list[TenantRecord]:
        """Every tenant, oldest first."""
        ...

    async def create(
        self,
        name: str,
        issuer: str,
        host_origin: str,
        asset_quota_bytes: int,
        key: NewTenantKey,
    ) -> TenantRecord:
        """The tenant and its first key, in ONE transaction. Raises
        `TenantIssuerTakenError` if the issuer is registered."""
        ...

    async def set_status(
        self, tenant_id: uuid.UUID, status: TenantStatus
    ) -> TenantRecord | None:
        """None if there is no such tenant."""
        ...

    async def add_key(self, tenant_id: uuid.UUID, key: NewTenantKey) -> TenantKeyRecord:
        """Raises `TenantNotFoundError`, or `TenantKeyTakenError` if the tenant
        has a key with that `kid` already (revoked ones count: a `kid` is never
        reused)."""
        ...

    async def revoke_key(
        self, tenant_id: uuid.UUID, kid: str
    ) -> TenantKeyRecord | None:
        """Marks the key revoked (idempotent). None if there is no such key."""
        ...

    async def list_keys(self, tenant_id: uuid.UUID) -> list[TenantKeyRecord]:
        """Every key of the tenant, oldest first."""
        ...

    async def active_key(
        self, tenant_id: uuid.UUID, kid: str
    ) -> TenantKeyRecord | None:
        """The tenant's key with this `kid`, only if it is active."""
        ...

    async def active_host_origins(self) -> list[str]:
        """The distinct `host_origin` of every active tenant, sorted."""
        ...


class AuthRepository(Protocol):
    """Bearer tokens and redeemed `jti`s. Not tenant-scoped: a token is how a
    request gets its tenant in the first place."""

    async def register_launch(
        self,
        tenant_id: uuid.UUID,
        jti: str,
        jti_expires_at: datetime,
        token: NewAccessToken,
    ) -> None:
        """Redeems the `jti` and stores the token in ONE transaction. Raises
        `LaunchReplayedError` (nothing stored) if the `jti` was redeemed."""
        ...

    async def find_grant(self, token_hash: str, now: datetime) -> TokenGrant | None:
        """The grant behind a token, only while the token has not expired and its
        tenant is active."""
        ...

    async def purge_expired(self, now: datetime) -> tuple[int, int]:
        """Deletes expired tokens and `jti`s; returns how many of each."""
        ...
