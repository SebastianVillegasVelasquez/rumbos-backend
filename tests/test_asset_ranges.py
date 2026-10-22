"""`GET /assets/{id}` answers byte-range requests, which audio players need
(iOS Safari refuses to play what it cannot seek in)."""

import httpx
import pytest

from tests import audio_files as af
from tests.asset_helpers import png_asset, upload_audio

DATA = af.mp3(40)
SIZE = len(DATA)  # 16 680 bytes


@pytest.fixture
async def track(client: httpx.AsyncClient) -> dict[str, str]:
    created = (await upload_audio(client, DATA)).json()
    assert created["bytes"] == SIZE
    return {"url": created["url"], "id": created["id"]}


async def test_a_plain_request_advertises_ranges_and_keeps_its_headers(
    client: httpx.AsyncClient, track: dict[str, str]
) -> None:
    r = await client.get(track["url"])

    assert r.status_code == 200 and r.content == DATA
    assert r.headers["accept-ranges"] == "bytes"
    assert r.headers["content-type"] == "audio/mpeg"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert r.headers["content-length"] == str(SIZE)
    assert r.headers["etag"].startswith('"')


async def test_a_range_gets_206_with_the_right_content_range(
    client: httpx.AsyncClient, track: dict[str, str]
) -> None:
    r = await client.get(track["url"], headers={"Range": "bytes=100-199"})

    assert r.status_code == 206
    assert r.content == DATA[100:200]
    assert r.headers["content-range"] == f"bytes 100-199/{SIZE}"
    assert r.headers["content-length"] == "100"
    assert r.headers["accept-ranges"] == "bytes"
    assert r.headers["content-type"] == "audio/mpeg"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"
    full = await client.get(track["url"])
    assert r.headers["etag"] == full.headers["etag"]


@pytest.mark.parametrize(
    ("header", "first", "last"),
    [
        ("bytes=0-1", 0, 1),  # what Safari asks first
        ("bytes=0-0", 0, 0),
        ("bytes=0-", 0, SIZE - 1),
        ("bytes=16000-", 16000, SIZE - 1),
        ("bytes=-100", SIZE - 100, SIZE - 1),
        ("bytes=-1", SIZE - 1, SIZE - 1),
        ("bytes=-99999", 0, SIZE - 1),  # a suffix longer than the file is all of it
        ("bytes=16000-99999", 16000, SIZE - 1),  # an end past the file is clamped
        (f"bytes={SIZE - 1}-{SIZE - 1}", SIZE - 1, SIZE - 1),
        ("  bytes=5-9  ", 5, 9),
    ],
)
async def test_the_forms_of_a_single_range(
    client: httpx.AsyncClient,
    track: dict[str, str],
    header: str,
    first: int,
    last: int,
) -> None:
    r = await client.get(track["url"], headers={"Range": header})

    assert r.status_code == 206, r.text
    assert r.content == DATA[first : last + 1]
    assert r.headers["content-range"] == f"bytes {first}-{last}/{SIZE}"


@pytest.mark.parametrize(
    "header", [f"bytes={SIZE}-", f"bytes={SIZE + 5}-{SIZE + 9}", "bytes=-0"]
)
async def test_a_range_outside_the_file_answers_416(
    client: httpx.AsyncClient, track: dict[str, str], header: str
) -> None:
    r = await client.get(track["url"], headers={"Range": header})

    assert r.status_code == 416
    assert r.headers["content-range"] == f"bytes */{SIZE}"
    assert r.headers["accept-ranges"] == "bytes"
    assert r.content == b""


@pytest.mark.parametrize(
    "header",
    [
        "bytes=abc",
        "bytes=5-2",  # last before first: not a range
        "items=0-1",  # another unit
        "bytes=0-1,5-9",  # several ranges: the whole file is a valid answer
        "bytes=-",
        "bytes",
        "",
        "bytes=0-" + "9" * 5000,  # absurd numbers must not break anything
    ],
)
async def test_a_range_we_do_not_honour_gets_the_whole_file(
    client: httpx.AsyncClient, track: dict[str, str], header: str
) -> None:
    r = await client.get(track["url"], headers={"Range": header})

    assert r.status_code == 200
    assert r.content == DATA
    assert "content-range" not in r.headers


async def test_if_range_with_the_current_etag_keeps_the_range(
    client: httpx.AsyncClient, track: dict[str, str]
) -> None:
    etag = (await client.get(track["url"])).headers["etag"]

    r = await client.get(track["url"], headers={"Range": "bytes=0-9", "If-Range": etag})

    assert r.status_code == 206 and r.content == DATA[:10]


@pytest.mark.parametrize(
    "if_range", ['"not-the-etag"', "Wed, 21 Oct 2026 07:28:00 GMT", 'W/"x"']
)
async def test_if_range_with_anything_else_gets_the_whole_file(
    client: httpx.AsyncClient, track: dict[str, str], if_range: str
) -> None:
    r = await client.get(
        track["url"], headers={"Range": "bytes=0-9", "If-Range": if_range}
    )

    assert r.status_code == 200 and r.content == DATA


async def test_a_fresh_cache_gets_304_even_when_it_asks_for_a_range(
    client: httpx.AsyncClient, track: dict[str, str]
) -> None:
    etag = (await client.get(track["url"])).headers["etag"]

    r = await client.get(
        track["url"], headers={"Range": "bytes=0-9", "If-None-Match": etag}
    )

    assert r.status_code == 304
    assert r.content == b""
    assert r.headers["accept-ranges"] == "bytes"


async def test_head_describes_the_file_without_sending_it(
    client: httpx.AsyncClient, track: dict[str, str]
) -> None:
    r = await client.head(track["url"])

    assert r.status_code == 200
    assert r.content == b""
    assert r.headers["content-length"] == str(SIZE)
    assert r.headers["accept-ranges"] == "bytes"
    assert r.headers["content-type"] == "audio/mpeg"


async def test_ranges_put_back_together_are_the_file(
    client: httpx.AsyncClient, track: dict[str, str]
) -> None:
    pieces = []
    for first in range(0, SIZE, 4096):
        r = await client.get(
            track["url"], headers={"Range": f"bytes={first}-{first + 4095}"}
        )
        assert r.status_code == 206
        pieces.append(r.content)

    assert b"".join(pieces) == DATA


async def test_a_missing_asset_is_still_404_with_a_range(
    client: httpx.AsyncClient,
) -> None:
    r = await client.get(
        "/assets/00000000-0000-7000-8000-000000000000", headers={"Range": "bytes=0-1"}
    )

    assert r.status_code == 404


async def test_images_get_ranges_too(client: httpx.AsyncClient) -> None:
    created = await png_asset(client, 1, "background")
    whole = (await client.get(created["url"])).content

    r = await client.get(created["url"], headers={"Range": "bytes=0-7"})

    assert r.status_code == 206 and r.content == whole[:8]
    assert r.headers["content-range"] == f"bytes 0-7/{len(whole)}"
