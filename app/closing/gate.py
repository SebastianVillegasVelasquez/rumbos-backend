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

import json
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
    "user_unlinked",
]

# What each refusal tells the student: fixed texts, no activity, no internals.
REASON_MESSAGES: dict[str, str] = {
    "not_enabled": "The final exam is not enabled for this course.",
    "no_final_step": "This course has no final exam step yet.",
    "form_unavailable": "The final exam is not published yet.",
    "already_passed": "You have already passed the exam.",
    "no_attempts_left": "You have used all your attempts at the final exam.",
    "scorm_incomplete": "Complete all the course content to unlock the final exam.",
    "progress_unavailable": (
        "Your progress could not be checked right now. Try again in a moment."
    ),
    "user_unlinked": (
        "Your Moodle account could not be identified in this session. Open the "
        "course again from Moodle; if it keeps happening, contact support."
    ),
}


@dataclass(frozen=True)
class Progress:
    completed: int
    total: int


@dataclass(frozen=True)
class Decision:
    available: bool
    reason: Reason | None = None
    progress: Progress | None = None


FinalStatus = Literal[
    "required",
    "not_required_passed_checkin",
    "passed",
    "exhausted",
    "locked",
]
ScormGateReason = Literal["passed_checkin", "not_checked"]


@dataclass(frozen=True)
class FinalOutcome:
    """Where the student stands with the final exam, for the frontend to pick
    the closing flow it shows (with the final-exam step, or without it)."""

    status: FinalStatus
    # Why the exam is `locked` (the verdict's own reason); null otherwise.
    reason: Reason | None = None


@dataclass(frozen=True)
class ScormGate:
    """Whether the SCORM condition is met. `satisfied` is null when it was not
    checked or could not be read; `reason` says that something other than the
    content met it (`passed_checkin`), or that this mode never looks at it
    (`not_checked`)."""

    satisfied: bool | None
    reason: ScormGateReason | None = None


def final_outcome(
    decision: Decision,
    *,
    final_passed: bool,
    checkin_exempts: bool,
    attempts_left: bool,
) -> FinalOutcome:
    """The final exam's status, derived from the one verdict `decide` gave and
    two facts it folds together (`already_passed` is true for a pass in the
    final and for a check-in pass that exempts).

    `checkin_exempts`: the student passed the check-in and the map's exemption is
    on, so the final is not required. A map with no final exam at all stays
    `locked` (`not_enabled`, `no_final_step`): there is nothing to require."""
    if decision.reason in ("not_enabled", "no_final_step"):
        return FinalOutcome("locked", decision.reason)
    if final_passed:
        return FinalOutcome("passed")
    if checkin_exempts:
        return FinalOutcome("not_required_passed_checkin")
    if not attempts_left:
        return FinalOutcome("exhausted")
    if decision.available:
        return FinalOutcome("required")
    return FinalOutcome("locked", decision.reason)


def scorm_gate(decision: Decision, *, checkin_exempts: bool) -> ScormGate:
    """A check-in pass satisfies the SCORM condition without the content being
    done: the plugin marks it complete from the signed pass statement. Otherwise
    the condition is what the verdict found: met, not met, or unknown (not read
    yet, or Moodle could not be asked)."""
    if checkin_exempts:
        return ScormGate(True, "passed_checkin")
    if decision.reason == "scorm_incomplete":
        return ScormGate(False)
    if decision.available and decision.progress is not None:
        return ScormGate(True)
    return ScormGate(None)


# What `observe` reports about the content, whatever the student did: it never
# looks, so it claims nothing.
SCORM_NOT_CHECKED = ScormGate(None, "not_checked")

# The refusals `observe` applies at the start of a final attempt: the ones that
# follow from the student's attempts alone.
OBSERVE_REFUSALS: frozenset[Reason] = frozenset({"already_passed", "no_attempts_left"})


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


def restriction_kinds(availability: str | None) -> list[str]:
    """The kinds of condition in a module's raw `availability` JSON (`group`,
    `grouping`, `date`, `completion`, `grade`, `profile`...), sorted and without
    repeats. Empty: unrestricted. Unreadable JSON: `["unreadable"]`. For the
    operator's report on activities that may never open for a student."""
    if not availability:
        return []
    try:
        tree = json.loads(availability)
    except ValueError:
        return ["unreadable"]
    kinds: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            kind = node.get("type")
            if isinstance(kind, str):
                kinds.add(kind)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(tree)
    return sorted(kinds)


def decide(
    *,
    enabled: bool,
    has_final_step: bool,
    form_served: bool,
    preview: bool,
    already_passed: bool,
    attempts_left: bool,
    check_scorm: bool = True,
    user_known: bool = True,
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
    if not user_known:
        # Enforcing, with SCORM to check, but the launch did not say who the
        # student is in Moodle: closed, and said clearly.
        return Decision(False, "user_unlinked")
    if completed is None:
        return Decision(False, "progress_unavailable")
    progress = Progress(len(required & completed), len(required))
    if progress.completed < progress.total:
        return Decision(False, "scorm_incomplete", progress)
    return Decision(True, progress=progress)
