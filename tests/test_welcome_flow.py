"""The welcome flow model and its write-time rules. No database."""

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from app.api.errors import welcome_problems
from app.publishing.snapshot import build_snapshot, content_hash, diff_snapshots
from app.schemas.map_settings import MapSettings
from app.schemas.welcome import Welcome
from app.welcome_flow.flow import flow_problems
from tests.test_snapshot import make_inputs

ASSET = "0197a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b"
# "Cafe" + combining acute, a bell, a newline: normalizes to "Café x".
TITLE_WITH_NOISE = "  Cafe" + chr(0x301) + chr(7) + chr(10) + "x  "


def step(type_: str, key: str, **fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"type": type_, "key": key, "title": f"Paso {key}"}
    extras: dict[str, dict[str, Any]] = {
        "welcome": {},
        "video": {"video": {"provider": "youtube", "videoId": "dQw4w9WgXcQ"}},
        "html": {"html": "<p>Hola</p>"},
        "pdf": {"assetId": ASSET},
        "quiz": {},
    }
    return {**base, **extras[type_], **fields}


FULL_FLOW: list[dict[str, Any]] = [
    step("welcome", "bienvenida", message="Hola {curso}", buttonLabel="Empezar"),
    step("video", "videointro", allowFallback=False),
    step(
        "html",
        "lectura01",
        css="p { color: #123 }",
        ack={"label": "Lo he leído"},
        minSeconds=30,
    ),
    step("pdf", "guiapdf01", ack={"label": "Lo he revisado"}, minSeconds=60),
    step(
        "quiz",
        "cuestionario",
        cmid=42,
        intro="Responde el cuestionario.",
        outcomeAbove="Bien hecho",
        outcomeBelow="Repasa el material",
        mandatory=False,
        revision=3,
    ),
]


def parse(raw: dict[str, Any]) -> Welcome:
    return Welcome.model_validate(raw)


def problems_of(raw: dict[str, Any]) -> list[dict[str, str]]:
    """Every problem of a curtain, structural first, else the flow's own."""
    try:
        welcome = parse(raw)
    except ValidationError as exc:
        found = welcome_problems(
            [
                {**e, "loc": ("body", "settings", "welcome", *e["loc"])}
                for e in exc.errors(include_url=False)
            ]
        )
        assert found is not None
        return found
    return flow_problems(welcome)


def codes(raw: dict[str, Any]) -> list[tuple[str, str]]:
    return [(p["path"], p["code"]) for p in problems_of(raw)]


# --- a valid flow ---------------------------------------------------------------


def test_a_flow_with_every_step_type_is_valid_and_kept() -> None:
    welcome = parse({"steps": FULL_FLOW})

    assert flow_problems(welcome) == []
    assert welcome.steps is not None
    assert [s.type for s in welcome.steps] == [
        "welcome",
        "video",
        "html",
        "pdf",
        "quiz",
    ]
    assert welcome.title is None and welcome.message is None
    assert welcome.video is None and welcome.play_on_enter is None
    assert welcome.show == "first_visit"


def test_a_flow_survives_the_stored_form() -> None:
    settings = MapSettings.model_validate({"welcome": {"steps": FULL_FLOW}})

    stored = settings.to_stored()

    assert [s["key"] for s in stored["welcome"]["steps"]] == [
        s["key"] for s in FULL_FLOW
    ]
    assert stored["welcome"]["playOnEnter"] is None
    assert MapSettings.model_validate(stored) == settings


def test_step_defaults() -> None:
    welcome = parse(
        {
            "steps": [
                step("video", "videointro"),
                step("html", "lectura01"),
                step("pdf", "documento1"),
                step("quiz", "cuestionario"),
            ]
        }
    )

    assert welcome.steps is not None
    for s in welcome.steps:
        assert s.mandatory is True and s.revision == 1
    video, html, pdf, quiz = welcome.steps
    assert video.type == "video" and video.allow_fallback is True
    assert html.type == "html" and html.min_seconds == 0 and html.ack is None
    assert html.css is None
    assert pdf.type == "pdf" and pdf.min_seconds == 0 and pdf.ack is None
    assert quiz.type == "quiz" and quiz.cmid is None
    assert quiz.intro is None and quiz.outcome_above is None


