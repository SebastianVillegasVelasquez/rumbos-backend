# Decisions: language trainer and native assessment

Judgment calls made while building the trainer and the assessment (English A1).
Each entry says what was decided, why, and what would change it. Decisions that
change what students see, or the certification rule, are marked **ASK** and are
listed in the final report.

## D1. Content packs are JSON, not YAML

The runtime has no YAML parser and new runtime dependencies are not allowed.
JSON is in the standard library, diffs well in git, and is validated by typed
pydantic models. Cost: no comments in the files; authoring notes go in a
`notes` field or in the docs.

## D2. Content lives in the repository, not in the database

Reviewable in git, deployed atomically with the code that reads it, no admin UI
to build or secure. Cost: a content change needs a deploy. The Dockerfile
copies `content/` into the image.

## D3. Client source documents are inputs, not part of the repository

The `.docx` and the curriculum `.pdf` sit in `docs/` for the import tool and are
ignored by git. Only the section "Evaluacion diagnostica", Forma A, of the
`.docx` is read; the rest of that file is never parsed or printed.

## D4. Assessment option ids are opaque, not letters **(design)**

In the source the correct option is very often A or B. The stored form keeps the
source letters; the API will serve ids derived from `item_id` and letter by a
hash, so an id carries no position and cannot reveal the bias even to someone
reading the network traffic. The shuffle then changes order only.

## D5. The module map leaves out two kinds of item

The matching task's code spans modules ("M-I a M-IV") and Writing and Speaking
tasks have no code in the source. None of them enters `by_module`; they still
count in the section scores and the total. Revisit if the client wants the five
pairs assigned to modules (that would mean editing the source's coding).

## D6. Listening transcripts are not served in an assessment

The contract's practice `audio` carries a transcript. In an exam a transcript
would turn Listening into Reading, so assessment audio is served without one.
The transcript is kept for authoring and audit. **ASK**: a student who cannot
hear needs an accommodation (a documented alternative), not a hidden
transcript.

## D7. Speaking written equivalents are ours **(ASK: needs pedagogical sign-off)**

The document allows "la version escrita equivalente". For Task 1 (read aloud)
the written equivalent is: write the welcome note of Writing Task 2, scored for
completeness, sentence marks and intelligibility. For Task 2 the three
questions are answered in writing; the document's alternative "or describe the
image" is not offered. Neither is the client's wording.

## D8. A pass can come from the template itself unless the rubric stops it

A first version of the Writing Task 2 rubric gave 4 of 5 points to a note whose
blanks were all empty, because the template's own words matched the checks. The
fix is a `multiplier` check that scales a criterion by how much of the template
the student filled. A test pins it. Other proxy limits are in the final report.

## D9. The Writing 2 word range is tight against the template

The template has 19 fixed words and the document asks for 15 to 25 words, so
the five blanks may hold about 6 words in all (a place named "Posada El
Encuentro" already breaks it). The proxy keeps the document's range; it costs
at most half a point in the intelligibility criterion. Raise the range if the
client agrees.

## D10. Exactly 80 does not pass; it is still "A1 logrado"

Under `pass_operator = "gt"` a total of exactly 80.00 is band `achieved` and
does not pass. Every reachable total is a multiple of 1/6, so the smallest
passing total is 80.17. **ASK: to be confirmed in writing** (the rule was given
verbally).

## D11. Rate limiting is per process

The existing limiter keeps counts in process memory, so with `WORKERS=n` the
effective limit is up to n times the configured one. The assessment endpoints
reuse it; a shared limit belongs in the reverse proxy.
