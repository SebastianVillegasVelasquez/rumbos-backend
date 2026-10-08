"""Domain errors, raised by repositories/services and mapped to HTTP in the API layer."""

import uuid


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


class AssetQuotaExceededError(DomainError):
    """Storing the file would pass `ASSETS_MAX_TOTAL_BYTES`."""


class AssetAlreadyExistsError(DomainError):
    """An asset with the same kind and content already exists."""


class InvalidUploadError(DomainError):
    """The upload form is malformed (no file, unknown kind)."""


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
