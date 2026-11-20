"""A1 as a draft. The real A1 pack is published, so tests about what happens to
unpublished content use these copies instead (nothing on disk is changed)."""

import dataclasses
import json
from pathlib import Path

from app.trainer.catalog import Catalog, load_catalog
from app.trainer.pack import Pack

CONTENT = Path("content/trainer")


def draft_pack(pack: Pack) -> Pack:
    """The same pack with the pack, its activities and its forms in `draft`."""
    undo = {"status": "draft", "reviewed_by": None, "reviewed_at": None}
    return dataclasses.replace(
        pack,
        # the exam engine caches per content_version: a draft must not reuse it
        content_version=f"draft-{pack.content_version}",
        meta=pack.meta.model_copy(update={"status": "draft"}),
        activities={k: a.model_copy(update=undo) for k, a in pack.activities.items()},
        forms={k: f.model_copy(update=undo) for k, f in pack.forms.items()},
    )


def draft_a1_catalog() -> Catalog:
    real = load_catalog(CONTENT)
    pack = real.pack("en", "A1")
    assert pack is not None
    return Catalog({("en", "A1"): draft_pack(pack)})


def reset_files_to_draft(root: Path) -> None:
    """Puts a copied A1 pack directory (`root`) back to `draft` on disk."""
    files = [
        root / "pack.json",
        *(root / "activities").glob("*.json"),
        *(root / "assessments").glob("*.json"),
    ]
    for path in files:
        data = json.loads(path.read_text(encoding="utf-8"))
        data["status"] = "draft"
        for key in ("reviewed_by", "reviewed_at"):
            data.pop(key, None) if path.name == "pack.json" else data.update(
                {key: None}
            )
        path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
