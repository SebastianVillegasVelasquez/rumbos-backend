"""`python -m app.scripts.assets verify` and `storage-adopt`, against real Postgres."""

import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.assets.fingerprint import (
    MARKER_NAME,
    StorageState,
    adopt_storage,
    inspect_storage,
    read_marker,
)
from app.assets.storage import AssetStorage, content_key, thumb_key
from app.models import Asset
from app.repositories.sqlalchemy.app_meta_repository import SqlAlchemyAppMetaRepository
from app.repositories.sqlalchemy.asset_repository import SqlAlchemyAssetRepository
from app.repositories.sqlalchemy.tenant_repository import SqlAlchemyTenantRepository
from app.scripts import assets as cli
from app.tenancy.scope import SYSTEM, bind_scope
from tests.asset_helpers import library_track, png_asset
from tests.test_publications_api import add_bubble, make_map, publish


class Verify:
    """Runs the command in the operator's (system) scope and keeps its output."""

    def __init__(self, session: AsyncSession, storage: AssetStorage, root: Path):
        bind_scope(session, SYSTEM)
        self.assets = SqlAlchemyAssetRepository(session)
        self.meta = SqlAlchemyAppMetaRepository(session)
        self.tenants = SqlAlchemyTenantRepository(session)
        self.storage = storage
        self.root = root
        self.out: list[str] = []

    async def verify(self, *, as_json: bool = False) -> int:
        return await cli.verify(
            self.assets,
            self.meta,
            self.tenants,
            self.storage,
            self.root,
            as_json=as_json,
            out=self.out.append,
        )

    async def adopt(self, *, apply: bool) -> int:
        return await cli.storage_adopt(
            self.assets, self.meta, self.root, apply=apply, out=self.out.append
        )

    @property
    def text(self) -> str:
        return "\n".join(self.out)


@pytest.fixture
async def op(
    engine: AsyncEngine, storage: AssetStorage, tmp_path: Path
) -> AsyncIterator[Verify]:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        operator = Verify(session, storage, tmp_path / "assets")
        yield operator


async def fingerprint(op: Verify) -> None:
    """The state a healthy deployment is in: the directory is the database's."""
    await adopt_storage(op.meta, op.root)


async def row_count(engine: AsyncEngine) -> int:
    async with AsyncSession(engine) as session:
        bind_scope(session, SYSTEM)
        return int(await session.scalar(select(func.count()).select_from(Asset)) or 0)


# --- verify ----------------------------------------------------------------------


async def test_verify_is_clean_and_exits_0_when_every_file_is_there(
    client: httpx.AsyncClient, op: Verify
) -> None:
    await png_asset(client, 1, "background")
    await fingerprint(op)

    code = await op.verify()

    assert code == 0
    assert "1 asset(s) checked, 0 with missing files" in op.text


async def test_verify_lists_what_is_missing_who_owns_it_and_what_uses_it(
    client: httpx.AsyncClient, op: Verify, storage: AssetStorage
) -> None:
    gone = await png_asset(client, 1, "background", title="Portada")
    intact = await png_asset(client, 2, "background")
    unused = await png_asset(client, 3, "bubble")
    created = await make_map(client, course=1, image_url=gone["url"])
    await add_bubble(client, created["id"], 1)
    await publish(client, created["id"])
    await fingerprint(op)
    for key in (
        content_key(uuid.UUID(gone["id"])),
        thumb_key(uuid.UUID(gone["id"])),
        content_key(uuid.UUID(unused["id"])),
    ):
        await storage.delete(key)

    code = await op.verify()

    assert code == 1
    first = next(line for line in op.out if gone["id"] in line)
    assert first.startswith("MISSING") and "background" in first
    assert "'Portada'" in first and "original, thumbnail" in first
    assert "created " in first and "UTC" in first
    uses = op.out[op.out.index(first) + 1]
    assert f"map {created['id']}" in uses and f"publication {created['id']}#1" in uses
    other = next(line for line in op.out if unused["id"] in line)
    assert "files: original" in other
    assert op.out[op.out.index(other) + 1].strip() == "used by: nothing"
    assert intact["id"] not in op.text
    assert "3 asset(s) checked, 2 with missing files, 1 of them in use" in op.text


