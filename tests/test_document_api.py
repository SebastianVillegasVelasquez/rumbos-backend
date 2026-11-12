"""`POST /assets` with `kind=document`: rights, title, inspection, limits, the
switch, serving, and who owns the result."""

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.enums import Role
from app.models import Asset
from app.tenancy.scope import SYSTEM
from tests import images
from tests.asset_helpers import png_asset, quarantine, upload_document
from tests.conftest import Box
from tests.editor_client import EditorClient
from tests.pdf_files import build_pdf, build_pdf_with_object_stream, encrypted
from tests.sessions import new_session
from tests.tenant_helpers import insert_tenant, insert_token

MakeClient = Callable[[str | None], EditorClient]

PDF = build_pdf(pages=3)


async def stored_rows(engine: AsyncEngine) -> list[Asset]:
    async with new_session(engine, SYSTEM) as s:
        return list((await s.scalars(select(Asset))).all())


async def count(engine: AsyncEngine) -> int:
    async with new_session(engine, SYSTEM) as s:
        return int(await s.scalar(select(func.count()).select_from(Asset)) or 0)


# --- success -------------------------------------------------------------------------


async def test_a_pdf_upload_returns_the_contract_shape(
    client: httpx.AsyncClient,
) -> None:
    r = await upload_document(client, PDF)

    assert r.status_code == 201, r.text
    body = r.json()
    assert set(body) == {
        "id",
        "kind",
        "mime",
        "width",
        "height",
        "durationSeconds",
        "pageCount",
        "bytes",
        "url",
        "thumbUrl",
        "title",
        "credit",
        "license",
        "originalFilename",
        "createdAt",
        "coverAssetId",
        "coverUrl",
        "warnings",
    }
    assert (body["kind"], body["mime"]) == ("document", "application/pdf")
    assert (body["title"], body["pageCount"], body["bytes"]) == (
        "Guia del curso",
        3,
        len(PDF),
    )
    assert (body["width"], body["height"], body["thumbUrl"]) == (None, None, None)
    assert body["url"] == f"/api/v1/assets/{body['id']}"
    assert body["warnings"] == []


async def test_a_pdf_with_object_streams_is_accepted(client: httpx.AsyncClient) -> None:
    r = await upload_document(
        client, build_pdf_with_object_stream("<< /Type /Metadata >>")
    )

    assert r.status_code == 201, r.text
    assert r.json()["pageCount"] == 1


async def test_the_pdf_is_stored_exactly_as_uploaded(
    client: httpx.AsyncClient,
) -> None:
    with_metadata = build_pdf(
        extra=("<< /Author (Juan Perez) /Producer (Writer) >>",),
        trailer="/Info 5 0 R",
    )
    created = (await upload_document(client, with_metadata)).json()

    served = await client.get(created["url"])

    # Not rewritten, and the metadata it carries is not stripped.
    assert served.content == with_metadata
    assert b"Juan Perez" in served.content
    assert created["bytes"] == len(with_metadata)


