"""What the closing flow reports and enforces about the final exam.

`availability()` is what `GET /trainer/closing` answers and `guard_final_start()`
is what `POST /trainer/assessments/final/attempts` runs first. Both go through
`availability()`, so the screen and the server read the same verdict.

Moodle is asked for two things, only when everything else already allows the
exam: the course's modules (which activities exist and are visible; through the
shared cache, so up to `MOODLE_CONTENTS_TTL_SECONDS` old) and the student's
completed activities (never cached here; see `CompletionProvider`). If either
cannot be read the verdict is closed (`progress_unavailable`).

That is `CLOSING_GATE_MODE=enforce`. In `observe` Moodle is asked for neither:
the verdict and the journey (`outcome`) come from the attempts alone, so the
content is not enforced. In `off` there is no journey and no guard.

The backend keeps no per-user closing state (D29): the flow's own progress
lives in Moodle.
"""

import logging
from dataclasses import dataclass
from typing import Any, Literal

from app.assessment.service import AssessmentService, Caller
from app.closing.gate import (
    OBSERVE_REFUSALS,
    SCORM_NOT_CHECKED,
    Decision,
    Reason,
    decide,
    final_outcome,
    required_scorm_cmids,
    scorm_gate,
)
from app.closing.providers import CompletionProvider, CompletionUnavailableError
from app.exceptions import AssessmentConflictError, ClosingNotAvailableError
from app.moodle.exceptions import MoodleError
from app.moodle.protocols import CourseContentsProvider
from app.schemas.map_settings import MapSettings
from app.schemas.welcome import AssessmentStep

log = logging.getLogger("rumbos.closing")

# `CLOSING_GATE_MODE` (see docs/CLOSING-GATE.md).
GateMode = Literal["off", "observe", "enforce"]


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
        mode: GateMode,
    ) -> None:
        self._ctx = context
        self._caller = caller
        self._assessments = assessments
        self._contents = contents
        self._completion = completion
        self._mode = mode

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
        enforced = self._mode == "enforce"
        if enforced and decision.available and not preview:
            decision = await self._with_progress(facts)
        verdict: dict[str, Any] = {
            "enforced": enforced,
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
        if self._mode != "off":
            # With the gate off the answer is exactly what it was before.
            verdict["outcome"] = await self._outcome(decision, facts, item)
        return verdict

    async def _outcome(
        self, decision: Decision, facts: dict[str, Any], item: dict[str, Any] | None
    ) -> dict[str, Any]:
        """The student's journey, read from their attempts (nothing is stored,
        D29): the check-in's outcome and the final's status, by the same verdict
        that gates the start. In `observe` that verdict never looked at the
        content, and the SCORM condition is reported as not checked."""
        standing = await self._assessments.standing()
        exempts = (
            standing.checkin_passed and self._ctx.settings.assessments.exemption.enabled
        )
        final = final_outcome(
            decision,
            final_passed=standing.final_passed,
            checkin_exempts=exempts,
            attempts_left=facts["attempts_left"],
        )
        gate = (
            SCORM_NOT_CHECKED
            if self._mode == "observe"
            else scorm_gate(decision, checkin_exempts=exempts)
        )
        return {
            "checkin": standing.checkin,
            "final": {
                "status": final.status,
                "reason": final.reason,
                "attempts": item["attempts"] if item else None,
            },
            "scorm_gate": {"satisfied": gate.satisfied, "reason": gate.reason},
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
        behaviour). A map whose closing flow has no final-exam step is not
        gated either (the transitional rule, D53).

        `observe` refuses only what the attempts decide (`OBSERVE_REFUSALS`);
        for anything else the start behaves as with the gate off."""
        if self._mode == "off":
            log.warning(
                "closing_gate_off the final exam is being started without the "
                "SCORM completion check (CLOSING_GATE_MODE=off)"
            )
            return
        if not has_final_step(self._ctx.settings):
            # Transitional (D53): a map whose closing flow has no final-exam
            # step keeps the behaviour it had before the gate, so its students
            # are not left without an entry to the final exam.
            log.info("closing_gate_skipped no_final_step")
            return
        if self._mode == "observe":
            log.warning(
                "closing_gate_observe the final exam is being started without the "
                "SCORM completion check (CLOSING_GATE_MODE=observe)"
            )
        verdict = await self.availability()
        if verdict["available"]:
            return
        reason: Reason = verdict["reason"]
        if self._mode == "observe" and reason not in OBSERVE_REFUSALS:
            return
        if reason == "no_attempts_left":
            raise AssessmentConflictError("no_attempts_left")
        progress = verdict["progress"]
        raise ClosingNotAvailableError(
            reason,
            completed=progress["completed"] if progress else None,
            total=progress["total"] if progress else None,
        )
