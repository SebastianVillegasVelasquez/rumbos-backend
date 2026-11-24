# ruff: noqa: F811
"""The signed statement is what the plugin acts on (C4): it marks a student's
content complete only from a verified statement of a passed attempt, never from
anything the browser says. This pins the four claims that decision rests on, for
every purpose and outcome, in the statement as signed and in both published
schemas."""

import json
from pathlib import Path
from typing import Any

import pytest

from app.passback.port import FakeMoodleResultPort
from app.passback.statement import verify
from tests.test_assessment_api import (  # noqa: F401
    FakeClock,
    clock,
    draft_content_is_served,
    env,
    exams,
)
from tests.test_passback import PUBLIC, finish, outbox, run

# The claims the plugin needs to decide "mark all the content complete".
ACTED_ON = {"purpose", "passed", "completes_course", "jti"}
SCHEMAS = Path(__file__).resolve().parent.parent / "contracts" / "schemas"


@pytest.mark.parametrize("schema", ["result-statement", "attempt-statement"])
def test_both_published_schemas_require_the_claims_the_plugin_acts_on(
    schema: str,
) -> None:
    document = json.loads((SCHEMAS / f"{schema}.schema.json").read_text("utf-8"))
    assert ACTED_ON <= set(document["required"])
    assert document["properties"]["purpose"]["enum"] == ["check_in", "final"]


@pytest.mark.parametrize(
    ("purpose", "answers", "passed", "completes"),
    [
        ("check_in", "right", True, True),
        ("check_in", "wrong", False, False),
        ("final", "right", True, True),
        ("final", "wrong", False, False),
    ],
)
async def test_the_signed_statement_says_what_the_plugin_needs(
    env: dict[str, Any], purpose: str, answers: str, passed: bool, completes: bool
) -> None:
    await env["setup"]({"assessments": exams()})
    s = await env["student"]()
    attempt = await finish(s, answers, purpose)
    port = FakeMoodleResultPort()
    assert (await run(env["engine"], port))["sent"] == 1

    claims = verify(port.received[0][2], PUBLIC, "rumbos-backend")
    assert ACTED_ON <= set(claims)
    assert claims["purpose"] == purpose
    assert claims["passed"] is passed and attempt["passed"] is passed
    assert claims["completes_course"] is completes
    assert claims["jti"] == attempt["attempt_id"]  # the attempt id, applied once
    # Whose course to complete, signed with it.
    assert (claims["sub"], claims["course_id"], claims["aud"]) == (
        "student-1",
        7,
        "moodle",
    )


async def test_a_pass_that_does_not_exempt_does_not_complete(
    env: dict[str, Any],
) -> None:
    # The plugin acts on `completes_course`, not on `passed` alone: with the
    # exemption off a passed check-in is a pass that completes nothing.
    await env["setup"]({"assessments": exams(exemption={"enabled": False})})
    s = await env["student"]()
    await finish(s, "right", "check_in")
    (row,) = await outbox(env["engine"])
    assert (row.statement["passed"], row.statement["completes_course"]) == (True, False)


async def test_a_trial_run_produces_no_statement_to_act_on(
    env: dict[str, Any],
) -> None:
    await env["setup"]({"assessments": exams()})
    editor = await env["editor"]()
    await finish(editor, "right", "check_in")
    assert await outbox(env["engine"]) == []
