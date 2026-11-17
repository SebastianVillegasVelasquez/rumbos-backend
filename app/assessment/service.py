"""The exam lifecycle: list, start (or resume), read a section, autosave,
submit, result. The server clock is the only clock.

An attempt past its deadline is closed as `expired` with whatever was saved,
lazily on any request touching it (and by `app.scripts.assessments sweep`).
Neither this module nor anything it returns includes a key, an accepted
answer, a rubric or a per-item verdict.
"""

import logging
import secrets
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from fractions import Fraction
from typing import Any

from app.assessment.engine import (
    FormRuntime,
    clean_response,
    runtime_for,
    score_form,
    section_summaries,
    section_view,
)
from app.assessment.metrics import METRICS
from app.assessment.scoring import decimal_string, passes
from app.exceptions import AssessmentConflictError, TrainerNotFoundError
from app.passback.statement import claims_for
from app.repositories.sqlalchemy.assessment_repository import (
    AttemptRecord,
    SqlAlchemyAssessmentRepository,
)
from app.schemas.map_settings import AssessmentPolicy, AssessmentSettings
from app.trainer.catalog import Catalog
from app.trainer.language import ENGLISH
from app.trainer.pack import Pack

log = logging.getLogger("rumbos.assessment")

TITLES = {"check_in": "Evaluación diagnóstica", "final": "Evaluación final"}
Clock = Callable[[], datetime]


@dataclass(frozen=True)
class Caller:
    """Who is asking: a student of a course, or an editor in preview."""

    tenant_id: uuid.UUID
    course_id: int
    sub: str
    preview: bool


def _res(attempt: AttemptRecord) -> Mapping[str, Any]:
    """The stored result of a closed attempt."""
    assert attempt.result is not None
    return attempt.result


def _frac(text: str) -> Fraction:
    return Fraction(text)


def _policy_of(settings: AssessmentSettings, purpose: str) -> AssessmentPolicy:
    policy = settings.check_in if purpose == "check_in" else settings.final
    if purpose not in TITLES or policy is None or not settings.enabled:
        raise TrainerNotFoundError
    return policy


