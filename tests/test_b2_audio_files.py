"""The B2 audio files on disk match the manifest and the build inventory."""

import json
from pathlib import Path

from app.scripts import tts
from app.trainer.pack import load_pack

ROOT = Path(__file__).resolve().parent.parent / "content/trainer/en/B2"
PACK = load_pack(ROOT)
BUILD = json.loads((ROOT / tts.BUILD_FILE).read_text(encoding="utf-8"))["clips"]


def test_every_manifest_clip_has_a_file_a_duration_and_an_inventory_row() -> None:
    rows = {c["id"]: c for c in BUILD}
    assert set(rows) == set(PACK.audio)
    for entry in PACK.audio.values():
        assert (ROOT / entry.file).is_file(), entry.id
        assert entry.duration_s and entry.duration_s > 0, entry.id
        assert rows[entry.id]["duration_s"] == entry.duration_s


def test_file_hashes_match_the_inventory() -> None:
    for row in BUILD:
        path = ROOT / row["file"]
        assert tts.sha256_of(path) == row["sha256"], row["id"]
        assert path.stat().st_size == row["bytes"], row["id"]


def test_nothing_is_stale_and_no_orphan_files() -> None:
    plan = tts.make_plan(PACK, tts.load_config(ROOT), [])
    assert not plan.make and not plan.problems
    on_disk = {p.name for p in (ROOT / "audio").glob("*")}
    assert on_disk == {Path(e.file).name for e in PACK.audio.values()}
