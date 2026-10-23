"""Discarding changes and restoring an older publication with content bubbles."""

import uuid
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.models.tenant import LEGACY_TENANT_ID
from tests.asset_helpers import png_asset
from tests.test_content_bubbles_api import (
    add_content,
    blocks,
    set_widgets,
    widget,
)
from tests.test_course_map_api import _create_map
from tests.test_publications_api import add_bubble, publish, state

PARAGRAPH = {"type": "paragraph", "text": "Hola"}


async def draft(client: httpx.AsyncClient, map_id: str) -> dict[str, Any]:
    body: dict[str, Any] = (await client.get(f"/course-maps/{map_id}")).json()
    return body


async def discard(client: httpx.AsyncClient, map_id: str) -> dict[str, Any]:
    r = await client.post(f"/course-maps/{map_id}/discard-changes")
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


async def test_discard_reverts_edited_content_and_bumps_the_version(
    client: httpx.AsyncClient,
) -> None:
    created = await _create_map(client)
    shown = (await add_content(client, created["id"], blocks(PARAGRAPH))).json()
    await publish(client, created["id"])
    url = f"/course-maps/{created['id']}/bubbles/{shown['id']}"
    await client.patch(url, json={"content": blocks({"type": "divider"})})

    result = await discard(client, created["id"])

    [bubble] = result["map"]["bubbles"]
    assert bubble["id"] == shown["id"] and bubble["content"] == blocks(PARAGRAPH)
    assert bubble["version"] == 3  # created, edited, restored
    assert result["warnings"] == []
    assert (await state(client, created["id"]))["status"] == "up_to_date"


async def test_a_deleted_content_bubble_comes_back_with_its_id_and_content(
    client: httpx.AsyncClient,
) -> None:
    created = await _create_map(client)
    await add_bubble(client, created["id"], 5)
    content = blocks(PARAGRAPH, {"type": "divider"}, title="Intro")
    shown = (await add_content(client, created["id"], content, icon="book")).json()
    await publish(client, created["id"])
    await client.delete(f"/course-maps/{created['id']}/bubbles/{shown['id']}")
    assert len((await draft(client, created["id"]))["bubbles"]) == 1

    result = await discard(client, created["id"])

    back = {b["id"]: b for b in result["map"]["bubbles"]}[shown["id"]]
    assert back["kind"] == "content" and back["activityId"] is None
    assert back["content"] == content and back["icon"] == "book"
    assert back["sequence"] == shown["sequence"]
    assert result["warnings"] == []
    assert (await state(client, created["id"]))["status"] == "up_to_date"


async def test_a_content_bubble_added_after_publishing_is_removed_by_discard(
    client: httpx.AsyncClient,
) -> None:
    created = await _create_map(client)
    await add_bubble(client, created["id"], 5)
    await publish(client, created["id"])
    await add_content(client, created["id"], blocks(PARAGRAPH))

    result = await discard(client, created["id"])

    assert [b["kind"] for b in result["map"]["bubbles"]] == ["activity"]


async def test_restoring_an_older_publication_brings_its_content_back(
    client: httpx.AsyncClient,
) -> None:
    created = await _create_map(client)
    shown = (await add_content(client, created["id"], blocks(PARAGRAPH))).json()
    first = await publish(client, created["id"])
    url = f"/course-maps/{created['id']}/bubbles/{shown['id']}"
    await client.patch(url, json={"content": blocks({"type": "divider"}, title="Dos")})
    await publish(client, created["id"])

    r = await client.post(
        f"/course-maps/{created['id']}/publications/{first['number']}/restore"
    )

    assert r.status_code == 200, r.text
    assert r.json()["map"]["bubbles"][0]["content"] == blocks(PARAGRAPH)
    published = (await client.get(f"/published/course-maps/{created['id']}")).json()
    assert published["bubbles"][0]["content"]["title"] == "Dos"  # not published again


