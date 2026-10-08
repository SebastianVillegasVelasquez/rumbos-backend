"""The demo skin pack script, and the pack going through the real image-skin flow."""

import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncEngine

from app.api.routes.assets import get_asset_settings, get_asset_storage
from app.assets.storage import LocalDiskAssetStorage
from app.main import app
from app.services.asset_service import AssetSettings

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "make_demo_skin_pack.py"
STATES = ["available", "locked", "next", "inProgress", "complete", "hover"]


@pytest.fixture(scope="module")
def pack(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("pack")
    subprocess.run([sys.executable, str(SCRIPT), str(out)], check=True)
    return out


def test_the_script_writes_six_identical_artboards(pack: Path) -> None:
    assert sorted(p.stem for p in pack.glob("*.png")) == sorted(STATES)
    for state in STATES:
        image = Image.open(pack / f"{state}.png")
        assert (image.size, image.mode) == ((256, 256), "RGBA")
        assert image.getchannel("A").getpixel((0, 0)) == 0  # transparent corners


def test_the_states_differ_from_each_other(pack: Path) -> None:
    pixels = {s: Image.open(pack / f"{s}.png").tobytes() for s in STATES}
    assert len(set(pixels.values())) == len(STATES)


@pytest.fixture
def uploads(tmp_path: Path) -> Iterator[None]:
    settings = AssetSettings(
        uploads_enabled=True,
        max_total_bytes=10**9,
        max_background_bytes=8 * 1024 * 1024,
        max_bubble_bytes=2 * 1024 * 1024,
        max_background_side=8192,
        max_bubble_side=1024,
    )
    storage = LocalDiskAssetStorage(tmp_path / "assets")
    app.dependency_overrides[get_asset_settings] = lambda: settings
    app.dependency_overrides[get_asset_storage] = lambda: storage
    yield
    app.dependency_overrides.pop(get_asset_settings, None)
    app.dependency_overrides.pop(get_asset_storage, None)


@pytest.fixture
async def client(
    engine: AsyncEngine, uploads: None
) -> AsyncIterator[httpx.AsyncClient]:
    app.state.db_engine = engine
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


async def test_the_pack_uploads_and_makes_an_image_skin(
    client: httpx.AsyncClient, pack: Path
) -> None:
    ids: dict[str, str] = {}
    for state in STATES:
        r = await client.post(
            "/assets",
            data={"kind": "bubble"},
            files={
                "file": (
                    f"{state}.png",
                    (pack / f"{state}.png").read_bytes(),
                    "image/png",
                )
            },
        )
        assert r.status_code == 201, r.text
        assert (r.json()["width"], r.json()["height"]) == (256, 256)
        ids[state] = r.json()["id"]

    config = {
        "schemaVersion": 1,
        "kind": "image",
        "size": 96,
        "anchor": "center",
        "states": {
            "available": ids["available"],
            "locked": ids["locked"],
            "next": ids["next"],
            "inProgress": ids["inProgress"],
            "complete": ids["complete"],
            "hover": ids["hover"],
        },
        "label": {"mode": "hover"},
        "effects": {"idle": "float", "completion": "burst"},
    }
    created = await client.post("/skins", json={"name": "Demo", "config": config})

    assert created.status_code == 201, created.text
    assert created.json()["config"]["states"]["hover"] == ids["hover"]
