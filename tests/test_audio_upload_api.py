"""`POST /assets` with `kind=audio`: rights, credit, parsing, limits, the
switch, and who owns the result."""

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.enums import Role
from app.models import Asset
from app.tenancy.scope import SYSTEM
from tests import audio_files as af
from tests import images
from tests.asset_helpers import png_asset, upload, upload_audio
from tests.conftest import Box
from tests.editor_client import EditorClient
from tests.sessions import new_session
from tests.tenant_helpers import insert_tenant, insert_token

MakeClient = Callable[[str | None], EditorClient]


async def stored_rows(engine: AsyncEngine) -> list[Asset]:
    """Every asset, whichever tenant it belongs to."""
    async with new_session(engine, SYSTEM) as s:
        return list((await s.scalars(select(Asset))).all())


async def count(engine: AsyncEngine) -> int:
    async with new_session(engine, SYSTEM) as s:
        return int(await s.scalar(select(func.count()).select_from(Asset)) or 0)


# --- success -------------------------------------------------------------------


async def test_an_mp3_upload_returns_the_contract_shape(
    client: httpx.AsyncClient,
) -> None:
    r = await upload_audio(client, af.mp3(40), license="CC BY 4.0")

    assert r.status_code == 201, r.text
    body = r.json()
    assert set(body) == {
        "id",
        "kind",
        "mime",
        "width",
        "height",
        "durationSeconds",
        "bytes",
        "url",
        "thumbUrl",
        "title",
        "credit",
        "license",
        "originalFilename",
        "createdAt",
    }
    assert (body["kind"], body["mime"]) == ("audio", "audio/mpeg")
    assert body["durationSeconds"] == pytest.approx(40 * af.FRAME_44K_SECONDS, abs=1e-3)
    assert (body["title"], body["credit"], body["license"]) == (
        "Brisa",
        "Ana Perez, CC BY 4.0",
        "CC BY 4.0",
    )
    assert (body["width"], body["height"], body["thumbUrl"]) == (None, None, None)
    assert body["url"] == f"/api/v1/assets/{body['id']}"
    assert body["bytes"] == len(af.mp3(40))


async def test_an_m4a_upload_is_accepted(client: httpx.AsyncClient) -> None:
    r = await upload_audio(
        client, af.m4a(), filename="song.m4a", content_type="audio/mp4"
    )

    assert r.status_code == 201, r.text
    assert r.json()["mime"] == "audio/mp4"
    assert r.json()["durationSeconds"] == pytest.approx(af.M4A_SECONDS, abs=1e-3)


async def test_the_stored_file_is_the_cleaned_audio_not_the_upload(
    client: httpx.AsyncClient, storage: object
) -> None:
    dirty = af.tagged_mp3(40)
    created = (await upload_audio(client, dirty)).json()

    served = await client.get(created["url"])

    assert served.content == af.mp3(40)
    for secret in af.ALL_SECRETS:
        assert secret not in served.content
    assert created["bytes"] == len(served.content)


