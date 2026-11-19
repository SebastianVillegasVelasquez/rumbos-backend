"""Makes the physical audio files of a content pack, once, on a developer machine.

    uv run --group tts python -m app.scripts.content tts --pack en/A2 \\
        [--only <audio id | exam item id | activity id>] [--dry-run | --check]

Nothing here runs in the service: the files are generated, listened to by a
person, committed with the pack and served as static files. The engine is a
dev-time dependency (the `tts` dependency group), imported only when a clip has
to be made, so a dry run and the tests need none of it.

What is read: `tts.json` of the pack (engine, model files and their hashes,
voices, pace, pauses, loudness, format), `audio_manifest.json` (what each clip
says, per voice) and the pack's content (to say which exam item or activity a
clip belongs to). What is written: the `.mp3` files, `duration_s` in
`audio_manifest.json`, and `audio_build.json`, the machine-readable inventory
(id, content reference, spoken text, voices, engine, format, duration, loudness
and `sha256` of each file, plus the `input_hash` that decides regeneration).

A clip is regenerated only when its file is missing or its `input_hash` changed
(its spoken text or voices, or anything in `tts.json` that changes the sound:
engine and model version, voice, pace, pause, loudness, bitrate). The spoken
text is never derived here: it is `text` / `voices[].text` of the manifest.
"""

import hashlib
import importlib
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.trainer.assessment_form import ChoiceAssessmentItem, MatchAudioItem
from app.trainer.pack import AudioEntry, Pack

Out = Callable[[str], None]
BUILD_FILE = "audio_build.json"
CONFIG_FILE = "tts.json"
MODEL_DIR_ENV = "TRAINER_TTS_MODEL_DIR"


class TtsError(Exception):
    """Something the operator has to fix (missing model, wrong hash, no engine)."""


@dataclass
class Plan:
    make: list[tuple[AudioEntry, str]] = field(default_factory=list)  # entry, why
    keep: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def load_config(root: Path) -> dict[str, Any]:
    path = root / CONFIG_FILE
    if not path.is_file():
        raise TtsError(f"{path} does not exist")
    config: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return config


def input_hash(entry: AudioEntry, config: dict[str, Any]) -> str:
    """What decides whether a clip sounds the same: what it says, in which
    voices, and everything in the config that shapes the sound."""
    turns = [(v.voice, v.text) for v in entry.voices] or [("host", entry.text)]
    payload = {"turns": turns, "tts": config}
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(blob).hexdigest()


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def content_refs(pack: Pack) -> dict[str, str]:
    """audio id -> where the clip is used, e.g. `exam a2-form-a a2-fa-l-01` or
    `activity en-a2-1.1-listening en-a2-1.1.l1`."""
    refs: dict[str, str] = {}
    for form in pack.forms.values():
        for section in form.sections:
            for item in section.items():
                if isinstance(item, ChoiceAssessmentItem) and item.audio:
                    refs[item.audio.audio] = f"exam {form.form_id} {item.item_id}"
                elif isinstance(item, MatchAudioItem):
                    for index, clip in enumerate(item.clips, start=1):
                        refs[clip.audio] = (
                            f"exam {form.form_id} {item.item_id} clip {index}"
                        )
    for activity in pack.activities.values():
        for graded in activity.items:
            if graded.audio:
                refs[graded.audio] = f"activity {activity.id} {graded.id}"
        if activity.dialogue:
            for node in activity.dialogue.nodes:
                if node.audio:
                    refs[node.audio] = f"activity {activity.id} {node.id}"
    return refs


def _selected(entry: AudioEntry, refs: dict[str, str], only: list[str]) -> bool:
    if not only:
        return True
    words = refs.get(entry.id, "").split()
    return any(o == entry.id or o in words for o in only)


def make_plan(
    pack: Pack, config: dict[str, Any], only: list[str], force: bool = False
) -> Plan:
    plan = Plan()
    refs = content_refs(pack)
    build_path = pack.root / BUILD_FILE
    built: dict[str, Any] = {}
    if build_path.is_file():
        built = {
            e["id"]: e
            for e in json.loads(build_path.read_text(encoding="utf-8"))["clips"]
        }
    roles = set(config["voices"])
    for entry in sorted(pack.audio.values(), key=lambda e: e.id):
        if not _selected(entry, refs, only):
            continue
        if entry.id not in refs:
            plan.problems.append(f"{entry.id}: no exam item or activity uses it")
        unknown = {v.voice for v in entry.voices} - roles
        if unknown:
            plan.problems.append(f"{entry.id}: voice {sorted(unknown)} not in tts.json")
        path = pack.root / entry.file
        old = built.get(entry.id)
        wanted = input_hash(entry, config)
        if force:
            plan.make.append((entry, "forced"))
        elif not path.is_file():
            plan.make.append((entry, "file missing"))
        elif old is None:
            plan.make.append((entry, "not in audio_build.json"))
        elif old["input_hash"] != wanted:
            plan.make.append((entry, "text, voice or engine settings changed"))
        else:
            plan.keep.append(entry.id)
    if only and not plan.make and not plan.keep:
        plan.problems.append(f"nothing matches --only {' '.join(only)}")
    return plan


