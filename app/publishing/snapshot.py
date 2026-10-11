"""Snapshots of a map's published content: build, hash, diff.

Pure functions, no database and no I/O. A snapshot is a plain JSON-able dict
(schemaVersion 1) holding everything a student needs to draw the map EXCEPT
live data: bubble `status`, level `position`, `moodleCourseId` and
`moodleSectionId` are always read from the live rows.

Two snapshots are "the same" exactly when their `content_hash` is: the hash is
SHA-256 of a canonical JSON text (sorted keys, no whitespace, normalized
numbers), so key order and float spelling (`1` vs `1.0`, `0.1` vs
`0.10000000000000001`) never change it.
"""

import hashlib
import json
import math
import uuid
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any

from app.assets.references import skin_asset_ids, stored_asset_id
from app.schemas.bubble import BubbleRead
from app.schemas.course_map import SkinRule
from app.schemas.map_settings import MapSettings
from app.schemas.skin import SkinRead, dump_config

SCHEMA_VERSION = 1
# Significant digits kept for floats. Coordinates are 0-1 doubles; twelve digits
# is far finer than any screen and makes the text independent of float noise.
_FLOAT_DIGITS = 12


@dataclass(frozen=True, slots=True)
class DraftInputs:
    """Everything read from the draft that goes into a snapshot."""

    title: str
    image_url: str
    settings: MapSettings
    default_skin_id: uuid.UUID | None
    skin_rules: list[SkinRule]
    bubbles: list[BubbleRead]
    # Skins that exist and may be referenced: the ones the draft names plus the
    # application's default skin. Anything else is ignored.
    skins: Mapping[uuid.UUID, SkinRead]


def normalize_numbers(value: Any) -> Any:
    """`value` with every float made canonical.

    Integral floats become ints (`1.0` -> `1`); others keep 12 significant
    digits. Booleans are left alone (they are ints in Python but not in JSON).
    Raises `ValueError` on NaN or infinity, which JSON cannot carry.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("snapshots cannot contain NaN or infinity")
        if value.is_integer():
            return int(value)
        return float(f"{value:.{_FLOAT_DIGITS}g}")
    if isinstance(value, Mapping):
        return {str(k): normalize_numbers(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [normalize_numbers(v) for v in value]
    return value


def canonical_json(content: Mapping[str, Any]) -> str:
    return json.dumps(
        normalize_numbers(content),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def content_hash(content: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(content).encode("utf-8")).hexdigest()


def referenced_skin_ids(inputs: DraftInputs) -> set[uuid.UUID]:
    """Skins the draft itself points at (not counting the application default)."""
    ids = {rule.skin_id for rule in inputs.skin_rules}
    if inputs.default_skin_id is not None:
        ids.add(inputs.default_skin_id)
    ids.update(b.skin_id for b in inputs.bubbles if b.skin_id is not None)
    return ids


def build_snapshot(inputs: DraftInputs) -> dict[str, Any]:
    """The snapshot content for a draft, canonical and ready to store or hash.

    The skins are FROZEN copies: the ones the draft references plus the
    application's default skin (the fallback when nothing else applies), so a
    later edit or deletion of a skin cannot change what was published.
    """
    wanted = referenced_skin_ids(inputs) | {
        skin.id for skin in inputs.skins.values() if skin.is_default
    }
    skins = {
        str(skin_id): {
            "name": skin.name,
            "isDefault": skin.is_default,
            "config": dump_config(skin.config),
        }
        for skin_id, skin in sorted(inputs.skins.items(), key=lambda kv: str(kv[0]))
        if skin_id in wanted
    }
    # The guided path order is the draft's: (sequence, created_at, id). The
    # snapshot stores the resulting rank 0..n-1, so ties or gaps in the draft
    # never show up as a difference.
    ordered = sorted(inputs.bubbles, key=lambda b: (b.sequence, b.created_at, b.id))
    snapshot = {
        "schemaVersion": SCHEMA_VERSION,
        "title": inputs.title,
        "imageUrl": inputs.image_url,
        "settings": inputs.settings.to_stored(),
        "defaultSkinId": (
            str(inputs.default_skin_id) if inputs.default_skin_id else None
        ),
        "skinRules": [
            {"modname": rule.modname, "skinId": str(rule.skin_id)}
            for rule in sorted(inputs.skin_rules, key=lambda r: r.modname)
        ],
        "skins": skins,
        "bubbles": [
            {
                "id": str(bubble.id),
                "activityId": bubble.activity_id,
                "x": bubble.x,
                "y": bubble.y,
                "icon": bubble.icon.value if bubble.icon else None,
                "skinId": str(bubble.skin_id) if bubble.skin_id else None,
                "sequence": rank,
            }
            for rank, bubble in enumerate(ordered)
        ],
    }
    normalized: dict[str, Any] = normalize_numbers(snapshot)
    return normalized


def collect_asset_ids(snapshot: Mapping[str, Any]) -> list[uuid.UUID]:
    """Assets a snapshot depends on: its background and every image of its
    frozen skins. Sorted and without repeats, so it is a stable set."""
    found: set[uuid.UUID] = set()
    background = stored_asset_id(snapshot["imageUrl"])
    if background is not None:
        found.add(background)
    for skin in snapshot["skins"].values():
        found.update(skin_asset_ids(skin["config"]))
    return sorted(found, key=str)


def snapshot_skin_ids(snapshot: Mapping[str, Any]) -> set[uuid.UUID]:
    """Skin ids the snapshot's default, rules and bubbles point at."""
    ids = {uuid.UUID(rule["skinId"]) for rule in snapshot["skinRules"]}
    if snapshot["defaultSkinId"]:
        ids.add(uuid.UUID(snapshot["defaultSkinId"]))
    ids.update(
        uuid.UUID(b["skinId"]) for b in snapshot["bubbles"] if b["skinId"] is not None
    )
    return ids


