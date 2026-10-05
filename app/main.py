from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from app.api.errors import register_exception_handlers
from app.api.routes import course_map
from app.core.config import get_settings
from app.core.database import create_engine


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
    try:
        yield
    finally:
        await moodle_http.aclose()
        await engine.dispose()


app = FastAPI(title="Rumbos Backend", lifespan=lifespan)
register_exception_handlers(app)
app.include_router(course_map.router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
