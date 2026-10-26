"""Curates the audio library and takes assets down. For the operator.

    python -m app.scripts.assets import-audio --file song.mp3 \\
        --title "Brisa" --credit "Ana Perez, CC BY 4.0" --license "CC BY 4.0"
    python -m app.scripts.assets list
    python -m app.scripts.assets quarantine <asset-id> --reason "Rights holder's complaint, ticket 123"
    python -m app.scripts.assets release <asset-id>
    python -m app.scripts.assets history <asset-id>
    python -m app.scripts.assets verify [--json]
    python -m app.scripts.assets storage-adopt [--apply]

`import-audio` makes a library track: it is checked exactly like an editor's
upload (real MP3 or M4A, at most the size and length limits, tags stripped) and
then belongs to no tenant, so every tenant can pick it and no editor can change
or delete it. The operator vouches for its rights; the credit and the licence
text are shown to editors.

`list` shows every audio track, the library's and each tenant's, with the
tenant, the pseudonymous id of whoever uploaded it, when they declared the
rights, and how much it is in use.

`quarantine` takes an asset out of service at once: it stops being served (404),
disappears from lists and pickers, and maps that use it go on working without
music. Published snapshots keep their frozen reference, but readers are no
longer shown the track. `release` undoes it. Both are recorded, with the reason
and the time, in the asset's audit trail (`history`) and in the log.

`verify` checks the storage against the database and changes nothing: it lists
every asset whose file (original or thumbnail) is not in the storage directory,
with its tenant, kind, title, creation date and the maps, skins and publications
that use it, then the totals. It exits with 1 when any file is missing or the
directory is not the one the database was filled from (see the README,
"Persistent storage"). Run it after every deployment that touches storage.

`storage-adopt` is for the day the operator has decided that the directory in
use is the right one although the database says otherwise (files lost for good,
a restore without the marker file): it records this directory as the storage.
Without `--apply` it only reports. Restart the app afterwards.
"""

import argparse
import asyncio
import json
import logging
import sys
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.assets.fingerprint import (
    STORAGE_ID_KEY,
    StorageState,
    adopt_storage,
    inspect_storage,
    read_marker,
)
from app.assets.storage import (
    AssetStorage,
    LocalDiskAssetStorage,
    content_key,
    thumb_key,
)
from app.core.config import get_settings
from app.core.database import create_engine
from app.exceptions import (
    AssetTooLargeError,
    AudioCreditRequiredError,
    AudioInvalidError,
    AudioQuarantinedError,
    AudioTooLongError,
    InvalidAssetMetadataError,
)
from app.repositories.protocols import (
    AppMetaRepository,
    AssetRepository,
    TenantRepository,
)
from app.repositories.sqlalchemy.app_meta_repository import SqlAlchemyAppMetaRepository
from app.repositories.sqlalchemy.asset_repository import SqlAlchemyAssetRepository
from app.repositories.sqlalchemy.tenant_repository import SqlAlchemyTenantRepository
from app.schemas.asset import AssetRecord, AssetReferences
from app.services.asset_service import AssetService, AssetSettings
from app.tenancy.scope import SYSTEM, bind_scope

OK = 0
FAILED = 1
REASON_MAX_LENGTH = 500

logger = logging.getLogger("rumbos.assets")


class UserError(Exception):
    """Something the operator typed that cannot be done."""


