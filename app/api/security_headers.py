"""Response headers that are not any one route's business."""

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.paths import API_PREFIX

AUTH_PATH = f"{API_PREFIX}/auth"


class NoStoreForAuth:
    """`Cache-Control: no-store` on everything under `/auth`, errors included:
    a response there may carry a token or say who is signed in, and no cache
    may keep it."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path: str = scope.get("path", "")
        if scope["type"] != "http" or not (
            path == AUTH_PATH or path.startswith(AUTH_PATH + "/")
        ):
            await self._app(scope, receive, send)
            return

        async def send_no_store(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Cache-Control"] = "no-store"
                headers["Pragma"] = "no-cache"
            await send(message)

        await self._app(scope, receive, send_no_store)
