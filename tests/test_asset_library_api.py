"""The asset library: metadata, listing, usage, deletion, background validation."""

from typing import Any

import httpx
import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine

from app.api.routes.assets import get_asset_storage
from app.assets.storage import AssetFileMissingError, AssetStorage
from app.main import app
from tests import images, skin_configs
from tests.asset_helpers import png_asset, upload

# --- metadata ---------------------------------------------------------------


async def test_title_defaults_to_the_file_name_without_extension(
    client: httpx.AsyncClient,
) -> None:
    body = (
        await upload(client, images.png((8, 8)), filename="Playa de Cartagena.v2.png")
    ).json()

    assert body["title"] == "Playa de Cartagena.v2"
    assert body["credit"] is None
    assert body["originalFilename"] == "Playa de Cartagena.v2.png"


async def test_title_and_credit_can_be_sent_with_the_upload(
    client: httpx.AsyncClient,
) -> None:
    r = await client.post(
        "/assets",
        data={"kind": "bubble", "title": "  Cofre  dorado ", "credit": " Foto: Ana "},
        files={"file": ("x.png", images.png((8, 8)), "image/png")},
    )

    assert r.status_code == 201
    assert r.json()["title"] == "Cofre dorado"
    assert r.json()["credit"] == "Foto: Ana"


@pytest.mark.parametrize(
    ("raw", "expected_name", "expected_title"),
    [
        ("../../etc/passwd.png", "passwd.png", "passwd"),
        ("C:\\Users\\x\\foto.png", "foto.png", "foto"),
    ],
)
async def test_the_original_filename_is_sanitized_display_text(
    client: httpx.AsyncClient, raw: str, expected_name: str, expected_title: str
) -> None:
    body = (await upload(client, images.png((8, 8)), filename=raw)).json()

    assert body["originalFilename"] == expected_name
    assert body["title"] == expected_title


async def test_a_title_or_credit_that_is_too_long_is_422(
    client: httpx.AsyncClient,
) -> None:
    for field, value in (("title", "t" * 121), ("credit", "c" * 301)):
        r = await client.post(
            "/assets",
            data={"kind": "bubble", field: value},
            files={"file": ("x.png", images.png((8, 8)), "image/png")},
        )
        assert r.status_code == 422, field
        assert r.json()["detail"]["code"] == "invalid_asset_metadata"


async def test_a_deduplicated_upload_keeps_the_existing_title_and_credit(
    client: httpx.AsyncClient,
) -> None:
    data = images.png((8, 8))
    first = await client.post(
        "/assets",
        data={"kind": "bubble", "title": "Original", "credit": "Ana"},
        files={"file": ("one.png", data, "image/png")},
    )
    again = await client.post(
        "/assets",
        data={"kind": "bubble", "title": "Otro", "credit": "Beto"},
        files={"file": ("two.png", data, "image/png")},
    )

    assert first.status_code == 201 and again.status_code == 200
    assert again.json() == first.json()
    assert again.json()["title"] == "Original"


async def test_patch_updates_title_and_credit_and_can_clear_the_credit(
    client: httpx.AsyncClient,
) -> None:
    created = (await upload(client, images.png((8, 8)))).json()

    r = await client.patch(
        f"/assets/{created['id']}", json={"title": " Nuevo ", "credit": "Ana"}
    )
    assert r.status_code == 200
    assert (r.json()["title"], r.json()["credit"]) == ("Nuevo", "Ana")

    only_credit = await client.patch(f"/assets/{created['id']}", json={"credit": None})
    assert only_credit.json()["credit"] is None
    assert only_credit.json()["title"] == "Nuevo"  # untouched

    blank = await client.patch(f"/assets/{created['id']}", json={"credit": "   "})
    assert blank.json()["credit"] is None


@pytest.mark.parametrize(
    "body",
    [{}, {"title": None}, {"title": ""}, {"title": "t" * 121}, {"credit": "c" * 301}],
)
async def test_patch_rejects_invalid_bodies(
    client: httpx.AsyncClient, body: dict[str, Any]
) -> None:
    created = (await upload(client, images.png((8, 8)))).json()

    r = await client.patch(f"/assets/{created['id']}", json=body)

    assert r.status_code == 422


async def test_patch_of_a_missing_asset_is_404(client: httpx.AsyncClient) -> None:
    r = await client.patch(
        "/assets/0192f3a0-0000-7000-8000-000000000000", json={"title": "x"}
    )

    assert r.status_code == 404


# --- library ----------------------------------------------------------------


