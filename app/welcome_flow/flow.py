"""The rules of a welcome flow that a type cannot say: the legacy fields next to
steps, keys, the position of the greeting, how many quizzes, how much markup,
and what the author's HTML and CSS contain.

`flow_problems` is pure and lists every problem (not just the first), each as
`{path, code, message}` with a JSON-style path (`welcome.steps[2].html`). It
runs when a flow is written (PUT appearance, and later imports), never when one
is read: a stored flow always loads, whatever rules tighten afterwards. Messages
are fixed texts; only the name of an offending tag or attribute is shown back.
"""

import uuid
from collections.abc import Mapping
from typing import Any

from app.schemas.welcome import (
    MAX_CSS_BYTES,
    MAX_FLOW_MARKUP_BYTES,
    MAX_HTML_BYTES,
    MAX_QUIZ_STEPS,
    HtmlStep,
    PdfStep,
    QuizStep,
    Welcome,
    WelcomeStep,
)
from app.welcome_flow.css import check_css
from app.welcome_flow.html import asset_ids_in, check_html

Problem = dict[str, str]


def _problem(path: str, code: str, message: str) -> Problem:
    return {"path": path, "code": code, "message": message}


def flow_problems(welcome: Welcome) -> list[Problem]:
    """Every rule `welcome.steps` breaks, in step order. Empty: it is fine (and
    always empty for a curtain without steps)."""
    steps = welcome.steps
    if steps is None:
        return []
    problems: list[Problem] = []
    legacy = {
        "title": welcome.title,
        "message": welcome.message,
        "video": welcome.video,
        "playOnEnter": welcome.play_on_enter,
    }
    for name, value in legacy.items():
        if value is not None:
            problems.append(
                _problem(
                    f"welcome.{name}",
                    "legacy_fields_with_steps",
                    "Leave this empty: when steps are defined they define the flow",
                )
            )

    seen: set[str] = set()
    quizzes = 0
    markup_bytes = 0
    for index, step in enumerate(steps):
        where = f"welcome.steps[{index}]"
        if step.key in seen:
            problems.append(
                _problem(
                    f"{where}.key",
                    "step_key_duplicate",
                    "Another step already uses this key",
                )
            )
        seen.add(step.key)
        if isinstance(step, WelcomeStep) and index != 0:
            problems.append(
                _problem(
                    where,
                    "welcome_step_position",
                    "The welcome step must be the first step, and there can be only one",
                )
            )
        if isinstance(step, QuizStep):
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
                "welcome.steps",
                "flow_too_large",
                f"The HTML and CSS of the flow add up to more than "
                f"{MAX_FLOW_MARKUP_BYTES // 1024} KB",
            )
        )
        return problems  # too big to be worth parsing
    for index, step in enumerate(steps):
        if isinstance(step, HtmlStep):
            problems.extend(_markup_problems(f"welcome.steps[{index}]", step))
    return problems


def pdf_steps(welcome: Welcome) -> list[tuple[int, PdfStep]]:
    """The steps that name a document, with their position in the flow."""
    return [
        (index, step)
        for index, step in enumerate(welcome.steps or [])
        if isinstance(step, PdfStep)
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