async def test_nothing_about_the_uploaded_file_name_is_kept(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    r = await upload_audio(client, af.mp3(40), filename="grabacion_juan_perez.mp3")

    assert r.json()["originalFilename"] is None
    [row] = await stored_rows(engine)
    assert row.original_filename is None


async def test_identical_audio_is_stored_once_whatever_tags_it_came_with(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    first = await upload_audio(client, af.mp3(40), title="Primera")
    second = await upload_audio(client, af.tagged_mp3(40), title="Segunda")

    assert first.status_code == 201 and second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["title"] == "Primera"  # left as it was
    assert await count(engine) == 1


# --- who uploaded it, and the rights trail -------------------------------------


async def test_the_pseudonymous_uploader_and_the_declaration_are_recorded(
    engine: AsyncEngine, make_client: MakeClient, box: Box, storage: object
) -> None:
    tenant = await insert_tenant(engine, "alpha")
    editor = make_client(await insert_token(engine, tenant, 7, Role.EDITOR))

    r = await upload_audio(editor, af.mp3(40), license="CC BY 4.0")

    assert r.status_code == 201, r.text
    [row] = await stored_rows(engine)
    assert row.tenant_id == tenant
    assert row.uploaded_by_sub == "b" * 32  # the token's pseudonymous id
    assert row.rights_declared_at is not None
    assert row.license == "CC BY 4.0"
    assert row.quarantined_at is None


async def test_the_legacy_anonymous_editor_has_no_uploader_to_record(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    await upload_audio(client, af.mp3(40))

    [row] = await stored_rows(engine)
    assert row.uploaded_by_sub is None and row.rights_declared_at is not None


@pytest.mark.parametrize("declared", [None, "", "false", "True", "yes", "1", " true"])
async def test_the_rights_declaration_is_required_and_must_be_true(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    declared: str | None,
    tmp_path: Path,
) -> None:
    r = await upload_audio(client, af.mp3(40), rightsDeclared=declared)

    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "audio_rights_declaration_required"
    assert await count(engine) == 0
    assert not (tmp_path / "assets").exists()


@pytest.mark.parametrize("credit", [None, "", "   ", "\t\n"])
async def test_a_credit_is_required(
    client: httpx.AsyncClient, engine: AsyncEngine, credit: str | None
) -> None:
    r = await upload_audio(client, af.mp3(40), credit=credit)

    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "audio_credit_required"
    assert await count(engine) == 0


@pytest.mark.parametrize(
    "fields",
    [
        {"title": None},
        {"title": "  "},
        {"title": "x" * 121},
        {"credit": "x" * 301},
        {"license": "x" * 121},
    ],
)
async def test_title_credit_and_license_are_bounded(
    client: httpx.AsyncClient, fields: dict[str, str | None]
) -> None:
    r = await upload_audio(client, af.mp3(40), **fields)  # type: ignore[arg-type]

    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "invalid_asset_metadata"


async def test_the_license_is_optional(client: httpx.AsyncClient) -> None:
    r = await upload_audio(client, af.mp3(40))

    assert r.status_code == 201 and r.json()["license"] is None


# --- validation of the file ----------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        images.png(),
        b"not audio at all" * 100,
        af.mp3(40)[:-3000] + bytes(range(256)) * 20 + af.mp3(20),
        af.m4a(drm=True),
        af.m4a(skew=100000),
        af.m4a()[:-300],
    ],
    ids=["png", "text", "broken-mp3", "drm", "bad-offsets", "truncated"],
)
async def test_a_file_that_is_not_valid_audio_is_refused(
    client: httpx.AsyncClient, engine: AsyncEngine, data: bytes
) -> None:
    r = await upload_audio(client, data)

    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["code"] == "audio_invalid"
    assert set(detail) == {"code", "message"}  # the parser's reason stays inside
    assert await count(engine) == 0


async def test_the_claimed_type_and_extension_decide_nothing(
    client: httpx.AsyncClient,
) -> None:
    refused = await upload_audio(
        client, images.png(), filename="song.mp3", content_type="audio/mpeg"
    )
    accepted = await upload_audio(
        client, af.mp3(40), filename="photo.png", content_type="image/png"
    )

    assert refused.status_code == 422
    assert accepted.status_code == 201 and accepted.json()["mime"] == "audio/mpeg"


async def test_an_empty_file_is_refused(client: httpx.AsyncClient) -> None:
    r = await upload_audio(client, b"")

    assert r.status_code == 422 and r.json()["detail"]["code"] == "audio_invalid"


async def test_audio_over_ten_minutes_is_too_long(
    client: httpx.AsyncClient, box: Box
) -> None:
    box.change(max_audio_bytes=5_000_000)
    frames = int(600 / af.FRAME_8K_SECONDS) + 2

    r = await upload_audio(client, af.mp3(frames, af.FRAME_8K))

    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "audio_too_long"
    assert r.json()["detail"]["maxSeconds"] == 600


async def test_audio_of_exactly_ten_minutes_is_accepted(
    client: httpx.AsyncClient, box: Box
) -> None:
    box.change(max_audio_bytes=5_000_000)
    frames = int(600 / af.FRAME_8K_SECONDS)  # 8333 frames: 599.976 s

    r = await upload_audio(client, af.mp3(frames, af.FRAME_8K))

    assert r.status_code == 201, r.text


async def test_audio_over_the_size_limit_answers_413(
    client: httpx.AsyncClient, engine: AsyncEngine, box: Box
) -> None:
    box.change(max_audio_bytes=10_000)

    r = await upload_audio(client, af.mp3(40))  # 16 680 bytes

    assert r.status_code == 413
    assert r.json()["detail"] == {
        "code": "asset_too_large",
        "message": "The file is too large",
        "limitBytes": 10_000,
    }
    assert await count(engine) == 0


async def test_the_default_audio_limit_is_eight_megabytes() -> None:
    from app.core.config import Settings
    from app.services.asset_service import AssetSettings

    settings = Settings.model_validate(
        {
            "postgres_user": "u",
            "postgres_password": "p",
            "postgres_db": "d",
            "moodle_base_url": "https://m.example",
            "moodle_service_token": "t",
        }
    )

    assert AssetSettings.from_app_settings(settings).max_audio_bytes == 8 * 1024**2
    assert AssetSettings.from_app_settings(settings).max_audio_seconds == 600


async def test_audio_counts_against_the_tenants_quota(
    engine: AsyncEngine, make_client: MakeClient, box: Box, storage: object
) -> None:
    tenant = await insert_tenant(engine, "small", quota=len(af.mp3(40)) + 100)
    editor = make_client(await insert_token(engine, tenant, 7, Role.EDITOR))

    first = await upload_audio(editor, af.mp3(40))
    second = await upload_audio(editor, af.mp3(41))

    assert first.status_code == 201
    assert second.status_code == 507
    assert second.json()["detail"]["code"] == "asset_quota_exceeded"


# --- the switch ----------------------------------------------------------------


async def test_disabled_audio_uploads_answer_503_and_store_nothing(
    client: httpx.AsyncClient, engine: AsyncEngine, box: Box, tmp_path: Path
) -> None:
    box.change(audio_uploads_enabled=False)

    r = await upload_audio(client, af.mp3(40))

    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "audio_uploads_disabled"
    assert await count(engine) == 0
    assert not (tmp_path / "assets").exists()


async def test_the_switch_blocks_only_uploading_audio(
    client: httpx.AsyncClient, box: Box
) -> None:
    created = (await upload_audio(client, af.mp3(40))).json()
    box.change(audio_uploads_enabled=False)

    played = await client.get(created["url"])
    image = await upload(client, images.png((60, 60)))
    listed = await client.get("/assets?kind=audio")

    assert played.status_code == 200 and played.content == af.mp3(40)
    assert image.status_code == 201  # images have their own switch
    assert [i["id"] for i in listed.json()["items"]] == [created["id"]]


async def test_the_image_switch_does_not_govern_audio(
    client: httpx.AsyncClient, box: Box
) -> None:
    box.change(uploads_enabled=False)

    audio = await upload_audio(client, af.mp3(40))
    image = await upload(client, images.png((60, 60)))

    assert audio.status_code == 201
    assert image.status_code == 503
    assert image.json()["detail"]["code"] == "uploads_disabled"


async def test_with_both_switches_off_nothing_is_read(
    client: httpx.AsyncClient, box: Box
) -> None:
    box.change(uploads_enabled=False, audio_uploads_enabled=False)

    assert (await upload_audio(client, af.mp3(40))).status_code == 503
    assert (await upload(client, images.png())).status_code == 503


# --- who may upload, and whose it is -------------------------------------------


async def test_students_cannot_upload_audio(
    engine: AsyncEngine, make_client: MakeClient, storage: object
) -> None:
    tenant = await insert_tenant(engine, "alpha")
    student = make_client(await insert_token(engine, tenant, 7, Role.STUDENT))

    r = await upload_audio(student, af.mp3(40))

    assert r.status_code == 403
    assert await count(engine) == 0


async def test_an_upload_is_the_uploading_tenants_alone(
    engine: AsyncEngine, make_client: MakeClient, storage: object
) -> None:
    alpha = make_client(
        await insert_token(engine, await insert_tenant(engine, "alpha"), 7, Role.EDITOR)
    )
    beta = make_client(
        await insert_token(engine, await insert_tenant(engine, "beta"), 7, Role.EDITOR)
    )
    created = (await upload_audio(alpha, af.mp3(40))).json()

    async def listed(client: EditorClient, scope: str) -> httpx.Response:
        return await client.get(f"/assets?kind=audio&scope={scope}")

    mine = (await listed(alpha, "tenant")).json()["items"]
    assert [i["id"] for i in mine] == [created["id"]]
    for client in (alpha, beta):
        assert (await listed(client, "library")).json()["items"] == []
    assert (await listed(beta, "tenant")).json()["items"] == []
    assert (
        await beta.patch(f"/assets/{created['id']}", json={"title": "x"})
    ).status_code == 404
    assert (await beta.delete(f"/assets/{created['id']}")).status_code == 404
    # The same bytes from the other tenant are a separate asset of its own.
    twin = await upload_audio(beta, af.mp3(40))
    assert twin.status_code == 201 and twin.json()["id"] != created["id"]


async def test_an_uploaded_track_can_be_retitled_but_never_uncredited(
    client: httpx.AsyncClient,
) -> None:
    created = (await upload_audio(client, af.mp3(40))).json()

    renamed = await client.patch(f"/assets/{created['id']}", json={"title": "Otra"})
    cleared = await client.patch(f"/assets/{created['id']}", json={"credit": None})
    blank = await client.patch(f"/assets/{created['id']}", json={"credit": ""})

    assert renamed.status_code == 200 and renamed.json()["title"] == "Otra"
    for r in (cleared, blank):
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "audio_credit_required"
    assert (await client.get("/assets?kind=audio")).json()["items"][0][
        "credit"
    ] == "Ana Perez, CC BY 4.0"


async def test_an_unused_uploaded_track_can_be_deleted_with_its_file(
    client: httpx.AsyncClient, engine: AsyncEngine, tmp_path: Path
) -> None:
    created = (await upload_audio(client, af.mp3(40))).json()

    assert (await client.delete(f"/assets/{created['id']}")).status_code == 204

    assert await count(engine) == 0
    assert (await client.get(created["url"])).status_code == 404
    assert [p for p in (tmp_path / "assets").rglob("*") if p.is_file()] == []


async def test_images_still_ignore_the_audio_fields(
    client: httpx.AsyncClient,
) -> None:
    image = await png_asset(client, 1, "background", license="whatever")

    assert image["license"] is None
