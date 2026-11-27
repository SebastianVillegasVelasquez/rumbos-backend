"""`app.scripts.closing check-service-account`: what the service account can read
of a student's completion. Run against the real HTTP client over a fake Moodle,
so the calls it makes (reads only) and what it prints are what is checked."""

import json
import uuid
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import SecretStr

from app.models.tenant import LEGACY_TENANT_ID, LEGACY_TENANT_ISSUER
from app.moodle.client import HttpMoodleClient
from app.scripts import closing as cli
from app.tenancy.scope import Scope
from tests import test_closing_api as api
from tests.sessions import new_session
from tests.test_assessment_api import COURSE

env = api.env
clock = api.clock
draft_content_is_served = api.draft_content_is_served

TOKEN = "tok-5ecret-5ervice-t0ken"
USER = 987654
SCORM_A, SCORM_B, QUIZ, HIDDEN = api.SCORM_A, api.SCORM_B, api.QUIZ, api.HIDDEN_SCORM
GONE = 999  # a bubble whose activity the contents do not show
BY_GROUP = '{"op":"&","c":[{"type":"group","id":3}],"showc":[true]}'


def module(cmid: int, modname: str, **over: Any) -> dict[str, Any]:
    return {
        "id": cmid,
        "name": f"Secret name {cmid}",
        "modname": modname,
        "url": f"https://m/mod/{modname}/view.php?id={cmid}",
        "completion": 2,
        **over,
    }


CONTENTS = [
    {
        "id": 1,
        "section": 1,
        "modules": [
            module(SCORM_A, "scorm"),
            module(SCORM_B, "scorm", availability=BY_GROUP, uservisible=False),
            module(QUIZ, "quiz"),
            module(HIDDEN, "scorm", visible=0),
            module(300, "label", completion=0),
        ],
    }
]


def status(cmid: int, modname: str = "scorm", state: int = 1) -> dict[str, Any]:
    return {"cmid": cmid, "modname": modname, "state": state, "tracking": 2}


