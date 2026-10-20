"""Registers and manages the Moodle sites (tenants) that may use Rumbos.

    python -m app.scripts.tenants add --name "Uni" --issuer https://moodle.uni.edu \\
        --host-origin https://moodle.uni.edu --public-key-file uni.pub.pem
    python -m app.scripts.tenants list
    python -m app.scripts.tenants disable --issuer https://moodle.uni.edu
    python -m app.scripts.tenants enable --issuer https://moodle.uni.edu
    python -m app.scripts.tenants add-key --issuer ... --public-key-file new.pub.pem
    python -m app.scripts.tenants revoke-key --issuer ... --kid <kid>
    python -m app.scripts.tenants frame-ancestors
    python -m app.scripts.tenants diagnose < assertion.txt

This is the only way to manage tenants: there is no HTTP surface for it. A
tenant is identified by its `--issuer` (the Moodle `wwwroot`, unique). Nothing
here prints a key's secret half or a token; public keys are never secret.
"""

import argparse
import asyncio
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import NoReturn, cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.launch import LaunchService, LaunchSettings
from app.auth.rate_limit import FixedWindowLimiter
from app.core.config import get_settings
from app.core.database import create_engine
from app.enums import KeyStatus, TenantStatus
from app.exceptions import (
    TenantIssuerTakenError,
    TenantKeyTakenError,
)
from app.repositories.protocols import AuthRepository, TenantRepository
from app.repositories.sqlalchemy.tenant_repository import SqlAlchemyTenantRepository
from app.schemas.tenant import NewTenantKey, TenantRecord
from app.tenancy.validation import (
    TenantInputError,
    load_public_key,
    normalize_issuer,
    normalize_origin,
    validate_kid,
)

OK = 0
FAILED = 1


def frame_ancestors_policy(origins: Sequence[str]) -> str:
    """The `Content-Security-Policy` header value that lets only `origins` embed
    the app; with none registered nothing may."""
    sources = " ".join(origins) if origins else "'none'"
    return f"frame-ancestors {sources}"


def _read_key(path: Path, kid: str | None) -> NewTenantKey:
    try:
        pem = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise TenantInputError(f"cannot read {path}: {exc.__class__.__name__}") from exc
    normalized, default_kid = load_public_key(pem)
    return NewTenantKey(
        kid=validate_kid(kid) if kid else default_kid, public_key_pem=normalized
    )


async def _tenant(tenants: TenantRepository, issuer: str) -> TenantRecord:
    tenant = await tenants.get_by_issuer(normalize_issuer(issuer))
    if tenant is None:
        raise TenantInputError(f"no tenant is registered for {issuer}")
    return tenant


async def _add(
    tenants: TenantRepository, args: argparse.Namespace, out: Callable[[str], None]
) -> int:
    name = args.name.strip()
    if not name or len(name) > 120:
        raise TenantInputError("name must be 1-120 characters")
    key = _read_key(args.public_key_file, args.kid)
    tenant = await tenants.create(
        name,
        normalize_issuer(args.issuer),
        normalize_origin(args.host_origin),
        args.asset_quota_bytes,
        key,
    )
    out(f"Registered {tenant.name!r}")
    out(f"  id:          {tenant.id}")
    out(f"  issuer:      {tenant.issuer}")
    out(f"  host origin: {tenant.host_origin}")
    out(f"  kid:         {key.kid}")
    return OK


async def _list(
    tenants: TenantRepository, args: argparse.Namespace, out: Callable[[str], None]
) -> int:
    rows = await tenants.list_all()
    for tenant in rows:
        keys = await tenants.list_keys(tenant.id)
        active = sum(1 for k in keys if k.status is KeyStatus.ACTIVE)
        out(
            f"{tenant.id}  {tenant.status.value:<8}  {tenant.issuer}  "
            f"origin={tenant.host_origin}  keys={active} active/{len(keys)}  "
            f"quota={tenant.asset_quota_bytes}  name={tenant.name!r}"
        )
    out(f"{len(rows)} tenant(s)")
    return OK


async def _set_status(
    tenants: TenantRepository,
    issuer: str,
    status: TenantStatus,
    out: Callable[[str], None],
) -> int:
    tenant = await _tenant(tenants, issuer)
    await tenants.set_status(tenant.id, status)
    out(f"{tenant.issuer} is now {status.value}")
    return OK


async def _add_key(
    tenants: TenantRepository, args: argparse.Namespace, out: Callable[[str], None]
) -> int:
    tenant = await _tenant(tenants, args.issuer)
    key = _read_key(args.public_key_file, args.kid)
    await tenants.add_key(tenant.id, key)
    out(f"Added key {key.kid} to {tenant.issuer}")
    return OK


