"""Content bubbles through the API: create, edit, the limits, the widget flag,
images, and who may do what."""

import uuid
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine

from app.enums import Role
from app.main import app as fastapi_app
from app.models import Tenant
from app.models.tenant import LEGACY_TENANT_ID
from tests.asset_helpers import png_asset, quarantine
from tests.conftest import Box
from tests.editor_client import EditorClient
from tests.tenant_helpers import insert_tenant, insert_token
from tests.test_course_map_api import _add_bubble, _create_map

MakeClient = Callable[[str | None], EditorClient]
MEGABYTE = 1024 * 1024


def blocks(*items: dict[str, Any], title: str = "Introduccion") -> dict[str, Any]:
    return {
        "type": "blocks",
        "title": title,
        "autoOpen": "never",
        "body": {"schemaVersion": 1, "blocks": list(items)},
    }


def widget(html: str = "<p>hola</p>") -> dict[str, Any]:
    return {
        "type": "widget",
        "title": "Simulador",
        "autoOpen": "first_visit",
        "altText": "Un simulador",
        "html": html,
    }


PARAGRAPH = {"type": "paragraph", "text": "Hola\nmundo"}


async def add_content(
    client: httpx.AsyncClient, map_id: str, content: dict[str, Any], **extra: Any
) -> httpx.Response:
    return await client.post(
        f"/course-maps/{map_id}/bubbles",
        json={"kind": "content", "content": content, "x": 0.4, "y": 0.6, **extra},
    )


async def set_widgets(engine: AsyncEngine, tenant_id: uuid.UUID, on: bool) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            update(Tenant).where(Tenant.id == tenant_id).values(widgets_enabled=on)
        )


@pytest.fixture
async def widgets_on(engine: AsyncEngine) -> None:
    await set_widgets(engine, LEGACY_TENANT_ID, True)


# --- creating ----------------------------------------------------------------------


async def test_create_a_blocks_bubble_returns_the_contract_shape(
    client: httpx.AsyncClient,
) -> None:
    created = await _create_map(client)
    content = blocks(PARAGRAPH, {"type": "divider"})

    r = await add_content(client, created["id"], content, icon="book")

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["kind"] == "content" and body["activityId"] is None
    assert body["content"] == content
    assert (body["x"], body["y"], body["icon"], body["version"]) == (
        0.4,
        0.6,
        "book",
        1,
    )
    assert r.headers["ETag"] == '"1"'
    fetched = (await client.get(f"/course-maps/{created['id']}")).json()
    assert fetched["bubbles"] == [body]


async def test_an_activity_bubble_reads_as_kind_activity_without_content(
    client: httpx.AsyncClient,
) -> None:
    created = await _create_map(client)

    bubble = await _add_bubble(client, created["id"])

    assert (bubble["kind"], bubble["content"], bubble["activityId"]) == (
        "activity",
        None,
        1,
    )


async def test_content_is_normalized_on_the_way_in(client: httpx.AsyncClient) -> None:
    created = await _create_map(client)
    messy = blocks(
        {"type": "paragraph", "text": "  Café\x00 \r\n fin  "},
        {"type": "image", "assetId": str(uuid.uuid4()), "alt": "x"},
        title="  Título  ",
    )
    messy["body"]["blocks"].pop()

    body = (await add_content(client, created["id"], messy)).json()

    assert body["content"]["title"] == "Título"
    assert body["content"]["body"]["blocks"][0]["text"] == "Café \n fin"


async def test_content_bubbles_join_the_path_in_order(
    client: httpx.AsyncClient,
) -> None:
    created = await _create_map(client)
    first = await _add_bubble(client, created["id"])
    second = (await add_content(client, created["id"], blocks(PARAGRAPH))).json()
    third = (await add_content(client, created["id"], blocks(PARAGRAPH))).json()

    assert [first["sequence"], second["sequence"], third["sequence"]] == [0, 1, 2]
    reorder = await client.put(
        f"/course-maps/{created['id']}/bubbles/order",
        json={"bubbleIds": [third["id"], first["id"], second["id"]]},
    )
    assert reorder.status_code == 200
    assert [b["id"] for b in reorder.json()["bubbles"]] == [
        third["id"],
        first["id"],
        second["id"],
    ]


