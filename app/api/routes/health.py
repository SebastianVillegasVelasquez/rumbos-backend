import asyncio
import logging

from fastapi import APIRouter, Request, Response, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])

# A database that hangs must answer "not ready" instead of hanging the probe.
_READY_TIMEOUT_SECONDS = 3.0


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness: the process is up and serving. Touches no dependency."""
    return {"status": "ok"}


@router.get("/health/ready")
async def ready(request: Request, response: Response) -> dict[str, str]:
    """Readiness: the database answers `SELECT 1`.

    Deliberately does NOT call Moodle: the app degrades gracefully when Moodle
    is down, so a Moodle outage must not take instances out of rotation.
    """
    engine: AsyncEngine = request.app.state.db_engine
    try:
        async with asyncio.timeout(_READY_TIMEOUT_SECONDS):
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001  any failure means "not ready"
        # Type only: driver messages can embed the connection URL.
        logger.error("Readiness check failed: database error=%s", type(exc).__name__)
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unavailable"}
    return {"status": "ok"}
