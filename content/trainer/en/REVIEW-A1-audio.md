# A1 audio: what a person must listen to

Nobody has heard these 150 clips. The manifest hash proves the file is the one
generated, not that it sounds right. Listen in this order; ids are in
`content/trainer/en/A1/audio/<id>.mp3`. Voices `host` = am_michael,
`guest` = af_heart; pace 0.80.

## 1. Exam clips (15), all of them, first
Each must say exactly the `text` in `audio_manifest.json` and nothing else.
- `a1fa-b383c06b05` "seven fifteen": must be 7:15, not "seventy-five".
- `a1fa-d80d1a378a` "three, oh, four": three separate digits.
- `a1fa-3cb47910eb` "twelve thousand pesos": "pesos" must sound Spanish/natural.
- `a1fa-ddfde47578` "opens at nine ... closes at six": nine vs. five is the answer.
- `a1fa-1f9b60ecf8` "forty": must not sound like "fourteen".
- `a1fa-4095dc1154` "seven o'clock in the morning" (matching: dock).
- `a1fa-5db229f78b`, `a1fa-c1bdc09467`, `a1fa-5849d9361c`, `a1fa-fba159242a`: the other matching clips; check "medicine for a cough" and "white bin".
- Two-voice dialogues, check guest asks first and host answers, no cut turns:
  `a1fa-7ccee28f41`, `a1fa-a6ea39545b` ("doesn't"), `a1fa-df9381df05`,
  `a1fa-6874276db2`, `a1fa-9bd7851840` ("What's the matter").

## 2. Spelled letters
- `en-a1-1.4.l3`: "T, O, R, R, E, S": one letter each, "O" is the letter, "R, R" two clear letters.

## 3. Numbers, times and prices (practice)
`en-a1-1.4.l1` (phone "two, five, one, nine"), `en-a1-1.4.l2` (twenty-two vs
twelve), `en-a1-2.1.d6` and `en-a1-2.1.l2` (fifteen vs fifty, eighteen vs
eighty; "pesos"), `en-a1-1.3.l1` ("eight o'clock"), `en-a1-1.3.l2` ("half past
nine"), `en-a1-1.3.d4`, `en-a1-2.2.l1`, `en-a1-3.3.d6`, `en-a1-4.3.l2` (ten,
nine, eight, five), `en-a1-2.2.l3`, `en-a1-3.1.l3`, `en-a1-3.4.l2`.

## 4. Names and words the engine may mispronounce
`en-a1-1.1.l2` ("Mister Brown"), `en-a1-1.1.d2` (Maria), `en-a1-1.1.l1` (Laura),
`en-a1-2.3.l2` (Carlos, Cartagena: English reading is expected),
`en-a1-3.4.d1` ("okay"). Also any clip with "pharmacy", "medicine", "reserve".

## 5. Dialogue partner lines (guest voice, 45 clips)
Spot-check the first and last `.dN` clip of every lesson: pace, no clipped start
or end, tone matches the line (questions rise).

## 6. Everything else
One listening clip per lesson (`.l1`), then loudness consistency (-19 LUFS target;
very short clips measure down to -22.6).

If a clip is wrong: fix `text` / `voices[].text` in `audio_manifest.json`, then
`uv run --group tts python -m app.scripts.content tts --pack en/A1`; only the
changed clips are remade.
