"""The 3D scenes a map can use.

A scene is a hand-authored world that ships with the frontend (its textures and
sprites are static frontend files, not served here), so what the backend needs
to know about one is only how many zones it has. Adding a scene is a code change
deployed together with the frontend; it needs no migration, because maps store
just the scene's id.
"""

from collections.abc import Mapping
from types import MappingProxyType

# scene id -> number of zones (`zoneId` is `0 <= zoneId < zoneCount`).
SCENES: Mapping[str, int] = MappingProxyType({"region-encuentros": 9})


def zone_count(scene: str) -> int | None:
    """The scene's number of zones, or None if there is no such scene."""
    return SCENES.get(scene)


def known_scenes() -> list[str]:
    return sorted(SCENES)
