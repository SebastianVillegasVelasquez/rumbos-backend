"""The welcome curtain settings: validation, defaults, the stored form and the
canonical snapshot. No database."""

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from app.api.errors import welcome_problems
from app.publishing.snapshot import build_snapshot, content_hash, diff_snapshots
from app.schemas.map_settings import MapSettings
from app.schemas.welcome import Welcome
from tests.test_snapshot import LEGACY_HASH, make_inputs

RLO = chr(0x202E)  # right-to-left override
NUL, BEL, TAB, LF = chr(0), chr(7), chr(9), chr(10)
CRLF = chr(13) + chr(10)
COMBINING_ACUTE = chr(0x301)


def welcome(**fields: Any) -> dict[str, Any]:
    return fields


def problems_of(raw: Any) -> list[dict[str, str]]:
    with pytest.raises(ValidationError) as caught:
        MapSettings.model_validate({"welcome": raw})
    found = welcome_problems(
        [
            {**e, "loc": ("body", "settings", *e["loc"])}
            for e in caught.value.errors(include_url=False)
        ]
    )
    assert found is not None
    return found


# --- valid -----------------------------------------------------------------------


def test_a_full_curtain_is_accepted_and_kept() -> None:
    raw = welcome(
        enabled=False,
        title="Bienvenido a {curso}",
        message="Línea uno\nLínea dos con {curso}",
        video={"provider": "vimeo", "videoId": "76979871"},
        playOnEnter=False,
        show="always",
    )

    parsed = MapSettings.model_validate({"welcome": raw}).welcome

    assert parsed.enabled is False
    assert parsed.title == "Bienvenido a {curso}"
    assert parsed.message == "Línea uno\nLínea dos con {curso}"
    assert parsed.video is not None and parsed.video.provider == "vimeo"
    assert parsed.play_on_enter is False and parsed.show == "always"


def test_text_is_normalized_and_stripped_of_control_characters() -> None:
    parsed = Welcome.model_validate(
        {
            "title": "  Cafe" + COMBINING_ACUTE + NUL + RLO + "  ",
            "message": "a" + CRLF + "b" + BEL + "c" + TAB + LF,
        }
    )

    assert parsed.title == "Café"
    assert parsed.message == "a\nbc"  # newline kept, tab and control gone


def test_an_empty_message_is_no_message() -> None:
    assert Welcome.model_validate({"message": "  \n "}).message is None


def test_html_is_plain_text_and_is_never_interpreted() -> None:
    parsed = Welcome.model_validate({"title": "<b>Hola</b> & <script>x</script>"})

    assert parsed.title == "<b>Hola</b> & <script>x</script>"


@pytest.mark.parametrize("length", [1, 120])
def test_title_lengths_at_the_edges_are_accepted(length: int) -> None:
    assert Welcome.model_validate({"title": "a" * length}).title == "a" * length


# --- invalid ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "path", "code"),
    [
        ({"title": ""}, "title", "too_short"),
        ({"title": "   "}, "title", "too_short"),
        ({"title": "a" * 121}, "title", "too_long"),
        ({"message": "a" * 301}, "message", "too_long"),
        ({"title": 5}, "title", "invalid_type"),
        ({"enabled": "yes"}, "enabled", "invalid_type"),
        ({"playOnEnter": 1}, "playOnEnter", "invalid_type"),
        ({"show": "never"}, "show", "invalid_value"),
        ({"extra": 1}, "extra", "unknown_field"),
        (
            {"video": {"provider": "dailymotion", "videoId": "abcdef"}},
            "video.provider",
            "invalid_value",
        ),
        ({"video": {"provider": "youtube"}}, "video.videoId", "required"),
        ({"video": {"videoId": "abcdef"}}, "video.provider", "required"),
        ({"video": "https://youtu.be/dQw4w9WgXcQ"}, "video", "invalid_type"),
    ],
)
def test_invalid_fields_are_reported_with_path_and_code(
    raw: dict[str, Any], path: str, code: str
) -> None:
    found = problems_of(raw)

    assert any(p["path"] == path and p["code"] == code for p in found), found
    assert all(p["message"] for p in found)


@pytest.mark.parametrize(
    "video_id",
    [
        "short",
        "a" * 33,
        "abc def1",
        "abc/def1",
        "abc?v=1234",
        "https://x.io/abcdef",
        "abc\ndef1",
        "<script>",
        "..%2f..%2fx",
        "éééééé",
    ],
)
def test_hostile_video_ids_are_refused(video_id: str) -> None:
    found = problems_of({"video": {"provider": "youtube", "videoId": video_id}})

    assert [(p["path"], p["code"]) for p in found] == [
        ("video.videoId", "invalid_video_id")
    ]


