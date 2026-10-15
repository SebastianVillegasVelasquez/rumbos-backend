"""The snapshot builder: determinism, skin freezing, asset ids, diffing. No database."""

import copy
import dataclasses
import json
import math
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from uuid_utils.compat import uuid7

from app.enums import BubbleIcon, BubbleStatus
from app.publishing.snapshot import (
    Changes,
    DraftInputs,
    build_snapshot,
    canonical_json,
    collect_asset_ids,
    content_hash,
    diff_snapshots,
    normalize_numbers,
)
from app.schemas.bubble import BubbleRead
from app.schemas.course_map import SkinRule
from app.schemas.map_settings import MapSettings
from app.schemas.skin import SkinRead, skin_config_adapter
from app.schemas.view3d import LocalizedText, View3d, View3dBinding
from tests import skin_configs

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def make_skin(
    name: str = "Skin",
    *,
    is_default: bool = False,
    config: dict[str, Any] | None = None,
) -> SkinRead:
    return SkinRead(
        id=uuid7(),
        name=name,
        builtin=False,
        is_default=is_default,
        config=skin_config_adapter.validate_python(config or skin_configs.procedural()),
        created_at=T0,
        updated_at=T0,
    )


def make_bubble(
    activity: int,
    *,
    sequence: int = 0,
    x: float = 0.25,
    y: float = 0.75,
    icon: BubbleIcon | None = None,
    skin_id: uuid.UUID | None = None,
    bubble_id: uuid.UUID | None = None,
    created: datetime = T0,
) -> BubbleRead:
    return BubbleRead(
        id=bubble_id or uuid7(),
        course_map_id=uuid.UUID(int=1),
        activity_id=activity,
        x=x,
        y=y,
        icon=icon,
        status=BubbleStatus.COMPLETE,  # live data: must never reach a snapshot
        skin_id=skin_id,
        sequence=sequence,
        created_at=created,
        updated_at=created,
    )


def make_inputs(
    *,
    bubbles: list[BubbleRead] | None = None,
    skins: list[SkinRead] | None = None,
    default_skin_id: uuid.UUID | None = None,
    rules: list[SkinRule] | None = None,
    title: str = "Mapa",
    image_url: str = "https://i/x.png",
    settings: MapSettings | None = None,
) -> DraftInputs:
    return DraftInputs(
        title=title,
        image_url=image_url,
        settings=settings or MapSettings(),
        default_skin_id=default_skin_id,
        skin_rules=rules or [],
        bubbles=bubbles or [],
        skins={s.id: s for s in skins or []},
    )


# --- content -----------------------------------------------------------------


def test_the_snapshot_has_exactly_the_contract_keys_and_no_live_data() -> None:
    bubble = make_bubble(7, icon=BubbleIcon.STAR)
    snapshot = build_snapshot(make_inputs(bubbles=[bubble]))

    assert set(snapshot) == {
        "schemaVersion",
        "title",
        "imageUrl",
        "settings",
        "defaultSkinId",
        "skinRules",
        "skins",
        "bubbles",
    }
    assert snapshot["schemaVersion"] == 1
    assert snapshot["bubbles"] == [
        {
            "id": str(bubble.id),
            "activityId": 7,
            "x": 0.25,
            "y": 0.75,
            "icon": "star",
            "skinId": None,
            "sequence": 0,
        }
    ]
    assert "status" not in json.dumps(snapshot)  # bubble status is live


def test_settings_are_stored_in_their_full_normalized_form() -> None:
    explicit = build_snapshot(make_inputs(settings=MapSettings()))
    from_empty = build_snapshot(make_inputs(settings=MapSettings.model_validate({})))

    assert explicit == from_empty
    assert explicit["settings"]["mode"] == "explorative"


def test_bubbles_are_ordered_by_the_guided_path_and_get_dense_sequences() -> None:
    a = make_bubble(1, sequence=5, created=T0)
    b = make_bubble(2, sequence=5, created=T0 + timedelta(seconds=1))  # tie with a
    c = make_bubble(3, sequence=2)

    snapshot = build_snapshot(make_inputs(bubbles=[b, a, c]))

    assert [x["id"] for x in snapshot["bubbles"]] == [str(c.id), str(a.id), str(b.id)]
    assert [x["sequence"] for x in snapshot["bubbles"]] == [0, 1, 2]


