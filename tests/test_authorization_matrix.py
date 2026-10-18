"""Who may call what.

Two layers. `test_every_route_declares_an_access_level` walks the application's
real routes and fails for one that has no access level, or whose level differs
from the table below; adding a route means adding it here, on purpose. The
matrix then calls EVERY route as every kind of caller and checks the status:
no token, a student, an editor, an editor of another course, of another tenant,
a disabled tenant, an expired or revoked or made-up token, legacy mode on and off.
"""

import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine

from app.auth.dependencies import (
    GUARDS,
    bearer_route,
    editor_route,
    moodle_source_route,
    public_route,
    student_route,
)
from app.auth.tokens import hash_token
from app.core.config import Settings, get_settings
from app.core.paths import API_PREFIX
from app.enums import Role, TenantStatus
from app.main import app as fastapi_app
from app.models import AccessToken
from tests.editor_client import EditorClient
from tests.tenant_helpers import insert_tenant, insert_token
from tests.test_publications_api import make_map

PUBLIC, BEARER, STUDENT, EDITOR = "public", "bearer", "student", "editor"
LEVELS = {
    public_route: PUBLIC,
    bearer_route: BEARER,
    student_route: STUDENT,
    editor_route: EDITOR,
}

# (method, path under /api/v1) -> (access level, answers from Moodle's web services)
ROUTES: dict[tuple[str, str], tuple[str, bool]] = {
    ("GET", "/health"): (PUBLIC, False),
    ("GET", "/health/ready"): (PUBLIC, False),
    ("POST", "/auth/launch"): (PUBLIC, False),
    ("GET", "/auth/me"): (BEARER, False),
    ("GET", "/assets/{asset_id}"): (PUBLIC, False),
    ("GET", "/assets/{asset_id}/thumb"): (PUBLIC, False),
    ("POST", "/assets"): (EDITOR, False),
    ("GET", "/assets"): (EDITOR, False),
    ("PATCH", "/assets/{asset_id}"): (EDITOR, False),
    ("DELETE", "/assets/{asset_id}"): (EDITOR, False),
    ("GET", "/skins"): (EDITOR, False),
    ("POST", "/skins"): (EDITOR, False),
    ("PATCH", "/skins/{skin_id}"): (EDITOR, False),
    ("DELETE", "/skins/{skin_id}"): (EDITOR, False),
    ("GET", "/view3d/scenes"): (EDITOR, False),
    ("GET", "/course-maps"): (EDITOR, False),
    ("POST", "/course-maps"): (EDITOR, False),
    ("PUT", "/course-maps/order"): (EDITOR, False),
    ("GET", "/course-maps/{course_map_id}"): (EDITOR, False),
    ("PATCH", "/course-maps/{course_map_id}"): (EDITOR, False),
    ("DELETE", "/course-maps/{course_map_id}"): (EDITOR, False),
    ("PUT", "/course-maps/{course_map_id}/appearance"): (EDITOR, False),
    ("PUT", "/course-maps/{course_map_id}/view3d"): (EDITOR, False),
    ("GET", "/course-maps/{course_map_id}/activities"): (EDITOR, True),
    ("GET", "/course-maps/{course_map_id}/resolved"): (EDITOR, True),
    ("POST", "/course-maps/{course_map_id}/bubbles"): (EDITOR, False),
    ("PUT", "/course-maps/{course_map_id}/bubbles/order"): (EDITOR, False),
    ("PATCH", "/course-maps/{course_map_id}/bubbles/{bubble_id}"): (EDITOR, False),
    ("DELETE", "/course-maps/{course_map_id}/bubbles/{bubble_id}"): (EDITOR, False),
    ("GET", "/course-maps/{course_map_id}/publication-state"): (EDITOR, False),
    ("POST", "/course-maps/{course_map_id}/publish"): (EDITOR, False),
    ("GET", "/course-maps/{course_map_id}/publications"): (EDITOR, False),
    ("POST", "/course-maps/{course_map_id}/discard-changes"): (EDITOR, False),
    ("POST", "/course-maps/{course_map_id}/publications/{number}/restore"): (
        EDITOR,
        False,
    ),
    ("GET", "/published/course-maps"): (STUDENT, False),
    ("GET", "/published/course-maps/{course_map_id}"): (STUDENT, False),
    ("GET", "/published/course-maps/{course_map_id}/resolved"): (STUDENT, True),
}


# --- walking the real routes ----------------------------------------------------------


def _effective_routes(node: Any) -> Iterator[Any]:
    """Every API route of the application with its final path and dependencies.

    FastAPI includes routers lazily; this reads the effective routes it builds
    (each has `path`, `dependant` and the original route's `methods`).
    """
    for child in getattr(node, "routes", None) or node.effective_candidates():
        if hasattr(child, "effective_candidates"):
            yield from _effective_routes(child)
        elif isinstance(getattr(child, "original_route", None), APIRoute) or isinstance(
            child, APIRoute
        ):
            yield child


def _calls(dependant: Dependant) -> Iterator[Callable[..., Any]]:
    for sub in dependant.dependencies:
        if sub.call is not None:
            yield sub.call
        yield from _calls(sub)


