"""Lifespan and dependency wiring for the shared Moodle HTTP client."""

import httpx
from fastapi import FastAPI
from starlette.requests import Request

from app.core.config import get_settings
from app.main import app
from app.moodle.client import HttpMoodleClient
from app.moodle.dependencies import get_moodle_client


async def test_lifespan_creates_and_closes_the_http_client() -> None:
    async with app.router.lifespan_context(app):
        http: httpx.AsyncClient = app.state.moodle_http
        settings = get_settings()
        assert not http.is_closed
        assert http.timeout.connect == settings.moodle_connect_timeout
        assert http.timeout.read == settings.moodle_read_timeout
    assert http.is_closed


def test_dependency_builds_a_client_from_app_state() -> None:
    fake_app = FastAPI()
    fake_app.state.moodle_http = httpx.AsyncClient()
    request = Request({"type": "http", "app": fake_app})
    assert isinstance(get_moodle_client(request), HttpMoodleClient)
