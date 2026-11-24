# B1 pack: what a human must decide before anything is published

Pack `en/B1`, exam `b1-form-a`. Everything was built as `draft` with `reviewed_by` and
`reviewed_at` empty. **At the owner's explicit request it was then signed and published with the
reviewer "Sebastian Villegas Velasquez" (audit line in `content/trainer/publication-audit.jsonl`)
before any of the points below was decided and before anyone listened to the audio.** The points
below are therefore open on live content. This file sits next to the pack (not inside it) so
editing it does not change the pack's `content_version`. Nothing here was fixed: each item is
flagged for a person to decide.

## 1. The exam may not discriminate at B1, and the pass rule makes that matter

The pass rule is the total **strictly above 80%** (the configured threshold, unchanged).
Receptive items (Listening, Reading, Use of English) are 70 of the 100 points; production is 30
(Writing 15 in two tasks, Speaking 15 in one task: Task 1 has no written form, see section 3).
Weights and structure are the same as B2, so the arithmetic is the same. Numbers below are exact
for this form.

**If every receptive item is right (70), the student needs more than 10 of the 30 production
points, a third of the production weight.** Alone, that is more than 6.67 of 10 in Writing, or
more than 3.33 of 5 in Speaking (one Speaking point is worth two Writing points). 5 of 10 in
Writing and 1 of 5 in Speaking already pass.

**If production is perfect (30), the student needs more than 50 of the 70 receptive points: at
least 31 of the 45 items.** Listening and Reading items are worth 5/3 of a point and Use of
English items 4/3, so 30 Listening and Reading items are exactly 50, which does not pass, and
one more item does.

| production earned (of 30) | receptive needed (of 70) | items right (best case) |
| --- | --- | --- |
| 10 (a third) | more than 70: impossible | none |
| 15 (half) | more than 65 | 42 of 45 |
| 20 (two thirds) | more than 60 | 38 of 45 |
| 25.75 (what a phrase list earns, below) | more than 54.25 | 34 of 45 |
| 30 (all) | more than 50 | 31 of 45 |

What follows from them:

- **The production proxy decides who passes, and it is weak** (section 4). A text that is a list
  of the expected phrases scores 5 of 5 in Writing Task 1, about 2.2 of 5 in Task 2 and 5 of 5 in
  Speaking (`test_known_limit_...` pins it): about 25.75 of 30 production points from a student
  who writes nothing real, after which 34 of 45 receptive items (76%) pass.
- **The receptive part is probably easy** (item f of section 2). 12 of the 45 receptive items
  have wrong options that are not plausible at B1 (Use of English 35 to 41 and Reading Part A, 12 to 16,
  see 2f); they are worth 17.7 of the 100 points. A student who gets 90% of the receptive part (63) only needs 17 of
  the 30 production points (57%).
- **Random guessing cannot pass** (about a third of the receptive items, 23 points; production
  would need to be worth more than 57), but a modest student reading the options can. The pass
  rate on this form, if it is used as is, will be high and unearned.
- **An honest B1 student can fail on the receptive part alone**: perfect production with 30 of
  45 receptive items right is exactly 80 and does not pass.

Decision needed: whether the pass rule should depend on this form at all until production is
scored by a person, and whether the weights of the sections that cannot be checked (Speaking
15%, Writing 15%) should stay as they are.

## 2. Content concerns in the check-in exam (flagged, not fixed)

