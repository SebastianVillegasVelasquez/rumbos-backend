"""The B1 audio files on disk match the manifest and the build inventory."""

import json
import re
from pathlib import Path

from app.scripts import tts
from app.trainer.pack import load_pack

ROOT = Path(__file__).resolve().parent.parent / "content/trainer/en/B1"
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


def test_the_voices_and_sound_are_the_a2_and_b2_ones() -> None:
    """B1 reuses the A2/B2 engine, voices, pace and format: only the pack differs."""
    config = tts.load_config(ROOT)
    for level in ("A2", "B2"):
        assert config == tts.load_config(ROOT.parent / level)


def test_the_exam_clips_say_nothing_in_their_names() -> None:
    names = [p.name for p in (ROOT / "audio").glob("b1fa-*.mp3")]
    assert len(names) == 15
    assert all(re.fullmatch(r"b1fa-[0-9a-f]{10}\.mp3", n) for n in names)
    for row in BUILD:
        if row["id"].startswith("b1fa-"):
            assert row["file"] == f"audio/{row['id']}.mp3"
