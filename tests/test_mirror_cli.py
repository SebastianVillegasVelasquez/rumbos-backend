"""The operator commands of the mirror: the switch, and the resync that is a dry
run unless told otherwise and leaves the same state when run twice."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from app.models import MirrorDefinition, Tenant
from app.models.tenant import LEGACY_TENANT_ID, LEGACY_TENANT_ISSUER
from app.scripts.mirror import (
    MirrorCommandError,
    check_endpoint,
    configure,
    resync,
)
from app.tenancy.scope import SYSTEM
from app.trainer.catalog import load_catalog
from tests.sessions import new_session

CATALOG = load_catalog(Path(__file__).resolve().parent.parent / "content/trainer")
NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
URL = "https://moodle.test/local/rumbos/mirror.php"
# Every form the catalog serves to students, so publishing one more form never
# makes these tests stale.
PUBLISHED = {
    form.form_id
    for pack in CATALOG.packs.values()
    for form in (pack.served_form(f, False) for f in pack.forms)
    if form is not None
}
N = len(PUBLISHED)


async def tenant(engine: AsyncEngine) -> Tenant:
    async with new_session(engine, SYSTEM) as s:
        found = await s.get(Tenant, LEGACY_TENANT_ID)
        assert found is not None
        return found


async def rows(engine: AsyncEngine) -> list[MirrorDefinition]:
    async with new_session(engine, SYSTEM) as s:
        return list(await s.scalars(select(MirrorDefinition)))


async def sync(
    engine: AsyncEngine, *, apply: bool, form: str | None = None
) -> dict[str, int]:
    async with new_session(engine, SYSTEM) as s:
        return await resync(
            s,
            CATALOG,
            str(LEGACY_TENANT_ID),
            form_id=form,
            apply=apply,
            issuer="rumbos-backend",
            now=NOW,
            out=lambda _: None,
        )


async def enable(engine: AsyncEngine) -> None:
    async with new_session(engine, SYSTEM) as s:
        await configure(
            s,
            LEGACY_TENANT_ISSUER,
            endpoint=URL,
            off=False,
            written_text=None,
            out=lambda _: None,
        )


# --- configure ----------------------------------------------------------------------------


async def test_the_mirror_is_off_and_text_stays_home_until_configured(
    engine: AsyncEngine,
) -> None:
    found = await tenant(engine)
    assert found.mirror_endpoint is None and found.mirror_written_text is False


async def test_configure_sets_and_clears_the_endpoint_and_the_text_switch(
    engine: AsyncEngine,
) -> None:
    await enable(engine)
    assert (await tenant(engine)).mirror_endpoint == URL
    async with new_session(engine, SYSTEM) as s:
        await configure(
            s,
            str(LEGACY_TENANT_ID),
            endpoint=None,
            off=False,
            written_text=True,
            out=lambda _: None,
        )
    found = await tenant(engine)
    assert (found.mirror_endpoint, found.mirror_written_text) == (URL, True)
    async with new_session(engine, SYSTEM) as s:
        await configure(
            s,
            str(LEGACY_TENANT_ID),
            endpoint=None,
            off=True,
            written_text=False,
            out=lambda _: None,
        )
    found = await tenant(engine)
    assert (found.mirror_endpoint, found.mirror_written_text) == (None, False)


@pytest.mark.parametrize(
    "url",
    [
        "http://moodle.test/mirror.php",
        "https://user:pass@moodle.test/mirror.php",
        "https://moodle.test/mirror.php#x",
        "ftp://moodle.test/mirror.php",
        "https:///mirror.php",
        "https://moodle.test/" + "a" * 500,
    ],
)
def test_an_unsafe_endpoint_is_refused(url: str) -> None:
    with pytest.raises(MirrorCommandError):
        check_endpoint(url)


def test_plain_http_is_allowed_only_on_the_machines_own_loopback() -> None:
    assert check_endpoint("http://localhost:8080/mirror.php")
    assert check_endpoint("http://127.0.0.1/mirror.php")
    assert check_endpoint(URL) == URL


async def test_configure_needs_something_to_do_and_a_known_tenant(
    engine: AsyncEngine,
) -> None:
    async with new_session(engine, SYSTEM) as s:
        with pytest.raises(MirrorCommandError):
            await configure(
                s, str(LEGACY_TENANT_ID), endpoint=None, off=False, written_text=None
            )
        with pytest.raises(MirrorCommandError):
            await configure(
                s, str(LEGACY_TENANT_ID), endpoint=URL, off=True, written_text=None
            )
        with pytest.raises(MirrorCommandError):
            await configure(
                s, "https://nobody.example", endpoint=URL, off=False, written_text=None
            )


# --- resync -------------------------------------------------------------------------------


async def test_resync_needs_the_mirror_to_be_on(engine: AsyncEngine) -> None:
    with pytest.raises(MirrorCommandError):
        await sync(engine, apply=True)


async def test_resync_is_a_dry_run_unless_applied(engine: AsyncEngine) -> None:
    await enable(engine)
    counts = await sync(engine, apply=False)
    assert counts == {"created": N, "requeued": 0, "waiting": 0}
    assert await rows(engine) == []


async def test_resync_queues_every_published_form_and_twice_is_the_same(
    engine: AsyncEngine,
) -> None:
    await enable(engine)
    assert await sync(engine, apply=True) == {"created": N, "requeued": 0, "waiting": 0}
    first = await rows(engine)
    assert {r.form_id for r in first} == PUBLISHED
    assert {r.status for r in first} == {"pending"}
    assert all(r.statement["kind"] == "definition" for r in first)
    assert await sync(engine, apply=True) == {"created": 0, "requeued": 0, "waiting": N}
    assert sorted(r.statement["jti"] for r in await rows(engine)) == sorted(
        r.statement["jti"] for r in first
    )


async def test_resync_sends_again_what_was_sent_and_keeps_the_signed_claims(
    engine: AsyncEngine,
) -> None:
    await enable(engine)
    await sync(engine, apply=True)
    before = {r.form_id: dict(r.statement) for r in await rows(engine)}
    async with new_session(engine, SYSTEM) as s:
        await s.execute(
            update(MirrorDefinition).values(status="sent", sent_at=NOW, attempts=2)
        )
        await s.commit()
    assert await sync(engine, apply=False) == {
        "created": 0,
        "requeued": N,
        "waiting": 0,
    }
    assert {r.status for r in await rows(engine)} == {"sent"}
    assert await sync(engine, apply=True) == {"created": 0, "requeued": N, "waiting": 0}
    after = await rows(engine)
    assert {r.status for r in after} == {"pending"} and {r.attempts for r in after} == {
        0
    }
    assert {r.form_id: dict(r.statement) for r in after} == before


async def test_resync_can_be_narrowed_to_one_form(engine: AsyncEngine) -> None:
    await enable(engine)
    counts = await sync(engine, apply=True, form="b2-form-a")
    assert counts == {"created": 1, "requeued": 0, "waiting": 0}
    assert [r.form_id for r in await rows(engine)] == ["b2-form-a"]
    with pytest.raises(MirrorCommandError):
        await sync(engine, apply=True, form="no-such-form")