class AssessmentService:
    def __init__(
        self,
        attempts: SqlAlchemyAssessmentRepository,
        catalog: Catalog,
        settings: AssessmentSettings | None,
        caller: Caller,
        clock: Clock,
        *,
        serve_drafts: bool,
        audio_url: Callable[[str, str], str],
        issuer: str = "rumbos-backend",
    ) -> None:
        self._issuer = issuer
        self._attempts = attempts
        self._catalog = catalog
        self._settings = settings
        self._caller = caller
        self._clock = clock
        self._drafts = serve_drafts
        self._audio_url = audio_url

    # --- content -----------------------------------------------------------------

    def _runtime(self, form_id: str) -> tuple[Pack, FormRuntime] | None:
        found = self._catalog.form(form_id)
        if found is None:
            return None
        pack, _ = found
        if pack.served_form(form_id, self._drafts) is None:
            return None
        return pack, runtime_for(pack, form_id, ENGLISH)

    def _audio(self, pack: Pack) -> Callable[[str], dict[str, Any]]:
        def ref(audio_id: str) -> dict[str, Any]:
            entry = pack.audio[audio_id]
            return {
                "url": self._audio_url(audio_id, pack.content_version),
                "duration_s": entry.duration_s,
            }

        return ref

    # --- list --------------------------------------------------------------------

    async def overview(self) -> dict[str, Any]:
        settings = self._settings
        if settings is None or not settings.enabled:
            return {"enabled": False}
        attempts = await self._attempts.list_for_user(
            self._caller.sub, self._caller.preview
        )
        items = []
        for purpose in ("check_in", "final"):
            policy = settings.check_in if purpose == "check_in" else settings.final
            if policy is None:
                continue
            items.append(await self._list_item(settings, purpose, policy, attempts))
        shown = settings.check_in or settings.final
        assert shown is not None
        return {
            "enabled": True,
            "preview": self._caller.preview,
            "policy": {
                "pass_percent": settings.pass_percent,
                "pass_operator": settings.pass_operator,
                "time_limit_min": shown.time_limit_min,
            },
            "items": items,
        }

    async def _list_item(
        self,
        settings: AssessmentSettings,
        purpose: str,
        policy: AssessmentPolicy,
        attempts: list[AttemptRecord],
    ) -> dict[str, Any]:
        mine = [a for a in attempts if a.purpose == purpose]
        runtime = self._runtime(policy.form_id)
        closed = []
        for attempt in mine:
            if attempt.status == "in_progress" and self._past(attempt):
                attempt = await self._expire(attempt)
            closed.append(attempt)
        open_ = next((a for a in closed if a.status == "in_progress"), None)
        done = [a for a in closed if a.result]
        best = max(done, key=lambda a: _frac(_res(a)["percent_exact"]), default=None)
        passed = any(_res(a)["passed"] for a in done)
        used = len(closed)
        state = "available"
        reason = None
        if runtime is None:
            state, reason = "locked", "not_available"
        elif open_:
            state = "in_progress"
        elif passed and not self._caller.preview:
            state = "passed"
        elif used >= policy.max_attempts and not self._caller.preview:
            state = "exhausted"
        form = runtime[1].form if runtime else self._any_form(policy.form_id)
        return {
            "purpose": purpose,
            "title": TITLES[purpose],
            "section_count": len(form.sections) if form else 0,
            "item_count": sum(len(s.items()) for s in form.sections) if form else 0,
            "attempts": {"used": used, "max": policy.max_attempts},
            "state": state,
            "locked_reason": reason,
            "in_progress": (
                {"attempt_id": open_.id, "deadline_at": open_.deadline_at}
                if open_
                else None
            ),
            "best": (
                {
                    "percent": _res(best)["percent"],
                    "passed": bool(_res(best)["passed"]),
                    "finished_at": best.finished_at,
                }
                if best and best.finished_at
                else None
            ),
        }

    def _any_form(self, form_id: str) -> Any:
        found = self._catalog.form(form_id)
        return found[0].forms[form_id] if found else None

    # --- start -------------------------------------------------------------------

    def _require_enabled(self) -> AssessmentSettings:
        if self._settings is None or not self._settings.enabled:
            raise TrainerNotFoundError
        return self._settings

    async def start(self, purpose: str) -> tuple[dict[str, Any], bool]:
        """(response, created). A second call while one is in progress resumes it."""
        self._require_enabled()
        assert self._settings is not None
        policy = _policy_of(self._settings, purpose)
        loaded = self._runtime(policy.form_id)
        if loaded is None:
            raise TrainerNotFoundError
        _, runtime = loaded
        caller = self._caller
        now = self._clock()

        existing = await self._attempts.open_attempt(
            caller.sub, purpose, caller.preview
        )
        if existing and self._past(existing):
            await self._expire(existing)
            existing = None
        if existing:
            return self._started(existing, runtime, now), False

        if not caller.preview:
            history = await self._attempts.list_for_user(caller.sub, False)
            mine = [a for a in history if a.purpose == purpose]
            if any(a.result and a.result["passed"] for a in mine):
                raise AssessmentConflictError("locked")
        created = await self._attempts.create(
            moodle_course_id=caller.course_id,
            sub=caller.sub,
            purpose=purpose,
            form_id=policy.form_id,
            content_version=self._catalog_version(policy.form_id),
            seed=secrets.token_hex(8),
            policy={
                "pass_percent": self._settings.pass_percent,
                "pass_operator": self._settings.pass_operator,
                "time_limit_min": policy.time_limit_min,
                "max_attempts": policy.max_attempts,
                "exemption": self._settings.exemption.enabled,
            },
            started_at=now,
            deadline_at=now + timedelta(minutes=policy.time_limit_min),
            preview=caller.preview,
            max_attempts=None if caller.preview else policy.max_attempts,
        )
        if created is None:
            # Either a racing request started one (resume it) or none are left.
            racing = await self._attempts.open_attempt(
                caller.sub, purpose, caller.preview
            )
            if racing:
                return self._started(racing, runtime, now), False
            raise AssessmentConflictError("no_attempts_left")
        METRICS.count("started", purpose, preview=caller.preview)
        log.info(
            "attempt_started attempt=%s purpose=%s preview=%s",
            created.id,
            purpose,
            caller.preview,
        )
        return self._started(created, runtime, now), True

    def _catalog_version(self, form_id: str) -> str:
        found = self._catalog.form(form_id)
        assert found is not None
        return found[0].content_version

    def _started(
        self, attempt: AttemptRecord, runtime: FormRuntime, now: datetime
    ) -> dict[str, Any]:
        return {
            "attempt_id": attempt.id,
            "started_at": attempt.started_at,
            "deadline_at": attempt.deadline_at,
            "server_now": now,
            "sections": section_summaries(runtime.form),
        }

    # --- reading and saving ---------------------------------------------------------

    def _past(self, attempt: AttemptRecord) -> bool:
        return self._clock() >= attempt.deadline_at

    async def _mine(self, attempt_id: uuid.UUID) -> AttemptRecord:
        self._require_enabled()
        attempt = await self._attempts.get(attempt_id)
        if (
            attempt is None
            or attempt.sub != self._caller.sub
            or attempt.preview != self._caller.preview
        ):
            raise TrainerNotFoundError
        if attempt.status == "in_progress" and self._past(attempt):
            attempt = await self._expire(attempt)
        return attempt

    def _runtime_of(self, attempt: AttemptRecord) -> tuple[Pack, FormRuntime]:
        found = self._catalog.form(attempt.form_id)
        if found is None:
            raise TrainerNotFoundError
        pack = found[0]
        return pack, runtime_for(pack, attempt.form_id, ENGLISH)

    async def section(self, attempt_id: uuid.UUID, key: str) -> dict[str, Any]:
        attempt = await self._mine(attempt_id)
        if attempt.status != "in_progress":
            raise AssessmentConflictError(attempt.status)
        pack, runtime = self._runtime_of(attempt)
        if key not in {s.key for s in runtime.form.sections}:
            raise TrainerNotFoundError
        view = section_view(runtime.form, key, attempt.seed, self._audio(pack))
        ids = {i["item_id"] for p in view["parts"] for i in p["items"]}
        saved = await self._attempts.answers(attempt.id)
        view["deadline_at"] = attempt.deadline_at
        view["server_now"] = self._clock()
        view["saved"] = {k: v for k, v in saved.items() if k in ids}
        return view

    async def save(
        self, attempt_id: uuid.UUID, answers: list[tuple[str, dict[str, Any]]]
    ) -> dict[str, Any]:
        attempt = await self._mine(attempt_id)
        if attempt.status != "in_progress":
            raise AssessmentConflictError(attempt.status)
        _, runtime = self._runtime_of(attempt)
        items = runtime.items()
        clean: dict[str, dict[str, Any]] = {}
        for item_id, response in answers:
            if item_id not in items:
                raise TrainerNotFoundError
            clean[item_id] = clean_response(attempt.form_id, items[item_id], response)
        now = self._clock()
        if not await self._attempts.save_answers(attempt.id, clean, now):
            latest = await self._mine(attempt_id)
            raise AssessmentConflictError(latest.status)
        return {"saved_at": now, "server_now": now, "deadline_at": attempt.deadline_at}

    # --- closing ------------------------------------------------------------------

    async def submit(self, attempt_id: uuid.UUID) -> dict[str, Any]:
        attempt = await self._mine(attempt_id)
        if attempt.status == "in_progress":
            attempt = await self._close(attempt, "submitted")
        return await self._result(attempt)

    async def result(self, attempt_id: uuid.UUID) -> dict[str, Any]:
        attempt = await self._mine(attempt_id)
        if attempt.status == "in_progress":
            raise AssessmentConflictError("in_progress")
        return await self._result(attempt)

    async def expire(self, attempt: AttemptRecord) -> AttemptRecord:
        """Closes a past-deadline attempt (the sweeper uses this too)."""
        return await self._close(attempt, "expired")

    async def _expire(self, attempt: AttemptRecord) -> AttemptRecord:
        return await self._close(attempt, "expired")

    async def _close(self, attempt: AttemptRecord, status: str) -> AttemptRecord:
        """Scores what was saved and closes the attempt, once. A request that
        loses the race reads the winner's result."""
        _, runtime = self._runtime_of(attempt)
        saved = await self._attempts.answers(attempt.id)
        scored = score_form(runtime, saved, ENGLISH)
        policy = attempt.policy
        percent = scored.total.percent
        result: dict[str, Any] = {
            "percent_exact": str(percent),
            "percent": decimal_string(percent),
            "passed": passes(percent, policy["pass_percent"], policy["pass_operator"]),
            "band": scored.total.band,
            "policy": dict(policy),
            "sections": [
                {
                    "key": s.key,
                    "points_exact": str(s.points),
                    "points": decimal_string(s.points),
                    "max": s.max,
                    "percent": decimal_string(s.percent),
                    "weight": s.weight,
                }
                for s in scored.total.sections
            ],
            "modules": [
                {"module": m, "percent": decimal_string(p)}
                for m, p in sorted(scored.modules.items())
            ],
            "items": {k: str(v) for k, v in scored.item_points.items()},
        }
        finished = (
            min(self._clock(), attempt.deadline_at)
            if status == "expired"
            else self._clock()
        )
        statement = None
        if not attempt.preview:
            statement = claims_for(
                attempt_id=attempt.id,
                tenant_id=attempt.tenant_id,
                course_id=attempt.moodle_course_id,
                sub=attempt.sub,
                purpose=attempt.purpose,
                ordinal=attempt.ordinal,
                form_id=attempt.form_id,
                content_version=attempt.content_version,
                policy=policy,
                result=result,
                finished_at=finished.isoformat(),
                issuer=self._issuer,
                issued_at=int(self._clock().timestamp()),
            )
        if await self._attempts.close(attempt.id, status, finished, result, statement):
            METRICS.count(status, attempt.purpose, preview=attempt.preview)
            METRICS.count_band(result["band"], preview=attempt.preview)
            log.info(
                "attempt_closed attempt=%s status=%s band=%s passed=%s",
                attempt.id,
                status,
                result["band"],
                result["passed"],
            )
        fresh = await self._attempts.get(attempt.id)
        assert fresh is not None
        return fresh

    async def _result(self, attempt: AttemptRecord) -> dict[str, Any]:
        assert attempt.result is not None
        r = attempt.result
        history = await self._attempts.list_for_user(attempt.sub, attempt.preview)
        mine = [a for a in history if a.purpose == attempt.purpose]
        gain = None
        if attempt.purpose == "final":
            check_in = [a for a in history if a.purpose == "check_in" and a.result]
            if check_in:
                first = _frac(_res(check_in[0])["percent_exact"])
                gain = decimal_string(_frac(r["percent_exact"]) - first)
        return {
            "status": attempt.status,
            "percent": r["percent"],
            "passed": r["passed"],
            "band": r["band"],
            "by_section": [
                {k: s[k] for k in ("key", "points", "max", "percent", "weight")}
                for s in r["sections"]
            ],
            "by_module": r["modules"],
            "attempts": {
                "used": len(mine),
                "max": attempt.policy["max_attempts"],
            },
            "gain": gain,
            "sync": {"state": await self._sync_state(attempt)},
        }

    async def _sync_state(self, attempt: AttemptRecord) -> str:
        """Where the report to Moodle stands: `not_required` (preview),
        `pending`, `synced` or `failed` (refused for good; an operator looks)."""
        found = await self._attempts.sync_states([attempt.id])
        return {
            "pending": "pending",
            "sent": "synced",
            "failed_permanent": "failed",
        }.get(found.get(attempt.id, ""), "not_required")
