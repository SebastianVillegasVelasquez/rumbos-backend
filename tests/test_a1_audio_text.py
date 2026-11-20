"""What the A1 voices say: opaque exam ids, the two voices, plain spoken words."""

import re
from pathlib import Path

from app.scripts import tts
from app.trainer.assessment_form import ChoiceAssessmentItem, MatchAudioItem
from app.trainer.pack import load_pack

ROOT = Path(__file__).resolve().parent.parent / "content/trainer/en/A1"
PACK = load_pack(ROOT)
CONFIG = tts.load_config(ROOT)

# Clips whose spoken words differ from the displayed transcript on purpose.
SPOKEN_DIFFERENTLY = {
    "en-a1-1.1.l2": "Mister Brown (Mr.)",
    "en-a1-1.4.l3": "T, O, R, R, E, S (T-O-R-R-E-S)",
    "en-a1-3.4.d1": "okay (OK)",
}


def _exam_ids() -> set[str]:
    ids: set[str] = set()
    for form in PACK.forms.values():
        for section in form.sections:
            for item in section.items():
                if isinstance(item, ChoiceAssessmentItem) and item.audio:
                    ids.add(item.audio.audio)
                elif isinstance(item, MatchAudioItem):
                    ids.update(c.audio for c in item.clips)
    return ids


def test_exam_audio_ids_and_files_reveal_nothing() -> None:
    exam = _exam_ids()
    assert len(exam) == 15
    for audio_id in exam:
        assert re.fullmatch(r"a1fa-[0-9a-f]{10}", audio_id)
        assert PACK.audio[audio_id].file == f"audio/{audio_id}.mp3"


def test_every_clip_uses_known_voices_and_exam_singles_use_the_host() -> None:
    for entry in PACK.audio.values():
        assert entry.voices, entry.id
        assert {v.voice for v in entry.voices} <= set(CONFIG["voices"]), entry.id
    assert CONFIG["voices"] == {"host": "am_michael", "guest": "af_heart"}
    for entry in PACK.audio.values():
        if entry.id.startswith("en-a1-") and ".d" in entry.id:
            assert [v.voice for v in entry.voices] == ["guest"], entry.id
        elif len(entry.voices) == 1:
            assert entry.voices[0].voice == "host", entry.id
        else:
            assert [v.voice for v in entry.voices] == ["guest", "host"], entry.id


def test_a1_is_paced_no_faster_than_a2() -> None:
    a2 = tts.load_config(ROOT.parent / "A2")
    assert CONFIG["speed"] < a2["speed"]
    assert CONFIG["turn_gap_ms"] >= a2["turn_gap_ms"]
    assert CONFIG["voices"] == a2["voices"] and CONFIG["engine"] == a2["engine"]


def test_spoken_text_is_plain_words_and_every_difference_is_logged() -> None:
    changed = set()
    for entry in PACK.audio.values():
        for text in [entry.text, *(v.text for v in entry.voices)]:
            assert not re.search(r"\d|%|:|\b[A-Z]{2,}\b|\bMr\.|’", text), (
                entry.id,
                text,
            )
        shown = re.sub(r"^— ", "", entry.transcript).replace(" — ", " ")
        if shown.replace("’", "'") != entry.text:
            changed.add(entry.id)
    exam_changed = {i for i in changed if i.startswith("a1fa-")}
    assert changed - exam_changed == set(SPOKEN_DIFFERENTLY)
    assert len(exam_changed) == 2  # "three, oh, four" and "seven o'clock ..."
