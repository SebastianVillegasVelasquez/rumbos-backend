"""The native exam (check-in and final): contract v2, section B.

Students and editors (in preview) may call it; it answers from the settings of
the caller's own course, and 404 when the feature is off there.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response, status

from app.assessment.access import (
    CallerDep,
    ClockDep,
    CourseSettingsDep,
    rate_limited,
)
from app.assessment.service import AssessmentService
from app.auth.dependencies import ScopedSession, student_route
from app.core.config import Settings, get_settings
from app.core.paths import API_PREFIX
from app.repositories.sqlalchemy.assessment_repository import (
    SqlAlchemyAssessmentRepository,
)
from app.schemas.assessment import (
    AssessmentList,
    AttemptStarted,
    Result,
    SaveAck,
    SaveAnswers,
    SectionRead,
)
from app.trainer.dependencies import CatalogDep

router = APIRouter(
    prefix="/trainer",
    tags=["trainer"],
    dependencies=[Depends(student_route), Depends(rate_limited)],
)


def audio_url(audio_id: str, version: str) -> str:
    return f"{API_PREFIX}/trainer/audio/{audio_id}?v={version}"


def get_service(
    session: ScopedSession,
    catalog: CatalogDep,
    settings: CourseSettingsDep,
    caller: CallerDep,
    clock: ClockDep,
    app_settings: Annotated[Settings, Depends(get_settings)],
) -> AssessmentService:
    return AssessmentService(
        SqlAlchemyAssessmentRepository(session),
        catalog,
        settings.assessments,
        caller,
        clock,
        serve_drafts=app_settings.trainer_serve_drafts or caller.preview,
        audio_url=audio_url,
        issuer=app_settings.passback_issuer,
    )


ServiceDep = Annotated[AssessmentService, Depends(get_service)]


@router.get(
    "/assessments", response_model=AssessmentList, response_model_exclude_unset=True
)
async def list_assessments(service: ServiceDep) -> Any:
    return await service.overview()


@router.post(
    "/assessments/{purpose}/attempts",
    response_model=AttemptStarted,
    status_code=status.HTTP_201_CREATED,
)
async def start_attempt(purpose: str, service: ServiceDep, response: Response) -> Any:
    """201 for a new attempt; 200 with the attempt already in progress."""
    started, created = await service.start(purpose)
    if not created:
        response.status_code = status.HTTP_200_OK
    return started


@router.get("/attempts/{attempt_id}/sections/{key}", response_model=SectionRead)
async def get_section(attempt_id: uuid.UUID, key: str, service: ServiceDep) -> Any:
    return await service.section(attempt_id, key)


@router.put("/attempts/{attempt_id}/answers", response_model=SaveAck)
async def save_answers(
    attempt_id: uuid.UUID, body: SaveAnswers, service: ServiceDep
) -> Any:
    return await service.save(
        attempt_id, [(a.item_id, a.response) for a in body.answers]
    )


@router.post("/attempts/{attempt_id}/submit", response_model=Result)
async def submit_attempt(attempt_id: uuid.UUID, service: ServiceDep) -> Any:
    return await service.submit(attempt_id)


@router.get("/attempts/{attempt_id}/result", response_model=Result)
async def get_result(attempt_id: uuid.UUID, service: ServiceDep) -> Any:
    return await service.result(attempt_id)
