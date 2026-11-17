"""Write-time checks of the trainer and assessment settings of a map against
the content that is actually deployed."""

from app.schemas.map_settings import MapSettings
from app.schemas.welcome import AssessmentStep
from app.trainer.catalog import Catalog

Problem = dict[str, str]


def _problem(path: str, code: str, message: str) -> Problem:
    return {"path": path, "code": code, "message": message}


def trainer_settings_problems(settings: MapSettings, catalog: Catalog) -> list[Problem]:
    problems: list[Problem] = []
    trainer = settings.trainer
    if trainer.enabled and catalog.pack(trainer.language, trainer.level) is None:
        problems.append(
            _problem(
                "trainer.level",
                "pack_not_found",
                "There is no content for this language and level",
            )
        )
    exams = settings.assessments
    if exams.enabled:
        if exams.check_in is None and exams.final is None:
            problems.append(
                _problem(
                    "assessments",
                    "no_exam_configured",
                    "Configure checkIn, final or both",
                )
            )
        for name, policy in (("checkIn", exams.check_in), ("final", exams.final)):
            if policy is not None and catalog.form(policy.form_id) is None:
                problems.append(
                    _problem(
                        f"assessments.{name}.formId",
                        "form_not_found",
                        "There is no exam form with this id",
                    )
                )
    steps = settings.welcome.steps or []
    for index, step in enumerate(steps):
        if isinstance(step, AssessmentStep) and not (
            exams.enabled and exams.check_in is not None
        ):
            problems.append(
                _problem(
                    f"welcome.steps[{index}]",
                    "assessment_step_needs_check_in",
                    "An assessment step needs assessments enabled with a checkIn",
                )
            )
    return problems
