"""`If-Match` / `ETag` handling for optimistic concurrency.

A map's `revision` and a bubble's `version` travel as a quoted number in the
`ETag` response header and come back in the `If-Match` request header, e.g.
`If-Match: "3"`. Weak validators (`W/"3"`), lists and `*` are not accepted: the
number is a counter we own, not a representation hash.
"""

import re
from typing import Annotated

from fastapi import Depends, Header, Response

from app.exceptions import InvalidIfMatchError, PreconditionRequiredError

# int32-safe: the counters are INT columns.
_QUOTED_NUMBER = re.compile(r'"(\d{1,9})"')


def parse_if_match(raw: str | None, *, required: bool) -> int | None:
    """The number in an `If-Match` value.

    Missing: `PreconditionRequiredError` (428) when `required`, else None.
    Anything that is not exactly one quoted number: `InvalidIfMatchError` (400),
    even when the header is optional (a malformed value is never ignored).
    """
    if raw is None:
        if required:
            raise PreconditionRequiredError
        return None
    match = _QUOTED_NUMBER.fullmatch(raw.strip())
    if match is None:
        raise InvalidIfMatchError(raw)
    return int(match[1])


def require_if_match(
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> int:
    value = parse_if_match(if_match, required=True)
    assert value is not None
    return value


def optional_if_match(
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> int | None:
    return parse_if_match(if_match, required=False)


IfMatch = Annotated[int, Depends(require_if_match)]
OptionalIfMatch = Annotated[int | None, Depends(optional_if_match)]


def etag_value(number: int) -> str:
    return f'"{number}"'


def set_etag(response: Response, number: int) -> None:
    response.headers["ETag"] = etag_value(number)
