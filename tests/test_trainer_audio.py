# ruff: noqa: F811
"""The audio route: content types, byte ranges, caching and who may fetch."""

import dataclasses
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from app.main import app
from app.trainer.catalog import Catalog
from app.trainer.dependencies import get_catalog
from app.trainer.pack import load_pack
from tests.test_assessment_api import (  # noqa: F401
    COURSE,
    clock,
    draft_content_is_served,
    env,
    exams,
)

DATA = bytes(range(256)) * 4  # 1024 bytes


@pytest.fixture
def audio_root(tmp_path: Path) -> Iterator[Path]:
    pack = load_pack(Path("content/trainer/en/A1"))
    (tmp_path / "audio").mkdir()
    (tmp_path / "audio/listening.a.01.mp3").write_bytes(DATA)
    fake = dataclasses.replace(pack, root=tmp_path)
    app.dependency_overrides[get_catalog] = lambda: Catalog({("en", "A1"): fake})
    yield tmp_path
    app.dependency_overrides.pop(get_catalog, None)


URL = "/trainer/audio/listening.a.01"


async def test_serves_the_file_with_type_and_cache_headers(
    env: dict[str, Any],
    audio_root: Path,
) -> None:
    await env["setup"]({"assessments": exams()})
    s = await env["student"]()
    r = await s.get(URL)
    assert r.status_code == 200 and r.content == DATA
    assert r.headers["content-type"] == "audio/mpeg"
    assert r.headers["accept-ranges"] == "bytes"
    assert r.headers["cache-control"] == "private, no-cache"
    version = load_pack(Path("content/trainer/en/A1")).content_version
    cached = await s.get(f"{URL}?v={version}")
    assert "immutable" in cached.headers["cache-control"]
    assert version in cached.headers["etag"]


@pytest.mark.parametrize(
    ("header", "start", "end"),
    [
        ("bytes=0-9", 0, 9),
        ("bytes=1000-", 1000, 1023),
        ("bytes=-24", 1000, 1023),
        ("bytes=1020-5000", 1020, 1023),
    ],
)
async def test_a_byte_range_gets_206(
    env: dict[str, Any],
    audio_root: Path,
    header: str,
    start: int,
    end: int,
) -> None:
    await env["setup"]({"assessments": exams()})
    s = await env["student"]()
    r = await s.get(URL, headers={"Range": header})
    assert r.status_code == 206 and r.content == DATA[start : end + 1]
    assert r.headers["content-range"] == f"bytes {start}-{end}/1024"


async def test_an_impossible_range_gets_416(
    env: dict[str, Any], audio_root: Path
) -> None:
    await env["setup"]({"assessments": exams()})
    s = await env["student"]()
    r = await s.get(URL, headers={"Range": "bytes=2000-"})
    assert r.status_code == 416 and r.headers["content-range"] == "bytes */1024"


async def test_unknown_missing_or_disabled_audio_is_404(
    env: dict[str, Any],
    audio_root: Path,
) -> None:
    await env["setup"]({"assessments": exams()})
    s = await env["student"]()
    assert (await s.get("/trainer/audio/nope")).status_code == 404
    assert (await s.get("/trainer/audio/listening.a.02")).status_code == 404  # no file
    other = await env["student"]("zed", course=COURSE + 1)  # feature off there
    assert (await other.get(URL)).status_code == 404


async def test_a_path_in_the_manifest_cannot_leave_the_pack(
    env: dict[str, Any],
    tmp_path: Path,
) -> None:
    pack = load_pack(Path("content/trainer/en/A1"))
    (tmp_path / "secret.mp3").write_bytes(b"x")
    root = tmp_path / "pack"
    root.mkdir()
    entry = pack.audio["listening.a.01"].model_copy(update={"file": "../secret.mp3"})
    fake = dataclasses.replace(pack, root=root, audio={"listening.a.01": entry})
    app.dependency_overrides[get_catalog] = lambda: Catalog({("en", "A1"): fake})
    try:
        await env["setup"]({"assessments": exams()})
        s = await env["student"]()
        assert (await s.get(URL)).status_code == 404
    finally:
        app.dependency_overrides.pop(get_catalog, None)