def application_routes(app: FastAPI) -> dict[tuple[str, str], list[Callable[..., Any]]]:
    found: dict[tuple[str, str], list[Callable[..., Any]]] = {}
    for route in _effective_routes(app):
        path = route.path.removeprefix(API_PREFIX)
        methods = getattr(route, "methods", None) or route.original_route.methods
        for method in methods:
            found[(method, path)] = list(_calls(route.dependant))
    return found


def test_every_route_declares_exactly_one_access_level() -> None:
    routes = application_routes(fastapi_app)
    assert len(routes) >= len(ROUTES)  # the walk really found the routes

    problems = []
    for key, calls in sorted(routes.items()):
        declared = [LEVELS[c] for c in calls if c in GUARDS]
        if len(declared) != 1:
            problems.append(f"{key}: declares {declared or 'no access level'}")
    assert problems == []


def test_the_access_levels_are_the_ones_in_the_table() -> None:
    routes = application_routes(fastapi_app)
    assert set(routes) == set(ROUTES), (
        "routes and the table disagree: "
        f"new={sorted(set(routes) - set(ROUTES))} gone={sorted(set(ROUTES) - set(routes))}"
    )
    wrong = []
    for key, calls in routes.items():
        level = next(LEVELS[c] for c in calls if c in GUARDS)
        moodle = moodle_source_route in calls
        if (level, moodle) != ROUTES[key]:
            wrong.append((key, (level, moodle), ROUTES[key]))
    assert wrong == []


# --- the matrix ----------------------------------------------------------------------

COURSE = 7


@dataclass
class World:
    """Callers of the matrix and one real map to aim at."""

    clients: dict[str, EditorClient]
    map_id: str
    legacy_on: bool = False


# Bodies that pass validation, so a foreign caller is turned away by the lookup
# (404) and not by the schema (422).
NULL_BODY = {("PUT", "/course-maps/{course_map_id}/view3d")}
BODIES: dict[tuple[str, str], dict[str, Any]] = {
    ("PATCH", "/course-maps/{course_map_id}"): {"title": "x"},
    ("PUT", "/course-maps/{course_map_id}/appearance"): {
        "settings": {},
        "defaultSkinId": None,
        "skinRules": [],
    },
    ("POST", "/course-maps/{course_map_id}/bubbles"): {
        "activityId": 5,
        "x": 0.1,
        "y": 0.1,
    },
    ("PUT", "/course-maps/{course_map_id}/bubbles/order"): {"bubbleIds": []},
    ("PATCH", "/course-maps/{course_map_id}/bubbles/{bubble_id}"): {
        "x": 0.2,
        "y": 0.2,
    },
    ("POST", "/course-maps/{course_map_id}/publish"): {"draftHash": "0" * 64},
}


def _path(template: str, map_id: str) -> str:
    return template.format(
        course_map_id=map_id,
        bubble_id=uuid.uuid4(),
        asset_id=uuid.uuid4(),
        skin_id=uuid.uuid4(),
        number=1,
    )


async def _call(
    client: httpx.AsyncClient, method: str, template: str, map_id: str
) -> int:
    kwargs: dict[str, Any] = {"headers": {"If-Match": '"1"'}}
    if (method, template) in NULL_BODY:
        kwargs["content"] = b"null"
        kwargs["headers"]["Content-Type"] = "application/json"
    elif method in ("POST", "PUT", "PATCH"):
        kwargs["json"] = BODIES.get((method, template), {})
    response = await client.request(method, _path(template, map_id), **kwargs)
    return response.status_code


@pytest.fixture
async def world(
    engine: AsyncEngine, make_client: Callable[[str | None], EditorClient]
) -> AsyncIterator[World]:
    alpha = await insert_tenant(engine, "alpha")
    beta = await insert_tenant(engine, "beta")
    off = await insert_tenant(engine, "off", status=TenantStatus.DISABLED)

    editor = await insert_token(engine, alpha, COURSE, Role.EDITOR)
    student = await insert_token(engine, alpha, COURSE, Role.STUDENT)
    other_course = await insert_token(engine, alpha, COURSE + 1, Role.EDITOR)
    other_course_student = await insert_token(engine, alpha, COURSE + 1, Role.STUDENT)
    other_tenant = await insert_token(engine, beta, COURSE, Role.EDITOR)
    other_tenant_student = await insert_token(engine, beta, COURSE, Role.STUDENT)
    disabled = await insert_token(engine, off, COURSE, Role.EDITOR)
    expired = await insert_token(
        engine, alpha, COURSE, Role.EDITOR, ttl=timedelta(seconds=-1)
    )
    revoked = await insert_token(engine, alpha, COURSE, Role.EDITOR)
    async with engine.begin() as conn:
        await conn.execute(
            delete(AccessToken).where(AccessToken.token_hash == hash_token(revoked))
        )

    clients = {
        "anonymous": make_client(None),
        "editor": make_client(editor),
        "student": make_client(student),
        "other_course": make_client(other_course),
        "other_course_student": make_client(other_course_student),
        "other_tenant": make_client(other_tenant),
        "other_tenant_student": make_client(other_tenant_student),
        "disabled_tenant": make_client(disabled),
        "expired": make_client(expired),
        "revoked": make_client(revoked),
        "made_up": make_client("x" * 43),
    }
    created = await make_map(clients["editor"], course=COURSE)
    yield World(clients, created["id"])


