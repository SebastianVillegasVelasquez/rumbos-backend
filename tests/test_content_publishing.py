"""Content bubbles in snapshots, the diff and what students read."""

import copy
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine

from app.enums import BubbleKind, BubbleStatus
from app.models.tenant import LEGACY_TENANT_ID
from app.publishing.snapshot import (
    DraftInputs,
    build_snapshot,
    collect_asset_ids,
    content_hash,
    diff_snapshots,
)
from app.schemas.bubble import BubbleRead
from app.schemas.content import parse_content
from app.schemas.map_settings import MapSettings
from tests.asset_helpers import png_asset
from tests.test_content_bubbles_api import (
    add_content,
    blocks,
    set_widgets,
    widget,
)
from tests.test_course_map_api import _create_map
from tests.test_publications_api import add_bubble, publish, state

IMAGE_ID = "0197a2b4-5c6d-7e8f-9a0b-1c2d3e4f5a6b"
PARAGRAPH = {"type": "paragraph", "text": "Hola"}


def activity_bubble(n: int = 1, sequence: int = 0) -> BubbleRead:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return BubbleRead(
        id=uuid.UUID(f"00000000-0000-7000-8000-00000000000{n}"),
        course_map_id=uuid.UUID(int=1),
        activity_id=40 + n,
        x=0.5,
        y=0.25,
        icon=None,
        status=BubbleStatus.LOCKED,
        skin_id=None,
        sequence=sequence,
        created_at=now,
        updated_at=now,
    )


def content_bubble(
    content: dict[str, Any], n: int = 2, sequence: int = 1
) -> BubbleRead:
    return activity_bubble(n, sequence).model_copy(
        update={
            "activity_id": None,
            "kind": BubbleKind.CONTENT,
            "content": parse_content(content),
        }
    )


def inputs(*bubbles: BubbleRead) -> DraftInputs:
    return DraftInputs(
        title="Mapa",
        image_url="/x.png",
        settings=MapSettings(),
        default_skin_id=None,
        skin_rules=[],
        bubbles=list(bubbles),
        skins={},
    )


# --- the snapshot builder ----------------------------------------------------------------


def test_an_activity_bubble_keeps_its_exact_old_shape() -> None:
    snapshot = build_snapshot(inputs(activity_bubble()))

    assert snapshot["bubbles"] == [
        {
            "id": "00000000-0000-7000-8000-000000000001",
            "activityId": 41,
            "x": 0.5,
            "y": 0.25,
            "icon": None,
            "skinId": None,
            "sequence": 0,
        }
    ]


def test_a_map_without_content_bubbles_hashes_as_it_always_did() -> None:
    """Spelled out by hand so that it cannot drift with the code."""
    legacy = {
        "schemaVersion": 1,
        "title": "Mapa",
        "imageUrl": "/x.png",
        "settings": MapSettings().to_stored(),
        "defaultSkinId": None,
        "skinRules": [],
        "skins": {},
        "bubbles": [
            {
                "id": "00000000-0000-7000-8000-000000000001",
                "activityId": 41,
                "x": 0.5,
                "y": 0.25,
                "icon": None,
                "skinId": None,
                "sequence": 0,
            }
        ],
    }

    assert content_hash(build_snapshot(inputs(activity_bubble()))) == content_hash(
        legacy
    )


def test_a_content_bubble_carries_kind_and_content_and_no_activity() -> None:
    content = blocks(PARAGRAPH)

    snapshot = build_snapshot(inputs(activity_bubble(), content_bubble(content)))

    activity, shown = snapshot["bubbles"]
    assert "kind" not in activity and "content" not in activity
    assert shown["kind"] == "content" and shown["content"] == content
    assert "activityId" not in shown
    assert shown["sequence"] == 1


def test_the_snapshot_is_the_same_whatever_the_key_order_of_the_content() -> None:
    first = blocks(PARAGRAPH, title="T")
    second = {k: first[k] for k in reversed(list(first))}

    assert content_hash(build_snapshot(inputs(content_bubble(first)))) == content_hash(
        build_snapshot(inputs(content_bubble(second)))
    )