def _asset_id(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError as exc:
        raise UserError(f"{raw!r} is not an asset id") from exc


def _day(moment: datetime | None) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M") + " UTC" if moment else "-"


async def _import_audio(
    assets: AssetRepository,
    service: AssetService,
    args: argparse.Namespace,
    out: Callable[[str], None],
) -> int:
    path: Path = args.file
    try:
        size = path.stat().st_size
        handle = path.open("rb")
    except OSError as exc:
        raise UserError(f"cannot read {path}: {exc.__class__.__name__}") from exc
    with handle:
        result = await service.import_library_audio(
            handle, size, title=args.title, credit=args.credit, license=args.license
        )
    asset = result.asset
    if not result.created:
        out(f"Already in the library as {asset.id} ({asset.title!r}); nothing changed")
        return OK
    await assets.record_event(asset.id, "created", "imported to the library")
    logger.info("Library track %s imported: %r", asset.id, asset.title)
    out(f"Imported {asset.title!r}")
    out(f"  id:       {asset.id}")
    out(
        f"  type:     {asset.mime}, {asset.duration_seconds:.1f} s, {asset.size_bytes} bytes"
    )
    out(f"  credit:   {asset.credit}")
    out(f"  license:  {asset.license or '-'}")
    return OK


async def _list(
    assets: AssetRepository,
    tenants: TenantRepository,
    out: Callable[[str], None],
) -> int:
    tracks = await assets.list_audio()
    usage = await assets.usage([t.id for t in tracks])
    names: dict[uuid.UUID, str] = {}

    async def owner(track: AssetRecord) -> str:
        if track.tenant_id is None:
            return "library"
        if track.tenant_id not in names:
            tenant = await tenants.get_by_id(track.tenant_id)
            names[track.tenant_id] = tenant.issuer if tenant else str(track.tenant_id)
        return names[track.tenant_id]

    for track in tracks:
        used = usage[track.id]
        state = "QUARANTINED" if track.is_quarantined else "active"
        out(
            f"{track.id}  {state:<11}  {await owner(track)}  "
            f"{track.duration_seconds or 0:.1f}s  {track.size_bytes}B  "
            f"uploader={track.uploaded_by_sub or '-'}  "
            f"declared={_day(track.rights_declared_at)}  "
            f"used={used.maps} map(s), {used.publications} publication(s)"
        )
        out(
            f"    {track.title!r}  credit={track.credit!r}  "
            f"license={track.license or '-'}"
        )
        if track.is_quarantined:
            out(f"    since {_day(track.quarantined_at)}: {track.quarantine_reason}")
    out(f"{len(tracks)} audio track(s)")
    return OK


async def _quarantine(
    assets: AssetRepository, args: argparse.Namespace, out: Callable[[str], None]
) -> int:
    asset_id = _asset_id(args.asset_id)
    reason = " ".join(args.reason.split())
    if not reason or len(reason) > REASON_MAX_LENGTH:
        raise UserError(f"the reason must be 1-{REASON_MAX_LENGTH} characters")
    now = datetime.now(UTC)
    found = await assets.set_quarantine(asset_id, reason, now)
    if found is None:
        raise UserError(f"no asset {asset_id}")
    asset, changed = found
    if not changed:
        out(
            f"{asset_id} was already quarantined since "
            f"{_day(asset.quarantined_at)}: {asset.quarantine_reason}"
        )
        return OK
    used = (await assets.usage([asset_id]))[asset_id]
    logger.warning("Asset %s quarantined at %s: %s", asset_id, now.isoformat(), reason)
    out(f"Quarantined {asset_id} ({asset.title!r}) at {_day(now)}")
    out(f"  reason: {reason}")
    out(
        f"  used by {used.maps} draft map(s) and {used.publications} publication(s): "
        "they go on without it"
    )
    out("  Purge any proxy or CDN cache of this asset's URL; see the README.")
    return OK


async def _release(
    assets: AssetRepository, args: argparse.Namespace, out: Callable[[str], None]
) -> int:
    asset_id = _asset_id(args.asset_id)
    found = await assets.set_quarantine(asset_id, None, datetime.now(UTC))
    if found is None:
        raise UserError(f"no asset {asset_id}")
    asset, changed = found
    if not changed:
        out(f"{asset_id} was not quarantined; nothing changed")
        return OK
    logger.warning("Asset %s released from quarantine", asset_id)
    out(f"Released {asset_id} ({asset.title!r}): it is served and listed again")
    return OK


async def _history(
    assets: AssetRepository, args: argparse.Namespace, out: Callable[[str], None]
) -> int:
    asset_id = _asset_id(args.asset_id)
    events = await assets.audit_events(asset_id)
    for event in events:
        out(f"{_day(event.created_at)}  {event.action:<11}  {event.reason or ''}")
    out(f"{len(events)} event(s)")
    return OK


def _marker_state(stored: str | None, marker: str | None) -> str:
    if marker is None:
        return "missing"
    return "ok" if marker == stored else "different"


def _uses(refs: AssetReferences) -> list[str]:
    return (
        [f"map {m.id} {m.title!r}" for m in refs.maps]
        + [f"skin {s.id} {s.name!r}" for s in refs.skins]
        + [f"publication {p.course_map_id}#{p.number}" for p in refs.publications]
    )


async def verify(
    assets: AssetRepository,
    meta: AppMetaRepository,
    tenants: TenantRepository,
    storage: AssetStorage,
    root: Path,
    *,
    as_json: bool,
    out: Callable[[str], None] = print,
) -> int:
    """Read-only: nothing is created, written or deleted. Returns `FAILED` when
    a file is missing or the directory does not match the database."""
    records = await assets.list_all()
    verdict = await inspect_storage(assets, meta, root)
    stored = await meta.get(STORAGE_ID_KEY)
    marker = _marker_state(stored, read_marker(root))
    parts = {
        r.id: {
            "original": content_key(r.id),
            **({"thumbnail": thumb_key(r.id)} if r.kind.has_thumbnail else {}),
        }
        for r in records
    }
    present = await storage.present(
        [k for keys in parts.values() for k in keys.values()]
    )
    broken = [
        (r, [name for name, key in parts[r.id].items() if key not in present])
        for r in records
    ]
    broken = [(r, lost) for r, lost in broken if lost]
    refs = await assets.references([r.id for r, _ in broken])
    owners: dict[uuid.UUID | None, str] = {None: "library"}
    for record, _ in broken:
        if record.tenant_id is not None and record.tenant_id not in owners:
            tenant = await tenants.get_by_id(record.tenant_id)
            owners[record.tenant_id] = (
                tenant.issuer if tenant else str(record.tenant_id)
            )
    used = sum(1 for r, _ in broken if _uses(refs[r.id]))

    if as_json:
        out(
            json.dumps(
                {
                    "storage": {
                        "path": str(root),
                        "id": stored,
                        "marker": marker,
                        "state": verdict.state.value,
                        "detail": verdict.detail,
                    },
                    "checked": len(records),
                    "missing": [
                        {
                            "id": str(r.id),
                            "kind": r.kind.value,
                            "tenant": owners[r.tenant_id],
                            "title": r.title,
                            "createdAt": r.created_at.isoformat(),
                            "missingFiles": lost,
                            "quarantined": r.is_quarantined,
                            "usedBy": refs[r.id].model_dump(mode="json", by_alias=True),
                        }
                        for r, lost in broken
                    ],
                    "totals": {"missing": len(broken), "inUse": used},
                },
                indent=2,
            )
        )
    else:
        out(f"Storage {root}: {verdict.state.value} ({verdict.detail})")
        out(f"  storage id in the database: {stored or '-'}; marker file: {marker}")
        for record, lost in broken:
            out(
                f"MISSING {record.id}  {record.kind.value}  {owners[record.tenant_id]}  "
                f"{record.title!r}  created {_day(record.created_at)}  "
                f"files: {', '.join(lost)}"
                + ("  (quarantined)" if record.is_quarantined else "")
            )
            uses = _uses(refs[record.id])
            out(f"    used by: {'; '.join(uses) if uses else 'nothing'}")
        out(
            f"{len(records)} asset(s) checked, {len(broken)} with missing files, "
            f"{used} of them in use"
        )
    if verdict.state is StorageState.DEGRADED and not as_json:
        out(
            "The storage does not match the database. Fix the volume mount, or, if "
            "this directory is the right one, run `storage-adopt`."
        )
    return FAILED if broken or verdict.state is StorageState.DEGRADED else OK


async def storage_adopt(
    assets: AssetRepository,
    meta: AppMetaRepository,
    root: Path,
    *,
    apply: bool,
    out: Callable[[str], None] = print,
) -> int:
    verdict = await inspect_storage(assets, meta, root)
    stored = await meta.get(STORAGE_ID_KEY)
    out(f"Storage {root}: {verdict.state.value} ({verdict.detail})")
    out(
        f"  storage id in the database: {stored or '-'}; marker file: "
        + _marker_state(stored, read_marker(root))
    )
    if not apply:
        out(
            "Dry run: with --apply this directory is recorded as the storage the "
            "database belongs to (a marker file is written into it). Assets whose "
            "files are not here stay broken; `verify` lists them."
        )
        return OK
    storage_id = await adopt_storage(meta, root)
    logger.warning("Storage %s adopted by the operator with id %s", root, storage_id)
    out(f"Adopted: storage id {storage_id} is in the database and in {root}.")
    out("Restart the app so that it stops reporting the storage as degraded.")
    return OK


async def execute(
    assets: AssetRepository,
    service: AssetService,
    tenants: TenantRepository,
    args: argparse.Namespace,
    out: Callable[[str], None] = print,
    err: Callable[[str], None] = lambda message: print(message, file=sys.stderr),
) -> int:
    """Runs one parsed command. Returns the process exit code."""
    try:
        match args.command:
            case "import-audio":
                return await _import_audio(assets, service, args, out)
            case "list":
                return await _list(assets, tenants, out)
            case "quarantine":
                return await _quarantine(assets, args, out)
            case "release":
                return await _release(assets, args, out)
            case "history":
                return await _history(assets, args, out)
    except UserError as exc:
        err(f"error: {exc}")
    except AudioInvalidError as exc:
        err(f"error: not a valid MP3 or M4A (AAC) file: {exc.reason}")
    except AudioQuarantinedError as exc:
        err(
            f"error: this audio is already here as {exc.asset_id} and is "
            "quarantined; `release` it if it should come back"
        )
    except AudioTooLongError as exc:
        err(f"error: the audio is longer than {exc.limit_seconds:.0f} seconds")
    except AssetTooLargeError as exc:
        err(f"error: the file is larger than {exc.limit_bytes} bytes")
    except AudioCreditRequiredError:
        err("error: --credit is required and cannot be blank")
    except InvalidAssetMetadataError as exc:
        err(f"error: {exc}")
    return FAILED


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)

    add = commands.add_parser("import-audio", help="add a track to the library")
    add.add_argument("--file", required=True, type=Path)
    add.add_argument("--title", required=True)
    add.add_argument("--credit", required=True, help="who made it, shown to editors")
    add.add_argument("--license", help="e.g. 'CC BY 4.0'")

    commands.add_parser("list", help="show every audio track and who is behind it")

    quarantine = commands.add_parser(
        "quarantine", help="take an asset out of service now"
    )
    quarantine.add_argument("asset_id")
    quarantine.add_argument("--reason", required=True)

    release = commands.add_parser("release", help="undo a quarantine")
    release.add_argument("asset_id")

    history = commands.add_parser("history", help="an asset's audit trail")
    history.add_argument("asset_id")

    check = commands.add_parser(
        "verify",
        help="list assets whose files are missing from the storage (read-only)",
    )
    check.add_argument("--json", action="store_true", help="machine-readable output")

    adopt = commands.add_parser(
        "storage-adopt",
        help="record this storage directory as the database's (operator decision)",
    )
    adopt.add_argument("--apply", action="store_true", help="do it (default: report)")
    return parser


async def _main(args: argparse.Namespace) -> int:
    settings = get_settings()
    engine = create_engine(settings.build_database_url)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            # The operator's view: every tenant's assets and the library.
            bind_scope(session, SYSTEM)
            assets = SqlAlchemyAssetRepository(session)
            root = Path(settings.assets_dir)
            storage = LocalDiskAssetStorage(root)
            meta = SqlAlchemyAppMetaRepository(session)
            if args.command == "verify":
                return await verify(
                    assets,
                    meta,
                    SqlAlchemyTenantRepository(session),
                    storage,
                    root,
                    as_json=args.json,
                )
            if args.command == "storage-adopt":
                return await storage_adopt(assets, meta, root, apply=args.apply)
            service = AssetService(
                assets, storage, AssetSettings.from_app_settings(settings)
            )
            return await execute(
                assets, service, SqlAlchemyTenantRepository(session), args
            )
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    args = build_parser().parse_args(argv)
    return asyncio.run(_main(args))


if __name__ == "__main__":
    sys.exit(main())
