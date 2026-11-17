"""The practice trainer: catalog, sessions, answers, vocabulary, progress.

Deterministic, no AI. A session is one sitting of one activity: an exercise set
(answer any step, any number of times, in any order) or a scripted dialogue
(one turn at a time). Each answer carries a client-chosen `attempt_id`, so a
retried request gives the same reply and is never counted twice. The summary
counts first tries only, and names the kinds of mistake to revise. Nothing here
returns a key before the student has answered.
"""

import secrets
import uuid
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from app.exceptions import AssessmentConflictError, TrainerNotFoundError
from app.repositories.sqlalchemy.practice_repository import (
    AnswerRecord,
    SessionRecord,
    SqlAlchemyPracticeRepository,
    WordRecord,
)
from app.trainer.grading import advance, grade
from app.trainer.items import GradedItem
from app.trainer.language import ENGLISH
from app.trainer.pack import Activity, Pack
from app.trainer.views import feedback_view, item_view, node_view

Clock = Callable[[], datetime]
# Leitner: days until a word is due again, by the box it is now in (1 to 5).
BOX_DAYS = {1: 1, 2: 2, 3: 4, 4: 8, 5: 16}
MAX_BOX = 5
REPORT_LIMIT = 500


class PracticeService:
    def __init__(
        self,
        repo: SqlAlchemyPracticeRepository,
        pack: Pack | None,
        *,
        course_id: int,
        sub: str,
        clock: Clock,
        serve_drafts: bool,
        audio_url: Callable[[str, str], str],
    ) -> None:
        self._repo = repo
        self._pack = pack
        self._course = course_id
        self._sub = sub
        self._clock = clock
        self._drafts = serve_drafts
        self._audio_url = audio_url

    # --- helpers -----------------------------------------------------------------

    def _need_pack(self) -> Pack:
        if self._pack is None:
            raise TrainerNotFoundError
        return self._pack

    def _activity(self, activity_id: str) -> Activity:
        pack = self._need_pack()
        activity = pack.activities.get(activity_id)
        if activity is None or (activity.status != "published" and not self._drafts):
            raise TrainerNotFoundError
        return activity

    def _audio(self) -> Callable[[str], dict[str, Any]]:
        pack = self._need_pack()

        def ref(audio_id: str) -> dict[str, Any]:
            entry = pack.audio[audio_id]
            return {
                "url": self._audio_url(audio_id, pack.content_version),
                "duration_s": entry.duration_s,
            }

        return ref

    @staticmethod
    def _items(activity: Activity) -> dict[str, GradedItem]:
        return {i.id: i for i in activity.items}

    # --- catalog and progress ------------------------------------------------------

    async def catalog(self) -> dict[str, Any]:
        pack = self._need_pack()
        sessions = await self._repo.sessions_of(self._sub)
        by_activity: dict[str, list[SessionRecord]] = {}
        for s in sessions:
            by_activity.setdefault(s.activity_id, []).append(s)
        served = pack.served_activities(self._drafts)
        vocab_count: Counter[str] = Counter(
            v.lesson_id for v in pack.vocabulary.values()
        )
        modules = []
        for module in pack.curriculum.modules:
            lessons = []
            for lesson in (
                x for x in pack.curriculum.lessons if x.module == module.key
            ):
                lessons.append(
                    {
                        "id": lesson.id,
                        "code": lesson.code,
                        "title": lesson.title,
                        "description": lesson.description,
                        "vocabulary_count": vocab_count[lesson.id],
                        "activities": [
                            self._catalog_activity(a, by_activity.get(a.id, []))
                            for a in served
                            if a.lesson_id == lesson.id
                        ],
                    }
                )
            modules.append(
                {"key": module.key, "title": module.title, "lessons": lessons}
            )
        return {
            "language": pack.meta.language.code,
            "level": pack.meta.level,
            "title": pack.meta.title,
            "content_version": pack.content_version,
            "modules": modules,
        }

    @staticmethod
    def _catalog_activity(a: Activity, sessions: list[SessionRecord]) -> dict[str, Any]:
        done = [s for s in sessions if s.status == "finished" and s.summary]
        best = max((s.summary["percent"] for s in done if s.summary), default=None)
        state = (
            "in_progress"
            if any(s.status == "in_progress" for s in sessions)
            else "finished"
            if done
            else "not_started"
        )
        return {
            "id": a.id,
            "skill": a.skill,
            "title": a.title,
            "estimated_minutes": a.estimated_minutes,
            "step_count": a.step_count,
            "role": a.role,
            "can_do": a.can_do,
            "state": state,
            "best_percent": best,
        }

    async def progress(self) -> dict[str, Any]:
        cat = await self.catalog()
        now = self._clock()
        words = await self._repo.words(self._sub)
        lessons = []
        for module in cat["modules"]:
            for lesson in module["lessons"]:
                acts = lesson["activities"]
                lessons.append(
                    {
                        "lesson_id": lesson["id"],
                        "activities_total": len(acts),
                        "activities_finished": sum(
                            a["state"] == "finished" for a in acts
                        ),
                    }
                )
        return {
            "lessons": lessons,
            "words_saved": len(words),
            "words_due": sum(w.due_at <= now for w in words),
            "words_by_box": {
                str(b): sum(w.box == b for w in words) for b in range(1, MAX_BOX + 1)
            },
        }

    # --- sessions ------------------------------------------------------------------

    async def start(self, activity_id: str) -> tuple[dict[str, Any], bool]:
        """(session view, created). Starting an activity that is already open
        resumes it."""
        activity = self._activity(activity_id)
        pack = self._need_pack()
        existing = await self._repo.open_session(self._sub, activity_id)
        created = False
        if existing is None:
            existing = await self._repo.create_session(
                course_id=self._course,
                sub=self._sub,
                activity_id=activity_id,
                content_version=pack.content_version,
                seed=secrets.token_hex(8),
                state={"node": activity.dialogue.start} if activity.dialogue else {},
                now=self._clock(),
            )
            created = existing is not None
            if existing is None:  # a racing start won
                existing = await self._repo.open_session(self._sub, activity_id)
            assert existing is not None
        return await self._view(existing, activity), created

    async def get(self, session_id: uuid.UUID) -> dict[str, Any]:
        record = await self._mine(session_id)
        return await self._view(record, self._activity(record.activity_id))

    async def _mine(self, session_id: uuid.UUID) -> SessionRecord:
        found = await self._repo.get_session(session_id)
        if found is None or found[1] != self._sub:
            raise TrainerNotFoundError
        return found[0]

    async def _view(self, record: SessionRecord, activity: Activity) -> dict[str, Any]:
        answers = await self._repo.answers(record.id)
        audio = self._audio()
        view: dict[str, Any] = {
            "session_id": record.id,
            "activity_id": activity.id,
            "title": activity.title,
            "status": record.status,
            "kind": "dialogue" if activity.dialogue else "exercise",
            "steps": [],
            "summary": record.summary,
        }
        if record.status != "in_progress":
            return view
        if activity.dialogue:
            node = activity.dialogue.node(record.state.get("node", ""))
            if node:
                view["steps"] = [node_view(node, audio)]
            view["turn"] = len(answers) + 1
            view["turns_total"] = len(activity.dialogue.nodes)
            return view
        first_try = {a.step_id: a for a in reversed(answers)}
        for item in activity.items:
            step = item_view(item, record.seed, audio)
            last = first_try.get(item.id)
            step["answered"] = last is not None
            step["last_outcome"] = last.outcome if last else None
            view["steps"].append(step)
        return view

    # --- answering -----------------------------------------------------------------

    async def answer(
        self,
        session_id: uuid.UUID,
        step_id: str,
        attempt_id: uuid.UUID,
        response: dict[str, Any],
    ) -> dict[str, Any]:
        record = await self._mine(session_id)
        activity = self._activity(record.activity_id)
        if record.status != "in_progress":
            raise AssessmentConflictError("finished")
        if activity.dialogue:
            return await self._answer_turn(
                record, activity, step_id, attempt_id, response
            )
        item = self._items(activity).get(step_id)
        if item is None:
            raise TrainerNotFoundError
        result = grade(item, response, ENGLISH)  # may raise InvalidResponseError
        tag = result.feedback.error_tags[0].tag if result.feedback.error_tags else None
        if result.outcome != "correct" and tag is None:
            tag = f"{item.type}"
        await self._repo.record_answer(
            record.id,
            AnswerRecord(
                step_id,
                attempt_id,
                result.outcome,
                result.earned,
                result.total,
                None if result.outcome == "correct" else tag,
            ),
            self._clock(),
        )
        return {
            "outcome": result.outcome,
            "earned": result.earned,
            "total": result.total,
            "feedback": feedback_view(result.feedback),
            "next": None,
            "finished": False,
        }

    async def _answer_turn(
        self,
        record: SessionRecord,
        activity: Activity,
        step_id: str,
        attempt_id: uuid.UUID,
        response: dict[str, Any],
    ) -> dict[str, Any]:
        assert activity.dialogue is not None
        known = {a.attempt_id for a in await self._repo.answers(record.id)}
        if attempt_id in known:
            # A retry: same reply, nothing counted or advanced twice.
            node = activity.dialogue.node(step_id)
            if node is None:
                raise TrainerNotFoundError
            turn = advance(activity.dialogue, step_id, response, ENGLISH)
            current = activity.dialogue.node(record.state.get("node", ""))
            replay_next = (
                node_view(current, self._audio())
                if current and not turn.finished and turn.outcome == "accepted"
                else None
            )
            return {
                "outcome": turn.outcome,
                "earned": 0,
                "total": 0,
                "feedback": feedback_view(turn.feedback),
                "next": replay_next,
                "finished": turn.finished,
            }
        if step_id != record.state.get("node"):
            raise AssessmentConflictError("not_current_turn")
        turn = advance(activity.dialogue, step_id, response, ENGLISH)
        await self._repo.record_answer(
            record.id,
            AnswerRecord(
                step_id,
                attempt_id,
                turn.outcome,
                int(turn.outcome == "accepted"),
                1,
                None if turn.outcome == "accepted" else "dialogue_reply",
            ),
            self._clock(),
        )
        nxt = None
        if turn.outcome == "accepted" and not turn.finished and turn.next_node_id:
            await self._repo.set_state(record.id, {"node": turn.next_node_id})
            node = activity.dialogue.node(turn.next_node_id)
            nxt = node_view(node, self._audio()) if node else None
        if turn.finished:
            await self._finish(record, activity)
        return {
            "outcome": turn.outcome,
            "earned": int(turn.outcome == "accepted"),
            "total": 1,
            "feedback": feedback_view(turn.feedback),
            "next": nxt,
            "finished": turn.finished,
        }

    # --- finishing -----------------------------------------------------------------

    async def finish(self, session_id: uuid.UUID) -> dict[str, Any]:
        record = await self._mine(session_id)
        activity = self._activity(record.activity_id)
        if record.status == "in_progress":
            await self._finish(record, activity)
            record = await self._mine(session_id)
        return {"session_id": record.id, "summary": record.summary}

    async def _finish(self, record: SessionRecord, activity: Activity) -> None:
        answers = await self._repo.answers(record.id)
        first: dict[str, AnswerRecord] = {}
        for a in answers:
            first.setdefault(a.step_id, a)
        total = (
            len(activity.dialogue.nodes) if activity.dialogue else len(activity.items)
        )
        earned = sum(1 for a in first.values() if a.outcome in ("correct", "accepted"))
        weak = Counter(a.error_tag for a in first.values() if a.error_tag)
        summary = {
            "steps_total": total,
            "steps_answered": len(first),
            "first_try_correct": earned,
            "percent": round(100 * earned / total) if total else 0,
            "to_revise": [tag for tag, _ in weak.most_common(5)],
            "missed_steps": sorted(
                s for s, a in first.items() if a.outcome not in ("correct", "accepted")
            ),
            "tries": len(answers),
        }
        await self._repo.finish(record.id, self._clock(), summary)

    # --- vocabulary ----------------------------------------------------------------

    def _entry(self, entry_id: str) -> Any:
        entry = self._need_pack().vocabulary.get(entry_id)
        if entry is None:
            raise TrainerNotFoundError
        return entry

    def _word_view(self, w: WordRecord, now: datetime) -> dict[str, Any]:
        e = self._entry(w.entry_id)
        return {
            "entry_id": w.entry_id,
            "word": e.word,
            "translation": e.translation,
            "lesson_id": e.lesson_id,
            "box": w.box,
            "due_at": w.due_at,
            "due": w.due_at <= now,
        }

    async def vocabulary(self, lesson_id: str | None) -> list[dict[str, Any]]:
        pack = self._need_pack()
        saved = {w.entry_id: w for w in await self._repo.words(self._sub)}
        return [
            {
                "entry_id": e.id,
                "word": e.word,
                "translation": e.translation,
                "lesson_id": e.lesson_id,
                "saved": e.id in saved,
                "box": saved[e.id].box if e.id in saved else None,
            }
            for e in pack.vocabulary.values()
            if lesson_id is None or e.lesson_id == lesson_id
        ]

    async def saved_words(self, due_only: bool) -> list[dict[str, Any]]:
        now = self._clock()
        words = await self._repo.words(self._sub)
        return [
            self._word_view(w, now) for w in words if not due_only or w.due_at <= now
        ]

    async def save_word(self, entry_id: str) -> dict[str, Any]:
        self._entry(entry_id)
        now = self._clock()
        return self._word_view(
            await self._repo.save_word(self._course, self._sub, entry_id, now), now
        )

    async def forget_word(self, entry_id: str) -> None:
        if not await self._repo.forget_word(self._sub, entry_id):
            raise TrainerNotFoundError

    async def review_word(self, entry_id: str, knew_it: bool) -> dict[str, Any]:
        """Leitner: right moves up a box (to 5), wrong goes back to box 1; the
        next review is due after the new box's interval."""
        self._entry(entry_id)
        now = self._clock()
        current = next(
            (w for w in await self._repo.words(self._sub) if w.entry_id == entry_id),
            None,
        )
        if current is None:
            raise TrainerNotFoundError
        box = min(current.box + 1, MAX_BOX) if knew_it else 1
        updated = await self._repo.review_word(
            self._sub, entry_id, box, now + timedelta(days=BOX_DAYS[box])
        )
        assert updated is not None
        return self._word_view(updated, now)

    # --- reports -------------------------------------------------------------------

    async def report(
        self, activity_id: str, step_id: str | None, text: str
    ) -> uuid.UUID:
        self._activity(activity_id)
        return await self._repo.add_report(
            self._course,
            self._sub,
            activity_id,
            step_id,
            self._need_pack().content_version,
            text.strip()[:REPORT_LIMIT],
        )

    async def erase_me(self) -> dict[str, int]:
        return await self._repo.erase_user(self._sub)
