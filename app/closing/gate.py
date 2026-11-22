"""The one rule that says whether a student may sit the final exam.

Both `GET /trainer/closing` (what the UI shows) and `POST
/trainer/assessments/final/attempts` (what the server enforces) call `decide`,
so they cannot disagree. Nothing here touches the network or the database: the
inputs are facts, gathered by `app.closing.service`.

Which SCORM activities are required is `required_scorm_cmids`: the activity
bubbles of the map that point at a SCORM module a student can see and that
Moodle tracks for completion. Hidden, deleted, non-SCORM and untracked ones do
not count. A restricted (`availability`) one does: the service account sees
everything, so what a restriction means for one student is unknown here, and
the safe side for an exam gate is to require it.
"""

from collections.abc import Collection
from dataclasses import dataclass
from typing import Literal

from app.moodle.schemas import MoodleModule, MoodleSection
from app.services.course_map_service import is_bubble_candidate, is_hidden

# The module types that must be completed. One place to widen the rule.
REQUIRED_MODNAMES = frozenset({"scorm"})

Reason = Literal[
    "not_enabled",
    "no_final_step",
    "form_unavailable",
    "already_passed",
    "no_attempts_left",
    "scorm_incomplete",
    "progress_unavailable",
]


@dataclass(frozen=True)
class Progress:
    completed: int
    total: int


@dataclass(frozen=True)
class Decision:
    available: bool
    reason: Reason | None = None
    progress: Progress | None = None


def required_scorm_cmids(
    sections: Collection[MoodleSection],
    activity_ids: Collection[int],
    modnames: frozenset[str] = REQUIRED_MODNAMES,
) -> frozenset[int]:
    """The course-module ids a student must have completed."""
    wanted = set(activity_ids)
    found: set[int] = set()
    for section in sections:
        for module in section.modules:
            if module.id in wanted and _counts(section, module, modnames):
                found.add(module.id)
    return frozenset(found)


def _counts(
    section: MoodleSection, module: MoodleModule, modnames: frozenset[str]
) -> bool:
    return (
        module.modname in modnames
        and is_bubble_candidate(module)
        and not is_hidden(section, module)
        # Moodle records no completion for an activity that is not tracked, so
        # requiring it would lock the exam for everybody.
        and module.completion != 0
    )


def decide(
    *,
    enabled: bool,
    has_final_step: bool,
    form_served: bool,
    preview: bool,
    already_passed: bool,
    attempts_left: bool,
    check_scorm: bool = True,
    required: frozenset[int] | None = frozenset(),
    completed: frozenset[int] | None = frozenset(),
) -> Decision:
    """`required` / `completed` are None when they could not be read (Moodle
    unreachable): the answer is then closed, never open.

    With `check_scorm=False` the SCORM condition is left out: the service asks
    first without it, so Moodle is only read when everything else already
    allows the exam. An editor in preview skips every student-only condition.
    """
    if not enabled:
        return Decision(False, "not_enabled")
    if not has_final_step:
        return Decision(False, "no_final_step")
    if not form_served:
        return Decision(False, "form_unavailable")
    if preview:
        return Decision(True)
    if already_passed:
        return Decision(False, "already_passed")
    if not attempts_left:
        return Decision(False, "no_attempts_left")
    if not check_scorm:
        return Decision(True)
    if required is None:
        return Decision(False, "progress_unavailable")
    if not required:
        # A map with no SCORM to finish has nothing to wait for.
        return Decision(True, progress=Progress(0, 0))
    if completed is None:
        return Decision(False, "progress_unavailable")
    progress = Progress(len(required & completed), len(required))
    if progress.completed < progress.total:
        return Decision(False, "scorm_incomplete", progress)
    return Decision(True, progress=progress)
