from fastapi import Request

from app.moodle.protocols import CourseContentsProvider, MoodleClient


def get_moodle_client(request: Request) -> MoodleClient:
    """The app-wide Moodle client: HTTP client behind the contents cache.

    Built once by the lifespan (the cache has to outlive requests).
    """
    client: MoodleClient = request.app.state.moodle_client
    return client


def get_contents_provider(request: Request) -> CourseContentsProvider:
    """The same app-wide client, seen through the freshness-aware Protocol."""
    provider: CourseContentsProvider = request.app.state.moodle_client
    return provider
