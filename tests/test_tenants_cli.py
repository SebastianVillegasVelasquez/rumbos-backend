"""`python -m app.scripts.tenants`, driven through `execute` against real Postgres."""

import time
from collections.abc import Callable
from pathlib import Path

import jwt
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.launch import LaunchSettings
from app.enums import KeyStatus, TenantStatus
from app.repositories.sqlalchemy.tenant_repository import SqlAlchemyTenantRepository
from app.scripts import tenants as cli
from app.tenancy.validation import load_public_key
from tests.rsa_keys import keypair

ISSUER = "https://moodle.uni.edu"


class Run:
    """Runs one command line and keeps what it printed."""

    def __init__(self, session: AsyncSession) -> None:
        self.repo = SqlAlchemyTenantRepository(session)
        self.out: list[str] = []
        self.err: list[str] = []

    async def __call__(self, *argv: str) -> int:
        args = cli.build_parser().parse_args(list(argv))
        if getattr(args, "asset_quota_bytes", 0) is None:
            args.asset_quota_bytes = 1000
        return await cli.execute(self.repo, args, self.out.append, self.err.append)


@pytest.fixture
def run(session: AsyncSession) -> Run:
    return Run(session)


@pytest.fixture
def pubkey(tmp_path: Path) -> Path:
    path = tmp_path / "uni.pub.pem"
    path.write_text(keypair()[1])
    return path


async def _add(run: Run, pubkey: Path, *extra: str) -> int:
    return await run(
        "add",
        "--name",
        "Uni",
        "--issuer",
        ISSUER,
        "--host-origin",
        ISSUER,
        "--public-key-file",
        str(pubkey),
        *extra,
    )


async def test_add_registers_the_tenant_with_a_derived_kid(
    run: Run, pubkey: Path
) -> None:
    assert await _add(run, pubkey) == 0

    tenant = await run.repo.get_by_issuer(ISSUER)
    assert tenant is not None
    assert tenant.asset_quota_bytes == 1000
    keys = await run.repo.list_keys(tenant.id)
    assert [k.kid for k in keys] == [load_public_key(keypair()[1])[1]]
    assert any(line.strip().startswith("kid:") for line in run.out)


async def test_add_accepts_an_explicit_kid(run: Run, pubkey: Path) -> None:
    assert await _add(run, pubkey, "--kid", "main-1") == 0
    tenant = await run.repo.get_by_issuer(ISSUER)
    assert tenant is not None
    assert await run.repo.active_key(tenant.id, "main-1") is not None


async def test_add_refuses_a_duplicate_issuer(run: Run, pubkey: Path) -> None:
    assert await _add(run, pubkey) == 0
    assert await _add(run, pubkey) == 1
    assert "already registered" in run.err[-1]


@pytest.mark.parametrize(
    ("issuer", "origin"),
    [
        ("http://moodle.uni.edu", ISSUER),
        (ISSUER + "/", ISSUER),
        (ISSUER, "http://moodle.uni.edu"),
        (ISSUER, ISSUER + "/path"),
    ],
)
async def test_add_validates_urls(
    run: Run, pubkey: Path, issuer: str, origin: str
) -> None:
    code = await run(
        "add",
        "--name",
        "Uni",
        "--issuer",
        issuer,
        "--host-origin",
        origin,
        "--public-key-file",
        str(pubkey),
    )
    assert code == 1
    assert await run.repo.get_by_issuer(ISSUER) is None


async def test_add_refuses_a_weak_or_missing_or_private_key(
    run: Run, tmp_path: Path
) -> None:
    weak = tmp_path / "weak.pem"
    weak.write_text(keypair("small", 1024)[1])
    private = tmp_path / "private.pem"
    private.write_text(keypair()[0])
    for path in (weak, private, tmp_path / "missing.pem"):
        assert await _add(run, path) == 1
    assert await run.repo.get_by_issuer(ISSUER) is None


async def test_disable_and_enable(run: Run, pubkey: Path) -> None:
    await _add(run, pubkey)
    assert await run("disable", "--issuer", ISSUER) == 0
    tenant = await run.repo.get_by_issuer(ISSUER)
    assert tenant is not None
    assert tenant.status is TenantStatus.DISABLED
    assert await run("enable", "--issuer", ISSUER) == 0
    tenant = await run.repo.get_by_issuer(ISSUER)
    assert tenant is not None
    assert tenant.status is TenantStatus.ACTIVE


async def test_an_unknown_issuer_is_an_error(run: Run) -> None:
    assert await run("disable", "--issuer", "https://nobody.example.com") == 1
    assert "no tenant" in run.err[-1]


