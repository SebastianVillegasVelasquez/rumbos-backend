"""`/assets` through the real stack: routes, service, Postgres, a temp directory."""

import asyncio
import dataclasses
import io
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.api.routes.assets import get_asset_settings, get_asset_storage
from app.assets.storage import AssetStorage, LocalDiskAssetStorage
from app.main import app
from app.models import Asset
from app.services.asset_service import AssetSettings
from tests import images

ENABLED = AssetSettings(
    uploads_enabled=True,
    max_total_bytes=1_000_000_000,
    max_background_bytes=200_000,
    max_bubble_bytes=50_000,
    max_background_side=2000,
    max_bubble_side=100,
)


class Box:
    """The settings the overridden dependency returns; tests tweak `current`."""

    current = ENABLED

    def change(self, **fields: Any) -> None:
        self.current = dataclasses.replace(self.current, **fields)


@pytest.fixture
def box() -> Iterator[Box]:
    box = Box()
    app.dependency_overrides[get_asset_settings] = lambda: box.current
    yield box
    app.dependency_overrides.pop(get_asset_settings, None)


@pytest.fixture
def storage(tmp_path: Path) -> Iterator[AssetStorage]:
    storage = LocalDiskAssetStorage(tmp_path / "assets")
    app.dependency_overrides[get_asset_storage] = lambda: storage
    yield storage
    app.dependency_overrides.pop(get_asset_storage, None)


@pytest.fixture
async def client(
    engine: AsyncEngine, box: Box, storage: AssetStorage
) -> AsyncIterator[httpx.AsyncClient]:
    app.state.db_engine = engine
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


async def upload(
    client: httpx.AsyncClient,
    data: bytes,
    kind: str = "bubble",
    *,
    content_type: str = "image/png",
    filename: str = "pic.png",
) -> httpx.Response:
    return await client.post(
        "/assets",
        data={"kind": kind},
        files={"file": (filename, data, content_type)},
    )


# --- uploading --------------------------------------------------------------


async def test_upload_a_background_returns_the_contract_shape(
    client: httpx.AsyncClient,
) -> None:
    r = await upload(client, images.png((1600, 900)), "background")

    assert r.status_code == 201
    body = r.json()
    assert set(body) == {
        "id",
        "kind",
        "mime",
        "width",
        "height",
        "bytes",
        "url",
        "thumbUrl",
        "createdAt",
    }
    assert body["kind"] == "background" and body["mime"] == "image/png"
    assert (body["width"], body["height"]) == (1600, 900)
    assert body["url"] == f"/assets/{body['id']}"
    assert body["thumbUrl"] == f"/assets/{body['id']}/thumb"
    stored = await client.get(body["url"])
    assert body["bytes"] == len(stored.content)


async def test_a_bubble_image_has_no_thumbnail(client: httpx.AsyncClient) -> None:
    body = (await upload(client, images.png((64, 64)))).json()

    assert body["thumbUrl"] is None
    assert (await client.get(f"/assets/{body['id']}/thumb")).status_code == 404


async def test_the_background_thumbnail_is_webp_at_most_640_wide(
    client: httpx.AsyncClient,
) -> None:
    body = (await upload(client, images.png((1600, 900)), "background")).json()

    thumb = await client.get(body["thumbUrl"])

    assert thumb.status_code == 200
    assert thumb.headers["content-type"] == "image/webp"
    assert Image.open(io.BytesIO(thumb.content)).size == (640, 360)


