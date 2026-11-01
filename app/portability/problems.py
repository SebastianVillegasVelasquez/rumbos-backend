"""What is wrong with an archive, as `{path, code, message}` dicts.

The same shape `ContentInvalidError` uses. The messages are fixed: the offending
input is never echoed, because it comes from a file somebody else wrote.
"""

import re
from typing import Any

from pydantic import ValidationError

# A response never lists more than this many problems.
MAX_PROBLEMS = 50

_CODE_OF = {
    "missing": "required",
    "extra_forbidden": "unknown_field",
    "string_too_short": "too_short",
    "too_short": "too_short",
    "string_too_long": "too_long",
    "too_long": "too_long",
    "literal_error": "invalid_value",
    "enum": "invalid_value",
    "string_pattern_mismatch": "invalid_format",
    "greater_than": "out_of_range",
    "greater_than_equal": "out_of_range",
    "less_than": "out_of_range",
    "less_than_equal": "out_of_range",
    "string_type": "invalid_type",
    "int_type": "invalid_type",
    "float_type": "invalid_type",
    "bool_type": "invalid_type",
    "dict_type": "invalid_type",
    "list_type": "invalid_type",
    "model_type": "invalid_type",
    "model_attributes_type": "invalid_type",
    "int_parsing": "invalid_type",
    "float_parsing": "invalid_type",
    "uuid_parsing": "invalid_format",
    "uuid_type": "invalid_type",
    "union_tag_invalid": "unknown_type",
    "union_tag_not_found": "required",
    "datetime_parsing": "invalid_format",
    "datetime_type": "invalid_type",
    "timezone_aware": "invalid_format",
}

_MESSAGES = {
    "required": "This field is required",
    "unknown_field": "This field is not allowed here",
    "too_short": "This value is too short",
    "too_long": "This value is too long",
    "invalid_value": "This value is not allowed",
    "invalid_format": "This value has the wrong format",
    "out_of_range": "This value is out of range",
    "invalid_type": "This value has the wrong type",
    "unknown_type": "Unknown type",
    "invalid": "Invalid value",
}

# Class names that pydantic adds to a location when a union is involved.
_UNION_BRANCH = re.compile(r"^[A-Z]")


def problem(path: str, code: str, message: str) -> dict[str, str]:
    return {"path": path, "code": code, "message": message}


def join(path: str, part: str | int) -> str:
    """`maps[0].title` from `maps[0]` and `title`."""
    if isinstance(part, int):
        return f"{path}[{part}]"
    return f"{path}.{part}" if path else part


def from_validation_error(
    error: ValidationError, prefix: str = ""
) -> list[dict[str, str]]:
    """Every error of `error` as a problem, located under `prefix`."""
    found: list[dict[str, str]] = []
    for item in error.errors(
        include_input=False, include_url=False, include_context=False
    ):
        path = prefix
        for part in item["loc"]:
            if isinstance(part, str) and _UNION_BRANCH.match(part):
                continue
            path = join(path, part)
        code = _CODE_OF.get(item["type"], "invalid")
        found.append(problem(path, code, _MESSAGES[code]))
        if len(found) >= MAX_PROBLEMS:
            break
    return found


def capped(problems: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return problems[:MAX_PROBLEMS]
