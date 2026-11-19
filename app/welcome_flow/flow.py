"""The rules of a welcome or closing flow that a type cannot say: the legacy
fields next to steps, keys, the position of the greeting, how many quizzes, how
much markup, and what the author's HTML and CSS contain.

The closing flow is the welcome flow's twin and shares every rule here. It
differs only in what it may hold: content steps (`html`, `pdf`, `video`) and at
most one `assessment` step of purpose `final`, never the greeting or a Moodle
`quiz`; the welcome flow's assessment step is the `check_in` one. A closing flow
must have steps. Step keys are also unique across the two flows, because Moodle
keeps the progress of a step by map and key alone.

`flow_problems` is pure and lists every problem (not just the first), each as
`{path, code, message}` with a JSON-style path (`welcome.steps[2].html`). It
runs when a flow is written (PUT appearance, and later imports), never when one
is read: a stored flow always loads, whatever rules tighten afterwards. Messages
are fixed texts; only the name of an offending tag or attribute is shown back.
"""

import uuid
from collections.abc import Collection, Mapping
from typing import Any, Literal

from app.schemas.map_settings import MapSettings
from app.schemas.welcome import (
    MAX_CSS_BYTES,
    MAX_FLOW_MARKUP_BYTES,
    MAX_HTML_BYTES,
    MAX_QUIZ_STEPS,
    AssessmentStep,
    HtmlStep,
    PdfStep,
    QuizStep,
    Welcome,
    WelcomeStep,
)
from app.welcome_flow.css import check_css
from app.welcome_flow.html import asset_ids_in, check_html

Problem = dict[str, str]
FlowName = Literal["welcome", "closing"]

# The exam each flow may hold.
_PURPOSE: dict[str, str] = {"welcome": "check_in", "closing": "final"}


def _problem(path: str, code: str, message: str) -> Problem:
    return {"path": path, "code": code, "message": message}


def flow_problems(
    welcome: Welcome,
    *,
    name: FlowName = "welcome",
    other_keys: Collection[str] = (),
) -> list[Problem]:
    """Every rule `welcome.steps` breaks, in step order. Empty: it is fine (and
    always empty for a welcome curtain without steps).

    `name` is the flow checked (`welcome` or `closing`): it prefixes every path
    and selects what the flow may hold. `other_keys` are the step keys of the
    other flow, which no step here may reuse."""
    steps = welcome.steps
    closing = name == "closing"
    if steps is None:
        if closing:
            return [
                _problem(
                    name,
                    "closing_needs_steps",
                    "A closing flow needs at least one step",
                )
            ]
        return []
    problems: list[Problem] = []
    legacy = {
        "title": welcome.title,
        "message": welcome.message,
        "video": welcome.video,
        "playOnEnter": welcome.play_on_enter,
    }
    for field, value in legacy.items():
        if value is not None:
            problems.append(
                _problem(
                    f"{name}.{field}",
                    "legacy_fields_with_steps",
                    "Leave this empty: when steps are defined they define the flow",
                )
            )

    seen: set[str] = set()
    quizzes = 0
    assessments = 0
    markup_bytes = 0
    for index, step in enumerate(steps):
        where = f"{name}.steps[{index}]"
        if step.key in seen or step.key in other_keys:
            problems.append(
                _problem(
                    f"{where}.key",
                    "step_key_duplicate",
                    "Another step already uses this key",
                )
            )
        seen.add(step.key)
        if isinstance(step, WelcomeStep):
            if closing:
                problems.append(_not_allowed(where))
            elif index != 0:
                problems.append(
                    _problem(
                        where,
                        "welcome_step_position",
                        "The welcome step must be the first step, "
                        "and there can be only one",
                    )
                )
        if isinstance(step, AssessmentStep):
            assessments += 1
            if assessments > 1:
                problems.append(
                    _problem(
                        where,
                        "assessment_too_many",
                        "Only one assessment step is allowed",
                    )
                )
            if step.purpose != _PURPOSE[name]:
                problems.append(
                    _problem(
                        f"{where}.purpose",
                        "assessment_purpose",
                        f"An assessment step here must have purpose {_PURPOSE[name]}",
                    )
                )
        if isinstance(step, QuizStep):
            if closing:
                problems.append(_not_allowed(where))
            quizzes += 1
            if quizzes > MAX_QUIZ_STEPS:
                problems.append(
                    _problem(
                        where,
                        "quiz_too_many",
                        f"At most {MAX_QUIZ_STEPS} quiz steps are allowed",
                    )
                )
        if isinstance(step, HtmlStep):
            markup_bytes += _byte_size(step.html) + _byte_size(step.css or "")

    if markup_bytes > MAX_FLOW_MARKUP_BYTES:
        problems.append(
            _problem(
                f"{name}.steps",
                "flow_too_large",
                f"The HTML and CSS of the flow add up to more than "
                f"{MAX_FLOW_MARKUP_BYTES // 1024} KB",
            )
        )
        return problems  # too big to be worth parsing
    for index, step in enumerate(steps):
        if isinstance(step, HtmlStep):
            problems.extend(_markup_problems(f"{name}.steps[{index}]", step))
    return problems


