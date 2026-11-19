# ruff: noqa: F811
"""An exam form says which language, level and purpose it is, and titles differ
by level."""

from pathlib import Path
from typing import Any

from app.trainer.catalog import load_catalog
from tests.test_assessment_api import FakeClock, clock, env  # noqa: F401

CONTENT = "content/trainer"


async def test_the_forms_list_says_language_level_and_purpose(
    env: dict[str, Any],
) -> None:
    ed = await env["editor"]()
    r = await ed.get("/trainer/assessments/forms")
    assert r.status_code == 200
    forms = {f["id"]: f for f in r.json()}
    for form_id, level in (("a1-form-a", "A1"), ("a2-form-a", "A2")):
        assert forms[form_id]["language"] == "en"
        assert forms[form_id]["level"] == level
        assert forms[form_id]["purpose"] == "check_in"
        # What an old client read before is still there, with the same meaning.
        assert {"id", "title", "publication_state"} <= set(forms[form_id])
        assert forms[form_id]["title"]


def test_every_form_title_is_unique_and_names_its_level() -> None:
    catalog = load_catalog(Path(CONTENT))
    pairs = [
        (key, form)
        for key, pack in catalog.packs.items()
        for form in pack.forms.values()
    ]
    assert pairs
    titles = [form.title for _, form in pairs]
    assert len(set(titles)) == len(titles)
    for (language, level), form in pairs:
        # The form says the same as the pack it lives in, and so does its title.
        assert (form.language, form.level) == (language, level)
        assert level in form.title
