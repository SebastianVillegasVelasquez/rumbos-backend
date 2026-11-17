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
