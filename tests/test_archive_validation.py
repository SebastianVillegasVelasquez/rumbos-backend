"""Semantic validation of an archive: references, content, skins, assets."""

from typing import Any

import pytest

from app.exceptions import ArchiveInvalidError
from app.portability.format import Manifest, MapsFile, parse_model
from app.portability.validation import CheckedArchive, check_archive
from tests import archive_builders as ab


def check(
    maps: dict[str, Any], assets: dict[str, bytes] | None = None
) -> CheckedArchive:
    files = {"maps.json": ab.dumps(maps), **(assets or {})}
    manifest = parse_model(Manifest, ab.manifest_for(maps, files))
    return check_archive(parse_model(MapsFile, maps), manifest)


def refused(maps: dict[str, Any], assets: dict[str, bytes] | None = None) -> set[str]:
    with pytest.raises(ArchiveInvalidError) as caught:
        check(maps, assets)
    return {p["code"] for p in caught.value.problems}


def with_bubbles(*bubbles: dict[str, Any], **over: Any) -> dict[str, Any]:
    return ab.maps_file(maps=[ab.archive_map(bubbles=list(bubbles), **over)])


def test_a_consistent_archive_is_accepted() -> None:
    checked = check(ab.maps_file())
    assert len(checked.maps) == 1


def test_a_full_archive_with_assets_and_skins_is_accepted() -> None:
    png = ab.good_png()
    bubble_png = ab.asset_entry("a2", png, kind="bubble")
    maps = ab.maps_file(
        maps=[
            ab.archive_map(
                background={"assetRef": "a1"},
                defaultSkin={"ref": "s1"},
                bubbles=[ab.activity_bubble(skin={"ref": "s1"})],
            )
        ],
        skins=[
            {
                "ref": "s1",
                "name": "Mine",
                "config": {
                    "kind": "image",
                    "size": 96,
                    "anchor": "center",
                    "states": {"available": "a2", "locked": "a2"},
                    "label": {"mode": "hover"},
                    "effects": {"idle": "none", "completion": "none"},
                },
            }
        ],
        assets=[ab.asset_entry("a1", png), bubble_png],
    )
    check(maps, {"assets/a1.png": png, "assets/a2.png": png})


@pytest.mark.parametrize(
    "url", ["javascript:alert(1)", "data:text/html;base64,AAAA", "//evil.test/x"]
)
def test_hostile_links_in_content_are_refused(url: str) -> None:
    content = ab.blocks_content()
    content["body"]["blocks"][2]["url"] = url
    assert refused(with_bubbles(ab.content_bubble(content=content)))


def test_a_bad_video_id_is_refused() -> None:
    content = ab.blocks_content()
    content["body"]["blocks"].append(
        {"type": "video", "provider": "youtube", "videoId": "../x"}
    )
    assert refused(with_bubbles(ab.content_bubble(content=content)))


def test_an_oversized_widget_is_refused() -> None:
    content = ab.widget_content(html="<p>" + "x" * 2_000_000 + "</p>")
    assert refused(with_bubbles(ab.content_bubble(content=content)))


def test_content_over_the_map_budget_is_refused() -> None:
    html = "<p>" + "x" * 190_000 + "</p>"
    bubbles = [
        ab.content_bubble(f"b{i}", content=ab.widget_content(html=html))
        for i in range(1, 8)
    ]
    assert "content_too_large" in refused(with_bubbles(*bubbles))


def test_an_image_block_must_name_an_asset_of_the_archive() -> None:
    content = ab.blocks_content()
    content["body"]["blocks"].append({"type": "image", "assetRef": "a9", "alt": "x"})
    assert "asset_ref_unknown" in refused(
        with_bubbles(ab.content_bubble(content=content))
    )


def test_a_database_id_inside_content_is_refused() -> None:
    content = ab.blocks_content()
    content["body"]["blocks"].append(
        {"type": "image", "assetId": "0191e0a0-0000-7000-8000-000000000000", "alt": "x"}
    )
    assert "asset_id_not_allowed" in refused(
        with_bubbles(ab.content_bubble(content=content))
    )