def test_show_applies_to_a_flow() -> None:
    assert parse({"steps": FULL_FLOW, "show": "always"}).show == "always"


def test_twelve_steps_and_three_quizzes_are_the_limit_and_fine() -> None:
    steps = [step("quiz", f"cuestion{i}") for i in range(3)] + [
        step("html", f"lectura{i:02d}") for i in range(9)
    ]

    assert problems_of({"steps": steps}) == []


# --- text rules ---------------------------------------------------------------------


def test_step_texts_are_plain_text_normalized() -> None:
    welcome = parse(
        {
            "steps": [
                step("html", "lectura01", title=TITLE_WITH_NOISE),
                step(
                    "quiz", "cuestionario", intro="Una\r\ndos\u0007", outcomeAbove=" "
                ),
            ]
        }
    )

    assert welcome.steps is not None
    first, second = welcome.steps
    assert first.title == "Café x"
    assert second.type == "quiz"
    assert second.intro == "Una\ndos"
    assert second.outcome_above is None  # empty means none


def test_html_and_css_are_kept_exactly_as_written() -> None:
    html = "  <p>  á \n </p>  "
    welcome = parse({"steps": [step("html", "lectura01", html=html, css="")]})

    assert welcome.steps is not None
    kept = welcome.steps[0]
    assert kept.type == "html" and kept.html == html and kept.css is None


def test_only_the_welcome_step_message_knows_the_curso_placeholder() -> None:
    ok = {"steps": [step("welcome", "bienvenida", message="Hola {curso}")]}
    bad = {"steps": [step("welcome", "bienvenida", message="Hola {nombre}")]}

    assert problems_of(ok) == []
    assert codes(bad) == [("welcome.steps[0].message", "welcome_invalid_placeholder")]


# --- structural errors, with JSON-style paths ----------------------------------------


