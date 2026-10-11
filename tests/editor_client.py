"""An HTTP client that behaves like the editor frontend about `If-Match`.

Guarded writes need the revision/version the caller last saw. Most tests do not
care about concurrency, so this client looks the current number up (`GET` the
map) and sends it, unless the test passes its own `If-Match`. Tests of the
preconditions themselves use a plain `httpx.AsyncClient` instead.
"""

import re
import uuid
from typing import Any

import httpx

_UUID = r"[0-9a-fA-F-]{36}"
_MAP = re.compile(rf"^/course-maps/({_UUID})$")
_APPEARANCE = re.compile(rf"^/course-maps/({_UUID})/appearance$")
_DISCARD = re.compile(rf"^/course-maps/({_UUID})/discard-changes$")
_RESTORE = re.compile(rf"^/course-maps/({_UUID})/publications/\d+/restore$")
_BUBBLE = re.compile(rf"^/course-maps/({_UUID})/bubbles/({_UUID})$")


class EditorClient(httpx.AsyncClient):
    async def request(self, method: str, url: Any, **kwargs: Any) -> httpx.Response:
        headers = httpx.Headers(kwargs.get("headers") or {})
        if "if-match" not in headers:
            number = await self._current_number(method, str(url))
            if number is not None:
                headers["If-Match"] = f'"{number}"'
                kwargs["headers"] = headers
        return await super().request(method, url, **kwargs)

    async def _current_number(self, method: str, path: str) -> int | None:
        path = path.split("?")[0]
        if method == "PATCH" and (m := _MAP.match(path)):
            return await self._revision(m[1])
        if method == "PUT" and (m := _APPEARANCE.match(path)):
            return await self._revision(m[1])
        if method == "POST" and (m := _DISCARD.match(path) or _RESTORE.match(path)):
            return await self._revision(m[1])
        if method in ("PATCH", "DELETE") and (m := _BUBBLE.match(path)):
            return await self._version(m[1], m[2])
        return None

    async def _revision(self, map_id: str) -> int:
        r = await super().request("GET", f"/course-maps/{map_id}")
        # A map that does not exist: any number, so the route answers 404.
        return int(r.json()["revision"]) if r.status_code == 200 else 1

    async def _version(self, map_id: str, bubble_id: str) -> int:
        r = await super().request("GET", f"/course-maps/{map_id}")
        if r.status_code == 200:
            for bubble in r.json()["bubbles"]:
                if uuid.UUID(bubble["id"]) == uuid.UUID(bubble_id):
                    return int(bubble["version"])
        return 1
