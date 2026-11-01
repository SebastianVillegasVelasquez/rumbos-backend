"""The `.rumbos` archive format, version 1 (see `docs/EXPORT-FORMAT.md`).

Strict on purpose: an unknown field is an error, only the camelCase spelling is
read, and every list and string has a limit. These models say what an archive
may CONTAIN; whether it is consistent (references that resolve, assets that
decode, content the public API would accept) is `app.portability.validation`.

Database ids never appear here. Things point at each other with `ref` values,
which only mean something inside one archive.
"""

import re
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)
from pydantic.alias_generators import to_camel

from app.enums import AssetKind, BubbleIcon, BubbleKind
from app.schemas.content import Line
from app.schemas.course_map import ImageUrl, Modname, Title
from app.schemas.skin import SkinName
from app.schemas.view3d import LocalizedDescriptionUpdate, LocalizedTitleUpdate

FORMAT_VERSION = 1
MIN_READER_VERSION = 1
GENERATOR = "rumbos"
EXTENSION = ".rumbos"

MANIFEST_PATH = "manifest.json"
MAPS_PATH = "maps.json"
ASSETS_DIR = "assets/"

# Hard limits of the format. `IMPORT_MAX_ENTRIES` and friends lower them further.
MAX_MAPS = 20
MAX_BUBBLES_PER_MAP = 500
MAX_SKINS = 100
MAX_ASSETS = 1000
MAX_SKIN_RULES = 200
MAX_BINDINGS = 100
NAME_MAX = 255
LIBRARY_TITLE_MAX = 120
INT32_MAX = 2**31 - 1

# The only extension each stored media type may have.
MIME_EXTENSION = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "audio/mpeg": "mp3",
    "audio/mp4": "m4a",
}
Mime = Literal["image/png", "image/jpeg", "image/webp", "audio/mpeg", "audio/mp4"]

REF_PATTERN = r"^[a-z]{1,2}[0-9]{1,5}$"
_ENTRY = (
    r"manifest\.json|maps\.json|assets/[a-z]{1,2}[0-9]{1,5}\.(?:png|jpg|webp|mp3|m4a)"
)
ENTRY_PATTERN = re.compile(_ENTRY)

Ref = Annotated[str, StringConstraints(pattern=REF_PATTERN)]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
EntryPath = Annotated[str, StringConstraints(pattern=f"^(?:{_ENTRY})$")]
UnitFloat = Annotated[float, Field(ge=0, le=1)]


class _Model(BaseModel):
    """Python code uses snake_case names; the file uses camelCase, and only
    camelCase: read with `model_validate(..., by_alias=True, by_name=False)`."""

    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid"
    )


# --- manifest.json ---------------------------------------------------------------


class ManifestFile(_Model):
    path: EntryPath
    sha256: Sha256
    size: int = Field(alias="bytes", ge=0)


class ManifestCounts(_Model):
    maps: int = Field(ge=0, le=MAX_MAPS)
    bubbles: int = Field(ge=0, le=MAX_MAPS * MAX_BUBBLES_PER_MAP)
    assets: int = Field(ge=0, le=MAX_ASSETS)


class Manifest(_Model):
    format_version: int = Field(ge=1)
    min_reader_version: int = Field(ge=1)
    exported_at: AwareDatetime
    generator: Literal["rumbos"]
    files: list[ManifestFile] = Field(max_length=MAX_ASSETS + 1)
    counts: ManifestCounts

    @model_validator(mode="after")
    def _files_are_consistent(self) -> Self:
        paths = [f.path for f in self.files]
        if len(set(paths)) != len(paths):
            raise ValueError("a file is listed twice")
        if MAPS_PATH not in paths:
            raise ValueError("maps.json is not listed")
        if MANIFEST_PATH in paths:
            raise ValueError("the manifest does not list itself")
        if sum(p.startswith(ASSETS_DIR) for p in paths) != self.counts.assets:
            raise ValueError("counts.assets does not match the listed assets")
        return self


# --- maps.json -------------------------------------------------------------------


class CustomSkinRef(_Model):
    """A skin embedded in `skins`."""

    ref: Ref


class BuiltinSkinRef(_Model):
    """A built-in skin, by its fixed id."""

    builtin_id: uuid.UUID


SkinRef = CustomSkinRef | BuiltinSkinRef


class LibraryAudioRef(_Model):
    """A track of the operator's library: only named, never carried."""

    library_title: Line = Field(min_length=1, max_length=LIBRARY_TITLE_MAX)
    sha256: Sha256


class OwnAudioRef(_Model):
    """The editor's own track, carried in `assets`."""

    asset_ref: Ref


AudioRef = LibraryAudioRef | OwnAudioRef


class ArchiveAudio(_Model):
    """`settings.audio` of a map: the track by reference, and how to play it."""

    audio_ref: AudioRef
    volume: Annotated[float, Field(ge=0, le=1)] = 0.4
    loop: bool = True
    start_after_interaction: bool = False