def test_a_widgets_html_is_frozen_byte_for_byte() -> None:
    html = "<script>1</script>\r\n  é "

    snapshot = build_snapshot(inputs(content_bubble(widget(html))))

    assert snapshot["bubbles"][0]["content"]["html"] == html


def test_the_images_of_content_bubbles_are_assets_the_snapshot_depends_on() -> None:
    shown = blocks({"type": "image", "assetId": IMAGE_ID, "alt": "x"})

    snapshot = build_snapshot(inputs(activity_bubble(), content_bubble(shown)))

    assert collect_asset_ids(snapshot) == [uuid.UUID(IMAGE_ID)]
    assert collect_asset_ids(build_snapshot(inputs(activity_bubble()))) == []


# --- the diff ---------------------------------------------------------------------------


def changes(old: DraftInputs, new: DraftInputs) -> Any:
    return diff_snapshots(build_snapshot(old), build_snapshot(new))


def test_changing_the_content_is_reported_as_content_changed() -> None:
    before = inputs(content_bubble(blocks(PARAGRAPH)))
    after = inputs(content_bubble(blocks(PARAGRAPH, {"type": "divider"})))

    result = changes(before, after)

    assert result.content_changed is True
    assert (result.bubbles_added, result.bubbles_removed, result.bubbles_moved) == (
        0,
        0,
        0,
    )


def test_a_title_a_widget_html_and_an_auto_open_each_count_as_a_change() -> None:
    base = blocks(PARAGRAPH)
    for other in (
        blocks(PARAGRAPH, title="Otro"),
        {**base, "autoOpen": "first_visit"},
        widget(),
    ):
        assert changes(
            inputs(content_bubble(base)), inputs(content_bubble(other))
        ).content_changed
    assert changes(
        inputs(content_bubble(widget("<a>"))), inputs(content_bubble(widget("<b>")))
    ).content_changed


def test_moving_or_reordering_a_content_bubble_is_not_a_content_change() -> None:
    content = blocks(PARAGRAPH)
    moved = content_bubble(content).model_copy(update={"x": 0.9})

    result = changes(inputs(content_bubble(content)), inputs(moved))

    assert result.content_changed is False and result.bubbles_moved == 1


def test_adding_a_content_bubble_is_an_added_bubble() -> None:
    result = changes(
        inputs(activity_bubble()),
        inputs(activity_bubble(), content_bubble(blocks(PARAGRAPH))),
    )

    assert result.bubbles_added == 1 and result.content_changed is False


def test_nothing_changing_is_nothing_changed() -> None:
    same = inputs(activity_bubble(), content_bubble(blocks(PARAGRAPH)))

    result = changes(same, copy.deepcopy(same))

    assert result.content_changed is False


# --- publishing through the API ------------------------------------------------------------


async def test_publishing_freezes_content_for_students(
    client: httpx.AsyncClient,
) -> None:
    created = await _create_map(client)
    await add_bubble(client, created["id"], 5)
    content = blocks(PARAGRAPH, {"type": "divider"})
    shown = (await add_content(client, created["id"], content)).json()
    await publish(client, created["id"])

    published = (await client.get(f"/published/course-maps/{created['id']}")).json()

    activity, frozen = published["bubbles"]
    assert (activity["kind"], activity["content"], activity["activityId"]) == (
        "activity",
        None,
        5,
    )
    assert frozen["id"] == shown["id"] and frozen["kind"] == "content"
    assert frozen["activityId"] is None and frozen["content"] == content
    assert frozen["status"] == "locked"  # live data, like any bubble


async def test_students_keep_the_published_content_while_editors_work(
    client: httpx.AsyncClient,
) -> None:
    created = await _create_map(client)
    shown = (await add_content(client, created["id"], blocks(PARAGRAPH))).json()
    await publish(client, created["id"])

    await client.patch(
        f"/course-maps/{created['id']}/bubbles/{shown['id']}",
        json={"content": blocks({"type": "divider"}, title="Borrador")},
    )

    published = (await client.get(f"/published/course-maps/{created['id']}")).json()
    assert published["bubbles"][0]["content"] == blocks(PARAGRAPH)


