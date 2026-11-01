"""The models of the `.rumbos` format: what an archive may contain."""

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from app.portability.format import (
    ArchiveAsset,
    ArchiveBubble,
    ArchiveMap,
    Manifest,
    MapsFile,
    iso,
    parse_model,
)
from app.portability.problems import MAX_PROBLEMS, from_validation_error
from tests import archive_builders as ab


def test_a_complete_maps_file_is_read() -> None:
    maps = ab.maps_file(
        skins=[
            {
                "ref": "s1",
                "name": "Mine",
                "config": {"kind": "procedural"},
            }
        ],
        assets=[ab.asset_entry("a1", b"x")],
    )
    parsed = parse_model(MapsFile, maps)
    assert parsed.maps[0].bubbles[0].activity is not None
    assert parsed.maps[0].bubbles[1].content is not None
    assert parsed.assets[0].size == 1
    assert parsed.skins[0].config == {"kind": "procedural"}


def test_the_manifest_is_read() -> None:
    maps = ab.maps_file()
    files = {"maps.json": ab.dumps(maps), "assets/a1.png": b"abc"}
    parsed = parse_model(Manifest, ab.manifest_for(maps, files))
    assert parsed.counts.assets == 1
    assert parsed.exported_at == datetime(2026, 10, 31, 12, tzinfo=UTC)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: m.update(extra=1),
        lambda m: m["maps"][0].update(surprise=True),
        lambda m: m["maps"][0]["bubbles"][0].update(id="0" * 36),
        lambda m: m["maps"][0]["bubbles"][0]["activity"].update(cmid=3),
    ],
    ids=["file", "map", "bubble", "activity"],
)
def test_unknown_fields_are_refused_at_every_level(mutate: Any) -> None:
    maps = ab.maps_file()
    mutate(maps)
    with pytest.raises(ValidationError) as caught:
        parse_model(MapsFile, maps)
    assert {p["code"] for p in from_validation_error(caught.value)} == {"unknown_field"}


def test_only_the_camel_case_spelling_is_read() -> None:
    maps = ab.maps_file()
    bubble = maps["maps"][0]["bubbles"][0]
    bubble["activity"]["section_number"] = bubble["activity"].pop("sectionNumber")
    with pytest.raises(ValidationError):
        parse_model(MapsFile, maps)


@pytest.mark.parametrize(
    "ref", ["", "A1", "a", "1a", "abc1", "a123456", "a1 ", "a1\n", "a-1", "../a1"]
)
def test_refs_follow_the_pattern(ref: str) -> None:
    maps = ab.maps_file()
    maps["maps"][0]["ref"] = ref
    with pytest.raises(ValidationError):
        parse_model(MapsFile, maps)


@pytest.mark.parametrize("ref", ["a1", "zz99999", "m12", "b00001"])
def test_refs_that_follow_the_pattern_are_read(ref: str) -> None:
    maps = ab.maps_file()
    maps["maps"][0]["ref"] = ref
    assert parse_model(MapsFile, maps).maps[0].ref == ref


def test_a_bubble_is_an_activity_or_content_never_both_or_neither() -> None:
    both = ab.activity_bubble(content=ab.blocks_content())
    neither = ab.activity_bubble()
    del neither["activity"]
    wrong = ab.content_bubble(activity=ab.activity_bubble()["activity"])
    for value in (both, neither, wrong):
        with pytest.raises(ValidationError):
            parse_model(ArchiveBubble, value)


def test_a_skin_reference_is_a_custom_one_or_a_built_in_one() -> None:
    bubble = ab.activity_bubble(skin={"ref": "s1"})
    assert parse_model(ArchiveBubble, bubble).skin is not None
    builtin = ab.activity_bubble(
        skin={"builtinId": "00000000-0000-4000-8000-000000000001"}
    )
    assert parse_model(ArchiveBubble, builtin).skin is not None
    for bad in ({"ref": "s1", "builtinId": "0" * 32}, {}, {"id": "x"}):
        with pytest.raises(ValidationError):
            parse_model(ArchiveBubble, ab.activity_bubble(skin=bad))


def test_the_background_is_an_asset_or_an_address() -> None:
    parse_model(ArchiveMap, ab.archive_map(background={"assetRef": "a1"}))
    parse_model(ArchiveMap, ab.archive_map(background={"imageUrl": "/maps/one.png"}))
    for bad in ({}, {"assetRef": "a1", "imageUrl": "https://x.test/a.png"}):
        with pytest.raises(ValidationError):
            parse_model(ArchiveMap, ab.archive_map(background=bad))


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:image/png;base64,AAAA",
        "file:///etc/passwd",
        "//evil.test/x.png",
    ],
)
def test_an_image_address_follows_the_rules_of_the_public_api(url: str) -> None:
    with pytest.raises(ValidationError):
        parse_model(ArchiveMap, ab.archive_map(background={"imageUrl": url}))