| # | Item (source no.) | Concern |
| --- | --- | --- |
| a | **29** ("The mountain farm is not ___ crowded ___ the luxury beaches, whereas ...") | Key `as / as`. Option C `more / than` ("not more crowded than") is also grammatical and near-synonymous, so two options are arguably correct. |
| b | **30** ("The auditors ___ tomorrow morning; the flights are already booked.") | Key `are arriving`. Option B `arrive` is acceptable for a fixed schedule (a flight timetable), so it may be arguably correct. |
| c | **26** gap (e) "You ___ arrive by eight" | Key `must`. `may` can read as acceptable ("you may arrive by eight" = you are allowed to). |
| d | **9** ("What does Elena recommend?") | The audio never names Elena; the two speakers are anonymous. The stem assumes a name the student cannot hear. |
| e | **11** matching | Audio 3 says "old customs **house**" while a scene is labelled "customs **post**" (audio 5). A confusable pair: check it is intended. Item 4 also uses "old customs house" as a key. |
| f | Many distractors are not plausible | Use of English **35** ("terrace", "receipt"), **36** ("balcony", "sunrise"), **37** ("You are completely wrong.", "That's a silly idea."), **38** ("heater", "platform"), **39** ("That's not my problem.", "Whatever you say."), **40** ("What is your favorite color?", "Please go away."), **41** ("a type of passport", "a boat ticket"); Reading Part A **12 to 16** ("There is no breakfast.", "The coast is for sale.", "Everyone can enter at any time."); Listening **3, 5, 8** ("only an apology", "a free tour"). The test is an A1/A2 vocabulary check in places, so it may not discriminate and a high pass rate is likely (section 1). |
| g | Speaking weight and delivery | Speaking counts 15% (the source's `weights`) although the source calls it formative in places; the source assumes a voice recording and the platform delivers a typed answer (`delivery: written_equivalent`); **the client must confirm in writing that this is acceptable.** Speaking Task 1 (read your email aloud) has no written equivalent (section 3). The matching instruction in the exam is in Spanish and mentions scenes, not "according to the image": the key uses text labels, and no image exists. |
| h | Answers repeat inside the exam | Reading B and the Listening texts overlap: Listening 3 and Use of English 31 are the same sentence ("Breakfast is served on the terrace ... rooms are cleaned"); Listening 5 and Use of English 32 / Reading 14 (life jacket, must); Listening 9 and Use of English 33 (night market, recommend); matching clip 3 is Listening 4's sentence; matching clip 4 combines Listening 3 and Reading 13; matching clip 5 gives the key of Use of English 40; Reading C gaps (b) and (e) and Listening 4 / 7 repeat each other's facts. A student who has heard or read one has the answer to the next. |
| i | Source details | Item 11's stem is in Spanish; every other stem is in English. The source spells in US English ("canceled", "favorite", "color") and the voices are US English, so the training pack uses US spelling too. Reading Part C is a grammar gap-fill coded "uso" but counted in Reading (25%), not Use of English (20%). The scoring note speaks of 20 production points (four tasks of 5); only 15 are scorable here because Speaking Task 1 is not shipped, so the scale is not the source's. Speaking Task 2 asks to "handle a complaint" without giving the complaint. |
| j | Images | The brief says to keep alt texts for images. The extract has no figure briefs, no alt text and no item with an image, so there is nothing to mark `image_pending`. |

## 3. Speaking Task 1: needs a pedagogical decision

Not in the form; listed in its `source_notes`. Shipped: Speaking = Task 2 only, 5 points, 15% of the total
(3 total points per Speaking point). A perfect student can reach 100%. If Task 1 existed as a scored task
nobody could answer, the ceiling would be **92.5%**. If a written Task 1 is defined later it adds a rubric
and an item `b1-fa-s-01`; weights do not change. Task 2 (clarify, compare, recommend) has a natural written
form and is shipped as one typed box (600 characters).

## 4. What the proxy rubrics can and cannot verify

`writing.apology_email`, `writing.balanced_report` and `speaking.clarify_compare_recommend_written` in
`rubrics.json`. Each criterion scores points x the mean of its checks, scaled by a word-count window.

What they **do** check: that expected phrases are present (a greeting and a closing, an apology formula,
"has been canceled/moved", "going to", "instead", "we can offer"; past-tense verbs, "was/were + -ing",
"while/when", contrast connectors, "than", "resilience/together"; indirect-question openers, "than/whereas",
"I recommend" and a relative word); that the text has enough sentence-ending marks and a linking word; and
that its length is in a window.

What they **cannot** check, and the tests pin it: register (a rude email with the right words scores the
same), cohesion, whether the phrases are used correctly rather than listed, spelling, accuracy of tenses,
whether the text answers the prompt, whether the apology is sincere or the alternative concrete, or anything
about pronunciation. **A list of the expected phrases, repeated until it has about 128 words, scores 5 of 5 in
Writing Task 1, 2.2 of 5 in Task 2 and 5 of 5 in Speaking** (`test_known_limit_...`). The rubrics do **not**
claim to measure register or cohesion; `past_verbs_at_least` counts any word ending in -ed.

Length: the source asks for 70 to 100 words. The form serves 70 and 100 as a hint (`min_words`, `max_words`) and
keeps 900 characters (`max_chars`). The rubric counts words in a wider window, **55 to 130** for Writing and
**25 to 150** for Speaking, applied to every criterion: outside it the score is 0 (all or nothing), inside it
the length no longer matters. **Needs sign-off.**

## 5. What the source did not give (so we wrote it, or left it out)

- Part titles and instructions, the writing box labels ("Write your email here.", "Write your report here.",
  "Write what you would say here."): `AUTHORED` in `tools/import_b1_form_a.py`. **Needs review.**
- Use of English has no parts in the source: one part, `tasks`.
- The voices of the conversations (who speaks first) are ours; single-sentence clips all use the same voice
  so it never hints at the answer. Item 7 is one speaker (the host).
- The 15 lessons are the curriculum's (1.1 to 4.4 does not exist; module sizes are 4, 4, 4 and 3). The titles
  and descriptions of the lessons are ours.
- Everything in the practice pack (dialogues, listening texts, exercises, vocabulary and its Spanish
  translations, prices and names) is new, written for this pack, and **needs a teacher's review** (each
  activity carries a flag saying so). The Spanish translations of the dialogue lines were not checked by a
  native speaker.