async def test_verify_shows_the_library_as_the_owner_of_a_library_track(
    engine: AsyncEngine, op: Verify
) -> None:
    track = await library_track(engine, title="Brisa")
    await fingerprint(op)

    assert await op.verify() == 1

    line = next(line for line in op.out if str(track.id) in line)
    assert "library" in line and "audio" in line and "'Brisa'" in line


async def test_verify_json_has_the_same_facts(
    client: httpx.AsyncClient, op: Verify, storage: AssetStorage
) -> None:
    gone = await png_asset(client, 1, "background")
    created = await make_map(client, course=1, image_url=gone["url"])
    await fingerprint(op)
    await storage.delete(content_key(uuid.UUID(gone["id"])))

    code = await op.verify(as_json=True)

    report = json.loads(op.text)
    assert code == 1
    assert report["checked"] == 1
    assert report["totals"] == {"missing": 1, "inUse": 1}
    assert report["storage"]["state"] == "ok" and report["storage"]["marker"] == "ok"
    [entry] = report["missing"]
    assert entry["id"] == gone["id"] and entry["kind"] == "background"
    assert entry["missingFiles"] == ["original"]
    assert entry["usedBy"]["maps"] == [{"id": created["id"], "title": "Mapa"}]
    assert entry["tenant"] and entry["createdAt"] and entry["title"]


async def test_verify_changes_nothing(
    client: httpx.AsyncClient,
    op: Verify,
    engine: AsyncEngine,
    storage: AssetStorage,
    tmp_path: Path,
) -> None:
    gone = await png_asset(client, 1, "background")
    kept = await png_asset(client, 2, "background")
    await storage.delete(content_key(uuid.UUID(gone["id"])))
    before = sorted(p.name for p in (tmp_path / "assets").rglob("*"))

    code = await op.verify()

    assert code == 1  # this storage was never fingerprinted either
    assert await row_count(engine) == 2
    assert sorted(p.name for p in (tmp_path / "assets").rglob("*")) == before
    assert not (tmp_path / "assets" / MARKER_NAME).exists()
    assert await op.meta.get("storage_id") is None
    assert await storage.get(content_key(uuid.UUID(kept["id"])))


async def test_verify_never_creates_the_directory_it_is_pointed_at(
    engine: AsyncEngine, storage: AssetStorage, tmp_path: Path
) -> None:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        operator = Verify(session, storage, tmp_path / "nowhere")

        assert await operator.verify() == 0

    assert not (tmp_path / "nowhere").exists()


async def test_verify_fails_when_the_storage_is_not_the_databases_even_if_files_exist(
    client: httpx.AsyncClient, op: Verify
) -> None:
    await png_asset(client, 1, "background")
    await fingerprint(op)
    (op.root / MARKER_NAME).unlink()  # a replaced volume that has the files

    code = await op.verify()

    assert code == 1
    assert "degraded" in op.text and "marker file: missing" in op.text
    assert "0 with missing files" in op.text


# --- storage-adopt ---------------------------------------------------------------


async def test_adopt_without_apply_only_reports(
    client: httpx.AsyncClient, op: Verify
) -> None:
    await png_asset(client, 1, "background")
    await fingerprint(op)
    (op.root / MARKER_NAME).unlink()

    assert await op.adopt(apply=False) == 0

    assert "Dry run" in op.text
    assert read_marker(op.root) is None


async def test_adopt_with_apply_makes_a_degraded_storage_the_databases(
    client: httpx.AsyncClient, op: Verify, storage: AssetStorage
) -> None:
    made = await png_asset(client, 1, "background")
    await fingerprint(op)
    await storage.delete(content_key(uuid.UUID(made["id"])))
    (op.root / MARKER_NAME).unlink()
    assert (await inspect_storage(op.assets, op.meta, op.root)).state is (
        StorageState.DEGRADED
    )

    assert await op.adopt(apply=True) == 0

    assert (await inspect_storage(op.assets, op.meta, op.root)).state is (
        StorageState.OK
    )
    assert read_marker(op.root) == await op.meta.get("storage_id")
    assert "Restart the app" in op.text
    assert await op.verify() == 1  # adopting does not conjure the missing file


def test_the_command_line_knows_both_commands() -> None:
    parser = cli.build_parser()

    assert parser.parse_args(["verify"]).json is False
    assert parser.parse_args(["verify", "--json"]).json is True
    assert parser.parse_args(["storage-adopt"]).apply is False
    assert parser.parse_args(["storage-adopt", "--apply"]).apply is True
