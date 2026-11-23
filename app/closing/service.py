"""What the closing flow reports and enforces about the final exam.

`availability()` is what `GET /trainer/closing` answers and `guard_final_start()`
is what `POST /trainer/assessments/final/attempts` runs first. Both go through
`availability()`, so the screen and the server read the same verdict.

Moodle is asked for two things, only when everything else already allows the
exam: the course's modules (which activities exist and are visible; through the
shared cache, so up to `MOODLE_CONTENTS_TTL_SECONDS` old) and the student's
completed activities (never cached here; see `CompletionProvider`). If either
cannot be read the verdict is closed (`progress_unavailable`).

The backend keeps no per-user closing state (D29): the flow's own progress
lives in Moodle.
"""

import logging
from dataclasses import dataclass
from typing import Any

from app.assessment.service import AssessmentService, Caller
from app.closing.gate import (
    Decision,
    Reason,
    decide,
    required_scorm_cmids,
)
from app.closing.providers import CompletionProvider, CompletionUnavailableError
from app.exceptions import AssessmentConflictError, ClosingNotAvailableError
from app.moodle.exceptions import MoodleError
from app.moodle.protocols import CourseContentsProvider
from app.schemas.map_settings import MapSettings
from app.schemas.welcome import AssessmentStep

log = logging.getLogger("rumbos.closing")


@dataclass(frozen=True)
class CourseContext:
    """The map a course's exam settings come from, and its activity bubbles."""

    settings: MapSettings
    activity_ids: tuple[int, ...]


def has_final_step(settings: MapSettings) -> bool:
    closing = settings.closing
    return bool(
        closing
        and closing.enabled
        and any(
            isinstance(step, AssessmentStep) and step.purpose == "final"
            for step in closing.steps or []
        )
    )


class ClosingService:
    def __init__(
        self,
        context: CourseContext,
        caller: Caller,
        assessments: AssessmentService,
        contents: CourseContentsProvider,
        completion: CompletionProvider,
        *,
        enforce: bool,
    ) -> None:
        self._ctx = context
        self._caller = caller
        self._assessments = assessments
        self._contents = contents
        self._completion = completion
        self._enforce = enforce

    async def availability(self) -> dict[str, Any]:
        settings = self._ctx.settings
        exams = settings.assessments
        policy = exams.final
        enabled = bool(
            exams.enabled
            and policy is not None
            and settings.closing is not None
            and settings.closing.enabled
        )
        item = await self._assessments.final_item() if enabled else None
        state = item["state"] if item else "locked"
        preview = self._caller.preview
        facts: dict[str, Any] = {
            "enabled": enabled,
            "has_final_step": has_final_step(settings),
            "form_served": state != "locked",
            "preview": preview,
            "already_passed": bool(item and item["already_passed"]),
            "attempts_left": state != "exhausted",
        }
        decision = decide(**facts, check_scorm=False)
        if self._enforce and decision.available and not preview:
            decision = await self._with_progress(facts)
        return {
            "enforced": self._enforce,
            "preview": preview,
            "available": decision.available,
            "reason": decision.reason,
            "progress": (
                {
                    "completed": decision.progress.completed,
                    "total": decision.progress.total,
                }
                if decision.progress
                else None
            ),
            "final": item,
        }

    async def _with_progress(self, facts: dict[str, Any]) -> Decision:
        required = await self._required()
        completed = None
        if required and self._caller.moodle_user_id is not None:
            completed = await self._completed(self._caller.moodle_user_id)
        decision = decide(
            **facts,
            required=required,
            completed=completed,
            user_known=self._caller.moodle_user_id is not None,
        )
        if required is not None and not required:
            log.info("closing_gate no_scorm_required course=%s", self._caller.course_id)
        return decision

    async def _required(self) -> frozenset[int] | None:
        try:
            result = await self._contents.fetch_course_contents(self._caller.course_id)
        except MoodleError:
            # Includes "course not found": an empty course must not open the exam.
            log.warning("closing_gate contents_unavailable")
            return None
        return required_scorm_cmids(result.sections, self._ctx.activity_ids)

    async def _completed(self, moodle_user_id: int) -> frozenset[int] | None:
        try:
            return await self._completion.completed_cmids(
                course_id=self._caller.course_id, moodle_user_id=moodle_user_id
            )
        except (CompletionUnavailableError, MoodleError):
            log.warning("closing_gate completion_unavailable")
            return None

    async def guard_final_start(self) -> None:
        """Raises unless the final exam may be started now. With the gate off
        it only says so, loudly, and lets the request through (today's
        behaviour)."""
        if not self._enforce:
            log.warning(
                "closing_gate_off the final exam is being started without the "
                "SCORM completion check (CLOSING_GATE_MODE=off)"
            )
            return
        verdict = await self.availability()
        if verdict["available"]:
            return
        reason: Reason = verdict["reason"]
        if reason == "no_attempts_left":
            raise AssessmentConflictError("no_attempts_left")
        progress = verdict["progress"]
        raise ClosingNotAvailableError(
            reason,
            completed=progress["completed"] if progress else None,
            total=progress["total"] if progress else None,
        )