async def test_nothing_about_the_uploaded_file_name_is_kept(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    r = await upload_document(client, PDF, filename="expediente_juan_perez_123.pdf")

    assert r.json()["originalFilename"] is None
    [row] = await stored_rows(engine)
    assert row.original_filename is None
    listed = (await client.get("/assets", params={"kind": "document"})).json()
    assert "juan_perez" not in str(listed)


async def test_identical_documents_are_stored_once(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    first = await upload_document(client, PDF, title="Primera")
    second = await upload_document(client, PDF, title="Segunda")

    assert first.status_code == 201 and second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["title"] == "Primera"  # left as it was
    assert await count(engine) == 1


async def test_the_same_bytes_as_another_kind_are_a_different_asset(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    await png_asset(client, 1)
    await upload_document(client, PDF)

    assert await count(engine) == 2


# --- serving ---------------------------------------------------------------------------


async def test_it_is_served_inline_as_a_pdf_with_the_safe_headers(
    client: httpx.AsyncClient,
) -> None:
    created = (await upload_document(client, PDF)).json()

    r = await client.get(created["url"])

    assert r.status_code == 200 and r.content == PDF
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert r.headers["content-disposition"] == "inline"
    assert r.headers["accept-ranges"] == "bytes"
    assert r.headers["etag"].startswith('"')
    # The uploader's file name never reaches the client.
    assert "guia" not in str(r.headers).lower().replace("guia del curso", "")


async def test_ranges_work_on_a_document(client: httpx.AsyncClient) -> None:
    created = (await upload_document(client, PDF)).json()

    r = await client.get(created["url"], headers={"Range": "bytes=0-4"})

    assert r.status_code == 206
    assert r.content == b"%PDF-"
    assert r.headers["content-range"] == f"bytes 0-4/{len(PDF)}"
    assert r.headers["content-type"] == "application/pdf"


async def test_a_conditional_request_gets_304(client: httpx.AsyncClient) -> None:
    created = (await upload_document(client, PDF)).json()
    etag = (await client.get(created["url"])).headers["etag"]

    r = await client.get(created["url"], headers={"If-None-Match": etag})

    assert r.status_code == 304


async def test_a_document_has_no_thumbnail(client: httpx.AsyncClient) -> None:
    created = (await upload_document(client, PDF)).json()

    assert (await client.get(f"{created['url']}/thumb")).status_code == 404


async def test_head_answers_like_get_without_a_body(client: httpx.AsyncClient) -> None:
    created = (await upload_document(client, PDF)).json()

    r = await client.head(created["url"])

    assert r.status_code == 200 and r.content == b""
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["content-length"] == str(len(PDF))


# --- rights and title --------------------------------------------------------------------


@pytest.mark.parametrize("declared", [None, "", "false", "True", "yes", "1", " true"])
async def test_the_rights_declaration_is_required_and_must_be_true(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    declared: str | None,
    tmp_path: Path,
) -> None:
    r = await upload_document(client, PDF, rightsDeclared=declared)

    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "document_rights_declaration_required"
    assert await count(engine) == 0
    assert not (tmp_path / "assets").exists()


async def test_the_declaration_and_the_uploader_are_recorded(
    engine: AsyncEngine, make_client: MakeClient, box: Box, storage: object
) -> None:
    tenant = await insert_tenant(engine, "alpha")
    editor = make_client(await insert_token(engine, tenant, 7, Role.EDITOR))

    r = await upload_document(editor, PDF)

    assert r.status_code == 201, r.text
    [row] = await stored_rows(engine)
    assert row.tenant_id == tenant
    assert row.uploaded_by_sub == "b" * 32
    assert row.rights_declared_at is not None
    assert row.quarantined_at is None
    assert (row.kind.value, row.page_count) == ("document", 3)


@pytest.mark.parametrize("title", [None, "", "   ", "\t\n"])
async def test_a_title_is_required(
    client: httpx.AsyncClient, engine: AsyncEngine, title: str | None
) -> None:
    r = await upload_document(client, PDF, title=title)

    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "invalid_asset_metadata"
    assert await count(engine) == 0


async def test_a_title_may_have_200_characters_not_201(
    client: httpx.AsyncClient,
) -> None:
    ok = await upload_document(client, PDF, title="t" * 200)
    too_long = await upload_document(client, build_pdf(pages=2), title="t" * 201)

    assert ok.status_code == 201 and len(ok.json()["title"]) == 200
    assert too_long.status_code == 422
    assert too_long.json()["detail"]["code"] == "invalid_asset_metadata"


async def test_a_title_is_cleaned_of_extra_whitespace(
    client: httpx.AsyncClient,
) -> None:
    r = await upload_document(client, PDF, title="  Guia \n  del   curso ")

    assert r.json()["title"] == "Guia del curso"


async def test_credit_and_license_are_for_audio_only(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    for field in ("credit", "license"):
        r = await upload_document(client, PDF, **{field: "x"})
        assert r.status_code == 422, r.text
    assert await count(engine) == 0


# --- what is refused ----------------------------------------------------------------------

REFUSED: dict[str, tuple[bytes, str]] = {
    "png": (images.png(), "not_pdf"),
    "html": (b"<html><script>alert(1)</script></html>", "not_pdf"),
    "empty": (b"", "not_pdf"),
    "junk first": (b"x" + PDF, "not_pdf"),
    "encrypted": (encrypted(PDF), "encrypted"),
    "javascript": (
        build_pdf(catalog="/OpenAction << /S /JavaScript /JS (app.alert\\(1\\)) >>"),
        "active_content",
    ),
    "obfuscated javascript": (
        build_pdf(catalog="/OpenAction << /S /J#61vaScript /JS (x) >>"),
        "active_content",
    ),
    "launch": (
        build_pdf(catalog="/OpenAction << /S /Launch /F (calc.exe) >>"),
        "active_content",
    ),
    "embedded file": (
        build_pdf(catalog="/Names << /EmbeddedFiles << /Names [] >> >>"),
        "active_content",
    ),
    "too many pages": (build_pdf(pages=201), "too_many_pages"),
    "huge page count": (build_pdf(pages=1, count=10**9), "too_many_pages"),
    "truncated": (PDF[: len(PDF) // 2], "corrupt"),
    "garbage": (b"%PDF-1.7\n" + b"\x00" * 300, "corrupt"),
}


@pytest.mark.parametrize(("data", "reason"), REFUSED.values(), ids=list(REFUSED))
async def test_a_file_that_is_not_a_usable_pdf_is_refused_and_not_stored(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    tmp_path: Path,
    data: bytes,
    reason: str,
) -> None:
    r = await upload_document(client, data)

    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "document_invalid" and detail["reason"] == reason
    assert await count(engine) == 0
    assert not list((tmp_path / "assets").rglob("*")) or not any(
        p.is_file() for p in (tmp_path / "assets").rglob("*")
    )


async def test_the_refusal_says_what_to_fix_and_nothing_of_the_file(
    client: httpx.AsyncClient,
) -> None:
    secret = b"SECRET-CASE-NUMBER-0042"
    data = build_pdf(catalog="/OpenAction << /S /JavaScript /JS (x) >>") + secret

    r = await upload_document(client, data, filename="secret_name.pdf")

    assert "SECRET-CASE" not in r.text and "secret_name" not in r.text
    assert set(r.json()["detail"]) == {"code", "message", "reason"}


async def test_the_declared_type_and_extension_decide_nothing(
    client: httpx.AsyncClient,
) -> None:
    as_png = await upload_document(
        client, PDF, filename="x.png", content_type="image/png"
    )
    as_pdf = await upload_document(
        client, images.png(), filename="x.pdf", content_type="application/pdf"
    )

    assert as_png.status_code == 201
    assert as_pdf.status_code == 422 and as_pdf.json()["detail"]["reason"] == "not_pdf"


async def test_the_inspection_runs_under_a_timeout(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    box: Box,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def slow(*args: Any, **kwargs: Any) -> object:
        time.sleep(0.5)
        raise AssertionError("must not be waited for")

    monkeypatch.setattr("app.services.asset_service.inspect_pdf", slow)
    box.change(document_scan_timeout_seconds=0.05)

    started = time.monotonic()
    r = await upload_document(client, PDF)

    assert time.monotonic() - started < 0.4
    assert r.status_code == 422 and r.json()["detail"]["reason"] == "corrupt"
    assert await count(engine) == 0


# --- limits and the switch -------------------------------------------------------------------


async def test_a_file_over_the_limit_is_413_and_the_limit_is_in_bytes(
    client: httpx.AsyncClient, box: Box, engine: AsyncEngine
) -> None:
    box.change(max_document_bytes=len(PDF) - 1)

    r = await upload_document(client, PDF)

    assert r.status_code == 413
    detail = r.json()["detail"]
    assert detail["code"] == "asset_too_large"
    assert detail["limitBytes"] == len(PDF) - 1
    assert await count(engine) == 0


async def test_a_file_at_the_limit_is_accepted(
    client: httpx.AsyncClient, box: Box
) -> None:
    box.change(max_document_bytes=len(PDF))

    assert (await upload_document(client, PDF)).status_code == 201


async def test_the_default_limit_is_ten_megabytes() -> None:
    from app.core.config import Settings
    from app.services.asset_service import AssetSettings

    settings = Settings.model_validate(
        {
            "postgres_user": "u",
            "postgres_password": "p",
            "postgres_db": "d",
            "postgres_host": "h",
            "moodle_base_url": "https://m",
            "moodle_service_token": "t",
        }
    )

    assert settings.document_max_bytes == 10 * 1024 * 1024
    assert AssetSettings.from_app_settings(settings).max_document_bytes == 10 * 1024**2


async def test_documents_count_against_the_tenants_quota(
    engine: AsyncEngine, make_client: MakeClient, box: Box, storage: object
) -> None:
    tenant = await insert_tenant(engine, "small", quota=len(PDF) + 10)
    editor = make_client(await insert_token(engine, tenant, 7, Role.EDITOR))

    first = await upload_document(editor, PDF)
    second = await upload_document(editor, build_pdf(pages=2))

    assert first.status_code == 201
    assert second.status_code == 507
    assert second.json()["detail"]["code"] == "asset_quota_exceeded"
    assert await count(engine) == 1


async def test_documents_follow_the_uploads_switch_not_the_audio_one(
    client: httpx.AsyncClient, box: Box
) -> None:
    box.change(audio_uploads_enabled=False)
    assert (await upload_document(client, PDF)).status_code == 201

    box.change(uploads_enabled=False)
    r = await upload_document(client, build_pdf(pages=2))

    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "uploads_disabled"


async def test_a_document_cannot_ride_on_another_kinds_endpoint_fields(
    client: httpx.AsyncClient,
) -> None:
    r = await client.post(
        "/assets",
        data={"kind": "document", "title": "x", "rightsDeclared": "true"},
        files={
            "file": ("a.pdf", PDF, "application/pdf"),
            "cover": ("c.png", images.png(), "image/png"),
        },
    )

    assert r.status_code == 422, r.text


# --- listing, editing, deleting -----------------------------------------------------------------


async def test_documents_are_listed_by_kind_with_usage_and_availability(
    client: httpx.AsyncClient,
) -> None:
    created = (await upload_document(client, PDF)).json()
    await png_asset(client, 1)

    r = await client.get("/assets", params={"kind": "document", "scope": "tenant"})

    [item] = r.json()["items"]
    assert item["id"] == created["id"] and item["pageCount"] == 3
    assert item["available"] is True
    assert item["usage"] == {"maps": 0, "skins": 0, "publications": 0, "tracks": 0}
    assert r.json()["total"] == 1


async def test_the_library_scope_never_holds_documents(
    client: httpx.AsyncClient,
) -> None:
    await upload_document(client, PDF)

    r = await client.get("/assets", params={"kind": "document", "scope": "library"})

    assert r.json()["items"] == []


async def test_a_missing_file_makes_the_document_unavailable(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    created = (await upload_document(client, PDF)).json()
    for stored in (tmp_path / "assets").rglob(created["id"]):
        stored.unlink()

    r = await client.get("/assets", params={"kind": "document"})

    assert r.json()["items"][0]["available"] is False


async def test_the_title_of_a_document_can_change_up_to_200_characters(
    client: httpx.AsyncClient,
) -> None:
    created = (await upload_document(client, PDF)).json()

    ok = await client.patch(f"/assets/{created['id']}", json={"title": "n" * 200})
    long = await client.patch(f"/assets/{created['id']}", json={"title": "n" * 201})

    assert ok.status_code == 200 and len(ok.json()["title"]) == 200
    assert long.status_code == 422


async def test_other_kinds_keep_their_120_character_title_limit(
    client: httpx.AsyncClient,
) -> None:
    image = await png_asset(client, 1)

    ok = await client.patch(f"/assets/{image['id']}", json={"title": "n" * 120})
    long = await client.patch(f"/assets/{image['id']}", json={"title": "n" * 121})

    assert ok.status_code == 200
    assert long.status_code == 422
    assert long.json()["detail"]["code"] == "invalid_asset_metadata"


async def test_an_unused_document_can_be_deleted_with_its_file(
    client: httpx.AsyncClient, engine: AsyncEngine, tmp_path: Path
) -> None:
    created = (await upload_document(client, PDF)).json()

    r = await client.delete(f"/assets/{created['id']}")

    assert r.status_code == 204
    assert await count(engine) == 0
    assert (await client.get(created["url"])).status_code == 404
    assert not any(p.is_file() for p in (tmp_path / "assets").rglob("*"))


async def test_a_document_is_not_a_background_a_content_image_or_music(
    client: httpx.AsyncClient,
) -> None:
    created = (await upload_document(client, PDF)).json()
    made = await client.post(
        "/course-maps",
        json={"courseId": 7, "sectionId": 1, "title": "M", "imageUrl": created["url"]},
    )
    assert made.status_code in (404, 422), made.text


# --- taking it down ------------------------------------------------------------------------------


async def test_a_quarantined_document_is_not_served_listed_or_brought_back(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    created = (await upload_document(client, PDF)).json()
    await quarantine(engine, created["id"])

    assert (await client.get(created["url"])).status_code == 404
    listed = await client.get("/assets", params={"kind": "document"})
    assert listed.json()["items"] == []
    again = await upload_document(client, PDF)
    assert again.status_code == 422
    assert again.json()["detail"]["code"] == "document_quarantined"
    assert await count(engine) == 1


# --- tenants and roles ---------------------------------------------------------------------------


async def test_a_tenant_does_not_see_or_change_anothers_documents(
    engine: AsyncEngine, make_client: MakeClient, box: Box, storage: object
) -> None:
    alpha = await insert_tenant(engine, "alpha")
    beta = await insert_tenant(engine, "beta")
    a = make_client(await insert_token(engine, alpha, 1, Role.EDITOR))
    b = make_client(await insert_token(engine, beta, 2, Role.EDITOR))
    mine = (await upload_document(a, PDF)).json()

    listed = await b.get("/assets", params={"kind": "document"})
    patched = await b.patch(f"/assets/{mine['id']}", json={"title": "x"})
    deleted = await b.delete(f"/assets/{mine['id']}")

    assert listed.json()["items"] == []
    assert patched.status_code == 404 and deleted.status_code == 404
    assert (
        len(((await a.get("/assets", params={"kind": "document"})).json())["items"])
        == 1
    )


async def test_the_same_pdf_uploaded_by_two_tenants_is_two_assets(
    engine: AsyncEngine, make_client: MakeClient, box: Box, storage: object
) -> None:
    alpha = await insert_tenant(engine, "alpha")
    beta = await insert_tenant(engine, "beta")
    a = make_client(await insert_token(engine, alpha, 1, Role.EDITOR))
    b = make_client(await insert_token(engine, beta, 2, Role.EDITOR))

    first = await upload_document(a, PDF)
    second = await upload_document(b, PDF)

    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["id"] != second.json()["id"]


async def test_students_cannot_upload_documents(
    engine: AsyncEngine, make_client: MakeClient, box: Box, storage: object
) -> None:
    tenant = await insert_tenant(engine, "alpha")
    student = make_client(await insert_token(engine, tenant, 7, Role.STUDENT))

    r = await upload_document(student, PDF)

    assert r.status_code == 403
    assert await count(engine) == 0


async def test_an_anonymous_request_cannot_upload_documents_outside_legacy_mode(
    make_client: MakeClient, box: Box, storage: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEGACY_ANONYMOUS_MODE", "false")
    from app.core.config import get_settings

    get_settings.cache_clear()
    try:
        r = await upload_document(make_client(None), PDF)
    finally:
        get_settings.cache_clear()

    assert r.status_code == 401