@pytest.mark.parametrize(
    ("steps", "path", "code"),
    [
        ([{"key": "abcdef", "title": "t"}], "welcome.steps[0]", "step_type_invalid"),
        (
            [{"type": "poll", "key": "abcdef", "title": "t"}],
            "welcome.steps[0]",
            "step_type_invalid",
        ),
        ([step("quiz", "ABCDEF")], "welcome.steps[0].key", "invalid_format"),
        ([step("quiz", "abc")], "welcome.steps[0].key", "invalid_format"),
        ([step("quiz", "a" * 17)], "welcome.steps[0].key", "invalid_format"),
        ([step("quiz", "abc-def1")], "welcome.steps[0].key", "invalid_format"),
        (
            [{**step("quiz", "abcdef"), "key": 5}],
            "welcome.steps[0].key",
            "invalid_type",
        ),
        ([step("quiz", "abcdef", title="")], "welcome.steps[0].title", "too_short"),
        (
            [step("quiz", "abcdef", title="a" * 121)],
            "welcome.steps[0].title",
            "too_long",
        ),
        (
            [step("quiz", "abcdef", revision=0)],
            "welcome.steps[0].revision",
            "out_of_range",
        ),
        (
            [step("quiz", "abcdef", revision=10000)],
            "welcome.steps[0].revision",
            "out_of_range",
        ),
        (
            [step("quiz", "abcdef", revision=True)],
            "welcome.steps[0].revision",
            "invalid_type",
        ),
        (
            [step("quiz", "abcdef", mandatory="yes")],
            "welcome.steps[0].mandatory",
            "invalid_type",
        ),
        ([step("quiz", "abcdef", cmid=0)], "welcome.steps[0].cmid", "out_of_range"),
        ([step("quiz", "abcdef", cmid=-3)], "welcome.steps[0].cmid", "out_of_range"),
        ([step("quiz", "abcdef", cmid="7")], "welcome.steps[0].cmid", "invalid_type"),
        (
            [step("quiz", "abcdef", intro="a" * 301)],
            "welcome.steps[0].intro",
            "too_long",
        ),
        (
            [step("quiz", "abcdef", outcomeBelow="a" * 301)],
            "welcome.steps[0].outcomeBelow",
            "too_long",
        ),
        (
            [step("welcome", "abcdef", message="a" * 301)],
            "welcome.steps[0].message",
            "too_long",
        ),
        (
            [step("welcome", "abcdef", buttonLabel="a" * 41)],
            "welcome.steps[0].buttonLabel",
            "too_long",
        ),
        (
            [step("html", "abcdef", ack={"label": ""})],
            "welcome.steps[0].ack.label",
            "too_short",
        ),
        (
            [step("html", "abcdef", ack={"label": "a" * 161})],
            "welcome.steps[0].ack.label",
            "too_long",
        ),
        (
            [step("html", "abcdef", ack={"label": "ok", "x": 1})],
            "welcome.steps[0].ack.x",
            "unknown_field",
        ),
        (
            [step("html", "abcdef", minSeconds=601)],
            "welcome.steps[0].minSeconds",
            "out_of_range",
        ),
        (
            [step("html", "abcdef", minSeconds=-1)],
            "welcome.steps[0].minSeconds",
            "out_of_range",
        ),
        (
            [step("html", "abcdef", minSeconds=1.5)],
            "welcome.steps[0].minSeconds",
            "invalid_type",
        ),
        ([step("html", "abcdef", html="")], "welcome.steps[0].html", "too_short"),
        (
            [step("pdf", "abcdef", assetId="nope")],
            "welcome.steps[0].assetId",
            "invalid_asset_id",
        ),
        (
            [step("pdf", "abcdef", assetId=None)],
            "welcome.steps[0].assetId",
            "invalid_asset_id",
        ),
        (
            [step("video", "abcdef", video={"provider": "vimeo", "videoId": "x/../y"})],
            "welcome.steps[0].video.videoId",
            "video_invalid",
        ),
        (
            [
                step(
                    "video",
                    "abcdef",
                    video={"provider": "dailymotion", "videoId": "abcdef"},
                )
            ],
            "welcome.steps[0].video.provider",
            "video_invalid",
        ),
        (
            [step("video", "abcdef", video={"url": "https://youtu.be/dQw4w9WgXcQ"})],
            "welcome.steps[0].video.url",
            "unknown_field",
        ),
        (
            [step("video", "abcdef", video="dQw4w9WgXcQ")],
            "welcome.steps[0].video",
            "invalid_type",
        ),
        (
            [step("quiz", "abcdef", surprise=1)],
            "welcome.steps[0].surprise",
            "unknown_field",
        ),
        (
            [step("quiz", "abcdef", html="<p>x</p>")],
            "welcome.steps[0].html",
            "unknown_field",
        ),
        (
            [step("html", "lectura01"), step("quiz", "abcdef", cmid="x")],
            "welcome.steps[1].cmid",
            "invalid_type",
        ),
        ([], "welcome.steps", "too_short"),
    ],
)
def test_structural_errors_name_the_path_and_the_code(
    steps: list[dict[str, Any]], path: str, code: str
) -> None:
    assert (path, code) in codes({"steps": steps})


def test_more_than_twelve_steps_is_one_clear_error() -> None:
    steps = [step("html", f"lectura{i:02d}") for i in range(13)]

    assert codes({"steps": steps}) == [("welcome.steps", "steps_too_many")]


def test_a_huge_list_is_refused_without_parsing_each_step() -> None:
    assert codes({"steps": [{}] * 5000}) == [("welcome.steps", "steps_too_many")]


def test_steps_that_are_not_a_list_are_refused() -> None:
    assert codes({"steps": "a"}) == [("welcome.steps", "invalid_type")]
    assert codes({"steps": {"0": step("quiz", "abcdef")}}) == [
        ("welcome.steps", "invalid_type")
    ]


def test_problems_never_echo_the_input() -> None:
    secret = "{sup3r-s3cret-token}"

    found = problems_of({"steps": [step("quiz", "abcdef", intro=secret * 40)]})

    assert secret not in str(found)


# --- rules across the flow ------------------------------------------------------------


def test_a_key_may_not_repeat() -> None:
    steps = [
        step("html", "lectura01"),
        step("quiz", "otraclave"),
        step("html", "lectura01"),
    ]

    assert codes({"steps": steps}) == [("welcome.steps[2].key", "step_key_duplicate")]