async def test_identical_content_of_the_same_kind_is_deduplicated(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    first = await upload(client, images.png((50, 50)))
    again = await upload(client, images.png((50, 50)), filename="other-name.png")
    other_kind = await upload(client, images.png((50, 50)), "background")

    assert first.status_code == 201
    assert again.status_code == 200
    assert again.json() == first.json()
    assert other_kind.status_code == 201
    assert other_kind.json()["id"] != first.json()["id"]
    async with AsyncSession(engine) as s:
        assert await s.scalar(select(func.count()).select_from(Asset)) == 2


async def test_equivalent_images_with_different_metadata_are_deduplicated(
    client: httpx.AsyncClient,
) -> None:
    """The hash is of the stored bytes, i.e. after metadata is stripped."""
    plain = await upload(client, images.jpeg((40, 40)), content_type="image/jpeg")
    tagged = await upload(
        client, images.jpeg((40, 40), exif=images.exif_with_gps().tobytes())
    )

    assert plain.status_code == 201
    assert tagged.status_code == 200
    assert tagged.json()["id"] == plain.json()["id"]


async def test_concurrent_identical_uploads_make_one_asset(
    client: httpx.AsyncClient, engine: AsyncEngine, tmp_path: Path
) -> None:
    data = images.png((70, 70))

    responses = await asyncio.gather(*(upload(client, data) for _ in range(6)))

    assert sorted(r.status_code for r in responses) == [200] * 5 + [201]
    assert len({r.json()["id"] for r in responses}) == 1
    async with AsyncSession(engine) as s:
        assert await s.scalar(select(func.count()).select_from(Asset)) == 1
    # The losers' files were cleaned up: only the winner's remain.
    stored = [p for p in (tmp_path / "assets").rglob("*") if p.is_file()]
    assert len(stored) == 1


@pytest.mark.parametrize(
    ("data", "content_type"),
    [
        (b"MZ\x90\x00 definitely not an image", "image/png"),
        (b"<html><body>hi</body></html>", "image/jpeg"),
        (images.svg(), "image/svg+xml"),
        (images.svg(), "image/png"),
        (images.gif(), "image/gif"),
        (images.gif(), "image/png"),
        (images.animated_webp(), "image/webp"),
        (images.animated_png(), "image/png"),
    ],
)
async def test_unsupported_files_are_refused_whatever_they_claim(
    client: httpx.AsyncClient, data: bytes, content_type: str
) -> None:
    r = await upload(client, data, content_type=content_type)

    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "asset_type_not_allowed"


async def test_a_corrupt_image_is_invalid(client: httpx.AsyncClient) -> None:
    data = images.png((64, 64))

    r = await upload(client, data[: len(data) // 2])

    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "asset_invalid_image"


async def test_an_empty_file_is_invalid(client: httpx.AsyncClient) -> None:
    r = await upload(client, b"")

    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "asset_invalid_image"


async def test_the_declared_type_and_name_do_not_matter(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    r = await upload(
        client,
        images.png((20, 20)),
        content_type="text/html",
        filename="../../../evil<script>.html",
    )

    assert r.status_code == 201
    assert r.json()["mime"] == "image/png"  # from the bytes
    served = await client.get(r.json()["url"])
    assert served.headers["content-type"] == "image/png"
    names = [p.name for p in (tmp_path / "assets").rglob("*") if p.is_file()]
    assert names == [r.json()["id"]]  # never the client's name


async def test_oversize_files_are_413_per_kind(
    client: httpx.AsyncClient, box: Box
) -> None:
    box.change(max_bubble_bytes=2_000, max_background_bytes=100_000)
    noisy = Image.effect_noise((300, 300), 90)  # incompressible
    big = images.encode(noisy, "PNG")
    assert 2_000 < len(big) < 100_000

    as_bubble = await upload(client, big)
    as_background = await upload(client, big, "background")

    assert as_bubble.status_code == 413
    assert as_bubble.json()["detail"] == {
        "code": "asset_too_large",
        "message": "The file is too large",
        "limitBytes": 2_000,
    }
    assert as_background.status_code == 201


async def test_an_oversize_body_is_cut_off_without_a_content_length(
    client: httpx.AsyncClient, box: Box
) -> None:
    """A chunked body, as a hostile client would send: stopped at the cap."""
    box.change(max_background_bytes=10_000, max_bubble_bytes=10_000)
    sent = 0

    async def endless() -> AsyncIterator[bytes]:
        nonlocal sent
        yield (
            b'--b\r\nContent-Disposition: form-data; name="kind"\r\n\r\nbubble\r\n'
            b'--b\r\nContent-Disposition: form-data; name="file"; filename="x.png"\r\n'
            b"Content-Type: image/png\r\n\r\n"
        )
        for _ in range(10_000):
            sent += 1
            yield b"\x00" * 10_000
        yield b"\r\n--b--\r\n"

    r = await client.post(
        "/assets",
        content=endless(),
        headers={"Content-Type": "multipart/form-data; boundary=b"},
    )

    assert r.status_code == 413
    assert r.json()["detail"]["code"] == "asset_too_large"
    assert sent < 100  # nowhere near the 100 MB it offered


async def test_a_declared_oversize_body_is_refused_up_front(
    client: httpx.AsyncClient, box: Box
) -> None:
    box.change(max_background_bytes=1_000, max_bubble_bytes=1_000)

    r = await client.post(
        "/assets",
        content=b"x" * 500_000,
        headers={"Content-Type": "multipart/form-data; boundary=b"},
    )

    assert r.status_code == 413


async def test_too_many_pixels_is_refused(client: httpx.AsyncClient) -> None:
    r = await upload(client, images.png((101, 20)))  # bubble limit is 100 px

    assert r.status_code == 422
    assert r.json()["detail"] == {
        "code": "asset_dimensions_too_large",
        "message": "The image is too large in pixels",
        "maxSide": 100,
        "width": 101,
        "height": 20,
    }


@pytest.mark.parametrize("size", [(60000, 60000), (9000, 9000)])
async def test_decompression_bombs_are_refused(
    client: httpx.AsyncClient, size: tuple[int, int]
) -> None:
    r = await upload(client, images.png_header_only(*size), "background")

    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "asset_dimensions_too_large"


async def test_metadata_is_stripped_from_what_is_served(
    client: httpx.AsyncClient,
) -> None:
    r = await upload(
        client,
        images.jpeg_with_metadata((30, 30)),
        content_type="image/jpeg",
    )

    served = (await client.get(r.json()["url"])).content

    for secret in (images.SECRET_CAMERA.encode(), b"Exif", b"GPS"):
        assert secret not in served
    assert dict(Image.open(io.BytesIO(served)).getexif()) == {}


async def test_the_quota_stops_uploads_with_507(
    client: httpx.AsyncClient, box: Box
) -> None:
    first = await upload(client, images.png((60, 60)))
    box.change(max_total_bytes=first.json()["bytes"] + 10)

    over = await upload(client, images.png((61, 61), color=(1, 2, 3)))
    # Known content is not "new storage", so it is still fine.
    twin = await upload(client, images.png((60, 60)))

    assert over.status_code == 507
    assert over.json()["detail"]["code"] == "asset_quota_exceeded"
    assert twin.status_code == 200


async def test_a_full_quota_refuses_before_decoding(
    client: httpx.AsyncClient, box: Box
) -> None:
    await upload(client, images.png((60, 60)))
    box.change(max_total_bytes=1)

    r = await upload(client, b"not even an image")

    assert r.status_code == 507


async def test_disabled_uploads_answer_503_and_store_nothing(
    client: httpx.AsyncClient, box: Box, engine: AsyncEngine, tmp_path: Path
) -> None:
    box.change(uploads_enabled=False)

    r = await upload(client, images.png())

    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "uploads_disabled"
    async with AsyncSession(engine) as s:
        assert await s.scalar(select(func.count()).select_from(Asset)) == 0
    assert not (tmp_path / "assets").exists()


async def test_disabled_uploads_still_serve_existing_assets(
    client: httpx.AsyncClient, box: Box
) -> None:
    created = (await upload(client, images.png())).json()
    box.change(uploads_enabled=False)

    assert (await client.get(created["url"])).status_code == 200


@pytest.mark.parametrize(
    "form",
    [
        {"data": {"kind": "bubble"}},
        {"files": {"file": ("a.png", b"x", "image/png")}},
        {"data": {"kind": "avatar"}, "files": {"file": ("a.png", b"x", "image/png")}},
        {"data": {"file": "text, not a file"}},
    ],
)
async def test_a_malformed_form_is_422(
    client: httpx.AsyncClient, form: dict[str, Any]
) -> None:
    r = await client.post("/assets", **form)

    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "invalid_upload"


async def test_extra_form_fields_are_refused(client: httpx.AsyncClient) -> None:
    r = await client.post(
        "/assets",
        data={"kind": "bubble", "extra": "1"},
        files={"file": ("a.png", images.png(), "image/png")},
    )

    assert r.status_code == 400


async def test_a_non_multipart_request_is_422(client: httpx.AsyncClient) -> None:
    r = await client.post("/assets", json={"kind": "bubble"})

    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "invalid_upload"


# --- serving ----------------------------------------------------------------


async def test_served_assets_carry_the_security_and_cache_headers(
    client: httpx.AsyncClient,
) -> None:
    created = (
        await upload(client, images.jpeg((30, 30)), content_type="image/png")
    ).json()

    r = await client.get(created["url"])

    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"  # the stored type, not the claim
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["content-security-policy"] == "default-src 'none'; sandbox"
    assert r.headers["etag"].startswith('"') and len(r.headers["etag"]) == 66
    assert Image.open(io.BytesIO(r.content)).size == (30, 30)


async def test_thumbnails_carry_the_same_headers_and_their_own_etag(
    client: httpx.AsyncClient,
) -> None:
    created = (await upload(client, images.png((900, 500)), "background")).json()

    full = await client.get(created["url"])
    thumb = await client.get(created["thumbUrl"])

    assert thumb.headers["x-content-type-options"] == "nosniff"
    assert thumb.headers["cache-control"] == full.headers["cache-control"]
    assert thumb.headers["content-security-policy"] == "default-src 'none'; sandbox"
    assert thumb.headers["etag"] != full.headers["etag"]


async def test_a_matching_etag_gets_304_without_a_body(
    client: httpx.AsyncClient,
) -> None:
    created = (await upload(client, images.png())).json()
    etag = (await client.get(created["url"])).headers["etag"]

    same = await client.get(created["url"], headers={"If-None-Match": etag})
    weak = await client.get(created["url"], headers={"If-None-Match": f"W/{etag}"})
    other = await client.get(created["url"], headers={"If-None-Match": '"nope"'})

    assert same.status_code == 304 and same.content == b""
    assert same.headers["etag"] == etag
    assert weak.status_code == 304
    assert other.status_code == 200


@pytest.mark.parametrize(
    "asset_id",
    [
        "..%2F..%2F..%2Fetc%2Fpasswd",
        "%2e%2e%2f%2e%2e%2fsecret",
        "..%5C..%5Cwindows%5Cwin.ini",
        "....//....//etc/passwd",
        "not-a-uuid",
        "0198a3b0-0000-7000-8000-000000000000",  # well-formed, unknown
        "00000000000000000000000000000000",  # a UUID without hyphens
        "{0198a3b0-0000-7000-8000-000000000000}",
        "%00",
    ],
)
@pytest.mark.parametrize("suffix", ["", "/thumb"])
async def test_ids_that_are_not_assets_are_404(
    client: httpx.AsyncClient, asset_id: str, suffix: str
) -> None:
    r = await client.get(f"/assets/{asset_id}{suffix}")

    assert r.status_code == 404


async def test_a_file_missing_from_storage_is_404_not_a_crash(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    created = (await upload(client, images.png())).json()
    for stored in (tmp_path / "assets").rglob("*"):
        if stored.is_file():
            stored.unlink()

    r = await client.get(created["url"])

    assert r.status_code == 404