async def test_content_bubbles_do_not_use_up_activities(
    client: httpx.AsyncClient,
) -> None:
    created = await _create_map(client)
    for _ in range(3):
        assert (await add_content(client, created["id"], blocks())).status_code == 201

    assert (
        await client.post(
            f"/course-maps/{created['id']}/bubbles",
            json={"activityId": 1, "x": 0.1, "y": 0.1},
        )
    ).status_code == 201
    again = await client.post(
        f"/course-maps/{created['id']}/bubbles",
        json={"activityId": 1, "x": 0.1, "y": 0.1},
    )
    assert again.status_code == 409
    assert again.json()["detail"]["code"] == "activity_already_placed"


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "content", "x": 0.1, "y": 0.1},  # no content
        {"kind": "content", "content": None, "x": 0.1, "y": 0.1},
        {
            "kind": "content",
            "activityId": 3,
            "content": {"type": "blocks"},
            "x": 0,
            "y": 0,
        },
        {"kind": "activity", "x": 0.1, "y": 0.1},  # no activity
        {
            "kind": "activity",
            "activityId": 3,
            "content": {"type": "blocks"},
            "x": 0,
            "y": 0,
        },
        {"kind": "video", "activityId": 3, "x": 0.1, "y": 0.1},
    ],
)
async def test_the_kind_decides_which_fields_are_needed(
    client: httpx.AsyncClient, payload: dict[str, Any]
) -> None:
    created = await _create_map(client)

    r = await client.post(f"/course-maps/{created['id']}/bubbles", json=payload)

    assert r.status_code == 422


# --- validation errors -------------------------------------------------------------------


async def test_invalid_content_is_a_422_listing_every_problem(
    client: httpx.AsyncClient,
) -> None:
    created = await _create_map(client)
    bad = blocks(
        {"type": "heading", "text": "", "level": 5},
        {"type": "link", "text": "x", "url": "javascript:alert(1)"},
        {"type": "video", "provider": "dailymotion", "videoId": "abcdef", "title": "t"},
        title="",
    )

    r = await add_content(client, created["id"], bad)

    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["code"] == "content_invalid"
    assert {(p["path"], p["code"]) for p in detail["problems"]} == {
        ("title", "too_short"),
        ("body.blocks[0].text", "too_short"),
        ("body.blocks[0].level", "invalid_value"),
        ("body.blocks[1].url", "invalid_url"),
        ("body.blocks[2].provider", "invalid_value"),
    }
    assert all(set(p) == {"path", "code", "message"} for p in detail["problems"])
    assert "javascript" not in r.text
    assert (await client.get(f"/course-maps/{created['id']}")).json()["bubbles"] == []


@pytest.mark.parametrize("content", [[], "blocks", 5, {}, {"type": "html"}])
async def test_content_of_the_wrong_shape_is_still_content_invalid(
    client: httpx.AsyncClient, content: Any
) -> None:
    created = await _create_map(client)

    r = await client.post(
        f"/course-maps/{created['id']}/bubbles",
        json={"kind": "content", "content": content, "x": 0, "y": 0},
    )

    assert r.status_code == 422
    if isinstance(content, dict):
        assert r.json()["detail"]["code"] == "content_invalid"


async def test_forty_one_blocks_are_refused(client: httpx.AsyncClient) -> None:
    created = await _create_map(client)

    ok = await add_content(client, created["id"], blocks(*[{"type": "divider"}] * 40))
    bad = await add_content(client, created["id"], blocks(*[{"type": "divider"}] * 41))

    assert ok.status_code == 201
    assert bad.status_code == 422
    assert bad.json()["detail"]["problems"] == [
        {
            "path": "body.blocks",
            "code": "too_many_blocks",
            "message": "At most 40 blocks are allowed",
        }
    ]


