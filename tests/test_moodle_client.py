"""HTTP-level tests for `HttpMoodleClient`, using `httpx.MockTransport`."""

from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import SecretStr

from app.moodle.client import HttpMoodleClient
from app.moodle.exceptions import (
    MoodleAuthError,
    MoodleCourseNotFoundError,
    MoodleError,
    MoodleUnavailableError,
)

TOKEN = "s3cr3t-token-value"
BASE_URL = "https://moodle.test"

Handler = Callable[[httpx.Request], httpx.Response]

# Shaped after Moodle's documented `core_course_get_contents` output, with
# extra fields we deliberately don't model, to prove they are ignored.
COURSE_CONTENTS: list[dict[str, Any]] = [
    {
        "id": 10,
        "name": "General",
        "visible": 1,
        "summary": "",
        "summaryformat": 1,
        "section": 0,
        "hiddenbynumsections": 0,
        "uservisible": True,
        "modules": [
            {
                "id": 101,
                "url": "https://moodle.test/mod/forum/view.php?id=101",
                "name": "Announcements",
                "instance": 1,
                "visible": 1,
                "uservisible": True,
                "visibleoncoursepage": 1,
                "modicon": "https://moodle.test/theme/image.php/forum/icon",
                "modname": "forum",
                "modplural": "Forums",
                "completion": 0,
                "dates": [],
                "contents": [],
            }
        ],
    },
    {
        "id": 11,
        "name": "Unit 1",
        "visible": 1,
        "section": 1,
        "modules": [
            {
                "id": 102,
                "url": "https://moodle.test/mod/quiz/view.php?id=102",
                "name": "Quiz 1",
                "visible": 1,
                "uservisible": True,
                "modname": "quiz",
                "completion": 2,
            },
            {"id": 103, "name": "Intro text", "modname": "label"},
        ],
    },
    {"id": 12, "name": "Empty", "section": 2},
]


def _client(handler: Handler) -> HttpMoodleClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return HttpMoodleClient(http, BASE_URL, SecretStr(TOKEN))


def _json(body: Any, status: int = 200) -> Handler:
    return lambda request: httpx.Response(status, json=body)


async def test_get_course_contents_parses_sections_and_modules() -> None:
    sections = await _client(_json(COURSE_CONTENTS)).get_course_contents(5)

    assert [s.name for s in sections] == ["General", "Unit 1", "Empty"]
    unit = sections[1]
    assert unit.section == 1
    assert [(m.id, m.modname) for m in unit.modules] == [(102, "quiz"), (103, "label")]
    quiz = unit.modules[0]
    assert quiz.url == "https://moodle.test/mod/quiz/view.php?id=102"
    assert quiz.completion == 2
    # Missing optional fields are tolerated.
    label = unit.modules[1]
    assert label.url is None and label.visible is None and label.uservisible is None
    assert sections[2].modules == []


async def test_get_site_info_parses_and_ignores_extra_fields() -> None:
    body = {"sitename": "Campus", "username": "ws", "userid": 7, "functions": []}
    info = await _client(_json(body)).get_site_info()
    assert (info.sitename, info.userid) == ("Campus", 7)


async def test_request_is_a_form_post_with_token_only_in_the_body() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[])

    await _client(handler).get_course_contents(5)

    (request,) = seen
    assert request.method == "POST"
    assert str(request.url) == f"{BASE_URL}/webservice/rest/server.php"
    assert request.url.query == b""
    assert TOKEN not in str(request.url)
    form = parse_qs(request.content.decode())
    assert form == {
        "wstoken": [TOKEN],
        "wsfunction": ["core_course_get_contents"],
        "moodlewsrestformat": ["json"],
        "courseid": ["5"],
    }


@pytest.mark.parametrize(
    ("errorcode", "expected"),
    [
        ("invalidtoken", MoodleAuthError),
        ("accessexception", MoodleAuthError),
        ("dml_missing_record_exception", MoodleCourseNotFoundError),
        ("invalidcourseid", MoodleCourseNotFoundError),
        ("something_else", MoodleError),
    ],
)
async def test_http_200_error_body_maps_to_typed_exception(
    errorcode: str, expected: type[MoodleError]
) -> None:
    body = {"exception": "moodle_exception", "errorcode": errorcode, "message": "x"}
    with pytest.raises(expected) as info:
        await _client(_json(body)).get_course_contents(5)
    # Specific subclasses must not be swallowed by the base-class fallback.
    assert type(info.value) is expected


async def test_5xx_and_non_json_map_to_unavailable() -> None:
    with pytest.raises(MoodleUnavailableError):
        await _client(lambda r: httpx.Response(503)).get_site_info()
    with pytest.raises(MoodleUnavailableError):
        await _client(lambda r: httpx.Response(200, text="<html>")).get_site_info()


async def test_unexpected_shape_is_a_generic_moodle_error() -> None:
    with pytest.raises(MoodleError) as info:
        await _client(_json({"not": "a list"})).get_course_contents(5)
    assert type(info.value) is MoodleError


@pytest.mark.parametrize(
    "error",
    [
        httpx.ReadTimeout("slow"),
        httpx.ConnectTimeout("slow"),
        httpx.ConnectError("refused"),
    ],
)
async def test_timeouts_and_connection_errors_map_to_unavailable(
    error: httpx.HTTPError,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error

    with pytest.raises(MoodleUnavailableError):
        await _client(handler).get_course_contents(5)


@pytest.mark.parametrize("failure", ["auth", "notfound", "generic", "5xx", "network"])
async def test_token_never_appears_in_exception_text(failure: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # Worst case: Moodle and the transport echo the token back.
        echo = f"{request.content.decode()} {request.url}"
        match failure:
            case "auth":
                body = {"errorcode": "invalidtoken", "message": echo}
            case "notfound":
                body = {"errorcode": "invalidcourseid", "message": echo}
            case "generic":
                body = {"errorcode": "boom", "message": echo, "debuginfo": echo}
            case "5xx":
                return httpx.Response(500, text=echo)
            case _:
                raise httpx.ConnectError(echo, request=request)
        return httpx.Response(200, json=body)

    with pytest.raises(MoodleError) as info:
        await _client(handler).get_course_contents(5)

    exc: BaseException | None = info.value
    while exc is not None:  # everything a rendered traceback would show
        assert TOKEN not in str(exc)
        assert TOKEN not in repr(exc)
        exc = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)


async def test_token_is_not_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("DEBUG")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(request.content.decode())

    with pytest.raises(MoodleUnavailableError):
        await _client(handler).get_site_info()
    assert TOKEN not in caplog.text
