"""Reports exam results to Moodle. For the operator (cron).

    python -m app.scripts.passback sync-run         # every minute or so
    python -m app.scripts.passback sync-reconcile   # daily
    python -m app.scripts.passback status

`sync-run` signs and sends every statement that is due; a Moodle outage only
delays them (retried with backoff, never dropped). `sync-reconcile` asks Moodle
which statements it has and queues again the ones it lacks. `status` counts the
outbox by state. The sender is `PASSBACK_SENDER`: `none` (default) leaves
everything pending until the real HTTP sender exists (it waits for the plugin
audit's decision on the receiving endpoint); `fake` is for development.
"""

import argparse
import asyncio
import sys
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.database import create_engine
from app.models import PassbackOutbox
from app.passback.port import FakeMoodleResultPort, MoodleResultPort
from app.passback.worker import sync_reconcile, sync_run
from app.tenancy.scope import SYSTEM, bind_scope


def port_for(settings: Settings) -> MoodleResultPort | None:
    if settings.passback_sender == "fake":
        return FakeMoodleResultPort()
    return None


async def status(
    session: AsyncSession, out: Callable[[str], None] = print
) -> Counter[str]:
    counts: Counter[str] = Counter(
        (await session.scalars(select(PassbackOutbox.status))).all()
    )
    for key in sorted(counts):
        out(f"{key}: {counts[key]}")
    return counts


async def _main(argv: Sequence[str] | None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.scripts.passback")
    p.add_argument("command", choices=["sync-run", "sync-reconcile", "status"])
    args = p.parse_args(argv)
    settings = get_settings()
    engine = create_engine(settings.build_database_url)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            bind_scope(session, SYSTEM)
            if args.command == "status":
                await status(session)
                return 0
            port = port_for(settings)
            key = settings.passback_signing_key_pem
            if port is None or key is None:
                print(
                    "Nothing sent: set PASSBACK_SENDER and PASSBACK_SIGNING_KEY_PEM "
                    "(the results stay pending)"
                )
                return 0
            now = datetime.now(UTC)
            if args.command == "sync-run":
                await sync_run(
                    session, port, key.get_secret_value(), settings.passback_key_id, now
                )
            else:
                await sync_reconcile(session, port, now)
    finally:
        await engine.dispose()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(_main(argv))


if __name__ == "__main__":
    sys.exit(main())
