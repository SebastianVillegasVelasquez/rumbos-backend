"""`python -m app.scripts.assets`, driven through `execute` against real Postgres:
the curated audio library and the takedown."""

import dataclasses
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.assets.storage import AssetStorage
from app.enums import Role
from app.models import Asset
from app.repositories.sqlalchemy.asset_repository import SqlAlchemyAssetRepository
from app.repositories.sqlalchemy.tenant_repository import SqlAlchemyTenantRepository
from app.scripts import assets as cli
from app.services.asset_service import AssetService
from app.tenancy.scope import SYSTEM, bind_scope
from tests import audio_files as af
from tests.asset_helpers import upload_audio
from tests.conftest import ENABLED
from tests.editor_client import EditorClient
from tests.tenant_helpers import insert_tenant, insert_token

MakeClient = Callable[[str | None], EditorClient]


class Run:
    """Runs one command line in the operator's (system) scope and keeps what it
    printed."""

    def __init__(self, session: AsyncSession, storage: AssetStorage) -> None:
        bind_scope(session, SYSTEM)
        self.assets = SqlAlchemyAssetRepository(session)
        self.tenants = SqlAlchemyTenantRepository(session)
        self.service = AssetService(self.assets, storage, ENABLED)
        self.out: list[str] = []
        self.err: list[str] = []

    async def __call__(self, *argv: str) -> int:
        args = cli.build_parser().parse_args(list(argv))
        return await cli.execute(
            self.assets,
            self.service,
            self.tenants,
            args,
            self.out.append,
            self.err.append,
        )

    @property
    def text(self) -> str:
        return "\n".join(self.out)


@pytest.fixture
async def run(engine: AsyncEngine, storage: AssetStorage) -> AsyncIterator[Run]:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        yield Run(session, storage)


def song(tmp_path: Path, data: bytes | None = None, name: str = "song.mp3") -> Path:
    path = tmp_path / name
    path.write_bytes(data if data is not None else af.tagged_mp3(40))
    return path


async def library_ids(client: httpx.AsyncClient) -> list[str]:
    r = await client.get("/assets?kind=audio&scope=library")
    return [i["id"] for i in r.json()["items"]]


async def imported(run: Run, tmp_path: Path, **extra: str) -> str:
    code = await run(
        "import-audio",
        "--file",
        str(song(tmp_path)),
        "--title",
        extra.get("title", "Brisa"),
        "--credit",
        "Ana Perez, CC BY 4.0",
        "--license",
        "CC BY 4.0",
    )
    assert code == 0, run.err
    return next(line.split()[-1] for line in run.out if line.strip().startswith("id:"))


# --- import-audio --------------------------------------------------------------


async def test_import_makes_a_library_track_every_tenant_can_use(
    run: Run, client: httpx.AsyncClient, tmp_path: Path, engine: AsyncEngine
) -> None:
    asset_id = await imported(run, tmp_path)

    assert await library_ids(client) == [asset_id]
    served = await client.get(f"/assets/{asset_id}")
    assert served.status_code == 200
    assert served.headers["content-type"] == "audio/mpeg"
    assert served.content == af.mp3(40)  # tags and trailers are gone
    [item] = (await client.get("/assets?kind=audio&scope=library")).json()["items"]
    assert (item["title"], item["credit"], item["license"]) == (
        "Brisa",
        "Ana Perez, CC BY 4.0",
        "CC BY 4.0",
    )
    async with AsyncSession(engine) as s:
        bind_scope(s, SYSTEM)
        row = await s.get(Asset, uuid.UUID(asset_id))
        assert row is not None
        assert row.tenant_id is None and row.uploaded_by_sub is None


async def test_import_does_not_depend_on_the_upload_switch_or_a_quota(
    run: Run, storage: AssetStorage, tmp_path: Path
) -> None:
    run.service = AssetService(
        run.assets,
        storage,
        dataclasses.replace(
            ENABLED,
            audio_uploads_enabled=False,
            uploads_enabled=False,
            max_total_bytes=1,
        ),
    )

    code = await run(
        "import-audio", "--file", str(song(tmp_path)), "--title", "T", "--credit", "C"
    )

    assert code == 0, run.err
    assert len(await run.assets.list_audio()) == 1


