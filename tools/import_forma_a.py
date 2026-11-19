"""Imports the A1 check-in / final exam (Forma A) from the client's `.docx`.

    uv run python tools/import_forma_a.py <path-to.docx> [-o <output.json>]

Only the section "Evaluacion diagnostica", Forma A, is read: the script finds
the heading "Forma A" that follows the heading "Evaluacion diagnostica" and
stops at the next heading of level 3 or higher. Nothing else in the file is
parsed or printed. The text is copied verbatim: nothing is corrected or
improved, and what looks wrong is listed in `source_notes` of the output.

The output is a draft (`status: "draft"`). It has to be checked against the
document, key by key, before anyone sets it to `published`.

Standard library only (a `.docx` is a zip of XML).
"""

import argparse
import json
import re
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.trainer.assessment_form import AssessmentForm

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
DEFAULT_OUTPUT = "content/trainer/en/A1/assessments/a1-form-a.json"

_ROMAN = {"I": "M1", "II": "M2", "III": "M3", "IV": "M4"}
_SKILLS = {"listening", "reading", "reading/uso"}
_SECTION_KEYS = {
    "listening": "listening",
    "reading": "reading",
    "use of english": "use_of_english",
    "writing": "writing",
    "speaking": "speaking",
}
_QUOTES = '“”"'


@dataclass
class Block:
    style: str
    text: str
    table: list[list[str]] | None = None


def _text(paragraph: ET.Element) -> str:
    out: list[str] = []
    for el in paragraph.iter():
        if el.tag == W + "t":
            out.append(el.text or "")
        elif el.tag == W + "tab":
            out.append("\t")
    return "".join(out)


def _style(paragraph: ET.Element) -> str:
    st = paragraph.find(f"{W}pPr/{W}pStyle")
    return st.get(W + "val", "") if st is not None else ""


def _level(style: str) -> int | None:
    match = re.fullmatch(r"T\S{0,2}tulo(\d)", style)
    return int(match.group(1)) if match else None


def _bare(text: str) -> str:
    """Letters only, lowercase, accents dropped: for recognising headings."""
    import unicodedata

    folded = unicodedata.normalize("NFKD", text)
    return "".join(c for c in folded if c.isalpha()).lower()


def read_section(docx: Path) -> list[Block]:
    """The blocks between the heading "Forma A" (under "Evaluacion diagnostica")
    and the next heading of level 3 or higher."""
    with zipfile.ZipFile(docx) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
    body = root.find(W + "body")
    assert body is not None
    blocks: list[Block] = []
    state = "seeking"  # seeking -> armed (saw the section heading) -> inside
    for child in body:
        if child.tag == W + "p":
            style, text = _style(child), _text(child).strip()
            level = _level(style)
            if state == "seeking":
                if level is not None and _bare(text) == "evaluaciondiagnostica":
                    state = "armed"
                continue
            if state == "armed":
                if level is not None and _bare(text).startswith("formaa"):
                    state = "inside"
                continue
            if level is not None and level <= 3:
                break
            blocks.append(Block(style, text))
        elif child.tag == W + "tbl" and state == "inside":
            rows = [
                [
                    " ".join(_text(p).strip() for p in cell.iter(W + "p"))
                    for cell in row.findall(W + "tc")
                ]
                for row in child.iter(W + "tr")
            ]
            blocks.append(Block("table", "", rows))
    if state != "inside":
        raise SystemExit("Could not find 'Forma A' under 'Evaluacion diagnostica'")
    return blocks


# --- parsing -----------------------------------------------------------------


def _unquote(text: str) -> str:
    return text.strip().strip(_QUOTES).strip()


def _competence(line: str) -> dict[str, Any]:
    raw = line.split(":", 1)[1].strip()
    parts = [p.strip() for p in raw.split("·")]
    head = parts[0]
    modules = (
        list(_ROMAN.values())
        if " a " in head
        else [_ROMAN[head.removeprefix("M-").strip()]]
    )
    rest = parts[1:]
    if rest and rest[-1].lower() in _SKILLS:
        rest = rest[:-1]
    lesson = next((p for p in rest if re.fullmatch(r"\d+(\.\d+)*", p)), None)
    topic = " · ".join(p for p in rest if p != lesson) or None
    return {"raw": raw, "modules": modules, "lesson": lesson, "topic": topic}


def _option(line: str) -> tuple[str, str, bool]:
    match = re.fullmatch(r"([A-E])\)\s*(.*?)\s*(✓)?", line)
    if not match:
        raise ValueError(f"not an option: {line!r}")
    return match.group(1).lower(), match.group(2).strip(), bool(match.group(3))


def _section_key(title: str) -> str:
    name = title.split("·")[1].split("(")[0].strip().lower()
    return next(key for label, key in _SECTION_KEYS.items() if name.startswith(label))


