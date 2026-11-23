"""The final-exam gate rule on its own: which SCORM bubbles are required and
what `decide` answers for every combination. No database, no network."""

from typing import Any

import pytest

from app.closing.gate import Decision, Progress, decide, required_scorm_cmids
from app.moodle.schemas import MoodleModule, MoodleSection


def module(cmid: int, modname: str = "scorm", **over: Any) -> MoodleModule:
    fields: dict[str, Any] = {
        "id": cmid,
        "name": f"Activity {cmid}",
        "modname": modname,
        "url": f"https://m/mod/{modname}/view.php?id={cmid}",
        "completion": 2,
    }
    return MoodleModule(**{**fields, **over})


def section(*modules: MoodleModule, visible: bool = True) -> MoodleSection:
    return MoodleSection(id=1, section=1, visible=visible, modules=list(modules))


# --- which bubbles are required ------------------------------------------------------


def test_the_required_content_is_the_visible_scorm_bubbles() -> None:
    sections = [section(module(1), module(2), module(3, "quiz"), module(4, "page"))]
    assert required_scorm_cmids(sections, [1, 2, 3, 4]) == {1, 2}


def test_a_scorm_that_is_not_a_bubble_of_the_map_does_not_count() -> None:
    sections = [section(module(1), module(2))]
    assert required_scorm_cmids(sections, [1]) == {1}


def test_a_hidden_module_does_not_count() -> None:
    sections = [
        section(module(1), module(2, visible=False), module(3, uservisible=False))
    ]
    assert required_scorm_cmids(sections, [1, 2, 3]) == {1}


def test_a_module_in_a_hidden_section_does_not_count() -> None:
    sections = [section(module(1), visible=False), section(module(2))]
    assert required_scorm_cmids(sections, [1, 2]) == {2}


def test_a_deleted_activity_does_not_count() -> None:
    assert required_scorm_cmids([section(module(1))], [1, 99]) == {1}


def test_a_scorm_moodle_does_not_track_does_not_count() -> None:
    sections = [section(module(1, completion=0), module(2, completion=1))]
    assert required_scorm_cmids(sections, [1, 2]) == {2}


def test_a_restricted_scorm_still_counts() -> None:
    restricted = module(1, availability='{"op":"&","c":[],"showc":[]}')
    assert required_scorm_cmids([section(restricted)], [1]) == {1}


def test_a_map_with_no_scorm_requires_nothing() -> None:
    assert required_scorm_cmids([section(module(3, "quiz"))], [3]) == frozenset()


# --- the verdict -----------------------------------------------------------------------

OPEN: dict[str, Any] = {
    "enabled": True,
    "has_final_step": True,
    "form_served": True,
    "preview": False,
    "already_passed": False,
    "attempts_left": True,
}


def verdict(**over: Any) -> Decision:
    return decide(**{**OPEN, **over})


@pytest.mark.parametrize(
    ("facts", "available", "reason"),
    [
        ({"enabled": False}, False, "not_enabled"),
        ({"has_final_step": False}, False, "no_final_step"),
        ({"form_served": False}, False, "form_unavailable"),
        ({"already_passed": True}, False, "already_passed"),
        ({"attempts_left": False}, False, "no_attempts_left"),
        ({"required": None}, False, "progress_unavailable"),
        (
            {"required": frozenset({1}), "completed": None},
            False,
            "progress_unavailable",
        ),
        ({}, True, None),
    ],
)
def test_the_verdict_for_each_condition(
    facts: dict[str, Any], available: bool, reason: str | None
) -> None:
    got = verdict(**facts)
    assert (got.available, got.reason) == (available, reason)


def test_all_required_scorm_completed_opens_the_exam() -> None:
    got = verdict(required=frozenset({1, 2}), completed=frozenset({1, 2, 9}))
    assert got == Decision(True, None, Progress(2, 2))


def test_one_missing_scorm_keeps_it_closed_and_reports_counts() -> None:
    got = verdict(required=frozenset({1, 2}), completed=frozenset({1}))
    assert got == Decision(False, "scorm_incomplete", Progress(1, 2))


def test_a_revoked_completion_closes_it_again() -> None:
    required = frozenset({1, 2})
    assert verdict(required=required, completed=frozenset({1, 2})).available
    assert not verdict(required=required, completed=frozenset({1})).available


def test_completing_an_activity_that_is_not_required_does_not_help() -> None:
    got = verdict(required=frozenset({1, 2}), completed=frozenset({1, 7, 8}))
    assert got.progress == Progress(1, 2)


def test_zero_scorm_opens_it_immediately() -> None:
    assert verdict(required=frozenset(), completed=None) == Decision(
        True, None, Progress(0, 0)
    )


def test_an_editor_in_preview_skips_the_student_conditions() -> None:
    for over in (
        {"already_passed": True},
        {"attempts_left": False},
        {"required": frozenset({1}), "completed": frozenset()},
        {"required": None},
    ):
        assert verdict(preview=True, **over) == Decision(True)


def test_preview_still_needs_the_flow_to_be_configured() -> None:
    assert verdict(preview=True, enabled=False).reason == "not_enabled"
    assert verdict(preview=True, has_final_step=False).reason == "no_final_step"
    assert verdict(preview=True, form_served=False).reason == "form_unavailable"


def test_the_first_answer_without_scorm_leaves_moodle_out() -> None:
    assert verdict(check_scorm=False, required=None) == Decision(True)
    assert verdict(check_scorm=False, already_passed=True).reason == "already_passed"


def test_restriction_kinds_reads_every_condition_in_the_tree() -> None:
    from app.closing.gate import restriction_kinds

    nested = (
        '{"op":"|","c":[{"type":"group","id":1},'
        '{"op":"&","c":[{"type":"date","d":">=","t":5},{"type":"grouping","id":2}]}],'
        '"show":true}'
    )
    assert restriction_kinds(nested) == ["date", "group", "grouping"]
    assert restriction_kinds(None) == [] and restriction_kinds("") == []
    assert restriction_kinds("not json") == ["unreadable"]


def test_without_a_user_id_the_scorm_cannot_be_checked() -> None:
    got = verdict(required=frozenset({1}), completed=None, user_known=False)
    assert (got.available, got.reason) == (False, "user_unlinked")
    # ... but nothing to check means nothing to identify
    assert verdict(required=frozenset(), user_known=False).available
