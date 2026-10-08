"""Where asset bytes live.

`AssetStorage` is the seam for a later S3/CDN adapter: services only ever call
`put`, `get` and `delete` with an opaque key, never touch paths.
"""

import asyncio
import os
import re
import uuid
from pathlib import Path
from typing import Protocol

# A key is an asset id, optionally with a variant suffix. Nothing else is ever
# turned into a path, so a hostile id cannot walk out of the storage directory.
_KEY_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(\.thumb)?$"
)


def content_key(asset_id: uuid.UUID) -> str:
    return str(asset_id)


def thumb_key(asset_id: uuid.UUID) -> str:
    return f"{asset_id}.thumb"


class AssetFileMissingError(Exception):
    """The database knows the asset but the storage has no bytes for it."""


class AssetStorage(Protocol):
    async def put(self, key: str, data: bytes) -> None:
        """Stores `data` under `key`, replacing anything there. All or nothing."""
        ...

    async def get(self, key: str) -> bytes:
        """Raises `AssetFileMissingError` if there is nothing under `key`."""
        ...

    async def delete(self, key: str) -> None:
        """Removes `key` if present. Only used to undo a failed upload."""
        ...


class LocalDiskAssetStorage:
    """Files under one directory, sharded so no folder grows huge.

    The file name is the key (an id we generated); a client-provided name is
    never used. Shards come from the END of the id: ids are UUIDv7, whose
    first characters are a timestamp and would put a whole day of uploads in
    one folder, while the last ones are random.
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    def _path(self, key: str) -> Path:
        if not _KEY_PATTERN.fullmatch(key):
            raise ValueError("invalid asset key")
        stem = key[:36]
        return self._root / stem[-4:-2] / stem[-2:] / key

    async def put(self, key: str, data: bytes) -> None:
        await asyncio.to_thread(self._write, self._path(key), data)

    @staticmethod
    def _write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write beside the target, then rename: a reader never sees half a file.
        temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_bytes(data)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    async def get(self, key: str) -> bytes:
        path = self._path(key)
        try:
            return await asyncio.to_thread(path.read_bytes)
        except FileNotFoundError as exc:
            raise AssetFileMissingError(key) from exc

    async def delete(self, key: str) -> None:
        path = self._path(key)
        await asyncio.to_thread(path.unlink, True)