class FakeSite:
    def __init__(self, completion: Any, contents: Any = CONTENTS) -> None:
        self.completion = completion
        self.contents = contents
        self.calls: list[dict[str, str]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.calls.append(form)
        answers = {
            "core_course_get_contents": self.contents,
            "core_completion_get_activities_completion_status": self.completion,
        }
        return httpx.Response(200, content=json.dumps(answers[form["wsfunction"]]))


async def run(
    env: dict[str, Any],
    site: FakeSite,
    *,
    tenant: uuid.UUID = LEGACY_TENANT_ID,
    url: str = LEGACY_TENANT_ISSUER,
) -> tuple[int, str]:
    lines: list[str] = []
    transport = httpx.MockTransport(site.handle)
    async with (
        httpx.AsyncClient(transport=transport) as http,
        new_session(env["engine"], Scope(tenant)) as session,
    ):
        moodle = HttpMoodleClient(http, url, SecretStr(TOKEN))
        code = await cli.check_service_account(
            session,
            moodle,
            tenant,
            COURSE,
            USER,
            moodle_base_url=url,
            out=lines.append,
        )
    return code, "\n".join(lines)


async def published_map(env: dict[str, Any]) -> None:
    await api.world(env, bubbles=(SCORM_A, SCORM_B, QUIZ, HIDDEN, GONE))


async def test_it_reports_what_comes_back_and_what_is_left_out(
    env: dict[str, Any],
) -> None:
    await published_map(env)
    site = FakeSite(
        {
            "statuses": [status(SCORM_A, state=2), status(QUIZ, "quiz", 0)],
            "warnings": [],
        }
    )
    code, text = await run(env, site)
    assert code == 0, text
    assert "completion: readable, 2 activities reported, 0 warning(s)" in text
    # Omitted: the restricted and the hidden one; the untracked label is not.
    assert (
        "omitted (tracked in the course contents, not in the completion answer): 2"
        in text
    )
    assert (
        f"cmid={SCORM_B} modname=scorm hidden=no restricted_by=group "
        "visible_to_service_account=no" in text
    )
    assert f"cmid={HIDDEN} modname=scorm hidden=yes" in text
    assert "cmid=300" not in text
    # For the gate: neither the hidden one nor the one the service account cannot
    # see is required. Only the second is flagged: students may well see it.
    assert "gate: 1 required SCORM on the published map, 1 reported, 0 omitted" in text
    assert f"cmid={SCORM_A} state=2 tracking=2" in text
    assert "the gate does NOT require: 2" in text
    flagged = [line for line in text.splitlines() if "check whether students" in line]
    assert len(flagged) == 1 and f"cmid={SCORM_B} " in flagged[0]
    assert f"the gate would NOT require them): [{GONE}]" in text
    assert "WARNING" not in text
    # Two reads, and only those; the user id reaches Moodle and nothing else.
    assert [c["wsfunction"] for c in site.calls] == [
        "core_course_get_contents",
        "core_completion_get_activities_completion_status",
    ]
    assert (site.calls[1]["userid"], site.calls[1]["courseid"]) == (str(USER), "7")
    for private in (TOKEN, str(USER), "Secret name"):
        assert private not in text


@pytest.mark.parametrize(
    ("errorcode", "kind", "hint"),
    [
        ("accessexception", "MoodleAuthError", "web service"),
        ("requiredcapability", "MoodleError", "report/progress:view"),
        ("accessdenied", "MoodleError", "separate groups"),
        ("something_new", "MoodleError", None),
    ],
)
async def test_a_refused_read_says_why_and_exits_1(
    env: dict[str, Any], errorcode: str, kind: str, hint: str | None
) -> None:
    await published_map(env)
    refused = {
        "exception": "moodle_exception",
        "errorcode": errorcode,
        "message": f"private detail about user {USER} and {TOKEN}",
    }
    code, text = await run(env, FakeSite(refused))
    assert code == 1
    assert f"completion: NOT readable ({kind}, errorcode={errorcode})" in text
    assert ("likely cause" in text) == (hint is not None)
    if hint:
        assert hint in text
    assert "private detail" not in text and TOKEN not in text
    assert str(USER) not in text


async def test_an_errorcode_that_is_not_an_identifier_is_not_printed(
    env: dict[str, Any],
) -> None:
    await published_map(env)
    refused = {"exception": "x", "errorcode": f"user {USER} has token {TOKEN}"}
    code, text = await run(env, FakeSite(refused))
    assert code == 1 and "errorcode=-" in text
    assert TOKEN not in text and str(USER) not in text


async def test_an_unexpected_shape_is_not_taken_as_no_completion(
    env: dict[str, Any],
) -> None:
    await published_map(env)
    code, text = await run(env, FakeSite({"unexpected": True}))
    assert code == 1 and "completion: NOT readable" in text


async def test_unreadable_contents_exit_1_without_a_comparison(
    env: dict[str, Any],
) -> None:
    await published_map(env)
    site = FakeSite(
        {"statuses": [status(SCORM_A)], "warnings": [{"warningcode": "w1"}]},
        contents={"exception": "x", "errorcode": "invalidtoken"},
    )
    code, text = await run(env, site)
    assert code == 1
    assert "course contents: NOT readable (MoodleAuthError" in text
    assert "completion: readable, 1 activities reported, 1 warning(s) w1" in text
    assert "no comparison" in text


async def test_without_a_published_exam_map_it_still_lists_the_omissions(
    env: dict[str, Any],
) -> None:
    code, text = await run(env, FakeSite({"statuses": [], "warnings": []}))
    assert code == 0
    assert "completion answer): 4" in text
    assert "no published map" in text


async def test_a_required_scorm_the_answer_omits_would_block_everybody(
    env: dict[str, Any],
) -> None:
    """The contents show it as visible, yet completion does not report it: the
    gate requires it and would count it as not completed."""
    await published_map(env)
    code, text = await run(
        env, FakeSite({"statuses": [status(SCORM_B)], "warnings": []})
    )
    assert code == 0
    assert f"cmid={SCORM_A} OMITTED" in text
    assert "RESULT: completion was read; 1 required SCORM omitted" in text


async def test_a_moodle_url_that_is_not_the_tenants_is_flagged(
    env: dict[str, Any],
) -> None:
    site = FakeSite({"statuses": [], "warnings": []})
    _, text = await run(env, site, url="https://other.example")
    assert "WARNING: they differ" in text


async def test_an_unknown_tenant_asks_moodle_nothing(env: dict[str, Any]) -> None:
    site = FakeSite({"statuses": [], "warnings": []})
    code, _ = await run(env, site, tenant=uuid.uuid4())
    assert code == 2 and site.calls == []


@pytest.mark.parametrize("uid", ["0", "-1", "2147483648", "abc"])
def test_the_command_refuses_what_is_not_a_user_id(uid: str) -> None:
    with pytest.raises(SystemExit):
        cli._parser().parse_args(
            [
                "check-service-account",
                "--tenant",
                str(LEGACY_TENANT_ID),
                "--uid",
                uid,
                "--course",
                "7",
            ]
        )
