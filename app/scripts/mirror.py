"""Operator commands for the exam mirror (see `docs/EXAM-MIRROR-EXPORT.md`).

    python -m app.scripts.mirror configure --tenant <id|issuer> \\
        --endpoint https://moodle.uni.edu/local/rumbos/mirror.php [--written-text on|off]
    python -m app.scripts.mirror configure --tenant <id|issuer> --off
    python -m app.scripts.mirror sync --tenant <id|issuer> [--form <form id>] [--apply]

`configure` sets where a tenant's plugin receives definitions and attempts (or
turns the mirror off) and whether the text a student wrote may be sent. Both
are off for every tenant until this is run, so a deploy changes nothing.

`sync` queues again the definition of every published form (or just `--form`)
for the tenant. It only reports what it would do unless `--apply` is given, and
doing it twice leaves the same state: a definition already waiting stays as it
is, one already sent is sent again (the plugin keeps each once), a missing one
is created. Nothing here sends anything: the passback worker does, in order.
"""

import argparse
import asyncio
import sys
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from uuid_utils.compat import uuid7

from app.core.config import get_settings
from app.core.database import create_engine
from app.mirror.definition import definition_of
from app.mirror.statements import definition_claims
from app.models import MirrorDefinition, Tenant
from app.tenancy.scope import SYSTEM, bind_scope
from app.trainer.catalog import Catalog, load_catalog

Out = Callable[[str], None]
OK, FAILED = 0, 1
MAX_ENDPOINT = 500
_LOCAL = {"localhost", "127.0.0.1", "::1"}


class MirrorCommandError(Exception):
    """A refusal the operator should read; the command exits 1."""


def check_endpoint(url: str) -> str:
    """https only (plain http just for a machine's own loopback), no user or
    password in it, no fragment, bounded."""
    parts = urlsplit(url.strip())
    local = parts.hostname in _LOCAL
    if parts.scheme != "https" and not (parts.scheme == "http" and local):
        raise MirrorCommandError("The endpoint must be an https URL")
    if not parts.hostname or parts.username or parts.password or parts.fragment:
        raise MirrorCommandError("The endpoint must be a plain URL with a host")
    if len(url) > MAX_ENDPOINT:
        raise MirrorCommandError(f"The endpoint is over {MAX_ENDPOINT} characters")
    return url.strip()


async def find_tenant(session: AsyncSession, ref: str) -> Tenant:
    """A tenant by id or by issuer (its Moodle `wwwroot`)."""
    try:
        wanted = Tenant.id == uuid.UUID(ref)
    except ValueError:
        wanted = Tenant.issuer == ref.rstrip("/")
    tenant = await session.scalar(select(Tenant).where(wanted))
    if tenant is None:
        raise MirrorCommandError("No such tenant")
    return tenant


async def configure(
    session: AsyncSession,
    ref: str,
    *,
    endpoint: str | None,
    off: bool,
    written_text: bool | None,
    out: Out = print,
) -> None:
    if off and endpoint is not None:
        raise MirrorCommandError("Give either --endpoint or --off, not both")
    if not off and endpoint is None and written_text is None:
        raise MirrorCommandError(
            "Nothing to change: give --endpoint, --off or --written-text"
        )
    tenant = await find_tenant(session, ref)
    if off:
        tenant.mirror_endpoint = None
    elif endpoint is not None:
        tenant.mirror_endpoint = check_endpoint(endpoint)
    if written_text is not None:
        tenant.mirror_written_text = written_text
    summary = (
        f"{tenant.name}: mirror {'on' if tenant.mirror_endpoint else 'off'}, "
        f"student text {'may' if tenant.mirror_written_text else 'may not'} be sent"
    )
    await session.commit()
    out(summary)


async def resync(
    session: AsyncSession,
    catalog: Catalog,
    ref: str,
    *,
    form_id: str | None,
    apply: bool,
    issuer: str,
    now: datetime,
    out: Out = print,
) -> dict[str, int]:
    """Queues again the definitions of the published forms. Returns how many
    were created, queued again, already waiting."""
    tenant = await find_tenant(session, ref)
    if not tenant.mirror_endpoint:
        raise MirrorCommandError("The tenant's mirror is off: configure it first")
    counts = {"created": 0, "requeued": 0, "waiting": 0}
    wanted = [
        (pack, form)
        for pack in catalog.packs.values()
        for form in (pack.served_form(f, False) for f in pack.forms)
        if form is not None and form_id in (None, form.form_id)
    ]
    if form_id is not None and not wanted:
        raise MirrorCommandError("No published form with that id")
    verb = "" if apply else "would be "
    for pack, form in sorted(wanted, key=lambda w: w[1].form_id):
        definition, digest = definition_of(pack.content_version, form)
        row = await session.scalar(
            select(MirrorDefinition).where(
                MirrorDefinition.tenant_id == tenant.id,
                MirrorDefinition.form_id == form.form_id,
                MirrorDefinition.definition_hash == digest,
            )
        )
        if row is None:
            state = "created"
            if apply:
                session.add(
                    MirrorDefinition(
                        id=uuid7(),
                        tenant_id=tenant.id,
                        form_id=form.form_id,
                        definition_hash=digest,
                        content_version=pack.content_version,
                        statement=definition_claims(
                            tenant_id=tenant.id,
                            form_id=form.form_id,
                            content_version=pack.content_version,
                            definition_hash=digest,
                            definition=definition,
                            issuer=issuer,
                            issued_at=int(now.timestamp()),
                        ),
                        status="pending",
                        attempts=0,
                        next_try_at=now,
                    )
                )
        elif row.status == "pending":
            state = "waiting"
        else:
            state = "requeued"
            if apply:
                row.status, row.attempts, row.next_try_at = "pending", 0, now
                row.last_error = None
        counts[state] += 1
        out(f"{form.form_id} {digest[:12]}: {verb}{state}")
    if apply:
        await session.commit()
    else:
        out("Dry run: nothing changed. Add --apply to do it.")
    return counts


async def _main(argv: Sequence[str] | None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.scripts.mirror")
    sub = parser.add_subparsers(dest="command", required=True)
    conf = sub.add_parser("configure")
    conf.add_argument("--tenant", required=True)
    conf.add_argument("--endpoint")
    conf.add_argument("--off", action="store_true")
    conf.add_argument("--written-text", choices=["on", "off"])
    sync = sub.add_parser("sync")
    sync.add_argument("--tenant", required=True)
    sync.add_argument("--form")
    sync.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    settings = get_settings()
    engine = create_engine(settings.build_database_url)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            bind_scope(session, SYSTEM)
            try:
                if args.command == "configure":
                    text = args.written_text
                    await configure(
                        session,
                        args.tenant,
                        endpoint=args.endpoint,
                        off=args.off,
                        written_text=None if text is None else text == "on",
                    )
                else:
                    await resync(
                        session,
                        load_catalog(Path(settings.trainer_content_dir)),
                        args.tenant,
                        form_id=args.form,
                        apply=args.apply,
                        issuer=settings.passback_issuer,
                        now=datetime.now(UTC),
                    )
            except MirrorCommandError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return FAILED
    finally:
        await engine.dispose()
    return OK


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(_main(argv))


if __name__ == "__main__":
    sys.exit(main())
