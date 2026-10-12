"""Every route is served under /api/v1, documentation included."""

import httpx

from app.core.paths import API_PREFIX, ASSETS_PATH
from app.main import create_app


def test_the_prefix_constants() -> None:
    assert API_PREFIX == "/api/v1"
    assert ASSETS_PATH == "/api/v1/assets"


def test_every_route_lives_under_the_prefix() -> None:
    app = create_app()

    served = [p for r in app.routes if (p := getattr(r, "path", ""))]
    documented = set(app.openapi()["paths"])

    assert all(p.startswith(f"{API_PREFIX}/") for p in served), served
    assert documented and all(p.startswith(f"{API_PREFIX}/") for p in documented)
    for expected in (
        "/api/v1/health",
        "/api/v1/health/ready",
        "/api/v1/course-maps",
        "/api/v1/course-maps/{course_map_id}/publication-state",
        "/api/v1/published/course-maps",
        "/api/v1/assets",
        "/api/v1/skins",
    ):
        assert expected in documented


async def test_unprefixed_paths_are_gone_and_prefixed_ones_answer() -> None:
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        assert (await c.get("/health")).status_code == 404
        assert (await c.get("/course-maps")).status_code == 404
        assert (await c.get("/api/v1/health")).json() == {"status": "ok"}
        docs = await c.get("/api/v1/openapi.json")
        assert docs.status_code == 200
        assert "/api/v1/course-maps" in docs.json()["paths"]
        assert (await c.get("/openapi.json")).status_code == 404