# --- determinism -----------------------------------------------------------------


def test_the_hash_does_not_depend_on_input_order() -> None:
    skin = make_skin()
    bubbles = [make_bubble(i, sequence=i, skin_id=skin.id) for i in range(5)]
    rules = [SkinRule(modname=m, skin_id=skin.id) for m in ("quiz", "scorm", "url")]

    one = make_inputs(bubbles=bubbles, skins=[skin], rules=rules)
    two = make_inputs(
        bubbles=list(reversed(bubbles)), skins=[skin], rules=list(reversed(rules))
    )

    assert content_hash(build_snapshot(one)) == content_hash(build_snapshot(two))


def test_the_hash_is_stable_across_key_order_and_float_spelling() -> None:
    base = build_snapshot(make_inputs(bubbles=[make_bubble(1, x=0.5, y=1.0)]))
    reordered = dict(reversed(list(base.items())))
    respelled = copy.deepcopy(base)
    respelled["bubbles"][0]["x"] = 0.50000000000000001  # same double
    respelled["bubbles"][0]["y"] = 1  # int instead of 1.0
    noisy = copy.deepcopy(base)
    noisy["bubbles"][0]["x"] = 0.5 + 1e-15  # below the 12 digits kept

    digests = {content_hash(s) for s in (base, reordered, respelled, noisy)}

    assert len(digests) == 1


def test_the_hash_survives_a_json_round_trip() -> None:
    skin = make_skin(is_default=True)
    snapshot = build_snapshot(
        make_inputs(
            bubbles=[make_bubble(1, x=0.1, y=0.30000000000000004)], skins=[skin]
        )
    )

    reloaded = json.loads(json.dumps(snapshot))

    assert content_hash(reloaded) == content_hash(snapshot)


def test_canonical_json_is_compact_sorted_and_unicode() -> None:
    assert (
        canonical_json({"b": 1.0, "a": [True, None, "ñ"]})
        == '{"a":[true,null,"ñ"],"b":1}'
    )


def test_normalize_numbers_rules() -> None:
    assert normalize_numbers(1.0) == 1 and isinstance(normalize_numbers(1.0), int)
    assert normalize_numbers(True) is True  # a bool is not a number here
    assert normalize_numbers({"a": [2.0, 0.1]}) == {"a": [2, 0.1]}
    for bad in (math.nan, math.inf, -math.inf):
        with pytest.raises(ValueError):
            normalize_numbers(bad)


BUBBLE_ID = uuid.UUID(int=99)


@pytest.mark.parametrize(
    "changes",
    [
        {"title": "Otro"},
        {"image_url": "https://i/y.png"},
        {"settings": MapSettings(mode="guided")},
        {"bubbles": [make_bubble(1, x=0.3, bubble_id=BUBBLE_ID)]},
        {"bubbles": [make_bubble(1, y=0.3, bubble_id=BUBBLE_ID)]},
        {"bubbles": [make_bubble(1, icon=BubbleIcon.FLAG, bubble_id=BUBBLE_ID)]},
        {"bubbles": [make_bubble(2, bubble_id=BUBBLE_ID)]},
    ],
)
def test_any_content_change_changes_the_hash(changes: dict[str, Any]) -> None:
    base = make_inputs(bubbles=[make_bubble(1, bubble_id=BUBBLE_ID)])

    changed = make_inputs(**{"bubbles": base.bubbles, **changes})

    assert content_hash(build_snapshot(base)) != content_hash(build_snapshot(changed))


# --- skins ---------------------------------------------------------------------------