def _legacy(on: bool) -> Callable[[], Settings]:
    base = get_settings().model_copy(update={"legacy_anonymous_mode": on})
    return lambda: base


@pytest.fixture(autouse=True)
def _restore_settings() -> Iterator[None]:
    yield
    fastapi_app.dependency_overrides.pop(get_settings, None)


def _passed_authorization(status: int) -> bool:
    """Not turned away: the route was reached (what it then answers is the route's)."""
    return status not in (401, 403)


ROUTE_IDS = [f"{m} {p}" for (m, p) in ROUTES]


@pytest.mark.parametrize(("method", "template"), list(ROUTES), ids=ROUTE_IDS)
@pytest.mark.parametrize("legacy_on", [False, True], ids=["legacy-off", "legacy-on"])
async def test_who_gets_in(
    world: World, method: str, template: str, legacy_on: bool
) -> None:
    fastapi_app.dependency_overrides[get_settings] = _legacy(legacy_on)
    level, moodle = ROUTES[(method, template)]

    async def call(who: str) -> int:
        return await _call(world.clients[who], method, template, world.map_id)

    if (method, template) == ("POST", "/auth/launch"):
        # Public: it never looks at a bearer token; its own 401 is `launch_invalid`.
        for who in world.clients:
            assert await call(who) == 401
        return

    # --- not signed in -------------------------------------------------------------
    anonymous = await call("anonymous")
    if level == PUBLIC:
        assert _passed_authorization(anonymous)
    elif level == BEARER or not legacy_on:
        assert anonymous == 401
    else:
        assert _passed_authorization(anonymous)  # the legacy anonymous editor

    # --- tokens that are not good (legacy mode never rescues them) ------------------
    for who in ("made_up", "expired", "revoked", "disabled_tenant"):
        status = await call(who)
        if level == PUBLIC:
            assert _passed_authorization(status), who
        else:
            assert status == 401, who

    # --- roles ---------------------------------------------------------------------
    student, editor = await call("student"), await call("editor")
    assert _passed_authorization(editor)
    if level == EDITOR:
        assert student == 403
    else:
        assert _passed_authorization(student)

    # --- the Moodle-backed routes belong to the legacy tenant only -----------------
    if moodle:
        assert editor == 501
        assert student in (501, 403)


FOREIGN = ["other_course", "other_tenant"]
FOREIGN_STUDENT = ["other_course_student", "other_tenant_student"]
MAP_ROUTES = [
    (m, p)
    for (m, p), (level, moodle) in ROUTES.items()
    if "{course_map_id}" in p and not moodle
]


@pytest.mark.parametrize(
    ("method", "template"), MAP_ROUTES, ids=[f"{m} {p}" for m, p in MAP_ROUTES]
)
async def test_other_courses_and_tenants_get_404_never_403(
    world: World, method: str, template: str
) -> None:
    level, _ = ROUTES[(method, template)]
    for who in FOREIGN + (FOREIGN_STUDENT if level == STUDENT else []):
        assert await _call(world.clients[who], method, template, world.map_id) == 404, (
            who
        )


@pytest.mark.parametrize(
    ("method", "template"), [r for r in ROUTES if ROUTES[r][1]], ids=str
)
async def test_moodle_routes_do_not_exist_for_plugin_tenants(
    world: World, method: str, template: str
) -> None:
    for who in ("editor", "other_course", "other_tenant"):
        assert await _call(world.clients[who], method, template, world.map_id) == 501, (
            who
        )
    body = (await world.clients["editor"].get(_path(template, world.map_id))).json()
    assert body["detail"]["code"] == "moodle_source_not_provided"


async def test_legacy_mode_still_reaches_the_moodle_routes(world: World) -> None:
    fastapi_app.dependency_overrides[get_settings] = _legacy(True)
    r = await world.clients["anonymous"].get(f"/course-maps/{world.map_id}/activities")
    assert r.status_code != 501
    assert r.status_code in (200, 404, 502, 503, 504)  # whatever Moodle answers


async def test_a_token_wins_over_legacy_mode(world: World) -> None:
    """With legacy mode on, a signed-in student is still just a student."""
    fastapi_app.dependency_overrides[get_settings] = _legacy(True)
    r = await world.clients["student"].get("/course-maps")
    assert r.status_code == 403


def test_the_default_for_legacy_mode_follows_the_environment() -> None:
    base = get_settings()
    assert base.model_copy(
        update={"app_env": "dev", "legacy_anonymous_mode": None}
    ).legacy_mode
    assert not base.model_copy(
        update={"app_env": "production", "legacy_anonymous_mode": None}
    ).legacy_mode
    assert base.model_copy(
        update={"app_env": "production", "legacy_anonymous_mode": True}
    ).legacy_mode
    assert not base.model_copy(
        update={"app_env": "dev", "legacy_anonymous_mode": False}
    ).legacy_mode
