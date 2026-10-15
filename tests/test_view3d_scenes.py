"""The scene registry and the endpoint that serves it. No database."""

from collections.abc import AsyncIterator

import httpx
import pytest

from app.main import app
from app.view3d import scenes
from tests.api_client import ApiClient


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with ApiClient(transport=transport, base_url="http://t/api/v1") as c:
        yield c


def test_the_registry_knows_the_region_of_encounters() -> None:
    assert scenes.zone_count("region-encuentros") == 9
    assert scenes.zone_count("nowhere") is None


async def test_scenes_are_listed_in_camel_case(client: httpx.AsyncClient) -> None:
    r = await client.get("/view3d/scenes")

    assert r.status_code == 200
    assert r.json() == {"items": [{"id": "region-encuentros", "zoneCount": 9}]}


async def test_a_new_scene_shows_up_without_other_changes(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scenes, "SCENES", {"region-encuentros": 9, "alpha": 4})

    r = await client.get("/view3d/scenes")

    assert r.json()["items"] == [
        {"id": "alpha", "zoneCount": 4},
        {"id": "region-encuentros", "zoneCount": 9},
    ]
