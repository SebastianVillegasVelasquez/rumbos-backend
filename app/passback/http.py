"""The real sender: POSTs each signed statement to the plugin's mirror receiver.

One endpoint per tenant (`tenants.mirror_endpoint`, set by `app.scripts.mirror`)
takes every kind of statement (result, definition, attempt) as a compact JWS in
the body (`Content-Type: application/jwt`). The answer decides what the outbox
does next:

    2xx                      accepted; `{"status": "duplicate"}` is as good
    409/422 `unknown_definition`   the attempt names a definition the plugin
                             does not have: the backend queues it again and
                             retries the attempt (never drops it)
    408, 425, 429, 5xx, no answer   transient: retry later with backoff
    any other 4xx, 3xx       permanent: parked for an operator

Redirects are not followed (a statement is never replayed to another host). The
receiver has no listing of what it holds, so reconciliation is off for it.
"""

import uuid
from collections.abc import Mapping
from typing import Any

import httpx

from app.passback.port import Outcome, SendResult

CONTENT_TYPE = "application/jwt"
TRANSIENT_STATUS = frozenset({408, 425, 429})


class HttpMirrorPort:
    supports_reconcile = False

    def __init__(
        self, client: httpx.AsyncClient, endpoints: Mapping[uuid.UUID, str]
    ) -> None:
        self._client = client
        self._endpoints = endpoints

    async def send(
        self, tenant_id: uuid.UUID, course_id: int, statement_jws: str
    ) -> SendResult:
        url = self._endpoints.get(tenant_id)
        if url is None:
            return SendResult(Outcome.TRANSIENT, "no mirror endpoint for the tenant")
        try:
            response = await self._client.post(
                url,
                content=statement_jws,
                headers={"Content-Type": CONTENT_TYPE, "Accept": "application/json"},
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            return SendResult(Outcome.TRANSIENT, f"{type(exc).__name__}")
        return classify(response)

    async def known(self, tenant_id: uuid.UUID, course_id: int) -> set[str]:
        return set()


def _body(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def classify(response: httpx.Response) -> SendResult:
    code = response.status_code
    body = _body(response)
    if 200 <= code < 300:
        duplicate = body.get("status") == "duplicate"
        return SendResult(Outcome.DUPLICATE if duplicate else Outcome.ACCEPTED)
    reason = str(body.get("error") or body.get("code") or "")
    if code in (409, 422) and reason == "unknown_definition":
        return SendResult(Outcome.UNKNOWN_DEFINITION, reason)
    detail = f"http {code}" + (f" {reason[:100]}" if reason else "")
    if code in TRANSIENT_STATUS or code >= 500:
        return SendResult(Outcome.TRANSIENT, detail)
    return SendResult(Outcome.PERMANENT, detail)
