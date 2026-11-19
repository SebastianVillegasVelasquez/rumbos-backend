# B2 pack: what a human must decide before anything is published

Pack `en/B2`, exam `b2-form-a`. Everything is `draft`; `reviewed_by` and
`reviewed_at` are empty. This file sits next to the pack (not inside it) so
editing it does not change the pack's `content_version`. Nothing here was
fixed: each item is flagged for a person to decide.

## 1. The exam may not discriminate at B2, and the pass rule makes that matter

The pass rule is the total **strictly above 80%**. Receptive items (Listening,
Reading, Use of English) are 70 of the 100 points; production is 30 (Writing 15
in two tasks, Speaking 15 in one task: Task 1 has no written form, see section
3). Numbers below are exact for this form.

**If every receptive item is right (70), the student needs more than 10 of the
30 production points, a third of the production weight.** Alone, that is more
than 6.67 of 10 in Writing, or more than 3.33 of 5 in Speaking (one Speaking
point is worth two Writing points). 5 of 10 in Writing and 1 of 5 in Speaking
already pass.

**If production is perfect (30), the student needs more than 50 of the 70
receptive points: at least 31 of the 45 items.** Listening and Reading items
are worth 5/3 of a point and Use of English items 4/3, so 30 Listening and
Reading items are exactly 50, which does not pass, and one more item does.

| production earned (of 30) | receptive needed (of 70) | items right (best case) |
| --- | --- | --- |
| 10 (a third) | more than 70: impossible | none |
| 15 (half) | more than 65 | 42 of 45 |
| 20 (two thirds) | more than 60 | 38 of 45 |
| 25.5 (85%) | more than 54.5 | 34 of 45 |
| 30 (all) | more than 50 | 31 of 45 |

What follows from them:

- **The production proxy decides who passes, and it is weak** (section 4). A
  text that lists the expected phrases scores 5 of 5 in Writing Task 1 and 5 of
  5 in Speaking; the same list scores 2 of 5 in Task 2. That is about 25.5 of 30
  production points from a student who writes nothing real, after which 34 of 45
  receptive items (76%) pass.
- **The receptive part is probably easy.** 12 of the 45 receptive points come
  from items whose wrong options are not plausible at B2 (Reading Part A and
  Use of English 35 to 41, see 2a). They are worth 17.7 of the 100 points. A B1
  student who gets 90% of the receptive part (63) only needs 17 of the 30
  production points (57%).
- **Random guessing cannot pass** (about 23 receptive points, production would
  need to be worth more than 56), but a modest student with a gaming strategy
  can. The pass rate on this form, if it is used as is, will be high and
  unearned.
- **An honest B2 student can fail on the receptive part alone**: perfect
  production with 30 of 45 receptive items right is 80 exactly or less, and
  does not pass.

Decision needed: whether the pass rule should depend on this form at all until
production is scored by a person, and whether the weights of the section that
cannot be checked (Speaking 15%, Writing 15%) should stay as they are.

## 2. Content concerns in the check-in exam (flagged, not fixed)

