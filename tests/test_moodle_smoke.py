"""Opt-in smoke test against the real Moodle configured in `.env`.

Skipped unless `RUN_MOODLE_SMOKE=1`, so CI and normal runs never need the VPN
or a live token. Optionally set `MOODLE_SMOKE_COURSE_ID` to also fetch a
course. Assertions never include the token or response bodies.
"""

import os

import httpx
import pytest

from app.core.config import get_settings
from app.moodle.client import HttpMoodleClient

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_MOODLE_SMOKE") != "1",
    reason="set RUN_MOODLE_SMOKE=1 to run against the real Moodle",
)


async def test_real_moodle_site_info_and_course_contents() -> None:
    settings = get_settings()
    timeout = httpx.Timeout(
        connect=settings.moodle_connect_timeout, read=settings.moodle_read_timeout
    )
    async with httpx.AsyncClient(timeout=timeout) as http:
        client = HttpMoodleClient(
            http, settings.moodle_base_url, settings.moodle_service_token
        )

        info = await client.get_site_info()
        assert info.sitename

        course_id = os.environ.get("MOODLE_SMOKE_COURSE_ID")
        if course_id:
            sections = await client.get_course_contents(int(course_id))
            assert sections
