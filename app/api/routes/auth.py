"""`POST /auth/launch` trades a signed launch assertion for a bearer token."""

import json
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import bearer_route, public_route
from app.auth.launch import MAX_ASSERTION_BYTES, LaunchService, LaunchSettings
from app.auth.principal import Principal
from app.auth.rate_limit import FixedWindowLimiter, client_key
from app.core.config import Settings, get_settings
from app.core.database import get_session
from app.exceptions import LaunchInvalidError
from app.repositories.sqlalchemy.auth_repository import SqlAlchemyAuthRepository
from app.repositories.sqlalchemy.tenant_repository import SqlAlchemyTenantRepository
from app.schemas.auth import Features, LaunchResponse, MeResponse

router = APIRouter(prefix="/auth", tags=["auth"])

# The JSON envelope around an assertion of at most MAX_ASSERTION_BYTES.
_BODY_LIMIT = MAX_ASSERTION_BYTES + 256


class _Limiters:
    def __init__(self, settings: Settings) -> None:
        self.by_ip = FixedWindowLimiter(settings.launch_rate_per_ip)
        self.by_tenant = FixedWindowLimiter(settings.launch_rate_per_tenant)


def get_limiters(
    request: Request, settings: Annotated[Settings, Depends(get_settings)]
) -> _Limiters:
    """One pair of limiters for the whole process, made on first use."""
    state = request.app.state
    if getattr(state, "launch_limiters", None) is None:
        state.launch_limiters = _Limiters(settings)
    limiters: _Limiters = state.launch_limiters
    return limiters


def get_launch_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    limiters: Annotated[_Limiters, Depends(get_limiters)],
) -> LaunchService:
    return LaunchService(
        SqlAlchemyTenantRepository(session),
        SqlAlchemyAuthRepository(session),
        LaunchSettings(
            audience=settings.auth_audience,
            token_ttl_seconds=settings.access_token_ttl_seconds,
            leeway_seconds=settings.launch_leeway_seconds,
        ),
        limiters.by_ip,
        limiters.by_tenant,
    )


async def _read_assertion(request: Request) -> str:
    """The `assertion` of a small JSON body. Anything wrong with the request is
    the same `launch_invalid` as a bad assertion (the caller learns nothing)."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > _BODY_LIMIT:
        raise LaunchInvalidError("oversize")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > _BODY_LIMIT:
            raise LaunchInvalidError("oversize")
    try:
        data = json.loads(body)
    except ValueError as exc:  # also UnicodeDecodeError
        raise LaunchInvalidError("malformed_body") from exc
    assertion = data.get("assertion") if isinstance(data, dict) else None
    if not isinstance(assertion, str):
        raise LaunchInvalidError("malformed_body")
    return assertion


@router.post(
    "/launch", response_model=LaunchResponse, dependencies=[Depends(public_route)]
)
async def launch(
    request: Request,
    service: Annotated[LaunchService, Depends(get_launch_service)],
) -> LaunchResponse:
    """Body `{"assertion": "<JWS>"}`. 401 `launch_invalid` for every
    verification failure (the reason is only logged), 403 `tenant_disabled`,
    429 `rate_limited`."""
    client = client_key(request.client.host if request.client else None)
    try:
        assertion = await _read_assertion(request)
    except LaunchInvalidError as exc:
        # Counted against the client like any other attempt.
        await service.reject(exc.reason, client=client)
        raise
    return await service.launch(assertion, client=client)


@router.get("/me", response_model=MeResponse)
async def me(
    principal: Annotated[Principal, Depends(bearer_route)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> MeResponse:
    assert principal.course_id is not None  # tokens are always bound to a course
    tenant = await SqlAlchemyTenantRepository(session).get_by_id(principal.tenant_id)
    assert tenant is not None  # the token's tenant exists (a foreign key)
    return MeResponse(
        role=principal.role,
        course_id=principal.course_id,
        features=Features(widgets=tenant.widgets_enabled),
    )