class AssetBackground(_Model):
    asset_ref: Ref


class UrlBackground(_Model):
    """A picture that is not stored here: an external address, or a path of the
    site. Never an address of this API's own assets."""

    image_url: ImageUrl


Background = AssetBackground | UrlBackground


class ActivityDescriptor(_Model):
    """What the importer needs to find the activity in the target course. Never
    its id: `original_cmid` is a hint and is never used as one."""

    modname: Modname
    name: Line = Field(min_length=1, max_length=NAME_MAX)
    section_number: int = Field(ge=0, le=10_000)
    section_name: Line = Field(max_length=NAME_MAX)
    position_in_section: int = Field(ge=0, le=10_000)
    original_cmid: int | None = Field(default=None, ge=1, le=INT32_MAX)


class ArchiveBubble(_Model):
    ref: Ref
    kind: BubbleKind
    x: UnitFloat
    y: UnitFloat
    icon: BubbleIcon | None
    skin: CustomSkinRef | BuiltinSkinRef | None
    sequence: int = Field(ge=0, le=100_000)
    activity: ActivityDescriptor | None = None
    # The stored form of a content bubble, with `assetRef` where the API has
    # `assetId`. Checked by `parse_content` once the refs are resolved.
    content: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _kind_decides_the_rest(self) -> Self:
        if self.kind is BubbleKind.ACTIVITY:
            if self.activity is None or self.content is not None:
                raise ValueError("an activity bubble has an activity and no content")
        elif self.content is None or self.activity is not None:
            raise ValueError("a content bubble has content and no activity")
        return self


class ArchiveSkinRule(_Model):
    modname: Modname
    skin: CustomSkinRef | BuiltinSkinRef


class ArchiveBinding(_Model):
    zone_id: int = Field(ge=0, le=10_000)
    bubble_ref: Ref
    title: LocalizedTitleUpdate | None = None
    description: LocalizedDescriptionUpdate | None = None


class ArchiveView3d(_Model):
    scene: str = Field(min_length=1, max_length=60)
    bindings: list[ArchiveBinding] = Field(max_length=MAX_BINDINGS)


class ArchiveMap(_Model):
    ref: Ref
    title: Title
    position: int = Field(ge=0, le=100_000)
    # `MapSettings` as stored, except that `audio` carries an `ArchiveAudio`.
    settings: dict[str, Any]
    background: AssetBackground | UrlBackground
    default_skin: CustomSkinRef | BuiltinSkinRef | None
    skin_rules: list[ArchiveSkinRule] = Field(max_length=MAX_SKIN_RULES)
    bubbles: list[ArchiveBubble] = Field(max_length=MAX_BUBBLES_PER_MAP)
    # Aliased by hand: the camelCase generator would spell it `view3D`.
    view3d: ArchiveView3d | None = Field(alias="view3d")


class ArchiveSkin(_Model):
    ref: Ref
    name: SkinName
    # A `SkinConfig` as stored, with asset refs where the API has asset ids.
    config: dict[str, Any]


class ArchiveAsset(_Model):
    ref: Ref
    kind: AssetKind
    file: EntryPath
    mime: Mime
    sha256: Sha256
    size: int = Field(alias="bytes", ge=1)
    width: int | None = Field(default=None, ge=1, le=16_384)
    height: int | None = Field(default=None, ge=1, le=16_384)
    duration_seconds: float | None = Field(default=None, gt=0, le=3_600)
    title: Line = Field(min_length=1, max_length=120)
    credit: Line | None = Field(default=None, max_length=300)
    license: Line | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def _fits_its_kind(self) -> Self:
        if self.file != f"{ASSETS_DIR}{self.ref}.{MIME_EXTENSION[self.mime]}":
            raise ValueError("the file name does not follow from the ref and mime")
        if self.kind is AssetKind.AUDIO:
            if not self.mime.startswith("audio/"):
                raise ValueError("audio has an audio media type")
            if self.duration_seconds is None or self.width or self.height:
                raise ValueError("audio has a duration and no dimensions")
            if not self.credit:
                raise ValueError("audio needs a credit")
        else:
            if not self.mime.startswith("image/"):
                raise ValueError("an image has an image media type")
            if self.width is None or self.height is None or self.duration_seconds:
                raise ValueError("an image has dimensions and no duration")
        return self


class MapsFile(_Model):
    maps: list[ArchiveMap] = Field(min_length=1, max_length=MAX_MAPS)
    skins: list[ArchiveSkin] = Field(max_length=MAX_SKINS)
    assets: list[ArchiveAsset] = Field(max_length=MAX_ASSETS)


def parse_model[M: _Model](model: type[M], raw: object) -> M:
    """`raw` (JSON read from an archive) as `model`: camelCase only."""
    return model.model_validate(raw, by_alias=True, by_name=False)


def iso(moment: datetime) -> str:
    """The spelling of `exportedAt`: UTC, whole seconds, a `Z`."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
