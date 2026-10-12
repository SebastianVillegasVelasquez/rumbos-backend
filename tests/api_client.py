"""An HTTP client for the API that lives under `/api/v1`.

Tests write paths relative to the API (`/course-maps`), and the client adds the
prefix. A path that already carries it (the `url` the API returns for an asset,
say) is sent as it is, exactly like a browser would.
"""

from typing import Any

import httpx

from app.core.paths import API_PREFIX


class ApiClient(httpx.AsyncClient):
    async def request(self, method: str, url: Any, **kwargs: Any) -> httpx.Response:
        raw = str(url)
        if raw.startswith(f"{API_PREFIX}/"):
            origin = str(self.base_url.copy_with(raw_path=b"/")).rstrip("/")
            url = origin + raw
        return await super().request(method, url, **kwargs)
