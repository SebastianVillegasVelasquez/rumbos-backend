import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.api.errors import register_exception_handlers
from app.api.routes import (
    assets,
    auth,
    course_map,
    health,
    publications,
    published,
    skins,
    view3d,
)
from app.api.security_headers import NoStoreForAuth
from app.assets.fingerprint import (
    StorageDegradedError,
    StorageState,
    StorageUnwritableError,
    check_storage,
)
from app.core.config import Settings, get_settings
from app.core.database import create_engine
from app.core.paths import API_PREFIX
from app.moodle.cache import CachedMoodleClient
from app.moodle.client import HttpMoodleClient
from app.repositories.sqlalchemy.app_meta_repository import SqlAlchemyAppMetaRepository
from app.repositories.sqlalchemy.asset_repository import SqlAlchemyAssetRepository
from app.tenancy.scope import SYSTEM, bind_scope

logger = logging.getLogger(__name__)


async def verify_storage(engine: AsyncEngine, settings: Settings) -> StorageState:
    """The start-up check of `app.assets.fingerprint`. A directory that cannot
    be written always stops the start; a mismatch stops it only with
    `STORAGE_FAIL_FAST`, and otherwise leaves the app up but degraded."""
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            bind_scope(session, SYSTEM)  # every tenant's assets count
            report = await check_storage(
                SqlAlchemyAssetRepository(session),
                SqlAlchemyAppMetaRepository(session),
                Path(settings.assets_dir),
                fail_fast=settings.storage_fail_fast,
            )
    except (StorageUnwritableError, StorageDegradedError):
        raise
    except Exception:
        # No answer is not "fine": uploads stay refused until a start can tell.
        logger.exception(
            "Could not verify the asset storage against the database (were "
            "the migrations run?); treating it as degraded"
        )
        if settings.storage_fail_fast:
            raise
        return StorageState.DEGRADED
    return report.state


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    engine = create_engine(settings.build_database_url)
    app.state.db_engine = engine
    try:
        app.state.storage_state = await verify_storage(engine, settings)
    except BaseException:
        await engine.dispose()
        raise
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
    application = FastAPI(
        title="Rumbos Backend",
        lifespan=lifespan,
        # The documentation lives under the prefix too, so one proxy rule
        # (`/api/`) covers the whole service.
        openapi_url=f"{API_PREFIX}/openapi.json",
        docs_url=f"{API_PREFIX}/docs",
        redoc_url=f"{API_PREFIX}/redoc",
        swagger_ui_oauth2_redirect_url=f"{API_PREFIX}/docs/oauth2-redirect",
    )
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
            # `If-Match` carries the revision/version a write is based on;
            # `ETag` is how the frontend reads it from single-resource responses.
            # `Range`/`If-Range`: ranged playback of audio fetched cross-origin.
            allow_headers=[
                "Content-Type",
                "Authorization",
                "If-Match",
                "Range",
                "If-Range",
            ],
            # `Content-Range` and `Accept-Ranges` are not exposed to scripts by
            # default; a player that fetches ranges needs both.
            expose_headers=["ETag", "Content-Range", "Accept-Ranges", "Content-Length"],
        )
    application.add_middleware(NoStoreForAuth)
    register_exception_handlers(application)
    application.include_router(health.router, prefix=API_PREFIX)
    application.include_router(auth.router, prefix=API_PREFIX)
    application.include_router(course_map.router, prefix=API_PREFIX)
    application.include_router(publications.router, prefix=API_PREFIX)
    application.include_router(published.router, prefix=API_PREFIX)
    application.include_router(assets.router, prefix=API_PREFIX)
    application.include_router(skins.router, prefix=API_PREFIX)
    application.include_router(view3d.router, prefix=API_PREFIX)
    return application


app = create_app()