## 6. Audio: what the voice says that the screen does not show

The displayed text is never changed; the spoken text (`text`, `voices[].text` in `audio_manifest.json`) is
written out when a voice would misread it. **The 15 exam clips need no expansion**: the source already spells
its numbers ("nine fifteen", "one hundred and twenty thousand pesos", "from seven") and has no acronyms.
In practice, every clip whose shown transcript has a digit, a colon or a percent sign is listed here (the
practice tests fail if one is missing):

| clip | shown | spoken |
| --- | --- | --- |
| `en-b1-1.1.l2` | Do you know if the bank is open on Saturday? Yes, it opens at 9:00 and closes at 1:00. | Do you know if the bank is open on Saturday? Yes, it opens at nine and closes at one o'clock. |
| `en-b1-1.1.l3` | Sorry, I didn't understand. Could you say that again, please? Of course. The bus to the lighthouse leaves every 30 minutes. | Sorry, I didn't understand. Could you say that again, please? Of course. The bus to the lighthouse leaves every thirty minutes. |
| `en-b1-1.3.l2` | Which hotel is cheaper? The Palm Inn costs 80 dollars a night, and the Sea View costs 110. | Which hotel is cheaper? The Palm Inn costs eighty dollars a night, and the Sea View costs one hundred and ten. |
| `en-b1-1.4.l2` | We have postponed the concert until next Thursday because the singer is ill. It is going to start at 8:30, and your tickets are still valid. | We have postponed the concert until next Thursday because the singer is ill. It is going to start at eight thirty, and your tickets are still valid. |
| `en-b1-2.1.l1` | Is there a gym in the hotel? Yes, the gym is open from 6:00 to 9:00 in the morning and it is used mostly by business guests. | Is there a gym in the hotel? Yes, the gym is open from six to nine in the morning and it is used mostly by business guests. |
| `en-b1-2.2.l1` | How much is a return ticket to the island? It's 45,000 pesos, and children under five travel free. | How much is a return ticket to the island? It's forty-five thousand pesos, and children under five travel free. |
| `en-b1-2.2.l2` | Your reservation is for two nights, from the fourth to the sixth. Check-in starts at 2:00 in the afternoon. | Your reservation is for two nights, from the fourth to the sixth. Check-in starts at two o'clock in the afternoon. |
| `en-b1-2.3.l2` | You ought to take a jacket if you go up the hill. It gets cold after 6:00. | You ought to take a jacket if you go up the hill. It gets cold after six o'clock. |
| `en-b1-2.3.l3` | Is the cooking class worth the money? Definitely. It costs 60,000 pesos, but you should book early because it is very popular. | Is the cooking class worth the money? Definitely. It costs sixty thousand pesos, but you should book early because it is very popular. |
| `en-b1-3.1.l1` | I'd like to return these shoes. They're too small. No problem. You can have an exchange or store credit, but we can't give a refund after 30 days. | I'd like to return these shoes. They're too small. No problem. You can have an exchange or store credit, but we can't give a refund after thirty days. |
| `en-b1-3.1.l3` | You were overcharged by 15 dollars on your last bill, so we are going to return the money to your account in three days. | You were overcharged by fifteen dollars on your last bill, so we are going to return the money to your account in three days. |
| `en-b1-3.2.l1` | I'm afraid the air conditioning didn't work in our room. I'm very sorry. We'll take 20% off your bill. | I'm afraid the air conditioning didn't work in our room. I'm very sorry. We'll take twenty percent off your bill. |
| `en-b1-3.2.l2` | I'm sorry, but I can't change the price without the manager's approval. She will be back at 4:00. | I'm sorry, but I can't change the price without the manager's approval. She will be back at four o'clock. |
| `en-b1-3.4.l2` | In my country people usually arrive on time, whereas here a meeting can start 15 minutes late. | In my country people usually arrive on time, whereas here a meeting can start fifteen minutes late. |
| `en-b1-4.1.l1` | Visitors mustn't pick any flowers, and they should stay on the marked trail. The fine for breaking the rules is 100 dollars. | Visitors mustn't pick any flowers, and they should stay on the marked trail. The fine for breaking the rules is one hundred dollars. |
| `en-b1-4.3.l3` | Is there a place where we can see traditional dancing? Yes, there is a small theater in the old town where local groups perform every Saturday at 7:00. | Is there a place where we can see traditional dancing? Yes, there is a small theater in the old town where local groups perform every Saturday at seven o'clock. |