@pytest.mark.parametrize(
    "text",
    [
        "{nombre}",
        "{}",
        "{CURSO}",
        "{ curso }",
        "{{curso}}",
        "Hola {curso} y {alumno}",
        "{curso.title}",
        "{0}",
    ],
)
@pytest.mark.parametrize("field", ["title", "message"])
def test_any_placeholder_other_than_curso_is_refused(field: str, text: str) -> None:
    found = problems_of({field: text})

    assert [(p["path"], p["code"]) for p in found] == [
        (field, "welcome_invalid_placeholder")
    ]


@pytest.mark.parametrize(
    "text", ["{curso}", "{curso} y {curso}", "Hola {curso}!", "{", "}", "a } b {"]
)
def test_curso_and_lone_braces_are_fine(text: str) -> None:
    assert Welcome.model_validate({"title": text}).title == text


def test_problems_never_echo_the_input() -> None:
    secret = "{sup3r-s3cret-token}"

    found = problems_of(
        {"title": secret, "video": {"provider": "youtube", "videoId": secret}}
    )

    assert secret not in str(found)


# --- defaults and the stored form -------------------------------------------------


@pytest.mark.parametrize("stored", [{}, {"welcome": None}])
def test_a_missing_or_null_welcome_reads_as_all_defaults(
    stored: dict[str, Any],
) -> None:
    welcome_ = MapSettings.model_validate(stored).welcome

    assert welcome_ == Welcome()
    assert welcome_.enabled is True
    assert welcome_.title is None and welcome_.message is None
    assert welcome_.video is None
    assert welcome_.play_on_enter is True and welcome_.show == "first_visit"


def test_the_stored_form_omits_a_welcome_that_is_all_defaults() -> None:
    assert "welcome" not in MapSettings().to_stored()
    assert "welcome" not in MapSettings.model_validate({"welcome": None}).to_stored()
    assert "welcome" not in MapSettings.model_validate({"welcome": {}}).to_stored()


def test_the_stored_form_keeps_a_configured_welcome() -> None:
    settings = MapSettings.model_validate({"welcome": {"enabled": False}})

    stored = settings.to_stored()

    assert stored["welcome"] == {
        "enabled": False,
        "title": None,
        "message": None,
        "video": None,
        "playOnEnter": True,
        "show": "first_visit",
    }
    assert MapSettings.model_validate(stored) == settings


# --- the canonical snapshot --------------------------------------------------------


def test_settings_that_never_had_a_welcome_hash_exactly_as_before() -> None:
    # The same pinned text the format-stability test in test_snapshot.py uses.
    snapshot = build_snapshot(make_inputs())

    assert "welcome" not in snapshot["settings"]
    assert content_hash(snapshot) == content_hash(
        build_snapshot(
            make_inputs(settings=MapSettings.model_validate({"welcome": {}}))
        )
    )
    assert LEGACY_HASH  # pinned by test_snapshot.test_a_map_without_extras_*


def test_a_stored_welcome_changes_the_hash_and_clearing_it_restores_it() -> None:
    plain = build_snapshot(make_inputs())
    curtain = build_snapshot(
        make_inputs(
            settings=MapSettings.model_validate({"welcome": {"show": "always"}})
        )
    )
    cleared = build_snapshot(
        make_inputs(settings=MapSettings.model_validate({"welcome": None}))
    )

    assert content_hash(curtain) != content_hash(plain)
    assert content_hash(cleared) == content_hash(plain)


@pytest.mark.parametrize(
    "other",
    [
        {"enabled": False},
        {"title": "Hola"},
        {"message": "Texto"},
        {"video": {"provider": "youtube", "videoId": "dQw4w9WgXcQ"}},
        {"playOnEnter": False},
        {"show": "always"},
    ],
)
def test_each_welcome_field_is_part_of_the_hash_and_of_settings_changed(
    other: dict[str, Any],
) -> None:
    old = build_snapshot(make_inputs())
    new = build_snapshot(
        make_inputs(settings=MapSettings.model_validate({"welcome": other}))
    )

    assert content_hash(new) != content_hash(old)
    changes = diff_snapshots(old, new)
    assert changes.settings_changed is True
    assert diff_snapshots(new, old).settings_changed is True
    assert diff_snapshots(old, copy.deepcopy(old)).settings_changed is False


def test_a_snapshot_without_welcome_reads_back_as_the_defaults() -> None:
    snapshot = build_snapshot(make_inputs())

    assert MapSettings.model_validate(snapshot["settings"]).welcome == Welcome()
