"""What an archive says, checked as the public API would check it.

`format` only says what an archive may contain. Here every piece is put through
the validator the API itself uses for that piece (`MapSettings`, `parse_content`,
the skin config models, the title and address rules, the 3D rules), after the
archive-local `ref`s have been swapped for stand-in ids so those validators can
run unchanged. Hostile text stays text: nothing is interpreted.

Everything that is wrong is collected, not just the first thing, and reported as
`{path, code, message}` without echoing the input.
"""

import io
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from app.assets.audio import process_audio
from app.assets.processing import ImageLimits, process_image
from app.assets.references import asset_id_of_image_url
from app.enums import AssetKind, BubbleKind
from app.exceptions import (
    ArchiveInvalidError,
    AssetDimensionsTooLargeError,
    AssetInvalidImageError,
    AssetTypeNotAllowedError,
    AudioInvalidError,
    AudioTooLongError,
    ContentInvalidError,
    ContentTooLargeError,
    View3dInvalidError,
)
from app.portability.format import (
    ArchiveAsset,
    ArchiveAudio,
    ArchiveBubble,
    ArchiveMap,
    ArchiveSkin,
    AssetBackground,
    BuiltinSkinRef,
    CustomSkinRef,
    Manifest,
    MapsFile,
    OwnAudioRef,
    UrlBackground,
    parse_model,
)
from app.portability.problems import MAX_PROBLEMS, from_validation_error, join, problem
from app.portability.reader import OpenedArchive
from app.schemas.content import (
    MAX_MAP_CONTENT_BYTES,
    parse_content,
    stored_size,
    to_stored,
)
from app.schemas.map_settings import MapSettings
from app.schemas.skin import skin_config_adapter
from app.schemas.view3d import View3d, View3dBinding
from app.services.asset_service import AssetSettings
from app.view3d import scenes
from app.view3d.validation import check_view3d

_NAMESPACE = uuid.UUID("5d3e2f4a-0c1b-4b6a-9c53-7a1f0e2d9b88")
_IMAGE_KINDS = (AssetKind.IMAGE, AssetKind.BACKGROUND)


def stand_in(*parts: str) -> uuid.UUID:
    """A stable id for something that only has a ref, so the API's validators,
    which want ids, can look at it."""
    return uuid.uuid5(_NAMESPACE, ":".join(parts))


@dataclass(frozen=True, slots=True)
class CheckedMap:
    archive: ArchiveMap
    # The map's settings as the API reads them (never any audio: that is `audio`).
    settings: MapSettings
    audio: ArchiveAudio | None


@dataclass(frozen=True, slots=True)
class CheckedArchive:
    manifest: Manifest
    maps: list[CheckedMap]
    assets: dict[str, ArchiveAsset]
    skins: dict[str, ArchiveSkin]


class _Problems:
    def __init__(self) -> None:
        self.found: list[dict[str, str]] = []

    def add(self, path: str, code: str, message: str) -> None:
        if len(self.found) < MAX_PROBLEMS:
            self.found.append(problem(path, code, message))

    def extend(self, problems: list[dict[str, str]]) -> None:
        for item in problems:
            self.add(item["path"], item["code"], item["message"])

    def raise_if_any(self) -> None:
        if self.found:
            raise ArchiveInvalidError(self.found)


# --- swapping refs for stand-in ids -----------------------------------------------


def content_with_ids(
    content: dict[str, Any], resolve: Callable[[str], uuid.UUID | None]
) -> tuple[dict[str, Any], list[tuple[str, str, str]]]:
    """`content` with each image block's `assetRef` turned into the `assetId` the
    API's content model wants. Returns it and the `(path, code, message)` of the
    refs that name nothing. A block that carries an `assetId` of its own is a
    database id in an archive: refused."""
    found: list[tuple[str, str, str]] = []
    body = content.get("body")
    blocks = body.get("blocks") if isinstance(body, dict) else None
    if not isinstance(body, dict) or not isinstance(blocks, list):
        return content, found
    new_blocks: list[Any] = []
    for index, block in enumerate(blocks):
        if isinstance(block, dict) and block.get("type") == "image":
            block = dict(block)
            where = f"body.blocks[{index}]"
            if "assetId" in block:
                del block["assetId"]
                found.append(
                    (f"{where}.assetId", "asset_id_not_allowed", "Ids are not allowed")
                )
            ref = block.pop("assetRef", None)
            resolved = resolve(ref) if isinstance(ref, str) else None
            if resolved is None:
                found.append(
                    (f"{where}.assetRef", "asset_ref_unknown", "Names no image")
                )
            else:
                block["assetId"] = str(resolved)
        new_blocks.append(block)
    return {**content, "body": {**body, "blocks": new_blocks}}, found


