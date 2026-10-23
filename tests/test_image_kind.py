"""The `image` asset kind: pictures for content bubbles, with a thumbnail."""

import io

import httpx
from PIL import Image
from pydantic import SecretStr

from app.assets.processing import THUMBNAIL_MAX_WIDTH, ImageLimits, process_image
from app.core.config import Settings
from app.enums import AssetKind
from app.services.asset_service import AssetSettings
from tests import images
from tests.asset_helpers import upload
from tests.conftest import Box


def test_the_defaults_are_2048_pixels_and_4_mb() -> None:
    settings = Settings(
        _env_file=None,
        postgres_user="u",
        postgres_password=SecretStr("p"),
        postgres_db="d",
        moodle_base_url="https://m.example",
        moodle_service_token=SecretStr("t"),
    )

    assert settings.assets_max_image_side == 2048
    assert settings.assets_max_image_bytes == 4 * 1024 * 1024
    policy = AssetSettings.from_app_settings(settings)
    assert policy.max_bytes_for(AssetKind.IMAGE) == 4 * 1024 * 1024
    assert policy.max_image_side == 2048


def test_an_image_gets_a_thumbnail_like_a_background() -> None:
    limits = ImageLimits(background_side=2000, bubble_side=100, image_side=2000)

    processed = process_image(
        io.BytesIO(images.png((1500, 900))), AssetKind.IMAGE, limits
    )

    assert processed.thumbnail is not None
    thumb = Image.open(io.BytesIO(processed.thumbnail))
    assert thumb.format == "WEBP" and thumb.width == THUMBNAIL_MAX_WIDTH


def test_a_bubble_image_still_has_no_thumbnail() -> None:
    limits = ImageLimits(background_side=2000, bubble_side=100)

    processed = process_image(io.BytesIO(images.png()), AssetKind.BUBBLE, limits)

    assert processed.thumbnail is None


async def test_upload_an_image_returns_the_asset_with_its_thumb_url(
    client: httpx.AsyncClient,
) -> None:
    r = await upload(client, images.png((1600, 900)), "image")

    assert r.status_code == 201
    body = r.json()
    assert body["kind"] == "image" and (body["width"], body["height"]) == (1600, 900)
    assert body["thumbUrl"] == f"/api/v1/assets/{body['id']}/thumb"
    thumb = await client.get(body["thumbUrl"])
    assert thumb.status_code == 200 and thumb.headers["content-type"] == "image/webp"


async def test_an_image_longer_than_the_side_limit_is_refused(
    client: httpx.AsyncClient, box: Box
) -> None:
    box.change(max_image_side=300)

    ok = await upload(client, images.png((300, 200)), "image")
    too_wide = await upload(client, images.png((301, 10)), "image")

    assert ok.status_code == 201
    assert too_wide.status_code == 422
    detail = too_wide.json()["detail"]
    assert detail["code"] == "asset_dimensions_too_large" and detail["maxSide"] == 300


async def test_an_image_over_the_byte_limit_is_refused(
    client: httpx.AsyncClient, box: Box
) -> None:
    box.change(max_image_bytes=1_000)

    noisy = images.encode(Image.effect_noise((200, 200), 90), "PNG")  # incompressible
    assert len(noisy) > 1_000

    r = await upload(client, noisy, "image")

    assert r.status_code == 413
    assert r.json()["detail"] == {
        "code": "asset_too_large",
        "message": "The file is too large",
        "limitBytes": 1_000,
    }


async def test_the_same_bytes_as_background_and_as_image_are_two_assets(
    client: httpx.AsyncClient,
) -> None:
    data = images.png((40, 30))

    as_image = await upload(client, data, "image")
    again = await upload(client, data, "image")
    as_background = await upload(client, data, "background")

    assert (as_image.status_code, again.status_code) == (201, 200)
    assert again.json()["id"] == as_image.json()["id"]
    assert as_background.status_code == 201
    assert as_background.json()["id"] != as_image.json()["id"]


async def test_the_list_can_be_filtered_by_the_image_kind(
    client: httpx.AsyncClient,
) -> None:
    await upload(client, images.png((40, 30)), "image")
    await upload(client, images.png((41, 30)), "background")

    listed = (await client.get("/assets", params={"kind": "image"})).json()

    assert listed["total"] == 1 and listed["items"][0]["kind"] == "image"


async def test_the_upload_error_names_the_image_kind(
    client: httpx.AsyncClient,
) -> None:
    r = await upload(client, images.png(), "video")

    assert r.status_code == 422
    assert "'image'" in r.json()["detail"]["message"]
