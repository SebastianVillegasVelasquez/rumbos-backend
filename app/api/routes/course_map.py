import uuid
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Header, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.preconditions import IfMatch, parse_if_match, set_etag
from app.core.database import get_session
from app.moodle.dependencies import get_contents_provider, get_moodle_client
from app.moodle.protocols import CourseContentsProvider, MoodleClient
from app.repositories.sqlalchemy.asset_repository import SqlAlchemyAssetRepository
from app.repositories.sqlalchemy.bubble_repository import SqlAlchemyBubbleRepository
from app.repositories.sqlalchemy.course_map_repository import (
    SqlAlchemyCourseMapRepository,
)
from app.repositories.sqlalchemy.publication_repository import (
    SqlAlchemyPublicationRepository,
)
from app.repositories.sqlalchemy.skin_repository import SqlAlchemySkinRepository
from app.repositories.sqlalchemy.view3d_repository import SqlAlchemyView3dRepository
from app.schemas.activity import ActivityRead
from app.schemas.bubble import (
    BubbleCreate,
    BubbleOrdered,
    BubbleOrderUpdate,
    BubbleRead,
    BubbleUpdate,
)
from app.schemas.course_map import (
    AppearanceUpdate,
    CourseMapCreate,
    CourseMapList,
    CourseMapOrdered,
    CourseMapOrderUpdate,
    CourseMapRead,
    CourseMapUpdate,
)
from app.schemas.resolved import ResolvedMap
from app.schemas.view3d import View3dUpdate
from app.services.course_map_service import CourseMapService
from app.services.resolution_service import MapResolutionService

router = APIRouter(prefix="/course-maps", tags=["course-maps"])


def get_course_map_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    moodle: Annotated[MoodleClient, Depends(get_moodle_client)],
) -> CourseMapService:
    return CourseMapService(
        SqlAlchemyCourseMapRepository(session),
        SqlAlchemyBubbleRepository(session),
        moodle,
        SqlAlchemySkinRepository(session),
        SqlAlchemyAssetRepository(session),
        SqlAlchemyPublicationRepository(session),
        SqlAlchemyView3dRepository(session),
    )


def get_resolution_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    contents: Annotated[CourseContentsProvider, Depends(get_contents_provider)],
) -> MapResolutionService:
    return MapResolutionService(
        SqlAlchemyCourseMapRepository(session),
        SqlAlchemyBubbleRepository(session),
        contents,
    )


ServiceDep = Annotated[CourseMapService, Depends(get_course_map_service)]
ResolutionServiceDep = Annotated[MapResolutionService, Depends(get_resolution_service)]


