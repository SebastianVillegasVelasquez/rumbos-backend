"""Publishing a map: state, publish, history, discard and restore.

SECURITY: there is no authentication, so none of this is access control. A
draft is not hidden from anyone who calls the editor endpoints; "published"
only means "what students are shown".
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.preconditions import IfMatch, set_etag
from app.core.config import Settings, get_settings
from app.core.database import get_session
from app.repositories.sqlalchemy.bubble_repository import SqlAlchemyBubbleRepository
from app.repositories.sqlalchemy.course_map_repository import (
    SqlAlchemyCourseMapRepository,
)
from app.repositories.sqlalchemy.publication_repository import (
    SqlAlchemyPublicationRepository,
)
from app.repositories.sqlalchemy.view3d_repository import SqlAlchemyView3dRepository
from app.schemas.publication import (
    PublicationList,
    PublicationState,
    PublicationSummary,
    PublishRequest,
    RestoreResult,
)
from app.services.publication_service import (
    MAX_PUBLICATIONS_LISTED,
    PublicationService,
)

router = APIRouter(prefix="/course-maps", tags=["publications"])


def get_publication_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> PublicationService:
    return PublicationService(
        SqlAlchemyPublicationRepository(session),
        SqlAlchemyCourseMapRepository(session),
        SqlAlchemyBubbleRepository(session),
        SqlAlchemyView3dRepository(session),
        settings.publications_keep,
    )


ServiceDep = Annotated[PublicationService, Depends(get_publication_service)]


@router.get("/{course_map_id}/publication-state", response_model=PublicationState)
async def get_publication_state(
    course_map_id: uuid.UUID, service: ServiceDep
) -> PublicationState:
    return await service.state(course_map_id)


@router.post(
    "/{course_map_id}/publish",
    response_model=PublicationSummary,
    status_code=status.HTTP_201_CREATED,
)
async def publish(
    course_map_id: uuid.UUID, data: PublishRequest, service: ServiceDep
) -> PublicationSummary:
    """`draftHash` is the hash the editor reviewed. 409 `draft_changed_since_review`
    if the draft moved, 409 `no_changes_to_publish`, 422 `publish_invalid`."""
    return await service.publish(course_map_id, data)


@router.get("/{course_map_id}/publications", response_model=PublicationList)
async def list_publications(
    course_map_id: uuid.UUID,
    service: ServiceDep,
    limit: Annotated[int, Query(ge=1, le=MAX_PUBLICATIONS_LISTED)] = 20,
) -> PublicationList:
    return await service.list_publications(course_map_id, limit)


@router.post("/{course_map_id}/discard-changes", response_model=RestoreResult)
async def discard_changes(
    course_map_id: uuid.UUID,
    service: ServiceDep,
    response: Response,
    revision: IfMatch,
) -> RestoreResult:
    """Puts the current publication back into the draft. Needs
    `If-Match: "<revision>"`; 409 `never_published` if nothing was published."""
    result = await service.discard_changes(course_map_id, revision)
    set_etag(response, result.map.revision)
    return result


@router.post(
    "/{course_map_id}/publications/{number}/restore", response_model=RestoreResult
)
async def restore_publication(
    course_map_id: uuid.UUID,
    number: int,
    service: ServiceDep,
    response: Response,
    revision: IfMatch,
) -> RestoreResult:
    """Loads an older publication into the draft (it does NOT publish). Needs
    `If-Match: "<revision>"`."""
    result = await service.restore(course_map_id, number, revision)
    set_etag(response, result.map.revision)
    return result