async def test_key_rotation(run: Run, pubkey: Path, tmp_path: Path) -> None:
    await _add(run, pubkey, "--kid", "old")
    new = tmp_path / "new.pem"
    new.write_text(keypair("rotated")[1])

    assert (
        await run(
            "add-key",
            "--issuer",
            ISSUER,
            "--public-key-file",
            str(new),
            "--kid",
            "new",
        )
        == 0
    )
    assert await run("revoke-key", "--issuer", ISSUER, "--kid", "old") == 0

    tenant = await run.repo.get_by_issuer(ISSUER)
    assert tenant is not None
    statuses = {k.kid: k.status for k in await run.repo.list_keys(tenant.id)}
    assert statuses == {"old": KeyStatus.REVOKED, "new": KeyStatus.ACTIVE}
    assert await run("revoke-key", "--issuer", ISSUER, "--kid", "ghost") == 1
    assert (
        await run(
            "add-key", "--issuer", ISSUER, "--public-key-file", str(new), "--kid", "old"
        )
        == 1
    )


async def test_list_shows_each_tenant(run: Run, pubkey: Path) -> None:
    await _add(run, pubkey)
    run.out.clear()
    assert await run("list") == 0
    assert any(ISSUER in line and "active" in line for line in run.out)
    assert run.out[-1].endswith("tenant(s)")


async def test_frame_ancestors_lists_the_active_origins(run: Run, pubkey: Path) -> None:
    run.out.clear()
    assert await run("frame-ancestors") == 0
    assert run.out == ["Content-Security-Policy: frame-ancestors 'none'"]

    await _add(run, pubkey)
    run.out.clear()
    await run("frame-ancestors")
    assert run.out == [f"Content-Security-Policy: frame-ancestors {ISSUER}"]

    await run("disable", "--issuer", ISSUER)
    run.out.clear()
    await run("frame-ancestors")
    assert run.out == ["Content-Security-Policy: frame-ancestors 'none'"]


def test_the_policy_joins_several_origins() -> None:
    policy = cli.frame_ancestors_policy(["https://a.example", "https://b.example"])
    assert policy == "frame-ancestors https://a.example https://b.example"


def test_the_command_requires_a_subcommand() -> None:
    with pytest.raises(SystemExit) as caught:
        cli.main([])
    assert caught.value.code == 2


def test_a_non_positive_quota_is_rejected() -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(
            [
                "add",
                "--name",
                "x",
                "--issuer",
                ISSUER,
                "--host-origin",
                ISSUER,
                "--public-key-file",
                "k.pem",
                "--asset-quota-bytes",
                "0",
            ]
        )


def _stdin(text: str) -> Callable[[], str]:
    return lambda: text + "\n"


async def test_diagnose_names_the_failed_check_and_never_echoes_the_input(
    session: AsyncSession, pubkey: Path
) -> None:
    run = Run(session)
    assert await _add(run, pubkey, "--kid", "kid-1") == 0
    run.out.clear()
    settings = LaunchSettings(
        audience="rumbos", token_ttl_seconds=1800, leeway_seconds=30
    )
    good = jwt.encode(
        {
            "iss": ISSUER,
            "aud": "rumbos",
            "sub": "a" * 32,
            "course": 4,
            "role": "editor",
            "iat": int(time.time()),
            "exp": int(time.time()) + 60,
            "jti": "j" * 32,
        },
        keypair()[0],
        algorithm="RS256",
        headers={"typ": "JWT", "kid": "kid-1"},
    )
    unsigned = good.rsplit(".", 1)[0]  # no signature part
    results = []
    for text in (good, unsigned, "garbage"):
        out: list[str] = []
        args = cli.build_parser().parse_args(["diagnose"])
        code = await cli.execute(
            run.repo,
            args,
            out.append,
            run.err.append,
            read_stdin=_stdin(text),
            launch_settings=settings,
        )
        results.append((code, out))
        assert all(part not in "".join(out + run.err) for part in text.split("."))

    assert results[0] == (0, ["result: ok"])
    assert results[1][0] == 1
    assert results[2] == (1, ["result: malformed"])
    # Nothing was redeemed: the same assertion still diagnoses as ok.
    out = []
    await cli.execute(
        run.repo,
        cli.build_parser().parse_args(["diagnose"]),
        out.append,
        run.err.append,
        read_stdin=_stdin(good),
        launch_settings=settings,
    )
    assert out == ["result: ok"]