async def test_import_applies_the_same_checks_as_an_upload(
    run: Run, storage: AssetStorage, tmp_path: Path
) -> None:
    run.service = AssetService(
        run.assets, storage, dataclasses.replace(ENABLED, max_audio_bytes=1_000_000)
    )
    bad = song(tmp_path, b"not audio" * 100, "bad.mp3")
    drm = song(tmp_path, af.m4a(drm=True), "drm.m4a")
    long_ = song(tmp_path, af.mp3(9000, af.FRAME_8K), "long.mp3")
    big = song(tmp_path, bytes(1_100_000), "big.mp3")

    for path, fragment in (
        (bad, "not a valid"),
        (drm, "protected"),
        (long_, "longer than 600"),
        (big, "larger than"),
    ):
        run.err.clear()
        code = await run(
            "import-audio", "--file", str(path), "--title", "T", "--credit", "C"
        )
        assert code == 1 and fragment in run.err[0], (path, run.err)
    assert (await run.assets.list_audio()) == []


@pytest.mark.parametrize(
    "extra",
    [["--credit", "  "], ["--title", ""]],
)
async def test_import_needs_a_title_and_a_credit(
    run: Run, tmp_path: Path, extra: list[str]
) -> None:
    args = {"--title": "T", "--credit": "C"}
    args.update(dict(zip(extra[::2], extra[1::2], strict=True)))
    argv = [x for pair in args.items() for x in pair]

    code = await run("import-audio", "--file", str(song(tmp_path)), *argv)

    assert code == 1 and run.err
    assert (await run.assets.list_audio()) == []


async def test_import_of_a_missing_file_says_so(run: Run, tmp_path: Path) -> None:
    code = await run(
        "import-audio",
        "--file",
        str(tmp_path / "nope.mp3"),
        "--title",
        "T",
        "--credit",
        "C",
    )

    assert code == 1 and "cannot read" in run.err[0]


async def test_importing_the_same_audio_twice_changes_nothing(
    run: Run, tmp_path: Path
) -> None:
    first = await imported(run, tmp_path)
    run.out.clear()

    code = await run(
        "import-audio",
        "--file",
        str(song(tmp_path, af.mp3(40), "again.mp3")),  # same audio, no tags
        "--title",
        "Otro",
        "--credit",
        "Otro",
    )

    assert code == 0 and "Already in the library" in run.text and first in run.text
    assert len(await run.assets.list_audio()) == 1


async def test_an_import_does_not_collide_with_a_tenants_copy_of_the_same_audio(
    run: Run,
    engine: AsyncEngine,
    make_client: MakeClient,
    tmp_path: Path,
    storage: object,
) -> None:
    tenant = await insert_tenant(engine, "alpha")
    editor = make_client(await insert_token(engine, tenant, 7, Role.EDITOR))
    own = (await upload_audio(editor, af.mp3(40))).json()

    asset_id = await imported(run, tmp_path)

    assert asset_id != own["id"]
    assert len(await run.assets.list_audio()) == 2


# --- list ----------------------------------------------------------------------


async def test_list_shows_tenants_uploaders_declarations_and_state(
    run: Run,
    engine: AsyncEngine,
    make_client: MakeClient,
    tmp_path: Path,
    storage: object,
) -> None:
    tenant = await insert_tenant(engine, "alpha")
    editor = make_client(await insert_token(engine, tenant, 7, Role.EDITOR))
    uploaded = (
        await upload_audio(editor, af.mp3(41), title="Mia", license="CC0")
    ).json()
    library = await imported(run, tmp_path)
    await run("quarantine", library, "--reason", "complaint")
    run.out.clear()

    assert await run("list") == 0

    lines = [line for line in run.out if line.startswith(("0", "1", "2"))]
    by_id = {line.split()[0]: line for line in lines}
    mine, shared = by_id[uploaded["id"]], by_id[library]
    assert "https://alpha.example.com" in mine
    assert "uploader=" + "b" * 32 in mine
    assert f"declared={datetime.now(UTC):%Y-%m-%d}" in mine
    assert "active" in mine
    assert "library" in shared and "QUARANTINED" in shared and "uploader=-" in shared
    assert any("complaint" in line for line in run.out)
    assert run.out[-1] == "2 audio track(s)"


# --- quarantine and release ----------------------------------------------------


