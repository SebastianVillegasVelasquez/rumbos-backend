# A2 pack: what a human must decide before anything is published

Pack `en/A2`, exam `a2-form-a`. Everything is `draft`; `reviewed_by` and
`reviewed_at` are empty. This file sits next to the pack (not inside it) so
editing it does not change the pack's `content_version`.

## 1. Content concerns in the check-in exam (flagged, not fixed)

| # | Item (source no.) | Concern |
| --- | --- | --- |
| a | `a2-fa-u-15` (41) "The colour of the recyclable bin: blue" | Locale-dependent. Colombia's national code (Resolución 2184 de 2019, Ministerio de Ambiente) is **white** for recyclables, black for non-recyclables, green for organic waste; blue is not in it. As keyed, a Colombian student who knows the national code is marked wrong. Needs a decision: change the key/options, or make the item explicit about the code. Source: minambiente.gov.co, "Gobierno unifica el código de colores". |
| b | Listening Part C (11) | You mentioned the instruction says "according to the image". The source JSON has no part instructions, so I could not check. The stem is Spanish and the key uses the text labels (trail plan, hotel counter...). The instruction shown to students is ours: "match each one with the correct scene". |
| c | `a2-fa-l-05` (5) | A Wi-Fi password is read aloud. Decision (logged in `docs/DECISIONS-trainer.md`): spoken letter by letter, then digit by digit ("M, A, C, I, Z, O, two, zero, two, six"). The displayed transcript keeps `MACIZO2026`. The same password is printed in Reading item `a2-fa-r-04` (15). |
| d | Distractors that could be argued | **27**: "Tomorrow we go to visit a coffee farm" is grammatical English (option "go to"). **24** (gap c): "Your room key is 202" is plausible, since keys often carry the room number. **Matching, audio 5** ("turn off the lights when you leave" = green farm): weak cue, could also be read as a hotel counter or cabin. |
| d2 | Same facts in several items | The technician sentence appears in Listening 6, the matching clip 3, Use of English 29 and Speaking Task 2 ("What is the technician doing now?"). The exam itself supplies the Speaking answer. Breakfast 7:00 and the Wi-Fi password repeat in items 5, 15, 21. |
| e | Speaking weight | Speaking counts 15% (confirmed by the product owner), while the source also calls it "formative, does not block progress" and assumes a voice recording. Our delivery is typed answers (`delivery: written_equivalent`). **The client must confirm in writing that this is acceptable for the Speaking requirement.** |
| f | Speaking Task 1 ("read your Writing story aloud") | No natural written equivalent. Not invented; see section 2. |
| g | Writing 2 length | 25 to 40 words counted over the whole text; the template alone has 14 words, so the blanks hold about 4 to 5 words each. Same tightness as A1 (D9). |
| h | Spelling | The exam mixes British ("colour") and American ("apologizing") spelling. Decide the variant (the audio voice accent follows it). |

## 2. Speaking Task 1: needs a pedagogical decision

The slot is **not in the form** (an unanswerable scored task would punish
students). It is listed in the form's `source_notes` as `a2-fa-s-01`. What the
score is:

- **Shipped (Task 2 only):** Speaking = 5 points, 15% of the total (3 points of
  total per Speaking point). A perfect student can reach 100%.
- **If Task 1 existed as a scored task nobody could answer (0 of 5):** Speaking
  is 10 points, 1.5% each; the ceiling is **92.5%**, and a student needs about
  half of everything else to clear 80% strictly.
- **If a written Task 1 is defined later:** it adds a rubric and an item
  `a2-fa-s-01`; weights do not change.

## 3. What the source did not give (so we wrote it, or left it out)

- Part titles and instructions for every part, and the Writing 1 box label
  "Write your email here." (`AUTHORED` in `tools/import_a2_form_a.py`). **Needs
  review.**
- Figures 1 to 3: the source JSON contains no figure briefs and no alt text, so
  there is nothing to keep as metadata and no image item. `image_pending` is
  therefore not set anywhere. Send the briefs and the images can be added.
- Use of English has no parts in the source: one part, `tasks`.
- Reading passages are shown without a title.

## 4. Proxy rubrics (all need pedagogical sign-off)

`writing.confirmation_email`, `writing.past_story`,
`speaking.tomorrow_now_written` in `rubrics.json`. They check presence of key
words, the template, past-tense forms (`-ed` or a listed irregular), and
*be* + *-ing*; they cannot judge meaning (see the note in `rubrics.json`).
Speaking never evaluates pronunciation.

---

## Practice content (39 activities, 104 words): needs a full review

Everything in `activities/` and `vocabulary.json` for A2 was **written by us**
(there was no client source for practice), in the A1 structure: per lesson a
6-turn dialogue, 3 listening items, 6 grammar items, 8 words. All of it is
`draft`, with no reviewer. A reviewer should read every string, not sample.
Known weak points:

- Lessons 2.1 and 2.2 each have four topics in the curriculum and only six
  exercises, so some topics have a single item.
- Typed dialogue replies and short answers use accepted-word lists (`all_of`);
  a correct reply worded differently will be marked wrong. Read the `accept`
  lists.
- The curriculum excludes free narration, *will* vs *going to* nuance and
  spontaneous planning. None is evaluated.
- Practice never reuses exam text (`tests/test_a2_practice.py` checks stems,
  options, templates, dialogue lines and audio text against the exam).

## Audio QA (a person must listen; nobody has yet)

No audio has been heard by a person. The generated files are checked only by
speech-recognition round trips on samples. Before publishing, listen to:

1. **Every exam clip (15).** Each must say exactly the `text` in
   `audio_manifest.json` and nothing that gives the answer away.
2. **Digits and times:** "one, one, two" (a2-fa-l-11 matching), "eight fifteen",
   prices in pesos ("pesos" must sound natural, not as an English word).
3. **The Wi-Fi password** (a2-fa-l-05): "M, A, C, I, Z, O, two, zero, two, six",
   one letter at a time, no merged letters ("A, C" must not sound like "ache").
4. **Speaker turns:** in dialogues the host and the guest voices alternate in
   the manifest order; no turn is cut or spoken by the wrong voice.
5. **Pace and pauses:** 0.85 speed, 450 ms between turns; confirm a learner at
   A2 can follow, and that nothing is clipped at the start or end.
6. **Practice clips (117):** same checks; spot-check at least every lesson's
   dialogue and one listening clip, and every clip with numbers or spelling.
7. **Loudness:** clips should sit at the same level (target -19 LUFS).

If a clip is wrong, fix its `text` or `voices[].text` in `audio_manifest.json`
(or the voice in `tts.json`) and run `python -m app.scripts.content tts --pack
en/A2`; only the clips whose input changed are remade.
