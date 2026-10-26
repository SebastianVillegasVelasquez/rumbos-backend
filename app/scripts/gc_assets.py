"""Deletes uploaded images that nothing references.

    python -m app.scripts.gc_assets                       # dry run: only lists
    python -m app.scripts.gc_assets --older-than-days 30 --apply

An asset is garbage when no draft map, skin or publication uses it (the same
usage rule as `DELETE /assets/{id}`) and it is older than the threshold, which
leaves time for an editor who has just uploaded something to use it. Library
tracks (audio with no tenant) are curated, not uploaded and forgotten, so they
are never collected. Deletion
goes through `AssetService.delete`, so the row goes first and then the files.

It refuses to run (dry runs included) while the storage directory does not
match the database (see `app.assets.fingerprint`): deleting rows of assets whose
files are merely not mounted would destroy the only record of them.
"""

import argparse
import asyncio
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.assets.fingerprint import StorageState, inspect_storage
from app.assets.storage import AssetStorage, LocalDiskAssetStorage
from app.core.config import get_settings
from app.core.database import create_engine
from app.exceptions import AssetInUseError, AssetNotFoundError
from app.repositories.sqlalchemy.app_meta_repository import SqlAlchemyAppMetaRepository
from app.repositories.sqlalchemy.asset_repository import SqlAlchemyAssetRepository
from app.schemas.asset import AssetRecord
from app.services.asset_service import AssetService, AssetSettings
from app.tenancy.scope import SYSTEM, bind_scope

DEFAULT_DAYS = 7
_USAGE_BATCH = 500


async def find_garbage(
    session: AsyncSession, older_than: timedelta, now: datetime | None = None
) -> list[AssetRecord]:
    """Assets older than `older_than` that no map, skin or publication uses."""
    cutoff = (now or datetime.now(UTC)) - older_than
    assets = SqlAlchemyAssetRepository(session)
    old = await assets.list_created_before(cutoff)
    garbage: list[AssetRecord] = []
    for start in range(0, len(old), _USAGE_BATCH):
        batch = old[start : start + _USAGE_BATCH]
        usage = await assets.usage([a.id for a in batch])
        garbage.extend(a for a in batch if usage[a.id].total == 0)
    return garbage


async def run(
    session: AsyncSession,
    storage: AssetStorage,
    settings: AssetSettings,
    *,
    older_than: timedelta,
    apply: bool,
    out: Callable[[str], None] = print,
    now: datetime | None = None,
) -> int:
    """Returns the bytes reclaimed (or, in a dry run, that would be)."""
    garbage = await find_garbage(session, older_than, now)
    service = AssetService(SqlAlchemyAssetRepository(session), storage, settings)
    reclaimed = 0
    removed = 0
    for asset in garbage:
        line = f"{asset.id} {asset.kind.value} {asset.size_bytes} bytes {asset.title!r}"
        if not apply:
            out(f"Would delete {line}")
            reclaimed += asset.size_bytes
            removed += 1
            continue
        try:
            await service.delete(asset.id)
        except (AssetInUseError, AssetNotFoundError):
            out(f"Skipped {line} (it changed while running)")
            continue
        out(f"Deleted {line}")
        reclaimed += asset.size_bytes
        removed += 1
    action = "deleted" if apply else "to delete"
    suffix = "" if apply else " (dry run: pass --apply to do it)"
    out(f"{removed} asset(s) {action}, {reclaimed} bytes reclaimed{suffix}")
    return reclaimed


async def _main(days: int, apply: bool) -> int:
    settings = get_settings()
    engine = create_engine(settings.build_database_url)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            # Housekeeping over every tenant's data, not one tenant's business.
            bind_scope(session, SYSTEM)
            report = await inspect_storage(
                SqlAlchemyAssetRepository(session),
                SqlAlchemyAppMetaRepository(session),
                Path(settings.assets_dir),
            )
            if report.state is StorageState.DEGRADED:
                print(
                    f"error: refusing to run: the storage at {settings.assets_dir} "
                    f"does not match the database ({report.detail}). Fix the "
                    "volume mount first; see the README, 'Persistent storage'.",
                    file=sys.stderr,
                )
                return 1
            await run(
                session,
                LocalDiskAssetStorage(Path(settings.assets_dir)),
                AssetSettings.from_app_settings(settings),
                older_than=timedelta(days=days),
                apply=apply,
            )
    finally:
        await engine.dispose()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--older-than-days",
        type=int,
        default=DEFAULT_DAYS,
        help=f"minimum age in days (default {DEFAULT_DAYS})",
    )
    parser.add_argument(
        "--apply", action="store_true", help="delete (default: dry run)"
    )
    args = parser.parse_args(argv)
    if args.older_than_days < 0:
        parser.error("--older-than-days cannot be negative")
    return asyncio.run(_main(args.older_than_days, args.apply))


if __name__ == "__main__":
    sys.exit(main())
