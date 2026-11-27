"""The texts of the other moments of the closing journey, written by the editor:

* `failure`: after a failed attempt. May carry `{attempt}`, `{attempts_max}`,
  `{attempts_left}` and `{threshold}`.
* `exhausted`: every attempt at the final used without passing.
* `routeInstructions`: on the map, after a failed check-in.

The last two take no placeholder. The rules are those of the congratulations
texts (`app.schemas.congratulations`): plain text the frontend renders as text,
cleaned the same way, the same three fields and caps, and checked at write time
by `app.trainer.settings_check`. A null block or field means the frontend's
default copy: the backend holds no copy and never fills a placeholder.
"""

from pydantic import ConfigDict, Field

from app.schemas.base import ApiModel
from app.schemas.congratulations import (
    BODY_MAX,
    CTA_LABEL_MAX,
    TITLE_MAX,
    LineText,
    ParagraphText,
)

# The placeholders each block may contain, exactly as spelled here, keyed by the
# block's name in the settings JSON.
JOURNEY_PLACEHOLDERS: dict[str, tuple[str, ...]] = {
    "failure": ("{attempt}", "{attempts_max}", "{attempts_left}", "{threshold}"),
    "exhausted": (),
    "routeInstructions": (),
}


class JourneyText(ApiModel):
    """One block of the journey. Plain text; which placeholders it may contain
    depends on the block it is in (see `MapSettings`). Each field null: the
    default copy."""

    model_config = ConfigDict(extra="forbid")

    title: LineText = Field(
        default=None, description=f"One line, up to {TITLE_MAX} characters."
    )
    body: ParagraphText = Field(
        default=None,
        description=f"Up to {BODY_MAX} characters, line breaks allowed.",
    )
    cta_label: LineText = Field(
        default=None,
        description=f"The button's label: one line, up to {CTA_LABEL_MAX} characters.",
    )

    def is_empty(self) -> bool:
        return self.title is None and self.body is None and self.cta_label is None
