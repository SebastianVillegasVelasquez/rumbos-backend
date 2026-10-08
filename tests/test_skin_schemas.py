"""The skin config contract: what validates and what is rejected."""

import copy
import uuid
from typing import Any

import pytest
from pydantic import ValidationError

from app.schemas.skin import (
    ImageSkin,
    ProceduralSkin,
    SkinCreate,
    SkinUpdate,
    dump_config,
    skin_config_adapter,
)
from tests import skin_configs


def parse(config: dict[str, Any]) -> ProceduralSkin | ImageSkin:
    return skin_config_adapter.validate_python(config)


def test_procedural_config_roundtrips_as_camel_case_json() -> None:
    config = skin_configs.procedural()

    assert dump_config(parse(config)) == config


def test_image_config_roundtrips_with_ids_as_strings() -> None:
    asset = uuid.uuid4()
    config = skin_configs.image(asset)
    expected = copy.deepcopy(config)
    for state in ("locked", "next", "inProgress", "complete", "hover"):
        expected["states"][state] = None  # unset states are explicit nulls

    dumped = dump_config(parse(config))

    assert dumped == expected
    assert isinstance(parse(config), ImageSkin)


def test_the_kind_picks_the_model() -> None:
    assert isinstance(parse(skin_configs.procedural()), ProceduralSkin)
    assert isinstance(parse(skin_configs.image(uuid.uuid4())), ImageSkin)


def test_an_unknown_kind_is_rejected() -> None:
    with pytest.raises(ValidationError, match="kind"):
        parse({**skin_configs.procedural(), "kind": "svg"})


def test_the_schema_version_defaults_to_one_and_nothing_else_is_accepted() -> None:
    config = skin_configs.procedural()
    del config["schemaVersion"]
    assert parse(config).schema_version == 1

    with pytest.raises(ValidationError):
        parse({**skin_configs.procedural(), "schemaVersion": 2})


@pytest.mark.parametrize(
    "color",
    [
        "#FFF",
        "FFB703",
        "#GGB703",
        "#FFB70",
        "#FFB7033",
        "red",
        "",
        "#ffb703 ",
        "rgb(1,2,3)",
    ],
)
def test_colors_must_be_six_digit_hex(color: str) -> None:
    config = skin_configs.procedural()
    config["palette"]["available"]["fill"] = color

    with pytest.raises(ValidationError):
        parse(config)


def test_lowercase_hex_is_accepted() -> None:
    config = skin_configs.procedural()
    config["palette"]["locked"]["glow"] = "#b4bccb"

    parse(config)


@pytest.mark.parametrize("size", [31, 161, 0, -5, 56.5])
def test_procedural_size_range(size: float) -> None:
    with pytest.raises(ValidationError):
        parse(skin_configs.procedural(size=size))


@pytest.mark.parametrize("size", [32, 160])
def test_procedural_size_limits_are_inclusive(size: int) -> None:
    parse(skin_configs.procedural(size=size))


@pytest.mark.parametrize(
    ("size", "ok"), [(31, False), (32, True), (320, True), (321, False)]
)
def test_image_size_range(size: int, ok: bool) -> None:
    config = skin_configs.image(uuid.uuid4())
    config["size"] = size

    if ok:
        parse(config)
    else:
        with pytest.raises(ValidationError):
            parse(config)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("shape",), "square"),
        (("label", "mode"), "sometimes"),
        (("effects", "idle"), "spin"),
        (("effects", "completion"), "confetti"),
        (("icon", "mode"), "emoji"),
        (("icon", "preset"), "banana"),
        (("icon", "color"), "blue"),
        (("effects", "ring"), "yes please"),
    ],
)
def test_enum_values_are_checked(path: tuple[str, ...], value: object) -> None:
    config = skin_configs.procedural()
    target = config
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    with pytest.raises(ValidationError):
        parse(config)


def test_image_skins_do_not_accept_the_pulse_idle_effect() -> None:
    config = skin_configs.image(uuid.uuid4())
    config["effects"]["idle"] = "pulse"

    with pytest.raises(ValidationError):
        parse(config)


def test_unknown_keys_are_rejected_at_every_level() -> None:
    top = skin_configs.procedural(extra=1)
    nested = skin_configs.procedural()
    nested["palette"]["locked"]["shadow"] = "#000000"
    image = skin_configs.image(uuid.uuid4())
    image["states"]["pressed"] = str(uuid.uuid4())

    for config in (top, nested, image):
        with pytest.raises(ValidationError, match="extra_forbidden"):
            parse(config)


def test_a_preset_icon_needs_its_preset() -> None:
    config = skin_configs.procedural()
    config["icon"] = {"mode": "preset", "preset": None, "color": "auto"}
    with pytest.raises(ValidationError, match="preset is required"):
        parse(config)

    config["icon"] = {"mode": "preset", "preset": "star", "color": "#112233"}
    parse(config)


def test_every_palette_state_is_required() -> None:
    config = skin_configs.procedural()
    del config["palette"]["complete"]

    with pytest.raises(ValidationError):
        parse(config)


def test_image_skins_need_the_available_state_and_ids_must_be_uuids() -> None:
    config = skin_configs.image(uuid.uuid4())
    del config["states"]["available"]
    with pytest.raises(ValidationError):
        parse(config)

    with pytest.raises(ValidationError):
        parse(skin_configs.image("not-a-uuid"))


def test_names_are_trimmed_and_bounded() -> None:
    config = skin_configs.procedural()

    assert (
        SkinCreate.model_validate({"name": "  Mi skin ", "config": config}).name
        == "Mi skin"
    )
    for bad in ("", "   ", "x" * 61):
        with pytest.raises(ValidationError):
            SkinCreate.model_validate({"name": bad, "config": config})
    SkinCreate.model_validate({"name": "x" * 60, "config": config})


def test_update_needs_a_field_and_rejects_nulls() -> None:
    with pytest.raises(ValidationError, match="at least one"):
        SkinUpdate.model_validate({})
    with pytest.raises(ValidationError, match="cannot be null"):
        SkinUpdate.model_validate({"name": None})
    assert SkinUpdate.model_validate({"name": "Nuevo"}).model_fields_set == {"name"}
