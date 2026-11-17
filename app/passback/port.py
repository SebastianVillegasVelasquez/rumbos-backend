"""The port to Moodle for exam results, and a fake for tests and development.

The real HTTP sender is not written yet: the plugin's audit has not recorded
which endpoint receives these statements (decision D13). Until it does, the
worker runs with `passback_sender=none` and rows stay `pending`.
"""

import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol


class Outcome(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"  # Moodle already had this `jti`; as good as accepted
    TRANSIENT = "transient"  # Moodle down or busy: try again later
    PERMANENT = "permanent"  # Moodle refused it for good


@dataclass(frozen=True)
class SendResult:
    outcome: Outcome
    detail: str = ""


class MoodleResultPort(Protocol):
    async def send(
        self, tenant_id: uuid.UUID, course_id: int, statement_jws: str
    ) -> SendResult: ...

    async def known(self, tenant_id: uuid.UUID, course_id: int) -> set[str]:
        """The `jti`s Moodle has recorded for the course, for reconciliation."""
        ...


@dataclass
class FakeMoodleResultPort:
    """Records what it receives. `mode` is what the next sends answer:
    `up`, `down` (transient) or `reject` (permanent)."""

    mode: str = "up"
    received: list[tuple[uuid.UUID, int, str]] = field(default_factory=list)
    jtis: dict[tuple[uuid.UUID, int], set[str]] = field(default_factory=dict)

    async def send(
        self, tenant_id: uuid.UUID, course_id: int, statement_jws: str
    ) -> SendResult:
        import jwt

        if self.mode == "down":
            return SendResult(Outcome.TRANSIENT, "moodle unavailable")
        if self.mode == "reject":
            return SendResult(Outcome.PERMANENT, "refused")
        jti = str(jwt.decode(statement_jws, options={"verify_signature": False})["jti"])
        seen = self.jtis.setdefault((tenant_id, course_id), set())
        self.received.append((tenant_id, course_id, statement_jws))
        if jti in seen:
            return SendResult(Outcome.DUPLICATE)
        seen.add(jti)
        return SendResult(Outcome.ACCEPTED)

    async def known(self, tenant_id: uuid.UUID, course_id: int) -> set[str]:
        return set(self.jtis.get((tenant_id, course_id), set()))
