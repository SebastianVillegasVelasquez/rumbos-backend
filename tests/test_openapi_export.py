"""The exported OpenAPI document: the check CI runs and what it catches."""

import json
from pathlib import Path

from app.scripts.export_openapi import main, render


def test_the_committed_document_is_the_one_the_code_builds() -> None:
    assert main(["--check"]) == 0


def test_check_fails_for_a_stale_or_missing_document(tmp_path: Path) -> None:
    target = tmp_path / "openapi.json"
    assert main(["--check", str(target)]) == 1  # missing
    target.write_text(json.dumps({"openapi": "3.1.0"}), encoding="utf-8")
    assert main(["--check", str(target)]) == 1  # stale


def test_export_writes_what_check_accepts(tmp_path: Path) -> None:
    target = tmp_path / "openapi.json"
    assert main([str(target)]) == 0
    assert target.read_text(encoding="utf-8") == render()
    assert main(["--check", str(target)]) == 0
