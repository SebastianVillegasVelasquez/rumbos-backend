"""`/skins` through the real stack, plus the delete side effects in Postgres."""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from uuid_utils.compat import uuid7

from app.enums import AssetKind
from app.main import app
from app.models import Bubble, CourseMap, CourseMapSkinRule, Skin
from app.repositories.sqlalchemy.asset_repository import SqlAlchemyAssetRepository
from tests import skin_configs

ORBE = uuid.UUID("01900000-0000-7000-8000-000000000001")
INSIGNIA = uuid.UUID("01900000-0000-7000-8000-000000000002")


@pytest.fixture
async def client(engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    app.state.db_engine = engine
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


@pytest.fixture
async def builtins(engine: AsyncEngine) -> None:
    """The test schema is built from the models, so the built-in rows (which
    only the migration seeds) are inserted here; the real seeds are covered in
    the migration tests."""
    async with AsyncSession(engine) as session:
        session.add_all(
            [
                Skin(
                    id=ORBE,
                    name="Orbe",
                    kind="procedural",
                    config=skin_configs.procedural(),
                    is_builtin=True,
                    is_default=True,
                ),
                Skin(
                    id=INSIGNIA,
                    name="Insignia",
                    kind="procedural",
                    config=skin_configs.procedural(shape="badge"),
                    is_builtin=True,
                ),
            ]
        )
        await session.commit()


async def make_asset(
    engine: AsyncEngine,
    kind: AssetKind = AssetKind.BUBBLE,
    width: int = 256,
    height: int = 256,
) -> uuid.UUID:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        record = await SqlAlchemyAssetRepository(session).create(
            uuid7(), kind, "image/png", width, height, 1000, uuid.uuid4().hex * 2
        )
    return record.id


async def create_skin(
    client: httpx.AsyncClient,
    name: str = "Mi skin",
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    r = await client.post(
        "/skins", json={"name": name, "config": config or skin_configs.procedural()}
    )
    assert r.status_code == 201, r.text
    body: dict[str, Any] = r.json()
    return body


# --- reading and creating ---------------------------------------------------


@pytest.mark.usefixtures("builtins")
async def test_list_skins_has_builtins_first_and_the_contract_shape(
    client: httpx.AsyncClient,
) -> None:
    mine = await create_skin(client)

    r = await client.get("/skins")

    assert r.status_code == 200
    items = r.json()["items"]
    assert set(r.json()) == {"items"}
    assert [s["name"] for s in items] == ["Orbe", "Insignia", "Mi skin"]
    assert set(items[0]) == {
        "id",
        "name",
        "builtin",
        "isDefault",
        "config",
        "createdAt",
        "updatedAt",
    }
    assert [(s["builtin"], s["isDefault"]) for s in items] == [
        (True, True),
        (True, False),
        (False, False),
    ]
    assert items[2]["id"] == mine["id"]


async def test_create_a_procedural_skin(client: httpx.AsyncClient) -> None:
    config = skin_configs.procedural(shape="hexagon", size=80)

    r = await client.post("/skins", json={"name": "  Hex  ", "config": config})

    assert r.status_code == 201
    body = r.json()
    assert uuid.UUID(body["id"]).version == 7
    assert body["name"] == "Hex"
    assert (body["builtin"], body["isDefault"]) == (False, False)
    assert body["config"] == config  # stored and returned exactly as sent


async def test_create_an_image_skin(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    available, locked = await make_asset(engine), await make_asset(engine)
    config = skin_configs.image(available, locked=locked)

    r = await client.post("/skins", json={"name": "Dibujado", "config": config})

    assert r.status_code == 201
    states = r.json()["config"]["states"]
    assert states["available"] == str(available) and states["locked"] == str(locked)
    assert states["next"] is None


@pytest.mark.parametrize(
    "body",
    [
        {"config": skin_configs.procedural()},
        {"name": "", "config": skin_configs.procedural()},
        {"name": "x" * 61, "config": skin_configs.procedural()},
        {"name": "ok"},
        {"name": "ok", "config": {**skin_configs.procedural(), "kind": "svg"}},
        {"name": "ok", "config": skin_configs.procedural(size=500)},
        {"name": "ok", "config": skin_configs.procedural(shape="star")},
    ],
)
async def test_invalid_skins_are_422(
    client: httpx.AsyncClient, body: dict[str, Any]
) -> None:
    assert (await client.post("/skins", json=body)).status_code == 422


# --- image skin asset rules -------------------------------------------------


async def test_a_missing_asset_is_422_and_names_the_states(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    real, ghost = await make_asset(engine), uuid.uuid4()

    r = await client.post(
        "/skins",
        json={"name": "x", "config": skin_configs.image(real, complete=ghost)},
    )

    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["code"] == "skin_asset_not_found"
    assert detail["states"] == {"complete": str(ghost)}


async def test_a_background_asset_is_the_wrong_kind(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    bubble = await make_asset(engine)
    background = await make_asset(engine, AssetKind.BACKGROUND)

    r = await client.post(
        "/skins",
        json={"name": "x", "config": skin_configs.image(bubble, hover=background)},
    )

    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["code"] == "skin_asset_wrong_kind"
    assert detail["states"] == {"hover": str(background)}


async def test_states_with_different_sizes_are_refused_and_the_sizes_listed(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    square = await make_asset(engine, width=256, height=256)
    wide = await make_asset(engine, width=300, height=256)
    same = await make_asset(engine, width=256, height=256)

    r = await client.post(
        "/skins",
        json={
            "name": "x",
            "config": skin_configs.image(square, locked=same, complete=wide),
        },
    )

    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["code"] == "skin_asset_size_mismatch"
    assert detail["sizes"] == [
        {"state": "available", "assetId": str(square), "width": 256, "height": 256},
        {"state": "locked", "assetId": str(same), "width": 256, "height": 256},
        {"state": "complete", "assetId": str(wide), "width": 300, "height": 256},
    ]
    assert (await client.get("/skins")).json()["items"] == []  # nothing was saved


async def test_identical_sizes_pass_even_with_every_state_set(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    ids = [await make_asset(engine, width=128, height=64) for _ in range(6)]
    config = skin_configs.image(
        ids[0],
        locked=ids[1],
        next=ids[2],
        inProgress=ids[3],
        complete=ids[4],
        hover=ids[5],
    )

    assert (
        await client.post("/skins", json={"name": "all", "config": config})
    ).status_code == 201


# --- updating ---------------------------------------------------------------


async def test_patch_changes_only_what_is_sent(client: httpx.AsyncClient) -> None:
    skin = await create_skin(client, "Antes")

    renamed = await client.patch(f"/skins/{skin['id']}", json={"name": " Después "})
    reconfigured = await client.patch(
        f"/skins/{skin['id']}", json={"config": skin_configs.procedural(shape="pin")}
    )

    assert renamed.status_code == 200
    assert renamed.json()["name"] == "Después"
    assert renamed.json()["config"] == skin["config"]
    assert reconfigured.json()["name"] == "Después"
    assert reconfigured.json()["config"]["shape"] == "pin"
    assert reconfigured.json()["updatedAt"] > skin["updatedAt"]


async def test_patch_can_switch_a_skin_to_images_and_validates_them(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    skin = await create_skin(client)
    a, b = await make_asset(engine, width=10, height=10), await make_asset(engine)

    bad = await client.patch(
        f"/skins/{skin['id']}", json={"config": skin_configs.image(a, locked=b)}
    )
    good = await client.patch(
        f"/skins/{skin['id']}", json={"config": skin_configs.image(a)}
    )

    assert bad.status_code == 422
    assert bad.json()["detail"]["code"] == "skin_asset_size_mismatch"
    assert good.status_code == 200 and good.json()["config"]["kind"] == "image"


@pytest.mark.parametrize("body", [{}, {"name": None}, {"config": None}, {"name": ""}])
async def test_invalid_patches_are_422(
    client: httpx.AsyncClient, body: dict[str, Any]
) -> None:
    skin = await create_skin(client)

    assert (await client.patch(f"/skins/{skin['id']}", json=body)).status_code == 422


async def test_unknown_skins_are_404(client: httpx.AsyncClient) -> None:
    missing = uuid.uuid4()

    patch = await client.patch(f"/skins/{missing}", json={"name": "x"})
    delete = await client.delete(f"/skins/{missing}")

    assert patch.status_code == 404 and delete.status_code == 404
    assert patch.json() == {"detail": "Skin not found"}


# --- built-in protection ----------------------------------------------------


@pytest.mark.usefixtures("builtins")
async def test_builtin_skins_cannot_be_edited_or_deleted(
    client: httpx.AsyncClient,
) -> None:
    patch = await client.patch(f"/skins/{ORBE}", json={"name": "Hacked"})
    delete = await client.delete(f"/skins/{ORBE}")

    for r in (patch, delete):
        assert r.status_code == 403
        assert r.json()["detail"]["code"] == "skin_is_builtin"
    names = [s["name"] for s in (await client.get("/skins")).json()["items"]]
    assert names == ["Orbe", "Insignia"]


# --- delete side effects ----------------------------------------------------


async def test_deleting_a_skin_clears_the_things_that_used_it(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    doomed, kept = (
        await create_skin(client, "Doomed"),
        await create_skin(client, "Kept"),
    )
    doomed_id, kept_id = uuid.UUID(doomed["id"]), uuid.UUID(kept["id"])
    async with AsyncSession(engine) as session:
        course_map = CourseMap(
            title="M",
            moodle_course_id=1,
            image_url="/x",
            default_skin_id=doomed_id,
        )
        other_map = CourseMap(
            title="N", moodle_course_id=2, image_url="/x", default_skin_id=kept_id
        )
        session.add_all([course_map, other_map])
        await session.flush()
        session.add_all(
            [
                Bubble(
                    course_map_id=course_map.id,
                    moodle_course_id=1,
                    activity_id=a,
                    x=0,
                    y=0,
                    skin_id=skin,
                )
                for a, skin in ((1, doomed_id), (2, kept_id), (3, None))
            ]
        )
        session.add_all(
            [
                CourseMapSkinRule(
                    course_map_id=course_map.id, modname="quiz", skin_id=doomed_id
                ),
                CourseMapSkinRule(
                    course_map_id=course_map.id, modname="scorm", skin_id=kept_id
                ),
            ]
        )
        await session.commit()

    r = await client.delete(f"/skins/{doomed['id']}")

    assert r.status_code == 204
    async with AsyncSession(engine) as session:
        bubbles = await session.execute(
            select(Bubble.activity_id, Bubble.skin_id).order_by(Bubble.activity_id)
        )
        assert [tuple(row) for row in bubbles] == [(1, None), (2, kept_id), (3, None)]
        defaults = await session.execute(
            select(CourseMap.moodle_course_id, CourseMap.default_skin_id).order_by(
                CourseMap.moodle_course_id
            )
        )
        assert [tuple(row) for row in defaults] == [(1, None), (2, kept_id)]
        rules = await session.execute(select(CourseMapSkinRule.modname))
        assert list(rules.scalars()) == ["scorm"]


# --- the single default -----------------------------------------------------


async def test_only_one_skin_can_be_the_default(engine: AsyncEngine) -> None:
    def skin(name: str, *, default: bool) -> Skin:
        return Skin(
            name=name,
            kind="procedural",
            config=skin_configs.procedural(),
            is_default=default,
        )

    async with AsyncSession(engine) as session:
        session.add_all([skin("a", default=True), skin("b", default=False)])
        session.add(skin("c", default=False))  # many non-defaults are fine
        await session.commit()

        session.add(skin("d", default=True))
        with pytest.raises(IntegrityError, match="uq_skins_single_default"):
            await session.commit()
        await session.rollback()

        defaults = await session.scalar(
            text("SELECT count(*) FROM skins WHERE is_default")
        )
        assert defaults == 1
