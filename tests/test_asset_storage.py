"""`LocalDiskAssetStorage`: layout, atomic writes, and refusing hostile keys."""

import uuid
from pathlib import Path

import pytest
from uuid_utils.compat import uuid7

from app.assets.storage import (
    AssetFileMissingError,
    LocalDiskAssetStorage,
    content_key,
    thumb_key,
)


async def test_roundtrip_and_overwrite(tmp_path: Path) -> None:
    storage = LocalDiskAssetStorage(tmp_path)
    key = content_key(uuid7())

    await storage.put(key, b"one")
    await storage.put(key, b"two")

    assert await storage.get(key) == b"two"


async def test_files_are_sharded_and_named_only_by_their_key(tmp_path: Path) -> None:
    storage = LocalDiskAssetStorage(tmp_path)
    asset_id = uuid7()

    await storage.put(content_key(asset_id), b"a")
    await storage.put(thumb_key(asset_id), b"b")

    files = sorted(p for p in tmp_path.rglob("*") if p.is_file())
    assert [p.name for p in files] == sorted([str(asset_id), f"{asset_id}.thumb"])
    # Sharded by the random tail of the id (UUIDv7 heads are timestamps).
    tail = str(asset_id)
    assert files[0].parent == tmp_path / tail[-4:-2] / tail[-2:]
    assert not list(tmp_path.rglob("*.tmp"))  # no half-written leftovers


async def test_missing_key_raises(tmp_path: Path) -> None:
    with pytest.raises(AssetFileMissingError):
        await LocalDiskAssetStorage(tmp_path).get(content_key(uuid.uuid4()))


async def test_delete_is_idempotent(tmp_path: Path) -> None:
    storage = LocalDiskAssetStorage(tmp_path)
    key = content_key(uuid7())
    await storage.put(key, b"x")

    await storage.delete(key)
    await storage.delete(key)

    with pytest.raises(AssetFileMissingError):
        await storage.get(key)


@pytest.mark.parametrize(
    "key",
    [
        "../../etc/passwd",
        "..\\..\\windows\\win.ini",
        "/etc/passwd",
        "photo.png",
        "",
        str(uuid.uuid4()).upper(),
        f"{uuid.uuid4()}/../x",
        f"{uuid.uuid4()}.png",
        f"{uuid.uuid4()}\x00",
    ],
)
async def test_only_asset_keys_ever_become_paths(tmp_path: Path, key: str) -> None:
    storage = LocalDiskAssetStorage(tmp_path / "root")

    with pytest.raises(ValueError, match="invalid asset key"):
        await storage.put(key, b"x")
    with pytest.raises(ValueError, match="invalid asset key"):
        await storage.get(key)
    with pytest.raises(ValueError, match="invalid asset key"):
        await storage.delete(key)
    assert not (tmp_path / "root").exists()