async def test_a_blocks_body_over_20_kb_is_413(client: httpx.AsyncClient) -> None:
    created = await _create_map(client)
    big = {"type": "paragraph", "text": "x" * 1500}

    ok = await add_content(client, created["id"], blocks(*[big] * 13))  # 19.5 KB
    bad = await add_content(client, created["id"], blocks(*[big] * 14))  # 21 KB

    assert ok.status_code == 201
    assert bad.status_code == 413
    assert bad.json()["detail"] == {
        "code": "content_too_large",
        "message": "The content is too large",
        "limitBytes": 20 * 1024,
        "scope": "bubble",
    }


# --- the budget of a map ------------------------------------------------------------------


async def test_a_map_holds_at_most_one_megabyte_of_content(
    client: httpx.AsyncClient, widgets_on: None
) -> None:
    created = await _create_map(client)
    html = "a" * (200 * 1024)

    made = [await add_content(client, created["id"], widget(html)) for _ in range(6)]

    assert [r.status_code for r in made] == [201] * 5 + [413]
    assert made[5].json()["detail"]["scope"] == "map"
    assert made[5].json()["detail"]["limitBytes"] == MEGABYTE
    # Another map has its own budget.
    other = await _create_map(client, 2)
    assert (await add_content(client, other["id"], widget(html))).status_code == 201


async def test_replacing_a_bubbles_content_does_not_count_it_twice(
    client: httpx.AsyncClient, widgets_on: None
) -> None:
    created = await _create_map(client)
    html = "a" * (200 * 1024)
    bubbles = [
        (await add_content(client, created["id"], widget(html))).json()
        for _ in range(5)
    ]
    url = f"/course-maps/{created['id']}/bubbles/{bubbles[0]['id']}"

    same_size = await client.patch(url, json={"content": widget("b" * len(html))})
    bigger = await client.patch(
        url, json={"content": widget("b" * (len(html) + 20 * 1024))}
    )

    assert same_size.status_code == 200
    assert bigger.status_code == 413


async def test_deleting_content_frees_the_budget(
    client: httpx.AsyncClient, widgets_on: None
) -> None:
    created = await _create_map(client)
    html = "a" * (200 * 1024)
    made = [
        (await add_content(client, created["id"], widget(html))).json()
        for _ in range(5)
    ]
    assert (await add_content(client, created["id"], widget(html))).status_code == 413

    gone = await client.delete(f"/course-maps/{created['id']}/bubbles/{made[0]['id']}")

    assert gone.status_code == 204
    assert (await add_content(client, created["id"], widget(html))).status_code == 201


# --- editing -------------------------------------------------------------------------------


async def test_patch_replaces_the_content_and_bumps_the_version(
    client: httpx.AsyncClient,
) -> None:
    created = await _create_map(client)
    bubble = (await add_content(client, created["id"], blocks(PARAGRAPH))).json()
    url = f"/course-maps/{created['id']}/bubbles/{bubble['id']}"
    new = blocks({"type": "divider"}, title="Nuevo")

    r = await client.patch(url, json={"content": new}, headers={"If-Match": '"1"'})

    assert r.status_code == 200, r.text
    assert r.json()["content"] == new and r.json()["version"] == 2
    assert r.headers["ETag"] == '"2"'
    assert (r.json()["x"], r.json()["y"]) == (0.4, 0.6)


async def test_a_content_edit_needs_if_match_and_a_current_one(
    engine: AsyncEngine, make_client: MakeClient
) -> None:
    tenant = await insert_tenant(engine, "alpha")
    token = await insert_token(engine, tenant, 7, Role.EDITOR)
    editor = make_client(token)
    created = await _create_map(editor, 7)
    bubble = (await add_content(editor, created["id"], blocks(PARAGRAPH))).json()
    url = f"/course-maps/{created['id']}/bubbles/{bubble['id']}"
    plain = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=fastapi_app),
        base_url="http://t/api/v1",
        headers={"Authorization": f"Bearer {token}"},
    )
    async with plain:
        missing = await plain.patch(url, json={"content": blocks()})
        stale = await plain.patch(
            url, json={"content": blocks()}, headers={"If-Match": '"9"'}
        )

    assert missing.status_code == 428
    assert stale.status_code == 412 and stale.json()["detail"]["currentVersion"] == 1