def parse(blocks: list[Block]) -> AssessmentForm:
    sections: list[dict[str, Any]] = []
    notes: list[str] = []
    section: dict[str, Any] = {}
    part: dict[str, Any] = {}
    item: dict[str, Any] | None = None
    audio_lines: list[str] = []
    waiting_passage: dict[str, Any] | None = None
    image_target: dict[str, Any] | None = None
    task_no = 0

    def finish_item() -> None:
        nonlocal item
        if item is None:
            return
        part["items"].append(item)
        item = None

    for block in blocks:
        style, text = block.style, block.text
        if block.table is not None:
            if image_target is not None:
                rows = {row[0]: row[1] for row in block.table if len(row) == 2}
                for label, field in (
                    ("Qué debe verse", "design_note"),
                    ("Texto dentro de la imagen", "image_text"),
                    ("Texto alternativo", "alt_text"),
                ):
                    key = next((k for k in rows if k.startswith(label)), None)
                    if key:
                        image_target[field] = rows[key]
                image_target = None
            continue
        if not text or re.fullmatch(r"Tabla \d+\.", text):
            continue  # an empty line, or the caption of an image sheet's table
        level = _level(style)
        if not section and level != 4:
            # The two lines before Section 1 (a note to the student and one on
            # how the validation tick is used) are not part of the exam.
            continue
        if level == 4:
            finish_item()
            weight = int(re.search(r"peso\s+(\d+)\s*%", text).group(1))  # type: ignore[union-attr]
            section = {
                "key": _section_key(text),
                "title": text.split("·")[1].split("(")[0].strip(),
                "instructions": None,
                "weight": weight,
                "parts": [],
            }
            sections.append(section)
            part = {}
            task_no = 0
        elif level == 5:
            finish_item()
            m = re.match(r"Part ([A-C]) — (.*)", text)
            if m:
                key, title = m.group(1).lower(), text
            else:
                key = _bare(text.split("(")[0].split("—")[0])
                key = {"grammar": "grammar", "vocabulary": "vocabulary"}.get(
                    key, "tasks"
                )
                title = text
            part = {
                "part_key": key,
                "title": title,
                "instructions": "",
                "shared": None,
                "items": [],
            }
            section["parts"].append(part)
        elif level == 6:
            finish_item()
            if section["key"] in ("writing", "speaking"):
                task_no += 1
                item = {"_task": text, "prompt": text, "task_no": task_no}
            else:
                item = {
                    "prompt": text,
                    "options": [],
                    "key": None,
                    "audio": None,
                    "competence": None,
                }
                if audio_lines and len(audio_lines) == 1:
                    item["audio"] = audio_lines[0]
                    audio_lines = []
                elif audio_lines:
                    item["_clips"] = audio_lines
                    audio_lines = []
        elif text.startswith("Instrucción:"):
            part["instructions"] = text.split(":", 1)[1].strip()
        elif text.startswith("Cómo se aplica:"):
            section["instructions"] = text.split(":", 1)[1].strip()
        elif text.startswith("\U0001f50a"):  # speaker: audio transcript
            body = text.split(":", 1)[1].strip()
            m = re.match(r"Audio \d+:\s*(.*)", body)
            audio_lines.append(_unquote(m.group(1) if m else body))
        elif text.startswith("\U0001f5bc"):  # picture: image sheet
            sheet = re.search(r"Ficha de imagen (\w+)", text).group(1)  # type: ignore[union-attr]
            shared = {
                "kind": "image",
                "title": None,
                "text": None,
                "image_id": sheet,
                "alt_text": None,
                "design_note": None,
                "image_text": None,
            }
            part["shared"] = shared
            image_target = shared
        elif text.startswith("\U0001f4c4"):  # page: a passage
            shared = {
                "kind": "passage",
                "title": text.lstrip("\U0001f4c4").strip(),
                "text": None,
                "image_id": None,
                "alt_text": None,
                "design_note": None,
                "image_text": None,
            }
            part["shared"] = shared
            waiting_passage = shared
        elif waiting_passage is not None and style == "":
            waiting_passage["text"] = text
            waiting_passage = None
        elif text.startswith("Competencia:") and item is not None:
            item["competence"] = _competence(text)
            finish_item()
        elif item is not None and "_task" in item:
            _task_line(item, text, section["key"], part)
            if text.startswith("Se evalúa"):
                finish_item()
        elif item is not None and style == "Prrafodelista":
            if "→)" in text:  # a matching pair: "Audio 1 ->) dock"
                item.setdefault("_pairs", []).append(text)
            else:
                letter, option, ok = _option(text)
                item["options"].append({"id": letter, "text": option})
                if ok:
                    item["key"] = letter
        else:
            notes.append(f"Unrecognised line left out: {text[:60]!r}")
    finish_item()
    return _build(sections, notes)


def _task_line(
    item: dict[str, Any], text: str, section: str, part: dict[str, Any]
) -> None:
    if text.startswith("Consigna:"):
        item["consigna"] = text.split(":", 1)[1].strip()
    elif text.startswith("Se evalúa"):
        item["source_rubric"] = text


