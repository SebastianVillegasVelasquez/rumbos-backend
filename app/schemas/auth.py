import uuid
from datetime import datetime
from typing import Literal

from app.enums import Role
from app.schemas.base import ApiModel


class LaunchResponse(ApiModel):
    access_token: str
    token_type: Literal["Bearer"] = "Bearer"
    expires_in: int
    role: Role
    course_id: int
    host_origin: str


class Features(ApiModel):
    """What the caller's tenant has switched on."""

    widgets: bool


class MeResponse(ApiModel):
    role: Role
    course_id: int
    features: Features


class NewAccessToken(ApiModel):
    """A token being stored; `token_hash` is the SHA-256 of the secret."""

    token_hash: str
    tenant_id: uuid.UUID
    course_id: int
    role: Role
    sub: str
    expires_at: datetime


class TokenGrant(ApiModel):
    """What a valid bearer token stands for."""

    tenant_id: uuid.UUID
    course_id: int
    role: Role
    sub: str
    expires_at: datetime
