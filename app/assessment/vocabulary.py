"""The course vocabulary the proxy rubric checks words against: every word in
the exam's own text (prompts, options, transcripts, passages) plus the curated
`course_words.json` of the pack."""

from collections.abc import Sequence

from app.assessment.rubric import vocabulary_from
from app.trainer.assessment_form import (
    AssessmentForm,
    ChoiceAssessmentItem,
    MatchAudioItem,
    ProductiveItem,
)
from app.trainer.language import LanguageRules


def form_texts(form: AssessmentForm) -> list[str]:
    """Every piece of English text a student sees in the form."""
    texts: list[str] = []
    for section in form.sections:
        for part in section.parts:
            if part.shared:
                texts += [t for t in (part.shared.title, part.shared.text) if t]
            for item in part.items:
                texts.append(item.prompt)
                if isinstance(item, ChoiceAssessmentItem):
                    texts += [o.text for o in item.options]
                    if item.audio:
                        texts.append(item.audio.transcript)
                elif isinstance(item, MatchAudioItem):
                    texts += [c.transcript for c in item.clips]
                    texts += [p.text for p in item.places]
                elif isinstance(item, ProductiveItem):
                    texts += [f.text for f in item.fields]
                    texts += [q.text for q in item.questions]
                    if item.template:
                        texts.append(item.template)
    return texts


def course_vocabulary(
    form: AssessmentForm, extra_words: Sequence[str], rules: LanguageRules
) -> frozenset[str]:
    return vocabulary_from(form_texts(form), extra_words, rules)