# --- the engine ----------------------------------------------------------------


def model_files(config: dict[str, Any]) -> dict[str, Path]:
    base = Path(
        os.environ.get(MODEL_DIR_ENV, str(Path.home() / ".cache" / "rumbos-tts"))
    )
    found: dict[str, Path] = {}
    for name, spec in config["engine"]["files"].items():
        path = base / spec["name"]
        if not path.is_file():
            raise TtsError(
                f"model file {path} is missing; download {spec['url']} "
                f"(or set {MODEL_DIR_ENV})"
            )
        if sha256_of(path) != spec["sha256"]:
            raise TtsError(f"{path} does not match the pinned sha256 of tts.json")
        found[name] = path
    return found


def _ffmpeg() -> str:
    explicit = os.environ.get("TRAINER_TTS_FFMPEG")
    if explicit:
        return explicit
    try:
        exe: str = importlib.import_module("imageio_ffmpeg").get_ffmpeg_exe()
        return exe
    except ImportError:
        return "ffmpeg"


class Synthesizer:
    """Kokoro through ONNX Runtime, then ffmpeg for loudness and MP3."""

    def __init__(self, config: dict[str, Any]) -> None:
        try:
            self._np = importlib.import_module("numpy")
            self._sf = importlib.import_module("soundfile")
            kokoro = importlib.import_module("kokoro_onnx")
        except ImportError as exc:
            raise TtsError(
                "the TTS tools are not installed; run with `uv run --group tts` "
                f"({exc})"
            ) from exc
        files = model_files(config)
        self._engine = kokoro.Kokoro(str(files["model"]), str(files["voices"]))
        self._config = config
        self._ffmpeg = _ffmpeg()

    def _turn(self, text: str, role: str) -> tuple[Any, int]:
        cfg = self._config
        samples, rate = self._engine.create(
            text,
            voice=cfg["voices"][role],
            speed=cfg["speed"],
            lang=cfg["engine"]["lang"],
        )
        return samples, int(rate)

    def render(self, entry: AudioEntry, target: Path) -> dict[str, Any]:
        np, cfg = self._np, self._config
        turns = [(v.voice, v.text) for v in entry.voices] or [("host", entry.text)]
        pieces: list[Any] = []
        rate = 0
        for index, (role, text) in enumerate(turns):
            samples, rate = self._turn(text, role)
            if index:
                pieces.append(np.zeros(int(rate * cfg["turn_gap_ms"] / 1000), "f4"))
            pieces.append(samples.astype("f4"))

        def pad(ms: int) -> Any:
            return np.zeros(int(rate * ms / 1000), "f4")

        audio = np.concatenate([pad(cfg["lead_ms"]), *pieces, pad(cfg["tail_ms"])])
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "in.wav"
            self._sf.write(str(wav), audio, rate, subtype="FLOAT")
            measured = self._measure(wav)
            self._encode(wav, target, measured)
        return {"lufs": self._integrated(target)}

    def _run(self, args: list[str]) -> str:
        done = subprocess.run(
            [self._ffmpeg, "-hide_banner", "-nostats", *args],
            capture_output=True,
            text=True,
            check=False,
        )
        if done.returncode != 0:
            raise TtsError(f"ffmpeg failed: {done.stderr.strip()[-300:]}")
        return done.stderr

    def _loudnorm(self) -> str:
        target = self._config["loudness"]
        return f"loudnorm=I={target['lufs']}:TP={target['true_peak_db']}:LRA=11"

    def _measure(self, wav: Path) -> dict[str, str]:
        log = self._run(
            [
                "-i",
                str(wav),
                "-af",
                self._loudnorm() + ":print_format=json",
                "-f",
                "null",
                "-",
            ]
        )
        match = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", log, re.DOTALL)
        if not match:
            raise TtsError("could not measure loudness")
        data: dict[str, str] = json.loads(match.group(0))
        return data

    def _encode(self, wav: Path, target: Path, m: dict[str, str]) -> None:
        # Second pass in linear mode: one fixed gain, so the result is the same
        # every time and speech is not "compressed" clip by clip.
        flt = (
            f"{self._loudnorm()}:measured_I={m['input_i']}:measured_TP={m['input_tp']}"
            f":measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}"
            f":offset={m['target_offset']}:linear=true"
        )
        fmt = self._config["format"]
        self._run(
            [
                "-y",
                "-i",
                str(wav),
                "-af",
                flt,
                "-ac",
                "1",
                "-ar",
                str(fmt["sample_rate"]),
                "-c:a",
                "libmp3lame",
                "-b:a",
                f"{fmt['bitrate_kbps']}k",
                "-map_metadata",
                "-1",
                "-fflags",
                "+bitexact",
                "-flags:a",
                "+bitexact",
                str(target),
            ]
        )

    def _integrated(self, mp3: Path) -> float:
        log = self._run(["-i", str(mp3), "-af", "ebur128", "-f", "null", "-"])
        found = re.findall(r"I:\s+(-?\d+(?:\.\d+)?) LUFS", log)
        return round(float(found[-1]), 1) if found else 0.0