async def test_invalid_content_in_a_patch_changes_nothing(
    client: httpx.AsyncClient,
) -> None:
    created = await _create_map(client)
    bubble = (await add_content(client, created["id"], blocks(PARAGRAPH))).json()
    url = f"/course-maps/{created['id']}/bubbles/{bubble['id']}"

    r = await client.patch(
        url,
        json={"content": blocks({"type": "link", "text": "x", "url": "http://a.co"})},
    )

    assert r.status_code == 422 and r.json()["detail"]["code"] == "content_invalid"
    now = (await client.get(f"/course-maps/{created['id']}")).json()["bubbles"][0]
    assert now == bubble


async def test_content_cannot_be_null_or_sent_to_an_activity_bubble(
    client: httpx.AsyncClient,
) -> None:
    created = await _create_map(client)
    activity = await _add_bubble(client, created["id"])
    content = (await add_content(client, created["id"], blocks())).json()
    base = f"/course-maps/{created['id']}/bubbles"

    null = await client.patch(f"{base}/{content['id']}", json={"content": None})
    wrong = await client.patch(f"{base}/{activity['id']}", json={"content": blocks()})

    assert null.status_code == 422
    assert wrong.status_code == 422
    assert wrong.json()["detail"]["problems"][0]["code"] == "not_a_content_bubble"


async def test_the_kind_and_activity_of_a_bubble_cannot_be_patched(
    client: httpx.AsyncClient,
) -> None:
    created = await _create_map(client)
    content = (await add_content(client, created["id"], blocks())).json()
    url = f"/course-maps/{created['id']}/bubbles/{content['id']}"

    for field in ({"kind": "activity"}, {"activityId": 4}):
        r = await client.patch(url, json=field)
        assert r.status_code in (200, 422)
    now = (await client.get(f"/course-maps/{created['id']}")).json()["bubbles"][0]
    assert (now["kind"], now["activityId"]) == ("content", None)


async def test_a_content_bubble_can_be_moved_restyled_and_deleted(
    client: httpx.AsyncClient,
) -> None:
    created = await _create_map(client)
    bubble = (await add_content(client, created["id"], blocks(PARAGRAPH))).json()
    url = f"/course-maps/{created['id']}/bubbles/{bubble['id']}"

    moved = await client.patch(url, json={"x": 0.9, "y": 0.1, "icon": "star"})
    live = await client.patch(url, json={"status": "complete"})

    assert moved.status_code == 200 and moved.json()["content"] == bubble["content"]
    assert live.status_code == 200 and live.json()["version"] == 2
    assert (await client.delete(url)).status_code == 204


async def test_a_content_bubble_can_be_bound_to_a_3d_zone(
    client: httpx.AsyncClient,
) -> None:
    created = await _create_map(client)
    bubble = (await add_content(client, created["id"], blocks(PARAGRAPH))).json()
    scene = "region-encuentros"

    r = await client.put(
        f"/course-maps/{created['id']}/view3d",
        json={"scene": scene, "bindings": [{"zoneId": 0, "bubbleId": bubble["id"]}]},
    )

    assert r.status_code == 200, r.text
    assert r.json()["view3d"]["bindings"][0]["bubbleId"] == bubble["id"]


# --- the widget flag -------------------------------------------------------------------------


async def test_widgets_are_refused_while_the_flag_is_off(
    client: httpx.AsyncClient,
) -> None:
    created = await _create_map(client)

    r = await add_content(client, created["id"], widget())

    assert r.status_code == 403
    assert r.json()["detail"] == {
        "code": "widgets_disabled",
        "message": "Widgets are not enabled for this site",
    }
    assert (await client.get(f"/course-maps/{created['id']}")).json()["bubbles"] == []


async def test_a_widget_can_be_created_and_edited_once_the_flag_is_on(
    client: httpx.AsyncClient, widgets_on: None
) -> None:
    created = await _create_map(client)
    html = "<script>parent.postMessage(1,'*')</script>\n<style>*{}</style>"

    r = await add_content(client, created["id"], widget(html))

    assert r.status_code == 201, r.text
    assert r.json()["content"]["html"] == html  # untouched, byte for byte
    url = f"/course-maps/{created['id']}/bubbles/{r.json()['id']}"
    edited = await client.patch(url, json={"content": widget("<b>2</b>")})
    assert edited.status_code == 200 and edited.json()["content"]["html"] == "<b>2</b>"


