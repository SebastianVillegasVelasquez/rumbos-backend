"""The example payloads and measured sizes of the contract, built with the
production builders from the B2 Form A (the largest exam) and fixed ids.

Written by `tools/export_contracts.py` into `contracts/`; a test keeps them
honest. Sizes are the canonical JSON bytes (what the 64 KB cap measures); the
signed token is about a third larger (base64url)."""

import json
import uuid
from typing import Any

from app.assessment.engine import score_form
from app.assessment.rubric import blank_ids
from app.mirror.attempt import MAX_STATEMENT_BYTES, attempt_claims
from app.mirror.definition import build_definition, canonical, definition_hash
from app.mirror.schemas import SCHEMAS
from app.mirror.statements import definition_claims
from app.passback.statement import claims_for
from app.trainer.assessment_form import ProductiveItem
from app.trainer.language import ENGLISH, MAX_FREE_TEXT_CHARS
from tests.signing_vectors import FINISHED, IAT, ISSUER, POLICY, TENANT
from tests.test_mirror_attempt import pack_runtime, responses_for

ATTEMPT = uuid.UUID("0190a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a60")
LEVELS = [("A1", "a1-form-a"), ("A2", "a2-form-a"), ("B2", "b2-form-a")]
TEXT = (
    "Dear Anna, thank you for your invitation. I would love to come to the "
    "party on Saturday. Shall I bring something to eat or drink? See you soon."
)
EMOJI = "\U0001f600"


def _filled_to_the_limit(responses: dict[str, Any], form: Any) -> dict[str, Any]:
    """Every written answer as long as `clean_response` lets it be, in
    four-byte characters: the largest record a student can make."""
    full = dict(responses)
    for section in form.sections:
        for item in section.items():
            if not isinstance(item, ProductiveItem):
                continue
            if item.template:
                ids, cap = blank_ids(item.template), MAX_FREE_TEXT_CHARS
            else:
                ids = [f.id for f in item.fields] or [q.id for q in item.questions]
                cap = item.max_chars or MAX_FREE_TEXT_CHARS
            full[item.item_id] = {"values": {i: EMOJI * cap for i in ids}}
    return full


def _attempt(
    level: str,
    form_id: str,
    *,
    text: str,
    include_text: bool,
    max_bytes: int = MAX_STATEMENT_BYTES,
    worst: bool = False,
) -> dict[str, Any]:
    runtime = pack_runtime(level, form_id)
    form = runtime.form
    responses = responses_for(form, right=True, text=text)
    if worst:
        responses = _filled_to_the_limit(responses, form)
    scored = score_form(runtime, responses, ENGLISH)
    percent = scored.total.percent
    base = claims_for(
        attempt_id=ATTEMPT,
        tenant_id=TENANT,
        course_id=7,
        sub="s-4f9d2c",
        purpose="final",
        ordinal=1,
        form_id=form_id,
        content_version="0123456789abcdef",
        policy=POLICY,
        result={"percent": f"{float(percent):.2f}", "passed": percent > 80},
        finished_at=FINISHED.isoformat(),
        issuer=ISSUER,
        issued_at=IAT,
    )
    claims = attempt_claims(
        base,
        form=form,
        definition_hash=definition_hash(build_definition(form)),
        status="submitted",
        responses=responses,
        scored=scored,
        include_written_text=include_text,
        max_bytes=max_bytes,
    )
    plain: dict[str, Any] = json.loads(json.dumps(claims))
    return plain


def _definition(level: str, form_id: str) -> dict[str, Any]:
    form = pack_runtime(level, form_id).form
    definition = build_definition(form)
    claims = definition_claims(
        tenant_id=TENANT,
        form_id=form_id,
        content_version="0123456789abcdef",
        definition_hash=definition_hash(definition),
        definition=definition,
        issuer=ISSUER,
        issued_at=IAT,
    )
    plain: dict[str, Any] = json.loads(json.dumps(claims))
    return plain


def examples() -> dict[str, dict[str, Any]]:
    """Name (file stem) to payload, in `contracts/examples/`."""
    full = _attempt("B2", "b2-form-a", text=TEXT, include_text=True)
    return {
        "definition-b2": _definition("B2", "b2-form-a"),
        "attempt-b2": _attempt("B2", "b2-form-a", text=TEXT, include_text=False),
        "attempt-b2-with-text": full,
        "attempt-b2-truncated": _attempt(
            "B2",
            "b2-form-a",
            text=TEXT,
            include_text=True,
            max_bytes=len(canonical(full)) - 1,
        ),
    }


def sizes() -> dict[str, Any]:
    """Canonical JSON bytes of the statements, for every exam we have."""
    out: dict[str, Any] = {"cap_bytes": MAX_STATEMENT_BYTES, "forms": {}}
    for level, form_id in LEVELS:
        out["forms"][form_id] = {
            "definition_bytes": len(canonical(_definition(level, form_id))),
            "attempt_bytes_without_text": len(
                canonical(_attempt(level, form_id, text=TEXT, include_text=False))
            ),
            # Every written answer filled to its limit with four-byte characters.
            "attempt_bytes_worst_case": len(
                canonical(
                    _attempt(level, form_id, text=TEXT, include_text=True, worst=True)
                )
            ),
        }
    return out


def schemas() -> dict[str, dict[str, Any]]:
    return {
        name: model.model_json_schema(by_alias=True) for name, model in SCHEMAS.items()
    }
