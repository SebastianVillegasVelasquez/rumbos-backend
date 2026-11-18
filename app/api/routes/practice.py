"""The practice trainer: contract v2, section C. Students and editors (to try
it) may call it; it answers from the trainer settings of the caller's course
(language and level are the editor's choice) and 404 when it is off there."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response, status

from app.api.routes.assessments import audio_url
from app.assessment.access import (
    CallerDep,
    ClockDep,
    CourseSettingsDep,
    rate_limited,
)
from app.auth.dependencies import ScopedSession, editor_route, student_route
from app.core.config import Settings, get_settings
from app.repositories.sqlalchemy.practice_repository import (
    SqlAlchemyPracticeRepository,
)
from app.schemas.practice import (
    AnswerIn,
    AnswerOut,
    Catalog,
    CatalogUnavailable,
    Erased,
    FinishOut,
    Progress,
    ReportAck,
    ReportIn,
    ReportRow,
    ReviewIn,
    SavedWordOut,
    SessionStart,
    SessionView,
    VocabEntry,
)
from app.trainer.dependencies import CatalogDep
from app.trainer.practice_service import PracticeService

router = APIRouter(
    prefix="/trainer",
    tags=["trainer"],
    dependencies=[Depends(student_route), Depends(rate_limited)],
)

# Editors only: kept apart so every route declares exactly one access level.
editor_router = APIRouter(
    prefix="/trainer",
    tags=["trainer"],
    dependencies=[Depends(editor_route), Depends(rate_limited)],
)


def get_service(
    session: ScopedSession,
    catalog: CatalogDep,
    settings: CourseSettingsDep,
    caller: CallerDep,
    clock: ClockDep,
    app_settings: Annotated[Settings, Depends(get_settings)],
) -> PracticeService:
    pack = (
        catalog.pack(settings.trainer.language, settings.trainer.level)
        if settings.trainer.enabled
        else None
    )
    # An editor in preview reviews draft and reviewed content in the real UI;
    # students get only `published` unless the server itself serves drafts.
    return PracticeService(
        SqlAlchemyPracticeRepository(session),
        pack,
        course_id=caller.course_id,
        sub=caller.sub,
        clock=clock,
        serve_drafts=app_settings.trainer_serve_drafts or caller.preview,
        audio_url=audio_url,
        trainer_enabled=settings.trainer.enabled,
    )


ServiceDep = Annotated[PracticeService, Depends(get_service)]


@router.get("/catalog", response_model=Catalog | CatalogUnavailable)
async def get_catalog(service: ServiceDep) -> Any:
    return await service.catalog()


@router.get("/progress", response_model=Progress)
async def get_progress(service: ServiceDep) -> Any:
    return await service.progress()


@router.post(
    "/sessions", response_model=SessionView, status_code=status.HTTP_201_CREATED
)
async def start_session(
    body: SessionStart, service: ServiceDep, response: Response
) -> Any:
    """201 for a new session; 200 with the one already open for the activity."""
    view, created = await service.start(body.activity_id)
    if not created:
        response.status_code = status.HTTP_200_OK
    return view


@router.get("/sessions/{session_id}", response_model=SessionView)
async def get_session(session_id: uuid.UUID, service: ServiceDep) -> Any:
    return await service.get(session_id)


@router.post("/sessions/{session_id}/answers", response_model=AnswerOut)
async def answer(session_id: uuid.UUID, body: AnswerIn, service: ServiceDep) -> Any:
    return await service.answer(
        session_id, body.step_id, body.attempt_id, body.response
    )


@router.post("/sessions/{session_id}/finish", response_model=FinishOut)
async def finish(session_id: uuid.UUID, service: ServiceDep) -> Any:
    return await service.finish(session_id)


@router.get("/vocabulary", response_model=list[VocabEntry])
async def vocabulary(service: ServiceDep, lesson: str | None = None) -> Any:
    return await service.vocabulary(lesson)


@router.get("/vocabulary/saved", response_model=list[SavedWordOut])
async def saved_words(service: ServiceDep, due: bool = False) -> Any:
    return await service.saved_words(due)


@router.put("/vocabulary/{entry_id}", response_model=SavedWordOut)
async def save_word(entry_id: str, service: ServiceDep) -> Any:
    return await service.save_word(entry_id)


@router.delete("/vocabulary/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
async def forget_word(entry_id: str, service: ServiceDep) -> Response:
    await service.forget_word(entry_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/vocabulary/{entry_id}/review", response_model=SavedWordOut)
async def review_word(entry_id: str, body: ReviewIn, service: ServiceDep) -> Any:
    return await service.review_word(entry_id, body.knew_it)


@router.post("/reports", response_model=ReportAck, status_code=status.HTTP_201_CREATED)
async def report(body: ReportIn, service: ServiceDep) -> Any:
    return {"id": await service.report(body.activity_id, body.step_id, body.text)}


@router.delete("/me", response_model=Erased)
async def erase_me(session: ScopedSession, caller: CallerDep) -> Any:
    """Deletes the caller's practice data (sessions, saved words, reports) in
    this course. Exam attempts are not touched: they are records of a
    certification and go only through the operator's erasure procedure."""
    return await SqlAlchemyPracticeRepository(session).erase_user(caller.sub)


@editor_router.get("/reports", response_model=list[ReportRow])
async def list_reports(session: ScopedSession, limit: int = 100) -> Any:
    """Editors only: what students flagged, newest first, with the content
    version it was flagged against."""
    return await SqlAlchemyPracticeRepository(session).reports(min(max(limit, 1), 500))