async def _revoke_key(
    tenants: TenantRepository, args: argparse.Namespace, out: Callable[[str], None]
) -> int:
    tenant = await _tenant(tenants, args.issuer)
    revoked = await tenants.revoke_key(tenant.id, args.kid)
    if revoked is None:
        raise TenantInputError(f"{tenant.issuer} has no key {args.kid!r}")
    out(f"Key {revoked.kid} of {tenant.issuer} is revoked")
    return OK


async def _frame_ancestors(
    tenants: TenantRepository, out: Callable[[str], None]
) -> int:
    policy = frame_ancestors_policy(await tenants.active_host_origins())
    out(f"Content-Security-Policy: {policy}")
    return OK


async def _diagnose(
    tenants: TenantRepository,
    settings: LaunchSettings,
    assertion: str,
    out: Callable[[str], None],
) -> int:
    """Says which check an assertion fails. Never prints the assertion."""
    service = LaunchService(
        tenants,
        cast(AuthRepository, _NoAuthRepository()),
        settings,
        FixedWindowLimiter(1),
        FixedWindowLimiter(1),
    )
    reason = await service.diagnose(assertion.strip())
    out(f"result: {reason}")
    return OK if reason == "ok" else FAILED


class _NoAuthRepository:
    """`diagnose` never reaches the token or `jti` tables."""

    def __getattr__(self, name: str) -> NoReturn:
        raise AssertionError(f"diagnose must not call auth.{name}")


async def execute(
    tenants: TenantRepository,
    args: argparse.Namespace,
    out: Callable[[str], None] = print,
    err: Callable[[str], None] = lambda message: print(message, file=sys.stderr),
    read_stdin: Callable[[], str] = sys.stdin.read,
    launch_settings: LaunchSettings | None = None,
) -> int:
    """Runs one parsed command. Returns the process exit code."""
    try:
        match args.command:
            case "add":
                return await _add(tenants, args, out)
            case "list":
                return await _list(tenants, args, out)
            case "disable":
                return await _set_status(
                    tenants, args.issuer, TenantStatus.DISABLED, out
                )
            case "enable":
                return await _set_status(tenants, args.issuer, TenantStatus.ACTIVE, out)
            case "add-key":
                return await _add_key(tenants, args, out)
            case "revoke-key":
                return await _revoke_key(tenants, args, out)
            case "frame-ancestors":
                return await _frame_ancestors(tenants, out)
            case "diagnose":
                assert launch_settings is not None
                return await _diagnose(tenants, launch_settings, read_stdin(), out)
    except TenantInputError as exc:
        err(f"error: {exc}")
    except TenantIssuerTakenError:
        err("error: a tenant is already registered for that issuer")
    except TenantKeyTakenError:
        err("error: that kid is already used by this tenant (kids are never reused)")
    return FAILED


def _positive_int(raw: str) -> int:
    value = int(raw)
    if value < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)

    def with_issuer(name: str, help_text: str) -> argparse.ArgumentParser:
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--issuer", required=True, help="the Moodle wwwroot")
        return command

    add = commands.add_parser("add", help="register a Moodle site")
    add.add_argument("--name", required=True)
    add.add_argument("--issuer", required=True, help="the Moodle wwwroot (https)")
    add.add_argument(
        "--host-origin", required=True, help="origin that embeds the app (https)"
    )
    add.add_argument("--public-key-file", required=True, type=Path)
    add.add_argument("--kid", help="default: derived from the key")
    add.add_argument(
        "--asset-quota-bytes",
        type=_positive_int,
        default=None,
        help="default: ASSETS_MAX_TOTAL_BYTES",
    )
    commands.add_parser("list", help="show every tenant")
    with_issuer("disable", "refuse launches and invalidate tokens")
    with_issuer("enable", "undo `disable`")
    add_key = with_issuer("add-key", "add a public key (key rotation)")
    add_key.add_argument("--public-key-file", required=True, type=Path)
    add_key.add_argument("--kid", help="default: derived from the key")
    revoke = with_issuer("revoke-key", "stop accepting a key")
    revoke.add_argument("--kid", required=True)
    commands.add_parser(
        "frame-ancestors", help="print the CSP header value for the SPA's nginx"
    )
    commands.add_parser(
        "diagnose",
        help="read an assertion on stdin and say which check it fails (read-only)",
    )
    return parser


async def _main(args: argparse.Namespace) -> int:
    settings = get_settings()
    if getattr(args, "asset_quota_bytes", 0) is None:
        args.asset_quota_bytes = settings.assets_max_total_bytes
    engine = create_engine(settings.build_database_url)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            return await execute(
                SqlAlchemyTenantRepository(session),
                args,
                launch_settings=LaunchSettings(
                    audience=settings.auth_audience,
                    token_ttl_seconds=settings.access_token_ttl_seconds,
                    leeway_seconds=settings.launch_leeway_seconds,
                ),
            )
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return asyncio.run(_main(args))


if __name__ == "__main__":
    sys.exit(main())
