"""`app.scripts.content publish`: dry run, confirmation, refusals and rollback."""

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.scripts.content import AUDIT_FILE, main
from app.trainer.pack import load_pack

NOW = datetime(2026, 11, 20, 15, 30, tzinfo=UTC)
GRAMMAR = "en-a1-1.1-grammar"
FORM = "a1-form-a"


@pytest.fixture
def base(tmp_path: Path) -> Path:
    shutil.copytree(Path("content/trainer/en/A1"), tmp_path / "en" / "A1")
    return tmp_path


def set_status(base: Path, relative: str, status: str) -> None:
    path = base / "en" / "A1" / relative
    data = json.loads(path.read_text(encoding="utf-8"))
    data["status"] = status
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", "utf-8")


def snapshot(base: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(base)): p.read_bytes()
        for p in sorted(base.rglob("*"))
        if p.is_file()
    }


def run(base: Path, *args: str) -> tuple[int, str]:
    lines: list[str] = []
    code = main(
        ["publish", "--content-dir", str(base), *args],
        now=NOW,
        out=lines.append,
    )
    return code, "\n".join(lines)


def test_a_draft_cannot_be_published(base: Path) -> None:
    before = snapshot(base)
    code, text = run(
        base,
        "--pack",
        "en/A1",
        "--activities",
        GRAMMAR,
        "--reviewer",
        "Ana",
        "--confirm",
    )
    assert code == 1
    assert "is draft; a person must review it first" in text
    assert snapshot(base) == before


def test_the_dry_run_lists_the_changes_and_writes_nothing(base: Path) -> None:
    set_status(base, f"activities/{GRAMMAR}.json", "reviewed")
    before = snapshot(base)
    code, text = run(
        base, "--pack", "en/A1", "--activities", GRAMMAR, "--reviewer", "Ana"
    )
    assert code == 0
    assert f"activity {GRAMMAR}" in text
    assert "pack.json: draft -> published" in text
    assert "Dry run" in text
    assert snapshot(base) == before
    assert not (base / AUDIT_FILE).exists()


def test_confirm_publishes_audits_and_changes_the_version(base: Path) -> None:
    set_status(base, f"activities/{GRAMMAR}.json", "reviewed")
    old = load_pack(base / "en/A1").content_version
    code, text = run(
        base,
        "--pack",
        "en/A1",
        "--activities",
        GRAMMAR,
        "--reviewer",
        " Ana Ruiz ",
        "--confirm",
    )
    assert code == 0, text
    pack = load_pack(base / "en/A1")
    activity = pack.activities[GRAMMAR]
    assert activity.status == "published"
    assert activity.reviewed_by == "Ana Ruiz"
    assert activity.reviewed_at == NOW
    assert pack.meta.status == "published"
    # Everything else stays a draft.
    assert [a.id for a in pack.served_activities(False)] == [GRAMMAR]
    lines = (base / AUDIT_FILE).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["reviewer"] == "Ana Ruiz"
    assert entry["activities"] == [GRAMMAR] and entry["forms"] == []
    assert entry["pack"] == "en/A1" and entry["at"] == "2026-11-20T15:30:00Z"
    assert entry["content_version_before"] == old
    assert entry["content_version_after"] == pack.content_version != old


def test_all_means_every_reviewed_item_and_nothing_else(base: Path) -> None:
    for name in ("en-a1-1.1-grammar", "en-a1-1.2-grammar"):
        set_status(base, f"activities/{name}.json", "reviewed")
    code, _ = run(
        base, "--pack", "en/A1", "--activities", "all", "--reviewer", "Ana", "--confirm"
    )
    assert code == 0
    pack = load_pack(base / "en/A1")
    assert sorted(a.id for a in pack.served_activities(False)) == [
        "en-a1-1.1-grammar",
        "en-a1-1.2-grammar",
    ]


def test_a_form_without_its_audio_is_rolled_back(base: Path) -> None:
    """The exam's listening items need audio files, which the pack does not
    have yet: the linter refuses and every file goes back as it was."""
    set_status(base, f"assessments/{FORM}.json", "reviewed")
    before = snapshot(base)
    code, text = run(
        base, "--pack", "en/A1", "--forms", FORM, "--reviewer", "Ana", "--confirm"
    )
    assert code == 1
    assert "every file was put back" in text
    assert "audio" in text
    assert snapshot(base) == before
    assert not (base / AUDIT_FILE).exists()


def test_unknown_published_or_empty_requests_are_refused(base: Path) -> None:
    set_status(base, f"activities/{GRAMMAR}.json", "published")
    code, text = run(
        base,
        "--pack",
        "en/A1",
        "--activities",
        GRAMMAR,
        "nope",
        "--reviewer",
        "Ana",
        "--confirm",
    )
    assert code == 1
    assert f"activity {GRAMMAR}: already published" in text
    assert "activity nope: not in the pack" in text
    code, text = run(
        base, "--pack", "en/A1", "--activities", "all", "--reviewer", "Ana"
    )
    assert code == 1 and "nothing to publish" in text


def test_a_reviewer_and_a_pack_are_required(base: Path) -> None:
    assert (
        run(base, "--pack", "en/A1", "--activities", "all", "--reviewer", " ")[0] == 2
    )
    assert (
        run(base, "--pack", "../x", "--activities", "all", "--reviewer", "Ana")[0] == 2
    )
