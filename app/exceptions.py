"""Domain errors, raised by repositories/services and mapped to HTTP in the API layer."""

import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.schemas.asset import AssetUsage


class DomainError(Exception):
    """Base class for expected, business-level failures."""


class CourseMapNotFoundError(DomainError):
    pass


class BubbleNotFoundError(DomainError):
    pass


class ActivityAlreadyPlacedError(DomainError):
    """The Moodle activity already has a bubble on one of the course's maps.

    `course_map_id` / `course_map_title` say where, when the repository could
    find out (it may not if the other bubble vanished in the meantime).
    """

    def __init__(
        self,
        activity_id: int,
        course_map_id: uuid.UUID | None = None,
        course_map_title: str | None = None,
    ) -> None:
        super().__init__(activity_id)
        self.activity_id = activity_id
        self.course_map_id = course_map_id
        self.course_map_title = course_map_title


class SectionAlreadyMappedError(DomainError):
    """The course already has a map for this Moodle section."""


class PreconditionRequiredError(DomainError):
    """A guarded write came without `If-Match`."""


class InvalidIfMatchError(DomainError):
    """`If-Match` is not a single quoted number such as `"3"`."""

    def __init__(self, raw: str) -> None:
        super().__init__("malformed If-Match")
        self.raw = raw


class VersionConflictError(DomainError):
    """The caller's `If-Match` is stale: the resource moved on.

    Exactly one of the two is set: `current_revision` for a map,
    `current_version` for a bubble.
    """

    def __init__(
        self, *, current_revision: int | None = None, current_version: int | None = None
    ) -> None:
        super().__init__("stale If-Match")
        self.current_revision = current_revision
        self.current_version = current_version


class NoChangesToPublishError(DomainError):
    """The draft is identical to the current publication."""


class DraftChangedSinceReviewError(DomainError):
    """The draft is not what the editor reviewed (`draftHash` is stale)."""

    def __init__(self, current_hash: str) -> None:
        super().__init__("draft changed since review")
        self.current_hash = current_hash


class PublishInvalidError(DomainError):
    """The draft (or a snapshot being loaded) points at things that are gone.

    `problems` are (code, message) pairs, every one of them, not just the first.
    """

    def __init__(self, problems: list[tuple[str, str]]) -> None:
        super().__init__("publish invalid")
        self.problems = problems


class PublishConflictError(DomainError):
    """Another publish of the same map won the race, twice in a row."""


class NeverPublishedError(DomainError):
    """There is no publication to go back to."""


class PublicationNotFoundError(DomainError):
    pass


class NotPublishedError(DomainError):
    """A student asked for a map that has no publication (or does not exist)."""


class OrderMismatchError(DomainError):
    """An ordering request does not list exactly the existing ids."""

    def __init__(
        self,
        missing: list[uuid.UUID],
        unexpected: list[uuid.UUID],
        duplicated: list[uuid.UUID],
    ) -> None:
        super().__init__("order does not match the existing items")
        self.missing = missing
        self.unexpected = unexpected
        self.duplicated = duplicated


class AssetNotFoundError(DomainError):
    pass


class UploadsDisabledError(DomainError):
    """`ASSETS_UPLOADS_ENABLED` is off."""


class AssetIsLibraryError(DomainError):
    """Library tracks are the operator's: editors can neither change nor delete
    them."""


class AssetTooLargeError(DomainError):
    def __init__(self, limit_bytes: int) -> None:
        super().__init__(f"upload exceeds {limit_bytes} bytes")
        self.limit_bytes = limit_bytes


class AssetTypeNotAllowedError(DomainError):
    """Not a PNG, JPEG or static WebP (SVG, GIF, animations, anything else)."""


class AssetInvalidImageError(DomainError):
    """Looks like an allowed format but does not decode."""


class AssetDimensionsTooLargeError(DomainError):
    def __init__(self, limit_side: int, width: int, height: int) -> None:
        super().__init__(f"{width}x{height} exceeds {limit_side} px per side")
        self.limit_side = limit_side
        self.width = width
        self.height = height


