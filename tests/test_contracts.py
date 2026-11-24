"""The contract files handed to the plugin match what the backend builds."""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from app.mirror.attempt import MAX_STATEMENT_BYTES
from app.mirror.definition import canonical, definition_hash
from app.mirror.schemas import (
    AttemptStatement,
    DefinitionStatement,
    ResultStatement,
)

ROOT = Path(__file__).resolve().parent.parent / "contracts"


def load(*parts: str) -> Any:
    return json.loads(ROOT.joinpath(*parts).read_text(encoding="utf-8"))


def test_the_generated_files_are_up_to_date() -> None:
    done = subprocess.run(
        [sys.executable, str(ROOT.parent / "tools" / "export_contracts.py"), "--check"],
        capture_output=True,
        check=False,
        text=True,
    )
    assert done.returncode == 0, done.stdout + done.stderr


@pytest.mark.parametrize(
    "name", ["attempt-b2", "attempt-b2-with-text", "attempt-b2-truncated"]
)
def test_the_attempt_examples_match_their_schema(name: str) -> None:
    claims = load("examples", f"{name}.json")
    AttemptStatement.model_validate(claims)
    ResultStatement.model_validate(
        {k: v for k, v in claims.items() if k in ResultStatement.model_fields}
    )


def test_the_definition_example_matches_its_schema_and_its_hash() -> None:
    claims = load("examples", "definition-b2.json")
    parsed = DefinitionStatement.model_validate(claims)
    assert parsed.definition_hash == definition_hash(claims["definition"])
    assert len(parsed.definition.sections) == 5


def test_the_three_text_modes_are_shown() -> None:
    plain = load("examples", "attempt-b2.json")
    text = load("examples", "attempt-b2-with-text.json")
    cut = load("examples", "attempt-b2-truncated.json")
    assert (plain["written_text_included"], plain["truncated"]) == (False, False)
    assert (text["written_text_included"], text["truncated"]) == (True, False)
    assert (cut["written_text_included"], cut["truncated"]) == (False, True)
    assert all(w["text"] for w in text["written"])
    assert all(w["text"] is None for w in plain["written"] + cut["written"])
    assert cut["answers"] == text["answers"]


@pytest.mark.parametrize("name", ["result", "attempt"])
def test_the_signed_vectors_match_their_schema(name: str) -> None:
    claims = load("signing-vectors", f"{name}.json")["claims"]
    model = ResultStatement if name == "result" else AttemptStatement
    model.model_validate(claims)


def test_the_signed_definition_vector_matches_its_schema() -> None:
    DefinitionStatement.model_validate(
        load("signing-vectors", "definition.json")["claims"]
    )


def test_a_statement_with_an_unknown_claim_does_not_match() -> None:
    claims = load("examples", "attempt-b2.json")
    with pytest.raises(ValueError):
        AttemptStatement.model_validate({**claims, "surprise": 1})


def test_no_exam_comes_near_the_cap_and_the_sizes_are_recorded() -> None:
    sizes = load("sizes.json")
    assert sizes["cap_bytes"] == MAX_STATEMENT_BYTES
    assert set(sizes["forms"]) == {"a1-form-a", "a2-form-a", "b2-form-a"}
    for form in sizes["forms"].values():
        assert form["attempt_bytes_worst_case"] < MAX_STATEMENT_BYTES // 2
        assert form["definition_bytes"] < MAX_STATEMENT_BYTES // 2


def test_the_definition_example_is_canonical_json_of_the_stated_size() -> None:
    claims = load("examples", "definition-b2.json")
    sizes = load("sizes.json")["forms"]["b2-form-a"]
    assert len(canonical(claims)) == sizes["definition_bytes"]
