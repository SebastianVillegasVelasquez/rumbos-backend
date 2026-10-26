"""Ties the database to the storage directory it was filled from.

Uploaded files live in a directory (`ASSETS_DIR`) and their metadata in the
database. If the directory is not persistent (a container filesystem, a volume
that was not mounted) the rows survive a deployment and the files do not, and
every asset answers 404 with nothing to say why.

So the first start with a writable directory generates a `storage_id`, keeps it
in `app_meta` and writes it to a marker file in the directory. Every start
compares them: assets in the database but no matching marker means this is not
the directory those assets were stored in. That is reported loudly (an ERROR
log, `storage: "degraded"` on `/health/ready`, uploads and the garbage
collector refused) or, with `STORAGE_FAIL_FAST`, the app does not start.
"""

import logging
import os
import uuid
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from fastapi import FastAPI

from app.assets.storage import LocalDiskAssetStorage, keys_of
from app.repositories.protocols import AppMetaRepository, AssetRepository

logger = logging.getLogger(__name__)

MARKER_NAME = ".rumbos-storage-id"
STORAGE_ID_KEY = "storage_id"
# How many of the newest assets are looked for when a database that predates the
# fingerprint (assets, but no storage id yet) meets a directory.
_SAMPLE = 50


class StorageState(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"


class StorageUnwritableError(Exception):
    """The storage directory cannot be created or written to."""


class StorageDegradedError(Exception):
    """The storage does not match the database and `STORAGE_FAIL_FAST` is on."""


@dataclass(frozen=True, slots=True)
class StorageReport:
    state: StorageState
    detail: str


def storage_state_of(app: FastAPI) -> StorageState:
    """The state the last start found. Unset (no lifespan ran): nothing to doubt."""
    return StorageState(getattr(app.state, "storage_state", StorageState.OK))


def ensure_writable(root: Path) -> None:
    """Creates `root` if needed and proves a file can be written in it."""
    probe = root / f".write-probe-{uuid.uuid4().hex}"
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe.write_bytes(b"")
    except OSError as exc:
        raise StorageUnwritableError(
            f"The asset storage directory {root} cannot be written to "
            f"({exc.__class__.__name__}: {exc.strerror or exc}). Check that the "
            "volume is mounted and that its owner is the user the app runs as "
            "(uid 1001 in the container image); see the README, 'Persistent storage'."
        ) from exc
    finally:
        probe.unlink(missing_ok=True)


def read_marker(root: Path) -> str | None:
    try:
        return (root / MARKER_NAME).read_text(encoding="utf-8").strip() or None
    except (FileNotFoundError, UnicodeDecodeError):
        return None


def write_marker(root: Path, storage_id: str) -> None:
    """Atomically, so a reader never sees half an id."""
    marker = root / MARKER_NAME
    temporary = root / f"{MARKER_NAME}.{uuid.uuid4().hex}.tmp"
    try:
        temporary.write_text(f"{storage_id}\n", encoding="utf-8")
        os.replace(temporary, marker)
    finally:
        temporary.unlink(missing_ok=True)


async def _any_recent_file_present(assets: AssetRepository, root: Path) -> bool:
    storage = LocalDiskAssetStorage(root)
    keys = [
        key
        for asset in await assets.list_recent(_SAMPLE)
        for key in keys_of(asset.id, has_thumbnail=asset.kind.has_thumbnail)
    ]
    return bool(await storage.present(keys))


async def adopt_storage(meta: AppMetaRepository, root: Path) -> str:
    """Makes the directory and the database agree, whatever they held. For the
    operator, who has decided this directory is the right one (`assets
    storage-adopt`); the app itself never does this. Returns the storage id."""
    ensure_writable(root)
    storage_id = await meta.set_if_absent(
        STORAGE_ID_KEY, read_marker(root) or str(uuid.uuid4())
    )
    write_marker(root, storage_id)
    return storage_id


async def check_storage(
    assets: AssetRepository,
    meta: AppMetaRepository,
    root: Path,
    *,
    fail_fast: bool = False,
) -> StorageReport:
    """Runs at every start. Raises `StorageUnwritableError` when the directory
    cannot be written, and `StorageDegradedError` on a mismatch when `fail_fast`."""
    ensure_writable(root)
    stored = await meta.get(STORAGE_ID_KEY)
    marker = read_marker(root)
    rows = await assets.count()

    if stored is None:
        if rows and not await _any_recent_file_present(assets, root):
            return _degraded(
                root,
                rows,
                "the database has no storage id yet and none of its newest "
                "files is in this directory",
                fail_fast,
            )
        # A new installation, or one that predates the fingerprint and whose
        # files are here: this directory becomes the storage. A marker already
        # in it is kept, so a database rebuilt over an old volume reunites.
        storage_id = await meta.set_if_absent(
            STORAGE_ID_KEY, marker or str(uuid.uuid4())
        )
        write_marker(root, storage_id)
        logger.info("Asset storage %s fingerprinted with id %s", root, storage_id)
        return StorageReport(StorageState.OK, "fingerprint created")

    if marker == stored:
        return StorageReport(StorageState.OK, "fingerprint matches")
    if rows == 0:
        # Nothing is registered, so nothing can be orphaned: take the directory.
        write_marker(root, stored)
        return StorageReport(StorageState.OK, "fingerprint restored")
    why = (
        "the directory has no storage marker"
        if marker is None
        else "the directory's storage marker belongs to a different storage"
    )
    return _degraded(root, rows, why, fail_fast)


def _degraded(root: Path, rows: int, why: str, fail_fast: bool) -> StorageReport:
    message = (
        f"The asset storage at {root} does not match the database: {why}, but the "
        f"database lists {rows} asset(s). This usually means the storage volume is "
        "not mounted (or a different, empty one is) so the uploaded files are not "
        "where the app looks. Check the volume mount of ASSETS_DIR. Uploads are "
        "refused and the garbage collector will not run until this is fixed; do "
        "NOT upload anything meanwhile. See the README, 'Persistent storage'."
    )
    logger.error(message)
    if fail_fast:
        raise StorageDegradedError(message)
    return StorageReport(StorageState.DEGRADED, why)