def _not_allowed(where: str) -> Problem:
    return _problem(
        where,
        "step_type_not_allowed",
        "A closing flow holds only content steps and a final assessment",
    )


def settings_flows(settings: MapSettings) -> list[tuple[FlowName, Welcome]]:
    """The flows a map defines: the welcome flow, then the closing one if any."""
    found: list[tuple[FlowName, Welcome]] = [("welcome", settings.welcome)]
    if settings.closing is not None:
        found.append(("closing", settings.closing))
    return found


def pdf_steps(welcome: Welcome) -> list[tuple[int, PdfStep]]:
    """The steps that name a document, with their position in the flow."""
    return [
        (index, step)
        for index, step in enumerate(welcome.steps or [])
        if isinstance(step, PdfStep)
    ]


def settings_pdf_ids(settings: MapSettings) -> list[uuid.UUID]:
    """The document of every PDF step of every flow of a map, in flow order."""
    return [
        step.asset_id
        for _, flow in settings_flows(settings)
        for _, step in pdf_steps(flow)
    ]


def stored_pdf_ids(welcome: Mapping[str, Any] | None) -> list[uuid.UUID]:
    """The document of every PDF step of a stored flow, in step order."""
    found: list[uuid.UUID] = []
    for step in (welcome or {}).get("steps") or []:
        if isinstance(step, Mapping) and step.get("type") == "pdf":
            try:
                found.append(uuid.UUID(str(step.get("assetId"))))
            except ValueError:
                pass
    return found


def stored_settings_pdf_ids(settings: Mapping[str, Any]) -> list[uuid.UUID]:
    """The documents of the welcome flow, then of the closing one, in stored
    settings (a snapshot's)."""
    return stored_pdf_ids(settings.get("welcome")) + stored_pdf_ids(
        settings.get("closing")
    )


def stored_flow_asset_ids(welcome: Mapping[str, Any] | None) -> set[uuid.UUID]:
    """The assets a stored welcome flow depends on: the document of every PDF
    step and every image its HTML steps show. Read from the stored JSON, so it
    works on a snapshot and never raises on what it does not recognise."""
    found: set[uuid.UUID] = set()
    for step in (welcome or {}).get("steps") or []:
        if not isinstance(step, Mapping):
            continue
        if step.get("type") == "pdf":
            try:
                found.add(uuid.UUID(str(step.get("assetId"))))
            except ValueError:
                pass
        elif step.get("type") == "html" and isinstance(step.get("html"), str):
            found.update(asset_ids_in(step["html"]))
    return found


def stored_settings_asset_ids(settings: Mapping[str, Any]) -> set[uuid.UUID]:
    """The assets the welcome and closing flows of stored settings depend on."""
    return stored_flow_asset_ids(settings.get("welcome")) | stored_flow_asset_ids(
        settings.get("closing")
    )


def _byte_size(text: str) -> int:
    return len(text.encode("utf-8", errors="replace"))


def _markup_problems(where: str, step: HtmlStep) -> list[Problem]:
    problems: list[Problem] = []
    if _byte_size(step.html) > MAX_HTML_BYTES:
        problems.append(
            _problem(
                f"{where}.html",
                "too_long",
                f"The HTML is over {MAX_HTML_BYTES // 1024} KB",
            )
        )
    else:
        refused = check_html(step.html)
        if refused is not None:
            problems.append(
                _problem(
                    f"{where}.html",
                    "html_forbidden_markup",
                    f"The HTML contains something that is not allowed: {refused}",
                )
            )
    if step.css is not None:
        if _byte_size(step.css) > MAX_CSS_BYTES:
            problems.append(
                _problem(
                    f"{where}.css",
                    "too_long",
                    f"The CSS is over {MAX_CSS_BYTES // 1024} KB",
                )
            )
        else:
            refused = check_css(step.css)
            if refused is not None:
                problems.append(
                    _problem(
                        f"{where}.css",
                        "css_forbidden",
                        f"The CSS contains something that is not allowed: {refused}",
                    )
                )
    return problems
