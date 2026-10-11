"""Plain helpers for tests that upload images through the API."""

from typing import Any

import httpx

from tests import images


async def upload(
    client: httpx.AsyncClient,
    data: bytes,
    kind: str = "bubble",
    *,
    content_type: str = "image/png",
    filename: str = "pic.png",
) -> httpx.Response:
    return await client.post(
        "/assets",
        data={"kind": kind},
        files={"file": (filename, data, content_type)},
    )


async def png_asset(
    client: httpx.AsyncClient,
    n: int,
    kind: str = "bubble",
    *,
    size: tuple[int, int] | None = None,
    **form: str,
) -> dict[str, Any]:
    """Uploads a distinct small PNG and returns the asset.

    `n` makes the content distinct (and, without `size`, the width too); give
    the same `size` to several calls to get distinct images of equal dimensions.
    """
    data = images.png(size or (8 + n, 8), color=(n * 7 % 256, 30, 200))
    r = await client.post(
        "/assets",
        data={"kind": kind, **form},
        files={"file": (f"f{n}.png", data, "image/png")},
    )
    assert r.status_code == 201, r.text
    body: dict[str, Any] = r.json()
    return body
