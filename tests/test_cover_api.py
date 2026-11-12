"""Covers through the real stack: sent with a track or found inside it, made by
the cover pipeline, replaced and removed, counted, protected, taken down with
their track, and never served as the editor sent them."""

import io
import uuid
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.assets.storage import AssetStorage, content_key
from app.enums import Role
from app.models import Asset
from app.tenancy.scope import SYSTEM
from tests import audio_files as af
from tests import images
from tests.asset_helpers import library_track, png_asset, quarantine, release
from tests.conftest import Box
from tests.editor_client import EditorClient
from tests.sessions import new_session
from tests.tenant_helpers import insert_tenant, insert_token

MakeClient = Callable[[str | None], EditorClient]


async def post_track(
    client: httpx.AsyncClient,
    audio: bytes | None = None,
    *,
    cover: bytes | None = None,
    cover_name: str = "portada.png",
    **form: str,
) -> httpx.Response:
    files = {"file": ("song.mp3", audio or af.mp3(40), "audio/mpeg")}
    if cover is not None:
        files["cover"] = (cover_name, cover, "image/png")
    fields = {
        "kind": "audio",
        "title": "Brisa",
        "credit": "Ana Perez, CC BY 4.0",
        "rightsDeclared": "true",
        **form,
    }
    return await client.post("/assets", data=fields, files=files)


async def put_cover(
    client: httpx.AsyncClient, asset_id: str, data: bytes, name: str = "c.png"
) -> httpx.Response:
    return await client.put(
        f"/assets/{asset_id}/cover", files={"file": (name, data, "image/png")}
    )


def served_image(r: httpx.Response) -> Image.Image:
    image = Image.open(io.BytesIO(r.content))
    image.load()
    return image


async def rows(engine: AsyncEngine, kind: str | None = None) -> list[Asset]:
    async with new_session(engine, SYSTEM) as s:
        stmt = select(Asset)
        if kind:
            stmt = stmt.where(Asset.kind == kind)
        return list((await s.scalars(stmt)).all())


async def present(storage: AssetStorage, asset_id: str) -> bool:
    key = content_key(uuid.UUID(asset_id))
    return key in await storage.present([key])


def track_with_art(art: bytes, *, mp4: bool = False) -> bytes:
    if mp4:
        return af.m4a(covers=[art])
    return af.mp3_with_artwork(af.id3v2_with_pictures([(3, art)]))


# --- a cover sent with the track ----------------------------------------------