@dataclass(frozen=True, slots=True)
class Changes:
    """What differs between two snapshots, as the editor summarizes it."""

    background_changed: bool = False
    title_changed: bool = False
    settings_changed: bool = False
    skins_changed: bool = False
    bubbles_added: int = 0
    bubbles_removed: int = 0
    bubbles_moved: int = 0
    bubbles_restyled: int = 0
    # Same bubbles, different guided-path order (an additive field).
    bubbles_reordered: bool = False


def diff_snapshots(old: Mapping[str, Any], new: Mapping[str, Any]) -> Changes:
    """`new` against `old`. Bubbles are matched by id: added/removed by
    presence, moved when x or y differ, restyled when skinId or icon differ (a
    bubble can be both)."""
    old_bubbles = {b["id"]: b for b in old["bubbles"]}
    new_bubbles = {b["id"]: b for b in new["bubbles"]}
    common = old_bubbles.keys() & new_bubbles.keys()
    moved = sum(
        1
        for i in common
        if (old_bubbles[i]["x"], old_bubbles[i]["y"])
        != (new_bubbles[i]["x"], new_bubbles[i]["y"])
    )
    restyled = sum(
        1
        for i in common
        if (old_bubbles[i]["skinId"], old_bubbles[i]["icon"])
        != (new_bubbles[i]["skinId"], new_bubbles[i]["icon"])
    )
    old_order = [b["id"] for b in old["bubbles"] if b["id"] in common]
    new_order = [b["id"] for b in new["bubbles"] if b["id"] in common]
    return Changes(
        background_changed=old["imageUrl"] != new["imageUrl"],
        title_changed=old["title"] != new["title"],
        settings_changed=old["settings"] != new["settings"],
        skins_changed=(
            old["defaultSkinId"] != new["defaultSkinId"]
            or old["skinRules"] != new["skinRules"]
            or old["skins"] != new["skins"]
        ),
        bubbles_added=len(new_bubbles.keys() - old_bubbles.keys()),
        bubbles_removed=len(old_bubbles.keys() - new_bubbles.keys()),
        bubbles_moved=moved,
        bubbles_restyled=restyled,
        bubbles_reordered=old_order != new_order,
    )


def bubble_ids(snapshot: Mapping[str, Any]) -> Collection[uuid.UUID]:
    return [uuid.UUID(b["id"]) for b in snapshot["bubbles"]]
