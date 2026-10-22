"""Plain helpers for tests that upload images through the API."""

import uuid
from typing import Any

import httpx
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from uuid_utils.compat import uuid7

from app.enums import AssetKind
from app.models import Asset
from app.repositories.sqlalchemy.asset_repository import SqlAlchemyAssetRepository
from app.schemas.asset import AssetCreate, AssetRecord
from app.tenancy.scope import SYSTEM, bind_scope
from tests import images


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


async def png_asset(
    client: httpx.AsyncClient,
    n: int,
    kind: str = "bubble",
    *,
    size: tuple[int, int] | None = None,
    **form: str,
) -> dict[str, Any]:
    """Uploads a distinct small PNG and returns the asset.

    `n` makes the content distinct (and, without `size`, the width too); give
    the same `size` to several calls to get distinct images of equal dimensions.
    """
    data = images.png(size or (8 + n, 8), color=(n * 7 % 256, 30, 200))
    r = await client.post(
        "/assets",
        data={"kind": kind, **form},
        files={"file": (f"f{n}.png", data, "image/png")},
    )
    assert r.status_code == 201, r.text
    body: dict[str, Any] = r.json()
    return body


async def library_track(
    engine: AsyncEngine,
    *,
    title: str = "Pista",
    credit: str = "Autor",
    sha256: str | None = None,
    quarantined: bool = False,
) -> AssetRecord:
    """A library track row (no bytes in storage), made the way the operator's
    tools make it: in the system scope, with no tenant."""
    async with AsyncSession(engine, expire_on_commit=False) as session:
        bind_scope(session, SYSTEM)
        record = await SqlAlchemyAssetRepository(session).create(
            AssetCreate(
                id=uuid7(),
                kind=AssetKind.AUDIO,
                mime="audio/mpeg",
                size_bytes=1000,
                sha256=sha256 or uuid.uuid4().hex * 2,
                title=title,
                credit=credit,
                duration_seconds=12.5,
            )
        )
        if quarantined:
            await session.execute(
                update(Asset)
                .where(Asset.id == record.id)
                .values(quarantined_at=record.created_at, quarantine_reason="test")
            )
            await session.commit()
    return record