def skin_config_with_ids(
    config: dict[str, Any], resolve: Callable[[str], uuid.UUID | None]
) -> tuple[dict[str, Any], list[tuple[str, str, str]]]:
    """The same for an image skin's `states`."""
    found: list[tuple[str, str, str]] = []
    states = config.get("states")
    if config.get("kind") != "image" or not isinstance(states, dict):
        return config, found
    new_states: dict[str, Any] = {}
    for state, ref in states.items():
        resolved = resolve(ref) if isinstance(ref, str) else None
        if resolved is None:
            found.append(
                (f"states.{state}", "asset_ref_unknown", "Names no bubble image")
            )
        else:
            new_states[state] = str(resolved)
    return {**config, "states": new_states}, found


def parse_stored_content(raw: dict[str, Any]) -> dict[str, Any]:
    """The stored form of content already given ids (`parse_content`, then
    `to_stored`): what the database holds."""
    return to_stored(parse_content(raw))


# --- the archive ------------------------------------------------------------------


def check_archive(maps_file: MapsFile, manifest: Manifest) -> CheckedArchive:
    """Raises `ArchiveInvalidError` listing everything that is wrong."""
    found = _Problems()
    assets = _unique(found, "assets", maps_file.assets)
    skins = _unique(found, "skins", maps_file.skins)
    _unique(found, "maps", maps_file.maps)

    listed = {f.path: f for f in manifest.files}
    for index, asset in enumerate(maps_file.assets):
        entry = listed.get(asset.file)
        if entry is None or entry.sha256 != asset.sha256 or entry.size != asset.size:
            found.add(
                f"assets[{index}]",
                "asset_mismatch",
                "The asset is not the file the manifest lists",
            )
    if {p for p in listed if p.startswith("assets/")} != {
        a.file for a in maps_file.assets
    }:
        found.add("assets", "asset_files_mismatch", "The assets and the files differ")
    bubbles = sum(len(m.bubbles) for m in maps_file.maps)
    counts = manifest.counts
    if (counts.maps, counts.bubbles, counts.assets) != (
        len(maps_file.maps),
        bubbles,
        len(maps_file.assets),
    ):
        found.add("manifest.counts", "counts_mismatch", "The counts are not true")

    used_assets: set[str] = set()
    used_skins: set[str] = set()
    for index, skin in enumerate(maps_file.skins):
        _check_skin(found, f"skins[{index}]", skin, assets, used_assets)
    checked: list[CheckedMap] = []
    for index, archive_map in enumerate(maps_file.maps):
        checked.append(
            _check_map(
                found,
                f"maps[{index}]",
                archive_map,
                assets,
                skins,
                used_assets,
                used_skins,
            )
        )
    for index, asset in enumerate(maps_file.assets):
        if asset.ref not in used_assets:
            found.add(f"assets[{index}]", "asset_unused", "Nothing uses this asset")
    for index, skin in enumerate(maps_file.skins):
        if skin.ref not in used_skins:
            found.add(f"skins[{index}]", "skin_unused", "Nothing uses this skin")
    found.raise_if_any()
    return CheckedArchive(manifest, checked, assets, skins)


def _unique[T: (ArchiveAsset, ArchiveSkin, ArchiveMap)](
    found: _Problems, where: str, items: list[T]
) -> dict[str, T]:
    by_ref: dict[str, T] = {}
    for index, item in enumerate(items):
        if item.ref in by_ref:
            found.add(f"{where}[{index}].ref", "duplicate_ref", "A ref is used twice")
        else:
            by_ref[item.ref] = item
    return by_ref


def _check_skin(
    found: _Problems,
    where: str,
    skin: ArchiveSkin,
    assets: dict[str, ArchiveAsset],
    used_assets: set[str],
) -> None:
    def resolve(ref: str) -> uuid.UUID | None:
        return stand_in("asset", ref) if ref in assets else None

    config, missing = skin_config_with_ids(skin.config, resolve)
    for path, code, message in missing:
        found.add(join(f"{where}.config", path), code, message)
    try:
        parsed = skin_config_adapter.validate_python(
            config, by_alias=True, by_name=False
        )
    except ValidationError as exc:
        found.extend(from_validation_error(exc, prefix=f"{where}.config"))
        return
    if parsed.kind != "image":
        return
    states = {
        name: ref for name, ref in skin.config["states"].items() if isinstance(ref, str)
    }
    sizes: set[tuple[int | None, int | None]] = set()
    for name, ref in states.items():
        asset = assets.get(ref)
        if asset is None:
            continue
        used_assets.add(ref)
        if asset.kind is not AssetKind.BUBBLE:
            found.add(
                f"{where}.config.states.{name}",
                "asset_wrong_kind",
                "Skin states must use bubble images",
            )
        sizes.add((asset.width, asset.height))
    if len(sizes) > 1:
        found.add(
            f"{where}.config.states",
            "skin_asset_size_mismatch",
            "All state images of a skin must have the same width and height",
        )


