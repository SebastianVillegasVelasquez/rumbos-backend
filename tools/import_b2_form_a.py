"""Builds the B2 check-in exam (Forma A) from the client's extracted source.

    uv run python tools/import_b2_form_a.py [tools/sources/b2-form-a.source.json]

The source is `tools/sources/b2-form-a.source.json`: the section "Evaluación
diagnóstica de entrada (Forma A)" of the B2 curriculum document, copied
verbatim. This script writes `content/trainer/en/B2/assessments/b2-form-a.json`
and the exam entries of `content/trainer/en/B2/audio_manifest.json` (entries that
do not belong to the exam are left alone).

Nothing in the source is corrected. Item ids are ours (`b2-fa-l-01`) and do not
follow the source numbering, which is kept as `source_number`. What the source
does not say (instructions of each part, titles of the parts, the label of each
writing box) is written here, in `AUTHORED`, and listed in `REVIEW-B2-checkin.md`.

The source's audio texts are already spelled out (`nine forty`, `eighty-five
thousand pounds`); the only thing a voice engine would get wrong is an acronym,
so `EXPANSIONS` spells those letter by letter and every expansion is written to
the form's `source_notes`. Anything else that is not plain words (a digit, a
currency sign, a percent sign, a capitalised acronym) stops the build instead of
being left to the engine. The displayed transcript stays exactly as the source has it.

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
DEFAULT_SOURCE = ROOT / "tools/sources/b2-form-a.source.json"
PACK = ROOT / "content/trainer/en/B2"
FORM_ID = "b2-form-a"

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
            "Part A — Notices",
            "Read each notice and choose the sentence that says the same.",
        ),
        ("reading", "b"): (
            "Part B — A briefing",
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
            "Speaking (written answer)",
            "Write your answer in English, as if you were speaking.",
        ),
    },
    "writing_1_question": "Write your letter here.",
    "writing_2_question": "Write your text here. Choose option (a) or option (b).",
    "speaking_2_question": "Write what you would say here.",
}

# Words of length the writing and speaking boxes accept. The source gives a word
# window for Writing (120-160) and none for Speaking. 1500 characters hold 160
# words of formal English (about 7.5 characters a word with the space) with room.
WRITING_MAX_CHARS = 1500
SPEAKING_MAX_CHARS = 900

SECTION_KEYS = {
    "Listening": "listening",
    "Reading": "reading",
    "Use of English": "use_of_english",
}
SECTION_LETTER = {"listening": "l", "reading": "r", "use_of_english": "u"}

# Who says each turn of a conversation, by the source number. `host` and `guest`
# are roles; every single-sentence clip uses `host` so the voice never hints at
# the answer. In a conversation the first voice is the one that speaks first.
VOICES: dict[int, tuple[str, ...]] = {
    1: ("guest", "host"),
    6: ("host", "guest"),
    7: ("guest", "host"),
    8: ("host",),
    9: ("guest", "host"),
    10: ("guest", "host"),
}

# The only words a voice would read wrongly: acronyms, letter by letter.
EXPANSIONS = {"GDS": "G D S", "OTA": "O T A"}


def audio_id(item_id: str, clip: str = "") -> str:
    """Opaque and stable: says nothing about the item, its position or its key."""
    digest = hashlib.sha256(f"{FORM_ID}|{item_id}|{clip}".encode()).hexdigest()
    return f"b2fa-{digest[:10]}"


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


def turns_of(transcript: str) -> list[str]:
    """The turns of a transcript: a dash starts a turn, a text without one is a
    single turn."""
    return [t.strip() for t in transcript.split("—") if t.strip()]


def spoken(text: str, expanded: set[str]) -> str:
    """What the voice reads: the displayed text with acronyms spelled out.
    Refuses anything else that an engine would have to guess."""
    out = text.replace("’", "'")
    for acronym, letters in EXPANSIONS.items():
        if re.search(rf"\b{acronym}\b", out):
            expanded.add(f"{acronym} -> {letters}")
            out = re.sub(rf"\b{acronym}\b", letters, out)
    left = re.search(r"\d|£|%|\b[A-Z]{2,}\b", out)
    if left:
        raise ValueError(f"not spoken as words: {left.group()!r} in {text!r}")
    return out


def build(source: dict[str, Any]) -> tuple[AssessmentForm, list[dict[str, Any]]]:
    notes: list[str] = []
    expanded: set[str] = set()
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
        text = " ".join(t for _, t in turns)
        found = {
            "id": clip_id,
            "text": text,
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
        item_id = f"b2-fa-{letter}-{counters[letter]:02d}"
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
                entry(clip_id, [("host", spoken(transcript, expanded))], transcript)
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
                texts = turns_of(transcript)
                voices = VOICES.get(number, ("host",))
                assert len(texts) == len(voices), (number, texts, voices)
                entry(
                    clip_id,
                    [
                        (v, spoken(t, expanded))
                        for v, t in zip(voices, texts, strict=True)
                    ],
                    transcript,
                )
                built["audio"] = {"audio": clip_id, "transcript": transcript}
            if kind == "gap_choice":
                label = re.search(r"\((\w)\)", raw["stem"])
                assert label
                built["gap_label"] = f"({label.group(1)})"
        if sec_key == "reading" and part_key in ("b", "c") and part["shared"] is None:
            part["shared"] = {
                "kind": "passage",
                "title": passages[f"reading_{part_key.upper()}_title"],
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
    for index, rubric, label in (
        (0, "writing.complaint_letter", AUTHORED["writing_1_question"]),
        (1, "writing.one_of_two", AUTHORED["writing_2_question"]),
    ):
        task = writing[index]
        wpart["items"].append(
            {
                "item_id": f"b2-fa-w-{index + 1:02d}",
                "type": "written_answers",
                "prompt": f"{task['title']}. {task['prompt']}",
                "rubric": rubric,
                "questions": [{"id": "q1", "text": label}],
                "min_words": 120,
                "max_words": 160,
                "max_chars": WRITING_MAX_CHARS,
                "source_rubric": task["rubric"],
            }
        )
    notes.append(
        "b2-fa-w-01 and b2-fa-w-02: the source asks for 120-160 words. The platform "
        f"keeps {WRITING_MAX_CHARS} characters of each answer (the shared cap of 300 "
        "would cut a letter at about 45 words) and serves 120 and 160 as a hint; "
        "the rubric counts words in a wider window"
    )
    notes.append(
        "b2-fa-w-02: the source makes Task 2 a choice of one of two prompts; the "
        "platform has no choice of task, so both prompts are shown in one item with "
        "one box, and the rubric credits the language of either one"
    )
    speaking = source["speaking"]
    ssec = section("speaking", "Speaking", source["weights"]["speaking"])
    ssec["delivery"] = "written_equivalent"
    spart = part_of(ssec, "tasks")
    task2 = speaking[1]
    spart["items"].append(
        {
            "item_id": "b2-fa-s-02",
            "type": "written_answers",
            "prompt": f"{task2['title']}. {task2['prompt']}",
            "rubric": "speaking.chair_negotiation_written",
            "questions": [{"id": "q1", "text": AUTHORED["speaking_2_question"]}],
            "max_chars": SPEAKING_MAX_CHARS,
            "source_rubric": task2["rubric"],
            "written_equivalent_of": f"Speaking Task 2 · {task2['title']}",
        }
    )
    notes.append(
        "Speaking Task 1 (b2-fa-s-01, read the Writing letter aloud) is NOT in this "
        "form: it has no written equivalent yet and needs a pedagogical decision. "
        "The Speaking section keeps its 15% weight and holds Task 2 only (0 to 5)"
    )
    notes.append(
        "Speaking is delivered as a typed answer (delivery: written_equivalent); it "
        "does not measure pronunciation"
    )
    notes.append(
        "The source has no figure briefs or alt text, so no item has an image and "
        "image_pending is not set anywhere; it also gives no instructions per part, "
        "so the part titles and instructions are ours (see REVIEW-B2-checkin.md)"
    )
    for text in sorted(expanded):
        notes.append(f"spoken text: acronym spelled out for the voice: {text}")
    ordered = [sections[k] for k in sections]
    form = AssessmentForm.model_validate(
        {
            "form_id": FORM_ID,
            "title": "Inglés B2 · Evaluación diagnóstica · Forma A · Diagnóstico de entrada",
            "language": "en",
            "level": "B2",
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