def test_an_asset_file_name_follows_from_its_ref_and_type() -> None:
    entry = ab.asset_entry("a1", b"x")
    parse_model(ArchiveAsset, entry)
    for field, value in (
        ("file", "assets/a2.png"),
        ("file", "assets/a1.jpg"),
        ("file", "../a1.png"),
        ("mime", "image/gif"),
        ("mime", "audio/mpeg"),
    ):
        with pytest.raises(ValidationError):
            parse_model(ArchiveAsset, {**entry, field: value})


def test_audio_needs_a_duration_and_a_credit_and_images_dimensions() -> None:
    audio = {
        "ref": "a2",
        "kind": "audio",
        "file": "assets/a2.mp3",
        "mime": "audio/mpeg",
        "sha256": "0" * 64,
        "bytes": 10,
        "durationSeconds": 12.5,
        "title": "Brisa",
        "credit": "Ana",
    }
    parse_model(ArchiveAsset, audio)
    changes: list[dict[str, Any]] = [
        {"credit": None},
        {"durationSeconds": None},
        {"width": 3, "height": 3},
    ]
    for change in changes:
        with pytest.raises(ValidationError):
            parse_model(ArchiveAsset, {**audio, **change})
    image = ab.asset_entry("a1", b"x")
    for change in ({"width": None}, {"durationSeconds": 4.0}):
        with pytest.raises(ValidationError):
            parse_model(ArchiveAsset, {**image, **change})


def test_text_is_cleaned_like_the_public_api_does() -> None:
    bubble = ab.activity_bubble()
    bubble["activity"]["name"] = "Quiz\N{RIGHT-TO-LEFT OVERRIDE}\x00 final\n"
    parsed = parse_model(ArchiveBubble, bubble)
    assert parsed.activity is not None
    assert parsed.activity.name == "Quiz final"


def test_limits_are_enforced() -> None:
    maps = ab.maps_file()
    maps["maps"] = [ab.archive_map(f"m{i}") for i in range(1, 22)]
    with pytest.raises(ValidationError):
        parse_model(MapsFile, maps)
    empty = ab.maps_file(maps=[])
    with pytest.raises(ValidationError):
        parse_model(MapsFile, empty)
    big = ab.archive_map(bubbles=[ab.activity_bubble(f"b{i}") for i in range(501)])
    with pytest.raises(ValidationError):
        parse_model(ArchiveMap, big)
    long_name = ab.activity_bubble()
    long_name["activity"]["name"] = "x" * 256
    with pytest.raises(ValidationError):
        parse_model(ArchiveBubble, long_name)


def test_the_manifest_must_be_consistent_with_itself() -> None:
    maps = ab.maps_file()
    files = {"maps.json": ab.dumps(maps), "assets/a1.png": b"abc"}
    good = ab.manifest_for(maps, files)
    parse_model(Manifest, good)

    cases: list[Any] = [
        {**good, "files": good["files"] + [good["files"][0]]},  # listed twice
        {**good, "files": [f for f in good["files"] if f["path"] != "maps.json"]},
        {**good, "counts": {**good["counts"], "assets": 5}},
        {**good, "generator": "other"},
        {**good, "exportedAt": "2026-10-31T12:00:00"},  # no time zone
        {**good, "formatVersion": 0},
    ]
    for case in cases:
        with pytest.raises(ValidationError):
            parse_model(Manifest, case)
    named = deepcopy(good)
    named["files"].append({"path": "manifest.json", "sha256": "0" * 64, "bytes": 1})
    with pytest.raises(ValidationError):
        parse_model(Manifest, named)


def test_a_manifest_cannot_name_a_path_outside_the_allowed_ones() -> None:
    maps = ab.maps_file()
    for path in (
        "../maps.json",
        "/etc/passwd",
        "assets/x.exe",
        "assets/a1.png/",
        "a.zip",
    ):
        files = {"maps.json": ab.dumps(maps)}
        manifest = ab.manifest_for(maps, files)
        manifest["files"].append({"path": path, "sha256": "0" * 64, "bytes": 1})
        with pytest.raises(ValidationError):
            parse_model(Manifest, manifest)


def test_problems_never_echo_the_input_and_are_bounded() -> None:
    maps = ab.maps_file()
    maps["maps"][0]["title"] = "SECRET-" + "x" * 300
    maps["maps"][0]["bubbles"] = [
        ab.activity_bubble(f"b{i}", x="SECRET-text") for i in range(1, 200)
    ]
    with pytest.raises(ValidationError) as caught:
        parse_model(MapsFile, maps)
    problems = from_validation_error(caught.value)
    assert 0 < len(problems) <= MAX_PROBLEMS
    assert "SECRET" not in str(problems)
    assert all(set(p) == {"path", "code", "message"} for p in problems)
    assert problems[0]["path"].startswith("maps[0]")


def test_the_time_is_spelled_in_utc_without_fractions() -> None:
    assert iso(datetime(2026, 1, 2, 3, 4, 5, 999_999, tzinfo=UTC)) == (
        "2026-01-02T03:04:05Z"
    )
