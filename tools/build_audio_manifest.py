"""Adds the exam's audio to `audio_manifest.json` of a pack.

    uv run python tools/build_audio_manifest.py content/trainer/en/A1

Every listening item of every form gets an entry (id, text, voices for
two-speaker dialogues, expected file). Entries already in the manifest keep
their `file` and `duration_s`; entries not derived from a form (the practice
pack's) are left untouched. The files themselves come from
`tools/synthesize_audio.py`.
"""

import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.trainer.assessment_form import (
    AssessmentForm,
    ChoiceAssessmentItem,
    MatchAudioItem,
)

# The two voices of a dialogue, in the order the speakers talk.
VOICES = ("speaker_a", "speaker_b")


def voices_of(transcript: str) -> list[dict[str, str]]:
    """A transcript like `- Good afternoon. - Thank you.` is two turns."""
    turns = [t.strip() for t in re.split(r"\s*—\s*", transcript) if t.strip()]
    if len(turns) < 2 or not transcript.lstrip().startswith("—"):
        return []
    return [{"voice": VOICES[i % 2], "text": turn} for i, turn in enumerate(turns)]


def form_entries(form: AssessmentForm) -> dict[str, dict[str, Any]]:
    wanted: dict[str, str] = {}
    for section in form.sections:
        for item in section.items():
            if isinstance(item, ChoiceAssessmentItem) and item.audio:
                wanted[item.audio.audio] = item.audio.transcript
            elif isinstance(item, MatchAudioItem):
                for clip in item.clips:
                    wanted[clip.audio] = clip.transcript
    return {
        audio_id: {
            "id": audio_id,
            "text": text,
            "voices": voices_of(text),
            "file": f"audio/{audio_id}.mp3",
            "duration_s": None,
            "transcript": text,
        }
        for audio_id, text in wanted.items()
    }


def main() -> int:
    pack = Path(sys.argv[1])
    manifest_path = pack / "audio_manifest.json"
    manifest: dict[str, Any] = (
        json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest_path.exists()
        else {"entries": []}
    )
    by_id = {e["id"]: e for e in manifest["entries"]}
    for form_path in sorted((pack / "assessments").glob("*.json")):
        form = AssessmentForm.model_validate_json(form_path.read_bytes())
        for audio_id, entry in form_entries(form).items():
            old = by_id.get(audio_id)
            if old:
                entry["file"] = old["file"]
                entry["duration_s"] = old.get("duration_s")
            by_id[audio_id] = entry
    manifest["entries"] = sorted(by_id.values(), key=lambda e: e["id"])
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"{manifest_path}: {len(by_id)} entries")
    return 0


if __name__ == "__main__":
    sys.exit(main())
