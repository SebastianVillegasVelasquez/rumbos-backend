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
    CourseContextDep,
    CourseSettingsDep,
    rate_limited,
)
from app.assessment.service import AssessmentService
from app.auth.dependencies import ScopedSession, editor_route, student_route
from app.closing.providers import CompletionProvider, get_completion_provider
from app.closing.service import ClosingService, has_final_step
from app.core.config import Settings, get_settings
from app.core.paths import API_PREFIX
from app.moodle.dependencies import get_contents_provider
from app.moodle.protocols import CourseContentsProvider
from app.repositories.sqlalchemy.assessment_repository import (
    SqlAlchemyAssessmentRepository,
)
from app.schemas.assessment import (
    AssessmentFormEntry,
    AssessmentList,
    AttemptStarted,
    ClosingStatus,
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


# Editors only, kept apart so every route declares exactly one access level.
editor_router = APIRouter(
    prefix="/trainer",
    tags=["trainer"],
    dependencies=[Depends(editor_route), Depends(rate_limited)],
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
        # Transitional (D53): only a map whose closing flow holds the final
        # exam step moves the exam out of the trainer list.
        hide_final=app_settings.closing_gate_mode == "enforce"
        and has_final_step(settings),
    )


ServiceDep = Annotated[AssessmentService, Depends(get_service)]


def get_closing_service(
    context: CourseContextDep,
    caller: CallerDep,
    service: ServiceDep,
    contents: Annotated[CourseContentsProvider, Depends(get_contents_provider)],
    completion: Annotated[CompletionProvider, Depends(get_completion_provider)],
    app_settings: Annotated[Settings, Depends(get_settings)],
) -> ClosingService:
    return ClosingService(
        context,
        caller,
        service,
        contents,
        completion,
        enforce=app_settings.closing_gate_mode == "enforce",
    )


ClosingDep = Annotated[ClosingService, Depends(get_closing_service)]


@editor_router.get("/assessments/forms", response_model=list[AssessmentFormEntry])
async def list_forms(catalog: CatalogDep) -> Any:
    """Editors only, read-only: every exam form in the deployed content with its
    language, level, purpose and publication state, drafts included, so a map
    can be configured before the form is published (saving it only warns).
    Ordered by id."""
    forms = [
        {
            "id": form.form_id,
            "title": form.title,
            "language": form.language,
            "level": form.level,
            "purpose": form.purpose,
            "publication_state": form.status,
        }
        for pack in catalog.packs.values()
        for form in pack.forms.values()
    ]
    return sorted(forms, key=lambda f: f["id"])


@router.get(
    "/assessments", response_model=AssessmentList, response_model_exclude_unset=True
)
async def list_assessments(service: ServiceDep) -> Any:
    return await service.overview()


@router.get("/closing", response_model=ClosingStatus)
async def closing_status(closing: ClosingDep) -> Any:
    """Whether the final exam of the closing flow may be started, why not, how
    much SCORM content is done, and the exam's own state. A course without a
    closing flow answers `available: false, reason: not_enabled`."""
    return await closing.availability()


@router.post(
    "/assessments/{purpose}/attempts",
    response_model=AttemptStarted,
    status_code=status.HTTP_201_CREATED,
)
async def start_attempt(
    purpose: str, service: ServiceDep, closing: ClosingDep, response: Response
) -> Any:
    """201 for a new attempt; 200 with the attempt already in progress.

    The final exam first passes the closing gate (409 `closing_not_available`,
    or the existing `no_attempts_left`); the check-in is not gated."""
    if purpose == "final":
        await closing.guard_final_start()
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
