"""What a degraded storage (see `test_storage_fingerprint`) stops: uploads and
the garbage collector, and how it shows on `/health/ready`."""

from pathlib import Path

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.assets.fingerprint import (
    StorageState,
    adopt_storage,
    inspect_storage,
)
from app.assets.storage import AssetStorage
from app.core.config import get_settings
from app.main import app
from app.repositories.sqlalchemy.app_meta_repository import SqlAlchemyAppMetaRepository
from app.repositories.sqlalchemy.asset_repository import SqlAlchemyAssetRepository
from app.scripts import gc_assets
from app.tenancy.scope import SYSTEM
from tests import audio_files as af
from tests import images
from tests.asset_helpers import png_asset
from tests.sessions import new_session


@pytest.fixture
def degraded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        app.state, "storage_state", StorageState.DEGRADED, raising=False
    )


async def test_uploads_are_refused_with_a_503_while_the_storage_is_degraded(
    client: httpx.AsyncClient, degraded: None, storage: AssetStorage, tmp_path: Path
) -> None:
    r = await client.post(
        "/assets",
        data={"kind": "bubble"},
        files={"file": ("a.png", images.png((8, 8)), "image/png")},
    )

    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "storage_unavailable"
    assert not list((tmp_path / "assets").rglob("*"))  # nothing landed anywhere
    assert (await client.get("/assets")).json()["total"] == 0


async def test_audio_uploads_are_refused_too(
    client: httpx.AsyncClient, degraded: None
) -> None:
    r = await client.post(
        "/assets",
        data={
            "kind": "audio",
            "title": "T",
            "credit": "C",
            "rightsDeclared": "true",
        },
        files={"file": ("a.mp3", af.mp3(), "audio/mpeg")},
    )

    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "storage_unavailable"


async def test_serving_and_listing_go_on_while_degraded(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = await png_asset(client, 1, "background")
    monkeypatch.setattr(
        app.state, "storage_state", StorageState.DEGRADED, raising=False
    )

    assert (await client.get(made["url"].removeprefix("/api/v1"))).status_code == 200
    assert (await client.get("/assets")).status_code == 200


async def test_uploads_work_when_the_storage_is_fine(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app.state, "storage_state", StorageState.OK, raising=False)

    await png_asset(client, 1)


async def test_ready_reports_the_storage_without_changing_the_status(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.api_client import ApiClient

    app.state.db_engine = engine
    transport = httpx.ASGITransport(app=app)
    async with ApiClient(transport=transport, base_url="http://t/api/v1") as c:
        ok = await c.get("/health/ready")
        monkeypatch.setattr(
            app.state, "storage_state", StorageState.DEGRADED, raising=False
        )
        bad = await c.get("/health/ready")

    assert (ok.status_code, ok.json()) == (200, {"status": "ok", "storage": "ok"})
    assert (bad.status_code, bad.json()) == (
        200,
        {"status": "ok", "storage": "degraded"},
    )


# --- the garbage collector --------------------------------------------------------


@pytest.fixture
def gc_target(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> "GcTarget":
    # The command opens its own engine from the settings; hand it the test one.
    monkeypatch.setattr(gc_assets, "create_engine", lambda url: engine)
    return GcTarget(monkeypatch, tmp_path)


class GcTarget:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self._monkeypatch = monkeypatch
        self._tmp = tmp_path

    def point_at(self, directory: Path) -> None:
        settings = get_settings().model_copy(update={"assets_dir": str(directory)})
        self._monkeypatch.setattr(gc_assets, "get_settings", lambda: settings)


async def test_gc_refuses_to_run_while_the_storage_is_degraded(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    tmp_path: Path,
    gc_target: GcTarget,
    capsys: pytest.CaptureFixture[str],
) -> None:
    await png_asset(client, 1, "background")
    async with new_session(engine, SYSTEM) as session:
        await adopt_storage(SqlAlchemyAppMetaRepository(session), tmp_path / "assets")
    gc_target.point_at(tmp_path / "somewhere-else")  # an empty, unmarked directory

    code = await gc_assets._main(0, True)

    assert code == 1
    err = capsys.readouterr().err
    assert "refusing to run" in err and "Persistent storage" in err
    assert (await client.get("/assets")).json()["total"] == 1  # nothing deleted


async def test_gc_runs_when_the_storage_matches(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    tmp_path: Path,
    gc_target: GcTarget,
    capsys: pytest.CaptureFixture[str],
) -> None:
    await png_asset(client, 1, "background")
    async with new_session(engine, SYSTEM) as session:
        await adopt_storage(SqlAlchemyAppMetaRepository(session), tmp_path / "assets")
    gc_target.point_at(tmp_path / "assets")

    code = await gc_assets._main(0, False)

    assert code == 0
    assert "1 asset(s) to delete" in capsys.readouterr().out


async def test_inspecting_changes_nothing(engine: AsyncEngine, tmp_path: Path) -> None:
    root = tmp_path / "never-created"
    async with new_session(engine, SYSTEM) as session:
        report = await inspect_storage(
            SqlAlchemyAssetRepository(session),
            SqlAlchemyAppMetaRepository(session),
            root,
        )
        stored = await SqlAlchemyAppMetaRepository(session).get("storage_id")

    assert report.state is StorageState.OK
    assert not root.exists()
    assert stored is None