async def test_a_cover_sent_with_a_track_is_stored_as_a_square_webp(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    r = await post_track(client, cover=images.png((900, 600)))

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["warnings"] == []
    assert body["coverAssetId"] and body["coverUrl"] == (
        f"/api/v1/assets/{body['coverAssetId']}"
    )
    served = await client.get(body["coverUrl"])
    assert served.status_code == 200
    assert served.headers["content-type"] == "image/webp"
    assert served.headers["x-content-type-options"] == "nosniff"
    assert "immutable" in served.headers["cache-control"]
    assert served_image(served).size == (512, 512)
    cover = (await rows(engine, "cover"))[0]
    assert (cover.width, cover.height, cover.mime) == (512, 512, "image/webp")


async def test_the_cover_asset_has_the_shape_of_an_asset(
    client: httpx.AsyncClient,
) -> None:
    track = (await post_track(client, cover=images.png((64, 64)))).json()

    listed = (await client.get("/assets", params={"kind": "cover"})).json()

    [cover] = listed["items"]
    assert cover["id"] == track["coverAssetId"]
    assert (cover["kind"], cover["mime"]) == ("cover", "image/webp")
    assert (cover["width"], cover["height"]) == (64, 64)
    assert cover["thumbUrl"] is None and cover["coverUrl"] is None
    assert cover["usage"]["tracks"] == 1
    # Not mixed into the library the editor browses.
    everything = (await client.get("/assets")).json()
    assert [i["id"] for i in everything["items"]] == [track["id"]]


async def test_a_wide_cover_is_cut_from_its_centre(
    client: httpx.AsyncClient,
) -> None:
    source = Image.new("RGB", (300, 100))
    for band, color in enumerate([(255, 0, 0), (0, 255, 0), (0, 0, 255)]):
        source.paste(color, (band * 100, 0, band * 100 + 100, 100))

    r = await post_track(client, cover=images.encode(source, "PNG"))

    pixels = served_image(await client.get(r.json()["coverUrl"])).convert("RGB")
    red, green, blue = pixels.getpixel((50, 50))  # type: ignore[misc]
    assert pixels.size == (100, 100) and green > 200 and red < 60 and blue < 60


async def test_a_cover_with_transparency_keeps_it(client: httpx.AsyncClient) -> None:
    r = await post_track(
        client, cover=images.png((50, 50), mode="RGBA", color=(1, 2, 3, 0))
    )

    image = served_image(await client.get(r.json()["coverUrl"]))
    assert image.mode == "RGBA"
    assert image.getpixel((10, 10))[3] == 0  # type: ignore[index]


@pytest.mark.parametrize(
    "make",
    [images.jpeg_with_metadata, images.png_with_metadata, images.webp_with_metadata],
    ids=["jpeg", "png", "webp"],
)
async def test_the_cover_the_editor_sent_is_never_served_as_it_was(
    client: httpx.AsyncClient, make: Callable[[], bytes]
) -> None:
    sent = make()
    secrets = [images.SECRET_CAMERA.encode(), images.SECRET_COMMENT.encode()]

    r = await post_track(client, cover=sent)

    served = await client.get(r.json()["coverUrl"])
    assert served.content != sent
    for secret in secrets:
        assert secret not in served.content
    assert dict(served_image(served).getexif()) == {}


async def test_an_identical_cover_is_stored_once_and_shared(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    first = (await post_track(client, af.mp3(40), cover=images.png())).json()
    second = (await post_track(client, af.mp3(41), cover=images.png())).json()

    assert first["id"] != second["id"]
    assert first["coverAssetId"] == second["coverAssetId"]
    assert len(await rows(engine, "cover")) == 1


# --- a cover found inside the track -------------------------------------------


@pytest.mark.parametrize("mp4", [False, True], ids=["mp3 APIC", "m4a covr"])
async def test_the_artwork_inside_the_file_becomes_the_cover(
    client: httpx.AsyncClient, mp4: bool
) -> None:
    artwork = images.jpeg_with_metadata((700, 400))
    data = track_with_art(artwork, mp4=mp4)

    r = await client.post(
        "/assets",
        data={
            "kind": "audio",
            "title": "Brisa",
            "credit": "Ana",
            "rightsDeclared": "true",
        },
        files={"file": ("a.bin", data, "application/octet-stream")},
    )

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["warnings"] == [] and body["coverUrl"]
    served = await client.get(body["coverUrl"])
    assert served_image(served).size == (400, 400)
    assert images.SECRET_CAMERA.encode() not in served.content
    assert artwork not in served.content


async def test_the_cover_sent_wins_over_the_artwork_inside(
    client: httpx.AsyncClient,
) -> None:
    inside = images.png((100, 100), color=(255, 0, 0))
    sent = images.png((80, 80), color=(0, 0, 255))

    r = await post_track(client, track_with_art(inside), cover=sent)

    served = served_image(await client.get(r.json()["coverUrl"])).convert("RGB")
    assert served.size == (80, 80)
    assert served.getpixel((5, 5))[2] > 200  # type: ignore[index]  # blue, not red


async def test_a_track_without_a_picture_has_no_cover(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    body = (await post_track(client)).json()

    assert body["coverAssetId"] is None and body["coverUrl"] is None
    assert body["warnings"] == []
    assert await rows(engine, "cover") == []


async def test_the_track_itself_never_carries_its_artwork(
    client: httpx.AsyncClient,
) -> None:
    artwork = images.jpeg((60, 60))
    r = await post_track(client, track_with_art(artwork))

    audio = await client.get(r.json()["url"])

    assert artwork not in audio.content and af.SECRET_ARTWORK not in audio.content


@pytest.mark.parametrize(
    "artwork",
    [
        b"this is not an image",
        images.png()[: len(images.png()) // 2],
        images.png_header_only(60000, 60000),  # a decompression bomb
        images.png((2001, 8)),  # over the side limit
        images.animated_webp(),
        images.gif(),
        images.jpeg((300, 300)) + bytes(150_000),  # over the byte limit
    ],
    ids=["text", "truncated", "bomb", "too wide", "animated", "gif", "too heavy"],
)
@pytest.mark.parametrize("mp4", [False, True], ids=["mp3", "m4a"])
async def test_hostile_artwork_is_dropped_and_the_audio_still_uploads(
    client: httpx.AsyncClient, engine: AsyncEngine, artwork: bytes, mp4: bool
) -> None:
    r = await client.post(
        "/assets",
        data={
            "kind": "audio",
            "title": "Brisa",
            "credit": "Ana",
            "rightsDeclared": "true",
        },
        files={"file": ("a.bin", track_with_art(artwork, mp4=mp4), "audio/mpeg")},
    )

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["warnings"] == ["cover_dropped"]
    assert body["coverAssetId"] is None and body["coverUrl"] is None
    assert len(await rows(engine, "audio")) == 1
    assert await rows(engine, "cover") == []


# --- a cover the editor sent has to be good -----------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        b"not an image",
        images.png()[: len(images.png()) // 2],
        images.png_header_only(60000, 60000),
        images.png((2001, 8)),
        images.animated_png(),
        images.svg(),
    ],
    ids=["text", "truncated", "bomb", "too wide", "animated", "svg"],
)
async def test_an_invalid_cover_refuses_the_whole_upload(
    client: httpx.AsyncClient, engine: AsyncEngine, bad: bytes
) -> None:
    r = await post_track(client, cover=bad)

    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["code"] == "cover_invalid" and set(detail) == {"code", "message"}
    assert await rows(engine) == []  # no track, no cover, nothing left behind


async def test_a_cover_over_the_byte_limit_is_too_large(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    heavy = images.jpeg((300, 300)) + bytes(150_000)

    r = await post_track(client, cover=heavy)

    assert r.status_code == 413
    assert r.json()["detail"]["code"] == "cover_too_large"
    assert await rows(engine) == []


async def test_an_empty_cover_is_invalid(client: httpx.AsyncClient) -> None:
    r = await post_track(client, cover=b"")

    assert r.status_code == 422 and r.json()["detail"]["code"] == "cover_invalid"


async def test_a_cover_only_comes_with_audio(client: httpx.AsyncClient) -> None:
    r = await client.post(
        "/assets",
        data={"kind": "bubble"},
        files={
            "file": ("a.png", images.png(), "image/png"),
            "cover": ("c.png", images.png(), "image/png"),
        },
    )

    assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_upload"


async def test_a_cover_cannot_be_uploaded_as_an_asset_of_its_own(
    client: httpx.AsyncClient,
) -> None:
    r = await client.post(
        "/assets",
        data={"kind": "cover"},
        files={"file": ("c.png", images.png(), "image/png")},
    )

    assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_upload"


async def test_an_identical_track_comes_back_untouched_whatever_cover_it_brings(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    first = await post_track(client)
    again = await post_track(client, cover=images.png())

    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json()["id"] == first.json()["id"]
    assert again.json()["coverUrl"] is None
    assert await rows(engine, "cover") == []


# --- replacing and removing ---------------------------------------------------


async def test_putting_a_cover_on_a_track_without_one(
    client: httpx.AsyncClient,
) -> None:
    track = (await post_track(client)).json()

    r = await put_cover(client, track["id"], images.jpeg((200, 100)))

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == track["id"] and body["coverUrl"]
    assert served_image(await client.get(body["coverUrl"])).size == (100, 100)


async def test_replacing_a_cover_releases_the_old_one(
    client: httpx.AsyncClient, engine: AsyncEngine, storage: AssetStorage
) -> None:
    track = (await post_track(client, cover=images.png(color=(255, 0, 0)))).json()
    old = track["coverAssetId"]

    r = await put_cover(client, track["id"], images.png(color=(0, 0, 255)))

    assert r.status_code == 200
    new = r.json()["coverAssetId"]
    assert new and new != old
    assert (await client.get(r.json()["coverUrl"])).status_code == 200
    assert (await client.get(f"/assets/{old}")).status_code == 404
    assert not await present(storage, old) and await present(storage, new)
    assert [str(c.id) for c in await rows(engine, "cover")] == [new]


async def test_putting_the_same_cover_again_changes_nothing(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    track = (await post_track(client, cover=images.png())).json()

    r = await put_cover(client, track["id"], images.png())

    assert r.status_code == 200
    assert r.json()["coverAssetId"] == track["coverAssetId"]
    assert len(await rows(engine, "cover")) == 1


async def test_a_cover_shared_with_another_track_survives_a_replacement(
    client: httpx.AsyncClient,
) -> None:
    first = (await post_track(client, af.mp3(40), cover=images.png())).json()
    second = (await post_track(client, af.mp3(41), cover=images.png())).json()

    await put_cover(client, first["id"], images.png(color=(9, 9, 9)))

    assert (await client.get(second["coverUrl"])).status_code == 200


async def test_removing_a_cover(
    client: httpx.AsyncClient, engine: AsyncEngine, storage: AssetStorage
) -> None:
    track = (await post_track(client, cover=images.png())).json()

    r = await client.delete(f"/assets/{track['id']}/cover")

    assert r.status_code == 204 and r.content == b""
    assert (await client.get(track["coverUrl"])).status_code == 404
    assert not await present(storage, track["coverAssetId"])
    assert await rows(engine, "cover") == []
    listed = (await client.get("/assets")).json()["items"][0]
    assert listed["coverAssetId"] is None and listed["coverUrl"] is None
    # Again: nothing to remove, still fine.
    assert (await client.delete(f"/assets/{track['id']}/cover")).status_code == 204


@pytest.mark.parametrize(
    "bad", [b"junk", images.png((3000, 10)), images.animated_png()]
)
async def test_a_bad_replacement_leaves_the_cover_as_it_was(
    client: httpx.AsyncClient, bad: bytes
) -> None:
    track = (await post_track(client, cover=images.png())).json()

    r = await put_cover(client, track["id"], bad)

    assert r.status_code == 422 and r.json()["detail"]["code"] == "cover_invalid"
    assert (await client.get(track["coverUrl"])).status_code == 200


async def test_a_replacement_over_the_byte_limit_is_too_large(
    client: httpx.AsyncClient,
) -> None:
    track = (await post_track(client)).json()

    r = await put_cover(client, track["id"], images.jpeg((300, 300)) + bytes(150_000))

    assert r.status_code == 413
    assert r.json()["detail"]["code"] == "cover_too_large"


async def test_an_endless_replacement_is_cut_off(
    client: httpx.AsyncClient, box: Box
) -> None:
    track = (await post_track(client)).json()
    sent = 0

    async def endless() -> Any:
        nonlocal sent
        yield (
            b'--b\r\nContent-Disposition: form-data; name="file"; filename="x.png"\r\n'
            b"Content-Type: image/png\r\n\r\n"
        )
        for _ in range(10_000):
            sent += 1
            yield b"\x00" * 10_000

    r = await client.put(
        f"/assets/{track['id']}/cover",
        content=endless(),
        headers={"Content-Type": "multipart/form-data; boundary=b"},
    )

    assert r.status_code == 413 and r.json()["detail"]["code"] == "cover_too_large"
    assert sent < 100


async def test_a_replacement_needs_a_file(client: httpx.AsyncClient) -> None:
    track = (await post_track(client)).json()

    r = await client.put(f"/assets/{track['id']}/cover", data={"x": "y"})

    assert r.status_code in (400, 422)


# --- who may touch which cover -------------------------------------------------


async def test_a_foreign_track_is_not_found(
    engine: AsyncEngine, make_client: MakeClient, storage: AssetStorage, box: Box
) -> None:
    alpha = make_client(
        await insert_token(engine, await insert_tenant(engine, "alpha"), 7, Role.EDITOR)
    )
    beta = make_client(
        await insert_token(engine, await insert_tenant(engine, "beta"), 7, Role.EDITOR)
    )
    track = (await post_track(alpha, cover=images.png())).json()

    put = await put_cover(beta, track["id"], images.png(color=(1, 2, 3)))
    delete = await beta.delete(f"/assets/{track['id']}/cover")

    assert (put.status_code, delete.status_code) == (404, 404)
    assert (await alpha.get(track["coverUrl"])).status_code == 200  # untouched
    assert (await beta.delete(f"/assets/{track['coverAssetId']}")).status_code == 404


async def test_a_library_track_is_not_theirs_to_change(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    library = await library_track(engine)

    put = await put_cover(client, str(library.id), images.png())
    delete = await client.delete(f"/assets/{library.id}/cover")

    assert (put.status_code, delete.status_code) == (403, 403)
    assert put.json()["detail"]["code"] == "asset_is_library"


@pytest.mark.parametrize("what", ["image", "unknown", "cover"])
async def test_only_a_track_can_have_a_cover(
    client: httpx.AsyncClient, what: str
) -> None:
    if what == "image":
        target = (await png_asset(client, 1))["id"]
    elif what == "cover":
        target = (await post_track(client, cover=images.png())).json()["coverAssetId"]
    else:
        target = str(uuid.uuid4())

    put = await put_cover(client, target, images.png())
    delete = await client.delete(f"/assets/{target}/cover")

    assert (put.status_code, delete.status_code) == (404, 404)


async def test_a_quarantined_track_cannot_be_given_a_cover(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    track = (await post_track(client)).json()
    await quarantine(engine, track["id"])

    assert (await put_cover(client, track["id"], images.png())).status_code == 404


async def test_a_student_cannot_change_covers(
    engine: AsyncEngine, make_client: MakeClient, storage: AssetStorage
) -> None:
    tenant = await insert_tenant(engine, "alpha")
    editor = make_client(await insert_token(engine, tenant, 7, Role.EDITOR))
    student = make_client(await insert_token(engine, tenant, 7, Role.STUDENT))
    track = (await post_track(editor, cover=images.png())).json()

    put = await put_cover(student, track["id"], images.png(color=(1, 1, 1)))
    delete = await student.delete(f"/assets/{track['id']}/cover")

    assert (put.status_code, delete.status_code) == (403, 403)


async def test_the_audio_switch_governs_covers(
    client: httpx.AsyncClient, box: Box
) -> None:
    track = (await post_track(client)).json()
    box.change(audio_uploads_enabled=False)

    r = await put_cover(client, track["id"], images.png())

    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "audio_uploads_disabled"


# --- quota, usage, deletion, garbage ------------------------------------------


async def test_the_cover_counts_in_the_quota(
    engine: AsyncEngine, make_client: MakeClient, storage: AssetStorage, box: Box
) -> None:
    audio = af.mp3(40)
    tenant = await insert_tenant(engine, "small", quota=len(audio) + 20)
    editor = make_client(await insert_token(engine, tenant, 7, Role.EDITOR))

    r = await post_track(editor, audio, cover=images.png((200, 200)))

    assert r.status_code == 507
    assert r.json()["detail"]["code"] == "asset_quota_exceeded"
    assert await rows(engine) == []
    # The track alone fits; a cover that no longer does is refused on its own.
    track = (await post_track(editor, audio)).json()
    assert (
        await put_cover(editor, track["id"], images.png((200, 200)))
    ).status_code == 507


async def test_a_cover_in_use_cannot_be_deleted_on_its_own(
    client: httpx.AsyncClient,
) -> None:
    track = (await post_track(client, cover=images.png())).json()

    r = await client.delete(f"/assets/{track['coverAssetId']}")

    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "asset_in_use" and detail["usage"]["tracks"] == 1
    assert (await client.get(track["coverUrl"])).status_code == 200


async def test_deleting_the_track_deletes_its_cover(
    client: httpx.AsyncClient, engine: AsyncEngine, storage: AssetStorage
) -> None:
    track = (await post_track(client, cover=images.png())).json()

    assert (await client.delete(f"/assets/{track['id']}")).status_code == 204

    assert (await client.get(track["coverUrl"])).status_code == 404
    assert not await present(storage, track["coverAssetId"])
    assert await rows(engine) == []


async def test_deleting_one_track_keeps_a_cover_another_still_uses(
    client: httpx.AsyncClient,
) -> None:
    first = (await post_track(client, af.mp3(40), cover=images.png())).json()
    second = (await post_track(client, af.mp3(41), cover=images.png())).json()

    await client.delete(f"/assets/{first['id']}")

    assert (await client.get(second["coverUrl"])).status_code == 200


# --- takedown ------------------------------------------------------------------


async def test_quarantining_the_track_takes_its_cover_down_and_releasing_restores_it(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    track = (await post_track(client, cover=images.png())).json()
    assert (await client.get(track["coverUrl"])).status_code == 200

    await quarantine(engine, track["id"])
    assert (await client.get(track["coverUrl"])).status_code == 404
    assert (await client.get(track["url"])).status_code == 404

    await release(engine, track["id"])
    assert (await client.get(track["coverUrl"])).status_code == 200


async def test_a_shared_cover_stays_up_while_one_of_its_tracks_is_in_service(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    first = (await post_track(client, af.mp3(40), cover=images.png())).json()
    second = (await post_track(client, af.mp3(41), cover=images.png())).json()

    await quarantine(engine, first["id"])
    assert (await client.get(first["coverUrl"])).status_code == 200  # the other's
    await quarantine(engine, second["id"])
    assert (await client.get(first["coverUrl"])).status_code == 404


async def test_the_total_count_of_assets_matches_what_is_stored(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    await post_track(client, cover=images.png())

    async with new_session(engine, SYSTEM) as s:
        total = await s.scalar(select(func.count()).select_from(Asset))
    assert total == 2  # the track and its cover