class AudioInvalidError(DomainError):
    """Not MP3 or AAC/M4A, malformed, or protected. `reason` is for the log; the
    client only ever sees the fixed message of `audio_invalid`."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class AudioTooLongError(DomainError):
    def __init__(self, limit_seconds: float) -> None:
        super().__init__(f"audio exceeds {limit_seconds} seconds")
        self.limit_seconds = limit_seconds


class AssetQuotaExceededError(DomainError):
    """Storing the file would pass `ASSETS_MAX_TOTAL_BYTES`."""


class AssetAlreadyExistsError(DomainError):
    """An asset with the same kind and content already exists."""


class InvalidUploadError(DomainError):
    """The upload form is malformed (no file, unknown kind)."""


class InvalidAssetMetadataError(DomainError):
    """The upload's `title` or `credit` is empty where required or too long."""


class AssetInUseError(DomainError):
    """The asset is referenced by a map, a skin or a publication."""

    def __init__(self, usage: "AssetUsage") -> None:
        super().__init__("asset in use")
        self.usage = usage


class BackgroundAssetNotFoundError(DomainError):
    """A map's `imageUrl` points at `/api/v1/assets/{id}` but no such asset exists."""

    def __init__(self, asset_id: uuid.UUID) -> None:
        super().__init__("background asset not found")
        self.asset_id = asset_id


class BackgroundAssetWrongKindError(DomainError):
    """A map's `imageUrl` points at an asset that is not a background."""

    def __init__(self, asset_id: uuid.UUID) -> None:
        super().__init__("asset is not a background")
        self.asset_id = asset_id


class SkinNotFoundError(DomainError):
    pass


class SkinIsBuiltinError(DomainError):
    """Built-in skins can be neither edited nor deleted."""


class SkinReferenceNotFoundError(DomainError):
    """A request points at a skin id that does not exist (422, not 404: the
    thing being addressed exists, the value it carries does not)."""

    def __init__(self, skin_ids: list[uuid.UUID]) -> None:
        super().__init__("skin not found")
        self.skin_ids = skin_ids


class SkinAssetNotFoundError(DomainError):
    def __init__(self, missing: dict[str, uuid.UUID]) -> None:
        super().__init__("skin asset not found")
        self.missing = missing  # state name -> asset id


class SkinAssetWrongKindError(DomainError):
    def __init__(self, wrong: dict[str, uuid.UUID]) -> None:
        super().__init__("skin asset is not a bubble image")
        self.wrong = wrong  # state name -> asset id


class SkinAssetSizeMismatchError(DomainError):
    """The state images of an image skin do not share one width and height."""

    def __init__(self, sizes: list[tuple[str, uuid.UUID, int, int]]) -> None:
        super().__init__("skin assets differ in size")
        self.sizes = sizes  # (state, asset id, width, height)


class DuplicateSkinRuleError(DomainError):
    """The same activity type appears in more than one skin rule."""

    def __init__(self, modnames: list[str]) -> None:
        super().__init__("duplicate skin rule")
        self.modnames = modnames


class View3dInvalidError(DomainError):
    """A 3D view cannot be stored as sent. `code` is one of `scene_unknown`,
    `zone_out_of_range`, `duplicate_zone`, `duplicate_bubble`,
    `bubble_not_in_map`; `extras` go into the response next to the message."""

    def __init__(self, code: str, message: str, **extras: object) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.extras = extras


class TenantNotFoundError(DomainError):
    pass


class TenantIssuerTakenError(DomainError):
    """Another tenant is already registered for this issuer."""


class TenantKeyNotFoundError(DomainError):
    pass


class TenantKeyTakenError(DomainError):
    """The tenant already has a key with this `kid` (even a revoked one)."""


class LaunchInvalidError(DomainError):
    """A launch assertion failed verification. The caller only ever sees one
    code; `reason` (a short slug) is for the server-side audit log."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class TenantDisabledError(DomainError):
    """The assertion is genuine but its tenant is disabled."""


class LaunchRateLimitedError(DomainError):
    def __init__(self, retry_after: int) -> None:
        super().__init__("too many launches")
        self.retry_after = retry_after


class LaunchReplayedError(DomainError):
    """The assertion's `jti` was already redeemed."""


class UnauthorizedError(DomainError):
    """No valid bearer token (missing, unknown, expired, or its tenant is off)."""


class ForbiddenError(DomainError):
    """The caller is signed in but their role does not allow this."""


class MoodleSourceNotProvidedError(DomainError):
    """The route answers from Moodle's web services, which a plugin tenant does
    not have: its Moodle state comes from the plugin, through the browser."""
