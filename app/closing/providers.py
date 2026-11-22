"""Where a student's completed activities come from.

Moodle is the source of truth. The port below says what the gate needs; the
real implementation (a live read of Moodle's completion for one user) is not
written yet: it needs a signed Moodle user id from the plugin and a check that
the service account may read other users' completion (see
docs/DECISIONS-trainer.md, D31). Until then the default provider refuses, and
`CLOSING_GATE_MODE=enforce` therefore keeps the final exam closed.
"""

from typing import Protocol

from fastapi import Request


class CompletionUnavailableError(Exception):
    """Completion could not be read (no provider, or Moodle failed). The gate
    treats this as "not completed", never as "completed"."""


class CompletionProvider(Protocol):
    async def completed_cmids(self, *, course_id: int, sub: str) -> frozenset[int]:
        """The course-module ids this user has completed (passed or not failed),
        read fresh. Raises `CompletionUnavailableError`."""
        ...


class UnavailableCompletionProvider:
    async def completed_cmids(self, *, course_id: int, sub: str) -> frozenset[int]:
        raise CompletionUnavailableError("no completion provider is configured")


def get_completion_provider(request: Request) -> CompletionProvider:
    """The app-wide provider; unset, the refusing one."""
    provider: CompletionProvider | None = getattr(
        request.app.state, "completion_provider", None
    )
    return provider or UnavailableCompletionProvider()