async def test_published_widgets_are_served_whatever_the_flag_says_now(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    await set_widgets(engine, LEGACY_TENANT_ID, True)
    created = await _create_map(client)
    html = "<p>frozen</p>"
    await add_content(client, created["id"], widget(html))
    await publish(client, created["id"])

    await set_widgets(engine, LEGACY_TENANT_ID, False)

    published = (await client.get(f"/published/course-maps/{created['id']}")).json()
    assert published["bubbles"][0]["content"]["html"] == html


async def test_the_publication_state_reports_content_changed(
    client: httpx.AsyncClient,
) -> None:
    created = await _create_map(client)
    shown = (await add_content(client, created["id"], blocks(PARAGRAPH))).json()
    await publish(client, created["id"])
    url = f"/course-maps/{created['id']}/bubbles/{shown['id']}"

    clean = await state(client, created["id"])
    await client.patch(url, json={"x": 0.9, "y": 0.9})
    moved = await state(client, created["id"])
    await client.patch(url, json={"content": blocks({"type": "divider"})})
    edited = await state(client, created["id"])

    assert clean["status"] == "up_to_date"
    assert clean["changes"]["contentChanged"] is False
    assert moved["status"] == "unpublished_changes"
    assert moved["changes"]["contentChanged"] is False
    assert moved["changes"]["bubblesMoved"] == 1
    assert edited["changes"]["contentChanged"] is True
    assert set(edited["changes"]) >= {"contentChanged", "audioChanged", "view3dChanged"}


async def test_putting_the_same_content_back_is_up_to_date_again(
    client: httpx.AsyncClient,
) -> None:
    created = await _create_map(client)
    shown = (await add_content(client, created["id"], blocks(PARAGRAPH))).json()
    await publish(client, created["id"])
    url = f"/course-maps/{created['id']}/bubbles/{shown['id']}"

    await client.patch(url, json={"content": blocks({"type": "divider"})})
    await client.patch(url, json={"content": blocks(PARAGRAPH)})

    assert (await state(client, created["id"]))["status"] == "up_to_date"


async def test_a_published_image_cannot_be_deleted_even_after_the_draft_drops_it(
    client: httpx.AsyncClient,
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
    await client.patch(
        f"/course-maps/{created['id']}/bubbles/{shown['id']}",
        json={"content": blocks()},
    )

    blocked = await client.delete(f"/assets/{asset['id']}")

    assert blocked.status_code == 409
    assert blocked.json()["detail"]["usage"] == {
        "maps": 0,
        "skins": 0,
        "publications": 1,
    }


async def test_the_published_resolution_skips_content_bubbles() -> None:
    from app.schemas.published import PublishedRow
    from app.services.published_service import PublishedMapService

    class Publications:
        async def get_published(self, course_map_id: uuid.UUID) -> PublishedRow:
            snapshot = build_snapshot(
                inputs(activity_bubble(), content_bubble(blocks(PARAGRAPH)))
            )
            return PublishedRow(
                course_map_id=course_map_id,
                moodle_course_id=1,
                moodle_section_id=None,
                position=0,
                number=1,
                published_at=datetime(2026, 1, 1, tzinfo=UTC),
                snapshot=snapshot,
            )

    class Resolver:
        def __init__(self) -> None:
            self.asked: list[tuple[uuid.UUID, int]] = []

        async def resolve_activities(
            self,
            moodle_course_id: int,
            bubbles: list[tuple[uuid.UUID, int]],
            *,
            include_hidden: bool = False,
        ) -> str:
            self.asked = bubbles
            return "resolved"

    resolver = Resolver()
    service = PublishedMapService(
        Publications(),  # type: ignore[arg-type]
        resolver,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
    )

    await service.resolve(uuid.uuid4())

    assert [activity for _, activity in resolver.asked] == [41]


async def test_publishing_a_map_of_only_content_works(
    client: httpx.AsyncClient,
) -> None:
    created = await _create_map(client)
    await add_content(client, created["id"], blocks(PARAGRAPH))

    summary = await publish(client, created["id"])

    assert summary["bubbleCount"] == 1
    listed = (await client.get("/published/course-maps")).json()["items"]
    assert listed[0]["bubbleCount"] == 1
