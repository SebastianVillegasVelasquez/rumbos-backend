"""The committed Forma A, and the tool that produces it from the client's
`.docx`. The document itself is not in the repository, so the re-import check
runs only where `docs/a1-source.docx` exists."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from app.trainer.assessment_form import (
    AssessmentForm,
    ChoiceAssessmentItem,
    MatchAudioItem,
    ProductiveItem,
)

ROOT = Path(__file__).resolve().parent.parent
FORM_PATH = ROOT / "content/trainer/en/A1/assessments/a1-form-a.json"
SOURCE = ROOT / "docs/a1-source.docx"


@pytest.fixture(scope="module")
def form() -> AssessmentForm:
    return AssessmentForm.model_validate_json(FORM_PATH.read_text(encoding="utf-8"))


def _count(form: AssessmentForm, key: str) -> int:
    """Objective points of a section (a matching item is one per pair)."""
    total = 0
    for item in form.section(key).items():
        if isinstance(item, ChoiceAssessmentItem):
            total += 1
        elif isinstance(item, MatchAudioItem):
            total += item.points
    return total


def test_section_sizes_and_weights(form: AssessmentForm) -> None:
    assert [(s.key, s.weight) for s in form.sections] == [
        ("listening", 25),
        ("reading", 25),
        ("use_of_english", 20),
        ("writing", 15),
        ("speaking", 15),
    ]
    assert _count(form, "listening") == 15
    assert _count(form, "reading") == 15
    assert _count(form, "use_of_english") == 15
    assert form.section("writing").max_points() == 10
    assert form.section("speaking").max_points() == 10


def test_listening_is_5_5_and_five_pairs(form: AssessmentForm) -> None:
    parts = {p.part_key: p for p in form.section("listening").parts}
    assert len(parts["a"].items) == 5
    assert len(parts["b"].items) == 5
    (match,) = parts["c"].items
    assert isinstance(match, MatchAudioItem)
    assert len(match.clips) == 5 and match.points == 5
    assert {i for i, _ in match.key} == {0, 1, 2, 3, 4}


def test_use_of_english_is_8_grammar_and_7_vocabulary(form: AssessmentForm) -> None:
    parts = {p.part_key: p for p in form.section("use_of_english").parts}
    assert (len(parts["grammar"].items), len(parts["vocabulary"].items)) == (8, 7)


def test_two_writing_and_two_speaking_tasks(form: AssessmentForm) -> None:
    for key in ("writing", "speaking"):
        items = form.section(key).items()
        assert len(items) == 2
        assert all(isinstance(i, ProductiveItem) for i in items)


def test_every_objective_item_has_one_key_among_its_options(
    form: AssessmentForm,
) -> None:
    for section in form.sections:
        for item in section.items():
            if isinstance(item, ChoiceAssessmentItem):
                assert item.key in {o.id for o in item.options}, item.item_id
                assert len(item.options) == 3, item.item_id
            elif isinstance(item, MatchAudioItem):
                places = {p.id for p in item.places}
                assert all(place in places for _, place in item.key)


def test_no_objective_item_lacks_a_competence_code(form: AssessmentForm) -> None:
    for section in form.sections:
        for item in section.items():
            if not isinstance(item, ProductiveItem):
                assert item.competence is not None, item.item_id
                assert item.competence.modules, item.item_id


def test_item_ids_are_unique(form: AssessmentForm) -> None:
    ids = [i.item_id for s in form.sections for i in s.items()]
    assert len(ids) == len(set(ids)) == 45


def test_listening_items_carry_their_transcript(form: AssessmentForm) -> None:
    for part in form.section("listening").parts:
        for item in part.items:
            if isinstance(item, ChoiceAssessmentItem):
                assert item.audio and item.audio.transcript
            if isinstance(item, MatchAudioItem):
                assert all(c.transcript for c in item.clips)


def test_image_sheets_have_alt_text(form: AssessmentForm) -> None:
    sheets = [
        p.shared
        for s in form.sections
        for p in s.parts
        if p.shared and p.shared.kind == "image"
    ]
    assert {s.image_id for s in sheets if s} == {"LA", "RA", "SA"}
    assert all(s and s.alt_text and s.design_note for s in sheets)


def test_the_import_is_a_draft_until_someone_reviews_it(form: AssessmentForm) -> None:
    assert form.status == "draft"
    assert form.reviewed_by is None and form.reviewed_at is None


def test_the_anomalies_are_recorded_not_fixed(form: AssessmentForm) -> None:
    notes = "\n".join(form.source_notes)
    assert "reading.a.01" in notes and "no lesson number" in notes
    assert "listening.c.01" in notes
    assert "pedagogical sign-off" in notes


@pytest.mark.skipif(not SOURCE.exists(), reason="the client's .docx is not present")
def test_reimporting_the_document_reproduces_the_committed_file(
    form: AssessmentForm,
) -> None:
    spec = importlib.util.spec_from_file_location(
        "import_forma_a", ROOT / "tools/import_forma_a.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["import_forma_a"] = module
    spec.loader.exec_module(module)
    fresh = module.parse(module.read_section(SOURCE))
    assert json.loads(fresh.model_dump_json()) == json.loads(form.model_dump_json())
