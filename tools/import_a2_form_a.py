"""Builds the A2 check-in exam (Forma A) from the client's extracted source.

    uv run python tools/import_a2_form_a.py [tools/sources/a2-form-a.source.json]

The source is `tools/sources/a2-form-a.source.json`: the section "Evaluación
diagnóstica de entrada (Forma A)" of the A2 curriculum document, copied
verbatim. This script writes `content/trainer/en/A2/assessments/a2-form-a.json`
and the exam entries of `content/trainer/en/A2/audio_manifest.json` (entries that
do not belong to the exam are left alone).

Nothing in the source is corrected. Item ids are ours (`a2-fa-l-01`) and do not
follow the source numbering, which is kept as `source_number`. What the source
does not say (instructions of each part, titles of the parts) is written here,
in `AUTHORED`, and listed in `REVIEW-A2-checkin.md` for the reviewer.

The spoken text of every clip is spelled out in `SPOKEN`, not derived: digits,
prices, times and the Wi-Fi password are decided here so the voice engine never
has to guess. The displayed transcript stays exactly as the source has it.

Standard library plus the app's own form model.
"""

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.trainer.assessment_form import AssessmentForm

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = ROOT / "tools/sources/a2-form-a.source.json"
PACK = ROOT / "content/trainer/en/A2"
FORM_ID = "a2-form-a"

_ROMAN = {"I": "M1", "II": "M2", "III": "M3", "IV": "M4"}
_SKILL_TAILS = ("listening", "reading", "uso")

# What the source does not give: shown to the student, needs review.
AUTHORED: dict[str, Any] = {
    "parts": {
        ("listening", "a"): (
            "Part A — Short messages",
            "Listen and choose the correct answer.",
        ),
        ("listening", "b"): (
            "Part B — Short conversations",
            "Listen to each conversation and choose the correct answer.",
        ),
        ("listening", "c"): (
            "Part C — Match the audio with the scene",
            "Listen to the five audios and match each one with the correct scene.",
        ),
        ("reading", "a"): (
            "Part A — Signs",
            "Read each sign and choose the sentence that says the same.",
        ),
        ("reading", "b"): (
            "Part B — A short text",
            "Read the text and answer the questions.",
        ),
        ("reading", "c"): (
            "Part C — A conversation with gaps",
            "Read the conversation and choose the right word for each gap.",
        ),
        ("use_of_english", "tasks"): (
            "Use of English",
            "Choose the correct option.",
        ),
        ("writing", "tasks"): ("Writing", "Write your answers in English."),
        ("speaking", "tasks"): (
            "Speaking (written answers)",
            "Write your answers in English, as if you were speaking.",
        ),
    },
    "writing_1_question": "Write your email here.",
}

SECTION_KEYS = {
    "Listening": "listening",
    "Reading": "reading",
    "Use of English": "use_of_english",
}
SECTION_LETTER = {"listening": "l", "reading": "r", "use_of_english": "u"}

# Voices: `host` and `guest` are roles (Mateo and Liam); the single-sentence
# clips all use `host` so that the voice never hints at the answer.
SPOKEN: dict[int, list[tuple[str, str]]] = {
    1: [("host", "Tomorrow we are going to hike at eight fifteen.")],
    2: [("host", "Your room number is one, one, two.")],
    3: [("host", "A bag of coffee is twenty thousand pesos.")],
    4: [("host", "The bus to the town leaves at nine, from platform two.")],
    5: [
        (
            "host",
            (
                "Breakfast is at seven, and the Wi-Fi password is "
                "M, A, C, I, Z, O, two, zero, two, six."
            ),
        )
    ],
    6: [
        ("guest", "The water is cold!"),
        ("host", "I'm so sorry. The technician is fixing the heater right now."),
    ],
    7: [
        ("host", "Where is the injury?"),
        ("guest", "My ankle hurts. I can't move my foot."),
    ],
    8: [
        ("guest", "I have a sore throat."),
        ("host", "I'm sorry to hear that. You should drink something warm."),
    ],
    9: [
        ("guest", "Can someone guide a night walk?"),
        ("host", "Yes, Mateo can guide it tonight."),
    ],
    10: [
        ("host", "Front desk, how can I help you?"),
        ("guest", "Your booking is wrong!"),
        ("host", "I understand, I'm so sorry."),
    ],
}
SPOKEN_MATCH = [
    "We are going to hike the trail in the morning.",
    "I'd like a double room for two nights.",
    "The technician is fixing the heater.",
    "Four tickets to the town, please.",
    "Please turn off the lights when you leave.",
]


