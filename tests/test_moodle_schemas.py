"""Moodle response models against the real course-8 payload."""

import pytest

from app.moodle.schemas import MoodleModule, MoodleSection
from tests.moodle_fixtures import COURSE8_MODULE_ORDER, course8, parse, raw_course8


def test_real_payload_parses_into_two_sections_and_eight_modules() -> None:
    general, recursos = course8()

    assert (general.id, general.name, general.section, general.modules) == (
        24,
        "General",
        0,
        [],
    )
    assert (recursos.id, recursos.name, recursos.section) == (25, "Recursos", 1)
    assert [m.id for m in recursos.modules] == COURSE8_MODULE_ORDER
    assert [m.modname for m in recursos.modules] == [
        "quiz",
        "scorm",
        "quiz",
        "scorm",
        "quiz",
        "scorm",
        "quiz",
        "customcert",
    ]


def test_real_payload_field_types() -> None:
    general, recursos = course8()
    # Ints on the wire, bools in the model.
    assert general.visible is True and general.uservisible is True
    assert recursos.visible is False and recursos.uservisible is True
    module = recursos.modules[0]
    assert module.visible is True and module.uservisible is True
    assert module.visibleoncoursepage is True
    assert module.noviewlink is False and module.candisplay is True
    assert module.completion == 0
    assert module.availability is None
    assert module.instance == 7  # distinct from the cmid, 28
    assert module.id == 28
    assert module.name == "Preguntas para reconocer cuánto sabes [IN-1]"  # unchanged
    assert module.url == "https://academiaturismo.mincit.gov.co/mod/quiz/view.php?id=28"


def test_section_has_no_summary_attribute() -> None:
    general, _ = course8()
    assert "summary" not in MoodleSection.model_fields
    assert not hasattr(general, "summary")
    assert "summary" not in general.model_dump()


def test_hostile_summary_is_dropped_at_parse_time() -> None:
    raw = raw_course8()
    marker = "EVIL_MARKER"
    raw[0]["summary"] = f"<script>alert('{marker}')</script>" + "x" * 200_000

    sections = parse(raw)

    assert marker not in sections[0].model_dump_json()
    assert marker not in repr(sections[0])


@pytest.mark.parametrize(
    ("wire", "expected"), [(0, False), (1, True), (False, False), (True, True)]
)
def test_visible_accepts_ints_and_bools(wire: int | bool, expected: bool) -> None:
    module = MoodleModule.model_validate(
        {"id": 1, "name": "n", "modname": "quiz", "visible": wire, "uservisible": wire}
    )
    assert module.visible is expected and module.uservisible is expected


def test_non_null_availability_is_kept_raw() -> None:
    raw = raw_course8()
    restriction = '{"op":"&","c":[],"showc":[]}'
    raw[1]["modules"][0]["availability"] = restriction
    assert parse(raw)[1].modules[0].availability == restriction