@router.get("", response_model=CourseMapList)
async def list_course_maps(
    service: ServiceDep,
    moodle_course_id: Annotated[int | None, Query(alias="moodleCourseId")] = None,
    q: Annotated[str | None, Query(max_length=120)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 24,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> CourseMapList:
    return await service.list_course_maps(
        moodle_course_id=moodle_course_id, q=q, limit=limit, offset=offset
    )


@router.post("", response_model=CourseMapRead, status_code=status.HTTP_201_CREATED)
async def create_course_map(
    data: CourseMapCreate, service: ServiceDep, response: Response
) -> CourseMapRead:
    created = await service.create_course_map(data)
    set_etag(response, created.revision)
    return created


@router.put("/order", response_model=CourseMapOrdered)
async def reorder_course_maps(
    data: CourseMapOrderUpdate, service: ServiceDep
) -> CourseMapOrdered:
    return await service.reorder_course_maps(data)


@router.get("/{course_map_id}", response_model=CourseMapRead)
async def get_course_map(
    course_map_id: uuid.UUID, service: ServiceDep, response: Response
) -> CourseMapRead:
    course_map = await service.get_course_map(course_map_id)
    set_etag(response, course_map.revision)
    return course_map


@router.patch("/{course_map_id}", response_model=CourseMapRead)
async def update_course_map(
    course_map_id: uuid.UUID,
    data: CourseMapUpdate,
    service: ServiceDep,
    response: Response,
    revision: IfMatch,
) -> CourseMapRead:
    """Needs `If-Match: "<revision>"` (428 without, 412 when stale)."""
    updated = await service.update_course_map(course_map_id, data, revision)
    set_etag(response, updated.revision)
    return updated


@router.put("/{course_map_id}/appearance", response_model=CourseMapRead)
async def update_appearance(
    course_map_id: uuid.UUID,
    data: AppearanceUpdate,
    service: ServiceDep,
    response: Response,
    revision: IfMatch,
) -> CourseMapRead:
    """Needs `If-Match: "<revision>"` (428 without, 412 when stale)."""
    updated = await service.update_appearance(course_map_id, data, revision)
    set_etag(response, updated.revision)
    return updated


async def require_request_body(request: Request) -> None:
    """422 when the request has no body at all.

    FastAPI reads a JSON `null` body as "missing", so `view3d` declares its body
    optional (`null` removes the 3D view); this keeps a request that merely
    forgot its body from doing that.
    """
    if not (await request.body()).strip():
        raise RequestValidationError(
            [{"type": "missing", "loc": ("body",), "msg": "Field required"}]
        )


@router.put(
    "/{course_map_id}/view3d",
    response_model=CourseMapRead,
    dependencies=[Depends(require_request_body)],
)
async def update_view3d(
    course_map_id: uuid.UUID,
    service: ServiceDep,
    response: Response,
    revision: IfMatch,
    data: Annotated[View3dUpdate | None, Body()] = None,
) -> CourseMapRead:
    """Replaces the 3D scene and its zone bindings; a `null` body removes the
    3D view. Needs `If-Match: "<revision>"` (428 without, 412 when stale);
    422 `scene_unknown` / `zone_out_of_range` / `duplicate_zone` /
    `duplicate_bubble` / `bubble_not_in_map`."""
    updated = await service.update_view3d(course_map_id, data, revision)
    set_etag(response, updated.revision)
    return updated


@router.delete("/{course_map_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_course_map(course_map_id: uuid.UUID, service: ServiceDep) -> Response:
    await service.delete_course_map(course_map_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{course_map_id}/activities", response_model=list[ActivityRead])
async def list_activities(
    course_map_id: uuid.UUID,
    service: ServiceDep,
    # TODO(auth): there is no authentication yet, so anyone can list the names
    # of hidden activities with `includeHidden=true` (here and on `/resolved`).
    # When auth arrives, this parameter must require a teacher/editor role.
    include_hidden: Annotated[bool, Query(alias="includeHidden")] = False,
    only_section: Annotated[bool, Query(alias="onlySection")] = False,
) -> list[ActivityRead]:
    return await service.list_activities(
        course_map_id, include_hidden=include_hidden, only_section=only_section
    )


@router.get("/{course_map_id}/resolved", response_model=ResolvedMap)
async def resolve_course_map(
    course_map_id: uuid.UUID,
    service: ResolutionServiceDep,
    # TODO(auth): same as `/activities`: until there is authentication anyone
    # can read the names of hidden activities with `includeHidden=true`. It
    # must require a teacher/editor role once roles exist.
    include_hidden: Annotated[bool, Query(alias="includeHidden")] = False,
) -> ResolvedMap:
    return await service.resolve(course_map_id, include_hidden=include_hidden)


@router.post(
    "/{course_map_id}/bubbles",
    response_model=BubbleRead,
    status_code=status.HTTP_201_CREATED,
)
async def add_bubble(
    course_map_id: uuid.UUID,
    data: BubbleCreate,
    service: ServiceDep,
    response: Response,
) -> BubbleRead:
    created = await service.add_bubble(course_map_id, data)
    set_etag(response, created.version)
    return created


@router.put("/{course_map_id}/bubbles/order", response_model=BubbleOrdered)
async def reorder_bubbles(
    course_map_id: uuid.UUID, data: BubbleOrderUpdate, service: ServiceDep
) -> BubbleOrdered:
    return await service.reorder_bubbles(course_map_id, data)


@router.patch("/{course_map_id}/bubbles/{bubble_id}", response_model=BubbleRead)
async def update_bubble(
    course_map_id: uuid.UUID,
    bubble_id: uuid.UUID,
    data: BubbleUpdate,
    service: ServiceDep,
    response: Response,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> BubbleRead:
    """`If-Match: "<version>"` is required unless the body is only `status`
    (live demo data), which neither needs nor bumps the version."""
    content_edit = bool(data.model_fields_set - {"status"})
    version = parse_if_match(if_match, required=content_edit)
    updated = await service.update_bubble(course_map_id, bubble_id, data, version)
    set_etag(response, updated.version)
    return updated


@router.delete(
    "/{course_map_id}/bubbles/{bubble_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def remove_bubble(
    course_map_id: uuid.UUID,
    bubble_id: uuid.UUID,
    service: ServiceDep,
    version: IfMatch,
) -> Response:
    """Needs `If-Match: "<version>"` (428 without, 412 when stale)."""
    await service.remove_bubble(course_map_id, bubble_id, version)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