async def test_an_invalid_widget_is_a_422_before_the_flag_matters(
    client: httpx.AsyncClient, widgets_on: None
) -> None:
    created = await _create_map(client)
    bad = widget()
    bad["altText"] = ""

    r = await add_content(client, created["id"], bad)

    assert r.status_code == 422
    assert r.json()["detail"]["problems"][0]["path"] == "altText"


async def test_turning_the_flag_off_keeps_widgets_but_blocks_widget_writes(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    created = await _create_map(client)
    await set_widgets(engine, LEGACY_TENANT_ID, True)
    bubble = (await add_content(client, created["id"], widget())).json()
    await set_widgets(engine, LEGACY_TENANT_ID, False)
    url = f"/course-maps/{created['id']}/bubbles/{bubble['id']}"

    still_there = (await client.get(f"/course-maps/{created['id']}")).json()
    move = await client.patch(url, json={"x": 0.5, "y": 0.5})
    edit = await client.patch(url, json={"content": widget("<b>x</b>")})
    to_blocks = await client.patch(url, json={"content": blocks(PARAGRAPH)})
    new = await add_content(client, created["id"], widget())

    assert still_there["bubbles"][0]["content"]["type"] == "widget"
    assert move.status_code == 200  # position is not content
    assert edit.status_code == 403 and new.status_code == 403
    assert to_blocks.status_code == 200  # the way out is always open
    assert to_blocks.json()["content"]["type"] == "blocks"


async def test_the_flag_of_one_tenant_does_not_open_another(
    engine: AsyncEngine, make_client: MakeClient
) -> None:
    on = await insert_tenant(engine, "on", widgets=True)
    off = await insert_tenant(engine, "off")
    editor_on = make_client(await insert_token(engine, on, 7, Role.EDITOR))
    editor_off = make_client(await insert_token(engine, off, 7, Role.EDITOR))

    map_on, map_off = await _create_map(editor_on, 7), await _create_map(editor_off, 7)

    assert (await add_content(editor_on, map_on["id"], widget())).status_code == 201
    assert (await add_content(editor_off, map_off["id"], widget())).status_code == 403


# --- images -------------------------------------------------------------------------------


def image_block(asset_id: str) -> dict[str, Any]:
    return {"type": "image", "assetId": asset_id, "alt": "Un dibujo"}


async def test_images_and_backgrounds_may_be_used_in_blocks(
    client: httpx.AsyncClient,
) -> None:
    created = await _create_map(client)
    image = await png_asset(client, 1, "image")
    background = await png_asset(client, 2, "background")

    r = await add_content(
        client,
        created["id"],
        blocks(image_block(image["id"]), image_block(background["id"])),
    )

    assert r.status_code == 201, r.text


async def test_other_kinds_of_asset_are_refused_with_their_position(
    client: httpx.AsyncClient,
) -> None:
    created = await _create_map(client)
    skin_image = await png_asset(client, 1, "bubble")
    image = await png_asset(client, 2, "image")

    r = await add_content(
        client,
        created["id"],
        blocks(
            image_block(image["id"]),
            image_block(skin_image["id"]),
            image_block(str(uuid.uuid4())),
        ),
    )

    assert r.status_code == 422
    assert [(p["path"], p["code"]) for p in r.json()["detail"]["problems"]] == [
        ("body.blocks[1].assetId", "asset_wrong_kind"),
        ("body.blocks[2].assetId", "asset_not_found"),
    ]


async def test_a_taken_down_image_cannot_be_used(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    created = await _create_map(client)
    image = await png_asset(client, 1, "image")
    await quarantine(engine, image["id"])

    r = await add_content(client, created["id"], blocks(image_block(image["id"])))

    assert r.status_code == 422
    assert r.json()["detail"]["problems"][0]["code"] == "asset_not_found"


async def test_an_image_of_another_tenant_does_not_exist(
    engine: AsyncEngine, make_client: MakeClient, box: Box, storage: object
) -> None:
    alpha = await insert_tenant(engine, "alpha")
    beta = await insert_tenant(engine, "beta")
    editor_a = make_client(await insert_token(engine, alpha, 7, Role.EDITOR))
    editor_b = make_client(await insert_token(engine, beta, 7, Role.EDITOR))
    foreign = await png_asset(editor_a, 1, "image")
    created = await _create_map(editor_b, 7)

    r = await add_content(editor_b, created["id"], blocks(image_block(foreign["id"])))

    assert r.status_code == 422
    assert r.json()["detail"]["problems"][0]["code"] == "asset_not_found"


async def test_an_audio_track_is_not_an_image(client: httpx.AsyncClient) -> None:
    from tests import audio_files
    from tests.asset_helpers import upload_audio

    created = await _create_map(client)
    audio = (await upload_audio(client, audio_files.mp3())).json()

    r = await add_content(client, created["id"], blocks(image_block(audio["id"])))

    assert r.status_code == 422
    assert r.json()["detail"]["problems"][0]["code"] == "asset_wrong_kind"


# --- who may do what ----------------------------------------------------------------------


async def test_students_cannot_write_content(
    engine: AsyncEngine, make_client: MakeClient
) -> None:
    tenant = await insert_tenant(engine, "alpha", widgets=True)
    editor = make_client(await insert_token(engine, tenant, 7, Role.EDITOR))
    student = make_client(await insert_token(engine, tenant, 7, Role.STUDENT))
    created = await _create_map(editor, 7)
    bubble = (await add_content(editor, created["id"], blocks(PARAGRAPH))).json()
    url = f"/course-maps/{created['id']}/bubbles/{bubble['id']}"

    create = await add_content(student, created["id"], blocks(PARAGRAPH))
    widget_create = await add_content(student, created["id"], widget())
    patch = await student.patch(
        url, json={"content": blocks()}, headers={"If-Match": '"1"'}
    )

    assert (create.status_code, widget_create.status_code, patch.status_code) == (
        403,
        403,
        403,
    )
    assert create.json()["detail"]["code"] == "forbidden"
    mine = (await editor.get(f"/course-maps/{created['id']}")).json()["bubbles"]
    assert mine == [bubble]


async def test_another_tenant_or_course_gets_404_for_content_writes(
    engine: AsyncEngine, make_client: MakeClient
) -> None:
    alpha = await insert_tenant(engine, "alpha", widgets=True)
    beta = await insert_tenant(engine, "beta", widgets=True)
    owner = make_client(await insert_token(engine, alpha, 7, Role.EDITOR))
    stranger = make_client(await insert_token(engine, beta, 7, Role.EDITOR))
    other_course = make_client(await insert_token(engine, alpha, 8, Role.EDITOR))
    created = await _create_map(owner, 7)
    bubble = (await add_content(owner, created["id"], blocks(PARAGRAPH))).json()
    url = f"/course-maps/{created['id']}/bubbles/{bubble['id']}"

    for other in (stranger, other_course):
        assert (await add_content(other, created["id"], blocks())).status_code == 404
        assert (await add_content(other, created["id"], widget())).status_code == 404
        patch = await other.patch(
            url, json={"content": blocks()}, headers={"If-Match": '"1"'}
        )
        assert patch.status_code == 404
        assert (await other.get(f"/course-maps/{created['id']}")).status_code == 404
    assert (await owner.get(f"/course-maps/{created['id']}")).json()["bubbles"] == [
        bubble
    ]


async def test_a_bubble_addressed_through_the_wrong_map_is_404(
    client: httpx.AsyncClient,
) -> None:
    first, second = await _create_map(client, 1), await _create_map(client, 2)
    bubble = (await add_content(client, first["id"], blocks())).json()

    r = await client.patch(
        f"/course-maps/{second['id']}/bubbles/{bubble['id']}",
        json={"content": blocks()},
    )

    assert r.status_code == 404
