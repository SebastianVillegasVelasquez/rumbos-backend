from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.errors import register_exception_handlers
from app.api.routes import assets, course_map, health
from app.core.config import Settings, get_settings
from app.core.database import create_engine
from app.moodle.cache import CachedMoodleClient
from app.moodle.client import HttpMoodleClient


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    engine = create_engine(settings.database_url)
    app.state.db_engine = engine
    # No automatic retries: a Moodle failure surfaces immediately.
    timeout = httpx.Timeout(
        connect=settings.moodle_connect_timeout,
        read=settings.moodle_read_timeout,
        write=settings.moodle_read_timeout,
        pool=settings.moodle_connect_timeout,
    )
    moodle_http = httpx.AsyncClient(timeout=timeout)
    app.state.moodle_http = moodle_http
    # One cache for the whole process, shared by every request.
    app.state.moodle_client = CachedMoodleClient(
        HttpMoodleClient(
            moodle_http, settings.moodle_base_url, settings.moodle_service_token
        ),
        ttl_seconds=settings.moodle_contents_ttl_seconds,
        stale_max_seconds=settings.moodle_stale_max_seconds,
    )
    try:
        yield
    finally:
        await moodle_http.aclose()
        await engine.dispose()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app. `settings` only drives the middleware; the lifespan
    reads its own (resources are created when the server starts)."""
    settings = settings or get_settings()
    application = FastAPI(title="Rumbos Backend", lifespan=lifespan)
    if settings.cors_allowed_origins:
        # Browsers on other origins (the deployed frontend) need this; in
        # development the Vite proxy makes requests same-origin, so the default
        # is no middleware. No cookies or credentials are involved, so
        # credentials stay off.
        application.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allowed_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["Content-Type", "Authorization"],
        )
    register_exception_handlers(application)
    application.include_router(health.router)
    application.include_router(course_map.router)
    application.include_router(assets.router)
    return application


app = create_app()
