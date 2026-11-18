# Practice trainer: request and response examples

Real exchanges captured from the running API on a small fictitious pack
(activities `demo-grammar` and `demo-dialogue`), with the ids replaced by fixed
fake ones. Every path is under `/api/v1`; all calls carry
`Authorization: Bearer <token>`. Field names are snake_case.

## Who sees what

| | Editor in preview | Student |
| --- | --- | --- |
| Activity is `draft` or `reviewed` (production) | session opens, same payloads as below | `404 {"detail": "Not found"}` on `POST /trainer/sessions`; the catalog does not list it |
| Activity is `published` | same payloads | same payloads |

The **payloads are identical for both roles**: `sessions`, `answers` and `finish`
carry no `publication_state` and no preview flag. What differs is only whether
the call is allowed. The state of an activity is in `GET /trainer/catalog`
(`activities[].publication_state`, and `preview` at the top level). Sessions
are private to the caller (`sub`): an editor's preview sessions are never seen
by students, and the editor does not see theirs.

The order of `options`, `tokens` and `right` is shuffled per session and stays
the same when the session is resumed. A step never carries its answer.

## 1. Editor opens a draft exercise

`201` the first time; `200` with the same `session_id` and the same order when
the activity is already open.

```http
POST /api/v1/trainer/sessions
Content-Type: application/json

{
  "activity_id": "demo-grammar"
}
```

Response `201`:

```json
{
  "session_id": "9b2e7c10-5d4f-4e8a-a6b3-1c0f8d2e4a01",
  "activity_id": "demo-grammar",
  "title": "Be: am, is, are",
  "status": "in_progress",
  "kind": "exercise",
  "steps": [
    {
      "step_id": "t.mcq",
      "type": "mcq",
      "prompt": "Pick the right one",
      "hint": null,
      "audio": null,
      "options": [
        {
          "id": "c",
          "text": "am"
        },
        {
          "id": "a",
          "text": "is"
        },
        {
          "id": "b",
          "text": "are"
        }
      ],
      "answered": false,
      "last_outcome": null
    },
    {
      "step_id": "t.gap",
      "type": "gap_fill",
      "prompt": "Fill",
      "hint": null,
      "audio": null,
      "template": "I {g1} a guide.",
      "gaps": [
        {
          "gap_id": "g1",
          "options": null
        }
      ],
      "answered": false,
      "last_outcome": null
    },
    {
      "step_id": "t.reorder",
      "type": "reorder",
      "prompt": "Order",
      "hint": null,
      "audio": null,
      "tokens": [
        {
          "id": "t2",
          "text": "are"
        },
        {
          "id": "t1",
          "text": "Where"
        },
        {
          "id": "t3",
          "text": "you"
        }
      ],
      "answered": false,
      "last_outcome": null
    },
    {
      "step_id": "t.match",
      "type": "match",
      "prompt": "Match",
      "hint": null,
      "audio": null,
      "left": [
        {
          "id": "l1",
          "text": "Hello"
        },
        {
          "id": "l2",
          "text": "Bye"
        }
      ],
      "right": [
        {
          "id": "r2",
          "text": "Goodbye"
        },
        {
          "id": "r1",
          "text": "Hi"
        }
      ],
      "answered": false,
      "last_outcome": null
    },
    {
      "step_id": "t.short",
      "type": "short_answer",
      "prompt": "Say your country",
      "hint": null,
      "audio": null,
      "max_length": 60,
      "answered": false,
      "last_outcome": null
    }
  ],
  "summary": null,
  "turn": null,
  "turns_total": null
}
```

## 2. Student asks for the same draft

Production, content not published:

```http
POST /api/v1/trainer/sessions
Content-Type: application/json

{
  "activity_id": "demo-grammar"
}
```

Response `404`:

```json
{
  "detail": "Not found"
}
```

## 3. Answers