def test_skins_are_frozen_copies_of_those_referenced_plus_the_default() -> None:
    default = make_skin("Default", is_default=True)
    on_map = make_skin("En el mapa")
    on_rule = make_skin("Por regla")
    on_bubble = make_skin("En burbuja")
    unrelated = make_skin("Ajena")
    inputs = make_inputs(
        skins=[default, on_map, on_rule, on_bubble, unrelated],
        default_skin_id=on_map.id,
        rules=[SkinRule(modname="quiz", skin_id=on_rule.id)],
        bubbles=[make_bubble(1, skin_id=on_bubble.id)],
    )

    snapshot = build_snapshot(inputs)

    assert set(snapshot["skins"]) == {
        str(s.id) for s in (default, on_map, on_rule, on_bubble)
    }
    frozen = snapshot["skins"][str(on_map.id)]
    assert frozen == {
        "name": "En el mapa",
        "isDefault": False,
        "config": skin_configs.procedural(),
    }
    assert snapshot["skins"][str(default.id)]["isDefault"] is True
    assert snapshot["defaultSkinId"] == str(on_map.id)
    assert snapshot["skinRules"] == [{"modname": "quiz", "skinId": str(on_rule.id)}]


def test_a_skin_edit_changes_the_hash_of_maps_that_use_it() -> None:
    skin = make_skin("Antes")
    edited = skin.model_copy(update={"name": "Después"})

    before = content_hash(
        build_snapshot(make_inputs(skins=[skin], default_skin_id=skin.id))
    )
    after = content_hash(
        build_snapshot(make_inputs(skins=[edited], default_skin_id=skin.id))
    )

    assert before != after


# --- asset ids -------------------------------------------------------------------------


def test_asset_ids_are_the_background_plus_every_image_of_the_frozen_skins() -> None:
    background, a, b, c = (uuid7() for _ in range(4))
    image_skin = make_skin(config=skin_configs.image(a, locked=b, complete=a))
    other_skin = make_skin(config=skin_configs.image(c))
    unused_skin = make_skin(config=skin_configs.image(uuid7()))
    snapshot = build_snapshot(
        make_inputs(
            image_url=f"/api/v1/assets/{background}",
            skins=[image_skin, other_skin, unused_skin, make_skin()],
            default_skin_id=image_skin.id,
            rules=[SkinRule(modname="quiz", skin_id=other_skin.id)],
        )
    )

    assert collect_asset_ids(snapshot) == sorted({background, a, b, c}, key=str)


@pytest.mark.parametrize(
    "image_url",
    [
        "https://example.com/a.png",
        "/uploads/x.png",
        "/api/v1/assets/not-a-uuid",
        "/api/v1/assets/0192F3A0-0000-7000-8000-000000000000",  # not the canonical spelling
        "/api/v1/assets/0192f3a0-0000-7000-8000-000000000000/thumb",
    ],
)
def test_only_a_canonical_asset_url_counts_as_a_background_asset(
    image_url: str,
) -> None:
    snapshot = build_snapshot(make_inputs(image_url=image_url))

    assert collect_asset_ids(snapshot) == []


def test_procedural_skins_reference_no_assets() -> None:
    skin = make_skin()
    snapshot = build_snapshot(make_inputs(skins=[skin], default_skin_id=skin.id))

    assert collect_asset_ids(snapshot) == []


# --- diff ---------------------------------------------------------------------------------


def snapshot_of(**kwargs: Any) -> dict[str, Any]:
    return build_snapshot(make_inputs(**kwargs))


def test_identical_snapshots_have_no_changes() -> None:
    bubble = make_bubble(1)
    snap = snapshot_of(bubbles=[bubble])

    changes = diff_snapshots(snap, copy.deepcopy(snap))

    assert changes == diff_snapshots(snap, snap)
    assert not any(vars_of(changes).values())


def vars_of(changes: Any) -> dict[str, Any]:
    return {f: getattr(changes, f) for f in changes.__dataclass_fields__}


def test_the_diff_counts_bubbles_by_id() -> None:
    kept, moved, restyled, both, removed = (make_bubble(i) for i in range(1, 6))
    skin = make_skin()
    old = snapshot_of(bubbles=[kept, moved, restyled, both, removed], skins=[skin])
    new = snapshot_of(
        bubbles=[
            kept,
            moved.model_copy(update={"x": 0.9}),
            restyled.model_copy(update={"icon": BubbleIcon.TROPHY}),
            both.model_copy(update={"y": 0.1, "skin_id": skin.id}),
            make_bubble(6),  # added
        ],
        skins=[skin],
    )

    changes = diff_snapshots(old, new)

    assert (
        changes.bubbles_added,
        changes.bubbles_removed,
        changes.bubbles_moved,
        changes.bubbles_restyled,
    ) == (1, 1, 2, 2)
    assert not changes.background_changed and not changes.title_changed


