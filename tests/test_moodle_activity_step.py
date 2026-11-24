"""The `moodle_activity` step: an activity of the Moodle course opened from a
flow. Allowed in the welcome and the closing flow (one validator table), keyed
like every step, and never carrying a URL."""

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from app.main import app
from app.schemas.map_settings import MapSettings
from app.schemas.welcome import ClosingFlow, Welcome
from app.welcome_flow.flow import flow_problems
from tests.editor_client import EditorClient
from tests.fakes import InMemoryMoodleClient
from tests.test_publications_api import full_map, make_map, publish
from tests.test_welcome_api import put_settings


@pytest.fixture
async def client(engine: Any) -> AsyncIterator[httpx.AsyncClient]:
    app.state.db_engine = engine
    app.state.moodle_client = InMemoryMoodleClient()
    transport = httpx.ASGITransport(app=app)
    async with EditorClient(transport=transport, base_url="http://t/api/v1") as c:
        yield c


def activity(key: str = "actividad1", **over: Any) -> dict[str, Any]:
    return {
        "type": "moodle_activity",
        "key": key,
        "title": "Guia de estudio",
        "cmid": 42,
        **over,
    }


def problems_of(r: httpx.Response) -> list[tuple[str, str]]:
    return [(p["path"], p["code"]) for p in r.json()["detail"]["problems"]]


FLOWS = ["welcome", "closing"]


# --- the shape -----------------------------------------------------------------------


@pytest.mark.parametrize("flow", FLOWS)
def test_the_step_is_allowed_in_both_flows(flow: str) -> None:
    model = ClosingFlow if flow == "closing" else Welcome
    parsed = model.model_validate({"steps": [activity()]})
    assert flow_problems(parsed, name=flow) == []  # type: ignore[arg-type]


@pytest.mark.parametrize("flow", FLOWS)
def test_it_is_stored_with_exactly_its_declared_fields(flow: str) -> None:
    model = ClosingFlow if flow == "closing" else Welcome
    stored = model.model_validate({"steps": [activity()]}).model_dump(
        by_alias=True, mode="json"
    )["steps"][0]
    assert stored == {
        "type": "moodle_activity",
        "key": "actividad1",
        "title": "Guia de estudio",
        "cmid": 42,
        "mandatory": True,
        "revision": 1,
    }


@pytest.mark.parametrize("cmid", [0, -1, -42])
def test_a_cmid_must_be_positive(cmid: int) -> None:
    with pytest.raises(ValueError, match="greater_than|greater than"):
        Welcome.model_validate({"steps": [activity(cmid=cmid)]})


@pytest.mark.parametrize("cmid", ["42", 4.2, True, None])
def test_a_cmid_must_be_an_integer(cmid: Any) -> None:
    with pytest.raises(ValueError):
        Welcome.model_validate({"steps": [activity(cmid=cmid)]})


def test_the_cmid_and_the_title_are_required() -> None:
    no_cmid = {k: v for k, v in activity().items() if k != "cmid"}
    no_title = {k: v for k, v in activity().items() if k != "title"}
    for step in (no_cmid, no_title):
        with pytest.raises(ValueError):
            Welcome.model_validate({"steps": [step]})
    with pytest.raises(ValueError):
        Welcome.model_validate({"steps": [activity(title="")]})
    with pytest.raises(ValueError):
        Welcome.model_validate({"steps": [activity(title="x" * 121)]})


@pytest.mark.parametrize("field", ["url", "link", "href", "assetId"])
def test_no_undeclared_field_is_accepted(field: str) -> None:
    with pytest.raises(ValueError):
        Welcome.model_validate({"steps": [activity(**{field: "https://x.test/"})]})


def test_a_map_stored_before_the_step_existed_loads_unchanged() -> None:
    old = {
        "steps": [
            {"type": "html", "key": "html0001", "title": "H", "html": "<p>x</p>"},
            {"type": "quiz", "key": "quiz0001", "title": "Q", "cmid": 5},
        ]
    }
    before = MapSettings.model_validate({"welcome": old}).to_stored()
    assert MapSettings.model_validate(before).to_stored() == before


# --- through the API -------------------------------------------------------------------


@pytest.mark.parametrize("flow", FLOWS)
async def test_the_step_is_saved_read_back_and_published(
    client: httpx.AsyncClient, flow: str
) -> None:
    created = await make_map(client)
    r = await put_settings(client, created["id"], {flow: {"steps": [activity()]}})
    assert r.status_code == 200, r.text
    step = (await full_map(client, created["id"]))["settings"][flow]["steps"][0]
    assert (step["type"], step["cmid"]) == ("moodle_activity", 42)
    await publish(client, created["id"])


async def test_both_flows_can_hold_activities_with_their_own_keys(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    r = await put_settings(
        client,
        created["id"],
        {
            "welcome": {"steps": [activity("actividad1", cmid=7)]},
            "closing": {"steps": [activity("actividad2", cmid=7)]},
        },
    )
    assert r.status_code == 200, r.text


async def test_a_key_may_not_repeat_across_the_flows(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    r = await put_settings(
        client,
        created["id"],
        {
            "welcome": {"steps": [activity("actividad1")]},
            "closing": {"steps": [activity("actividad1", cmid=9)]},
        },
    )
    assert r.status_code == 422
    assert problems_of(r) == [("closing.steps[0].key", "step_key_duplicate")]


@pytest.mark.parametrize("flow", FLOWS)
async def test_a_bad_cmid_or_a_missing_title_is_reported_under_its_flow(
    client: httpx.AsyncClient, flow: str
) -> None:
    created = await make_map(client)
    r = await put_settings(client, created["id"], {flow: {"steps": [activity(cmid=0)]}})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "welcome_invalid"
    assert problems_of(r) == [(f"{flow}.steps[0].cmid", "out_of_range")]

    no_title = {k: v for k, v in activity().items() if k != "title"}
    r = await put_settings(client, created["id"], {flow: {"steps": [no_title]}})
    assert r.status_code == 422
    assert problems_of(r) == [(f"{flow}.steps[0].title", "required")]


async def test_a_url_in_the_step_is_refused(client: httpx.AsyncClient) -> None:
    created = await make_map(client)
    r = await put_settings(
        client,
        created["id"],
        {"closing": {"steps": [activity(url="https://evil.test/x")]}},
    )
    assert r.status_code == 422
    assert problems_of(r) == [("closing.steps[0].url", "unknown_field")]