async def test_the_library_lists_newest_first_with_paging(
    client: httpx.AsyncClient,
) -> None:
    made = [await png_asset(client, n, title=f"Imagen {n}") for n in range(5)]

    r = await client.get("/assets", params={"limit": 2, "offset": 1})

    assert r.status_code == 200
    body = r.json()
    assert (body["total"], body["limit"], body["offset"]) == (5, 2, 1)
    assert [i["id"] for i in body["items"]] == [made[3]["id"], made[2]["id"]]
    assert set(body["items"][0]) >= {"id", "title", "usage", "thumbUrl", "url"}
    assert body["items"][0]["usage"] == {"maps": 0, "skins": 0, "publications": 0}


async def test_the_library_defaults_and_limits(client: httpx.AsyncClient) -> None:
    default = (await client.get("/assets")).json()
    assert (default["limit"], default["offset"]) == (24, 0)
    assert (await client.get("/assets", params={"limit": 100})).status_code == 200
    for bad in ({"limit": 101}, {"limit": 0}, {"offset": -1}, {"kind": "nope"}):
        assert (await client.get("/assets", params=bad)).status_code == 422


async def test_the_library_filters_by_kind(client: httpx.AsyncClient) -> None:
    await png_asset(client, 1, "bubble")
    background = await png_asset(client, 2, "background")

    body = (await client.get("/assets", params={"kind": "background"})).json()

    assert body["total"] == 1
    assert [i["id"] for i in body["items"]] == [background["id"]]


async def test_the_library_search_matches_title_and_credit_ignoring_case(
    client: httpx.AsyncClient,
) -> None:
    by_title = await png_asset(client, 1, title="Playa Caribe")
    by_credit = await png_asset(client, 2, title="Otra", credit="Foto de PLAYA norte")
    await png_asset(client, 3, title="Montaña")

    body = (await client.get("/assets", params={"q": "playa"})).json()

    assert body["total"] == 2
    assert {i["id"] for i in body["items"]} == {by_title["id"], by_credit["id"]}


async def test_search_treats_percent_and_underscore_literally(
    client: httpx.AsyncClient,
) -> None:
    await png_asset(client, 1, title="100% listo")
    await png_asset(client, 2, title="otro")

    assert (await client.get("/assets", params={"q": "%"})).json()["total"] == 1
    assert (await client.get("/assets", params={"q": "_"})).json()["total"] == 0


async def _map(
    client: httpx.AsyncClient, image_url: str, course_id: int = 1
) -> dict[str, Any]:
    r = await client.post(
        "/course-maps",
        json={"title": "Mapa", "moodleCourseId": course_id, "imageUrl": image_url},
    )
    assert r.status_code == 201, r.text
    body: dict[str, Any] = r.json()
    return body


async def _skin_using(client: httpx.AsyncClient, asset_id: str) -> dict[str, Any]:
    r = await client.post(
        "/skins", json={"name": "Con imagen", "config": skin_configs.image(asset_id)}
    )
    assert r.status_code == 201, r.text
    body: dict[str, Any] = r.json()
    return body