def audio_id(item_id: str, clip: str = "") -> str:
    """Opaque and stable: says nothing about the item, its position or its key."""
    digest = hashlib.sha256(f"{FORM_ID}|{item_id}|{clip}".encode()).hexdigest()
    return f"a2fa-{digest[:10]}"


def competence(raw: str) -> dict[str, Any]:
    parts = [p.strip() for p in raw.split("·")]
    head = parts[0]
    modules = (
        list(_ROMAN.values())
        if " a " in head
        else [_ROMAN[head.removeprefix("M-").strip()]]
    )
    rest = parts[1:]
    if rest and rest[-1].split(" (")[0].lower() in _SKILL_TAILS:
        rest = rest[:-1]
    lesson = next((p for p in rest if re.fullmatch(r"\d+(\.\d+)*", p)), None)
    topic = " · ".join(p for p in rest if p != lesson) or None
    return {"raw": raw, "modules": modules, "lesson": lesson, "topic": topic}


def _ascii(text: str) -> str:
    return text.replace("’", "'")


def _dialogue(transcript: str) -> bool:
    return transcript.lstrip().startswith("—")


def build(source: dict[str, Any]) -> tuple[AssessmentForm, list[dict[str, Any]]]:
    notes: list[str] = []
    entries: list[dict[str, Any]] = []
    sections: dict[str, dict[str, Any]] = {}
    counters = {"l": 0, "r": 0, "u": 0}

    def section(key: str, title: str, weight: int) -> dict[str, Any]:
        return sections.setdefault(
            key,
            {"key": key, "title": title, "instructions": None, "weight": weight},
        )

    def part_of(sec: dict[str, Any], key: str) -> dict[str, Any]:
        parts: list[dict[str, Any]] = sec.setdefault("parts", [])
        for part in parts:
            if part["part_key"] == key:
                return part
        title, instructions = AUTHORED["parts"][(sec["key"], key)]
        part = {
            "part_key": key,
            "title": title,
            "instructions": instructions,
            "shared": None,
            "items": [],
        }
        parts.append(part)
        return part

    def entry(
        clip_id: str, turns: list[tuple[str, str]], transcript: str
    ) -> dict[str, Any]:
        spoken = " ".join(t for _, t in turns)
        found = {
            "id": clip_id,
            "text": spoken,
            "voices": [{"voice": v, "text": t} for v, t in turns],
            "file": f"audio/{clip_id}.mp3",
            "duration_s": None,
            "transcript": transcript,
        }
        entries.append(found)
        return found

    passages = source["passages"]
    for raw in source["items"]:
        number = raw["source_number"]
        sec_key = SECTION_KEYS[raw["section"]]
        sec = section(sec_key, raw["section"], raw["section_weight"])
        part_key = (raw["part"] or "tasks").lower()
        part = part_of(sec, part_key)
        letter = SECTION_LETTER[sec_key]
        counters[letter] += 1
        item_id = f"a2-fa-{letter}-{counters[letter]:02d}"
        comp = competence(raw["competency"])
        if raw.get("type") == "matching":
            match = source["matching_item_11"]
            places = [
                {"id": re.sub(r"\W+", "_", s.lower()), "text": s}
                for s in match["scenes"]
            ]
            clips = []
            for index, clip in enumerate(match["audios"]):
                clip_id = audio_id(item_id, str(index + 1))
                transcript = clip["text"]
                entry(clip_id, [("host", _ascii(SPOKEN_MATCH[index]))], transcript)
                clips.append({"audio": clip_id, "transcript": transcript})
            scene_ids = {p["text"]: p["id"] for p in places}
            built: dict[str, Any] = {
                "item_id": item_id,
                "type": "match_audio",
                "prompt": raw["stem"],
                "clips": clips,
                "places": places,
                "key": [(k["audio"] - 1, scene_ids[k["scene"]]) for k in match["key"]],
                "competence": comp,
                "source_number": number,
            }
            notes.append(
                f"{item_id}: source item {number} is the matching task; it counts as "
                f"{match['counts_as']} items and is left out of the per-module map"
            )
        else:
            options = [
                {"id": o["label"].lower(), "text": o["text"]} for o in raw["options"]
            ]
            kind = "mcq"
            if "audio_text" in raw:
                kind = "listen_choose"
            elif sec_key == "reading" and part_key == "c":
                kind = "gap_choice"
            built = {
                "item_id": item_id,
                "type": kind,
                "prompt": raw["stem"],
                "options": options,
                "key": raw["key"].lower(),
                "competence": comp,
                "source_number": number,
            }
            if kind == "listen_choose":
                clip_id = audio_id(item_id)
                transcript = raw["audio_text"]
                turns = SPOKEN[number]
                assert _dialogue(transcript) == (len(turns) > 1), number
                entry(clip_id, [(v, _ascii(t)) for v, t in turns], transcript)
                built["audio"] = {"audio": clip_id, "transcript": transcript}
            if kind == "gap_choice":
                label = re.search(r"\((\w)\)", raw["stem"])
                assert label
                built["gap_label"] = f"({label.group(1)})"
        if sec_key == "reading" and part_key in ("b", "c") and part["shared"] is None:
            part["shared"] = {
                "kind": "passage",
                "title": None,
                "text": passages[f"reading_{part_key.upper()}"],
                "image_id": None,
                "alt_text": None,
                "design_note": None,
                "image_text": None,
            }
        part["items"].append(built)

    writing = source["writing"]
    wsec = section("writing", "Writing", source["weights"]["writing"])
    wpart = part_of(wsec, "tasks")
    wnum = AUTHORED["writing_1_question"]
    wpart["items"].append(
        {
            "item_id": "a2-fa-w-01",
            "type": "written_answers",
            "prompt": f"{writing[0]['title']}. {writing[0]['prompt']}",
            "rubric": "writing.confirmation_email",
            "questions": [{"id": "q1", "text": wnum}],
            "source_rubric": writing[0]["rubric"],
        }
    )
    wpart["items"].append(
        {
            "item_id": "a2-fa-w-02",
            "type": "template_text",
            "prompt": writing[1]["title"],
            "rubric": "writing.past_story",
            "template": writing[1]["prompt"],
            "min_words": 25,
            "max_words": 40,
            "source_rubric": writing[1]["rubric"],
        }
    )
    notes.append(
        "a2-fa-w-01: the source says 'write a short email' with no template or "
        "fields; it is a free text of up to 300 characters"
    )
    speaking = source["speaking"]
    ssec = section("speaking", "Speaking", source["weights"]["speaking"])
    ssec["delivery"] = "written_equivalent"
    spart = part_of(ssec, "tasks")
    task2 = speaking[1]
    questions = [q.strip() + "?" for q in task2["prompt"].split("?") if q.strip()]
    spart["items"].append(
        {
            "item_id": "a2-fa-s-02",
            "type": "written_answers",
            "prompt": task2["title"],
            "rubric": "speaking.tomorrow_now_written",
            "questions": [
                {"id": f"q{i + 1}", "text": q} for i, q in enumerate(questions)
            ],
            "source_rubric": task2["rubric"],
            "written_equivalent_of": f"Speaking Task 2 · {task2['title']}",
        }
    )
    notes.append(
        "Speaking Task 1 (a2-fa-s-01, read the Writing story aloud) is NOT in this "
        "form: it has no written equivalent yet and needs a pedagogical decision. "
        "The Speaking section keeps its 15% weight and holds Task 2 only (0 to 5)"
    )
    notes.append(
        "Speaking is delivered as typed answers (delivery: written_equivalent); it "
        "does not measure pronunciation"
    )
    notes.append(
        "The source gives no figure briefs or alt text and no instructions per "
        "part; the part titles and instructions are ours (see REVIEW-A2-checkin.md)"
    )
    ordered = [sections[k] for k in sections]
    form = AssessmentForm.model_validate(
        {
            "form_id": FORM_ID,
            "title": "Inglés A2 · Evaluación diagnóstica · Forma A · Diagnóstico de entrada",
            "language": "en",
            "level": "A2",
            "purpose": "check_in",
            "status": "draft",
            "sections": ordered,
            "source_notes": notes,
        }
    )
    return form, entries


def write_manifest(entries: list[dict[str, Any]], path: Path) -> None:
    manifest: dict[str, Any] = (
        json.loads(path.read_text(encoding="utf-8"))
        if path.exists()
        else {"entries": []}
    )
    by_id = {e["id"]: e for e in manifest["entries"]}
    for found in entries:
        old = by_id.get(found["id"])
        if old:
            found["file"] = old["file"]
            found["duration_s"] = old.get("duration_s")
        by_id[found["id"]] = found
    manifest["entries"] = sorted(by_id.values(), key=lambda e: e["id"])
    path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def main() -> int:
    source_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SOURCE
    source = json.loads(source_path.read_text(encoding="utf-8"))
    form, entries = build(source)
    target = PACK / "assessments" / f"{FORM_ID}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(form.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    write_manifest(entries, PACK / "audio_manifest.json")
    print(f"Wrote {target} and {len(entries)} exam audio entries")
    return 0


if __name__ == "__main__":
    sys.exit(main())