def test_an_own_asset_path_as_background_is_refused() -> None:
    url = "/api/v1/assets/0191e0a0-0000-7000-8000-000000000000"
    maps = ab.maps_file(maps=[ab.archive_map(background={"imageUrl": url})])
    assert "asset_id_not_allowed" in refused(maps)


def test_unknown_skin_and_asset_refs_are_refused() -> None:
    maps = ab.maps_file(
        maps=[
            ab.archive_map(
                background={"assetRef": "a7"},
                defaultSkin={"ref": "s7"},
            )
        ]
    )
    assert refused(maps) >= {"asset_ref_unknown", "skin_ref_unknown"}


def test_duplicate_refs_are_refused() -> None:
    assert "duplicate_ref" in refused(
        with_bubbles(ab.activity_bubble("b1"), ab.activity_bubble("b1"))
    )
    maps = ab.maps_file(maps=[ab.archive_map("m1"), ab.archive_map("m1")])
    assert "duplicate_ref" in refused(maps)


def test_unused_assets_and_skins_are_refused() -> None:
    png = ab.good_png()
    maps = ab.maps_file(
        assets=[ab.asset_entry("a1", png)],
        skins=[{"ref": "s1", "name": "x", "config": {"kind": "procedural"}}],
    )
    assert refused(maps, {"assets/a1.png": png}) >= {"asset_unused", "skin_unused"}


def test_an_asset_that_differs_from_the_manifest_is_refused() -> None:
    png = ab.good_png()
    maps = ab.maps_file(
        maps=[ab.archive_map(background={"assetRef": "a1"})],
        assets=[ab.asset_entry("a1", png, sha256="0" * 64)],
    )
    assert "asset_mismatch" in refused(maps, {"assets/a1.png": png})


def test_a_background_must_be_a_background_asset() -> None:
    png = ab.good_png()
    maps = ab.maps_file(
        maps=[ab.archive_map(background={"assetRef": "a1"})],
        assets=[ab.asset_entry("a1", png, kind="bubble")],
    )
    assert "asset_wrong_kind" in refused(maps, {"assets/a1.png": png})


def test_a_binding_to_an_unknown_bubble_is_refused() -> None:
    view = {
        "scene": "region-encuentros",
        "bindings": [{"zoneId": 0, "bubbleRef": "b9"}],
    }
    assert "bubble_ref_unknown" in refused(
        with_bubbles(ab.activity_bubble(), view3d=view)
    )


def test_an_unknown_scene_is_not_an_error() -> None:
    view = {"scene": "nowhere", "bindings": [{"zoneId": 0, "bubbleRef": "b1"}]}
    check(with_bubbles(ab.activity_bubble(), view3d=view))


def test_a_zone_outside_the_scene_is_refused() -> None:
    view = {
        "scene": "region-encuentros",
        "bindings": [{"zoneId": 99, "bubbleRef": "b1"}],
    }
    assert refused(with_bubbles(ab.activity_bubble(), view3d=view))


def test_two_skin_rules_for_one_type_are_refused() -> None:
    rules = [
        {"modname": "quiz", "skin": {"ref": "s1"}},
        {"modname": "quiz", "skin": {"ref": "s1"}},
    ]
    maps = ab.maps_file(
        maps=[ab.archive_map(skinRules=rules)],
        skins=[{"ref": "s1", "name": "x", "config": {"kind": "procedural"}}],
    )
    assert "duplicate_skin_rule" in refused(maps)


def test_the_counts_must_be_true() -> None:
    maps = ab.maps_file()
    files = {"maps.json": ab.dumps(maps)}
    manifest = ab.manifest_for(maps, files)
    manifest["counts"]["bubbles"] += 1
    with pytest.raises(ArchiveInvalidError) as caught:
        check_archive(parse_model(MapsFile, maps), parse_model(Manifest, manifest))
    assert {p["code"] for p in caught.value.problems} == {"counts_mismatch"}


def test_problems_never_echo_hostile_text() -> None:
    content = ab.blocks_content()
    content["body"]["blocks"][2]["url"] = "javascript:SECRET()"
    with pytest.raises(ArchiveInvalidError) as caught:
        check(with_bubbles(ab.content_bubble(content=content)))
    assert "SECRET" not in str(caught.value.problems)
