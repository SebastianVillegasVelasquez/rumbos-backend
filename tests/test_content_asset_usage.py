"""Images shown by content bubbles are in use: they cannot be deleted and the
garbage collector leaves them alone."""

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine

from app.assets.storage import AssetStorage
from app.models.tenant import LEGACY_TENANT_ID
from app.scripts import gc_assets
from tests.asset_helpers import png_asset
from tests.conftest import ENABLED
from tests.sessions import new_session
from tests.test_content_bubbles_api import add_content, blocks, set_widgets, widget
from tests.test_course_map_api import _create_map
from tests.test_publications_api import make_map


def image(asset_id: str) -> dict[str, Any]:
    return {"type": "image", "assetId": asset_id, "alt": "Un dibujo"}


async def usage_of(client: httpx.AsyncClient, asset: dict[str, Any]) -> dict[str, int]:
    listed = (await client.get("/assets", params={"kind": asset["kind"]})).json()
    [item] = [i for i in listed["items"] if i["id"] == asset["id"]]
    usage: dict[str, int] = item["usage"]
    return usage


async def test_a_content_image_counts_as_used_by_its_map(
    client: httpx.AsyncClient,
) -> None:
    created = await _create_map(client)
    asset = await png_asset(client, 1, "image")
    assert (await usage_of(client, asset))["maps"] == 0

    await add_content(client, created["id"], blocks(image(asset["id"])))

    assert await usage_of(client, asset) == {"maps": 1, "skins": 0, "publications": 0}


async def test_a_map_counts_once_however_many_bubbles_show_the_image(
    client: httpx.AsyncClient,
) -> None:
    first, second = await _create_map(client, 1), await _create_map(client, 2)
    asset = await png_asset(client, 1, "image")

    for map_id in (first["id"], first["id"], second["id"]):
        await add_content(
            client, map_id, blocks(image(asset["id"]), image(asset["id"]))
        )

    assert (await usage_of(client, asset))["maps"] == 2


async def test_an_image_in_use_cannot_be_deleted_until_it_is_replaced(
    client: httpx.AsyncClient,
) -> None:
    created = await _create_map(client)
    asset = await png_asset(client, 1, "image")
    bubble = (
        await add_content(client, created["id"], blocks(image(asset["id"])))
    ).json()

    blocked = await client.delete(f"/assets/{asset['id']}")
    await client.patch(
        f"/course-maps/{created['id']}/bubbles/{bubble['id']}",
        json={"content": blocks()},
    )
    freed = await client.delete(f"/assets/{asset['id']}")

    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "asset_in_use"
    assert blocked.json()["detail"]["usage"]["maps"] == 1
    assert freed.status_code == 204


async def test_deleting_the_bubble_or_the_map_frees_the_image(
    client: httpx.AsyncClient,
) -> None:
    created = await _create_map(client)
    asset = await png_asset(client, 1, "image")
    other = await png_asset(client, 2, "image")
    bubble = (
        await add_content(client, created["id"], blocks(image(asset["id"])))
    ).json()
    await add_content(client, created["id"], blocks(image(other["id"])))

    await client.delete(f"/course-maps/{created['id']}/bubbles/{bubble['id']}")
    assert (await client.delete(f"/assets/{asset['id']}")).status_code == 204
    assert (await client.delete(f"/assets/{other['id']}")).status_code == 409
    await client.delete(f"/course-maps/{created['id']}")
    assert (await client.delete(f"/assets/{other['id']}")).status_code == 204


async def test_a_widget_that_mentions_an_id_does_not_hold_the_image(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    await set_widgets(engine, LEGACY_TENANT_ID, True)
    created = await _create_map(client)
    asset = await png_asset(client, 1, "image")

    await add_content(
        client, created["id"], widget(f'<img src="/api/v1/assets/{asset["id"]}">')
    )

    assert (await usage_of(client, asset))["maps"] == 0
    assert (await client.delete(f"/assets/{asset['id']}")).status_code == 204


async def test_an_image_used_by_another_course_of_the_tenant_is_still_used(
    client: httpx.AsyncClient,
) -> None:
    other_course = await make_map(client, course=9)
    asset = await png_asset(client, 1, "image")
    await add_content(client, other_course["id"], blocks(image(asset["id"])))

    assert (await usage_of(client, asset))["maps"] == 1
    assert (await client.delete(f"/assets/{asset['id']}")).status_code == 409


async def test_the_garbage_collector_keeps_images_that_content_shows(
    client: httpx.AsyncClient, engine: AsyncEngine, storage: AssetStorage
) -> None:
    created = await _create_map(client)
    shown = await png_asset(client, 1, "image")
    spare = await png_asset(client, 2, "image")
    await add_content(client, created["id"], blocks(image(shown["id"])))
    lines: list[str] = []

    async with new_session(engine) as session:
        await gc_assets.run(
            session,
            storage,
            ENABLED,
            older_than=timedelta(days=7),
            apply=False,
            out=lines.append,
            now=datetime.now(UTC) + timedelta(days=30),
        )

    would = [line for line in lines if line.startswith("Would delete")]
    assert len(would) == 1 and spare["id"] in would[0]
