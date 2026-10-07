"""Lifespan and dependency wiring for the shared Moodle HTTP client and cache."""

import httpx
from fastapi import FastAPI
from starlette.requests import Request

from app.core.config import get_settings
from app.main import app
from app.moodle.cache import CachedMoodleClient
from app.moodle.dependencies import get_contents_provider, get_moodle_client


async def test_lifespan_creates_and_closes_the_http_client() -> None:
    async with app.router.lifespan_context(app):
        http: httpx.AsyncClient = app.state.moodle_http
        settings = get_settings()
        assert not http.is_closed
        assert http.timeout.connect == settings.moodle_connect_timeout
        assert http.timeout.read == settings.moodle_read_timeout
    assert http.is_closed


async def test_lifespan_builds_one_cached_client_with_the_settings() -> None:
    async with app.router.lifespan_context(app):
        client = app.state.moodle_client
        assert isinstance(client, CachedMoodleClient)
        settings = get_settings()
        assert client._ttl == settings.moodle_contents_ttl_seconds
        assert client._stale_max == settings.moodle_stale_max_seconds


def test_dependencies_return_the_one_app_wide_client() -> None:
    fake_app = FastAPI()
    sentinel = object()
    fake_app.state.moodle_client = sentinel
    request = Request({"type": "http", "app": fake_app})
    # Same instance every time: the cache must be shared across requests, and
    # `/activities` and `/resolved` must share it with each other.
    seen = {id(get_moodle_client(request)), id(get_contents_provider(request))}
    assert seen == {id(sentinel)}