def test_the_welcome_step_can_only_be_first() -> None:
    steps = [step("html", "lectura01"), step("welcome", "bienvenida")]

    assert codes({"steps": steps}) == [("welcome.steps[1]", "welcome_step_position")]


def test_there_can_be_only_one_welcome_step() -> None:
    steps = [step("welcome", "bienvenida"), step("welcome", "bienvenid2")]

    assert codes({"steps": steps}) == [("welcome.steps[1]", "welcome_step_position")]


def test_at_most_three_quizzes() -> None:
    steps = [step("quiz", f"cuestion{i}") for i in range(4)]

    assert codes({"steps": steps}) == [("welcome.steps[3]", "quiz_too_many")]


def test_every_problem_is_listed_not_just_the_first() -> None:
    steps = [
        step("html", "lectura01", html="<script>x</script>"),
        step("html", "lectura01", css="@import url(x.css);"),
        step("welcome", "bienvenida"),
    ]

    assert codes({"steps": steps}) == [
        ("welcome.steps[1].key", "step_key_duplicate"),
        ("welcome.steps[2]", "welcome_step_position"),
        ("welcome.steps[0].html", "html_forbidden_markup"),
        ("welcome.steps[1].css", "css_forbidden"),
    ]


def test_the_markup_of_the_whole_flow_has_a_budget() -> None:
    chunk = "<p>" + "a" * (90 * 1024) + "</p>"
    steps = [step("html", f"lectura{i:02d}", html=chunk) for i in range(5)]

    assert codes({"steps": steps}) == [("welcome.steps", "flow_too_large")]


def test_html_and_css_have_their_own_size_limits_in_bytes() -> None:
    html = "<p>" + "é" * (51 * 1024) + "</p>"  # 102 K bytes, under 100 K characters
    css = "p::after { content: '" + "é" * (26 * 1024) + "' }"

    found = codes({"steps": [step("html", "lectura01", html=html, css=css)]})

    assert found == [
        ("welcome.steps[0].html", "too_long"),
        ("welcome.steps[0].css", "too_long"),
    ]


def test_html_problems_name_the_offender_and_the_path() -> None:
    steps = [
        step("html", "lectura01", html="<p>ok</p>"),
        step("html", "lectura02", html="<svg/>"),
    ]

    found = problems_of({"steps": steps})

    assert [(p["path"], p["code"]) for p in found] == [
        ("welcome.steps[1].html", "html_forbidden_markup")
    ]
    assert "<svg>" in found[0]["message"]


def test_css_problems_name_the_offender_and_the_path() -> None:
    found = problems_of({"steps": [step("html", "lectura01", css="@import url(x);")]})

    assert [(p["path"], p["code"]) for p in found] == [
        ("welcome.steps[0].css", "css_forbidden")
    ]
    assert "@import" in found[0]["message"]


# --- legacy fields and steps ----------------------------------------------------------


@pytest.mark.parametrize(
    ("legacy", "path"),
    [
        ({"title": "Hola"}, "welcome.title"),
        ({"message": "Hola"}, "welcome.message"),
        ({"video": {"provider": "youtube", "videoId": "dQw4w9WgXcQ"}}, "welcome.video"),
        ({"playOnEnter": True}, "welcome.playOnEnter"),
        ({"playOnEnter": False}, "welcome.playOnEnter"),
    ],
)
def test_legacy_fields_next_to_steps_are_refused(
    legacy: dict[str, Any], path: str
) -> None:
    raw = {"steps": [step("quiz", "abcdef")], **legacy}

    assert codes(raw) == [(path, "legacy_fields_with_steps")]


def test_null_legacy_fields_next_to_steps_are_fine() -> None:
    raw = {
        "steps": [step("quiz", "abcdef")],
        "title": None,
        "message": None,
        "video": None,
        "playOnEnter": None,
    }

    assert problems_of(raw) == []


def test_without_steps_the_legacy_fields_mean_what_they_always_did() -> None:
    for raw in ({}, {"steps": None}):
        welcome = parse({**raw, "title": "Hola", "playOnEnter": False})
        assert welcome.steps is None
        assert welcome.play_on_enter is False
        assert flow_problems(welcome) == []


