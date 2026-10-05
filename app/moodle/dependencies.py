from fastapi import Request

from app.core.config import get_settings
from app.moodle.client import HttpMoodleClient
from app.moodle.protocols import MoodleClient


def get_moodle_client(request: Request) -> MoodleClient:
    """A Moodle client over the app-wide `httpx.AsyncClient`.

    The HTTP client is created and closed by the lifespan; this only wires
    it with the Moodle settings. Cheap to build, so one per request.
    """
    settings = get_settings()
    return HttpMoodleClient(
        request.app.state.moodle_http,
        settings.moodle_base_url,
        settings.moodle_service_token,
    )