def duration_of(path: Path) -> float:
    media = importlib.import_module("mutagen").File(path)
    if media is None or media.info is None:
        raise TtsError(f"cannot read the duration of {path}")
    return round(float(media.info.length), 2)


# --- running -------------------------------------------------------------------


def _write_json(path: Path, data: Any) -> None:
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def update_files(
    pack: Pack, config: dict[str, Any], made: dict[str, dict[str, Any]]
) -> None:
    """Writes `audio_build.json` (every clip of the pack that has a file) and
    the durations of `audio_manifest.json`."""
    refs = content_refs(pack)
    build_path = pack.root / BUILD_FILE
    clips: dict[str, Any] = {}
    if build_path.is_file():
        clips = {
            e["id"]: e
            for e in json.loads(build_path.read_text(encoding="utf-8"))["clips"]
        }
    for entry in pack.audio.values():
        if entry.id in made:
            path = pack.root / entry.file
            clips[entry.id] = {
                "id": entry.id,
                "content_ref": refs.get(entry.id, ""),
                "spoken_text": entry.text,
                "turns": [{"voice": v.voice, "text": v.text} for v in entry.voices],
                "engine_voices": {
                    v: config["voices"][v] for v in {t.voice for t in entry.voices}
                },
                "language_variant": config["engine"]["variant"],
                "format": config["format"]["container"],
                "file": entry.file,
                "duration_s": made[entry.id]["duration_s"],
                "lufs": made[entry.id]["lufs"],
                "bytes": path.stat().st_size,
                "sha256": sha256_of(path),
                "input_hash": input_hash(entry, config),
            }
    for stale in [i for i in clips if i not in pack.audio]:
        del clips[stale]
    _write_json(
        build_path,
        {
            "engine": config["engine"]["name"],
            "engine_version": config["engine"]["version"],
            "model": config["engine"]["model"],
            "language_variant": config["engine"]["variant"],
            "clips": sorted(clips.values(), key=lambda e: e["id"]),
        },
    )
    manifest_path = pack.root / "audio_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for item in manifest["entries"]:
        if item["id"] in made:
            item["duration_s"] = made[item["id"]]["duration_s"]
    _write_json(manifest_path, manifest)


def run_tts(
    pack: Pack,
    *,
    only: list[str],
    dry_run: bool,
    check: bool,
    force: bool,
    out: Out,
    synthesizer: Callable[[dict[str, Any]], Synthesizer] = Synthesizer,
) -> int:
    try:
        config = load_config(pack.root)
    except TtsError as exc:
        out(str(exc))
        return 1
    plan = make_plan(pack, config, only, force)
    if plan.problems:
        out("Nothing was generated:")
        for problem in plan.problems:
            out(f"  {problem}")
        return 1
    out(f"{len(plan.make)} clip(s) to make, {len(plan.keep)} up to date")
    for entry, why in plan.make:
        out(f"  make {entry.id} ({why})")
    if dry_run or check:
        out("Dry run: nothing was written.")
        return 1 if (check and plan.make) else 0
    if not plan.make:
        return 0
    try:
        engine = synthesizer(config)
        made: dict[str, dict[str, Any]] = {}
        for entry, _ in plan.make:
            target = pack.root / entry.file
            info = engine.render(entry, target)
            made[entry.id] = {"duration_s": duration_of(target), **info}
            out(f"  made {entry.id} {made[entry.id]['duration_s']}s")
    except TtsError as exc:
        out(str(exc))
        return 1
    update_files(pack, config, made)
    out(f"Done. Listen to the clips before publishing; see {BUILD_FILE}.")
    return 0
