import json
import shutil
from pathlib import Path

import pytest

from app.trainer.catalog import load_catalog
from app.trainer.pack import PackError, load_pack

BASE = Path(__file__).resolve().parent.parent / "content/trainer"
PACK = BASE / "en/A1"


def test_the_real_pack_loads() -> None:
    pack = load_pack(PACK)
    assert pack.meta.language.code == "en" and pack.meta.level == "A1"
    assert "a1-form-a" in pack.forms
    assert len(pack.curriculum.lessons) == 15
    assert {m.key for m in pack.curriculum.modules} == {"M1", "M2", "M3", "M4"}
    assert pack.served_form("a1-form-a", allow_drafts=False) is None  # a draft
    assert pack.served_form("a1-form-a", allow_drafts=True) is not None


def test_the_catalog_finds_packs_and_forms() -> None:
    catalog = load_catalog(BASE)
    assert catalog.pack("en", "A1") is not None
    assert catalog.pack("fr", "A1") is None
    assert catalog.form("a1-form-a") is not None
    assert catalog.form("nope") is None


def test_content_version_is_stable_and_follows_content(tmp_path: Path) -> None:
    copy = tmp_path / "en/A1"
    shutil.copytree(PACK, copy)
    first = load_pack(copy).content_version
    assert load_pack(copy).content_version == first
    (copy / "course_words.json").write_text(
        json.dumps({"words": ["x"]}), encoding="utf-8"
    )
    assert load_pack(copy).content_version != first


def test_a_bad_pack_is_refused_with_every_problem(tmp_path: Path) -> None:
    copy = tmp_path / "en/A1"
    shutil.copytree(PACK, copy)
    (copy / "lessons.json").write_text(
        '{"modules": [], "lessons": "x"}', encoding="utf-8"
    )
    (copy / "activities/bad.json").write_text('{"id": "a"}', encoding="utf-8")
    with pytest.raises(PackError) as err:
        load_pack(copy)
    text = "\n".join(err.value.problems)
    assert "lessons.json" in text and "bad.json" in text


def test_the_catalog_never_serves_half_a_pack(tmp_path: Path) -> None:
    shutil.copytree(BASE, tmp_path / "t")
    (tmp_path / "t/en/A1/pack.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(PackError):
        load_catalog(tmp_path / "t")


def test_a_form_whose_id_differs_from_its_file_name_is_refused(tmp_path: Path) -> None:
    copy = tmp_path / "en/A1"
    shutil.copytree(PACK, copy)
    (copy / "assessments/a1-form-a.json").rename(copy / "assessments/other.json")
    with pytest.raises(PackError, match="form_id"):
        load_pack(copy)