`attempt_id` is a client-generated UUID; resending the same one answers again without counting twice. `response` depends on the step type: `{option_id}` (mcq), `{values: {gap_id: text}}` (gap_fill), `{token_ids: [...]}` (reorder), `{pairs: [[left, right], ...]}` (match), `{text}` (short_answer, and typed dialogue turns).

Wrong answer (`outcome` is `correct`, `partial`, `incorrect` or `accepted`):

```http
POST /api/v1/trainer/sessions/9b2e7c10-5d4f-4e8a-a6b3-1c0f8d2e4a01/answers
Content-Type: application/json

{
  "step_id": "t.mcq",
  "attempt_id": "9b2e7c10-5d4f-4e8a-a6b3-1c0f8d2e4a02",
  "response": {
    "option_id": "a"
  }
}
```

Response `200`:

```json
{
  "outcome": "incorrect",
  "earned": 0,
  "total": 1,
  "feedback": {
    "explanation": "You takes are.",
    "closest_answer": "are",
    "error_tags": [
      "Use are with you."
    ]
  },
  "next": null,
  "finished": false
}
```

Right answer:

```http
POST /api/v1/trainer/sessions/9b2e7c10-5d4f-4e8a-a6b3-1c0f8d2e4a01/answers
Content-Type: application/json

{
  "step_id": "t.gap",
  "attempt_id": "9b2e7c10-5d4f-4e8a-a6b3-1c0f8d2e4a03",
  "response": {
    "values": {
      "g1": "am"
    }
  }
}
```

Response `200`:

```json
{
  "outcome": "correct",
  "earned": 1,
  "total": 1,
  "feedback": {
    "explanation": "I takes am.",
    "closest_answer": null,
    "error_tags": []
  },
  "next": null,
  "finished": false
}
```

Not a possible answer (for example a right-hand item used twice):

```http
POST /api/v1/trainer/sessions/9b2e7c10-5d4f-4e8a-a6b3-1c0f8d2e4a01/answers
Content-Type: application/json

{
  "step_id": "t.match",
  "attempt_id": "9b2e7c10-5d4f-4e8a-a6b3-1c0f8d2e4a04",
  "response": {
    "pairs": [
      [
        "l1",
        "r1"
      ],
      [
        "l2",
        "r1"
      ]
    ]
  }
}
```

Response `422`:

```json
{
  "detail": {
    "code": "invalid_response",
    "message": "That is not a possible answer to this item"
  }
}
```

Unknown step:

```http
POST /api/v1/trainer/sessions/9b2e7c10-5d4f-4e8a-a6b3-1c0f8d2e4a01/answers
Content-Type: application/json

{
  "step_id": "nope",
  "attempt_id": "9b2e7c10-5d4f-4e8a-a6b3-1c0f8d2e4a05",
  "response": {
    "text": "x"
  }
}
```

Response `404`:

```json
{
  "detail": "Not found"
}
```

## 4. Finish

Counts the steps answered, not the steps in the activity (`steps_answered` 2 of `steps_total` 5 gives `percent` 20).

```http
POST /api/v1/trainer/sessions/9b2e7c10-5d4f-4e8a-a6b3-1c0f8d2e4a01/finish
```

Response `200`:

```json
{
  "session_id": "9b2e7c10-5d4f-4e8a-a6b3-1c0f8d2e4a01",
  "summary": {
    "tries": 3,
    "percent": 20,
    "to_revise": [
      "be_agreement"
    ],
    "steps_total": 5,
    "missed_steps": [
      "t.mcq"
    ],
    "steps_answered": 2,
    "first_try_correct": 1
  }
}
```

## 5. Dialogue (editor, draft)

One turn at a time: `steps` has only the current turn, `turn` / `turns_total` give the progress. A right answer returns the next turn in `next`; the last one returns `finished: true`.

```http
POST /api/v1/trainer/sessions
Content-Type: application/json

{
  "activity_id": "demo-dialogue"
}
```

Response `201`:

```json
{
  "session_id": "9b2e7c10-5d4f-4e8a-a6b3-1c0f8d2e4a06",
  "activity_id": "demo-dialogue",
  "title": "Meet a guest",
  "status": "in_progress",
  "kind": "dialogue",
  "steps": [
    {
      "step_id": "n1",
      "type": "dialogue_turn",
      "partner_line": "Hello!",
      "translation": null,
      "audio": null,
      "mode": "choose",
      "options": [
        {
          "id": "o1",
          "text": "Hi"
        },
        {
          "id": "o2",
          "text": "Hello"
        }
      ],
      "starters": [],
      "word_bank": []
    }
  ],
  "summary": null,
  "turn": 1,
  "turns_total": 2
}
```

```http
POST /api/v1/trainer/sessions/9b2e7c10-5d4f-4e8a-a6b3-1c0f8d2e4a06/answers
Content-Type: application/json

{
  "step_id": "n1",
  "attempt_id": "9b2e7c10-5d4f-4e8a-a6b3-1c0f8d2e4a07",
  "response": {
    "option_id": "o1"
  }
}
```

Response `200`:

```json
{
  "outcome": "accepted",
  "earned": 1,
  "total": 1,
  "feedback": {
    "explanation": null,
    "closest_answer": null,
    "error_tags": []
  },
  "next": {
    "step_id": "n2",
    "type": "dialogue_turn",
    "partner_line": "What is your name?",
    "translation": null,
    "audio": null,
    "mode": "type",
    "options": [],
    "starters": [],
    "word_bank": []
  },
  "finished": false
}
```

Answering a turn that is not the current one:

```http
POST /api/v1/trainer/sessions/9b2e7c10-5d4f-4e8a-a6b3-1c0f8d2e4a06/answers
Content-Type: application/json

{
  "step_id": "n1",
  "attempt_id": "9b2e7c10-5d4f-4e8a-a6b3-1c0f8d2e4a08",
  "response": {
    "option_id": "o1"
  }
}
```

Response `409`:

```json
{
  "detail": {
    "code": "not_current_turn",
    "message": "The request conflicts with the state of the exam"
  }
}
```

## 6. Student on a published activity

Same shapes as the editor's. Opening, answering and finishing:

```http
POST /api/v1/trainer/sessions/9b2e7c10-5d4f-4e8a-a6b3-1c0f8d2e4a09/answers
Content-Type: application/json

{
  "step_id": "t.mcq",
  "attempt_id": "9b2e7c10-5d4f-4e8a-a6b3-1c0f8d2e4a10",
  "response": {
    "option_id": "a"
  }
}
```

Response `200`:

```json
{
  "outcome": "incorrect",
  "earned": 0,
  "total": 1,
  "feedback": {
    "explanation": "You takes are.",
    "closest_answer": "are",
    "error_tags": [
      "Use are with you."
    ]
  },
  "next": null,
  "finished": false
}
```

```http
POST /api/v1/trainer/sessions/9b2e7c10-5d4f-4e8a-a6b3-1c0f8d2e4a09/finish
```

Response `200`:

```json
{
  "session_id": "9b2e7c10-5d4f-4e8a-a6b3-1c0f8d2e4a09",
  "summary": {
    "tries": 1,
    "percent": 0,
    "to_revise": [
      "be_agreement"
    ],
    "steps_total": 5,
    "missed_steps": [
      "t.mcq"
    ],
    "steps_answered": 1,
    "first_try_correct": 0
  }
}
```

Answering after `finish`:

```http
POST /api/v1/trainer/sessions/9b2e7c10-5d4f-4e8a-a6b3-1c0f8d2e4a09/answers
Content-Type: application/json

{
  "step_id": "t.gap",
  "attempt_id": "9b2e7c10-5d4f-4e8a-a6b3-1c0f8d2e4a11",
  "response": {
    "values": {
      "g1": "am"
    }
  }
}
```

Response `409`:

```json
{
  "detail": {
    "code": "finished",
    "message": "The request conflicts with the state of the exam"
  }
}
```

The full `201` session view of a published activity is the one in section 1 (starting it again answers `200` with the same body).