def _check_map(
    found: _Problems,
    where: str,
    archive_map: ArchiveMap,
    assets: dict[str, ArchiveAsset],
    skins: dict[str, ArchiveSkin],
    used_assets: set[str],
    used_skins: set[str],
) -> CheckedMap:
    def skin_ref(path: str, ref: CustomSkinRef | BuiltinSkinRef | None) -> None:
        if isinstance(ref, CustomSkinRef):
            if ref.ref in skins:
                used_skins.add(ref.ref)
            else:
                found.add(path, "skin_ref_unknown", "Names no skin of the archive")

    settings, audio = _check_settings(found, where, archive_map, assets, used_assets)

    background = archive_map.background
    if isinstance(background, AssetBackground):
        asset = assets.get(background.asset_ref)
        if asset is None:
            found.add(f"{where}.background", "asset_ref_unknown", "Names no asset")
        else:
            used_assets.add(asset.ref)
            if asset.kind is not AssetKind.BACKGROUND:
                found.add(
                    f"{where}.background", "asset_wrong_kind", "Not a background image"
                )
    elif isinstance(background, UrlBackground) and (
        asset_id_of_image_url(background.image_url) is not None
    ):
        found.add(
            f"{where}.background.imageUrl",
            "asset_id_not_allowed",
            "Ids are not allowed",
        )

    skin_ref(f"{where}.defaultSkin", archive_map.default_skin)
    seen: set[str] = set()
    for index, rule in enumerate(archive_map.skin_rules):
        if rule.modname in seen:
            found.add(
                f"{where}.skinRules[{index}].modname",
                "duplicate_skin_rule",
                "An activity type can have only one skin rule",
            )
        seen.add(rule.modname)
        skin_ref(f"{where}.skinRules[{index}].skin", rule.skin)

    budget = 0
    refs: set[str] = set()
    for index, bubble in enumerate(archive_map.bubbles):
        path = f"{where}.bubbles[{index}]"
        if bubble.ref in refs:
            found.add(f"{path}.ref", "duplicate_ref", "A ref is used twice")
        refs.add(bubble.ref)
        skin_ref(f"{path}.skin", bubble.skin)
        if bubble.kind is BubbleKind.CONTENT:
            budget += _check_content(
                found, f"{path}.content", archive_map.ref, bubble, assets, used_assets
            )
    if budget > MAX_MAP_CONTENT_BYTES:
        found.add(
            f"{where}.bubbles",
            "content_too_large",
            "The content of the map is too large",
        )
    _check_view3d(found, where, archive_map, refs)
    return CheckedMap(archive_map, settings, audio)


def _check_settings(
    found: _Problems,
    where: str,
    archive_map: ArchiveMap,
    assets: dict[str, ArchiveAsset],
    used_assets: set[str],
) -> tuple[MapSettings, ArchiveAudio | None]:
    raw = dict(archive_map.settings)
    has_audio = "audio" in raw
    audio_raw = raw.pop("audio", None)
    try:
        settings = MapSettings.model_validate(raw, by_alias=True, by_name=False)
    except ValidationError as exc:
        found.extend(from_validation_error(exc, prefix=f"{where}.settings"))
        settings = MapSettings()
    audio: ArchiveAudio | None = None
    if has_audio:
        try:
            audio = parse_model(ArchiveAudio, audio_raw)
        except ValidationError as exc:
            found.extend(from_validation_error(exc, prefix=f"{where}.settings.audio"))
        else:
            if isinstance(audio.audio_ref, OwnAudioRef):
                asset = assets.get(audio.audio_ref.asset_ref)
                path = f"{where}.settings.audio.audioRef"
                if asset is None:
                    found.add(path, "asset_ref_unknown", "Names no asset")
                else:
                    used_assets.add(asset.ref)
                    if asset.kind is not AssetKind.AUDIO:
                        found.add(path, "asset_wrong_kind", "Not an audio track")
    return settings, audio