def test_the_diff_flags_background_title_settings_and_skins() -> None:
    skin = make_skin()
    old = snapshot_of()
    assert diff_snapshots(old, snapshot_of(image_url="/x")).background_changed
    assert diff_snapshots(old, snapshot_of(title="Otro")).title_changed
    assert diff_snapshots(
        old, snapshot_of(settings=MapSettings(mode="guided"))
    ).settings_changed
    assert diff_snapshots(
        old, snapshot_of(skins=[skin], default_skin_id=skin.id)
    ).skins_changed
    assert diff_snapshots(
        old,
        snapshot_of(skins=[skin], rules=[SkinRule(modname="quiz", skin_id=skin.id)]),
    ).skins_changed


def test_a_reordered_path_is_reported_without_counting_as_moved() -> None:
    a, b, c = (make_bubble(i, sequence=i) for i in range(3))
    old = snapshot_of(bubbles=[a, b, c])
    new = snapshot_of(
        bubbles=[
            c.model_copy(update={"sequence": 0}),
            a.model_copy(update={"sequence": 1}),
            b.model_copy(update={"sequence": 2}),
        ]
    )

    changes = diff_snapshots(old, new)

    assert changes.bubbles_reordered
    assert (changes.bubbles_moved, changes.bubbles_added, changes.bubbles_removed) == (
        0,
        0,
        0,
    )


def test_adding_or_removing_a_bubble_is_not_a_reorder() -> None:
    a, b = make_bubble(1, sequence=0), make_bubble(2, sequence=1)
    old = snapshot_of(bubbles=[a, b])
    new = snapshot_of(bubbles=[a, b, make_bubble(3, sequence=2)])

    assert not diff_snapshots(old, new).bubbles_reordered
    assert not diff_snapshots(new, old).bubbles_reordered


# --- format stability ----------------------------------------------------------

# Computed once and never to be edited: it pins the canonical text of a snapshot
# as it was published before 3D views existed. If a change to the builder moves
# this hash, every map published so far would suddenly show "unpublished changes".
LEGACY_HASH = "4ec6eff5b462a0066f8ca623906b1e5726d867a75eb31d6515ff3021d00a9fb7"


def test_a_map_without_extras_hashes_exactly_as_it_always_did() -> None:
    skin = SkinRead(
        id=uuid.UUID(int=10),
        name="Skin",
        builtin=False,
        is_default=True,
        config=skin_config_adapter.validate_python(skin_configs.procedural()),
        created_at=T0,
        updated_at=T0,
    )
    bubble = make_bubble(7, icon=BubbleIcon.STAR, bubble_id=uuid.UUID(int=20))
    snapshot = build_snapshot(
        make_inputs(bubbles=[bubble], skins=[skin], default_skin_id=skin.id)
    )

    assert content_hash(snapshot) == LEGACY_HASH


# --- 3D view ---------------------------------------------------------------------


def make_view3d(
    *pairs: tuple[int, uuid.UUID], title: LocalizedText | None = None
) -> View3d:
    return View3d(
        scene="region-encuentros",
        bindings=[
            View3dBinding(zone_id=zone, bubble_id=bubble, title=title)
            for zone, bubble in pairs
        ],
    )


def test_a_map_without_a_3d_view_omits_the_key_instead_of_writing_null() -> None:
    snapshot = build_snapshot(make_inputs())

    assert "view3d" not in snapshot
    assert "view3d" not in canonical_json(snapshot)
    assert content_hash(snapshot) == content_hash(
        build_snapshot(make_inputs())  # and nothing else about it is different
    )


