"""Map appearance (settings, default skin, skin rules) and bubble ordering."""

import copy
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.main import app
from app.repositories.sqlalchemy.skin_repository import SqlAlchemySkinRepository
from tests import skin_configs
from tests.fakes import InMemoryMoodleClient

DEFAULT_SETTINGS: dict[str, Any] = {
    "schemaVersion": 1,
    "mode": "explorative",
    "fit": "fit-width",
    "initialView": None,
    "path": {"visible": True, "style": "dashed", "color": None, "animated": True},
    "ambient": {"kind": "none", "intensity": 0.5},
    "intro": "none",
}

CUSTOM_SETTINGS: dict[str, Any] = {
    "schemaVersion": 1,
    "mode": "guided",
    "fit": "contain",
    "initialView": {"x": 0.25, "y": 0.75, "zoom": 1.5},
    "path": {"visible": True, "style": "dotted", "color": "#336699", "animated": False},
    "ambient": {"kind": "fireflies", "intensity": 0.8},
    "intro": "flyin",
}


@pytest.fixture
async def client(engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    app.state.db_engine = engine
    app.state.moodle_client = InMemoryMoodleClient()  # never called here
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


async def make_map(client: httpx.AsyncClient, course: int = 1) -> dict[str, Any]:
    r = await client.post(
        "/course-maps",
        json={"title": "M", "moodleCourseId": course, "imageUrl": "/m.png"},
    )
    assert r.status_code == 201, r.text
    body: dict[str, Any] = r.json()
    return body


async def make_skin(client: httpx.AsyncClient, name: str = "S") -> str:
    r = await client.post(
        "/skins", json={"name": name, "config": skin_configs.procedural()}
    )
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def add_bubbles(
    client: httpx.AsyncClient, map_id: str, count: int
) -> list[dict[str, Any]]:
    bubbles = []
    for activity in range(1, count + 1):
        r = await client.post(
            f"/course-maps/{map_id}/bubbles",
            json={"activityId": activity, "x": 0.1, "y": 0.1},
        )
        assert r.status_code == 201, r.text
        bubbles.append(r.json())
    return bubbles


def appearance(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "settings": CUSTOM_SETTINGS,
        "defaultSkinId": None,
        "skinRules": [],
    }
    body.update(overrides)
    return body


# --- reading: defaults ------------------------------------------------------


async def test_a_new_map_carries_the_default_appearance(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)

    assert created["settings"] == DEFAULT_SETTINGS
    assert created["defaultSkinId"] is None
    assert created["skinRules"] == []
    fetched = (await client.get(f"/course-maps/{created['id']}")).json()
    assert fetched["settings"] == DEFAULT_SETTINGS


@pytest.mark.parametrize(
    ("stored", "expected_overrides"),
    [
        ("{}", {}),
        ('{"mode": "guided"}', {"mode": "guided"}),
        (
            '{"path": {"style": "solid"}, "ambient": {"kind": "snow"}}',
            {
                "path": {**DEFAULT_SETTINGS["path"], "style": "solid"},
                "ambient": {"kind": "snow", "intensity": 0.5},
            },
        ),
    ],
)
async def test_missing_keys_in_the_stored_column_read_as_defaults(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    stored: str,
    expected_overrides: dict[str, Any],
) -> None:
    created = await make_map(client)
    async with AsyncSession(engine) as session:
        await session.execute(
            text("UPDATE course_maps SET settings = CAST(:s AS jsonb)"), {"s": stored}
        )
        await session.commit()

    fetched = (await client.get(f"/course-maps/{created['id']}")).json()

    assert fetched["settings"] == {**DEFAULT_SETTINGS, **expected_overrides}


# --- PUT /appearance --------------------------------------------------------


async def test_put_appearance_replaces_everything_and_returns_the_map(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    orbe, pin = await make_skin(client, "A"), await make_skin(client, "B")
    await add_bubbles(client, created["id"], 2)

    r = await client.put(
        f"/course-maps/{created['id']}/appearance",
        json=appearance(
            defaultSkinId=orbe,
            skinRules=[
                {"modname": "scorm", "skinId": pin},
                {"modname": "quiz", "skinId": orbe},
            ],
        ),
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["settings"] == CUSTOM_SETTINGS
    assert body["defaultSkinId"] == orbe
    # Ordered by modname, so the response is stable.
    assert body["skinRules"] == [
        {"modname": "quiz", "skinId": orbe},
        {"modname": "scorm", "skinId": pin},
    ]
    assert [b["activityId"] for b in body["bubbles"]] == [1, 2]
    assert body["id"] == created["id"] and body["title"] == "M"
    fetched = (await client.get(f"/course-maps/{created['id']}")).json()
    assert {k: fetched[k] for k in ("settings", "defaultSkinId", "skinRules")} == {
        k: body[k] for k in ("settings", "defaultSkinId", "skinRules")
    }


async def test_a_second_put_replaces_not_merges(client: httpx.AsyncClient) -> None:
    created = await make_map(client)
    skin = await make_skin(client)
    url = f"/course-maps/{created['id']}/appearance"
    await client.put(
        url,
        json=appearance(
            defaultSkinId=skin,
            skinRules=[
                {"modname": "quiz", "skinId": skin},
                {"modname": "url", "skinId": skin},
            ],
        ),
    )

    r = await client.put(
        url,
        json=appearance(
            settings={"mode": "guided"},
            skinRules=[{"modname": "scorm", "skinId": skin}],
        ),
    )

    body = r.json()
    assert body["defaultSkinId"] is None  # not sent: cleared
    assert body["skinRules"] == [{"modname": "scorm", "skinId": skin}]
    assert body["settings"] == {**DEFAULT_SETTINGS, "mode": "guided"}


async def test_putting_only_settings_clears_the_skins(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    skin = await make_skin(client)
    url = f"/course-maps/{created['id']}/appearance"
    await client.put(url, json=appearance(defaultSkinId=skin))

    r = await client.put(url, json={"settings": CUSTOM_SETTINGS})

    assert r.status_code == 200
    assert r.json()["defaultSkinId"] is None and r.json()["skinRules"] == []


async def test_the_maps_other_fields_are_untouched(client: httpx.AsyncClient) -> None:
    created = await make_map(client)

    r = await client.put(f"/course-maps/{created['id']}/appearance", json=appearance())

    body = r.json()
    for field in (
        "id",
        "title",
        "moodleCourseId",
        "moodleSectionId",
        "position",
        "imageUrl",
        "createdAt",
    ):
        assert body[field] == created[field]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: s.update(mode="free"),
        lambda s: s.update(fit="stretch"),
        lambda s: s.update(intro="zoom"),
        lambda s: s.update(schemaVersion=2),
        lambda s: s.update(unknownKey=True),
        lambda s: s["path"].update(style="wavy"),
        lambda s: s["path"].update(color="red"),
        lambda s: s["path"].update(color="#12345"),
        lambda s: s["path"].update(extra=1),
        lambda s: s["ambient"].update(kind="rain"),
        lambda s: s["ambient"].update(intensity=1.5),
        lambda s: s["ambient"].update(intensity=-0.1),
        lambda s: s["initialView"].update(zoom=0.2),
        lambda s: s["initialView"].update(zoom=4.5),
        lambda s: s["initialView"].update(x=1.1),
        lambda s: s["initialView"].update(y=-0.1),
    ],
)
async def test_invalid_settings_are_422_and_change_nothing(
    client: httpx.AsyncClient, mutate: Any
) -> None:
    created = await make_map(client)
    settings = copy.deepcopy(CUSTOM_SETTINGS)
    mutate(settings)

    r = await client.put(
        f"/course-maps/{created['id']}/appearance", json=appearance(settings=settings)
    )

    assert r.status_code == 422
    after = (await client.get(f"/course-maps/{created['id']}")).json()
    assert after["settings"] == DEFAULT_SETTINGS


async def test_boundary_settings_are_accepted(client: httpx.AsyncClient) -> None:
    created = await make_map(client)
    settings = copy.deepcopy(CUSTOM_SETTINGS)
    settings["initialView"] = {"x": 0, "y": 1, "zoom": 0.25}
    settings["ambient"]["intensity"] = 1

    r = await client.put(
        f"/course-maps/{created['id']}/appearance", json=appearance(settings=settings)
    )

    assert r.status_code == 200


async def test_an_unknown_map_is_404(client: httpx.AsyncClient) -> None:
    r = await client.put(f"/course-maps/{uuid.uuid4()}/appearance", json=appearance())

    assert r.status_code == 404


async def test_the_body_must_carry_settings(client: httpx.AsyncClient) -> None:
    created = await make_map(client)

    r = await client.put(f"/course-maps/{created['id']}/appearance", json={})

    assert r.status_code == 422


# --- rejected skins and rules: all or nothing -------------------------------


async def seed_appearance(
    client: httpx.AsyncClient, map_id: str, skin: str
) -> dict[str, Any]:
    r = await client.put(
        f"/course-maps/{map_id}/appearance",
        json=appearance(
            defaultSkinId=skin, skinRules=[{"modname": "quiz", "skinId": skin}]
        ),
    )
    assert r.status_code == 200
    body: dict[str, Any] = r.json()
    return body


async def test_an_unknown_default_skin_is_422(client: httpx.AsyncClient) -> None:
    created = await make_map(client)
    ghost = str(uuid.uuid4())

    r = await client.put(
        f"/course-maps/{created['id']}/appearance", json=appearance(defaultSkinId=ghost)
    )

    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "skin_not_found"
    assert r.json()["detail"]["skinIds"] == [ghost]


async def test_one_bad_rule_changes_nothing(client: httpx.AsyncClient) -> None:
    created = await make_map(client)
    skin = await make_skin(client)
    before = await seed_appearance(client, created["id"], skin)
    ghost = str(uuid.uuid4())

    r = await client.put(
        f"/course-maps/{created['id']}/appearance",
        json=appearance(
            settings={"mode": "guided", "intro": "flyin"},  # valid, but must not stick
            defaultSkinId=None,
            skinRules=[
                {"modname": "scorm", "skinId": skin},
                {"modname": "url", "skinId": ghost},
            ],
        ),
    )

    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "skin_not_found"
    assert r.json()["detail"]["skinIds"] == [ghost]
    after = (await client.get(f"/course-maps/{created['id']}")).json()
    for field in ("settings", "defaultSkinId", "skinRules"):
        assert after[field] == before[field]


async def test_duplicate_rules_are_422_and_change_nothing(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    skin, other = await make_skin(client), await make_skin(client, "O")
    before = await seed_appearance(client, created["id"], skin)

    r = await client.put(
        f"/course-maps/{created['id']}/appearance",
        json=appearance(
            skinRules=[
                {"modname": "quiz", "skinId": skin},
                {"modname": "scorm", "skinId": skin},
                {"modname": "quiz", "skinId": other},
            ]
        ),
    )

    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "duplicate_skin_rule"
    assert r.json()["detail"]["modnames"] == ["quiz"]
    after = (await client.get(f"/course-maps/{created['id']}")).json()
    for field in ("settings", "defaultSkinId", "skinRules"):
        assert after[field] == before[field]


@pytest.mark.parametrize("modname", ["", "Quiz", "my quiz", "x" * 51, "qu-iz"])
async def test_rule_modnames_are_validated(
    client: httpx.AsyncClient, modname: str
) -> None:
    created = await make_map(client)
    skin = await make_skin(client)

    r = await client.put(
        f"/course-maps/{created['id']}/appearance",
        json=appearance(skinRules=[{"modname": modname, "skinId": skin}]),
    )

    assert r.status_code == 422


async def test_a_skin_deleted_after_the_check_rolls_everything_back(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The settings UPDATE and the rule DELETE have already run when the rule
    INSERT hits the foreign key: the transaction must undo all of it."""
    created = await make_map(client)
    skin = await make_skin(client)
    before = await seed_appearance(client, created["id"], skin)
    ghost = uuid.uuid4()

    async def everything_exists(
        self: SqlAlchemySkinRepository, skin_ids: Any
    ) -> set[uuid.UUID]:
        return set(skin_ids)  # the race: the check passes, the skin is gone

    monkeypatch.setattr(SqlAlchemySkinRepository, "existing_ids", everything_exists)

    r = await client.put(
        f"/course-maps/{created['id']}/appearance",
        json=appearance(
            skinRules=[
                {"modname": "scorm", "skinId": skin},
                {"modname": "url", "skinId": str(ghost)},
            ]
        ),
    )

    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "skin_not_found"
    after = (await client.get(f"/course-maps/{created['id']}")).json()
    for field in ("settings", "defaultSkinId", "skinRules"):
        assert after[field] == before[field]


async def test_deleting_a_skin_clears_it_from_the_appearance(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    skin, kept = await make_skin(client), await make_skin(client, "Kept")
    await client.put(
        f"/course-maps/{created['id']}/appearance",
        json=appearance(
            defaultSkinId=skin,
            skinRules=[
                {"modname": "quiz", "skinId": skin},
                {"modname": "url", "skinId": kept},
            ],
        ),
    )

    assert (await client.delete(f"/skins/{skin}")).status_code == 204

    after = (await client.get(f"/course-maps/{created['id']}")).json()
    assert after["defaultSkinId"] is None
    assert after["skinRules"] == [{"modname": "url", "skinId": kept}]
    assert after["settings"] == CUSTOM_SETTINGS


async def test_deleting_a_map_removes_its_rules(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    created = await make_map(client)
    skin = await make_skin(client)
    await seed_appearance(client, created["id"], skin)

    assert (await client.delete(f"/course-maps/{created['id']}")).status_code == 204

    async with AsyncSession(engine) as session:
        count = await session.scalar(text("SELECT count(*) FROM course_map_skin_rules"))
    assert count == 0


# --- bubble order -----------------------------------------------------------


async def test_reorder_bubbles_sets_sequence_and_the_order_of_the_map(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    a, b, c = await add_bubbles(client, created["id"], 3)

    r = await client.put(
        f"/course-maps/{created['id']}/bubbles/order",
        json={"bubbleIds": [c["id"], a["id"], b["id"]]},
    )

    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"bubbles"}
    assert [x["id"] for x in body["bubbles"]] == [c["id"], a["id"], b["id"]]
    assert [x["sequence"] for x in body["bubbles"]] == [0, 1, 2]
    fetched = (await client.get(f"/course-maps/{created['id']}")).json()
    assert [x["id"] for x in fetched["bubbles"]] == [c["id"], a["id"], b["id"]]


async def test_new_bubbles_go_after_a_reordered_path(client: httpx.AsyncClient) -> None:
    created = await make_map(client)
    a, b = await add_bubbles(client, created["id"], 2)
    await client.put(
        f"/course-maps/{created['id']}/bubbles/order",
        json={"bubbleIds": [b["id"], a["id"]]},
    )

    new = await client.post(
        f"/course-maps/{created['id']}/bubbles",
        json={"activityId": 99, "x": 0.5, "y": 0.5},
    )

    assert new.json()["sequence"] == 2
    ids = [
        x["id"]
        for x in (await client.get(f"/course-maps/{created['id']}")).json()["bubbles"]
    ]
    assert ids == [b["id"], a["id"], new.json()["id"]]


async def test_removing_a_bubble_leaves_a_gap_that_a_reorder_closes(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    a, b, c = await add_bubbles(client, created["id"], 3)
    await client.delete(f"/course-maps/{created['id']}/bubbles/{b['id']}")

    r = await client.put(
        f"/course-maps/{created['id']}/bubbles/order",
        json={"bubbleIds": [c["id"], a["id"]]},
    )

    assert [x["sequence"] for x in r.json()["bubbles"]] == [0, 1]


@pytest.mark.parametrize("case", ["missing", "unknown", "duplicate", "foreign"])
async def test_reorder_needs_exactly_the_maps_bubbles(
    client: httpx.AsyncClient, case: str
) -> None:
    created, other = await make_map(client, 1), await make_map(client, 2)
    a, b = await add_bubbles(client, created["id"], 2)
    (stranger,) = await add_bubbles(client, other["id"], 1)
    ids = {
        "missing": [a["id"]],
        "unknown": [a["id"], b["id"], str(uuid.uuid4())],
        "duplicate": [a["id"], a["id"], b["id"]],
        "foreign": [a["id"], b["id"], stranger["id"]],
    }[case]

    r = await client.put(
        f"/course-maps/{created['id']}/bubbles/order", json={"bubbleIds": ids}
    )

    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "order_mismatch"
    after = (await client.get(f"/course-maps/{created['id']}")).json()["bubbles"]
    assert [(x["id"], x["sequence"]) for x in after] == [(a["id"], 0), (b["id"], 1)]
    untouched = (await client.get(f"/course-maps/{other['id']}")).json()["bubbles"]
    assert [x["sequence"] for x in untouched] == [0]


async def test_reorder_of_an_empty_map_and_of_an_unknown_map(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)

    empty = await client.put(
        f"/course-maps/{created['id']}/bubbles/order", json={"bubbleIds": []}
    )
    unknown = await client.put(
        f"/course-maps/{uuid.uuid4()}/bubbles/order", json={"bubbleIds": []}
    )

    assert empty.status_code == 200 and empty.json() == {"bubbles": []}
    assert unknown.status_code == 404