def _build(sections: list[dict[str, Any]], notes: list[str]) -> AssessmentForm:
    out_sections: list[dict[str, Any]] = []
    for sec in sections:
        for part in sec["parts"]:
            items: list[dict[str, Any]] = []
            for n, raw in enumerate(part["items"], start=1):
                item_id = f"{sec['key']}.{part['part_key']}.{n:02d}"
                items.append(_build_item(item_id, sec["key"], part, raw, notes))
            part["items"] = items
        out_sections.append(sec)
    return AssessmentForm.model_validate(
        {
            "form_id": "a1-form-a",
            "title": "Inglés A1 · Evaluación diagnóstica · Forma A · Diagnóstico de entrada",
            "language": "en",
            "level": "A1",
            "purpose": "check_in",
            "status": "draft",
            "sections": out_sections,
            "source_notes": notes,
        }
    )


def _build_item(
    item_id: str,
    section: str,
    part: dict[str, Any],
    raw: dict[str, Any],
    notes: list[str],
) -> dict[str, Any]:
    if "_task" in raw:
        notes.append(f"{item_id}: the source gives this task no competence code")
        built = _productive(item_id, section, raw)
        if section == "speaking":
            notes.append(
                f"{item_id}: the written equivalent is ours, not the source's; "
                "it needs pedagogical sign-off"
            )
        return built
    competence = raw["competence"]
    if "_pairs" in raw:
        return _match(item_id, part, raw, notes)
    if competence and competence["lesson"] is None:
        notes.append(
            f"{item_id}: the source gives no lesson number ({competence['raw']!r})"
        )
    kind = "mcq"
    if raw["audio"]:
        kind = "listen_choose"
    elif part["part_key"] == "c" and section == "reading":
        kind = "gap_choice"
    out: dict[str, Any] = {
        "item_id": item_id,
        "type": kind,
        "prompt": raw["prompt"],
        "options": raw["options"],
        "key": raw["key"],
        "competence": competence,
    }
    if raw["audio"]:
        out["audio"] = {"audio": f"{item_id}", "transcript": raw["audio"]}
    if kind == "gap_choice":
        label = re.search(r"\((\w)\)", raw["prompt"])
        out["gap_label"] = f"({label.group(1)})" if label else None
    return out


def _match(
    item_id: str, part: dict[str, Any], raw: dict[str, Any], notes: list[str]
) -> dict[str, Any]:
    pairs: list[tuple[int, str]] = []
    places: list[dict[str, str]] = []
    for line in raw["_pairs"]:
        m = re.fullmatch(r"Audio (\d+)\s*→\)\s*(.*?)\s*✓?", line)
        assert m, line
        place = m.group(2).strip()
        place_id = re.sub(r"\W+", "_", place.lower())
        places.append({"id": place_id, "text": place})
        pairs.append((int(m.group(1)) - 1, place_id))
    notes.append(
        f"{item_id}: the matching task's code spans modules "
        f"({raw['competence']['raw']!r}); it counts as 5 items and is left out of "
        "the per-module map"
    )
    return {
        "item_id": item_id,
        "type": "match_audio",
        "prompt": raw["prompt"],
        "clips": [
            {"audio": f"{item_id}.{i + 1}", "transcript": t}
            for i, t in enumerate(raw["_clips"])
        ],
        "places": places,
        "key": pairs,
        "competence": raw["competence"],
    }


def _productive(item_id: str, section: str, raw: dict[str, Any]) -> dict[str, Any]:
    consigna: str = raw.get("consigna", "")
    base = {
        "item_id": item_id,
        "prompt": raw["prompt"],
        "competence": None,
        "source_rubric": raw["source_rubric"],
    }
    n = raw["task_no"]
    if section == "writing" and n == 1:
        names = consigna.split(":", 1)[1].rstrip(".")
        fields = [f.strip() for f in names.split(",")]
        return {
            **base,
            "type": "form_fields",
            "rubric": "writing.host_card",
            "fields": [
                {"id": re.sub(r"\W+", "_", f.lower()), "text": f} for f in fields
            ],
        }
    if section == "writing":
        template = _unquote(consigna.split(":", 1)[1])
        words = re.search(r"\((\d+)–(\d+) words\)", raw["prompt"])
        assert words
        return {
            **base,
            "type": "template_text",
            "rubric": "writing.welcome_note",
            "template": template,
            "min_words": int(words.group(1)),
            "max_words": int(words.group(2)),
        }
    if n == 1:
        return {
            **base,
            "type": "written_answers",
            "rubric": "speaking.read_aloud_written",
            "questions": [
                {
                    "id": "q1",
                    "text": "Write the welcome note you wrote in Writing Task 2, "
                    "reading it carefully.",
                }
            ],
            "written_equivalent_of": raw["prompt"],
        }
    quoted = re.search(r"“(.*?)”", consigna)
    assert quoted
    questions = [q.strip() + "?" for q in quoted.group(1).split("?") if q.strip()]
    return {
        **base,
        "type": "written_answers",
        "rubric": "speaking.three_questions_written",
        "questions": [{"id": f"q{i + 1}", "text": q} for i, q in enumerate(questions)],
        "written_equivalent_of": raw["prompt"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("docx", type=Path)
    parser.add_argument("-o", "--output", type=Path, default=Path(DEFAULT_OUTPUT))
    args = parser.parse_args()
    form = parse(read_section(args.docx))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(form.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"Wrote {args.output} ({len(form.source_notes)} note(s) in source_notes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
