"""The `content tts` command: what it regenerates and what it never touches.
The engine is replaced by a fake, so no model, ffmpeg or GPU is needed."""

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from app.scripts import tts
from app.scripts.content import main as content_main
from app.trainer.pack import AudioEntry, Pack, load_pack

ROOT = Path(__file__).resolve().parent.parent
EXAM_ITEM_AUDIO = "a2fa-"


class FakeEngine:
    def __init__(self, _config: dict[str, Any]) -> None:
        self.made: list[str] = []

    def render(self, entry: AudioEntry, target: Path) -> dict[str, Any]:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f"audio:{entry.text}".encode())
        self.made.append(entry.id)
        return {"lufs": -19.0}


@pytest.fixture
def pack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Pack:
    root = tmp_path / "en" / "A2"
    shutil.copytree(ROOT / "content/trainer/en/A2", root)
    for stale in (root / "audio").glob("*"):
        stale.unlink()
    (root / tts.BUILD_FILE).unlink(missing_ok=True)
    monkeypatch.setattr(tts, "duration_of", lambda _path: 1.5)
    return load_pack(root)


def run(
    pack: Pack, engine: FakeEngine, *only: str, dry: bool = False, check: bool = False
) -> tuple[int, list[str]]:
    lines: list[str] = []
    code = tts.run_tts(
        pack,
        only=list(only),
        dry_run=dry,
        check=check,
        force=False,
        out=lines.append,
        synthesizer=lambda _c: engine,  # type: ignore[arg-type,return-value]
    )
    return code, lines


def test_a_dry_run_lists_every_missing_clip_and_writes_nothing(pack: Pack) -> None:
    engine = FakeEngine({})
    code, lines = run(pack, engine, dry=True)
    assert code == 0 and not engine.made
    assert lines[0] == "132 clip(s) to make, 0 up to date"
    assert not (pack.root / tts.BUILD_FILE).exists()
    assert not list((pack.root / "audio").glob("*"))


def test_check_fails_while_something_is_stale(pack: Pack) -> None:
    assert run(pack, FakeEngine({}), check=True)[0] == 1


def test_only_makes_the_named_clip_and_records_it(pack: Pack) -> None:
    engine = FakeEngine({})
    exam_audio = next(i for i in pack.audio if i.startswith(EXAM_ITEM_AUDIO))
    code, _ = run(pack, engine, exam_audio)
    assert code == 0 and engine.made == [exam_audio]
    build = json.loads((pack.root / tts.BUILD_FILE).read_text(encoding="utf-8"))
    (clip,) = build["clips"]
    file = pack.root / clip["file"]
    assert (
        clip["sha256"] == tts.sha256_of(file) and clip["bytes"] == file.stat().st_size
    )
    assert clip["content_ref"].startswith("exam a2-form-a a2-fa-")
    assert clip["language_variant"] == "en-US" and clip["format"] == "mp3"
    manifest = json.loads((pack.root / "audio_manifest.json").read_text("utf-8"))
    durations = {e["id"]: e["duration_s"] for e in manifest["entries"]}
    assert durations[exam_audio] == 1.5
    assert sum(d is not None for d in durations.values()) == 1


def test_only_accepts_an_exam_item_or_an_activity_id(pack: Pack) -> None:
    code, lines = run(pack, FakeEngine({}), "a2-fa-l-11", dry=True)
    assert code == 0 and lines[0].startswith("5 clip(s) to make")
    _, lines = run(pack, FakeEngine({}), "en-a2-1.1-listening", dry=True)
    assert lines[0].startswith("3 clip(s) to make")


def test_nothing_is_remade_until_text_voice_or_settings_change(pack: Pack) -> None:
    run(pack, FakeEngine({}), "a2-fa-l-01")
    clip_id = next(
        i for i, r in tts.content_refs(pack).items() if r.endswith("a2-fa-l-01")
    )
    again = FakeEngine({})
    _, lines = run(pack, again, "a2-fa-l-01")
    assert again.made == [] and lines[0] == "0 clip(s) to make, 1 up to date"

    # Its spoken text changes: only that clip is remade.
    manifest_path = pack.root / "audio_manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    for entry in manifest["entries"]:
        if entry["id"] == clip_id:
            entry["text"] = "Tomorrow we are going to hike at eight."
            entry["voices"][0]["text"] = entry["text"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    changed = load_pack(pack.root)
    remade = FakeEngine({})
    _, lines = run(changed, remade, "a2-fa-l-01")
    assert remade.made == [clip_id]
    assert "text, voice or engine settings changed" in "\n".join(lines)

    # A setting that shapes the sound (the pace) makes the clip stale again.
    config_path = pack.root / tts.CONFIG_FILE
    config = json.loads(config_path.read_text("utf-8"))
    config["speed"] = 0.8
    config_path.write_text(json.dumps(config), encoding="utf-8")
    assert run(load_pack(pack.root), FakeEngine({}), "a2-fa-l-01", check=True)[0] == 1


def test_an_unknown_target_is_refused(pack: Pack) -> None:
    code, lines = run(pack, FakeEngine({}), "nope")
    assert code == 1 and "nothing matches" in lines[-1]


def test_a_voice_the_config_does_not_know_is_refused(pack: Pack) -> None:
    config_path = pack.root / tts.CONFIG_FILE
    config = json.loads(config_path.read_text("utf-8"))
    del config["voices"]["guest"]
    config_path.write_text(json.dumps(config), encoding="utf-8")
    code, lines = run(load_pack(pack.root), FakeEngine({}), dry=True)
    assert code == 1 and any("not in tts.json" in line for line in lines)


def test_every_clip_of_the_real_pack_is_used_and_voiced() -> None:
    real = load_pack(ROOT / "content/trainer/en/A2")
    config = tts.load_config(real.root)
    plan = tts.make_plan(real, config, [])
    assert not plan.problems
    assert len(plan.make) + len(plan.keep) == 132
    assert {v.voice for e in real.audio.values() for v in e.voices} == {"host", "guest"}


def test_the_missing_model_is_reported_not_guessed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(tts.MODEL_DIR_ENV, str(tmp_path))
    real = load_pack(ROOT / "content/trainer/en/A2")
    with pytest.raises(tts.TtsError, match="model file .* is missing"):
        tts.model_files(tts.load_config(real.root))


def test_the_content_cli_has_the_tts_command() -> None:
    lines: list[str] = []
    code = content_main(
        ["tts", "--pack", "en/A2", "--dry-run", "--only", "a2-fa-l-01"],
        out=lines.append,
    )
    assert code == 0 and "clip(s)" in lines[0]
    assert content_main(["tts", "--pack", "bad"], out=lines.append) == 2
