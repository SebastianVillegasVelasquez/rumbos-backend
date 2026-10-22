"""Import every model here so its table registers on `BaseORM.metadata`.

Alembic's autogenerate only sees tables that have been imported.
"""

from app.models.asset import Asset, AssetAuditEvent
from app.models.auth import AccessToken, AuthORM, UsedJti
from app.models.base import BaseORM
from app.models.bubble import Bubble
from app.models.course_map import CourseMap
from app.models.publication import Publication
from app.models.skin import CourseMapSkinRule, Skin
from app.models.tenant import Tenant, TenantKey
from app.models.view3d import CourseMapView3dBinding

__all__ = [
    "AccessToken",
    "Asset",
    "AssetAuditEvent",
    "AuthORM",
    "BaseORM",
    "Bubble",
    "CourseMap",
    "CourseMapSkinRule",
    "CourseMapView3dBinding",
    "Publication",
    "Skin",
    "Tenant",
    "TenantKey",
    "UsedJti",
]
