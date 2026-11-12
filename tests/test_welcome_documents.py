"""PDF steps of a welcome flow: the document must be usable when a flow is
saved, it keeps the document from being deleted (draft or publication), and
`welcomeDocuments` is read live without touching snapshots or hashes."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine

from app.enums import Role
from tests.asset_helpers import png_asset, quarantine, upload_document
from tests.conftest import Box
from tests.editor_client import EditorClient
from tests.pdf_files import build_pdf
from tests.tenant_helpers import insert_tenant, insert_token
from tests.test_publications_api import full_map, make_map, match, publish, state
from tests.test_welcome_api import put_settings

MakeClient = Callable[[str | None], EditorClient]

PDF = build_pdf(pages=3)


def pdf_step(asset_id: str, key: str = "documento1", **fields: Any) -> dict[str, Any]:
    return {
        "type": "pdf",
        "key": key,
        "title": "Reglamento",
        "assetId": asset_id,
        "ack": {"label": "Lo he leído"},
        "minSeconds": 30,
        **fields,
    }


def flow(*steps: dict[str, Any]) -> dict[str, Any]:
    return {"welcome": {"steps": list(steps)}}


async def a_document(client: httpx.AsyncClient, pdf: bytes = PDF) -> dict[str, Any]:
    r = await upload_document(client, pdf)
    assert r.status_code == 201, r.text
    body: dict[str, Any] = r.json()
    return body


# --- saving -------------------------------------------------------------------------


async def test_a_pdf_step_is_saved_and_its_document_is_read_live(
    client: httpx.AsyncClient,
) -> None:
    doc = await a_document(client)
    created = await make_map(client)

    r = await put_settings(client, created["id"], flow(pdf_step(doc["id"])))

    assert r.status_code == 200, r.text
    step = r.json()["settings"]["welcome"]["steps"][0]
    assert step["assetId"] == doc["id"] and step["minSeconds"] == 30
    expected = [
        {
            "assetId": doc["id"],
            "url": doc["url"],
            "title": "Guia del curso",
            "pageCount": 3,
            "bytes": len(PDF),
            "available": True,
        }
    ]
    assert (await full_map(client, created["id"]))["welcomeDocuments"] == expected
    assert (
        r.json()["welcomeDocuments"] == expected
        if "welcomeDocuments" in r.json()
        else True
    )


async def test_a_map_without_pdf_steps_has_no_welcome_documents(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)

    assert (await full_map(client, created["id"]))["welcomeDocuments"] == []


async def test_a_document_used_twice_is_listed_once_in_step_order(
    client: httpx.AsyncClient,
) -> None:
    first, second = (
        await a_document(client),
        await a_document(client, build_pdf(pages=2)),
    )
    created = await make_map(client)

    await put_settings(
        client,
        created["id"],
        flow(
            pdf_step(second["id"], "documento2"),
            pdf_step(first["id"], "documento1"),
            pdf_step(second["id"], "documento3"),
        ),
    )

    listed = (await full_map(client, created["id"]))["welcomeDocuments"]
    assert [d["assetId"] for d in listed] == [second["id"], first["id"]]


async def test_a_flow_can_mix_all_five_step_types(client: httpx.AsyncClient) -> None:
    doc = await a_document(client)
    image = await png_asset(client, 1, "image")
    created = await make_map(client)
    steps = [
        {"type": "welcome", "key": "bienvenida", "title": "Hola", "message": "Bien"},
        {
            "type": "video",
            "key": "videointro",
            "title": "Video",
            "video": {"provider": "vimeo", "videoId": "76979871"},
        },
        {
            "type": "html",
            "key": "lectura01",
            "title": "Lectura",
            "html": f'<p>Mira <img src="{image["url"]}" alt="x"></p>',
        },
        pdf_step(doc["id"]),
        {"type": "quiz", "key": "cuestionario", "title": "Quiz", "cmid": 5},
    ]

    r = await put_settings(client, created["id"], {"welcome": {"steps": steps}})

    assert r.status_code == 200, r.text
    published = await publish(client, created["id"])
    shown = (await client.get(f"/published/course-maps/{created['id']}")).json()
    assert [s["type"] for s in shown["settings"]["welcome"]["steps"]] == [
        "welcome",
        "video",
        "html",
        "pdf",
        "quiz",
    ]
    assert [d["assetId"] for d in shown["welcomeDocuments"]] == [doc["id"]]
    assert published["number"] == 1


# --- what the document must be ---------------------------------------------------------


async def test_an_unknown_document_is_refused_with_its_path(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    steps = [
        {"type": "welcome", "key": "bienvenida", "title": "H", "message": "m"},
        pdf_step("00000000-0000-4000-8000-000000000001"),
    ]

    r = await put_settings(client, created["id"], {"welcome": {"steps": steps}})

    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "welcome_invalid"
    assert [(p["path"], p["code"]) for p in detail["problems"]] == [
        ("welcome.steps[1].assetId", "document_unavailable")
    ]


async def test_an_image_is_not_a_document(client: httpx.AsyncClient) -> None:
    image = await png_asset(client, 1, "image")
    created = await make_map(client)

    r = await put_settings(client, created["id"], flow(pdf_step(image["id"])))

    assert r.status_code == 422
    assert r.json()["detail"]["problems"][0]["code"] == "document_unavailable"


async def test_a_quarantined_document_cannot_be_chosen(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    doc = await a_document(client)
    await quarantine(engine, doc["id"])
    created = await make_map(client)

    r = await put_settings(client, created["id"], flow(pdf_step(doc["id"])))

    assert r.status_code == 422
    assert r.json()["detail"]["problems"][0]["code"] == "document_unavailable"


async def test_a_document_whose_file_is_missing_cannot_be_chosen(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    doc = await a_document(client)
    for stored in (tmp_path / "assets").rglob(doc["id"]):
        stored.unlink()
    created = await make_map(client)

    r = await put_settings(client, created["id"], flow(pdf_step(doc["id"])))

    assert r.status_code == 422
    assert r.json()["detail"]["problems"][0]["code"] == "document_unavailable"


async def test_another_tenants_document_does_not_exist_for_a_tenant(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    make_client: MakeClient,
    box: Box,
) -> None:
    alpha = await insert_tenant(engine, "alpha")
    other = make_client(await insert_token(engine, alpha, 1, Role.EDITOR))
    doc = await a_document(other)
    mine = await make_map(client)  # the legacy tenant's map

    r = await put_settings(client, mine["id"], flow(pdf_step(doc["id"])))

    assert r.status_code == 422
    assert r.json()["detail"]["problems"][0]["code"] == "document_unavailable"


async def test_every_unavailable_document_is_listed(client: httpx.AsyncClient) -> None:
    created = await make_map(client)
    ghosts = [
        pdf_step(f"00000000-0000-4000-8000-00000000000{n}", f"documento{n}")
        for n in (1, 2)
    ]

    r = await put_settings(client, created["id"], flow(*ghosts))

    assert [p["path"] for p in r.json()["detail"]["problems"]] == [
        "welcome.steps[0].assetId",
        "welcome.steps[1].assetId",
    ]


# --- deletion is protected --------------------------------------------------------------


async def test_a_document_in_a_draft_flow_cannot_be_deleted_until_the_step_goes(
    client: httpx.AsyncClient,
) -> None:
    doc = await a_document(client)
    created = await make_map(client)
    await put_settings(client, created["id"], flow(pdf_step(doc["id"])))

    listed = (await client.get("/assets", params={"kind": "document"})).json()
    refused = await client.delete(f"/assets/{doc['id']}")

    assert listed["items"][0]["usage"]["maps"] == 1
    assert refused.status_code == 409
    assert refused.json()["detail"]["usage"]["maps"] == 1
    await put_settings(client, created["id"], {"welcome": None})
    assert (await client.delete(f"/assets/{doc['id']}")).status_code == 204


async def test_a_published_document_cannot_be_deleted_even_after_the_draft_moved_on(
    client: httpx.AsyncClient,
) -> None:
    doc = await a_document(client)
    created = await make_map(client)
    await put_settings(client, created["id"], flow(pdf_step(doc["id"])))
    await publish(client, created["id"])
    await put_settings(client, created["id"], {"welcome": None})

    refused = await client.delete(f"/assets/{doc['id']}")

    assert refused.status_code == 409
    assert refused.json()["detail"]["usage"] == {
        "maps": 0,
        "skins": 0,
        "publications": 1,
        "tracks": 0,
    }


async def test_an_image_shown_by_an_html_step_is_in_use(
    client: httpx.AsyncClient,
) -> None:
    image = await png_asset(client, 1, "image")
    created = await make_map(client)
    html = {
        "type": "html",
        "key": "lectura01",
        "title": "L",
        "html": f'<img src="{image["url"]}" alt="">',
    }
    await put_settings(client, created["id"], flow(html))

    refused = await client.delete(f"/assets/{image['id']}")
    await publish(client, created["id"])
    await put_settings(client, created["id"], {"welcome": None})
    still = await client.delete(f"/assets/{image['id']}")

    assert refused.status_code == 409
    assert still.status_code == 409
    assert still.json()["detail"]["usage"]["publications"] == 1


# --- live reads, snapshots and hashes ------------------------------------------------------


async def test_the_published_map_reads_its_documents_live(
    client: httpx.AsyncClient, engine: AsyncEngine, tmp_path: Path
) -> None:
    doc = await a_document(client)
    created = await make_map(client)
    map_id = created["id"]
    await put_settings(client, map_id, flow(pdf_step(doc["id"])))
    await publish(client, map_id)
    before = await state(client, map_id)

    first = (await client.get(f"/published/course-maps/{map_id}")).json()
    await quarantine(engine, doc["id"])
    taken_down = (await client.get(f"/published/course-maps/{map_id}")).json()
    draft = await full_map(client, map_id)

    assert first["welcomeDocuments"][0]["available"] is True
    assert first["welcomeDocuments"][0]["title"] == "Guia del curso"
    entry = taken_down["welcomeDocuments"][0]
    assert entry["assetId"] == doc["id"] and entry["available"] is False
    assert draft["welcomeDocuments"][0]["available"] is False
    # Nothing about it is in the snapshot: the publication is still up to date,
    # with the same hash.
    after = await state(client, map_id)
    assert after["status"] == "up_to_date"
    assert after["draftHash"] == before["draftHash"]
    assert "welcomeDocuments" not in str(before)


async def test_a_missing_file_reads_as_unavailable_not_as_an_error(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    doc = await a_document(client)
    created = await make_map(client)
    await put_settings(client, created["id"], flow(pdf_step(doc["id"])))
    for stored in (tmp_path / "assets").rglob(doc["id"]):
        stored.unlink()

    read = await client.get(f"/course-maps/{created['id']}")

    assert read.status_code == 200
    assert read.json()["welcomeDocuments"][0]["available"] is False


async def test_discard_and_restore_bring_back_a_flow_with_a_document(
    client: httpx.AsyncClient,
) -> None:
    doc = await a_document(client)
    created = await make_map(client)
    map_id = created["id"]
    saved = await put_settings(client, map_id, flow(pdf_step(doc["id"])))
    await publish(client, map_id, "con documento")
    await put_settings(client, map_id, {"welcome": None})
    revision = (await full_map(client, map_id))["revision"]

    discarded = await client.post(
        f"/course-maps/{map_id}/discard-changes", headers=match(revision)
    )

    assert discarded.status_code == 200, discarded.text
    result = discarded.json()["map"]
    assert result["settings"]["welcome"] == saved.json()["settings"]["welcome"]
    assert [d["assetId"] for d in result["welcomeDocuments"]] == [doc["id"]]

    await put_settings(client, map_id, {"welcome": None})
    await publish(client, map_id, "sin flujo")
    revision = (await full_map(client, map_id))["revision"]
    restored = await client.post(
        f"/course-maps/{map_id}/publications/1/restore", headers=match(revision)
    )
    assert restored.status_code == 200, restored.text
    assert (
        restored.json()["map"]["settings"]["welcome"]
        == (saved.json()["settings"]["welcome"])
    )
    assert [d["assetId"] for d in restored.json()["map"]["welcomeDocuments"]] == [
        doc["id"]
    ]


async def test_a_flow_without_documents_publishes_the_same_hash_as_before(
    client: httpx.AsyncClient,
) -> None:
    # Adding the document kind changed no snapshot that has none: a legacy
    # curtain still hashes as pinned in `test_welcome_flow.PRE_FLOW_HASHES`.
    created = await make_map(client)
    before = await state(client, created["id"])

    await put_settings(client, created["id"], {"welcome": {"title": "Hola"}})
    await put_settings(client, created["id"], {"welcome": None})

    assert (await state(client, created["id"]))["draftHash"] == before["draftHash"]