async def test_quarantine_takes_a_track_down_everywhere_and_release_restores_it(
    run: Run, client: httpx.AsyncClient, tmp_path: Path
) -> None:
    asset_id = await imported(run, tmp_path)
    assert (await client.get(f"/assets/{asset_id}")).status_code == 200

    assert (
        await run("quarantine", asset_id, "--reason", "  rights   holder\ncomplaint ")
        == 0
    )

    assert "rights holder complaint" in run.text
    assert (await client.get(f"/assets/{asset_id}")).status_code == 404
    assert (
        await client.get(f"/assets/{asset_id}", headers={"Range": "bytes=0-9"})
    ).status_code == 404
    assert await library_ids(client) == []
    assert (await client.get("/assets?kind=audio&scope=library&q=Brisa")).json()[
        "total"
    ] == 0

    assert await run("release", asset_id) == 0

    assert (await client.get(f"/assets/{asset_id}")).status_code == 200
    assert await library_ids(client) == [asset_id]


async def test_the_quarantine_is_recorded_with_its_reason_and_time(
    run: Run, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    asset_id = await imported(run, tmp_path)
    caplog.set_level("INFO", logger="rumbos.assets")

    await run("quarantine", asset_id, "--reason", "complaint 42")
    await run("release", asset_id)

    record = await run.assets.get_by_id(uuid.UUID(asset_id))
    assert record is not None and record.quarantined_at is None
    events = await run.assets.audit_events(uuid.UUID(asset_id))
    assert [(e.action, e.reason) for e in events] == [
        ("created", "imported to the library"),
        ("quarantined", "complaint 42"),
        ("released", None),
    ]
    assert any(
        "quarantined" in m and asset_id in m and "complaint 42" in m
        for m in caplog.messages
    )
    run.out.clear()
    assert await run("history", asset_id) == 0
    assert run.out[-1] == "3 event(s)"
    assert "complaint 42" in run.text


async def test_quarantine_keeps_its_first_reason_and_time_when_repeated(
    run: Run, tmp_path: Path
) -> None:
    asset_id = await imported(run, tmp_path)
    await run("quarantine", asset_id, "--reason", "first")
    first = await run.assets.get_by_id(uuid.UUID(asset_id))
    run.out.clear()

    code = await run("quarantine", asset_id, "--reason", "second")

    again = await run.assets.get_by_id(uuid.UUID(asset_id))
    assert code == 0 and "already quarantined" in run.text
    assert again is not None and first is not None
    assert (again.quarantined_at, again.quarantine_reason) == (
        first.quarantined_at,
        "first",
    )
    assert len(await run.assets.audit_events(uuid.UUID(asset_id))) == 2  # +created


async def test_release_of_a_free_asset_changes_nothing(
    run: Run, tmp_path: Path
) -> None:
    asset_id = await imported(run, tmp_path)

    assert await run("release", asset_id) == 0
    assert "nothing changed" in run.text


@pytest.mark.parametrize(
    "argv",
    [
        ("quarantine", "not-an-id", "--reason", "x"),
        ("quarantine", str(uuid.uuid4()), "--reason", "x"),
        ("quarantine", str(uuid.uuid4()), "--reason", "   "),
        ("quarantine", str(uuid.uuid4()), "--reason", "x" * 501),
        ("release", "not-an-id"),
        ("release", str(uuid.uuid4())),
    ],
)
async def test_bad_targets_and_reasons_are_errors(
    run: Run, argv: tuple[str, ...]
) -> None:
    assert await run(*argv) == 1 and run.err


async def test_a_tenants_own_upload_can_be_quarantined_too(
    run: Run,
    engine: AsyncEngine,
    make_client: MakeClient,
    storage: object,
) -> None:
    tenant = await insert_tenant(engine, "alpha")
    editor = make_client(await insert_token(engine, tenant, 7, Role.EDITOR))
    created = (await upload_audio(editor, af.mp3(40))).json()

    assert await run("quarantine", created["id"], "--reason", "complaint") == 0

    assert (await editor.get(created["url"])).status_code == 404
    mine = await editor.get("/assets?kind=audio")
    assert mine.json()["items"] == []
    async with AsyncSession(engine) as s:
        bind_scope(s, SYSTEM)
        row = (await s.scalars(select(Asset))).one()
        assert row.quarantine_reason == "complaint"