def test_a_3d_view_is_stored_with_its_bindings_ordered_by_zone() -> None:
    first, second = make_bubble(1), make_bubble(2)
    view = make_view3d(
        (7, second.id),
        (2, first.id),
        title=LocalizedText(es="Hola"),
    )
    inputs = make_inputs(bubbles=[first, second])
    snapshot = build_snapshot(with_view3d(inputs, view))

    assert snapshot["view3d"] == {
        "scene": "region-encuentros",
        "bindings": [
            {
                "zoneId": 2,
                "bubbleId": str(first.id),
                "title": {"es": "Hola"},
                "description": None,
            },
            {
                "zoneId": 7,
                "bubbleId": str(second.id),
                "title": {"es": "Hola"},
                "description": None,
            },
        ],
    }
    assert set(snapshot) - set(build_snapshot(inputs)) == {"view3d"}


def with_view3d(inputs: DraftInputs, view: View3d | None) -> DraftInputs:
    return dataclasses.replace(inputs, view3d=view)


def test_the_order_bindings_were_given_in_does_not_change_the_hash() -> None:
    a, b = make_bubble(1), make_bubble(2)
    base = make_inputs(bubbles=[a, b])

    one = with_view3d(base, make_view3d((1, a.id), (5, b.id)))
    other = with_view3d(base, make_view3d((5, b.id), (1, a.id)))

    assert content_hash(build_snapshot(one)) == content_hash(build_snapshot(other))


def test_any_change_to_the_3d_view_changes_the_hash() -> None:
    a, b = make_bubble(1), make_bubble(2)
    base = make_inputs(bubbles=[a, b])
    plain = content_hash(build_snapshot(base))
    variants = [
        make_view3d((1, a.id)),
        make_view3d((2, a.id)),  # another zone
        make_view3d((1, b.id)),  # another bubble
        make_view3d((1, a.id), (2, b.id)),
        make_view3d((1, a.id), title=LocalizedText(es="x")),
        make_view3d((1, a.id), title=LocalizedText(en="x")),
        View3d(scene="otra", bindings=[]),
        View3d(scene="region-encuentros", bindings=[]),
    ]

    hashes = {
        content_hash(build_snapshot(with_view3d(base, view))) for view in variants
    }

    assert len(hashes) == len(variants)  # all different from each other...
    assert plain not in hashes  # ...and from having no 3D view


def test_removing_the_3d_view_returns_to_the_hash_it_had_before() -> None:
    a = make_bubble(1)
    base = make_inputs(bubbles=[a])
    before = content_hash(build_snapshot(base))

    with_it = with_view3d(base, make_view3d((0, a.id)))
    without = with_view3d(with_it, None)

    assert content_hash(build_snapshot(with_it)) != before
    assert content_hash(build_snapshot(without)) == before


def test_the_3d_view_adds_no_assets() -> None:
    a = make_bubble(1)
    base = make_inputs(bubbles=[a])
    with_it = with_view3d(base, make_view3d((0, a.id)))

    assert collect_asset_ids(build_snapshot(with_it)) == collect_asset_ids(
        build_snapshot(base)
    )


def test_the_diff_reports_a_changed_3d_view() -> None:
    a, b = make_bubble(1), make_bubble(2)
    base = make_inputs(bubbles=[a, b])
    plain = build_snapshot(base)
    one = build_snapshot(with_view3d(base, make_view3d((1, a.id))))
    moved = build_snapshot(with_view3d(base, make_view3d((2, a.id))))
    reworded = build_snapshot(
        with_view3d(base, make_view3d((1, a.id), title=LocalizedText(es="x")))
    )

    assert not diff_snapshots(plain, plain).view3d_changed
    assert not diff_snapshots(one, one).view3d_changed
    assert diff_snapshots(plain, one).view3d_changed  # added
    assert diff_snapshots(one, plain).view3d_changed  # removed
    assert diff_snapshots(one, moved).view3d_changed  # another zone
    assert diff_snapshots(one, reworded).view3d_changed  # only the text
    # And it is the only thing that differs.
    assert diff_snapshots(plain, one) == Changes(view3d_changed=True)


def test_an_old_snapshot_without_the_key_diffs_cleanly() -> None:
    old = build_snapshot(make_inputs())
    assert "view3d" not in old

    assert diff_snapshots(old, build_snapshot(make_inputs())) == Changes()