def _check_content(
    found: _Problems,
    where: str,
    map_ref: str,
    bubble: ArchiveBubble,
    assets: dict[str, ArchiveAsset],
    used_assets: set[str],
) -> int:
    """Checks one content bubble; returns the bytes it counts for in its map."""
    assert bubble.content is not None

    def resolve(ref: str) -> uuid.UUID | None:
        asset = assets.get(ref)
        if asset is None:
            return None
        used_assets.add(ref)
        if asset.kind not in _IMAGE_KINDS:
            found.add(where, "asset_wrong_kind", "The asset is not an image")
        return stand_in("asset", ref)

    content, missing = content_with_ids(bubble.content, resolve)
    for path, code, message in missing:
        found.add(join(where, path), code, message)
    try:
        return stored_size(parse_stored_content(content))
    except ContentInvalidError as exc:
        found.extend(
            [
                problem(
                    join(where, p["path"]) if p["path"] else where,
                    p["code"],
                    p["message"],
                )
                for p in exc.problems
            ]
        )
    except ContentTooLargeError:
        found.add(where, "content_too_large", "The content is too large")
    return 0


def _check_view3d(
    found: _Problems, where: str, archive_map: ArchiveMap, bubble_refs: set[str]
) -> None:
    view = archive_map.view3d
    if view is None:
        return
    path = f"{where}.view3d"
    unknown = False
    for index, binding in enumerate(view.bindings):
        if binding.bubble_ref not in bubble_refs:
            unknown = True
            found.add(
                f"{path}.bindings[{index}].bubbleRef",
                "bubble_ref_unknown",
                "Names no bubble of the map",
            )
    if unknown or scenes.zone_count(view.scene) is None:
        return  # an unknown scene is a warning, not an error
    stand = View3d(
        scene=view.scene,
        bindings=[
            View3dBinding(
                zone_id=b.zone_id, bubble_id=stand_in(archive_map.ref, b.bubble_ref)
            )
            for b in view.bindings
        ],
    )
    try:
        check_view3d(stand, [stand_in(archive_map.ref, r) for r in bubble_refs])
    except View3dInvalidError as exc:
        found.add(path, exc.code, exc.message)


# --- the assets' bytes ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProcessedAsset:
    """An asset as it would be stored: re-encoded, so nothing hidden in the
    original comes along."""

    ref: str
    kind: AssetKind
    mime: str
    data: bytes
    thumbnail: bytes | None
    width: int | None
    height: int | None
    duration_seconds: float | None
    sha256: str


def process_archive_asset(
    opened: OpenedArchive, asset: ArchiveAsset, settings: AssetSettings
) -> ProcessedAsset:
    """Reads, decodes and re-encodes one asset with the pipeline of uploads. It
    stores nothing. Raises `ArchiveInvalidError` for any asset the API would
    have refused, or whose description lies about it."""
    import hashlib

    where = f"assets[{asset.ref}]"

    def refuse(code: str, message: str) -> ArchiveInvalidError:
        return ArchiveInvalidError([problem(where, code, message)])

    if asset.size > settings.max_bytes_for(asset.kind):
        raise refuse("asset_too_large", "The file is too large")
    data = opened.read_asset(asset.file)
    try:
        if asset.kind is AssetKind.AUDIO:
            audio = process_audio(io.BytesIO(data), settings.max_audio_seconds)
            processed = ProcessedAsset(
                asset.ref,
                asset.kind,
                audio.mime,
                audio.data,
                None,
                None,
                None,
                audio.duration_seconds,
                hashlib.sha256(audio.data).hexdigest(),
            )
        else:
            image = process_image(
                io.BytesIO(data),
                asset.kind,
                ImageLimits(
                    settings.max_background_side,
                    settings.max_bubble_side,
                    settings.max_image_side,
                ),
            )
            processed = ProcessedAsset(
                asset.ref,
                asset.kind,
                image.mime,
                image.data,
                image.thumbnail,
                image.width,
                image.height,
                None,
                hashlib.sha256(image.data).hexdigest(),
            )
    except (AssetTypeNotAllowedError, AssetInvalidImageError):
        raise refuse("asset_invalid", "The file is not a valid image") from None
    except AssetDimensionsTooLargeError:
        raise refuse("asset_dimensions_too_large", "The image is too large") from None
    except (AudioInvalidError, AudioTooLongError):
        raise refuse("asset_invalid", "The file is not a valid audio track") from None
    if processed.mime != asset.mime or (
        asset.kind is not AssetKind.AUDIO
        and (processed.width, processed.height) != (asset.width, asset.height)
    ):
        raise refuse("asset_mismatch", "The file is not what the archive says")
    return processed