| # | Item (source no.) | Concern |
| --- | --- | --- |
| a | Many distractors are not plausible at B2 | Use of English **35** ("it is a terrace", "a cancellation fee"), **36** ("doubled", "paid twice"), **37** ("You are all useless.", "Whatever, forget it."), **38** ("courtyard", "briefcase"), **39** ("Go away, please.", "I have no idea."), **40** ("Give me a boat ticket.", "What is your favourite colour?"), **41** ("a type of backpack", "a boat engine"); and Reading Part A **12 to 16** ("No account can ever be rebooked", "The site is for sale", "Nobody may cancel", "The meeting never ends", "Only guests may close the session"). The test is an A1/A2 vocabulary check in places. A B1 student, or a guesser who reads the options, can answer them without B2 grammar. The exam may not discriminate, and a high pass rate would be unearned (section 1). |
| b | Length of the audios | The brief says the source claims 25 to 45 seconds per audio; the extract contains no such claim, so I could not check where it comes from. The actual transcripts are one or two sentences and the generated clips last seconds, not 25 to 45 (real durations are in section 6). The generated audio was not padded. A timing claim in the client's document needs correcting, or longer audio texts. |
| c | Matching instruction (11) | The brief says the instruction reads "according to the image". The extract's stem is in Spanish ("Empareja cada audio (1 a 5) con la escena correcta: ...") and does not mention an image; the key uses text labels. I did not re-read the original document. The instruction students see is ours: "Listen to the five audios and match each one with the correct scene." |
| d | Speaking weight | Speaking counts 15% (the source's `weights`), and the source also lists Speaking tasks in the production points. The brief says the source calls it "formative"; that word is not in the extract. The delivery here is a typed answer (`delivery: written_equivalent`); **the client must confirm in writing that this is acceptable for the Speaking requirement.** |
| e | Writing Task 2: a choice of one of two prompts | The platform has no choice of task. It is one item that shows both prompts, with one box; the rubric credits the language of either one (section 4). The student is not asked which prompt they chose, so the score cannot check that the text answers the prompt chosen. Option (b), "Colombia, a Mediator's Perspective", assumes content (a climate defence) the exam does not provide. |
| f | Speaking Task 1 ("read your letter aloud") | No written equivalent exists. It is **not** in the form (an unanswerable scored task would cap the total at 92.5%). Listed in `source_notes` as `b2-fa-s-01`; see section 3. |
| g | Distractors that could be argued | **34**: "we would face a very different future" (option C) is correct as a mixed conditional (past condition, present result) and "a very different future" invites it; the key is "would have faced". **6**: the key "they cannot rebook until the account is cleared" is the client's own question ("Meaning that we cannot rebook until it is cleared?"); the agent never confirms it, so the key is an inference. **7**: the stem calls the speaker "the investor"; the audio never says so. **14**: option A says the fee "can be cancelled" for a fee that is "waived"; a student can read "cancellation fees ... cancelled" as circular. **3**: the audio makes two statements (keynote moved: highly probable; press room relocated: almost certain); only the first is asked. |
| h | Inconsistencies in the source | See the list below. |

Inconsistencies (h):

1. **Answers repeat inside the exam.** Matching clip 3 is the sentence of item 3's
   audio ("It is highly probable that the keynote will be moved to the main
   auditorium"); matching clip 2 is the sentence of Use of English 33; clip 5 is
   item 6's first sentence and Reading 12 and 14's content; the Reading B passage
   repeats items 1, 3, 4, 5 and 17 to 21 almost word for word ("approximately nine
   forty", "highly probable", "likely to arrive", "canceling our confirmed
   bookings"); Use of English 31 and gap (e) restate item 7's audio. A student who
   has seen or heard one has the answer to the next.
2. **Competence codes.** The exam tags lessons 1.5 and 1.6, and tags the theft
   report and the consular request (33, 40) as 2.4; the curriculum in the brief
   has 2.4 as "effects of personal problems on professional life" and the theft
   report in 2.5. The pack has six lessons in Module I so every code in the exam
   exists (see section 5).
3. **Spelling.** US ("canceling", "catalog") and UK ("cancelling", "cancelled",
   "favourite", "colour") forms are mixed, even across the two Writing prompts and
   items 5, 17 and 29. The generated voices are US English.
4. **Reading Part C** is a grammar gap-fill coded "uso" in the source, but it is
   counted in Reading (25%), not Use of English (20%).
5. **Production points.** The scoring note speaks of 20 production points (four
   tasks of 5); only 15 are scorable in this form because Speaking Task 1 is not
   shipped. The weights are as the source says, so the scale is not.
6. **Speaking Task 2** asks to "defend the value of the destination" and never
   says which one.
7. **Item 11's stem is in Spanish**; every other stem is in English.
8. **Images.** The brief mentions images and alt texts to keep. The extract has no
   figure briefs, no alt text and no item with an image, so there is nothing to
   mark `image_pending`.

## 3. Speaking Task 1: needs a pedagogical decision

Not in the form; listed in its `source_notes`. Shipped: Speaking = Task 2 only,
5 points, 15% of the total (3 total points per Speaking point). A perfect
student can reach 100%. If Task 1 existed as a scored task nobody could answer,
the ceiling would be **92.5%**. If a written Task 1 is defined later it adds a
rubric and an item `b2-fa-s-01`; weights do not change.

## 4. What the proxy rubrics can and cannot verify

`writing.complaint_letter`, `writing.one_of_two` and
`speaking.chair_negotiation_written` in `rubrics.json`. Each criterion scores
points x the mean of its checks, scaled by a word-count window.

What they **do** check: that expected phrases are present (formal opening and
closing, "I am writing to request", "we require that", polite requests,
"has been", "compliance", "cancellation fees", "waived", chairing formulas,
"we strongly disagree", "if we had", "would have"); that the text has at least
six sentence-ending marks and a linking word; and that its length is in a
window.

What they **cannot** check, and the tests pin it: register (a rude letter with
the right words scores the same), cohesion, whether the phrases are used
correctly rather than listed, spelling, accuracy of tenses, whether the text
answers the prompt, which option of Task 2 was chosen, or anything about
pronunciation. **A list of the expected phrases, repeated until it has 100
words, scores 5 of 5 in Writing Task 1** (`test_known_limit_a_list_of_the_expected_phrases...`).
The rubrics do **not** claim to measure register or cohesion.

Length: the source asks for 120 to 160 words. The form serves 120 and 160 as a
hint (`min_words`, `max_words`) and keeps 1500 characters (`max_chars`); the
shared cap of 300 characters would have cut a letter at about 45 words (it is
now per item and A1 and A2 are unaffected). The rubric counts words in a wider
window, **100 to 190**, applied to every criterion: outside it the score is 0
(all or nothing), inside it the length no longer matters. Tight on purpose so a
good letter of 118 words is not zero; loose enough to let padding through.
Speaking has no length in the source: 25 to 200 words. **Needs sign-off.**

Speaking Task 2 is one box (900 characters). The source's prompt is a single
paragraph, not a list of questions, so it is not split.

## 5. What the source did not give (so we wrote it, or left it out)

- Part titles and instructions, the writing box labels ("Write your letter
  here.", "Write your text here. Choose option (a) or option (b).", "Write what
  you would say here."): `AUTHORED` in `tools/import_b2_form_a.py`. **Needs
  review.**
- Use of English has no parts in the source: one part, `tasks`.
- The voices of the conversations (who speaks first) are ours; single-sentence
  clips all use the same voice so it never hints at the answer.
- Module I has six lessons (1.1 to 1.6), not the four topic groups the brief
  lists, because the exam codes six of them. The split is ours: 1.1 travel
  details, 1.2 degrees of certainty, 1.3 solving a service problem, 1.4
  describing a service or package (typical and specific features, passive and
  modals in the passive), 1.5 clarifying and confirming by paraphrase, 1.6
  correcting misunderstandings. Module II has the five lessons the brief
  numbers (2.1 to 2.5), III four, IV three: 18 lessons.
