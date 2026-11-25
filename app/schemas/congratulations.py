"""The text of the congratulations screen, one per exam: `checkin` for a passed
check-in and `final` for a passed final. The screen itself is the frontend's
Rumbos card; the editor only chooses what it says.

Everything is plain text the frontend renders as text, never as HTML. A text may
carry two placeholders and nothing else: `{score}` (the student's score, as the
exam reports it) and `{threshold}` (the pass mark the attempt was graded with).
The frontend fills them from the outcome it already has; nothing here is ever
interpolated, evaluated or formatted by the backend.

`null` (the whole object, one purpose, or one field) means the frontend's default
copy. A text is cleaned like every other text of the settings (normalised,
control characters dropped, ends trimmed) and one that ends up empty is null.

This module holds the shape only. The limits and what a text may not contain are
checked at write time by `app.trainer.settings_check` (so a refusal is reported
with the same `errors[{path, rule, message_key}]` as the other settings), and a
read never re-runs them: a stored text always loads.
"""

from typing import Annotated, Any

from pydantic import BeforeValidator, ConfigDict, Field, StrictStr

from app.schemas.base import ApiModel
from app.schemas.content import clean_text

TITLE_MAX = 80
BODY_MAX = 600
CTA_LABEL_MAX = 40
# The only placeholders a text may contain, exactly as spelled here.
PLACEHOLDERS = ("{score}", "{threshold}")


def _none_if_empty(value: Any) -> Any:
    return None if value == "" else value


def _line(value: Any) -> Any:
    return _none_if_empty(clean_text(value, multiline=False))


def _paragraph(value: Any) -> Any:
    return _none_if_empty(clean_text(value, multiline=True))


LineText = Annotated[StrictStr | None, BeforeValidator(_line)]
ParagraphText = Annotated[StrictStr | None, BeforeValidator(_paragraph)]


class _Strict(ApiModel):
    model_config = ConfigDict(extra="forbid")


class CongratulationsText(_Strict):
    """What one congratulations screen says. Each field null: the default copy."""

    title: LineText = Field(
        default=None,
        description=f"One line, up to {TITLE_MAX} characters. Plain text; "
        "may contain {score} and {threshold}.",
    )
    body: ParagraphText = Field(
        default=None,
        description=f"Up to {BODY_MAX} characters, line breaks allowed. Plain "
        "text; may contain {score} and {threshold}.",
    )
    cta_label: LineText = Field(
        default=None,
        description=f"The button's label: one line, up to {CTA_LABEL_MAX} "
        "characters. Plain text; may contain {score} and {threshold}.",
    )

    def is_empty(self) -> bool:
        return self.title is None and self.body is None and self.cta_label is None


class Congratulations(_Strict):
    """The two screens: after a passed check-in (`checkin`) and after a passed
    final (`final`). Placeholders: `{score}` and `{threshold}` only."""

    checkin: CongratulationsText | None = None
    final: CongratulationsText | None = None

    def is_empty(self) -> bool:
        return all(t is None or t.is_empty() for t in (self.checkin, self.final))
