"""Loader for the real `core_course_get_contents` payload (course 8).

The JSON file is never edited. Tests that need a variant take a deep copy of
the raw payload, change it in code, and parse that.
"""

import copy
import json
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from app.moodle.schemas import MoodleSection

FIXTURE = Path(__file__).parent / "fixtures" / "moodle_course_contents_course8.json"

# Module ids in Moodle's course order (not id order), all inside section id 25.
COURSE8_MODULE_ORDER = [28, 27, 29, 31, 30, 32, 25, 33]

_SECTIONS = TypeAdapter(list[MoodleSection])


def raw_course8() -> list[dict[str, Any]]:
    """A fresh deep copy of the raw payload, safe to mutate."""
    data: list[dict[str, Any]] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return copy.deepcopy(data)


def parse(raw: list[dict[str, Any]]) -> list[MoodleSection]:
    return _SECTIONS.validate_python(raw)


def course8() -> list[MoodleSection]:
    return parse(raw_course8())
