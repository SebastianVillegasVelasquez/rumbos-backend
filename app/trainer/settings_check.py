"""Write-time checks of the trainer and assessment settings of a map against
the content that is actually deployed.

A **problem** blocks the save (422): the settings name content that does not
exist at all, or break a rule between settings. A **warning** does not: the
content exists but is not `published` yet, so an editor can configure it
before a person has reviewed and published it.
"""

import re
from dataclasses import dataclass, field

from app.schemas.congratulations import (
    BODY_MAX,
    CTA_LABEL_MAX,
    PLACEHOLDERS,
    TITLE_MAX,
    Congratulations,
)
from app.schemas.map_settings import MapSettings
from app.schemas.welcome import AssessmentStep
from app.trainer.catalog import Catalog

Problem = dict[str, str]


@dataclass(frozen=True)
class SettingsCheck:
    problems: list[Problem] = field(default_factory=list)
    warnings: list[Problem] = field(default_factory=list)


def _finding(path: str, rule: str, code: str, message: str) -> Problem:
    # `code` is the first spelling of the rule, kept in `problems[].code`.
    return {"path": path, "rule": rule, "code": code, "message": message}


def snake_path(path: str) -> str:
    """`assessments.checkIn.formId` -> `assessments.check_in.form_id`, the
    spelling the editor's own model uses."""
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", path).lower()


def error_entry(problem: Problem) -> dict[str, str]:
    """The public `errors[]` entry of a problem: where, which rule and a key
    the frontend translates. Never carries a value the user typed."""
    rule = problem["rule"]
    return {
        "path": snake_path(problem["path"]),
        "rule": rule,
        "message_key": f"trainer_settings.{rule}",
    }


# (attribute of the text, name in the JSON path, its limit)
_TEXT_FIELDS = (
    ("title", "title", TITLE_MAX),
    ("body", "body", BODY_MAX),
    ("cta_label", "ctaLabel", CTA_LABEL_MAX),
)


def _stray_braces(value: str, allowed: tuple[str, ...]) -> bool:
    """Whether `value` has a brace that is not part of an allowed placeholder.
    All of them are taken out in one pass over the original text: one at a time,
    removing the inner one of `{thres{score}hold}` would leave an allowed one."""
    left = re.split("|".join(map(re.escape, allowed)), value) if allowed else [value]
    return any("{" in piece or "}" in piece for piece in left)


def congratulations_problems(texts: Congratulations | None) -> list[Problem]:
    """What the congratulations texts break: too long, markup (`<` or `>`: they
    are plain text, shown as text), or any brace that is not exactly `{score}` or
    `{threshold}`. Nothing is echoed: the findings carry a path and a rule."""
    problems: list[Problem] = []
    for purpose in ("checkin", "final"):
        entry = getattr(texts, purpose, None) if texts else None
        if entry is None:
            continue
        for attribute, name, limit in _TEXT_FIELDS:
            value: str | None = getattr(entry, attribute)
            if value is None:
                continue
            path = f"congratulations.{purpose}.{name}"
            if len(value) > limit:
                problems.append(
                    _finding(
                        path,
                        "congratulations_too_long",
                        "too_long",
                        f"This text is longer than {limit} characters",
                    )
                )
            if "<" in value or ">" in value:
                problems.append(
                    _finding(
                        path,
                        "congratulations_markup_not_allowed",
                        "markup_not_allowed",
                        "This text is plain text: it cannot contain < or >",
                    )
                )
            if _stray_braces(value, PLACEHOLDERS):
                problems.append(
                    _finding(
                        path,
                        "congratulations_placeholder_unknown",
                        "placeholder_unknown",
                        "Only the {score} and {threshold} placeholders are allowed",
                    )
                )
    return problems


def trainer_settings_check(settings: MapSettings, catalog: Catalog) -> SettingsCheck:
    problems: list[Problem] = []
    warnings: list[Problem] = []
    trainer = settings.trainer
    if trainer.enabled:
        pack = catalog.pack(trainer.language, trainer.level)
        if pack is None:
            problems.append(
                _finding(
                    "trainer.level",
                    "unknown_pack",
                    "pack_not_found",
                    "There is no content for this language and level",
                )
            )
        elif not pack.served_activities(False):
            warnings.append(
                _finding(
                    "trainer.level",
                    "content_not_published",
                    "content_not_published",
                    "The content of this level is not published yet",
                )
            )
    exams = settings.assessments
    if exams.enabled:
        if exams.check_in is None and exams.final is None:
            problems.append(
                _finding(
                    "assessments",
                    "no_exam_configured",
                    "no_exam_configured",
                    "Configure checkIn, final or both",
                )
            )
        for name, policy in (("checkIn", exams.check_in), ("final", exams.final)):
            if policy is None:
                continue
            path = f"assessments.{name}.formId"
            # A form in any state is accepted: it can be configured before it
            # is published. Only a form that does not exist at all is refused.
            found = catalog.form(policy.form_id)
            if found is None:
                problems.append(
                    _finding(
                        path,
                        "unknown_form",
                        "form_not_found",
                        "There is no exam form with this id",
                    )
                )
            elif trainer.enabled and (
                (form := found[0].forms[policy.form_id]).language,
                form.level,
            ) != (trainer.language, trainer.level):
                # The map says which language and level its students learn; an
                # exam of another level would be a silent, student-facing error.
                # Only checked while the trainer is on: with it off the level is
                # just a default the editor never chose.
                problems.append(
                    _finding(
                        path,
                        "form_level_mismatch",
                        "form_level_mismatch",
                        "This exam form is for another language or level than the map",
                    )
                )
            elif found[0].forms[policy.form_id].status != "published":
                warnings.append(
                    _finding(
                        path,
                        "content_not_published",
                        "content_not_published",
                        "This exam form is not published yet",
                    )
                )
    steps = settings.welcome.steps or []
    for index, step in enumerate(steps):
        if isinstance(step, AssessmentStep) and not (
            exams.enabled and exams.check_in is not None
        ):
            problems.append(
                _finding(
                    f"welcome.steps[{index}]",
                    "assessment_step_needs_check_in",
                    "assessment_step_needs_check_in",
                    "An assessment step needs assessments enabled with a checkIn",
                )
            )
    # The closing flow's final-exam step needs the final exam configured; the
    # form it names is checked above (unknown: 422, unpublished: warning).
    for index, step in enumerate(
        settings.closing.steps or [] if settings.closing else []
    ):
        if isinstance(step, AssessmentStep) and not (
            exams.enabled and exams.final is not None
        ):
            problems.append(
                _finding(
                    f"closing.steps[{index}]",
                    "assessment_step_needs_final",
                    "assessment_step_needs_final",
                    "An assessment step needs assessments enabled with a final",
                )
            )
    problems.extend(congratulations_problems(settings.congratulations))
    return SettingsCheck(problems, warnings)
