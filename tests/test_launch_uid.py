"""The optional signed `uid` claim (the Moodle user id) and the launch's
tolerance for claims it does not know."""

import logging
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.assessment.service import Caller
from app.auth.principal import Principal
from app.enums import Role
from app.models import AccessToken
from app.models.tenant import LEGACY_TENANT_ID
from tests import test_auth_api as base
from tests.auth_helpers import assertion
from tests.sessions import new_session

settings_for_test = base.settings_for_test
http = base.http
tenant = base.tenant

UID = 987654321


async def launch(c: httpx.AsyncClient, **claims: Any) -> httpx.Response:
    return await c.post("/auth/launch", json={"assertion": assertion(**claims)})


async def stored_uids(engine: AsyncEngine) -> list[int | None]:
    async with new_session(engine) as s:
        return [r.moodle_user_id for r in (await s.scalars(select(AccessToken))).all()]


@pytest.mark.usefixtures("tenant")
async def test_a_signed_uid_is_kept_on_the_token_and_never_returned(
    http: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    r = await launch(http, role="student", uid=UID)
    assert r.status_code == 200
    assert str(UID) not in r.text
    assert await stored_uids(engine) == [UID]
    me = await http.get(
        "/auth/me", headers={"Authorization": f"Bearer {r.json()['accessToken']}"}
    )
    assert str(UID) not in me.text


@pytest.mark.usefixtures("tenant")
async def test_a_launch_without_uid_still_works(
    http: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    assert (await launch(http, role="student")).status_code == 200
    assert await stored_uids(engine) == [None]


@pytest.mark.usefixtures("tenant")
async def test_claims_the_backend_does_not_know_are_not_rejected(
    http: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    r = await launch(
        http,
        role="student",
        uid=UID,
        future_claim="x",
        another={"nested": [1, 2, 3]},
        flag=True,
    )
    assert r.status_code == 200, r.text
    assert await stored_uids(engine) == [UID]


@pytest.mark.usefixtures("tenant")
@pytest.mark.parametrize("bad", ["12", 12.5, True, 0, -3, 2**40, None, [1], {"a": 1}])
async def test_a_malformed_uid_is_ignored_not_refused(
    http: httpx.AsyncClient,
    engine: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
    bad: Any,
) -> None:
    with caplog.at_level(logging.INFO):
        r = await launch(http, role="student", uid=bad)
    assert r.status_code == 200, r.text
    assert await stored_uids(engine) == [None]


@pytest.mark.usefixtures("tenant")
async def test_the_uid_never_reaches_a_log(
    http: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        assert (await launch(http, role="student", uid=UID)).status_code == 200
    assert str(UID) not in caplog.text


def test_the_uid_stays_out_of_the_repr_of_who_is_asking() -> None:
    principal = Principal(LEGACY_TENANT_ID, 7, Role.STUDENT, "s" * 32, UID)
    caller = Caller(LEGACY_TENANT_ID, 7, "s" * 32, False, UID)
    assert str(UID) not in repr(principal) + repr(caller)