async def test_a_widget_is_restored_with_a_warning_when_the_flag_is_off(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    await set_widgets(engine, LEGACY_TENANT_ID, True)
    created = await _create_map(client)
    html = "<script>1</script>\r\n<p>frozen</p>"
    shown = (await add_content(client, created["id"], widget(html))).json()
    await publish(client, created["id"])
    await client.delete(f"/course-maps/{created['id']}/bubbles/{shown['id']}")
    await set_widgets(engine, LEGACY_TENANT_ID, False)

    result = await discard(client, created["id"])

    [bubble] = result["map"]["bubbles"]
    assert bubble["id"] == shown["id"] and bubble["content"]["html"] == html
    assert [w["code"] for w in result["warnings"]] == ["widgets_disabled"]
    assert shown["id"] in result["warnings"][0]["message"]
    # What was published is intact, and the draft matches it again.
    published = (await client.get(f"/published/course-maps/{created['id']}")).json()
    assert published["bubbles"][0]["content"]["html"] == html
    assert (await state(client, created["id"]))["status"] == "up_to_date"
    # Editing it still needs the flag.
    edit = await client.patch(
        f"/course-maps/{created['id']}/bubbles/{bubble['id']}",
        json={"content": widget("<b>x</b>")},
    )
    assert edit.status_code == 403


async def test_an_existing_widget_bubble_also_warns_when_the_flag_is_off(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    await set_widgets(engine, LEGACY_TENANT_ID, True)
    created = await _create_map(client)
    await add_content(client, created["id"], widget())
    await publish(client, created["id"])
    await set_widgets(engine, LEGACY_TENANT_ID, False)

    result = await discard(client, created["id"])

    assert [w["code"] for w in result["warnings"]] == ["widgets_disabled"]


async def test_a_widget_restores_without_a_warning_when_the_flag_is_on(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    await set_widgets(engine, LEGACY_TENANT_ID, True)
    created = await _create_map(client)
    shown = (await add_content(client, created["id"], widget())).json()
    await publish(client, created["id"])
    await client.delete(f"/course-maps/{created['id']}/bubbles/{shown['id']}")

    result = await discard(client, created["id"])

    assert result["warnings"] == []
    assert result["map"]["bubbles"][0]["content"]["type"] == "widget"


async def test_a_binding_to_a_content_bubble_is_restored_too(
    client: httpx.AsyncClient,
) -> None:
    created = await _create_map(client)
    shown = (await add_content(client, created["id"], blocks(PARAGRAPH))).json()
    bound = await client.put(
        f"/course-maps/{created['id']}/view3d",
        json={
            "scene": "region-encuentros",
            "bindings": [{"zoneId": 0, "bubbleId": shown["id"]}],
        },
    )
    assert bound.status_code == 200, bound.text
    await publish(client, created["id"])
    await client.delete(f"/course-maps/{created['id']}/bubbles/{shown['id']}")

    result = await discard(client, created["id"])

    [binding] = result["map"]["view3d"]["bindings"]
    assert binding["bubbleId"] == shown["id"]


async def test_a_missing_image_is_a_warning_and_blocks_publishing_again(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    created = await _create_map(client)
    asset = await png_asset(client, 1, "image")
    shown = (
        await add_content(
            client,
            created["id"],
            blocks({"type": "image", "assetId": asset["id"], "alt": "x"}),
        )
    ).json()
    await publish(client, created["id"])
    await client.delete(f"/course-maps/{created['id']}/bubbles/{shown['id']}")
    async with engine.begin() as conn:  # an operator removed it behind our back
        await conn.execute(
            text("DELETE FROM assets WHERE id = :i"), {"i": uuid.UUID(asset["id"])}
        )

    result = await discard(client, created["id"])

    assert [w["code"] for w in result["warnings"]] == ["content_asset_missing"]
    assert asset["id"] in result["warnings"][0]["message"]
    await add_bubble(client, created["id"], 9)  # make the draft differ again
    current = await state(client, created["id"])
    refused = await client.post(
        f"/course-maps/{created['id']}/publish",
        json={"draftHash": current["draftHash"]},
    )
    assert refused.status_code == 422
    assert refused.json()["detail"]["problems"][0]["code"] == "content_asset_missing"


async def test_restoring_leaves_activity_only_maps_exactly_as_before(
    client: httpx.AsyncClient,
) -> None:
    created = await _create_map(client)
    first = await add_bubble(client, created["id"], 5)
    await publish(client, created["id"])
    await client.delete(f"/course-maps/{created['id']}/bubbles/{first['id']}")

    result = await discard(client, created["id"])

    [bubble] = result["map"]["bubbles"]
    assert (bubble["id"], bubble["kind"], bubble["activityId"]) == (
        first["id"],
        "activity",
        5,
    )
    assert bubble["content"] is None and result["warnings"] == []