Real durations (from the generated files, no padding): **15 exam clips**, 4.13 to 12.53 s, mean 7.9 s, total 2.0 min; **135 practice clips**,
1.85 to 11.83 s, mean 5.2 s, total 11.7 min; **150 clips in all**. Longest: `b1fa-ab10370c4f` (12.5 s), `en-b1-4.3.l3` (11.8 s), `en-b1-3.1.l1` (11.4 s), `en-b1-4.3.l1` (11.3 s), `en-b1-3.4.l3` (11.3 s).
Loudness is -22.9 to -18.8 LUFS around the -19 target; clips below -21.5 LUFS (a single short turn
has too little signal for the normaliser): `en-b1-1.2.d3` (-22.9), `en-b1-3.4.d3` (-22.0).

## 7. Audio QA: listen to these first (nobody has yet)

1. **Numbers, times and prices** (the sixteen clips in section 6, then the exam's): `b1fa-5851db3141`
   (nine fifteen), `b1fa-67d673eb56` (one hundred and twenty thousand pesos: "pesos" in an English voice),
   `b1fa-4ca657c38b` (from seven); practice `en-b1-1.1.l2`, `en-b1-1.3.l2` (eighty / one hundred and ten),
   `en-b1-2.2.l1` (forty-five thousand pesos), `en-b1-2.2.l2` (fourth, sixth, two o'clock),
   `en-b1-2.3.l3` (sixty thousand pesos), `en-b1-3.2.l1` (twenty percent), `en-b1-4.1.l1` (one hundred dollars).
2. **The exam conversations** (two voices): `b1fa-5851db3141`, `b1fa-ab10370c4f`, `b1fa-cad0a69784`,
   `b1fa-f17a895d5f`, `b1fa-12ba25658d`. Turns must alternate with the right voice, 450 ms apart, nothing cut;
   the complaint (item 6) and the apology (item 7, `b1fa-ed6216a4e3`, one voice) must sound natural.
3. **The five matching clips** (`b1fa-3615973861`, `b1fa-b3b444f6ea`, `b1fa-e7d8f61631`, `b1fa-62446bfba4`,
   `b1fa-f8a5436d96`): each must say exactly its text. Check "customs house" (audio 3) is not heard as "customs
   post" (audio 5), see 2e.
4. **Words with odd stress or foreign names:** "pesos", "customs", "lighthouse", "itinerary", "Pérez" is not in
   any audio; "Marta", "Casa Verde", "Calle del Faro" appear in options only.
5. **The longest clips** (above) at the 0.85 speed: pace and pauses at the semicolons of `b1fa-afa6323603`.
6. **The shortest practice clips** (the dialogue partner lines, {", ".join(f"`{c['id']}` ({c['duration_s']:.1f} s)" for c in shortest)}): too quiet or clipped at the start or end.
7. Each exam clip must say exactly its `text` and nothing that gives the answer away; no file name, URL or
   response carries the item or the key (`b1fa-<hash>`).

If a clip is wrong, fix its `text` or `voices[].text` in `audio_manifest.json` (or the voice in `tts.json`)
and run `python -m app.scripts.content tts --pack en/B1`; only the clips whose input changed are remade.

## 8. Exam audio: who says what

| item | clip | voices |
| --- | --- | --- |
| item 1 (what time: nine fifteen) | `b1fa-5851db3141` (10.51 s) | guest then host |
| item 2 (one hundred and twenty thousand pesos) | `b1fa-67d673eb56` (7.85 s) | host |
| item 3 (from seven) | `b1fa-4ca657c38b` (6.34 s) | host |
| item 4 | `b1fa-554a8dece8` (7.73 s) | host |
| item 5 | `b1fa-afa6323603` (7.94 s) | host |
| item 6 (conversation) | `b1fa-ab10370c4f` (12.53 s) | guest then host |
| item 7 | `b1fa-ed6216a4e3` (9.94 s) | host |
| item 8 (conversation) | `b1fa-cad0a69784` (11.21 s) | guest then host |
| item 9 (conversation) | `b1fa-f17a895d5f` (8.62 s) | guest then host |
| item 10 (conversation) | `b1fa-12ba25658d` (10.97 s) | guest then host |
| item 11, audio 1 | `b1fa-3615973861` (4.13 s) | host |
| item 11, audio 2 | `b1fa-b3b444f6ea` (4.94 s) | host |
| item 11, audio 3 (customs house) | `b1fa-e7d8f61631` (4.92 s) | host |
| item 11, audio 4 | `b1fa-62446bfba4` (5.93 s) | host |
| item 11, audio 5 (passport, declare) | `b1fa-f8a5436d96` (4.61 s) | host |