async def test_the_library_counts_usage_in_batch(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    bg1 = await png_asset(client, 1, "background")
    bg2 = await png_asset(client, 2, "background")
    unused = await png_asset(client, 3, "background")
    bubble = await png_asset(client, 4, "bubble")
    await _map(client, bg1["url"], 1)
    await _map(client, bg1["url"], 2)
    await _map(client, bg2["url"], 3)
    await _skin_using(client, bubble["id"])
    await _skin_using(client, bubble["id"])

    statements: list[str] = []
    event.listen(
        engine.sync_engine,
        "before_cursor_execute",
        lambda conn, cur, stmt, *a: statements.append(stmt),
    )
    body = (await client.get("/assets", params={"limit": 100})).json()

    usage = {i["id"]: i["usage"] for i in body["items"]}
    assert usage[bg1["id"]] == {"maps": 2, "skins": 0, "publications": 0}
    assert usage[bg2["id"]] == {"maps": 1, "skins": 0, "publications": 0}
    assert usage[unused["id"]] == {"maps": 0, "skins": 0, "publications": 0}
    assert usage[bubble["id"]] == {"maps": 0, "skins": 2, "publications": 0}
    # Four assets cost a fixed handful of queries, not a few each.
    assert len(statements) <= 6


# --- deleting ---------------------------------------------------------------


async def test_deleting_an_unused_asset_removes_the_row_and_both_files(
    client: httpx.AsyncClient, storage: AssetStorage
) -> None:
    made = await png_asset(client, 1, "background")
    assert (await client.get(made["url"])).status_code == 200

    r = await client.delete(f"/assets/{made['id']}")

    assert r.status_code == 204 and r.content == b""
    assert (await client.get(made["url"])).status_code == 404
    assert (await client.get(made["thumbUrl"])).status_code == 404
    assert (await client.get("/assets")).json()["total"] == 0
    for key in (made["id"], f"{made['id']}.thumb"):
        with pytest.raises(AssetFileMissingError):
            await storage.get(key)


async def test_deleting_a_missing_asset_is_404(client: httpx.AsyncClient) -> None:
    r = await client.delete("/assets/0192f3a0-0000-7000-8000-000000000000")

    assert r.status_code == 404


async def test_an_asset_used_as_a_map_background_cannot_be_deleted(
    client: httpx.AsyncClient,
) -> None:
    bg = await png_asset(client, 1, "background")
    await _map(client, bg["url"])

    r = await client.delete(f"/assets/{bg['id']}")

    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "asset_in_use"
    assert detail["usage"] == {"maps": 1, "skins": 0, "publications": 0}
    assert (await client.get(bg["url"])).status_code == 200  # still served


async def test_an_asset_used_by_a_skin_cannot_be_deleted(
    client: httpx.AsyncClient,
) -> None:
    bubble = await png_asset(client, 1, "bubble")
    skin = await _skin_using(client, bubble["id"])

    r = await client.delete(f"/assets/{bubble['id']}")

    assert r.status_code == 409
    assert r.json()["detail"]["usage"] == {"maps": 0, "skins": 1, "publications": 0}

    # Once the skin is gone, the image is free to go.
    assert (await client.delete(f"/skins/{skin['id']}")).status_code == 204
    assert (await client.delete(f"/assets/{bubble['id']}")).status_code == 204


async def test_a_failing_file_removal_leaves_no_listed_asset_with_missing_bytes(
    client: httpx.AsyncClient, storage: AssetStorage
) -> None:
    made = await png_asset(client, 1, "bubble")

    class Failing:
        async def put(self, key: str, data: bytes) -> None:
            await storage.put(key, data)

        async def get(self, key: str) -> bytes:
            return await storage.get(key)

        async def delete(self, key: str) -> None:
            raise OSError("disk on fire")

    app.dependency_overrides[get_asset_storage] = lambda: Failing()
    r = await client.delete(f"/assets/{made['id']}")

    # The asset is gone for every client; only unreachable bytes may remain.
    assert r.status_code == 204
    assert (await client.get(made["url"])).status_code == 404
    assert (await client.get("/assets")).json()["total"] == 0


# --- a map's background must be a real background ---------------------------


async def test_a_map_background_must_exist_when_it_is_an_asset_url(
    client: httpx.AsyncClient,
) -> None:
    ghost = "/assets/0192f3a0-0000-7000-8000-000000000000"
    created = await _map(client, "http://i/x")

    on_create = await client.post(
        "/course-maps",
        json={"title": "M", "moodleCourseId": 9, "imageUrl": ghost},
    )
    on_patch = await client.patch(
        f"/course-maps/{created['id']}", json={"imageUrl": ghost}
    )

    for r in (on_create, on_patch):
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "background_asset_not_found"


async def test_a_bubble_image_is_not_a_valid_map_background(
    client: httpx.AsyncClient,
) -> None:
    bubble = await png_asset(client, 1, "bubble")
    created = await _map(client, "http://i/x")

    on_create = await client.post(
        "/course-maps",
        json={"title": "M", "moodleCourseId": 9, "imageUrl": bubble["url"]},
    )
    on_patch = await client.patch(
        f"/course-maps/{created['id']}", json={"imageUrl": bubble["url"]}
    )

    for r in (on_create, on_patch):
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "background_asset_wrong_kind"


async def test_other_image_urls_are_not_checked(client: httpx.AsyncClient) -> None:
    bg = await png_asset(client, 1, "background")
    created = await _map(client, bg["url"])  # a real background: accepted

    for url in (
        "https://cdn.example.com/a.png",
        "/assets/not-a-uuid",
        f"{bg['url']}/thumb",
        "/uploads/x.png",
    ):
        r = await client.patch(f"/course-maps/{created['id']}", json={"imageUrl": url})
        assert r.status_code == 200, url


async def test_an_upper_case_asset_id_is_not_accepted_as_a_background(
    client: httpx.AsyncClient,
) -> None:
    bg = await png_asset(client, 1, "background")

    r = await client.post(
        "/course-maps",
        json={
            "title": "M",
            "moodleCourseId": 9,
            "imageUrl": f"/assets/{bg['id'].upper()}",
        },
    )

    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "background_asset_not_found"