def test_a_null_play_on_enter_without_steps_is_still_refused() -> None:
    assert ("welcome.playOnEnter", "invalid_type") in codes({"playOnEnter": None})
    assert ("welcome.playOnEnter", "invalid_type") in codes(
        {"steps": None, "playOnEnter": None}
    )


# --- the stored form and the snapshot ---------------------------------------------------


def test_a_welcome_without_steps_stores_no_steps_key() -> None:
    settings = MapSettings.model_validate({"welcome": {"title": "Hola"}})

    assert "steps" not in settings.to_stored()["welcome"]


def test_a_curtain_stored_before_flows_existed_reads_back_unchanged() -> None:
    stored = {
        "welcome": {
            "enabled": True,
            "title": "Hola",
            "message": None,
            "video": None,
            "playOnEnter": True,
            "show": "always",
        }
    }

    settings = MapSettings.model_validate(stored)

    assert settings.welcome.steps is None
    assert settings.to_stored()["welcome"] == stored["welcome"]


def test_a_flow_changes_the_snapshot_and_counts_as_a_settings_change() -> None:
    base = make_inputs()
    with_flow = make_inputs(
        settings=MapSettings.model_validate({"welcome": {"steps": FULL_FLOW}})
    )

    old, new = build_snapshot(base), build_snapshot(with_flow)

    assert content_hash(old) != content_hash(new)
    assert diff_snapshots(old, new).settings_changed is True
    assert new["settings"]["welcome"]["steps"][0]["key"] == "bienvenida"


def test_the_snapshot_of_a_flow_is_stable_and_order_independent() -> None:
    settings = MapSettings.model_validate({"welcome": {"steps": FULL_FLOW}})
    reordered = copy.deepcopy(FULL_FLOW)
    for s in reordered:
        s_items = list(s.items())
        s.clear()
        s.update(reversed(s_items))
    again = MapSettings.model_validate({"welcome": {"steps": reordered}})

    one = content_hash(build_snapshot(make_inputs(settings=settings)))
    two = content_hash(build_snapshot(make_inputs(settings=again)))

    assert one == two


def test_a_snapshot_with_a_flow_loads_back() -> None:
    settings = MapSettings.model_validate({"welcome": {"steps": FULL_FLOW}})
    snapshot = build_snapshot(make_inputs(settings=settings))

    assert MapSettings.model_validate(snapshot["settings"]) == settings


# Content hashes computed with the code that existed BEFORE flows (the audio
# covers tip, f0c22c8). A curtain without steps must keep building them, or every
# publication that has one would suddenly read as changed.
PRE_FLOW_HASHES = {
    "defaults": (
        {},
        "16819c90443557ced504dadfa02c0264cfe4684fef8e8b410d25f25c79ca40bb",
    ),
    "disabled": (
        {"welcome": {"enabled": False}},
        "0fb666394d285956ac7595db856f736be27ed96747407a6c5a52542e0773ddee",
    ),
    "title only": (
        {"welcome": {"title": "Bienvenido a {curso}"}},
        "c4ef07fa39fe17bfdffe2080164537941c62fbaf7f3c7cd71980a7c0b008a1f6",
    ),
    "full legacy": (
        {
            "welcome": {
                "enabled": True,
                "title": "Hola",
                "message": "Explora\nel mapa.",
                "video": {"provider": "youtube", "videoId": "dQw4w9WgXcQ"},
                "playOnEnter": False,
                "show": "always",
            }
        },
        "53f995d3323c622835e89281837f3874887b2e0c6303360a5f77bbabd76c83c8",
    ),
    "with other settings": (
        {
            "mode": "guided",
            "fit": "contain",
            "initialView": {"x": 0.25, "y": 0.75, "zoom": 1.5},
            "intro": "flyin",
            "welcome": {"message": "Hola", "show": "always"},
        },
        "12e4e25812079738f147de4cf65de577ebb50dcf430edd94632acc4e30583dd3",
    ),
}


@pytest.mark.parametrize(
    ("raw", "expected"), PRE_FLOW_HASHES.values(), ids=list(PRE_FLOW_HASHES)
)
def test_publications_made_before_flows_keep_their_hash(
    raw: dict[str, Any], expected: str
) -> None:
    settings = MapSettings.model_validate(raw)

    assert content_hash(build_snapshot(make_inputs(settings=settings))) == expected
