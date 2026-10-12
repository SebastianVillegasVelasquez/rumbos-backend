"""The two operations scripts: dry run vs apply, and idempotence."""

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.assets.storage import AssetFileMissingError, AssetStorage
from app.models import Asset, Publication
from app.scripts import gc_assets, publish_existing_maps
from tests.asset_helpers import png_asset
from tests.conftest import ENABLED
from tests.test_publications_api import add_bubble, make_map, publish, state

# --- publish_existing_maps ------------------------------------------------------------


async def publication_numbers(engine: AsyncEngine, map_id: str) -> list[int]:
    async with AsyncSession(engine) as s:
        return list(
            await s.scalars(
                select(Publication.number)
                .where(Publication.course_map_id == map_id)
                .order_by(Publication.number)
            )
        )


async def test_publish_existing_maps_dry_run_changes_nothing(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    old = await make_map(client)
    await add_bubble(client, old["id"], 1)
    lines: list[str] = []

    async with AsyncSession(engine) as session:
        count = await publish_existing_maps.run(
            session, apply=False, keep=20, out=lines.append
        )

    assert count == 1
    assert any(old["id"] in line and line.startswith("Would publish") for line in lines)
    assert "dry run" in lines[-1]
    assert await publication_numbers(engine, old["id"]) == []


async def test_publish_existing_maps_apply_publishes_only_unpublished_maps_with_bubbles(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    old = await make_map(client, course=1)
    await add_bubble(client, old["id"], 1)
    await add_bubble(client, old["id"], 2)
    empty = await make_map(client, course=2)  # no bubbles: stays unpublished
    done = await make_map(client, course=3)
    await add_bubble(client, done["id"], 1)
    await publish(client, done["id"], "Ya publicado")

    async with AsyncSession(engine) as session:
        count = await publish_existing_maps.run(
            session, apply=True, keep=20, out=lambda _: None
        )

    assert count == 1
    assert await publication_numbers(engine, old["id"]) == [1]
    assert await publication_numbers(engine, empty["id"]) == []
    assert await publication_numbers(engine, done["id"]) == [1]  # untouched
    history = (await client.get(f"/course-maps/{old['id']}/publications")).json()[
        "items"
    ]
    assert history[0]["note"] == "Publicación inicial (migración)"
    assert history[0]["bubbleCount"] == 2
    assert (await state(client, old["id"]))["status"] == "up_to_date"
    shown = (await client.get("/published/course-maps")).json()["items"]
    assert {i["id"] for i in shown} == {old["id"], done["id"]}


async def test_publish_existing_maps_is_idempotent(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    old = await make_map(client)
    await add_bubble(client, old["id"], 1)

    async with AsyncSession(engine) as session:
        first = await publish_existing_maps.run(
            session, apply=True, keep=20, out=lambda _: None
        )
    async with AsyncSession(engine) as session:
        lines: list[str] = []
        second = await publish_existing_maps.run(
            session, apply=True, keep=20, out=lines.append
        )

    assert (first, second) == (1, 0)
    assert await publication_numbers(engine, old["id"]) == [1]
    assert lines == ["0 map(s) published"]


async def test_publish_existing_maps_skips_a_map_whose_background_is_gone(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    ghost = await make_map(client, course=1)
    await add_bubble(client, ghost["id"], 1)
    fine = await make_map(client, course=2)
    await add_bubble(client, fine["id"], 1)
    async with engine.begin() as conn:
        # A background that points at a stored asset which was removed behind
        # the API's back.
        from sqlalchemy import text

        await conn.execute(
            text("UPDATE course_maps SET image_url = :u WHERE id = :i"),
            {
                "u": "/api/v1/assets/0192f3a0-0000-7000-8000-000000000000",
                "i": ghost["id"],
            },
        )
    lines: list[str] = []

    async with AsyncSession(engine) as session:
        count = await publish_existing_maps.run(
            session, apply=True, keep=20, out=lines.append
        )

    assert count == 1
    assert any("SKIPPED" in line for line in lines)
    assert await publication_numbers(engine, ghost["id"]) == []
    assert await publication_numbers(engine, fine["id"]) == [1]


# --- gc_assets ---------------------------------------------------------------------------


async def stored(storage: AssetStorage, asset: dict[str, Any]) -> bool:
    try:
        await storage.get(asset["id"])
    except AssetFileMissingError:
        return False
    return True


async def asset_count(engine: AsyncEngine) -> int:
    async with AsyncSession(engine) as s:
        return int(await s.scalar(select(func.count()).select_from(Asset)) or 0)


async def test_gc_assets_dry_run_lists_only_unreferenced_old_assets(
    client: httpx.AsyncClient, engine: AsyncEngine, storage: AssetStorage
) -> None:
    unused = await png_asset(client, 1, "background")
    used_by_map = await png_asset(client, 2, "background")
    used_by_publication = await png_asset(client, 3, "background")
    used_by_skin = await png_asset(client, 4, "bubble", size=(16, 16))
    await make_map(client, course=1, image_url=used_by_map["url"])
    published = await make_map(client, course=2, image_url=used_by_publication["url"])
    await publish(client, published["id"])
    await client.patch(
        f"/course-maps/{published['id']}", json={"imageUrl": "http://i/z"}
    )
    await client.post(
        "/skins",
        json={
            "name": "S",
            "config": {
                "schemaVersion": 1,
                "kind": "image",
                "size": 64,
                "anchor": "center",
                "states": {"available": used_by_skin["id"]},
                "label": {"mode": "hover"},
                "effects": {"idle": "none", "completion": "none"},
            },
        },
    )
    later = datetime.now(UTC) + timedelta(days=30)
    lines: list[str] = []

    async with AsyncSession(engine) as session:
        reclaimed = await gc_assets.run(
            session,
            storage,
            ENABLED,
            older_than=timedelta(days=7),
            apply=False,
            out=lines.append,
            now=later,
        )

    assert [line for line in lines if line.startswith("Would delete")] == [
        next(line for line in lines if unused["id"] in line)
    ]
    assert reclaimed == unused["bytes"]
    assert f"{unused['bytes']} bytes reclaimed" in lines[-1] and "dry run" in lines[-1]
    assert await asset_count(engine) == 4
    assert await stored(storage, unused)


async def test_gc_assets_respects_the_age_threshold(
    client: httpx.AsyncClient, engine: AsyncEngine, storage: AssetStorage
) -> None:
    await png_asset(client, 1, "background")

    async with AsyncSession(engine) as session:
        reclaimed = await gc_assets.run(
            session,
            storage,
            ENABLED,
            older_than=timedelta(days=7),
            apply=True,
            out=lambda _: None,
        )

    assert reclaimed == 0  # uploaded just now: too young
    assert await asset_count(engine) == 1


async def test_gc_assets_apply_deletes_rows_and_files_and_is_idempotent(
    client: httpx.AsyncClient, engine: AsyncEngine, storage: AssetStorage
) -> None:
    gone = await png_asset(client, 1, "background")
    kept = await png_asset(client, 2, "background")
    await make_map(client, image_url=kept["url"])
    later = datetime.now(UTC) + timedelta(days=30)

    async def collect() -> int:
        async with AsyncSession(engine) as session:
            return await gc_assets.run(
                session,
                storage,
                ENABLED,
                older_than=timedelta(days=7),
                apply=True,
                out=lambda _: None,
                now=later,
            )

    assert await collect() == gone["bytes"]
    assert await collect() == 0
    assert await asset_count(engine) == 1
    assert not await stored(storage, gone)
    assert (await client.get(gone["url"])).status_code == 404
    assert await stored(storage, kept)
    assert (await client.get(kept["url"])).status_code == 200


async def test_gc_assets_zero_days_considers_everything_old(
    client: httpx.AsyncClient, engine: AsyncEngine, storage: AssetStorage
) -> None:
    made = await png_asset(client, 1, "bubble")

    async with AsyncSession(engine) as session:
        reclaimed = await gc_assets.run(
            session,
            storage,
            ENABLED,
            older_than=timedelta(0),
            apply=False,
            out=lambda _: None,
            now=datetime.now(UTC) + timedelta(seconds=5),
        )

    assert reclaimed == made["bytes"]


# --- command lines ---------------------------------------------------------------------------


def test_the_command_lines_are_dry_run_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[Any, ...]] = []

    async def fake_publish(apply: bool) -> int:
        seen.append(("publish", apply))
        return 0

    async def fake_gc(days: int, apply: bool) -> int:
        seen.append(("gc", days, apply))
        return 0

    monkeypatch.setattr(publish_existing_maps, "_main", fake_publish)
    monkeypatch.setattr(gc_assets, "_main", fake_gc)

    publish_existing_maps.main([])
    publish_existing_maps.main(["--apply"])
    gc_assets.main([])
    gc_assets.main(["--older-than-days", "30", "--apply"])

    assert seen == [
        ("publish", False),
        ("publish", True),
        ("gc", 7, False),
        ("gc", 30, True),
    ]


def test_gc_rejects_a_negative_age() -> None:
    with pytest.raises(SystemExit) as caught:
        gc_assets.main(["--older-than-days", "-1"])

    assert caught.value.code == 2
