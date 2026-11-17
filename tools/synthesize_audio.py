"""Produces the audio files of a pack from its manifest, with a command YOU
configure. Nothing here is part of the running service: the files are made
once, reviewed by a person and committed (or deployed) with the pack.

    TRAINER_TTS_COMMAND='piper --model en_US-lessac-medium --output_file {out}' \\
        uv run python tools/synthesize_audio.py content/trainer/en/A1

The command is run (without a shell) once per missing file, with the text on
standard input. Placeholders: `{out}` the output path, `{voice}` the voice id
of the turn (`speaker_a` / `speaker_b`, or `default`), `{text}` the text.
For a two-speaker entry each turn is made in its own voice and the pieces are
joined (needs `ffmpeg` on the PATH). The duration is then read from the file
(needs the dev dependency `mutagen`) and written back to the manifest.

Offline options (check each licence for your use before shipping):
  * Piper (MIT): fast neural voices; individual voice models carry their own
    licences, many are CC0 or CC-BY.
  * Coqui TTS (MPL-2.0 code; model licences vary, XTTS is non-commercial).
  * eSpeak NG (GPL-3.0): robotic, but a safe last resort; the output audio is
    not covered by the GPL.
"""

import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


def run_command(template: str, text: str, voice: str, out: Path) -> None:
    args = [
        part.replace("{out}", str(out))
        .replace("{voice}", voice)
        .replace("{text}", text)
        for part in shlex.split(template)
    ]
    subprocess.run(args, input=text.encode(), check=True)


def make(entry: dict[str, Any], pack: Path, template: str) -> None:
    out = pack / entry["file"]
    out.parent.mkdir(parents=True, exist_ok=True)
    if not entry["voices"]:
        run_command(template, entry["text"], "default", out)
        return
    with tempfile.TemporaryDirectory() as tmp:
        pieces: list[Path] = []
        for i, turn in enumerate(entry["voices"]):
            piece = Path(tmp) / f"{i}{out.suffix}"
            run_command(template, turn["text"], turn["voice"], piece)
            pieces.append(piece)
        listing = Path(tmp) / "list.txt"
        listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in pieces))
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(listing),
                "-c",
                "copy",
                str(out),
            ],
            check=True,
        )


def duration_of(path: Path) -> float:
    from mutagen import File  # dev dependency

    media = File(path)
    if media is None or media.info is None:
        raise RuntimeError(f"cannot read the duration of {path}")
    return round(float(media.info.length), 2)


def main() -> int:
    template = os.environ.get("TRAINER_TTS_COMMAND")
    if not template:
        print("Set TRAINER_TTS_COMMAND (see this file's docstring).", file=sys.stderr)
        return 2
    pack = Path(sys.argv[1])
    manifest_path = pack / "audio_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    made = 0
    for entry in manifest["entries"]:
        path = pack / entry["file"]
        if not path.exists():
            make(entry, pack, template)
            made += 1
        if path.exists() and entry["duration_s"] is None:
            entry["duration_s"] = duration_of(path)
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"{made} file(s) made; manifest updated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
