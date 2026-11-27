"""With `CLOSING_GATE_MODE=off`, `GET /trainer/closing` answers the very bytes it
answered before the journey existed.

The files in `tests/fixtures/closing_off/` were recorded by running this module
against `main` at ff84961 (before the observe mode), so they are the proof, not a
copy of what the current code says. Everything in the answers is deterministic:
the clock is fake and no state used here has an attempt in progress (the one
random value, its id). To record again, on a commit whose `off` is the wanted
one: `RECORD_CLOSING_OFF=1 pytest tests/test_closing_off_bytes.py`.
"""

import os
from pathlib import Path
from typing import Any

import pytest

from tests import test_closing_api as api
from tests.fakes import FakeCompletionProvider
from tests.test_assessment_api import put, start

env = api.env
clock = api.clock
draft_content_is_served = api.draft_content_is_served
provider = api.provider

FIXTURES = Path(__file__).parent / "fixtures" / "closing_off"

# What the student sits, in order: (purpose, `right` or `wrong`).
STATES: dict[str, list[tuple[str, str]]] = {
    "nothing_sat": [],
    "checkin_passed": [("check_in", "right")],
    "checkin_failed": [("check_in", "wrong")],
    "final_passed": [("check_in", "wrong"), ("final", "right")],
    "final_exhausted": [("check_in", "wrong"), *[("final", "wrong")] * 3],
}


@pytest.mark.parametrize("state", list(STATES))
async def test_the_off_answer_is_byte_identical(
    env: dict[str, Any], provider: FakeCompletionProvider, state: str
) -> None:
    await api.world(env)  # no `mode` fixture: CLOSING_GATE_MODE keeps its default
    s = await api.learner(env, "alumno1")
    for purpose, answers in STATES[state]:
        attempt = (await start(s, purpose))["attempt_id"]
        await put(s, attempt, answers)
        assert (await s.post(f"/trainer/attempts/{attempt}/submit")).status_code == 200
    r = await s.get("/trainer/closing")
    assert r.status_code == 200, r.text
    path = FIXTURES / f"{state}.json"
    if os.environ.get("RECORD_CLOSING_OFF"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(r.content)
    assert r.content == path.read_bytes()
    assert provider.calls == 0
