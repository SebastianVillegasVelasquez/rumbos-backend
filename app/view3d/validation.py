from collections import Counter
from collections.abc import Collection
from uuid import UUID

from app.exceptions import View3dInvalidError
from app.schemas.view3d import View3d
from app.view3d import scenes


def check_view3d(view3d: View3d, map_bubble_ids: Collection[UUID]) -> None:
    """Raises `View3dInvalidError` unless `view3d` is storable on a map whose
    bubbles are `map_bubble_ids`. Checks run in the order of the contract's
    codes and the first failure is the one reported."""
    count = scenes.zone_count(view3d.scene)
    if count is None:
        raise View3dInvalidError(
            "scene_unknown",
            "The 3D scene is not one of the available scenes",
            scene=view3d.scene,
            scenes=scenes.known_scenes(),
        )

    bindings = view3d.bindings
    out_of_range = sorted({b.zone_id for b in bindings if not 0 <= b.zone_id < count})
    if out_of_range:
        raise View3dInvalidError(
            "zone_out_of_range",
            f"A zone must be between 0 and {count - 1} for this scene",
            zoneIds=out_of_range,
            zoneCount=count,
        )

    zones = Counter(b.zone_id for b in bindings)
    repeated_zones = sorted(z for z, n in zones.items() if n > 1)
    if repeated_zones:
        raise View3dInvalidError(
            "duplicate_zone",
            "A zone can have only one bubble",
            zoneIds=repeated_zones,
        )

    bubbles = Counter(b.bubble_id for b in bindings)
    repeated_bubbles = sorted((i for i, n in bubbles.items() if n > 1), key=str)
    if repeated_bubbles:
        raise View3dInvalidError(
            "duplicate_bubble",
            "A bubble can be bound to only one zone",
            bubbleIds=[str(i) for i in repeated_bubbles],
        )

    known = set(map_bubble_ids)
    foreign = sorted(
        (b.bubble_id for b in bindings if b.bubble_id not in known), key=str
    )
    if foreign:
        raise View3dInvalidError(
            "bubble_not_in_map",
            "A bound bubble does not belong to this map",
            bubbleIds=[str(i) for i in foreign],
        )
