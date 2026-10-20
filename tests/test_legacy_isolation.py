"""With `LEGACY_ANONYMOUS_MODE` on, a request without a token acts as the legacy
tenant and as nothing else: it cannot read or list what a plugin tenant owns."""

from collections.abc import AsyncIterator, Callable

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import get_settings
from app.enums import Role
from app.main import app as fastapi_app
from tests.asset_helpers import png_asset
from tests.conftest import Box
from tests.editor_client import EditorClient
from tests.tenant_helpers import insert_tenant, insert_token
from tests.test_publications_api import add_bubble, publish

COURSE = 4
ACTIVITY = 100

MakeClient = Callable[[str | None], EditorClient]


@pytest.fixture
async def legacy_on() -> AsyncIterator[None]:
    settings = get_settings().model_copy(update={"legacy_anonymous_mode": True})
    fastapi_app.dependency_overrides[get_settings] = lambda: settings
    yield
    fastapi_app.dependency_overrides.pop(get_settings, None)


async def test_an_anonymous_request_sees_nothing_of_a_plugin_tenant(
    engine: AsyncEngine,
    make_client: MakeClient,
    box: Box,
    storage: object,
    legacy_on: None,
) -> None:
    tenant_id = await insert_tenant(engine, "mincit")
    owner = make_client(await insert_token(engine, tenant_id, COURSE, Role.EDITOR))
    created = await owner.post(
        "/course-maps",
        json={
            "title": "Mapa",
            "moodleCourseId": COURSE,
            "moodleSectionId": 3,
            "imageUrl": "http://i/x.png",
        },
    )
    assert created.status_code == 201, created.text
    map_id = created.json()["id"]
    await add_bubble(owner, map_id, ACTIVITY)
    await publish(owner, map_id)
    asset = await png_asset(owner, 1)

    anonymous = make_client(None)

    for path in ("/course-maps", "/published/course-maps", "/assets"):
        listed = await anonymous.get(path)
        assert listed.status_code == 200, path
        assert listed.json()["items"] == [], path
    for path in (
        f"/course-maps/{map_id}",
        f"/course-maps/{map_id}/publications",
        f"/course-maps/{map_id}/publication-state",
        f"/published/course-maps/{map_id}",
    ):
        assert (await anonymous.get(path)).status_code == 404, path
    # Writes aimed at the plugin tenant's map fail the same way.
    assert (await anonymous.delete(f"/course-maps/{map_id}")).status_code == 404
    assert (await anonymous.delete(f"/assets/{asset['id']}")).status_code == 404
    # The owner still has everything.
    assert (await owner.get(f"/course-maps/{map_id}")).status_code == 200
    assert (await owner.get("/assets")).json()["total"] == 1
